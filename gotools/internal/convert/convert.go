// Package convert rewrites legacy gin handlers to the n-api template shape,
// deterministically, without a model.
//
// # Why this exists
//
// A live /migration of pao-backend (2026-10-13) spent 216 model turns over six
// sessions and converted no handler method at all; with every harness fix in
// place a second run converted eight of 103 in fifteen minutes. The model was
// being asked to do something mechanical -- the same five edits to every
// method -- and it is bad at mechanical work at scale: it changed signatures
// and left gin bodies behind, re-read files it had just read, and sent the
// same reply word for word. Those five edits are exactly what `unit_check`
// tests for, so they are exactly what a program can do:
//
//   - (ctx *gin.Context) becomes (sctx *serverRoute.Context, req T) (*R, error);
//   - the ShouldBind* and ValidateStruct blocks go -- the server binds and
//     validates req before the method runs;
//   - apierrors.HandleXxxError(ctx, err); return becomes return nil, err, and
//     a hand-built error response becomes HandleErrorWithStatusCodeAndMessage
//     with the same status and message;
//   - handleSuccess(ctx, rsp) becomes return &rsp, nil;
//   - every other use of ctx becomes sctx.Ctx, and every repository method or
//     helper that only ever used its *gin.Context as a context takes a
//     context.Context instead.
//
// # One method at a time, all or nothing
//
// A method is converted only when every statement in it matches a pattern the
// conversion understands; otherwise it is left exactly as it was and the
// result says why. A half-rewritten method is worse than an untouched one --
// that is the failure the live runs kept producing -- and an untouched one
// with a reason is a small, specific job for the model or the developer.
//
// # Edits, not re-printing
//
// As in internal/gopatch: located with the AST, applied at byte offsets, so
// the swagger annotations above every handler survive and the file keeps its
// own line endings. The rewritten method is parsed on its own before it is
// accepted, and must end in a terminating statement, so a converted method
// is always valid Go.
package convert

import (
	"bytes"
	"fmt"
	"go/ast"
	"go/parser"
	"go/token"
	"os"
	"path/filepath"
	"sort"
	"strconv"
	"strings"

	"gitlab.cept.gov.in/it-2.0/dakcoder/gotools/internal/gopatch"
	"gitlab.cept.gov.in/it-2.0/dakcoder/gotools/internal/workspace"
)

// The import paths the conversion writes.
const (
	RoutePath      = "gitlab.cept.gov.in/it-2.0-common/n-api-server/route"
	RouteAlias     = "serverRoute"
	legacyErrors   = "gitlab.cept.gov.in/it-2.0-common/api-errors"
	templateErrors = "gitlab.cept.gov.in/it-2.0-common/n-api-errors"
	legacyLog      = "gitlab.cept.gov.in/it-2.0-common/api-log"
	templateLog    = "gitlab.cept.gov.in/it-2.0-common/n-api-log"
)

// Method is what happened to one handler method.
type Method struct {
	Name   string `json:"name"`
	Status string `json:"status"` // converted | left
	// Why it was left, one line each, written to be acted on.
	Reasons []string `json:"reasons,omitempty"`
	// The request and response types it now takes and returns.
	Request  string `json:"request,omitempty"`
	Response string `json:"response,omitempty"`
	// Created is set when the legacy method answered 201 (handleCreateSuccess):
	// the route that registers it must pass http.StatusCreated.
	Created bool `json:"created,omitempty"`
	// Notes are things a reviewer should know that did not stop the conversion.
	Notes []string `json:"notes,omitempty"`
	Start int      `json:"start"`
	End   int      `json:"end"`
}

// Helper is a non-route function whose *gin.Context parameter became a
// context.Context: repository methods and handler-package helpers.
type Helper struct {
	Name string `json:"name"`
	File string `json:"file"`
}

// FileResult is one file's outcome.
type FileResult struct {
	File      string   `json:"file"`
	Methods   []Method `json:"methods,omitempty"`
	Helpers   []string `json:"helpers,omitempty"`
	Changed   bool     `json:"changed"`
	Converted int      `json:"converted"`
	Left      int      `json:"left"`
	Error     string   `json:"error,omitempty"`
}

// Result is a whole run.
type Result struct {
	Files     []FileResult `json:"files"`
	Converted int          `json:"converted"`
	Left      int          `json:"left"`
	Helpers   int          `json:"helpers"`
	Written   []string     `json:"written,omitempty"`
	DryRun    bool         `json:"dry_run"`
}

// Options scopes a run.
type Options struct {
	// Paths are handler files to convert. Empty means every handler-layer file.
	// Repository methods and helpers are converted module-wide either way:
	// their change is a type swap that keeps every gin caller compiling.
	Paths []string
	// Methods narrows the handler conversion to these names.
	Methods []string
	// Write puts the result on disk; otherwise it is a dry run.
	Write bool
}

// Run converts what it can and reports the rest.
func Run(ws *workspace.Workspace, opts Options) (*Result, error) {
	files, endings := goFiles(ws)
	want := map[string]bool{}
	for _, p := range opts.Paths {
		want[filepath.ToSlash(strings.TrimPrefix(p, "./"))] = true
	}
	only := map[string]bool{}
	for _, m := range opts.Methods {
		only[strings.TrimSpace(m)] = true
	}

	helpers := contextOnly(files)
	res := &Result{DryRun: !opts.Write}

	for _, f := range files {
		handlerFile := f.Layer == workspace.LayerHandler && (len(want) == 0 || want[f.Rel])
		fileHelpers := helpers.inFile(f)
		if !handlerFile && len(fileHelpers) == 0 {
			continue
		}
		if handlerFile && len(want) > 0 && f.ParseErr != nil {
			res.Files = append(res.Files, FileResult{File: f.Rel, Error: "does not parse: " + f.ParseErr.Error()})
			continue
		}
		fr, out, err := convertFile(f, endings[f], handlerFile, only, helpers)
		if err != nil {
			fr.Error = err.Error()
		}
		if fr.Converted == 0 && fr.Left == 0 && len(fr.Helpers) == 0 && fr.Error == "" {
			continue
		}
		res.Converted += fr.Converted
		res.Left += fr.Left
		res.Helpers += len(fr.Helpers)
		if fr.Changed && opts.Write && err == nil {
			if werr := os.WriteFile(f.Abs, out, 0o644); werr != nil {
				fr.Error = "write: " + werr.Error()
			} else {
				res.Written = append(res.Written, f.Rel)
			}
		}
		res.Files = append(res.Files, fr)
	}
	sort.Slice(res.Files, func(i, j int) bool { return res.Files[i].File < res.Files[j].File })
	return res, nil
}

// goFiles is every hand-written Go file, with CRLF files re-read as LF so that
// every offset the conversion computes is into the bytes it edits. The
// original line ending is put back on write.
func goFiles(ws *workspace.Workspace) ([]*workspace.File, map[*workspace.File]gopatch.EOL) {
	var out []*workspace.File
	eol := map[*workspace.File]gopatch.EOL{}
	for _, f := range ws.Files {
		if f.AST == nil || f.Generated || strings.HasSuffix(f.Rel, "_test.go") {
			continue
		}
		if lf := gopatch.ToLF(f.Src); !bytes.Equal(lf, f.Src) {
			re, err := reparse(f.Rel, lf)
			if err != nil {
				continue
			}
			re.Abs, re.Layer, re.Package = f.Abs, f.Layer, f.Package
			eol[re] = gopatch.DetectEOL(f.Src)
			f = re
		}
		out = append(out, f)
	}
	return out, eol
}

// ── the gin context, and functions that only use it as a context ────────────

// ginParam is the name of fd's *gin.Context parameter and its type node, or "".
func ginParam(fd *ast.FuncDecl) (string, ast.Expr, int) {
	if fd.Type.Params == nil {
		return "", nil, -1
	}
	idx := 0
	for _, field := range fd.Type.Params.List {
		n := len(field.Names)
		if n == 0 {
			n = 1
		}
		if isGinContext(field.Type) {
			if len(field.Names) == 1 {
				return field.Names[0].Name, field.Type, idx
			}
			return "", field.Type, idx
		}
		idx += n
	}
	return "", nil, -1
}

func isGinContext(e ast.Expr) bool {
	st, ok := e.(*ast.StarExpr)
	if !ok {
		return false
	}
	sel, ok := st.X.(*ast.SelectorExpr)
	if !ok {
		return false
	}
	id, ok := sel.X.(*ast.Ident)
	return ok && id.Name == "gin" && sel.Sel.Name == "Context"
}

// isRouteHandler is what a legacy route dispatches to: an exported method on
// a *Handler type taking exactly the gin context and returning nothing.
func isRouteHandler(f *workspace.File, fd *ast.FuncDecl) bool {
	if f.Layer != workspace.LayerHandler || fd.Recv == nil || fd.Body == nil || !fd.Name.IsExported() {
		return false
	}
	if !strings.HasSuffix(recvName(fd), "Handler") || fd.Type.Results != nil {
		return false
	}
	return fd.Type.Params != nil && len(fd.Type.Params.List) == 1 &&
		len(fd.Type.Params.List[0].Names) == 1 && isGinContext(fd.Type.Params.List[0].Type)
}

func recvName(fd *ast.FuncDecl) string {
	if fd.Recv == nil || len(fd.Recv.List) == 0 {
		return ""
	}
	t := fd.Recv.List[0].Type
	if st, ok := t.(*ast.StarExpr); ok {
		t = st.X
	}
	if id, ok := t.(*ast.Ident); ok {
		return id.Name
	}
	return ""
}

// helperSet is every non-route function with a *gin.Context parameter, and
// whether that parameter is only ever used as a context.Context.
type helperSet struct {
	byName map[string][]*helper // by function name; methods and functions alike
	all    []*helper
}

type helper struct {
	file   *workspace.File
	decl   *ast.FuncDecl
	param  string
	typ    ast.Expr
	ok     bool
	reason string
	// reqCtx are the param.Request.Context() calls, which become param.
	reqCtx []*ast.CallExpr
}

func (h *helperSet) inFile(f *workspace.File) []*helper {
	var out []*helper
	for _, x := range h.all {
		if x.file == f && x.ok {
			out = append(out, x)
		}
	}
	return out
}

// calleeOK answers whether passing a context as argument to call is safe once
// the caller holds a context.Context instead of a *gin.Context.
func (h *helperSet) calleeOK(call *ast.CallExpr) (bool, string) {
	switch fn := call.Fun.(type) {
	case *ast.SelectorExpr:
		if id, ok := fn.X.(*ast.Ident); ok {
			switch id.Name {
			case "log":
				return true, ""
			case "apierrors", "gin", "validation":
				return false, id.Name + "." + fn.Sel.Name + " takes the gin context"
			}
		}
		return h.named(fn.Sel.Name)
	case *ast.Ident:
		return h.named(fn.Name)
	}
	return true, ""
}

func (h *helperSet) named(name string) (bool, string) {
	for _, x := range h.byName[name] {
		if !x.ok {
			return false, name + " still needs the gin context (" + x.reason + ")"
		}
	}
	return true, ""
}

func contextOnly(files []*workspace.File) *helperSet {
	h := &helperSet{byName: map[string][]*helper{}}
	for _, f := range files {
		for _, d := range f.AST.Decls {
			fd, ok := d.(*ast.FuncDecl)
			if !ok || fd.Body == nil || isRouteHandler(f, fd) {
				continue
			}
			name, typ, _ := ginParam(fd)
			if typ == nil {
				continue
			}
			x := &helper{file: f, decl: fd, param: name, typ: typ, ok: true}
			if name == "" {
				x.ok, x.reason = false, "its gin context parameter is unnamed or shared"
			}
			h.all = append(h.all, x)
			h.byName[fd.Name.Name] = append(h.byName[fd.Name.Name], x)
		}
	}
	// A fixed point: a helper stops qualifying when it uses its parameter as
	// anything but a context, or hands it to one that does.
	for changed := true; changed; {
		changed = false
		for _, x := range h.all {
			if !x.ok {
				continue
			}
			ok, why, reqCtx := h.usesAsContext(x.decl.Body, x.param)
			if !ok {
				x.ok, x.reason, changed = false, why, true
			}
			x.reqCtx = reqCtx
		}
	}
	return h
}

// usesAsContext reports whether every use of name in body is as a plain
// argument to a call that can take a context.Context.
func (h *helperSet) usesAsContext(body *ast.BlockStmt, name string) (bool, string, []*ast.CallExpr) {
	ok, why := true, ""
	var reqCtx []*ast.CallExpr
	parents := parentMap(body)
	ast.Inspect(body, func(n ast.Node) bool {
		if !ok {
			return false
		}
		id, isID := n.(*ast.Ident)
		if !isID || id.Name != name {
			return true
		}
		switch pp := parents[id].(type) {
		case *ast.SelectorExpr:
			if pp.X != id {
				return true
			}
			// gctx.Request.Context() -- every legacy repository opens with it
			// -- is the context itself once gctx is a context.Context.
			if call := requestContext(parents, pp); call != nil {
				reqCtx = append(reqCtx, call)
				return true
			}
			ok, why = false, "uses "+name+"."+pp.Sel.Name
		case *ast.CallExpr:
			for _, a := range pp.Args {
				if a == id {
					if good, w := h.calleeOK(pp); !good {
						ok, why = false, w
					}
					return true
				}
			}
			ok, why = false, "calls "+name+" as a function"
		case *ast.KeyValueExpr:
			if pp.Key == id {
				return true
			}
			ok, why = false, "stores "+name+" in a value"
		default:
			ok, why = false, "uses "+name+" other than as a call argument"
		}
		return true
	})
	return ok, why, reqCtx
}

// requestContext is the call when sel is the `x.Request` of x.Request.Context().
func requestContext(parents map[ast.Node]ast.Node, sel *ast.SelectorExpr) *ast.CallExpr {
	if sel.Sel.Name != "Request" {
		return nil
	}
	outer, ok := parents[sel].(*ast.SelectorExpr)
	if !ok || outer.Sel.Name != "Context" {
		return nil
	}
	call, ok := parents[outer].(*ast.CallExpr)
	if !ok || call.Fun != outer || len(call.Args) != 0 {
		return nil
	}
	return call
}

func parentMap(root ast.Node) map[ast.Node]ast.Node {
	parents := map[ast.Node]ast.Node{}
	var stack []ast.Node
	ast.Inspect(root, func(n ast.Node) bool {
		if n == nil {
			stack = stack[:len(stack)-1]
			return false
		}
		if len(stack) > 0 {
			parents[n] = stack[len(stack)-1]
		}
		stack = append(stack, n)
		return true
	})
	return parents
}

// ── per file ─────────────────────────────────────────────────────────────────

type edit struct {
	lo, hi int
	text   string
}

func convertFile(f *workspace.File, ending gopatch.EOL, handlers bool, only map[string]bool, helpers *helperSet) (FileResult, []byte, error) {
	fr := FileResult{File: f.Rel}
	src := f.Src
	var edits []edit
	var extra []string
	usedNames := declaredNames(f)
	convertedHandler := false

	if handlers {
		for _, d := range f.AST.Decls {
			fd, ok := d.(*ast.FuncDecl)
			if !ok || !isRouteHandler(f, fd) {
				continue
			}
			if len(only) > 0 && !only[fd.Name.Name] {
				continue
			}
			m, es, decl := convertMethod(f, fd, helpers, usedNames)
			if m.Status == "converted" {
				edits = append(edits, es...)
				if decl != "" {
					extra = append(extra, decl)
				}
				fr.Converted++
				convertedHandler = true
			} else {
				fr.Left++
			}
			fr.Methods = append(fr.Methods, m)
		}
	}
	convertedHelper := false
	for _, x := range helpers.inFile(f) {
		lo, hi := off(f, x.typ.Pos()), off(f, x.typ.End())
		edits = append(edits, edit{lo, hi, "context.Context"})
		for _, call := range x.reqCtx {
			edits = append(edits, edit{off(f, call.Pos()), off(f, call.End()), x.param})
		}
		label := x.decl.Name.Name
		if r := recvName(x.decl); r != "" {
			label = r + "." + label
		}
		fr.Helpers = append(fr.Helpers, label)
		convertedHelper = true
	}
	if len(edits) == 0 {
		return fr, nil, nil
	}

	out, err := apply(src, edits)
	if err != nil {
		return fr, nil, err
	}
	if len(extra) > 0 {
		out = append(bytes.TrimRight(out, "\n"), []byte("\n\n"+strings.Join(extra, "\n\n")+"\n")...)
	}
	out, err = fixImports(out, f.Rel, convertedHandler, convertedHelper)
	if err != nil {
		return fr, nil, err
	}
	formatted, err := gopatch.Format(out)
	if err != nil {
		return fr, nil, fmt.Errorf("the converted file does not format: %w", err)
	}
	if ending == "" {
		ending = gopatch.LF
	}
	formatted = gopatch.ApplyEOL(formatted, ending)
	fr.Changed = !bytes.Equal(gopatch.ToLF(formatted), f.Src)
	return fr, formatted, nil
}

func reparse(rel string, src []byte) (*workspace.File, error) {
	fset := token.NewFileSet()
	af, err := parser.ParseFile(fset, rel, src, parser.ParseComments)
	if err != nil {
		return nil, err
	}
	return &workspace.File{Rel: rel, Src: src, FSet: fset, AST: af, Layer: layerOf(rel)}, nil
}

func layerOf(rel string) workspace.Layer {
	switch {
	case strings.HasPrefix(rel, "handler/response/"):
		return workspace.LayerResponse
	case strings.HasPrefix(rel, "handler/"):
		return workspace.LayerHandler
	}
	return workspace.LayerOther
}

func declaredNames(f *workspace.File) map[string]bool {
	out := map[string]bool{}
	for _, d := range f.AST.Decls {
		switch x := d.(type) {
		case *ast.GenDecl:
			for _, s := range x.Specs {
				if ts, ok := s.(*ast.TypeSpec); ok {
					out[ts.Name.Name] = true
				}
			}
		case *ast.FuncDecl:
			if x.Recv == nil {
				out[x.Name.Name] = true
			}
		}
	}
	return out
}

func off(f *workspace.File, p token.Pos) int { return f.FSet.Position(p).Offset }

func apply(src []byte, edits []edit) ([]byte, error) {
	sort.Slice(edits, func(i, j int) bool {
		if edits[i].lo != edits[j].lo {
			return edits[i].lo > edits[j].lo
		}
		return edits[i].hi > edits[j].hi
	})
	out := append([]byte(nil), src...)
	last := len(src) + 1
	for _, e := range edits {
		if e.hi > last || e.lo > e.hi {
			return nil, fmt.Errorf("internal: overlapping edits at %d", e.lo)
		}
		out = append(out[:e.lo], append([]byte(e.text), out[e.hi:]...)...)
		last = e.lo
	}
	return out, nil
}

// ── imports ──────────────────────────────────────────────────────────────────

func fixImports(src []byte, rel string, handler, helper bool) ([]byte, error) {
	var err error
	if handler {
		for _, swap := range [][2]string{{legacyErrors, templateErrors}, {legacyLog, templateLog}} {
			src = bytes.ReplaceAll(src, []byte(strconv.Quote(swap[0])), []byte(strconv.Quote(swap[1])))
		}
		if src, err = intErrorCodes(src, rel); err != nil {
			return nil, err
		}
		if bytes.Contains(src, []byte("fmt.Sprint(")) {
			if src, _, err = gopatch.EnsureImport(src, "", "fmt", ""); err != nil {
				return nil, err
			}
		}
		if src, _, err = gopatch.EnsureImport(src, RouteAlias, RoutePath, ""); err != nil {
			return nil, err
		}
	}
	if helper {
		if src, _, err = gopatch.EnsureImport(src, "", "context", ""); err != nil {
			return nil, err
		}
	}
	return dropUnused(src, rel)
}

// intErrorCodes rewrites apierrors.NewAppError(msg, "409", err) to take 409.
// The swap to n-api-errors changed that parameter from string to int, and the
// file's unconverted methods still build their errors the legacy way; this is
// what keeps them compiling until they are converted too.
func intErrorCodes(src []byte, rel string) ([]byte, error) {
	fset := token.NewFileSet()
	af, err := parser.ParseFile(fset, rel, src, parser.ParseComments)
	if err != nil {
		return nil, fmt.Errorf("the converted file does not parse: %w", err)
	}
	var edits []edit
	ast.Inspect(af, func(n ast.Node) bool {
		call, ok := n.(*ast.CallExpr)
		if !ok || len(call.Args) < 3 {
			return true
		}
		sel, ok := call.Fun.(*ast.SelectorExpr)
		if !ok || (sel.Sel.Name != "NewAppError" && sel.Sel.Name != "NewAppErrorWithId") {
			return true
		}
		if pkg, ok := sel.X.(*ast.Ident); !ok || pkg.Name != "apierrors" {
			return true
		}
		lit, ok := call.Args[1].(*ast.BasicLit)
		if !ok || lit.Kind != token.STRING {
			return true
		}
		code, err := strconv.Unquote(lit.Value)
		if err != nil {
			return true
		}
		if _, err := strconv.Atoi(code); err != nil {
			return true
		}
		edits = append(edits, edit{fset.Position(lit.Pos()).Offset, fset.Position(lit.End()).Offset, code})
		return true
	})
	if len(edits) == 0 {
		return src, nil
	}
	return apply(src, edits)
}

// dropUnused removes imports nothing references any more. The file compiled
// before the conversion, so an unused import now is one the conversion
// orphaned -- gin, the legacy validator -- and removing it is the fix.
func dropUnused(src []byte, rel string) ([]byte, error) {
	fset := token.NewFileSet()
	af, err := parser.ParseFile(fset, rel, src, parser.ParseComments)
	if err != nil {
		return nil, fmt.Errorf("the converted file does not parse: %w", err)
	}
	used := map[string]bool{}
	ast.Inspect(af, func(n ast.Node) bool {
		if sel, ok := n.(*ast.SelectorExpr); ok {
			if id, ok := sel.X.(*ast.Ident); ok {
				used[id.Name] = true
			}
		}
		return true
	})
	var drop []edit
	for _, im := range af.Imports {
		path, _ := strconv.Unquote(im.Path.Value)
		name := importName(im)
		if !orphanable[path] || name == "_" || name == "." || used[name] {
			continue
		}
		lo := fset.Position(im.Pos()).Offset
		if im.Doc != nil {
			lo = fset.Position(im.Doc.Pos()).Offset
		}
		hi := fset.Position(im.End()).Offset
		if im.Comment != nil {
			hi = fset.Position(im.Comment.End()).Offset
		}
		drop = append(drop, edit{lo, hi, ""})
	}
	if len(drop) == 0 {
		return src, nil
	}
	return apply(src, drop)
}

// orphanable are the imports a conversion can leave unused. Nothing else is
// touched: a package's name is not always its path's last element, and
// guessing it wrong would delete an import the file needs.
var orphanable = map[string]bool{
	"github.com/gin-gonic/gin": true, "net/http": true, "errors": true, "context": true,
	"gitlab.cept.gov.in/it-2.0-common/api-validation": true,
	legacyErrors: true, templateErrors: true, legacyLog: true, templateLog: true,
}

func importName(im *ast.ImportSpec) string {
	if im.Name != nil {
		return im.Name.Name
	}
	path, _ := strconv.Unquote(im.Path.Value)
	base := path[strings.LastIndex(path, "/")+1:]
	// A version suffix (pgx/v5) is not the package name.
	if len(base) > 1 && base[0] == 'v' && strings.Trim(base[1:], "0123456789") == "" {
		trimmed := strings.TrimSuffix(path, "/"+base)
		base = trimmed[strings.LastIndex(trimmed, "/")+1:]
	}
	return strings.ReplaceAll(base, "-", "")
}
