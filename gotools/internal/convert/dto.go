package convert

import (
	"bytes"
	"fmt"
	"go/ast"
	"go/token"
	"os"
	"os/exec"
	"path/filepath"
	"reflect"
	"sort"
	"strconv"
	"strings"

	"gitlab.cept.gov.in/it-2.0/dakcoder/gotools/internal/gopatch"
	"gitlab.cept.gov.in/it-2.0/dakcoder/gotools/internal/workspace"
)

// The validation half of a conversion. n-api-server calls Validate() on every
// request before the handler runs, and refuses a non-GET request whose type
// has none (422 "request must implement Validator.Validate()") -- so a
// converted handler with an unvalidated DTO is an endpoint that rejects every
// call.
//
// govalid generates Validate() from validate tags at compile time, which is
// what the SOP asks for. But it silently skips any tag it does not know, and a
// legacy service is full of them: pao-backend's DTOs carry validatePaocode,
// validateDdocode, employee_id, date_yyyy_mm_dd, head_of_account and more --
// rules registered with the legacy runtime validator. Generating with govalid
// alone would keep `required` and drop every one of those, with no error.
//
// So each request type is decided on its tags:
//
//   - every tag one govalid implements: govalid generates its Validate();
//   - any other tag: Validate() runs the legacy runtime validator --
//     validation.ValidateStruct, the call the legacy handler made, with the
//     same custom rules registered at startup -- so nothing it checked is lost.
//
// govalid's partial validator for a type in the second group is discarded, so
// nothing is declared twice.

// govalidTags are the validate-tag names govalid implements (MARKERS.md),
// plus the structural ones it honours.
var govalidTags = map[string]bool{
	"required": true, "omitempty": true, "dive": true,
	"lt": true, "lte": true, "gt": true, "gte": true, "min": true, "max": true,
	"len": true, "eq": true, "ne": true, "oneof": true,
	"email": true, "url": true, "uri": true, "uuid": true,
	"alpha": true, "alphanum": true, "numeric": true, "number": true, "boolean": true,
	"lowercase": true, "ipv4": true, "ipv6": true, "fqdn": true,
	"latitude": true, "longitude": true, "iscolor": true, "iscolour": true,
	"containsany": true, "excludes": true, "excludesall": true, "unique": true,
	"required_if": true, "required_unless": true, "required_with": true,
	"required_with_all": true, "required_without": true, "required_without_all": true,
}

// DTOResult is what the validation step did.
type DTOResult struct {
	// Moved is how many types moved into handler/request.
	Moved int `json:"moved"`
	// Generated are the request types govalid validates.
	Generated []string `json:"generated,omitempty"`
	// Runtime are the request types whose Validate() runs the legacy runtime
	// validator, with the tags that put them there.
	Runtime map[string][]string `json:"runtime,omitempty"`
	// Already had a Validate() and were left alone.
	Already []string `json:"already,omitempty"`
	Written []string `json:"written,omitempty"`
	// Removed are files deleted: validators regenerated in the new package.
	Removed []string `json:"removed,omitempty"`
	Notes   []string `json:"notes,omitempty"`
	DryRun  bool     `json:"dry_run"`
}

const runtimeFile = "request_validation.go"

// DTOs moves the request types to handler/request and gives each a Validate().
func DTOs(ws *workspace.Workspace, write bool) (*DTOResult, error) {
	res := &DTOResult{DryRun: !write, Runtime: map[string][]string{}}
	if _, err := moveDTOs(ws, write, res); err != nil {
		return nil, err
	}
	if write && res.Moved > 0 {
		reloaded, err := workspace.Load(ws.Root)
		if err != nil {
			return nil, err
		}
		ws = reloaded
	}
	files, _ := goFiles(ws)

	// The DTOs' package: handler/request once they moved, else handler.
	dtoRel := requestDir
	if _, err := os.Stat(filepath.Join(ws.Root, filepath.FromSlash(requestDir), "request.go")); err != nil {
		dtoRel = "handler"
	}
	var dtoFiles []*workspace.File
	for _, f := range files {
		if filepath.ToSlash(filepath.Dir(f.Rel)) == dtoRel {
			dtoFiles = append(dtoFiles, f)
		}
	}
	handlerDir := filepath.Join(ws.Root, filepath.FromSlash(dtoRel))

	structs := map[string]*ast.StructType{}
	hasValidate := map[string]bool{}
	slices := map[string]string{}
	for _, f := range dtoFiles {
		for _, d := range f.AST.Decls {
			switch x := d.(type) {
			case *ast.GenDecl:
				for _, sp := range x.Specs {
					ts, ok := sp.(*ast.TypeSpec)
					if !ok {
						continue
					}
					switch t := ts.Type.(type) {
					case *ast.StructType:
						structs[ts.Name.Name] = t
					case *ast.ArrayType:
						if id, ok := t.Elt.(*ast.Ident); ok {
							slices[ts.Name.Name] = id.Name
						}
					}
				}
			case *ast.FuncDecl:
				if x.Name.Name == "Validate" && x.Recv != nil && !strings.HasSuffix(f.Rel, "_validator.go") && filepath.Base(f.Rel) != runtimeFile {
					hasValidate[recvName(x)] = true
				}
			}
		}
	}
	if len(dtoFiles) == 0 {
		res.Notes = append(res.Notes, "no handler package found")
		return res, nil
	}

	// The request types: every request-named struct, and the element of every
	// request-named slice (its Validate checks the elements).
	requests := map[string]bool{}
	for n := range structs {
		if isRequestName(n) {
			requests[n] = true
		}
	}
	// ...and whatever a converted handler binds, named like a request or not.
	for _, f := range files {
		if f.Layer != workspace.LayerHandler {
			continue
		}
		for _, d := range f.AST.Decls {
			fd, ok := d.(*ast.FuncDecl)
			if !ok || fd.Recv == nil || !isTemplateHandler(fd) || len(fd.Type.Params.List) < 2 {
				continue
			}
			switch t := fd.Type.Params.List[1].Type.(type) {
			case *ast.Ident:
				requests[t.Name] = true
			case *ast.SelectorExpr:
				requests[t.Sel.Name] = true
			}
		}
	}
	for n, elem := range slices {
		if isRequestName(n) || requests[n] {
			requests[elem] = true
		}
	}
	names := make([]string, 0, len(requests))
	for n := range requests {
		if _, ok := structs[n]; ok {
			names = append(names, n)
		}
	}
	sort.Strings(names)

	var adapters, untagged []string
	runtimeTypes := map[string]bool{}
	for _, n := range names {
		if hasValidate[n] {
			res.Already = append(res.Already, n)
			continue
		}
		if unknown := unknownTags(structs, n, map[string]bool{}); len(unknown) > 0 {
			res.Runtime[n] = unknown
			runtimeTypes[n] = true
			adapters = append(adapters, n)
		} else {
			res.Generated = append(res.Generated, n)
		}
	}

	if !write {
		// What a real run would write, named the way govalid names its files.
		for _, n := range res.Generated {
			res.Written = append(res.Written, fmt.Sprintf("%s/%s_%s_validator.go", requestDir, "request", strings.ToLower(n)))
		}
		res.Written = append(res.Written, requestDir+"/"+runtimeFile)
		sort.Strings(res.Written)
		return res, nil
	}

	// govalid over the whole package: one file at a time it cannot type-check.
	if len(res.Generated) > 0 {
		if _, err := exec.LookPath("govalid"); err != nil {
			res.Notes = append(res.Notes, "govalid is not installed: "+strings.Join(res.Generated, ", ")+
				" get the runtime validator instead (go install gitlab.cept.gov.in/it-2.0-common/n-api-validation/cmd/govalid)")
			for _, n := range res.Generated {
				runtimeTypes[n] = true
				adapters = append(adapters, n)
			}
			res.Generated = nil
		} else {
			before := validatorFiles(handlerDir)
			target := "."
			if dtoRel == requestDir {
				target = "./request.go"
			}
			cmd := exec.Command("govalid", target)
			cmd.Dir = handlerDir
			out, err := cmd.CombinedOutput()
			if err != nil {
				res.Notes = append(res.Notes, fmt.Sprintf("govalid failed (%v): %s", err, firstLine(string(out))))
			}
			// Keep only what this run created and a request type needs.
			for name, typ := range validatorFiles(handlerDir) {
				if _, existed := before[name]; existed {
					continue
				}
				// Nested element types too: the gate regenerates them otherwise.
				keep := !runtimeTypes[typ] && !hasValidate[typ]
				if !keep {
					_ = os.Remove(filepath.Join(handlerDir, name))
					continue
				}
				res.Written = append(res.Written, dtoRel+"/"+name)
			}
			// govalid writes nothing for a struct with no validate tags, and
			// n-api-server then refuses every non-GET call to it with a 422.
			validated := map[string]bool{}
			for _, typ := range validatorFiles(handlerDir) {
				validated[typ] = true
			}
			for _, n := range res.Generated {
				if !validated[n] {
					untagged = append(untagged, n)
				}
			}
		}
	}

	if len(adapters) > 0 || len(untagged) > 0 {
		sort.Strings(adapters)
		sort.Strings(untagged)
		path := filepath.Join(handlerDir, runtimeFile)
		var b strings.Builder
		b.WriteString("package " + dtoFiles[0].AST.Name.Name + "\n\n")
		b.WriteString("import validation \"gitlab.cept.gov.in/it-2.0-common/api-validation\"\n\n")
		b.WriteString("// The request types below carry validate tags govalid does not implement --\n")
		b.WriteString("// this service's own rules and the legacy library's -- so their Validate()\n")
		b.WriteString("// runs the runtime validator the legacy handlers ran, with the same rules\n")
		b.WriteString("// registered at startup (bootstrap.Fxvalidator). Written by the migration;\n")
		b.WriteString("// move a type to govalid by replacing those tags with govalid's markers.\n")
		// The whole file, every time. It used to skip the adapters already in
		// it and then overwrite it with the rest -- so a second run wrote the
		// header alone, and live run 2026-10-14 was down to 4 of 36 runtime
		// validators after one more session: every custom-tag DTO behind a
		// POST answered 422. The list is recomputed in full on each run.
		for _, n := range adapters {
			fmt.Fprintf(&b, "\n// Validate checks %s against its tags (%s).\nfunc (t *%s) Validate() error { return validation.ValidateStruct(t) }\n",
				n, strings.Join(res.Runtime[n], ", "), n)
		}
		for _, n := range untagged {
			fmt.Fprintf(&b, "\n// Validate: %s has no validate tags, so there is nothing to check -- but\n// n-api-server refuses a non-GET request whose type has no Validate().\nfunc (t *%s) Validate() error { return nil }\n", n, n)
		}
		if len(adapters) == 0 {
			// No runtime-validated type: the import would be unused.
			text := strings.Replace(b.String(), "import validation \"gitlab.cept.gov.in/it-2.0-common/api-validation\"\n\n", "", 1)
			b.Reset()
			b.WriteString(text)
		}
		src, err := gopatch.Format([]byte(b.String()))
		if err != nil {
			return nil, err
		}
		// Unchanged is not written: every session re-runs this step, and a
		// rewrite reported as a change made each one look like progress.
		if existing, err := os.ReadFile(path); err != nil || !bytes.Equal(existing, src) {
			if err := os.WriteFile(path, src, 0o644); err != nil {
				return nil, err
			}
			res.Written = append(res.Written, dtoRel+"/"+runtimeFile)
		}
	}
	sort.Strings(res.Written)
	return res, nil
}

func filesPackage(files []*workspace.File) string {
	for _, f := range files {
		if f.Layer == workspace.LayerHandler {
			return f.AST.Name.Name
		}
	}
	return "handler"
}

// sliceElem is E for `type name []E` in the handler package.
func sliceElem(files []*workspace.File, name string) string {
	for _, f := range files {
		if f.Layer != workspace.LayerHandler {
			continue
		}
		for _, d := range f.AST.Decls {
			g, ok := d.(*ast.GenDecl)
			if !ok || g.Tok != token.TYPE {
				continue
			}
			for _, s := range g.Specs {
				ts, ok := s.(*ast.TypeSpec)
				if !ok || ts.Name.Name != name {
					continue
				}
				if at, ok := ts.Type.(*ast.ArrayType); ok {
					if id, ok := at.Elt.(*ast.Ident); ok {
						return id.Name
					}
				}
			}
		}
	}
	return ""
}

// unknownTags lists the validate-tag rules on name (and the handler-package
// structs it embeds or holds) that govalid does not implement.
func unknownTags(structs map[string]*ast.StructType, name string, seen map[string]bool) []string {
	if seen[name] {
		return nil
	}
	seen[name] = true
	st := structs[name]
	if st == nil {
		return nil
	}
	set := map[string]bool{}
	for _, field := range st.Fields.List {
		if field.Tag != nil {
			raw, _ := strconv.Unquote(field.Tag.Value)
			for _, rule := range strings.Split(reflect.StructTag(raw).Get("validate"), ",") {
				rule = strings.TrimSpace(rule)
				if rule == "" {
					continue
				}
				key, _, _ := strings.Cut(rule, "=")
				if !govalidTags[key] {
					set[key] = true
				}
			}
		}
		// A nested request struct's rules count too: dive runs them.
		t := field.Type
		for {
			switch x := t.(type) {
			case *ast.StarExpr:
				t = x.X
				continue
			case *ast.ArrayType:
				t = x.Elt
				continue
			}
			break
		}
		if id, ok := t.(*ast.Ident); ok {
			for _, k := range unknownTags(structs, id.Name, seen) {
				set[k] = true
			}
		}
	}
	out := make([]string, 0, len(set))
	for k := range set {
		out = append(out, k)
	}
	sort.Strings(out)
	return out
}

// validatorFiles maps each *_validator.go in dir to the type its Validate() is on.
func validatorFiles(dir string) map[string]string {
	out := map[string]string{}
	entries, _ := os.ReadDir(dir)
	for _, e := range entries {
		if !strings.HasSuffix(e.Name(), "_validator.go") {
			continue
		}
		b, err := os.ReadFile(filepath.Join(dir, e.Name()))
		if err != nil {
			continue
		}
		i := bytes.Index(b, []byte(") Validate() error"))
		if i < 0 {
			continue
		}
		j := bytes.LastIndex(b[:i], []byte("(t *"))
		if j < 0 {
			continue
		}
		out[e.Name()] = string(b[j+4 : i])
	}
	return out
}

func firstLine(s string) string {
	if i := strings.IndexByte(s, '\n'); i >= 0 {
		return s[:i]
	}
	return s
}
