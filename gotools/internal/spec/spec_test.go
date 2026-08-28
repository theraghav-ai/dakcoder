package spec

import (
	"errors"
	"strings"
	"testing"
)

func minimal() Resource {
	return Resource{
		Name:   "Pension",
		Fields: []Field{{Go: "Amount", Type: "float64"}},
	}
}

// issuePaths returns the paths of every issue, so a test can assert on which
// part of the spec was rejected without matching on prose.
func issuePaths(t *testing.T, err error) []string {
	t.Helper()
	var bad *InvalidSpecError
	if !errors.As(err, &bad) {
		t.Fatalf("want *InvalidSpecError, got %T: %v", err, err)
	}
	out := make([]string, 0, len(bad.Issues))
	for _, i := range bad.Issues {
		out = append(out, i.Path)
	}
	return out
}

func mustNormalise(t *testing.T, r Resource) Resource {
	t.Helper()
	out, err := r.Normalise()
	if err != nil {
		t.Fatalf("normalise: %v", err)
	}
	return out
}

func TestNormaliseInfersEverythingDerivable(t *testing.T) {
	got := mustNormalise(t, minimal())

	checks := map[string]string{
		"Plural":    got.Plural,
		"Table":     got.Table,
		"RouteBase": got.RouteBase,
	}
	want := map[string]string{
		"Plural":    "Pensions",
		"Table":     "pensions",
		"RouteBase": "/pensions",
	}
	for k, w := range want {
		if checks[k] != w {
			t.Errorf("%s = %q, want %q", k, checks[k], w)
		}
	}
	if len(got.Operations) != 5 {
		t.Errorf("operations = %v, want all five", got.Operations)
	}
	if got.Fields[0].JSON != "amount" || got.Fields[0].DB != "amount" {
		t.Errorf("tags = %q/%q, want amount/amount", got.Fields[0].JSON, got.Fields[0].DB)
	}
	// The default validate tag bounds the field, it does not merely require it.
	// Bare `required` on a numeric field is the defect the manual review of 41
	// services raised most often — 39 of them — so scaffolding it would be
	// generating the finding.
	if got.Fields[0].Validate != "required,min=0" {
		t.Errorf("default validate = %q, want a bound tag", got.Fields[0].Validate)
	}
	if got.Fields[0].SQL == "" {
		t.Error("sql type should be inferred from the Go type")
	}
}

// TestSpikeFailuresAreCorrectedOrRejected covers the two things the
// pre-implementation spike's model output actually got wrong. Both are
// mechanically preventable, which is why they are prevented here rather than
// prompted around.
func TestSpikeFailuresAreCorrectedOrRejected(t *testing.T) {
	t.Run("PpoNumber is corrected to PPONumber", func(t *testing.T) {
		r := minimal()
		r.Fields = []Field{{Go: "PpoNumber", Type: "string"}}
		got := mustNormalise(t, r)
		if got.Fields[0].Go != "PPONumber" {
			t.Errorf("field = %q, want PPONumber", got.Fields[0].Go)
		}
		if got.Fields[0].DB != "ppo_number" {
			t.Errorf("db = %q, want ppo_number", got.Fields[0].DB)
		}
	})

	t.Run("decimal.Decimal is rejected with a substitute", func(t *testing.T) {
		r := minimal()
		r.Fields = []Field{{Go: "Amount", Type: "decimal.Decimal"}}
		_, err := r.Normalise()
		if err == nil {
			t.Fatal("decimal.Decimal is not in the template's dependency set; it must be rejected")
		}
		var bad *InvalidSpecError
		if !errors.As(err, &bad) {
			t.Fatalf("want *InvalidSpecError, got %T", err)
		}
		if !strings.Contains(bad.Issues[0].Fix, "float64") {
			t.Errorf("the rejection must name the substitute, got fix %q", bad.Issues[0].Fix)
		}
	})
}

func TestTypeAliasesAreAccepted(t *testing.T) {
	for in, want := range map[string]string{
		"integer": "int", "boolean": "bool", "text": "string",
		"timestamp": "time.Time", "double": "float64", "float32": "float64",
	} {
		r := minimal()
		r.Fields = []Field{{Go: "Value", Type: in}}
		got := mustNormalise(t, r)
		if got.Fields[0].Type != want {
			t.Errorf("type %q normalised to %q, want %q", in, got.Fields[0].Type, want)
		}
	}
}

// TestHostileSpecValuesAreRefused is the injection guard. Every string here
// ends up inside generated Go or SQL, and the spec is model output — which
// plan.md §17 treats as untrusted, because a prompt injection in a source
// comment can reach it.
func TestHostileSpecValuesAreRefused(t *testing.T) {
	cases := []struct {
		name  string
		field Field
		want  string
	}{
		{"tag escape in validate", Field{Go: "X", Type: "string", Validate: "required\"` + evil + `\""}, "fields[0].validate"},
		{"statement injection in sql", Field{Go: "X", Type: "string", SQL: "varchar(10); DROP TABLE users"}, "fields[0].sql"},
		{"comment marker in sql", Field{Go: "X", Type: "string", SQL: "varchar(10) -- NOT NULL"}, "fields[0].sql"},
		{"newline in sql", Field{Go: "X", Type: "string", SQL: "varchar(10)\nDROP TABLE users"}, "fields[0].sql"},
		{"expression as field name", Field{Go: "X int `db:\"y\"`; Z", Type: "string"}, "fields[0].go"},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			r := minimal()
			r.Fields = []Field{tc.field}
			_, err := r.Normalise()
			if err == nil {
				t.Fatalf("expected %s to be rejected", tc.name)
			}
			paths := issuePaths(t, err)
			found := false
			for _, p := range paths {
				if p == tc.want {
					found = true
				}
			}
			if !found {
				t.Errorf("issues %v do not include %s", paths, tc.want)
			}
		})
	}
}

func TestReservedFieldsAreRejected(t *testing.T) {
	for _, name := range []string{"ID", "CreatedAt", "UpdatedAt"} {
		r := minimal()
		r.Fields = []Field{{Go: name, Type: "int64"}}
		if _, err := r.Normalise(); err == nil {
			t.Errorf("%s is supplied by the scaffolder and must be rejected", name)
		}
	}
}

// TestShadowingResourceNamesAreRejected: the generated handler writes
// `pension, err := ...`, so a resource whose camel form is an import alias
// would shadow it inside its own methods.
func TestShadowingResourceNamesAreRejected(t *testing.T) {
	for _, name := range []string{"Resp", "Port", "Repo", "Log", "Config"} {
		r := minimal()
		r.Name = name
		if _, err := r.Normalise(); err == nil {
			t.Errorf("resource name %q shadows a generated identifier and must be rejected", name)
		}
	}
}

func TestDuplicateFieldsAreRejected(t *testing.T) {
	r := minimal()
	r.Fields = []Field{
		{Go: "Amount", Type: "float64"},
		{Go: "amount", Type: "int"}, // normalises to the same Go name
	}
	if _, err := r.Normalise(); err == nil {
		t.Fatal("duplicate field names must be rejected")
	}

	r.Fields = []Field{
		{Go: "Amount", Type: "float64", DB: "value"},
		{Go: "Total", Type: "float64", DB: "value"},
	}
	if _, err := r.Normalise(); err == nil {
		t.Fatal("duplicate db columns must be rejected")
	}
}

// TestBoolFiltersAreRejected: a value-typed bool filter cannot distinguish
// false from absent, so it would silently constrain every request.
func TestBoolFiltersAreRejected(t *testing.T) {
	r := minimal()
	r.Fields = []Field{{Go: "Active", Type: "bool"}}
	r.ListFilters = []Filter{{Go: "Active"}}
	_, err := r.Normalise()
	if err == nil {
		t.Fatal("a bool list filter must be rejected")
	}
	var bad *InvalidSpecError
	if errors.As(err, &bad) && !strings.Contains(bad.Issues[0].Fix, "oneof") {
		t.Errorf("the rejection should suggest a workable alternative, got %q", bad.Issues[0].Fix)
	}
}

func TestFiltersMustNameADeclaredField(t *testing.T) {
	r := minimal()
	r.ListFilters = []Filter{{Go: "Nonexistent"}}
	_, err := r.Normalise()
	if err == nil {
		t.Fatal("a filter on an undeclared field cannot become a WHERE clause and must be rejected")
	}
	var bad *InvalidSpecError
	if errors.As(err, &bad) && !strings.Contains(bad.Issues[0].Fix, "Amount") {
		t.Errorf("the rejection should list the available fields, got %q", bad.Issues[0].Fix)
	}
}

func TestFiltersCopyTheirFieldsTypeAndColumn(t *testing.T) {
	r := minimal()
	r.Fields = []Field{{Go: "Status", Type: "string", DB: "pension_status"}}
	r.ListFilters = []Filter{{Go: "Status"}}
	got := mustNormalise(t, r)
	f := got.ListFilters[0]
	if f.Type != "string" || f.DB != "pension_status" {
		t.Errorf("filter carries type=%q db=%q, want string/pension_status", f.Type, f.DB)
	}
	if f.Form != "status" {
		t.Errorf("form = %q, want status", f.Form)
	}
}

func TestOperationSynonymsAndOrdering(t *testing.T) {
	r := minimal()
	r.Operations = []string{"delete", "read", "add", "index", "get"}
	got := mustNormalise(t, r)
	want := []string{"create", "list", "get", "delete"}
	if len(got.Operations) != len(want) {
		t.Fatalf("operations = %v, want %v", got.Operations, want)
	}
	for i := range want {
		if got.Operations[i] != want[i] {
			t.Fatalf("operations = %v, want %v (canonical order, deduplicated)", got.Operations, want)
		}
	}
}

func TestUnknownOperationIsRejected(t *testing.T) {
	r := minimal()
	r.Operations = []string{"create", "upsert"}
	if _, err := r.Normalise(); err == nil {
		t.Fatal("an unknown operation must be rejected rather than ignored")
	}
}

// TestEveryIssueCarriesAFix mirrors the rules engine's equivalent assertion: a
// finding the caller cannot act on is a finding they learn to ignore, and here
// the caller is a model that has exactly one turn to correct itself.
func TestEveryIssueCarriesAFix(t *testing.T) {
	bad := Resource{
		Name: "",
		Fields: []Field{
			{Go: "ID", Type: "string"},
			{Go: "Amount", Type: "decimal.Decimal"},
			{Go: "Bad Name", Type: "string"},
			{Go: "Sql", Type: "string", SQL: "varchar(1); DROP TABLE x"},
		},
		Operations:  []string{"frobnicate"},
		ListFilters: []Filter{{Go: "Missing"}},
	}
	_, err := bad.Normalise()
	if err == nil {
		t.Fatal("expected this spec to be rejected")
	}
	var bs *InvalidSpecError
	if !errors.As(err, &bs) {
		t.Fatalf("want *InvalidSpecError, got %T", err)
	}
	if len(bs.Issues) < 5 {
		t.Errorf("got %d issues; every problem should be reported at once so one turn fixes them all", len(bs.Issues))
	}
	for _, i := range bs.Issues {
		if i.Path == "" {
			t.Errorf("issue %q has no path", i.Message)
		}
		if i.Fix == "" {
			t.Errorf("issue %q at %s has no fix", i.Message, i.Path)
		}
	}
}

func TestUpdateValidateKeepsConstraintsButDropsRequired(t *testing.T) {
	tests := map[string]string{
		"required":                  "omitempty",
		"oneof=active closed":       "omitempty,oneof=active closed",
		"required,max=20":           "omitempty,max=20",
		"omitempty":                 "omitempty",
		"required,email":            "omitempty,email",
		"required,oneof=a b,max=10": "omitempty,oneof=a b,max=10",
		"":                          "omitempty",
	}
	for in, want := range tests {
		f := Field{Go: "X", Type: "string", Validate: in}
		if got := f.UpdateValidate(); got != want {
			t.Errorf("UpdateValidate(%q) = %q, want %q", in, got, want)
		}
	}
}

func TestSignatureGroupsConsecutiveSameTypedParams(t *testing.T) {
	r := mustNormalise(t, Resource{
		Name: "User",
		Fields: []Field{
			{Go: "FirstName", Type: "string"},
			{Go: "LastName", Type: "string"},
			{Go: "Age", Type: "int"},
			{Go: "City", Type: "string"},
			{Go: "Email", Type: "string"},
		},
	})
	got := SignatureOf(r.CreateParams())
	want := "firstName, lastName string, age int, city, email string"
	if got != want {
		t.Errorf("SignatureOf = %q,\nwant                %q", got, want)
	}
}

func TestSelectColumnsBracketTheResourceFields(t *testing.T) {
	r := mustNormalise(t, Resource{
		Name:   "Pension",
		Fields: []Field{{Go: "PpoNumber", Type: "string"}, {Go: "Amount", Type: "float64"}},
	})
	got := r.ReturningColumns()
	want := "id, ppo_number, amount, created_at, updated_at"
	if got != want {
		t.Errorf("ReturningColumns = %q, want %q", got, want)
	}
	if r.QuotedInsertColumns() != `"ppo_number", "amount"` {
		t.Errorf("QuotedInsertColumns = %s", r.QuotedInsertColumns())
	}
}

func TestImportDecisionsFollowTheOperations(t *testing.T) {
	base := Resource{Name: "Pension", Fields: []Field{{Go: "Amount", Type: "float64"}}}

	createOnly := base
	createOnly.Operations = []string{"create"}
	r := mustNormalise(t, createOnly)
	if r.NeedsSquirrel() {
		t.Error("a create-only resource has no WHERE clause and does not need the squirrel alias")
	}
	if !r.NeedsPgx() {
		t.Error("create uses InsertReturning with a row mapper, so pgx is needed")
	}

	withTime := base
	withTime.Fields = append(withTime.Fields, Field{Go: "SanctionDate", Type: "time.Time"})
	withTime.Operations = []string{"list"}
	r = mustNormalise(t, withTime)
	if r.NeedsTimeInRepo() {
		t.Error("a list-only resource never names time.Time in a signature")
	}
	if r.NeedsTimeInHandler() {
		t.Error("only the update path declares time.Time locals in the handler")
	}

	withTime.Operations = []string{"update"}
	r = mustNormalise(t, withTime)
	if !r.NeedsTimeInRepo() || !r.NeedsTimeInHandler() || !r.NeedsTimeInRequest() {
		t.Error("update carries time.Time through the request, handler and repository")
	}
}
