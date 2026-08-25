package rules

import (
	"go/ast"
	"go/parser"
	"go/token"
	"strings"
	"testing"
)

// parseExpr parses a single expression for testing helpers in isolation.
func parseExpr(t *testing.T, src string) ast.Expr {
	t.Helper()
	e, err := parser.ParseExpr(src)
	if err != nil {
		t.Fatalf("parse %q: %v", src, err)
	}
	return e
}

// TestSelectorChain is a regression test.
//
// The original implementation appended only the bare method name for every
// element, so the root of `serverHandler.New(...).SetPrefix(...)` came back as
// "New" rather than "serverHandler.New". Rules matching on the qualified root
// therefore never matched, and handler-base reported a false positive against
// the reference template — which is exactly what the baseline test caught.
func TestSelectorChain(t *testing.T) {
	cases := []struct {
		src  string
		want []string
	}{
		{
			src:  `serverHandler.New("Users").SetPrefix("/v1").AddPrefix("")`,
			want: []string{"serverHandler.New", "SetPrefix", "AddPrefix"},
		},
		{
			src:  `serverRoute.GET("/users", h.List).Name("List Users")`,
			want: []string{"serverRoute.GET", "Name"},
		},
		{
			src:  `serverRoute.GET("/users", h.List)`,
			want: []string{"serverRoute.GET"},
		},
		{
			src:  `New("x").WithY()`,
			want: []string{"New", "WithY"},
		},
	}
	for _, tc := range cases {
		t.Run(tc.src, func(t *testing.T) {
			got := selectorChain(parseExpr(t, tc.src))
			if len(got) != len(tc.want) {
				t.Fatalf("got %v, want %v", got, tc.want)
			}
			for i := range got {
				if got[i] != tc.want[i] {
					t.Fatalf("got %v, want %v", got, tc.want)
				}
			}
		})
	}
}

// TestNewArgType is the second regression test.
//
// fx.As names its interface as new(T). typeString sees a CallExpr and returns
// "?", so a substring check against the interface name silently failed and
// fx-registration reported the correctly-wired reference template as broken.
// A silently-failing check on the highest-value rule is the worst possible
// failure mode, so it gets a dedicated test.
func TestNewArgType(t *testing.T) {
	cases := []struct {
		src    string
		want   string
		wantOK bool
	}{
		{src: `new(serverHandler.Handler)`, want: "serverHandler.Handler", wantOK: true},
		{src: `new(Handler)`, want: "Handler", wantOK: true},
		{src: `make([]int, 0)`, wantOK: false},
		{src: `serverHandler.Handler`, wantOK: false},
		{src: `new(a, b)`, wantOK: false}, // not a valid new() call
	}
	for _, tc := range cases {
		t.Run(tc.src, func(t *testing.T) {
			got, ok := newArgType(parseExpr(t, tc.src))
			if ok != tc.wantOK {
				t.Fatalf("ok = %v, want %v", ok, tc.wantOK)
			}
			if ok && got != tc.want {
				t.Fatalf("got %q, want %q", got, tc.want)
			}
		})
	}
}

func TestTypeString(t *testing.T) {
	cases := map[string]string{
		`*serverRoute.Context`:               "*serverRoute.Context",
		`[]domain.User`:                      "[]domain.User",
		`map[string]int`:                     "map[string]int",
		`struct{}`:                           "struct{}",
		`error`:                              "error",
		`pgx.RowToStructByName[domain.User]`: "pgx.RowToStructByName[domain.User]",
		`*multipart.FileHeader`:              "*multipart.FileHeader",
		`[]*multipart.FileHeader`:            "[]*multipart.FileHeader",
	}
	for src, want := range cases {
		t.Run(src, func(t *testing.T) {
			if got := typeString(parseExpr(t, src)); got != want {
				t.Errorf("got %q, want %q", got, want)
			}
		})
	}
}

// TestSnake pins the initialism handling. PPONumber must become ppo_number,
// not p_p_o_number — the suggested fix is pasted straight into a struct tag, so
// a wrong suggestion is worse than none.
func TestSnake(t *testing.T) {
	cases := map[string]string{
		"ID":            "id",
		"PPONumber":     "ppo_number",
		"PensionerName": "pensioner_name",
		"CreatedAt":     "created_at",
		"HOACode":       "hoa_code",
		"URL":           "url",
		"UserID":        "user_id",
		"Amount":        "amount",
		"":              "",
	}
	for in, want := range cases {
		t.Run(in, func(t *testing.T) {
			if got := snake(in); got != want {
				t.Errorf("snake(%q) = %q, want %q", in, got, want)
			}
		})
	}
}

func TestIsSnake(t *testing.T) {
	for _, s := range []string{"ppo_number", "id", "created_at", "-", ""} {
		if !isSnake(s) {
			t.Errorf("isSnake(%q) = false, want true", s)
		}
	}
	for _, s := range []string{"PPONumber", "ppoNumber", "ppo-number", "Ppo_Number"} {
		if isSnake(s) {
			t.Errorf("isSnake(%q) = true, want false", s)
		}
	}
}

// TestTagOfUsesStructTagParsing guards against the substring-matching approach
// this replaced: `json:",inline"` and `json:"inline_thing"` are different, and
// a field named "validated" must not look like it has a validate tag.
func TestTagOfUsesStructTagParsing(t *testing.T) {
	src := "package p\ntype T struct {\n" +
		"\tA string `json:\",inline\"`\n" +
		"\tB string `json:\"validated_name\"`\n" +
		"\tC string `json:\"c\" validate:\"required\"`\n" +
		"}\n"
	fset := token.NewFileSet()
	f, err := parser.ParseFile(fset, "t.go", src, 0)
	if err != nil {
		t.Fatalf("parse: %v", err)
	}
	var st *ast.StructType
	ast.Inspect(f, func(n ast.Node) bool {
		if s, ok := n.(*ast.StructType); ok {
			st = s
			return false
		}
		return true
	})
	if st == nil {
		t.Fatal("no struct found")
	}
	fields := st.Fields.List

	if v, ok := tagOf(fields[0]).Lookup("json"); !ok || !strings.Contains(v, "inline") {
		t.Errorf("field A json tag = %q, want it to contain inline", v)
	}
	if _, ok := tagOf(fields[1]).Lookup("validate"); ok {
		t.Error("field B must not appear to have a validate tag (its json value merely contains 'validated')")
	}
	if v, ok := tagOf(fields[2]).Lookup("validate"); !ok || v != "required" {
		t.Errorf("field C validate tag = %q, want required", v)
	}
}
