package libversion

import (
	"context"
	"errors"
	"os"
	"path/filepath"
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
