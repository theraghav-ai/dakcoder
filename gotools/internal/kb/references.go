package kb

// The knowledge base, as specified by plan.md §14.2.
//
// # Why it is generated rather than written
//
// skill.md is 2,339 lines — roughly 30k tokens. It cannot live in a system
// prompt, and 95% of it is irrelevant to any given turn. §14.2's answer is
// progressive disclosure: a small always-loaded index, with detail in bundled
// reference files fetched on demand.
//
// The obvious way to build that is to copy sections into twelve files by hand.
// The obvious problem with it is that skill.md then has thirteen copies, twelve
// of which go stale silently. So the references are *assembled*: each declares
// the sections it draws on, the generator extracts them verbatim, and
// `gotools kb --check` fails when the assembled output no longer matches what
// is committed. Same discipline as the scaffold golden snapshots.
//
// # Why they carry corrections
//
// Three parts of skill.md's worked example do not compile against the current
// libraries, and the shipped `user` resource contradicts the document in two
// more places (plan.md §6). Handing the agent the document verbatim would hand
// it those defects: the pre-implementation spike showed the model follows its
// context closely, which is exactly why the context has to be right.
//
// So a reference is the document section *plus* what we know about it. That
// makes the knowledge base strictly better than the source it is built from,
// and it is the reason to build one at all rather than pointing search_docs at
// skill.md.

// Reference is one bundled reference file.
type Reference struct {
	// Slug is the file name stem: handler-pattern -> references/handler-pattern.md.
	// It is also the handle prompts cite: @skill:handler-pattern.
	Slug string
	// Title heads the file.
	Title string
	// Purpose is the one-line "fetch this when…" entry in SKILL.md's table. It
	// is what the agent reads to decide whether to spend a call, so it says
	// when to fetch rather than what is inside.
	Purpose string
	// Intro is the editorial preamble: what this pattern is for and what goes
	// wrong without it.
	Intro string
	// Corrections are the places the source document is wrong. Rendered before
	// the extracted text so a reader meets them first.
	Corrections []string
	// Sources are the document sections to extract verbatim.
	Sources []Ref
	// Generator names a built-in generator for references whose content comes
	// from the workspace or the rule set rather than from a document.
	Generator string
	// Body is the whole reference, hand-authored, for knowledge that has no
	// source section to extract.
	//
	// The four references drawn from the manual code review are like this:
	// skill.md has nothing to say about batching, about which database library
	// is which, or about what must never be logged, because those lessons came
	// from reading 41 services rather than from the template. Extracting a
	// section that does not exist is not an option, and inventing one in
	// skill.md would put knowledge in the template that the template did not
	// teach.
	Body string
}

// References is the knowledge base's shape, from plan.md §14.2.
//
// Ordered as an agent would need them: the four layer patterns first, then the
// cross-cutting concerns, then the reference material.
var References = []Reference{
	{
		Slug:    "handler-pattern",
		Title:   "Handler pattern",
		Purpose: "writing or changing a handler, a route, or a handler constructor",
		Intro: "A handler is a struct embedding `*serverHandler.Base`, a constructor that " +
			"builds that base with the prefix chain, a `Routes()` method, and one method per " +
			"route on the DTO signature.\n\n" +
			"Two things fail silently if you get them wrong. Omitting the `*serverHandler.Base` " +
			"embed means the type does not satisfy `serverHandler.Handler`, which surfaces at " +
			"start-up as an Uber-FX graph error naming a type rather than a file. Omitting " +
			"`.Name(...)` on a route means the route serves correctly and is missing from the " +
			"generated OpenAPI document, with no error at all.\n\n" +
			"Enforced by `handler-signature`, `handler-base` and `routes-in-handler`.",
		Corrections: []string{
			"skill.md's worked example imports `github.com/jackc/pgx/v5` into the handler to " +
				"special-case `pgx.ErrNoRows`. Do not: that crosses the layer boundary " +
				"`layer-sql-boundary` enforces, and the shipped `user` handler does not do it. " +
				"Return the error and let the framework map it.",
		},
		Sources: []Ref{
			{Doc: "skill.md", Section: "Handler Pattern"},
			{Doc: "skill.md", Section: "Routing Pattern"},
		},
	},
	{
		Slug:    "repository-pattern",
		Title:   "Repository pattern",
		Purpose: "writing a query, or anything that touches the database",
		Intro: "Every repository method builds its query with `dblib.Psql`, takes its deadline " +
			"from config, and maps rows by name.\n\n" +
			"`dblib.Psql` is a Squirrel builder pre-configured with `$N` placeholders. A " +
			"hand-rolled `sq.Insert(...)` defaults to `?` placeholders, which Postgres rejects " +
			"at runtime rather than at compile time — so the mistake reaches production " +
			"looking like a driver problem.\n\n" +
			"Enforced by `repo-contract`, `repo-rowmapper` and `repo-norows`.",
		Corrections: []string{
			"skill.md's worked example writes `sq.Insert(...).PlaceholderFormat(sq.Dollar)`. " +
				"That is the older idiom. Use `dblib.Psql.Insert(...)`, which is that builder " +
				"already configured — the shipped `user` repository does, and `repo-contract` " +
				"requires it (plan.md §6).",
			"The shipped `user` repository builds its create and update results from the " +
				"arguments it was handed rather than from the row it wrote, so `POST` answers " +
				"with `\"id\": 0` and a partial `PUT` dereferences a nil pointer and panics. " +
				"Use `dblib.InsertReturning` / `dblib.UpdateReturning` with a `RETURNING` " +
				"clause, as `resource_scaffold` does.",
		},
		Sources: []Ref{{Doc: "skill.md", Section: "Repository Pattern"}},
	},
	{
		Slug:    "domain-model",
		Title:   "Domain model and database schema",
		Purpose: "adding a field, a table, or a new domain type",
		Intro: "A domain model is plain Go: `ID`, the resource's own fields, `CreatedAt` and " +
			"`UpdatedAt`, each with a `json` and a `db` tag in snake_case.\n\n" +
			"The `db` tag is load-bearing rather than decorative — `pgx.RowToStructByName` " +
			"matches result columns to fields through it, so a missing or misspelt tag is a " +
			"silent scan failure at runtime, not a compile error.\n\n" +
			"Enforced by `domain-tags` and `layer-dto-boundary`.",
		Sources: []Ref{
			{Doc: "skill.md", Section: "Domain Model Pattern"},
			{Doc: "skill.md", Section: "Database Schema"},
			{Doc: "skill.md", Section: "Naming Conventions"},
		},
	},
	{
		Slug:    "request-dto",
		Title:   "Request DTOs and validation",
		Purpose: "adding or changing a request payload, a path parameter, or a query filter",
		Intro: "Every request struct lives in `handler/request/request.go`, package `request`, and " +
			"validation comes from `validate` tags plus generated code.\n\n" +
			"The location is not bureaucracy. `govalid` is run as `govalid ./request.go` from " +
			"`handler/request/`, so a request struct in any other file never gets a generated validator — and the failure is silent: input reaches the handler " +
			"unvalidated.\n\n" +
			"Running `govalid` is not optional either. The framework returns **422** for any " +
			"non-GET route whose request DTO has no generated `Validate()` method, so a fresh " +
			"resource fails before it reaches your code until the validators exist.\n\n" +
			"A tag must also *bound* the field, not merely require it. `validate:\"required\"` " +
			"on a free-text string means \"not empty\" and nothing else, so a 10MB string is " +
			"valid input. Bounds were the most systemic finding in the review of 41 services: " +
			"numeric ranges asked for in 39 of them, string constraints in 34.\n\n" +
			"Enforced by `request-dto`, `request-validate-depth` and `validator-generated`; " +
			"listed field by field by `gotools validation-audit`.",
		Corrections: []string{
			"skill.md and SOP.md place request structs in a flat `handler/request.go` inside " +
				"`package handler`. The canonical location is `handler/request/request.go` in " +
				"its own `package request` — the layout the migration SOP converges every " +
				"service on (see @skill:legacy-migration), imported as " +
				"`request \"<module>/handler/request\"`. Where the extracted text below says " +
				"`handler/request.go`, read `handler/request/request.go`.",
			"skill.md's worked example gives request DTOs a `ToDomain()` converter. No such " +
				"method exists anywhere in the template; handlers pass fields positionally to " +
				"the repository (plan.md §6). Do not add one.",
			"skill.md teaches only `required` and `omitempty`, which is why so much production " +
				"code carries nothing else. The vocabulary you actually need:\n\n" +
				"| Field | Tag | Why |\n" +
				"|---|---|---|\n" +
				"| free text | `required,max=255` | an unbounded string is an unbounded row |\n" +
				"| code or ref | `required,len=13` | fixed-width identifiers |\n" +
				"| enum | `required,oneof=pending approved rejected` | rejects unknown states at the edge |\n" +
				"| email | `required,email` | |\n" +
				"| number | `required,min=1,max=9999` | bounds reach the database as intent |\n" +
				"| optional number | `omitempty,min=0` | |\n" +
				"| date string | `required,datetime=2006-01-02` | |\n" +
				"| slice | `required,max=100,dive` | caps the request body; `dive` validates elements |\n\n" +
				"A field that is genuinely unconstrained is fine — `omitempty` alone is an " +
				"acceptable floor, and says the absence was deliberate.",
		},
		Sources: []Ref{
			{Doc: "skill.md", Section: "Request DTO Pattern"},
			{Doc: "SOP.md", Section: "Validation"},
		},
	},
	{
		Slug:    "response-dto",
		Title:   "Response DTOs and envelopes",
		Purpose: "shaping a response, or choosing a status constant",
		Intro: "A response is a wire type plus a `New*Response` converter, wrapped in an " +
			"operation envelope that embeds `port.StatusCodeAndMessage`.\n\n" +
			"`json:\",inline\"` on the embedded status is the subtle part: without it the " +
			"embedded struct marshals as a nested object, so `status_code` and `message` end " +
			"up under a key instead of at the top level, and every client breaks at once.\n\n" +
			"Status codes and messages come from the predefined `port.*Success` constants, not " +
			"from literals, so every service in the estate answers identically.\n\n" +
			"Enforced by `response-dto` and `response-status`.",
		Corrections: []string{
			"skill.md's list example builds `port.MetaDataResponse{TotalCount: …, Count: …}`. " +
				"Those fields do not exist — the struct declares `Skip`, `Limit`, `OrderBy`, " +
				"`SortType`, `TotalRecordsCount` and `ReturnedRecordsCount`. Use " +
				"`port.NewMetaDataResponse(skip, limit, returned)`, as the shipped `ListUsers` " +
				"does.",
			"skill.md prescribes `CreatedAt` and `UpdatedAt` on response types, formatted " +
				"`\"2006-01-02 15:04:05\"`. The shipped `UserResponse` omits both. Follow the " +
				"document: include them (plan.md §6).",
		},
		Sources: []Ref{{Doc: "skill.md", Section: "Response DTO Pattern"}},
	},
	{
		Slug:    "bootstrap-fx",
		Title:   "Bootstrap and Uber-FX registration",
		Purpose: "registering a new repository or handler — read before editing bootstrapper.go",
		Intro: "The composition root registers repositories and handlers, and the two " +
			"registrations are **not** interchangeable.\n\n" +
			"A repository is a plain provider. A handler must be wrapped in `fx.Annotate` with " +
			"`fx.As(new(serverHandler.Handler))` and " +
			"`fx.ResultTags(serverHandler.ServerControllersGroupTag)`, because the server " +
			"collects handlers by group tag.\n\n" +
			"Getting this wrong has two failure modes and neither is obvious. A missing " +
			"registration fails at start-up with an Uber-FX error naming a type, not a file. A " +
			"handler registered with a bare `fx.Provide` compiles, starts, and silently serves " +
			"none of its routes — there is no error at all.\n\n" +
			"Prefer the `fx_wire` tool over editing by hand; it produces the correct shape and " +
			"is a no-op if the constructor is already registered.\n\n" +
			"Enforced by `fx-registration`.",
		Sources: []Ref{
			{Doc: "skill.md", Section: "Bootstrap Configuration"},
			{Doc: "SOP.md", Section: "bootstrap/bootstrapper.go"},
		},
	},
	{
		Slug:    "errors",
		Title:   "Error handling",
		Purpose: "returning an error, or deciding a status code",
		Intro: "Log with context, then return. An error returned without a log line is " +
			"invisible in production: the developer sees a 500 in Grafana with no trace of " +
			"where it came from.\n\n" +
			"`pgx.ErrNoRows` is the one to be careful with — it has to reach the framework " +
			"intact to become a 404, so do not wrap it with `%v` and do not swallow it in the " +
			"repository.\n\n" +
			"Enforced by `error-handling`, and by `go-idiom` for `%w` versus `%v`.",
		Sources: []Ref{
			{Doc: "skill.md", Section: "Error Handling"},
			{Doc: "SOP.md", Section: "[handler].go"},
		},
	},
	{
		Slug:    "file-upload",
		Title:   "File upload and file responses",
		Purpose: "a route that accepts an upload or returns a file",
		Intro: "Uploads arrive as `*multipart.FileHeader` fields on the request DTO with " +
			"`form:` tags — one for a single file, a slice for many. Responses use " +
			"`port.FileResponse`, either with the bytes in `Data` or with an `io.ReadCloser` " +
			"in `Reader` for anything large enough to be worth streaming.",
		Sources: []Ref{
			{Doc: "SOP.md", Section: "Handler with file upload"},
			{Doc: "SOP.md", Section: "File as a Response"},
		},
	},
	{
		Slug:    "worked-example",
		Title:   "Worked example: adding a resource end to end",
		Purpose: "the full ten-step recipe, when you want to see every file at once",
		Intro: "The complete sequence for adding a resource. Read the corrections first — three " +
			"parts of this example do not compile against the current libraries.\n\n" +
			"In practice, prefer `resource_scaffold`: it emits the same seven files " +
			"deterministically, already corrected, and wires the FX registration.",
		Corrections: []string{
			"The example adds its DTOs to a flat `handler/request.go`. The canonical location " +
				"is `handler/request/request.go`, package `request` — see @skill:request-dto.",
			"The repository uses `sq.Insert(...).PlaceholderFormat(sq.Dollar)`. Use " +
				"`dblib.Psql` — see @skill:repository-pattern.",
			"The request DTOs have a `ToDomain()` method. No such method exists in the " +
				"template — see @skill:request-dto.",
			"The list handler builds `port.MetaDataResponse` with fields that do not exist, " +
				"and the handler imports `pgx` — see @skill:response-dto and " +
				"@skill:handler-pattern.",
		},
		Sources: []Ref{{Doc: "skill.md", Section: "Complete Example Workflow"}},
	},
	{
		Slug:      "config-keys",
		Title:     "Configuration keys",
		Purpose:   "reading a config value, or adding a key — every key, and which environments declare it",
		Generator: "config-keys",
		Intro: "Every key declared across `configs/*.yaml`, and which environment files declare " +
			"it.\n\n" +
			"A key the code reads but a config does not declare returns the **zero value**: a " +
			"repository asking for an absent `db.QueryTimeoutLow` gets a 0s deadline, and " +
			"every query it wraps fails immediately with `context deadline exceeded` — an " +
			"error naming the context, not the config. A key present in the base file and " +
			"missing from `config.prod.yaml` is how a service works in dev and dies in " +
			"production.\n\n" +
			"Key lookups fold case, so `db.QueryTimeoutLow` and `db.querytimeoutlow` address " +
			"the same value. The segments still have to be right.\n\n" +
			"Enforced by `config-key-exists` and `swagger-visible`. Generated from the " +
			"reference template, so it cannot describe a key that is not there.",
	},
	{
		Slug:      "legacy-patterns",
		Title:     "Legacy patterns and their replacements",
		Purpose:   "auditing or migrating a pre-template (api-* generation) service",
		Generator: "legacy-rules",
		Intro: "The concrete differences between the `api-*` generation and the current " +
			"`n-api-*` template, each with what to do about it.\n\n" +
			"Most real IT 2.0 Go code looks like the former. Run `legacy_audit` to find these " +
			"in a specific service rather than reading for them.\n\n" +
			"Generated from the legacy rule set, so this table and the audit cannot disagree.",
	},
	{
		Slug:      "go-idiom",
		Title:     "Idiomatic Go, the checkable subset",
		Purpose:   "general Go style questions — sits under the template rules, not over them",
		Generator: "idiom-rules",
		Intro: "The part of the house Go style that is machine-checked. It sits deliberately " +
			"*under* the template rules: a service that is idiomatic but off-template is a " +
			"much bigger problem than one that is on-template and slightly unidiomatic.\n\n" +
			"All of it is advisory except a mismatched package declaration, which does not " +
			"compile.\n\n" +
			"Generated from the rule set. `golangci-lint` covers far more at the verification " +
			"gate; this is what is cheap enough to check after every edit.",
	},
}

// init appends the references drawn from the manual code review.
//
// Appended rather than written inline so the two bodies of knowledge stay
// visibly distinct: everything above is extracted from the reference template,
// everything in references_review.go was learned from reading 41 services in
// production. When skill.md changes, only the first set needs re-checking.
func init() { References = append(References, reviewReferences...) }

// ReferenceBySlug looks up a reference by its handle.
func ReferenceBySlug(slug string) (Reference, bool) {
	for _, r := range References {
		if r.Slug == slug {
			return r, true
		}
	}
	return Reference{}, false
}
