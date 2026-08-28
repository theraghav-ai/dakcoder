package rules

import (
	"go/ast"
	"strings"

	"gitlab.cept.gov.in/it-2.0/dakcoder/gotools/internal/workspace"
)

// This file implements the reviewers' standing free-text checklist, which
// appears near-verbatim in 15 of the 41 service sheets:
//
//	1. For hard coded values use constants.
//	2. Avoid using rows.Next()/rows.Close.          [-> repo-raw-rows]
//	3. Use switch instead of if-elseif wherever feasible.
//	4. Use time.Now() instead of NOW(), and take the value once
//	   at the top of the func.                      [-> repo-sql-now]
//	5. Use meaningful variable names — for context.Context use ctx, not gctx.
//
// It was a rule set they had already written down. It just never reached the
// linter.

const checklistCitation = "docs/CODE-REVIEW-FINDINGS.md"

// NoFmtPrint bans fmt.Print* outside main.
//
// Print writes to stdout with no level, no timestamp, no request id and no
// service name, so in a container it is either invisible or it is noise in the
// middle of structured JSON. It is also, invariably, debugging left behind.
var NoFmtPrint = Rule{
	ID:       "no-fmt-print",
	Severity: SeverityError,
	Summary:  "no fmt.Print/Printf/Println in service code; use the structured logger",
	Citation: checklistCitation,
	Check: func(p *Pass) {
		for _, f := range p.WS.Files {
			switch f.Layer {
			case workspace.LayerTest, workspace.LayerMain:
				continue
			}
			ast.Inspect(f.AST, func(n ast.Node) bool {
				call, ok := n.(*ast.CallExpr)
				if !ok {
					return true
				}
				name := callName(call)
				switch name {
				case "fmt.Print", "fmt.Printf", "fmt.Println":
					p.At(f, call).
						Fix(`use log.Debug(ctx, "…") — it carries the level, the service name and the request id`).
						Report("%s writes to stdout; it is unlevelled and untraceable in production", name)
				}
				return true
			})
		}
	},
}

// CtxNaming enforces the conventional name for a context.
//
// Small, and worth it. `ctx` is load-bearing convention in Go: reviewers scan
// for it, and `gctx` in a signature is a reliable signal that the value is a
// *gin.Context being smuggled through code that claims to take a
// context.Context. The legacy corpus carries 415 of them; the template has
// none.
var CtxNaming = Rule{
	ID:       "ctx-naming",
	Severity: SeverityWarning,
	Summary:  "context.Context parameters are named ctx",
	Citation: checklistCitation,
	Check: func(p *Pass) {
		for _, f := range p.WS.Files {
			if f.Layer == workspace.LayerTest {
				continue
			}
			funcsIn(f, func(fd *ast.FuncDecl) {
				if fd.Type.Params == nil {
					return
				}
				for _, param := range fd.Type.Params.List {
					// `*context.Context` is included deliberately. It is
					// always a mistake — Context is an interface, so a pointer
					// to one adds a nil check and buys nothing — and the legacy
					// corpus has six of them, every one named gctx.
					t := typeString(param.Type)
					if t != "context.Context" && t != "*context.Context" {
						continue
					}
					for _, id := range param.Names {
						if id.Name == "ctx" || id.Name == "_" {
							continue
						}
						fix := "rename it to ctx"
						if t == "*context.Context" {
							fix = "take ctx context.Context by value — Context is an interface"
						}
						p.At(f, id).
							Fix("%s", fix).
							Report("context parameter is named %q; the convention every reader scans for is ctx", id.Name)
					}
				}
			})
		}
	},
}

// PreferSwitch reports long if/else-if chains over one value.
//
// Advisory. A three-branch chain testing the same operand reads better as a
// switch, and — the reason it made the reviewers' list — a switch makes a
// missing default obvious, where an if/else-if chain just falls through
// silently.
//
// Only fires when every branch tests the same left-hand operand, so genuinely
// unrelated conditions are left alone.
var PreferSwitch = Rule{
	ID:       "prefer-switch",
	Severity: SeverityWarning,
	Summary:  "if/else-if chains over one value read better as a switch",
	Citation: checklistCitation,
	Check: func(p *Pass) {
		for _, f := range p.WS.Files {
			if f.Layer == workspace.LayerTest {
				continue
			}
			ast.Inspect(f.AST, func(n ast.Node) bool {
				ifs, ok := n.(*ast.IfStmt)
				if !ok {
					return true
				}
				operand, branches := chainOperand(ifs)
				if branches < 3 || operand == "" {
					return true
				}
				p.At(f, ifs).
					Fix("rewrite as `switch %s { case ... }` so a missing case is visible", operand).
					Report("%d-branch if/else-if chain all testing %s", branches, operand)
				// Do not descend: the nested else-ifs are the same chain.
				return false
			})
		}
	},
}

// chainOperand walks an if/else-if chain and reports the operand every branch
// compares, plus the branch count. Returns "" when the branches disagree.
func chainOperand(ifs *ast.IfStmt) (string, int) {
	operand, count := "", 0
	for cur := ifs; cur != nil; {
		bin, ok := cur.Cond.(*ast.BinaryExpr)
		if !ok {
			return "", 0
		}
		// Only equality chains dispatch on a value; ordering comparisons are a
		// range test, which a switch does not express better.
		if bin.Op.String() != "==" {
			return "", 0
		}
		lhs := typeString(bin.X)
		if lhs == "?" {
			return "", 0
		}
		if operand == "" {
			operand = lhs
		} else if operand != lhs {
			return "", 0
		}
		count++
		next, ok := cur.Else.(*ast.IfStmt)
		if !ok {
			break
		}
		cur = next
	}
	return operand, count
}

// MagicLiteral reports a literal repeated enough times to deserve a name.
//
// Three occurrences, because two is a coincidence and three is a pattern that
// will be edited inconsistently. What the reviewers meant by "for hard coded
// values use constants" was business vocabulary — the `"Pending"` that appears
// eight times and the `"Conflict"` that appears twenty-six.
//
// Everything else is excluded, and the exclusions matter more than the rule.
// An unrestricted version of this produced 407 findings on one service, most of
// them column names in query builders and Go's reference time layout — none of
// which anybody should extract to a constant. The repository layer is skipped
// wholesale for that reason: a schema name repeated across the queries that use
// it is the schema, not a magic value.
var MagicLiteral = Rule{
	ID:       "magic-literal",
	Severity: SeverityWarning,
	Summary:  "business strings repeated three or more times in a file become constants",
	Citation: checklistCitation,
	Check: func(p *Pass) {
		for _, f := range p.WS.Files {
			switch f.Layer {
			case workspace.LayerTest, workspace.LayerRepo:
				continue
			}
			counts := map[string]int{}
			first := map[string]*ast.BasicLit{}
			for _, bl := range stringLitsIn(f.AST) {
				v := litValue(bl)
				if len(v) < 4 || isStructTagLike(v) || isSchemaOrFormat(v) {
					continue
				}
				counts[v]++
				if first[v] == nil {
					first[v] = bl
				}
			}
			// Struct tags are string literals too, and they repeat by design.
			for _, bl := range structTagLits(f) {
				delete(counts, litValue(bl))
			}
			for v, n := range counts {
				if n < 3 {
					continue
				}
				p.At(f, first[v]).
					Fix("declare `const … = %q` and use it at all %d sites", v, n).
					Report("literal %q appears %d times; one of them will eventually be edited alone", v, n)
			}
		}
	},
}

// isStructTagLike reports whether a literal looks like a struct tag body, which
// repeats legitimately and is not a magic value.
func isStructTagLike(v string) bool {
	return strings.Contains(v, `:"`) || strings.HasPrefix(v, "json:") ||
		strings.HasPrefix(v, "db:") || strings.HasPrefix(v, "validate:")
}

// goTimeLayouts are the reference-time fragments. `2006-01-02` repeated thirty
// times is not a magic constant — it is how Go spells a date, and hoisting it to
// a named constant makes the call sites harder to read, not easier.
var goTimeLayouts = []string{"2006", "01-02", "15:04", "Jan", "Mon"}

// isSchemaOrFormat reports whether a literal is a database identifier, a time
// layout, or a format string — none of which become clearer as constants.
func isSchemaOrFormat(v string) bool {
	for _, l := range goTimeLayouts {
		if strings.Contains(v, l) {
			return true
		}
	}
	// A qualified column reference: `a.remarks`, `pao.pfms_main`.
	if strings.Contains(v, ".") && !strings.Contains(v, " ") {
		return true
	}
	// A printf-style format is a message template, not a value.
	if strings.Contains(v, "%") {
		return true
	}
	return false
}

// structTagLits returns the literals used as struct tags in a file.
func structTagLits(f *workspace.File) []*ast.BasicLit {
	var out []*ast.BasicLit
	structsIn(f, func(_ string, _ *ast.TypeSpec, st *ast.StructType) {
		for _, fld := range st.Fields.List {
			if fld.Tag != nil {
				out = append(out, fld.Tag)
			}
		}
	})
	return out
}

// HandlerSingleRepoCall reports handlers that make several repository calls.
//
// The reviewers' "Morethan one repo calls in handler". Each call is a round
// trip, and a handler orchestrating three of them is doing work the repository
// could do in one batch — with the added problem that a failure halfway through
// leaves the first write committed and no transaction to undo it.
//
// Advisory, and it will have false positives: fetch-then-act is a legitimate
// shape, and sometimes the second call genuinely needs the first one's result.
// The rule says "look at this", not "this is wrong".
var HandlerSingleRepoCall = Rule{
	ID:       "handler-single-repo-call",
	Severity: SeverityWarning,
	Summary:  "handlers delegate to one repository call; several suggest work that belongs in the repository",
	Citation: dbPerfCitation,
	Check: func(p *Pass) {
		for _, f := range p.WS.FilesIn(workspace.LayerHandler) {
			fields := repoFields(f)
			if len(fields) == 0 {
				continue
			}
			funcsIn(f, func(fd *ast.FuncDecl) {
				recv, isMethod := receiverType(fd)
				if !isMethod || !strings.HasSuffix(recv, "Handler") || fd.Body == nil {
					return
				}
				if fd.Name.Name == "Routes" || !fd.Name.IsExported() {
					return
				}
				n := countCalls(fd.Body, func(c *ast.CallExpr) bool {
					return isRepoCall(c, fields)
				})
				if n < 2 {
					return
				}
				p.At(f, fd).
					Fix("move the sequence into one repository method and batch the statements there").
					Report("%s makes %d repository calls; that is %d round trips with no transaction across them",
						fd.Name.Name, n, n)
			})
		}
	},
}

// repoFields returns the names of the handler struct fields that hold a
// repository, resolved from the struct declaration in the same file.
//
// Resolved rather than guessed, because the field is conventionally called
// `svc` — in the reference template and in the legacy corpus alike — so a rule
// looking for a field named `repo` finds nothing anywhere. The type is what
// identifies it: `*repo.UserRepository`.
func repoFields(f *workspace.File) map[string]bool {
	out := map[string]bool{}
	structsIn(f, func(name string, _ *ast.TypeSpec, st *ast.StructType) {
		if !strings.HasSuffix(name, "Handler") {
			return
		}
		for _, fld := range st.Fields.List {
			if isEmbedded(fld) {
				continue
			}
			t := typeString(fld.Type)
			if !strings.HasSuffix(t, "Repository") && !strings.HasPrefix(t, "*repo.") {
				continue
			}
			for _, n := range fld.Names {
				out[n.Name] = true
			}
		}
	})
	return out
}

// isRepoCall reports whether a call is `h.<repoField>.Method(...)`.
func isRepoCall(c *ast.CallExpr, fields map[string]bool) bool {
	sel, ok := c.Fun.(*ast.SelectorExpr)
	if !ok {
		return false
	}
	inner, ok := sel.X.(*ast.SelectorExpr)
	if !ok {
		return false
	}
	return fields[inner.Sel.Name]
}
