---
name: n-api-template
handle: "@skill"
---

# The n-api-template contract

> **Generated.** Do not edit — run `make knowledge` and commit the result.
> Assembled from the reference template, so it cannot drift from what the linter enforces.

This file is always in context. Everything else is fetched on demand — `skill.md` is 2,339 lines and 95% of it is irrelevant to any given turn.

## The contract, in short

```
core/domain/     plain Go models: json + db tags, ID, CreatedAt, UpdatedAt
core/port/       shared request and response envelopes
repo/postgres/   the ONLY place SQL, squirrel or pgx may appear
handler/         handlers, routes, and every request DTO in request.go
handler/response/  wire types and their New*Response converters
bootstrap/       the Uber-FX composition root
db/              DDL, applied by hand — never by the agent
configs/         one file per environment
```

- Handlers take `(sctx *serverRoute.Context, req T) (*resp.R, error)` — never a
  `*gin.Context`, never a manual `ShouldBind`. Input-less routes take `_ struct{}`.
- Handlers embed `*serverHandler.Base` and declare their own `Routes()`. Every route
  carries `.Name(...)` or it is missing from the generated OpenAPI document.
- Repositories build queries with `dblib.Psql`, take a deadline from
  `cfg.GetDuration("db.QueryTimeout…")`, and map rows with
  `pgx.RowToStructByName[domain.X]`. A zero-row write returns `pgx.ErrNoRows`.
- Responses embed `port.StatusCodeAndMessage` with `json:",inline"` and take their
  status from the predefined `port.*Success` constants.
- Request structs live in `handler/request.go` and nowhere else, because `govalid`
  reads only that file. Run it, or every non-GET route answers 422.
- Repositories go into `FxRepo` as plain providers; handlers go into `FxHandler`
  wrapped in `fx.Annotate` with `fx.As` and `fx.ResultTags`. Use `fx_wire`.
- Never add a credential to `configs/*.yaml`. Never echo one that is already there.

## What to fetch, and when

| Handle | Fetch when |
|---|---|
| `@skill:handler-pattern` | writing or changing a handler, a route, or a handler constructor |
| `@skill:repository-pattern` | writing a query, or anything that touches the database |
| `@skill:domain-model` | adding a field, a table, or a new domain type |
| `@skill:request-dto` | adding or changing a request payload, a path parameter, or a query filter |
| `@skill:response-dto` | shaping a response, or choosing a status constant |
| `@skill:bootstrap-fx` | registering a new repository or handler — read before editing bootstrapper.go |
| `@skill:errors` | returning an error, or deciding a status code |
| `@skill:file-upload` | a route that accepts an upload or returns a file |
| `@skill:worked-example` | the full ten-step recipe, when you want to see every file at once |
| `@skill:config-keys` | reading a config value, or adding a key — every key, and which environments declare it |
| `@skill:legacy-patterns` | auditing or migrating a pre-template (api-* generation) service |
| `@skill:go-idiom` | general Go style questions — sits under the template rules, not over them |
| `@skill:data-access-library` | before editing any repository, or when a lint finding names a legacy library |
| `@skill:db-performance` | writing a repository method that touches the database more than once |
| `@skill:clients-and-context` | adding an outbound call, a new client, or anything that takes a context |
| `@skill:logging` | adding a log line, or deciding where to report an error |
| `@skill:legacy-migration` | converting or migrating a whole legacy api-* service to the n-api template — the step-by-step SOP |

## Verification

`rules_lint` runs after every edit batch and checks 41 rules. Every violation carries a one-line fix and a citation, so it can be acted on without fetching anything — call `list_rules` only if you need the whole set.

Pass `paths` with the files you changed. Findings elsewhere are reported but never block, which is what stops a stray legacy violation turning into unrequested work.

9 legacy-pattern rules run only under `legacy_audit` — see `@skill:legacy-patterns`. They never fire during ordinary edits: a pre-template service trips roughly 1,700 compliance findings, which would bury the two you actually caused.
