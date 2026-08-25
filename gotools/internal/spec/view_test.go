package spec

import (
	"strings"
	"testing"
)

// TestDerivedNamesMatchTheReferenceResource pins every generated identifier
// against the shipped `user` resource.
//
// These are one-line accessors, and the temptation is to trust them. They are
// the reason the generated handler calls `h.svc.GetAllUsers` and not
// `h.svc.ListUsers` — the repository and the handler use *different* names for
// the list operation, which is the reference's asymmetry and not one a reader
// would guess. A single wrong accessor produces code that does not compile, in a
// file nobody hand-wrote.
func TestDerivedNamesMatchTheReferenceResource(t *testing.T) {
	r := mustNormalise(t, Resource{
		Name:   "User",
		Fields: []Field{{Go: "FirstName", Type: "string"}},
	})

	got := map[string]string{
		"RepoType":      r.RepoType(),
		"HandlerType":   r.HandlerType(),
		"TableConst":    r.TableConst(),
		"FileStem":      r.FileStem(),
		"TableFileStem": r.TableFileStem(),
		"Var":           r.Var(),
		"PluralVar":     r.PluralVar(),
		"Title":         r.Title(),
		"PluralTitle":   r.PluralTitle(),
		"RepoCreate":    r.RepoCreate(),
		"RepoList":      r.RepoList(),
		"RepoGet":       r.RepoGet(),
		"RepoUpdate":    r.RepoUpdate(),
		"RepoDelete":    r.RepoDelete(),
		"HandlerCreate": r.HandlerCreate(),
		"HandlerList":   r.HandlerList(),
		"HandlerGet":    r.HandlerGet(),
		"HandlerUpdate": r.HandlerUpdate(),
		"HandlerDelete": r.HandlerDelete(),
		"CreateReq":     r.CreateReq(),
		"UpdateReq":     r.UpdateReq(),
		"IDUri":         r.IDUri(),
		"ListParams":    r.ListParams(),
		"ItemResp":      r.ItemResp(),
		"NewItemResp":   r.NewItemResp(),
		"NewItemsResp":  r.NewItemsResp(),
		"CreateResp":    r.CreateResp(),
		"FetchResp":     r.FetchResp(),
		"UpdateResp":    r.UpdateResp(),
		"DeleteResp":    r.DeleteResp(),
		"ListResp":      r.ListResp(),
	}
	want := map[string]string{
		"RepoType":      "UserRepository",
		"HandlerType":   "UserHandler",
		"TableConst":    "userTable",
		"FileStem":      "user",
		"TableFileStem": "users",
		"Var":           "user",
		"PluralVar":     "users",
		"Title":         "User",
		"PluralTitle":   "Users",
		// The repository lists with GetAll*, the handler with List*. That
		// asymmetry is the reference's.
		"RepoCreate":    "CreateUser",
		"RepoList":      "GetAllUsers",
		"RepoGet":       "GetUserByID",
		"RepoUpdate":    "UpdateUserByID",
		"RepoDelete":    "DeleteUserByID",
		"HandlerCreate": "CreateUser",
		"HandlerList":   "ListUsers",
		"HandlerGet":    "GetUserByID",
		"HandlerUpdate": "UpdateUserByID",
		"HandlerDelete": "DeleteUserByID",
		"CreateReq":     "CreateUserRequest",
		"UpdateReq":     "UpdateUserRequest",
		"IDUri":         "UserIDUri",
		"ListParams":    "ListUsersParams",
		"ItemResp":      "UserResponse",
		"NewItemResp":   "NewUserResponse",
		"NewItemsResp":  "NewUsersResponse",
		"CreateResp":    "UserCreateResponse",
		"FetchResp":     "UserFetchResponse",
		"UpdateResp":    "UserUpdateResponse",
		"DeleteResp":    "UserDeleteResponse",
		"ListResp":      "UsersListResponse",
	}
	for k, w := range want {
		if got[k] != w {
			t.Errorf("%s() = %q, want %q", k, got[k], w)
		}
	}
}

// TestDerivedNamesSurviveAnInitialism: PPONumber must not become PpoNumber
// anywhere downstream, and a resource whose own name carries one is the case
// that would show it.
func TestDerivedNamesSurviveAnInitialism(t *testing.T) {
	r := mustNormalise(t, Resource{
		Name:   "otp_request",
		Fields: []Field{{Go: "ppo_number", Type: "string"}},
	})
	if r.Name != "OTPRequest" {
		t.Fatalf("name = %q, want OTPRequest", r.Name)
	}
	checks := map[string]string{
		"RepoType":      "OTPRequestRepository",
		"TableConst":    "otpRequestTable",
		"FileStem":      "otp_request",
		"TableFileStem": "otp_requests",
		"Table":         "otp_requests",
		"RouteBase":     "/otp-requests",
		"Var":           "otpRequest",
		"PluralTitle":   "OTP Requests",
	}
	got := map[string]string{
		"RepoType": r.RepoType(), "TableConst": r.TableConst(),
		"FileStem": r.FileStem(), "TableFileStem": r.TableFileStem(),
		"Table": r.Table, "RouteBase": r.RouteBase,
		"Var": r.Var(), "PluralTitle": r.PluralTitle(),
	}
	for k, w := range checks {
		if got[k] != w {
			t.Errorf("%s = %q, want %q", k, got[k], w)
		}
	}
	if r.Fields[0].Var() != "ppoNumber" || r.Fields[0].GoType() != "string" {
		t.Errorf("field accessors: var=%q type=%q", r.Fields[0].Var(), r.Fields[0].GoType())
	}
	if r.Fields[0].PtrType() != "*string" {
		t.Errorf("PtrType = %q", r.Fields[0].PtrType())
	}
}

func TestArgsAndSelectorRendering(t *testing.T) {
	r := mustNormalise(t, Resource{
		Name: "Pension",
		Fields: []Field{
			{Go: "PpoNumber", Type: "string"},
			{Go: "Amount", Type: "float64"},
		},
	})
	if got := ArgsOf(r.CreateParams()); got != "ppoNumber, amount" {
		t.Errorf("ArgsOf = %q", got)
	}
	if got := SignatureOf(r.UpdateParams()); got != "ppoNumber *string, amount *float64" {
		t.Errorf("SignatureOf(update) = %q", got)
	}
	if got := SignatureOf(nil); got != "" {
		t.Errorf("SignatureOf(nil) = %q, want empty", got)
	}
}

func TestZeroTestsPerType(t *testing.T) {
	r := mustNormalise(t, Resource{
		Name: "Sample",
		Fields: []Field{
			{Go: "Label", Type: "string"},
			{Go: "Count", Type: "int"},
			{Go: "Amount", Type: "float64"},
			{Go: "Active", Type: "bool"},
			{Go: "OccurredAt", Type: "time.Time"},
		},
	})
	want := map[string]string{
		"Label":      `req.Label != ""`,
		"Count":      "req.Count != 0",
		"Amount":     "req.Amount != 0",
		"Active":     "", // indistinguishable from absent; always written
		"OccurredAt": "!req.OccurredAt.IsZero()",
	}
	for _, p := range r.UpdateParams() {
		if got := p.ZeroTest("req."); got != want[p.Field.Go] {
			t.Errorf("%s ZeroTest = %q, want %q", p.Field.Go, got, want[p.Field.Go])
		}
		if p.Field.Go == "Active" && !p.AlwaysSet() {
			t.Error("a bool has no usable zero test, so it must be marked always-set")
		}
	}
}

func TestRespTypeFormatsTimestamps(t *testing.T) {
	r := mustNormalise(t, Resource{
		Name: "Sample",
		Fields: []Field{
			{Go: "Label", Type: "string"},
			{Go: "OccurredAt", Type: "time.Time"},
		},
	})
	if got := r.Fields[0].RespType(); got != "string" {
		t.Errorf("RespType(string) = %q", got)
	}
	// Timestamps go over the wire pre-formatted, so every service renders them
	// identically regardless of the client's timezone handling.
	if got := r.Fields[1].RespType(); got != "string" {
		t.Errorf("RespType(time.Time) = %q, want string", got)
	}
	if !r.Fields[1].IsTime() {
		t.Error("IsTime should be true for a timestamp")
	}
}

func TestTypesListingIsStableAndComplete(t *testing.T) {
	got := Types()
	for _, want := range []string{"bool", "float64", "int", "int64", "string", "time.Time"} {
		found := false
		for _, g := range got {
			if g == want {
				found = true
			}
		}
		if !found {
			t.Errorf("%s is missing from Types()", want)
		}
	}
	if !strings.EqualFold(strings.Join(got, ","), strings.Join(Types(), ",")) {
		t.Error("Types() is not stable across calls; it appears in error messages")
	}
	if _, ok := Type("string"); !ok {
		t.Error("Type(string) should resolve")
	}
	if _, ok := Type("decimal.Decimal"); ok {
		t.Error("Type should not resolve a rejected type")
	}
}
