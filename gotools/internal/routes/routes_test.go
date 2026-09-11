package routes

import (
	"os"
	"path/filepath"
	"testing"

	"gitlab.cept.gov.in/it-2.0/dakcoder/gotools/internal/workspace"
)

func load(t *testing.T, files map[string]string) *workspace.Workspace {
	t.Helper()
	root := t.TempDir()
	files["go.mod"] = "module example.test\n\ngo 1.24\n"
	for rel, body := range files {
		path := filepath.Join(root, filepath.FromSlash(rel))
		if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(path, []byte(body), 0o644); err != nil {
			t.Fatal(err)
		}
	}
	ws, err := workspace.Load(root)
	if err != nil {
		t.Fatal(err)
	}
	return ws
}

func keys(inv *Inventory) map[string]bool {
	out := map[string]bool{}
	for _, r := range inv.Routes {
		out[r.Key()] = true
	}
	return out
}

// The legacy shape, with the nesting expressed as variable assignment -- which
// is the part a naive reader gets wrong, because the path of a route is not
// written anywhere near the route.
const legacy = `package routes

import "github.com/gin-gonic/gin"

func Routes(r *gin.Engine, h *handler.AwardHandler) {
	v1 := r.Group("/v1")
	awards := v1.Group("/awards")
	awards.POST("", h.CreateAwardsBulk)
	awards.GET("/:award-id", h.FetchAwardDetails)
	awards.PUT("/:award-id", h.UpdateAwardDetails)
	awards.DELETE("/:award-id", h.DeleteAwardDetails)

	reports := v1.Group("/reports")
	reports.GET("/summary", h.FetchAwardsSummary)
}
`

// The template shape, with the base path in the constructor and the rest in
// Routes() -- so a route's path is split across two functions joined only by
// the handler type.
const converted = `package handler

import (
	serverHandler "gitlab.cept.gov.in/it-2.0-common/n-api-server/handler"
	serverRoute "gitlab.cept.gov.in/it-2.0-common/n-api-server/route"
)

type AwardHandler struct {
	*serverHandler.Base
}

func NewAwardsHandler() *AwardHandler {
	base := serverHandler.New("Awards").SetPrefix("/v1").AddPrefix("/awards")
	return &AwardHandler{Base: base}
}

func (c *AwardHandler) Routes() []serverRoute.Route {
	return []serverRoute.Route{
		serverRoute.POST("", c.CreateAwardsBulk).Name("Create Awards Bulk"),
		serverRoute.GET("/:id", c.FetchAwardDetails).Name("Fetch Award Details"),
		serverRoute.PUT("/:id", c.UpdateAwardDetails).Name("Update Award Details"),
		serverRoute.DELETE("/:id", c.DeleteAwardDetails).Name("Delete Award Details"),
	}
}
`

func TestGinRoutesCarryTheirGroupPrefixes(t *testing.T) {
	inv := Take(load(t, map[string]string{"routes/routes.go": legacy}))

	if len(inv.Routes) != 5 {
		t.Fatalf("found %d routes, want 5: %+v", len(inv.Routes), inv.Routes)
	}
	got := keys(inv)
	for _, want := range []string{
		"POST /v1/awards",
		"GET /v1/awards/:p",
		"PUT /v1/awards/:p",
		"DELETE /v1/awards/:p",
		"GET /v1/reports/summary",
	} {
		if !got[want] {
			t.Errorf("missing %q; have %v", want, got)
		}
	}
}

func TestACollectionRouteIsTheGroupsOwnPath(t *testing.T) {
	// `awards.POST("", ...)` is POST /v1/awards, not POST /v1/awards/ --
	// which is a different route to a router, and would compare unequal.
	inv := Take(load(t, map[string]string{"routes/routes.go": legacy}))
	for _, r := range inv.Routes {
		if r.Method == "POST" && r.Path != "/v1/awards" {
			t.Fatalf("collection route is %q, want /v1/awards", r.Path)
		}
	}
}

func TestTemplateRoutesCarryTheConstructorsPrefixChain(t *testing.T) {
	inv := Take(load(t, map[string]string{"handler/award.go": converted}))

	if len(inv.Routes) != 4 {
		t.Fatalf("found %d routes, want 4: %+v", len(inv.Routes), inv.Routes)
	}
	got := keys(inv)
	for _, want := range []string{
		"POST /v1/awards",
		"GET /v1/awards/:p",
		"PUT /v1/awards/:p",
		"DELETE /v1/awards/:p",
	} {
		if !got[want] {
			t.Errorf("missing %q; have %v", want, got)
		}
	}
	for _, r := range inv.Routes {
		if r.Style != "template" {
			t.Errorf("%s %s recorded as %q", r.Method, r.Path, r.Style)
		}
		if r.Name == "" {
			t.Errorf("%s %s lost its .Name()", r.Method, r.Path)
		}
	}
}

// The whole point of the package: a conversion that carries everything across
// compares clean even though it renamed the path parameter.
func TestAFaithfulConversionLosesNothing(t *testing.T) {
	before := Take(load(t, map[string]string{"routes/routes.go": legacy}))
	after := Take(load(t, map[string]string{
		"handler/award.go": converted,
		"handler/report.go": `package handler

import (
	serverHandler "gitlab.cept.gov.in/it-2.0-common/n-api-server/handler"
	serverRoute "gitlab.cept.gov.in/it-2.0-common/n-api-server/route"
)

type ReportHandler struct{ *serverHandler.Base }

func NewReportHandler() *ReportHandler {
	return &ReportHandler{Base: serverHandler.New("Reports").SetPrefix("/v1").AddPrefix("/reports")}
}

func (c *ReportHandler) Routes() []serverRoute.Route {
	return []serverRoute.Route{
		serverRoute.GET("/summary", c.FetchAwardsSummary).Name("Fetch Awards Summary"),
	}
}
`,
	}))

	if missing := Compare(before, after); len(missing) != 0 {
		t.Fatalf("a faithful conversion reported %d lost routes: %+v", len(missing), missing)
	}
}

func TestAHandlerLeftBehindIsReported(t *testing.T) {
	before := Take(load(t, map[string]string{"routes/routes.go": legacy}))
	// The awards handler was converted; the report route was never carried
	// over. This is the exact failure the package exists to catch: it compiles,
	// it passes the linter, and the endpoint is gone.
	after := Take(load(t, map[string]string{"handler/award.go": converted}))

	missing := Compare(before, after)
	if len(missing) != 1 {
		t.Fatalf("reported %d lost routes, want 1: %+v", len(missing), missing)
	}
	if missing[0].Path != "/v1/reports/summary" {
		t.Fatalf("reported %q as lost, want /v1/reports/summary", missing[0].Path)
	}
	if missing[0].Handler != "h.FetchAwardsSummary" {
		t.Errorf("lost route names handler %q", missing[0].Handler)
	}
}

// A prefix that moves is not a lost route, and reporting it as one would bury
// the routes that really went under a page of noise.
func TestAMovedPrefixIsNotALostRoute(t *testing.T) {
	before := Take(load(t, map[string]string{"routes/routes.go": `package routes

func Routes(r *gin.Engine, h *handler.AwardHandler) {
	g := r.Group("/awards")
	g.GET("/:id", h.FetchAwardDetails)
}
`}))
	after := Take(load(t, map[string]string{"handler/award.go": converted}))

	if missing := Compare(before, after); len(missing) != 0 {
		t.Fatalf("a moved prefix reported as lost: %+v", missing)
	}
}

// An inventory that under-counts silently would make the comparison pass by
// omission, which is the one outcome this must never produce.
func TestANonLiteralPathIsReportedRatherThanDropped(t *testing.T) {
	inv := Take(load(t, map[string]string{"routes/routes.go": `package routes

func Routes(r *gin.Engine, h *handler.AwardHandler) {
	g := r.Group("/v1")
	g.GET(awardPath, h.FetchAwardDetails)
}
`}))

	if len(inv.Unresolved) != 1 {
		t.Fatalf("reported %d unresolved, want 1: %v", len(inv.Unresolved), inv.Unresolved)
	}
}

func TestTestFilesAreNotAnInventory(t *testing.T) {
	inv := Take(load(t, map[string]string{"tests/routes_test.go": `package tests

func TestX(t *testing.T) {
	r := gin.New()
	r.GET("/only-in-a-test", nil)
}
`}))
	if len(inv.Routes) != 0 {
		t.Fatalf("a test file contributed routes: %+v", inv.Routes)
	}
}
