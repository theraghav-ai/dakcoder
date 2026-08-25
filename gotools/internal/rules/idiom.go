package rules

import (
	"go/ast"
	"path"
	"sort"
	"strings"
	"unicode"

	"gitlab.cept.gov.in/it-2.0/dakcoder/gotools/internal/workspace"
)

// GoIdiom is the machine-checkable subset of go.instructions.md.
//
// It sits deliberately *under* the template rules and is advisory: a service
// that is idiomatic but off-template is a much bigger problem than one that is
// on-template and slightly unidiomatic, and a linter that blocks on style
// teaches people to reach for nolint. golangci-lint is configured separately
// and covers far more; what is here is the handful of things worth telling the
// agent about in its own inner loop, where golangci-lint is too slow to run.
//
// The exception is a mismatched package declaration, which does not compile. It
// gates, per plan.md §9.2, and it is here rather than left to the compiler
// because go.instructions.md flags it as a recurring LLM failure mode — the
// model writes `package handler` into a file in `repo/postgres/`, and the error
// the developer then sees names a directory, not the edit that caused it.
//
// Curated from go.instructions.md §Naming Conventions, §Error Handling,
// §Type Definitions and §Common Pitfalls. Everything in those sections that
// needs type information — unchecked errors, missing defer close, nil interface
// versus nil pointer — is left to golangci-lint at the gate.
var GoIdiom = Rule{
	ID:       "go-idiom",
	Severity: SeverityWarning,
	Summary:  "idiomatic Go: any over interface{}, lower-case unpunctuated error strings, one package per directory",
	Citation: "go.instructions.md §Naming Conventions, §Error Handling, §Type Definitions",
	Check: func(p *Pass) {
		checkPackageConsistency(p)
		for _, f := range p.WS.Files {
			if f.Layer == workspace.LayerTest {
				continue
			}
			checkAnyOverInterface(p, f)
			checkErrorStrings(p, f)
			checkErrorWrapping(p, f)
		}
		checkPackageNames(p)
	},
}

// checkPackageConsistency reports files whose package declaration disagrees
// with the rest of their directory.
//
// This is the one finding in go-idiom that gates. It is a compile error, and
// the compiler's version of it names the directory rather than the file that
// introduced the mismatch — so catching it here, against the file the agent
// just wrote, saves a debugging cycle rather than duplicating one.
func checkPackageConsistency(p *Pass) {
	byDir := map[string]map[string][]*workspace.File{}
	for _, f := range p.WS.Files {
		if f.Layer == workspace.LayerTest {
			continue
		}
		dir := path.Dir(f.Rel)
		if byDir[dir] == nil {
			byDir[dir] = map[string][]*workspace.File{}
		}
		byDir[dir][f.Package] = append(byDir[dir][f.Package], f)
	}

	dirs := make([]string, 0, len(byDir))
	for d := range byDir {
		dirs = append(dirs, d)
	}
	sort.Strings(dirs)

	for _, dir := range dirs {
		packages := byDir[dir]
		if len(packages) < 2 {
			continue
		}
		// The majority declaration is taken as correct: the odd file out is the
		// one that was just written, and moving it is the smaller edit.
		majority, majorityCount := "", 0
		names := make([]string, 0, len(packages))
		for name, files := range packages {
			names = append(names, name)
			if len(files) > majorityCount || (len(files) == majorityCount && name < majority) {
				majority, majorityCount = name, len(files)
			}
		}
		sort.Strings(names)
		for _, name := range names {
			if name == majority {
				continue
			}
			for _, f := range packages[name] {
				p.AtLine(f, 1).
					Severity(SeverityError).
					Fix("change it to `package %s` to match the other files in %s/", majority, dir).
					Report("declares `package %s` while %s/ is `package %s`; a directory cannot hold two packages",
						name, dir, majority)
			}
		}
	}
}

// checkAnyOverInterface reports the empty interface written the long way.
func checkAnyOverInterface(p *Pass, f *workspace.File) {
	ast.Inspect(f.AST, func(n ast.Node) bool {
		it, ok := n.(*ast.InterfaceType)
		if !ok || it.Methods == nil || len(it.Methods.List) != 0 {
			return true
		}
		// `any` is a *ast.Ident, so reaching an empty *ast.InterfaceType means
		// the source really did write interface{}.
		p.At(f, it).
			Fix("use `any` — the predeclared alias, available since Go 1.18").
			Report("interface{} written out; the template targets Go 1.25")
		return true
	})
}

// errorConstructors produce an error from a message string.
var errorConstructors = map[string]int{
	"errors.New": 0,
	"fmt.Errorf": 0,
}

// checkErrorStrings enforces the Go convention that error strings read as a
// clause inside a larger sentence: lower-case, no trailing punctuation.
//
// Not cosmetic — errors get wrapped, and `failed to open file: Could not read
// config.` is what a capitalised, punctuated inner message turns into.
func checkErrorStrings(p *Pass, f *workspace.File) {
	ast.Inspect(f.AST, func(n ast.Node) bool {
		call, ok := n.(*ast.CallExpr)
		if !ok {
			return true
		}
		argIdx, isCtor := errorConstructors[callName(call)]
		if !isCtor || len(call.Args) <= argIdx {
			return true
		}
		msg, ok := stringLit(call.Args[argIdx])
		if !ok || msg == "" {
			return true
		}

		if r := []rune(msg)[0]; unicode.IsUpper(r) && !startsWithInitialism(msg) {
			p.At(f, call.Args[argIdx]).
				Fix("start it lower-case: error strings are clauses, not sentences").
				Report("error string is capitalised; it reads badly once wrapped")
		}
		switch msg[len(msg)-1] {
		case '.', '!', '?':
			p.At(f, call.Args[argIdx]).
				Fix("drop the trailing punctuation").
				Report("error string ends in punctuation; it reads badly once wrapped")
		}
		return true
	})
}

// startsWithInitialism allows `HTTP request failed` and `PPO number missing`,
// which are correctly capitalised because the first word is a proper noun or an
// initialism rather than the start of a sentence.
func startsWithInitialism(msg string) bool {
	first := msg
	if i := strings.IndexByte(msg, ' '); i > 0 {
		first = msg[:i]
	}
	if len(first) < 2 {
		return false
	}
	// A word in full capitals is an initialism, not a capitalised sentence.
	return first == strings.ToUpper(first) && strings.IndexFunc(first, unicode.IsLetter) >= 0
}

// checkErrorWrapping reports fmt.Errorf calls that interpolate an error with a
// non-wrapping verb, which severs the chain errors.Is and errors.As walk.
//
// The consequence is concrete in this template: the handler layer relies on
// pgx.ErrNoRows reaching the framework to become a 404. An intermediate
// `fmt.Errorf("fetch pension: %v", err)` turns that into a 500 with no trace of
// why, and nothing about the code looks wrong.
func checkErrorWrapping(p *Pass, f *workspace.File) {
	ast.Inspect(f.AST, func(n ast.Node) bool {
		call, ok := n.(*ast.CallExpr)
		if !ok || callName(call) != "fmt.Errorf" || len(call.Args) < 2 {
			return true
		}
		format, ok := stringLit(call.Args[0])
		if !ok || strings.Contains(format, "%w") {
			return true
		}
		for _, arg := range call.Args[1:] {
			if !looksLikeError(arg) {
				continue
			}
			p.At(f, call).
				Fix("use %%w instead of %%v or %%s so errors.Is and errors.As still work").
				Report("fmt.Errorf interpolates an error without %%w; the wrapped error becomes unreachable")
			return false
		}
		return true
	})
}

// looksLikeError reports whether an expression is, by name, an error value.
// Syntax-only, so this is a naming convention check rather than a type check —
// which is exactly why the rule is advisory.
func looksLikeError(e ast.Expr) bool {
	switch t := e.(type) {
	case *ast.Ident:
		return t.Name == "err" || strings.HasSuffix(t.Name, "Err") || strings.HasSuffix(t.Name, "Error")
	case *ast.SelectorExpr:
		return t.Sel.Name == "err" || strings.HasSuffix(t.Sel.Name, "Err")
	}
	return false
}

// checkPackageNames enforces the package naming convention: lower-case, one
// word, no underscores or mixedCaps.
func checkPackageNames(p *Pass) {
	seen := map[string]bool{}
	for _, f := range p.WS.Files {
		if f.Layer == workspace.LayerTest || seen[f.Package] {
			continue
		}
		seen[f.Package] = true
		name := f.Package
		switch {
		case strings.Contains(name, "_"):
			p.AtLine(f, 1).
				Fix("use a single lower-case word: %s", strings.ReplaceAll(name, "_", "")).
				Report("package name %q contains an underscore", name)
		case name != strings.ToLower(name):
			p.AtLine(f, 1).
				Fix("use a single lower-case word: %s", strings.ToLower(name)).
				Report("package name %q is not lower-case", name)
		}
	}
}
