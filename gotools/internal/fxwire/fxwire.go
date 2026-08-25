// Package fxwire registers constructors in bootstrap/bootstrapper.go.
//
// # Why this is a tool and not a prompt instruction
//
// Uber-FX registration is the single most common way a correct resource fails
// to work. A handler that is missing from FxHandler compiles perfectly and then
// fails at start-up with "missing dependencies for function" — an error that
// names a type, not a file, and gives no hint that the fix is three lines in a
// file the developer was not editing.
//
// Worse is the near-miss. A handler registered with a plain fx.Provide instead
// of the annotated form compiles, starts, and silently serves nothing: the
// server collects handlers by group tag, so an untagged provider is simply
// never collected. There is no error at all. The two registrations are not
// interchangeable and the difference is invisible:
//
//	repositories:  fx.Provide(repo.NewXRepository)
//	handlers:      fx.Provide(fx.Annotate(handler.NewXHandler,
//	                   fx.As(new(serverHandler.Handler)),
//	                   fx.ResultTags(serverHandler.ServerControllersGroupTag)))
//
// The pre-implementation spike found the model gets this right when it is
// asked directly. That is not the same as getting it right on turn 23 of a
// long task, and it is exactly the kind of fixed, mechanical edit that should
// never be spent model tokens on.
//
// # Insertion, not re-printing
//
// Edits are byte-level at AST-located offsets (see internal/gopatch): comments
// in bootstrapper.go survive, and the diff a developer approves shows three
// added lines rather than a whole rewritten file.
package fxwire

import (
	"bytes"
	"fmt"
	"go/ast"
	"go/parser"
	"go/token"
	"os"
	"path/filepath"
	"sort"
	"strings"

	"gitlab.cept.gov.in/it-2.0/dakcoder/gotools/internal/gopatch"
)

// Kind selects which fx module a constructor belongs in.
type Kind string

const (
	// KindRepo registers a repository: a plain fx.Provide entry in FxRepo.
	KindRepo Kind = "repo"
	// KindHandler registers a handler: an annotated entry in FxHandler.
	KindHandler Kind = "handler"
)

// Module names and the annotation shape, from SOP.md §bootstrap/bootstrapper.go.
const (
	repoModuleVar    = "FxRepo"
	handlerModuleVar = "FxHandler"
	handlerInterface = "Handler"
	groupTagConst    = "ServerControllersGroupTag"
)

// Known import paths, so the inserted code uses the file's own aliases rather
// than assuming the reference template's.
const (
	pathHandlerPkgSuffix = "/handler"
	pathRepoPkgSuffix    = "/repo/postgres"
)

var serverHandlerPaths = []string{
	"gitlab.cept.gov.in/it-2.0-common/n-api-server/handler",
	"gitlab.cept.gov.in/it-2.0-common/api-server/handler", // legacy generation
}

// Registration is one constructor to wire in.
type Registration struct {
	Kind Kind   `json:"kind" jsonschema:"repo for a repository constructor, handler for a handler constructor"`
	Ctor string `json:"ctor" jsonschema:"the constructor's bare name, e.g. NewPensionHandler"`
}

// Result describes the outcome of a wiring run.
type Result struct {
	// Path is the bootstrap file, workspace-relative.
	Path string `json:"path"`
	// Content is the full patched file. Empty when nothing changed.
	Content string `json:"content,omitempty"`
	// Added lists the constructors newly registered.
	Added []string `json:"added,omitempty"`
	// AlreadyRegistered lists the constructors that were already present, so a
	// re-run is a no-op that says so rather than a duplicate entry.
	AlreadyRegistered []string `json:"already_registered,omitempty"`
	// Changed reports whether the file content differs from what was on disk.
	Changed bool `json:"changed"`
}

// ctorRe-style validation: the constructor name is interpolated into source, so
// it has to be an identifier and nothing else.
func validCtor(name string) bool {
	if name == "" || !strings.HasPrefix(name, "New") {
		return false
	}
	for i, r := range name {
		switch {
		case r >= 'a' && r <= 'z', r >= 'A' && r <= 'Z', r == '_':
		case r >= '0' && r <= '9' && i > 0:
		default:
			return false
		}
	}
	return true
}

// Plan computes the patched bootstrap file without writing it.
func Plan(root string, modulePath string, regs ...Registration) (*Result, error) {
	for _, r := range regs {
		if r.Kind != KindRepo && r.Kind != KindHandler {
			return nil, fmt.Errorf("unknown kind %q: use %q or %q", r.Kind, KindRepo, KindHandler)
		}
		if !validCtor(r.Ctor) {
			return nil, fmt.Errorf("constructor %q is not a valid New* identifier", r.Ctor)
		}
	}

	rel, abs, err := findBootstrapFile(root)
	if err != nil {
		return nil, err
	}
	original, err := os.ReadFile(abs)
	if err != nil {
		return nil, fmt.Errorf("read %s: %w", rel, err)
	}

	res := &Result{Path: rel}
	src := original

	// Applied one at a time and re-parsed each round: offsets from an earlier
	// parse are invalid the moment bytes are inserted, and quietly reusing them
	// is how a second insertion lands in the middle of the first.
	for _, reg := range regs {
		patched, added, werr := wireOne(src, modulePath, reg)
		if werr != nil {
			return nil, fmt.Errorf("%s: %w", rel, werr)
		}
		if !added {
			res.AlreadyRegistered = append(res.AlreadyRegistered, reg.Ctor)
			continue
		}
		res.Added = append(res.Added, reg.Ctor)
		src = patched
	}

	res.Changed = !bytes.Equal(src, original)
	if res.Changed {
		res.Content = string(src)
	}
	return res, nil
}

// Apply computes and writes the patched bootstrap file.
func Apply(root, modulePath string, regs ...Registration) (*Result, error) {
	res, err := Plan(root, modulePath, regs...)
	if err != nil {
		return nil, err
	}
	if !res.Changed {
		return res, nil
	}
	abs := filepath.Join(root, filepath.FromSlash(res.Path))
	if err := os.WriteFile(abs, []byte(res.Content), 0o644); err != nil {
		return nil, fmt.Errorf("write %s: %w", res.Path, err)
	}
	return res, nil
}

// findBootstrapFile locates the file declaring the fx modules.
//
// bootstrap/bootstrapper.go is the template's name and is tried first, but a
// service that split its modules across files is still a valid target, so the
// directory is scanned before giving up.
func findBootstrapFile(root string) (rel, abs string, err error) {
	preferred := filepath.Join(root, "bootstrap", "bootstrapper.go")
	if _, statErr := os.Stat(preferred); statErr == nil {
		return "bootstrap/bootstrapper.go", preferred, nil
	}

	dir := filepath.Join(root, "bootstrap")
	entries, readErr := os.ReadDir(dir)
	if readErr != nil {
		return "", "", fmt.Errorf(
			"no bootstrap/ directory under %s; fx_wire needs bootstrap/bootstrapper.go to edit", root)
	}
	var names []string
	for _, e := range entries {
		if !e.IsDir() && strings.HasSuffix(e.Name(), ".go") && !strings.HasSuffix(e.Name(), "_test.go") {
			names = append(names, e.Name())
		}
	}
	sort.Strings(names)
	for _, name := range names {
		p := filepath.Join(dir, name)
		b, rerr := os.ReadFile(p)
		if rerr != nil {
			continue
		}
		if bytes.Contains(b, []byte(repoModuleVar)) || bytes.Contains(b, []byte(handlerModuleVar)) {
			return "bootstrap/" + name, p, nil
		}
	}
	return "", "", fmt.Errorf(
		"no file under bootstrap/ declares %s or %s; fx_wire has nothing to edit",
		repoModuleVar, handlerModuleVar)
}

// wireOne inserts a single registration, returning the patched source and
// whether anything was added.
func wireOne(src []byte, modulePath string, reg Registration) ([]byte, bool, error) {
	eol := gopatch.DetectEOL(src)
	body := gopatch.ToLF(src)

	fset := token.NewFileSet()
	file, err := parser.ParseFile(fset, "bootstrapper.go", body, parser.ParseComments)
	if err != nil {
		return nil, false, fmt.Errorf("parse: %w", err)
	}

	moduleVar := repoModuleVar
	if reg.Kind == KindHandler {
		moduleVar = handlerModuleVar
	}

	provide, err := findProvide(file, moduleVar)
	if err != nil {
		return nil, false, err
	}
	if registered(provide, reg.Ctor) {
		return src, false, nil
	}

	// Make sure the packages the inserted code names are imported. Imports are
	// added first, then the entry, because adding an import shifts every offset
	// after it and re-deriving them is cheaper than adjusting them.
	patched, err := ensureImports(body, modulePath, reg.Kind)
	if err != nil {
		return nil, false, err
	}
	if !bytes.Equal(patched, body) {
		body = patched
		fset = token.NewFileSet()
		file, err = parser.ParseFile(fset, "bootstrapper.go", body, parser.ParseComments)
		if err != nil {
			return nil, false, fmt.Errorf("parse after import insertion: %w", err)
		}
		provide, err = findProvide(file, moduleVar)
		if err != nil {
			return nil, false, err
		}
	}

	pkgAlias := packageAlias(file, modulePath, reg.Kind)
	entry := qualify(pkgAlias, reg.Ctor)
	if reg.Kind == KindHandler {
		entry = handlerEntry(pkgAlias, serverHandlerAlias(file), reg.Ctor)
	}

	out, err := insertArg(fset, body, provide, entry)
	if err != nil {
		return nil, false, err
	}
	return gopatch.ApplyEOL(out, eol), true, nil
}

// findProvide locates the fx.Provide call inside the named fx.Module variable.
func findProvide(file *ast.File, moduleVar string) (*ast.CallExpr, error) {
	var moduleCall *ast.CallExpr
	for _, d := range file.Decls {
		gd, ok := d.(*ast.GenDecl)
		if !ok || gd.Tok != token.VAR {
			continue
		}
		for _, s := range gd.Specs {
			vs, ok := s.(*ast.ValueSpec)
			if !ok {
				continue
			}
			for i, name := range vs.Names {
				if name.Name != moduleVar || i >= len(vs.Values) {
					continue
				}
				if call, ok := vs.Values[i].(*ast.CallExpr); ok {
					moduleCall = call
				}
			}
		}
	}
	if moduleCall == nil {
		return nil, fmt.Errorf(
			"no `var %s = fx.Module(...)` declaration found; add one before wiring into it", moduleVar)
	}

	var provide *ast.CallExpr
	ast.Inspect(moduleCall, func(n ast.Node) bool {
		if provide != nil {
			return false
		}
		call, ok := n.(*ast.CallExpr)
		if ok && callName(call) == "fx.Provide" {
			provide = call
			return false
		}
		return true
	})
	if provide == nil {
		return nil, fmt.Errorf("%s contains no fx.Provide(...) to add to", moduleVar)
	}
	return provide, nil
}

// registered reports whether a constructor already appears in the provide list,
// under any package alias.
func registered(provide *ast.CallExpr, ctor string) bool {
	found := false
	ast.Inspect(provide, func(n ast.Node) bool {
		if found {
			return false
		}
		switch t := n.(type) {
		case *ast.SelectorExpr:
			if t.Sel.Name == ctor {
				found = true
			}
		case *ast.Ident:
			if t.Name == ctor {
				found = true
			}
		}
		return true
	})
	return found
}

// insertArg adds an argument to a call's list, preserving every existing byte.
func insertArg(fset *token.FileSet, body []byte, call *ast.CallExpr, entry string) ([]byte, error) {
	lparenLine := fset.Position(call.Lparen).Line
	callIndent := indentOf(body, fset.Position(call.Pos()).Offset)
	argIndent := callIndent + "\t"

	var out []byte
	if len(call.Args) > 0 && fset.Position(call.Args[len(call.Args)-1].End()).Line > lparenLine {
		// Multi-line form: append a line after the last argument. Go requires a
		// trailing comma before a newline-separated `)`, so one is already
		// there and the new line simply follows.
		last := call.Args[len(call.Args)-1]
		at := lineEnd(body, fset.Position(last.End()).Offset)
		out = splice(body, at, at, indentBlock(entry, argIndent)+",\n")
	} else {
		// Single-line form, or an empty list: break it open so the result is
		// the shape the template uses and stays readable as it grows. This path
		// reformats the whole file, because a hand-written single-line
		// fx.Provide has no indentation to inherit — but it is not the shape
		// the template ships, so the common case keeps its minimal diff.
		at := fset.Position(call.Rparen).Offset
		prefix := "\n"
		if len(call.Args) > 0 {
			prefix = ",\n"
		}
		out = splice(body, at, at, prefix+indentBlock(entry, argIndent)+",\n"+callIndent)
		formatted, err := gopatch.Format(out)
		if err != nil {
			return nil, err
		}
		out = formatted
	}

	fs := token.NewFileSet()
	if _, err := parser.ParseFile(fs, "patched.go", out, parser.ParseComments); err != nil {
		return nil, fmt.Errorf("patched bootstrap file does not parse: %w", err)
	}
	return out, nil
}

// handlerEntry is the annotated provider. The three parts are not optional and
// not interchangeable — see the package comment.
func handlerEntry(pkgAlias, serverAlias, ctor string) string {
	return "fx.Annotate(\n" +
		"\t" + qualify(pkgAlias, ctor) + ",\n" +
		"\tfx.As(new(" + qualify(serverAlias, handlerInterface) + ")),\n" +
		"\tfx.ResultTags(" + qualify(serverAlias, groupTagConst) + "),\n" +
		")"
}

func qualify(alias, name string) string {
	if alias == "" {
		return name
	}
	return alias + "." + name
}

// indentBlock prefixes every line of a multi-line entry with the base indent.
func indentBlock(entry, indent string) string {
	lines := strings.Split(entry, "\n")
	for i, l := range lines {
		lines[i] = indent + l
	}
	return strings.Join(lines, "\n")
}

// packageAlias returns the identifier the bootstrap file uses for the handler
// or repository package, falling back to the conventional name.
func packageAlias(file *ast.File, modulePath string, kind Kind) string {
	suffix, fallback := pathHandlerPkgSuffix, "handler"
	if kind == KindRepo {
		suffix, fallback = pathRepoPkgSuffix, "repo"
	}
	want := modulePath + suffix
	for _, im := range file.Imports {
		p := strings.Trim(im.Path.Value, `"`)
		if p != want && !(modulePath == "" && strings.HasSuffix(p, suffix)) {
			continue
		}
		if im.Name != nil {
			return im.Name.Name
		}
		return fallback
	}
	return fallback
}

// serverHandlerAlias returns the identifier the bootstrap file uses for the
// server handler package.
func serverHandlerAlias(file *ast.File) string {
	for _, im := range file.Imports {
		p := strings.Trim(im.Path.Value, `"`)
		for _, known := range serverHandlerPaths {
			if p == known {
				if im.Name != nil {
					return im.Name.Name
				}
				return "handler"
			}
		}
	}
	return "serverHandler"
}

// ensureImports adds whatever the inserted entry names, if it is missing.
func ensureImports(body []byte, modulePath string, kind Kind) ([]byte, error) {
	type need struct{ alias, path string }
	var needs []need

	if modulePath != "" {
		if kind == KindHandler {
			needs = append(needs,
				need{"handler", modulePath + pathHandlerPkgSuffix},
				need{"", "go.uber.org/fx"},
				need{"serverHandler", serverHandlerPaths[0]},
			)
		} else {
			needs = append(needs,
				need{"repo", modulePath + pathRepoPkgSuffix},
				need{"", "go.uber.org/fx"},
			)
		}
	}

	out := body
	for _, n := range needs {
		patched, _, err := gopatch.EnsureImport(out, n.alias, n.path, modulePath)
		if err != nil {
			return nil, err
		}
		out = patched
	}
	return out, nil
}

// ── small helpers ───────────────────────────────────────────────────────────

func callName(c *ast.CallExpr) string {
	switch f := c.Fun.(type) {
	case *ast.Ident:
		return f.Name
	case *ast.SelectorExpr:
		if id, ok := f.X.(*ast.Ident); ok {
			return id.Name + "." + f.Sel.Name
		}
	}
	return ""
}

func indentOf(body []byte, off int) string {
	start := lineStart(body, off)
	i := start
	for i < len(body) && (body[i] == '\t' || body[i] == ' ') {
		i++
	}
	return string(body[start:i])
}

func lineStart(body []byte, off int) int {
	if off > len(body) {
		off = len(body)
	}
	if i := bytes.LastIndexByte(body[:off], '\n'); i >= 0 {
		return i + 1
	}
	return 0
}

func lineEnd(body []byte, off int) int {
	if off > len(body) {
		return len(body)
	}
	if i := bytes.IndexByte(body[off:], '\n'); i >= 0 {
		return off + i + 1
	}
	return len(body)
}

func splice(body []byte, lo, hi int, with string) []byte {
	out := make([]byte, 0, len(body)+len(with))
	out = append(out, body[:lo]...)
	out = append(out, with...)
	return append(out, body[hi:]...)
}
