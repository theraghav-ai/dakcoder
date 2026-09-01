package spec

import (
	"strings"

	"gitlab.cept.gov.in/it-2.0/dakcoder/gotools/internal/naming"
)

// TimestampLayout is the layout response DTOs format times with.
//
// It lives here rather than in the scaffolder or the rules engine because both
// need it and they must agree: the rule checks what the scaffolder writes.
// rules.DefaultConfig() takes its default from this constant for that reason.
//
// Source: skill.md §Response DTO Pattern. Note the shipped `user` resource
// omits both timestamps from its response — a known divergence between the
// reference document and the reference code (plan.md §6). The scaffolder
// follows the document.
const TimestampLayout = "2006-01-02 15:04:05"

// Derived names. These are methods rather than fields so that a spec is fully
// described by its JSON, and so a hand-built spec in a test cannot get out of
// step with a normalised one.

// Has reports whether an operation is selected.
func (r Resource) Has(op string) bool { return contains(r.Operations, op) }

// Var is the singular resource name as a local variable, e.g. "pension".
func (r Resource) Var() string { return naming.Camel(r.Name) }

// PluralVar is the plural name as a local variable, e.g. "pensions".
func (r Resource) PluralVar() string { return naming.Camel(r.Plural) }

// RepoType is the repository struct name.
func (r Resource) RepoType() string { return r.Name + "Repository" }

// HandlerType is the handler struct name.
func (r Resource) HandlerType() string { return r.Name + "Handler" }

// TableConst is the unexported package-level constant holding the table name.
func (r Resource) TableConst() string { return naming.Camel(r.Name) + "Table" }

// FileStem is the snake_case file name stem used for the generated Go files.
func (r Resource) FileStem() string { return naming.Snake(r.Name) }

// TableFileStem is the stem for the DDL file, which is named after the table.
func (r Resource) TableFileStem() string { return naming.Snake(r.Plural) }

// Title renders the resource for prose: "Pension".
func (r Resource) Title() string { return naming.Title(r.Name) }

// PluralTitle renders the plural for prose: "Pensions".
func (r Resource) PluralTitle() string { return naming.Title(r.Plural) }

// Repository method names, following the reference `user` resource exactly.

// RepoCreate is the repository insert method.
func (r Resource) RepoCreate() string { return "Create" + r.Name }

// RepoList is the repository collection read.
func (r Resource) RepoList() string { return "GetAll" + r.Plural }

// RepoGet is the repository single-row read.
func (r Resource) RepoGet() string { return "Get" + r.Name + "ByID" }

// RepoUpdate is the repository partial update.
func (r Resource) RepoUpdate() string { return "Update" + r.Name + "ByID" }

// RepoDelete is the repository delete.
func (r Resource) RepoDelete() string { return "Delete" + r.Name + "ByID" }

// Handler method names. Note the list method is List<Plural> while the
// repository's is GetAll<Plural> — that asymmetry is the reference's, not ours.

// HandlerCreate is the POST handler method.
func (r Resource) HandlerCreate() string { return "Create" + r.Name }

// HandlerList is the collection GET handler method.
func (r Resource) HandlerList() string { return "List" + r.Plural }

// HandlerGet is the single-row GET handler method.
func (r Resource) HandlerGet() string { return "Get" + r.Name + "ByID" }

// HandlerUpdate is the PUT handler method.
func (r Resource) HandlerUpdate() string { return "Update" + r.Name + "ByID" }

// HandlerDelete is the DELETE handler method.
func (r Resource) HandlerDelete() string { return "Delete" + r.Name + "ByID" }

// Request and response type names.

// CreateReq is the create request DTO.
func (r Resource) CreateReq() string { return "Create" + r.Name + "Request" }

// UpdateReq is the update request DTO.
func (r Resource) UpdateReq() string { return "Update" + r.Name + "Request" }

// IDUri is the :id path-parameter binding struct.
func (r Resource) IDUri() string { return r.Name + "IDUri" }

// ListParams is the list route's query-parameter struct.
func (r Resource) ListParams() string { return "List" + r.Plural + "Params" }

// ItemResp is the wire representation of one resource.
func (r Resource) ItemResp() string { return r.Name + "Response" }

// NewItemResp is the single-item domain-to-wire converter.
func (r Resource) NewItemResp() string { return "New" + r.Name + "Response" }

// NewItemsResp is the collection domain-to-wire converter.
func (r Resource) NewItemsResp() string { return "New" + r.Plural + "Response" }

// CreateResp is the create operation envelope.
func (r Resource) CreateResp() string { return r.Name + "CreateResponse" }

// FetchResp is the read operation envelope.
func (r Resource) FetchResp() string { return r.Name + "FetchResponse" }

// UpdateResp is the update operation envelope.
func (r Resource) UpdateResp() string { return r.Name + "UpdateResponse" }

// DeleteResp is the delete operation envelope.
func (r Resource) DeleteResp() string { return r.Name + "DeleteResponse" }

// ListResp is the list operation envelope.
func (r Resource) ListResp() string { return r.Plural + "ListResponse" }

// UseListParams reports whether the list route binds a request struct rather
// than the reference's `_ struct{}`.
//
// The default is `_ struct{}`, matching the shipped ListUsers byte for byte.
// A params struct appears only when the spec actually needs one — filters or
// pagination — because an unused binding is a difference from the reference
// with nothing to show for it.
func (r Resource) UseListParams() bool {
	return r.Has("list") && (len(r.ListFilters) > 0 || r.Paginate)
}

// NeedsManualListValidate reports whether the generated ListXParams needs a
// hand-written Validate() method.
//
// govalid generates a Validate() for every struct in request.go that has at
// least one directly-tagged `validate` field. ListXParams with filters has such
// fields, so govalid generates one and a hand-written method would be a
// duplicate-method compile error. Without filters it has none — only the
// embedded port.MetadataRequest, whose tags live in another file that govalid
// does not read — so the interface has to be satisfied by hand, exactly as the
// reference ListUsersParams does.
//
// Note this is about the *struct*, not about whether the handler binds it: the
// params struct is generated for any list resource, so the question is settled
// by the filters alone.
func (r Resource) NeedsManualListValidate() bool {
	return r.Has("list") && len(r.ListFilters) == 0
}

// ── column lists ────────────────────────────────────────────────────────────

// SelectColumns are every column a read returns, in table order.
func (r Resource) SelectColumns() []string {
	out := make([]string, 0, len(r.Fields)+3)
	out = append(out, "id")
	for _, f := range r.Fields {
		out = append(out, f.DB)
	}
	return append(out, "created_at", "updated_at")
}

// QuotedSelectColumns renders SelectColumns as Go string literals.
func (r Resource) QuotedSelectColumns() string { return quoteJoin(r.SelectColumns()) }

// InsertColumns are the columns an insert writes: the resource's own fields.
func (r Resource) InsertColumns() []string {
	out := make([]string, 0, len(r.Fields))
	for _, f := range r.Fields {
		out = append(out, f.DB)
	}
	return out
}

// QuotedInsertColumns renders InsertColumns as Go string literals.
func (r Resource) QuotedInsertColumns() string { return quoteJoin(r.InsertColumns()) }

// ReturningColumns renders the column list for a SQL RETURNING clause.
func (r Resource) ReturningColumns() string { return strings.Join(r.SelectColumns(), ", ") }

// NeedsIDUri reports whether the :id binding struct is required. Any route with
// a path parameter needs it.
func (r Resource) NeedsIDUri() bool {
	return r.Has("get") || r.Has("delete")
}

func quoteJoin(cols []string) string {
	parts := make([]string, len(cols))
	for i, c := range cols {
		parts[i] = `"` + c + `"`
	}
	return strings.Join(parts, ", ")
}

// ── import decisions ────────────────────────────────────────────────────────

// NeedsSquirrel reports whether the repository needs the squirrel alias. Any
// operation with a WHERE clause does, and so does a filtered list.
func (r Resource) NeedsSquirrel() bool {
	return r.Has("get") || r.Has("update") || r.Has("delete") || len(r.ListFilters) > 0
}

// NeedsPgx reports whether the repository needs pgx. Every operation does:
// reads and the RETURNING writes need the row mappers, and delete needs
// pgx.ErrNoRows.
func (r Resource) NeedsPgx() bool { return len(r.Operations) > 0 }

// HasTimeField reports whether any field is a timestamp.
func (r Resource) HasTimeField() bool {
	for _, f := range r.Fields {
		if goTypes[f.Type].Import == "time" {
			return true
		}
	}
	return false
}

// NeedsTimeInRequest reports whether handler/request/request.go needs the time import,
// which happens when a field is a timestamp the client supplies.
func (r Resource) NeedsTimeInRequest() bool {
	return r.HasTimeField() && (r.Has("create") || r.Has("update"))
}

// NeedsTimeInRepo reports whether the repository names time.Time. It does so in
// the create and update signatures, which carry the field types through.
func (r Resource) NeedsTimeInRepo() bool {
	return r.HasTimeField() && (r.Has("create") || r.Has("update"))
}

// NeedsTimeInHandler reports whether the handler names time.Time. Only the
// update path does, where it declares the optional pointer locals.
//
// The response file does not need it: it holds the formatted string and calls
// Format on the domain value, so the type never appears.
func (r Resource) NeedsTimeInHandler() bool {
	return r.HasTimeField() && r.Has("update")
}

// ── parameter rendering ─────────────────────────────────────────────────────

// Param is one repository or handler parameter.
type Param struct {
	Name string
	Type string
	// Field is the spec field it came from, for the caller that needs the
	// zero-value test or the column name.
	Field Field
}

// CreateParams are the repository's create parameters, in field order.
func (r Resource) CreateParams() []Param {
	out := make([]Param, 0, len(r.Fields))
	for _, f := range r.Fields {
		out = append(out, Param{Name: naming.Camel(f.Go), Type: goTypes[f.Type].Name, Field: f})
	}
	return out
}

// UpdateParams are the repository's update parameters. Every one is a pointer:
// a partial update must distinguish "set this to the zero value" from "leave it
// alone", and only a pointer can carry that distinction across the call.
func (r Resource) UpdateParams() []Param {
	out := make([]Param, 0, len(r.Fields))
	for _, f := range r.Fields {
		out = append(out, Param{Name: naming.Camel(f.Go), Type: "*" + goTypes[f.Type].Name, Field: f})
	}
	return out
}

// ZeroTest renders the "was this field supplied?" condition for a param, given
// the selector prefix its request struct is bound to — ZeroTest("req.") yields
// `req.Amount != 0`. An empty result means the field is always considered
// supplied (see GoType.AlwaysSet).
func (p Param) ZeroTest(prefix string) string {
	return goTypes[p.Field.Type].ZeroTest(prefix + p.Field.Go)
}

// AlwaysSet reports whether the field's zero value is indistinguishable from
// absence, so a partial update always writes it.
func (p Param) AlwaysSet() bool { return goTypes[p.Field.Type].AlwaysSet }

// GoType is the field's Go type as written in source.
func (f Field) GoType() string { return goTypes[f.Type].Name }

// PtrType is the field's Go type as an optional pointer.
func (f Field) PtrType() string { return "*" + goTypes[f.Type].Name }

// RespType is the field's type on the wire. Timestamps become pre-formatted
// strings so every service renders them identically.
func (f Field) RespType() string {
	if f.IsTime() {
		return "string"
	}
	return goTypes[f.Type].Name
}

// Var is the field name as a local variable.
func (f Field) Var() string { return naming.Camel(f.Go) }

// IsTime reports whether the field is a timestamp.
func (f Field) IsTime() bool { return goTypes[f.Type].Import == "time" }

// CreateValidate is the validate tag for the create request: the spec's own
// constraint, used verbatim.
func (f Field) CreateValidate() string { return f.Validate }

// UpdateValidate is the validate tag for the update request.
//
// A partial update must accept an absent field, so `required` is dropped and
// `omitempty` leads — but any *other* constraint the author wrote is kept, so
// a status declared oneof=active|closed still cannot be updated to something
// else. Dropping the whole tag would quietly widen the contract.
func (f Field) UpdateValidate() string {
	out := []string{"omitempty"}
	for _, part := range strings.Split(f.Validate, ",") {
		part = strings.TrimSpace(part)
		if part == "" || part == "required" || part == "omitempty" {
			continue
		}
		out = append(out, part)
	}
	return strings.Join(out, ",")
}

// Filter accessors mirror the field the filter names; Type and DB are copied
// across during normalisation.

// GoType is the filter's Go type.
func (f Filter) GoType() string { return goTypes[f.Type].Name }

// Var is the filter name as a local variable.
func (f Filter) Var() string { return naming.Camel(f.Go) }

// ZeroTest renders the "was this filter supplied?" condition against the bound
// request struct.
func (f Filter) ZeroTest() string { return goTypes[f.Type].ZeroTest("req." + f.Go) }

// SignatureOf renders a parameter list with consecutive same-typed parameters
// grouped, which is what a Go author writes by hand and what the reference
// repository does:
//
//	firstName, lastName string, age int, city, email string
//
// Generating the ungrouped form would compile identically and read as
// machine-written, which is exactly the impression a scaffolder should avoid —
// the output is meant to be reviewed as if a colleague wrote it.
func SignatureOf(params []Param) string {
	if len(params) == 0 {
		return ""
	}
	var (
		parts []string
		group []string
	)
	for i, p := range params {
		group = append(group, p.Name)
		last := i == len(params)-1
		if last || params[i+1].Type != p.Type {
			parts = append(parts, strings.Join(group, ", ")+" "+p.Type)
			group = group[:0]
		}
	}
	return strings.Join(parts, ", ")
}

// ArgsOf renders the parameter names as a call argument list.
func ArgsOf(params []Param) string {
	names := make([]string, len(params))
	for i, p := range params {
		names[i] = p.Name
	}
	return strings.Join(names, ", ")
}
