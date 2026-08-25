package scaffold

import (
	"flag"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"testing"
)

// update regenerates the golden files:
//
//	go test ./internal/scaffold/ -update
//
// Review the resulting diff. A golden test whose snapshots are refreshed
// without being read is a test that asserts nothing.
var update = flag.Bool("update", false, "rewrite the golden files")

// TestGoldenResource pins resource_scaffold's output byte for byte.
//
// This is the cheapest possible detector of template drift (plan §20.2). The
// scaffolder is deterministic precisely so that this test can exist: any change
// to a template, to the naming rules, to the import grouping, or to the
// reference template these files are patched into shows up here as a reviewable
// diff rather than as a surprise in someone's merge request three weeks later.
//
// It also pins the two files the scaffolder *modifies*. Those snapshots carry
// the reference template's own content, which means this test additionally
// fails when new-template changes underneath us — which is the coupling
// §14.4 asks for, not an accident.
func TestGoldenResource(t *testing.T) {
	root := copyTree(t, corpusRoot(t, "new-template"))
	res, err := Resource(root, pensionSpec(), ResourceOptions{})
	if err != nil {
		t.Fatalf("scaffold: %v", err)
	}
	compareGolden(t, "resource", res)
}

// TestGoldenProject pins project_scaffold's output, including the config files
// — which is where an accidentally reintroduced credential would show up.
func TestGoldenProject(t *testing.T) {
	root := t.TempDir()
	res, err := NewProject(root, Project{
		Module:      "gitlab.cept.gov.in/it-2.0/pension-api",
		Description: "Pension disbursement API.",
	}, pensionSpec())
	if err != nil {
		t.Fatalf("project scaffold: %v", err)
	}
	compareGolden(t, "project", res)
}

// TestGoldenConfigsCarryNoCredentials is a standing assertion rather than a
// snapshot comparison, because the failure it guards against is one a reviewer
// approving a golden diff could easily wave through.
//
// The reference template commits a MinIO access/secret pair, an Aadhaar client
// secret and a database password (plan.md §6). Those values must never be
// reproduced by a tool that writes twenty files at a time.
func TestGoldenConfigsCarryNoCredentials(t *testing.T) {
	root := t.TempDir()
	res, err := NewProject(root, Project{Module: "pensionapi"}, pensionSpec())
	if err != nil {
		t.Fatalf("project scaffold: %v", err)
	}

	// Fragments taken from the reference template's committed configs. Kept as
	// short prefixes so this test does not itself become a place credentials
	// live.
	leaks := []string{"ncMdhCXFXoq8", "uMfja1erWdKp", "DOPAUAUVeduks", "Cept@123", "AXSDHMSyOz7c"}

	for _, f := range res.Files {
		if !strings.HasPrefix(f.Path, "configs/") {
			continue
		}
		for _, leak := range leaks {
			if strings.Contains(f.Content, leak) {
				t.Errorf("%s reproduces a credential from the reference template", f.Path)
			}
		}
		for _, key := range []string{"password:", "secretKey:", "redispassword:"} {
			if i := strings.Index(f.Content, key); i >= 0 {
				line := f.Content[i:]
				if j := strings.IndexByte(line, '\n'); j >= 0 {
					line = line[:j]
				}
				value := strings.TrimSpace(strings.TrimPrefix(line, key))
				value = strings.TrimSpace(strings.SplitN(value, "#", 2)[0])
				if value != "" && value != `""` {
					t.Errorf("%s sets %s to %q; scaffolded configs must ship empty", f.Path, key, value)
				}
			}
		}
	}
}

// compareGolden diffs a result against testdata/golden/<name>/.
func compareGolden(t *testing.T, name string, res *Result) {
	t.Helper()
	dir := filepath.Join("testdata", "golden", name)

	if *update {
		if err := os.RemoveAll(dir); err != nil {
			t.Fatalf("clear %s: %v", dir, err)
		}
		for _, f := range res.Files {
			p := filepath.Join(dir, filepath.FromSlash(goldenName(f.Path)))
			if err := os.MkdirAll(filepath.Dir(p), 0o755); err != nil {
				t.Fatalf("mkdir: %v", err)
			}
			if err := os.WriteFile(p, []byte(f.Content), 0o644); err != nil {
				t.Fatalf("write %s: %v", p, err)
			}
		}
		t.Logf("wrote %d golden file(s) to %s — review the diff", len(res.Files), dir)
		return
	}

	want, err := readGolden(dir)
	if err != nil {
		t.Fatalf("read golden (run `go test ./internal/scaffold/ -update` to create them): %v", err)
	}

	got := map[string]string{}
	for _, f := range res.Files {
		got[goldenName(f.Path)] = f.Content
	}

	for _, key := range sortedKeys(want) {
		g, ok := got[key]
		if !ok {
			t.Errorf("golden has %s but the scaffold no longer produces it", key)
			continue
		}
		if g != want[key] {
			t.Errorf("%s differs from its golden snapshot\n%s", key, firstDifference(want[key], g))
		}
	}
	for _, key := range sortedKeys(got) {
		if _, ok := want[key]; !ok {
			t.Errorf("scaffold produces %s, which has no golden snapshot", key)
		}
	}
}

// goldenName maps a workspace-relative path to a flat golden file name, so the
// snapshot directory reads as a manifest rather than a tree.
//
// Two rewrites, both because a snapshot that keeps its original extension is a
// file other tools believe they own:
//
//   - The trailing ".golden" hides these from gofmt. Two of them are
//     deliberately CRLF, because the reference template is, and a stray
//     `gofmt -w ./...` rewrites them to LF — which broke the golden tests twice
//     during development before this rename. The .gitattributes and Makefile
//     exclusions still exist, but the naming is the defence that does not
//     depend on anyone reading them.
//   - The leading dot is dropped, because a snapshot literally named .gitignore
//     is a working .gitignore inside testdata, quietly excluding whatever it
//     happens to match from the repository.
func goldenName(path string) string {
	name := strings.ReplaceAll(path, "/", "__")
	if strings.HasPrefix(name, ".") {
		name = "dot" + name
	}
	return name + ".golden"
}

func readGolden(dir string) (map[string]string, error) {
	entries, err := os.ReadDir(dir)
	if err != nil {
		return nil, err
	}
	out := map[string]string{}
	for _, e := range entries {
		if e.IsDir() {
			continue
		}
		b, rerr := os.ReadFile(filepath.Join(dir, e.Name()))
		if rerr != nil {
			return nil, rerr
		}
		out[e.Name()] = string(b)
	}
	return out, nil
}

func sortedKeys(m map[string]string) []string {
	out := make([]string, 0, len(m))
	for k := range m {
		out = append(out, k)
	}
	sort.Strings(out)
	return out
}

// firstDifference reports the first differing line with a little context, which
// is far more useful in a test log than two thousand lines of file.
func firstDifference(want, got string) string {
	w := strings.Split(strings.ReplaceAll(want, "\r\n", "\n"), "\n")
	g := strings.Split(strings.ReplaceAll(got, "\r\n", "\n"), "\n")
	for i := range max(len(w), len(g)) {
		wl, gl := "", ""
		if i < len(w) {
			wl = w[i]
		}
		if i < len(g) {
			gl = g[i]
		}
		if wl != gl {
			var b strings.Builder
			b.WriteString("  first difference at line ")
			b.WriteString(itoa(i + 1))
			b.WriteString(":\n")
			for j := max(0, i-2); j < min(len(w), i+3); j++ {
				b.WriteString("    want | " + w[j] + "\n")
			}
			b.WriteString("    ---\n")
			for j := max(0, i-2); j < min(len(g), i+3); j++ {
				b.WriteString("    got  | " + g[j] + "\n")
			}
			return b.String()
		}
	}
	if want != got {
		return "  content differs only in line endings"
	}
	return ""
}

func itoa(n int) string {
	if n == 0 {
		return "0"
	}
	var b []byte
	for n > 0 {
		b = append([]byte{byte('0' + n%10)}, b...)
		n /= 10
	}
	return string(b)
}
