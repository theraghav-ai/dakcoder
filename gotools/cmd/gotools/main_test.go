// Tests for the CLI surface.
//
// These were the gap that the initial rules work left open: cmd/gotools was
// verified by running it, which catches what you thought to try and nothing
// else. The exit codes in particular are load-bearing — CI distinguishes "your
// code is wrong" (1) from "the linter is broken" (2), and nothing had ever
// asserted that distinction held.
package main

import (
	"bytes"
	"encoding/json"
	"io"
	"io/fs"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

// exec runs the CLI in-process and returns its exit code and streams.
func exec(t *testing.T, stdin string, args ...string) (code int, stdout, stderr string) {
	t.Helper()
	var out, errBuf bytes.Buffer
	code = run(args, strings.NewReader(stdin), &out, &errBuf)
	return code, out.String(), errBuf.String()
}

func corpus(t *testing.T, rel string) string {
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
		t.Fatalf("copy: %v", err)
	}
	return dst
}

// TestExitCodesDistinguishFindingsFromFailure is the reason this file exists.
//
// A CI job that cannot tell a violation from a broken tool will eventually
// treat both as "red" and then as "flaky", and the gate stops meaning anything.
func TestExitCodesDistinguishFindingsFromFailure(t *testing.T) {
	clean := corpus(t, "new-template")
	legacy := corpus(t, "pao-back-end-development")

	t.Run("clean corpus exits 0", func(t *testing.T) {
		code, _, stderr := exec(t, "", "lint", "--root", clean)
		if code != exitOK {
			t.Errorf("exit = %d, want %d\n%s", code, exitOK, stderr)
		}
	})

	t.Run("violations exit 1", func(t *testing.T) {
		code, _, _ := exec(t, "", "legacy-audit", "--root", legacy, "--quiet")
		if code != exitFindings {
			t.Errorf("exit = %d, want %d", code, exitFindings)
		}
	})

	t.Run("a broken invocation exits 2", func(t *testing.T) {
		for _, args := range [][]string{
			{"lint", "--root", filepath.Join(t.TempDir(), "nope")},
			{"lint", "--only", "no-such-rule", "--root", clean},
			{"lint", "--format", "yaml", "--root", clean},
			{"no-such-command"},
			{},
		} {
			code, _, _ := exec(t, "", args...)
			if code != exitError {
				t.Errorf("%v: exit = %d, want %d", args, code, exitError)
			}
		}
	})
}

func TestUnknownRuleNamesItselfAndPointsAtTheList(t *testing.T) {
	code, _, stderr := exec(t, "", "lint", "--root", corpus(t, "new-template"), "--only", "no-such-rule")
	if code != exitError {
		t.Fatalf("exit = %d, want %d", code, exitError)
	}
	if !strings.Contains(stderr, "no-such-rule") {
		t.Errorf("stderr should name the unknown rule: %s", stderr)
	}
	if !strings.Contains(stderr, "gotools rules") {
		t.Errorf("stderr should point at the rule listing: %s", stderr)
	}
}

func TestLintJSONIsValidAndComplete(t *testing.T) {
	code, stdout, stderr := exec(t, "", "lint", "--root", corpus(t, "new-template"), "--format", "json")
	if code != exitOK {
		t.Fatalf("exit = %d: %s", code, stderr)
	}
	var res struct {
		OK           bool `json:"ok"`
		Count        int  `json:"count"`
		FilesScanned int  `json:"files_scanned"`
		RulesRun     int  `json:"rules_run"`
	}
	if err := json.Unmarshal([]byte(stdout), &res); err != nil {
		t.Fatalf("output is not valid JSON: %v\n%s", err, stdout)
	}
	if !res.OK || res.Count != 0 {
		t.Errorf("reference template should be clean: ok=%v count=%d", res.OK, res.Count)
	}
	if res.FilesScanned == 0 || res.RulesRun == 0 {
		t.Errorf("files=%d rules=%d; both should be non-zero", res.FilesScanned, res.RulesRun)
	}
}

func TestRulesListingCarriesCitations(t *testing.T) {
	code, stdout, stderr := exec(t, "", "rules", "--format", "json")
	if code != exitOK {
		t.Fatalf("exit = %d: %s", code, stderr)
	}
	var all []struct {
		ID       string `json:"id"`
		Severity string `json:"severity"`
		Citation string `json:"citation"`
		Summary  string `json:"summary"`
	}
	if err := json.Unmarshal([]byte(stdout), &all); err != nil {
		t.Fatalf("output is not valid JSON: %v", err)
	}
	if len(all) < 20 {
		t.Fatalf("got %d rules; the suite is larger than that", len(all))
	}
	for _, r := range all {
		if r.Citation == "" {
			t.Errorf("rule %s has no citation; a violation nobody can trace reads as the tool being opinionated", r.ID)
		}
		if r.Summary == "" {
			t.Errorf("rule %s has no summary", r.ID)
		}
		if r.Severity != "error" && r.Severity != "warning" {
			t.Errorf("rule %s has severity %q", r.ID, r.Severity)
		}
		if r.ID == "" {
			t.Error("a rule came back with no id; the JSON field names are a published contract")
		}
	}
}

func TestVersionAndHelp(t *testing.T) {
	code, stdout, _ := exec(t, "", "version")
	if code != exitOK || strings.TrimSpace(stdout) == "" {
		t.Errorf("version: exit=%d out=%q", code, stdout)
	}
	code, stdout, _ = exec(t, "", "help")
	if code != exitOK || !strings.Contains(stdout, "resource-scaffold") ||
		!strings.Contains(stdout, "repo-map") || !strings.Contains(stdout, "doc-check") ||
		!strings.Contains(stdout, "tool-catalog") || !strings.Contains(stdout, "knowledge") {
		t.Errorf("help should list every command: exit=%d\n%s", code, stdout)
	}
}

const pensionSpecJSON = `{
  "name": "Pension",
  "fields": [
    {"go": "PpoNumber", "type": "string", "validate": "required"},
    {"go": "Amount", "type": "float64"}
  ],
  "list_filters": [{"go": "PpoNumber"}]
}`

func TestResourceScaffoldFromStdin(t *testing.T) {
	root := copyTree(t, corpus(t, "new-template"))

	code, stdout, stderr := exec(t, pensionSpecJSON,
		"resource-scaffold", "--root", root, "--spec", "-")
	if code != exitOK {
		t.Fatalf("exit = %d: %s", code, stderr)
	}
	if !strings.Contains(stdout, "wrote 7 file(s)") {
		t.Errorf("unexpected summary:\n%s", stdout)
	}
	// The follow-up steps must be surfaced; a scaffold that silently leaves
	// govalid unrun produces 422s the developer cannot explain.
	if !strings.Contains(stdout, "govalid") {
		t.Errorf("the summary should name the govalid step:\n%s", stdout)
	}
	for _, rel := range []string{
		"core/domain/pension.go", "db/pensions.sql", "repo/postgres/pension.go",
		"handler/pension.go", "handler/response/pension.go",
	} {
		if _, err := os.Stat(filepath.Join(root, filepath.FromSlash(rel))); err != nil {
			t.Errorf("%s was not written: %v", rel, err)
		}
	}
}

func TestResourceScaffoldDryRunWritesNothing(t *testing.T) {
	root := copyTree(t, corpus(t, "new-template"))

	code, stdout, stderr := exec(t, pensionSpecJSON,
		"resource-scaffold", "--root", root, "--spec", "-", "--dry-run")
	if code != exitOK {
		t.Fatalf("exit = %d: %s", code, stderr)
	}
	if !strings.Contains(stdout, "would write") {
		t.Errorf("a dry run should say so:\n%s", stdout)
	}
	if _, err := os.Stat(filepath.Join(root, "core", "domain", "pension.go")); err == nil {
		t.Error("the dry run wrote a file")
	}
}

// TestInvalidSpecListsEveryProblemWithAFix: the caller is usually a model with
// one turn to correct itself, so the output has to be a work-list.
func TestInvalidSpecListsEveryProblemWithAFix(t *testing.T) {
	root := copyTree(t, corpus(t, "new-template"))
	bad := `{"name":"Pension","fields":[
	  {"go":"Amount","type":"decimal.Decimal"},
	  {"go":"ID","type":"int64"}
	],"operations":["frobnicate"]}`

	code, _, stderr := exec(t, bad, "resource-scaffold", "--root", root, "--spec", "-")
	if code != exitError {
		t.Fatalf("exit = %d, want %d", code, exitError)
	}
	for _, want := range []string{"decimal.Decimal", "float64", "frobnicate", "fix:"} {
		if !strings.Contains(stderr, want) {
			t.Errorf("stderr should mention %q:\n%s", want, stderr)
		}
	}
}

func TestUnknownSpecFieldIsRefused(t *testing.T) {
	root := copyTree(t, corpus(t, "new-template"))
	// A caller that believes it asked for soft deletes should be told we did
	// not hear it, rather than quietly getting a resource without them.
	bad := `{"name":"Pension","soft_delete":true,"fields":[{"go":"Amount","type":"float64"}]}`

	code, _, stderr := exec(t, bad, "resource-scaffold", "--root", root, "--spec", "-")
	if code != exitError {
		t.Fatalf("exit = %d, want %d", code, exitError)
	}
	if !strings.Contains(stderr, "soft_delete") {
		t.Errorf("stderr should name the unrecognised field:\n%s", stderr)
	}
}

func TestScaffoldRequiresASpec(t *testing.T) {
	for _, cmd := range []string{"resource-scaffold", "project-scaffold"} {
		code, _, stderr := exec(t, "", cmd, "--root", t.TempDir())
		if code != exitError {
			t.Errorf("%s without --spec: exit = %d, want %d", cmd, code, exitError)
		}
		if !strings.Contains(stderr, "--spec") {
			t.Errorf("%s should say --spec is required: %s", cmd, stderr)
		}
	}
}

func TestProjectScaffoldFromFile(t *testing.T) {
	root := t.TempDir()
	specPath := filepath.Join(t.TempDir(), "service.json")
	body := `{
	  "project": {"module": "gitlab.cept.gov.in/it-2.0/pension-api"},
	  "resource": ` + pensionSpecJSON + `
	}`
	if err := os.WriteFile(specPath, []byte(body), 0o644); err != nil {
		t.Fatal(err)
	}

	code, stdout, stderr := exec(t, "", "project-scaffold", "--root", root, "--spec", specPath)
	if code != exitOK {
		t.Fatalf("exit = %d: %s", code, stderr)
	}
	if !strings.Contains(stdout, "go.mod") {
		t.Errorf("summary should list go.mod:\n%s", stdout)
	}
	for _, rel := range []string{"go.mod", "main.go", "bootstrap/bootstrapper.go", "configs/config.yaml", "README.md"} {
		if _, err := os.Stat(filepath.Join(root, filepath.FromSlash(rel))); err != nil {
			t.Errorf("%s was not written: %v", rel, err)
		}
	}

	// And the scaffolded service must pass its own linter.
	code, _, stderr = exec(t, "", "lint", "--root", root)
	if code != exitOK {
		t.Errorf("the scaffolded service does not lint clean: exit=%d\n%s", code, stderr)
	}
}

func TestFxWireCLI(t *testing.T) {
	root := copyTree(t, corpus(t, "new-template"))

	code, stdout, stderr := exec(t, "", "fx-wire", "--root", root,
		"--kind", "handler", "--ctor", "NewPensionHandler", "--dry-run")
	if code != exitOK {
		t.Fatalf("dry run exit = %d: %s", code, stderr)
	}
	if !strings.Contains(stdout, "fx.ResultTags(serverHandler.ServerControllersGroupTag)") {
		t.Errorf("the dry run should show the annotated registration:\n%s", stdout)
	}

	code, _, stderr = exec(t, "", "fx-wire", "--root", root,
		"--kind", "handler", "--ctor", "NewPensionHandler")
	if code != exitOK {
		t.Fatalf("apply exit = %d: %s", code, stderr)
	}

	code, stdout, _ = exec(t, "", "fx-wire", "--root", root,
		"--kind", "handler", "--ctor", "NewPensionHandler")
	if code != exitOK || !strings.Contains(stdout, "already registers") {
		t.Errorf("a repeat call should be a reported no-op: exit=%d\n%s", code, stdout)
	}
}

func TestFxWireRequiresItsArguments(t *testing.T) {
	root := copyTree(t, corpus(t, "new-template"))
	for _, args := range [][]string{
		{"fx-wire", "--root", root},
		{"fx-wire", "--root", root, "--kind", "handler"},
		{"fx-wire", "--root", root, "--ctor", "NewXHandler"},
		{"fx-wire", "--root", root, "--kind", "service", "--ctor", "NewXHandler"},
		{"fx-wire", "--root", root, "--kind", "handler", "--ctor", "not-an-identifier"},
	} {
		code, _, _ := exec(t, "", args...)
		if code != exitError {
			t.Errorf("%v: exit = %d, want %d", args[1:], code, exitError)
		}
	}
}

// TestScopedLintDoesNotBlockOnPreExistingViolations mirrors the discipline the
// agent depends on: a lint scoped to the files it touched must not fail because
// of legacy elsewhere in the repository.
func TestScopedLintDoesNotBlockOnPreExistingViolations(t *testing.T) {
	root := copyTree(t, corpus(t, "new-template"))
	bad := "package handler\n\nimport \"github.com/gin-gonic/gin\"\n\ntype LegacyHandler struct{}\n\nfunc (h *LegacyHandler) Get(c *gin.Context) {}\n"
	if err := os.WriteFile(filepath.Join(root, "handler", "legacy.go"), []byte(bad), 0o644); err != nil {
		t.Fatal(err)
	}

	code, _, _ := exec(t, "", "lint", "--root", root)
	if code != exitFindings {
		t.Fatalf("an unscoped lint should report the new violations: exit = %d", code)
	}

	code, stdout, _ := exec(t, "", "lint", "--root", root, "--paths", "handler/user.go")
	if code != exitOK {
		t.Errorf("a lint scoped to an untouched file should pass: exit = %d\n%s", code, stdout)
	}
	if !strings.Contains(stdout, "outside the requested scope") {
		t.Errorf("out-of-scope findings should still be summarised:\n%s", stdout)
	}
}

var _ io.Reader = (*bytes.Buffer)(nil)

func TestRepoMapCLI(t *testing.T) {
	clean := corpus(t, "new-template")

	code, stdout, stderr := exec(t, "", "repo-map", "--root", clean)
	if code != exitOK {
		t.Fatalf("exit = %d: %s", code, stderr)
	}
	for _, want := range []string{"module pisapi", "n-api generation", "repo/postgres", "UserHandler", "fx:"} {
		if !strings.Contains(stdout, want) {
			t.Errorf("output should mention %q:\n%s", want, stdout)
		}
	}

	code, stdout, stderr = exec(t, "", "repo-map", "--root", clean, "--format", "json")
	if code != exitOK {
		t.Fatalf("json exit = %d: %s", code, stderr)
	}
	var m struct {
		Module     string `json:"module"`
		Generation string `json:"generation"`
		EstTokens  int    `json:"est_tokens"`
		Packages   []struct {
			Dir string `json:"dir"`
		} `json:"packages"`
	}
	if err := json.Unmarshal([]byte(stdout), &m); err != nil {
		t.Fatalf("output is not valid JSON: %v", err)
	}
	if m.Module != "pisapi" || m.Generation != "n-api" || len(m.Packages) == 0 {
		t.Errorf("unexpected map: %+v", m)
	}
	if m.EstTokens == 0 || m.EstTokens > 4000 {
		t.Errorf("est_tokens = %d; the budget is what makes this tool sendable every turn", m.EstTokens)
	}

	// Narrowing is what the elision hint tells the agent to call.
	code, stdout, _ = exec(t, "", "repo-map", "--root", clean, "--package", "core/domain")
	if code != exitOK || !strings.Contains(stdout, "core/domain") || strings.Contains(stdout, "repo/postgres ") {
		t.Errorf("narrowing did not restrict the map:\n%s", stdout)
	}
}

// TestRepoMapWorksOnABrokenBuild is the reason it is not built on go/packages:
// the Planner runs it first, often on a service the developer has just
// described as not working.
func TestRepoMapWorksOnABrokenBuild(t *testing.T) {
	root := copyTree(t, corpus(t, "new-template"))
	broken := "package handler\n\nfunc Broken() { this is not go }\n"
	if err := os.WriteFile(filepath.Join(root, "handler", "broken.go"), []byte(broken), 0o644); err != nil {
		t.Fatal(err)
	}

	code, stdout, stderr := exec(t, "", "repo-map", "--root", root)
	if code != exitOK {
		t.Fatalf("repo-map must survive a file that does not parse: exit=%d\n%s", code, stderr)
	}
	if !strings.Contains(stdout, "UserHandler") {
		t.Errorf("the parseable files' symbols were lost:\n%s", stdout)
	}
}

func TestRepoMapRejectsABadFormat(t *testing.T) {
	code, _, stderr := exec(t, "", "repo-map", "--root", corpus(t, "new-template"), "--format", "yaml")
	if code != exitError {
		t.Errorf("exit = %d, want %d", code, exitError)
	}
	if !strings.Contains(stderr, "format") {
		t.Errorf("stderr should name the bad flag: %s", stderr)
	}
}

// TestDocCheckCLI covers the §14.4 coupling: every rule cites a section of
// skill.md or SOP.md, and a citation that names nothing is a violation message
// a developer cannot trace.
func TestDocCheckCLI(t *testing.T) {
	docs := corpus(t, "new-template")

	code, stdout, stderr := exec(t, "", "doc-check", "--docs", docs, "--manifest", filepath.Join(t.TempDir(), "none.json"))
	if code != exitOK {
		t.Fatalf("every shipped citation should resolve: exit=%d\n%s%s", code, stdout, stderr)
	}
	if !strings.Contains(stdout, "citation(s) resolved") {
		t.Errorf("unexpected output:\n%s", stdout)
	}
}

// TestDocCheckFailsOnDrift: renaming a cited heading has to be loud, and the
// report has to name both the heading that vanished and the rules that dangle.
func TestDocCheckFailsOnDrift(t *testing.T) {
	docs := copyTree(t, corpus(t, "new-template"))
	manifest := filepath.Join(t.TempDir(), "pin.json")

	code, _, stderr := exec(t, "", "doc-check", "--docs", docs, "--manifest", manifest, "--update")
	if code != exitOK {
		t.Fatalf("pinning should succeed: exit=%d\n%s", code, stderr)
	}

	skillPath := filepath.Join(docs, "skill.md")
	body, err := os.ReadFile(skillPath)
	if err != nil {
		t.Fatal(err)
	}
	edited := strings.Replace(string(body), "## Repository Pattern", "## Repository Patterns", 1)
	if edited == string(body) {
		t.Skip("the corpus no longer has a '## Repository Pattern' heading to rename")
	}
	if err := os.WriteFile(skillPath, []byte(edited), 0o644); err != nil {
		t.Fatal(err)
	}

	code, stdout, _ := exec(t, "", "doc-check", "--docs", docs, "--manifest", manifest)
	if code != exitFindings {
		t.Fatalf("a renamed cited heading must fail: exit=%d\n%s", code, stdout)
	}
	for _, want := range []string{"Repository Pattern", "drift", "doc-check --update", "repo-contract"} {
		if !strings.Contains(stdout, want) {
			t.Errorf("the report should mention %q:\n%s", want, stdout)
		}
	}
}

func TestDocCheckRejectsABadFormat(t *testing.T) {
	code, _, stderr := exec(t, "", "doc-check", "--docs", corpus(t, "new-template"), "--format", "yaml")
	if code != exitError {
		t.Errorf("exit = %d, want %d", code, exitError)
	}
	if !strings.Contains(stderr, "format") {
		t.Errorf("stderr should name the bad flag: %s", stderr)
	}
}

// TestToolCatalogCLI covers contract C1's published half.
func TestToolCatalogCLI(t *testing.T) {
	out := t.TempDir()

	code, stdout, stderr := exec(t, "", "tool-catalog", "--out", out)
	if code != exitOK {
		t.Fatalf("exit = %d: %s", code, stderr)
	}
	if !strings.Contains(stdout, "contract C1 conformant") {
		t.Errorf("unexpected output:\n%s", stdout)
	}
	for _, name := range []string{"TOOL-CATALOG.md", "tool-catalog.json"} {
		if _, err := os.Stat(filepath.Join(out, name)); err != nil {
			t.Errorf("%s was not written: %v", name, err)
		}
	}

	// Freshly written, so the freshness check must pass.
	if code, _, stderr = exec(t, "", "tool-catalog", "--out", out, "--check"); code != exitOK {
		t.Errorf("a just-written catalogue reports stale: exit=%d\n%s", code, stderr)
	}

	// And it must notice when it is not.
	stale := filepath.Join(out, "tool-catalog.json")
	if err := os.WriteFile(stale, []byte("{}\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	code, _, stderr = exec(t, "", "tool-catalog", "--out", out, "--check")
	if code != exitFindings {
		t.Errorf("a stale catalogue should fail: exit=%d", code)
	}
	if !strings.Contains(stderr, "make tool-catalog") {
		t.Errorf("the failure should say how to fix it: %s", stderr)
	}
}

// TestCommittedCatalogIsCurrent is the repository's own freshness check, so a
// tool added without regenerating fails here rather than in CI.
//
// The path is relative to the module root rather than to this package, because
// `go test` runs each package in its own directory and the committed catalogue
// lives beside go.mod.
func TestCommittedCatalogIsCurrent(t *testing.T) {
	docs := filepath.Join("..", "..", "docs")
	if _, err := os.Stat(filepath.Join(docs, "tool-catalog.json")); err != nil {
		t.Skipf("no committed catalogue at %s; run `make tool-catalog`", docs)
	}
	code, _, stderr := exec(t, "", "tool-catalog", "--out", docs, "--check")
	if code != exitOK {
		t.Errorf("the committed catalogue is stale — run `make tool-catalog`:\n%s", stderr)
	}
}

// TestKnowledgeCLI covers the progressive-disclosure knowledge base (§14.2).
func TestKnowledgeCLI(t *testing.T) {
	docs := corpus(t, "new-template")
	out := t.TempDir()

	code, stdout, stderr := exec(t, "", "knowledge", "--docs", docs, "--out", out)
	if code != exitOK {
		t.Fatalf("exit = %d: %s", code, stderr)
	}
	if !strings.Contains(stdout, "SKILL.md") {
		t.Errorf("unexpected output:\n%s", stdout)
	}
	for _, rel := range []string{
		"SKILL.md",
		"references/handler-pattern.md",
		"references/repository-pattern.md",
		"references/config-keys.md",
	} {
		if _, err := os.Stat(filepath.Join(out, filepath.FromSlash(rel))); err != nil {
			t.Errorf("%s was not written: %v", rel, err)
		}
	}

	if code, _, stderr = exec(t, "", "knowledge", "--docs", docs, "--out", out, "--check"); code != exitOK {
		t.Errorf("a just-written knowledge base reports stale: exit=%d\n%s", code, stderr)
	}

	// And it must notice when a reference is edited by hand, which is the way
	// a generated file actually rots.
	edited := filepath.Join(out, "references", "repository-pattern.md")
	if err := os.WriteFile(edited, []byte("# hand-edited\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	code, _, stderr = exec(t, "", "knowledge", "--docs", docs, "--out", out, "--check")
	if code != exitFindings {
		t.Errorf("a hand-edited reference should fail the check: exit=%d", code)
	}
	if !strings.Contains(stderr, "make knowledge") {
		t.Errorf("the failure should say how to fix it: %s", stderr)
	}
}

// TestCommittedKnowledgeIsCurrent is the repository's own freshness check.
func TestCommittedKnowledgeIsCurrent(t *testing.T) {
	kbRoot := filepath.Join("..", "..", "..", "packages", "knowledge")
	if _, err := os.Stat(filepath.Join(kbRoot, "SKILL.md")); err != nil {
		t.Skipf("no committed knowledge base at %s; run `make knowledge`", kbRoot)
	}
	code, _, stderr := exec(t, "", "knowledge",
		"--docs", corpus(t, "new-template"), "--out", kbRoot, "--check")
	if code != exitOK {
		t.Errorf("the committed knowledge base is stale — run `make knowledge`:\n%s", stderr)
	}
}
