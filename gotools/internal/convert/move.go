package convert

import (
	"fmt"
	"go/ast"
	"go/token"
	"os"
	"path/filepath"
	"reflect"
	"sort"
	"strconv"
	"strings"
	"unicode"

	"gitlab.cept.gov.in/it-2.0/dakcoder/gotools/internal/gopatch"
	"gitlab.cept.gov.in/it-2.0/dakcoder/gotools/internal/workspace"
)

// Moving the request DTOs to where the template keeps them.
//
// The template's request types live in handler/request/request.go, package
// request, and that is not taste: govalid is run on that one file, and the
// request-dto rule the final gate enforces reports every request struct
// declared anywhere else. A legacy service declares them beside the handlers
// that bind them -- pao-backend has 53 across five files.
//
// What moves: every handler-package struct named like a request (…Request,
// …Req, …Uri, …Params), every named-slice request the handler conversion
// wrote, and every handler-package type those reference, transitively, with
// the methods declared on them. An unexported name is exported, since the
// handlers now refer to it from another package. Every use in the handler
// package is qualified. Two gaps the same rule reports are closed in passing,
// both without changing behaviour: a field with no validate tag gets
// validate:"omitempty" (which checks nothing), and a field with no json/uri/
// form tag gets json:"<FieldName>" -- the name encoding/json already matched
// it by, case-insensitively.

const requestDir = "handler/request"

type movedDecl struct {
	file *workspace.File
	decl *ast.GenDecl
	spec *ast.TypeSpec
	name string // new (exported) name
}

// moveDTOs moves the request types out of the handler package. It returns the
// names now in package request, and the files it wrote or removed.
func moveDTOs(ws *workspace.Workspace, write bool, res *DTOResult) (map[string]string, error) {
	files, endings := goFiles(ws)
	var handlerFiles []*workspace.File
	for _, f := range files {
		if f.Layer == workspace.LayerHandler && filepath.ToSlash(filepath.Dir(f.Rel)) == "handler" {
			handlerFiles = append(handlerFiles, f)
		}
	}
	if len(handlerFiles) == 0 {
		return nil, nil
	}

	// every type declared in the handler package
	decls := map[string]*movedDecl{}
	for _, f := range handlerFiles {
		if strings.HasSuffix(f.Rel, "_validator.go") || filepath.Base(f.Rel) == runtimeFile {
			continue
		}
		for _, d := range f.AST.Decls {
			g, ok := d.(*ast.GenDecl)
			if !ok || g.Tok != token.TYPE {
				continue
			}
			for _, s := range g.Specs {
				if ts, ok := s.(*ast.TypeSpec); ok {
					decls[ts.Name.Name] = &movedDecl{file: f, decl: g, spec: ts, name: ts.Name.Name}
				}
			}
		}
	}

	// seeds, then their closure over handler-package types
	moving := map[string]*movedDecl{}
	var queue []string
	for name, d := range decls {
		_, isStruct := d.spec.Type.(*ast.StructType)
		_, isSlice := d.spec.Type.(*ast.ArrayType)
		if (isStruct || isSlice) && isRequestName(name) && !strings.HasSuffix(name, "Handler") {
			queue = append(queue, name)
		}
	}
	// ...and every type a converted handler actually binds, whatever its name:
	// pao binds ObjectionId, SubTeVerifiedBullk and DdoMasterInput, and a
	// request type left behind keeps no validator.
	for _, f := range handlerFiles {
		for _, d := range f.AST.Decls {
			fd, ok := d.(*ast.FuncDecl)
			if !ok || fd.Recv == nil || !isTemplateHandler(fd) || len(fd.Type.Params.List) < 2 {
				continue
			}
			if id, ok := fd.Type.Params.List[1].Type.(*ast.Ident); ok && decls[id.Name] != nil {
				queue = append(queue, id.Name)
			}
		}
	}
	sort.Strings(queue)
	for len(queue) > 0 {
		name := queue[0]
		queue = queue[1:]
		if moving[name] != nil {
			continue
		}
		d := decls[name]
		if d == nil {
			continue
		}
		if len(d.decl.Specs) != 1 {
			res.Notes = append(res.Notes, fmt.Sprintf("%s is declared in a grouped type (...) block; left in %s", name, d.file.Rel))
			continue
		}
		moving[name] = d
		ast.Inspect(d.spec.Type, func(n ast.Node) bool {
			if id, ok := n.(*ast.Ident); ok && decls[id.Name] != nil && moving[id.Name] == nil {
				queue = append(queue, id.Name)
			}
			return true
		})
	}
	if len(moving) == 0 {
		return nil, nil
	}

	// the methods on moved types (a named slice's Validate, say)
	type method struct {
		file *workspace.File
		decl *ast.FuncDecl
	}
	var methods []method
	for _, f := range handlerFiles {
		for _, d := range f.AST.Decls {
			fd, ok := d.(*ast.FuncDecl)
			if ok && fd.Recv != nil && moving[recvName(fd)] != nil {
				if strings.HasSuffix(f.Rel, "_validator.go") || filepath.Base(f.Rel) == runtimeFile {
					continue // regenerated in package request
				}
				methods = append(methods, method{f, fd})
			}
		}
	}

	// exported names, without collisions
	rename := map[string]string{}
	for name, d := range moving {
		n := name
		if !ast.IsExported(n) {
			r := []rune(n)
			r[0] = unicode.ToUpper(r[0])
			n = string(r)
			for decls[n] != nil && decls[n] != d {
				n += "DTO"
			}
		}
		d.name = n
		rename[name] = n
	}

	// ── request.go ──
	imports := map[string]string{} // spec text -> ""
	var body strings.Builder
	names := make([]string, 0, len(moving))
	for n := range moving {
		names = append(names, n)
	}
	sort.Slice(names, func(i, j int) bool {
		a, b := moving[names[i]], moving[names[j]]
		if a.file.Rel != b.file.Rel {
			return a.file.Rel < b.file.Rel
		}
		return a.decl.Pos() < b.decl.Pos()
	})
	for _, n := range names {
		d := moving[n]
		text := declText(d.file, d.decl, rename, "", true)
		body.WriteString(text + "\n\n")
		collectImports(d.file, d.decl, imports)
	}
	for _, m := range methods {
		body.WriteString(declText(m.file, m.decl, rename, "", false) + "\n\n")
		collectImports(m.file, m.decl, imports)
	}
	var head strings.Builder
	head.WriteString("// Package request holds the service's request DTOs: what each handler binds\n")
	head.WriteString("// and validates. Moved here from the handler files by the migration, as the\n")
	head.WriteString("// template keeps them; govalid generates their validators from this file.\n")
	head.WriteString("package request\n\n")
	if len(imports) > 0 {
		specs := make([]string, 0, len(imports))
		for s := range imports {
			specs = append(specs, s)
		}
		sort.Strings(specs)
		head.WriteString("import (\n\t" + strings.Join(specs, "\n\t") + "\n)\n\n")
	}
	reqSrc, err := gopatch.Format([]byte(head.String() + body.String()))
	if err != nil {
		return nil, fmt.Errorf("request.go does not format: %w", err)
	}
	reqSrc, err = dropUnused(reqSrc, "request.go")
	if err != nil {
		return nil, err
	}
	reqSrc, err = gopatch.Format(reqSrc)
	if err != nil {
		return nil, err
	}

	// ── the handler files: decls out, references qualified ──
	importPath := ws.ModulePath + "/" + requestDir
	edited := map[*workspace.File][]byte{}
	for _, f := range handlerFiles {
		if strings.HasSuffix(f.Rel, "_validator.go") || filepath.Base(f.Rel) == runtimeFile {
			continue
		}
		alias := "request"
		if definesIdent(f.AST, "request") {
			alias = "requestdto"
		}
		var edits []edit
		used := false
		for _, n := range names {
			d := moving[n]
			if d.file == f {
				lo, hi := declSpan(f, d.decl)
				edits = append(edits, edit{lo, hi, ""})
			}
		}
		for _, m := range methods {
			if m.file == f {
				lo, hi := declSpan(f, m.decl)
				edits = append(edits, edit{lo, hi, ""})
			}
		}
		removed := func(p int) bool {
			for _, e := range edits {
				if e.text == "" && p >= e.lo && p < e.hi {
					return true
				}
			}
			return false
		}
		parents := parentMap(f.AST)
		ast.Inspect(f.AST, func(n ast.Node) bool {
			id, ok := n.(*ast.Ident)
			if !ok || moving[id.Name] == nil || !isTypeUse(parents, id) {
				return true
			}
			p := off(f, id.Pos())
			if removed(p) {
				return true
			}
			edits = append(edits, edit{p, p + len(id.Name), alias + "." + rename[id.Name]})
			used = true
			return true
		})
		if len(edits) == 0 {
			continue
		}
		src, err := apply(f.Src, edits)
		if err != nil {
			return nil, fmt.Errorf("%s: %w", f.Rel, err)
		}
		if used {
			if src, _, err = gopatch.EnsureImport(src, alias, importPath, ""); err != nil {
				return nil, err
			}
		}
		src, err = dropUnusedAll(src, f.Rel)
		if err != nil {
			return nil, fmt.Errorf("%s: %w", f.Rel, err)
		}
		if src, err = gopatch.Format(src); err != nil {
			return nil, fmt.Errorf("%s: %w", f.Rel, err)
		}
		edited[f] = src
	}

	// Listed whether or not they are written: a dry run is how the caller
	// learns which files to snapshot before the real one.
	dir := filepath.Join(ws.Root, filepath.FromSlash(requestDir))
	if write {
		if err := os.MkdirAll(dir, 0o755); err != nil {
			return nil, err
		}
	}
	// the handler package's validators for moved types are regenerated there
	for _, f := range handlerFiles {
		base := filepath.Base(f.Rel)
		if strings.HasSuffix(base, "_validator.go") || base == runtimeFile {
			if write {
				_ = os.Remove(f.Abs)
			}
			res.Removed = append(res.Removed, f.Rel)
		}
	}
	if write {
		if err := os.WriteFile(filepath.Join(dir, "request.go"), reqSrc, 0o644); err != nil {
			return nil, err
		}
	}
	res.Written = append(res.Written, requestDir+"/request.go")
	for f, src := range edited {
		if write {
			ending := endings[f]
			if ending == "" {
				ending = gopatch.LF
			}
			if err := os.WriteFile(f.Abs, gopatch.ApplyEOL(src, ending), 0o644); err != nil {
				return nil, err
			}
		}
		res.Written = append(res.Written, f.Rel)
	}
	res.Moved = len(moving)
	return rename, nil
}

func isRequestName(name string) bool {
	for _, s := range []string{"Request", "Req", "Uri", "Params"} {
		if strings.HasSuffix(name, s) {
			return true
		}
	}
	return false
}

// declText is a declaration's source, doc comment included, with the moved
// types renamed. For a struct it also closes the request-dto tag gaps.
func declText(f *workspace.File, d ast.Decl, rename map[string]string, qualifier string, tags bool) string {
	lo, hi := off(f, d.Pos()), off(f, d.End())
	var doc *ast.CommentGroup
	switch x := d.(type) {
	case *ast.GenDecl:
		doc = x.Doc
	case *ast.FuncDecl:
		doc = x.Doc
	}
	if doc != nil {
		lo = off(f, doc.Pos())
	}
	parents := parentMap(d)
	var edits []edit
	ast.Inspect(d, func(n ast.Node) bool {
		switch x := n.(type) {
		case *ast.Ident:
			if to, ok := rename[x.Name]; ok && to != x.Name && (isTypeUse(parents, x) || isDeclName(parents, x)) {
				p := off(f, x.Pos()) - lo
				edits = append(edits, edit{p, p + len(x.Name), qualifier + to})
			}
		case *ast.Field:
			if !tags || len(x.Names) == 0 {
				return true
			}
			if _, isStruct := parents[x].(*ast.FieldList); !isStruct {
				return true
			}
			if _, inStruct := parents[parents[x]].(*ast.StructType); !inStruct {
				return true
			}
			raw := ""
			if x.Tag != nil {
				raw, _ = strconv.Unquote(x.Tag.Value)
			}
			st := reflect.StructTag(raw)
			add := ""
			_, j := st.Lookup("json")
			_, u := st.Lookup("uri")
			_, fm := st.Lookup("form")
			if !j && !u && !fm {
				add += fmt.Sprintf(` json:%q`, x.Names[0].Name)
			}
			if _, ok := st.Lookup("validate"); !ok {
				add += ` validate:"omitempty"`
			}
			if add == "" {
				return true
			}
			newTag := "`" + strings.TrimSpace(raw+add) + "`"
			if x.Tag != nil {
				p, q := off(f, x.Tag.Pos())-lo, off(f, x.Tag.End())-lo
				edits = append(edits, edit{p, q, newTag})
			} else {
				p := off(f, x.Type.End()) - lo
				edits = append(edits, edit{p, p, " " + newTag})
			}
		}
		return true
	})
	out, err := apply(f.Src[lo:hi], edits)
	if err != nil {
		return string(f.Src[lo:hi])
	}
	return string(out)
}

// isTypeUse: an identifier that refers to a type, not a field, key or selector.
func isTypeUse(parents map[ast.Node]ast.Node, id *ast.Ident) bool {
	switch p := parents[id].(type) {
	case *ast.SelectorExpr:
		return false // pkg.Name or x.Field -- never the bare type
	case *ast.KeyValueExpr:
		return p.Key != id
	case *ast.Field:
		for _, n := range p.Names {
			if n == id {
				return false
			}
		}
		return true
	case *ast.TypeSpec:
		return p.Name != id
	case *ast.FuncDecl:
		return false
	case *ast.AssignStmt:
		for _, l := range p.Lhs {
			if l == id {
				return false
			}
		}
	case *ast.ValueSpec:
		for _, n := range p.Names {
			if n == id {
				return false
			}
		}
	case *ast.LabeledStmt, *ast.BranchStmt:
		return false
	}
	return true
}

func isDeclName(parents map[ast.Node]ast.Node, id *ast.Ident) bool {
	ts, ok := parents[id].(*ast.TypeSpec)
	return ok && ts.Name == id
}

// collectImports adds the import specs a declaration's selectors use.
func collectImports(f *workspace.File, d ast.Decl, into map[string]string) {
	used := map[string]bool{}
	ast.Inspect(d, func(n ast.Node) bool {
		if sel, ok := n.(*ast.SelectorExpr); ok {
			if id, ok := sel.X.(*ast.Ident); ok {
				used[id.Name] = true
			}
		}
		return true
	})
	for _, im := range f.AST.Imports {
		name := importName(im)
		if im.Name == nil {
			p, _ := strconv.Unquote(im.Path.Value)
			name = p[strings.LastIndex(p, "/")+1:]
			if strings.HasPrefix(name, "v") && strings.Trim(name[1:], "0123456789") == "" {
				trimmed := strings.TrimSuffix(p, "/"+name)
				name = trimmed[strings.LastIndex(trimmed, "/")+1:]
			}
		}
		if used[name] {
			text := im.Path.Value
			if im.Name != nil {
				text = im.Name.Name + " " + text
			}
			into[text] = ""
		}
	}
}

// declSpan is a top-level declaration with its doc comment, as whole lines.
func declSpan(f *workspace.File, d ast.Decl) (int, int) {
	lo, hi := off(f, d.Pos()), off(f, d.End())
	switch x := d.(type) {
	case *ast.GenDecl:
		if x.Doc != nil {
			lo = off(f, x.Doc.Pos())
		}
	case *ast.FuncDecl:
		if x.Doc != nil {
			lo = off(f, x.Doc.Pos())
		}
	}
	return wholeLines(f.Src, lo, hi)
}

// definesIdent reports whether name is declared anywhere in the file as a
// variable or parameter -- which would shadow an import of that name.
func definesIdent(af *ast.File, name string) bool {
	found := false
	ast.Inspect(af, func(n ast.Node) bool {
		switch x := n.(type) {
		case *ast.AssignStmt:
			if x.Tok == token.DEFINE {
				for _, l := range x.Lhs {
					if id, ok := l.(*ast.Ident); ok && id.Name == name {
						found = true
					}
				}
			}
		case *ast.ValueSpec:
			for _, id := range x.Names {
				if id.Name == name {
					found = true
				}
			}
		case *ast.Field:
			for _, id := range x.Names {
				if id.Name == name {
					found = true
				}
			}
		case *ast.RangeStmt:
			for _, e := range []ast.Expr{x.Key, x.Value} {
				if id, ok := e.(*ast.Ident); ok && id.Name == name {
					found = true
				}
			}
		}
		return !found
	})
	return found
}

// dropUnusedAll removes any import the file no longer references. Used only
// on files whose declarations were moved out: whatever those declarations
// alone imported is now unused here.
func dropUnusedAll(src []byte, rel string) ([]byte, error) {
	re, err := reparse(rel, src)
	if err != nil {
		return nil, err
	}
	used := map[string]bool{}
	ast.Inspect(re.AST, func(n ast.Node) bool {
		if sel, ok := n.(*ast.SelectorExpr); ok {
			if id, ok := sel.X.(*ast.Ident); ok {
				used[id.Name] = true
			}
		}
		return true
	})
	var drop []edit
	for _, im := range re.AST.Imports {
		if im.Name != nil && (im.Name.Name == "_" || im.Name.Name == ".") {
			continue
		}
		name := ""
		if im.Name != nil {
			name = im.Name.Name
		} else {
			// Only unaliased imports whose package name is certain.
			p, _ := strconv.Unquote(im.Path.Value)
			base := p[strings.LastIndex(p, "/")+1:]
			if strings.ContainsAny(base, ".-") || (strings.HasPrefix(base, "v") && strings.Trim(base[1:], "0123456789") == "") {
				continue
			}
			name = base
		}
		if used[name] {
			continue
		}
		lo, hi := wholeLines(re.Src, re.FSet.Position(im.Pos()).Offset, re.FSet.Position(im.End()).Offset)
		drop = append(drop, edit{lo, hi, ""})
	}
	if len(drop) == 0 {
		return src, nil
	}
	return apply(re.Src, drop)
}
