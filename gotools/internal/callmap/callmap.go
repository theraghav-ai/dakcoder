// Package callmap answers the question a migration keeps asking and nothing
// else in the toolchain could: which handler calls which repository method.
//
// # Why this exists
//
// Every template service reaches its repositories through a field on the
// handler struct:
//
//	type ObjectionHandler struct{ svc *repo.ObjectionRepository }
//	...
//	p, err := uh.svc.ObjectionCreationRepo(ctx, &req)
//
// A general code graph (graphify was measured on pao-back-end-development)
// records none of those calls -- 0 handler-to-repository edges against 182
// call sites -- because resolving `uh.svc.X` needs the type of the field `svc`,
// and a tree-sitter grammar does not have it. A migration needs exactly that
// edge: splitting a 6,571-line handler into steps is choosing which methods go
// together, and a method cannot be converted without the repository methods it
// calls, whose signatures change with it (`*gin.Context` to `context.Context`).
//
// # How
//
// Syntax only, like the rest of gotools: the field's declared type is right
// there in the struct, so receiver -> field -> field type -> method is a lookup,
// not type checking. That keeps it working on code that does not compile, which
// is every service between the dependency swap and the last converted handler.
//
// What is resolved: calls on the receiver (`h.m()`), on one of its fields
// (`h.f.m()`), on a parameter or local declared with a type (`r *repo.X`,
// `var r repo.X`, `r := &repo.X{}`), package functions (`pkg.F()`), and
// same-package functions (`f()`). What is not: calls through an interface, a
// map or slice element, or a function value. Those are counted, never guessed.
package callmap

import (
	"go/ast"
	"path"
	"sort"
	"strings"

	"gitlab.cept.gov.in/it-2.0/dakcoder/gotools/internal/workspace"
)

// Func is one function or method declared in the workspace.
type Func struct {
	// Key is "<dir>.<Recv>.<Name>" for a method and "<dir>.<Name>" for a
	// function, where dir is the package directory ("handler", "repo/postgres").
	Key   string `json:"key"`
	Dir   string `json:"dir"`
	Recv  string `json:"recv,omitempty"`
	Name  string `json:"name"`
	File  string `json:"file"`
	Start int    `json:"start"`
	End   int    `json:"end"`

	Calls []Call `json:"calls,omitempty"`

	decl *ast.FuncDecl
	file *workspace.File
}

// Lines is the declaration's length, doc comment excluded.
func (f *Func) Lines() int { return f.End - f.Start + 1 }

// Label is how a person names it: "PaogenHandler.ListPAOHandler".
func (f *Func) Label() string {
	if f.Recv == "" {
		return f.Name
	}
	return f.Recv + "." + f.Name
}

// Call is one resolved call site.
type Call struct {
	Callee string `json:"callee"`
	Line   int    `json:"line"`
}

// Caller is one call site seen from the callee's side.
type Caller struct {
	Func *Func
	Line int
}

// Map is every function in the workspace and the calls between them.
type Map struct {
	Funcs map[string]*Func
	// Unresolved counts calls through a selector this package would not guess
	// at -- an interface, a map value, a function field. Reported so that "no
	// callers" is never mistaken for proof when it is a gap.
	Unresolved int

	callers map[string][]Caller
}

type typeRef struct{ dir, name string }

// external marks a field whose type lives outside the workspace.
const external = "<external>"

type index struct {
	ws       *workspace.Workspace
	pkgName  map[string]string              // dir -> package clause
	fields   map[typeRef]map[string]typeRef // struct -> field -> declared type
	funcs    map[string]*Func
	typeKeys map[typeRef]bool

	unresolved int // for the function being resolved; collected by Build
}

// Build indexes the workspace. Tests and generated files are left out by the
// workspace load; this adds nothing back.
func Build(ws *workspace.Workspace) *Map {
	ix := &index{
		ws:       ws,
		pkgName:  map[string]string{},
		fields:   map[typeRef]map[string]typeRef{},
		funcs:    map[string]*Func{},
		typeKeys: map[typeRef]bool{},
	}
	for _, f := range ws.Files {
		if f.AST == nil {
			continue
		}
		ix.pkgName[dirOf(f.Rel)] = f.Package
	}
	for _, f := range ws.Files {
		if f.AST != nil {
			ix.collectTypes(f)
		}
	}
	for _, f := range ws.Files {
		if f.AST != nil {
			ix.collectFuncs(f)
		}
	}

	m := &Map{Funcs: ix.funcs, callers: map[string][]Caller{}}
	for _, fn := range ix.funcs {
		fn.Calls, m.Unresolved = ix.calls(fn), m.Unresolved+ix.unresolved
		ix.unresolved = 0
		for _, c := range fn.Calls {
			m.callers[c.Callee] = append(m.callers[c.Callee], Caller{Func: fn, Line: c.Line})
		}
	}
	for k := range m.callers {
		sort.Slice(m.callers[k], func(i, j int) bool {
			a, b := m.callers[k][i], m.callers[k][j]
			if a.Func.File != b.Func.File {
				return a.Func.File < b.Func.File
			}
			return a.Line < b.Line
		})
	}
	return m
}

// Callers of a function key, in file and line order.
func (m *Map) Callers(key string) []Caller { return m.callers[key] }

// InFile is every function declared in one file, in line order.
func (m *Map) InFile(rel string) []*Func {
	var out []*Func
	for _, fn := range m.Funcs {
		if fn.File == rel {
			out = append(out, fn)
		}
	}
	sort.Slice(out, func(i, j int) bool { return out[i].Start < out[j].Start })
	return out
}

// Resolve finds what a symbol could mean. Accepted spellings, most specific
// first: an exact key; "file.go::Name" or "file.go::Type.Name"; "Type.Name";
// "Name". A trailing "()" and a leading "." are ignored, since that is how
// people and other tools write them.
func (m *Map) Resolve(symbol string) []*Func {
	s := strings.TrimSpace(symbol)
	s = strings.TrimSuffix(strings.TrimPrefix(s, "."), "()")
	if fn, ok := m.Funcs[s]; ok {
		return []*Func{fn}
	}
	file := ""
	if i := strings.LastIndex(s, "::"); i >= 0 {
		file, s = strings.Trim(strings.ReplaceAll(s[:i], "\\", "/"), "/"), s[i+2:]
	}
	recv, name := "", s
	if i := strings.LastIndex(s, "."); i >= 0 {
		recv, name = strings.TrimPrefix(s[:i], "*"), s[i+1:]
		if j := strings.LastIndex(recv, "."); j >= 0 {
			recv = recv[j+1:] // "repo.PaogenRepository" -> "PaogenRepository"
		}
	}
	var out []*Func
	for _, fn := range m.Funcs {
		if fn.Name != name || (recv != "" && fn.Recv != recv) {
			continue
		}
		if file != "" && !strings.HasSuffix(fn.File, file) {
			continue
		}
		out = append(out, fn)
	}
	sort.Slice(out, func(i, j int) bool { return out[i].Key < out[j].Key })
	return out
}

// ── indexing ────────────────────────────────────────────────────────────────

func dirOf(rel string) string {
	d := path.Dir(rel)
	if d == "." {
		return ""
	}
	return d
}

func key(dir, recv, name string) string {
	parts := []string{}
	if dir != "" {
		parts = append(parts, dir)
	}
	if recv != "" {
		parts = append(parts, recv)
	}
	return strings.Join(append(parts, name), ".")
}

// importDirs maps each local import name in a file to the workspace directory
// it refers to. Imports from outside the module are left out: nothing they
// declare is in the map.
func (ix *index) importDirs(f *workspace.File) map[string]string {
	out := map[string]string{}
	mod := ix.ws.ModulePath
	for p, alias := range f.Imports() {
		dir, ok := "", false
		switch {
		case mod != "" && p == mod:
			dir, ok = "", true
		case mod != "" && strings.HasPrefix(p, mod+"/"):
			dir, ok = strings.TrimPrefix(p, mod+"/"), true
		}
		if !ok {
			continue
		}
		name := alias
		if name == "" {
			name = ix.pkgName[dir]
			if name == "" {
				name = path.Base(p)
			}
		}
		if name == "_" || name == "." {
			continue
		}
		out[name] = dir
	}
	return out
}

// resolveType reads a declared type as a workspace type, if it is one.
func resolveType(e ast.Expr, dir string, imports map[string]string) (typeRef, bool) {
	for {
		switch t := e.(type) {
		case *ast.StarExpr:
			e = t.X
			continue
		case *ast.Ident:
			return typeRef{dir, t.Name}, true
		case *ast.SelectorExpr:
			if x, ok := t.X.(*ast.Ident); ok {
				if d, ok := imports[x.Name]; ok {
					return typeRef{d, t.Sel.Name}, true
				}
			}
			return typeRef{}, false
		case *ast.IndexExpr: // generic instantiation: T[X]
			e = t.X
			continue
		default:
			return typeRef{}, false
		}
	}
}

func (ix *index) collectTypes(f *workspace.File) {
	dir := dirOf(f.Rel)
	imports := ix.importDirs(f)
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
			ref := typeRef{dir, ts.Name.Name}
			ix.typeKeys[ref] = true
			st, ok := ts.Type.(*ast.StructType)
			if !ok || st.Fields == nil {
				continue
			}
			fields := map[string]typeRef{}
			for _, fld := range st.Fields.List {
				t, ok := resolveType(fld.Type, dir, imports)
				if !ok {
					// A library type (`*config.Config`): known, and outside the
					// map, so a call on it is not an unresolved call.
					t = typeRef{dir: external, name: "?"}
				}
				if len(fld.Names) == 0 { // embedded: the field is named for its type
					fields[t.name] = t
				}
				for _, n := range fld.Names {
					fields[n.Name] = t
				}
			}
			ix.fields[ref] = fields
		}
	}
}

func (ix *index) collectFuncs(f *workspace.File) {
	dir := dirOf(f.Rel)
	for _, d := range f.AST.Decls {
		fd, ok := d.(*ast.FuncDecl)
		if !ok || fd.Name == nil {
			continue
		}
		recv := ""
		if fd.Recv != nil && len(fd.Recv.List) > 0 {
			if t, ok := resolveType(fd.Recv.List[0].Type, dir, nil); ok {
				recv = t.name
			}
		}
		start, _ := f.Position(fd.Pos())
		end, _ := f.Position(fd.End())
		fn := &Func{
			Key: key(dir, recv, fd.Name.Name), Dir: dir, Recv: recv, Name: fd.Name.Name,
			File: f.Rel, Start: start, End: end, decl: fd, file: f,
		}
		ix.funcs[fn.Key] = fn
	}
}

// ── resolving calls ─────────────────────────────────────────────────────────

func (ix *index) calls(fn *Func) []Call {
	fd := fn.decl
	if fd.Body == nil {
		return nil
	}
	imports := ix.importDirs(fn.file)

	// What each local name is known to be: the receiver, typed parameters,
	// and locals declared or constructed with a workspace type.
	vars := map[string]typeRef{}
	if fn.Recv != "" && len(fd.Recv.List[0].Names) > 0 {
		vars[fd.Recv.List[0].Names[0].Name] = typeRef{fn.Dir, fn.Recv}
	}
	if fd.Type.Params != nil {
		for _, p := range fd.Type.Params.List {
			if t, ok := resolveType(p.Type, fn.Dir, imports); ok {
				for _, n := range p.Names {
					vars[n.Name] = t
				}
			}
		}
	}
	ast.Inspect(fd.Body, func(n ast.Node) bool {
		switch s := n.(type) {
		case *ast.ValueSpec:
			if s.Type != nil {
				if t, ok := resolveType(s.Type, fn.Dir, imports); ok {
					for _, name := range s.Names {
						vars[name.Name] = t
					}
				}
			}
		case *ast.AssignStmt:
			for i, rhs := range s.Rhs {
				if i >= len(s.Lhs) {
					break
				}
				id, ok := s.Lhs[i].(*ast.Ident)
				if !ok {
					continue
				}
				if t, ok := constructed(rhs, fn.Dir, imports); ok {
					vars[id.Name] = t
				}
			}
		}
		return true
	})

	var out []Call
	seen := map[string]bool{}
	ast.Inspect(fd.Body, func(n ast.Node) bool {
		call, ok := n.(*ast.CallExpr)
		if !ok {
			return true
		}
		callee, known := ix.callee(call.Fun, fn.Dir, imports, vars)
		if callee == "" {
			if known {
				ix.unresolved++
			}
			return true
		}
		line, _ := fn.file.Position(call.Pos())
		k := callee + "@" + itoa(line)
		if !seen[k] {
			seen[k] = true
			out = append(out, Call{Callee: callee, Line: line})
		}
		return true
	})
	return out
}

// constructed reads `&pkg.T{...}` and `pkg.T{...}`. A constructor call is
// not followed: `NewT` returning a T is a convention, not a guarantee.
func constructed(e ast.Expr, dir string, imports map[string]string) (typeRef, bool) {
	if u, ok := e.(*ast.UnaryExpr); ok {
		e = u.X
	}
	if cl, ok := e.(*ast.CompositeLit); ok && cl.Type != nil {
		return resolveType(cl.Type, dir, imports)
	}
	return typeRef{}, false
}

// callee returns the key of what a call reaches, or "" -- and whether the
// shape was one this package would have resolved had the pieces existed, so
// genuinely unresolvable calls can be counted apart from calls into libraries.
func (ix *index) callee(fun ast.Expr, dir string, imports map[string]string, vars map[string]typeRef) (string, bool) {
	switch f := fun.(type) {
	case *ast.Ident: // same-package function
		k := key(dir, "", f.Name)
		if _, ok := ix.funcs[k]; ok {
			return k, true
		}
		return "", false
	case *ast.SelectorExpr:
		method := f.Sel.Name
		switch x := f.X.(type) {
		case *ast.Ident:
			if t, ok := vars[x.Name]; ok { // v.m()
				return ix.method(t, method)
			}
			if d, ok := imports[x.Name]; ok { // pkg.F()
				k := key(d, "", method)
				if _, ok := ix.funcs[k]; ok {
					return k, true
				}
			}
			return "", false
		case *ast.SelectorExpr: // v.field.m()
			if base, ok := x.X.(*ast.Ident); ok {
				if t, ok := vars[base.Name]; ok {
					if ft, ok := ix.fields[t][x.Sel.Name]; ok {
						if ft.dir == external {
							return "", false
						}
						return ix.method(ft, method)
					}
					return "", true // a field we could not type
				}
			}
			return "", false
		}
	}
	return "", false
}

// method finds a method on a type, looking through embedded fields once.
func (ix *index) method(t typeRef, name string) (string, bool) {
	k := key(t.dir, t.name, name)
	if _, ok := ix.funcs[k]; ok {
		return k, true
	}
	for fieldName, ft := range ix.fields[t] {
		if fieldName != ft.name { // only embedded fields promote methods
			continue
		}
		k := key(ft.dir, ft.name, name)
		if _, ok := ix.funcs[k]; ok {
			return k, true
		}
	}
	return "", ix.typeKeys[t]
}

func itoa(n int) string {
	if n == 0 {
		return "0"
	}
	var b [20]byte
	i := len(b)
	for n > 0 {
		i--
		b[i] = byte('0' + n%10)
		n /= 10
	}
	return string(b[i:])
}
