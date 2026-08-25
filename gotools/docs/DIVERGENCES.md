# Where the scaffolder does not copy the reference

`resource_scaffold` is meant to emit what the template does, not what we wish it
did. That principle is in the plan for a good reason: an agent that generates
its author's preferences instead of the house style produces code that reviewers
argue with rather than merge.

It is applied here with three exceptions, and this file exists so that each one
is a decision on the record rather than something a reviewer discovers.

Each entry says what the reference does, what the scaffolder does instead, and
what it would cost to reverse.

---

## 1. Create and update return the stored row

**Reference.** `UserRepository.CreateUser` inserts, then builds its return value
from the arguments it was handed:

```go
_, err := dblib.Insert(ctx, r.db, ins)
// ...
domainUser := domain.User{FirstName: firstName, LastName: lastName, /* ... */}
return domainUser, nil
```

The returned value has `ID: 0` and zero timestamps, so `POST /v1/users` answers
with `"id": 0` for every record it creates. A client cannot address the resource
it just created.

`UpdateUserByID` is worse. It builds its result by dereferencing every optional
pointer:

```go
updatedUser := domain.User{
	ID:        id,
	FirstName: *firstName,   // nil when the caller omitted first_name
	// ...
}
```

The handler only sets pointers for non-empty fields, so a partial update — the
only kind the endpoint is designed for — dereferences nil and panics. `PUT
/v1/users/1` with `{"city": "Nashik"}` takes the process down.

**Scaffolder.** Both use the returning variants that `n-api-db` already ships,
with an explicit `RETURNING` clause:

```go
ins := dblib.Psql.Insert(pensionTable).
	Columns(...).Values(...).
	Suffix("RETURNING id, ppo_number, amount, created_at, updated_at")

return dblib.InsertReturning(ctx, r.db, ins, pgx.RowToStructByName[domain.Pension])
```

`dblib.UpdateReturning` behaves the same way and, because it collects through
`pgx.CollectOneRow`, returns `pgx.ErrNoRows` for an update that matched nothing
— which is exactly what `repo-norows` asks for, obtained for free.

**Why not follow the reference.** Reproducing a nil dereference in every
resource the agent generates is not defensible on consistency grounds. The
alternative that stays closest to the reference — building the domain value from
only the non-nil pointers — still reports every untouched column as its zero
value, so the response would be wrong rather than fatal. Returning the stored
row is the only option that is actually correct.

**Cost to reverse.** Two template blocks in
`internal/scaffold/templates/resource/repository.go.tmpl`, plus a golden
refresh. Nothing else depends on it.

**Recommended.** Raise the reference `user` resource's update panic with the
template owner. It is a live defect independent of this programme.

---

## 2. Response DTOs carry timestamps

**Reference.** `skill.md` §Response DTO Pattern prescribes `CreatedAt` and
`UpdatedAt` as strings formatted `"2006-01-02 15:04:05"`. The shipped
`UserResponse` omits both. The reference document and the reference code
disagree — this is the divergence already recorded in `plan.md` §6.

**Scaffolder.** Follows `skill.md`: both fields present, string-formatted
through `spec.TimestampLayout`.

**Why.** `plan.md` §6 settles it — the rule follows the document and the golden
tests pin the document. `rules.DefaultConfig().TimestampLayout` reads the same
constant, so the linter and the scaffolder cannot drift apart on the format.

**Cost to reverse.** One template block and a constant.

---

## 3. `skill.md`'s worked example is not followed where it does not compile

The §Complete Example Workflow section is a documentation example rather than
shipped code, and three parts of it do not build against the current libraries.
The scaffolder follows the shipped `user` resource in each case.

| `skill.md` §Complete Example Workflow | Why it is not followed |
|---|---|
| `sq.Insert(...).PlaceholderFormat(sq.Dollar)` | `dblib.Psql` is that builder, already configured. `plan.md` §6 corrects this explicitly, and `repo-contract` enforces the corrected form. |
| `req.ToDomain()` on request DTOs | No `ToDomain` exists anywhere in the template; handlers pass fields positionally. `plan.md` §6. |
| `port.MetaDataResponse{TotalCount: …, Count: …}` | `MetaDataResponse` has no such fields. It declares `Skip`, `Limit`, `OrderBy`, `SortType`, `TotalRecordsCount`, `ReturnedRecordsCount`. The example does not compile; the scaffolder uses `port.NewMetaDataResponse(skip, limit, returned)` as the shipped `ListUsers` does. |
| `if err == pgx.ErrNoRows` inside the handler | That imports `pgx` into `handler/`, which `layer-sql-boundary` forbids and the shipped handler does not do. |

---

## Deliberately *not* divergences

Things that look like improvements and were left alone on purpose:

- **`ListXParams` is declared even when the handler takes `_ struct{}`.** The
  reference declares `ListUsersParams` and does not use it. Matching that keeps
  an unfiltered list byte-identical in shape to the reference, and the struct
  costs nothing.
- **Import aliases that repeat the package name** (`handler "pisapi/handler"`).
  Redundant, and the reference does it.
- **`AddPrefix("")` with full paths in `Routes()`** rather than
  `AddPrefix("/pensions")` with relative paths. `SOP.md` shows the latter; the
  shipped handler uses the former, and it is the one that is demonstrably in
  service.
- **No `.Suffix("RETURNING …")` on delete.** The reference's `RowsAffected() == 0
  → pgx.ErrNoRows` is correct as written and needs no change.

---

## The properties that hold regardless

Whatever is decided about the three divergences above, these are asserted in CI
and are not matters of taste:

- The scaffolded resource **passes every `rules_lint` rule** — the same
  assertion the reference template is held to
  (`TestScaffoldedResourceIsLintClean`).
- It **compiles and passes `go vet`** against the real private modules
  (`TestScaffoldedResourceCompiles`).
- A greenfield service **resolves, compiles and lints clean**
  (`TestProjectScaffoldCompilesAndLints`).
- Output is **byte-identical across runs**, so the golden snapshots mean
  something (`TestScaffoldIsDeterministic`).
- Scaffolded configs **carry no credentials** — asserted twice over, by
  `TestGoldenConfigsCarryNoCredentials` and by `secrets-in-config` running over
  the scaffolded project in `TestProjectScaffoldCompilesAndLints`.
- **`repo_map` stays inside its token budget** on both corpora, and every
  reduction it makes is counted and reported (`TestBudgetIsHonouredAndBreadthSurvives`,
  `TestImpossibleBudgetDropsAndSaysSo`).
- **The tree walk prunes rather than filters, and reads each file once**
  (`TestPrunedTreeIsFastAndReadsEachFileOnce`) — findings S2 and S3 as CI gates.

---

## A defect found in the reference configuration

Not a divergence — the scaffolder does the right thing here and the reference
does not — but it is the second thing this work turned up that belongs with the
template owner rather than in code.

`swagger.generation.mode` is declared in `configs/config.yaml` and in **none** of
the six environment files. The environment configs are full replacements rather
than overlays (`config.dev.yaml` is 121 lines and repeats the whole document), so
a service started with `APP_ENV=dev` — or sit, staging, test, training or prod —
generates no OpenAPI document at all. Every route still serves; they are simply
absent from `/docs/v3Doc.json`, with no error anywhere.

`swagger-visible` reports it as a warning rather than an error precisely because
it may be deliberate. Scaffolded services declare the key in every environment.
