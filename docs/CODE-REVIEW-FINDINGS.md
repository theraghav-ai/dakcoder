# What the manual review says dakcoder should check

> **Sources**, two of them:
>
> 1. `code-review/` — 42 PDFs exported from the IT 2.0 manual code review of 41
>    production Go microservices. `Code_Review_Main.pdf` is the roll-up; the
>    other 41 are per-service sheets. **17,480 observations.**
> 2. **The reviewers' 22 standing suggestions** (§5) — policy written for the
>    coding agent directly, rather than derived from a service. Where the two
>    disagree, the suggestions win: they are the considered version of what the
>    sheets say in shorthand.
>
> **Purpose**: these are the defects human reviewers actually found, plus the
> policy they want enforced going forward. Anything here that `gotools lint`
> cannot catch is a defect dakcoder is free to reproduce. This document is the
> gap analysis and the proposed closure.

---

## 1. The corpus

| | |
|---|---|
| Services reviewed | 41 |
| Total observations | 17,480 |
| Closed | 8,251 |
| Still open | 9,229 |

Every per-service sheet has the same column schema, which tells us the reviewers
worked from a fixed checklist:

```
Db/batch │ Description │ Status │ Temporal │ Description.1
```

plus a second sheet per service:

```
Struct Name │ Column Name      ("Validations to be added")
```

The roll-up carries two more columns, tracked per service:

```
Bootstrap library update status (V0.0.36) │ API version update status
```

So the manual review has four axes: **database round trips**, **what belongs on
Temporal**, **request validation depth**, and **CEPT library currency** — that
last one confirming suggestion #2 is not a new idea but an existing column that
no tool has ever filled in. A fifth, smaller axis — general Go hygiene — is
carried in free text in the `Description` column.

---

## 2. The taxonomy, measured

Counts are matching lines across all 41 sheets, and the number of distinct
services in which the category appears. Service-spread is the better signal:
it says how systemic the problem is.

| # | Category | Lines | Services | Caught today? |
|---|---|---:|---:|---|
| 1 | DB batching — use `pgx.Batch` | 748 | 33 | **no** |
| 2 | Temporal / Nexus / async offload | 459 | 29 | **no** |
| 3 | Drop the needless transaction | 100 | 15 | **no** |
| 4 | Combine queries — CTE / JOIN / UNION | 77 | 8 | **no** |
| 5 | Remove `rows.Next()` / `row.Scan()` | 55 | 30 | **partly** |
| 6 | Validation tags — numeric `min`/`max` | 42 | 39 | **partly** |
| 7 | Validation tags — string constraints | 37 | 34 | **partly** |
| 8 | N+1 — query inside a loop | 26 | 10 | **no** |
| 9 | `switch` instead of if/else-if chain | 19 | 5 | **no** |
| 10 | Multiple repo calls in one handler | 18 | 5 | **no** |
| 11 | MinIO / file upload placement | 17 | 9 | **no** |
| 12 | `ctx` naming, not `gctx` | 16 | 2 | **no** |
| 13 | `time.Now()` instead of SQL `NOW()` | 14 | 4 | **no** |
| 14 | Remove `fmt.Println` | 13 | 13 | **no** |
| 15 | Kafka / CDC for cross-service writes | 10 | 4 | **no** |
| 16 | No stored procedures | 10 | 2 | **no** |
| 17 | Hardcoded literals → constants | 9 | 3 | **no** |
| 18 | `SELECT *` / `COUNT(*)` | 7 | 4 | **no** |
| 19 | `dblib.SelectRows` over hand-rolled scans | 3 | 1 | **partly** |
| 20 | Log level and service name | 2 | 2 | **no** |

**The headline: the axes the humans spent their time on are the axes the linter
is blind to.** `gotools` has 30 rules — 21 compliance + 9 legacy — and not one
of them mentions `Batch`, `rows.Next`, `Println`, `Begin`, `transaction`,
`SELECT *`, `NOW()`, `gctx`, or `min`/`max`. Verified by grep over
`gotools/internal/rules/*.go`; every probe returned empty.

The existing rules enforce that generated code is **on-template and wired**.
The manual review is about whether it is **correct, fast and safe**. Those are
disjoint sets today.

---

## 3. Where the near-misses are

Four rules get close and stop short. These are the cheapest fixes.

**`repo-rowmapper`** requires a by-name mapper on `dblib.SelectRows` and
friends, and its reasoning about `RowToStructByPos` is exactly right. But it
only fires on calls that are *already* `dblib.*`. The pattern the reviewers
flagged 55 times across 30 services is the one that never reaches dblib at all:

```go
rows, _ := r.db.Query(ctx, sql, args...)
for rows.Next() { rows.Scan(&a, &b) }        // invisible to repo-rowmapper
r.db.QueryRow(ctx, sql, args...).Scan(&x)    // invisible to repo-rowmapper
```

**`repo-contract` has a false negative worth fixing now.** It requires a repo
method to call `context.WithTimeout` and to take the duration from
`cfg.GetDuration`. Both conditions are satisfied by this, which appears **11
times** in the PAO repo layer against 110 correct uses:

```go
ctx, cancel := context.WithTimeout(context.Background(), ur.Cfg.GetDuration("db.QueryTimeoutMed"))
```

The parent is `context.Background()`, so the request's cancellation and tracing
are severed while the rule reports clean. The check needs to assert the
*parent* is the incoming context, not merely that a timeout exists. This is
suggestion #8 arriving as a concrete defect in an existing rule.

**`request-dto`** requires *a* `validate` tag on every request field — which is
exactly the baseline suggestion #1 asks for, and it is already enforced. What
is missing is depth: 39 of 41 services were pulled up on numeric `min`/`max`
and 34 of 41 on string constraints. `validate:"required"` on a `Remarks string`
satisfies the rule today and satisfies no reviewer. Compounding it,
`packages/knowledge/references/request-dto.md` only ever teaches `required` and
`omitempty`, so the agent has never been shown the vocabulary.

**`go-idiom`** covers `any`, error strings, error wrapping and package naming.
It does not cover `fmt.Println`, `gctx`, or if/else-if chains, all three of
which are in the reviewers' standing free-text checklist:

> *1. For hard coded values use constants. 2. Avoid using `rows.Next()`/`rows.Close`.
> 3. Use `switch` instead of if-elseif wherever feasible. 4. Use `time.Now()`
> instead of `NOW()`, and the `time.Now()` value can be taken at the beginning
> of the func and used wherever needed. 5. Use meaningful variable names — for
> `context.Context` use `ctx` not `gctx`.*

That paragraph appears, near-verbatim, in 15 separate service sheets. It is a
rule set the reviewers had already written down; it just never reached the
linter.

---

## 4. The data-access library, and the batch idiom

### 4.1 There are two database libraries, and they are the same code

| | |
|---|---|
| **Old** | `gitlab.cept.gov.in/it-2.0-common/api-db` — pinned at `v1.0.32` |
| **New** | `gitlab.cept.gov.in/it-2.0-common/n-api-db` — pinned at `v0.0.1` |

Both are on disk in the module cache, so this is checked rather than assumed:

- **Identical file lists.** `find . -name '*.go'` over both modules diffs clean.
- **Identical exported API.** Every exported function — all 120 of them — is
  present in both, with the same names. The diff is empty.
- **The only difference is the `module` line in `go.mod`.**

So `n-api-db` is a re-publish of `api-db` under a new path, not a redesign.
**Migrating is a one-line import change with zero call-site edits.** That is
worth stating plainly in the knowledge base, because the alternative assumption
— that the new library is a rewrite needing a porting effort — is the reason
services put the migration off.

**The agent must know which one it is looking at**, because the two are
indistinguishable at the call site. Both are imported as `dblib`:

```go
dblib "gitlab.cept.gov.in/it-2.0-common/api-db"      // old
dblib "gitlab.cept.gov.in/it-2.0-common/n-api-db"    // new
```

Every `dblib.Psql`, `dblib.SelectRows` and `dblib.QueueExecRow` below the import
line is byte-identical either way. The import is the only evidence.

**Where each corpus stands.** The reference template has already migrated its
Go code — `new-template/repo/postgres/user.go:11` imports `n-api-db`, and no Go
file in the template imports `api-db`. The legacy service has not:
`pao-back-end-development` imports `api-db` in seven files. And one production
service is further behind still — the Service Desk review sheet carries a stack
trace from `api-db@v1.0.5`, twenty-seven patch releases behind what the template
pins.

**One inconsistency to fix in the template.** `new-template/go.mod` declares
`api-db v1.0.32` as a *direct* dependency (line 9) and `n-api-db v0.0.1` as
*indirect* (line 58). The code says the opposite. A `go mod tidy` would correct
it, and until it is corrected any tool reading `go.mod` to decide which
generation a service is on will get the wrong answer for the template itself.

**Good news for the rules**: `legacy-lib-generation` already maps
`api-db → n-api-db`, alongside `api-server`, `api-log`, `api-bootstrapper` and
`api-validation`.

**But the `n-` prefix is not universal, and the rule is right to list only
those five.** Querying the registry directly (§10 Q5): `n-api-config` and
`n-api-trace` do not exist. So `api-config v0.0.17` is not a legacy module
awaiting migration — it is simply the current config library, and flagging it
would be wrong. `n-api-errors` and `n-api-metrics` do exist but are not in the
map; whether `api-errors` is meant to be migrated is worth confirming, though
the template currently carries both as indirect dependencies.

The existing rule needs no change. It needs a knowledge reference behind it so
the agent can explain the fix rather than just report it.

### 4.2 The library already has a batch wrapper, and nobody uses it

This was an open question in an earlier draft. It is answered: `dblib` exports a
batch API above raw `pgx.Batch`, in **both** libraries.

```go
func NewTimedBatch(timeoutMs int) *TimedBatch        // wraps &pgx.Batch{}
func TimedQueueExecRow(batch *TimedBatch, builder sq.Sqlizer) error
func TimedQueueReturnBulk[T any](batch *TimedBatch, ...) error
func TimedQueueReturnRow[T any](batch *TimedBatch, ...) error
```

Usage across both corpora: **zero**. Every batch in the corpus is hand-rolled on
raw `pgx.Batch`. Whatever `TimedBatch` was built for, it never reached the
services.

**Decided (§10.1): it is not adopted.** The rules and the scaffolder use the raw
`pgx.Batch` drain-loop shape, which is what 8 of the 15 existing batch sites
already do and the only one that preserves per-statement error detail. The
endorsed snippet is in §10.1.

Two more things the library exposes that the corpus never touches, both relevant
to the batch-versus-transaction guidance:

- **`dblib.TxExec(ctx, tx pgx.Tx, builder)`** — usable, context-first, no
  framework dependency.
- **`dblib.Tx(gctx *gin.Context, dbPool *DB, f func(...) error, ...)`** — takes
  a **`*gin.Context`**. The template has moved off gin entirely: `gin` is only
  an *indirect* dependency in `new-template/go.mod`, and both
  `layer-sql-boundary` and `handler-signature` exist specifically to keep gin
  out of handlers and repositories. **`dblib.Tx` is therefore unusable in the
  current template architecture**, and the knowledge base should say so before
  someone reaches for the obvious-looking helper and has to take a direct gin
  dependency to make it compile.

### 4.3 The batch idiom in the corpus

The single largest category — 748 lines, 33 services — has a canonical
implementation sitting in the legacy corpus, and `dblib` already exports the
API for it. Usage counts across both corpora:

```
238  dblib.Psql                 36  dblib.QueueExecRow      ← the batch queue
109  dblib.SelectRows            2  dblib.QueueReturnBulk   ← batch + row mapper
 70  dblib.GenerateColumnsFromStruct
```

From `pao-back-end-development/repo/postgres/paogen.go:539`:

```go
batch := &pgx.Batch{}

updateBuilder := dblib.Psql.Update("pao.pfms_main").Set(...).Where(...)
if err := dblib.QueueExecRow(batch, updateBuilder); err != nil { return err }

for _, t := range request {
    insertBuilder := dblib.Psql.Insert("pao.pfms_detail").Columns(...).Values(...)
    if err := dblib.QueueExecRow(batch, insertBuilder); err != nil { return err }
}

results := ur.Db.SendBatch(ctx, batch)      // one round trip
defer results.Close()
```

`dblib.QueueReturnBulk(batch, query, pgx.RowToStructByName[domain.X], &dst)` is
the read-side equivalent, and it composes with the existing `repo-rowmapper`
rule rather than fighting it.

### Batch and transaction are not substitutes

The sheets say "use batch instead of transaction" bluntly, 100 times across 15
services. Suggestion #4 is the careful version, and it is the one the rules
should encode:

- **Batch** is a *round-trip* optimisation. Prefer it when the goal is fewer DB
  calls and the operations do not have to succeed or fail together.
- **Transaction** is an *atomicity* primitive. Use it when multiple operations
  must commit or roll back as one unit.
- They compose. Where both properties are wanted, queue the batch inside a
  transaction; a batch on its own is not an atomicity guarantee.

This changes the rule proposed below from "replace transactions with batches"
to two narrower, defensible checks: a transaction wrapping a *single* statement
is pure overhead, and a multi-statement sequence with no atomicity requirement
is a batching opportunity. Neither claims the other's territory.

Two facts that shape all of it:

- `packages/knowledge/` has **zero** occurrences of `batch`, `SendBatch`,
  `transaction`, `CTE`, `round trip` or `N+1`. The agent has never been told
  any of this. A rule that fires without a knowledge reference to fix from will
  just produce a loop.
- The legacy corpus carries **415** occurrences of `gctx`; the template carries
  zero and uses `ctx` throughout. That is a clean legacy-audit signal.

---

## 5. The reviewers' 22 suggestions, mapped

Each suggestion, what the code says about it, and what it becomes. "Evidence"
is what I found in the two corpora; where it is empty the suggestion is
forward-looking policy rather than an observed defect.

### 5.1 Confirms something already proposed

| # | Suggestion | Evidence | Becomes |
|---|---|---|---|
| 1 | Every request field carries a validate tag, `omitempty` at minimum | already enforced by `request-dto` | baseline holds; depth is the gap → `request-validate-depth` |
| 3 | Batch multiple DB round trips in a handler | 748 lines, 33 services | `repo-multi-roundtrip`, `handler-single-repo-call` |
| 10 | Avoid unnecessary/repeated DB calls | 748 lines, 33 services | `repo-multi-roundtrip` |
| 11 | N+1 detection, esp. calls inside loops | 26 lines, 10 services | `repo-batch-in-loop` |
| 4 | Prefer batch over transaction *where applicable* | 100 lines, 15 services | **refines** the rule — see §4 above |

### 5.2 New rules, with observed defects behind them

| # | Suggestion | Evidence in corpora | Becomes |
|---|---|---|---|
| 5 | Log at the handler layer, not the repository | **135 log calls across 7 PAO repo files**; 66 in `paogen.go` alone | `repo-no-logging` |
| 6 | New clients as bootstrap singletons, injected | `resty.New()` called **inside handler methods** at `paogen.go:2813`, `paogen.go:4172`, `transferentry.go:2024` — a fresh client per request | `client-singleton` |
| 8 | Propagate `context.Context`; flag `Background()`/`TODO()` in request paths | **22 occurrences**, incl. `repo/postgres/objection.go:304` and a `// ← FIXED: use context.Background() for all DB ops` comment at `paogen.go:5351` | `ctx-propagation`, plus the `repo-contract` fix in §3 |
| 13 | Reuse HTTP clients; pooling and timeouts | same three `resty.New()` sites | `client-singleton` (same rule) |
| 14 | Timeouts on external calls and DB ops | DB side already covered by `repo-contract`; **external calls are not** | `external-call-timeout` |
| 15 | No sensitive data in logs | not yet measured; `secrets-in-config` covers YAML only, never log arguments | `no-sensitive-logging` |
| 16 | New config through the config mechanism, with defaults | `config-key-exists` covers the reverse direction only (key used → must be declared) | `config-no-hardcode` |
| 22 | Inject dependencies rather than instantiate in business logic | the `resty.New()` sites again | `client-singleton` generalised |

The `resty.New()` finding is worth dwelling on: suggestions #6, #13 and #22 are
three framings of the same defect, it is observable in three lines of the legacy
corpus, and one rule closes all three. It is the highest ratio of policy
satisfied to rule written in this document.

### 5.3 Better served by a golangci-lint profile than by a new rule

Suggestions #7 (error handling), #9 (resource management) and #12 (concurrency
safety) are exactly what mature Go linters already do, and `go-idiom` explicitly
defers type-dependent checks to golangci-lint. The problem is scope:

**`gotools/.golangci.yml` lints the sidecar itself. Neither `new-template` nor
`pao-back-end-development` has a `.golangci.yml` at all.** So dakcoder has a
carefully-reasoned static-analysis profile for its own source and none for the
services it writes.

The fix is to ship a target-service profile. Starting from the existing one —
which already enables `bodyclose`, `noctx`, `errorlint`, `nilerr` and `errcheck`
with `check-type-assertions` — and adding:

| Linter | Serves | Why it is not already there |
|---|---|---|
| `rowserrcheck` | #9 | `rows.Err()` unchecked after iteration |
| `sqlclosecheck` | #9 | `rows`/`stmt` not closed |
| `contextcheck` | #8 | function passes a non-inherited context |
| `errchkjson`, `noctx` | #7, #14 | already on; keep |
| race detector in CI | #12 | `make ci` runs race tests for gotools; target services need the same |

One conflict to settle: the sidecar profile disables `gocritic`'s `ifElseChain`
with the note *"rule dispatch reads better as if/else than switch"*. The
reviewers want the opposite for service code (19 lines, 5 services). Both can be
right — different profiles, different code. The target-service profile should
enable it; the sidecar profile should keep its exemption.

### 5.4 Not a linter rule — review-mode prompt and agent behaviour

These are judgement calls a syntactic rule will get wrong. They belong in the
review-mode overlay and in the knowledge base, where they steer the agent
without gating a build.

| # | Suggestion | Why not a rule |
|---|---|---|
| 17 | Unit tests for new logic and error paths | `new-template` ships **zero** `_test.go` files, so there is no baseline and a gate would fire on every change. **Decided (§10.6): no scaffolded tests, no rule** — the agent may remark on missing tests during review, nothing more. |
| 18 | Backward compatibility of APIs, DTOs, schemas | requires diffing against the deployed contract, not reading a file. Candidate for a `contract-diff` tool later; see §7. |
| 19 | Flag significant duplication, avoid trivial abstraction | "significant" is the whole question. Threshold-based clone detection produces mostly noise at this scale. |
| 20 | Avoid over-engineering | this is a *constraint on every fix the agent emits*, not a check. See below. |
| 21 | API contract consistency (status codes, error shapes) | partly covered by `response-dto`, `response-status`, `handler-signature`; the remainder is convention-matching best done by the model with the KB in context. |

**Suggestion #20 deserves separate weight.** It is not one item in a list of 22;
it governs how every other rule's `Fix(...)` string should be written, and it
independently supports the §6.3 decision to keep CTE consolidation and Temporal
placement out of the gate. A linter that suggests an abstraction is a linter
that will be ignored. Every rule below emits the smallest edit that resolves
the finding, and no rule proposes a new interface, helper or layer.

### 5.5 A new tool, not a rule

**Suggestion #2 — CEPT library version check.** The roll-up sheet already has
`Bootstrap library update status (V0.0.36)` and `API version update status` as
per-service columns, so this is an existing manual chore, not a new idea. The
suggestion is explicit that updating must **not** be mandatory — so this is
reporting, never a gate. See `gotools lib-version-check` in §7.

---

## 6. Proposed rules

Naming follows the existing convention. Severities follow the existing
philosophy: gate on contract breaches and runtime-visible defects, warn on
judgement calls, so nobody learns to reach for `nolint`.

### 6.1 Tier 1 — mechanical, low false-positive, worth gating

| ID | Sev | Checks | Fix it emits | From |
|---|---|---|---|---|
| `repo-batch-in-loop` | error | a `dblib.Insert/Update/Delete/Exec/SelectOne/SelectRows` call inside a `for`/`range` body in `repo/` | queue with `dblib.QueueExecRow(batch, b)`, send once via `db.SendBatch(ctx, batch)` | corpus, #11 |
| `repo-raw-rows` | error | `rows.Next()`, `rows.Scan`, `rows.Close`, `.QueryRow(...).Scan(...)` anywhere in `repo/` | `dblib.SelectRows(ctx, db, q, pgx.RowToStructByName[domain.X])` | corpus, #9 |
| `repo-select-star` | error | `Select("*")`, `Select("count(*)")`, or a `SELECT *` string literal | `dblib.GenerateColumnsFromStruct(domain.X{})` | corpus |
| `no-stored-procedure` | error | `CALL ` / `call ` prefix in a query string, or `SELECT * FROM <proc>(` | move the logic into Go and a Squirrel query | corpus |
| `no-fmt-print` | error | `fmt.Print`/`Println`/`Printf` outside `_test.go` and `main.go` | `log.Debug(ctx, "…")` | corpus |
| `ctx-propagation` | error | `context.Background()` / `context.TODO()` outside `main.go`, `bootstrap/` and tests; **and** `context.WithTimeout` whose parent is not the incoming ctx | derive from the request ctx: `context.WithTimeout(ctx, …)` | **#8** |
| `client-singleton` | error | a client constructor (`resty.New`, `minio.New`, `http.Client{}`, `kafka.NewWriter`) called inside a handler, service or repo method body | provide it once in `bootstrap/`, inject the pointer | **#6, #13, #22** |
| `no-sensitive-logging` | error | a `log.*` argument named `password`, `token`, `authorization`, `secret`, `otp`, `aadhaar`, `pan`, `mobile`, or a whole request/response struct | log identifiers, not payloads | **#15** |
| `repo-multi-roundtrip` | warning | **2** sequential dblib exec calls with no `pgx.Batch` in scope → note it; **3 or more** → recommend batching *where feasible*. Never mandatory (§10.2) | at 2: "a batch is possible here"; at 3+: "batch these where feasible" | corpus, #3, #10 |
| `repo-transaction-scope` | warning | (a) `Begin`/`BeginTx` in a method issuing ≤1 statement; (b) ≥3 statements, no batch, no stated atomicity need | (a) drop it — one statement is already atomic; (b) batch it, or keep the transaction if they must roll back together | corpus, **#4** |
| `repo-no-logging` | warning | `log.*` calls in the `repo/` layer | return the error; log once in the handler where the request context is | **#5** |
| `external-call-timeout` | warning | an outbound client call with no timeout, deadline or `ctx` carrying one | set the timeout on the injected client, or pass a deadline-bearing ctx | **#14** |
| `repo-sql-now` | warning | `NOW()` / `CURRENT_TIMESTAMP` inside a Squirrel expression | hoist `now := time.Now()` to the top of the func and pass it | corpus |
| `request-validate-depth` | warning | string field with `required` but no `max`/`len`/`oneof`/format; numeric field with no `min`/`max` | `validate:"required,max=64"` / `validate:"required,min=1,max=9999"` | corpus, **#1** |
| `config-no-hardcode` | warning | literal URL, host, port or duration in handler/repo/service code | declare it in `configs/*.yaml` with a default and read via `cfg.Get*` | **#16** |
| `ctx-naming` | warning | a `context.Context` parameter or variable not named `ctx` | rename to `ctx` | corpus |

Three of these are worth singling out:

- **`repo-raw-rows`** — 30 of 41 services, and it closes the exact hole
  `repo-rowmapper` leaves open. It should share that rule's citation and its
  reasoning about positional binding.
- **`client-singleton`** — one rule, three suggestions (#6, #13, #22), three
  observable defect sites in the legacy corpus.
- **`request-validate-depth`** — highest service-spread in the corpus (39/41),
  but it must ship **with** the `request-dto.md` knowledge extension in §8. A
  rule that says "add a constraint" to an agent that has only seen `required`
  and `omitempty` will produce `validate:"required,required"`.

### 6.2 Tier 2 — advisory, real signal, genuine false-positive risk

| ID | Sev | Checks | Note |
|---|---|---|---|
| `handler-single-repo-call` | warning | a handler method calling `h.repo.*` more than once | reviewers' "Morethan one repo calls in handler" (#3). Legitimate multi-call flows exist; advisory only |
| `prefer-switch` | warning | if/else-if chain ≥3 branches testing the same operand | from the standing checklist. Enable in the target-service golangci profile via `gocritic.ifElseChain` rather than hand-rolling it |
| `magic-literal` | warning | the same string/int literal ≥3 times in one file | "For hard coded values use constants" |
| `log-level-hygiene` | warning | `log.Info` whose argument is a struct or a whole response | *"Many Debug messages are being printed as Log.Info… Avoid printing the whole response as Info → make them Debug."* Pairs with `no-sensitive-logging` |

### 6.3 What should deliberately **not** become a rule

**CTE / JOIN / UNION consolidation** (77 lines, 8 services). Whether two selects
should become one CTE depends on cardinality and the business meaning of the
join. A linter that guesses will be wrong often enough to be switched off — and
suggestion #20 says so directly. Knowledge base and review-mode prompt, not the
gate.

**Temporal placement** (459 lines, 29 services). Same reasoning, more so: an
architectural judgement about failure semantics. It gets a *reporting* tool in
§7, not a rule. What can be said mechanically is only "this handler does file
I/O / an outbound HTTP call / an SMS send" — a candidate list, not a violation.

**Decided (§10.3): deprioritised entirely.** Temporal is not in wide enough use
to build guidance around, so the recommendations in the sheets are set aside for
now. `temporal-audit` still ships, as a report with no advice attached, and it
is sequenced last. This is the largest category in the corpus that we are
deliberately not acting on.

**Everything in §5.4.** Tests, backward compatibility, duplication,
over-engineering and API-convention consistency are all review-mode concerns.

---

## 7. Proposed `gotools` commands

The manual review is fundamentally a set of reports. `gotools` should produce
them, so the next review round is a diff against a baseline rather than 41
people reading code.

### `gotools db-roundtrip-audit`

Reproduces the `Db/batch` column — 748 findings, the largest single bucket, and
suggestions #3, #10 and #11. Per repository method:

```
method                     stmts  in-loop  batched  txn  verdict
UpdatePfmsMain                 9     yes      yes    no  ok
CreateBagBarcode               4      no       no   yes  batch: 4 stmts, txn unnecessary
ListArticlesByBag              1      no       no   yes  drop txn: single statement
```

Ranked by `stmts × (in-loop ? 10 : 1)`, so the N+1s surface first. This is the
one tool that would have replaced the most human hours.

### `gotools validation-audit`

Reproduces the second sheet verbatim — struct name, field name, current
`validate` tag, and what is missing (#1). Roughly 40% of all rows in the corpus
are this table. Output should be diffable so a service can prove it closed the
gap.

### `gotools lib-version-check`

Suggestion #2, and the roll-up's two existing columns. Reads `go.mod`, resolves
the latest published versions of the `gitlab.cept.gov.in/it-2.0-common/*`
modules, and reports drift.

**The mechanism is verified working** (§10.5). `GOPRIVATE` is already set to
`gitlab.cept.gov.in`, so `go list -m -versions <module>` resolves straight from
the VCS and needs no token. Real output against `new-template`:

```
module                current   latest    status
n-api-bootstrapper    v0.0.14   v0.0.22   behind (8)
n-api-db              v0.0.1    v0.0.8    behind (7)
n-api-validation      v0.0.3    v0.0.7    behind (4)
api-db                v1.0.32   v1.0.34   behind (2), SUPERSEDED → n-api-db
n-api-log             v0.0.1    v0.0.1    current
api-config            v0.0.17   v0.0.17   current — no n- successor exists
```

There are **two** kinds of drift and the report needs both. A service can be on
the newest release of a library that has been replaced wholesale: reporting
`api-db v1.0.32` as merely "2 behind" would miss that the module is superseded
and that, per §4.1, migrating is a one-line import change. The generation map
for the second column already exists in `legacy-lib-generation`, so the tool
should read it from there rather than keeping a second copy — and, per §4.1,
that map deliberately does not cover every `api-*` module.

**Reports only — never gates, never auto-updates.** The suggestion is explicit
on this, and it is the right call: a coding agent that bumps a shared library
mid-review turns a code review into a regression hunt. Surface it to the user
and let them decide.

### `gotools temporal-audit`

Candidate reporter, not a gate. Flags handler and repo methods that perform:

- MinIO / file upload or download (9 services flagged this)
- SMS, email or notification sends — *"sms can be pushed to async temporal"*
- report or document generation — *"GeneratePayBill sent to Temporal"*
- outbound service-to-service HTTP — *"API to API to be done by nexus-temporal"*
- payment gateway or wallet writes — *"update to wallet should be either a
  kafka call or a temporal call; this is error prone"*

Output is a ranked candidate list with triggering call sites, for a human to
accept or reject.

**Scope, per §10.3**: report only, no recommendations, sequenced last. Temporal
is not in wide enough use for the tool to advise on what should move where.

### A target-service `.golangci.yml`

Per §5.3 — the profile dakcoder runs against the services it writes, as opposed
to `gotools/.golangci.yml` which lints the sidecar. Ship it as a scaffolder
output so every new service gets it, and add it to the verification gate. This
is the cheapest way to satisfy #7, #9 and #12.

### Scaffolder extensions

- `resource_scaffold`: for bulk operations, emit the batch-shaped repo method
  (`&pgx.Batch{}` → `QueueExecRow` in the loop → the §10.1 drain loop) rather
  than a loop of single statements. The scaffolder currently teaches the shape the
  reviewers reject 748 times.
- `resource_scaffold`: derive `min`/`max`/`len` on `validate` tags from the
  spec's field constraints instead of emitting bare `required`.
- `project_scaffold`: emit the target-service `.golangci.yml`, and a
  `bootstrap/clients.go` FX provider so the first Resty or MinIO client has an
  obvious home (#6).
- ~~A `temporal-activity` scaffolder~~ — dropped by §10.3.

### Later, if the appetite exists

`contract-diff` for suggestion #18 — compare the generated `docs/v3Doc.json`
and the domain structs against the previous commit, and flag removed fields,
narrowed types and changed status codes. Deferred because it needs a baseline
artefact to diff against, which is a deployment question rather than a linting
one.

---

## 8. Knowledge base additions

Rules without a reference to fix from produce loops. In priority order:

1. **`references/data-access-library.md`** — new, and the one the agent is
   missing most, because it cannot infer any of it from the code it is editing:

   - There are two libraries, `api-db` (old) and `n-api-db` (new), and **they
     are the same code under two module paths** — identical files, identical
     exported API. Migration is a one-line import change, not a port.
   - Both are imported as `dblib`, so **the import line is the only way to tell
     which one a file is using**. Everything below it looks the same.
   - The `n-` prefix is the new generation of *all* the CEPT libraries —
     `n-api-server`, `n-api-log`, `n-api-bootstrapper`, `n-api-validation` —
     not a db-specific convention. This is what `legacy-lib-generation` already
     enforces; the reference gives the agent the *why* so it can explain the fix.
   - `dblib.Tx` takes a `*gin.Context` and is unusable in the current template;
     `dblib.TxExec` is the context-first one. See §4.2.
   - `TimedBatch` exists and is unused; §10.1 decided against adopting it.

2. **`references/db-performance.md`** — the batch idiom quoted from
   `paogen.go:539`, `QueueExecRow` vs `QueueReturnBulk`, the batch-vs-transaction
   distinction from §4, when to reach for a CTE instead, and the N+1 shape with
   its fix. Currently the KB says nothing about any of this.
3. **`references/request-dto.md` extension** — the full `validate` vocabulary
   with numeric and string constraints (#1), since 39 of 41 services were pulled
   up on exactly the tags this file fails to teach.
4. **`references/clients-and-context.md`** — new. Bootstrap-singleton clients
   and injection (#6, #13, #22), context propagation through handler → service →
   repo (#8), and timeouts on external calls (#14). These three suggestions are
   one story and should be taught as one.
5. **`references/logging.md`** — new. Log at the handler layer (#5), levels
   (#20 in the sheets: Debug vs Info), never log payloads or secrets (#15), and
   the service-name config finding from the Service Desk sheet.
6. **`references/async-boundaries.md`** — what belongs on Temporal, what belongs
   on Kafka/CDC, and what stays inline. Sourced from the `Temporal` column
   across all 41 sheets.
7. **Review-mode overlay additions** — the §5.4 judgement items: tests for new
   logic (#17), backward compatibility (#18), duplication (#19),
   over-engineering (#20), API convention consistency (#21).

---

## 9. Suggested sequencing

**First — pure syntax, unambiguous, cite the reviewers' own checklist.**
`repo-raw-rows`, `no-fmt-print`, `repo-select-star`, `no-stored-procedure`,
`ctx-naming`, plus the `repo-contract` parent-context fix from §3. Together they
cover 5 corpus categories and ~30 services, and the `repo-contract` fix closes a
live false negative. Add them to `legacy-audit` too; `ctx-naming` alone will
report 415 sites in the PAO service.

Write `references/data-access-library.md` alongside them. It is the cheapest
item in this document and the one the agent is most blind without: nothing in
the code tells it that `api-db` and `n-api-db` are the same library under two
paths, and `legacy-lib-generation` already fires on the import without being
able to explain why the fix is safe.

**Second — the client and context story.** `references/clients-and-context.md`,
then `client-singleton`, `ctx-propagation` and `external-call-timeout`. One
reference, three rules, four suggestions (#6, #8, #13, #14, #22) — the best
return in the document, and every rule has an observed defect behind it.

**Third — the target-service `.golangci.yml`.** Satisfies #7, #9 and #12 with
configuration rather than code, and it is a prerequisite for trusting any of the
Tier 2 style rules.

**Fourth — the batching bucket.** `db-performance.md`, then
`repo-batch-in-loop`, `repo-multi-roundtrip` and `repo-transaction-scope`.
Reference before rule, so the agent can act on the finding. This is the
748-line category.

**Fifth — validation.** `request-dto.md` extension, then
`request-validate-depth`, then `gotools validation-audit`. Highest
service-spread in the corpus.

**Sixth — the reports.** `db-roundtrip-audit` and the `resource_scaffold` batch
shape, so new code stops arriving with the defect.

**Seventh — logging.** `logging.md` with `repo-no-logging`,
`no-sensitive-logging` and `log-level-hygiene`. `no-sensitive-logging` ships as
a warning until the field list has an owner (§10.4).

**Last — `temporal-audit`, report only.** Deprioritised by §10.3. No
`async-boundaries.md`, no recommendations attached: the tool lists candidate
call sites and stops there. Revisit when Temporal adoption is settled.

**Out of band, and cheap — `lib-version-check`.** It is verified working with
no token (§10.5) and it is independent of everything above, so it can be built
whenever there is a gap. Worth doing sooner than its position here suggests:
the reference template is currently behind on four of its own libraries,
including 7 releases on `n-api-db`, and nothing surfaces that today.

---

## 10. Decisions taken

All six questions this document raised have been answered by the template
owner. Recorded here as the decision log; the rules and tools above are written
against these answers.

| # | Decision | Effect |
|---|---|---|
| 1 | Batches use the raw `pgx.Batch` drain-loop shape | rules and scaffolder cite one fixed snippet |
| 2 | Flag at 2 queries, recommend batching at 3, never mandatory | `repo-multi-roundtrip` becomes two-tier, warning-only |
| 3 | Temporal is deprioritised — report only | `temporal-audit` ships last, with no recommendations |
| 4 | The drafted sensitive-field list stands | `no-sensitive-logging` ships with it, configurable |
| 5 | Registry is readable — **verified working, no token needed** | `lib-version-check` can be built as designed |
| 6 | Do not scaffold tests unless explicitly asked | nothing to build; coverage stays a review-time remark |

---

### 1. The endorsed batch shape

**Decided**: the drain-loop form. `TimedBatch` is not adopted.

```go
results := ur.Db.SendBatch(ctx, batch)
if results != nil {
    defer results.Close()
    for i := 0; i < batch.Len(); i++ {
        if _, err := results.Exec(); err != nil { return err }
    }
}
```

This is the shape that preserves per-statement error detail, and it is what 8
of the 15 existing batch sites already do. It becomes the canonical snippet in
`references/db-performance.md`, the `Fix(...)` string on every batching rule,
and what `resource_scaffold` emits.

Two consequences worth noting:

- The seven sites using `SendBatch(ctx, batch).Close()` are now
  off-pattern. They are not broken — they still surface failures — so this is
  cleanup, not a defect to chase.
- `dblib.TimedBatch` and the `TimedQueue*` family stay unused. If they are ever
  adopted, one rule and one scaffolder template change.

### 2. The round-trip thresholds

**Decided**: two tiers, and batching is never mandatory.

| Queries in one function | What the agent does |
|---|---|
| 1 | nothing |
| 2 | flag it — note that a batch is possible |
| 3 or more | recommend batching **where feasible** |
| any | never blocks; the developer decides |

The word *feasible* is doing real work here and the rule must respect it. A
batch is not always available — the second query may depend on the first
query's result, in which case there is nothing to batch. So the finding is
phrased as an observation with a suggestion, never as a violation, and
`repo-multi-roundtrip` stays a warning at both tiers.

Measured against the 170 repository functions in the two corpora, this flags 8
functions at tier one and 4 at tier two.

### 3. Temporal — report only, and last

**Decided**: ignore the Temporal recommendations for now. It is not used
widely enough to build guidance around.

This is the largest single category we are *not* acting on — 459 findings
across 29 services — so the reasoning is worth recording: the review sheets
recommend `nexus-temporal` for work that should move off the request path, but
the template has no Temporal wiring, and only one service in the corpus has any
Temporal code at all. Guidance written now would be guessing.

What still ships, unchanged from §7: `gotools temporal-audit` as a **report**.
It lists handler and repo methods doing file I/O, SMS or email sends, report
generation, or outbound service calls, and says "review these". It attaches no
recommendation and it is sequenced last. When Temporal adoption is settled, the
report becomes the input to that work rather than something to rebuild.

### 4. The sensitive-field list

**Decided**: ship the draft.

```
password  token  authorization  secret  otp  pan  mobile  email
aadhaar (all four spellings: aadhaar, aadhar, adhar, aadhaar_number)
account_number  ifsc  card  cvv  pin  dob
```

Per §5.2 this matches **whole field names, not fragments** — the measurement
that drove that decision was `pan` matching 20 field names in the review sheets
of which 19 were harmless (`CompanyName`, `Discrepancy`,
`NoOfPanchayatSanchaarSevaKendras`). The list is per-service configurable so a
service with an unusual field can extend it without waiting on us.

No owner was named, so `no-sensitive-logging` ships as a **warning** rather
than an error. Promoting it to a build failure should wait until someone owns
the list, since that is the point at which false positives become expensive.

### 5. Registry access — tried, and it works

**Decided**: try reading the registry. **Result: it works, with no token.**

`GOPRIVATE` is already set to `gitlab.cept.gov.in`, so Go resolves these
modules directly from the VCS rather than through `proxy.golang.org`. Both of
these succeed on this machine as-is:

```bash
go list -m -versions gitlab.cept.gov.in/it-2.0-common/n-api-db
git ls-remote --tags https://gitlab.cept.gov.in/it-2.0-common/n-api-db.git
```

So `lib-version-check` can be built as originally designed — no token to
provision, no hand-maintained manifest, no deferral. It shells out to
`go list -m -versions`, which is the same mechanism the Go toolchain already
uses and which respects whatever credentials the developer's machine has.

**The first real drift report.** Running it by hand against `new-template`:

| Module | Pinned | Latest | Status |
|---|---|---|---|
| `n-api-bootstrapper` | v0.0.14 | **v0.0.22** | behind 8 |
| `n-api-db` | v0.0.1 | **v0.0.8** | behind 7 |
| `n-api-validation` | v0.0.3 | **v0.0.7** | behind 4 |
| `api-db` | v1.0.32 | v1.0.34 | behind 2, **superseded → n-api-db** |
| `n-api-server` | v0.0.17 | v0.0.18 | behind 1 |
| `n-api-log` | v0.0.1 | v0.0.1 | current |
| `api-config` | v0.0.17 | v0.0.17 | current — no `n-` successor exists |

**The reference template is behind on four of its own libraries**, including 7
releases on the new database library it just migrated to. Since the template is
the contract every generated service is held against, this is worth a look
independently of anything in this document.

For contrast, `pao-back-end-development` pins `api-bootstrapper v0.0.36` —
which is exactly the version named in the roll-up sheet's *"Bootstrap library
update status V0.0.36"* column. Latest is now **v0.0.67**. That column was
tracking a migration target that has since moved 31 releases.

As decided earlier: this tool **only reports**. It does not update anything and
does not fail a build.

### 6. Tests

**Decided**: do not scaffold tests unless explicitly asked.

So `resource_scaffold` and `project_scaffold` are unchanged, and there is no
test-coverage rule. Suggestion #17 stays what it already was — something the
agent may remark on during review when it sees new business logic arrive
without tests, with no threshold and no gate behind it.

This also settles why suggestion #17 could not have become a rule anyway:
`new-template` ships zero `_test.go` files, so there is no baseline to measure
against, and a rule would have fired on every change from day one.
