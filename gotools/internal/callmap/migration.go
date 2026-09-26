package callmap

import (
	"fmt"
	"go/ast"
	"sort"
	"strings"

	"gitlab.cept.gov.in/it-2.0/dakcoder/gotools/internal/routes"
	"gitlab.cept.gov.in/it-2.0/dakcoder/gotools/internal/workspace"
)

// The three questions a conversion asks, answered from the call map.
//
//   - HandlerMap: what is in this handler file, and how does it divide into
//     steps? Every method with its lines, its route and the repository methods
//     it calls, grouped into runs of about DefaultStepLines.
//   - Check: is this step done? Whether each named method has the template
//     shape -- which works on a file whose package does not build, and in the
//     middle of a migration no package does.
//   - Impact: who calls this, before its signature changes?
//
// Each returns data and a rendered report. The report is what the model reads,
// so it is written to be acted on: every group is already a plan step.

// DefaultStepLines is the size of one conversion step, matching the planner's
// rule of about one step per 800 lines.
const DefaultStepLines = 800

// Shape is where a method stands in the conversion.
type Shape string

const (
	ShapeLegacy    Shape = "legacy"    // still on gin
	ShapeConverted Shape = "converted" // has the template signature
	ShapeOther     Shape = "other"     // neither: a helper, or an unfinished edit
)

// Status is one method's shape, with the reasons it is not converted.
type Status struct {
	Func    *Func    `json:"-"`
	Name    string   `json:"name"`
	Shape   Shape    `json:"shape"`
	Reasons []string `json:"reasons,omitempty"`
}

// Classify reads a handler or repository method's shape from its declaration.
func Classify(fn *Func) Status {
	st := Status{Func: fn, Name: fn.Label(), Shape: ShapeConverted}
	fd, f := fn.decl, fn.file
	var params []string
	if fd.Type.Params != nil {
		for _, p := range fd.Type.Params.List {
			n := len(p.Names)
			if n == 0 {
				n = 1
			}
			for i := 0; i < n; i++ {
				params = append(params, f.Text(p.Type))
			}
		}
	}
	var results []string
	if fd.Type.Results != nil {
		for _, r := range fd.Type.Results.List {
			n := len(r.Names)
			if n == 0 {
				n = 1
			}
			for i := 0; i < n; i++ {
				results = append(results, f.Text(r.Type))
			}
		}
	}
	for _, p := range params {
		if strings.Contains(p, "gin.Context") {
			st.Shape = ShapeLegacy
			st.Reasons = append(st.Reasons, "takes "+p)
		}
	}
	if fd.Body != nil {
		for _, bind := range []string{"ShouldBindJSON", "ShouldBindQuery", "ShouldBindUri", "ShouldBind", "BindJSON"} {
			if bodyCalls(fd.Body, bind) {
				st.Shape = ShapeLegacy
				st.Reasons = append(st.Reasons, "calls "+bind)
				break
			}
		}
		if bodyCalls(fd.Body, "handleSuccess") || bodyCalls(fd.Body, "handleCreateSuccess") {
			st.Shape = ShapeLegacy
			st.Reasons = append(st.Reasons, "writes its own response (handleSuccess)")
		}
	}
	if st.Shape == ShapeLegacy || workspace.LayerRepo == layerOf(fn.File) {
		return st
	}
	// A handler: the template shape is (sctx *serverRoute.Context, req T) (*R, error).
	switch {
	case len(params) != 2 || !strings.Contains(params[0], "serverRoute.Context"):
		st.Shape = ShapeOther
		st.Reasons = append(st.Reasons, fmt.Sprintf("parameters (%s), want (sctx *serverRoute.Context, req T)", strings.Join(params, ", ")))
	case len(results) != 2 || results[1] != "error" || !strings.HasPrefix(results[0], "*"):
		st.Shape = ShapeOther
		st.Reasons = append(st.Reasons, fmt.Sprintf("results (%s), want (*response.R, error)", strings.Join(results, ", ")))
	}
	return st
}

func layerOf(rel string) workspace.Layer {
	switch {
	case strings.HasPrefix(rel, "repo/"):
		return workspace.LayerRepo
	case strings.HasPrefix(rel, "handler/response/"):
		return workspace.LayerResponse
	case strings.HasPrefix(rel, "handler/"):
		return workspace.LayerHandler
	}
	return workspace.LayerOther
}

func bodyCalls(body *ast.BlockStmt, name string) bool {
	found := false
	ast.Inspect(body, func(n ast.Node) bool {
		if found {
			return false
		}
		call, ok := n.(*ast.CallExpr)
		if !ok {
			return true
		}
		switch fn := call.Fun.(type) {
		case *ast.SelectorExpr:
			found = fn.Sel.Name == name
		case *ast.Ident:
			found = fn.Name == name
		}
		return !found
	})
	return found
}

// isHandlerMethod is what a route dispatches to: an exported method on a
// *Handler type, other than Routes itself.
func isHandlerMethod(fn *Func) bool {
	return fn.Recv != "" && strings.HasSuffix(fn.Recv, "Handler") &&
		fn.Name != "Routes" && ast.IsExported(fn.Name)
}

// repoCallees is every repository-layer method a function calls, in call order.
func (m *Map) repoCallees(fn *Func) []*Func {
	var out []*Func
	seen := map[string]bool{}
	for _, c := range fn.Calls {
		callee := m.Funcs[c.Callee]
		if callee == nil || seen[c.Callee] || layerOf(callee.File) != workspace.LayerRepo {
			continue
		}
		seen[c.Callee] = true
		out = append(out, callee)
	}
	return out
}

// routesByMethod maps a handler method name to the routes that dispatch to it.
// By name, because a legacy route is written `uh.ListPAOHandler` in
// routes/routes.go with nothing there to say which handler type `uh` is.
func routesByMethod(inv *routes.Inventory) map[string][]string {
	out := map[string][]string{}
	if inv == nil {
		return out
	}
	for _, r := range inv.Routes {
		name := r.Handler
		if i := strings.LastIndex(name, "."); i >= 0 {
			name = name[i+1:]
		}
		p := r.Path
		if p == "" {
			p = "(unresolved path)"
		}
		out[name] = append(out[name], strings.ToUpper(r.Method)+" "+p)
	}
	return out
}

// ── HandlerMap ──────────────────────────────────────────────────────────────

// MethodEntry is one handler method in the map.
type MethodEntry struct {
	Name   string   `json:"name"`
	Start  int      `json:"start"`
	End    int      `json:"end"`
	Shape  Shape    `json:"shape"`
	Routes []string `json:"routes,omitempty"`
	Repo   []string `json:"repo,omitempty"`
}

// Group is one conversion step: consecutive methods, about DefaultStepLines.
type Group struct {
	Start   int      `json:"start"`
	End     int      `json:"end"`
	Methods []string `json:"methods"`
	Repo    []string `json:"repo,omitempty"`
	// Shared is the repository methods this group calls that handlers outside
	// it also call. Converting them is safe -- *gin.Context satisfies
	// context.Context, so an unconverted caller still compiles -- but worth
	// knowing, because the change is visible from the other group too.
	Shared []string `json:"shared,omitempty"`
	Done   bool     `json:"done"`
}

// HandlerMapResult is the map of one handler file.
type HandlerMapResult struct {
	File      string        `json:"file"`
	Types     []string      `json:"types"`
	Lines     int           `json:"lines"`
	Methods   []MethodEntry `json:"methods"`
	Groups    []Group       `json:"groups"`
	Converted int           `json:"converted"`
	Report    string        `json:"report"`
}

// HandlerMap maps one handler file. stepLines of 0 means DefaultStepLines.
func (m *Map) HandlerMap(ws *workspace.Workspace, rel string, stepLines int) (*HandlerMapResult, error) {
	f, ok := ws.File(rel)
	if !ok {
		return nil, fmt.Errorf("%s is not a Go file in this workspace (tests and generated files are not mapped)", rel)
	}
	if stepLines <= 0 {
		stepLines = DefaultStepLines
	}
	byRoute := routesByMethod(routes.Take(ws))

	res := &HandlerMapResult{File: rel, Lines: strings.Count(string(f.Src), "\n") + 1}
	types := map[string]bool{}
	var methods []*Func
	for _, fn := range m.InFile(rel) {
		if isHandlerMethod(fn) {
			methods = append(methods, fn)
			types[fn.Recv] = true
		}
	}
	for t := range types {
		res.Types = append(res.Types, t)
	}
	sort.Strings(res.Types)

	groupOf := map[string]int{}
	var cur *Group
	flush := func() {
		if cur != nil {
			res.Groups = append(res.Groups, *cur)
			cur = nil
		}
	}
	for _, fn := range methods {
		st := Classify(fn)
		if st.Shape == ShapeConverted {
			res.Converted++
		}
		e := MethodEntry{Name: fn.Name, Start: fn.Start, End: fn.End, Shape: st.Shape, Routes: byRoute[fn.Name]}
		for _, r := range m.repoCallees(fn) {
			e.Repo = append(e.Repo, r.Label())
		}
		res.Methods = append(res.Methods, e)

		if cur != nil && fn.End-cur.Start+1 > stepLines {
			flush()
		}
		if cur == nil {
			cur = &Group{Start: fn.Start, Done: true}
		}
		cur.End = fn.End
		cur.Methods = append(cur.Methods, fn.Name)
		cur.Done = cur.Done && st.Shape == ShapeConverted
		groupOf[fn.Key] = len(res.Groups)
	}
	flush()

	// Repository methods per group, and which of them another group also calls.
	for gi := range res.Groups {
		g := &res.Groups[gi]
		seen := map[string]bool{}
		for _, fn := range methods {
			if groupOf[fn.Key] != gi {
				continue
			}
			for _, r := range m.repoCallees(fn) {
				if seen[r.Key] {
					continue
				}
				seen[r.Key] = true
				g.Repo = append(g.Repo, r.Label())
				for _, c := range m.Callers(r.Key) {
					if isHandlerMethod(c.Func) && (c.Func.File != rel || groupOf[c.Func.Key] != gi) {
						g.Shared = append(g.Shared, r.Label())
						break
					}
				}
			}
		}
	}
	res.Report = renderHandlerMap(res, stepLines)
	return res, nil
}

func renderHandlerMap(r *HandlerMapResult, stepLines int) string {
	var b strings.Builder
	fmt.Fprintf(&b, "%s: %s -- %d handler methods, %d converted, %d lines.\n",
		r.File, strings.Join(r.Types, ", "), len(r.Methods), r.Converted, r.Lines)
	if len(r.Methods) == 0 {
		b.WriteString("No handler methods here (exported methods on a *Handler type).\n")
		return b.String()
	}
	fmt.Fprintf(&b, "Steps of about %d lines, in file order. Each is one plan step: convert its methods "+
		"and the repository methods they call (their *gin.Context parameter becomes context.Context; "+
		"unconverted callers still compile, since *gin.Context is a context.Context). Check a step with "+
		"unit_check path=%s methods=<its methods>.\n", stepLines, r.File)
	byName := map[string]MethodEntry{}
	for _, e := range r.Methods {
		byName[e.Name] = e
	}
	for i, g := range r.Groups {
		status := ""
		if g.Done {
			status = " [done]"
		}
		fmt.Fprintf(&b, "\nStep %d: lines %d-%d (%d lines), %d methods%s\n", i+1, g.Start, g.End, g.End-g.Start+1, len(g.Methods), status)
		for _, name := range g.Methods {
			e := byName[name]
			line := fmt.Sprintf("  %s %d-%d", name, e.Start, e.End)
			if e.Shape == ShapeConverted {
				line += " [converted]"
			}
			if len(e.Routes) > 0 {
				line += "  " + strings.Join(e.Routes, ", ")
			}
			if len(e.Repo) > 0 {
				line += "  -> " + strings.Join(shortRepo(e.Repo), ", ")
			}
			b.WriteString(line + "\n")
		}
		if len(g.Shared) > 0 {
			fmt.Fprintf(&b, "  shared with other steps: %s\n", strings.Join(shortRepo(g.Shared), ", "))
		}
	}
	return b.String()
}

// shortRepo drops the receiver when every name shares it, which in a handler
// file is nearly always: "PaogenRepository.X, .Y" reads better than repeating.
func shortRepo(labels []string) []string {
	out := make([]string, len(labels))
	prev := ""
	for i, l := range labels {
		recv, name := "", l
		if j := strings.LastIndex(l, "."); j >= 0 {
			recv, name = l[:j], l[j+1:]
		}
		if recv != "" && recv == prev {
			out[i] = "." + name
		} else {
			out[i] = l
		}
		prev = recv
	}
	return out
}

// ── Check ───────────────────────────────────────────────────────────────────

// CheckResult says whether a conversion step is finished.
type CheckResult struct {
	File       string   `json:"file"`
	Parses     bool     `json:"parses"`
	ParseError string   `json:"parse_error,omitempty"`
	Methods    []Status `json:"methods"`
	// Repo is the repository methods the named handlers call that still take
	// *gin.Context -- the other half of the step.
	Repo    []Status `json:"repo,omitempty"`
	Unknown []string `json:"unknown,omitempty"`
	OK      bool     `json:"ok"`
	Report  string   `json:"report"`
}

// Check reports whether the named methods of a file are converted. With no
// names it checks every handler method in the file (or every method, for a
// repository file).
func (m *Map) Check(ws *workspace.Workspace, rel string, names []string) (*CheckResult, error) {
	f, ok := ws.File(rel)
	if !ok {
		return nil, fmt.Errorf("%s is not a Go file in this workspace", rel)
	}
	res := &CheckResult{File: rel, Parses: f.ParseErr == nil}
	if f.ParseErr != nil {
		res.ParseError = firstLine(f.ParseErr.Error())
	}
	inFile := m.InFile(rel)
	byName := map[string]*Func{}
	for _, fn := range inFile {
		byName[fn.Name] = fn
		byName[fn.Label()] = fn
	}
	var targets []*Func
	if len(names) == 0 {
		for _, fn := range inFile {
			if isHandlerMethod(fn) || (layerOf(rel) == workspace.LayerRepo && fn.Recv != "") {
				targets = append(targets, fn)
			}
		}
	}
	for _, n := range names {
		n = strings.TrimSuffix(strings.TrimPrefix(strings.TrimSpace(n), "."), "()")
		if n == "" {
			continue
		}
		if fn, ok := byName[n]; ok {
			targets = append(targets, fn)
		} else {
			res.Unknown = append(res.Unknown, n)
		}
	}

	res.OK = res.Parses && len(res.Unknown) == 0 && len(targets) > 0
	seenRepo := map[string]bool{}
	for _, fn := range targets {
		st := Classify(fn)
		res.Methods = append(res.Methods, st)
		if st.Shape != ShapeConverted {
			res.OK = false
		}
		for _, r := range m.repoCallees(fn) {
			if seenRepo[r.Key] {
				continue
			}
			seenRepo[r.Key] = true
			if rs := Classify(r); rs.Shape == ShapeLegacy {
				res.Repo = append(res.Repo, rs)
				res.OK = false
			}
		}
	}
	res.Report = renderCheck(res)
	return res, nil
}

func firstLine(s string) string {
	if i := strings.IndexByte(s, '\n'); i >= 0 {
		return s[:i]
	}
	return s
}

func renderCheck(r *CheckResult) string {
	var b strings.Builder
	done := 0
	for _, s := range r.Methods {
		if s.Shape == ShapeConverted {
			done++
		}
	}
	verdict := "NOT DONE"
	if r.OK {
		verdict = "DONE"
	}
	parse := "parses"
	if !r.Parses {
		parse = "does NOT parse: " + r.ParseError
	}
	fmt.Fprintf(&b, "%s: %s. %s %d of %d methods converted", verdict, r.File, parse, done, len(r.Methods))
	if len(r.Repo) > 0 {
		fmt.Fprintf(&b, "; %d repository method(s) they call still take *gin.Context", len(r.Repo))
	}
	b.WriteString(".\n")
	if len(r.Methods) == 0 && len(r.Unknown) == 0 {
		b.WriteString("Nothing to check: name the methods this step converts in `methods`.\n")
	}
	for _, s := range r.Methods {
		if s.Shape == ShapeConverted {
			fmt.Fprintf(&b, "  ok    %s\n", s.Name)
			continue
		}
		fmt.Fprintf(&b, "  todo  %s %d-%d: %s\n", s.Name, s.Func.Start, s.Func.End, strings.Join(s.Reasons, "; "))
	}
	for _, s := range r.Repo {
		fmt.Fprintf(&b, "  todo  %s (%s:%d): %s -- make it context.Context\n", s.Name, s.Func.File, s.Func.Start, strings.Join(s.Reasons, "; "))
	}
	for _, n := range r.Unknown {
		fmt.Fprintf(&b, "  ??    %s: no such function in %s\n", n, r.File)
	}
	b.WriteString("This checks shape, not compilation: in a migration the service builds only once every phase is done.\n")
	return b.String()
}

// ── Impact ──────────────────────────────────────────────────────────────────

// ImpactResult is who reaches a function, two levels up.
type ImpactResult struct {
	Symbol     string   `json:"symbol"`
	Candidates []string `json:"candidates,omitempty"`
	Target     string   `json:"target,omitempty"`
	Callers    []string `json:"callers"`
	Unresolved int      `json:"unresolved"`
	Report     string   `json:"report"`
}

// Impact lists the callers of a symbol, and their callers, with the routes
// that reach the handlers among them.
func (m *Map) Impact(ws *workspace.Workspace, symbol string) *ImpactResult {
	res := &ImpactResult{Symbol: symbol, Unresolved: m.Unresolved}
	found := m.Resolve(symbol)
	var b strings.Builder
	switch len(found) {
	case 0:
		fmt.Fprintf(&b, "No function named %q in this workspace. Pass Type.Method or file.go::Name.\n", symbol)
		res.Report = b.String()
		return res
	case 1:
	default:
		fmt.Fprintf(&b, "%q matches %d functions; call again with one of these:\n", symbol, len(found))
		for _, fn := range found {
			res.Candidates = append(res.Candidates, fn.File+"::"+fn.Label())
			fmt.Fprintf(&b, "  %s::%s  (%s:%d)\n", fn.File, fn.Label(), fn.File, fn.Start)
		}
		res.Report = b.String()
		return res
	}
	target := found[0]
	res.Target = target.Key
	byRoute := routesByMethod(routes.Take(ws))

	direct := m.Callers(target.Key)
	fmt.Fprintf(&b, "%s (%s:%d-%d) is called from %d place(s).\n", target.Label(), target.File, target.Start, target.End, len(direct))
	seen := map[string]bool{}
	for _, c := range direct {
		line := fmt.Sprintf("  %s  %s:%d", c.Func.Label(), c.Func.File, c.Line)
		if rs := byRoute[c.Func.Name]; isHandlerMethod(c.Func) && len(rs) > 0 {
			line += "  (" + strings.Join(rs, ", ") + ")"
		}
		res.Callers = append(res.Callers, c.Func.Key)
		b.WriteString(line + "\n")
		if seen[c.Func.Key] || isHandlerMethod(c.Func) {
			continue
		}
		seen[c.Func.Key] = true
		for _, up := range m.Callers(c.Func.Key) {
			fmt.Fprintf(&b, "      <- %s  %s:%d\n", up.Func.Label(), up.Func.File, up.Line)
		}
	}
	if len(direct) == 0 {
		fmt.Fprintf(&b, "Nothing in the workspace calls it directly. %d call(s) elsewhere go through an "+
			"interface or a function value and cannot be traced; search_repo for .%s( to be sure.\n",
			m.Unresolved, target.Name)
	}
	if st := Classify(target); st.Shape == ShapeLegacy {
		fmt.Fprintf(&b, "It still %s. Changing *gin.Context to context.Context keeps every caller above "+
			"compiling, converted or not.\n", strings.Join(st.Reasons, "; "))
	}
	res.Report = b.String()
	return res
}
