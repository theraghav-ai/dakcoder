// Package workspace loads a Go workspace once, so every rule can iterate it
// cheaply instead of re-walking and re-parsing the tree.
//
// # Why syntax-only, and not go/packages
//
// Loading type information (golang.org/x/tools/go/packages with NeedTypes)
// would make a handful of rules more precise. We deliberately do not do it in
// this tier, for one decisive reason: rules_lint has to work on code that does
// not compile.
//
// The agent runs the linter after every edit batch, and in the Debugger loop it
// runs specifically because the build is broken. A type-aware loader returns
// nothing useful in exactly the situation where the agent most needs an answer.
// Syntax-only parsing degrades gracefully: a file with a type error still
// parses, so its handler signatures, struct tags and imports are all still
// checkable.
//
// It is also two orders of magnitude faster. A full go/packages load has to
// resolve the private gitlab.cept.gov.in modules; a cold build of the reference
// template takes ~2m30s, and even warm it is seconds. Parsing the same tree
// takes ~50ms.
//
// Rules that genuinely need types (e.g. proving a request DTO field's type
// matches its domain counterpart) belong in a separate opt-in tier that runs at
// the verification gate, not in the inner loop. See README.md §Design notes.
package workspace

import (
	"fmt"
	"go/ast"
	"go/parser"
	"go/token"
	"io/fs"
	"os"
	"path/filepath"
	"regexp"
	"strings"
	"sync"
)

// Layer is the architectural layer a file belongs to, derived from its path.
//
// Every layer-boundary rule asks this question, so it is answered exactly once,
// here, rather than being re-derived with ad-hoc strings.HasPrefix calls
// scattered across rules (which is how boundary checks drift out of agreement).
type Layer int

const (
	LayerOther Layer = iota
	LayerDomain
	LayerPort
	LayerRepo
	LayerHandler  // handler/*.go, excluding handler/response
	LayerResponse // handler/response/*.go
	LayerBootstrap
	LayerMain
	LayerRoutes // legacy: a routes/ package should not exist
	LayerTest
)

var layerNames = map[Layer]string{
	LayerOther: "other", LayerDomain: "domain", LayerPort: "port",
	LayerRepo: "repo", LayerHandler: "handler", LayerResponse: "response",
	LayerBootstrap: "bootstrap", LayerMain: "main", LayerRoutes: "routes", LayerTest: "test",
}

// String renders a layer as it appears in violation messages.
func (l Layer) String() string { return layerNames[l] }

// File is one parsed Go source file.
type File struct {
	Rel       string // workspace-relative, always forward slashes
	Abs       string
	Src       []byte
	FSet      *token.FileSet
	AST       *ast.File
	Package   string
	Layer     Layer
	Generated bool // carries the "Code generated ... DO NOT EDIT." marker
	ParseErr  error

	imports map[string]string // path -> local alias ("" when unaliased)
	once    sync.Once
}

// Position resolves a token position to a 1-indexed line and column.
func (f *File) Position(p token.Pos) (line, col int) {
	if f.FSet == nil || !p.IsValid() {
		return 0, 0
	}
	pos := f.FSet.Position(p)
	return pos.Line, pos.Column
}

// Imports maps import path to local name; the local name is "" when the import
// is unaliased.
func (f *File) Imports() map[string]string {
	f.once.Do(func() {
		f.imports = make(map[string]string, len(f.AST.Imports))
		for _, im := range f.AST.Imports {
			path := strings.Trim(im.Path.Value, `"`)
			alias := ""
			if im.Name != nil {
				alias = im.Name.Name
			}
			f.imports[path] = alias
		}
	})
	return f.imports
}

// ImportsAny reports whether the file imports any path containing one of the
// given substrings, and returns the first match with its AST node so a rule can
// report an accurate position.
func (f *File) ImportsAny(substrings ...string) (path string, spec *ast.ImportSpec, ok bool) {
	for _, im := range f.AST.Imports {
		p := strings.Trim(im.Path.Value, `"`)
		for _, s := range substrings {
			if strings.Contains(p, s) {
				return p, im, true
			}
		}
	}
	return "", nil, false
}

// Text returns the source text spanned by a node. Used where an AST match is
// awkward but an exact substring check on a bounded region is precise enough.
func (f *File) Text(n ast.Node) string {
	if n == nil || f.FSet == nil {
		return ""
	}
	lo := f.FSet.Position(n.Pos()).Offset
	hi := f.FSet.Position(n.End()).Offset
	if lo < 0 || hi > len(f.Src) || lo > hi {
		return ""
	}
	return string(f.Src[lo:hi])
}

// Workspace is a parsed Go module.
type Workspace struct {
	Root       string
	ModulePath string // from go.mod; "" when there is no go.mod
	GoVersion  string
	Files      []*File
	Requires   []Require // direct requires only

	byRel     map[string]*File
	configs   []*ConfigFile
	readCount int
}

// ReadCount is the number of files read from disk during the load.
//
// Exported so a regression test can assert the property finding S3 is about:
// the frontend agent's repo_map read every file twice — once for a preview and
// once to count its lines — which doubled the I/O on the single most expensive
// tool in the catalogue. Everything downstream here consumes the parsed
// workspace, so this count must equal the number of files considered, and must
// not move when a tool runs over an already-loaded workspace.
func (w *Workspace) ReadCount() int { return w.readCount }

// Require is one direct dependency from go.mod.
type Require struct {
	Path    string
	Version string
	Line    int
}

// Directories we never descend into.
//
// Pruning happens during the walk (fs.SkipDir), never after: a post-hoc filter
// still stats every file in vendor/ or node_modules/, which on Windows means
// the antivirus filter driver intercepts each one. Measured on a comparable
// tree, the post-hoc approach cost 1.6s to keep 200 files out of 16,680.
var pruneDirs = map[string]bool{
	".git": true, "vendor": true, "node_modules": true, ".venv": true,
	"__pycache__": true, "bin": true, "dist": true, "build": true,
	".dakcoder": true, ".claude": true, ".idea": true, ".vscode": true,
	"testdata": true,
}

// generatedRe is the Go convention for generated files (see `go help generate`).
// We detect by marker rather than by filename: `request_x_validator.go` happens
// to be recognisable by name today, but the marker is what the toolchain and
// every other linter honour.
var generatedRe = regexp.MustCompile(`^// Code generated .* DO NOT EDIT\.$`)

// Option configures a Load.
type Option func(*loadOpts)

type loadOpts struct {
	includeTests bool
	includeGen   bool
	extraPrune   []string
}

// WithTests includes _test.go files. Off by default: test files legitimately
// use dependencies and patterns the production rules forbid.
func WithTests() Option { return func(o *loadOpts) { o.includeTests = true } }

// WithGenerated includes generated files. Off by default: the agent must
// regenerate them, never hand-edit them, so linting them produces noise the
// agent cannot act on.
func WithGenerated() Option { return func(o *loadOpts) { o.includeGen = true } }

// WithExtraPrune adds directory names to skip.
func WithExtraPrune(dirs ...string) Option {
	return func(o *loadOpts) { o.extraPrune = append(o.extraPrune, dirs...) }
}

// Load parses every Go file under root.
//
// Parse errors are recorded on the File rather than aborting the load — see the
// package comment: linting broken code is a first-class use case.
func Load(root string, opts ...Option) (*Workspace, error) {
	o := &loadOpts{}
	for _, fn := range opts {
		fn(o)
	}
	extra := make(map[string]bool, len(o.extraPrune))
	for _, d := range o.extraPrune {
		extra[d] = true
	}

	abs, err := filepath.Abs(root)
	if err != nil {
		return nil, fmt.Errorf("resolve root: %w", err)
	}
	info, err := os.Stat(abs)
	if err != nil {
		return nil, fmt.Errorf("stat root: %w", err)
	}
	if !info.IsDir() {
		return nil, fmt.Errorf("root is not a directory: %s", abs)
	}

	ws := &Workspace{Root: abs, byRel: map[string]*File{}}
	if err := ws.loadGoMod(); err != nil {
		return nil, err
	}

	walkErr := filepath.WalkDir(abs, func(p string, d fs.DirEntry, err error) error {
		if err != nil {
			// An unreadable directory must not fail the whole lint.
			if d != nil && d.IsDir() {
				return fs.SkipDir
			}
			return nil
		}
		if d.IsDir() {
			name := d.Name()
			if p != abs && (pruneDirs[name] || extra[name] || strings.HasPrefix(name, ".")) {
				return fs.SkipDir
			}
			return nil
		}
		if !strings.HasSuffix(p, ".go") {
			return nil
		}
		isTest := strings.HasSuffix(p, "_test.go")
		if isTest && !o.includeTests {
			return nil
		}

		rel, rerr := filepath.Rel(abs, p)
		if rerr != nil {
			return nil
		}
		rel = filepath.ToSlash(rel)

		src, rerr := os.ReadFile(p)
		if rerr != nil {
			return nil
		}
		ws.readCount++
		fset := token.NewFileSet()
		// ParseComments is required: the generated-file marker is a comment, and
		// swagger annotations live in comments too.
		af, perr := parser.ParseFile(fset, p, src, parser.ParseComments|parser.SkipObjectResolution)
		if af == nil {
			return nil // unrecoverable; nothing to check
		}

		f := &File{
			Rel: rel, Abs: p, Src: src, FSet: fset, AST: af,
			Package: af.Name.Name, ParseErr: perr,
			Generated: isGenerated(af),
			Layer:     classify(rel, isTest),
		}
		if f.Generated && !o.includeGen {
			return nil
		}
		ws.Files = append(ws.Files, f)
		ws.byRel[rel] = f
		return nil
	})
	if walkErr != nil {
		return nil, fmt.Errorf("walk %s: %w", abs, walkErr)
	}
	ws.loadConfigs()
	return ws, nil
}

func isGenerated(f *ast.File) bool {
	for _, cg := range f.Comments {
		for _, c := range cg.List {
			if generatedRe.MatchString(c.Text) {
				return true
			}
		}
	}
	return false
}

// classify maps a workspace-relative path to its architectural layer.
func classify(rel string, isTest bool) Layer {
	if isTest {
		return LayerTest
	}
	switch {
	case rel == "main.go":
		return LayerMain
	case strings.HasPrefix(rel, "core/domain/"):
		return LayerDomain
	case strings.HasPrefix(rel, "core/port/"):
		return LayerPort
	case strings.HasPrefix(rel, "repo/"):
		return LayerRepo
	case strings.HasPrefix(rel, "handler/response/"):
		return LayerResponse
	case strings.HasPrefix(rel, "handler/"):
		return LayerHandler
	case strings.HasPrefix(rel, "bootstrap/"):
		return LayerBootstrap
	case strings.HasPrefix(rel, "routes/"):
		return LayerRoutes
	default:
		return LayerOther
	}
}

// File returns the parsed file at a workspace-relative path.
func (w *Workspace) File(rel string) (*File, bool) {
	f, ok := w.byRel[filepath.ToSlash(rel)]
	return f, ok
}

// FilesIn returns every file in the given layers, in stable path order.
func (w *Workspace) FilesIn(layers ...Layer) []*File {
	want := make(map[Layer]bool, len(layers))
	for _, l := range layers {
		want[l] = true
	}
	var out []*File
	for _, f := range w.Files {
		if want[f.Layer] {
			out = append(out, f)
		}
	}
	return out
}

// SourceOf returns the concatenated source of every file in a layer. Used by
// whole-layer questions such as "is this constructor registered anywhere in
// bootstrap/".
func (w *Workspace) SourceOf(layers ...Layer) string {
	var b strings.Builder
	for _, f := range w.FilesIn(layers...) {
		b.Write(f.Src)
		b.WriteByte('\n')
	}
	return b.String()
}

var (
	moduleRe  = regexp.MustCompile(`(?m)^module\s+(\S+)`)
	goVerRe   = regexp.MustCompile(`(?m)^go\s+(\S+)`)
	requireRe = regexp.MustCompile(`(?m)^\s*([\w.\-/]+\.[\w.\-/]+)\s+(v\S+)(\s*//\s*indirect)?\s*$`)
)

// ModulePath reads only the module path from a workspace's go.mod.
//
// Separate from Load because the scaffolders need the module path to write
// import lines and nothing else — parsing every Go file in the tree to learn
// one string would be the wrong trade, especially on the greenfield path where
// there are no Go files yet.
func ModulePath(root string) (string, error) {
	b, err := os.ReadFile(filepath.Join(root, "go.mod"))
	if err != nil {
		if os.IsNotExist(err) {
			return "", fmt.Errorf("no go.mod in %s; run `go mod init <module>` first", root)
		}
		return "", fmt.Errorf("read go.mod: %w", err)
	}
	m := moduleRe.FindSubmatch(b)
	if m == nil {
		return "", fmt.Errorf("go.mod in %s has no module declaration", root)
	}
	return string(m[1]), nil
}

// loadGoMod extracts the module path, Go version, and direct requires.
//
// Parsed with regexes rather than golang.org/x/mod/modfile to keep this binary
// dependency-light and, more importantly, to stay tolerant of a malformed
// go.mod — which is a state the agent can legitimately create mid-edit.
func (w *Workspace) loadGoMod() error {
	b, err := os.ReadFile(filepath.Join(w.Root, "go.mod"))
	if err != nil {
		if os.IsNotExist(err) {
			return nil // not a module root; rules that need it will no-op
		}
		return fmt.Errorf("read go.mod: %w", err)
	}
	if m := moduleRe.FindSubmatch(b); m != nil {
		w.ModulePath = string(m[1])
	}
	if m := goVerRe.FindSubmatch(b); m != nil {
		w.GoVersion = string(m[1])
	}
	for i, line := range strings.Split(string(b), "\n") {
		m := requireRe.FindStringSubmatch(line)
		if m == nil || strings.Contains(line, "// indirect") {
			continue
		}
		w.Requires = append(w.Requires, Require{Path: m[1], Version: m[2], Line: i + 1})
	}
	return nil
}
