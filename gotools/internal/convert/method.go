package convert

import (
	"fmt"
	"go/ast"
	"go/parser"
	"go/token"
	"strconv"
	"strings"

	"gitlab.cept.gov.in/it-2.0/dakcoder/gotools/internal/workspace"
)

// methodConv is the state of one method's conversion. Any `left` reason makes
// the whole method stay as it was.
type methodConv struct {
	f       *workspace.File
	fd      *ast.FuncDecl
	ctx     string // the gin context parameter's name
	helpers *helperSet
	parents map[ast.Node]ast.Node

	edits    []edit
	consumed [][2]int // byte ranges already deleted or replaced
	reasons  []string
	notes    []string

	bindVar  string
	bindType ast.Expr
	bindDecl ast.Stmt
	resType  string
	created  bool
	returns  map[*ast.ReturnStmt]bool // bare returns an emitter has claimed
}

func convertMethod(f *workspace.File, fd *ast.FuncDecl, helpers *helperSet, used map[string]bool) (Method, []edit, string) {
	start, _ := f.Position(fd.Pos())
	end, _ := f.Position(fd.End())
	m := Method{Name: fd.Name.Name, Start: start, End: end}
	c := &methodConv{
		f: f, fd: fd, ctx: fd.Type.Params.List[0].Names[0].Name, helpers: helpers,
		parents: parentMap(fd.Body), returns: map[*ast.ReturnStmt]bool{},
	}
	c.run()

	extraDecl := ""
	reqType := "struct{}"
	param := "_"
	if c.bindVar != "" && c.bindType == nil && len(c.reasons) == 0 {
		c.leave("it binds into %s, declared in a form the conversion does not read; declare it `var %s T`", c.bindVar, c.bindVar)
	}
	if c.bindVar != "" && c.bindType != nil {
		param = c.bindVar
		switch t := c.bindType.(type) {
		case *ast.ArrayType:
			// A JSON array body. A named slice type with its own Validate keeps
			// the payload clients send exactly as it was; a wrapper struct, the
			// other way to satisfy route.Validator, would change the API.
			name := fd.Name.Name + "Request"
			for used[name] {
				name += "_"
			}
			used[name] = true
			reqType = name
			elem := c.text(t.Elt)
			extraDecl = fmt.Sprintf(
				"// %s is the request body of %s: a JSON array, bound as it is.\n"+
					"type %s []%s\n\n"+
					"// Validate validates every element, as the legacy handler did one by one.\n"+
					"func (r %s) Validate() error {\n"+
					"\tfor i := range r {\n"+
					"\t\tif v, ok := any(&r[i]).(interface{ Validate() error }); ok {\n"+
					"\t\t\tif err := v.Validate(); err != nil {\n\t\t\t\treturn err\n\t\t\t}\n\t\t}\n"+
					"\t}\n\treturn nil\n}",
				name, fd.Name.Name, name, elem, name)
			c.notes = append(c.notes, fmt.Sprintf("its JSON array body is now %s ([]%s) with a Validate method", name, elem))
		default:
			reqType = c.text(c.bindType)
		}
	}
	if c.resType == "" && len(c.reasons) == 0 {
		c.leave("it never writes a success response (no handleSuccess / handleCreateSuccess)")
	}
	if len(c.reasons) == 0 && c.created {
		// Said in the code, where whoever registers the route will read it.
		// The legacy method chose 201 itself; a template method returns a
		// value, and the status is the route's.
		c.insert(fd.Pos(), "// Answers 201 Created: register its route with http.StatusCreated.\n")
	}
	if len(c.reasons) == 0 {
		sig := fmt.Sprintf("(sctx *%s.Context, %s %s) (*%s, error)", RouteAlias, param, reqType, c.resType)
		c.replace(fd.Type.Params.Pos(), fd.Type.Params.End(), sig)
		c.replaceCtxIdents()
	}
	if len(c.reasons) == 0 {
		c.verify()
	}
	m.Notes = c.notes
	if len(c.reasons) > 0 {
		m.Status, m.Reasons = "left", dedupe(c.reasons)
		return m, nil, ""
	}
	m.Status, m.Request, m.Response, m.Created = "converted", reqType, "*"+c.resType, c.created
	return m, c.edits, extraDecl
}

func (c *methodConv) leave(format string, args ...any) {
	c.reasons = append(c.reasons, fmt.Sprintf(format, args...))
}

func (c *methodConv) off(p token.Pos) int { return off(c.f, p) }

func (c *methodConv) text(n ast.Node) string {
	return string(c.f.Src[c.off(n.Pos()):c.off(n.End())])
}

// rewritten is n's source with the gin context renamed, for text copied into
// a replacement.
func (c *methodConv) rewritten(n ast.Node) string {
	lo := c.off(n.Pos())
	src := c.f.Src[lo:c.off(n.End())]
	var b strings.Builder
	last := 0
	ast.Inspect(n, func(x ast.Node) bool {
		id, ok := x.(*ast.Ident)
		if !ok || id.Name != c.ctx || !c.isCtxUse(id) {
			return true
		}
		p := c.off(id.Pos()) - lo
		b.Write(src[last:p])
		b.WriteString("sctx.Ctx")
		last = p + len(id.Name)
		return true
	})
	b.Write(src[last:])
	return b.String()
}

func (c *methodConv) isCtxUse(id *ast.Ident) bool {
	switch p := c.parents[id].(type) {
	case *ast.SelectorExpr:
		return p.X == id
	case *ast.KeyValueExpr:
		return p.Key != id
	}
	return true
}

func (c *methodConv) consume(lo, hi int) { c.consumed = append(c.consumed, [2]int{lo, hi}) }

func (c *methodConv) inConsumed(p int) bool {
	for _, r := range c.consumed {
		if p >= r[0] && p < r[1] {
			return true
		}
	}
	return false
}

func (c *methodConv) remove(n ast.Node) { c.replace(n.Pos(), n.End(), "") }

func (c *methodConv) replace(from, to token.Pos, text string) {
	lo, hi := c.off(from), c.off(to)
	if text == "" {
		lo, hi = wholeLines(c.f.Src, lo, hi)
	}
	c.edits = append(c.edits, edit{lo, hi, text})
	c.consume(lo, hi)
}

// wholeLines widens a deletion to the lines it fills, so removing a statement
// does not leave its indentation behind as a blank line.
func wholeLines(src []byte, lo, hi int) (int, int) {
	start := lo
	for start > 0 && (src[start-1] == ' ' || src[start-1] == '\t') {
		start--
	}
	if start > 0 && src[start-1] != '\n' {
		return lo, hi
	}
	end := hi
	for end < len(src) && (src[end] == ' ' || src[end] == '\t' || src[end] == '\r') {
		end++
	}
	if end < len(src) && src[end] != '\n' {
		return lo, hi
	}
	if end < len(src) {
		end++
	}
	return start, end
}

func (c *methodConv) insert(at token.Pos, text string) {
	p := c.off(at)
	c.edits = append(c.edits, edit{p, p, text})
}

// ── the walk ─────────────────────────────────────────────────────────────────

func (c *methodConv) run() {
	if c.ctx == "_" {
		c.leave("its gin context parameter is unnamed")
		return
	}
	shadowed := false
	ast.Inspect(c.fd.Body, func(n ast.Node) bool {
		switch x := n.(type) {
		case *ast.AssignStmt:
			if x.Tok == token.DEFINE {
				for _, l := range x.Lhs {
					if id, ok := l.(*ast.Ident); ok && (id.Name == c.ctx || id.Name == "sctx") {
						shadowed = true
					}
				}
			}
		case *ast.ValueSpec:
			for _, id := range x.Names {
				if id.Name == c.ctx || id.Name == "sctx" {
					shadowed = true
				}
			}
		}
		return true
	})
	if shadowed {
		c.leave("it declares its own %s or sctx, which the conversion would rebind", c.ctx)
		return
	}
	c.block(c.fd.Body.List, true, c.fd.Body.Rbrace)
	// Bare returns no emitter claimed would be invalid in a method with results.
	ast.Inspect(c.fd.Body, func(n ast.Node) bool {
		if _, ok := n.(*ast.FuncLit); ok {
			return false
		}
		if r, ok := n.(*ast.ReturnStmt); ok && len(r.Results) == 0 && !c.returns[r] && !c.inConsumed(c.off(r.Pos())) {
			line, _ := c.f.Position(r.Pos())
			c.leave("line %d returns without writing a response; say what it should return", line)
		}
		return true
	})
}

// block converts one statement list. terminal says the list is the last
// thing the method runs, so falling off its end ends the method.
func (c *methodConv) block(list []ast.Stmt, terminal bool, rbrace token.Pos) {
	for i := 0; i < len(list); i++ {
		st := list[i]
		last := i == len(list)-1
		if c.removable(st) {
			c.remove(st)
			continue
		}
		if out, ok := c.emitter(st, list, i); ok {
			c.emit(list, i, out, terminal, rbrace)
			continue
		}
		switch x := st.(type) {
		case *ast.IfStmt:
			c.ifStmt(x, terminal && last)
		case *ast.BlockStmt:
			c.block(x.List, terminal && last, x.Rbrace)
		case *ast.ForStmt:
			c.block(x.Body.List, false, x.Body.Rbrace)
		case *ast.RangeStmt:
			c.block(x.Body.List, false, x.Body.Rbrace)
		case *ast.SwitchStmt:
			c.clauses(x.Body)
		case *ast.TypeSwitchStmt:
			c.clauses(x.Body)
		case *ast.SelectStmt:
			c.clauses(x.Body)
		case *ast.LabeledStmt:
			c.block([]ast.Stmt{x.Stmt}, terminal && last, rbrace)
		}
	}
}

func (c *methodConv) ifStmt(x *ast.IfStmt, terminal bool) {
	c.block(x.Body.List, terminal, x.Body.Rbrace)
	switch e := x.Else.(type) {
	case *ast.BlockStmt:
		c.block(e.List, terminal, e.Rbrace)
	case *ast.IfStmt:
		c.ifStmt(e, terminal)
	}
}

func (c *methodConv) clauses(body *ast.BlockStmt) {
	for _, s := range body.List {
		switch cl := s.(type) {
		case *ast.CaseClause:
			c.block(cl.Body, false, token.NoPos)
		case *ast.CommClause:
			c.block(cl.Body, false, token.NoPos)
		}
	}
}

// ── what is removed ──────────────────────────────────────────────────────────

// removable: the request variable's declaration, its bind blocks, and the
// validation of it (or of its elements). The server does all three before the
// method runs.
func (c *methodConv) removable(st ast.Stmt) bool {
	switch x := st.(type) {
	case *ast.IfStmt:
		if v, ok := c.bindCall(x); ok {
			if c.bindVar != "" && c.bindVar != v {
				c.leave("it binds two request variables (%s and %s); the template takes one", c.bindVar, v)
				return false
			}
			c.bindVar = v
			return true
		}
		if c.bindVar != "" && c.validates(x, func(name string) bool { return name == c.bindVar }) {
			return true
		}
	case *ast.RangeStmt:
		// for _, r := range req { if err := validation.ValidateStruct(r) ... }
		if id, ok := x.X.(*ast.Ident); ok && id.Name == c.bindVar && len(x.Body.List) > 0 {
			elem := ""
			if v, ok := x.Value.(*ast.Ident); ok {
				elem = v.Name
			}
			all := true
			for _, s := range x.Body.List {
				ifs, ok := s.(*ast.IfStmt)
				if !ok || !c.validates(ifs, func(name string) bool { return name == elem }) {
					all = false
					break
				}
			}
			return all
		}
	case *ast.DeclStmt:
		if v, t, ok := c.reqDecl(x); ok && c.declaresBound(v) {
			c.bindType, c.bindDecl = t, st
			return true
		}
	case *ast.AssignStmt:
		// req := T{}
		if x.Tok == token.DEFINE && len(x.Lhs) == 1 && len(x.Rhs) == 1 {
			id, _ := x.Lhs[0].(*ast.Ident)
			cl, _ := x.Rhs[0].(*ast.CompositeLit)
			if id != nil && cl != nil && len(cl.Elts) == 0 && c.declaresBound(id.Name) {
				c.bindType, c.bindDecl = cl.Type, st
				return true
			}
		}
	}
	return false
}

// declaresBound looks ahead: is name the variable the method binds into?
func (c *methodConv) declaresBound(name string) bool {
	found := false
	ast.Inspect(c.fd.Body, func(n ast.Node) bool {
		if ifs, ok := n.(*ast.IfStmt); ok {
			if v, ok := c.bindCall(ifs); ok && v == name {
				found = true
			}
		}
		return !found
	})
	return found
}

func (c *methodConv) reqDecl(d *ast.DeclStmt) (string, ast.Expr, bool) {
	g, ok := d.Decl.(*ast.GenDecl)
	if !ok || g.Tok != token.VAR || len(g.Specs) != 1 {
		return "", nil, false
	}
	vs, ok := g.Specs[0].(*ast.ValueSpec)
	if !ok || len(vs.Names) != 1 || vs.Type == nil || len(vs.Values) != 0 {
		return "", nil, false
	}
	return vs.Names[0].Name, vs.Type, true
}

// bindCall matches `if err := ctx.ShouldBindX(&v); err != nil { ... }`.
func (c *methodConv) bindCall(x *ast.IfStmt) (string, bool) {
	call := initCall(x)
	if call == nil {
		return "", false
	}
	sel, ok := call.Fun.(*ast.SelectorExpr)
	if !ok {
		return "", false
	}
	recv, ok := sel.X.(*ast.Ident)
	if !ok || recv.Name != c.ctx || !(strings.HasPrefix(sel.Sel.Name, "ShouldBind") || strings.HasPrefix(sel.Sel.Name, "Bind")) {
		return "", false
	}
	if len(call.Args) != 1 {
		return "", false
	}
	u, ok := call.Args[0].(*ast.UnaryExpr)
	if !ok || u.Op != token.AND {
		return "", false
	}
	v, ok := u.X.(*ast.Ident)
	if !ok {
		return "", false
	}
	return v.Name, true
}

// validates matches `if err := validation.ValidateStruct(v); err != nil {...}`.
func (c *methodConv) validates(x *ast.IfStmt, wanted func(string) bool) bool {
	call := initCall(x)
	if call == nil || x.Else != nil {
		return false
	}
	sel, ok := call.Fun.(*ast.SelectorExpr)
	if !ok || sel.Sel.Name != "ValidateStruct" || len(call.Args) != 1 {
		return false
	}
	pkg, ok := sel.X.(*ast.Ident)
	if !ok || pkg.Name != "validation" {
		return false
	}
	arg := call.Args[0]
	if u, ok := arg.(*ast.UnaryExpr); ok {
		arg = u.X
	}
	v, ok := arg.(*ast.Ident)
	return ok && wanted(v.Name)
}

func initCall(x *ast.IfStmt) *ast.CallExpr {
	as, ok := x.Init.(*ast.AssignStmt)
	if !ok || len(as.Rhs) != 1 {
		return nil
	}
	call, _ := as.Rhs[0].(*ast.CallExpr)
	return call
}

// ── what writes a response ───────────────────────────────────────────────────

// emission is what a response-writing statement becomes: the values a return
// carries, and the statements it consumed beside itself.
type emission struct {
	values string
	extra  []ast.Stmt
}

// emitter recognises a statement that writes the response, and says what the
// method returns instead.
func (c *methodConv) emitter(st ast.Stmt, list []ast.Stmt, i int) (emission, bool) {
	es, ok := st.(*ast.ExprStmt)
	if !ok {
		return emission{}, false
	}
	call, ok := es.X.(*ast.CallExpr)
	if !ok || len(call.Args) == 0 {
		return emission{}, false
	}
	first, _ := call.Args[0].(*ast.Ident)
	if first == nil || first.Name != c.ctx {
		// ctx.JSON has ctx as the receiver, not the first argument.
		return c.jsonEmitter(call, list, i)
	}
	switch fn := call.Fun.(type) {
	case *ast.Ident:
		switch fn.Name {
		case "handleSuccess", "handleCreateSuccess":
			if len(call.Args) != 2 {
				return emission{}, false
			}
			v, ok := c.success(call.Args[1])
			if !ok {
				return emission{}, false
			}
			c.created = c.created || fn.Name == "handleCreateSuccess"
			return emission{values: v + ", nil"}, true
		}
	case *ast.SelectorExpr:
		pkg, _ := fn.X.(*ast.Ident)
		if pkg != nil && pkg.Name == "apierrors" && strings.HasPrefix(fn.Sel.Name, "Handle") {
			if len(call.Args) == 2 {
				return emission{values: "nil, " + c.rewritten(call.Args[1])}, true
			}
			c.leave("apierrors.%s takes %d arguments; only (ctx, err) converts by rule", fn.Sel.Name, len(call.Args))
			return emission{}, false
		}
	}
	return emission{}, false
}

// success reads the response value, and settles the method's response type.
func (c *methodConv) success(arg ast.Expr) (string, bool) {
	typ, value := "", ""
	switch a := arg.(type) {
	case *ast.CompositeLit:
		typ, value = c.text(a.Type), "&"+c.rewritten(a)
	case *ast.UnaryExpr:
		if cl, ok := a.X.(*ast.CompositeLit); ok && a.Op == token.AND {
			typ, value = c.text(cl.Type), c.rewritten(a)
		}
	case *ast.Ident:
		t, ptr := c.varType(a.Name)
		if t != "" {
			typ = t
			value = "&" + a.Name
			if ptr {
				value = a.Name
			}
		}
	}
	if typ == "" {
		c.leave("cannot tell the response type of %s; declare it as a composite literal", c.text(arg))
		return "", false
	}
	if strings.HasPrefix(typ, "gin.") || strings.HasPrefix(typ, "map[") || strings.HasPrefix(typ, "[]") {
		c.leave("responds with %s; the template returns a named response struct", typ)
		return "", false
	}
	if c.resType != "" && c.resType != typ {
		c.leave("responds with two types (%s and %s); a template method returns one", c.resType, typ)
		return "", false
	}
	c.resType = typ
	return value, true
}

// varType finds name's type from its declaration in the method.
func (c *methodConv) varType(name string) (string, bool) {
	typ, ptr := "", false
	ast.Inspect(c.fd.Body, func(n ast.Node) bool {
		if typ != "" {
			return false
		}
		switch x := n.(type) {
		case *ast.AssignStmt:
			for i, l := range x.Lhs {
				id, ok := l.(*ast.Ident)
				if !ok || id.Name != name || i >= len(x.Rhs) || x.Tok != token.DEFINE {
					continue
				}
				switch r := x.Rhs[i].(type) {
				case *ast.CompositeLit:
					typ = c.text(r.Type)
				case *ast.UnaryExpr:
					if cl, ok := r.X.(*ast.CompositeLit); ok && r.Op == token.AND {
						typ, ptr = c.text(cl.Type), true
					}
				}
			}
		case *ast.ValueSpec:
			for _, id := range x.Names {
				if id.Name == name && x.Type != nil {
					typ = c.text(x.Type)
					if st, ok := x.Type.(*ast.StarExpr); ok {
						typ, ptr = c.text(st.X), true
					}
				}
			}
		}
		return true
	})
	return typ, ptr
}

// jsonEmitter handles ctx.JSON(status, body) when it writes an error: the
// apierrors chain the legacy code builds, or an ad hoc gin.H.
func (c *methodConv) jsonEmitter(call *ast.CallExpr, list []ast.Stmt, i int) (emission, bool) {
	before := list[:i]
	sel, ok := call.Fun.(*ast.SelectorExpr)
	if !ok || sel.Sel.Name != "JSON" || len(call.Args) != 2 {
		return emission{}, false
	}
	recv, ok := sel.X.(*ast.Ident)
	if !ok || recv.Name != c.ctx {
		return emission{}, false
	}
	status, body := call.Args[0], call.Args[1]

	// apiErrorResponse := apierrors.NewAPIErrorResponse(S, T, appError)
	// appError := apierrors.NewAppError(M, C, E)
	// ctx.JSON(apiErrorResponse.StatusCode, apiErrorResponse)
	if id, ok := body.(*ast.Ident); ok {
		resp, respStmt := findDefine(before, id.Name, "NewAPIErrorResponse")
		if resp == nil || len(resp.Args) != 3 {
			c.leave("ctx.JSON writes %s, which is not built by apierrors.NewAPIErrorResponse here", id.Name)
			return emission{}, false
		}
		code, text := resp.Args[0], resp.Args[1]
		msg, errExpr := c.rewritten(text), "nil"
		extra := []ast.Stmt{respStmt}
		if appID, ok := resp.Args[2].(*ast.Ident); ok {
			app, appStmt := findDefine(before, appID.Name, "NewAppError")
			if app == nil || len(app.Args) != 3 {
				c.leave("the error response wraps %s, which is not built by apierrors.NewAppError here", appID.Name)
				return emission{}, false
			}
			msg, errExpr = c.rewritten(app.Args[0]), c.rewritten(app.Args[2])
			extra = append(extra, appStmt)
			if usesIn(list, appID.Name) > 2 {
				c.leave("%s is used beyond the error response it builds", appID.Name)
				return emission{}, false
			}
		}
		if usesIn(list, id.Name) > 3 {
			c.leave("%s is used beyond the error response", id.Name)
			return emission{}, false
		}
		return emission{values: "nil, " + errorValue(c.rewritten(code), c.rewritten(text), msg, errExpr), extra: extra}, true
	}

	// ctx.JSON(S, apierrors.NewAPIErrorResponse(S, "Text", appError))
	if inner, ok := body.(*ast.CallExpr); ok {
		if sel, ok := inner.Fun.(*ast.SelectorExpr); ok && sel.Sel.Name == "NewAPIErrorResponse" && len(inner.Args) == 3 {
			code, text := inner.Args[0], inner.Args[1]
			msg, errExpr := c.rewritten(text), "nil"
			var extra []ast.Stmt
			if appID, ok := inner.Args[2].(*ast.Ident); ok {
				app, appStmt := findDefine(before, appID.Name, "NewAppError")
				if app == nil || len(app.Args) != 3 || usesIn(list, appID.Name) > 2 {
					c.leave("the error response wraps %s, which is not built here by apierrors.NewAppError alone", appID.Name)
					return emission{}, false
				}
				msg, errExpr = c.rewritten(app.Args[0]), c.rewritten(app.Args[2])
				extra = append(extra, appStmt)
			}
			return emission{values: "nil, " + errorValue(c.rewritten(code), c.rewritten(text), msg, errExpr), extra: extra}, true
		}
	}

	code, ok := statusCode(status)
	if !ok {
		c.leave("ctx.JSON(%s, ...) writes a status the conversion cannot read", c.text(status))
		return emission{}, false
	}
	if code < 400 {
		c.leave("ctx.JSON(%s, ...) writes its own success body; return a response struct instead", c.text(status))
		return emission{}, false
	}
	cl, ok := body.(*ast.CompositeLit)
	if !ok || c.text(cl.Type) != "gin.H" {
		c.leave("ctx.JSON(%s, %s) writes an error body the conversion cannot read", c.text(status), c.text(body))
		return emission{}, false
	}
	msg, errExpr := "", "nil"
	for _, e := range cl.Elts {
		kv, ok := e.(*ast.KeyValueExpr)
		if !ok {
			continue
		}
		key, _ := strconv.Unquote(c.text(kv.Key))
		switch strings.ToLower(key) {
		case "error", "message", "msg":
			if msg == "" {
				msg = c.rewritten(kv.Value)
				if ce, ok := kv.Value.(*ast.CallExpr); ok && isErrorCall(ce) {
					errExpr = c.rewritten(ce.Fun.(*ast.SelectorExpr).X)
				}
			}
		case "details", "detail", "err", "cause":
			if ce, ok := kv.Value.(*ast.CallExpr); ok && isErrorCall(ce) && errExpr == "nil" {
				errExpr = c.rewritten(ce.Fun.(*ast.SelectorExpr).X)
			}
		}
	}
	if msg == "" {
		c.leave("ctx.JSON(%s, gin.H{...}) has no error or message key to carry over", c.text(status))
		return emission{}, false
	}
	if len(cl.Elts) > 2 {
		c.notes = append(c.notes, fmt.Sprintf("the %s error body had %d keys; the template error carries the message and the cause", c.text(status), len(cl.Elts)))
	}
	s := c.rewritten(status)
	return emission{values: "nil, " + errorValue(s, "http.StatusText("+s+")", msg, errExpr)}, true
}

// plainString is a message the compiler already knows is a string.
func plainString(expr string) bool {
	e, err := parser.ParseExpr(expr)
	if err != nil {
		return false
	}
	switch x := e.(type) {
	case *ast.BasicLit:
		return x.Kind == token.STRING
	case *ast.BinaryExpr:
		return x.Op == token.ADD && (plainString(exprString(expr, x.X)) || plainString(exprString(expr, x.Y)))
	case *ast.CallExpr:
		if sel, ok := x.Fun.(*ast.SelectorExpr); ok {
			if id, ok := sel.X.(*ast.Ident); ok && id.Name == "fmt" && strings.HasPrefix(sel.Sel.Name, "Sprint") {
				return true
			}
			return isErrorCall(x)
		}
	}
	return false
}

func exprString(src string, e ast.Expr) string {
	// ParseExpr positions are 1-based offsets into src.
	lo, hi := int(e.Pos())-1, int(e.End())-1
	if lo < 0 || hi > len(src) || lo > hi {
		return ""
	}
	return src[lo:hi]
}

func isErrorCall(ce *ast.CallExpr) bool {
	sel, ok := ce.Fun.(*ast.SelectorExpr)
	return ok && sel.Sel.Name == "Error" && len(ce.Args) == 0
}

func errorValue(code, text, msg, err string) string {
	if !plainString(msg) {
		// A gin.H value is often a map lookup or an interface{}; the template
		// takes a string, and fmt.Sprint is the identity on one.
		msg = "fmt.Sprint(" + msg + ")"
	}
	return fmt.Sprintf("apierrors.HandleErrorWithStatusCodeAndMessage(apierrors.NewHTTPStatusCodeAndMessage(%s, %s), %s, %s)", code, text, msg, err)
}

// statusCode reads http.StatusXxx or an integer literal.
func statusCode(e ast.Expr) (int, bool) {
	switch x := e.(type) {
	case *ast.BasicLit:
		n, err := strconv.Atoi(x.Value)
		return n, err == nil
	case *ast.SelectorExpr:
		if pkg, ok := x.X.(*ast.Ident); ok && pkg.Name == "http" {
			n, ok := httpStatus[x.Sel.Name]
			return n, ok
		}
	}
	return 0, false
}

var httpStatus = map[string]int{
	"StatusOK": 200, "StatusCreated": 201, "StatusAccepted": 202, "StatusNoContent": 204,
	"StatusBadRequest": 400, "StatusUnauthorized": 401, "StatusForbidden": 403, "StatusNotFound": 404,
	"StatusMethodNotAllowed": 405, "StatusConflict": 409, "StatusGone": 410,
	"StatusPreconditionFailed": 412, "StatusRequestEntityTooLarge": 413, "StatusUnsupportedMediaType": 415,
	"StatusUnprocessableEntity": 422, "StatusTooManyRequests": 429,
	"StatusInternalServerError": 500, "StatusNotImplemented": 501, "StatusBadGateway": 502,
	"StatusServiceUnavailable": 503, "StatusGatewayTimeout": 504,
}

// findDefine finds `name := pkg.ctor(...)` in stmts.
func findDefine(stmts []ast.Stmt, name, ctor string) (*ast.CallExpr, ast.Stmt) {
	for i := len(stmts) - 1; i >= 0; i-- {
		as, ok := stmts[i].(*ast.AssignStmt)
		if !ok || len(as.Lhs) != 1 || len(as.Rhs) != 1 {
			continue
		}
		id, ok := as.Lhs[0].(*ast.Ident)
		if !ok || id.Name != name {
			continue
		}
		call, ok := as.Rhs[0].(*ast.CallExpr)
		if !ok {
			return nil, nil
		}
		sel, ok := call.Fun.(*ast.SelectorExpr)
		if !ok || sel.Sel.Name != ctor {
			return nil, nil
		}
		return call, stmts[i]
	}
	return nil, nil
}

// usesIn counts name in one statement list -- the block a legacy error
// response is built in, which is also the scope of the variables it builds.
func usesIn(list []ast.Stmt, name string) int {
	n := 0
	for _, st := range list {
		ast.Inspect(st, func(x ast.Node) bool {
			if sel, ok := x.(*ast.SelectorExpr); ok {
				ast.Inspect(sel.X, func(y ast.Node) bool {
					if id, ok := y.(*ast.Ident); ok && id.Name == name {
						n++
					}
					return true
				})
				return false
			}
			if id, ok := x.(*ast.Ident); ok && id.Name == name {
				n++
			}
			return true
		})
	}
	return n
}

// emit turns the response-writing statement at list[i] into a return. What
// follows it may only be logging, up to a bare return (which becomes the new
// return) or the end of a block that ends the method (where one is added).
func (c *methodConv) emit(list []ast.Stmt, i int, out emission, terminal bool, rbrace token.Pos) {
	for _, s := range out.extra {
		c.remove(s)
	}
	for j := i + 1; j < len(list); j++ {
		switch x := list[j].(type) {
		case *ast.ReturnStmt:
			if len(x.Results) != 0 {
				line, _ := c.f.Position(x.Pos())
				c.leave("line %d returns a value after writing the response", line)
				return
			}
			c.returns[x] = true
			c.replace(list[i].Pos(), list[i].End(), "")
			c.replace(x.Pos(), x.End(), "return "+out.values)
			return
		case *ast.ExprStmt:
			if isLogCall(x) {
				continue
			}
		case *ast.EmptyStmt:
			continue
		}
		line, _ := c.f.Position(list[j].Pos())
		c.leave("line %d runs after the response is written (%s)", line, firstWord(c.text(list[j])))
		return
	}
	if !terminal || !rbrace.IsValid() {
		line, _ := c.f.Position(list[i].Pos())
		c.leave("line %d writes the response and carries on; the method has to return there", line)
		return
	}
	c.replace(list[i].Pos(), list[i].End(), "")
	c.insert(rbrace, "return "+out.values+"\n")
}

func isLogCall(x *ast.ExprStmt) bool {
	call, ok := x.X.(*ast.CallExpr)
	if !ok {
		return false
	}
	sel, ok := call.Fun.(*ast.SelectorExpr)
	if !ok {
		return false
	}
	pkg, ok := sel.X.(*ast.Ident)
	return ok && pkg.Name == "log"
}

func firstWord(s string) string {
	s = strings.TrimSpace(s)
	if i := strings.IndexAny(s, "\n({"); i > 0 {
		s = s[:i]
	}
	if len(s) > 40 {
		s = s[:40]
	}
	return s
}

// replaceCtxIdents renames every remaining use of the gin context, and leaves
// the method if one of them needs gin.
func (c *methodConv) replaceCtxIdents() {
	ast.Inspect(c.fd.Body, func(n ast.Node) bool {
		id, ok := n.(*ast.Ident)
		if !ok || id.Name != c.ctx || !c.isCtxUse(id) || c.inConsumed(c.off(id.Pos())) {
			return true
		}
		switch p := c.parents[id].(type) {
		case *ast.SelectorExpr:
			// ctx.Request.Context() is the one gin use with a template answer.
			if outer, ok := c.parents[p].(*ast.SelectorExpr); ok && p.Sel.Name == "Request" && outer.Sel.Name == "Context" {
				if call, ok := c.parents[outer].(*ast.CallExpr); ok && len(call.Args) == 0 {
					c.replace(call.Pos(), call.End(), "sctx.Ctx")
					return true
				}
			}
			line, _ := c.f.Position(id.Pos())
			c.leave("line %d uses %s.%s, which needs gin", line, c.ctx, p.Sel.Name)
		case *ast.CallExpr:
			if ok, why := c.helpers.calleeOK(p); !ok {
				line, _ := c.f.Position(id.Pos())
				c.leave("line %d passes the context on: %s", line, why)
				return true
			}
			c.replace(id.Pos(), id.End(), "sctx.Ctx")
		default:
			line, _ := c.f.Position(id.Pos())
			c.leave("line %d uses %s other than as a call argument", line, c.ctx)
		}
		return true
	})
}

// verify parses the converted method on its own, and checks that it ends in
// a terminating statement -- the two ways a rewrite here could produce Go
// that does not compile for reasons of its own making.
func (c *methodConv) verify() {
	lo, hi := c.off(c.fd.Pos()), c.off(c.fd.End())
	var local []edit
	for _, e := range c.edits {
		if e.lo >= lo && e.hi <= hi {
			local = append(local, edit{e.lo - lo, e.hi - lo, e.text})
		}
	}
	body, err := apply(c.f.Src[lo:hi], local)
	if err != nil {
		c.leave("internal: %v", err)
		return
	}
	src := "package p\n\n" + string(body) + "\n"
	af, err := parser.ParseFile(token.NewFileSet(), "", src, 0)
	if err != nil {
		c.leave("the conversion did not parse (%v); left for review", err)
		return
	}
	for _, d := range af.Decls {
		if fd, ok := d.(*ast.FuncDecl); ok && !terminates(fd.Body) {
			c.leave("not every path returns after conversion; it has a branch that ends without a response")
		}
	}
}

// terminates is the Go specification's terminating statement, for the shapes
// a handler uses.
func terminates(s ast.Stmt) bool {
	switch x := s.(type) {
	case *ast.ReturnStmt:
		return true
	case *ast.BlockStmt:
		return len(x.List) > 0 && terminates(x.List[len(x.List)-1])
	case *ast.IfStmt:
		return x.Else != nil && terminates(x.Body) && terminates(x.Else)
	case *ast.ExprStmt:
		if call, ok := x.X.(*ast.CallExpr); ok {
			if id, ok := call.Fun.(*ast.Ident); ok && id.Name == "panic" {
				return true
			}
		}
	case *ast.ForStmt:
		return x.Cond == nil && !hasBreak(x.Body)
	case *ast.LabeledStmt:
		return terminates(x.Stmt)
	case *ast.SwitchStmt:
		return clausesTerminate(x.Body)
	case *ast.TypeSwitchStmt:
		return clausesTerminate(x.Body)
	}
	return false
}

func clausesTerminate(body *ast.BlockStmt) bool {
	hasDefault := false
	for _, s := range body.List {
		cl, ok := s.(*ast.CaseClause)
		if !ok {
			return false
		}
		if cl.List == nil {
			hasDefault = true
		}
		if len(cl.Body) == 0 || !terminates(cl.Body[len(cl.Body)-1]) || hasBreak(&ast.BlockStmt{List: cl.Body}) {
			return false
		}
	}
	return hasDefault
}

func hasBreak(b *ast.BlockStmt) bool {
	found := false
	ast.Inspect(b, func(n ast.Node) bool {
		switch x := n.(type) {
		case *ast.FuncLit, *ast.ForStmt, *ast.RangeStmt, *ast.SwitchStmt, *ast.TypeSwitchStmt, *ast.SelectStmt:
			return n == ast.Node(b)
		case *ast.BranchStmt:
			if x.Tok == token.BREAK {
				found = true
			}
		}
		return !found
	})
	return found
}

func dedupe(in []string) []string {
	seen := map[string]bool{}
	var out []string
	for _, s := range in {
		if !seen[s] {
			seen[s] = true
			out = append(out, s)
		}
	}
	return out
}
