// Package routes takes an inventory of the HTTP routes a service declares, in
// both the shape the legacy services use and the shape the template requires.
//
// It exists for one question, asked twice: **is every route that was here
// before still here afterwards?** A conversion rewrites every handler in the
// service, and the failure it is most likely to make quietly is losing one —
// a method that never reached the new Routes(), a prefix that moved, a group
// that was not carried over. Nothing else in the toolchain notices. The build
// passes: a route that is never registered is a method nobody calls, which Go
// is perfectly happy about. The swagger check only sees what did register. The
// first thing that notices is a client getting a 404 in production.
//
// So the inventory is taken before the migration starts and again when it
// finishes, and the two are compared. That comparison is the point; everything
// below is in service of making both sides of it accurate.
//
// # The two shapes
//
// Legacy services register routes imperatively with Gin, in routes/routes.go
// or in main.go:
//
//	v1 := r.Group("/v1")
//	awards := v1.Group("/awards")
//	awards.GET("/:award-id", h.FetchAwardDetails)
//
// so a route's path is the concatenation of the groups it is nested inside,
// and the nesting is expressed as variable assignment rather than as syntax.
// Resolving it means following those variables.
//
// Template services declare routes on the handler, with the base path in the
// constructor's prefix chain:
//
//	base := serverHandler.New("Awards").SetPrefix("/v1").AddPrefix("/awards")
//	...
//	serverRoute.GET("/:award-id", c.FetchAwardDetails).Name("Fetch Award Details")
//
// so the path is the constructor's chain plus the literal in Routes(), and the
// two live in different functions joined by the handler type.
//
// # What it does not do
//
// It does not resolve a path built by concatenation or held in a constant: a
// route registered as `awards.GET(pathVar, h.X)` is recorded with an empty path
// and counted as *unresolved*, not silently dropped. That number is reported
// alongside the routes, because an inventory that quietly under-counts is worse
// than no inventory — it would make a comparison come out clean by omission,
// which is the one outcome this package must never produce.
package routes

import (
	"fmt"
	"go/ast"
	"sort"
	"strconv"
	"strings"

	"gitlab.cept.gov.in/it-2.0/dakcoder/gotools/internal/workspace"
)

// Methods registered by both styles. PATCH and HEAD are included because a
// service that has one and loses it in conversion is exactly the case this
// package is for; OPTIONS is not, because Gin registers it implicitly.
var methods = map[string]bool{
	"GET": true, "POST": true, "PUT": true, "DELETE": true,
	"PATCH": true, "HEAD": true,
}

// Route is one registered endpoint.
type Route struct {
	Method string `json:"method"`
	// Path is the full path, prefixes applied. Empty when the registration used
	// something this package will not guess at; see Inventory.Unresolved.
	Path string `json:"path"`
	// Handler is the function the route dispatches to, as written -- "h.Fetch"
	// or "c.FetchAwardDetails". It is the strongest identity a route has across
	// a conversion, because the path can legitimately change and the method
	// usually does not move with it.
	Handler string `json:"handler"`
	// Name is the .Name("...") a template route carries. Empty for legacy ones,
	// and empty for a template route that forgot it -- which is itself worth
	// reporting, since a route with no name is missing from swagger.
	Name  string `json:"name,omitempty"`
	Style string `json:"style"` // "gin" or "template"
	File  string `json:"file"`
	Line  int    `json:"line"`
}

// Key is what two inventories are compared on.
//
// Method and path, lowercased, with the parameter *names* dropped: a conversion
// legitimately renames `:id` to `:award-id` while registering the same
// endpoint, and a comparison that called that a lost route would cry wolf on
// every service. What it must not do is ignore the shape -- `/awards/:id` and
// `/awards/:id/lines` stay different keys.
func (r Route) Key() string {
	parts := strings.Split(r.Path, "/")
	for i, p := range parts {
		if strings.HasPrefix(p, ":") {
			parts[i] = ":p"
		} else if strings.HasPrefix(p, "*") {
			parts[i] = "*p"
		}
	}
	return strings.ToUpper(r.Method) + " " + strings.ToLower(strings.Join(parts, "/"))
}

// Inventory is every route a service declares, and what could not be read.
type Inventory struct {
	Routes []Route `json:"routes"`
	// Unresolved is registrations that were found but whose path could not be
	// read as a literal. Reported rather than dropped: an inventory that
	// under-counts silently makes the comparison pass by omission.
	Unresolved []string `json:"unresolved,omitempty"`
	Files      []string `json:"files"`
}

// Counts of each style, for the summary line.
func (inv *Inventory) Counts() (gin, template int) {
	for _, r := range inv.Routes {
		if r.Style == "template" {
			template++
		} else {
			gin++
		}
	}
	return gin, template
}

// Take walks the workspace and records every route it declares, in either
// style.
func Take(ws *workspace.Workspace) *Inventory {
	inv := &Inventory{Routes: []Route{}}
	seen := map[string]bool{}

	// Every file rather than a layer set. `FilesIn()` with no arguments matches
	// nothing, and naming layers would be the wrong instinct anyway: a legacy
	// service registers routes in `routes/`, in `main.go` and sometimes in a
	// `bootstrap` helper, and the one place an inventory must not have a blind
	// spot is the place a route is hiding.
	for _, f := range ws.Files {
		if f.AST == nil || f.Layer == workspace.LayerTest {
			continue
		}
		before := len(inv.Routes)
		collectGin(f, inv)
		collectTemplate(f, inv)
		if len(inv.Routes) > before && !seen[f.Rel] {
			seen[f.Rel] = true
			inv.Files = append(inv.Files, f.Rel)
		}
	}

	sort.Slice(inv.Routes, func(i, j int) bool {
		if inv.Routes[i].Path != inv.Routes[j].Path {
			return inv.Routes[i].Path < inv.Routes[j].Path
		}
		return inv.Routes[i].Method < inv.Routes[j].Method
	})
	sort.Strings(inv.Files)
	sort.Strings(inv.Unresolved)
	return inv
}

// Compare reports what the second inventory lost relative to the first.
//
// One direction only, and deliberately: routes *added* by a conversion are not
// a defect -- a service that gains `/docs/v3Doc.json` has gained the thing the
// conversion is for. Routes *lost* are the whole question.
func Compare(before, after *Inventory) (missing []Route) {
	have := map[string]bool{}
	handlers := map[string]bool{}
	for _, r := range after.Routes {
		have[r.Key()] = true
		if r.Handler != "" {
			handlers[method(r.Handler)] = true
		}
	}
	for _, r := range before.Routes {
		if have[r.Key()] {
			continue
		}
		// The path moved but the handler came across. Not a loss: a conversion
		// is allowed to change a prefix, and reporting it as a lost route would
		// bury the ones that really went. The path change is visible in the
		// two inventories either way.
		if r.Handler != "" && handlers[method(r.Handler)] {
			continue
		}
		missing = append(missing, r)
	}
	sort.Slice(missing, func(i, j int) bool { return missing[i].Key() < missing[j].Key() })
	return missing
}

// method is the method name out of "h.FetchAwardDetails", lowercased. The
// receiver is dropped because a conversion renames it routinely -- `h` becomes
// `ah`, `c` becomes the handler's own letter -- and the method name is what
// identifies the endpoint.
func method(handler string) string {
	if i := strings.LastIndex(handler, "."); i >= 0 {
		return strings.ToLower(handler[i+1:])
	}
	return strings.ToLower(handler)
}

// ── the legacy shape ────────────────────────────────────────────────────────

// collectGin finds `x.GET("/p", h.M)` registrations and resolves the prefix
// from the `x := parent.Group("/p")` chain in the same function.
func collectGin(f *workspace.File, inv *Inventory) {
	ast.Inspect(f.AST, func(n ast.Node) bool {
		fn, ok := n.(*ast.FuncDecl)
		if !ok || fn.Body == nil {
			return true
		}
		// Group variables resolved in declaration order, so a group declared
		// from another group picks up the prefix that is already known. Gin
		// requires that order too -- the parent has to exist before it is
		// grouped -- so a single forward pass is not a simplification, it is
		// the same constraint the compiler already enforces.
		prefixes := map[string]string{}
		ast.Inspect(fn.Body, func(inner ast.Node) bool {
			switch node := inner.(type) {
			case *ast.AssignStmt:
				recordGroup(node, prefixes)
			case *ast.CallExpr:
				if r, ok := ginRoute(f, node, prefixes); ok {
					inv.Routes = append(inv.Routes, r)
				} else if u, bad := ginUnresolved(f, node); bad {
					inv.Unresolved = append(inv.Unresolved, u)
				}
			}
			return true
		})
		return true
	})
}

// recordGroup notes `x := parent.Group("/p")`, with the parent's prefix
// applied when the parent is itself a known group.
func recordGroup(as *ast.AssignStmt, prefixes map[string]string) {
	if len(as.Lhs) != 1 || len(as.Rhs) != 1 {
		return
	}
	name, ok := identName(as.Lhs[0])
	if !ok {
		return
	}
	call, ok := as.Rhs[0].(*ast.CallExpr)
	if !ok {
		return
	}
	sel, ok := call.Fun.(*ast.SelectorExpr)
	if !ok || sel.Sel.Name != "Group" || len(call.Args) == 0 {
		return
	}
	literal, ok := stringLit(call.Args[0])
	if !ok {
		return
	}
	parent := ""
	if base, ok := identName(sel.X); ok {
		parent = prefixes[base]
	}
	prefixes[name] = join(parent, literal)
}

func ginRoute(f *workspace.File, call *ast.CallExpr, prefixes map[string]string) (Route, bool) {
	sel, ok := call.Fun.(*ast.SelectorExpr)
	if !ok || !methods[sel.Sel.Name] || len(call.Args) < 2 {
		return Route{}, false
	}
	// `serverRoute.GET(...)` is the template's, not Gin's, and is collected by
	// the other pass. Told apart by the receiver being a package alias that
	// ends in "Route" or "route", which is how every converted file in the
	// corpus spells it.
	if base, ok := identName(sel.X); ok && isRoutePkg(base) {
		return Route{}, false
	}
	literal, ok := stringLit(call.Args[0])
	if !ok {
		return Route{}, false
	}
	prefix := ""
	if base, ok := identName(sel.X); ok {
		prefix = prefixes[base]
	}
	line, _ := f.Position(call.Pos())
	return Route{
		Method:  sel.Sel.Name,
		Path:    join(prefix, literal),
		Handler: exprName(call.Args[len(call.Args)-1]),
		Style:   "gin",
		File:    f.Rel,
		Line:    line,
	}, true
}

// ginUnresolved reports a registration whose path is not a literal, so the
// inventory can say it under-counted rather than pretend it did not.
func ginUnresolved(f *workspace.File, call *ast.CallExpr) (string, bool) {
	sel, ok := call.Fun.(*ast.SelectorExpr)
	if !ok || !methods[sel.Sel.Name] || len(call.Args) < 2 {
		return "", false
	}
	if _, ok := stringLit(call.Args[0]); ok {
		return "", false
	}
	line, _ := f.Position(call.Pos())
	return fmt.Sprintf("%s:%d: %s with a non-literal path", f.Rel, line, sel.Sel.Name), true
}

// ── the template shape ──────────────────────────────────────────────────────

// collectTemplate finds `serverRoute.GET("/p", c.M).Name("...")` inside
// Routes() and applies the prefix chain from the handler's constructor.
func collectTemplate(f *workspace.File, inv *Inventory) {
	prefixes := constructorPrefixes(f)

	ast.Inspect(f.AST, func(n ast.Node) bool {
		fn, ok := n.(*ast.FuncDecl)
		if !ok || fn.Body == nil || fn.Name.Name != "Routes" {
			return true
		}
		recv, isMethod := receiverType(fn)
		if !isMethod {
			return true
		}
		prefix := prefixes[strings.TrimPrefix(recv, "*")]

		ast.Inspect(fn.Body, func(inner ast.Node) bool {
			call, ok := inner.(*ast.CallExpr)
			if !ok {
				return true
			}
			// The outermost expression is `....Name("x")`, so the route call is
			// found by unwrapping and the name by reading the wrapper.
			//
			// Whichever branch records the route stops the walk there. The
			// inner call is a *child* of the `.Name(...)` node, so descending
			// past it records the same route a second time -- once with its
			// name and once without, which doubles every count and makes the
			// comparison meaningless in the direction that matters least
			// obviously: a doubled "after" hides nothing, a doubled "before"
			// invents losses.
			name := ""
			if sel, ok := call.Fun.(*ast.SelectorExpr); ok && sel.Sel.Name == "Name" {
				if len(call.Args) > 0 {
					name, _ = stringLit(call.Args[0])
				}
				if next, ok := sel.X.(*ast.CallExpr); ok {
					call = next
				}
			}
			sel, ok := call.Fun.(*ast.SelectorExpr)
			if !ok || !methods[sel.Sel.Name] {
				return true
			}
			base, isIdent := identName(sel.X)
			if !isIdent || !isRoutePkg(base) {
				return true
			}
			if len(call.Args) < 2 {
				return true
			}
			literal, ok := stringLit(call.Args[0])
			if !ok {
				line, _ := f.Position(call.Pos())
				inv.Unresolved = append(inv.Unresolved, fmt.Sprintf(
					"%s:%d: %s with a non-literal path", f.Rel, line, sel.Sel.Name))
				return false
			}
			line, _ := f.Position(call.Pos())
			inv.Routes = append(inv.Routes, Route{
				Method:  sel.Sel.Name,
				Path:    join(prefix, literal),
				Handler: exprName(call.Args[len(call.Args)-1]),
				Name:    name,
				Style:   "template",
				File:    f.Rel,
				Line:    line,
			})
			return false
		})
		return true
	})
}

// constructorPrefixes maps a handler type to the base path its constructor
// builds: serverHandler.New("X").SetPrefix("/v1").AddPrefix("/awards").
//
// Keyed by the type the constructor returns rather than by the constructor's
// name, because Routes() is a method on the type and the type is the only thing
// the two have in common.
func constructorPrefixes(f *workspace.File) map[string]string {
	out := map[string]string{}
	ast.Inspect(f.AST, func(n ast.Node) bool {
		fn, ok := n.(*ast.FuncDecl)
		if !ok || fn.Body == nil || fn.Recv != nil {
			return true
		}
		chain := ""
		found := false
		ast.Inspect(fn.Body, func(inner ast.Node) bool {
			call, ok := inner.(*ast.CallExpr)
			if !ok {
				return true
			}
			if prefix, ok := prefixChain(call); ok {
				chain, found = prefix, true
				return false
			}
			return true
		})
		if !found {
			return true
		}
		if name := returnedType(fn); name != "" {
			out[name] = chain
		}
		return true
	})
	return out
}

// prefixChain reads New(...).SetPrefix(...).AddPrefix(...) outermost-in and
// returns the path it builds. SetPrefix replaces; AddPrefix appends -- which is
// what the template's own chain means, and getting it backwards would report
// every converted route under the wrong path.
func prefixChain(call *ast.CallExpr) (string, bool) {
	var segments []string
	set := ""
	node := call
	sawChain := false
	for {
		sel, ok := node.Fun.(*ast.SelectorExpr)
		if !ok {
			break
		}
		switch sel.Sel.Name {
		case "AddPrefix":
			if len(node.Args) > 0 {
				if lit, ok := stringLit(node.Args[0]); ok {
					segments = append([]string{lit}, segments...)
				}
			}
			sawChain = true
		case "SetPrefix":
			if len(node.Args) > 0 {
				if lit, ok := stringLit(node.Args[0]); ok {
					set = lit
				}
			}
			sawChain = true
		case "New":
			// The root of the chain. Its argument is the handler's display
			// name, not a path.
			next, ok := sel.X.(*ast.CallExpr)
			if !ok {
				return join(set, strings.Join(segments, "/")), sawChain
			}
			node = next
			continue
		}
		next, ok := sel.X.(*ast.CallExpr)
		if !ok {
			break
		}
		node = next
	}
	if !sawChain {
		return "", false
	}
	return join(set, strings.Join(segments, "/")), true
}

// returnedType is the type name a constructor returns: `*AwardHandler` gives
// "AwardHandler".
func returnedType(fn *ast.FuncDecl) string {
	if fn.Type.Results == nil || len(fn.Type.Results.List) == 0 {
		return ""
	}
	expr := fn.Type.Results.List[0].Type
	if star, ok := expr.(*ast.StarExpr); ok {
		expr = star.X
	}
	if id, ok := expr.(*ast.Ident); ok {
		return id.Name
	}
	return ""
}

// ── small helpers ───────────────────────────────────────────────────────────

func isRoutePkg(name string) bool {
	lower := strings.ToLower(name)
	return strings.HasSuffix(lower, "route") || strings.HasSuffix(lower, "routes")
}

func identName(e ast.Expr) (string, bool) {
	id, ok := e.(*ast.Ident)
	if !ok {
		return "", false
	}
	return id.Name, true
}

func stringLit(e ast.Expr) (string, bool) {
	lit, ok := e.(*ast.BasicLit)
	if !ok || lit.Kind.String() != "STRING" {
		return "", false
	}
	v, err := strconv.Unquote(lit.Value)
	if err != nil {
		return "", false
	}
	return v, true
}

// exprName renders a handler expression as it is written: "h.Fetch".
func exprName(e ast.Expr) string {
	switch node := e.(type) {
	case *ast.Ident:
		return node.Name
	case *ast.SelectorExpr:
		if base, ok := identName(node.X); ok {
			return base + "." + node.Sel.Name
		}
		return node.Sel.Name
	}
	return ""
}

func receiverType(fn *ast.FuncDecl) (string, bool) {
	if fn.Recv == nil || len(fn.Recv.List) == 0 {
		return "", false
	}
	expr := fn.Recv.List[0].Type
	if star, ok := expr.(*ast.StarExpr); ok {
		expr = star.X
	}
	if id, ok := expr.(*ast.Ident); ok {
		return id.Name, true
	}
	return "", false
}

// join concatenates two path segments into one clean path. The empty cases
// matter more than they look: a route registered at "" on a group is the
// collection endpoint, and must come out as the group's own path rather than
// as a path with a trailing slash, which is a different route to a router.
func join(prefix, suffix string) string {
	a := strings.Trim(prefix, "/")
	b := strings.Trim(suffix, "/")
	switch {
	case a == "" && b == "":
		return "/"
	case a == "":
		return "/" + b
	case b == "":
		return "/" + a
	}
	return "/" + a + "/" + b
}
