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
| `apps/agent/prompts` — one system prompt, five overlays | **built**, 791/1,200 tokens |
| `apps/gateway/auth` — PKCE, JWT, roles, refresh rotation | **built** |
| `apps/gateway/quota` — rolling windows, reserve/reconcile | **built**, two stores |
| `apps/gateway/ledger` — append-only usage events | **built** |
| `apps/gateway/proxy` — `/v1/llm/*`, SSE tee | **built** |
| `apps/gateway/app` — the HTTP surface (C1, C3, C4) | **built** |
| `apps/agent/session` — event log, resumption, abort, revert | **built** |
| `apps/agent/loopback` — the endpoint the extension talks to | **built**, API v1.0 |
| `apps/agent/serve` — `dakcoderd`, the spawnable runtime | **built** |
| VS Code extension (Part B) | **unblocked** — see §7 |

**Part A is complete.** Every backend surface Part B binds against exists,
is tested, and is documented: the tool catalogue (C1), the event stream (C2),
sign-in (C3), quota (C4), and the context budget the server owns (C5). The
handover is §7.

**Phase 0 is complete, Phase 1's happy path runs end to end, and the gateway
is built.** Identity, quota, the ledger and the model proxy are done, with the
one control everything else rests on in place: the model API key exists in
exactly one process, and every model call is metered, attributed and recorded
before it reaches the GPU.

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
**D-52 · The prompts are files, and the budget is a test**

`prompts/system.md` plus five `prompts/modes/*.md`, loaded through
`importlib.resources`. 791 tokens against §6.1's 1,200; overlays 134–199.

**Why files**: prompt review is the highest-leverage review there is, and a
`.md` diff is readable in a way a Python string literal is not. It also makes
the byte content hashable and pinnable, so a change to a prompt becomes a
deliberate act rather than a side effect of editing whatever module happened to
contain it.

**Why the budget is a test**: a prompt is the easiest thing in a codebase to
grow by accident. Every addition looks individually reasonable and nothing fails
when it gets too long — the working set just quietly shrinks. `test_prompts.py`
asserts the ceiling, and asserts the whole prefix leaves ≥26,000 tokens for code.

Newlines are normalised on read. A prefix whose bytes depend on the reader's git
configuration is not a stable prefix: it would produce a different cache key on a
colleague's machine for a file neither of them edited.

---
**D-53 · The gateway is async; the agent stays synchronous**

**Why not one or the other**: they are different shapes of program. A gateway
serves many concurrent requests each mostly waiting on I/O, which is what an
event loop is for. An agent loop is strictly sequential — one turn, then the
next — and making it async would add an event loop, colour every function, and
buy nothing, because there is never a second thing to do while a turn is in
flight.

The seam is the HTTP boundary, which is a process boundary anyway. The one place
it could have leaked is the `gotools` bridge, and D-44's hand-written client is
what keeps it synchronous.

---
**D-54 · The quota store's contract is one atomic method**

`apply(sub, checks, now) -> Applied`, all-or-nothing. Not `check()` then
`consume()`.

**Why**: two turns arriving together must not both read "599,000 used" and both
proceed. And a request refused by the weekly cap must not have already consumed
from the hourly one, or a client retrying into a wall drains a budget it never
spent. Check-then-consume as two calls has both bugs; a port that does not offer
them separately cannot be implemented with either.

The conformance suite includes a **deliberately wrong store** that checks, yields,
then writes — and requires it to fail the atomicity assertion. Without that, a
passing suite proves nothing: it might be asserting things true of any code at
all. `NaiveStore` is not a strawman; it is what most rate limiters look like
before someone notices the counters do not add up.

`RedisStore` carries the Lua translation and is honestly labelled untested here:
the suite runs against it only when `DAKCODER_REDIS_URL` reaches a server. A mock
of Redis would execute the script exactly as correctly as the mock was written.

---
**D-55 · Reserve high, settle true — and settle in both directions**

The frontend agent reserves a flat 4,096 tokens per call and never refunds, so a
turn that used 300 is billed 4,096 and the error compounds across forty turns
(finding S18). Here the reservation is provisional and the endpoint's own usage
figure replaces it.

The direction that is easy to leave out is the other one. An *under*-reservation
is **charged**, not refused: the tokens are already spent by the time we know, so
refusing would only make the counters wrong, and the overshoot correctly bites on
the next reservation. Both directions are one signed `adjust()`. The first draft
had `refund()` with an early return on non-positive amounts, which silently made
under-reservations free — caught by writing the test for it.

---
**D-56 · Fail closed everywhere, with one deliberate exception**

If the quota store is unreachable, requests are refused. There is exactly one
`_guarded` wrapper so there is exactly one place where infrastructure trouble
becomes an answer — scattering try/except is how one path ends up defaulting to
"allow" and nobody notices until the audit.

**The ledger is the exception, and it is reasoned rather than convenient.** A
failed ledger write is logged and swallowed. The quota decision has already been
made and enforced by the time a row is written, so losing a row costs reporting
accuracy; refusing the turn would cost a developer their work over a bookkeeping
problem. The counters stay correct and the hole is logged.

`StoreUnavailable` maps to 503 with `Retry-After`, not 500: the request is
refused because it cannot be *metered*, which is a temporary condition of ours
rather than a fault in what was sent — and that distinction tells the client to
retry later rather than to change the request.

---
**D-57 · `/v1/auth/start` is added to §15.2's flow, for a security reason**

In the plan's diagram the extension generates `state` itself. That means the
gateway receives a value it has never seen and cannot check — so the CSRF
protection `state` exists to provide is unenforceable on the only side that could
enforce it.

Having the gateway issue and store it closes an authorization-code injection: an
attacker who gets a victim's browser to complete a flow cannot then post that
code, because they hold no state the gateway issued. The state is single-use, ten
minutes, and **bound to the redirect URI** — otherwise a flow started for the
`vscode://` handler could be completed against a loopback port an attacker owns.

An unknown state is refused unconditionally. There is no fallback for the plan's
original shape: accepting a state we did not issue makes the check decorative,
and a control that can be satisfied by guessing is worse than none because it
reads as protection in a design review.

**Cost**: one extra round trip at sign-in, once.

---
**D-58 · Refresh rotates, reuse kills the family, and access tokens die with it**

OAuth 2.0 BCP. A refresh token is a thirty-day credential in a keychain; the one
thing that makes theft survivable is noticing when both the thief and the owner
use it.

We cannot tell which party is which, and the safe reading of "cannot tell" is to
end the family. A legitimate user signs in again; a thief loses everything.

The part that is easy to omit: **revoking a family invalidates its access tokens
immediately**, checked at verify. Without it a stolen access token stays good for
its full fifteen minutes after the theft was detected — exactly the window the
detection was meant to close. Access tokens carry a `fam` claim for this.

Every refresh re-reads the account from the IdP, which is what makes revocation
real: a blocked GitLab account loses access within one token lifetime and nobody
runs a deprovisioning step.

---
**D-59 · The client names a role; the gateway names the model**

`model: "coder"` in, `model: "Qwen3.8-27B"` out.

**Why**: forwarding the client's `model` would let a developer route to a model
nobody has budgeted for, on a shared GPU, with our key attached. Only two paths
are proxied — `chat/completions` and `embeddings` — so a new upstream capability
cannot become reachable by accident.

`stream_options.include_usage` is forced on every stream rather than trusted from
the client. Without the usage chunk there is no accounting and quota could only
be enforced from reservations, which is exactly S18.

`user` is set to the subject, so LiteLLM's own spend tables attribute correctly
even before per-user virtual keys exist (§16.6 phase 1). It costs nothing and
makes the cross-check meaningful from day one.

---
**D-60 · The stream is primed before the response starts**

The first chunk is pulled inside the route, before `StreamingResponse` is
returned.

**Why**: everything that can fail before the model produces a byte — quota
refused, unknown role, unreachable upstream — happens inside the generator. Once
the response is returned the status line is on the wire, and an exception raised
then cannot change it: Starlette says "Caught handled exception, but response
already started" and the client sees a 200 that stops mid-stream. This was a real
bug, found by the HTTP tests and not by any unit test.

After that point the status code is spent, so a mid-flight failure becomes a C2
`error` event. Dropping the connection instead would be indistinguishable from a
network fault — which clients retry, doubling the cost of whatever went wrong.

Related, and measured rather than assumed: httpx strips the blank line that
frames an SSE event, so the relay re-terminates every line. A relay that forgets
produces a stream parsing as one enormous event, which looks exactly like the
model hanging.

---
**D-61 · A missing usage chunk does not refund the reservation**

If the stream produced output but reported no usage, the reservation stands.

**Why**: a turn that produced output certainly cost something, and refunding what
we cannot measure would make a broken endpoint the cheapest way to use the
service. The capability probe's `usage_chunk` check exists so this surfaces as an
endpoint fault rather than being absorbed silently.

Same reasoning for a client that disconnects mid-stream: the model produced those
tokens, so settlement runs in a `finally`. Otherwise abandoning turns would be
the cheapest way to work.

---
**D-62 · The cached-prefill discount is wired, dormant, and read anyway**

`cached_discount` defaults to 1.0 — no discount — because
`prompt_tokens_details.cached_tokens` is absent from this endpoint (plan.md §9
Q1). The proxy reads the field regardless, so the day it appears the discount has
data rather than needing a code change.

**Why keep it visible while dormant**: discounting cached prefill makes a session
with good context discipline go further than one without, which points the quota
model at the same behaviour the latency work rewards. Defaulting to a discount we
cannot verify would under-bill; deleting the mechanism would mean rediscovering
the intent later.

---
**D-63 · A refusal says what was requested, and whether waiting will help**

A 429 carries used, limit, *requested*, and a one-sentence human reason.

**Why the third**: "you have used 920 of 5,000" is true and useless when the ask
was 9,000. And a request larger than the limit itself is flagged separately —
every other refusal is answered by waiting, and telling someone to wait for
something that will never happen is the worst possible answer, because they wait.

The reset time is when the *oldest* event ages out, not a fixed period boundary.
That is the whole difference between a rolling window and a bucket, and it is the
number a developer needs in order to decide whether to wait.

---
**D-64 · Every event carries an id, and the log is the source of truth**

Part B §14 names the one real gap in the current client: the SSE parser handles
a clean stream well but has no resumption path, so a dropped connection loses the
live view of a run that is still executing — and the developer cannot tell that
from the run having died.

Events are persisted **before** they are sent. The order is the whole point:
sending first would let a crash between the two produce an event the client saw
and the log does not have, and resumption would then silently skip it. A hole
that looks like nothing is wrong is worse than a dropped connection.

`Last-Event-ID` is honoured alongside an explicit `since_id`, because that is
what a browser's `EventSource` sends automatically on reconnect — a client that
does nothing special still resumes correctly.

Transient events (`assistant_delta`, `heartbeat`) are relayed but not stored.
They are superseded by the `assistant` message that follows, so replaying them
would re-type an answer the client already has in full.

---
**D-65 · The run is on a worker thread, and that is load-bearing twice**

Not a performance choice. Two things become impossible if the loop runs on the
event loop:

* **Abort.** The endpoint has to answer *while* a run is in flight, and a turn
  can be minutes long. An inline loop blocks exactly the request that has to get
  through.
* **Approval.** The loop blocks on a `threading.Event` that an HTTP handler sets.
  On the event loop that is a deadlock: the run waits for a decision the server
  cannot deliver because the run is holding it.

The bridge back is `call_soon_threadsafe`, guarded. A server shutting down while
a run is in flight closes the loop, and an unguarded call then raises *inside the
worker thread*, killing it and leaving the session stuck at "running" for ever.
Found by the tests, not by reasoning.

---
**D-66 · Abort is checked at two points, not one**

At the top of each turn, and before each tool call in a batch.

**Why both**: Part B §12 keeps both checks because they exist for real "stopped
but kept moving" reports. A turn can run for minutes, and a single tool batch can
contain five writes — so checking only at the turn boundary produces exactly the
complaint "it stopped, and then three more files changed".

---
**D-67 · The `edit` decision re-dispatches the request's arguments**

Part B §9 calls `edit` the standout of the approval card, and the reason is
arithmetic: correcting a path costs nothing, while rejecting costs a turn and the
model usually makes the same mistake again.

The first version re-dispatched `call.arguments` — the model's original string —
so an approved edit applied the approval and silently discarded the correction.
That is the worst of the three possible outcomes: the developer believes they
fixed it, and the wrong thing happens anyway. It now re-dispatches the
`ApprovalRequest`'s arguments, which the approver may have replaced.

A timeout is a **refusal**. Nobody looked, so nobody agreed — and the failure
mode of the opposite default is a write that happened while the developer was at
lunch.

---
**D-68 · Revert reads git at revert time rather than snapshotting at write time**

§12: restore every path the session touched to HEAD, deleting files with no
baseline.

**Why not snapshots**: no memory is held for a revert that will probably never
happen, and the restored content is exactly what git would give a developer
typing the command themselves — which is what they will compare it against.

The plan is a separate call from the apply, because §12 asks for the
confirmation to list the exact paths: "revert my last task" is easy to fire by
accident.

**A bug worth recording.** The first version inferred "not a git repository" from
the exit code of `cat-file`, which conflates it with "the file is not in HEAD" —
and those lead to opposite actions. The first means the session created the file,
so revert **deletes** it; the second means revert cannot run at all. Deleting a
developer's file because git happened to be absent is a mistake with no undo. The
repository check is now its own explicit question.

---
**D-69 · The loopback token defends against the local machine, not the network**

Bound to 127.0.0.1, so there is no network to defend against. What there is, on a
developer laptop, is every npm postinstall script and browser extension that can
reach localhost — and an unauthenticated port there is an agent anyone can drive.

`secrets.compare_digest`, because a timing side channel on a local socket is
entirely practical.

`/v1/health` is deliberately exempt. It is what the extension polls for up to
sixty seconds while deciding whether the runtime came up, and a health check that
needs a credential cannot tell it whether the credential path is the broken
thing.

---
**D-70 · `dakcoderd` announces its port on stdout, before serving**

Port 0, then print what the OS gave us.

**Why**: a fixed port turns a second VS Code window into a confusing failure.
Announcing before serving means the parent has the number even if startup then
fails — and `listen()` happens before the announcement, so a parent that connects
the instant it reads the line does not hit a bound-but-not-accepting socket. That
race only appears on a fast machine, which is the worst kind to only appear on.

`Server.run(sockets=[...])` rather than `uvicorn.run(fd=...)`: passing a file
descriptor works on POSIX and fails silently on Windows, where socket handles are
not file descriptors. The primary platform here is Windows 11. Found by running
it, not by reading about it.

Prewarm is on by default, reversing Part B §3.3's current `--no-prewarm`. A
four-token probe in a background thread costs nothing a developer can perceive
and moves cold start off the first request — the one they are watching. Its
failure is recorded in `/v1/health`, never raised: a runtime that refuses to
start because the gateway was briefly unreachable is worse than one that starts
and says so.

---

**D-71 · The approval id is minted with the request, not by the runtime**

`ApprovalRequest` carries its own `id`, generated in the router where the request
is created. The loopback keys its `PendingApproval` table on that id rather than
minting a second one.

The first version had the loopback mint an id inside `approve()` — which the loop
calls *after* it has already yielded `tool_pending`. So the event announcing an
approval carried no id at all, and `POST /v1/approvals/{id}` had nothing to take.
The most-used interaction in the product could not be actioned from the event
that raises it. §7 of this document asserted the opposite, which is worse: a
handover document that is confidently wrong costs a day before anyone suspects it.

It was also a race. Even with an id on the event, registration happening after
emission means a client that answers the instant it reads the event can arrive
before the approval exists. So the loop calls `on_pending(request)` *before* the
yield, and the runtime registers there. Ordering, not luck.

`as_dict()` also carries `protected: list[str]` — which of the paths tripped the
flag. `reason` named them in prose and `paths` listed everything without saying
which, so a client wanting to badge the offending file had to parse a sentence.

**D-72 · `_summarise` returns a `Recap`, and the fallback returns one too**

`ContextManager.compact` is typed `Callable[[Sequence[Message]], Recap]` and
calls `.markdown()` on the result. `AgentLoop._summarise` was `(str) -> str`.

The failure was worse than a plain type error, because the `except Exception`
that was meant to protect a degraded recap swallowed the `TypeError` and returned
`text[-4000:]` — a *list slice* — which then died on `.markdown()` one frame
later, where the cause was invisible. The first compaction of any long run killed
it.

461 tests were green over this. Every compaction test supplied its own
correctly-typed summariser and drove `ContextManager` directly, so the summariser
that ships was never called by anything. The unit under test was not the unit
that ships, and that is the general lesson: a collaborator's contract is only
tested if the real collaborator is on the other end of it.

The replacement asks for structured JSON and parses it tolerantly (fenced or
bare), because `do_not_retry` is the field that earns the compaction — without it
the post-compaction agent cheerfully repeats the dead end that made compaction
necessary — and a prose summary loses exactly that. Both failure paths, a dead
gateway and an unparseable reply, return a real `Recap` built from the tail.

**D-73 · Steering: a correction the run reads before its next turn**

`POST /v1/sessions/{id}/messages` queues text that `AgentLoop` drains at the top
of each turn and appends as a user message.

The interface design review found the gap and it is the largest one in the
product: nothing let a developer disagree with a run in progress. The answer to
"the agent is going the wrong way at turn 12" was Stop, which ends the run and
discards twelve turns of context. The alternatives all failed — a queued message
did not arrive until `finish`, a rejection carries no words because the approval
route has no reason field, and `edit` fires only when an approval happens to
raise, which on `patch_file` over an unprotected path never happens. On a run
touching no protected path there was no intervention point at all.

Appended as a *user* message rather than a tool result, so the model treats it as
instruction rather than as output it can weigh against its own plan.

**D-74 · Wind-down is a different request from abort**

`POST /v1/sessions/{id}/wind-down` sets a flag the loop checks only *between*
turns; `abort` sets one checked *inside* them. A turn can be several minutes long
and can be halfway through a file, so "let it finish and then stop" and "stop
now" are different asks and neither substitutes for the other.

**D-75 · A failing gate stage carries its output; a passing one does not**

`GateReport.as_dict()` dropped `StageResult.content` entirely, so the client could
say *which* stage blocked and never *why* — the compiler errors went to the model
as a tool message and nowhere else. Now `content` (capped at 4000 bytes, with
`truncated`) is carried for stages that failed, plus a top-level `blocked_by`.

Only for failures, deliberately: thirteen clean stages every gate is a lot of
bytes nobody reads.

This does not replace re-running the stage locally. Decision D2 puts the Go
toolchain on the developer's own machine, so a ▶ that re-runs `go build ./...` as
a real Task with a `problemMatcher` gives navigable errors in the Problems panel
for zero tokens and works signed out. The agent says which stage failed; the
editor says why.

**D-76 · `protected` is computed at serialisation, once**

`Mutation.as_dict()` calls `is_protected()`. The alternative was the extension
reimplementing `PROTECTED_GLOBS` in TypeScript — a security-relevant constant
duplicated across the seam with no test binding the copies, and the matcher is
custom rather than `fnmatch`, so a naive port disagrees at exactly the edges that
matter. Computing it where the value is serialised means every surface that shows
a mutation shows the badge, from one implementation.

**D-77 · `usage` carries the absolute budget and the reasoning count every turn**

`budget_used_pct` alone forced clients to divide to recover the denominator, and
two surfaces dividing independently produced two different numbers on screen at
low usage — which is precisely what the interface review found between the
console row and the header meter. One number, sent once.

`reasoning_tokens` was previously emitted only as `reasoning_leaked`, on the
anomaly path where a thinking-off mode was charged for reasoning. Every mode
ships thinking-off, so on a healthy run the key was always absent and a developer
could never see what "plan this carefully" costs.

**D-78 · An approval can be given more time**

`POST /v1/approvals/{id}/extend`. The runtime releases an unanswered approval
after ten minutes and records it as a rejection, with no way to ask for longer —
a WCAG 2.2.1 (Timing Adjustable) failure, because the user can neither turn off
nor extend the limit and the consequence is a decision made on their behalf. The
people most likely to exceed ten minutes are those reviewing a seven-file
changeset with a screen reader, on exactly the cards that matter most.

**D-79 · Scaffold notes are carried structurally, not only in prose**

`payload['notes']` — the steps the scaffolder deliberately leaves for a human,
most often "apply this DDL" — were flattened into the content string as
`  NOTE:` lines and existed nowhere else. A client wanting a follow-ups panel had
to parse a prefix back out of a string it shares with the file listing, which
breaks the first time a note contains a newline. The Go side already had
`Notes []string`; only the bridge was lossy.

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
| The system prompt fits 1,200 tokens and the prefix leaves ≥26k | A prompt growing by accident, one reasonable addition at a time |
| A mode switch does not change the pinned head | Finding S6 — three cold prefills per task |
| Five concurrent reservations of 3,000 against a 5,000 ceiling admit one | Two callers reading the same "used" figure and both acting on it |
| A deliberately non-atomic store *fails* that assertion | A conformance suite that asserts things true of any code at all |
| A refused reservation consumes nothing | A client retrying into a wall draining a budget it never spent |
| An over-reservation is refunded and an under-reservation is charged | Finding S18, in both directions |
| An unreachable quota store refuses rather than allows | The unmetered bypass §15.4 exists to close |
| A state the gateway never issued is refused | A CSRF check that can be satisfied by guessing |
| Reusing a refresh token ends the family, access tokens included | A stolen token staying good for 15 minutes after detection |
| A blocked account loses access at the next refresh | Revocation that needs a separate deprovisioning step |
| The model key never appears in a response, a request body or a log | The one secret whose leak makes every limit decorative |
| A failure before the first byte is a status code; after it, a C2 error event | A 200 that stops mid-stream, indistinguishable from a network fault |
| A stream that drops midway is still billed and ledgered | Abandoning turns becoming the cheapest way to use the service |
| `since_id` and `Last-Event-ID` replay only what was missed | Part B §14's gap: a dropped connection indistinguishable from a dead run |
| A finished session's stream replays and then closes | An endless stream reading to the extension as a run still in progress |
| Abort answers while a run is in flight, and releases its approval | A card nothing will ever answer, and "it stopped but kept moving" |
| An `edit` decision's corrected arguments are the ones that run | An approval that silently discards the correction |
| An approval timeout refuses | A write that happened while nobody was looking |
| Revert deletes what a session created and restores what it changed | A revert that reports success and changes nothing |
| Revert outside a git repository is blocked, not attempted | Deleting a developer's file because git was absent |
| A running session cannot be reverted or deleted | A tree matching neither the before nor the after |
| `dakcoderd` refuses to start holding a model key, or without a gateway | The unmetered bypass, at the one place a laptop could open it |

---

## 5. Changelog

### 2026-08-26 — The greeting that burned seventeen turns

A pilot typed a greeting and watched the full engineering ladder run against a
workspace nothing had touched: planner to coder to verifier to debugger, two
coder attempts and three debug cycles, ending `Unverified` at `go_build`. Six
faults, each independently sufficient to ruin a first run.

**Not every message is a task.** `planner.md` opened by ordering `repo_map`
before the model had decided anything, and a Planner reply carrying no numbered
steps was handed to the Coder as though it were a plan. `_advance` now ends the
run when `_count_steps` is zero — the loop already computed that number and
shipped it in the `PLAN` event, then discarded it. One turn instead of
seventeen.

**A gate that cannot pass is worse than no gate.** Every Go stage runs `./...`,
a module-relative pattern, from `workspace.root`. Open a checkout root rather
than a service and the toolchain refuses the pattern itself, which `_result`
turned into a failure and the loop read as a defect in code it never compiled.
Every Go stage is now guarded on a root `go.mod` — every stage, not just
`go_build`, because guarding one only promotes the next to first blocker.
Deliberately *not* a downward scan for modules: building or tidying other
people's services on a task that never touched them is a worse bug than the one
being fixed.

**A skipped gate is not a clean gate.** With stages skipping, `ok` covers two
different claims. `_done_summary` now reads the report rather than the outcome,
because "3 file(s) changed and the gate is clean" for a gate that compiled
nothing is the same overclaim D-42 refuses from the model, pointed at the
developer instead.

**The loop could not see a model repeating itself.** `_stuck` fingerprints tool
arguments, so a turn that calls no tool was invisible to it — and every rung of
that ladder was such a turn. `_repeating` mirrors it for prose, on its own
ledger because `_switch` clears `recent` on each mode change and this run
changed mode on every advancing turn. It sits *after* the tool-call branch: a
model that prefixes three different edits with one stock sentence is working,
not stuck.

**The sidecar never reached the daemon.** The extension resolves
`bin/gotools-win32-x64.exe`, checksum-verifies it and spawns it; `childEnv()`
told the Python child nothing, so `_find_binary()` searched PATH for an
unsuffixed name the build never emits and then walked `parents[5]` into
site-packages. The extension now shares the *resolution* — not the name —
through `GOTOOLS_PATH`, so the child honours `dakcoder.gotoolsPath` and never
sees a binary the manifest refused. `resolveGotools` also restores the execute
bit, which a `.vsix` written on Windows does not carry.

**`_raw` was a rendering artefact that cost a bug report its evidence.** The
router did refuse the malformed call and told the model why; only the display
payload was misnamed. It is `_malformed_arguments` now.

`scripts/offline-smoke.py` grew the check that would have caught the sidecar
fault: every previous check stopped at an HTTP route, so nothing ever crossed
the bridge. Its env block is a hand-maintained mirror of `childEnv` and drifted
the moment `childEnv` learned something new — mirrors do that, and the check is
what makes the drift visible.

### 2026-08-26 — Two errors from the first pilot session, one root cause each

**`quota refresh failed: Cannot read properties of undefined (reading 'replace')`.**
The gateway's `Snapshot.as_dict()` sent its counters flat (`used`/`limits`
keyed by series, `tightest: {limit, used_pct}`); every extension surface read
the nested C4 view (`window`/`week`/`hour`, `tightest: {name, used, cap, pct}`)
and the tooltip called `escape(tightest.name)` on `undefined`. Fixed on both
sides, additively: the gateway now emits both envelopes (and `expires_in`,
derived server-side so a laptop's clock never enters into it), and the
extension routes every `/v1/quota` and preflight body through
`normaliseQuota()` in `protocol.ts`, which accepts either envelope and never
yields a `tightest` without a name. A gateway that has not been redeployed is
therefore still rendered correctly.

**`'str' object has no attribute 'get'` on every run.** Not a new bug: the
source had already fixed both halves — `_error_for` assuming the OpenAI error
envelope where the gateway sends `error: "<kind>"`, and a local runtime
resolving the model *name* instead of passing the *role* (D-59), which is what
made the gateway refuse in the first place — but the vendored
`dakcoder_shared` wheel in `extension/runtime/` predated those commits by
seven hours, and `runtime.ts` installs from the wheels, not the source. The
wheels are rebuilt from the current tree. `wheelHash()` keys on content, so an
installed runtime reinstalls itself on the next activation. Lesson recorded in
§4.3's spirit: a wheel is a build artefact and goes stale like one; rebuild it
whenever `apps/shared` or `apps/agent` change before packaging a `.vsix`.

### 2026-08-26 — Part B, completion: the wheels and the two real hosts

The two gaps left open when the extension first went green, closed — and each
closed by a test that found a bug the moment it ran for real.

**The vendored wheel closure.** 20 wheels, 3.6 MB, installed with
`--no-index --find-links`. `scripts/offline-smoke.py` builds a clean venv from
them, spawns `dakcoderd` with the exact environment `runtime.ts` constructs,
reads the port off stdout and drives 14 checks over HTTP. Nothing is stubbed.

It failed on its first run: `dakcoder_shared.llm` imports `httpx` at module
scope and **no `pyproject.toml` declared it**, so the offline install produced a
runtime that could not import itself. Every developer machine had httpx sitting
in a shared environment, which is exactly why nothing caught it — and behind the
proxy the vendored wheels are the only source, so the first pilot developer
would have hit it on day one. That is the failure mode §4.3 exists to remove,
arriving through a different door.

It also surfaced a first-run bug in the extension. The runtime refuses to start
without a JWT, correctly — every model call goes through the gateway as the
developer and there is no local key. But `ready()` spawned first and read the
refusal off stderr, so the first task after install died as *"the runtime exited
with 2"*. Sign-in is now asked for properly, before the spawn.

**Integration tests in a real VS Code** (`@vscode/test-electron`, 9 tests).
They assert what a regex over source cannot: that the host loads the bundle,
that every declared command resolves in the *running* registry, that the
`dakcoder-proposed` scheme has a provider, that the forbidden settings do not
exist, and that the approval timeout still defaults to waiting indefinitely.

The first run found **two views sharing the id `dakcoder.chat`** — VS Code
refuses to register the second, so the extension loaded with a panel missing.
The merge that assembles `package.json` from eight modules deduplicated arrays
on `command`, and views key on `id`. The merger is fixed; the manifest is
checked.

Getting the suite to run at all took three findings worth recording, because
each presented as something else entirely:

- `node:test`'s `run()` executes each file in a **child process**, which inside
  the extension host has no `vscode` module to import. It hung for seven minutes
  and was killed — with no indication the tests had never started. Replaced with
  a ten-line in-process harness rather than adding mocha to an extension whose
  discipline is zero dependencies.
- `ELECTRON_RUN_AS_NODE` was set in the environment and is inherited, so
  `Code.exe` ran as a bare Node interpreter, rejected every one of its own flags
  as *"bad option"*, and tried to `require` the workspace path as a module. The
  runner now clears it.
- A positional folder path in `launchArgs` is consumed as the test entry point by
  this launcher, so the suite opens no folder — which is also the path a
  developer takes when they install the extension before opening a repo.

**Two CI jobs added.** `offline-smoke` builds the wheels and proves the
network-free install; `extension` now runs the integration suite under `xvfb`.

#### Verification

```
python      467 passed, 33 skipped
go          12 packages, vet clean, gofmt clean
extension   32 unit + 9 integration (real VS Code), 0 type errors
            52/52 commands resolve, no credentials in 15 packaged files
offline     20 wheels, network-free install, 14 spawn checks
vsix        3.72 MB, 35 files
```

### 2026-08-26 — Part B: the extension

Ten TypeScript modules, a webview, and the manifest. Zero runtime dependencies;
esbuild bundles to 190 KB against a 400 KB budget.

| Module | What it owns |
|---|---|
| `protocol.ts` | The wire contract. Additive-only: every type is a *lower bound*. |
| `client.ts` | Typed REST for both servers, plus a resumable SSE parser. |
| `runtime.ts` | Python discovery, offline wheel install, credential-stripped spawn. |
| `session-state.ts` | `RunState` — the single derivation of every number a surface shows. |
| `statusbar.ts` | The two ambient items, polled only while a task runs. |
| `auth.ts` | GitLab PKCE as a real `AuthenticationProvider`, with a loopback fallback. |
| `chat.ts` + `media/chat/*` | The panel. Real files, not a template string. |
| `approvals.ts` | Native diff, editor-title actions, the multi-file changeset. |
| `trees.ts` | Sessions, quota, context inspector. |
| `doctor.ts` | The Go toolchain matrix, every failure with a remedy. |
| `diagnostics.ts` | `gotools lint --format json` → real `Diagnostic`s, plus seven code actions. |
| `wizard.ts` | The scaffold wizard and the migration plan viewer. |

**One state, many surfaces.** `RunState` consumes the event stream once and
every surface reads its getters. Nothing else parses SSE, calls `parsePlan`, or
divides tokens by a budget — which is what stopped the two-context-readings bug
the interface review found between the console row and the header meter.

**Two de-duplication rules live in the renderer that owns the transcript.** The
server emits `assistant` and then `plan` from the same text on the planner's
final turn, and `error` followed by `finish` carrying the identical string.
Without suppression the first plan prints twice and every failure states itself
twice. A row is held for one event (or 400 ms) so its twin can supersede it.

**The webview's live region is one announcer, not the container.** `aria-live`
on the transcript announces every descendant insertion — on a forty-turn run
that narrates every code block and all thirty gate cells, which is exactly the
flood the accessibility contract exists to prevent. So the transcript is
`role="log"` with no `aria-live`, and one visually-hidden `role="status"`
sibling receives a single composed sentence per event.

**Two gates that hold an invariant nobody would otherwise check.**
`check-no-credentials.mjs` fails the build on a model key anywhere in the
packaged extension — verified by planting one, which failed with exit 1.
`check-commands.mjs` fails on a palette command with no registration, which
typechecks, bundles and packages perfectly and then throws for the first person
who finds the feature. It caught two.

#### The bug the SSE tests found twenty minutes after the parser was written

Under CRLF the frame terminator is `\r\n\r\n`, which contains no `\n\n`. A
parser searching for `\n\n` emits **nothing at all** against a CRLF server — not
a corruption, a total silence. A lone trailing CR is now held back across chunk
boundaries rather than normalised per chunk, because a chunk boundary can fall
between the CR and its LF; both the whole-frame and byte-shredded cases are
tested.

#### Verification

```
python      467 passed, 33 skipped
go          12 packages, vet clean, gofmt clean
extension   32 tests, 0 type errors, 52/52 commands registered
            190 KB bundle, no credentials in 15 packaged files
```

The 33 skips are the Redis and Postgres conformance suites, which run in CI
against real servers and are labelled as never having spoken to one locally.

### 2026-08-26 — Part B, phase 0: the blockers Part A had to clear first

Designing the interface against the *running code* rather than the plan found
fifteen places where the client was asked to display something the server does
not send. Two were defects (D-71, D-72) and both were load-bearing: the approval
card could not be actioned from the event that raises it, and the first
compaction of any long run killed it — under 461 green tests.

- **`tool_pending` carries an id, registered before it is announced** (D-71).
- **Compaction survives** (D-72), with a regression test proven to fail against
  the original code by restoring it, which reproduced
  `AttributeError: 'list' object has no attribute 'markdown'` exactly.
- **Steering** (D-73) and **wind-down** (D-74): the product previously had no way
  to disagree with a run in progress except Stop, which discards its context.
- **`POST /v1/sessions/{id}/resume`** runs a session again on the same
  transcript, sharing `_spawn` with `start` so the two paths cannot drift.
- **`GET /v1/sessions/{id}/context`** — `ContextManager.inspect()` already
  returned exactly what the inspector needs, and no route exposed it.
- **Failing gate stages carry their output** (D-75), plus a top-level
  `blocked_by`.
- **`protected` on every mutation** (D-76); **`ms` on every tool result**;
  **`budget` and `reasoning_tokens` on every usage** (D-77).
- **Approvals can be extended** (D-78). **Scaffold notes are structured** (D-79).

Extension foundation laid: `protocol.ts` (the wire contract, additive-only),
`client.ts` (typed REST plus a resumable SSE parser), `runtime.ts` (offline wheel
install, credential-stripped spawn, port read from stdout).

The SSE parser's own tests found a real defect in it within twenty minutes of it
being written: under CRLF the frame terminator is `\r\n\r\n`, which contains no
`\n\n` — so a parser searching for `\n\n` emits **nothing at all** against a CRLF
server. A lone trailing CR is now held back across chunk boundaries rather than
normalised per chunk, and both the whole-frame and byte-shredded CRLF cases are
tested.

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

### 2026-08-26 — Phase 3: sessions, the loopback, and `dakcoderd`

Part A is finished. The extension has a runtime to spawn and a contract to bind
against.

- `agent/session.py`: the event log with monotonic ids, resumption, abort, and
  a git-based revert (D-64, D-68).
- `agent/loopback.py`: the HTTP+SSE surface — tasks, sessions, events,
  approvals, abort, revert (D-65, D-66, D-69).
- `agent/serve.py`: `dakcoderd`, spawnable exactly as Part B §4 describes
  (D-70).
- `agent/loop.py`: cancellation at two checkpoints (D-66).

**Bugs this phase produced**, each caught by a test rather than by review:

- The `edit` approval decision re-dispatched the model's original arguments, so
  a correction was silently discarded while the approval went through (D-67).
- `call_soon_threadsafe` was unguarded, so a shutdown mid-run killed the worker
  thread and left the session "running" for ever (D-65).
- Revert inferred "not a git repository" from the wrong exit code, which would
  have **deleted** files it could not restore (D-68).
- `uvicorn.run(fd=...)` binds silently and never serves on Windows (D-70).

### 2026-08-25 — Phase 2: prompts and the gateway

The gateway is built: identity, quota, the ledger and the model proxy, with the
HTTP surface that carries contracts C1, C3 and C4.

- `agent/prompts/`: one system prompt, five overlays, budget asserted (D-52).
- `gateway/auth/`: PKCE with a server-issued state, our own JWT, roles from
  GitLab groups, refresh rotation with family revocation (D-57, D-58).
- `gateway/quota/`: rolling windows over an atomic store, reserve-and-reconcile,
  priority lanes, idempotency (D-54, D-55, D-63).
- `gateway/ledger.py`: append-only usage events, reasoning tokens in their own
  column so §4.4's choices stay measurable.
- `gateway/proxy.py`: `/v1/llm/*` with SSE passthrough and a usage tee (D-59,
  D-61, D-62).
- `gateway/app.py`: the routes, and one place where each domain error becomes a
  status code (D-56, D-60).

**Bugs this phase produced and the tests that caught them**, all before any of
it ran against a server:

- `refund()` with a non-positive early return made under-reservations free.
- The idempotency claim stored `None`, so a replay could never be recognised.
- `Reservation.settled` was set on a frozen dataclass by a no-op expression, so
  double reconciliation was never detected.
- `StreamingResponse` was returned before the generator's first chunk, so a
  quota refusal arrived as a 200 that stopped mid-stream.
- `_guarded` turned a 409 idempotency conflict into a 503, telling the caller to
  retry the one request that must not be retried.

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

---

## 7. Handover to Part B

Everything the extension binds against, and where it lives.

### What to spawn

```
dakcoderd --workspace <repo> [--port 0] [--no-prewarm]

env: DAKCODER_GATEWAY_URL    the gateway base, e.g. https://aiops.cept.gov.in/coder/backend
     DAKCODER_GATEWAY_TOKEN  a random per-session loopback token you generate
     DAKCODER_JWT            the dakcoder JWT from sign-in
```

It prints one line of JSON on stdout before serving — `{"port", "pid",
"version"}` — and refuses to start if any model credential is in its
environment, or if the gateway URL or loopback token is missing. Strip
`OPENAI_API_KEY` and friends before spawn (Part B §4.6); the runtime will
otherwise stop with a message naming the variable.

### The loopback (`http://127.0.0.1:<port>`, `Authorization: Bearer <token>`)

| Route | Purpose |
|---|---|
| `GET /v1/health` | **No token.** `api_version`, `version`, prewarm result. Poll this after spawn. |
| `GET /v1/tools` | Contract C1, 29 tools, same shape as `docs/tool-catalog.json` |
| `POST /v1/tasks` | `{task, mode?, acceptance?}` → the session |
| `GET /v1/sessions/{id}/events` | SSE. `?since_id=` or `Last-Event-ID:` to resume |
| `GET /v1/sessions` | the tree; `?status=` filters |
| `GET /v1/sessions/{id}` | detail; `?transcript=true` for the full event log |
| `DELETE /v1/sessions/{id}` | 409 while running |
| `POST /v1/sessions/{id}/abort` | honoured mid-turn and before each tool call |
| `GET  /v1/sessions/{id}/revert` | the plan: `{restore, delete, blocked}` |
| `POST /v1/sessions/{id}/revert` | apply it; 409 while running |
| `GET  /v1/approvals` | what is waiting |
| `POST /v1/approvals/{id}` | `{decision: accept\|reject\|edit, arguments?}`; 410 once gone |

`api_version` is `1.0`. Pin it: Part B §15 is right that silent version skew
across this seam is the failure that costs the most support time.

### The gateway (`https://<gateway>`, `Authorization: Bearer <jwt>`)

| Route | Purpose |
|---|---|
| `POST /v1/auth/start` | `{redirect_uri, code_challenge}` → `{state, authorize_url}` |
| `POST /v1/auth/exchange` | `{code, code_verifier, state, redirect_uri}` → session + quota |
| `POST /v1/auth/refresh` | rotates; reuse ends the family |
| `GET  /v1/quota` | contract C4's snapshot, including `tightest` for the status bar |
| `POST /v1/quota/preflight` | would a run of this size be admitted? |
| `POST /v1/runs` | opens a session window |
| `GET  /v1/health` | **no token**; capabilities and the limits in force |
| `POST /v1/llm/*` | the model proxy; the runtime uses this, not the extension |

**`/v1/auth/start` is new** relative to Part A §15.2's diagram, and it is not
optional — see D-57. The extension must call it and use the `state` it returns,
because a state the gateway did not issue is refused.

### Event types on the stream (contract C2)

`turn_start`, `assistant`, `assistant_delta`, `tool_call`, `tool_pending`,
`tool_result`, `plan`, `gate`, `usage`, `quota`, `finish`, `error`,
`heartbeat`, `end`.

Additive only: **ignore unknown types and unknown fields.** That rule is what
lets the `.vsix` and the wheel version independently, which they will, because
one ships through a marketplace and the other through GitLab.

`assistant_delta` is coalesced server-side and never persisted (fix S11); do not
build a transcript from it. `gate` carries `kind: inner|full|compaction`.
`tool_pending` is an approval — its `id` is what `POST /v1/approvals/{id}` takes.

### What Part B still owns

- The extension itself, all of Part B.
- `gopls` integration. `go_symbols` and `go_diagnostics` are specified, in the
  catalogue, and marked `unavailable` with a substitute named — see §6.3.
- The `swagger_check` boot-and-diff half: it needs a database and a free port,
  and a check that fails when Postgres is down gets disabled within a week.
