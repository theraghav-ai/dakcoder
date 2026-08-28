package rules

import (
	"go/ast"
	"go/token"
	"reflect"
	"strconv"
	"strings"

	"gitlab.cept.gov.in/it-2.0/dakcoder/gotools/internal/workspace"
)

// typeString renders a type expression the way it appears in source.
//
// Deliberately not exhaustive over the AST: it covers the shapes the template
// uses (idents, pointers, selectors, slices, maps, ellipsis, empty struct) and
// returns "?" otherwise. A rule that gets "?" reports nothing rather than
// guessing — a false positive on a signature check is far more damaging than a
// miss, because the developer cannot override it.
func typeString(e ast.Expr) string {
	switch t := e.(type) {
	case nil:
		return ""
	case *ast.Ident:
		return t.Name
	case *ast.StarExpr:
		return "*" + typeString(t.X)
	case *ast.SelectorExpr:
		return typeString(t.X) + "." + t.Sel.Name
	case *ast.ArrayType:
		if t.Len == nil {
			return "[]" + typeString(t.Elt)
		}
		return "[N]" + typeString(t.Elt)
	case *ast.MapType:
		return "map[" + typeString(t.Key) + "]" + typeString(t.Value)
	case *ast.Ellipsis:
		return "..." + typeString(t.Elt)
	case *ast.InterfaceType:
		if t.Methods == nil || len(t.Methods.List) == 0 {
			return "any"
		}
		return "interface{...}"
	case *ast.StructType:
		if t.Fields == nil || len(t.Fields.List) == 0 {
			return "struct{}"
		}
		return "struct{...}"
	case *ast.FuncType:
		return "func(...)"
	case *ast.IndexExpr: // generic instantiation, e.g. pgx.RowToStructByName[domain.User]
		return typeString(t.X) + "[" + typeString(t.Index) + "]"
	case *ast.ChanType:
		return "chan " + typeString(t.Value)
	default:
		return "?"
	}
}

// tagOf parses a struct field tag. Returns the zero StructTag when absent.
//
// Uses reflect.StructTag rather than substring matching so that `json:"a,inline"`
// and `json:",inline"` are distinguished correctly, and so a tag containing the
// word "validate" inside another value is not mistaken for a validate tag.
func tagOf(f *ast.Field) reflect.StructTag {
	if f == nil || f.Tag == nil {
		return ""
	}
	unquoted, err := strconv.Unquote(f.Tag.Value)
	if err != nil {
		return ""
	}
	return reflect.StructTag(unquoted)
}

// fieldName returns a field's first declared name, or "" for an embedded field.
func fieldName(f *ast.Field) string {
	if f == nil || len(f.Names) == 0 {
		return ""
	}
	return f.Names[0].Name
}

// isEmbedded reports whether a struct field is an embedded type.
func isEmbedded(f *ast.Field) bool { return f != nil && len(f.Names) == 0 }

// structsIn yields every top-level struct type declaration in a file.
func structsIn(f *workspace.File, fn func(name string, ts *ast.TypeSpec, st *ast.StructType)) {
	for _, d := range f.AST.Decls {
		gd, ok := d.(*ast.GenDecl)
		if !ok {
			continue
		}
		for _, spec := range gd.Specs {
			ts, ok := spec.(*ast.TypeSpec)
			if !ok {
				continue
			}
			st, ok := ts.Type.(*ast.StructType)
			if !ok || st.Fields == nil {
				continue
			}
			fn(ts.Name.Name, ts, st)
		}
	}
}

// funcsIn yields every function and method declaration in a file.
func funcsIn(f *workspace.File, fn func(*ast.FuncDecl)) {
	for _, d := range f.AST.Decls {
		if fd, ok := d.(*ast.FuncDecl); ok {
			fn(fd)
		}
	}
}

// receiverType returns a method's receiver type as source text ("*UserHandler"),
// and false for a plain function.
func receiverType(fd *ast.FuncDecl) (string, bool) {
	if fd.Recv == nil || len(fd.Recv.List) == 0 {
		return "", false
	}
	return typeString(fd.Recv.List[0].Type), true
}

// results returns a function's result types.
func results(fd *ast.FuncDecl) []string {
	if fd.Type.Results == nil {
		return nil
	}
	var out []string
	for _, r := range fd.Type.Results.List {
		n := 1
		if len(r.Names) > 1 {
			n = len(r.Names)
		}
		for range n {
			out = append(out, typeString(r.Type))
		}
	}
	return out
}

// params returns a function's parameter types.
func params(fd *ast.FuncDecl) []string {
	if fd.Type.Params == nil {
		return nil
	}
	var out []string
	for _, p := range fd.Type.Params.List {
		n := 1
		if len(p.Names) > 1 {
			n = len(p.Names)
		}
		for range n {
			out = append(out, typeString(p.Type))
		}
	}
	return out
}

// callName renders a call's function as "pkg.Fn", "recv.Method" or "Fn".
func callName(c *ast.CallExpr) string {
	switch f := c.Fun.(type) {
	case *ast.Ident:
		return f.Name
	case *ast.SelectorExpr:
		return typeString(f.X) + "." + f.Sel.Name
	case *ast.IndexExpr: // generic call
		return typeString(f.X)
	}
	return ""
}

// findCall walks a node and reports the first call whose rendered name satisfies
// match.
func findCall(n ast.Node, match func(name string) bool) (*ast.CallExpr, bool) {
	var found *ast.CallExpr
	ast.Inspect(n, func(x ast.Node) bool {
		if found != nil {
			return false
		}
		if c, ok := x.(*ast.CallExpr); ok && match(callName(c)) {
			found = c
			return false
		}
		return true
	})
	return found, found != nil
}

// anyCall reports whether any call in the subtree has one of the given names.
func anyCall(n ast.Node, names ...string) bool {
	_, ok := findCall(n, func(name string) bool {
		for _, w := range names {
			if name == w {
				return true
			}
		}
		return false
	})
	return ok
}

// callsMatching reports whether any call's name contains the substring.
func callsMatching(n ast.Node, substr string) bool {
	_, ok := findCall(n, func(name string) bool { return strings.Contains(name, substr) })
	return ok
}

// selectorChain renders a method-chain expression root-first, e.g.
// `serverHandler.New("Users").SetPrefix("/v1").AddPrefix("")` yields
// ["serverHandler.New", "SetPrefix", "AddPrefix"].
//
// The root element is package-qualified and the rest are bare method names.
// That asymmetry is intentional: rules need to know the chain *started* at
// serverHandler.New specifically, but the intermediate calls are unambiguous by
// name alone.
func selectorChain(e ast.Expr) []string {
	var out []string
	for {
		call, ok := e.(*ast.CallExpr)
		if !ok {
			break
		}
		sel, ok := call.Fun.(*ast.SelectorExpr)
		if !ok {
			// Root is a plain function call: New(...) rather than pkg.New(...).
			out = append(out, callName(call))
			break
		}
		// When the receiver is not itself a call we have reached the root, and
		// the caller needs it qualified.
		if _, receiverIsCall := sel.X.(*ast.CallExpr); !receiverIsCall {
			out = append(out, typeString(sel.X)+"."+sel.Sel.Name)
			break
		}
		out = append(out, sel.Sel.Name)
		e = sel.X
	}
	for i, j := 0, len(out)-1; i < j; i, j = i+1, j-1 {
		out[i], out[j] = out[j], out[i]
	}
	return out
}

// newArgType renders the type inside a `new(T)` call, which is how fx.As names
// an interface: fx.As(new(serverHandler.Handler)).
//
// Without this, typeString sees a CallExpr and returns "?", so any check for
// the interface name silently fails — and a silently-failing check on the
// highest-value rule in the suite is worse than no check at all.
func newArgType(e ast.Expr) (string, bool) {
	call, ok := e.(*ast.CallExpr)
	if !ok {
		return "", false
	}
	id, ok := call.Fun.(*ast.Ident)
	if !ok || id.Name != "new" || len(call.Args) != 1 {
		return "", false
	}
	return typeString(call.Args[0]), true
}

// hasSuffixFold reports a case-insensitive suffix match.
func hasSuffixFold(s, suffix string) bool {
	return len(s) >= len(suffix) && strings.EqualFold(s[len(s)-len(suffix):], suffix)
}

// callsInLoops returns every matching call that sits inside the *body* of a for
// or range statement — that is, every call whose execution repeats.
//
// The loop header is deliberately excluded, and that exclusion is the whole
// reason this exists rather than a loopDepth counter over ast.Inspect. In
//
//	for _, u := range dblib.SelectRows(ctx, db, q, m) { ... }
//
// the query is a child of the RangeStmt but runs exactly once. Counting it as
// an N+1 would be a false positive on the one rule that most needs to be
// trusted, since its remedy is a rewrite rather than a one-line edit.
//
// A function literal resets nothing: a closure defined inside a loop but
// invoked once is still ambiguous, so it inherits the loop context it was
// written in rather than pretending to know better.
func callsInLoops(body ast.Node, match func(*ast.CallExpr) bool) []*ast.CallExpr {
	var out []*ast.CallExpr
	var scan func(n ast.Node, inLoop bool)
	scan = func(n ast.Node, inLoop bool) {
		if n == nil {
			return
		}
		switch t := n.(type) {
		case *ast.ForStmt:
			scan(t.Init, inLoop)
			scan(t.Cond, inLoop)
			scan(t.Post, inLoop)
			scan(t.Body, true)
			return
		case *ast.RangeStmt:
			scan(t.X, inLoop)
			scan(t.Body, true)
			return
		}
		if c, ok := n.(*ast.CallExpr); ok && inLoop && match(c) {
			out = append(out, c)
		}
		// Descend one level and hand each child back to scan, so the loop
		// bookkeeping above is never bypassed by Inspect's own recursion.
		ast.Inspect(n, func(child ast.Node) bool {
			if child == n {
				return true
			}
			scan(child, inLoop)
			return false
		})
	}
	scan(body, false)
	return out
}

// countCalls returns how many calls in a subtree satisfy match.
func countCalls(n ast.Node, match func(*ast.CallExpr) bool) int {
	count := 0
	ast.Inspect(n, func(x ast.Node) bool {
		if c, ok := x.(*ast.CallExpr); ok && match(c) {
			count++
		}
		return true
	})
	return count
}

// methodNamed reports whether a rendered call name ends in the given method,
// so `r.db.Begin`, `ur.Db.Begin` and `pool.Begin` all match "Begin" without the
// rule having to know what the receiver is called.
func methodNamed(callName string, methods ...string) bool {
	for _, m := range methods {
		if callName == m || strings.HasSuffix(callName, "."+m) {
			return true
		}
	}
	return false
}

// stringLitsIn returns every string literal in a subtree, unquoted.
func stringLitsIn(n ast.Node) []*ast.BasicLit {
	var out []*ast.BasicLit
	ast.Inspect(n, func(x ast.Node) bool {
		if bl, ok := x.(*ast.BasicLit); ok && bl.Kind == token.STRING {
			out = append(out, bl)
		}
		return true
	})
	return out
}

// litValue unquotes a string literal, returning "" when it is not one.
func litValue(bl *ast.BasicLit) string {
	if bl == nil || bl.Kind != token.STRING {
		return ""
	}
	v, err := strconv.Unquote(bl.Value)
	if err != nil {
		return ""
	}
	return v
}
