package kb

// The references in this file come from the manual code review of 41
// production services, recorded in docs/CODE-REVIEW-FINDINGS.md.
//
// They are hand-authored rather than extracted, because skill.md has nothing to
// say about any of it. The template teaches the shape of a service; these four
// documents carry what 17,480 review findings said about running one. Both
// halves are knowledge the agent needs, and neither can be derived from the
// other.
//
// Each exists because a rule fires without it. A linter that says "batch these"
// to an agent that has never seen a batch produces a loop, not a fix.

// reviewReferences are appended to References in references.go.
var reviewReferences = []Reference{
	{
		Slug:    "data-access-library",
		Title:   "The data-access library: api-db and n-api-db",
		Purpose: "before editing any repository, or when a lint finding names a legacy library",
		Intro: "There are two database libraries and they are the same code. Knowing that is " +
			"the difference between a one-line change and a migration nobody schedules.\n\n" +
			"Enforced by `legacy-lib-generation`; reported by `gotools lib-version-check`.",
		Body: `## Two libraries, one codebase

| | |
|---|---|
| **Old** | ` + "`gitlab.cept.gov.in/it-2.0-common/api-db`" + ` |
| **New** | ` + "`gitlab.cept.gov.in/it-2.0-common/n-api-db`" + ` |

Compared module-to-module in the module cache, these are **identical**: the same
file list, the same exported API — all ~120 functions, same names, same
signatures. The only difference is the ` + "`module`" + ` line in ` + "`go.mod`" + `.

` + "`n-api-db`" + ` is a re-publish of ` + "`api-db`" + ` under a new path, not a redesign.

**Migrating is a one-line import change with zero call-site edits.** Nothing
below the import needs to be touched.

## You cannot tell them apart at the call site

Both are imported under the same alias:

` + "```go" + `
dblib "gitlab.cept.gov.in/it-2.0-common/api-db"      // old
dblib "gitlab.cept.gov.in/it-2.0-common/n-api-db"    // new
` + "```" + `

Every ` + "`dblib.Psql`" + `, ` + "`dblib.SelectRows`" + ` and ` + "`dblib.QueueExecRow`" + ` below that line is
byte-identical either way. **The import line is the only evidence.** When you
need to know which generation a file is on, read the import — never infer it
from the calls.

## The ` + "`n-`" + ` prefix is not universal

The new generation covers five libraries, and only these five:

| Legacy | Current |
|---|---|
| ` + "`api-server`" + ` | ` + "`n-api-server`" + ` |
| ` + "`api-db`" + ` | ` + "`n-api-db`" + ` |
| ` + "`api-log`" + ` | ` + "`n-api-log`" + ` |
| ` + "`api-bootstrapper`" + ` | ` + "`n-api-bootstrapper`" + ` |
| ` + "`api-validation`" + ` | ` + "`n-api-validation`" + ` |

` + "`n-api-config`" + ` and ` + "`n-api-trace`" + ` **do not exist**. So ` + "`api-config`" + ` is the current
config library, not a migration target — do not "modernise" it.

## Two traps in the library

**` + "`dblib.Tx`" + ` takes a ` + "`*gin.Context`" + `.** It is the obvious-looking transaction
helper and it is unusable here: the template has moved off gin, so calling it
means taking a direct gin dependency in the repository layer, which
` + "`layer-sql-boundary`" + ` exists to prevent. Use ` + "`dblib.TxExec(ctx, tx, builder)`" + `,
which is context-first.

**` + "`dblib.TimedBatch`" + ` is not adopted.** ` + "`NewTimedBatch`" + ` and the ` + "`TimedQueue*`" + `
family exist in both libraries and are used by nothing in the template or in any
reviewed service. Use the raw ` + "`pgx.Batch`" + ` shape in ` + "`@skill:db-performance`" + `.
`,
	},
	{
		Slug:    "db-performance",
		Title:   "Database round trips: batching, transactions and N+1",
		Purpose: "writing a repository method that touches the database more than once",
		Intro: "Database round trips were the largest single category in the review of 41 " +
			"services — 748 findings across 33 of them.\n\n" +
			"Enforced by `repo-batch-in-loop`, `repo-multi-roundtrip`, `repo-transaction-scope`, " +
			"`repo-raw-rows`, `repo-select-star` and `repo-sql-now`; profiled by " +
			"`gotools db-roundtrip-audit`.",
		Body: `## The batch shape

This is the endorsed form. Use it exactly.

` + "```go" + `
batch := &pgx.Batch{}

updateBuilder := dblib.Psql.Update("pao.pfms_main").Set("closing_bal", req.ClosingBal).Where(...)
if err := dblib.QueueExecRow(batch, updateBuilder); err != nil {
    return err
}

for _, t := range request {
    insertBuilder := dblib.Psql.Insert("pao.pfms_detail").Columns(...).Values(...)
    if err := dblib.QueueExecRow(batch, insertBuilder); err != nil {
        return err
    }
}

results := r.db.SendBatch(ctx, batch)
if results != nil {
    defer results.Close()
    for i := 0; i < batch.Len(); i++ {
        if _, err := results.Exec(); err != nil {
            return err
        }
    }
}
` + "```" + `

**Drain the results; do not shortcut with ` + "`SendBatch(ctx, batch).Close()`" + `.** The
short form reports that *something* failed. The drain loop reports *which
statement* failed. That difference is the whole reason this shape was chosen.

For reads, ` + "`dblib.QueueReturnBulk(batch, q, pgx.RowToStructByName[domain.X], &dst)`" + `
is the equivalent, and it keeps the by-name mapping ` + "`repo-rowmapper`" + ` requires.

## Batch is not a transaction

They solve different problems and are not substitutes.

| | Batch | Transaction |
|---|---|---|
| Buys you | fewer round trips | all-or-nothing |
| Use when | the statements are independent | they must commit or roll back together |

They compose: where both properties are wanted, queue the batch **inside** a
transaction. A batch on its own is not an atomicity guarantee.

Two corollaries:

- A transaction around a **single** statement buys nothing. One statement is
  already atomic in Postgres.
- "Use batch instead of transaction" — which the review says bluntly, 100 times
  — means *where atomicity was not required in the first place*. Do not remove a
  transaction that is holding several writes together.

## N+1: the one that gates

A query inside a loop costs one round trip per element, so the request's cost is
the size of its input:

` + "```go" + `
for _, id := range ids {
    row, err := dblib.SelectOne(ctx, r.db, q(id), m)   // N round trips
}
` + "```" + `

Queue them into a batch, or rewrite as a single query with ` + "`WHERE id = ANY($1)`" + `.
This is the only rule in the family that blocks: the others cost latency, this
one costs availability.

## Thresholds

| Calls in one method | What happens |
|---|---|
| 1 | nothing |
| 2 | noted — a batch may be possible |
| 3 or more | batching recommended **where feasible** |

Never mandatory. When the second query needs the first one's result there is
nothing to batch, and that is a perfectly good answer.

## Also in this family

- **Name your columns.** No ` + "`SELECT *`" + `: a new column silently changes every
  result. ` + "`dblib.GenerateColumnsFromStruct(domain.X{})`" + ` keeps the list and the
  struct in step.
- **Never scan by hand.** No ` + "`rows.Next()`" + `, no ` + "`QueryRow(...).Scan(...)`" + `. Both
  bind by position, so adding a column shifts every field after it with no error
  raised. Use ` + "`dblib.SelectRows`" + ` with ` + "`pgx.RowToStructByName`" + `.
- **Timestamps come from Go.** ` + "`time.Now()`" + ` once at the top of the function, not
  ` + "`NOW()`" + ` in the SQL — the database clock and the request clock differ, and
  inside a batch ` + "`NOW()`" + ` returns the transaction start time for every statement.
- **No stored procedures.** Invisible to the compiler, to review and to git.
`,
	},
	{
		Slug:    "clients-and-context",
		Title:   "Clients, context and deadlines",
		Purpose: "adding an outbound call, a new client, or anything that takes a context",
		Intro: "Three review findings that are one defect wearing three hats: clients built " +
			"per request, dependencies constructed instead of injected, and contexts that do " +
			"not descend from the request.\n\n" +
			"Enforced by `client-singleton`, `ctx-propagation`, `external-call-timeout` and " +
			"`repo-contract`.",
		Body: `## Build clients once, inject them

A client constructed inside a handler is rebuilt on every request, and so is its
connection pool. Nothing is reused, keep-alive never helps, and the pool limits
that exist to protect the upstream service become per-request rather than
per-process.

` + "```go" + `
// WRONG — a new client, and a new pool, per request
func (h *PensionHandler) Fetch(sctx *serverRoute.Context, req FetchRequest) (*resp.R, error) {
    client := resty.New().SetTimeout(15 * time.Second)
    ...
}
` + "```" + `

Provide it once in ` + "`bootstrap/`" + ` and inject the pointer:

` + "```go" + `
// bootstrap/clients.go
func NewRestyClient(cfg *config.Config) *resty.Client {
    return resty.New().SetTimeout(cfg.GetDuration("http.timeout"))
}

// handler
type PensionHandler struct {
    *serverHandler.Base
    svc  *repo.PensionRepository
    http *resty.Client        // injected
}
` + "```" + `

The same applies to MinIO, Kafka, Redis and gRPC clients. If it holds a
connection pool, it is built once.

## Propagate the context you were given

` + "`context.Background()`" + ` in a handler or repository discards the client's
cancellation, the request deadline and the trace id. When the caller hangs up,
the work carries on and nothing links the log lines back to the request.

` + "```go" + `
// WRONG — passes the rule that requires a timeout, and defeats its purpose
ctx, cancel := context.WithTimeout(context.Background(), cfg.GetDuration("db.QueryTimeoutMed"))

// RIGHT
ctx, cancel := context.WithTimeout(ctx, cfg.GetDuration("db.QueryTimeoutMed"))
defer cancel()
` + "```" + `

The wrong form is not hypothetical: it appears 11 times in the legacy corpus,
once with a comment explaining that it was added deliberately to work around an
already-cancelled parent. **If a parent context is cancelled too early, fix the
parent.** Detaching hides the problem and loses the trace.

` + "`context.Background()`" + ` is correct in exactly two places: ` + "`main.go`" + ` and
` + "`bootstrap/`" + `, which own the process lifetime rather than a request.

Name it ` + "`ctx`" + `. Not ` + "`gctx`" + `, and never ` + "`*context.Context`" + ` — Context is an
interface, so a pointer to one adds a nil check and buys nothing.

## Bound everything that leaves the process

Every database call already needs a deadline from config, which ` + "`repo-contract`" + `
enforces. Outbound HTTP needs one too: an unbounded call to a slow upstream is
how one dependency's bad afternoon becomes an exhausted worker pool here.

Set it on the injected client — ` + "`resty.New().SetTimeout(...)`" + ` — or pass a
context that already carries a deadline. Take the value from config, so it can
be tuned per environment without a rebuild.
`,
	},
	{
		Slug:    "logging",
		Title:   "Logging: where, at what level, and what never goes in",
		Purpose: "adding a log line, or deciding where to report an error",
		Intro: "Log once, in the layer that has the request, at a level someone can read " +
			"during an incident, and never log the payload.\n\n" +
			"Enforced by `repo-no-logging`, `no-sensitive-logging`, `log-level-hygiene`, " +
			"`error-handling` and `no-fmt-print`.",
		Body: `## Log in the handler, not the repository

The handler knows the route, the request id and the user. The repository knows
none of those, so a line written there is a message with no context attached —
and a repository that both logs and returns an error produces two entries for
one failure.

` + "```go" + `
// repository: return the error, say nothing
func (r *PensionRepository) FetchByID(ctx context.Context, id int64) (domain.Pension, error) {
    return dblib.SelectOne(ctx, r.db, q, pgx.RowToStructByName[domain.Pension])
}

// handler: log once, where the context is
p, err := h.svc.FetchByID(sctx.Ctx, req.ID)
if err != nil {
    log.Error(sctx.Ctx, "fetch pension %d: %v", req.ID, err)
    return nil, err
}
` + "```" + `

The review found 135 log calls inside one service's repository layer, 66 of them
in a single file.

## Levels

` + "`Info`" + ` is the stream someone reads at 3am during an incident. Keep it to
events, not to values.

| Level | For |
|---|---|
| ` + "`Error`" + ` | a request failed; always paired with the error being returned |
| ` + "`Warn`" + ` | something degraded but the request succeeded |
| ` + "`Info`" + ` | a business event worth counting |
| ` + "`Debug`" + ` | anything you needed while writing the code |

Never ` + "`fmt.Println`" + `. It has no level, no timestamp, no service name and no
request id, so in a container it is either invisible or it is noise in the
middle of structured JSON.

## Never log the payload

Logging a whole request or response is how personal data reaches the logs
without anyone deciding to put it there. Log identifiers — the record id — not
values.

These must never appear in a log line:

    password  token  authorization  secret  otp
    aadhaar (all spellings: aadhaar, aadhar, adhar)
    pan  mobile  phone  email  dob
    account_number  ifsc  card  cvv  pin  upi_id

The list is configurable per service in ` + "`.dakcoder/gotools.yaml`" + ` under
` + "`sensitive_fields`" + `, because services carry fields this list has not met.

One reason the spelling note matters: production request structs spell the same
identity number four ways — ` + "`AadhaarNumber`" + `, ` + "`Aadhaar_Number`" + `,
` + "`AadharNumber`" + `, ` + "`ReceiverAdharNo`" + `. Matching one spelling misses three.
`,
	},
}
