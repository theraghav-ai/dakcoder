package rules

import (
	"go/ast"
	"strings"

	"gitlab.cept.gov.in/it-2.0/dakcoder/gotools/internal/naming"
	"gitlab.cept.gov.in/it-2.0/dakcoder/gotools/internal/workspace"
)

// DomainTags enforces the domain model shape: every field carries json and db
// tags, and the standard identity/audit fields are present.
//
// The db tag is load-bearing rather than decorative — pgx.RowToStructByName
// matches columns to fields through it, so a missing db tag is a runtime scan
// failure, not a style issue.
var DomainTags = Rule{
	ID:       "domain-tags",
	Severity: SeverityError,
	Summary:  "domain fields carry json and db tags; every model has ID, CreatedAt, UpdatedAt",
	Citation: "skill.md §Domain Model Pattern, §Naming Conventions",
	Check: func(p *Pass) {
		for _, f := range p.WS.FilesIn(workspace.LayerDomain) {
			structsIn(f, func(name string, ts *ast.TypeSpec, st *ast.StructType) {
				present := map[string]bool{}
				for _, fld := range st.Fields.List {
					if isEmbedded(fld) {
						continue
					}
					for _, n := range fld.Names {
						present[n.Name] = true
					}
					tag := tagOf(fld)
					jsonTag, hasJSON := tag.Lookup("json")
					dbTag, hasDB := tag.Lookup("db")
					switch {
					case !hasJSON:
						p.At(f, fld).
							Fix(`add json:"%s"`, snake(fieldName(fld))).
							Report("%s.%s has no json tag", name, fieldName(fld))
					case !isSnake(strings.Split(jsonTag, ",")[0]):
						p.At(f, fld).
							Fix(`use json:"%s"`, snake(fieldName(fld))).
							Report("%s.%s json tag %q is not snake_case", name, fieldName(fld), jsonTag)
					}
					switch {
					case !hasDB:
						p.At(f, fld).
							Fix(`add db:"%s" — pgx.RowToStructByName matches columns through it`, snake(fieldName(fld))).
							Report("%s.%s has no db tag; row scanning will fail at runtime", name, fieldName(fld))
					case !isSnake(strings.Split(dbTag, ",")[0]):
						p.At(f, fld).
							Fix(`use db:"%s"`, snake(fieldName(fld))).
							Report("%s.%s db tag %q is not snake_case", name, fieldName(fld), dbTag)
					}
				}
				for _, req := range p.Cfg.RequiredDomainFields {
					if !present[req] {
						p.At(f, ts).
							Fix("add %s to %s", req, name).
							Report("%s is missing the standard field %s", name, req)
					}
				}
			})
		}
	},
}

// DepAllowlist gates new direct dependencies.
//
// Scoped to *direct* imports in first-party code. Transitive dependencies are
// the common libraries' business, and test dependencies have their own list —
// flagging testify would train people to ignore the linter, which is worse than
// not having the rule.
var DepAllowlist = Rule{
	ID:       "dep-allowlist",
	Severity: SeverityError,
	Summary:  "new direct dependencies must be on the approved list",
	Citation: "go.mod; plan.md §4 dependency allow-list",
	Check: func(p *Pass) {
		for _, f := range p.WS.Files {
			inTest := f.Layer == workspace.LayerTest
			for _, im := range f.AST.Imports {
				path := strings.Trim(im.Path.Value, `"`)
				if p.Cfg.DepAllowed(path, p.WS.ModulePath, inTest) {
					continue
				}
				p.At(f, im).
					Fix("use an approved equivalent, or request approval to add %q", path).
					Report("dependency %q is not on the approved list", path)
			}
		}
	},
}

// ErrorHandling enforces the SOP error order: log with context, then return.
//
// An error returned without a log line is invisible in production — the
// developer sees a 500 in Grafana with no trace of where it came from.
var ErrorHandling = Rule{
	ID:       "error-handling",
	Severity: SeverityError,
	Summary:  "log.Error(sctx.Ctx, ...) precedes returning an error from a handler",
	Citation: "SOP.md §[handler].go (step 8, error response order); skill.md §Error Handling",
	Check: func(p *Pass) {
		for _, f := range p.WS.FilesIn(workspace.LayerHandler) {
			funcsIn(f, func(fd *ast.FuncDecl) {
				recv, isMethod := receiverType(fd)
				if !isMethod || !strings.HasSuffix(recv, "Handler") || fd.Body == nil {
					return
				}
				if fd.Name.Name == "Routes" || !fd.Name.IsExported() {
					return
				}
				ast.Inspect(fd.Body, func(n ast.Node) bool {
					blk, ok := n.(*ast.BlockStmt)
					if !ok {
						return true
					}
					for i, st := range blk.List {
						ret, ok := st.(*ast.ReturnStmt)
						if !ok || len(ret.Results) != 2 {
							continue
						}
						// Only `return <something>, err` — a wrapped or
						// constructed error is a deliberate choice we allow.
						id, ok := ret.Results[1].(*ast.Ident)
						if !ok || id.Name != "err" {
							continue
						}
						logged := false
						for j := range i {
							if callsMatching(blk.List[j], "log.Error") || callsMatching(blk.List[j], "log.Fatal") {
								logged = true
								break
							}
						}
						if !logged {
							p.At(f, ret).
								Fix(`log.Error(sctx.Ctx, "…: %%v", err) before returning`).
								Report("%s returns err without logging it first; the failure would be untraceable", fd.Name.Name)
						}
					}
					return true
				})
			})
		}
	},
}

// FileSize keeps files reviewable. A warning, not an error: a 620-line file is
// a smell, not a contract breach, and blocking on it would be punitive.
var FileSize = Rule{
	ID:       "file-size",
	Severity: SeverityWarning,
	Summary:  "Go files stay under the configured line cap",
	Citation: "plan.md §9.2 (the Go analogue of the frontend check-lines rule)",
	Check: func(p *Pass) {
		if p.Cfg.MaxFileLines <= 0 {
			return
		}
		for _, f := range p.WS.Files {
			if f.Generated || f.Layer == workspace.LayerTest {
				continue
			}
			lines := strings.Count(string(f.Src), "\n") + 1
			if lines > p.Cfg.MaxFileLines {
				p.AtFile(f).
					Fix("split by responsibility — one resource or concern per file").
					Report("file is %d lines, over the %d-line cap", lines, p.Cfg.MaxFileLines)
			}
		}
	},
}

// ValidatorStale catches hand-edits to generated validators. The govalid header
// says DO NOT EDIT for a reason: the next regeneration silently discards the
// change, so the validation the developer thought they added disappears.
var ValidatorStale = Rule{
	ID:       "validator-generated",
	Severity: SeverityError,
	Summary:  "govalid validators are generated, never hand-edited",
	Citation: "SOP.md §Validation",
	Check: func(p *Pass) {
		// Loaded with WithGenerated by the runner when this rule is active.
		for _, f := range p.WS.Files {
			if !strings.Contains(f.Rel, "_validator.go") || f.Generated {
				continue
			}
			p.AtFile(f).
				Fix("restore the generated header and re-run `govalid ./request.go` from handler/").
				Report("validator file has no 'Code generated ... DO NOT EDIT.' header; it looks hand-written")
		}
	},
}

// snake converts a Go field name to snake_case.
//
// Delegated to internal/naming so the linter and the scaffolder cannot disagree
// about what `PPONumber` should be tagged as. Two implementations of this one
// function is how a scaffolder ends up emitting code its own linter rejects.
func snake(s string) string { return naming.Snake(s) }

// isSnake reports whether a tag value is lower_snake_case.
func isSnake(s string) bool {
	if s == "" || s == "-" {
		return true // "-" means "not serialised", which is legitimate
	}
	for _, r := range s {
		switch {
		case r >= 'a' && r <= 'z', r >= '0' && r <= '9', r == '_':
		default:
			return false
		}
	}
	return true
}
