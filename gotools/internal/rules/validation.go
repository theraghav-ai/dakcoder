package rules

import (
	"go/ast"
	"strings"

	"gitlab.cept.gov.in/it-2.0/dakcoder/gotools/internal/workspace"
)

// RequestValidateDepth is the most systemic finding in the whole review:
// numeric bounds were asked for in 39 of the 41 services, string constraints in
// 34. Both sheets are titled "Validations to be added", and both list a struct
// name and a field name for every field that had a validate tag with nothing
// useful in it.
//
// The existing request-dto rule requires *a* validate tag, which is exactly the
// baseline suggestion #1 asks for and is already enforced. This rule is about
// depth: `validate:"required"` on a free-text field means "not empty" and
// nothing else, so a 10MB string is valid input, and an unbounded integer
// reaches the database to be rejected there — or not.
//
// A warning, not an error, for two reasons. The right bound is a domain
// question the linter cannot answer, and the reference template itself would
// fail this on its own request DTOs — which under the project's own rule
// ("if a rule fires on the template, the rule is wrong") would make it a
// blocking rule that is wrong by construction. Warning is the honest severity
// until the template's own DTOs carry bounds.
var RequestValidateDepth = Rule{
	ID:       "request-validate-depth",
	Severity: SeverityWarning,
	Summary:  "validate tags bound their fields: max/len on strings, min/max on numbers",
	Citation: "docs/CODE-REVIEW-FINDINGS.md; references/request-dto.md",
	Check: func(p *Pass) {
		for _, f := range p.WS.FilesIn(workspace.LayerHandler) {
			if f.Rel != requestFile {
				continue
			}
			structsIn(f, func(name string, _ *ast.TypeSpec, st *ast.StructType) {
				if !isRequestStruct(name) {
					return
				}
				for _, fld := range st.Fields.List {
					if isEmbedded(fld) {
						continue
					}
					tag, ok := tagOf(fld).Lookup("validate")
					if !ok {
						continue // request-dto reports the missing tag itself
					}
					rules := validateRules(tag)
					// A field that is explicitly not required and not bound is
					// a deliberate opt-out, and #1 says omitempty alone is an
					// acceptable floor.
					if rules["omitempty"] && len(rules) == 1 {
						continue
					}
					kind, want := boundFor(fld.Type)
					if kind == "" || hasAny(rules, want...) {
						continue
					}
					p.At(f, fld).
						Fix("add a bound, e.g. validate:%q", suggestedTag(tag, kind)).
						Report("%s.%s is a %s validated only as %q; nothing bounds its size",
							name, fieldName(fld), kind, tag)
				}
			})
		}
	},
}

// validateRules splits a validate tag into its comma-separated directives,
// dropping any parameter so `max=64` indexes as `max`.
func validateRules(tag string) map[string]bool {
	out := map[string]bool{}
	for _, part := range strings.Split(tag, ",") {
		part = strings.TrimSpace(part)
		if part == "" {
			continue
		}
		key, _, _ := strings.Cut(part, "=")
		out[strings.TrimSpace(key)] = true
	}
	return out
}

func hasAny(rules map[string]bool, keys ...string) bool {
	for _, k := range keys {
		if rules[k] {
			return true
		}
	}
	return false
}

// stringBounds are directives that constrain a string's size or shape.
var stringBounds = []string{
	"max", "len", "oneof", "eq", "email", "url", "uuid", "uuid4", "e164",
	"alpha", "alphanum", "numeric", "datetime", "iso3166_1_alpha2", "contains", "startswith",
}

// numericBounds are directives that constrain a number's range.
var numericBounds = []string{"min", "max", "gte", "lte", "gt", "lt", "eq", "oneof", "len"}

// boundFor reports which family of bounds a field type needs, and the
// directives that would satisfy it. Types that carry their own bounds — bool,
// time.Time, nested structs — return "" and are skipped.
func boundFor(e ast.Expr) (kind string, want []string) {
	t := typeString(e)
	t = strings.TrimPrefix(t, "*")
	switch {
	case t == "string":
		return "string", stringBounds
	case strings.HasPrefix(t, "int"), strings.HasPrefix(t, "uint"),
		strings.HasPrefix(t, "float"):
		return "number", numericBounds
	case strings.HasPrefix(t, "[]"):
		// A slice with no max is an unbounded request body.
		return "slice", []string{"max", "len", "dive"}
	}
	return "", nil
}

// suggestedTag returns the existing tag with a plausible bound appended, so the
// fix string is something the developer can paste and then adjust.
func suggestedTag(tag, kind string) string {
	switch kind {
	case "string":
		return tag + ",max=255"
	case "number":
		return tag + ",min=0,max=999999"
	default:
		return tag + ",max=100"
	}
}

// ConfigNoHardcode reports environment-dependent values written into code.
//
// A URL or a port in a handler is a value that differs between dev, UAT and
// production and can only be changed by a rebuild. The template already has the
// mechanism — configs/*.yaml plus cfg.Get* — and config-key-exists already
// checks the reverse direction, that every key read is declared. This is the
// direction nothing checked: values that never became keys at all.
var ConfigNoHardcode = Rule{
	ID:       "config-no-hardcode",
	Severity: SeverityWarning,
	Summary:  "hosts, URLs and ports come from configs/*.yaml, not from string literals",
	Citation: "docs/CODE-REVIEW-FINDINGS.md",
	Check: func(p *Pass) {
		for _, f := range p.WS.Files {
			switch f.Layer {
			// bootstrap and main legitimately hold wiring defaults; tests hold
			// fixtures; configs are the answer, not the problem.
			case workspace.LayerBootstrap, workspace.LayerMain, workspace.LayerTest:
				continue
			}
			for _, bl := range stringLitsIn(f.AST) {
				v := litValue(bl)
				what := hardcodedKind(v)
				if what == "" {
					continue
				}
				p.At(f, bl).
					Fix(`declare it in configs/*.yaml with a default and read it via cfg.GetString("…")`).
					Report("%s %q is hard-coded; it cannot differ between environments without a rebuild", what, v)
			}
		}
	},
}

// hardcodedKind classifies a literal that should have been configuration.
//
// Narrow on purpose. It looks for the two shapes that are unambiguous — an
// absolute URL and a host:port — and ignores paths, formats and everything else
// where a literal is perfectly reasonable.
func hardcodedKind(v string) string {
	switch {
	case strings.HasPrefix(v, "http://"), strings.HasPrefix(v, "https://"):
		// localhost is a developer default, not a deployed endpoint.
		if strings.Contains(v, "localhost") || strings.Contains(v, "127.0.0.1") {
			return ""
		}
		return "endpoint"
	case looksLikeHostPort(v):
		return "host:port"
	}
	return ""
}

// looksLikeHostPort reports a `host:port` literal with a dotted host, which is
// a deployment target rather than a format string.
func looksLikeHostPort(v string) bool {
	host, port, ok := strings.Cut(v, ":")
	if !ok || host == "" || port == "" || len(port) > 5 {
		return false
	}
	if !strings.Contains(host, ".") || strings.Contains(host, " ") {
		return false
	}
	for _, r := range port {
		if r < '0' || r > '9' {
			return false
		}
	}
	return true
}
