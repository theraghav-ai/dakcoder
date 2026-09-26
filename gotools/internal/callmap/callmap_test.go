package callmap

import (
	"os"
	"path/filepath"
	"strings"
	"testing"

	"gitlab.cept.gov.in/it-2.0/dakcoder/gotools/internal/workspace"
)

// ── a small service, in both shapes ─────────────────────────────────────────

var service = map[string]string{
	"go.mod": "module svc\n\ngo 1.25\n",
	"repo/postgres/user.go": `package repository

import "github.com/gin-gonic/gin"

type UserRepository struct{ db int }

func (r *UserRepository) GetAll(gctx *gin.Context) error { return r.helper() }
func (r *UserRepository) GetByID(gctx *gin.Context, id int) error { return nil }
func (r *UserRepository) helper() error { return nil }
`,
	"repo/postgres/audit.go": `package repository

import "context"

type AuditRepository struct{}

func (a *AuditRepository) Record(ctx context.Context) error { return nil }
`,
	"handler/user.go": `package handler

import (
	"github.com/gin-gonic/gin"
	repo "svc/repo/postgres"
)

type UserHandler struct {
	svc   *repo.UserRepository
	audit *repo.AuditRepository
}

func (h *UserHandler) ListUsers(ctx *gin.Context) {
	var req struct{}
	_ = ctx.ShouldBindQuery(&req)
	_ = h.svc.GetAll(ctx)
	_ = h.audit.Record(ctx)
	handleSuccess(ctx, nil)
}

func (h *UserHandler) GetUser(sctx *serverRoute.Context, req GetUserRequest) (*response.User, error) {
	return nil, h.svc.GetByID(sctx.Ctx, req.ID)
}

func handleSuccess(ctx *gin.Context, v any) {}
`,
	"routes/routes.go": `package routes

import "github.com/gin-gonic/gin"

func Routes(r *gin.Engine, uh any) {
	v1 := r.Group("/v1")
	v1.GET("/users", uh.ListUsers)
	v1.GET("/users/:id", uh.GetUser)
}
`,
}

func load(t *testing.T, files map[string]string) *workspace.Workspace {
	t.Helper()
	dir := t.TempDir()
	for rel, body := range files {
		p := filepath.Join(dir, filepath.FromSlash(rel))
		if err := os.MkdirAll(filepath.Dir(p), 0o755); err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(p, []byte(body), 0o644); err != nil {
			t.Fatal(err)
		}
	}
	ws, err := workspace.Load(dir)
	if err != nil {
		t.Fatal(err)
	}
	return ws
}

func TestCallsThroughAStructFieldResolveToTheFieldsType(t *testing.T) {
	m := Build(load(t, service))
	callers := m.Callers("repo/postgres.UserRepository.GetAll")
	if len(callers) != 1 || callers[0].Func.Label() != "UserHandler.ListUsers" {
		t.Fatalf("h.svc.GetAll() was not traced to its handler: %+v", callers)
	}
	if got := m.Callers("repo/postgres.AuditRepository.Record"); len(got) != 1 {
		t.Fatalf("a second field on the same struct was not traced: %+v", got)
	}
	if got := m.Callers("repo/postgres.UserRepository.helper"); len(got) != 1 {
		t.Fatalf("a call on the receiver itself was not traced: %+v", got)
	}
	if got := m.Callers("handler.handleSuccess"); len(got) != 1 {
		t.Fatalf("a same-package function call was not traced: %+v", got)
	}
}

func TestResolveAcceptsTheWaysPeopleNameAMethod(t *testing.T) {
	m := Build(load(t, service))
	for _, s := range []string{"GetAll", "GetAll()", ".GetAll()", "UserRepository.GetAll",
		"repo.UserRepository.GetAll", "user.go::GetAll", "repo/postgres/user.go::UserRepository.GetAll"} {
		got := m.Resolve(s)
		if len(got) != 1 || got[0].Key != "repo/postgres.UserRepository.GetAll" {
			t.Errorf("Resolve(%q) = %v", s, got)
		}
	}
}

func TestClassifyTellsLegacyFromConverted(t *testing.T) {
	m := Build(load(t, service))
	cases := map[string]Shape{
		"handler.UserHandler.ListUsers":        ShapeLegacy,
		"handler.UserHandler.GetUser":          ShapeConverted,
		"repo/postgres.UserRepository.GetAll":  ShapeLegacy,
		"repo/postgres.AuditRepository.Record": ShapeConverted,
	}
	for k, want := range cases {
		if got := Classify(m.Funcs[k]).Shape; got != want {
			t.Errorf("%s: %s, want %s", k, got, want)
		}
	}
}

func TestCheckAStepIsDoneOnlyWithItsRepositoryMethods(t *testing.T) {
	ws := load(t, service)
	m := Build(ws)

	res, err := m.Check(ws, "handler/user.go", []string{"GetUser"})
	if err != nil {
		t.Fatal(err)
	}
	if res.OK {
		t.Fatalf("GetUser is converted but calls GetByID, still on *gin.Context:\n%s", res.Report)
	}
	if !strings.Contains(res.Report, "UserRepository.GetByID") {
		t.Fatalf("the unconverted repository method was not named:\n%s", res.Report)
	}

	res, _ = m.Check(ws, "handler/user.go", []string{"ListUsers", "Nope"})
	if res.OK || len(res.Unknown) != 1 || !strings.Contains(res.Report, "ShouldBindQuery") {
		t.Fatalf("a legacy method and an unknown name must both fail the step:\n%s", res.Report)
	}
}

func TestCheckReportsAFileThatDoesNotParse(t *testing.T) {
	files := map[string]string{"go.mod": "module svc\n", "handler/x.go": "package handler\n\nfunc (h *XHandler) A( {\n"}
	ws := load(t, files)
	res, err := Build(ws).Check(ws, "handler/x.go", nil)
	if err != nil {
		t.Fatal(err)
	}
	if res.OK || res.Parses || res.ParseError == "" {
		t.Fatalf("a syntax error must fail the step and be reported: %+v", res)
	}
}

func TestHandlerMapGroupsMethodsAndNamesTheirRoutesAndRepositories(t *testing.T) {
	ws := load(t, service)
	res, err := Build(ws).HandlerMap(ws, "handler/user.go", 0)
	if err != nil {
		t.Fatal(err)
	}
	if len(res.Methods) != 2 || res.Converted != 1 {
		t.Fatalf("want 2 handler methods, 1 converted: %+v", res.Methods)
	}
	list := res.Methods[0]
	if list.Name != "ListUsers" || len(list.Routes) != 1 || list.Routes[0] != "GET /v1/users" {
		t.Fatalf("route not attached: %+v", list)
	}
	if strings.Join(list.Repo, ",") != "UserRepository.GetAll,AuditRepository.Record" {
		t.Fatalf("repository callees wrong: %v", list.Repo)
	}
	if len(res.Groups) != 1 || !strings.Contains(res.Report, "unit_check path=handler/user.go") {
		t.Fatalf("groups/report wrong:\n%s", res.Report)
	}
}

func TestHandlerMapSplitsAtTheStepSize(t *testing.T) {
	var b strings.Builder
	b.WriteString("package handler\n\ntype BigHandler struct{}\n")
	for _, name := range []string{"A", "B", "C", "D"} {
		b.WriteString("\nfunc (h *BigHandler) " + name + "() {\n")
		b.WriteString(strings.Repeat("\t_ = 1\n", 30))
		b.WriteString("}\n")
	}
	ws := load(t, map[string]string{"go.mod": "module svc\n", "handler/big.go": b.String()})
	res, _ := Build(ws).HandlerMap(ws, "handler/big.go", 70)
	if len(res.Groups) != 2 || len(res.Groups[0].Methods) != 2 {
		t.Fatalf("four 32-line methods at 70 lines a step should be two steps of two: %+v", res.Groups)
	}
}

func TestImpactListsCallersAndSaysTheChangeIsCompatible(t *testing.T) {
	ws := load(t, service)
	res := Build(ws).Impact(ws, "GetAll")
	if len(res.Callers) != 1 || !strings.Contains(res.Report, "GET /v1/users") {
		t.Fatalf("impact did not reach the route:\n%s", res.Report)
	}
	if !strings.Contains(res.Report, "context.Context keeps every caller") {
		t.Fatalf("the compatibility note is missing:\n%s", res.Report)
	}
}

// ── the real legacy service ─────────────────────────────────────────────────

// The corpus the migration is judged on. graphify found 0 of these edges.
func TestPaoHandlersReachTheirRepositories(t *testing.T) {
	root := filepath.Join("..", "..", "..", "pao-back-end-development")
	if _, err := os.Stat(filepath.Join(root, "go.mod")); err != nil {
		t.Skip("pao-back-end-development corpus not present")
	}
	ws, err := workspace.Load(root)
	if err != nil {
		t.Fatal(err)
	}
	m := Build(ws)
	edges := 0
	for _, fn := range m.Funcs {
		if isHandlerMethod(fn) {
			edges += len(m.repoCallees(fn))
		}
	}
	if edges < 150 {
		t.Fatalf("handler->repository edges: %d, want at least 150 (text search finds ~182 call sites)", edges)
	}
	res, err := m.HandlerMap(ws, "handler/paogen.go", 0)
	if err != nil {
		t.Fatal(err)
	}
	if len(res.Methods) < 50 || len(res.Groups) < 7 {
		t.Fatalf("paogen.go: %d methods in %d steps", len(res.Methods), len(res.Groups))
	}
	for _, g := range res.Groups {
		if g.End-g.Start+1 > DefaultStepLines && len(g.Methods) > 1 {
			t.Errorf("step %d-%d is over the step size with %d methods", g.Start, g.End, len(g.Methods))
		}
	}
	t.Logf("handler->repo edges=%d, paogen methods=%d steps=%d unresolved=%d",
		edges, len(res.Methods), len(res.Groups), m.Unresolved)
}
