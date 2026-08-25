package fxwire

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
)

const referenceBootstrap = `package bootstrap

import (
	handler "pisapi/handler"
	repo "pisapi/repo/postgres"

	serverHandler "gitlab.cept.gov.in/it-2.0-common/n-api-server/handler"
	"go.uber.org/fx"
)

var FxRepo = fx.Module(
	"Repomodule",
	fx.Provide(
		repo.NewUserRepository,
	),
)

var FxHandler = fx.Module(
	"Handlermodule",
	fx.Provide(
		fx.Annotate(
			handler.NewUserHandler,
			fx.As(new(serverHandler.Handler)),
			fx.ResultTags(serverHandler.ServerControllersGroupTag),
		),
	),
)
`

// mkroot writes a workspace containing a bootstrap file and returns its root.
func mkroot(t *testing.T, bootstrap string) string {
	t.Helper()
	root := t.TempDir()
	dir := filepath.Join(root, "bootstrap")
	if err := os.MkdirAll(dir, 0o755); err != nil {
		t.Fatal(err)
	}
	if bootstrap != "" {
		if err := os.WriteFile(filepath.Join(dir, "bootstrapper.go"), []byte(bootstrap), 0o644); err != nil {
			t.Fatal(err)
		}
	}
	return root
}

func TestWiresBothKindsInTheReferenceShape(t *testing.T) {
	root := mkroot(t, referenceBootstrap)

	res, err := Plan(root, "pisapi",
		Registration{Kind: KindRepo, Ctor: "NewPensionRepository"},
		Registration{Kind: KindHandler, Ctor: "NewPensionHandler"},
	)
	if err != nil {
		t.Fatalf("plan: %v", err)
	}
	if !res.Changed || len(res.Added) != 2 {
		t.Fatalf("added = %v, changed = %v", res.Added, res.Changed)
	}

	want := `var FxRepo = fx.Module(
	"Repomodule",
	fx.Provide(
		repo.NewUserRepository,
		repo.NewPensionRepository,
	),
)`
	if !strings.Contains(res.Content, want) {
		t.Errorf("FxRepo does not match the reference shape:\n%s", res.Content)
	}

	want = `		fx.Annotate(
			handler.NewPensionHandler,
			fx.As(new(serverHandler.Handler)),
			fx.ResultTags(serverHandler.ServerControllersGroupTag),
		),`
	if !strings.Contains(res.Content, want) {
		t.Errorf("FxHandler does not match the reference shape:\n%s", res.Content)
	}
}

// TestHandlerIsNeverRegisteredPlain is the whole point of the package. A plain
// fx.Provide for a handler compiles, starts, and silently serves nothing.
func TestHandlerIsNeverRegisteredPlain(t *testing.T) {
	root := mkroot(t, referenceBootstrap)
	res, err := Plan(root, "pisapi", Registration{Kind: KindHandler, Ctor: "NewPensionHandler"})
	if err != nil {
		t.Fatalf("plan: %v", err)
	}
	for _, required := range []string{
		"fx.Annotate(",
		"fx.As(new(serverHandler.Handler))",
		"fx.ResultTags(serverHandler.ServerControllersGroupTag)",
	} {
		if !strings.Contains(res.Content, required) {
			t.Errorf("handler registration is missing %s", required)
		}
	}
	// And it must land in FxHandler, not FxRepo.
	repoBlock := res.Content[strings.Index(res.Content, "var FxRepo"):strings.Index(res.Content, "var FxHandler")]
	if strings.Contains(repoBlock, "NewPensionHandler") {
		t.Error("handler was registered in FxRepo")
	}
}

// TestReRunIsANoOp: the agent retries tool calls. A second wire must report
// "already registered" rather than adding a duplicate provider, which Uber-FX
// rejects at start-up with a confusing error about a type being provided twice.
func TestReRunIsANoOp(t *testing.T) {
	root := mkroot(t, referenceBootstrap)
	regs := []Registration{
		{Kind: KindRepo, Ctor: "NewPensionRepository"},
		{Kind: KindHandler, Ctor: "NewPensionHandler"},
	}
	if _, err := Apply(root, "pisapi", regs...); err != nil {
		t.Fatalf("first apply: %v", err)
	}
	res, err := Apply(root, "pisapi", regs...)
	if err != nil {
		t.Fatalf("second apply: %v", err)
	}
	if res.Changed {
		t.Error("second run changed the file")
	}
	if len(res.AlreadyRegistered) != 2 {
		t.Errorf("already registered = %v, want both", res.AlreadyRegistered)
	}

	body, err := os.ReadFile(filepath.Join(root, "bootstrap", "bootstrapper.go"))
	if err != nil {
		t.Fatal(err)
	}
	if n := strings.Count(string(body), "NewPensionRepository"); n != 1 {
		t.Errorf("NewPensionRepository appears %d times, want 1", n)
	}
}

// TestExistingRegistrationUnderADifferentAliasIsDetected: matching on the bare
// constructor name rather than the qualified selector means a service that
// aliases its imports differently is still recognised.
func TestExistingRegistrationUnderADifferentAliasIsDetected(t *testing.T) {
	src := strings.ReplaceAll(referenceBootstrap, `repo "pisapi/repo/postgres"`, `pg "pisapi/repo/postgres"`)
	src = strings.ReplaceAll(src, "repo.NewUserRepository", "pg.NewUserRepository")
	root := mkroot(t, src)

	res, err := Plan(root, "pisapi", Registration{Kind: KindRepo, Ctor: "NewUserRepository"})
	if err != nil {
		t.Fatalf("plan: %v", err)
	}
	if res.Changed {
		t.Error("an already-registered constructor was registered again under a different alias")
	}
}

// TestNewEntryUsesTheFilesOwnAliases: inserting `repo.X` into a file that calls
// the package `pg` would not compile.
func TestNewEntryUsesTheFilesOwnAliases(t *testing.T) {
	src := strings.ReplaceAll(referenceBootstrap, `repo "pisapi/repo/postgres"`, `pg "pisapi/repo/postgres"`)
	src = strings.ReplaceAll(src, "repo.NewUserRepository", "pg.NewUserRepository")
	src = strings.ReplaceAll(src, `serverHandler "gitlab.cept.gov.in/it-2.0-common/n-api-server/handler"`,
		`sh "gitlab.cept.gov.in/it-2.0-common/n-api-server/handler"`)
	src = strings.ReplaceAll(src, "serverHandler.Handler", "sh.Handler")
	src = strings.ReplaceAll(src, "serverHandler.ServerControllersGroupTag", "sh.ServerControllersGroupTag")
	root := mkroot(t, src)

	res, err := Plan(root, "pisapi",
		Registration{Kind: KindRepo, Ctor: "NewPensionRepository"},
		Registration{Kind: KindHandler, Ctor: "NewPensionHandler"},
	)
	if err != nil {
		t.Fatalf("plan: %v", err)
	}
	if !strings.Contains(res.Content, "pg.NewPensionRepository") {
		t.Error("the new repo entry ignored the file's own package alias")
	}
	if !strings.Contains(res.Content, "sh.Handler") || strings.Contains(res.Content, "serverHandler.Handler") {
		t.Error("the new handler entry ignored the file's own serverHandler alias")
	}
}

// TestCommentsAndLineEndingsSurvive: bootstrapper.go is a file developers
// annotate, and the reference template is CRLF. Re-printing the AST would lose
// the first and rewrite the second.
func TestCommentsAndLineEndingsSurvive(t *testing.T) {
	src := strings.ReplaceAll(referenceBootstrap,
		"var FxRepo = fx.Module(",
		"// FxRepo is deliberately ordered: the audit repo must come last.\nvar FxRepo = fx.Module(")
	src = strings.ReplaceAll(src, "\t\trepo.NewUserRepository,", "\t\trepo.NewUserRepository, // the original")
	crlf := strings.ReplaceAll(src, "\n", "\r\n")
	root := mkroot(t, crlf)

	res, err := Plan(root, "pisapi", Registration{Kind: KindRepo, Ctor: "NewPensionRepository"})
	if err != nil {
		t.Fatalf("plan: %v", err)
	}
	if !strings.Contains(res.Content, "the audit repo must come last") {
		t.Error("a leading comment was lost")
	}
	if !strings.Contains(res.Content, "// the original") {
		t.Error("a trailing comment was lost")
	}
	if !strings.Contains(res.Content, "\r\n") {
		t.Error("CRLF line endings were not preserved")
	}
	if strings.Contains(strings.ReplaceAll(res.Content, "\r\n", ""), "\n") {
		t.Error("the patched file has mixed line endings")
	}
}

// TestSingleLineProvideIsBrokenOpen: a service that wrote its provide list on
// one line is still a valid target, and the result has to stay readable.
func TestSingleLineProvideIsBrokenOpen(t *testing.T) {
	src := `package bootstrap

import (
	repo "pisapi/repo/postgres"

	"go.uber.org/fx"
)

var FxRepo = fx.Module("Repomodule", fx.Provide(repo.NewUserRepository))
`
	root := mkroot(t, src)
	res, err := Plan(root, "pisapi", Registration{Kind: KindRepo, Ctor: "NewPensionRepository"})
	if err != nil {
		t.Fatalf("plan: %v", err)
	}
	if !strings.Contains(res.Content, "repo.NewPensionRepository") {
		t.Errorf("entry not added:\n%s", res.Content)
	}
	if !strings.Contains(res.Content, "repo.NewUserRepository") {
		t.Errorf("existing entry lost:\n%s", res.Content)
	}
}

// TestMissingImportsAreAdded: a bootstrap file that does not yet import the
// handler package still has to end up compiling.
func TestMissingImportsAreAdded(t *testing.T) {
	src := `package bootstrap

import (
	repo "pisapi/repo/postgres"

	"go.uber.org/fx"
)

var FxRepo = fx.Module(
	"Repomodule",
	fx.Provide(
		repo.NewUserRepository,
	),
)

var FxHandler = fx.Module(
	"Handlermodule",
	fx.Provide(),
)
`
	root := mkroot(t, src)
	res, err := Plan(root, "pisapi", Registration{Kind: KindHandler, Ctor: "NewPensionHandler"})
	if err != nil {
		t.Fatalf("plan: %v", err)
	}
	for _, want := range []string{
		`"pisapi/handler"`,
		`"gitlab.cept.gov.in/it-2.0-common/n-api-server/handler"`,
	} {
		if !strings.Contains(res.Content, want) {
			t.Errorf("missing import %s in:\n%s", want, res.Content)
		}
	}
}

func TestMissingModuleVarIsANamedError(t *testing.T) {
	src := `package bootstrap

import "go.uber.org/fx"

var FxRepo = fx.Module("Repomodule", fx.Provide())
`
	root := mkroot(t, src)
	_, err := Plan(root, "pisapi", Registration{Kind: KindHandler, Ctor: "NewPensionHandler"})
	if err == nil {
		t.Fatal("expected an error when FxHandler does not exist")
	}
	if !strings.Contains(err.Error(), "FxHandler") {
		t.Errorf("the error should name the missing module variable, got: %v", err)
	}
}

func TestMissingBootstrapDirectoryIsANamedError(t *testing.T) {
	_, err := Plan(t.TempDir(), "pisapi", Registration{Kind: KindRepo, Ctor: "NewXRepository"})
	if err == nil {
		t.Fatal("expected an error with no bootstrap directory")
	}
	if !strings.Contains(err.Error(), "bootstrap") {
		t.Errorf("the error should say what is missing, got: %v", err)
	}
}

// TestBootstrapFileIsFoundByContent: a service that split its modules into
// bootstrap/modules.go is still wireable.
func TestBootstrapFileIsFoundByContent(t *testing.T) {
	root := t.TempDir()
	dir := filepath.Join(root, "bootstrap")
	if err := os.MkdirAll(dir, 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(dir, "modules.go"), []byte(referenceBootstrap), 0o644); err != nil {
		t.Fatal(err)
	}
	res, err := Plan(root, "pisapi", Registration{Kind: KindRepo, Ctor: "NewPensionRepository"})
	if err != nil {
		t.Fatalf("plan: %v", err)
	}
	if res.Path != "bootstrap/modules.go" {
		t.Errorf("path = %q, want bootstrap/modules.go", res.Path)
	}
}

// TestHostileConstructorNamesAreRefused: the ctor is interpolated into source,
// and it ultimately comes from model output.
func TestHostileConstructorNamesAreRefused(t *testing.T) {
	root := mkroot(t, referenceBootstrap)
	for _, ctor := range []string{
		"NewX), fx.Invoke(os.Exit",
		"NewX\n\t\tevil()",
		"",
		"PensionRepository", // not a New* constructor
		"New X",
	} {
		if _, err := Plan(root, "pisapi", Registration{Kind: KindRepo, Ctor: ctor}); err == nil {
			t.Errorf("constructor %q should have been refused", ctor)
		}
	}
}

func TestUnknownKindIsRefused(t *testing.T) {
	root := mkroot(t, referenceBootstrap)
	if _, err := Plan(root, "pisapi", Registration{Kind: "service", Ctor: "NewThing"}); err == nil {
		t.Fatal("an unknown kind should be refused rather than defaulted")
	}
}

// TestPlanDoesNotWrite: Plan is what the approval gate calls, so it must leave
// the working tree untouched.
func TestPlanDoesNotWrite(t *testing.T) {
	root := mkroot(t, referenceBootstrap)
	path := filepath.Join(root, "bootstrap", "bootstrapper.go")
	before, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := Plan(root, "pisapi", Registration{Kind: KindRepo, Ctor: "NewPensionRepository"}); err != nil {
		t.Fatalf("plan: %v", err)
	}
	after, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	if string(before) != string(after) {
		t.Error("Plan wrote to disk")
	}
}
