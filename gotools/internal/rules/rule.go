// Package rules implements the n-api-template compliance suite.
//
// # Shape of a rule
//
// A rule is a value, not a type: an ID, metadata, and a Check function that
// receives a *Pass and reports violations. Rules never walk the filesystem —
// the workspace is parsed once by the caller and handed to every rule.
//
// # Why not go/analysis
//
// go/analysis is the standard harness for Go static analysis and we considered
// it. Two properties of this rule set make it a poor fit:
//
//  1. Several rules are inherently cross-package. `fx-registration` has to see
//     a handler constructor in package `handler` and its registration in package
//     `bootstrap` at the same time. go/analysis runs per-package, and faking
//     cross-package state through Facts is more machinery than the rules need.
//  2. Several rules are about *directories*, not packages. "SQL must not appear
//     outside repo/postgres" is a question about a path, which go/analysis has
//     no vocabulary for.
//
// We keep the parts of go/analysis that earned their reputation — one parse,
// declarative rule metadata, a reporting API that carries positions — and drop
// the per-package unit of work.
//
// # Every rule cites its source
//
// A violation the developer cannot trace back to skill.md or SOP.md reads as
// the tool being opinionated. Every rule carries a Citation, and it is rendered
// in both text and JSON output. This is also what makes the CI drift check
// possible: when skill.md changes, the rules that cite the changed section are
// the ones to review.
package rules

import (
	"fmt"
	"go/ast"
	"sort"
	"strings"

	"gitlab.cept.gov.in/it-2.0/dakcoder/gotools/internal/workspace"
)

// Severity determines whether a violation blocks the verification gate.
type Severity string

const (
	// SeverityError blocks. The template contract is not satisfied.
	SeverityError Severity = "error"
	// SeverityWarning does not block. Idiomatic advice, or a signal that needs
	// human judgement.
	SeverityWarning Severity = "warning"
)

// Violation is one finding.
type Violation struct {
	Rule     string   `json:"rule"`
	Severity Severity `json:"severity"`
	Path     string   `json:"path"` // workspace-relative, forward slashes
	Line     int      `json:"line"`
	Col      int      `json:"col,omitempty"`
	Message  string   `json:"message"`
	Fix      string   `json:"fix,omitempty"`      // one-line remedy, imperative
	Citation string   `json:"citation,omitempty"` // e.g. "skill.md §Repository Pattern"
}

// String renders a violation for a terminal, fix and citation included.
func (v Violation) String() string {
	loc := v.Path
	if v.Line > 0 {
		loc = fmt.Sprintf("%s:%d", v.Path, v.Line)
	}
	s := fmt.Sprintf("[%s] %s — %s", v.Rule, loc, v.Message)
	if v.Fix != "" {
		s += "\n      fix: " + v.Fix
	}
	if v.Citation != "" {
		s += "\n      see: " + v.Citation
	}
	return s
}

// Rule is one compliance check.
//
// The JSON tags are a published contract, not decoration: `gotools rules
// --format json` is what the extension reads to render "Explain this rule"
// (Part B §11.2), and the field names it sees should not change because someone
// renamed a Go field.
type Rule struct {
	// ID is the stable, kebab-case identifier. It appears in violations, in
	// playbook filenames, and in the agent's prompts — so it is API and must
	// not change once published.
	ID string `json:"id"`

	// Severity is the default; config may override it per deployment.
	Severity Severity `json:"severity"`

	// Summary is a one-line description shown by `gotools rules`.
	Summary string `json:"summary"`

	// Citation points at the authority for this rule.
	Citation string `json:"citation,omitempty"`

	// Legacy marks a rule that detects pre-template patterns. Legacy rules run
	// only in `legacy_audit`, never in `rules_lint` — otherwise auditing an old
	// service would bury the agent in findings it was not asked to fix.
	Legacy bool `json:"legacy"`

	// Check reports violations via pass.Report.
	//
	// Excluded from JSON: encoding/json cannot marshal a func, so without this
	// tag `gotools rules --format json` fails outright with "unsupported type".
	// It did, until a CLI test asked.
	Check func(p *Pass) `json:"-"`
}

// Pass is the context handed to each rule.
//
// Reporting is deliberately fluent — `p.At(f, node).Fix("...").Report("...")` —
// because a violation without a remedy is the difference between a tool people
// adopt and a tool people resent. Making the fix part of the reporting call
// makes it awkward to omit.
type Pass struct {
	WS   *workspace.Workspace
	Cfg  Config
	rule *Rule
	out  *[]Violation

	// scope is the set of paths the caller said it touched, normalised. Empty
	// means the caller did not narrow the run.
	scope map[string]bool
}

// Touched reports whether the caller named this path as one it just changed.
//
// This is how a rule tells "the agent did this" from "this was already here",
// which is a distinction some rules need and the scoping machinery cannot make
// for them. secrets-in-config is the reason it exists: a credential the agent
// just wrote has to block, while the credentials already committed to the
// reference template are somebody else's problem to rotate and must not fail
// every lint until they do.
//
// Note this is *not* the same question as "will this violation block". An
// unscoped run blocks on everything; Touched is false for every path in one,
// because the caller made no claim about what it changed.
func (p *Pass) Touched(path string) bool {
	if len(p.scope) == 0 {
		return false
	}
	return p.scope[normalisePath(path)]
}

// Finding is a violation under construction.
type Finding struct {
	p         *Pass
	file      *workspace.File
	line, col int
	fix       string
	severity  Severity // "" means the rule's own severity
}

// At starts a finding at an AST node's position.
func (p *Pass) At(f *workspace.File, n ast.Node) *Finding {
	fd := &Finding{p: p, file: f}
	if f != nil && n != nil {
		fd.line, fd.col = f.Position(n.Pos())
	}
	return fd
}

// AtFile starts a file-level finding (line 1).
func (p *Pass) AtFile(f *workspace.File) *Finding {
	return &Finding{p: p, file: f, line: 1}
}

// AtLine starts a finding at an explicit 1-indexed line.
func (p *Pass) AtLine(f *workspace.File, line int) *Finding {
	return &Finding{p: p, file: f, line: line}
}

// AtPath starts a finding on a path that may not be a parsed Go file
// (go.mod, a stray routes.go, a config file).
func (p *Pass) AtPath(path string, line int) *Finding {
	return &Finding{p: p, file: &workspace.File{Rel: path}, line: line}
}

// Fix attaches the one-line, imperative remedy shown with the violation.
func (f *Finding) Fix(format string, args ...any) *Finding {
	f.fix = fmt.Sprintf(format, args...)
	return f
}

// Severity overrides the rule's default for this one finding.
//
// Two rules genuinely need this. go-idiom is advisory throughout except for a
// mismatched package declaration, which does not compile and so has to gate.
// secrets-in-config is advisory on credentials that were already committed and
// blocking on one the agent just added. Both are cases where a single rule id
// covers findings of materially different consequence, and splitting them into
// separate ids purely to carry a severity would make the rule table lie about
// how many distinct things it checks.
//
// A per-repository `severity:` override in .dakcoder/gotools.yaml still wins:
// an operator's explicit choice outranks the rule author's.
func (f *Finding) Severity(s Severity) *Finding {
	f.severity = s
	return f
}

// Report records the finding.
func (f *Finding) Report(format string, args ...any) {
	path := ""
	if f.file != nil {
		path = f.file.Rel
	}
	severity := f.p.Cfg.SeverityFor(f.p.rule)
	if f.severity != "" && !f.p.Cfg.HasSeverityOverride(f.p.rule.ID) {
		severity = f.severity
	}
	*f.p.out = append(*f.p.out, Violation{
		Rule:     f.p.rule.ID,
		Severity: severity,
		Path:     path,
		Line:     f.line,
		Col:      f.col,
		Message:  fmt.Sprintf(format, args...),
		Fix:      f.fix,
		Citation: f.p.rule.Citation,
	})
}

// Result is the outcome of a run.
type Result struct {
	OK bool `json:"ok"`
	// Count is the number of blocking, in-scope violations.
	Count      int         `json:"count"`
	Violations []Violation `json:"violations"`
	// OutOfScope are violations in files the caller did not ask about. They are
	// reported for visibility but never block — pre-existing findings in
	// untouched code must not send the agent off to "fix" unrelated legacy.
	OutOfScope      []Violation `json:"out_of_scope,omitempty"`
	OutOfScopeCount int         `json:"out_of_scope_count"`
	Warnings        []Violation `json:"warnings,omitempty"`
	FilesScanned    int         `json:"files_scanned"`
	RulesRun        int         `json:"rules_run"`
	DurationMS      int64       `json:"duration_ms"`
}

// Registry holds the rule set.
type Registry struct {
	rules []Rule
	byID  map[string]*Rule
}

// NewRegistry builds a registry, panicking on a duplicate ID — a duplicate is a
// programming error that must fail at startup, not silently shadow a rule.
func NewRegistry(rs ...Rule) *Registry {
	r := &Registry{byID: map[string]*Rule{}}
	for _, rule := range rs {
		if rule.ID == "" {
			panic("rules: rule with empty ID")
		}
		if rule.Check == nil {
			panic("rules: rule " + rule.ID + " has no Check")
		}
		if _, dup := r.byID[rule.ID]; dup {
			panic("rules: duplicate rule ID " + rule.ID)
		}
		cp := rule
		r.rules = append(r.rules, cp)
		r.byID[cp.ID] = &r.rules[len(r.rules)-1]
	}
	sort.Slice(r.rules, func(i, j int) bool { return r.rules[i].ID < r.rules[j].ID })
	// Re-index after sorting; the pointers above are invalidated by the sort.
	r.byID = make(map[string]*Rule, len(r.rules))
	for i := range r.rules {
		r.byID[r.rules[i].ID] = &r.rules[i]
	}
	return r
}

// All returns every registered rule in ID order.
func (r *Registry) All() []Rule { return r.rules }

// Get returns a rule by ID.
func (r *Registry) Get(id string) (*Rule, bool) {
	rule, ok := r.byID[id]
	return rule, ok
}

// RunOptions controls a run.
type RunOptions struct {
	// Only restricts the run to these rule IDs. Empty means all applicable.
	Only []string
	// Scope restricts *blocking* to these workspace-relative paths. Violations
	// elsewhere land in Result.OutOfScope. Empty means everything blocks.
	Scope []string
	// Legacy selects the legacy rule set (legacy_audit) instead of the
	// template-compliance set (rules_lint).
	Legacy bool
	// Config carries allow-lists and severity overrides.
	Config Config
}

// UnknownRuleError reports rule IDs that do not exist. Failing loudly beats
// silently running fewer rules than the caller asked for.
type UnknownRuleError struct{ IDs []string }

// Error lists the rule ids that do not exist.
func (e *UnknownRuleError) Error() string {
	return "unknown rule(s): " + strings.Join(e.IDs, ", ")
}

// Run executes the applicable rules against a loaded workspace.
func (r *Registry) Run(ws *workspace.Workspace, opts RunOptions) (*Result, error) {
	cfg := opts.Config
	if cfg.isZero() {
		cfg = DefaultConfig()
	}

	selected, err := r.selectRules(opts)
	if err != nil {
		return nil, err
	}

	scope := map[string]bool{}
	for _, s := range opts.Scope {
		scope[normalisePath(s)] = true
	}

	var all []Violation
	for i := range selected {
		rule := selected[i]
		p := &Pass{WS: ws, Cfg: cfg, rule: rule, out: &all, scope: scope}
		rule.Check(p)
	}

	res := &Result{FilesScanned: len(ws.Files), RulesRun: len(selected)}
	for _, v := range all {
		switch {
		case v.Severity == SeverityWarning:
			res.Warnings = append(res.Warnings, v)
		case len(scope) > 0 && !scope[normalisePath(v.Path)]:
			res.OutOfScope = append(res.OutOfScope, v)
		default:
			res.Violations = append(res.Violations, v)
		}
	}
	sortViolations(res.Violations)
	sortViolations(res.OutOfScope)
	sortViolations(res.Warnings)
	res.Count = len(res.Violations)
	res.OutOfScopeCount = len(res.OutOfScope)
	res.OK = res.Count == 0
	return res, nil
}

func (r *Registry) selectRules(opts RunOptions) ([]*Rule, error) {
	cfg := opts.Config
	if cfg.isZero() {
		cfg = DefaultConfig()
	}
	if len(opts.Only) > 0 {
		var out []*Rule
		var unknown []string
		for _, id := range opts.Only {
			rule, ok := r.byID[id]
			if !ok {
				unknown = append(unknown, id)
				continue
			}
			out = append(out, rule)
		}
		if len(unknown) > 0 {
			return nil, &UnknownRuleError{IDs: unknown}
		}
		return out, nil
	}
	var out []*Rule
	for i := range r.rules {
		rule := &r.rules[i]
		if rule.Legacy != opts.Legacy {
			continue
		}
		if cfg.Disabled(rule.ID) {
			continue
		}
		out = append(out, rule)
	}
	return out, nil
}

func sortViolations(vs []Violation) {
	sort.SliceStable(vs, func(i, j int) bool {
		if vs[i].Path != vs[j].Path {
			return vs[i].Path < vs[j].Path
		}
		if vs[i].Line != vs[j].Line {
			return vs[i].Line < vs[j].Line
		}
		return vs[i].Rule < vs[j].Rule
	})
}

func normalisePath(p string) string {
	return strings.TrimPrefix(strings.ReplaceAll(p, `\`, `/`), "./")
}
