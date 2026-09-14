package libversion

import (
	"context"
	"errors"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"gitlab.cept.gov.in/it-2.0/dakcoder/gotools/internal/workspace"
)

// fakeLister answers from a table, so the report logic is tested without a
// network and without the registry's availability deciding whether CI passes.
type fakeLister struct {
	versions map[string][]string
	err      error
}

func (f fakeLister) Versions(_ context.Context, module string) ([]string, error) {
	if f.err != nil {
		return nil, f.err
	}
	return f.versions[module], nil
}

func loadModule(t *testing.T, gomod string) *workspace.Workspace {
	t.Helper()
	root := t.TempDir()
	if err := os.WriteFile(filepath.Join(root, "go.mod"), []byte(gomod), 0o644); err != nil {
		t.Fatalf("write go.mod: %v", err)
	}
	ws, err := workspace.Load(root)
	if err != nil {
		t.Fatalf("load: %v", err)
	}
	return ws
}

const gomod = `module pisapi

go 1.25.0

require (
	gitlab.cept.gov.in/it-2.0-common/api-db v1.0.32
	gitlab.cept.gov.in/it-2.0-common/api-config v0.0.17
	gitlab.cept.gov.in/it-2.0-common/n-api-server v0.0.17
	github.com/jackc/pgx/v5 v5.7.6
)
`

func TestCheckClassifiesBothKindsOfDrift(t *testing.T) {
	ws := loadModule(t, gomod)
	res := Check(context.Background(), ws, fakeLister{versions: map[string][]string{
		"gitlab.cept.gov.in/it-2.0-common/api-db":       {"v1.0.32", "v1.0.33", "v1.0.34"},
		"gitlab.cept.gov.in/it-2.0-common/api-config":   {"v0.0.16", "v0.0.17"},
		"gitlab.cept.gov.in/it-2.0-common/n-api-server": {"v0.0.17", "v0.0.18"},
	}})

	byModule := map[string]Report{}
	for _, r := range res.Reports {
		byModule[r.Module] = r
	}

	// Third-party modules are somebody else's release cadence.
	if len(res.Reports) != 3 {
		t.Fatalf("reported %d modules, want the 3 CEPT ones only", len(res.Reports))
	}

	// api-db is both behind and superseded. Superseded is what matters: being
	// on the newest release of a replaced library is true and useless.
	db := byModule["gitlab.cept.gov.in/it-2.0-common/api-db"]
	if db.Status != StatusSuperseded {
		t.Errorf("api-db status = %s, want superseded", db.Status)
	}
	if db.SupersededBy != "gitlab.cept.gov.in/it-2.0-common/n-api-db" {
		t.Errorf("api-db superseded_by = %q", db.SupersededBy)
	}
	if db.Behind != 2 {
		t.Errorf("api-db behind = %d, want 2", db.Behind)
	}

	// api-config has no n- successor, so it is current rather than legacy.
	// Reporting it as a migration target would send people after a module that
	// does not exist.
	if cfg := byModule["gitlab.cept.gov.in/it-2.0-common/api-config"]; cfg.Status != StatusCurrent {
		t.Errorf("api-config status = %s, want current — n-api-config does not exist", cfg.Status)
	}

	if srv := byModule["gitlab.cept.gov.in/it-2.0-common/n-api-server"]; srv.Status != StatusBehind || srv.Behind != 1 {
		t.Errorf("n-api-server = %s behind %d, want behind 1", srv.Status, srv.Behind)
	}

	// Superseded sorts first, because it is the finding worth acting on.
	if res.Reports[0].Status != StatusSuperseded {
		t.Errorf("first report is %s, want the superseded one first", res.Reports[0].Status)
	}
}

// TestCheckSurvivesAnUnreachableRegistry: the superseded column is static
// knowledge, so an offline run still answers the more important question.
func TestCheckSurvivesAnUnreachableRegistry(t *testing.T) {
	ws := loadModule(t, gomod)
	res := Check(context.Background(), ws, fakeLister{err: errors.New("dial tcp: no route to host")})

	if res.Reachable {
		t.Error("registry should be reported unreachable")
	}
	if res.Error == "" {
		t.Error("the reason should be carried, so the user knows it was not 'all current'")
	}
	var superseded bool
	for _, r := range res.Reports {
		if r.Status == StatusSuperseded {
			superseded = true
		}
		if r.Latest != "" {
			t.Errorf("%s reported a latest version with no registry", r.Module)
		}
	}
	if !superseded {
		t.Error("supersession needs no network and must still be reported")
	}
}

func TestBehindByCountsReleasesNotSemver(t *testing.T) {
	versions := []string{"v0.0.1", "v0.0.2", "v0.0.3", "v0.0.8"}
	if got := behindBy(versions, "v0.0.1"); got != 3 {
		t.Errorf("behindBy = %d, want 3 releases", got)
	}
	if got := behindBy(versions, "v0.0.8"); got != 0 {
		t.Errorf("newest should be 0 behind, got %d", got)
	}
	// An unrecognised version yields 0 rather than a guess: reporting a made-up
	// distance is worse than reporting none.
	if got := behindBy(versions, "v9.9.9"); got != 0 {
		t.Errorf("unknown version should yield 0, got %d", got)
	}
}

// The replacement's own version is the one fact a conversion needs from this
// report, and it was the one fact the report did not carry.
//
// It said "api-db v1.0.32 is superseded by n-api-db" and resolved versions for
// api-db alone. A field run filled the gap the only way left to it: it carried
// v1.0.32 across the rename. The two libraries are separate release lines —
// api-db is at v1.0.32, n-api-db's tags stop at v0.0.1 — so go.mod ended up
// asserting six revisions that had never existed, every fetch after it failed
// with `unknown revision`, and the run reported the cause as missing GitLab
// credentials on a machine whose credentials were fine.
func TestSupersededReportsTheReplacementsLatestVersion(t *testing.T) {
	ws := loadModule(t, gomod)
	res := Check(context.Background(), ws, fakeLister{versions: map[string][]string{
		"gitlab.cept.gov.in/it-2.0-common/api-db":     {"v1.0.30", "v1.0.31", "v1.0.32"},
		"gitlab.cept.gov.in/it-2.0-common/n-api-db":   {"v0.0.1"},
		"gitlab.cept.gov.in/it-2.0-common/api-config": {"v0.0.17"},
	}})

	var found bool
	for _, r := range res.Reports {
		if r.Module != "gitlab.cept.gov.in/it-2.0-common/api-db" {
			continue
		}
		found = true
		if r.SupersededBy != "gitlab.cept.gov.in/it-2.0-common/n-api-db" {
			t.Fatalf("superseded_by = %q", r.SupersededBy)
		}
		if r.SupersededByLatest != "v0.0.1" {
			t.Fatalf("superseded_by_latest = %q, want v0.0.1 — the replacement's line, not this one's", r.SupersededByLatest)
		}
		if r.Current == r.SupersededByLatest {
			t.Fatal("the two release lines must not be reported as one")
		}
		if !strings.Contains(r.Note, "v0.0.1") || !strings.Contains(r.Note, "carrying") {
			t.Fatalf("the note does not say which version to fetch: %q", r.Note)
		}
	}
	if !found {
		t.Fatal("api-db was not reported at all")
	}
}

// One lookup per replacement, not one per module that names it: a lookup is a
// process launch against a remote VCS, and a migration's go.mod names several
// modules from the same generation.
func TestTheReplacementIsResolvedOncePerModule(t *testing.T) {
	ws := loadModule(t, `module pisapi

go 1.25.0

require (
	gitlab.cept.gov.in/it-2.0-common/api-db v1.0.32
	gitlab.cept.gov.in/it-2.0-common/api-log v1.1.5
)
`)
	counting := &countingLister{inner: fakeLister{versions: map[string][]string{
		"gitlab.cept.gov.in/it-2.0-common/n-api-db":  {"v0.0.1"},
		"gitlab.cept.gov.in/it-2.0-common/n-api-log": {"v0.0.1"},
	}}}
	Check(context.Background(), ws, counting)

	for module, n := range counting.seen {
		if n > 1 {
			t.Fatalf("%s was resolved %d times", module, n)
		}
	}
}

type countingLister struct {
	inner fakeLister
	seen  map[string]int
}

func (c *countingLister) Versions(ctx context.Context, module string) ([]string, error) {
	if c.seen == nil {
		c.seen = map[string]int{}
	}
	c.seen[module]++
	return c.inner.Versions(ctx, module)
}

// The superseded column is static knowledge and survives a registry that cannot
// be reached; the replacement's version simply goes unstated rather than the
// whole row being lost.
func TestAnUnreachableRegistryStillNamesTheReplacement(t *testing.T) {
	ws := loadModule(t, gomod)
	res := Check(context.Background(), ws, fakeLister{err: errors.New("dial tcp: no route to host")})

	for _, r := range res.Reports {
		if r.Module != "gitlab.cept.gov.in/it-2.0-common/api-db" {
			continue
		}
		if r.SupersededBy == "" {
			t.Fatal("the replacement's name needs no network")
		}
		if r.SupersededByLatest != "" {
			t.Fatalf("invented a version off a failed lookup: %q", r.SupersededByLatest)
		}
	}
}

// A partial answer must be distinguishable from a complete one.
//
// `Reachable` is "did anything answer", and one success used to be enough to
// withhold the caller's only warning. A field run got a report whose first
// lookup landed and whose next thirteen were cancelled by a shared deadline,
// rendered as four blank version columns with nothing saying why — read the
// blanks as "no versions published", asked the developer which versions to use,
// was told "the latest", and asked the identical question twice more.
func TestAPartialReportSaysWhatItCouldNotAnswer(t *testing.T) {
	ws := loadModule(t, gomod)
	res := Check(context.Background(), ws, selectiveLister{answers: map[string][]string{
		// Only one lookup lands, as in the field run.
		"gitlab.cept.gov.in/it-2.0-common/api-db": {"v1.0.32"},
	}})

	if !res.Reachable {
		t.Fatal("one lookup answered, so the registry was reachable")
	}
	if len(res.Unresolved) == 0 {
		t.Fatal("thirteen lookups did not answer and the report claims none are missing")
	}
	var sawReplacement bool
	for _, m := range res.Unresolved {
		if m == "gitlab.cept.gov.in/it-2.0-common/n-api-db" {
			sawReplacement = true
		}
	}
	if !sawReplacement {
		t.Fatalf("a replacement whose lookup failed is not listed: %v", res.Unresolved)
	}
}

// The blank column carries its own instruction, because the correct move never
// needed the number.
func TestAnUnresolvedReplacementSaysToFetchWithoutAVersion(t *testing.T) {
	ws := loadModule(t, gomod)
	res := Check(context.Background(), ws, selectiveLister{answers: map[string][]string{}})

	for _, r := range res.Reports {
		if r.SupersededBy == "" || r.SupersededByLatest != "" {
			continue
		}
		if !strings.Contains(r.Note, "could not be looked up") {
			t.Fatalf("a blank version reads as 'none published': %q", r.Note)
		}
		if !strings.Contains(r.Note, "no version") {
			t.Fatalf("the note does not name the move that works: %q", r.Note)
		}
		return
	}
	t.Fatal("no superseded module had an unresolved replacement")
}

func TestUnresolvedIsDeduplicated(t *testing.T) {
	ws := loadModule(t, `module pisapi

go 1.25.0

require (
	gitlab.cept.gov.in/it-2.0-common/api-db v1.0.32
	gitlab.cept.gov.in/it-2.0-common/api-log v1.1.5
)
`)
	res := Check(context.Background(), ws, selectiveLister{answers: map[string][]string{}})
	seen := map[string]int{}
	for _, m := range res.Unresolved {
		seen[m]++
		if seen[m] > 1 {
			t.Fatalf("%s listed %d times", m, seen[m])
		}
	}
}

// A report that answered everything claims nothing is missing.
func TestACompleteReportHasNothingUnresolved(t *testing.T) {
	ws := loadModule(t, gomod)
	res := Check(context.Background(), ws, everythingLister{})
	if len(res.Unresolved) != 0 {
		t.Fatalf("a complete report lists %v as unresolved", res.Unresolved)
	}
}

// selectiveLister answers for the modules in its table and fails for the rest,
// which is what a shared deadline expiring part-way through looks like.
type selectiveLister struct{ answers map[string][]string }

func (s selectiveLister) Versions(_ context.Context, module string) ([]string, error) {
	if versions, ok := s.answers[module]; ok {
		return versions, nil
	}
	return nil, errors.New("context deadline exceeded")
}

// everythingLister answers every module with one version.
type everythingLister struct{}

func (everythingLister) Versions(_ context.Context, module string) ([]string, error) {
	return []string{"v9.9.9"}, nil
}
