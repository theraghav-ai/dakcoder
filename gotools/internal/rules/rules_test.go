package rules

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
)

// mkws writes an inline workspace to a temp dir and returns its root.
//
// Inline fixtures rather than testdata trees: the input and the expectation sit
// side by side in one table, so a reviewer can see what a rule fires on without
// opening six files. The real corpora are covered separately by the baseline
// tests below.
func mkws(t *testing.T, files map[string]string) string {
	t.Helper()
	root := t.TempDir()
	if _, ok := files["go.mod"]; !ok {
		files["go.mod"] = "module pisapi\n\ngo 1.25.0\n"
	}
	for rel, body := range files {
		p := filepath.Join(root, filepath.FromSlash(rel))
		if err := os.MkdirAll(filepath.Dir(p), 0o755); err != nil {
			t.Fatalf("mkdir %s: %v", rel, err)
		}
		if err := os.WriteFile(p, []byte(body), 0o644); err != nil {
			t.Fatalf("write %s: %v", rel, err)
		}
	}
	return root
}

// lint runs one rule over an inline workspace and returns its violations.
func lint(t *testing.T, ruleID string, files map[string]string) []Violation {
	t.Helper()
	res, err := Analyze(mkws(t, files), RunOptions{Only: []string{ruleID}})
	if err != nil {
		t.Fatalf("analyze: %v", err)
	}
	return append(append([]Violation{}, res.Violations...), res.Warnings...)
}

// ── shared fixture fragments ────────────────────────────────────────────────

const goodHandler = `package handler

import (
	"pisapi/core/port"
	resp "pisapi/handler/response"
	repo "pisapi/repo/postgres"

	log "gitlab.cept.gov.in/it-2.0-common/n-api-log"
	serverHandler "gitlab.cept.gov.in/it-2.0-common/n-api-server/handler"
	serverRoute "gitlab.cept.gov.in/it-2.0-common/n-api-server/route"
)

type UserHandler struct {
	*serverHandler.Base
	svc *repo.UserRepository
}

func NewUserHandler(svc *repo.UserRepository) *UserHandler {
	base := serverHandler.New("Users").SetPrefix("/v1").AddPrefix("")
	return &UserHandler{Base: base, svc: svc}
}

func (h *UserHandler) Routes() []serverRoute.Route {
	return []serverRoute.Route{
		serverRoute.GET("/users", h.ListUsers).Name("List Users"),
	}
}

func (h *UserHandler) ListUsers(sctx *serverRoute.Context, _ struct{}) (*resp.UsersListResponse, error) {
	users, err := h.svc.GetAllUsers(sctx.Ctx)
	if err != nil {
		log.Error(sctx.Ctx, "Error fetching users: %v", err)
		return nil, err
	}
	return &resp.UsersListResponse{StatusCodeAndMessage: port.ListSuccess, Data: resp.NewUsersResponse(users)}, nil
}
`

const goodBootstrap = `package bootstrap

import (
	handler "pisapi/handler"
	repo "pisapi/repo/postgres"

	serverHandler "gitlab.cept.gov.in/it-2.0-common/n-api-server/handler"
	"go.uber.org/fx"
)

var FxRepo = fx.Module("Repomodule", fx.Provide(repo.NewUserRepository))

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

const goodRepo = `package repo

import (
	"context"

	"pisapi/core/domain"

	"github.com/jackc/pgx/v5"
	config "gitlab.cept.gov.in/it-2.0-common/api-config"
	dblib "gitlab.cept.gov.in/it-2.0-common/n-api-db"
)

type UserRepository struct {
	db  *dblib.DB
	cfg *config.Config
}

func NewUserRepository(db *dblib.DB, cfg *config.Config) *UserRepository {
	return &UserRepository{db: db, cfg: cfg}
}

func (r *UserRepository) GetAllUsers(ctx context.Context) ([]domain.User, error) {
	ctx, cancel := context.WithTimeout(ctx, r.cfg.GetDuration("db.QueryTimeoutMed"))
	defer cancel()
	q := dblib.Psql.Select("id").From("user_details")
	return dblib.SelectRows(ctx, r.db, q, pgx.RowToStructByName[domain.User])
}
`

// ── table-driven rule tests ─────────────────────────────────────────────────

func TestRules(t *testing.T) {
	cases := []struct {
		name    string
		rule    string
		files   map[string]string
		wantN   int
		wantSub string // substring the message must contain
	}{
		{
			name: "layer-sql-boundary/clean",
			rule: "layer-sql-boundary",
			files: map[string]string{
				"repo/postgres/user.go": goodRepo,
			},
			wantN: 0,
		},
		{
			name: "layer-sql-boundary/squirrel in handler",
			rule: "layer-sql-boundary",
			files: map[string]string{
				"handler/user.go": "package handler\n\nimport sq \"github.com/Masterminds/squirrel\"\n\nvar _ = sq.Eq{}\n",
			},
			wantN:   1,
			wantSub: "SQL belongs in repo/",
		},
		{
			name: "layer-dto-boundary/repo imports handler",
			rule: "layer-dto-boundary",
			files: map[string]string{
				"repo/postgres/user.go": "package repo\n\nimport resp \"pisapi/handler/response\"\n\nvar _ = resp.UserResponse{}\n",
			},
			wantN:   1,
			wantSub: "must not know about DTOs",
		},
		{
			name:  "handler-signature/clean",
			rule:  "handler-signature",
			files: map[string]string{"handler/user.go": goodHandler},
			wantN: 0,
		},
		{
			name: "handler-signature/gin context",
			rule: "handler-signature",
			files: map[string]string{
				"handler/user.go": `package handler

import "github.com/gin-gonic/gin"

type UserHandler struct{}

func (h *UserHandler) Get(c *gin.Context) {}
`,
			},
			wantN:   1,
			wantSub: "must not depend on gin",
		},
		{
			name: "handler-signature/ShouldBind",
			rule: "handler-signature",
			files: map[string]string{
				"handler/user.go": `package handler

type UserHandler struct{}

type ctx struct{}

func (c *ctx) ShouldBindJSON(any) error { return nil }

func (h *UserHandler) Create(c *ctx, req struct{}) (*struct{}, error) {
	_ = c.ShouldBindJSON(&req)
	return nil, nil
}
`,
			},
			wantN:   2, // manual bind + wrong first-param type
			wantSub: "framework binds and validates",
		},
		{
			name:  "handler-base/clean",
			rule:  "handler-base",
			files: map[string]string{"handler/user.go": goodHandler},
			wantN: 0,
		},
		{
			name: "handler-base/missing embed",
			rule: "handler-base",
			files: map[string]string{
				"handler/user.go": `package handler

type UserHandler struct {
	svc int
}
`,
			},
			wantN:   1,
			wantSub: "does not embed *serverHandler.Base",
		},
		{
			name:  "routes-in-handler/clean",
			rule:  "routes-in-handler",
			files: map[string]string{"handler/user.go": goodHandler},
			wantN: 0,
		},
		{
			name: "routes-in-handler/route without Name",
			rule: "routes-in-handler",
			files: map[string]string{
				"handler/user.go": `package handler

import serverRoute "gitlab.cept.gov.in/it-2.0-common/n-api-server/route"

type UserHandler struct{}

func (h *UserHandler) Routes() []serverRoute.Route {
	return []serverRoute.Route{serverRoute.GET("/users", nil)}
}
`,
			},
			wantN:   1,
			wantSub: "missing from docs/v3Doc.json",
		},
		{
			name:  "repo-contract/clean",
			rule:  "repo-contract",
			files: map[string]string{"repo/postgres/user.go": goodRepo},
			wantN: 0,
		},
		{
			name: "repo-contract/raw squirrel",
			rule: "repo-contract",
			files: map[string]string{
				"repo/postgres/user.go": `package repo

import (
	"context"

	sq "github.com/Masterminds/squirrel"
	config "gitlab.cept.gov.in/it-2.0-common/api-config"
	dblib "gitlab.cept.gov.in/it-2.0-common/n-api-db"
)

type UserRepository struct {
	db  *dblib.DB
	cfg *config.Config
}

func NewUserRepository(db *dblib.DB, cfg *config.Config) *UserRepository { return nil }

func (r *UserRepository) Get(ctx context.Context) error {
	ctx, cancel := context.WithTimeout(ctx, r.cfg.GetDuration("db.QueryTimeoutLow"))
	defer cancel()
	q := sq.Select("id").From("t")
	_, err := dblib.SelectOne(ctx, r.db, q, nil)
	return err
}
`,
			},
			wantN:   1, // repo-rowmapper owns the mapper check; this rule owns the builder
			wantSub: "Postgres rejects",
		},
		{
			name: "repo-contract/missing timeout",
			rule: "repo-contract",
			files: map[string]string{
				"repo/postgres/user.go": `package repo

import (
	"context"

	config "gitlab.cept.gov.in/it-2.0-common/api-config"
	dblib "gitlab.cept.gov.in/it-2.0-common/n-api-db"
)

type UserRepository struct {
	db  *dblib.DB
	cfg *config.Config
}

func NewUserRepository(db *dblib.DB, cfg *config.Config) *UserRepository { return nil }

func (r *UserRepository) Get(ctx context.Context) error {
	q := dblib.Psql.Select("id").From("t")
	_, err := dblib.Insert(ctx, r.db, q)
	return err
}
`,
			},
			wantN:   1,
			wantSub: "without context.WithTimeout",
		},
		{
			name: "repo-norows/delete without ErrNoRows",
			rule: "repo-norows",
			files: map[string]string{
				"repo/postgres/user.go": `package repo

import (
	"context"

	dblib "gitlab.cept.gov.in/it-2.0-common/n-api-db"
)

type UserRepository struct{ db *dblib.DB }

func (r *UserRepository) Del(ctx context.Context) error {
	_, err := dblib.Delete(ctx, r.db, nil)
	return err
}
`,
			},
			wantN:   1,
			wantSub: "zero-row write",
		},
		{
			name: "domain-tags/missing db tag",
			rule: "domain-tags",
			files: map[string]string{
				"core/domain/user.go": "package domain\n\nimport \"time\"\n\ntype User struct {\n\tID int64 `json:\"id\"`\n\tCreatedAt time.Time `json:\"created_at\" db:\"created_at\"`\n\tUpdatedAt time.Time `json:\"updated_at\" db:\"updated_at\"`\n}\n",
			},
			wantN:   1,
			wantSub: "row scanning will fail",
		},
		{
			name: "domain-tags/missing standard field",
			rule: "domain-tags",
			files: map[string]string{
				"core/domain/user.go": "package domain\n\ntype User struct {\n\tID int64 `json:\"id\" db:\"id\"`\n}\n",
			},
			wantN:   2, // CreatedAt + UpdatedAt
			wantSub: "standard field",
		},
		{
			name: "request-dto/struct outside request.go",
			rule: "request-dto",
			files: map[string]string{
				"handler/user.go": "package handler\n\ntype CreateUserRequest struct {\n\tName string `json:\"name\" validate:\"required\"`\n}\n",
			},
			wantN:   1,
			wantSub: "no generated validator",
		},
		{
			name: "request-dto/missing validate tag",
			rule: "request-dto",
			files: map[string]string{
				"handler/request.go": "package handler\n\ntype CreateUserRequest struct {\n\tName string `json:\"name\"`\n}\n",
			},
			wantN:   1,
			wantSub: "will not be validated",
		},
		{
			name: "response-dto/embed without inline",
			rule: "response-dto",
			files: map[string]string{
				"handler/response/user.go": "package response\n\nimport \"pisapi/core/port\"\n\ntype UserCreateResponse struct {\n\tport.StatusCodeAndMessage\n}\n",
			},
			wantN:   1,
			wantSub: "would be nested",
		},
		{
			name: "response-dto/list missing metadata",
			rule: "response-dto",
			files: map[string]string{
				"handler/response/user.go": "package response\n\nimport \"pisapi/core/port\"\n\ntype UsersListResponse struct {\n\tport.StatusCodeAndMessage `json:\",inline\"`\n}\n",
			},
			wantN:   1,
			wantSub: "port.MetaDataResponse",
		},
		{
			name: "fx-registration/handler not registered",
			rule: "fx-registration",
			files: map[string]string{
				"handler/user.go":           goodHandler,
				"bootstrap/bootstrapper.go": "package bootstrap\n\nimport \"go.uber.org/fx\"\n\nvar FxHandler = fx.Module(\"h\")\n",
				"repo/postgres/user.go":     goodRepo,
			},
			wantN:   2, // handler + repo both unregistered
			wantSub: "Uber-FX will fail at startup",
		},
		{
			name: "fx-registration/handler registered without ResultTags",
			rule: "fx-registration",
			files: map[string]string{
				"handler/user.go": goodHandler,
				"bootstrap/bootstrapper.go": `package bootstrap

import (
	handler "pisapi/handler"

	serverHandler "gitlab.cept.gov.in/it-2.0-common/n-api-server/handler"
	"go.uber.org/fx"
)

var FxHandler = fx.Module("h", fx.Provide(
	fx.Annotate(handler.NewUserHandler, fx.As(new(serverHandler.Handler))),
))
`,
			},
			wantN:   1,
			wantSub: "routes will not be served",
		},
		{
			name: "fx-registration/plain provide for a handler",
			rule: "fx-registration",
			files: map[string]string{
				"handler/user.go":           goodHandler,
				"bootstrap/bootstrapper.go": "package bootstrap\n\nimport (\n\thandler \"pisapi/handler\"\n\n\t\"go.uber.org/fx\"\n)\n\nvar FxHandler = fx.Module(\"h\", fx.Provide(handler.NewUserHandler))\n",
			},
			wantN:   1,
			wantSub: "silently do not serve",
		},
		{
			name:  "fx-registration/clean",
			rule:  "fx-registration",
			files: map[string]string{"handler/user.go": goodHandler, "repo/postgres/user.go": goodRepo, "bootstrap/bootstrapper.go": goodBootstrap},
			wantN: 0,
		},
		{
			name:  "error-handling/clean",
			rule:  "error-handling",
			files: map[string]string{"handler/user.go": goodHandler},
			wantN: 0,
		},
		{
			name: "error-handling/unlogged return",
			rule: "error-handling",
			files: map[string]string{
				"handler/user.go": `package handler

import serverRoute "gitlab.cept.gov.in/it-2.0-common/n-api-server/route"

type UserHandler struct{}

func (h *UserHandler) Get(sctx *serverRoute.Context, _ struct{}) (*struct{}, error) {
	err := doThing()
	if err != nil {
		return nil, err
	}
	return nil, nil
}

func doThing() error { return nil }
`,
			},
			wantN:   1,
			wantSub: "without logging it first",
		},
		{
			name: "dep-allowlist/disallowed direct dep",
			rule: "dep-allowlist",
			files: map[string]string{
				"handler/user.go": "package handler\n\nimport _ \"github.com/sirupsen/logrus\"\n",
			},
			wantN:   1,
			wantSub: "not on the approved list",
		},
		{
			name: "dep-allowlist/stdlib and first-party allowed",
			rule: "dep-allowlist",
			files: map[string]string{
				"handler/user.go": "package handler\n\nimport (\n\t_ \"context\"\n\t_ \"pisapi/core/domain\"\n\t_ \"go.uber.org/fx\"\n)\n",
			},
			wantN: 0,
		},
		{
			name: "dep-allowlist/test deps exempt",
			rule: "dep-allowlist",
			files: map[string]string{
				"handler/user_test.go": "package handler\n\nimport _ \"github.com/stretchr/testify/assert\"\n",
			},
			wantN: 0,
		},
		{
			name: "legacy-lib-generation/api-server",
			rule: "legacy-lib-generation",
			files: map[string]string{
				"handler/user.go": "package handler\n\nimport _ \"gitlab.cept.gov.in/it-2.0-common/api-server\"\n",
			},
			wantN:   1,
			wantSub: "n-api-server",
		},
		{
			name: "legacy-manual-validation/binding tag",
			rule: "legacy-manual-validation",
			files: map[string]string{
				"handler/request.go": "package handler\n\ntype Req struct {\n\tName string `json:\"name\" binding:\"required\"`\n}\n",
			},
			wantN:   1,
			wantSub: "govalid reads validate",
		},
		{
			name: "legacy-response-helper/handleSuccess",
			rule: "legacy-response-helper",
			files: map[string]string{
				"handler/user.go": "package handler\n\ntype UserHandler struct{}\n\nfunc (h *UserHandler) Get() { handleSuccess(nil, nil) }\n\nfunc handleSuccess(a, b any) {}\n",
			},
			wantN:   1,
			wantSub: "typed response structs",
		},
	}

	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			got := lint(t, tc.rule, tc.files)
			if len(got) != tc.wantN {
				t.Errorf("got %d violation(s), want %d", len(got), tc.wantN)
				for _, v := range got {
					t.Logf("  %s", v)
				}
				return
			}
			if tc.wantSub == "" {
				return
			}
			for _, v := range got {
				if strings.Contains(v.Message, tc.wantSub) {
					return
				}
			}
			t.Errorf("no violation message contained %q", tc.wantSub)
			for _, v := range got {
				t.Logf("  %s", v)
			}
		})
	}
}

// TestEveryViolationHasAFixAndCitation is the rule that keeps the rules usable.
//
// A finding the developer cannot act on is a finding they learn to ignore, and
// a rule without a citation reads as the tool being opinionated rather than
// enforcing an agreed standard. Both are enforced mechanically so a new rule
// cannot regress them.
func TestEveryViolationHasAFixAndCitation(t *testing.T) {
	for _, r := range Default().All() {
		if r.Citation == "" {
			t.Errorf("rule %s has no Citation", r.ID)
		}
		if r.Summary == "" {
			t.Errorf("rule %s has no Summary", r.ID)
		}
	}

	// Exercise a workspace that trips a broad set of rules and assert every
	// resulting violation carries a remedy.
	root := mkws(t, map[string]string{
		"handler/user.go":           "package handler\n\nimport \"github.com/gin-gonic/gin\"\n\ntype UserHandler struct{}\n\nfunc (h *UserHandler) Get(c *gin.Context) {}\n",
		"core/domain/user.go":       "package domain\n\ntype User struct {\n\tName string\n}\n",
		"repo/postgres/user.go":     "package repo\n\nimport resp \"pisapi/handler/response\"\n\nvar _ = resp.X\n",
		"bootstrap/bootstrapper.go": "package bootstrap\n",
	})
	res, err := Analyze(root, RunOptions{})
	if err != nil {
		t.Fatalf("analyze: %v", err)
	}
	all := append(append([]Violation{}, res.Violations...), res.Warnings...)
	if len(all) == 0 {
		t.Fatal("expected violations from a deliberately broken workspace")
	}
	for _, v := range all {
		if v.Fix == "" {
			t.Errorf("violation without a fix: [%s] %s — %s", v.Rule, v.Path, v.Message)
		}
		if v.Citation == "" {
			t.Errorf("violation without a citation: [%s] %s", v.Rule, v.Path)
		}
		if v.Path == "" {
			t.Errorf("violation without a path: [%s] %s", v.Rule, v.Message)
		}
	}
}

// TestScopingDoesNotBlockOutOfScope pins the behaviour that stops the agent
// wandering off to fix unrelated legacy code: violations in files the caller
// did not name are reported but never block.
func TestScopingDoesNotBlockOutOfScope(t *testing.T) {
	root := mkws(t, map[string]string{
		"handler/touched.go":   "package handler\n\nimport \"github.com/gin-gonic/gin\"\n\ntype AHandler struct{}\n\nfunc (h *AHandler) Get(c *gin.Context) {}\n",
		"handler/untouched.go": "package handler\n\nimport \"github.com/gin-gonic/gin\"\n\ntype BHandler struct{}\n\nfunc (h *BHandler) Get(c *gin.Context) {}\n",
	})
	res, err := Analyze(root, RunOptions{
		Only:  []string{"handler-signature"},
		Scope: []string{"handler/touched.go"},
	})
	if err != nil {
		t.Fatalf("analyze: %v", err)
	}
	if res.Count != 1 {
		t.Errorf("blocking count = %d, want 1", res.Count)
	}
	if res.OutOfScopeCount != 1 {
		t.Errorf("out-of-scope count = %d, want 1", res.OutOfScopeCount)
	}
	if res.OK {
		t.Error("OK should be false when an in-scope violation exists")
	}
	for _, v := range res.Violations {
		if v.Path != "handler/touched.go" {
			t.Errorf("blocking violation on out-of-scope file %s", v.Path)
		}
	}
}

// TestScopingWithOnlyOutOfScopeViolationsPasses is the other half: if every
// finding is pre-existing, the run succeeds.
func TestScopingWithOnlyOutOfScopeViolationsPasses(t *testing.T) {
	root := mkws(t, map[string]string{
		"handler/clean.go":     goodHandler,
		"handler/untouched.go": "package handler\n\nimport \"github.com/gin-gonic/gin\"\n\ntype BHandler struct{}\n\nfunc (h *BHandler) Get(c *gin.Context) {}\n",
	})
	res, err := Analyze(root, RunOptions{
		Only:  []string{"handler-signature"},
		Scope: []string{"handler/clean.go"},
	})
	if err != nil {
		t.Fatalf("analyze: %v", err)
	}
	if !res.OK {
		t.Errorf("expected OK when all findings are out of scope; got %d blocking", res.Count)
	}
	if res.OutOfScopeCount != 1 {
		t.Errorf("out-of-scope count = %d, want 1", res.OutOfScopeCount)
	}
}

// TestUnknownRuleIsAnError: silently running fewer rules than the caller asked
// for is worse than failing, because the caller believes it was checked.
func TestUnknownRuleIsAnError(t *testing.T) {
	root := mkws(t, map[string]string{"handler/user.go": goodHandler})
	_, err := Analyze(root, RunOptions{Only: []string{"no-such-rule"}})
	if err == nil {
		t.Fatal("expected an error for an unknown rule id")
	}
	if !strings.Contains(err.Error(), "no-such-rule") {
		t.Errorf("error should name the unknown rule, got: %v", err)
	}
}

// TestGeneratedFilesAreSkipped: govalid validators must never be linted as if
// they were hand-written, because the agent cannot act on the findings.
func TestGeneratedFilesAreSkipped(t *testing.T) {
	root := mkws(t, map[string]string{
		"handler/request_x_validator.go": "// Code generated by govalid; DO NOT EDIT.\npackage handler\n\nimport _ \"github.com/sirupsen/logrus\"\n",
	})
	res, err := Analyze(root, RunOptions{Only: []string{"dep-allowlist"}})
	if err != nil {
		t.Fatalf("analyze: %v", err)
	}
	if !res.OK {
		t.Errorf("generated file should not produce violations; got %d", res.Count)
		for _, v := range res.Violations {
			t.Logf("  %s", v)
		}
	}
}

// TestLegacyRulesAreNotRunByDefault: mixing 1,300 legacy findings into an
// ordinary edit-loop lint would bury the two the developer actually caused.
func TestLegacyRulesAreNotRunByDefault(t *testing.T) {
	files := map[string]string{
		"handler/user.go": "package handler\n\nimport _ \"gitlab.cept.gov.in/it-2.0-common/api-server\"\n",
	}
	root := mkws(t, files)

	compliance, err := Analyze(root, RunOptions{})
	if err != nil {
		t.Fatalf("analyze: %v", err)
	}
	for _, v := range compliance.Violations {
		if strings.HasPrefix(v.Rule, "legacy-") {
			t.Errorf("legacy rule %s ran during a compliance lint", v.Rule)
		}
	}

	legacy, err := Analyze(root, RunOptions{Legacy: true})
	if err != nil {
		t.Fatalf("analyze legacy: %v", err)
	}
	found := false
	for _, v := range legacy.Violations {
		if v.Rule == "legacy-lib-generation" {
			found = true
		}
	}
	if !found {
		t.Error("legacy-lib-generation did not fire during a legacy audit")
	}
}

// ── baseline assertions against the real corpora ────────────────────────────

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

// TestReferenceTemplateIsClean is the load-bearing assertion of the whole
// suite: the shipped reference resource must satisfy every rule.
//
// If this fails, the rule is wrong — not the template. It has already earned
// its keep once, catching two false positives (an unqualified selector chain
// and an unhandled new(T) in fx.As) that would otherwise have shipped.
func TestReferenceTemplateIsClean(t *testing.T) {
	res, err := Analyze(corpus(t, "new-template"), RunOptions{})
	if err != nil {
		t.Fatalf("analyze: %v", err)
	}
	if !res.OK {
		t.Errorf("reference template produced %d violation(s); the rule is wrong, not the template", res.Count)
		for _, v := range res.Violations {
			t.Errorf("  %s", v)
		}
	}
	for _, v := range res.Warnings {
		t.Logf("warning on reference template: %s", v)
	}
}

// TestLegacyServiceTriggersExpectedRules asserts the other direction: a real
// pre-template service must trip the legacy rules, and specifically the ones we
// know apply to it.
func TestLegacyServiceTriggersExpectedRules(t *testing.T) {
	res, err := Analyze(corpus(t, "pao-back-end-development"), RunOptions{Legacy: true})
	if err != nil {
		t.Fatalf("analyze: %v", err)
	}
	seen := map[string]bool{}
	for _, v := range append(append([]Violation{}, res.Violations...), res.Warnings...) {
		seen[v.Rule] = true
	}
	for _, want := range []string{
		"legacy-lib-generation",
		"legacy-routes-file",
		"legacy-gin-handler",
		"legacy-manual-validation",
		"legacy-response-helper",
		"legacy-swaggo",
	} {
		if !seen[want] {
			t.Errorf("expected %s to fire on the legacy corpus", want)
		}
	}
}

// TestLegacyHandmadeHealthDoesNotFlagTests is a regression for a false positive
// found on the real legacy corpus: the rule matched
// `TestHealthCheckHandler_Success`, a test exercising the very handler it was
// meant to find. A warning nobody can act on is how people learn to skip
// warnings.
func TestLegacyHandmadeHealthDoesNotFlagTests(t *testing.T) {
	files := map[string]string{
		"routes/routes.go": `package routes

import "github.com/gin-gonic/gin"

func HealthCheckHandler(c *gin.Context) {
	c.JSON(200, gin.H{"status": "ok"})
}
`,
		"tests/health_test.go": `package tests

import "testing"

func TestHealthCheckHandler_Success(t *testing.T) {}

func TestHealthzEndpoint(t *testing.T) {}
`,
		"internal/probe.go": `package internal

// A plain helper with a healthcheck-ish name and no HTTP parameter.
func healthcheckInterval() int { return 30 }
`,
	}
	root := mkws(t, files)
	res, err := Analyze(root, RunOptions{Legacy: true, Only: []string{"legacy-handmade-health"}})
	if err != nil {
		t.Fatalf("analyze: %v", err)
	}
	all := append(append([]Violation{}, res.Violations...), res.Warnings...)
	if len(all) != 1 {
		t.Fatalf("got %d finding(s), want exactly the gin handler:\n%v", len(all), all)
	}
	if all[0].Path != "routes/routes.go" {
		t.Errorf("finding is on %s, want routes/routes.go", all[0].Path)
	}
}

// TestRepoRowMapperAcceptsEveryByNameMapper: the rule's intent is "bind by
// name", not "call this one function". A scalar COUNT(*) read needs pgx.RowTo,
// and the returning variants take a mapper too — rejecting either would make
// the scaffolder's own output fail its own linter.
func TestRepoRowMapperAcceptsEveryByNameMapper(t *testing.T) {
	accepted := []string{
		`dblib.SelectOne(ctx, r.db, q, pgx.RowToStructByName[domain.User])`,
		`dblib.SelectRows(ctx, r.db, q, pgx.RowToAddrOfStructByName[domain.User])`,
		`dblib.SelectOne(ctx, r.db, q, pgx.RowToStructByNameLax[domain.User])`,
		`dblib.SelectOne(ctx, r.db, q, pgx.RowTo[int])`,
		`dblib.InsertReturning(ctx, r.db, ins, pgx.RowToStructByName[domain.User])`,
		`dblib.UpdateReturning(ctx, r.db, b, pgx.RowToStructByName[domain.User])`,
	}
	for _, call := range accepted {
		src := "package repo\n\nfunc f() {\n\t_ = " + call + "\n}\n"
		if vs := lint(t, "repo-rowmapper", map[string]string{"repo/postgres/x.go": src}); len(vs) != 0 {
			t.Errorf("%s should be accepted, got: %v", call, vs)
		}
	}

	rejected := map[string]string{
		"positional mapping silently shifts every field when a column is added": `dblib.SelectOne(ctx, r.db, q, pgx.RowToStructByPos[domain.User])`,
		"a hand-rolled mapper bypasses the db tags entirely":                    `dblib.SelectRows(ctx, r.db, q, scanUser)`,
		"a missing mapper is a compile error waiting to happen":                 `dblib.SelectOne(ctx, r.db, q)`,
	}
	for why, call := range rejected {
		src := "package repo\n\nfunc f() {\n\t_ = " + call + "\n}\n"
		if vs := lint(t, "repo-rowmapper", map[string]string{"repo/postgres/x.go": src}); len(vs) == 0 {
			t.Errorf("%s should be rejected — %s", call, why)
		}
	}
}
