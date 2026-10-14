package convert

import (
	"go/parser"
	"go/token"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"gitlab.cept.gov.in/it-2.0/dakcoder/gotools/internal/workspace"
)

// A legacy service in miniature, as pao-backend is shaped: one routes.go with
// group prefixes, a bootstrap with plain fx.Provide, main.go invoking
// routes.Routes, a legacy test harness, and request DTOs beside the handlers.
var legacyService = map[string]string{
	"go.mod": "module svc\n\ngo 1.24\n",
	"main.go": `package main

import (
	"context"
	"svc/bootstrap"
	"svc/routes"

	bootstrapper "gitlab.cept.gov.in/it-2.0-common/api-bootstrapper"
	"go.uber.org/fx"
)

func main() {
	app := bootstrapper.New().Options(
		fx.Invoke(routes.Routes),
		bootstrap.FxHandler,
	)
	app.WithContext(context.Background()).Run()
}
`,
	"bootstrap/bootstrapper.go": `package bootstrap

import (
	handler "svc/handler"
	"go.uber.org/fx"
)

var FxHandler = fx.Module(
	"Handlermodule",
	fx.Provide(
		handler.NewAwardHandler,
		handler.NewAwardGrpcHandler,
	),
)
`,
	"routes/routes.go": `package routes

import (
	r "gitlab.cept.gov.in/it-2.0-common/api-server"
	handler "svc/handler"
	"github.com/gin-gonic/gin"
)

func Health(c *gin.Context) {}

func Routes(router *r.Router, awardHandler *handler.AwardHandler) {
	router.GET("/healthz", Health)
	v1 := router.Group("/v1")
	{
		awards := v1.Group("/awards")
		{
			awards.GET("/:award-id", awardHandler.FetchAward)
			awards.POST("", awardHandler.CreateAward)
			awards.GET("/legacy", awardHandler.LegacyAward)
		}
	}
}
`,
	"handler/award.go": `package handler

import (
	"github.com/gin-gonic/gin"
	serverRoute "gitlab.cept.gov.in/it-2.0-common/n-api-server/route"
	"svc/handler/response"
)

type AwardHandler struct {
	svc string
}

func NewAwardHandler() *AwardHandler {
	return &AwardHandler{
		"x",
	}
}

type awardIdRequest struct {
	ID string ` + "`uri:\"award-id\" validate:\"required,validatePaocode\"`" + `
}

type AwardInput struct {
	Name  string ` + "`json:\"name\" validate:\"required,max=50\"`" + `
	Notes string
}

func (h *AwardHandler) FetchAward(sctx *serverRoute.Context, req awardIdRequest) (*response.Award, error) {
	return &response.Award{}, nil
}

// Answers 201 Created: register its route with http.StatusCreated.
func (h *AwardHandler) CreateAward(sctx *serverRoute.Context, req AwardInput) (*response.Award, error) {
	return &response.Award{}, nil
}

func (h *AwardHandler) LegacyAward(ctx *gin.Context) {
	ctx.JSON(200, nil)
}
`,
	"handler/awardgrpc.go": `package handler

import (
	"context"

	"connectrpc.com/connect"
)

type AwardGrpcHandler struct{}

func NewAwardGrpcHandler() *AwardGrpcHandler { return &AwardGrpcHandler{} }

func (h *AwardGrpcHandler) Get(ctx context.Context, req *connect.Request[string]) (*connect.Response[string], error) {
	return nil, nil
}
`,
	"handler/response/award.go": "package response\n\ntype Award struct{ Name string }\n",
	"core/domain/award.go": "package domain\n\ntype Award struct {\n\tDate string `json\t:\"date\" validate:\"required\"`\n}\n",
	"tests/testmain_test.go": `package tests

import (
	"svc/bootstrap"
	"svc/routes"
	"testing"

	config "gitlab.cept.gov.in/it-2.0-common/api-config"
	router "gitlab.cept.gov.in/it-2.0-common/api-server"
	"go.uber.org/fx"
	"go.uber.org/fx/fxtest"
)

var Router *router.Router

func BootstrapTestApp(tb testing.TB) *router.Router {
	app := fxtest.New(
		tb,
		fx.Populate(&Router),
		bootstrap.FxHandler,
		fx.Invoke(routes.Routes),
	)
	app.RequireStart()
	_ = config.Config{}
	return Router
}
`,
}

func service(t *testing.T) string {
	t.Helper()
	root := t.TempDir()
	for rel, body := range legacyService {
		p := filepath.Join(root, filepath.FromSlash(rel))
		if err := os.MkdirAll(filepath.Dir(p), 0o755); err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(p, []byte(body), 0o644); err != nil {
			t.Fatal(err)
		}
	}
	return root
}

func load(t *testing.T, root string) *workspace.Workspace {
	t.Helper()
	ws, err := workspace.Load(root)
	if err != nil {
		t.Fatal(err)
	}
	return ws
}

func mustParse(t *testing.T, root, rel string) string {
	t.Helper()
	src := read(t, root, rel)
	if _, err := parser.ParseFile(token.NewFileSet(), rel, src, 0); err != nil {
		t.Fatalf("%s does not parse: %v\n%s", rel, err, src)
	}
	return src
}

func TestTheServiceWiringMovesOntoTheHandlers(t *testing.T) {
	root := service(t)
	res, err := Service(load(t, root), true)
	if err != nil || res.Error != "" {
		t.Fatalf("%v %s", err, res.Error)
	}

	award := mustParse(t, root, "handler/award.go")
	for _, want := range []string{
		"*serverHandler.Base",
		`serverHandler.New("Award").SetPrefix("/v1").AddPrefix("/awards"),`,
		"func (h *AwardHandler) Routes() []serverRoute.Route {",
		`serverRoute.GET("/:award-id", h.FetchAward).Name("Fetch Award"),`,
		`serverRoute.POST("", h.CreateAward, http.StatusCreated).Name("Create Award"),`,
		`// TODO(migration): GET "/legacy" -> h.LegacyAward, once that method is converted.`,
	} {
		if !strings.Contains(award, want) {
			t.Errorf("handler/award.go lacks %q\n%s", want, award)
		}
	}
	boot := mustParse(t, root, "bootstrap/bootstrapper.go")
	if !strings.Contains(boot, "fx.Annotate(\n\t\t\thandler.NewAwardHandler,") ||
		!strings.Contains(boot, "fx.ResultTags(serverHandler.ServerControllersGroupTag)") {
		t.Errorf("bootstrap not wired:\n%s", boot)
	}
	if !strings.Contains(boot, "\t\thandler.NewAwardGrpcHandler,") {
		t.Errorf("a Connect handler must stay a plain provide:\n%s", boot)
	}
	main := mustParse(t, root, "main.go")
	if strings.Contains(main, "routes") || !strings.Contains(main, "n-api-bootstrapper") {
		t.Errorf("main.go still invokes routes or the legacy bootstrapper:\n%s", main)
	}
	if _, err := os.Stat(filepath.Join(root, "routes")); !os.IsNotExist(err) {
		t.Error("routes/ should be gone with routes.go")
	}
	if len(res.Pending) != 1 || len(res.Dropped) != 1 {
		t.Errorf("pending %v dropped %v", res.Pending, res.Dropped)
	}
	// the test harness, on the template router
	tm := mustParse(t, root, "tests/testmain_test.go")
	for _, want := range []string{
		"var Router *TestRouter",
		"func BootstrapTestApp(tb testing.TB) *TestRouter {",
		"fx.Invoke(newTestRouter)",
		"server.NewRouter(engine, cfg, server.ParseControllers(p.Controllers...)).RegisterRoutes()",
	} {
		if !strings.Contains(tm, want) {
			t.Errorf("test harness lacks %q\n%s", want, tm)
		}
	}
	if strings.Contains(tm, "fx.Populate(&Router)") || strings.Contains(tm, `"svc/routes"`) {
		t.Errorf("legacy harness wiring left behind:\n%s", tm)
	}
	// the malformed tag go vet rejects
	if !strings.Contains(read(t, root, "core/domain/award.go"), "`json:\"date\" validate:\"required\"`") {
		t.Errorf("struct tag not fixed:\n%s", read(t, root, "core/domain/award.go"))
	}
}

func TestTheServiceStepIsADryRunUnlessAskedToWrite(t *testing.T) {
	root := service(t)
	before := read(t, root, "handler/award.go")
	res, err := Service(load(t, root), false)
	if err != nil || len(res.Written) == 0 || !res.DryRun {
		t.Fatalf("%v %+v", err, res)
	}
	if read(t, root, "handler/award.go") != before {
		t.Fatal("a dry run changed a file")
	}
	if _, err := os.Stat(filepath.Join(root, "routes", "routes.go")); err != nil {
		t.Fatal("a dry run removed routes.go")
	}
}

func TestTheRequestDTOsMoveToTheRequestPackage(t *testing.T) {
	root := service(t)
	if _, err := Service(load(t, root), true); err != nil {
		t.Fatal(err)
	}
	res, err := DTOs(load(t, root), true)
	if err != nil {
		t.Fatal(err)
	}
	req := mustParse(t, root, "handler/request/request.go")
	for _, want := range []string{
		"package request",
		"type AwardIdRequest struct", // exported: the handlers refer to it from outside
		"type AwardInput struct",     // bound by a handler, whatever its name
		"`json:\"Notes\" validate:\"omitempty\"`",
	} {
		if !strings.Contains(req, want) {
			t.Errorf("request.go lacks %q\n%s", want, req)
		}
	}
	award := mustParse(t, root, "handler/award.go")
	if !strings.Contains(award, "req request.AwardIdRequest") || !strings.Contains(award, "req request.AwardInput") {
		t.Errorf("handler references not qualified:\n%s", award)
	}
	if strings.Contains(award, "type awardIdRequest") {
		t.Error("the moved declaration is still in the handler file")
	}
	// validatePaocode is not a govalid tag: the runtime validator keeps it
	if tags := res.Runtime["AwardIdRequest"]; len(tags) != 1 || tags[0] != "validatePaocode" {
		t.Errorf("runtime-validated: %v", res.Runtime)
	}
	adapters := read(t, root, "handler/request/"+runtimeFile)
	if !strings.Contains(adapters, "func (t *AwardIdRequest) Validate() error { return validation.ValidateStruct(t) }") {
		t.Errorf("no runtime Validate:\n%s", adapters)
	}
}

func TestRunningTheDTOStepTwiceKeepsEveryValidator(t *testing.T) {
	root := service(t)
	if _, err := Service(load(t, root), true); err != nil {
		t.Fatal(err)
	}
	if _, err := DTOs(load(t, root), true); err != nil {
		t.Fatal(err)
	}
	first := read(t, root, "handler/request/"+runtimeFile)
	if _, err := DTOs(load(t, root), true); err != nil {
		t.Fatal(err)
	}
	if second := read(t, root, "handler/request/"+runtimeFile); second != first {
		t.Fatalf("a second run changed the runtime validators:\n--- first\n%s\n--- second\n%s", first, second)
	}
	if !strings.Contains(first, "func (t *AwardIdRequest) Validate()") {
		t.Fatalf("validator missing:\n%s", first)
	}
}
