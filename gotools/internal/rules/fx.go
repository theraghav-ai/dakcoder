package rules

import (
	"go/ast"
	"sort"
	"strings"

	"gitlab.cept.gov.in/it-2.0/dakcoder/gotools/internal/workspace"
)

const (
	fxModuleRepo    = "FxRepo"
	fxModuleHandler = "FxHandler"
	handlerIface    = "serverHandler.Handler"
	groupTag        = "serverHandler.ServerControllersGroupTag"
)

// ── the composition root, read once ─────────────────────────────────────────

// FXKind distinguishes the two registrations, which are not interchangeable.
type FXKind string

const (
	FXRepo    FXKind = "repo"
	FXHandler FXKind = "handler"
)

// FXState is what the composition root says about one constructor.
type FXState string

const (
	// FXUnregistered: absent from bootstrap/ entirely. Uber-FX fails at startup
	// with an error naming a type rather than a file.
	FXUnregistered FXState = "unregistered"
	// FXPlain: registered with a bare fx.Provide. Correct for a repository;
	// for a handler it compiles, starts, and silently serves nothing, because
	// the server collects handlers by group tag and an untagged provider is
	// never collected.
	FXPlain FXState = "plain"
	// FXAnnotated: registered through fx.Annotate.
	FXAnnotated FXState = "annotated"
)

// FXCtor is one constructor declared in code, with its registration.
type FXCtor struct {
	Name string
	Kind FXKind
	File string
	Line int

	State FXState
	// HasAs and HasResultTags apply when State is FXAnnotated.
	HasAs, HasResultTags bool
	// RegPath and RegLine locate the registration, when there is one.
	RegPath string
	RegLine int

	decl ast.Node
	file *workspace.File
}

// OK reports whether a constructor is correctly registered for its kind.
func (c FXCtor) OK() bool {
	if c.Kind == FXRepo {
		return c.State != FXUnregistered
	}
	return c.State == FXAnnotated && c.HasAs && c.HasResultTags
}

// FXScan is the composition root as the analyser sees it.
//
// One traversal, two consumers. fx-registration reports from it and repo_map
// summarises it, so the two cannot disagree — and if they could, the failure
// would be a loop rather than a wrong answer: the Planner reads repo_map,
// believes a handler is wired, plans no wiring step, and the Verifier then
// blocks on fx-registration for the rest of the run.
type FXScan struct {
	// Present reports whether a bootstrap package was found at all.
	Present bool
	// Ctors are the constructors declared in code, in name order.
	Ctors []FXCtor
}

// Names returns the constructor names of a kind, optionally filtered.
func (s FXScan) Names(kind FXKind, keep func(FXCtor) bool) []string {
	var out []string
	for _, c := range s.Ctors {
		if c.Kind != kind {
			continue
		}
		if keep != nil && !keep(c) {
			continue
		}
		out = append(out, c.Name)
	}
	return out
}

// Unwired returns every constructor with no registration at all.
func (s FXScan) Unwired() []string {
	var out []string
	for _, c := range s.Ctors {
		if c.State == FXUnregistered {
			out = append(out, c.Name)
		}
	}
	return out
}

// Misregistered returns handlers that are registered but will not serve — the
// worst failure mode in the template, because nothing reports it at runtime.
func (s FXScan) Misregistered() []string {
	var out []string
	for _, c := range s.Ctors {
		if c.Kind == FXHandler && c.State != FXUnregistered && !c.OK() {
			out = append(out, c.Name)
		}
	}
	return out
}

// ScanFX reads the composition root out of a loaded workspace.
func ScanFX(ws *workspace.Workspace) FXScan {
	var scan FXScan

	annotated := map[string]FXAnnotation{}
	plain := map[string]bool{}
	boot := ws.FilesIn(workspace.LayerBootstrap)
	scan.Present = len(boot) > 0
	for _, f := range boot {
		collectFxProvides(f, annotated, plain)
	}

	add := func(f *workspace.File, kind FXKind, suffix string) {
		for _, ref := range constructorsReturning(f, suffix) {
			c := FXCtor{
				Name: ref.Name, Kind: kind, File: ref.File, Line: ref.Line,
				State: FXUnregistered, file: f, decl: ref.decl(f),
			}
			if ann, ok := annotated[ref.Name]; ok {
				c.State = FXAnnotated
				c.HasAs, c.HasResultTags = ann.HasAs, ann.HasResultTags
				c.RegPath, c.RegLine = ann.Path, ann.Line
			} else if plain[ref.Name] {
				c.State = FXPlain
			}
			scan.Ctors = append(scan.Ctors, c)
		}
	}
	for _, f := range ws.FilesIn(workspace.LayerHandler) {
		add(f, FXHandler, "Handler")
	}
	for _, f := range ws.FilesIn(workspace.LayerRepo) {
		add(f, FXRepo, "Repository")
	}
	sort.SliceStable(scan.Ctors, func(i, j int) bool { return scan.Ctors[i].Name < scan.Ctors[j].Name })
	return scan
}

// ── the rule ────────────────────────────────────────────────────────────────

// FXRegistration is the highest-value rule in the suite.
//
// An unregistered handler compiles perfectly and then fails at startup with an
// Uber-FX graph error — "missing dependencies for function", "could not build
// arguments" — that names a type, not a file, and gives no hint that the fix is
// three lines in bootstrapper.go. It is the single most common way a correct
// resource fails to work, and it is entirely mechanical to detect.
//
// Two distinct registrations are required and they are NOT interchangeable:
//
//   - repositories: plain fx.Provide(repo.NewXRepository)
//   - handlers:     fx.Annotate(handler.NewXHandler,
//     fx.As(new(serverHandler.Handler)),
//     fx.ResultTags(serverHandler.ServerControllersGroupTag))
//
// A handler registered with plain fx.Provide compiles, starts, and silently
// serves no routes — the server collects handlers by group tag, and an
// untagged provider is simply never collected. That is the worst failure mode
// in the template, so it gets its own message.
var FXRegistration = Rule{
	ID:       "fx-registration",
	Severity: SeverityError,
	Summary:  "every repository is in FxRepo; every handler is in FxHandler with fx.Annotate + fx.As + fx.ResultTags",
	Citation: "SOP.md §bootstrap/bootstrapper.go; skill.md §Bootstrap Configuration",
	Check: func(p *Pass) {
		scan := ScanFX(p.WS)
		if !scan.Present {
			return // not a template service; other rules will say so
		}

		for _, c := range scan.Ctors {
			if c.Kind == FXRepo {
				if c.State == FXUnregistered {
					p.At(c.file, c.decl).
						Fix("add repo.%s to the fx.Provide list in %s", c.Name, fxModuleRepo).
						Report("repository constructor %s is not registered in bootstrap/; injection will fail at startup", c.Name)
				}
				continue
			}

			switch c.State {
			case FXUnregistered:
				p.At(c.file, c.decl).
					Fix("add fx.Annotate(handler.%s, fx.As(new(%s)), fx.ResultTags(%s)) to %s",
						c.Name, handlerIface, groupTag, fxModuleHandler).
					Report("handler constructor %s is not registered in bootstrap/; Uber-FX will fail at startup", c.Name)
			case FXPlain:
				p.AtPath("bootstrap/bootstrapper.go", 0).
					Fix("wrap it: fx.Annotate(handler.%s, fx.As(new(%s)), fx.ResultTags(%s))",
						c.Name, handlerIface, groupTag).
					Report("handler %s is registered with plain fx.Provide; without the group tag the server never collects it and its routes silently do not serve", c.Name)
			default:
				if !c.HasAs {
					p.AtPath(c.RegPath, c.RegLine).
						Fix("add fx.As(new(%s))", handlerIface).
						Report("%s is annotated without fx.As(new(%s))", c.Name, handlerIface)
				}
				if !c.HasResultTags {
					p.AtPath(c.RegPath, c.RegLine).
						Fix("add fx.ResultTags(%s)", groupTag).
						Report("%s is annotated without fx.ResultTags(%s); its routes will not be served", c.Name, groupTag)
				}
			}
		}
	},
}

// ── scanning primitives ─────────────────────────────────────────────────────

// FXAnnotation records the parts of an fx.Annotate entry that matter.
type FXAnnotation struct {
	HasAs         bool
	HasResultTags bool
	Path          string
	Line          int
}

type ctorRef struct {
	Name string
	File string
	Line int
}

func (c ctorRef) decl(f *workspace.File) ast.Node {
	var found ast.Node
	funcsIn(f, func(fd *ast.FuncDecl) {
		if fd.Name.Name == c.Name {
			found = fd
		}
	})
	return found
}

// constructorsReturning finds exported New* functions whose single result type
// ends with the given suffix.
func constructorsReturning(f *workspace.File, suffix string) []ctorRef {
	var out []ctorRef
	funcsIn(f, func(fd *ast.FuncDecl) {
		if _, isMethod := receiverType(fd); isMethod {
			return
		}
		if !strings.HasPrefix(fd.Name.Name, "New") || !fd.Name.IsExported() {
			return
		}
		rs := results(fd)
		if len(rs) == 0 || !strings.HasSuffix(strings.TrimPrefix(rs[0], "*"), suffix) {
			return
		}
		line, _ := f.Position(fd.Pos())
		out = append(out, ctorRef{Name: fd.Name.Name, File: f.Rel, Line: line})
	})
	return out
}

// collectFxProvides scans a bootstrap file for fx.Provide and fx.Annotate
// entries, keyed by the bare constructor name.
//
// Keyed by name rather than by qualified selector because the bootstrap package
// aliases its imports (`handler "pisapi/handler"`), and the alias is a local
// detail we should not couple the rule to.
func collectFxProvides(f *workspace.File, annotated map[string]FXAnnotation, plain map[string]bool) {
	ast.Inspect(f.AST, func(n ast.Node) bool {
		call, ok := n.(*ast.CallExpr)
		if !ok {
			return true
		}
		switch callName(call) {
		case "fx.Annotate":
			if len(call.Args) == 0 {
				return true
			}
			name := bareName(call.Args[0])
			if name == "" {
				return true
			}
			line, _ := f.Position(call.Pos())
			a := FXAnnotation{Path: f.Rel, Line: line}
			for _, arg := range call.Args[1:] {
				inner, ok := arg.(*ast.CallExpr)
				if !ok {
					continue
				}
				switch callName(inner) {
				case "fx.As":
					// fx.As names its interface as new(T), so unwrap it.
					if len(inner.Args) > 0 {
						if t, ok := newArgType(inner.Args[0]); ok && strings.HasSuffix(t, "Handler") {
							a.HasAs = true
						}
					}
				case "fx.ResultTags":
					a.HasResultTags = true
				}
			}
			annotated[name] = a
			return false
		case "fx.Provide":
			for _, arg := range call.Args {
				if _, isCall := arg.(*ast.CallExpr); isCall {
					continue // handled by the fx.Annotate branch
				}
				if name := bareName(arg); name != "" {
					plain[name] = true
				}
			}
		}
		return true
	})
}

// bareName renders `repo.NewUserRepository` or `NewUserRepository` as
// `NewUserRepository`.
func bareName(e ast.Expr) string {
	switch t := e.(type) {
	case *ast.Ident:
		return t.Name
	case *ast.SelectorExpr:
		return t.Sel.Name
	}
	return ""
}
