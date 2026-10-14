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

// The fixture is pao-backend's handler and repository shapes, verbatim in
// form: every pattern the 2026-10-14 survey of its 103 legacy handlers found,
// plus the ones the conversion must refuse.
const handlerSrc = `package handler

import (
	"errors"
	"net/http"

	"github.com/gin-gonic/gin"
	apierrors "gitlab.cept.gov.in/it-2.0-common/api-errors"
	log "gitlab.cept.gov.in/it-2.0-common/api-log"
	validation "gitlab.cept.gov.in/it-2.0-common/api-validation"

	"example.test/handler/response"
	repo "example.test/repo/postgres"
)

type PaogenHandler struct{ svc *repo.PaogenRepository }

type OfficeNameRequest struct {
	Id string ` + "`uri:\"id\" validate:\"required\"`" + `
}

type EntryRequest struct {
	Amount int ` + "`json:\"amount\"`" + `
}

// FetchOfficenameHandler godoc
//
//	@Router			/v1/office-names/{id} [get]
func (uh *PaogenHandler) FetchOfficenameHandler(ctx *gin.Context) {

	var req OfficeNameRequest
	if err := ctx.ShouldBindUri(&req); err != nil {
		apierrors.HandleBindingError(ctx, err)
		log.Error(ctx, "Binding failed: %s", err.Error())
		return
	}
	if err := validation.ValidateStruct(req); err != nil {
		apierrors.HandleValidationError(ctx, err)
		return
	}
	u, b, err := uh.svc.GetOfficenameRepo(ctx, req.Id)
	if err != nil {
		apierrors.HandleDBError(ctx, err)
		log.Error(ctx, "Repo call failed: %s", err.Error())
		return
	}
	if b {
		apiRsp := response.GetOfficenameResponse{Data: u}
		log.Debug(ctx, "response", apiRsp)
		handleSuccess(ctx, apiRsp)
	} else {
		err1 := errors.New("Invalid office_id")
		appError := apierrors.NewAppError(
			"No office corresponding to this office_id",
			"404",
			err1,
		)
		apiErrorResponse := apierrors.NewAPIErrorResponse(
			http.StatusNotFound,
			"Not Found",
			appError,
		)
		ctx.JSON(apiErrorResponse.StatusCode, apiErrorResponse)
	}
}

func (uh *PaogenHandler) CreateEntriesHandler(ctx *gin.Context) {
	var requestsin []EntryRequest
	if err := ctx.ShouldBindJSON(&requestsin); err != nil {
		apierrors.HandleBindingError(ctx, err)
		return
	}
	for _, r := range requestsin {
		if err := validation.ValidateStruct(r); err != nil {
			apierrors.HandleValidationError(ctx, err)
			return
		}
	}
	if len(requestsin) > 100 {
		ctx.JSON(http.StatusBadRequest, gin.H{"error": "too many entries"})
		return
	}
	if err := uh.svc.Insert(ctx, len(requestsin)); err != nil {
		ctx.JSON(http.StatusInternalServerError, gin.H{"error": "insert failed", "details": err.Error()})
		return
	}
	handleCreateSuccess(ctx, response.CreatedResponse{})
}

func (uh *PaogenHandler) ListPAOHandler(ctx *gin.Context) {
	u, err := uh.svc.GetPAOsRepo(ctx)
	if err != nil {
		apierrors.HandleDBError(ctx, err)
		return
	}
	apiRsp := response.GetPAOsResponse{Data: u}
	handleSuccess(ctx, apiRsp)
}

// Refused: reads a header, which needs gin.
func (uh *PaogenHandler) DownloadHandler(ctx *gin.Context) {
	name := ctx.Query("name")
	apiRsp := response.GetPAOsResponse{Data: name}
	handleSuccess(ctx, apiRsp)
}

// Refused: writes the response and carries on in a loop.
func (uh *PaogenHandler) LoopHandler(ctx *gin.Context) {
	for i := 0; i < 3; i++ {
		if err := uh.svc.Insert(ctx, i); err != nil {
			apierrors.HandleDBError(ctx, err)
			continue
		}
	}
	handleSuccess(ctx, response.GetPAOsResponse{})
}
`

const repoSrc = `package repository

import (
	"context"
	"time"

	"github.com/gin-gonic/gin"
)

type PaogenRepository struct{}

func (ur *PaogenRepository) GetOfficenameRepo(gctx *gin.Context, id string) (string, bool, error) {
	ctx, cancel := context.WithTimeout(gctx.Request.Context(), time.Second)
	defer cancel()
	_ = ctx
	return id, true, nil
}

func (ur *PaogenRepository) GetPAOsRepo(gctx *gin.Context) ([]string, error) {
	ctx, cancel := context.WithTimeout(gctx.Request.Context(), time.Second)
	defer cancel()
	_ = ctx
	return nil, nil
}

func (ur *PaogenRepository) Insert(gctx *gin.Context, n int) error {
	_ = n
	return ur.child(gctx)
}

func (ur *PaogenRepository) child(gctx *gin.Context) error {
	return nil
}

// Keeps gin: it reads a header.
func (ur *PaogenRepository) Header(gctx *gin.Context) string {
	return gctx.GetHeader("x")
}
`

func fixture(t *testing.T, eol string) string {
	t.Helper()
	root := t.TempDir()
	write := func(rel, body string) {
		p := filepath.Join(root, filepath.FromSlash(rel))
		if err := os.MkdirAll(filepath.Dir(p), 0o755); err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(p, []byte(strings.ReplaceAll(body, "\n", eol)), 0o644); err != nil {
			t.Fatal(err)
		}
	}
	write("go.mod", "module example.test\n\ngo 1.24\n")
	write("handler/paogen.go", handlerSrc)
	write("repo/postgres/paogen.go", repoSrc)
	return root
}

func run(t *testing.T, root string, write bool) *Result {
	t.Helper()
	ws, err := workspace.Load(root)
	if err != nil {
		t.Fatal(err)
	}
	res, err := Run(ws, Options{Write: write})
	if err != nil {
		t.Fatal(err)
	}
	return res
}

func methodsByName(res *Result) map[string]Method {
	out := map[string]Method{}
	for _, f := range res.Files {
		for _, m := range f.Methods {
			out[m.Name] = m
		}
	}
	return out
}

func read(t *testing.T, root, rel string) string {
	t.Helper()
	b, err := os.ReadFile(filepath.Join(root, filepath.FromSlash(rel)))
	if err != nil {
		t.Fatal(err)
	}
	return string(b)
}

func TestTheRuleShapedHandlersAreConvertedWhole(t *testing.T) {
	root := fixture(t, "\n")
	res := run(t, root, true)
	m := methodsByName(res)

	for _, name := range []string{"FetchOfficenameHandler", "CreateEntriesHandler", "ListPAOHandler"} {
		if m[name].Status != "converted" {
			t.Errorf("%s: %s %v", name, m[name].Status, m[name].Reasons)
		}
	}
	got := read(t, root, "handler/paogen.go")
	for _, want := range []string{
		"func (uh *PaogenHandler) FetchOfficenameHandler(sctx *serverRoute.Context, req OfficeNameRequest) (*response.GetOfficenameResponse, error) {",
		"u, b, err := uh.svc.GetOfficenameRepo(sctx.Ctx, req.Id)",
		"log.Error(sctx.Ctx, \"Repo call failed: %s\", err.Error())\n\t\treturn nil, err",
		"return &apiRsp, nil",
		`return nil, apierrors.HandleErrorWithStatusCodeAndMessage(apierrors.NewHTTPStatusCodeAndMessage(http.StatusNotFound, "Not Found"), "No office corresponding to this office_id", err1)`,
		"func (uh *PaogenHandler) ListPAOHandler(sctx *serverRoute.Context, _ struct{}) (*response.GetPAOsResponse, error) {",
		// a JSON array body keeps its shape: a named slice with Validate
		"CreateEntriesHandler(sctx *serverRoute.Context, requestsin CreateEntriesHandlerRequest) (*response.CreatedResponse, error)",
		"type CreateEntriesHandlerRequest []EntryRequest",
		"func (r CreateEntriesHandlerRequest) Validate() error {",
		`return nil, apierrors.HandleErrorWithStatusCodeAndMessage(apierrors.NewHTTPStatusCodeAndMessage(http.StatusBadRequest, http.StatusText(http.StatusBadRequest)), "too many entries", nil)`,
		`"insert failed", err)`,
		"return &response.CreatedResponse{}, nil",
		`serverRoute "gitlab.cept.gov.in/it-2.0-common/n-api-server/route"`,
		`apierrors "gitlab.cept.gov.in/it-2.0-common/n-api-errors"`,
		`log "gitlab.cept.gov.in/it-2.0-common/n-api-log"`,
		"//	@Router			/v1/office-names/{id} [get]",
	} {
		if !strings.Contains(got, want) {
			t.Errorf("converted file lacks:\n%s\n--- file ---\n%s", want, got)
		}
	}
	// once: in DownloadHandler, which is left as it was
	if n := strings.Count(got, "handleSuccess(ctx, apiRsp)"); n != 1 {
		t.Errorf("handleSuccess(ctx, apiRsp) appears %d times, want 1 (the left method)", n)
	}
	for _, gone := range []string{"ShouldBindUri", "ValidateStruct(req)", "HandleDBError(ctx, err)\n\t\tlog"} {
		if strings.Contains(got, gone) {
			t.Errorf("converted file still has %q", gone)
		}
	}
	if !m["CreateEntriesHandler"].Created {
		t.Error("handleCreateSuccess must be reported as a 201 for the route")
	}
	if _, err := parser.ParseFile(token.NewFileSet(), "", got, 0); err != nil {
		t.Fatalf("converted file does not parse: %v", err)
	}
}

func TestAMethodNoRuleFitsIsLeftExactlyAsItWasWithItsReason(t *testing.T) {
	root := fixture(t, "\n")
	res := run(t, root, true)
	m := methodsByName(res)
	got := read(t, root, "handler/paogen.go")

	for name, why := range map[string]string{
		"DownloadHandler": "uses ctx.Query",
		"LoopHandler":     "runs after the response is written (continue)",
	} {
		if m[name].Status != "left" || !strings.Contains(strings.Join(m[name].Reasons, ";"), why) {
			t.Errorf("%s: %s %v, want left because %q", name, m[name].Status, m[name].Reasons, why)
		}
	}
	if !strings.Contains(got, "func (uh *PaogenHandler) DownloadHandler(ctx *gin.Context) {\n\tname := ctx.Query(\"name\")") {
		t.Error("a left method must be byte-for-byte unchanged")
	}
	// gin is still needed by the left methods
	if !strings.Contains(got, `"github.com/gin-gonic/gin"`) {
		t.Error("gin import removed while left methods still use it")
	}
}

func TestRepositoryMethodsTakeAContextAndKeepEveryGinCallerCompiling(t *testing.T) {
	root := fixture(t, "\n")
	run(t, root, true)
	got := read(t, root, "repo/postgres/paogen.go")

	for _, want := range []string{
		"GetOfficenameRepo(gctx context.Context, id string)",
		"context.WithTimeout(gctx, time.Second)",
		"Insert(gctx context.Context, n int) error",
		"child(gctx context.Context) error",
		// uses gin itself: left alone
		"Header(gctx *gin.Context) string",
	} {
		if !strings.Contains(got, want) {
			t.Errorf("repository lacks %q\n%s", want, got)
		}
	}
}

func TestADryRunWritesNothing(t *testing.T) {
	root := fixture(t, "\n")
	before := read(t, root, "handler/paogen.go")
	res := run(t, root, false)
	if res.Converted == 0 || !res.DryRun || len(res.Written) != 0 {
		t.Fatalf("dry run: %+v", res)
	}
	if read(t, root, "handler/paogen.go") != before {
		t.Fatal("a dry run changed the file")
	}
}

func TestACRLFFileKeepsItsLineEndings(t *testing.T) {
	root := fixture(t, "\r\n")
	res := run(t, root, true)
	if res.Converted != 3 {
		t.Fatalf("converted %d on a CRLF file, want 3: %+v", res.Converted, res.Files)
	}
	got := read(t, root, "handler/paogen.go")
	if strings.Count(got, "\n") != strings.Count(got, "\r\n") {
		t.Error("CRLF file came back with LF lines")
	}
}

func TestRunningItTwiceChangesNothingTheSecondTime(t *testing.T) {
	root := fixture(t, "\n")
	run(t, root, true)
	first := read(t, root, "handler/paogen.go")
	res := run(t, root, true)
	if res.Converted != 0 {
		t.Errorf("second run converted %d", res.Converted)
	}
	if read(t, root, "handler/paogen.go") != first {
		t.Error("second run changed the file")
	}
}
