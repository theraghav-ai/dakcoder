package scaffold

import (
	"fmt"
	"os"
	"path/filepath"
	"regexp"
	"strings"

	"gitlab.cept.gov.in/it-2.0/dakcoder/gotools/internal/naming"
	"gitlab.cept.gov.in/it-2.0/dakcoder/gotools/internal/spec"
)

// Versions pins the direct dependencies a scaffolded service starts from.
//
// Pinned rather than left to `go mod tidy` to resolve, because a greenfield
// service that silently picks up whatever is latest is a service whose first
// build is not reproducible — and because the api-* generation is still
// published alongside the n-api-* one, so "latest" is genuinely ambiguous.
//
// These are the versions the reference template is built against. Bumping them
// is a deliberate act with a golden-snapshot diff to review, which is the point.
type Versions struct {
	Squirrel     string
	Pgx          string
	Config       string
	Bootstrapper string
	DBLib        string
	Log          string
	Server       string
	Validation   string
	Fx           string
}

// DefaultVersions matches new-template/go.mod as of the reference snapshot.
func DefaultVersions() Versions {
	return Versions{
		Squirrel:     "v1.5.4",
		Pgx:          "v5.7.6",
		Config:       "v0.0.17",
		Bootstrapper: "v0.0.14",
		DBLib:        "v0.0.1",
		Log:          "v0.0.1",
		Server:       "v0.0.17",
		Validation:   "v0.0.3",
		Fx:           "v1.24.0",
	}
}

// Environments are the config files a service ships with, matching the
// reference template's set.
var Environments = []string{"dev", "test", "sit", "staging", "training", "prod"}

// Project is the greenfield service specification.
type Project struct {
	// Module is the Go module path, e.g. gitlab.cept.gov.in/it-2.0/pension-api.
	Module string `json:"module" jsonschema:"go module path, e.g. gitlab.cept.gov.in/it-2.0/pension-api"`
	// AppName is the short service name used in config and metrics.
	AppName string `json:"app_name,omitempty" jsonschema:"short service name; derived from the module when omitted"`
	// Title and Description appear in the generated OpenAPI document.
	Title       string `json:"title,omitempty" jsonschema:"human title for the swagger document"`
	Description string `json:"description,omitempty" jsonschema:"one-line description for the swagger document"`
	// GoVersion is the go directive.
	GoVersion string `json:"go_version,omitempty" jsonschema:"go directive, e.g. 1.25.0"`
	// Addr is the listen address.
	Addr string `json:"addr,omitempty" jsonschema:"listen address, e.g. :8080"`

	// Env is set per config file during rendering; it is not caller input.
	Env string `json:"-"`
	// Ver carries the pinned dependency versions.
	Ver Versions `json:"-"`
}

// BinaryName is the default output binary, used by .gitignore.
func (p Project) BinaryName() string {
	if i := strings.LastIndex(p.Module, "/"); i >= 0 {
		return p.Module[i+1:]
	}
	return p.Module
}

var modulePathRe = regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9._~/-]*$`)

// Normalise fills in derived defaults and validates the module path.
func (p Project) Normalise() (Project, error) {
	out := p
	out.Module = strings.Trim(strings.TrimSpace(p.Module), "/")
	if out.Module == "" {
		return out, &spec.InvalidSpecError{Issues: []spec.Issue{{
			Path: "module", Message: "module path is empty",
			Fix: "supply a module path, e.g. gitlab.cept.gov.in/it-2.0/pension-api",
		}}}
	}
	if !modulePathRe.MatchString(out.Module) {
		return out, &spec.InvalidSpecError{Issues: []spec.Issue{{
			Path:    "module",
			Message: fmt.Sprintf("module path %q contains characters that are not valid in a Go module path", p.Module),
			Fix:     "use letters, digits, dots, dashes, underscores and slashes only",
		}}}
	}

	if out.AppName == "" {
		out.AppName = naming.Kebab(out.BinaryName())
	}
	if out.Title == "" {
		// naming.Title upper-cases known initialisms, so a module called
		// pension-api already ends in "API"; appending another would give
		// "Pension API API" in the swagger document.
		out.Title = naming.Title(out.BinaryName())
		if !strings.HasSuffix(strings.ToUpper(out.Title), "API") {
			out.Title += " API"
		}
	}
	if out.Description == "" {
		out.Description = "IT 2.0 service scaffolded from the n-api-template contract."
	}
	if out.GoVersion == "" {
		out.GoVersion = "1.25.0"
	}
	if out.Addr == "" {
		out.Addr = ":8080"
	}
	if !strings.HasPrefix(out.Addr, ":") {
		out.Addr = ":" + out.Addr
	}
	if out.Ver == (Versions{}) {
		out.Ver = DefaultVersions()
	}
	return out, nil
}

// NewProject lays down a complete, buildable service around one working
// resource.
//
// Producing a service that does not serve anything would be a worse starting
// point than an empty directory: the first thing a developer does with a new
// service is run it, and a skeleton that starts and answers a real route is the
// difference between "this works" and "what am I missing?". So the greenfield
// path always includes a resource, and it is the same resource scaffolder the
// brownfield path uses — one code path, one golden snapshot, no second
// implementation to drift.
func NewProject(root string, p Project, r spec.Resource) (*Result, error) {
	project, err := p.Normalise()
	if err != nil {
		return nil, err
	}
	normalised, err := r.Normalise()
	if err != nil {
		return nil, err
	}

	if err := assertEmptyEnough(root); err != nil {
		return nil, err
	}

	res := &Result{Module: project.Module}
	d := data{R: normalised, P: project, Module: project.Module, Layout: spec.TimestampLayout}

	// go.mod — first, because everything else imports against this path.
	content, err := render("go.mod.tmpl", d)
	if err != nil {
		return nil, err
	}
	res.add("go.mod", ActionCreate, content)

	// main.go
	d.Imports = renderImports(project.Module, []imp{
		{Path: "context"},
		{Path: project.Module + "/bootstrap"},
		{Alias: "bootstrapper", Path: pkgBootstrapper},
	})
	content, err = renderGo("main.go.tmpl", d)
	if err != nil {
		return nil, err
	}
	res.add("main.go", ActionCreate, content)

	// bootstrap/bootstrapper.go — pre-wired with the seed resource, so the
	// service serves on first run rather than after a manual edit.
	d.Imports = renderImports(project.Module, []imp{
		{Alias: "handler", Path: project.Module + "/handler"},
		{Alias: "repo", Path: project.Module + "/repo/postgres"},
		{Alias: "serverHandler", Path: pkgServerHandler},
		{Path: pkgFx},
	})
	content, err = renderGo("bootstrapper.go.tmpl", d)
	if err != nil {
		return nil, err
	}
	res.add("bootstrap/bootstrapper.go", ActionCreate, content)

	// core/port — the shared envelopes, verbatim from the reference.
	for name, path := range map[string]string{
		"port_request.go.tmpl":  "core/port/request.go",
		"port_response.go.tmpl": "core/port/response.go",
	} {
		content, err = renderGo(name, d)
		if err != nil {
			return nil, err
		}
		res.add(path, ActionCreate, content)
	}

	// configs/*.yaml — every credential field empty. The reference template
	// commits a MinIO key pair, an Aadhaar client secret and a database
	// password; reproducing that in every new service would turn one incident
	// into a pattern.
	base := project
	base.Env = "local"
	d.P = base
	content, err = render("config.yaml.tmpl", d)
	if err != nil {
		return nil, err
	}
	res.add("configs/config.yaml", ActionCreate, content)

	for _, env := range Environments {
		envProject := project
		envProject.Env = env
		d.P = envProject
		content, err = render("config.yaml.tmpl", d)
		if err != nil {
			return nil, err
		}
		res.add("configs/config."+env+".yaml", ActionCreate, content)
	}
	d.P = project

	// The seed resource, through the same code path the brownfield tool uses:
	// one implementation, one golden snapshot, no second template set to drift.
	//
	// Module is supplied because go.mod is not on disk yet, and FX wiring is
	// skipped because the bootstrapper above is already wired to this resource.
	seed, err := Resource(root, normalised, ResourceOptions{
		Module:     project.Module,
		SkipFXWire: true,
	})
	if err != nil {
		return nil, fmt.Errorf("seed resource: %w", err)
	}
	for _, f := range seed.Files {
		res.add(f.Path, ActionCreate, f.Content)
	}

	content, err = render("gitignore.tmpl", d)
	if err != nil {
		return nil, err
	}
	res.add(".gitignore", ActionCreate, content)

	// The static-analysis profile for the service.
	//
	// Shipped with the scaffold rather than left to the developer because it is
	// the cheapest half of the code review: three of the reviewers' standing
	// suggestions — error handling, resource management and concurrency safety
	// — are enforced entirely by configuration here, with no rule of our own to
	// write or maintain. Neither the reference template nor any reviewed
	// service had one, so nothing was checking them.
	content, err = render("golangci.yml.tmpl", d)
	if err != nil {
		return nil, err
	}
	res.add(".golangci.yml", ActionCreate, content)

	content, err = render("README.md.tmpl", d)
	if err != nil {
		return nil, err
	}
	res.add("README.md", ActionCreate, content)

	res.note("run `go env -w GOPRIVATE=gitlab.cept.gov.in/*` then `go mod tidy` — go.mod is pinned but there is no go.sum yet")
	res.note("apply db/%s.sql to your database", normalised.TableFileStem())
	res.note("run `govalid ./request.go` from handler/ — without it every non-GET route returns 422")
	res.note("configs/*.yaml ship with empty credentials by design; supply them from your secret store, never by committing them")
	res.note("verify with: go build ./... && gotools lint")
	return res, nil
}

// assertEmptyEnough refuses to scaffold over an existing service.
//
// A greenfield scaffold that lands in a populated directory is close to
// unrecoverable: it writes twenty files, and telling which were already there
// afterwards is guesswork. The presence of go.mod is the honest signal.
func assertEmptyEnough(root string) error {
	if _, err := os.Stat(filepath.Join(root, "go.mod")); err == nil {
		return fmt.Errorf(
			"%s already contains a go.mod; project_scaffold creates a new service and will not scaffold over an existing one — use resource_scaffold to add to it",
			root)
	}
	return nil
}
