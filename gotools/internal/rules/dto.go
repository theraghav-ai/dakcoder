package rules

import (
	"go/ast"
	"strings"

	"gitlab.cept.gov.in/it-2.0/dakcoder/gotools/internal/workspace"
)

const requestFile = "handler/request.go"

// RequestDTO enforces where request structs live and that they are validatable.
//
// The location requirement is not bureaucracy: govalid is run as
// `govalid ./request.go` from the handler directory, so a request struct in any
// other file simply never gets a generated validator — and the failure is
// silent. Input reaches the handler unvalidated.
//
// Note there is deliberately no ToDomain() requirement. The v1 plan asserted one;
// the reference template has no such method anywhere, and handlers pass fields
// positionally to the repository. Rules describe the template as it is.
var RequestDTO = Rule{
	ID:       "request-dto",
	Severity: SeverityError,
	Summary:  "request structs live in handler/request.go and carry validate tags",
	Citation: "SOP.md §Validation; skill.md §Request DTO Pattern",
	Check: func(p *Pass) {
		for _, f := range p.WS.FilesIn(workspace.LayerHandler) {
			structsIn(f, func(name string, ts *ast.TypeSpec, st *ast.StructType) {
				if !isRequestStruct(name) {
					return
				}
				if f.Rel != requestFile {
					p.At(f, ts).
						Fix("move %s into %s — govalid only generates validators for structs in that file", name, requestFile).
						Report("request struct %s is declared in %s; it will have no generated validator", name, f.Rel)
					return
				}
				for _, fld := range st.Fields.List {
					if isEmbedded(fld) {
						continue // e.g. port.MetadataRequest carries its own tags
					}
					tag := tagOf(fld)
					if _, ok := tag.Lookup("validate"); !ok {
						p.At(f, fld).
							Fix(`add validate:"required" (or "omitempty") to %s.%s`, name, fieldName(fld)).
							Report("%s.%s has no validate tag; it will not be validated", name, fieldName(fld))
					}
					// A field must be bindable from somewhere.
					if !hasAnyTag(tag, "json", "uri", "form") {
						p.At(f, fld).
							Fix(`add a json:, uri: or form: tag to %s.%s`, name, fieldName(fld)).
							Report("%s.%s has no json/uri/form tag; it can never be populated", name, fieldName(fld))
					}
				}
			})
		}
	},
}

func isRequestStruct(name string) bool {
	return strings.HasSuffix(name, "Request") || strings.HasSuffix(name, "Uri") ||
		strings.HasSuffix(name, "Params") || strings.HasSuffix(name, "Req")
}

func hasAnyTag(tag interface{ Lookup(string) (string, bool) }, keys ...string) bool {
	for _, k := range keys {
		if _, ok := tag.Lookup(k); ok {
			return true
		}
	}
	return false
}

// responseKinds are the operation envelopes every resource declares.
var responseKinds = []string{"Create", "Fetch", "List", "Update", "Delete"}

// ResponseDTO enforces the response envelope.
//
// `json:",inline"` is the subtle one: without it the embedded struct is
// marshalled as a nested object, so `status_code` and `message` end up under a
// key instead of at the top level, and every client breaks.
var ResponseDTO = Rule{
	ID:       "response-dto",
	Severity: SeverityError,
	Summary:  "response envelopes embed port.StatusCodeAndMessage with json:\",inline\"; lists also embed port.MetaDataResponse",
	Citation: "skill.md §Response DTO Pattern",
	Check: func(p *Pass) {
		for _, f := range p.WS.FilesIn(workspace.LayerResponse) {
			structsIn(f, func(name string, ts *ast.TypeSpec, st *ast.StructType) {
				kind, ok := envelopeKind(name)
				if !ok {
					return
				}
				var hasStatus, statusInline, hasMeta, metaInline bool
				for _, fld := range st.Fields.List {
					if !isEmbedded(fld) {
						continue
					}
					inline := false
					if v, ok := tagOf(fld).Lookup("json"); ok {
						inline = strings.Contains(v, "inline")
					}
					switch typeString(fld.Type) {
					case "port.StatusCodeAndMessage":
						hasStatus, statusInline = true, inline
					case "port.MetaDataResponse":
						hasMeta, metaInline = true, inline
					}
				}
				switch {
				case !hasStatus:
					p.At(f, ts).
						Fix("embed port.StatusCodeAndMessage `json:\",inline\"` as the first field").
						Report("%s does not embed port.StatusCodeAndMessage", name)
				case !statusInline:
					p.At(f, ts).
						Fix("tag the embed as `json:\",inline\"`").
						Report("%s embeds port.StatusCodeAndMessage without json:\",inline\"; status_code and message would be nested", name)
				}
				if kind == "List" {
					switch {
					case !hasMeta:
						p.At(f, ts).
							Fix("embed port.MetaDataResponse `json:\",inline\"` for pagination metadata").
							Report("list response %s does not embed port.MetaDataResponse", name)
					case !metaInline:
						p.At(f, ts).
							Fix("tag the embed as `json:\",inline\"`").
							Report("%s embeds port.MetaDataResponse without json:\",inline\"", name)
					}
				}
			})

			// Converters: a FooResponse should have a NewFooResponse.
			declared := map[string]*ast.TypeSpec{}
			structsIn(f, func(name string, ts *ast.TypeSpec, _ *ast.StructType) {
				if _, isEnvelope := envelopeKind(name); !isEnvelope && strings.HasSuffix(name, "Response") {
					declared[name] = ts
				}
			})
			ctors := map[string]bool{}
			funcsIn(f, func(fd *ast.FuncDecl) {
				if _, isMethod := receiverType(fd); !isMethod && strings.HasPrefix(fd.Name.Name, "New") {
					ctors[fd.Name.Name] = true
				}
			})
			for name, ts := range declared {
				if !ctors["New"+name] {
					p.At(f, ts).
						Fix("add func New%s(d domain.X) %s to convert from the domain model", name, name).
						Report("%s has no New%s converter; conversion would leak into the handler", name, name)
				}
			}
		}
	},
}

// envelopeKind reports whether a type name is an operation envelope
// (PensionCreateResponse -> "Create").
func envelopeKind(name string) (string, bool) {
	if !strings.HasSuffix(name, "Response") {
		return "", false
	}
	stem := strings.TrimSuffix(name, "Response")
	for _, k := range responseKinds {
		if strings.HasSuffix(stem, k) {
			return k, true
		}
	}
	return "", false
}

// ResponseStatus requires the predefined status constants rather than
// hand-written literals, so status codes and messages stay consistent across
// every service.
var ResponseStatus = Rule{
	ID:       "response-status",
	Severity: SeverityWarning,
	Summary:  "handlers set status from the predefined port.*Success constants",
	Citation: "core/port/response.go; skill.md §Response DTO Pattern",
	Check: func(p *Pass) {
		valid := map[string]bool{
			"port.CreateSuccess": true, "port.ListSuccess": true, "port.FetchSuccess": true,
			"port.UpdateSuccess": true, "port.DeleteSuccess": true, "port.OTPSuccess": true,
			"port.OTPAuthSuccess": true, "port.CustomEnv": true,
		}
		for _, f := range p.WS.FilesIn(workspace.LayerHandler) {
			ast.Inspect(f.AST, func(n ast.Node) bool {
				kv, ok := n.(*ast.KeyValueExpr)
				if !ok {
					return true
				}
				key, ok := kv.Key.(*ast.Ident)
				if !ok || key.Name != "StatusCodeAndMessage" {
					return true
				}
				val := typeString(kv.Value)
				if valid[val] {
					return true
				}
				// A composite literal means a hand-rolled status.
				if _, isLit := kv.Value.(*ast.CompositeLit); isLit || !strings.HasPrefix(val, "port.") {
					p.At(f, kv).
						Fix("use port.CreateSuccess / ListSuccess / FetchSuccess / UpdateSuccess / DeleteSuccess").
						Report("StatusCodeAndMessage set to a non-standard value; use the predefined port constants")
				}
				return true
			})
		}
	},
}
