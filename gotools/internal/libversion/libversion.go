// Package libversion reports how far a service has drifted from the current
// CEPT common libraries.
//
// It exists because the roll-up sheet of the manual review already had two
// columns for this — "Bootstrap library update status (V0.0.36)" and "API
// version update status" — filled in by hand, per service, for 41 services.
//
// # It never updates anything
//
// The template owner was explicit, and the constraint is right: a coding agent
// that bumps a shared library in the middle of a review turns a code review into
// a regression hunt. This package reads, compares and reports. Nothing here
// writes to go.mod and nothing here fails a build.
//
// # Two kinds of drift
//
// A module can be behind its own latest release, and a module can have been
// replaced wholesale. Reporting only the first would call `api-db v1.0.34`
// current — true, and useless, because api-db is the superseded library and the
// useful statement is that migrating to n-api-db is a one-line import change.
package libversion

import (
	"context"
	"fmt"
	"os/exec"
	"sort"
	"strconv"
	"strings"
	"time"

	"gitlab.cept.gov.in/it-2.0/dakcoder/gotools/internal/rules"
	"gitlab.cept.gov.in/it-2.0/dakcoder/gotools/internal/workspace"
)

// ModulePrefix is the namespace the report covers. Third-party modules are
// somebody else's release cadence and are deliberately out of scope.
const ModulePrefix = "gitlab.cept.gov.in/it-2.0-common/"

// Status classifies one dependency.
type Status string

const (
	// StatusCurrent means the newest release of a library that is still current.
	StatusCurrent Status = "current"
	// StatusBehind means a newer release of the same module exists.
	StatusBehind Status = "behind"
	// StatusSuperseded means the module has been replaced by a new generation.
	// It can be both superseded and behind; superseded is the one that matters.
	StatusSuperseded Status = "superseded"
	// StatusUnknown means the registry could not be reached for this module.
	StatusUnknown Status = "unknown"
)

// Report is one dependency's version position.
type Report struct {
	Module       string `json:"module"`
	Current      string `json:"current"`
	Latest       string `json:"latest,omitempty"`
	Behind       int    `json:"behind,omitempty"`
	Status       Status `json:"status"`
	SupersededBy string `json:"superseded_by,omitempty"`
	Note         string `json:"note,omitempty"`
}

// Result is the whole report.
type Result struct {
	Module  string   `json:"module"`
	Reports []Report `json:"reports"`
	// Reachable records whether the registry answered. When it did not, every
	// Latest is empty and the report still lists what is superseded — which is
	// static knowledge and needs no network.
	Reachable bool   `json:"registry_reachable"`
	Error     string `json:"registry_error,omitempty"`
}

// Lister resolves the published versions of a module, newest last.
//
// An interface so the command can be tested without a network, and so a
// deployment that cannot reach the registry can supply a pinned manifest
// instead without the reporting logic changing.
type Lister interface {
	Versions(ctx context.Context, module string) ([]string, error)
}

// GoListLister asks the Go toolchain, which is the right answer here rather
// than a hand-rolled GitLab API client: GOPRIVATE is already set to
// gitlab.cept.gov.in on developer machines, so `go list` resolves these modules
// straight from the VCS using whatever credentials the developer already has.
// No token to provision, and no second code path that can disagree with the
// build.
type GoListLister struct {
	// Dir is the directory to run from, so the module's own GOPRIVATE and
	// GOPROXY settings apply.
	Dir string
	// Timeout bounds one lookup. A registry that hangs must not hang the agent.
	Timeout time.Duration
}

// Versions returns every published version of a module, oldest first.
func (l GoListLister) Versions(ctx context.Context, module string) ([]string, error) {
	timeout := l.Timeout
	if timeout <= 0 {
		timeout = 30 * time.Second
	}
	ctx, cancel := context.WithTimeout(ctx, timeout)
	defer cancel()

	cmd := exec.CommandContext(ctx, "go", "list", "-m", "-versions", module)
	cmd.Dir = l.Dir
	out, err := cmd.Output()
	if err != nil {
		return nil, fmt.Errorf("go list -m -versions %s: %w", module, err)
	}
	fields := strings.Fields(string(out))
	if len(fields) < 2 {
		return nil, nil
	}
	return fields[1:], nil
}

// Check builds the report for a loaded workspace.
//
// A lookup failure for one module is recorded against that module rather than
// failing the run: a partially-answered report is still worth reading, and the
// superseded column does not need the network at all.
func Check(ctx context.Context, ws *workspace.Workspace, l Lister) *Result {
	res := &Result{Module: ws.ModulePath}
	for _, req := range ws.Requires {
		if !strings.HasPrefix(req.Path, ModulePrefix) {
			continue
		}
		r := Report{Module: req.Path, Current: req.Version, Status: StatusCurrent}

		if replacement, ok := rules.SupersededBy(req.Path); ok {
			r.Status = StatusSuperseded
			r.SupersededBy = replacement
			r.Note = "migrating is a one-line import change; the API is identical"
		}

		versions, err := l.Versions(ctx, req.Path)
		switch {
		case err != nil:
			if res.Error == "" {
				res.Error = err.Error()
			}
			if r.Status != StatusSuperseded {
				r.Status = StatusUnknown
			}
		case len(versions) > 0:
			res.Reachable = true
			r.Latest = versions[len(versions)-1]
			if n := behindBy(versions, req.Version); n > 0 {
				r.Behind = n
				if r.Status != StatusSuperseded {
					r.Status = StatusBehind
				}
			}
		}
		res.Reports = append(res.Reports, r)
	}
	sort.SliceStable(res.Reports, func(i, j int) bool {
		if rank(res.Reports[i]) != rank(res.Reports[j]) {
			return rank(res.Reports[i]) < rank(res.Reports[j])
		}
		return res.Reports[i].Module < res.Reports[j].Module
	})
	return res
}

// rank orders the report so the things worth acting on come first.
func rank(r Report) int {
	switch r.Status {
	case StatusSuperseded:
		return 0
	case StatusBehind:
		return 1
	case StatusUnknown:
		return 2
	default:
		return 3
	}
}

// behindBy counts how many releases separate current from the newest.
//
// Counts positions in the published list rather than parsing semver, because
// "7 releases behind" is the number a human wants and it is correct regardless
// of how the project numbers its versions. An unrecognised current version
// yields 0 rather than a guess.
func behindBy(versions []string, current string) int {
	current = strings.TrimSpace(current)
	for i, v := range versions {
		if v == current {
			return len(versions) - 1 - i
		}
	}
	return 0
}

// Behind reports whether anything in the result needs attention, which is what
// a caller wanting a single yes/no should ask.
func (r *Result) Behind() bool {
	for _, rep := range r.Reports {
		if rep.Status == StatusBehind || rep.Status == StatusSuperseded {
			return true
		}
	}
	return false
}

// Summary is a one-line description of the result.
func (r *Result) Summary() string {
	var behind, superseded, unknown int
	for _, rep := range r.Reports {
		switch rep.Status {
		case StatusBehind:
			behind++
		case StatusSuperseded:
			superseded++
		case StatusUnknown:
			unknown++
		}
	}
	parts := []string{strconv.Itoa(len(r.Reports)) + " CEPT module(s)"}
	if superseded > 0 {
		parts = append(parts, strconv.Itoa(superseded)+" superseded")
	}
	if behind > 0 {
		parts = append(parts, strconv.Itoa(behind)+" behind")
	}
	if unknown > 0 {
		parts = append(parts, strconv.Itoa(unknown)+" not resolved")
	}
	if len(parts) == 1 {
		parts = append(parts, "all current")
	}
	return strings.Join(parts, ", ")
}
