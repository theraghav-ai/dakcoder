package rules

import (
	"strings"
	"testing"
)

func idiom(t *testing.T, files map[string]string) []Violation {
	t.Helper()
	res, err := Analyze(mkws(t, files), RunOptions{Only: []string{"go-idiom"}})
	if err != nil {
		t.Fatalf("analyze: %v", err)
	}
	return append(append([]Violation{}, res.Violations...), res.Warnings...)
}

func TestGoIdiomAnyOverInterface(t *testing.T) {
	bad := idiom(t, map[string]string{
		"core/domain/x.go": "package domain\n\ntype X struct{ Data interface{} }\n\nfunc F(v interface{}) {}\n",
	})
	if len(bad) != 2 {
		t.Errorf("got %d findings, want 2:\n%v", len(bad), bad)
	}
	for _, v := range bad {
		if v.Severity != SeverityWarning {
			t.Errorf("style advice should not block: %s is %s", v.Rule, v.Severity)
		}
	}

	good := idiom(t, map[string]string{
		"core/domain/x.go": "package domain\n\ntype X struct{ Data any }\n\nfunc F(v any) {}\n\ntype Reader interface{ Read() error }\n",
	})
	if len(good) != 0 {
		t.Errorf("`any` and a real interface are both fine:\n%v", good)
	}
}

func TestGoIdiomErrorStrings(t *testing.T) {
	tests := map[string]int{
		`errors.New("could not open file")`:        0,
		`errors.New("Could not open file")`:        1, // capitalised
		`errors.New("could not open file.")`:       1, // punctuated
		`errors.New("Could not open file.")`:       2, // both
		`errors.New("HTTP request failed")`:        0, // leading initialism
		`errors.New("PPO number is required")`:     0,
		`fmt.Errorf("could not open %s", name)`:    0,
		`fmt.Errorf("Could not open %s!", name)`:   2,
		`errors.New("")`:                           0,
		`errors.New("pgx: no rows in result set")`: 0,
	}
	for expr, want := range tests {
		src := "package domain\n\nimport (\n\t\"errors\"\n\t\"fmt\"\n)\n\nvar name = \"x\"\n\nvar _ = " + expr +
			"\n\nvar _ = errors.New\nvar _ = fmt.Errorf\n"
		got := idiom(t, map[string]string{"core/domain/x.go": src})
		var relevant int
		for _, v := range got {
			if strings.Contains(v.Message, "error string") {
				relevant++
			}
		}
		if relevant != want {
			t.Errorf("%s: got %d error-string findings, want %d\n%v", expr, relevant, want, got)
		}
	}
}

// TestGoIdiomErrorWrapping: in this template the consequence is concrete —
// pgx.ErrNoRows has to survive the trip up to the framework to become a 404,
// and %v severs it.
func TestGoIdiomErrorWrapping(t *testing.T) {
	bad := idiom(t, map[string]string{
		"core/domain/x.go": `package domain

import "fmt"

func F(err error) error { return fmt.Errorf("fetch pension: %v", err) }
`,
	})
	found := false
	for _, v := range bad {
		if strings.Contains(v.Message, "%w") {
			found = true
		}
	}
	if !found {
		t.Errorf("wrapping with %%v should be reported:\n%v", bad)
	}

	good := idiom(t, map[string]string{
		"core/domain/x.go": `package domain

import "fmt"

func F(err error) error { return fmt.Errorf("fetch pension: %w", err) }

func G(name string) error { return fmt.Errorf("bad name %q", name) }
`,
	})
	for _, v := range good {
		if strings.Contains(v.Message, "%w") {
			t.Errorf("false positive: %s", v)
		}
	}
}

// TestGoIdiomPackageMismatchGates is the one finding in this rule that blocks.
//
// go.instructions.md calls out duplicate and mismatched package declarations as
// a recurring LLM failure mode, and plan.md §9.2 makes it a hard error. The
// compiler's version of the message names a directory, not the file that
// introduced the mismatch.
func TestGoIdiomPackageMismatchGates(t *testing.T) {
	got := idiom(t, map[string]string{
		"repo/postgres/user.go":    "package repo\n\ntype UserRepository struct{}\n",
		"repo/postgres/pension.go": "package repo\n\ntype PensionRepository struct{}\n",
		"repo/postgres/audit.go":   "package handler\n\ntype AuditRepository struct{}\n",
	})
	if len(got) != 1 {
		t.Fatalf("got %d findings, want exactly one (the odd file out):\n%v", len(got), got)
	}
	if got[0].Path != "repo/postgres/audit.go" {
		t.Errorf("finding is on %s; the majority declaration is the correct one", got[0].Path)
	}
	if got[0].Severity != SeverityError {
		t.Errorf("a mismatched package does not compile, so it must gate; got %s", got[0].Severity)
	}
	if !strings.Contains(got[0].Fix, "package repo") {
		t.Errorf("the fix should name the package to change to: %s", got[0].Fix)
	}
}

func TestGoIdiomPackageNames(t *testing.T) {
	got := idiom(t, map[string]string{
		"core/domain/x.go":     "package domain\n",
		"internal/my_pkg/y.go": "package my_pkg\n",
		"internal/myPkg/z.go":  "package myPkg\n",
	})
	var names int
	for _, v := range got {
		if strings.Contains(v.Message, "package name") {
			names++
		}
	}
	if names != 2 {
		t.Errorf("got %d package-name findings, want 2:\n%v", names, got)
	}
}

// TestGoIdiomIgnoresTestFiles: test code legitimately does things production
// code should not, and flagging it trains people to ignore the rule.
func TestGoIdiomIgnoresTestFiles(t *testing.T) {
	got := idiom(t, map[string]string{
		"core/domain/x.go":      "package domain\n",
		"core/domain/x_test.go": "package domain\n\nimport \"errors\"\n\nvar _ = errors.New(\"Boom.\")\n\nfunc F(v interface{}) {}\n",
	})
	if len(got) != 0 {
		t.Errorf("test files are out of scope:\n%v", got)
	}
}

// TestGoIdiomIsAdvisoryByDefault: everything except the package mismatch is a
// warning, so an off-idiom but on-template service still passes the gate.
func TestGoIdiomIsAdvisoryByDefault(t *testing.T) {
	got := idiom(t, map[string]string{
		"core/domain/x.go": "package domain\n\nimport \"errors\"\n\nvar E = errors.New(\"Boom.\")\n\nfunc F(v interface{}) {}\n",
	})
	if len(got) == 0 {
		t.Fatal("expected findings")
	}
	for _, v := range got {
		if v.Severity != SeverityWarning {
			t.Errorf("%s should be advisory, got %s", v.Message, v.Severity)
		}
	}
}

// TestPerFindingSeverityYieldsToConfig: an operator who pins a rule's severity
// means every finding of it. A rule quietly escalating past that would make the
// setting untrustworthy.
func TestPerFindingSeverityYieldsToConfig(t *testing.T) {
	files := map[string]string{
		"repo/postgres/a.go": "package repo\n",
		"repo/postgres/b.go": "package handler\n",
	}
	root := mkws(t, files)

	cfg := DefaultConfig()
	cfg.SeverityOverride = map[string]Severity{"go-idiom": SeverityWarning}
	res, err := Analyze(root, RunOptions{Only: []string{"go-idiom"}, Config: cfg})
	if err != nil {
		t.Fatalf("analyze: %v", err)
	}
	if res.Count != 0 {
		t.Errorf("the operator pinned go-idiom to warning; nothing should block. got %d", res.Count)
	}
	if len(res.Warnings) != 1 {
		t.Errorf("got %d warnings, want 1", len(res.Warnings))
	}
}
