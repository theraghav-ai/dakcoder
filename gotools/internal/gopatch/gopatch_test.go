package gopatch

import (
	"strings"
	"testing"
)

func TestDetectAndApplyEOL(t *testing.T) {
	if got := DetectEOL([]byte("a\r\nb\r\n")); got != CRLF {
		t.Errorf("CRLF file detected as %q", got)
	}
	if got := DetectEOL([]byte("a\nb\n")); got != LF {
		t.Errorf("LF file detected as %q", got)
	}
	// A file with no newline at all: LF, which is what a new file gets.
	if got := DetectEOL([]byte("package p")); got != LF {
		t.Errorf("newline-free file detected as %q", got)
	}
	// Mixed, mostly CRLF.
	if got := DetectEOL([]byte("a\r\nb\r\nc\n")); got != CRLF {
		t.Errorf("mostly-CRLF file detected as %q", got)
	}
	if got := string(ApplyEOL([]byte("a\nb\n"), CRLF)); got != "a\r\nb\r\n" {
		t.Errorf("ApplyEOL = %q", got)
	}
}

// TestAppendDeclsPreservesEverythingBeforeIt is the property that makes a
// generated diff reviewable: the existing bytes are untouched, so the diff
// shows the addition and nothing else.
func TestAppendDeclsPreservesEverythingBeforeIt(t *testing.T) {
	src := "package handler\n\n// Existing carries a comment.\ntype Existing struct {\n\tA int `json:\"a\"` // and a trailing one\n}\n"
	out, err := AppendDecls([]byte(src), "type Added struct{ B int }")
	if err != nil {
		t.Fatalf("append: %v", err)
	}
	if !strings.HasPrefix(string(out), src) {
		t.Errorf("the original bytes were modified:\n%s", out)
	}
	if !strings.Contains(string(out), "type Added struct{ B int }") {
		t.Errorf("the declaration was not added:\n%s", out)
	}
	if !strings.Contains(string(out), "// and a trailing one") {
		t.Error("a trailing comment was lost")
	}
}

func TestAppendDeclsPreservesCRLF(t *testing.T) {
	src := "package handler\r\n\r\ntype Existing struct{}\r\n"
	out, err := AppendDecls([]byte(src), "type Added struct{ B int }")
	if err != nil {
		t.Fatalf("append: %v", err)
	}
	if !strings.Contains(string(out), "\r\n") {
		t.Error("CRLF was not preserved")
	}
	if strings.Contains(strings.ReplaceAll(string(out), "\r\n", ""), "\n") {
		t.Errorf("mixed line endings:\n%q", string(out))
	}
}

// TestAppendDeclsRejectsInvalidGo: the fragment is gofmt'd, so a template with
// a missing brace fails here with a position rather than at the next build.
func TestAppendDeclsRejectsInvalidGo(t *testing.T) {
	_, err := AppendDecls([]byte("package p\n"), "type Broken struct {")
	if err == nil {
		t.Fatal("invalid Go should have been rejected")
	}
	if !strings.Contains(err.Error(), "does not parse") {
		t.Errorf("the error should say the source does not parse, got: %v", err)
	}
}

func TestHasDecl(t *testing.T) {
	src := []byte("package p\n\ntype A struct{}\n\nfunc B() {}\n\nfunc (a A) C() {}\n")
	for name, want := range map[string]bool{
		"A": true, "B": true,
		"C": false, // a method, not a top-level declaration
		"D": false,
	} {
		got, err := HasDecl(src, name)
		if err != nil {
			t.Fatalf("HasDecl(%s): %v", name, err)
		}
		if got != want {
			t.Errorf("HasDecl(%s) = %v, want %v", name, got, want)
		}
	}
}

// TestClassifyImportPutsTheModuleFirst guards the ordering bug that would file
// every first-party import under the standard library, because the reference
// module is called `pisapi` and has no dot in it either.
func TestClassifyImportPutsTheModuleFirst(t *testing.T) {
	cases := []struct {
		path, module string
		want         ImportGroup
	}{
		{"context", "pisapi", GroupStd},
		{"pisapi/core/domain", "pisapi", GroupModule},
		{"pisapi", "pisapi", GroupModule},
		{"pisapifoo/bar", "pisapi", GroupStd}, // prefix, not a path prefix
		{"github.com/jackc/pgx/v5", "pisapi", GroupExternal},
		{"gitlab.cept.gov.in/it-2.0/x/handler", "gitlab.cept.gov.in/it-2.0/x", GroupModule},
		{"gitlab.cept.gov.in/it-2.0-common/n-api-db", "gitlab.cept.gov.in/it-2.0/x", GroupExternal},
	}
	for _, tc := range cases {
		if got := ClassifyImport(tc.path, tc.module); got != tc.want {
			t.Errorf("ClassifyImport(%q, %q) = %v, want %v", tc.path, tc.module, got, tc.want)
		}
	}
}

func TestEnsureImportIsIdempotent(t *testing.T) {
	src := []byte("package p\n\nimport (\n\t\"context\"\n)\n")
	out, changed, err := EnsureImport(src, "", "context", "pisapi")
	if err != nil {
		t.Fatalf("ensure: %v", err)
	}
	if changed {
		t.Error("an already-present import was added again")
	}
	if string(out) != string(src) {
		t.Error("an idempotent call modified the file")
	}
}

func TestEnsureImportPlacement(t *testing.T) {
	tests := []struct {
		name         string
		src          string
		alias, path  string
		wantContains string
	}{
		{
			name:         "sorted inside the standard-library group",
			src:          "package p\n\nimport (\n\t\"context\"\n\t\"strings\"\n)\n",
			path:         "os",
			wantContains: "\t\"context\"\n\t\"os\"\n\t\"strings\"\n",
		},
		{
			name:         "new module group between std and external",
			src:          "package p\n\nimport (\n\t\"context\"\n\n\t\"github.com/jackc/pgx/v5\"\n)\n",
			path:         "pisapi/core/port",
			wantContains: "\t\"context\"\n\n\t\"pisapi/core/port\"\n\n\t\"github.com/jackc/pgx/v5\"\n",
		},
		{
			name:         "new std group above an existing module group",
			src:          "package p\n\nimport (\n\t\"pisapi/core/port\"\n)\n",
			path:         "time",
			wantContains: "\t\"time\"\n\n\t\"pisapi/core/port\"\n",
		},
		{
			name:         "aliased import",
			src:          "package p\n\nimport (\n\t\"context\"\n)\n",
			alias:        "dblib",
			path:         "gitlab.cept.gov.in/it-2.0-common/n-api-db",
			wantContains: `dblib "gitlab.cept.gov.in/it-2.0-common/n-api-db"`,
		},
		{
			name:         "no import block at all",
			src:          "package p\n\ntype A struct{}\n",
			path:         "time",
			wantContains: "import (\n\t\"time\"\n)",
		},
		{
			name:         "single unparenthesised import is converted to a block",
			src:          "package p\n\nimport \"context\"\n\ntype A struct{}\n",
			path:         "time",
			wantContains: "import (\n\t\"context\"\n\t\"time\"\n)",
		},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			out, changed, err := EnsureImport([]byte(tc.src), tc.alias, tc.path, "pisapi")
			if err != nil {
				t.Fatalf("ensure: %v", err)
			}
			if !changed {
				t.Fatal("expected a change")
			}
			if !strings.Contains(string(out), tc.wantContains) {
				t.Errorf("got:\n%s\nwant it to contain:\n%s", out, tc.wantContains)
			}
			// Whatever the placement, the result must be gofmt-stable —
			// otherwise the agent's next `gofmt -w` produces a second diff.
			formatted, ferr := Format(out)
			if ferr != nil {
				t.Fatalf("result does not parse: %v", ferr)
			}
			if string(formatted) != string(out) {
				t.Errorf("gofmt would rewrite the result, so the next `gofmt -w` shows a spurious diff:\ngot:\n%s\ngofmt:\n%s", out, formatted)
			}
		})
	}
}

func TestEnsureImportPreservesCRLF(t *testing.T) {
	src := "package p\r\n\r\nimport (\r\n\t\"context\"\r\n)\r\n"
	out, _, err := EnsureImport([]byte(src), "", "time", "pisapi")
	if err != nil {
		t.Fatalf("ensure: %v", err)
	}
	if !strings.Contains(string(out), "\r\n") {
		t.Error("CRLF was not preserved")
	}
	if strings.Contains(strings.ReplaceAll(string(out), "\r\n", ""), "\n") {
		t.Errorf("mixed line endings:\n%q", string(out))
	}
}

func TestFormatReportsThePositionOfBadSource(t *testing.T) {
	_, err := Format([]byte("package p\n\nfunc broken( {\n"))
	if err == nil {
		t.Fatal("expected an error")
	}
	// The numbered listing is what makes a template bug findable.
	if !strings.Contains(err.Error(), "3 |") {
		t.Errorf("the error should include a numbered listing, got: %v", err)
	}
}

func TestFormatFragmentStripsItsWrapper(t *testing.T) {
	out, err := FormatFragment("type A struct{ B int }")
	if err != nil {
		t.Fatalf("fragment: %v", err)
	}
	if strings.Contains(out, "package p") {
		t.Errorf("the wrapper leaked into the fragment: %q", out)
	}
	if !strings.HasPrefix(out, "type A struct") {
		t.Errorf("fragment = %q", out)
	}
}
