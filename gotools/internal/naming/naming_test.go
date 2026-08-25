package naming

import (
	"slices"
	"strings"
	"testing"
)

func TestWords(t *testing.T) {
	tests := []struct {
		in   string
		want []string
	}{
		{"PPONumber", []string{"PPO", "Number"}},
		{"ppo_number", []string{"ppo", "number"}},
		{"PpoNumber", []string{"Ppo", "Number"}},
		{"userID", []string{"user", "ID"}},
		{"UserID", []string{"User", "ID"}},
		{"ID", []string{"ID"}},
		{"CreatedAt", []string{"Created", "At"}},
		{"UTF8Encoding", []string{"UTF8", "Encoding"}},
		{"Address2", []string{"Address2"}},
		{"already-kebab-case", []string{"already", "kebab", "case"}},
		{"HTTPServerURL", []string{"HTTP", "Server", "URL"}},
		{"HTTPURL", []string{"HTTP", "URL"}}, // fully decomposable
		{"IDS", []string{"IDS"}},             // ID + stray S: left alone
		{"XYZQRS", []string{"XYZQRS"}},       // no decomposition at all
		{"UIDAPI", []string{"UID", "API"}},   // needs backtracking past UI
		{"", nil},
		{"___", nil},
	}
	for _, tc := range tests {
		if got := Words(tc.in); !slices.Equal(got, tc.want) {
			t.Errorf("Words(%q) = %v, want %v", tc.in, got, tc.want)
		}
	}
}

// TestPascalNormalisesInitialisms is the spike's Test-A failure as a
// regression: the model produced `PpoNumber` for a PPO number, and every
// generated tag, column and parameter downstream inherits the mistake.
func TestPascalNormalisesInitialisms(t *testing.T) {
	tests := map[string]string{
		"PpoNumber":     "PPONumber",
		"ppo_number":    "PPONumber",
		"ppoNumber":     "PPONumber",
		"userId":        "UserID",
		"user_id":       "UserID",
		"Id":            "ID",
		"httpUrl":       "HTTPURL",
		"first_name":    "FirstName",
		"hoa":           "HOA",
		"otp_reference": "OTPReference",
		"pension":       "Pension",
	}
	for in, want := range tests {
		if got := Pascal(in); got != want {
			t.Errorf("Pascal(%q) = %q, want %q", in, got, want)
		}
	}
}

func TestSnake(t *testing.T) {
	tests := map[string]string{
		"PPONumber": "ppo_number",
		"UserID":    "user_id",
		"ID":        "id",
		"CreatedAt": "created_at",
		"FirstName": "first_name",
		"Pension":   "pension",
		"HTTPURL":   "http_url",
		"Address2":  "address2",
	}
	for in, want := range tests {
		if got := Snake(in); got != want {
			t.Errorf("Snake(%q) = %q, want %q", in, got, want)
		}
	}
}

// TestCamelKeepsLeadingInitialismLowercase: `pPONumber` compiles but reads as a
// typo, and these names become repository parameters that reviewers read.
func TestCamelKeepsLeadingInitialismLowercase(t *testing.T) {
	tests := map[string]string{
		"PPONumber": "ppoNumber",
		"ID":        "id",
		"UserID":    "userID",
		"FirstName": "firstName",
		"Type":      "typeVal", // reserved word
		"Range":     "rangeVal",
	}
	for in, want := range tests {
		if got := Camel(in); got != want {
			t.Errorf("Camel(%q) = %q, want %q", in, got, want)
		}
	}
}

func TestPlural(t *testing.T) {
	tests := map[string]string{
		"Pension":    "Pensions",
		"User":       "Users",
		"Category":   "Categories",
		"Address":    "Addresses",
		"Box":        "Boxes",
		"Branch":     "Branches",
		"Dish":       "Dishes",
		"Person":     "People",
		"Status":     "Statuses",
		"Day":        "Days", // vowel before y
		"ppo_entry":  "ppo_entries",
		"Data":       "Data", // uncountable
		"PPO":        "PPOS", // fully upper-case input stays upper-case
		"OTPRequest": "OTPRequests",
	}
	for in, want := range tests {
		if got := Plural(in); got != want {
			t.Errorf("Plural(%q) = %q, want %q", in, got, want)
		}
	}
}

func TestKebabAndTitle(t *testing.T) {
	if got := Kebab("PPONumber"); got != "ppo-number" {
		t.Errorf("Kebab = %q", got)
	}
	if got := Title("GetPensionByID"); got != "Get Pension By ID" {
		t.Errorf("Title = %q", got)
	}
}

// TestSnakeRoundTripsThroughPascal guards the property the scaffolder actually
// depends on: a db tag written from a Go field name must map back to that same
// field name, or pgx.RowToStructByName silently fails to populate the column.
func TestSnakeRoundTripsThroughPascal(t *testing.T) {
	for _, name := range []string{
		"ID", "PPONumber", "FirstName", "CreatedAt", "HOACode", "Amount",
		"OTPReference", "UserID", "Status", "Address2",
	} {
		if got := Pascal(Snake(name)); got != name {
			t.Errorf("Pascal(Snake(%q)) = %q; the db tag would not map back", name, got)
		}
	}
}

func TestIsGoKeyword(t *testing.T) {
	if !IsGoKeyword("type") || !IsGoKeyword("range") {
		t.Error("expected type and range to be keywords")
	}
	if IsGoKeyword("Type") || IsGoKeyword("pension") {
		t.Error("unexpected keyword")
	}
}

func TestInitialismsAreUpperCase(t *testing.T) {
	// A lower-case entry would never match, because every lookup upper-cases
	// first — so it would be a silently dead entry.
	for k := range initialisms {
		if k != strings.ToUpper(k) {
			t.Errorf("initialism %q is not upper-case and can never match", k)
		}
	}
}
