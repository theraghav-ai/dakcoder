package convert

import (
	"bytes"
	"fmt"
	"go/ast"
	"go/token"
	"strconv"
	"strings"

	"gitlab.cept.gov.in/it-2.0/dakcoder/gotools/internal/gopatch"
	"gitlab.cept.gov.in/it-2.0/dakcoder/gotools/internal/workspace"
)

// The legacy test harness, moved onto the template router.
//
// A legacy service's tests boot the app with fxtest, populate the legacy
// api-server Router, invoke routes.Routes on it and send requests through
// Router.Engine.ServeHTTP. With routes.go retired and the handlers on the
// template signature, that harness no longer compiles -- and `go vet ./...`,
// which the final gate runs, compiles tests.
//
// The rewrite keeps every test file as it is: Router stays a package variable
// with an Engine, built by the template server's own registration
// (NewRouter(...).RegisterRoutes() over the controller group), so a request a
// test sends goes through exactly the routes production serves.

const (
	legacyServer = "gitlab.cept.gov.in/it-2.0-common/api-server"
	serverPath   = "gitlab.cept.gov.in/it-2.0-common/n-api-server"
	mwPath       = "gitlab.cept.gov.in/it-2.0-common/n-api-server/middlewares"
	configPath   = "gitlab.cept.gov.in/it-2.0-common/api-config"
)

const testRouterDecl = `// TestRouter is the router the tests send requests through: the template
// server's own route registration, over every handler in the controller group.
type TestRouter struct{ Engine *gin.Engine }

type testControllers struct {
	fx.In
	Controllers []serverHandler.Handler ` + "`group:\"servercontrollers\"`" + `
}

func newTestRouter(p testControllers, cfg *%s.Config) {
	engine := gin.New()
	engine.Use(middlewares.ErrorHandler())
	server.NewRouter(engine, cfg, server.ParseControllers(p.Controllers...)).RegisterRoutes()
	%s = &TestRouter{Engine: engine}
}
`

// testHarness rewrites one test file. Unchanged unless it holds the legacy
// router harness.
func testHarness(rel string, src []byte, routesPkg string) ([]byte, bool, error) {
	re, err := reparse(rel, src)
	if err != nil {
		return nil, false, err
	}
	imports := map[string]*ast.ImportSpec{}
	for _, im := range re.AST.Imports {
		p, _ := strconv.Unquote(im.Path.Value)
		imports[p] = im
	}
	legacy := imports[legacyServer]
	if legacy == nil {
		return src, false, nil
	}
	alias := importName(legacy)
	if legacy.Name == nil {
		alias = "router"
	}
	cfgAlias := "config"
	if c := imports[configPath]; c != nil && c.Name != nil {
		cfgAlias = c.Name.Name
	}
	routesAlias := ""
	if r := imports[routesPkg]; r != nil {
		routesAlias = importName(r)
		if r.Name == nil {
			routesAlias = routesPkg[strings.LastIndex(routesPkg, "/")+1:]
		}
	}
	off := func(p token.Pos) int { return re.FSet.Position(p).Offset }

	// var Router *router.Router
	routerVar := ""
	var edits []edit
	for _, d := range re.AST.Decls {
		g, ok := d.(*ast.GenDecl)
		if !ok || g.Tok != token.VAR {
			continue
		}
		for _, s := range g.Specs {
			vs, ok := s.(*ast.ValueSpec)
			if !ok || len(vs.Names) != 1 || !isSel(vs.Type, alias, "Router", true) {
				continue
			}
			routerVar = vs.Names[0].Name
			edits = append(edits, edit{off(vs.Type.Pos()), off(vs.Type.End()), "*TestRouter"})
		}
	}
	if routerVar == "" {
		return src, false, nil
	}
	ast.Inspect(re.AST, func(n ast.Node) bool {
		switch x := n.(type) {
		case *ast.FuncDecl:
			if x.Type.Results != nil {
				for _, r := range x.Type.Results.List {
					if isSel(r.Type, alias, "Router", true) {
						edits = append(edits, edit{off(r.Type.Pos()), off(r.Type.End()), "*TestRouter"})
					}
				}
			}
		case *ast.CallExpr:
			sel, ok := x.Fun.(*ast.SelectorExpr)
			if !ok || len(x.Args) != 1 {
				return true
			}
			switch sel.Sel.Name {
			case "Populate":
				if u, ok := x.Args[0].(*ast.UnaryExpr); ok {
					if id, ok := u.X.(*ast.Ident); ok && id.Name == routerVar {
						lo, hi := argSpan(re.Src, off(x.Pos()), off(x.End()))
						edits = append(edits, edit{lo, hi, ""})
					}
				}
			case "Invoke":
				if routesAlias != "" && isSel(x.Args[0], routesAlias, "Routes", false) {
					edits = append(edits, edit{off(x.Args[0].Pos()), off(x.Args[0].End()), "newTestRouter"})
				}
			}
		}
		return true
	})
	out, err := apply(re.Src, edits)
	if err != nil {
		return nil, false, err
	}
	out = append(bytes.TrimRight(out, "\n"), []byte("\n\n"+fmt.Sprintf(testRouterDecl, cfgAlias, routerVar))...)
	// imports: the legacy router and the routes package go; the template's come in
	out, err = removeImport(out, rel, legacyServer)
	if err != nil {
		return nil, false, err
	}
	if routesAlias != "" {
		if out, err = removeImport(out, rel, routesPkg); err != nil {
			return nil, false, err
		}
	}
	for _, im := range [][2]string{
		{"", "github.com/gin-gonic/gin"}, {"server", serverPath}, {HandlerAlias, HandlerPath},
		{"", mwPath}, {"", "go.uber.org/fx"},
	} {
		if out, _, err = gopatch.EnsureImport(out, im[0], im[1], ""); err != nil {
			return nil, false, err
		}
	}
	if imports[configPath] == nil {
		if out, _, err = gopatch.EnsureImport(out, "config", configPath, ""); err != nil {
			return nil, false, err
		}
	}
	return out, true, nil
}

// isSel matches pkg.Name, or *pkg.Name when ptr.
func isSel(e ast.Expr, pkg, name string, ptr bool) bool {
	if ptr {
		st, ok := e.(*ast.StarExpr)
		if !ok {
			return false
		}
		e = st.X
	}
	sel, ok := e.(*ast.SelectorExpr)
	if !ok {
		return false
	}
	id, ok := sel.X.(*ast.Ident)
	return ok && id.Name == pkg && sel.Sel.Name == name
}

// argSpan widens a call in an argument list to its whole line and comma.
func argSpan(src []byte, lo, hi int) (int, int) {
	k := hi
	for k < len(src) && (src[k] == ' ' || src[k] == '\t') {
		k++
	}
	if k < len(src) && src[k] == ',' {
		hi = k + 1
	}
	return wholeLines(src, lo, hi)
}

func removeImport(src []byte, rel, path string) ([]byte, error) {
	re, err := reparse(rel, src)
	if err != nil {
		return nil, err
	}
	for _, im := range re.AST.Imports {
		p, _ := strconv.Unquote(im.Path.Value)
		if p != path {
			continue
		}
		lo, hi := wholeLines(re.Src, re.FSet.Position(im.Pos()).Offset, re.FSet.Position(im.End()).Offset)
		return apply(re.Src, []edit{{lo, hi, ""}})
	}
	return src, nil
}

// testFiles are the workspace's _test.go files, LF-normalised.
func testFiles(ws *workspace.Workspace) ([]*workspace.File, map[*workspace.File]gopatch.EOL) {
	var out []*workspace.File
	eol := map[*workspace.File]gopatch.EOL{}
	// The workspace leaves tests out unless asked; the harness is in them.
	withTests, err := workspace.Load(ws.Root, workspace.WithTests())
	if err != nil {
		return nil, eol
	}
	for _, f := range withTests.Files {
		if f.AST == nil || !strings.HasSuffix(f.Rel, "_test.go") {
			continue
		}
		if lf := gopatch.ToLF(f.Src); !bytes.Equal(lf, f.Src) {
			re, err := reparse(f.Rel, lf)
			if err != nil {
				continue
			}
			re.Abs = f.Abs
			eol[re] = gopatch.DetectEOL(f.Src)
			f = re
		}
		out = append(out, f)
	}
	return out, eol
}
