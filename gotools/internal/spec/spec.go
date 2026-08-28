// Package spec defines the resource specification the scaffolders consume, and
// — more importantly — validates it.
//
// # Why validation is the point of this package
//
// The division of labour in resource_scaffold is that the LLM produces the
// *spec* and text/template produces the *code*. That is what makes the output
// deterministic and byte-comparable against a golden snapshot. It is also what
// concentrates all the risk here: the spec is model output, so it is untrusted
// input in both senses — it can be wrong, and it can be hostile.
//
// Wrong, from the pre-implementation spike: asked for a Pension resource, the
// model emitted `"type": "decimal.Decimal"` — a dependency that is not on the
// allow-list and would not compile — and `PpoNumber` instead of `PPONumber`.
// Neither is a model failure worth prompting around; both are mechanically
// preventable here, once.
//
// Hostile, because every string in a spec is interpolated into generated Go and
// SQL. A field type of "string `json:\"x\"` // " would close the tag literal
// and inject arbitrary source. So every field that reaches a template is
// constrained to an explicit character class, and the type must be one of a
// closed set — never merely "not obviously bad".
//
// Validate returns every problem at once, each with a fix, so the agent can
// correct a spec in one turn instead of discovering the faults one at a time.
package spec

import (
	"fmt"
	"regexp"
	"sort"
	"strings"

	"gitlab.cept.gov.in/it-2.0/dakcoder/gotools/internal/naming"
)

// defaultValidate is the validate tag a field gets when the spec does not name
// one.
//
// It bounds the field rather than merely requiring it. `required` alone means
// "not empty" and nothing else, so a 10MB string is valid input — and that was
// the most systemic finding in the review of 41 production services: numeric
// bounds asked for in 39 of them, string bounds in 34. Emitting bare `required`
// here would scaffold the defect the review spent the most time on.
//
// The numbers are starting points a developer narrows, not guesses at the
// domain. 255 is a conventional varchar bound; the numeric ceilings are wide
// enough not to reject real data and narrow enough to reject nonsense. A spec
// that names its own tag always wins.
func defaultValidate(goType string) string {
	switch goType {
	case "string":
		return "required,max=255"
	case "int", "int64":
		return "required,min=0,max=2147483647"
	case "float64":
		return "required,min=0"
	default:
		// bool has two values and time.Time parses or does not; neither takes
		// a bound that means anything.
		return "required"
	}
}

// Resource is the specification of one CRUD resource.
type Resource struct {
	// Name is the singular Go type name, e.g. "Pension". Normalised to
	// PascalCase with initialisms applied.
	Name string `json:"name" jsonschema:"singular resource name in PascalCase, e.g. Pension"`

	// Plural is the plural form used for the table, routes and list types.
	// Inferred from Name when empty.
	Plural string `json:"plural,omitempty" jsonschema:"plural form; inferred from name when omitted"`

	// Table is the Postgres table name. Inferred as snake_case(Plural).
	Table string `json:"table,omitempty" jsonschema:"postgres table name; inferred from the plural when omitted"`

	// RouteBase is the URL segment under the /v1 prefix, e.g. "/pensions".
	RouteBase string `json:"route_base,omitempty" jsonschema:"route base under /v1, e.g. /pensions; inferred when omitted"`

	// Fields are the resource's own columns. ID, CreatedAt and UpdatedAt are
	// always added by the scaffolder and must not appear here.
	Fields []Field `json:"fields" jsonschema:"the resource's own columns; do NOT include id, created_at or updated_at"`

	// Operations selects the CRUD subset. Empty means all five.
	Operations []string `json:"operations,omitempty" jsonschema:"subset of create, list, get, update, delete; omit for all five"`

	// ListFilters add optional query-string filters to the list endpoint. Each
	// must name a declared field.
	ListFilters []Filter `json:"list_filters,omitempty" jsonschema:"optional query-string filters on the list route; each must name a declared field"`

	// Paginate switches the list route to a bound request struct carrying
	// port.MetadataRequest, so skip and limit reach the query.
	Paginate bool `json:"paginate,omitempty" jsonschema:"true to accept skip/limit on the list route"`
}

// Field is one resource column.
type Field struct {
	Go       string `json:"go" jsonschema:"Go field name in PascalCase, e.g. PPONumber"`
	JSON     string `json:"json,omitempty" jsonschema:"json tag; inferred as snake_case when omitted"`
	DB       string `json:"db,omitempty" jsonschema:"db column; inferred as snake_case when omitted"`
	Type     string `json:"type" jsonschema:"one of string, int, int64, float64, bool, time.Time"`
	Validate string `json:"validate,omitempty" jsonschema:"go-playground validate tag, e.g. required or oneof=active closed"`
	SQL      string `json:"sql,omitempty" jsonschema:"postgres column type; inferred from the Go type when omitted"`
}

// Filter is one list-route query parameter.
type Filter struct {
	Go   string `json:"go" jsonschema:"the declared field to filter on"`
	Form string `json:"form,omitempty" jsonschema:"query-string parameter name; inferred as snake_case when omitted"`

	// Type and DB are copied from the field the filter names, during
	// normalisation. They are not part of the input contract — a caller cannot
	// set them to something the field disagrees with — so they stay out of the
	// JSON and out of the tool schema.
	Type string `json:"-"`
	DB   string `json:"-"`
}

// ── the closed type set ─────────────────────────────────────────────────────

// GoType describes one permitted field type.
//
// The set is closed on purpose. An open set — "any type that parses" — is what
// lets `decimal.Decimal` through, and a scaffolder that emits an import the
// module does not have produces a repository that cannot compile, at which
// point every downstream verification stage is reporting the same one mistake.
type GoType struct {
	// Name is the type as written in Go source.
	Name string
	// Import is the package the type needs, or "" for a builtin.
	Import string
	// SQL is the default Postgres column type.
	SQL string
	// zeroTest renders the "was this optional field supplied?" condition used
	// by the update handler, given a selector like "req.Amount".
	zeroTest func(sel string) string
	// AlwaysSet marks types whose zero value is indistinguishable from "not
	// supplied", so a partial update always writes them.
	AlwaysSet bool
}

// ZeroTest renders the presence condition for an optional update field.
func (t GoType) ZeroTest(sel string) string {
	if t.zeroTest == nil {
		return ""
	}
	return t.zeroTest(sel)
}

var goTypes = map[string]GoType{
	"string": {
		Name: "string", SQL: "varchar(255) NOT NULL",
		zeroTest: func(s string) string { return s + ` != ""` },
	},
	"int": {
		Name: "int", SQL: "int4 NOT NULL",
		zeroTest: func(s string) string { return s + " != 0" },
	},
	"int64": {
		Name: "int64", SQL: "int8 NOT NULL",
		zeroTest: func(s string) string { return s + " != 0" },
	},
	"float64": {
		Name: "float64", SQL: "numeric(12, 2) NOT NULL",
		zeroTest: func(s string) string { return s + " != 0" },
	},
	"bool": {
		// A false bool is indistinguishable from an absent one in a value-typed
		// request DTO, so a partial update always writes it. The alternative —
		// a *bool in the DTO — changes what govalid generates, and the template
		// has no precedent for it.
		Name: "bool", SQL: "bool NOT NULL DEFAULT false", AlwaysSet: true,
	},
	"time.Time": {
		Name: "time.Time", Import: "time", SQL: "timestamp NOT NULL",
		zeroTest: func(s string) string { return "!" + s + ".IsZero()" },
	},
}

// typeAliases map common spellings and near-misses onto the closed set. The
// value is the replacement; an empty value means "rejected, no substitute".
var typeAliases = map[string]string{
	"integer": "int", "int32": "int", "uint": "int64", "uint64": "int64",
	"float": "float64", "float32": "float64", "double": "float64",
	"number": "float64", "decimal": "float64",
	"boolean": "bool", "text": "string", "varchar": "string", "str": "string",
	"date": "time.Time", "datetime": "time.Time", "timestamp": "time.Time",
	"time": "time.Time",
}

// rejectedTypes name the substitute explicitly, because "not allowed" without
// an alternative costs the agent a turn guessing.
var rejectedTypes = map[string]string{
	"decimal.Decimal":  "float64 (or string when exact decimal arithmetic is required)",
	"shopspring":       "float64",
	"uuid.UUID":        "string",
	"json.RawMessage":  "string",
	"map[string]any":   "string (store JSON as text, or model it as a related resource)",
	"any":              "an explicit type",
	"interface{}":      "an explicit type",
	"[]byte":           "string (file payloads use *multipart.FileHeader — see SOP.md §Handler with file upload)",
	"null.String":      "string",
	"sql.NullString":   "string",
	"pgtype.Timestamp": "time.Time",
}

// Type resolves a spec type name to its Go type.
func Type(name string) (GoType, bool) {
	t, ok := goTypes[name]
	return t, ok
}

// Types lists the permitted type names in a stable order, for error messages
// and for the tool schema description.
func Types() []string {
	out := make([]string, 0, len(goTypes))
	for k := range goTypes {
		out = append(out, k)
	}
	sort.Strings(out)
	return out
}

// ── validation ──────────────────────────────────────────────────────────────

// Issue is one problem with a spec.
type Issue struct {
	Path    string `json:"path"`          // e.g. "fields[2].type"
	Message string `json:"message"`       // what is wrong
	Fix     string `json:"fix,omitempty"` // what to do instead
}

// String renders an issue as "path: message (fix: ...)".
func (i Issue) String() string {
	s := i.Path + ": " + i.Message
	if i.Fix != "" {
		s += " (fix: " + i.Fix + ")"
	}
	return s
}

// InvalidSpecError carries every problem found, so one turn corrects them all.
type InvalidSpecError struct{ Issues []Issue }

// Error lists every problem, so one turn can correct them all.
func (e *InvalidSpecError) Error() string {
	parts := make([]string, 0, len(e.Issues))
	for _, i := range e.Issues {
		parts = append(parts, i.String())
	}
	return fmt.Sprintf("invalid resource spec (%d problem(s)):\n  %s",
		len(e.Issues), strings.Join(parts, "\n  "))
}

var (
	// identRe is the only shape allowed for anything that becomes a Go
	// identifier. No underscores: the template's own convention is PascalCase,
	// and permitting them invites `Foo_Bar` fields that the linter then flags.
	identRe = regexp.MustCompile(`^[A-Za-z][A-Za-z0-9]*$`)

	// tagRe is the character class for a struct-tag value. It deliberately
	// excludes the quote, backtick and backslash characters that would let a
	// value escape the tag literal and inject source.
	tagRe = regexp.MustCompile(`^[A-Za-z0-9_,=|.:+ '\-]*$`)

	// snakeRe constrains json tags, db columns and table names.
	snakeRe = regexp.MustCompile(`^[a-z][a-z0-9_]*$`)

	// sqlTypeRe constrains a column type. No semicolon and no comment marker:
	// the value is written straight into a .sql file, so a statement separator
	// there is an injection into the DDL a developer is about to run as a
	// database superuser.
	sqlTypeRe = regexp.MustCompile(`^[A-Za-z0-9_ ,()'.\-]+$`)

	// routeRe constrains the route base.
	routeRe = regexp.MustCompile(`^/[a-z0-9]+(?:[-/][a-z0-9]+)*$`)
)

// reservedFields are supplied by the scaffolder itself.
var reservedFields = map[string]bool{"ID": true, "CreatedAt": true, "UpdatedAt": true}

// reservedIdents are the import aliases and local variable names the generated
// files use. A resource whose camel-case form lands on one of them would shadow
// it inside its own methods.
var reservedIdents = map[string]bool{
	// Import aliases.
	"resp": true, "repo": true, "port": true, "log": true, "domain": true,
	"dblib": true, "config": true, "sq": true, "pgx": true, "handler": true,
	"response": true, "bootstrap": true, "time": true, "context": true,
	"serverHandler": true, "serverRoute": true, "fx": true,
	// Locals in the generated methods.
	"ctx": true, "sctx": true, "req": true, "err": true, "base": true,
	"data": true, "md": true, "ins": true, "del": true, "svc": true,
	"commandTag": true, "skip": true, "limit": true, "id": true, "res": true,
}

// validOperations is the closed operation set.
var validOperations = []string{"create", "list", "get", "update", "delete"}

// Normalise fills in every inferred value and canonicalises names, then
// validates. It returns a corrected copy; the receiver is not modified.
//
// Normalisation runs *before* validation on purpose: `ppo_number` is a
// perfectly good thing for a model to emit for a field name, and rejecting it
// when we can correct it deterministically would be pedantry that costs a turn.
// What validation then rejects is only what cannot be corrected without
// guessing.
func (r Resource) Normalise() (Resource, error) {
	out := r
	var issues []Issue

	out.Name = naming.Pascal(strings.TrimSpace(r.Name))
	switch {
	case out.Name == "":
		issues = append(issues, Issue{"name", "resource name is empty",
			`supply a singular PascalCase name, e.g. "Pension"`})
	case !identRe.MatchString(out.Name):
		issues = append(issues, Issue{"name",
			fmt.Sprintf("resource name %q is not a valid Go identifier", out.Name),
			"use letters and digits only, starting with a letter"})
	case naming.IsGoKeyword(strings.ToLower(out.Name)):
		issues = append(issues, Issue{"name",
			fmt.Sprintf("resource name %q collides with a Go keyword", out.Name),
			"choose a different noun"})
	case reservedIdents[naming.Camel(out.Name)]:
		// The generated handler writes `pension, err := h.svc.CreatePension(...)`,
		// so a resource whose camel form is `resp` or `port` would shadow an
		// import alias inside its own methods. That compiles in some shapes and
		// fails confusingly in others, so it is refused here where the message
		// can be precise.
		issues = append(issues, Issue{"name",
			fmt.Sprintf("resource name %q becomes the local variable %q, which shadows an identifier the generated code uses",
				out.Name, naming.Camel(out.Name)),
			"choose a more specific noun, e.g. " + out.Name + "Record"})
	}

	if strings.TrimSpace(r.Plural) == "" {
		out.Plural = naming.Plural(out.Name)
	} else {
		out.Plural = naming.Pascal(strings.TrimSpace(r.Plural))
	}
	if out.Plural != "" && !identRe.MatchString(out.Plural) {
		issues = append(issues, Issue{"plural",
			fmt.Sprintf("plural %q is not a valid Go identifier", out.Plural),
			"use letters and digits only"})
	}

	if strings.TrimSpace(r.Table) == "" {
		out.Table = naming.Snake(out.Plural)
	} else {
		out.Table = strings.ToLower(strings.TrimSpace(r.Table))
	}
	if out.Table != "" && !snakeRe.MatchString(out.Table) {
		issues = append(issues, Issue{"table",
			fmt.Sprintf("table %q is not lower_snake_case", out.Table),
			"use lower_snake_case, e.g. " + naming.Snake(out.Plural)})
	}

	if strings.TrimSpace(r.RouteBase) == "" {
		out.RouteBase = "/" + naming.Kebab(out.Plural)
	} else {
		out.RouteBase = "/" + strings.Trim(strings.ToLower(strings.TrimSpace(r.RouteBase)), "/")
	}
	if out.RouteBase != "/" && !routeRe.MatchString(out.RouteBase) {
		issues = append(issues, Issue{"route_base",
			fmt.Sprintf("route base %q is not a valid path", out.RouteBase),
			"use a lower-case path such as /" + naming.Kebab(out.Plural)})
	}

	out.Operations, issues = normaliseOperations(r.Operations, issues)
	out.Fields, issues = normaliseFields(r.Fields, issues)
	out.ListFilters, issues = normaliseFilters(r.ListFilters, out.Fields, out.Operations, issues)

	// Only complain about an empty field list when it is genuinely empty.
	// Rejected fields are dropped as they are validated, so a spec with one bad
	// field would otherwise report both "type is not allowed" and "no fields
	// declared" — two messages for one cause, which reads as two problems.
	if len(out.Fields) == 0 && len(r.Fields) == 0 {
		issues = append(issues, Issue{"fields", "no fields declared",
			"a resource needs at least one field besides id/created_at/updated_at"})
	}

	if len(issues) > 0 {
		return out, &InvalidSpecError{Issues: issues}
	}
	return out, nil
}

func normaliseOperations(ops []string, issues []Issue) ([]string, []Issue) {
	if len(ops) == 0 {
		return append([]string(nil), validOperations...), issues
	}
	seen := map[string]bool{}
	var out []string
	for i, op := range ops {
		op = strings.ToLower(strings.TrimSpace(op))
		// Accept the obvious synonyms rather than bouncing the spec back.
		switch op {
		case "read", "fetch", "getbyid", "get_by_id":
			op = "get"
		case "index", "listall":
			op = "list"
		case "add", "post", "insert":
			op = "create"
		case "patch", "edit":
			op = "update"
		case "remove", "destroy":
			op = "delete"
		}
		if !contains(validOperations, op) {
			issues = append(issues, Issue{
				fmt.Sprintf("operations[%d]", i),
				fmt.Sprintf("unknown operation %q", op),
				"use one of " + strings.Join(validOperations, ", ")})
			continue
		}
		if seen[op] {
			continue
		}
		seen[op] = true
		out = append(out, op)
	}
	// Canonical order, so the generated route list is stable regardless of the
	// order the model happened to emit.
	sort.Slice(out, func(i, j int) bool {
		return indexOf(validOperations, out[i]) < indexOf(validOperations, out[j])
	})
	return out, issues
}

func normaliseFields(fields []Field, issues []Issue) ([]Field, []Issue) {
	out := make([]Field, 0, len(fields))
	seenGo, seenJSON, seenDB := map[string]bool{}, map[string]bool{}, map[string]bool{}

	for i, f := range fields {
		path := fmt.Sprintf("fields[%d]", i)
		nf := f
		nf.Go = naming.Pascal(strings.TrimSpace(f.Go))

		switch {
		case nf.Go == "":
			issues = append(issues, Issue{path + ".go", "field name is empty",
				"supply a PascalCase field name"})
			continue
		case !identRe.MatchString(nf.Go):
			issues = append(issues, Issue{path + ".go",
				fmt.Sprintf("field name %q is not a valid Go identifier", nf.Go),
				"use letters and digits only, starting with a letter"})
			continue
		case reservedFields[nf.Go]:
			issues = append(issues, Issue{path + ".go",
				fmt.Sprintf("%s is added by the scaffolder", nf.Go),
				"remove it — every domain model gets ID, CreatedAt and UpdatedAt"})
			continue
		case seenGo[nf.Go]:
			issues = append(issues, Issue{path + ".go",
				fmt.Sprintf("duplicate field %s", nf.Go), "remove the duplicate"})
			continue
		}
		seenGo[nf.Go] = true

		// Type.
		raw := strings.TrimSpace(nf.Type)
		if sub, rejected := rejectedTypes[raw]; rejected {
			issues = append(issues, Issue{path + ".type",
				fmt.Sprintf("type %q is not available in this template", raw),
				"use " + sub})
			continue
		}
		if alias, ok := typeAliases[strings.ToLower(raw)]; ok {
			raw = alias
		}
		if raw == "" {
			issues = append(issues, Issue{path + ".type",
				fmt.Sprintf("field %s has no type", nf.Go),
				"use one of " + strings.Join(Types(), ", ")})
			continue
		}
		if _, ok := goTypes[raw]; !ok {
			issues = append(issues, Issue{path + ".type",
				fmt.Sprintf("type %q is not on the allow-list", nf.Type),
				"use one of " + strings.Join(Types(), ", ")})
			continue
		}
		nf.Type = raw

		// json / db tags.
		if strings.TrimSpace(nf.JSON) == "" {
			nf.JSON = naming.Snake(nf.Go)
		} else {
			nf.JSON = strings.TrimSpace(nf.JSON)
		}
		if !snakeRe.MatchString(nf.JSON) {
			issues = append(issues, Issue{path + ".json",
				fmt.Sprintf("json tag %q is not lower_snake_case", nf.JSON),
				`use "` + naming.Snake(nf.Go) + `"`})
		}
		if strings.TrimSpace(nf.DB) == "" {
			nf.DB = naming.Snake(nf.Go)
		} else {
			nf.DB = strings.ToLower(strings.TrimSpace(nf.DB))
		}
		if !snakeRe.MatchString(nf.DB) {
			issues = append(issues, Issue{path + ".db",
				fmt.Sprintf("db column %q is not lower_snake_case", nf.DB),
				`use "` + naming.Snake(nf.Go) + `"`})
		}
		if seenJSON[nf.JSON] {
			issues = append(issues, Issue{path + ".json",
				fmt.Sprintf("json tag %q is already used", nf.JSON), "give the field a distinct json name"})
		}
		if seenDB[nf.DB] {
			issues = append(issues, Issue{path + ".db",
				fmt.Sprintf("db column %q is already used", nf.DB), "give the field a distinct column"})
		}
		seenJSON[nf.JSON], seenDB[nf.DB] = true, true

		// validate tag.
		nf.Validate = strings.TrimSpace(nf.Validate)
		if nf.Validate == "" {
			nf.Validate = defaultValidate(nf.Type)
		}
		if !tagRe.MatchString(nf.Validate) {
			issues = append(issues, Issue{path + ".validate",
				fmt.Sprintf("validate tag %q contains characters that cannot appear in a struct tag", nf.Validate),
				`use plain validator syntax, e.g. required or oneof=active closed`})
		}

		// SQL column type.
		if strings.TrimSpace(nf.SQL) == "" {
			nf.SQL = goTypes[nf.Type].SQL
		} else {
			nf.SQL = strings.TrimSpace(nf.SQL)
		}
		switch {
		case !sqlTypeRe.MatchString(nf.SQL):
			issues = append(issues, Issue{path + ".sql",
				fmt.Sprintf("sql type %q contains characters that are not allowed in a column definition", nf.SQL),
				"use a plain Postgres type such as " + goTypes[nf.Type].SQL})
		case strings.Contains(nf.SQL, "--"):
			issues = append(issues, Issue{path + ".sql",
				"sql type contains a comment marker",
				"use a plain Postgres type such as " + goTypes[nf.Type].SQL})
		}

		out = append(out, nf)
	}
	return out, issues
}

func normaliseFilters(filters []Filter, fields []Field, ops []string, issues []Issue) ([]Filter, []Issue) {
	if len(filters) == 0 {
		return nil, issues
	}
	byGo := map[string]Field{}
	for _, f := range fields {
		byGo[f.Go] = f
	}
	if !contains(ops, "list") {
		issues = append(issues, Issue{"list_filters",
			"list filters were given but the list operation is not selected",
			`add "list" to operations, or drop the filters`})
	}
	seen := map[string]bool{}
	out := make([]Filter, 0, len(filters))
	for i, fl := range filters {
		path := fmt.Sprintf("list_filters[%d]", i)
		nf := fl
		nf.Go = naming.Pascal(strings.TrimSpace(fl.Go))
		field, ok := byGo[nf.Go]
		if !ok {
			// A filter on something that is not a column cannot become a WHERE
			// clause, so this is a spec error rather than a warning.
			issues = append(issues, Issue{path + ".go",
				fmt.Sprintf("filter %q does not name a declared field", fl.Go),
				"filter on one of: " + strings.Join(fieldNames(fields), ", ")})
			continue
		}
		if seen[nf.Go] {
			continue
		}
		seen[nf.Go] = true
		if goTypes[field.Type].AlwaysSet {
			// A value-typed bool filter cannot distinguish "false" from "not
			// supplied", so it would silently constrain every request to
			// false. Rejecting is the only honest option.
			issues = append(issues, Issue{path + ".go",
				fmt.Sprintf("%s is a %s, which cannot be an optional filter: a false value is indistinguishable from an absent one", nf.Go, field.Type),
				"model the state as a string with oneof=, or add an explicit separate flag field"})
			continue
		}
		nf.Type, nf.DB = field.Type, field.DB
		if strings.TrimSpace(nf.Form) == "" {
			nf.Form = field.JSON
		} else {
			nf.Form = strings.TrimSpace(nf.Form)
		}
		if !snakeRe.MatchString(nf.Form) {
			issues = append(issues, Issue{path + ".form",
				fmt.Sprintf("form parameter %q is not lower_snake_case", nf.Form),
				`use "` + field.JSON + `"`})
		}
		out = append(out, nf)
	}
	return out, issues
}

func fieldNames(fields []Field) []string {
	out := make([]string, 0, len(fields))
	for _, f := range fields {
		out = append(out, f.Go)
	}
	return out
}

func contains(list []string, s string) bool {
	for _, v := range list {
		if v == s {
			return true
		}
	}
	return false
}

func indexOf(list []string, s string) int {
	for i, v := range list {
		if v == s {
			return i
		}
	}
	return len(list)
}
