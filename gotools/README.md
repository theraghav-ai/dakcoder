# gotools

Go-native analysis and scaffolding for the **dakcoder-go** backend coding agent.

One static binary. It **checks** template compliance and it **writes**
template-compliant code, and it does both from a CLI and over MCP:

```bash
gotools lint              --root .   # check compliance
gotools legacy-audit      --root .   # find pre-template patterns
gotools resource-scaffold --root . --spec resource.json
gotools project-scaffold  --root . --spec service.json
gotools fx-wire           --root . --kind handler --ctor NewPensionHandler
gotools repo-map          --root .   # orient: packages, symbols, FX graph
gotools mcp               --root .   # all of the above, over stdio, for the agent

gotools doc-check                    # rule citations still resolve into skill.md / SOP.md
gotools tool-catalog                 # publish contract C1
gotools knowledge                    # build the agent's knowledge base
```

Both surfaces call the same entry points, so the agent, your terminal and the
pipeline can never disagree — not about what a violation is, and not about what
a resource looks like.

---

## What it checks

The `n-api-template` contract — the layered `domain` / `port` / `repo` /
`handler` split, the Uber-FX composition root, the `govalid` request-DTO
pipeline, the `dblib.Psql` repository idiom, and the `port.StatusCodeAndMessage`
response envelope.

`gotools rules` lists the full set with citations. Every rule names the section
of `skill.md` or `SOP.md` it enforces, and every violation carries a one-line
remedy:

```
[fx-registration] handler/pension.go:18 — handler constructor NewPensionHandler
    is not registered in bootstrap/; Uber-FX will fail at startup
      fix: add fx.Annotate(handler.NewPensionHandler,
           fx.As(new(serverHandler.Handler)),
           fx.ResultTags(serverHandler.ServerControllersGroupTag)) to FxHandler
      see: SOP.md §bootstrap/bootstrapper.go; skill.md §Bootstrap Configuration
```

Two separate rule sets:

| Command | Set | Use |
|---|---|---|
| `gotools lint` | template compliance | after every edit; in CI |
| `gotools legacy-audit` | pre-template (`api-*` generation) patterns | when planning a migration |

They are kept apart deliberately. A `pao`-era service trips ~1,700 compliance
findings; surfacing those during an ordinary edit would bury the two the
developer actually introduced.

Three rules read `configs/*.yaml` rather than Go, because that is where the
failure lives:

| Rule | Catches |
|---|---|
| `config-key-exists` | `cfg.GetDuration("db.QueryTimeoutLow")` where the key is absent. Viper returns the zero value, so the query gets a 0s deadline and fails with `context deadline exceeded` — an error that names the context, not the config. A key present in the base config and missing from `config.prod.yaml` is reported separately: that is how a service works in dev and dies in production. |
| `swagger-visible` | `swagger.generation.mode` unset, so the framework generates nothing and every route is missing from `/docs/v3Doc.json` while serving perfectly. |
| `secrets-in-config` | Literal credentials. Severity depends on who wrote them — see below. |

**`secrets-in-config` reports what it finds without ever quoting it.** A
violation message goes into a prompt, a log, a trace and a diff at once, so the
value is unreachable from the rule: `ConfigKey` keeps it unexported and exposes
only `HasRealValue()`. Severity follows authorship, which the rule can tell
because the caller says which files it just changed:

- the agent edited this config in this run → **error**, and the write is refused
- the value was already committed → **warning**, once

That split is what lets the rule be strict without being unusable. The reference
template ships a MinIO access/secret pair, an Aadhaar client secret, a database
password and two Redis passwords; blocking on those would fail every lint of the
very template the rules enforce, and rotating them is not the agent's call
(plan.md §9 Q7). Running `gotools lint --root new-template` surfaces all twelve
as advisories, in one place, without printing a single value.

### Scoping

Pass the files you changed. Findings elsewhere are reported but never block:

```bash
gotools lint --paths handler/pension.go,repo/postgres/pension.go
```

This is what stops an agent from wandering off to "fix" unrelated legacy code
that happened to be in the same repository.

---

## What it writes

Adding a resource to `n-api-template` is a fixed seven-file recipe. The
scaffolder does it from a field spec:

```json
{
  "name": "Pension",
  "fields": [
    {"go": "PpoNumber", "type": "string",  "validate": "required", "sql": "varchar(20) NOT NULL"},
    {"go": "Amount",    "type": "float64", "validate": "required"},
    {"go": "Status",    "type": "string",  "validate": "oneof=active suspended closed"}
  ],
  "list_filters": [{"go": "Status"}]
}
```

```
$ gotools resource-scaffold --root . --spec pension.json
wrote 7 file(s):

  create core/domain/pension.go                    626 bytes
  create db/pensions.sql                           784 bytes
  create repo/postgres/pension.go                 4885 bytes
  create handler/response/pension.go              2152 bytes
  create handler/pension.go                       4268 bytes
  modify handler/request.go                       1910 bytes
  modify bootstrap/bootstrapper.go                 678 bytes

next:
  · run `govalid ./request.go` from handler/ — the framework returns 422 for any
    non-GET route whose request DTO has no generated Validate()
  · apply db/pensions.sql to your database; the agent never runs DDL, because it
    is the one action git cannot undo
  · verify with: go build ./... && gotools lint
```

`--dry-run` prints the plan without writing anything, which is what the agent's
approval gate uses to show a diff before touching the working tree.

### The model chooses the spec; `text/template` writes the code

That division is the whole design. It is what makes the output deterministic
enough to pin against a byte-exact golden snapshot, and it is what stops the
model inventing a dependency — the pre-implementation spike asked for a
`Pension` resource and got back `"type": "decimal.Decimal"`, which is not in the
template's dependency set and would not have compiled.

The spec is validated against a closed type set before a single file is
rendered, and the rejection names the substitute:

```
$ gotools resource-scaffold --spec bad.json
gotools resource-scaffold: the spec has 1 problem(s):

  fields[0].type
      type "decimal.Decimal" is not available in this template
      fix: use float64 (or string when exact decimal arithmetic is required)
```

The same pass corrects what it can rather than bouncing it back: `PpoNumber`
becomes `PPONumber` — as do `ppo_number` and `ppoNumber` — and the `json`, `db`,
table and route names all follow from it.

Every string in a spec is interpolated into generated Go or SQL, and a spec is
model output, so it is treated as untrusted: field names must be plain
identifiers, `validate` tags cannot contain the characters that would close a
struct-tag literal, and a column type cannot contain a statement separator or a
comment marker.

### `fx-wire`

Registration is separated out because it is the single most common way a correct
resource fails to work. A handler missing from `FxHandler` compiles perfectly
and then fails at start-up with an Uber-FX error naming a *type*, not a file.

Worse is the near miss: a handler registered with a plain `fx.Provide` instead
of the annotated form compiles, starts, and silently serves nothing — the server
collects handlers by group tag, so an untagged provider is simply never
collected. There is no error at all.

```
repositories:  fx.Provide(repo.NewXRepository)
handlers:      fx.Provide(fx.Annotate(handler.NewXHandler,
                   fx.As(new(serverHandler.Handler)),
                   fx.ResultTags(serverHandler.ServerControllersGroupTag)))
```

`fx-wire` edits `bootstrap/bootstrapper.go` at AST-located byte offsets rather
than re-printing it, so comments survive and the diff shows three added lines
instead of a rewritten file. Running it twice is a reported no-op, not a
duplicate provider — which matters, because agents retry tool calls.

### `repo-map`

The orientation call, and the one the Planner makes first:

```
$ gotools repo-map --root .
module pisapi  (go 1.25.0)  [n-api generation]

packages:
  core/domain                  domain      1 file(s)
      types: User
  handler                      handler     2 file(s)
      types: CreateUserRequest, UpdateUserRequest, UserHandler, UserIDUri
      funcs: (*UserHandler).CreateUser, (*UserHandler).Routes, NewUserHandler
  repo/postgres                repo        1 file(s)
      types: UserRepository
      funcs: (*UserRepository).CreateUser, NewUserRepository

fx:
  repos:    NewUserRepository
  handlers: NewUserHandler

9 file(s), ~573 tokens, 0ms
```

**573 tokens.** The frontend agent's equivalent emitted indented JSON with a
preview of every file, up to 200 files: 20–30k tokens, injected at turn one and
then re-sent on every subsequent turn for the rest of the task (§5, finding S4).
On a 25-turn task that one tool result cost more than everything else combined.

So the output has a hard budget and degrades within it, in this order:

1. every package's symbol list shortens uniformly, each keeping its `(+N more)`
   count;
2. then whole symbol lists drop, least-important layer first;
3. then — only if the bare package list still does not fit — packages are
   omitted.

Breadth is given up last, because the agent needs to know a package *exists* far
more than it needs the ninetieth method of one it is not editing. Every stage is
counted and reported: a cap the output does not mention reads as a complete
answer, and the agent plans against a repository that has more in it than it was
shown. `--package repo/postgres` returns any one package in full, which is what
the elision hint tells the agent to call.

The FX section comes from the same scan `fx-registration` reports from, so the
map and the linter cannot disagree — and if they could, the failure would be a
loop rather than a wrong answer: the Planner reads the map, believes a handler
is wired, plans no wiring step, and the Verifier blocks on it for the rest of the
run. It separates two failures the agent must not confuse:

- **`unwired`** — absent from `bootstrap/`. Uber-FX fails at startup.
- **`misregistered`** — registered without the group tag. Starts cleanly, serves
  nothing, reports nothing. On the legacy corpus all eight handlers land here.

It is built from the parsed workspace rather than `go list` or `packages.Load`,
for the same reason the rules engine is syntax-only: repo_map is needed *most*
when the build is broken, and a loader that needs the module graph returns
nothing useful there.

### `project-scaffold`

Lays down a complete service — `go.mod`, `main.go` with the swagger annotation
block, the FX composition root, `core/port`, all seven config files, a
`.gitignore`, a README — seeded with one working resource, so it builds and
serves on first run.

Configs ship with **empty** credential fields. The reference template has a
MinIO access/secret pair, an Aadhaar client secret and a database password
committed to git; a tool that writes twenty files at a time must not turn one
incident into a pattern. `TestGoldenConfigsCarryNoCredentials` asserts it.

Three divergences from the reference template are deliberate. Each is recorded
with its reasoning and its cost to reverse in
[docs/DIVERGENCES.md](docs/DIVERGENCES.md).

---

## What it publishes

Three artefacts other components bind against. All three are **generated and
freshness-checked in CI**, because each of them is a place where a fourth copy
of the truth would otherwise accumulate and go quietly stale.

### `docs/TOOL-CATALOG.md` + `tool-catalog.json` — contract C1

The published tool schemas. The gateway routes against them, the extension
renders approvals from them, and the model is sent them verbatim on every turn —
which is why C1's limits (at most six parameters, at most a 200-character
description) are limits rather than preferences. Generated from the running MCP
server, so the document cannot describe a tool that does not exist, omit one
that does, or disagree with what the model actually receives.

`gotools tool-catalog --check` fails a build on a stale catalogue.

### `packages/knowledge/` — the agent's knowledge base

`skill.md` is 2,339 lines, roughly 30k tokens. It cannot live in a system prompt
and 95% of it is irrelevant to any given turn, so it is chunked the way plan.md
§14.2 asks: a small always-loaded `SKILL.md` — currently **944 tokens**, inside
§6.1's 1,200-token budget, with a test that says so — plus twelve reference
files fetched on demand under handles like `@skill:repository-pattern`.

The references are *assembled*, not copied. Each declares the document sections
it draws on; the generator extracts them verbatim and attributes them by line
number. Three of them have no document source at all and are generated from the
workspace instead: `config-keys` from the real `configs/*.yaml`, `legacy-patterns`
and `go-idiom` from the rule set. So the knowledge base cannot describe a config
key that is not there, or a rule the linter does not enforce.

**And they carry corrections.** Three parts of skill.md's worked example do not
compile against the current libraries, and the shipped `user` resource
contradicts the document in two more places (plan.md §6). Handing the agent the
document verbatim would hand it those defects — the pre-implementation spike
showed the model follows its context closely, which is exactly why the context
has to be right. Each affected reference states what is wrong *before* the text
that is wrong, because a reader who meets the correction afterwards has already
absorbed the wrong version.

That makes the knowledge base strictly better than the document it is built
from, which is the only good reason to build one rather than pointing
`search_docs` at `skill.md`.

### `docs/doc-manifest.json` — the rules-to-doc pin

Every rule cites the section of `skill.md` or `SOP.md` it enforces, and that
citation is rendered with every violation. It is the difference between a tool
that looks opinionated and one a developer can check — but a citation is just a
string, and nothing stops it naming a section that was renamed.

`gotools doc-check` resolves all 34 of them and pins the documents' hashes.
When a document changes, the check names the cited headings that vanished and
the rules that depended on them; re-pinning with `make doc-pin` is the moment
someone reviews them. It earned its keep immediately: five rules were citing
SOP.md *intro bullets* as if they were sections, so five violation messages
pointed at text a reader could not navigate to.

---

## Design notes

**Syntax-only, not `go/packages`.** Type information would make a handful of
rules more precise. It is not worth it here, for one decisive reason: the agent
runs this linter *after every edit*, and in the Debugger loop it runs
specifically because the build is broken. A type-aware loader returns nothing
useful in exactly the situation where an answer matters most. Parsing degrades
gracefully — a file with a type error still yields its signatures, struct tags
and imports.

It is also two orders of magnitude faster. A cold `go build` of the reference
template takes 2m30s; parsing the same tree takes single-digit milliseconds.

**Directories are pruned during the walk, never filtered after.** A post-hoc
filter still stats every entry in `vendor/`; measured on a comparable tree that
cost 1.6s to keep 200 files out of 16,680. On Windows each stat goes through the
antivirus filter driver.

**Not `go/analysis`.** Several rules are inherently cross-package
(`fx-registration` must see a handler and its bootstrap registration together)
and several are about *directories* rather than packages ("SQL must not appear
outside `repo/`"). We keep what `go/analysis` gets right — one parse,
declarative rule metadata, positional reporting — and drop the per-package unit
of work.

**Byte-level patching, not AST re-printing.** `handler/request.go` and
`bootstrap/bootstrapper.go` are edited in place at offsets the AST located.
Re-printing would lose comments — `go/printer` places them by token offsets that
AST mutation invalidates — and would rewrite every line of the file, because the
reference template is CRLF throughout and `go/printer` emits LF. A diff a human
is asked to approve has to show only what changed.

**Every generated file goes through gofmt before it is returned**, as a syntax
check rather than a formatting nicety: a template with a missing brace fails at
scaffold time with a line number, instead of arriving at a developer as an error
in a file they did not write.

**One naming implementation.** `internal/naming` is shared by the linter and the
scaffolder, because they have to agree on what `PPONumber` should be tagged as.
Two implementations of `snake_case` is how a scaffolder ends up emitting code
its own linter rejects.

---

## Measured performance

| Corpus | Files | Time |
|---|---|---|
| `new-template` (reference) | 12 | **2 ms** |
| model-generated resource | 16 | **~5 ms** |
| `pao-back-end-development` (legacy) | 49 | **64 ms** |

`repo_map` on the same corpora: 0 ms for the reference, **39 ms** for the legacy
service against a 1.5 s target. Over a synthetic tree with 20,000 vendored files
alongside 20 real ones: **249 ms**, with exactly one read per real file. Both are
CI gates (§20.5), not dashboard numbers — findings S2 and S3 are that the
frontend agent's walk stat'd every vendored path before excluding it, and then
read every file it kept twice.

---

## Configuration

Optional, at `.dakcoder/gotools.yaml` in the workspace root:

```yaml
allowed_deps:
  - github.com/Masterminds/squirrel
  - github.com/jackc/pgx/v5
  - gitlab.cept.gov.in/it-2.0-common/
  - go.uber.org/fx
allowed_test_deps:
  - github.com/stretchr/testify
  - github.com/testcontainers/testcontainers-go
max_file_lines: 600
disable:
  - file-size
severity:
  response-status: error
  go-idiom: warning        # pins every finding, including the package-mismatch one
```

A `severity:` entry is absolute: two rules escalate individual findings above
their own default — `go-idiom` for a mismatched package declaration, which does
not compile, and `secrets-in-config` for a credential the agent just wrote — and
a pin here overrides both. An operator setting that has exceptions is a setting
nobody trusts.

The allow-list is data rather than a Go constant on purpose: it changes when the
IT 2.0 common libraries release, and a rule set that needs a binary rebuild to
accept an approved dependency gets worked around instead of updated.

---

## Exit codes

| Code | Meaning |
|---|---|
| 0 | clean |
| 1 | violations found |
| 2 | `gotools` itself failed |

CI can therefore distinguish "your code is wrong" from "the linter is broken",
which a single non-zero code cannot.

---

## Development

```bash
make ci             # fmt-check, vet, tidy-check, test-race, lint — the pipeline
make test           # tests only
make test-short     # only what needs no Go toolchain or private modules
make cover          # cross-package coverage
make golden         # rewrite the scaffold snapshots, then review the diff
make baseline       # run the corpus assertions by hand
make scaffold-demo  # scaffold into a throwaway copy of the template and lint it
make dist           # cross-compile every shipped platform + SHA256SUMS

make doc-check      # rule citations still resolve; documents have not drifted
make doc-pin        # re-pin the documents, after reviewing the rules they affect
make tool-catalog   # regenerate contract C1
make knowledge      # regenerate the knowledge base
```

`make test` needs the sibling corpora (`../new-template`,
`../pao-back-end-development`); tests that use them skip cleanly when they are
absent. Two of them additionally build real Go against the private
`gitlab.cept.gov.in` modules, and skip rather than fail when those cannot be
resolved — a test that goes red when you are offline is a test people learn to
ignore. `make test-short` runs everything that does neither.

### The load-bearing assertions

These are the first thing to look at when anything misbehaves:

1. **The reference `user` resource passes every rule.** If it does not, *the
   rule is wrong, not the template.* This has already earned its keep three
   times — an unqualified selector chain, an unhandled `new(T)` inside `fx.As`,
   and a `legacy-handmade-health` warning that fired on the test exercising the
   very handler it was meant to find.
2. **A real legacy service trips the expected legacy rules**, and only those.
3. **The scaffolder's own output passes every rule.** The two halves of this
   binary have to agree: a scaffolder that emits code its own linter rejects
   opens the agent's inner loop with a violation the agent did not cause and
   cannot fix.
4. **The scaffolder's output compiles and passes `go vet`** against the real
   private modules. `rules_lint` is syntax-only by design, so only the compiler
   knows that `dblib.InsertReturning` takes four arguments or that a `time.Time`
   field needs the `time` import — both of which the templates got wrong on
   their first draft.
5. **A greenfield service resolves, compiles and lints clean.**
6. **Output is byte-identical across runs**, which is what makes the golden
   snapshots mean anything.
7. **Every violation carries a fix and a citation**, and every spec rejection
   names its substitute. Enforced mechanically, so a new rule or a new type
   cannot regress it.
8. **Every citation resolves to a real heading**, and the documents behind them
   have not changed since anyone last looked at the rules that cite them.
9. **The published contracts are current** — the tool catalogue and the
   knowledge base are regenerated and diffed, so a tool added without
   regenerating fails the build rather than shipping a stale schema.

### Adding a rule

1. Write it in the matching `internal/rules/*.go` file as a `Rule` value with an
   `ID`, `Severity`, `Summary`, `Citation` and `Check`.
2. Register it in `internal/rules/registry.go`.
3. Add a `good` and a `bad` case to the table in `internal/rules/rules_test.go`.
4. Run `make test`. If `TestReferenceTemplateIsClean` fails, your rule is wrong.

Rule IDs are API — they appear in agent prompts, playbook filenames and stored
violations — so they must not change once published. So are the MCP tool names
and the JSON field names of `gotools rules --format json`; both have a test
asserting they have not moved.

### Changing a template

1. Edit the file under `internal/scaffold/templates/`.
2. `make test` — the golden tests will fail, which is the point.
3. `make golden`, then **read the diff**. A snapshot refreshed without being read
   is a test that asserts nothing.
4. Check `TestScaffoldedResourceCompiles` and `TestScaffoldedResourceIsLintClean`
   still pass. If the linter now rejects the scaffolder, decide which of the two
   is wrong before touching either.

The snapshots for `handler/request.go` and `bootstrap/bootstrapper.go` carry the
reference template's own content, so they also fail when `new-template` changes
underneath us. That coupling is deliberate — it is the local half of the
rules-to-doc drift check the plan asks for.

Snapshots are stored with a `.golden` extension. Two of them are deliberately
CRLF, because the reference template is, and a stray `gofmt -w ./...` rewrites
them to LF — which broke the golden tests twice during development before the
rename. `.gitattributes` and the `fmt` target guard the same thing, but the
extension is the defence that does not depend on anyone reading them.
