package rules

import (
	"strings"
	"testing"

	"gitlab.cept.gov.in/it-2.0/dakcoder/gotools/internal/workspace"
)

func TestClientSingleton(t *testing.T) {
	handler := `package handler

import (
	"time"

	"github.com/go-resty/resty/v2"
	serverRoute "gitlab.cept.gov.in/it-2.0-common/n-api-server/route"
)

type PensionHandler struct{}

func (h *PensionHandler) Fetch(sctx *serverRoute.Context, req struct{}) (*struct{}, error) {
	client := resty.New().SetTimeout(15 * time.Second)
	_ = client
	return nil, nil
}
`
	if got := lint(t, ClientSingleton.ID, map[string]string{"handler/pension.go": handler}); len(got) == 0 {
		t.Error("resty.New() inside a handler method rebuilds the pool per request; expected a finding")
	}

	// bootstrap/ is where a client is supposed to be built.
	boot := `package bootstrap

import "github.com/go-resty/resty/v2"

func NewRestyClient() *resty.Client { return resty.New() }
`
	if got := lint(t, ClientSingleton.ID, map[string]string{"bootstrap/clients.go": boot}); len(got) > 0 {
		t.Errorf("building the client in bootstrap is the remedy; got %v", got)
	}
}

func TestCtxPropagation(t *testing.T) {
	repo := repoFile(`func (r *UserRepository) Get(ctx context.Context) error {
	other := context.Background()
	_ = other
	return nil
}`)
	if got := lint(t, CtxPropagation.ID, map[string]string{"repo/postgres/user.go": repo}); len(got) == 0 {
		t.Error("context.Background() in a function holding a request context should be reported")
	}

	// main.go owns the process lifetime; a root context is right there.
	main := `package main

import "context"

func main() {
	ctx := context.Background()
	_ = ctx
}
`
	if got := lint(t, CtxPropagation.ID, map[string]string{"main.go": main}); len(got) > 0 {
		t.Errorf("main.go legitimately starts the context tree; got %v", got)
	}
}

func TestRepoNoLoggingAndSensitiveLogging(t *testing.T) {
	repo := `package postgres

import (
	"context"

	log "gitlab.cept.gov.in/it-2.0-common/n-api-log"
)

type UserRepository struct{}

func (r *UserRepository) Get(ctx context.Context) error {
	log.Error(ctx, "boom")
	return nil
}
`
	if got := lint(t, RepoNoLogging.ID, map[string]string{"repo/postgres/user.go": repo}); len(got) == 0 {
		t.Error("logging in the repository layer should be reported; the line has no request context")
	}

	handler := `package handler

import (
	"context"

	log "gitlab.cept.gov.in/it-2.0-common/n-api-log"
)

type H struct{}

func (h *H) Do(ctx context.Context, password string, req struct{ AadhaarNumber string }) {
	log.Info(ctx, "signing in %s", password)
	log.Info(ctx, "verifying %s", req.AadhaarNumber)
}
`
	got := lint(t, NoSensitiveLogging.ID, map[string]string{"handler/h.go": handler})
	if len(got) != 2 {
		t.Errorf("both a bare secret and a sensitive field should be reported; got %d: %v", len(got), got)
	}
}

// TestSensitiveMatchingIsWholeWord is the measurement that shaped the rule: a
// substring match on "pan" hits 20 field names in the review sheets and 19 are
// innocent. If this test ever fails, the rule has become the noise generator it
// was written to avoid.
func TestSensitiveMatchingIsWholeWord(t *testing.T) {
	cfg := DefaultConfig()
	for _, s := range []string{"AadhaarNumber", "Aadhaar_Number", "AadharNumber", "ReceiverAdharNo", "password", "OTP"} {
		if !cfg.IsSensitive(s) {
			t.Errorf("%q should be treated as sensitive", s)
		}
	}
	for _, s := range []string{"CompanyName", "Discrepancy", "NoOfPanchayatSanchaarSevaKendras", "AccountCode", "Description"} {
		if cfg.IsSensitive(s) {
			t.Errorf("%q is ordinary business vocabulary and must not match", s)
		}
	}
}

func TestRequestValidateDepth(t *testing.T) {
	req := `package handler

type CreateUserRequest struct {
	Name    string ` + "`json:\"name\" validate:\"required\"`" + `
	Age     int    ` + "`json:\"age\" validate:\"required,min=0,max=150\"`" + `
	Note    string ` + "`json:\"note\" validate:\"omitempty\"`" + `
	Bounded string ` + "`json:\"bounded\" validate:\"required,max=64\"`" + `
}
`
	got := lint(t, RequestValidateDepth.ID, map[string]string{"handler/request.go": req})
	if len(got) != 1 {
		t.Fatalf("only the unbounded string should be reported; got %d: %v", len(got), got)
	}
	if !strings.Contains(got[0].Message, "Name") {
		t.Errorf("message = %q, want it to name the unbounded field", got[0].Message)
	}
}

func TestPreferSwitchOnlyOnOneOperand(t *testing.T) {
	same := `package handler

func f(status string) int {
	if status == "a" {
		return 1
	} else if status == "b" {
		return 2
	} else if status == "c" {
		return 3
	}
	return 0
}
`
	if got := lint(t, PreferSwitch.ID, map[string]string{"handler/f.go": same}); len(got) == 0 {
		t.Error("a three-branch chain on one operand should be reported")
	}

	mixed := `package handler

func f(a, b, c string) int {
	if a == "x" {
		return 1
	} else if b == "y" {
		return 2
	} else if c == "z" {
		return 3
	}
	return 0
}
`
	if got := lint(t, PreferSwitch.ID, map[string]string{"handler/f.go": mixed}); len(got) > 0 {
		t.Errorf("branches testing different operands are not a switch; got %v", got)
	}
}

func TestMagicLiteralIgnoresSchemaAndFormats(t *testing.T) {
	// Column names and time layouts repeat by design. An earlier version of
	// this rule produced 407 findings on one service, almost all of them these.
	h := `package handler

func f() []string {
	return []string{
		"2006-01-02", "2006-01-02", "2006-01-02",
		"a.remarks", "a.remarks", "a.remarks",
		"Pending", "Pending", "Pending",
	}
}
`
	got := lint(t, MagicLiteral.ID, map[string]string{"handler/f.go": h})
	if len(got) != 1 {
		t.Fatalf("only the business string should be reported; got %d: %v", len(got), got)
	}
	if !strings.Contains(got[0].Message, "Pending") {
		t.Errorf("message = %q, want it to name the business string", got[0].Message)
	}
}

func TestAuditsAgreeWithTheRules(t *testing.T) {
	// The audits live in this package so a report and a lint finding cannot
	// disagree about what a database call is. This asserts that property
	// directly: the same method, counted twice.
	src := repoFile(`func (r *UserRepository) Do(ctx context.Context) error {
	qa := dblib.Psql.Select("id").From("users")
	if _, err := dblib.SelectOne(ctx, r.db, qa, pgx.RowToStructByName[domain.User]); err != nil {
		return err
	}
	qb := dblib.Psql.Select("id").From("roles")
	if _, err := dblib.SelectOne(ctx, r.db, qb, pgx.RowToStructByName[domain.User]); err != nil {
		return err
	}
	qc := dblib.Psql.Select("id").From("offices")
	_, err := dblib.SelectOne(ctx, r.db, qc, pgx.RowToStructByName[domain.User])
	return err
}`)
	files := map[string]string{"repo/postgres/user.go": src}

	ws, err := workspace.Load(mkws(t, files))
	if err != nil {
		t.Fatalf("load: %v", err)
	}
	reports := RoundTripAudit(ws)
	if len(reports) != 1 {
		t.Fatalf("audit reported %d methods, want 1", len(reports))
	}
	if reports[0].Statements != 3 {
		t.Errorf("audit counted %d statements, want 3", reports[0].Statements)
	}

	got := lint(t, RepoMultiRoundTrip.ID, files)
	if len(got) != 1 || !strings.Contains(got[0].Message, "3 separate database calls") {
		t.Errorf("the rule and the audit disagree: rule said %v, audit said %d statements",
			got, reports[0].Statements)
	}
}

// TestTemporalAuditReportsWithoutAdvising pins the decision in findings §10.3:
// the tool lists candidates and says nothing about where they should go.
func TestTemporalAuditReportsWithoutAdvising(t *testing.T) {
	h := `package handler

import "context"

type H struct{ MinioClient interface{ PutObject(context.Context) error } }

func (h *H) Upload(ctx context.Context) error {
	return h.MinioClient.PutObject(ctx)
}
`
	ws, err := workspace.Load(mkws(t, map[string]string{"handler/h.go": h}))
	if err != nil {
		t.Fatalf("load: %v", err)
	}
	got := TemporalAudit(ws)
	if len(got) != 1 {
		t.Fatalf("expected one file-storage candidate, got %d: %v", len(got), got)
	}
	if got[0].Kind != "file storage" {
		t.Errorf("kind = %q, want file storage", got[0].Kind)
	}
}
