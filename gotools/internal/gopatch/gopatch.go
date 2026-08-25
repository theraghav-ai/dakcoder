// Package gopatch makes surgical edits to existing Go files.
//
// # Why not re-print the AST
//
// The obvious implementation of "add a declaration to this file" is: parse it,
// mutate the AST, print it back. Two things make that the wrong choice here.
//
// Comment attachment does not survive AST mutation reliably. go/printer
// positions comments by the token offsets recorded in the FileSet, and once
// nodes move relative to those offsets, comments migrate to the wrong
// declaration or vanish. The files we edit — bootstrapper.go and request.go —
// are exactly the ones developers annotate.
//
// And re-printing rewrites the whole file. Every file in the reference template
// uses CRLF line endings, and go/printer emits LF, so a three-line insertion
// would arrive as a diff touching every line in the file. A diff a human is
// asked to approve (Part B §9) has to show only what changed.
//
// So: locate with the AST, edit at byte offsets, verify the result parses, and
// preserve the file's own line endings. The AST is used for precision, not for
// output.
package gopatch

import (
	"bytes"
	"fmt"
	"go/ast"
	"go/format"
	"go/parser"
	"go/token"
	"sort"
	"strconv"
	"strings"
)

// EOL is a file's dominant line ending.
type EOL string

const (
	LF   EOL = "\n"
	CRLF EOL = "\r\n"
)

// DetectEOL reports a file's dominant line ending. A file with no newline at
// all is treated as LF, which is what a newly created file gets.
func DetectEOL(src []byte) EOL {
	crlf := bytes.Count(src, []byte("\r\n"))
	lf := bytes.Count(src, []byte("\n")) - crlf
	if crlf > lf {
		return CRLF
	}
	return LF
}

// ToLF normalises line endings so the parser and our own offset arithmetic
// only ever see one form.
func ToLF(src []byte) []byte {
	return bytes.ReplaceAll(src, []byte("\r\n"), []byte("\n"))
}

// ApplyEOL rewrites LF-normalised content with the given line ending.
func ApplyEOL(src []byte, eol EOL) []byte {
	if eol == LF {
		return src
	}
	return bytes.ReplaceAll(src, []byte("\n"), []byte("\r\n"))
}

// Format runs gofmt over a complete source file, reporting the offending source
// on failure.
//
// This is also the scaffolder's own correctness gate: every generated file goes
// through it, so a template that produces syntactically invalid Go fails at
// scaffold time with a precise position, rather than at the developer's next
// `go build` as a mysterious error in a file they did not write.
func Format(src []byte) ([]byte, error) {
	out, err := format.Source(src)
	if err != nil {
		return nil, fmt.Errorf("generated source does not parse: %w\n%s", err, numbered(src))
	}
	return out, nil
}

// numbered renders source with line numbers, for a template-authoring error
// that would otherwise be very hard to place.
func numbered(src []byte) string {
	var b strings.Builder
	for i, line := range strings.Split(string(src), "\n") {
		fmt.Fprintf(&b, "%4d | %s\n", i+1, line)
	}
	return b.String()
}

// FormatFragment gofmts a fragment of top-level declarations by formatting it
// inside a throwaway package and then removing the wrapper.
func FormatFragment(decls string) (string, error) {
	const wrapper = "package p\n\n"
	out, err := Format([]byte(wrapper + decls))
	if err != nil {
		return "", err
	}
	return strings.TrimLeft(strings.TrimPrefix(string(out), wrapper), "\n"), nil
}

// AppendDecls appends formatted top-level declarations to a Go file, preserving
// the file's existing bytes and line endings exactly.
func AppendDecls(src []byte, decls string) ([]byte, error) {
	eol := DetectEOL(src)
	body := ToLF(src)

	frag, err := FormatFragment(decls)
	if err != nil {
		return nil, err
	}

	var out bytes.Buffer
	out.Write(bytes.TrimRight(body, "\n"))
	out.WriteString("\n\n")
	out.WriteString(strings.TrimRight(frag, "\n"))
	out.WriteString("\n")

	if err := check(out.Bytes()); err != nil {
		return nil, err
	}
	return ApplyEOL(out.Bytes(), eol), nil
}

// check parses a candidate file and reports a syntax error with position.
// Every mutation runs through it: writing a file that does not parse turns one
// clear failure into a confusing cascade at the next build.
func check(src []byte) error {
	fset := token.NewFileSet()
	if _, err := parser.ParseFile(fset, "patched.go", src, parser.ParseComments); err != nil {
		return fmt.Errorf("patched file does not parse: %w", err)
	}
	return nil
}

// HasDecl reports whether a file already declares a top-level type or function
// with the given name. Used to make scaffolding idempotent rather than
// destructive: re-running resource_scaffold must say "already there", not
// append a second copy of every type.
func HasDecl(src []byte, name string) (bool, error) {
	fset := token.NewFileSet()
	f, err := parser.ParseFile(fset, "src.go", ToLF(src), parser.ParseComments)
	if err != nil {
		return false, fmt.Errorf("parse: %w", err)
	}
	for _, d := range f.Decls {
		switch t := d.(type) {
		case *ast.FuncDecl:
			if t.Recv == nil && t.Name.Name == name {
				return true, nil
			}
		case *ast.GenDecl:
			for _, s := range t.Specs {
				if ts, ok := s.(*ast.TypeSpec); ok && ts.Name.Name == name {
					return true, nil
				}
			}
		}
	}
	return false, nil
}

// ImportGroup classifies an import for placement.
type ImportGroup int

const (
	// GroupStd is the standard library: no dot in the first path segment.
	GroupStd ImportGroup = iota
	// GroupModule is the current module's own packages.
	GroupModule
	// GroupExternal is everything else.
	GroupExternal
)

// ClassifyImport places an import path in its group.
//
// The module test comes first, and that ordering is load-bearing. The usual
// "no dot in the first segment means standard library" heuristic is wrong for
// this template: the reference module is called `pisapi`, so `pisapi/core/domain`
// has no dot either and would be filed under the standard library — putting a
// first-party import in the wrong group in every generated file.
func ClassifyImport(path, modulePath string) ImportGroup {
	if modulePath != "" && (path == modulePath || strings.HasPrefix(path, modulePath+"/")) {
		return GroupModule
	}
	first, _, _ := strings.Cut(path, "/")
	if !strings.Contains(first, ".") {
		return GroupStd
	}
	return GroupExternal
}

// EnsureImport adds an import to a file if it is not already present, placing
// it in sorted position inside the group it belongs to.
//
// Placement matters because the agent's inner loop runs `gofmt -w` on every
// mutated file (Part A §9.3). gofmt sorts within a blank-line-separated group
// but never moves an import between groups — so an import appended in the wrong
// place stays in the wrong place, and shows up in the next diff as an unrelated
// change made by the formatter.
//
// Returns the patched source and whether anything changed.
func EnsureImport(src []byte, alias, path, modulePath string) ([]byte, bool, error) {
	eol := DetectEOL(src)
	body := ToLF(src)

	fset := token.NewFileSet()
	file, err := parser.ParseFile(fset, "src.go", body, parser.ParseComments)
	if err != nil {
		return nil, false, fmt.Errorf("parse: %w", err)
	}
	for _, im := range file.Imports {
		if strings.Trim(im.Path.Value, `"`) == path {
			return src, false, nil
		}
	}

	line := "\t" + importSpecText(alias, path)

	decl := findImportDecl(file)
	if decl == nil {
		// No imports at all: open a block immediately after the package clause.
		offset := fset.Position(file.Name.End()).Offset
		block := "\n\nimport (\n" + line + "\n)"
		out := splice(body, offset, offset, block)
		if err := check(out); err != nil {
			return nil, false, err
		}
		return ApplyEOL(out, eol), true, nil
	}

	if !decl.Lparen.IsValid() {
		// Single unparenthesised import: convert to a block so the new spec has
		// somewhere to live.
		lo := fset.Position(decl.Pos()).Offset
		hi := fset.Position(decl.End()).Offset
		existing := decl.Specs[0].(*ast.ImportSpec)
		existingLine := "\t" + importSpecText(aliasOf(existing), strings.Trim(existing.Path.Value, `"`))
		lines := []string{existingLine, line}
		sort.Strings(lines)
		out := splice(body, lo, hi, "import (\n"+strings.Join(lines, "\n")+"\n)")
		formatted, ferr := Format(out)
		if ferr != nil {
			return nil, false, ferr
		}
		return ApplyEOL(formatted, eol), true, nil
	}

	at, text := insertPoint(fset, body, decl, line, path, modulePath)
	out := splice(body, at, at, text)
	if err := check(out); err != nil {
		return nil, false, err
	}
	return ApplyEOL(out, eol), true, nil
}

// insertPoint finds where a new import line belongs, and what to write there.
//
// It walks the existing specs, tracking group boundaries (a blank line between
// specs starts a new group). Within the matching group it inserts in sorted
// path order; when no group matches it opens a new one, keeping the
// std < module < external order and carrying the blank-line separator with it.
func insertPoint(fset *token.FileSet, body []byte, decl *ast.GenDecl, line, path, modulePath string) (int, string) {
	want := ClassifyImport(path, modulePath)

	type entry struct {
		group ImportGroup
		path  string
		start int // offset of the start of this spec's line
		end   int // offset just past this spec's line, including its newline
	}
	var entries []entry
	for _, s := range decl.Specs {
		im, ok := s.(*ast.ImportSpec)
		if !ok {
			continue
		}
		p := strings.Trim(im.Path.Value, `"`)
		start := lineStart(body, fset.Position(im.Pos()).Offset)
		end := lineEnd(body, fset.Position(im.End()).Offset)
		entries = append(entries, entry{ClassifyImport(p, modulePath), p, start, end})
	}
	if len(entries) == 0 {
		return lineStart(body, fset.Position(decl.Rparen).Offset), line + "\n"
	}

	// Sorted position inside the first matching group.
	for i, e := range entries {
		if e.group != want {
			continue
		}
		if path < e.path {
			return e.start, line + "\n"
		}
		// Last spec of this group?
		if i+1 == len(entries) || entries[i+1].group != want {
			return e.end, line + "\n"
		}
	}

	// No matching group: open one above the first later group, keeping the
	// std < module < external order and its blank-line separator.
	for _, e := range entries {
		if e.group > want {
			return e.start, line + "\n\n"
		}
	}
	return entries[len(entries)-1].end, "\n" + line + "\n"
}

func lineStart(body []byte, off int) int {
	if i := bytes.LastIndexByte(body[:off], '\n'); i >= 0 {
		return i + 1
	}
	return 0
}

func lineEnd(body []byte, off int) int {
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

func findImportDecl(f *ast.File) *ast.GenDecl {
	for _, d := range f.Decls {
		if gd, ok := d.(*ast.GenDecl); ok && gd.Tok == token.IMPORT {
			return gd
		}
	}
	return nil
}

func aliasOf(im *ast.ImportSpec) string {
	if im.Name == nil {
		return ""
	}
	return im.Name.Name
}

func importSpecText(alias, path string) string {
	if alias == "" {
		return strconv.Quote(path)
	}
	return alias + " " + strconv.Quote(path)
}
