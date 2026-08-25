package scaffold

import (
	"io/fs"
	"os"
	"path/filepath"
	"testing"

	"gitlab.cept.gov.in/it-2.0/dakcoder/gotools/internal/spec"
)

// corpusRoot resolves one of the repository's reference corpora, skipping the
// test when it is not present — the scaffolder must remain testable in a
// checkout that does not carry them.
func corpusRoot(t *testing.T, rel string) string {
	t.Helper()
	p, err := filepath.Abs(filepath.Join("..", "..", "..", rel))
	if err != nil {
		t.Skipf("resolve %s: %v", rel, err)
	}
	if _, err := os.Stat(p); err != nil {
		t.Skipf("corpus %s not present; skipping", rel)
	}
	return p
}

// copyTree copies a directory into a fresh temp dir, skipping .git.
//
// Scaffolding is a mutating operation and the corpora are inputs to a dozen
// other tests, so every test that writes works on its own copy.
func copyTree(t *testing.T, src string) string {
	t.Helper()
	dst := t.TempDir()
	err := filepath.WalkDir(src, func(p string, d fs.DirEntry, err error) error {
		if err != nil {
			return err
		}
		rel, rerr := filepath.Rel(src, p)
		if rerr != nil {
			return rerr
		}
		if d.IsDir() {
			if d.Name() == ".git" || d.Name() == ".claude" {
				return fs.SkipDir
			}
			return os.MkdirAll(filepath.Join(dst, rel), 0o755)
		}
		b, rerr := os.ReadFile(p)
		if rerr != nil {
			return rerr
		}
		return os.WriteFile(filepath.Join(dst, rel), b, 0o644)
	})
	if err != nil {
		t.Fatalf("copy %s: %v", src, err)
	}
	return dst
}

// pensionSpec is the worked example from the plan's hand-off envelope
// (Part A §10.1). It is used across the golden, lint and compile tests so that
// one spec exercises every interesting path: an initialism needing correction,
// a float, an enum constraint, a timestamp, and a list filter.
func pensionSpec() spec.Resource {
	return spec.Resource{
		Name: "Pension",
		Fields: []spec.Field{
			{Go: "PpoNumber", Type: "string", Validate: "required", SQL: "varchar(20) NOT NULL"},
			{Go: "Amount", Type: "float64", Validate: "required", SQL: "numeric(12, 2) NOT NULL"},
			{Go: "Status", Type: "string", Validate: "oneof=active suspended closed", SQL: "varchar(16) NOT NULL"},
			{Go: "SanctionDate", Type: "time.Time", Validate: "required"},
		},
		Operations:  []string{"create", "list", "get", "update", "delete"},
		ListFilters: []spec.Filter{{Go: "Status"}},
	}
}
