package convert

import (
	"bytes"
	"fmt"
	"go/ast"
	"go/token"
	"os"
	"path/filepath"
	"regexp"
	"sort"
	"strconv"
	"strings"
	"unicode"

	"gitlab.cept.gov.in/it-2.0/dakcoder/gotools/internal/gopatch"
	"gitlab.cept.gov.in/it-2.0/dakcoder/gotools/internal/routes"
	"gitlab.cept.gov.in/it-2.0/dakcoder/gotools/internal/workspace"
)

// The service half of a conversion: where routes are declared and how the
// application is wired. Everything here is decided by what routes/routes.go
// already says, so none of it needs a model:
//
//   - every route moves onto its handler, as a Routes() method, with the
//     handler embedding *serverHandler.Base and its constructor setting the
//     prefix the routes share;
//   - bootstrap registers each HTTP handler into the server's controller
//     group (fx.Annotate ... serverHandler.ServerControllersGroupTag);
//   - main.go stops invoking routes.Routes and starts n-api-bootstrapper;
//   - routes/routes.go is retired;
//   - the logger, database, error and bootstrapper libraries swap to their
//     n-api lines module-wide. Measured on pao-backend: with those swaps the
//     whole service compiled except routes/routes.go -- the file this retires.
//
// A route whose handler method is still on gin cannot be registered on the
// template router, so it is written into Routes() as a comment naming the
// method and path -- nothing about it is lost when routes.go goes -- and
// reported. Registering it is one line once the method is converted.

const (
	HandlerPath  = "gitlab.cept.gov.in/it-2.0-common/n-api-server/handler"
	HandlerAlias = "serverHandler"
)

// librarySwaps are the legacy imports whose n-api replacement is a drop-in
// for the way a legacy service uses them. api-validation is not here:
// n-api-validation is compile-time govalid and has no ValidateStruct, so a
// method still validating at runtime keeps the legacy one. api-config stays
// by the SOP.
var librarySwaps = [][2]string{
	{legacyLog, templateLog},
	{"gitlab.cept.gov.in/it-2.0-common/api-db", "gitlab.cept.gov.in/it-2.0-common/n-api-db"},
	{legacyErrors, templateErrors},
	{"gitlab.cept.gov.in/it-2.0-common/api-bootstrapper", "gitlab.cept.gov.in/it-2.0-common/n-api-bootstrapper"},
}

// ServiceResult is what the service conversion did.
type ServiceResult struct {
	// Handlers that now declare Routes(), with how many routes each.
	Handlers []HandlerRoutes `json:"handlers"`
	// Routes still on gin methods, written into Routes() as comments.
	Pending []string `json:"pending,omitempty"`
	// Routes the template serves itself (health, swagger) and were dropped.
	Dropped []string `json:"dropped,omitempty"`
	// Wired lists the constructors bootstrap now registers as controllers.
	Wired   []string `json:"wired,omitempty"`
	Retired []string `json:"retired,omitempty"`
	// Tests are the test files whose harness now builds the template router.
	Tests []string `json:"tests,omitempty"`
	Written []string `json:"written,omitempty"`
	Notes   []string `json:"notes,omitempty"`
	Error   string   `json:"error,omitempty"`
	DryRun  bool     `json:"dry_run"`
}

// HandlerRoutes is one handler's new Routes().
type HandlerRoutes struct {
	// Base is the constructor expression still to add, when the constructor
	// is declared in another file than the struct.
	Base string `json:"-"`
	Type    string `json:"type"`
	File    string `json:"file"`
	Prefix  string `json:"prefix"`
	Routes  int    `json:"routes"`
	Pending int    `json:"pending,omitempty"`
}

type routeEntry struct {
	method, path, fn string
	created          bool
	converted        bool
}

// Service converts the service's wiring. Run it after the handlers.
func Service(ws *workspace.Workspace, write bool) (*ServiceResult, error) {
	res := &ServiceResult{DryRun: !write}
	files, endings := goFiles(ws)
	byRel := map[string]*workspace.File{}
	for _, f := range files {
		byRel[f.Rel] = f
	}
	out := map[string][]byte{} // rel -> new LF content
	get := func(rel string) []byte {
		if b, ok := out[rel]; ok {
			return b
		}
		return byRel[rel].Src
	}

	// ── 1. what routes.go registers, per handler type ────────────────────
	inv := routes.Take(ws)
	regFile, perType, dropped, err := legacyRegistrations(inv, byRel)
	if err != nil {
		res.Error = err.Error()
		return res, nil
	}
	res.Dropped = dropped

	// ── 2. Routes() on each handler ──────────────────────────────────────
	types := make([]string, 0, len(perType))
	for t := range perType {
		types = append(types, t)
	}
	sort.Strings(types)
	for _, typ := range types {
		f, ok := handlerFile(files, typ)
		if !ok {
			res.Notes = append(res.Notes, fmt.Sprintf("%s: no struct declaration found in handler/; its routes stay in %s", typ, regFile))
			continue
		}
		src, hr, pending, err := addRoutes(f, get(f.Rel), typ, perType[typ])
		if err != nil {
			res.Notes = append(res.Notes, fmt.Sprintf("%s: %v", typ, err))
			continue
		}
		if hr.Base != "" {
			// The constructor lives in another file: extend it there, and keep
			// nothing for this handler if it cannot be.
			rel, ctorSrc, err := extendCtor(files, get, typ, hr.Base)
			if err != nil {
				res.Notes = append(res.Notes, fmt.Sprintf("%s: %v", typ, err))
				continue
			}
			out[rel] = ctorSrc
		}
		out[f.Rel] = src
		res.Handlers = append(res.Handlers, hr)
		res.Pending = append(res.Pending, pending...)
	}
	routed := map[string]bool{}
	for _, h := range res.Handlers {
		routed[h.Type] = true
	}

	// ── 3. bootstrap: HTTP handlers join the controller group ────────────
	for _, f := range files {
		if f.Layer != workspace.LayerBootstrap && !strings.HasPrefix(f.Rel, "bootstrap/") {
			continue
		}
		src, wired, err := wireControllers(f.Rel, get(f.Rel), routed, handlerCtors(files, routed))
		if err != nil {
			res.Notes = append(res.Notes, fmt.Sprintf("%s: %v", f.Rel, err))
			continue
		}
		if len(wired) > 0 {
			out[f.Rel] = src
			res.Wired = append(res.Wired, wired...)
		}
	}

	// ── 4. main.go and every other importer of the routes package ────────
	if regFile != "" && len(res.Handlers) > 0 {
		routesPkg := ws.ModulePath + "/" + filepath.ToSlash(filepath.Dir(regFile))
		for _, f := range files {
			if f.Rel == regFile || strings.HasSuffix(f.Rel, "_test.go") {
				continue
			}
			src, changed, err := dropRoutesInvoke(f.Rel, get(f.Rel), routesPkg)
			if err != nil {
				res.Notes = append(res.Notes, fmt.Sprintf("%s: %v", f.Rel, err))
				continue
			}
			if changed {
				out[f.Rel] = src
			}
		}
		res.Retired = append(res.Retired, regFile)
	}

	// ── 5. the library swaps, module-wide ────────────────────────────────
	for _, f := range files {
		if f.Rel == regFile || strings.HasPrefix(f.Rel, "gen/") {
			continue
		}
		src := get(f.Rel)
		swapped := src
		for _, s := range librarySwaps {
			swapped = bytes.ReplaceAll(swapped, []byte(strconv.Quote(s[0])), []byte(strconv.Quote(s[1])))
		}
		if bytes.Equal(swapped, src) {
			continue
		}
		swapped, err := intErrorCodes(swapped, f.Rel)
		if err != nil {
			res.Notes = append(res.Notes, fmt.Sprintf("%s: %v", f.Rel, err))
			continue
		}
		out[f.Rel] = swapped
	}

	// ── 5b. struct tags go vet rejects ───────────────────────────────────
	// `json	:"trans_date"` -- whitespace between key and colon -- is a tag
	// reflect cannot read, so the field was never bound or validated, and go
	// vet (which the final gate runs) fails on it. The legacy build never ran
	// vet. Only that one malformation is fixed, and only when the fix makes
	// the tag valid.
	for _, f := range files {
		src := get(f.Rel)
		fixed, n, err := fixStructTags(f.Rel, src)
		if err != nil || n == 0 {
			continue
		}
		out[f.Rel] = fixed
		res.Notes = append(res.Notes, fmt.Sprintf("%s: fixed %d malformed struct tag(s)", f.Rel, n))
	}

	// ── 6. the test harness, and the library swaps in tests ──────────────
	if regFile != "" && len(res.Handlers) > 0 {
		routesPkg := ws.ModulePath + "/" + filepath.ToSlash(filepath.Dir(regFile))
		tfiles, tendings := testFiles(ws)
		for _, f := range tfiles {
			src, changed, err := testHarness(f.Rel, f.Src, routesPkg)
			if err != nil {
				res.Notes = append(res.Notes, fmt.Sprintf("%s: %v", f.Rel, err))
				continue
			}
			swapped := src
			for _, s := range librarySwaps {
				swapped = bytes.ReplaceAll(swapped, []byte(strconv.Quote(s[0])), []byte(strconv.Quote(s[1])))
			}
			if !changed && bytes.Equal(swapped, f.Src) {
				continue
			}
			formatted, err := gopatch.Format(swapped)
			if err != nil {
				res.Notes = append(res.Notes, fmt.Sprintf("%s: the result does not format (%v); left as it was", f.Rel, err))
				continue
			}
			ending := tendings[f]
			if ending == "" {
				ending = gopatch.LF
			}
			if write {
				if err := os.WriteFile(f.Abs, gopatch.ApplyEOL(formatted, ending), 0o644); err != nil {
					res.Notes = append(res.Notes, fmt.Sprintf("%s: write: %v", f.Rel, err))
					continue
				}
			}
			if changed {
				res.Tests = append(res.Tests, f.Rel)
			}
			res.Written = append(res.Written, f.Rel)
		}
	}

	// ── write ────────────────────────────────────────────────────────────
	rels := make([]string, 0, len(out))
	for rel := range out {
		rels = append(rels, rel)
	}
	sort.Strings(rels)
	for _, rel := range rels {
		formatted, err := gopatch.Format(out[rel])
		if err != nil {
			res.Notes = append(res.Notes, fmt.Sprintf("%s: the result does not format (%v); left as it was", rel, err))
			continue
		}
		f := byRel[rel]
		ending := endings[f]
		if ending == "" {
			ending = gopatch.LF
		}
		if bytes.Equal(formatted, f.Src) {
			continue
		}
		if write {
			if err := os.WriteFile(f.Abs, gopatch.ApplyEOL(formatted, ending), 0o644); err != nil {
				res.Notes = append(res.Notes, fmt.Sprintf("%s: write: %v", rel, err))
				continue
			}
		}
		res.Written = append(res.Written, rel)
	}
	if write && regFile != "" && len(res.Handlers) > 0 {
		if err := os.Remove(byRel[regFile].Abs); err != nil {
			res.Notes = append(res.Notes, fmt.Sprintf("%s: could not remove: %v", regFile, err))
		} else if entries, err := os.ReadDir(filepath.Dir(byRel[regFile].Abs)); err == nil && len(entries) == 0 {
			// The package was only routes.go: leave no empty directory behind.
			_ = os.Remove(filepath.Dir(byRel[regFile].Abs))
		}
	}
	return res, nil
}

// legacyRegistrations groups routes.go's gin registrations by handler type.
// The handler expression is the registering function's parameter
// (`paogenHandler.ListHoaHandler`), so the parameter's type names the handler.
func legacyRegistrations(inv *routes.Inventory, byRel map[string]*workspace.File) (string, map[string][]routeEntry, []string, error) {
	perType := map[string][]routeEntry{}
	var dropped []string
	regFile := ""
	params := map[string]map[string]string{} // file -> param -> type
	for _, r := range inv.Routes {
		if r.Style != "gin" {
			continue
		}
		f := byRel[r.File]
		if f == nil {
			continue
		}
		if regFile == "" {
			regFile = r.File
		} else if regFile != r.File {
			return "", nil, nil, fmt.Errorf("gin routes are registered in two files (%s, %s); the conversion handles one", regFile, r.File)
		}
		if params[r.File] == nil {
			params[r.File] = handlerParams(f)
		}
		recv, fn, ok := strings.Cut(r.Handler, ".")
		typ := params[r.File][recv]
		if !ok || typ == "" {
			dropped = append(dropped, fmt.Sprintf("%s %s (%s): served by the template itself or not a handler method", r.Method, r.Path, firstNonEmpty(r.Handler, "inline")))
			continue
		}
		perType[typ] = append(perType[typ], routeEntry{method: strings.ToUpper(r.Method), path: r.Path, fn: fn})
	}
	return regFile, perType, dropped, nil
}

func firstNonEmpty(a, b string) string {
	if a != "" {
		return a
	}
	return b
}

// handlerParams maps every parameter of type *pkg.XHandler to XHandler.
func handlerParams(f *workspace.File) map[string]string {
	out := map[string]string{}
	for _, d := range f.AST.Decls {
		fd, ok := d.(*ast.FuncDecl)
		if !ok || fd.Type.Params == nil {
			continue
		}
		for _, p := range fd.Type.Params.List {
			st, ok := p.Type.(*ast.StarExpr)
			if !ok {
				continue
			}
			var name string
			switch t := st.X.(type) {
			case *ast.SelectorExpr:
				name = t.Sel.Name
			case *ast.Ident:
				name = t.Name
			}
			if !strings.HasSuffix(name, "Handler") {
				continue
			}
			for _, id := range p.Names {
				out[id.Name] = name
			}
		}
	}
	return out
}

func handlerFile(files []*workspace.File, typ string) (*workspace.File, bool) {
	for _, f := range files {
		if f.Layer != workspace.LayerHandler {
			continue
		}
		if structDecl(f.AST, typ) != nil {
			return f, true
		}
	}
	return nil, false
}

func structDecl(af *ast.File, typ string) *ast.StructType {
	for _, d := range af.Decls {
		g, ok := d.(*ast.GenDecl)
		if !ok || g.Tok != token.TYPE {
			continue
		}
		for _, s := range g.Specs {
			ts, ok := s.(*ast.TypeSpec)
			if ok && ts.Name.Name == typ {
				st, _ := ts.Type.(*ast.StructType)
				return st
			}
		}
	}
	return nil
}

// addRoutes embeds the Base, sets the prefix in the constructor, and appends
// Routes(), in one handler file.
func addRoutes(f *workspace.File, src []byte, typ string, entries []routeEntry) ([]byte, HandlerRoutes, []string, error) {
	hr := HandlerRoutes{Type: typ, File: f.Rel}
	re, err := reparse(f.Rel, src)
	if err != nil {
		return nil, hr, nil, err
	}
	af := re.AST
	off := func(p token.Pos) int { return re.FSet.Position(p).Offset }

	st := structDecl(af, typ)
	if st == nil {
		return nil, hr, nil, fmt.Errorf("struct not found")
	}
	recv := "h"
	var ctor *ast.FuncDecl
	methods := map[string]*ast.FuncDecl{}
	for _, d := range af.Decls {
		fd, ok := d.(*ast.FuncDecl)
		if !ok {
			continue
		}
		if fd.Recv != nil && recvName(fd) == typ {
			methods[fd.Name.Name] = fd
			if len(fd.Recv.List[0].Names) == 1 {
				recv = fd.Recv.List[0].Names[0].Name
			}
		}
		if fd.Recv == nil && returnsPtrTo(fd, typ) {
			ctor = fd
		}
	}
	if _, has := methods["Routes"]; has {
		return nil, hr, nil, fmt.Errorf("already declares Routes()")
	}
	var lit *ast.CompositeLit
	if ctor != nil {
		if lit = returnedLiteral(ctor, typ); lit == nil {
			return nil, hr, nil, fmt.Errorf("%s does not return a &%s{...} literal the conversion can extend", ctor.Name.Name, typ)
		}
	}

	// Mark each route with what its method now is.
	for i := range entries {
		m := methods[entries[i].fn]
		entries[i].converted = m != nil && isTemplateHandler(m)
		entries[i].created = m != nil && createdMarker(re, m)
	}
	prefix := commonPrefix(entries)
	hr.Prefix = prefix

	var edits []edit
	// the embedded Base, first field
	edits = append(edits, edit{off(st.Fields.Opening) + 1, off(st.Fields.Opening) + 1, "\n\t*" + HandlerAlias + ".Base"})
	// the constructor -- here, or in another file of the package (the caller
	// extends it there from hr.Base)
	base := fmt.Sprintf("%s.New(%q)%s", HandlerAlias, strings.TrimSuffix(typ, "Handler"), prefixChain(prefix))
	if lit != nil {
		edits = append(edits, baseEdit(lit, off, base))
	} else {
		hr.Base = base
	}

	// Routes()
	var b strings.Builder
	fmt.Fprintf(&b, "\n\n// Routes is what routes/routes.go registered for %s, moved onto the handler\n// by the migration. Paths are relative to the prefix its constructor sets.\n", typ)
	fmt.Fprintf(&b, "func (%s *%s) Routes() []%s.Route {\n\treturn []%s.Route{\n", recv, typ, RouteAlias, RouteAlias)
	names := map[string]int{}
	var pending []string
	needHTTP := false
	for _, e := range entries {
		rel := strings.TrimPrefix(e.path, prefix)
		if !e.converted {
			hr.Pending++
			pending = append(pending, fmt.Sprintf("%s %s -> %s.%s", e.method, e.path, typ, e.fn))
			fmt.Fprintf(&b, "\t\t// TODO(migration): %s %q -> %s.%s, once that method is converted.\n", e.method, rel, recv, e.fn)
			continue
		}
		name := humanName(e.fn)
		names[name]++
		if names[name] > 1 {
			name = fmt.Sprintf("%s (%s %s)", name, e.method, e.path)
		}
		status := ""
		if e.created {
			status, needHTTP = ", http.StatusCreated", true
		}
		fmt.Fprintf(&b, "\t\t%s.%s(%q, %s.%s%s).Name(%q),\n", RouteAlias, e.method, rel, recv, e.fn, status, name)
		hr.Routes++
	}
	b.WriteString("\t}\n}\n")
	end := len(re.Src)
	if ctor != nil {
		end = off(ctor.End())
	}
	edits = append(edits, edit{end, end, b.String()})

	out, err := apply(re.Src, edits)
	if err != nil {
		return nil, hr, nil, err
	}
	for _, im := range [][2]string{{HandlerAlias, HandlerPath}, {RouteAlias, RoutePath}} {
		if out, _, err = gopatch.EnsureImport(out, im[0], im[1], ""); err != nil {
			return nil, hr, nil, err
		}
	}
	if needHTTP {
		if out, _, err = gopatch.EnsureImport(out, "", "net/http", ""); err != nil {
			return nil, hr, nil, err
		}
	}
	return out, hr, pending, nil
}

// baseEdit inserts the Base into a &T{...} constructor literal.
func baseEdit(lit *ast.CompositeLit, off func(token.Pos) int, base string) edit {
	at := off(lit.Lbrace) + 1
	keyed := len(lit.Elts) > 0
	if keyed {
		_, keyed = lit.Elts[0].(*ast.KeyValueExpr)
	}
	switch {
	case len(lit.Elts) == 0:
		return edit{at, at, "Base: " + base}
	case keyed:
		return edit{at, at, "\n\t\tBase: " + base + ","}
	default:
		return edit{at, at, "\n\t\t" + base + ","}
	}
}

// extendCtor sets the Base in typ's constructor, wherever in the package it is.
func extendCtor(files []*workspace.File, get func(string) []byte, typ, base string) (string, []byte, error) {
	for _, f := range files {
		if f.Layer != workspace.LayerHandler {
			continue
		}
		re, err := reparse(f.Rel, get(f.Rel))
		if err != nil {
			continue
		}
		for _, d := range re.AST.Decls {
			fd, ok := d.(*ast.FuncDecl)
			if !ok || fd.Recv != nil || !returnsPtrTo(fd, typ) {
				continue
			}
			lit := returnedLiteral(fd, typ)
			if lit == nil {
				return "", nil, fmt.Errorf("%s does not return a &%s{...} literal the conversion can extend", fd.Name.Name, typ)
			}
			off := func(p token.Pos) int { return re.FSet.Position(p).Offset }
			out, err := apply(re.Src, []edit{baseEdit(lit, off, base)})
			if err != nil {
				return "", nil, err
			}
			out, _, err = gopatch.EnsureImport(out, HandlerAlias, HandlerPath, "")
			return f.Rel, out, err
		}
	}
	return "", nil, fmt.Errorf("no constructor returning *%s", typ)
}

func returnsPtrTo(fd *ast.FuncDecl, typ string) bool {
	if fd.Type.Results == nil || len(fd.Type.Results.List) != 1 {
		return false
	}
	st, ok := fd.Type.Results.List[0].Type.(*ast.StarExpr)
	if !ok {
		return false
	}
	id, ok := st.X.(*ast.Ident)
	return ok && id.Name == typ
}

func returnedLiteral(fd *ast.FuncDecl, typ string) *ast.CompositeLit {
	var lit *ast.CompositeLit
	ast.Inspect(fd.Body, func(n ast.Node) bool {
		u, ok := n.(*ast.UnaryExpr)
		if !ok || u.Op != token.AND || lit != nil {
			return lit == nil
		}
		cl, ok := u.X.(*ast.CompositeLit)
		if !ok {
			return true
		}
		if id, ok := cl.Type.(*ast.Ident); ok && id.Name == typ {
			lit = cl
		}
		return lit == nil
	})
	return lit
}

func isTemplateHandler(fd *ast.FuncDecl) bool {
	if fd.Type.Params == nil || len(fd.Type.Params.List) == 0 {
		return false
	}
	st, ok := fd.Type.Params.List[0].Type.(*ast.StarExpr)
	if !ok {
		return false
	}
	sel, ok := st.X.(*ast.SelectorExpr)
	return ok && sel.Sel.Name == "Context" && !isGinContext(fd.Type.Params.List[0].Type)
}

func createdMarker(f *workspace.File, fd *ast.FuncDecl) bool {
	if fd.Doc == nil {
		return false
	}
	for _, c := range fd.Doc.List {
		if strings.Contains(c.Text, "Answers 201 Created") {
			return true
		}
	}
	return false
}

// commonPrefix is the longest run of literal path segments every route
// shares, never the whole of any route's path.
func commonPrefix(entries []routeEntry) string {
	if len(entries) == 0 {
		return ""
	}
	split := func(p string) []string { return strings.Split(strings.Trim(p, "/"), "/") }
	common := split(entries[0].path)
	for _, e := range entries[1:] {
		segs := split(e.path)
		n := 0
		for n < len(common) && n < len(segs) && common[n] == segs[n] {
			n++
		}
		common = common[:n]
	}
	for i, s := range common {
		if strings.HasPrefix(s, ":") || strings.HasPrefix(s, "*") || s == "" {
			common = common[:i]
			break
		}
	}
	if len(common) == 0 {
		return ""
	}
	return "/" + strings.Join(common, "/")
}

// prefixChain writes the prefix as the template does: SetPrefix("/v1").AddPrefix("/rest").
func prefixChain(prefix string) string {
	if prefix == "" {
		return `.SetPrefix("").AddPrefix("")`
	}
	first, rest, _ := strings.Cut(strings.TrimPrefix(prefix, "/"), "/")
	if rest != "" {
		rest = "/" + rest
	}
	// AddPrefix even when empty, as the template writes it: handler-base
	// checks the chain, not the resulting string.
	return fmt.Sprintf(".SetPrefix(%q).AddPrefix(%q)", "/"+first, rest)
}

// humanName is a route's swagger name from its method: ListHoaHandler -> "List Hoa".
func humanName(fn string) string {
	fn = strings.TrimSuffix(fn, "Handler")
	var b strings.Builder
	runes := []rune(fn)
	for i, r := range runes {
		if i > 0 && unicode.IsUpper(r) && (unicode.IsLower(runes[i-1]) ||
			(i+1 < len(runes) && unicode.IsLower(runes[i+1]) && unicode.IsUpper(runes[i-1]))) {
			b.WriteByte(' ')
		}
		b.WriteRune(r)
	}
	return b.String()
}

// handlerCtors maps each routed handler type's constructor name to its type.
func handlerCtors(files []*workspace.File, routed map[string]bool) map[string]bool {
	out := map[string]bool{}
	for _, f := range files {
		if f.Layer != workspace.LayerHandler {
			continue
		}
		for _, d := range f.AST.Decls {
			fd, ok := d.(*ast.FuncDecl)
			if !ok || fd.Recv != nil || fd.Type.Results == nil || len(fd.Type.Results.List) != 1 {
				continue
			}
			if st, ok := fd.Type.Results.List[0].Type.(*ast.StarExpr); ok {
				if id, ok := st.X.(*ast.Ident); ok && routed[id.Name] {
					out[fd.Name.Name] = true
				}
			}
		}
	}
	return out
}

// wireControllers wraps `handler.NewX,` in fx.Provide with the controller
// group annotation, for each routed handler's constructor.
func wireControllers(rel string, src []byte, routed, ctors map[string]bool) ([]byte, []string, error) {
	re, err := reparse(rel, src)
	if err != nil {
		return nil, nil, err
	}
	var edits []edit
	var wired []string
	ast.Inspect(re.AST, func(n ast.Node) bool {
		call, ok := n.(*ast.CallExpr)
		if !ok {
			return true
		}
		sel, ok := call.Fun.(*ast.SelectorExpr)
		if !ok || sel.Sel.Name != "Provide" {
			return true
		}
		for _, a := range call.Args {
			s, ok := a.(*ast.SelectorExpr)
			if !ok || !ctors[s.Sel.Name] {
				continue
			}
			text := string(re.Src[re.FSet.Position(a.Pos()).Offset:re.FSet.Position(a.End()).Offset])
			edits = append(edits, edit{
				re.FSet.Position(a.Pos()).Offset, re.FSet.Position(a.End()).Offset,
				fmt.Sprintf("fx.Annotate(\n\t\t\t%s,\n\t\t\tfx.As(new(%s.Handler)),\n\t\t\tfx.ResultTags(%s.ServerControllersGroupTag),\n\t\t)", text, HandlerAlias, HandlerAlias),
			})
			wired = append(wired, s.Sel.Name)
		}
		return true
	})
	if len(edits) == 0 {
		return src, nil, nil
	}
	out, err := apply(re.Src, edits)
	if err != nil {
		return nil, nil, err
	}
	out, _, err = gopatch.EnsureImport(out, HandlerAlias, HandlerPath, "")
	return out, wired, err
}

// dropRoutesInvoke removes `fx.Invoke(routes.Routes)` and the routes import.
func dropRoutesInvoke(rel string, src []byte, routesPkg string) ([]byte, bool, error) {
	re, err := reparse(rel, src)
	if err != nil {
		return nil, false, err
	}
	alias := ""
	var spec *ast.ImportSpec
	for _, im := range re.AST.Imports {
		p, _ := strconv.Unquote(im.Path.Value)
		if p == routesPkg {
			spec = im
			alias = importName(im)
			if im.Name == nil {
				alias = filepath.Base(p)
			}
		}
	}
	if spec == nil {
		return src, false, nil
	}
	var edits []edit
	ast.Inspect(re.AST, func(n ast.Node) bool {
		call, ok := n.(*ast.CallExpr)
		if !ok {
			return true
		}
		sel, ok := call.Fun.(*ast.SelectorExpr)
		if !ok || sel.Sel.Name != "Invoke" || len(call.Args) != 1 {
			return true
		}
		arg, ok := call.Args[0].(*ast.SelectorExpr)
		if !ok {
			return true
		}
		if id, ok := arg.X.(*ast.Ident); ok && id.Name == alias {
			lo, hi := wholeLines(re.Src, re.FSet.Position(call.Pos()).Offset, re.FSet.Position(call.End()).Offset)
			// the trailing comma of an argument list
			if hi < len(re.Src) && re.Src[hi-1] != ',' {
				for k := re.FSet.Position(call.End()).Offset; k < len(re.Src) && re.Src[k] != '\n'; k++ {
					if re.Src[k] == ',' {
						lo2, hi2 := wholeLines(re.Src, re.FSet.Position(call.Pos()).Offset, k+1)
						lo, hi = lo2, hi2
						break
					}
				}
			}
			edits = append(edits, edit{lo, hi, ""})
		}
		return true
	})
	lo, hi := wholeLines(re.Src, re.FSet.Position(spec.Pos()).Offset, re.FSet.Position(spec.End()).Offset)
	edits = append(edits, edit{lo, hi, ""})
	out, err := apply(re.Src, edits)
	if err != nil {
		return nil, false, err
	}
	return out, true, nil
}

var tagKeySpace = regexp.MustCompile(`([A-Za-z_][A-Za-z0-9_]*)[ \t]+:"`)

// fixStructTags closes the whitespace between a struct tag's key and its colon.
func fixStructTags(rel string, src []byte) ([]byte, int, error) {
	re, err := reparse(rel, src)
	if err != nil {
		return nil, 0, err
	}
	var edits []edit
	ast.Inspect(re.AST, func(n ast.Node) bool {
		field, ok := n.(*ast.Field)
		if !ok || field.Tag == nil || !strings.HasPrefix(field.Tag.Value, "`") {
			return true
		}
		raw := field.Tag.Value[1 : len(field.Tag.Value)-1]
		if validTag(raw) {
			return true
		}
		fixed := tagKeySpace.ReplaceAllString(raw, `$1:"`)
		if fixed == raw || !validTag(fixed) {
			return true
		}
		lo := re.FSet.Position(field.Tag.Pos()).Offset
		edits = append(edits, edit{lo, lo + len(field.Tag.Value), "`" + fixed + "`"})
		return true
	})
	if len(edits) == 0 {
		return src, 0, nil
	}
	out, err := apply(re.Src, edits)
	return out, len(edits), err
}

// validTag is go vet's structtag well-formedness: key:"value" pairs separated
// by spaces, keys without spaces or colons.
func validTag(tag string) bool {
	for tag != "" {
		i := 0
		for i < len(tag) && tag[i] == ' ' {
			i++
		}
		tag = tag[i:]
		if tag == "" {
			return true
		}
		i = 0
		for i < len(tag) && tag[i] > ' ' && tag[i] != ':' && tag[i] != '"' && tag[i] != 0x7f {
			i++
		}
		if i == 0 || i+1 >= len(tag) || tag[i] != ':' || tag[i+1] != '"' {
			return false
		}
		tag = tag[i+1:]
		i = 1
		for i < len(tag) && tag[i] != '"' {
			if tag[i] == '\\' {
				i++
			}
			i++
		}
		if i >= len(tag) {
			return false
		}
		if _, err := strconv.Unquote(tag[:i+1]); err != nil {
			return false
		}
		tag = tag[i+1:]
	}
	return true
}
