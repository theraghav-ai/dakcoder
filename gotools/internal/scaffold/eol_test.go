package scaffold

import (
	"bytes"
	"os"
	"path/filepath"
	"testing"

	"gitlab.cept.gov.in/it-2.0/dakcoder/gotools/internal/gopatch"
)

// A scaffolder that writes LF into a CRLF repository leaves two conventions in
// one tree. It is invisible until somebody sets core.autocrlf or adds a
// .gitattributes — n-api-template has neither — and then every generated file
// shows as entirely rewritten by a commit that touched none of them.
//
// The asymmetry is what made this worth fixing: modified files already keep
// their endings through gopatch, so one scaffold call produced CRLF for the two
// files it edits and LF for the five it creates.
func TestApplyMatchesTheRepositoryLineEnding(t *testing.T) {
	t.Parallel()

	for _, tc := range []struct {
		name     string
		existing string
		want     gopatch.EOL
	}{
		{"a CRLF repository", "package handler\r\n\r\nfunc New() {}\r\n", gopatch.CRLF},
		{"an LF repository", "package handler\n\nfunc New() {}\n", gopatch.LF},
	} {
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			root := t.TempDir()
			mustWrite(t, filepath.Join(root, "handler", "user.go"), tc.existing)

			res := &Result{}
			res.add("core/domain/pension.go", ActionCreate, "package domain\n\ntype Pension struct{}\n")

			if err := Apply(root, res); err != nil {
				t.Fatalf("apply: %v", err)
			}

			got, err := os.ReadFile(filepath.Join(root, "core", "domain", "pension.go"))
			if err != nil {
				t.Fatal(err)
			}
			if eol := gopatch.DetectEOL(got); eol != tc.want {
				t.Errorf("created file uses %v, repository uses %v", eol, tc.want)
			}
			if crlf := bytes.Count(got, []byte("\r\n")); tc.want == gopatch.CRLF {
				if lf := bytes.Count(got, []byte("\n")) - crlf; lf != 0 {
					t.Errorf("%d bare LF line(s) in a CRLF file", lf)
				}
			}
		})
	}
}

// project_scaffold targets an empty directory. There is no convention to match,
// and LF is the one a new repository should start with.
func TestRepoEOLDefaultsToLFForAnEmptyDirectory(t *testing.T) {
	t.Parallel()
	if got := RepoEOL(t.TempDir()); got != gopatch.LF {
		t.Errorf("RepoEOL on an empty directory = %v, want LF", got)
	}
}

// One file saved by an editor with the wrong setting must not flip the
// convention for everything scaffolded afterwards.
func TestRepoEOLIsDecidedByTheMajority(t *testing.T) {
	t.Parallel()
	root := t.TempDir()
	for _, name := range []string{"a.go", "b.go", "c.go"} {
		mustWrite(t, filepath.Join(root, name), "package p\r\n\r\nvar X = 1\r\n")
	}
	mustWrite(t, filepath.Join(root, "stray.go"), "package p\n\nvar Y = 2\n")

	if got := RepoEOL(root); got != gopatch.CRLF {
		t.Errorf("RepoEOL = %v, want CRLF despite one LF file", got)
	}
}

// Vendored code is somebody else's convention and there is a great deal of it.
// Sampling it would let a vendor directory decide how our files are written.
func TestRepoEOLIgnoresVendoredCode(t *testing.T) {
	t.Parallel()
	root := t.TempDir()
	mustWrite(t, filepath.Join(root, "handler", "user.go"), "package handler\r\n\r\nvar X = 1\r\n")
	for _, name := range []string{"a.go", "b.go", "c.go", "d.go"} {
		mustWrite(t, filepath.Join(root, "vendor", "x", name), "package x\n\nvar Y = 2\n")
	}

	if got := RepoEOL(root); got != gopatch.CRLF {
		t.Errorf("RepoEOL = %v, want CRLF — vendor/ should not have been sampled", got)
	}
}

// Modified files go through gopatch, which preserves their own endings. Apply
// must not second-guess that: the file's existing convention is more specific
// than the repository's.
func TestApplyLeavesModifiedContentAlone(t *testing.T) {
	t.Parallel()
	root := t.TempDir()
	mustWrite(t, filepath.Join(root, "handler", "user.go"), "package handler\r\n")

	patched := "package handler\r\n\r\ntype CreatePensionRequest struct{}\r\n"
	res := &Result{}
	res.add("handler/request.go", ActionModify, patched)

	if err := Apply(root, res); err != nil {
		t.Fatalf("apply: %v", err)
	}
	got, err := os.ReadFile(filepath.Join(root, "handler", "request.go"))
	if err != nil {
		t.Fatal(err)
	}
	if string(got) != patched {
		t.Errorf("modified content was rewritten:\n got %q\nwant %q", got, patched)
	}
}

func mustWrite(t *testing.T, path, content string) {
	t.Helper()
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(path, []byte(content), 0o644); err != nil {
		t.Fatal(err)
	}
}
