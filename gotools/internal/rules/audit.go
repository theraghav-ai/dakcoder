package rules

import (
	"go/ast"
	"sort"
	"strings"

	"gitlab.cept.gov.in/it-2.0/dakcoder/gotools/internal/workspace"
)

// The three reports in this file reproduce, mechanically, the three sheets the
// manual review of 41 services was assembled by hand:
//
//	Db/batch          -> RoundTripAudit
//	Validations       -> ValidationAudit
//	Temporal          -> TemporalAudit
//
// They live in the rules package rather than beside the CLI so that a report
// and a lint finding cannot disagree about what a database call is. The same
// dblibExec table drives both; if the report said four statements and the
// linter said three, neither number would be believed.
//
// Reports, not rules. Nothing here has a severity and nothing here blocks.

// RoundTripReport is one repository method's database profile.
type RoundTripReport struct {
	Path        string `json:"path"`
	Line        int    `json:"line"`
	Method      string `json:"method"`
	Statements  int    `json:"statements"`
	InLoop      bool   `json:"in_loop"`
	Batched     bool   `json:"batched"`
	Transaction bool   `json:"transaction"`
	Verdict     string `json:"verdict"`
	// Score orders the report. A statement inside a loop counts ten times,
	// because its real cost is the size of the input rather than the number
	// written down — so N+1s surface above merely chatty methods.
	Score int `json:"score"`
}

// RoundTripAudit profiles every repository method's use of the database.
func RoundTripAudit(ws *workspace.Workspace) []RoundTripReport {
	var out []RoundTripReport
	for _, f := range ws.FilesIn(workspace.LayerRepo) {
		funcsIn(f, func(fd *ast.FuncDecl) {
			if fd.Body == nil {
				return
			}
			n := countCalls(fd.Body, isDBExec)
			looped := len(callsInLoops(fd.Body, isDBExec)) > 0
			if n == 0 && !looped {
				return
			}
			line, _ := f.Position(fd.Pos())
			r := RoundTripReport{
				Path:        f.Rel,
				Line:        line,
				Method:      fd.Name.Name,
				Statements:  n,
				InLoop:      looped,
				Batched:     usesBatch(fd.Body),
				Transaction: usesTransaction(fd.Body),
			}
			r.Score = n
			if looped {
				r.Score = n * 10
			}
			r.Verdict = roundTripVerdict(r)
			out = append(out, r)
		})
	}
	sort.SliceStable(out, func(i, j int) bool {
		if out[i].Score != out[j].Score {
			return out[i].Score > out[j].Score
		}
		if out[i].Path != out[j].Path {
			return out[i].Path < out[j].Path
		}
		return out[i].Line < out[j].Line
	})
	return out
}

// roundTripVerdict states, in one line, what to do about a method.
func roundTripVerdict(r RoundTripReport) string {
	switch {
	case r.InLoop && !r.Batched:
		return "N+1: query inside a loop — queue into a batch"
	case r.Transaction && r.Statements <= 1:
		return "drop the transaction: a single statement is already atomic"
	case r.Batched:
		return "ok"
	case r.Transaction && r.Statements >= 2:
		return "transaction without a batch: still one round trip per statement"
	case r.Statements >= 3:
		return "batch these where feasible"
	case r.Statements == 2:
		return "a batch may be possible"
	default:
		return "ok"
	}
}

// ValidationReport is one request field and what its validate tag does not say.
type ValidationReport struct {
	Path    string `json:"path"`
	Line    int    `json:"line"`
	Struct  string `json:"struct"`
	Field   string `json:"field"`
	Type    string `json:"type"`
	Tag     string `json:"tag"`
	Missing string `json:"missing,omitempty"`
}

// ValidationAudit reproduces the reviewers' second sheet: every request struct,
// every field, its current validate tag, and what it is still missing.
//
// Reports every field rather than only the deficient ones, because the sheet it
// replaces was a checklist — the value is being able to diff two runs and show
// that a service closed the gap.
func ValidationAudit(ws *workspace.Workspace) []ValidationReport {
	var out []ValidationReport
	for _, f := range ws.FilesIn(workspace.LayerHandler) {
		structsIn(f, func(name string, _ *ast.TypeSpec, st *ast.StructType) {
			if !isRequestStruct(name) {
				return
			}
			for _, fld := range st.Fields.List {
				if isEmbedded(fld) {
					continue
				}
				line, _ := f.Position(fld.Pos())
				tag, _ := tagOf(fld).Lookup("validate")
				rep := ValidationReport{
					Path:   f.Rel,
					Line:   line,
					Struct: name,
					Field:  fieldName(fld),
					Type:   typeString(fld.Type),
					Tag:    tag,
				}
				rep.Missing = missingValidation(fld, tag)
				out = append(out, rep)
			}
		})
	}
	return out
}

// missingValidation names what a field's validate tag still does not constrain.
func missingValidation(fld *ast.Field, tag string) string {
	if tag == "" {
		return "no validate tag"
	}
	rules := validateRules(tag)
	if rules["omitempty"] && len(rules) == 1 {
		return ""
	}
	kind, want := boundFor(fld.Type)
	if kind == "" || hasAny(rules, want...) {
		return ""
	}
	switch kind {
	case "string":
		return "max/len"
	case "number":
		return "min/max"
	default:
		return "max"
	}
}

// TemporalCandidate is one call site doing work on the request path that may
// belong somewhere else.
type TemporalCandidate struct {
	Path string `json:"path"`
	Line int    `json:"line"`
	Func string `json:"func"`
	Kind string `json:"kind"`
	Call string `json:"call"`
}

// temporalKinds maps a call-name fragment to the category of work it performs.
//
// Taken from the Temporal column of all 41 sheets. Matching is on the rendered
// call name, lower-cased, so it is deliberately loose: this is a candidate
// list a human triages, not a rule, and a miss costs more than a false hit.
var temporalKinds = []struct {
	needle string
	kind   string
}{
	{"minio", "file storage"},
	{"putobject", "file storage"},
	{"getobject", "file storage"},
	{"uploadfile", "file storage"},
	{"sendsms", "notification"},
	{"sendmail", "notification"},
	{"sendemail", "notification"},
	{"notify", "notification"},
	{"notification", "notification"},
	{"generatereport", "report generation"},
	{"generatepdf", "report generation"},
	{"generateexcel", "report generation"},
	{"writecsv", "report generation"},
	{"kafka", "event publication"},
	{"publish", "event publication"},
}

// TemporalAudit lists work done inline that the review repeatedly said should
// move off the request path.
//
// A report with no recommendation attached, deliberately. The review named
// nexus-temporal 459 times across 29 services, but the template has no Temporal
// wiring and the decision to adopt it has been deferred (findings §10.3). What
// can be stated mechanically is only "this handler uploads a file" — where that
// work should live is an architectural judgement about failure semantics, and
// guessing at it in a tool would be worse than staying quiet.
func TemporalAudit(ws *workspace.Workspace) []TemporalCandidate {
	var out []TemporalCandidate
	for _, f := range ws.Files {
		switch f.Layer {
		case workspace.LayerTest, workspace.LayerMain, workspace.LayerBootstrap:
			continue
		}
		funcsIn(f, func(fd *ast.FuncDecl) {
			if fd.Body == nil {
				return
			}
			seen := map[string]bool{}
			ast.Inspect(fd.Body, func(n ast.Node) bool {
				call, ok := n.(*ast.CallExpr)
				if !ok {
					return true
				}
				name := callName(call)
				kind := temporalKind(name)
				if kind == "" || seen[kind+name] {
					return true
				}
				seen[kind+name] = true
				line, _ := f.Position(call.Pos())
				out = append(out, TemporalCandidate{
					Path: f.Rel, Line: line, Func: fd.Name.Name, Kind: kind, Call: name,
				})
				return true
			})
			// An outbound service call is the fourth category, and it is
			// recognised by shape rather than by name.
			ast.Inspect(fd.Body, func(n ast.Node) bool {
				call, ok := n.(*ast.CallExpr)
				if !ok {
					return true
				}
				name := callName(call)
				if !isOutboundCall(name) {
					return true
				}
				line, _ := f.Position(call.Pos())
				out = append(out, TemporalCandidate{
					Path: f.Rel, Line: line, Func: fd.Name.Name,
					Kind: "outbound service call", Call: name,
				})
				return false
			})
		})
	}
	sort.SliceStable(out, func(i, j int) bool {
		if out[i].Path != out[j].Path {
			return out[i].Path < out[j].Path
		}
		return out[i].Line < out[j].Line
	})
	return out
}

// notWork are method names that name a pure transformation rather than the
// side effect the report is looking for.
//
// Needed because the matching above is on the whole rendered call name, so a
// Squirrel builder held in a variable called `deleteKafka` matches "kafka", and
// `minio.ToErrorResponse` matches "minio". Both are local computation. Keyed on
// the method, which is where the distinction actually lives.
var notWork = []string{"ToSql", "ToErrorResponse", "String", "Error", "Bytes"}

// temporalKind classifies a call name, returning "" when it is ordinary work.
func temporalKind(name string) string {
	if methodNamed(name, notWork...) {
		return ""
	}
	n := strings.ToLower(name)
	for _, k := range temporalKinds {
		if strings.Contains(n, k.needle) {
			return k.kind
		}
	}
	return ""
}

// SupersededBy reports the current generation of a CEPT library, so the version
// report and the legacy rule cannot disagree about what replaces what.
//
// The map is deliberately not "every api-* becomes n-api-*": n-api-config and
// n-api-trace do not exist, so api-config is the current config library rather
// than a migration target, and reporting it as legacy would be wrong.
func SupersededBy(path string) (string, bool) {
	for old, replacement := range legacyGeneration {
		if path == old || strings.HasPrefix(path, old+"/") {
			return "gitlab.cept.gov.in/it-2.0-common/" + replacement, true
		}
	}
	return "", false
}
