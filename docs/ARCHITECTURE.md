# dakcoder — architecture and decision log

> **Maintained, not generated.** Every decision that a future engineer might
> otherwise re-litigate belongs here, with its reasoning and what it would cost
> to reverse. Add to the decision log in the same commit as the change; add to
> the changelog when a phase closes.

**Project**: `dakcoder` — the IT 2.0 backend coding agent for Go services on
`n-api-template`.
**Companions**: [plan.md](../plan.md) (shared context, decisions D1–D7,
contracts C1–C5), [plan-backend-agent.md](../plan-backend-agent.md) (Part A),
[plan-vscode-extension.md](../plan-vscode-extension.md) (Part B).

This file records what has actually been *built* and why it was built that way.
Where an implementation decision differs from the plan, that difference is a
numbered entry below with its justification — not a silent deviation.

---

## 1. Where things stand

| Component | State |
|---|---|
| `gotools` — the Go analysis and scaffolding sidecar | **built**, 30 rules, 7 MCP tools, 84.5% coverage |
| `packages/knowledge` — the agent's knowledge base | **built**, generated and freshness-checked |
| `docs/TOOL-CATALOG.md` — contract C1 | **built**, generated from the live MCP schemas |
| `apps/shared` — token estimation and calibration | **built** |
| `apps/agent` — context manager and mode config | **built**, budget gate green |
| `apps/shared` — LLM client (transport, retry, streaming, usage) | **built** |
| `apps/gateway` — capability probe | **built** |
| `apps/agent` — tool registry and router (29 tools, C1-enforced) | **built** |
| `apps/agent` — filesystem, command and knowledge tools | **built** |
| `apps/agent` — the `gotools` sidecar bridge (MCP over stdio) | **built** |
| `apps/agent` — the verification gate | **built** |
| `apps/agent` — the loop | **built**, five modes, C2 event stream |
| `docs/TOOL-CATALOG.md` — contract C1, model-facing half | **built**, generated |
| `apps/agent/prompts` — the shared system prompt | not started |
| `apps/gateway` — auth, quota, ledger, model proxy | not started |
| VS Code extension (Part B) | not started |

**Phase 0 is complete, and Phase 1's happy path runs end to end.** The section
11.1 recipe — plan, scaffold a resource, wire it, verify — completes against a
real copy of the reference service with a real `gotools` sidecar, a real
`go build` against the real private modules, and a clean gate. Only the model is
scripted. `apps/agent/tests/test_happy_path.py` is that run, pinned.

What remains before Phase 1 ships is the system prompt, the loopback HTTP
surface, and the gateway.

---

## 2. System shape

```
┌── LOCAL MODE (default, D2) ─────────────────────────────────────────────────┐
│  DEVELOPER'S MACHINE                                                         │
│  ┌──────────────────┐        ┌──────────────────────────────────────────┐   │
│  │ VS Code ext      │◀─SSE──▶│ dakcoderd  (loopback)                    │   │
│  │  (Part B)        │  HTTP  │  ┌────────────────────────────────────┐  │   │
│  └──────────────────┘        │  │ apps/agent                         │  │   │
│                              │  │   context.py  ← the message list   │  │   │
│                              │  │   modes.py    ← budgets, reasoning │  │   │
│                              │  └────────────────────────────────────┘  │   │
│                              │        ├─▶ gotools    (MCP over stdio)   │   │
│                              │        ├─▶ gopls mcp  (MCP over stdio)   │   │
│                              │        └─▶ local FS + go toolchain + git │   │
│                              └───────────────────┬──────────────────────┘   │
└──────────────────────────────────────────────────┼──────────────────────────┘
     only model traffic leaves the machine ────────┤
                                                    ▼
┌── CENTRAL SERVICES (aiops.cept.gov.in/coder/backend) ───────────────────────┐
│  /v1/auth/*  ·  /v1/quota  ·  usage ledger  ·  OTel  ·  KB index            │
│  /v1/llm/*   ← model proxy: holds the LiteLLM key, meters, forwards         │
│  Postgres (ledger) · Redis (quota counters)  ← provisioned on this host     │
└────────────────────────────────┬────────────────────────────────────────────┘
                                 ▼
        LiteLLM proxy  https://ai.cept.gov.in/v1  →  vLLM · Qwen3.8-27B
```

The split that matters: **file access and command execution are local; auth,
quota, the ledger and model access are always central.** That is the honest
version of "local-first", and it is why the model proxy is not ceremony — the
LiteLLM key is a single shared secret, so a laptop holding it could spend the
shared GPU budget with no ceiling and no attribution.

---

## 3. Decision log

Each entry: what was decided, why, what was rejected, and what reversing it
would cost. Entries are append-only; a superseded decision is marked, not
deleted.

### 3.1 Programme

---
**D-01 · The name is `dakcoder`** · *settled 25 Aug 2026*

Confirmed by the programme owner, closing plan.md §9 Q5. The Go module path
moved from `…/dakcoder-go/gotools` to `…/dakcoder/gotools` in the same change.

**Why now**: the module path *is* the GitLab project path, and the plan flags
both it and the nginx location as painful to rename once they exist. Doing it
before either is provisioned cost one `sed` across 37 files; doing it afterwards
costs a repository move and a redirect.

**Cost to reverse**: low today, high once the GitLab project exists.

---
**D-02 · Postgres and Redis live on the Python spine's host** · *settled 25 Aug 2026*

Confirmed by the programme owner. The gateway's ledger (Postgres) and quota
counters (Redis) are provisioned alongside the spine rather than as separate
infrastructure.

**Consequence for the design**: none structural — §16.3 already assumes Redis is
the hot path and Postgres the durable ledger, rebuildable from each other. It
does remove the "provision `/coder/backend/` + DB + Redis index" item from
Phase 0's blocking list.

---

### 3.2 The Go sidecar

---
**D-10 · Syntax-only parsing, not `go/packages`** · *superseding Part A §8.1*

`gotools` parses with `go/parser` and never loads type information.

**Why**: `rules_lint` has to work on code that does not compile. The agent runs
it after every edit batch, and in the Debugger loop it runs *specifically because
the build is broken*. A type-aware loader returns nothing useful in exactly the
situation where an answer matters most. Syntax-only degrades gracefully — a file
with a type error still yields its signatures, struct tags and imports.

It is also two orders of magnitude faster: a cold `go build` of the reference
template takes 2m30s; parsing the same tree takes single-digit milliseconds.

**Rejected**: `packages.Load` with `NeedTypes`, as Part A §8.1 sketches. It would
sharpen perhaps three rules and break all thirty on broken input.

**Cost to reverse**: moderate. Rules needing types belong in a separate opt-in
tier that runs at the verification gate, which is where
`request-domain-type-match` (§4.6 decision 2) still waits.

---
**D-11 · Not `go/analysis`**

**Why**: two properties of this rule set fight the harness. `fx-registration`
must see a handler constructor in package `handler` and its registration in
package `bootstrap` *at the same time*, and `go/analysis` runs per-package —
faking cross-package state through Facts is more machinery than the rules need.
And several rules are about *directories*, not packages: "SQL must not appear
outside `repo/`" is a question `go/analysis` has no vocabulary for.

What was kept: one parse, declarative rule metadata, positional reporting. What
was dropped: the per-package unit of work.

---
**D-12 · One naming implementation, shared by the linter and the scaffolder**

`internal/naming` is used by both.

**Why**: they have to agree on what `PPONumber` is tagged as. Two
implementations of `snake_case` is precisely how a scaffolder ends up emitting
code its own linter rejects — and that is the least forgivable bug a tool like
this can have, because it opens the agent's inner loop with a violation the
agent did not cause and cannot fix.

The same reasoning put `TimestampLayout` in `internal/spec` and had
`rules.DefaultConfig()` read it from there, and later unified the FX scan
(D-19).

---
**D-13 · The model chooses the spec; `text/template` writes the code**

`resource_scaffold` takes a validated field spec and renders files
deterministically.

**Why**: it makes the output byte-comparable against a golden snapshot, and it
stops the model inventing a dependency. The pre-implementation spike asked for a
`Pension` resource and got back `"type": "decimal.Decimal"` — not in the
template's dependency set, would not have compiled.

**Consequence**: the spec is the risk surface, so it is validated against a
**closed** type set before a single file is rendered, and the rejection names
the substitute. An open set ("any type that parses") is exactly what lets
`decimal.Decimal` through.

---
**D-14 · Spec strings are treated as untrusted input**

Field names must match `^[A-Za-z][A-Za-z0-9]*$`; `validate` tags cannot contain
the characters that close a struct-tag literal; SQL column types cannot contain
a statement separator or a comment marker.

**Why**: every string in a spec is interpolated into generated Go or SQL, and
the spec is model output — which plan.md §17 treats as untrusted, because a
prompt injection in a source comment can reach it. A field type of
``string `json:"x"` //`` would close the tag and inject arbitrary source.

---
**D-15 · Byte-level patching, not AST re-printing**

`handler/request.go` and `bootstrap/bootstrapper.go` are edited in place at
offsets the AST located.

**Why**: comment attachment does not survive AST mutation reliably —
`go/printer` positions comments by token offsets that mutation invalidates, and
these are exactly the files developers annotate. And re-printing rewrites the
whole file: the reference template is CRLF throughout and `go/printer` emits LF,
so a three-line insertion would arrive as a diff touching every line. A diff a
human is asked to approve has to show only what changed.

---
**D-16 · `gofmt` is a syntax gate on generated files, not a formatting step**

Every generated Go file goes through `format.Source` before it is returned.

**Why**: it fails on unparseable Go. A template with a missing brace therefore
fails at scaffold time with a line number, rather than reaching a developer as a
mysterious error in a file they did not write.

---
**D-17 · Create and update return the stored row** · *diverges from the reference*

`resource_scaffold` emits `dblib.InsertReturning` / `UpdateReturning` with a
`RETURNING` clause, where the shipped `user` resource builds its result from the
arguments it was handed.

**Why**: the reference's version has two defects. `CreateUser` returns `ID: 0`,
so `POST /v1/users` answers with `"id": 0` for every record it creates.
`UpdateUserByID` dereferences every optional pointer — and the handler only sets
pointers for non-empty fields, so `PUT /v1/users/1` with `{"city": "Nashik"}`
is a nil dereference that takes the process down. Partial update is the only
kind that endpoint is designed for.

Reproducing a panic in every generated resource is not defensible on consistency
grounds. The returning variants are already in `n-api-db`, stay on `dblib.Psql`,
are rules-clean, and make `pgx.ErrNoRows` on a zero-row update fall out for free.

**Cost to reverse**: two template blocks and a golden refresh.
**Escalation**: the reference's update panic should be raised with the template
owner; it is a live defect independent of this programme.

Full detail: [gotools/docs/DIVERGENCES.md](../gotools/docs/DIVERGENCES.md).

---
**D-18 · `repo_map` is built from the parsed workspace, not `go list`** ·
*superseding Part A §8.1*

**Why**: same reason as D-10, more sharply. The Planner runs `repo_map` before
anything else, often on a service the developer has just described as not
working. A loader that needs the module graph to resolve returns nothing useful
there — or spends two and a half minutes discovering it cannot.

Result: 573 tokens and 0 ms on the reference template; 39 ms on the legacy
corpus against a 1.5 s target.

---
**D-19 · The FX graph is scanned once and shared**

`rules.ScanFX` is used by both `fx-registration` and `repo_map`.

**Why**: if the map and the linter derived it separately they would eventually
disagree, and the failure would be a *loop* rather than a wrong answer — the
Planner reads the map, believes a handler is wired, plans no wiring step, and the
Verifier blocks on `fx-registration` for the rest of the run.

---
**D-20 · `repo_map` gives up breadth last**

Under budget pressure it (1) shortens every package's symbol list uniformly,
(2) drops whole symbol lists least-important-layer-first, (3) omits packages
only as a last resort. Every stage is counted and reported.

**Why**: the agent needs to know a package *exists* far more than it needs the
ninetieth method of one it is not editing. The first implementation had this
backwards — the highest-priority package kept all 90 of its symbols while nine
others lost everything, producing a 2,400-token dump of one handler package and
nothing about the rest.

A cap the output does not mention reads as a complete answer, so the elision is
always stated and always says how to get the detail.

---
**D-21 · `secrets-in-config` severity follows authorship**

A credential the agent wrote in this run is an **error**; one already committed
is a **warning**, reported once.

**Why**: both halves are needed and they conflict. Blocking on pre-existing
credentials would fail every lint of the very template the rules enforce — the
reference ships a MinIO pair, an Aadhaar client secret, a DB password and two
Redis passwords — and rotating them is not the agent's call (plan.md §9 Q7).
Not blocking at all would let the agent commit a new one.

The caller already tells the linter which files it touched, so the distinction
is available. `Pass.Touched()` exists for this.

**The value is never quoted.** `ConfigKey` keeps it unexported and exposes only
`HasRealValue()`, because a violation message reaches a prompt, a log, a trace
and a diff at once, and §17 requires committed credentials are never echoed into
any of them.

---
**D-22 · A finding may override its rule's severity; an operator's pin outranks both**

**Why**: two rules cover findings of materially different consequence under one
id. `go-idiom` is advisory throughout *except* a mismatched package
declaration, which does not compile. `secrets-in-config` is D-21. Splitting them
into separate ids purely to carry a severity would make the rule table lie about
how many distinct things it checks.

A `severity:` entry in `.dakcoder/gotools.yaml` wins over both: an operator
setting that has exceptions is a setting nobody trusts.

---
**D-23 · Published contracts are generated and freshness-checked**

`docs/TOOL-CATALOG.md`, `docs/tool-catalog.json`, `packages/knowledge/` and
`docs/doc-manifest.json` are all produced by `gotools` and diffed in CI.

**Why**: each is otherwise a fourth copy of the truth, and the first to go
stale. A hand-written tool catalogue describes a tool set that drifts from the
schemas the model is actually sent; a hand-copied knowledge base drifts from
`skill.md` the moment it changes.

**Consequence**: adding a tool without regenerating fails the build rather than
shipping a stale schema.

---
**D-24 · The knowledge base is assembled, and it carries corrections**

Each reference declares the document sections it draws on; the generator
extracts them verbatim and attributes them by line number. Three references have
no document source and are generated from the workspace instead — `config-keys`
from the real `configs/*.yaml`, `legacy-patterns` and `go-idiom` from the rule
set.

**Why corrections**: three parts of `skill.md`'s worked example do not compile
against the current libraries, and the shipped `user` resource contradicts the
document in two more places. Handing the agent the document verbatim would hand
it those defects — the spike showed the model follows its context closely, which
is exactly why the context has to be right. Corrections render *before* the text
they correct, because a reader who meets them afterwards has already absorbed
the wrong version.

That makes the knowledge base strictly better than the document it is built
from, which is the only good reason to build one rather than pointing
`search_docs` at `skill.md`.

**`SKILL.md` is 944 tokens**, inside §6.1's 1,200-token system-prompt budget,
with a test asserting it. The first cut was 1,424 because it carried the full
rule table — 475 tokens telling the agent things `rules_lint` tells it again,
with a fix attached, the moment a rule fires.

---
**D-25 · Golden snapshots are stored as `*.golden`**

**Why**: two of them are deliberately CRLF, because the reference template is,
and a stray `gofmt -w ./...` rewrites them to LF. That broke the golden tests
twice during development. `.gitattributes` and the Makefile's `fmt` target guard
the same thing, but the extension is the defence that does not depend on anyone
reading them.

---
**D-26 · Coverage is measured per-package, without `-coverpkg`**

**Why**: `-coverpkg` looks correct here — several packages are exercised mostly
from another package's tests. But the merged profile it produces is wrong: it
reported one-line accessors that every test calls at 40%, and a total eight
points below the honest per-package figure. A coverage number that is quietly
wrong is worse than one that is quietly incomplete. Where a package reads low,
the fix is a direct test, not a flag.

---

### 3.3 The Python spine

---
**D-30 · The context manager is a component, and it is first**

`apps/agent/context.py` is the only thing allowed to build a message list.

**Why**: the frontend agent has no context management inside a run, and §5.2's
worked estimate puts a 25-turn brownfield task at ~1.25M tokens of prefill, of
which roughly 95% is recomputation of a prefix that never changed. That is not a
criticism of a system that shipped — it is what happens when context is nobody's
component.

§21's first risk is that this work gets deferred as "optimisation" and the agent
ships slow. Its mitigation is that the targets are CI-asserted, so a regression
is a red build rather than a slow agent nobody can quite explain.

**Measured**, on a 25-turn simulation built from §5.2's own figures:

| | peak | raw prefill | novel prefill |
|---|---|---|---|
| managed | 21,624 | 347,601 | **142,258** |
| unmanaged | 305,399 | 4,251,429 | — |

12.2× on raw tokens, 29.9× once prefix reuse is counted. Targets: P95 ≤ 24k ✓,
effective prefill ≤ 180k ✓.

---
**D-31 · Messages are immutable**

`Message` is a frozen dataclass.

**Why**: §6.4's rule — the list is append-only below the pinned head, and any
mutation of `messages[0..k]` is a cache-invalidating bug — is easy to state and
easy to violate by accident three refactors later. Immutability makes the
accident impossible rather than merely detectable.

Superseding a stale file read therefore *replaces* a message rather than
removing it: removing renumbers everything after it, and a tool result whose
matching `tool_call_id` has vanished is a malformed conversation, not a smaller
one.

---
**D-32 · Compaction retains a token budget, not a message count**

`compact(retain_pct=0.35)`.

**Why**: the first implementation kept a fixed number of recent messages, and
the budget regression test caught it thrashing — 16 compactions in 25 turns.
Four capped `read_file` results are 24k tokens, which is 73% of a coder budget,
so a count-based compaction handed back a context already above the threshold it
had just fired at.

This is Part B §10.4's mistake one layer down: that section retires the frontend
agent's `contextMaxMessages` setting on exactly this ground — "a message *count*
is the wrong unit; forty messages can be 5k tokens or 200k" — and I reproduced
it inside my own compaction. Fixing it took compactions from 16 to 8 and prefill
from 430k to 348k.

Rarity matters twice: each compaction rewrites the middle of the message list,
which invalidates every cached prefix below it.

---
**D-33 · Token estimation is a documented heuristic with a calibration seam,
not `tiktoken`**

**Why**: `tiktoken` ships OpenAI's encodings. The model is Qwen3.8-27B behind
LiteLLM, which tokenizes differently — so `tiktoken` would be a precise answer to
the wrong question, and it would look authoritative while being wrong. The real
Qwen tokenizer means a `transformers` dependency and a model download on a
laptop meant to work offline behind a corporate proxy (Part B §4.2).

The estimate only has to answer "will this fit". "What did this cost" is
answered by the API's `usage` field, which is authoritative and is what the
ledger bills from. `Calibration.observe()` folds that measurement back into the
ratio, so after a handful of real turns the estimate is measured rather than
guessed — which is exactly the reconciliation §16.4 asks for and the frontend
agent omits.

---
**D-34 · Reasoning is off in every mode**

`ModeConfig.enable_thinking` is `False` throughout, and the dataclass refuses to
construct a thinking-on mode with fewer than 6,144 output tokens.

**Why**: the spike measured it. Identical prompt, only `max_tokens` varied —
thinking off produced a 517-character answer in 2.0 s; thinking on produced
1,247, then 9,948, then 4,828 characters of reasoning across three runs, taking
4.5 s, 31.4 s and 15.4 s, and returned the same ~330-character answer every
time. Reasoning expands to fill the available budget, non-deterministically, so
it is not a cost anyone can budget for — and on this task it bought a 15×
latency penalty for nothing. It also failed outright twice, both times on a turn
that had to produce structured output.

The Debugger is the one place it might genuinely pay, because ranking hypotheses
is the rare task where the reasoning text *is* the deliverable. It is left off
here so that switching it on is a deliberate edit with an A/B behind it, not a
default nobody chose.

---
**D-35 · `novel_tokens` is the prefix-cache proxy metric**

**Why**: §18 proposes alerting on P95 prompt tokens while
`prompt_tokens_details.cached_tokens` is unavailable (plan.md §9 Q1). Novel
tokens is a better stand-in: P95 catches a prompt *growing*, but novel tokens
catches the thing that actually costs money, which is a prefix being
*invalidated* — a mutated system message, a mode switch inserted in the wrong
place, a compaction rewriting the middle of the list. Those cost a full prefill
while leaving P95 untouched.

It is also what let the budget gate assert §5.3's 180k target as it is actually
written — "≤180k (cap + compaction + **prefix reuse**)" — rather than against a
raw sum the target never meant.

---
**D-36 · The agent and gateway are separate packages from day one**

`apps/agent` and `apps/gateway` are distinct distributables; `apps/shared` is a
dependency of both.

**Why**: Part A §19.1's security boundary. The gateway holds the code that reads
`DAKCODER_MODEL_API_KEY`, performs the GitLab token exchange and signs JWTs.
`postgen` ships its entire gateway inside the wheel every developer installs;
doing the same here would weaken the invariant from *"the code that reads the key
is not on the machine"* to *"the key is not on the machine"* — a much weaker
claim, and the first thing a security review will pull on.

Establishing the split before the gateway exists costs nothing. Retrofitting it
means untangling imports.

---
**D-37 · The LLM client is built on httpx, not the OpenAI SDK** ·
*diverges from Part A §4.7*

**Why**: everything that actually matters here is about *what request is made*
and *how the stream is consumed* — `chat_template_kwargs` for reasoning control,
`stream_options.include_usage` for accounting, `trust_env=False` so the
corporate proxy is never inherited, HTTP/1.1 because nginx's HTTP/2 handling
breaks long-lived streams, and a retry policy we own rather than one the SDK
owns. None of that needs the SDK.

Dropping it removes a package and its dependency closure from the wheels Part B
§4.3 has to vendor into the `.vsix` for offline install — which is a real gain
on the path where the first-run experience can fail on the corporate network.
The request shape is unchanged, so the seam remains if the SDK is ever wanted.

**Cost to reverse**: low. `LLMClient` is one file behind a small surface.

---
**D-38 · Both are tested against a fake endpoint, and this is better than
testing against the real one**

`apps/conftest.py` is a configurable stand-in for LiteLLM + vLLM, with every
response shape taken from plan.md §4.2's verified capability matrix — including
the two documented *absences*: no `prompt_tokens_details`, and `reasoning_effort`
returning a 400.

**Why**: I initially deferred both components on the grounds that they could not
be verified without the live endpoint. That was wrong, and it conflated two
different things. Against production a test can only ever confirm the happy
path. Against a fake it can assert the *failure* paths — that the probe catches
a missing usage chunk, that a 429 is retried and a 400 is not, that
`content: null` raises a typed error, that an HTML error page is reduced to one
sentence. Those are precisely the behaviours nobody exercises by hand, and the
ones that decide whether an endpoint change surfaces as a clear failure or as a
week of confusing agent behaviour.

The probe's own test suite makes the point: `test_each_drift_fails_exactly_the_
check_that_owns_it` turns on one drift at a time and asserts exactly one check
goes red. A probe whose checks bleed into each other cannot name a cause, and
naming the cause is the entire point of running one. That test is impossible
against a real endpoint.

**What is still unverified**: whether the real endpoint currently behaves as
plan.md §4.2 documents. That is the probe's runtime job, not a build-time claim,
and it is answered the first time the gateway starts.

---
**D-39 · The probe never fails on an absent `cached_tokens`, and asserts that
`reasoning_effort` is *rejected***

Two checks that look backwards until you consider what the alternative would
mean.

`prompt_tokens_details.cached_tokens` is missing from this endpoint, and its
absence is plan.md §9 Q1 — a tracked open question, not a regression. A probe
that failed on it would be red from its first run, and a check that is always
red is a check that gets disabled. It is reported as informational in both
directions, and says so explicitly the day the field appears.

`reasoning_effort` returning a 400 is asserted as a *pass*. If it ever starts
succeeding, `drop_params` has been turned on and unknown parameters are being
silently dropped rather than refused — so every future request typo would fail
quietly instead of loudly, and §4.5's whole premise, that the endpoint tells us
when we are wrong, would be gone.

---
**D-40 · An empty completion is a typed error, and recovery turns thinking
*off* rather than raising the budget**

`EmptyCompletionError` carries the finish reason and the reasoning-token count,
and `chat()` retries once with `enable_thinking: false`.

**Why**: §4.4's rule 3. `content: null` with `finish_reason: "length"` is a
wasted turn, and treating it as an empty string is how a wasted turn becomes an
invisible one. Recovery raises no budget because a bigger budget is exactly what
produced the spike's 31-second run for the same 330-character answer — reasoning
expands to fill whatever it is given.

The check is guarded on having actually seen a `content` key, so a pure
tool-call turn — which legitimately carries no content — is not mistaken for it.

---
**D-41 · The budget is enforced before dispatch, not by the endpoint**

`agent.llm.complete()` raises `OverBudgetError` rather than sending.

**Why**: a prompt over budget is a bug in the caller — the context manager
exists to prevent exactly this. Letting it through means finding out via a 400
from someone else's proxy, several seconds later, with the turn already spent.
The error names the per-layer breakdown so the caller can see what to compact.

---
**D-42 · The verification gate is a pipeline, not a menu**

`gofmt`, `swagger_check`, `golangci_lint` and `govulncheck` are marked
`gate_only`: the gate dispatches them on a fixed schedule and no mode is ever
offered them as a tool.

**Why**: §9.3 specifies the gate as an ordered fail-fast sequence, which is a
pipeline. A model that *chooses* whether to run `go vet` is a model that
sometimes does not, and "it said it was done and it wasn't" is the exact failure
this whole design exists to prevent. Not asking is the only way to not depend on
the answer.

It also pays for itself in tokens. Removing the five gate stages from the schema
lists cut the Verifier from 19 tools to 9 and the Coder from 18 to 16 — no mode
now spends more than ~2.2k tokens on schemas.

The gate needs its own dispatch path (`Router.run_gate_tool`, `gate=True`) that
bypasses the mode filter and the approval layer. That is not a hole: those two
exist to constrain what the *model* may do, and applying them to the harness
would mean the gate needed permission to check the work.

---
**D-43 · The plan's "≤12 schemas per turn" is not met, and the token target is**

Planner 9, Scaffolder 11, Verifier 9, Coder 16, Debugger 19.

**Why record a miss rather than fix it**: §7.1's "~12" is a proxy for a token
budget, and the budget is met — the largest mode spends ~2,200 tokens of a
32,768 cap, 6.7%, in the *stable prefix* where it is paid once per prefix rather
than per turn. The cuts that were justified on their own merits were made (D-42).
Hiding three more tools from the Coder to reach a round number would trade real
capability for a number nobody measures.

It will get worse before it gets better: wiring `gopls` returns `go_symbols` and
`go_diagnostics`, taking the Debugger to 21. That is the point at which the count
should be re-examined against a measurement rather than against the plan's
estimate.

---
**D-44 · The sidecar bridge speaks MCP directly rather than through the SDK**

`apps/agent/tools/gotools.py` writes newline-delimited JSON-RPC to a long-lived
`gotools mcp` process. The `mcp` Python package is installed and unused.

**Why**: three methods (`initialize`, `notifications/initialized`, `tools/call`)
against a server in this same repository. The SDK is async, which would push an
event loop into an otherwise synchronous agent, and it is another package in the
closure Part B §4.3 must vendor for offline install. Same trade as D-37, and the
same result — with the added benefit that the test spawns the *real* binary, so
it proves protocol compatibility rather than compatibility with a mock.

One process for the session, not one per call: startup plus handshake is ~35 ms,
which across a hundred lint calls is several seconds of the latency budget §3
calls the single biggest lever. A dead sidecar is restarted once and the call
retried; twice means the input is the problem, and retrying forever would turn a
bad argument into a hang.

---
**D-45 · `gofmt -w` converts CRLF to LF, so the gate puts the endings back**

Measured: `package p\r\n` comes back `package p\n`.

**Why this matters more than it sounds**: §9.3 already noticed the symptom —
"every `.go` file in `new-template` is flagged by `gofmt -l`, because they all
use CRLF" — and drew the right conclusion, scope `gofmt` to mutated paths. But
scoping shrinks the blast radius rather than removing it: the *touched* files,
which are exactly the ones under review, come back with every line changed.

Worse, it silently undid `patch_file`'s line-ending preservation one step later.
That is worse than never having had it, because the unit test for `patch_file`
still passed. Preserving an invariant in one component and destroying it in the
next is the failure mode that unit tests are structurally blind to.

So `gofmt` captures each file's ending before formatting and restores it after,
and a file whose only change was the ending is not reported as a mutation —
otherwise every gofmt run would mark every touched file as modified and the
mutation list the gate scopes itself to would fill with files nothing happened to.

The same reasoning fixed the Go side: `scaffold.Apply` now writes new files with
the repository's existing ending. Before that, one scaffold call produced CRLF
for the two files it *edits* (gopatch preserves) and LF for the five it
*creates* — a split nobody chose, in a repository with no `.gitattributes`, which
converts silently the first time anyone sets `core.autocrlf`.

---
**D-46 · Refusals redirect by alias table, not only by edit distance**

`grep` → `search_repo`, `cat` → `read_file`, `str_replace` → `patch_file`,
`bash` → `run_terminal`, and thirty more.

**Why**: `postgen`'s single highest-value line was `run_terminal` refusing `grep`
and naming `search_repo`. Edit distance cannot reproduce it — `grep` and
`search_repo` share no prefix — because these are not misspellings. Every one of
them is a *correct* tool name in some other harness or in the shell, which is
precisely why a model reaches for it and precisely why fuzzy matching fails.

A bare "unknown tool" costs a full turn while the model guesses; at ~4 s a turn
that is measurable. Naming the tool costs nothing.

---
**D-47 · Blocking is decided by who caused the problem, not by what is wrong**

Three stages make the same distinction:

* `rules_lint` blocks on in-scope violations and reports `out_of_scope_count`.
* `swagger_check` blocks on an unnamed route (the agent's doing) and reports a
  missing `swagger.generation.mode` (absent from all six reference configs).
* `go_mod tidy` blocks on a diff, and names the modules that moved so a reader
  can tell an inherited defect from something this run did.

**Why**: blocking on pre-existing damage makes the first change to any legacy
service impossible — and legacy services are precisely the codebase this agent
exists to help with (D3). A gate that fails every run on a defect the service
was born with is a gate that gets switched off within a week, and then it catches
nothing at all.

The `go mod tidy` case is live: the reference template's `go.mod` requires
`api-db` while its code imports `n-api-db`, so the *first* gate on any service
derived from it fails, tidy has already applied the fix, and the second gate
passes. That sequence — detected, corrected, confirmed — is the design working,
and `test_the_first_gate_catches_the_templates_own_go_mod_drift` pins it.

---
**D-48 · Path confinement refuses `..` outright, including when it normalises inside**

`handler/../handler/user.go` lands on a legal file and is still refused.

**Why**: allowing it makes the rule "resolve, then check", which is one subtle
bug away from "resolve, then check the wrong thing". No legitimate tool call
contains `..` at all. Refusing the syntax keeps the rule statable in one
sentence, and a security rule that cannot be stated in one sentence does not
survive contact with a growing codebase.

Three classes of escape are handled, because handling only the obvious one is
the usual mistake: syntactic (`..`, absolute, drive letters, UNC), symbolic (a
path that normalises clean and resolves through a symlink — caught by re-testing
containment *after* resolution), and Windows-specific (reserved device names,
alternate data streams, trailing dots and spaces that Win32 strips after
validation). The Windows rules are enforced on every platform: a path written on
a Linux runner is opened on a Windows machine, and a rule that only fires where
the damage happens is not a rule.

---
**D-49 · Approval is returned, never raised, and never resolved by the router**

`Router.dispatch` returns `ToolResult | ApprovalRequest`.

**Why**: needing approval is an ordinary outcome, not an error, and modelling it
as an exception makes the happy path and the common path different shapes. The
router stays synchronous and pure — who asks the developer, how, and what
happens if they walk away are the caller's problem, which is the only way the
same router serves the CLI, the extension and the test suite.

`always_ask` cannot be overridden by session auto-approval. A blanket "yes to
everything" that also covers `delete_file` is how an approval layer becomes
decoration.

---
**D-50 · Search is pure Python, not ripgrep**

**Why**: "use `rg` when installed, fall back otherwise" means the same pattern
matches differently depending on the machine. Rust's regex crate has no
backreferences and no lookaround; Python's has both; character classes differ. A
pattern that works for one developer and silently returns nothing for another is
worse than being uniformly slower — and it is the kind of bug that gets blamed on
the model. The services this agent works on are a few thousand files.

The glob matcher is hand-written for the same reason `fnmatch` could not be used:
it lets `*` cross `/`, so `handler/*.go` would match `handler/response/user.go`,
silently widening every scoped search into an unscoped one. One translation
serves both `search_repo` and the protected-path check, so a pattern means the
same thing in both places.

---
**D-51 · The catalogue is generated from the registry, both halves**

`docs/TOOL-CATALOG.md` is now the model-facing contract (29 tools);
`gotools/docs/TOOL-CATALOG.md` remains the sidecar's (7).

**Why two**: the shapes are allowed to differ, and do. `rules_lint` takes a
comma-separated string on the model side and an array on the sidecar side,
because only the model side is bound by C1's six-parameter limit. Publishing
only the sidecar's half would document the seam from the wrong side — which is
how three contract mismatches survived until the first end-to-end run
(`fx_wire` wanting a bare constructor name, `project_scaffold` taking two specs
rather than one, and `list_filters` rejecting the `type` field that plan.md
§10.1's own example envelope includes).

C1's limits are enforced when the registry is imported — a violation is an
`ImportError`, not a lint warning — and checked again from outside in
`test_catalog.py`, because a claim about a safety property is worth verifying
from something that does not share its code path.

---

## 4. Verification strategy

The load-bearing assertions, and what each is guarding against:

| Assertion | Guards |
|---|---|
| The reference `user` resource passes every rule | A wrong rule. If it fires on the template, *the rule* is wrong. Caught three false positives. |
| The scaffolder's output passes every rule | The two halves of `gotools` disagreeing, which would open the agent's loop with a violation it did not cause |
| The scaffolder's output compiles and passes `go vet` | What syntax-only analysis structurally cannot see — `dblib.InsertReturning`'s arity, a missing `time` import. Both were wrong in the first draft. |
| A greenfield service resolves, compiles and lints clean | A scaffold whose first act is to fail |
| Output is byte-identical across runs | Golden snapshots meaning anything |
| Every violation carries a fix and a citation | Findings nobody can act on |
| Every citation resolves to a real heading | Violations pointing at text a reader cannot navigate to. Caught five. |
| Published contracts are current | A stale schema shipping to another team |
| `repo_map` stays inside its token budget | Finding S4 — a 20–30k tool result resident from turn one |
| The walk prunes and reads each file once | Findings S2 and S3 |
| P95 prompt ≤ 24k, effective prefill ≤ 180k | §21's first risk |
| Compaction does not thrash | D-32's regression |
| The probe reddens on each simulated drift, and only the owning check | A probe that cannot name a cause |
| A local runtime refuses to start holding a model key | The unmetered bypass §15.4 exists to prevent |
| A 429 is retried and a 400 is not | Spending a second re-learning that our request is malformed |
| The section 11.1 recipe runs end to end on a real service | Contract drift between the Python tool layer and the Go sidecar — invisible to both sides' unit tests. Caught three on its first run. |
| A Planner call to `write_file` is refused | Mode filtering being a prompt instruction rather than a property |
| Every refusal carries an actionable fix | Turns spent guessing. Checked for every tool in the registry, not sampled. |
| No tool can reach outside the workspace | One hallucinated path reading a developer's private key |
| A patched CRLF file is still CRLF *after the gate* | D-45: an invariant held in one component and destroyed by the next |
| The gate runs even when the model says it is done | The failure this whole design exists to prevent |
| The same call three turns running stops the run | A stuck loop spending the whole token budget arriving nowhere |
| A tool that raises becomes a result, not a crash | One bad tool ending a session the model could have recovered |
| No committed credential reaches the event stream | The stream is logged, traced, and screenshotted |
| The published C1 catalogue matches the registry | A contract document drifting from the code, silently |

---

## 5. Changelog

### 2026-08-25 — Phase 0, part 3: the three contracts

- `internal/kb` + `gotools doc-check`: rule citations resolved against
  `skill.md`/`SOP.md`, documents pinned by hash (§14.4). Found and fixed **five
  rules citing SOP.md intro bullets as if they were sections**.
- `internal/catalog` + `gotools tool-catalog`: contract C1 generated from the
  live MCP schemas, with a C1 conformance check and a freshness gate (§11 #6).
- `internal/kb` builder + `gotools knowledge`: the progressive-disclosure
  knowledge base (§14.2) — `SKILL.md` at 944 tokens plus twelve assembled
  references carrying corrections. Surfaced a second config defect:
  **`config.prod.yaml` has no `cache:` block at all**, so 15 keys return zero
  values in production.
- `make ci` gained `doc-check`, `tool-catalog-check`, `knowledge-check`.

### 2026-08-25 — Phase 1: the tool router, the gate, and the loop

The MVP happy path now runs end to end. `test_happy_path.py` plans, scaffolds a
Pension resource into a real copy of the reference service, wires it, and takes
it through the full gate — `go build`, `go vet`, `rules_lint`, `swagger_check`,
`go mod tidy` — against real private modules, clean. Only the model is scripted.

- `shared/paths.py`: workspace confinement, three classes of escape (D-48).
- `shared/envelope.py`: contract C1's result shape and C2's event stream, with
  `assistant_delta` coalesced at the source (fix S11).
- `agent/tools/registry.py`: 29 tools, C1 enforced at import (D-51).
- `agent/tools/router.py`: six ordered checks between a model's call and a tool
  running, with every refusal naming an alternative (D-46, D-49).
- `agent/tools/fs.py`, `commands.py`, `knowledge.py`: the tools themselves, plus
  BM25 retrieval over the knowledge base and eleven §13.2 playbooks.
- `agent/tools/gotools.py`: the sidecar bridge (D-44).
- `agent/gate.py`: §9.3's two-speed verification (D-42, D-47).
- `agent/loop.py`: five modes, one system prompt, C2 events.

**Findings this phase produced**, each now pinned by a test:

- `gofmt -w` strips CRLF, undoing `patch_file` one step later (D-45).
- `scaffold.Apply` wrote LF into a CRLF repository, while the files it *edited*
  kept CRLF — one call, two conventions (D-45, fixed on the Go side).
- Three contract mismatches between the Python tool layer and the sidecar,
  including one where plan.md §10.1's own example envelope is wrong (D-51).
- The reference template's `go.mod`/import drift makes the first gate of every
  derived service fail, correct itself, and pass (D-47).

### 2026-08-25 — Phase 0, part 5: the LLM client and the capability probe

- `apps/shared/config.py`: the credential invariant, enforced at construction —
  a local runtime holding a model key fails at startup rather than quietly
  using it (§4.7 enforcement point 1).
- `apps/shared/llm.py`: request shaping, SSE streaming, tool-call reassembly,
  retry with backoff, usage accounting, `content: null` as a typed error.
  Built on httpx rather than the OpenAI SDK (D-37).
- `apps/gateway/probe.py`: six checks against plan.md §4.2's capability matrix,
  reporting rather than raising, with the impact of each failure named (D-39).
- `apps/conftest.py`: a configurable fake LiteLLM endpoint, which is what makes
  the failure paths testable at all (D-38).
- `apps/agent/llm.py`: the three components composed, with the budget enforced
  before dispatch (D-41) and usage reconciled into the estimator after it.

**Correcting an earlier call.** Both of these were deferred in part 4 on the
grounds that they could not be verified without the live endpoint. That was
wrong — see D-38. The deferral cost nothing but it was poor reasoning, and it
is recorded here rather than quietly fixed.

### 2026-08-25 — Phase 0, part 4: the context manager

- `apps/shared`: token estimation with a calibration seam (D-33).
- `apps/agent/modes.py`: per-mode budgets and reasoning control (D-34).
- `apps/agent/context.py`: budget, insertion caps, the file-slice ledger,
  stable-prefix discipline, compaction (§6).
- `apps/agent/tests/test_budget_regression.py`: §20.5's gate. Caught D-32.
- Name settled (D-01); Go module path moved to `…/dakcoder/gotools`.

### 2026-08-25 — Phase 1/2 (early): scaffolders and `repo_map`

- `internal/spec`, `internal/scaffold`, `internal/fxwire`, `internal/gopatch`,
  `internal/naming`: `resource_scaffold`, `project_scaffold`, `fx_wire`, with
  golden snapshots and real compile verification.
- Four missing §9.2 rules: `secrets-in-config`, `config-key-exists`,
  `swagger-visible`, `go-idiom` — bringing the compliance set to the 21 the plan
  specifies.
- `internal/repomap`: `repo_map` with the §6.2 budget and the §20.5 walk gates.
- Fixed `legacy-handmade-health`'s false positive; refined `repo-rowmapper` to
  accept every by-name pgx mapper.
- Bugs found by new tests: `gotools rules --format json` crashed outright
  (`encoding/json` cannot marshal `Rule.Check`); import grouping filed every
  first-party import under the standard library.

### Earlier — the rules engine

- `internal/workspace`, `internal/rules`, `internal/mcpserver`, `cmd/gotools`:
  17 compliance and 9 legacy rules over MCP and a CLI.

---

## 6. Open questions

### 6.1 Needs a named owner — blocking nothing yet, blocking Phase 1 soon

1. **Prefix caching (plan.md §9 Q1).** `prompt_tokens_details.cached_tokens` is
   absent from the endpoint, so the hit rate cannot be measured. Everything in
   §6 is built to make the prefix reusable regardless, and `novel_tokens`
   (D-35) is the stand-in — but the difference between 142k and 348k of prefill
   per task turns on this answer. **Highest-priority operational question.**
2. **Who operates `ai.cept.gov.in`.** Needed for Q1, for LiteLLM's response
   cache being off for agent traffic, and for capacity.
3. **Endpoint capacity.** Every number in the quota model is a placeholder until
   this is known.
4. **API key rotation.** The supplied key has been circulated in plain text.

### 6.2 Raised with the template owner

These came out of building the analysis and are independent of this programme:

- **Committed credentials** in `new-template/configs/*.yaml` — surfaced by
  `secrets-in-config`, twelve of them, never echoed.
- **`UpdateUserByID` panics on a partial update** (D-17).
- **`swagger.generation.mode` is absent from all six environment configs**, so
  any non-default environment generates no OpenAPI document.
- **`config.prod.yaml` has no `cache:` block**, so 15 keys read as zero in
  production.
- **`go.mod` requires `api-db` while the code imports `n-api-db`** — an untidied
  require block that made `repo_map` initially label the reference template as
  legacy.

### 6.3 Deferred, with reasons

- ~~**The capability probe** and **the LLM client**~~ — both were deferred here
  for bad reasons and are now built. See D-38: "cannot hit production" is not
  "cannot verify", and a fake endpoint tests the failure paths that production
  never could. What remains genuinely unverified is whether the *real* endpoint
  currently behaves as plan.md §4.2 documents, which is the probe's runtime job
  and is answered the first time the gateway starts.
- **`request-domain-type-match`** needs the `go/types` gate tier that D-10
  deliberately deferred.
- **`swagger_check`'s boot-and-diff half** and **`govalid_gen`** are command
  runners, which belong to the Python tool router rather than to `gotools`.
