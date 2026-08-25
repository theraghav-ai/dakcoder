package scaffold

import (
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"

	"gitlab.cept.gov.in/it-2.0/dakcoder/gotools/internal/rules"
	"gitlab.cept.gov.in/it-2.0/dakcoder/gotools/internal/spec"
)

// TestScaffoldedResourceIsLintClean is the load-bearing assertion of this
// package, and the counterpart to rules' TestReferenceTemplateIsClean.
//
// The scaffolder and the linter are two halves of one contract. If the
// scaffolder emits code its own linter rejects, the agent's inner loop opens
// with a violation the agent did not cause and cannot fix, and it will spend
// turns trying. Either the template is wrong or the rule is — this test does
// not care which, only that they agree.
func TestScaffoldedResourceIsLintClean(t *testing.T) {
	root := copyTree(t, corpusRoot(t, "new-template"))

	res, err := Resource(root, pensionSpec(), ResourceOptions{})
	if err != nil {
		t.Fatalf("scaffold: %v", err)
	}
	if err := Apply(root, res); err != nil {
		t.Fatalf("apply: %v", err)
	}

	out, err := rules.Analyze(root, rules.RunOptions{})
	if err != nil {
		t.Fatalf("analyze: %v", err)
	}
	if !out.OK {
		t.Errorf("scaffolded resource produced %d violation(s)", out.Count)
		for _, v := range out.Violations {
			t.Errorf("  %s", v)
		}
	}
	for _, w := range out.Warnings {
		t.Logf("warning: %s", w)
	}
}

// TestScaffoldedResourceCompiles is the only check that proves the templates
// name real functions with real signatures.
//
// rules_lint is syntax-only by design (see internal/workspace), so it cannot
// tell that dblib.InsertReturning takes four arguments or that a time.Time
// field needs the time import — both of which the templates got wrong on their
// first draft. Only the compiler knows.
//
// It needs the private gitlab.cept.gov.in modules resolved, so it verifies the
// pristine copy builds first and skips rather than fails when it does not. A
// test that fails because the developer is offline teaches people to ignore
// failures.
func TestScaffoldedResourceCompiles(t *testing.T) {
	if testing.Short() {
		t.Skip("-short: skipping the compile check")
	}
	root := copyTree(t, corpusRoot(t, "new-template"))

	if out, err := goBuild(t, root); err != nil {
		t.Skipf("reference template does not build in this environment, so a "+
			"failure here would not be about the scaffolder; skipping.\n%s", out)
	}

	res, err := Resource(root, pensionSpec(), ResourceOptions{})
	if err != nil {
		t.Fatalf("scaffold: %v", err)
	}
	if err := Apply(root, res); err != nil {
		t.Fatalf("apply: %v", err)
	}

	if out, err := goBuild(t, root); err != nil {
		t.Fatalf("scaffolded resource does not compile: %v\n%s", err, out)
	}
	if out, err := goVet(t, root); err != nil {
		t.Fatalf("scaffolded resource fails go vet: %v\n%s", err, out)
	}
}

// TestEveryFieldTypeCompiles walks the whole permitted type set through the
// templates and past the compiler.
//
// The bool case is the one that needed thinking about: a false bool in a
// value-typed request DTO is indistinguishable from an omitted one, so the
// update path has no zero-value test to emit and writes the column
// unconditionally. That is a different code shape from every other type, and it
// is the shape a template conditional most easily gets wrong.
func TestEveryFieldTypeCompiles(t *testing.T) {
	if testing.Short() {
		t.Skip("-short: skipping the compile check")
	}
	root := copyTree(t, corpusRoot(t, "new-template"))
	if out, err := goBuild(t, root); err != nil {
		t.Skipf("reference template does not build in this environment; skipping.\n%s", out)
	}

	s := spec.Resource{
		Name: "Sample",
		Fields: []spec.Field{
			{Go: "Label", Type: "string"},
			{Go: "Count", Type: "int"},
			{Go: "Reference", Type: "int64"},
			{Go: "Amount", Type: "float64"},
			{Go: "Active", Type: "bool"},
			{Go: "OccurredAt", Type: "time.Time"},
		},
		// A filter on each type a filter is allowed to have. bool is refused by
		// the spec — see TestBoolFiltersAreRejected — so it is absent here.
		ListFilters: []spec.Filter{{Go: "Label"}, {Go: "Count"}, {Go: "OccurredAt"}},
	}
	res, err := Resource(root, s, ResourceOptions{})
	if err != nil {
		t.Fatalf("scaffold: %v", err)
	}
	if err := Apply(root, res); err != nil {
		t.Fatalf("apply: %v", err)
	}

	if out, err := goBuild(t, root); err != nil {
		t.Fatalf("a resource using every permitted type does not compile: %v\n%s", err, out)
	}
	if out, err := goVet(t, root); err != nil {
		t.Fatalf("go vet: %v\n%s", err, out)
	}

	lintOut, err := rules.Analyze(root, rules.RunOptions{})
	if err != nil {
		t.Fatalf("analyze: %v", err)
	}
	if !lintOut.OK {
		for _, v := range lintOut.Violations {
			t.Errorf("  %s", v)
		}
		t.Fatalf("%d violation(s)", lintOut.Count)
	}

	// The bool has to be written on every update, and it must not have picked
	// up a zero-value guard that would make `false` unsettable.
	handler := fileNamed(t, res, "handler/sample.go")
	if !strings.Contains(handler, "active = &req.Active") {
		t.Error("the bool field is not written on update")
	}
	if strings.Contains(handler, "if req.Active") {
		t.Error("the bool field has a zero-value guard, so it could never be set to false")
	}
}

// TestProjectScaffoldCompilesAndLints does the same for the greenfield path.
//
// A greenfield scaffold that does not build is worse than no scaffold at all:
// the developer's first act is to run it, and twenty generated files is a large
// surface to debug before writing a line of their own.
func TestProjectScaffoldCompilesAndLints(t *testing.T) {
	if testing.Short() {
		t.Skip("-short: skipping the compile check")
	}
	root := t.TempDir()

	res, err := NewProject(root, Project{Module: "pensionapi"}, pensionSpec())
	if err != nil {
		t.Fatalf("project scaffold: %v", err)
	}
	if err := Apply(root, res); err != nil {
		t.Fatalf("apply: %v", err)
	}

	lintOut, err := rules.Analyze(root, rules.RunOptions{})
	if err != nil {
		t.Fatalf("analyze: %v", err)
	}
	if !lintOut.OK {
		t.Errorf("scaffolded project produced %d violation(s)", lintOut.Count)
		for _, v := range lintOut.Violations {
			t.Errorf("  %s", v)
		}
	}

	// go.mod carries no go.sum, so `go mod tidy` has to run before a build can
	// resolve anything. Both need the private module host; skip when it is not
	// reachable rather than reporting a network failure as a template defect.
	if out, err := run(t, root, "go", "mod", "tidy"); err != nil {
		t.Skipf("go mod tidy could not resolve the private modules here; skipping the build.\n%s", out)
	}
	if out, err := goBuild(t, root); err != nil {
		t.Fatalf("scaffolded project does not compile: %v\n%s", err, out)
	}
}

// TestRescaffoldingIsRefusedNotDuplicated: running the same scaffold twice must
// not append a second copy of every DTO. An agent that retries a tool call —
// which it does, on a timeout — would otherwise corrupt request.go silently.
func TestRescaffoldingIsRefusedNotDuplicated(t *testing.T) {
	root := copyTree(t, corpusRoot(t, "new-template"))

	res, err := Resource(root, pensionSpec(), ResourceOptions{})
	if err != nil {
		t.Fatalf("first scaffold: %v", err)
	}
	if err := Apply(root, res); err != nil {
		t.Fatalf("apply: %v", err)
	}

	if _, err = Resource(root, pensionSpec(), ResourceOptions{}); err == nil {
		t.Fatal("second scaffold should have been refused")
	} else if !strings.Contains(err.Error(), "already declares") {
		t.Errorf("error should explain the duplicate declaration, got: %v", err)
	}
}

// TestApplyRefusesToOverwrite: create means create.
func TestApplyRefusesToOverwrite(t *testing.T) {
	root := copyTree(t, corpusRoot(t, "new-template"))
	res, err := Resource(root, pensionSpec(), ResourceOptions{})
	if err != nil {
		t.Fatalf("scaffold: %v", err)
	}
	if err := Apply(root, res); err != nil {
		t.Fatalf("first apply: %v", err)
	}
	err = Apply(root, res)
	if err == nil {
		t.Fatal("expected a refusal on the second apply")
	}
	var exists *ExistsError
	if !asExists(err, &exists) {
		t.Fatalf("want *ExistsError, got %T: %v", err, err)
	}
	if len(exists.Paths) == 0 {
		t.Error("the refusal should name the offending paths")
	}
}

// TestProjectScaffoldRefusesPopulatedDirectory guards the least recoverable
// mistake this tool could make.
func TestProjectScaffoldRefusesPopulatedDirectory(t *testing.T) {
	root := t.TempDir()
	if err := os.WriteFile(filepath.Join(root, "go.mod"), []byte("module existing\n\ngo 1.25.0\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	if _, err := NewProject(root, Project{Module: "newthing"}, pensionSpec()); err == nil {
		t.Fatal("expected a refusal to scaffold over an existing module")
	}
}

// TestScaffoldIsDeterministic: the same spec must produce identical bytes.
// Determinism is what makes golden snapshots meaningful and what lets the agent
// diff two runs; map iteration order is the usual way it is lost.
func TestScaffoldIsDeterministic(t *testing.T) {
	root := copyTree(t, corpusRoot(t, "new-template"))
	first, err := Resource(root, pensionSpec(), ResourceOptions{})
	if err != nil {
		t.Fatalf("scaffold: %v", err)
	}
	for i := range 5 {
		next, err := Resource(root, pensionSpec(), ResourceOptions{})
		if err != nil {
			t.Fatalf("scaffold %d: %v", i, err)
		}
		if len(next.Files) != len(first.Files) {
			t.Fatalf("run %d produced %d files, want %d", i, len(next.Files), len(first.Files))
		}
		for j := range next.Files {
			if next.Files[j].Path != first.Files[j].Path {
				t.Fatalf("run %d file %d is %s, want %s", i, j, next.Files[j].Path, first.Files[j].Path)
			}
			if next.Files[j].Content != first.Files[j].Content {
				t.Errorf("run %d differs in %s", i, next.Files[j].Path)
			}
		}
	}
}

// TestOperationSubsetsRenderValidGo: every subset of the CRUD operations has to
// produce a file that parses, because a spec asking for a read-only resource is
// entirely reasonable and is the case where conditional templates break.
func TestOperationSubsetsRenderValidGo(t *testing.T) {
	root := copyTree(t, corpusRoot(t, "new-template"))
	subsets := [][]string{
		{"create"},
		{"list"},
		{"get"},
		{"update"},
		{"delete"},
		{"list", "get"},
		{"create", "get"},
		{"create", "list", "get", "update", "delete"},
	}
	for _, ops := range subsets {
		t.Run(strings.Join(ops, "+"), func(t *testing.T) {
			s := pensionSpec()
			s.Operations = ops
			// The filter only makes sense with a list; drop it otherwise so the
			// spec itself is valid.
			if !contains(ops, "list") {
				s.ListFilters = nil
			}
			// renderGo gofmts every Go file, so reaching here at all proves the
			// output parses.
			if _, err := Resource(root, s, ResourceOptions{}); err != nil {
				t.Fatalf("ops %v: %v", ops, err)
			}
		})
	}
}

// TestListWithoutFiltersMatchesTheReferenceShape pins the two decisions that
// keep a plain list byte-identical in shape to the shipped ListUsers: the
// `_ struct{}` parameter, and the hand-written Validate() that govalid will not
// generate for a struct with no tags of its own.
func TestListWithoutFiltersMatchesTheReferenceShape(t *testing.T) {
	root := copyTree(t, corpusRoot(t, "new-template"))
	s := pensionSpec()
	s.ListFilters = nil
	s.Paginate = false

	res, err := Resource(root, s, ResourceOptions{})
	if err != nil {
		t.Fatalf("scaffold: %v", err)
	}
	handler := fileNamed(t, res, "handler/pension.go")
	if !strings.Contains(handler, "ListPensions(sctx *serverRoute.Context, _ struct{})") {
		t.Error("an unfiltered list should take _ struct{}, as the reference ListUsers does")
	}
	request := fileNamed(t, res, "handler/request.go")
	if !strings.Contains(request, "func (p *ListPensionsParams) Validate() error") {
		t.Error("ListPensionsParams with no tagged fields needs a hand-written Validate()")
	}
}

// TestListWithFiltersOmitsTheManualValidate is the other half, and the one that
// would be a compile error if it were wrong: with a tagged filter field govalid
// generates Validate(), so emitting one here too is a duplicate method.
func TestListWithFiltersOmitsTheManualValidate(t *testing.T) {
	root := copyTree(t, corpusRoot(t, "new-template"))

	res, err := Resource(root, pensionSpec(), ResourceOptions{})
	if err != nil {
		t.Fatalf("scaffold: %v", err)
	}
	request := fileNamed(t, res, "handler/request.go")
	if strings.Contains(request, "func (p *ListPensionsParams) Validate() error") {
		t.Error("govalid generates Validate() for a filtered params struct; a hand-written one would be a duplicate method")
	}
	handler := fileNamed(t, res, "handler/pension.go")
	if !strings.Contains(handler, "ListPensions(sctx *serverRoute.Context, req ListPensionsParams)") {
		t.Error("a filtered list should bind the params struct")
	}
}

// TestModificationsPreserveLineEndings: the reference template is CRLF
// throughout. A patch that normalises to LF turns a three-line insertion into a
// diff touching every line, which is unreviewable.
func TestModificationsPreserveLineEndings(t *testing.T) {
	root := copyTree(t, corpusRoot(t, "new-template"))

	before, err := os.ReadFile(filepath.Join(root, "handler", "request.go"))
	if err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(string(before), "\r\n") {
		t.Skip("the reference copy is not CRLF here; nothing to preserve")
	}

	res, err := Resource(root, pensionSpec(), ResourceOptions{})
	if err != nil {
		t.Fatalf("scaffold: %v", err)
	}
	for _, path := range []string{"handler/request.go", "bootstrap/bootstrapper.go"} {
		content := fileNamed(t, res, path)
		if !strings.Contains(content, "\r\n") {
			t.Errorf("%s lost its CRLF line endings", path)
		}
		if strings.Contains(strings.ReplaceAll(content, "\r\n", ""), "\n") {
			t.Errorf("%s has mixed line endings", path)
		}
	}
}

// ── helpers ─────────────────────────────────────────────────────────────────

func fileNamed(t *testing.T, res *Result, path string) string {
	t.Helper()
	for _, f := range res.Files {
		if f.Path == path {
			return f.Content
		}
	}
	t.Fatalf("scaffold produced no %s (got %v)", path, res.Paths())
	return ""
}

func goBuild(t *testing.T, root string) (string, error) { return run(t, root, "go", "build", "./...") }
func goVet(t *testing.T, root string) (string, error)   { return run(t, root, "go", "vet", "./...") }

func run(t *testing.T, root, name string, args ...string) (string, error) {
	t.Helper()
	cmd := exec.Command(name, args...)
	cmd.Dir = root
	out, err := cmd.CombinedOutput()
	return string(out), err
}

func asExists(err error, target **ExistsError) bool {
	for err != nil {
		if e, ok := err.(*ExistsError); ok {
			*target = e
			return true
		}
		u, ok := err.(interface{ Unwrap() error })
		if !ok {
			return false
		}
		err = u.Unwrap()
	}
	return false
}

func contains(list []string, s string) bool {
	for _, v := range list {
		if v == s {
			return true
		}
	}
	return false
}

var _ = spec.TimestampLayout // keep the spec import meaningful if fixtures move
