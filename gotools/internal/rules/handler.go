package rules

import (
	"go/ast"
	"strings"

	"gitlab.cept.gov.in/it-2.0/dakcoder/gotools/internal/workspace"
)

const (
	serverCtxType = "*serverRoute.Context"
	baseEmbed     = "*serverHandler.Base"
)

// bindMethods are gin's manual binding calls, ordered most-specific first so a
// single call is reported once, under its real name.
var bindMethods = []string{
	"ShouldBindBodyWith", "ShouldBindJSON", "ShouldBindQuery", "ShouldBindUri",
	"ShouldBindWith", "ShouldBind", "BindJSON", "BindQuery", "BindUri", "MustBindWith",
}

// HandlerSignature enforces the DTO handler contract from SOP.md.
//
//	func (h *XHandler) M(sctx *serverRoute.Context, req ReqT) (*resp.RespT, error)
//
// Input-less routes legitimately use `_ struct{}` as the request parameter —
// the reference `ListUsers` does exactly that — so the rule accepts it.
var HandlerSignature = Rule{
	ID:       "handler-signature",
	Severity: SeverityError,
	Summary:  "handler methods take (sctx *serverRoute.Context, req T) and return (*resp.R, error)",
	Citation: "SOP.md §[handler].go (step 5, no gin.Context); skill.md §Handler Pattern",
	Check: func(p *Pass) {
		for _, f := range p.WS.FilesIn(workspace.LayerHandler) {
			// Manual binding is a violation wherever it appears in the layer.
			//
			// One finding per function, and the list is ordered most-specific
			// first: "ShouldBindJSON" contains "ShouldBind", so a naive loop
			// reports the same call twice and the developer sees phantom
			// duplicates.
			funcsIn(f, func(fd *ast.FuncDecl) {
				if fd.Body == nil {
					return
				}
				for _, bad := range bindMethods {
					if callsMatching(fd.Body, "."+bad) {
						p.At(f, fd).
							Fix("delete the manual bind; declare the fields on the request DTO and let govalid validate them").
							Report("%s calls %s; the framework binds and validates from the request struct", fd.Name.Name, bad)
						return
					}
				}
			})

			funcsIn(f, func(fd *ast.FuncDecl) {
				recv, isMethod := receiverType(fd)
				if !isMethod || !strings.HasSuffix(recv, "Handler") {
					return
				}
				// Routes() and unexported helpers are not route handlers.
				if fd.Name.Name == "Routes" || !fd.Name.IsExported() {
					return
				}

				ps, rs := params(fd), results(fd)

				// A gin.Context anywhere in the signature is the legacy shape;
				// report it specifically because the remedy differs.
				for _, t := range ps {
					if strings.Contains(t, "gin.Context") {
						p.At(f, fd).
							Fix("replace *gin.Context with sctx *serverRoute.Context and a typed request DTO").
							Report("%s takes %s; handlers must not depend on gin", fd.Name.Name, t)
						return
					}
				}

				if len(ps) != 2 || len(rs) != 2 {
					p.At(f, fd).
						Fix("use (sctx *serverRoute.Context, req T) (*resp.R, error)").
						Report("%s has %d parameter(s) and %d result(s); want 2 and 2", fd.Name.Name, len(ps), len(rs))
					return
				}
				if ps[0] != serverCtxType {
					p.At(f, fd).
						Fix("first parameter must be sctx *serverRoute.Context").
						Report("%s first parameter is %s, want %s", fd.Name.Name, ps[0], serverCtxType)
				}
				if rs[1] != "error" {
					p.At(f, fd).
						Fix("second result must be error").
						Report("%s second result is %s, want error", fd.Name.Name, rs[1])
				}
				if !strings.HasPrefix(rs[0], "*") {
					p.At(f, fd).
						Fix("return a pointer to a response struct from handler/response").
						Report("%s first result is %s, want a *response struct", fd.Name.Name, rs[0])
				}
			})
		}
	},
}

// HandlerBase enforces the embed and the constructor chain. Without the Base
// embed the type does not satisfy serverHandler.Handler, which surfaces later as
// an opaque Uber-FX graph error rather than a compile error — so catching it
// here saves a debugging cycle.
var HandlerBase = Rule{
	ID:       "handler-base",
	Severity: SeverityError,
	Summary:  "handlers embed *serverHandler.Base and construct with New(...).SetPrefix(...).AddPrefix(...)",
	Citation: "SOP.md §[handler].go steps 2–3; skill.md §Handler Pattern",
	Check: func(p *Pass) {
		for _, f := range p.WS.FilesIn(workspace.LayerHandler) {
			structsIn(f, func(name string, ts *ast.TypeSpec, st *ast.StructType) {
				if !strings.HasSuffix(name, "Handler") {
					return
				}
				embedded := false
				for _, fld := range st.Fields.List {
					if isEmbedded(fld) && typeString(fld.Type) == baseEmbed {
						embedded = true
					}
				}
				if !embedded {
					p.At(f, ts).
						Fix("add %s as the first embedded field", baseEmbed).
						Report("%s does not embed %s", name, baseEmbed)
				}
			})

			funcsIn(f, func(fd *ast.FuncDecl) {
				if _, isMethod := receiverType(fd); isMethod {
					return
				}
				if !strings.HasPrefix(fd.Name.Name, "New") || fd.Body == nil {
					return
				}
				rs := results(fd)
				if len(rs) != 1 || !strings.HasSuffix(rs[0], "Handler") {
					return
				}
				var chain []string
				ast.Inspect(fd.Body, func(n ast.Node) bool {
					call, ok := n.(*ast.CallExpr)
					if !ok {
						return true
					}
					if c := selectorChain(call); len(c) > 0 && strings.HasSuffix(c[0], "serverHandler.New") {
						chain = c
						return false
					}
					return true
				})
				if chain == nil {
					p.At(f, fd).
						Fix(`base := serverHandler.New("Xs").SetPrefix("/v1").AddPrefix("")`).
						Report("%s does not build a base with serverHandler.New(...)", fd.Name.Name)
					return
				}
				has := func(m string) bool {
					for _, c := range chain {
						if c == m {
							return true
						}
					}
					return false
				}
				if !has("SetPrefix") {
					p.At(f, fd).
						Fix(`add .SetPrefix("/v1") to the constructor chain`).
						Report("%s is missing SetPrefix; the API version prefix would be lost", fd.Name.Name)
				}
				if !has("AddPrefix") {
					p.At(f, fd).
						Fix(`add .AddPrefix("") (or a resource prefix) to the constructor chain`).
						Report("%s is missing AddPrefix", fd.Name.Name)
				}
			})
		}
	},
}

// RoutesInHandler enforces that routes are declared per-handler and named.
//
// The .Name(...) requirement is not cosmetic: the generated swagger document
// takes its operation names from it, so an unnamed route is invisible in
// docs/v3Doc.json.
var RoutesInHandler = Rule{
	ID:       "routes-in-handler",
	Severity: SeverityError,
	Summary:  "each handler declares Routes() and every route carries .Name(...)",
	Citation: "SOP.md §Define the Routes; skill.md §Routing Pattern",
	Check: func(p *Pass) {
		for _, f := range p.WS.FilesIn(workspace.LayerHandler) {
			declaresHandler := false
			structsIn(f, func(name string, _ *ast.TypeSpec, _ *ast.StructType) {
				if strings.HasSuffix(name, "Handler") {
					declaresHandler = true
				}
			})
			if !declaresHandler {
				continue
			}

			var routesFn *ast.FuncDecl
			funcsIn(f, func(fd *ast.FuncDecl) {
				if fd.Name.Name == "Routes" {
					if _, isMethod := receiverType(fd); isMethod {
						routesFn = fd
					}
				}
			})
			if routesFn == nil {
				p.AtFile(f).
					Fix("add func (h *XHandler) Routes() []serverRoute.Route { ... }").
					Report("handler declared here has no Routes() method; its routes will never be registered")
				continue
			}

			// Every serverRoute.<VERB>(...) must be followed by .Name(...).
			ast.Inspect(routesFn, func(n ast.Node) bool {
				call, ok := n.(*ast.CallExpr)
				if !ok {
					return true
				}
				chain := selectorChain(call)
				if len(chain) == 0 || !strings.HasPrefix(chain[0], "serverRoute.") {
					return true
				}
				verb := strings.TrimPrefix(chain[0], "serverRoute.")
				switch verb {
				case "GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS":
				default:
					return true
				}
				for _, c := range chain[1:] {
					if c == "Name" {
						return false
					}
				}
				p.At(f, call).
					Fix(`append .Name("Describe The Operation") — swagger takes operation names from it`).
					Report("route %s(...) has no .Name(...); it will be missing from docs/v3Doc.json", chain[0])
				return false
			})
		}
	},
}
