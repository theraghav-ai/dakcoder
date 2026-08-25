# Part A — The Agent Backend

> Companion to **[plan.md](plan.md)** (shared context, locked decisions D1–D7, corrections, contracts C1–C5) and **[plan-vscode-extension.md](plan-vscode-extension.md)** (Part B).
> Read plan.md §1–§8 first. This document does not repeat it.

---

## 1. Scope and topology

Part A owns everything from the HTTP boundary inwards: the gateway, the agent loop, the context manager, the tool router, the Go analysis sidecar, the rules engine, the scaffolders, the knowledge base, identity, quota, and observability.

Under **D2 (local-first)**, the *same* server binary runs in two places. This is `postgen`'s proven shape and it is not a compromise — it is the whole reason DOP source can stay on the laptop.

```
┌── LOCAL MODE (default) ──────────────────────────────────────────────────────┐
│  DEVELOPER'S MACHINE                                                          │
│  ┌─────────────────────┐        ┌───────────────────────────────────────────┐ │
│  │ VS Code extension   │◀─SSE──▶│ dakcoderd  (loopback, private token)      │ │
│  │  (Part B)           │  HTTP  │  gateway → context mgr → agent loop       │ │
│  └─────────────────────┘        │        │                                   │ │
│                                  │        ├─▶ gotools   (Go binary, MCP stdio)│ │
│                                  │        ├─▶ gopls mcp (Go binary, MCP stdio)│ │
│                                  │        └─▶ local FS + go toolchain + git   │ │
│                                  └──────────────────┬────────────────────────┘ │
└─────────────────────────────────────────────────────┼──────────────────────────┘
   only model traffic leaves the machine ─────────────┼───────────┐
                                                       ▼           │
┌── CENTRAL SERVICES (aiops.cept.gov.in/coder/backend) ────────────┼───────────┐
│  /v1/auth/*  ·  /v1/quota  ·  usage ledger  ·  OTel  ·  KB index ◀┘           │
│  /v1/llm/*   ← model proxy: holds the LiteLLM key, meters, forwards  (§15.4)  │
└───────────────────────────────┬──────────────────────────────────────────────┘
                                ▼
        LiteLLM proxy  https://ai.cept.gov.in/v1   →   vLLM 0.23.0 · Qwen3.8-27B

┌── SERVER MODE (opt-in, Phase 3) ─────────────────────────────────────────────┐
│  Same dakcoderd, running on the aiops host behind nginx /coder/backend/,     │
│  against a per-user ephemeral workspace cloned from GitLab, with a warm      │
│  GOMODCACHE and a sandboxed toolchain. Extension points at a different URL.  │
└──────────────────────────────────────────────────────────────────────────────┘
```

**Local mode still needs the server.** Auth, quota, the usage ledger, the shared knowledge-base index, telemetry, **and model access** are always central — otherwise there is no identity, no quota, and no audit. Only *file access and command execution* are local. That split is the honest version of "local-first", and it is what §15–§18 are built around.

The model proxy (`/v1/llm/*`) is not ceremony: the LiteLLM key is a single shared secret, so if the local runtime held it, every laptop could spend the shared GPU budget with no attribution and no ceiling. §15.4 specifies the proxy; plan.md §4.5 explains why it is mandatory rather than preferable.

---

## 2. Why this shape (D1 in detail)

`postgen` is ~10,100 lines of Python. Of that, roughly 6,500 lines are entirely language-agnostic and already survived production: the FastAPI gateway with SSE, the SQLAlchemy session store and append-only audit log, the approval gate with diff previews and an `edit` decision, the prompt-injection guard, the sandbox abstraction with graceful degradation, resume/replay from the audit log, the Loki sink with redaction, OTel wiring, idempotency keys, and the whole abort/revert/heartbeat/disconnect-grace layer that only exists because real users hit real corporate-proxy bugs.

Rebuilding that in Go would be several engineer-months to arrive at the same place, minus the bug fixes.

What genuinely cannot be Python is the analysis. Layer-boundary checks, handler-signature checks, and FX-registration checks are *type* questions, not text questions. `repo` must not import `handler`; a handler method's second parameter must be a struct whose type is declared in package `handler`; `dblib.Psql` must be the receiver of the builder chain. Regex gets these ~80% right, which for a rule the developer cannot override is not good enough. `go/parser` + `go/types` via `golang.org/x/tools/go/packages` gets them right.

So: **Python spine, Go analysis, MCP/stdio between them.** The seam is a process boundary with a JSON-RPC contract, which also means `gotools` is independently testable, independently versionable, and usable from any other MCP client.

```
apps/gateway  (Python)  auth · quota · SSE · sessions · audit · approval · injection guard
apps/agent    (Python)  context manager · agent loop · mode overlays · tool router · playbooks
  └── MCP/stdio ─▶ gotools    (Go)   rules_lint · resource_scaffold · project_scaffold · fx_wire · repo_map
  └── MCP/stdio ─▶ gopls mcp  (Go)   go_diagnostics · go_search · go_references · go_package_api · go_vulncheck
```

---

## 3. The single biggest lever: this agent must be fast

The programme owner's ask was to understand why the frontend agent is slow and **not repeat it here**. `postgen` itself is out of scope — §5 is a design rationale, not a punch list for someone else's codebase. But it is the rationale, and it is worth stating plainly because everything downstream — tool design, prompt design, rule design — follows from the token budget.

The short version: **`postgen` has no context management inside a run.** Its message list is append-only for up to 40 turns, tool results enter it untruncated, `repo_map` alone contributes 20–30k tokens permanently, and there is a ~5.7k-token fixed overhead re-sent every turn. Nothing summarises, nothing evicts, nothing measures. Four separate implementation choices each cost seconds per call, and the KV prefix is discarded three times per task by design.

None of that is a criticism of a system that shipped and works. It is what happens when context is nobody's component. Here it is a component (§6), owned, budgeted, and regression-tested (§20.5).

---

## 4. LLM layer

### 4.1 Model access

`dakcoder-go` talks to a **LiteLLM proxy**, not to vLLM directly. Endpoint, key policy, and the full verified capability matrix are in [plan.md §4](plan.md); this section covers what the agent does with them.

```yaml
base_url: ${DAKCODER_MODEL_BASE_URL}   # https://ai.cept.gov.in/v1
api_key:  ${DAKCODER_MODEL_API_KEY}    # server-only secret — never on a laptop (§15.4)
```

### 4.2 One model, three configurable roles (D7)

Every role points at `Qwen3.8-27B` today. The *seam* exists from day one because it is nearly free to build and genuinely expensive to retrofit — introducing a second tier later must be an env change, never a refactor.

```bash
DAKCODER_MODEL_CODER=Qwen3.8-27B    # planning, coding, debugging, all tool calls
DAKCODER_MODEL_FAST=Qwen3.8-27B     # compaction, elision summaries, titles, routing, injection triage
DAKCODER_MODEL_EMBED=Qwen3.8-27B    # unused while search_docs runs BM25-only (§14.3)

DAKCODER_TEMPERATURE_CODER=0.1      # lower than the frontend's 0.2 — Go boilerplate rewards determinism
DAKCODER_TEMPERATURE_FAST=0.0       # summarisation should be reproducible
```

Every call site resolves its model through `models.for_role("fast")` — never a bare `cfg.model`. A single hard-coded model name anywhere is the bug that makes the tiering unusable later, so it is worth a lint rule in our own CI.

**When a second tier is wanted, the switch is already provisioned.** Verified live on the same endpoint and the same key: `Phi-4-mini-instruct` produces clean terse summaries (tested), and `qwen3-embedding` returns 1024-dimension vectors (tested). So `DAKCODER_MODEL_FAST=Phi-4-mini-instruct` is a one-line change with no new infrastructure, no new key, and no new network path. Switch it on when telemetry shows compaction and summarisation are a meaningful share of tokens — §18 measures exactly that, split by role.

### 4.3 What we do not control

We do not own the vLLM launch command. Three consequences the design has to absorb:

- **Prefix caching is unverifiable from here.** `prompt_tokens_details.cached_tokens` is absent from responses (plan.md §4.2), so the single largest latency lever in an agent workload is one we cannot currently measure. In agent traffic 85–95% of each request's prompt was already processed in a prior turn; published results on multi-turn tool-calling workloads show roughly 2× lower median per-turn latency on short workflows and ~4× on long coding workflows once that prefix is reused. **Everything in §6 is built to make the prefix reusable regardless** — stable prefixes and small prompts are correct whether or not APC is on, and they are what will let us prove the difference the moment the flag is confirmed. This is plan.md §9 Q1.
- **LiteLLM's own response cache must be off for our traffic.** It is an exact-match cache. Serving a cached completion for a near-identical agent context would be a correctness bug wearing an optimisation's clothes.
- **`drop_params` is off**, so unknown parameters return a 400 rather than being silently dropped (`reasoning_effort` was rejected outright). This is the right behaviour, and it makes §4.5's startup probe load-bearing rather than decorative.

`--max-model-len` is **262,144**, but that is a ceiling and not a target: the agent caps its own prompts at 32k (§6.1). A 262k prompt would be slow, would consume KV blocks other tenants need, and — per the context-rot literature — would be *less* accurate. The large window is for the rare long-file case only.

### 4.4 Reasoning control: a per-mode decision

`Qwen3.8-27B` is a **reasoning model**. Left at its default it returns `reasoning_content` with **`content: null`**, and a `max_tokens` too small to finish reasoning burns the entire turn for nothing. This is the most consequential new fact in this revision and the v1 plan had no concept of it.

Reasoning is switchable per request via `chat_template_kwargs: {"enable_thinking": false}` (verified). **The spike (§4.6) found that it should be off almost everywhere**, which reverses this plan's earlier guidance.

The evidence (full spike results in §4.6). Identical prompt, identical temperature, only `max_tokens` varied:

| `enable_thinking` | `max_tokens` | Latency | Reasoning emitted | Answer |
|---|---|---|---|---|
| off | 1,000 | **2.0 s** | — | 517 chars |
| on | 1,000 | 4.5 s | 1,247 chars | 328 chars |
| on | 4,000 | **31.4 s** | 9,948 chars | 333 chars |
| on | 16,000 | 15.4 s | 4,828 chars | 328 chars |

Two things to read off that table:

1. **Reasoning expands to fill the available budget, non-deterministically.** The same prompt produced 1,247, then 9,948, then 4,828 characters of reasoning. It is not a stable cost you can budget for.
2. **The answer did not improve.** All three thinking-on runs returned the same ~330-character JSON that thinking-off produced in 2 seconds. On this task, reasoning bought a **15× latency penalty for nothing.**

It also failed outright twice in the spike: a spec-extraction turn burned 3,000 tokens and returned `content: null`, and a migration turn consumed its entire 8,000-token budget on reasoning plus a violations list and never emitted the code it was asked for.

**So: thinking is off by default, in every mode.**

| Mode | `enable_thinking` | `max_tokens` | Rationale |
|---|---|---|---|
| **Planner** | off | 4096 | Reversed. The spike shows no quality gain on structured output, and the plan *is* structured output. |
| **Scaffolder** | off | 2048 | Emits a JSON spec. |
| **Coder** | off | 4096 | Mechanical edits and tool dispatch — the bulk of all turns. |
| **Verifier** | off | 2048 | Runs commands, reads output, reports. |
| **Debugger** | off *(experiment: on)* | 6144 | The one place reasoning might genuinely pay, because ranking hypotheses is the rare task where the *reasoning text itself* is the deliverable. Treat it as an A/B in Phase 2 with the eval suite as the judge — **not** as a default. |
| **fast role** (any) | off | 512–1024 | Summarisation and classification. |

Four hard rules:

1. **Never enable thinking on a turn that must produce a tool call or structured output.** Both spike failures were exactly that.
2. **Any thinking-on call gets `max_tokens ≥ 6144`** — the budget must hold a runaway reasoning block *plus* the answer, and the spike shows reasoning alone can exceed 2,500 tokens on a trivial prompt.
3. **Treat `content: null` with `finish_reason: "length"` as a typed error**, not an empty response. Retry once with thinking **off** — not with a bigger budget, since a bigger budget is what produced the 31-second run. Log it and count it (§18).
4. **Meter `reasoning_tokens` per mode** and alert on any non-zero value in a mode configured for thinking-off — that would mean the parameter is not reaching the model. Note the field is populated on **streaming** responses but came back `null` on non-streaming ones, so the metering path must read it from the stream's final usage chunk.

One incidental benefit of thinking-off: the thinking chat template adds ~37 prompt tokens of its own, so those turns are marginally cheaper on input as well.

### 4.5 Startup capability probe

Because we sit behind someone else's proxy in front of someone else's vLLM, the endpoint's behaviour can change without notice. A ~100-line probe runs at server startup (and in CI) and asserts every row of plan.md §4.2: the model answers, tool calling returns `finish_reason: "tool_calls"`, `enable_thinking: false` yields non-null `content`, `stream_options.include_usage` produces a final usage chunk, and `prompt_tokens_details` is present or absent (recorded either way, never fatal).

Results land in `/v1/health` and in a startup log line. A regression here — LiteLLM upgraded, the model swapped, `drop_params` flipped — becomes an immediate, legible failure instead of a week of confusing agent behaviour. Do not validate against `/v1/models`: it does not list `Qwen3.8-27B` even though the model serves correctly.

### 4.6 Spike results — can the model hold the contract?

Run 25 Aug 2026 against the live endpoint, before any implementation. The question was whether `Qwen3.8-27B` can actually produce `n-api-template`-compliant Go, because if it cannot, nothing else in this plan matters. Artifacts and the harness are in the session scratchpad; the checks are reproducible.

| Test | What was asked | Result |
|---|---|---|
| **A — spec extraction** | NL instruction → structured resource spec (the Scaffolder happy path) | **Pass, with a caveat.** Valid JSON, correct table/route/snake_case/`oneof` validate/list filter, in **2.0 s**. But it emitted `"type":"decimal.Decimal"` — **a dependency that is not in the allow-list** — and `PpoNumber` instead of `PPONumber`. |
| **B — code generation** | Reference `user` resource (~3.8k tokens of context) → a complete `Pension` resource: domain, DDL, repository, request DTOs, response DTOs, handler | **Pass.** 6 files, 3,493 output tokens, **30 s**. `go build ./...` **clean**. `go vet ./...` **clean**. `rules_lint` **0 violations**. It used `dblib.Psql`, `QueryTimeoutLow` for single-row and `QueryTimeoutMed` for the list, `pgx.RowToStructByName`, the DTO signature, `port.*Success` constants, `json:",inline"` envelopes, and correct import aliases — unprompted. |
| **C — fx wiring** | Register a new repo and handler in `bootstrap/bootstrapper.go` | **Pass, byte-correct**, in **2.2 s** — repo as plain `fx.Provide`, handler wrapped in `fx.Annotate` + `fx.As(new(serverHandler.Handler))` + `fx.ResultTags(serverHandler.ServerControllersGroupTag)`. This is the #1 failure class in §13.2 and the model got it exactly right. |
| **D — legacy migration** | A `pao`-style gin handler → list violations, then migrate | **Split.** Violation detection was **excellent** — 15 findings, all correct, including `binding:"required"` vs `validate:"required"` and that `GetObjection` should be `GetObjectionByID` to match its `:id` route. But the run hit its 8,000-token ceiling on reasoning and **never emitted the migrated code** (see §4.4). |

Four decisions come out of this:

1. **The scaffolder must validate the spec, not trust it.** Test A hallucinating `decimal.Decimal` is precisely why §11.1 has the LLM produce the *spec* and `text/template` produce the *code*. Validate the spec against a JSON Schema **and** the dependency allow-list before a single file is written, and normalise Go initialisms (`PPO`, `ID`, `URL`, `HOA`) on the way through.
2. **A type-consistency rule is needed.** The generated `CreatePensionRequest.SanctionDate` was `string` while `domain.Pension.SanctionDate` was `time.Time`. It compiles and Postgres accepts it, but it is wrong. Add `request-domain-type-match` to §9.2 — it needs `go/types`, and it is a good early argument for the `packages.Load` path rather than syntax-only parsing.
3. **Give the model our rule vocabulary.** Test D invented its own ids (`NO_GIN_CONTEXT`, `BASE_HANDLER`) instead of ours (`legacy-gin-handler`, `handler-base`). Supply the rule table in the migration prompt so its output joins with `rules_lint` output instead of needing translation.
4. **Migration must be split into two turns**, not one. Detect-then-emit in a single call is what blew the budget. One turn to produce the violation list, a second to emit each migrated file — which also matches §12.2's per-unit isolation.

**Overall: the model can hold the contract.** The largest product risk in the programme is retired. What remains is engineering, and the failure modes the spike found are all mechanically preventable — schema validation, allow-list enforcement, turn splitting, and thinking-off.

### 4.7 Client design — and where the key lives

**The shared model key exists in exactly one place: the gateway's secret store.** Nothing else — not the local runtime, not the `.vsix`, not a developer's `.env`, not a config file — ever holds a model credential. This is a hard invariant, so the client is built in two variants and the *only* difference between them is what they authenticate to.

```python
# apps/agent/llm.py — one factory, two targets, selected by deployment mode.

def make_client(cfg) -> OpenAI:
    if cfg.mode == "gateway":
        # Runs INSIDE the gateway. The only place the LiteLLM key is ever read.
        return _client(base_url=cfg.model_base_url,        # https://ai.cept.gov.in/v1
                       api_key=secrets.require("DAKCODER_MODEL_API_KEY"))

    # Runs on the DEVELOPER'S MACHINE. Holds no model credential at all — it
    # authenticates as the developer and lets the gateway attach the real key.
    assert cfg.model_api_key is None, "local runtime must never hold a model key"
    return _client(base_url=f"{cfg.gateway_url}/v1/llm",   # §15.4 proxy
                   api_key=auth.current_jwt())            # refreshed, 15-min TTL

def _client(*, base_url, api_key) -> OpenAI:
    return OpenAI(
        base_url=base_url, api_key=api_key,
        timeout=httpx.Timeout(connect=5.0, read=cfg.request_timeout, write=30.0, pool=5.0),
        max_retries=0,                   # we own retry policy (backoff + jitter)
        http_client=httpx.Client(
            http2=False,                 # see note
            limits=httpx.Limits(max_keepalive_connections=32, keepalive_expiry=300),
            trust_env=False,             # never inherit the corporate proxy
        ),
    )
```

Because the proxy is OpenAI-compatible, **the agent loop is identical in both modes** — same request shape, same streaming, same tool calls. Only the base URL and the bearer token differ. That is what makes this invariant cheap enough to hold permanently rather than a thing we trade away for latency later.

Three enforcement points, because an invariant nobody checks is a comment:

1. `make_client` asserts it, as above — a local runtime that somehow has a key fails at startup rather than quietly using it.
2. The local runtime **never reads `DAKCODER_MODEL_API_KEY`**, and Part B's spawn env explicitly deletes it before launching the child, the way the frontend agent already strips `POSTGEN_REDIS_URL` and `POSTGEN_WORKSPACE_ROOT`. A stale variable in a developer's shell must not become a bypass.
3. A CI check greps the built `.vsix` and the packaged wheel for anything matching `sk-[A-Za-z0-9]{16,}`. Cheap, and it is the failure that would matter most.

Because the local runtime holds no key, **§4.5's capability probe runs on the gateway only.** The local runtime reads the result from `GET /v1/health` at startup and refuses to run modes whose required capability failed — so a `enable_thinking` regression surfaces as a clear refusal on the laptop, not as a Planner that silently returns nothing.

Per-request defaults, all of them load-bearing, and identical in both modes:

```python
common = dict(
    model=models.for_role(role),
    messages=ctx.build(),                                    # §6 — the only builder
    tools=schemas.for_mode(mode),                            # §7.1 — mode-filtered
    temperature=cfg.temperature_for(role),
    max_tokens=MODE_MAX_TOKENS[mode],                        # §4.4
    stream=True,
    stream_options={"include_usage": True},                  # ← without this there is no accounting
    extra_body={"chat_template_kwargs": {"enable_thinking": MODE_THINKING[mode]}},
    user=f"gitlab:{claims['sub']}",                          # attribution at the proxy
)
```

- **`http2=False` is deliberate.** nginx's HTTP/2 handling is a known source of buffering and dropped streams on long-lived streaming responses; HTTP/1.1 with keep-alive plus `proxy_buffering off` is the combination that works.
- **`trust_env=False`** encodes the launcher lesson (unset `HTTP(S)_PROXY`) in code rather than a shell script.
- **`stream_options.include_usage` on every call.** This is what makes §16.4's real token accounting possible; the frontend agent's omission of it is the root cause of its quota being fiction.
- **`user` is set on every request** so the proxy can attribute spend even before per-user virtual keys exist (§16.6).
- Retry: 3 attempts on connect errors, timeouts, 429, 502/503/504 with exponential backoff + jitter (1.5 s, 3.5 s). Never retry a non-429 4xx — LiteLLM's `UnsupportedParamsError` is a bug in our request, and retrying it just wastes a second. Strip HTML from upstream error pages before surfacing.
- Prewarm at startup with a small probe (fold it into §4.5) and **keep prewarm on in local mode** — Part B currently disables it, trading a background second for a visible first-request stall.

---

## 5. Learnings from the frontend agent

Every finding below is from reading `postgen`'s shipped code, with file references and — where possible — a measurement taken on this machine. **Fixing `postgen` is out of scope.** This section exists so that each finding maps to a design decision here, and so nobody has to rediscover any of it. The "Fix" column is what *we* build, in *this* system.

Keep the file references: when a Phase-0 engineer is implementing §6 and wonders why a cap or a ledger exists, the answer is one `grep` away in a codebase they already have.

### 5.1 Ranked findings

| # | Finding | Evidence | Cost | Fix |
|---|---|---|---|---|
| **S1** | **No context management inside a run.** `AgentRun.messages` is append-only across up to 40 turns. The only trimming in the codebase is a 40-*message* cap in `resume.py`, and it applies **only to resumed sessions**. | `agent.py` `AgentRun.messages`; `resume.py:39-40` `DEFAULT_CONTEXT_MAX_MESSAGES`, applied in `reconstruct_messages` only | Quadratic. Turn *n* re-prefills everything from turns 1..*n*−1. | §6 — token-budgeted context manager with compaction |
| **S2** | **`repo_map` and `search_repo` walk `node_modules`.** `search.py::_walk` uses `Path.rglob("*")`; exclusion happens *after* each path is yielded and stat'd. **`rules.py` already fixed exactly this** with `os.walk` + in-place `dirnames` pruning, and its own comment calls `rglob` "the single biggest cost… brutal on Windows, where antivirus intercepts every `stat()`". `search.py` never got the fix. | `search.py::_walk`, `search_repo` (`workspace.glob(glob_pat)`) vs `rules.py:104-152` | **Measured: 16,680 paths → 1.61 s, of which only 200 were kept.** A real Next.js `node_modules` is 150k–400k files → **15–40 s per call** on Windows. `search_repo` additionally *reads* every matching file. | Port `_PRUNE_DIRS` + `os.walk` pruning into `search.py`. Use `ripgrep` for `search_repo`. For Go: prune `vendor`, `.git`, `bin`, `gen`, plus `GOMODCACHE`. |
| **S3** | **`repo_map` reads every file twice.** Once for the 120-char `head`, then again in full to count `\n` for `lines`. | `search.py::repo_map` — `text = path.read_text(...)` then `path.read_text(...).count("\n")` | Doubles I/O on up to 200 files, every uncached call. | Read once, derive both. |
| **S4** | **`repo_map`'s output is enormous and permanent.** `json.dumps(..., indent=2)` with a per-file head, over up to 200 files. | `search.py::repo_map` | **≈20–30k tokens**, injected at turn 1, then re-sent on every subsequent turn for the rest of the task. | Cap at 4k tokens; emit a compact non-indented form; make depth/breadth adaptive; put the detail behind a second call (§7.2). |
| **S5** | **~5.7k tokens of fixed per-turn overhead.** | **Measured**: `PLANNER` 2,672 tok, `CODER` 2,562 tok, `DEBUGGER` 2,757 tok; `TOOL_SCHEMAS` 3,020 tok across 19 tools (`template_scaffold`'s description alone is **562 tok**) | 5.6–5.8k tokens re-sent every turn. Harmless *with* a warm prefix cache, expensive without. | Slim to ≤1.2k prompt + ≤1.2k schemas via progressive disclosure (§14) and mode-filtered schemas (§7.1). |
| **S6** | **Phase transitions destroy the KV prefix by design.** `_run_planner`, `_run_coder` and `_run_debugger` each do `run_state.messages = [{"role":"system", ...}, ...]` with a *different* system prompt. | `agent.py` — three separate assignments | Three cold prefills per task, even with APC on. | §6.4 — **one** system prompt for all modes; mode instructions as an appended message. Makes the ~2.4k-token prefix cacheable across phases *and* across tasks. |
| **S7** | **Prefix caching is neither enabled-by-contract nor measured.** No flag, no `cached_tokens` read, no metric. | absent throughout | We do not know whether the biggest available win is even switched on. | §4.3 — and note we inherit the *same blindness*, because `prompt_tokens_details` is absent from our proxy too. The difference is that here it is a tracked open question (plan.md §9 Q1) with §6.4 built so the prefix is reusable either way. |
| **S8** | **Tool results enter history untruncated.** `read_file` caps at 2,000 lines / 512 KB — about **25k tokens** — and `ToolResult.to_payload()` applies no cap at all. Only the *SSE event* is capped (`content[:4000]`). | `fs.py` `MAX_READ_LINES = 2000`; `tools/__init__.py::to_payload`; `agent.py` `content=result.content[:4000]` | One unlucky `read_file` can consume most of a sane budget, permanently. | §6.2 — per-tool insertion caps with elision markers. |
| **S9** | **Tool calls in a batch run strictly serially.** | `agent.py::_execute_tool_calls` — a plain `for` loop | A turn that requests four `read_file`s pays 4× latency instead of 1×. | Execute read-only tools concurrently (bounded pool); keep write/exec tools serial and ordered. |
| **S10** | **A fresh HTTPS client per run.** `make_client(cfg)` is called inside `agent.run`. | `agent.py::run` → `llm.make_client` | TCP + TLS handshake on every task; no connection reuse, no keep-alive pool. | §4.3 — one process-wide client. |
| **S11** | **One `asyncio.to_thread` hop, one `json.dumps`, and one SSE frame *per streamed token*.** | `server.py::event_stream` — `await asyncio.wait_for(asyncio.to_thread(event_q.get, True, 0.5), timeout=1.0)`, and `assistant_delta` is emitted per chunk in `agent.py::_complete` | Thread-pool round-trip per token. Dominates CPU during generation and adds jitter to every token. | Coalesce deltas in a 40–60 ms window into one frame; replace the `to_thread` poll with an `asyncio.Queue` fed by `loop.call_soon_threadsafe`. |
| **S12** | **Single-process uvicorn; agent runs on GIL-bound threads.** `uvicorn.run(app, host, port)` with no `workers`; each run is a `threading.Thread`. | `server.py::main`, `server.py` `worker()` | CPU-bound Python (regex search, rules engine, tree walks) holds the GIL and stalls *every other session* **and** the SSE loop. | Move CPU-bound work out of Python (that is what `gotools` is); run uvicorn with `workers=N` in server mode; keep local mode single-worker (one user). |
| **S13** | **Generous turn budgets.** Planner 10 + Coder up to `max_turns=40` + Debugger 12, plus 2 verify-gate retries. | `agent.py` `PLANNER_MAX_TURNS`, `cfg.max_turns`, `DEBUGGER_MAX_TURNS`, `VERIFY_GATE_BUDGET` | Worst case ≈60 model round-trips for one task. | §6.5 — budget by *tokens and wall-clock*, not just turn count; tighten turn caps once the Go compiler shortens the loop. |
| **S14** | **One model tier for everything.** | `config.py` single `model` | Summarisation, classification and routing all pay 27B latency. | §4.1 — the `fast` tier. |
| **S18** | **No token accounting at all.** `stream_options`/`include_usage` is never sent; `usage` is never read (except the 4-token prewarm probe); `session.token_usage` exists only in a TODO comment. This is *why* the quota reserves a flat 4,096 tokens and never refunds. | grep: `stream_options` — 0 hits in `src/`; `quota.py:25-27` comment; `server.py:533-534` comment | Quota is fiction; there is no cost attribution and no way to measure S1–S14. | §16.4 — reconcile from real `usage` on every turn. |

Two more that belong to Part B but are named here because they are part of the same latency story: **S15** — the local runtime `pip install`s `[server]` extras from the network on every wheel change, which behind the corporate proxy is minutes and a documented failure mode; **S16** — the extension spawns with `--no-prewarm`, so the first request pays cold start. Both are fixed in Part B §4.

### 5.2 What this adds up to

A worked estimate for a 25-turn brownfield task under the frontend agent's design, with a warm model but no prefix cache. This is the shape we are avoiding:

```
fixed overhead per turn            5,700 tok   (S5)
repo_map, resident from turn 1    25,000 tok   (S4)
average new content per turn       1,500 tok   (assistant + tool result; S8 makes this spiky)

prompt at turn 25  ≈  5,700 + 25,000 + 25 × 1,500        ≈   68,000 tok
total prefill      ≈  25 × 30,700  +  1,500 × (25·26/2)   ≈ 1,250,000 tok
```

**≈1.25 M tokens of prefill for one task, of which roughly 95% is recomputation of a prefix that never changed.** That is the finding. Everything in §6 exists to collapse it.

### 5.3 Our targets

| Metric | Frontend agent (est.) | `dakcoder-go` target |
|---|---|---|
| Prompt tokens, P95, coder turn | ~68k | **≤24k** |
| Total prefill per 25-turn task | ~1.25 M | **≤180k** (cap + compaction + prefix reuse) |
| Prefix-cache hit rate across a task | unknown | **≥80%** — *contingent on plan.md §9 Q1; until then, measure prompt-token growth as the proxy* |
| Reasoning tokens as a share of completion, Coder mode | n/a | **0%** (thinking off, §4.4) |
| First-token latency, P95, warm | ~4 s | **≤2.5 s**, inclusive of the LiteLLM hop |
| `repo_map`, P95, 5k-file repo with `vendor/` | 15–40 s | **≤1.5 s** |
| Inner-loop type check | — | **`go_diagnostics` ≤800 ms**, else incremental `go build` ~4 s (measured, §8.2) |
| Median wall-clock, "add a resource" | — | **≤90 s** |
| Wasted turns from `content: null` | n/a | **0** (§4.4 rule 2) |

---

## 6. Context and token engineering

This is a first-class subsystem — `apps/agent/context.py` — not a helper. It is the only component allowed to construct the message list.

### 6.1 The budget

A hard **32,768-token prompt budget** for a coder turn (24k for planner, 32k for debugger). Rationale: the context-rot literature is consistent that accuracy degrades with input length across every frontier model tested — lost-in-the-middle, attention dilution, distractor interference — so a big window is not free even when the GPU allows it. A tight budget is a *quality* decision as much as a latency one.

Allocation, in eviction-priority order (last evicted first):

| Layer | Budget | Notes |
|---|---|---|
| System prompt (shared, all modes) | ≤1,200 | §6.4, §14 |
| Tool schemas (mode-filtered) | ≤1,200 | §7.1 |
| Task + plan + acceptance criteria | ≤800 | pinned, never evicted |
| Compaction recap | ≤2,000 | grows as history is evicted |
| **Live working set** | **~27,500** | file slices, tool results, recent turns |

The budget is on **input**. Output is budgeted separately per mode in §4.4, and on a thinking-on mode the reasoning block is charged against that output budget — so a Planner turn is 24k in, up to 4k out, of which reasoning may be most of it. The two budgets are tracked and alerted on independently; conflating them is how a mode ends up with enough room to think and not enough room to answer.

### 6.2 Insertion caps

Every tool result is capped **at the moment it enters history**, not at display time (fixing S8). Caps are per-tool, and elision always leaves a machine-readable marker so the model knows it can re-read:

| Tool | Cap | Elision rule |
|---|---|---|
| `read_file` | 400 lines / 6k tok | keep the requested range; `[... N lines elided — re-read <path>:<a>-<b> ...]` |
| `repo_map` | 4k tok | breadth-first, adaptive depth; `[... M packages summarised — call repo_map(package=…) ...]` |
| `search_repo` | 2k tok | first 40 hits + per-file hit counts |
| `go_build` / `go_vet` / `go_test` | 4k tok | **error and failure lines verbatim, always.** Elide the middle of long output, never the `file:line:col` messages — those are the agent's best fuel. |
| `rules_lint` | 3k tok | scoped to mutated paths (inherit `postgen`'s scoping); group by rule id |
| `go_diagnostics` | 2k tok | severity-ordered |
| everything else | 2k tok | tail-elide |

When a cap bites hard (>3× over), summarise the overflow with the **`fast`** model rather than truncating blindly.

### 6.3 The file-slice ledger

The single largest win on edit-heavy tasks. An agent that reads `handler/user.go`, patches it, re-reads it, patches again, and re-reads once more currently keeps **three full copies** in history forever.

The ledger keeps only the **newest** read of each path. Older reads are replaced in place with a one-line stub:

```
[stale read of handler/user.go:1-120 — superseded by a later read; re-read if needed]
```

Combined with the caps in §6.2, this bounds the working set by *distinct files touched*, not by *number of reads*.

### 6.4 Stable-prefix discipline (fixes S6)

**One system prompt for every mode.** It carries only what is genuinely invariant: who the agent is, the non-negotiable template contract in its shortest form, the tool-use protocol, and the discipline rules. Mode-specific instruction (Planner vs Coder vs Scaffolder vs Verifier vs Debugger) is appended as a **developer/user message after the system message**, and mode switches append a new instruction message rather than rebuilding the list.

Consequences, all good:

- The `system + tool-schemas` prefix — roughly 2.4k tokens — is **identical across all phases and across every task in the repo**, so it is a permanent prefix-cache hit.
- Phase transitions become cheap: the planner→coder handoff keeps everything already cached.
- Prompt versioning gets simpler: one file to review, with mode overlays beside it.

The Debugger keeps its "fresh conversation" property (it should not inherit the Coder's failed reasoning) — but it does so by *starting a new working set under the same system prefix*, not by swapping the system prompt.

**Rule: the message list is append-only below the pinned head.** Any mutation of `messages[0..k]` is a cache-invalidating bug and should fail a unit test.

### 6.5 Compaction

Triggered at **70% of budget**, or on an explicit `/compact`, or on an `overflow_recovery` path if a call still comes back over-length.

Summarise — do not truncate. This is the lesson from Cline's move away from truncation: truncation silently drops the decision that explains the current diff, and the agent then re-derives it wrongly. Summarisation preserves it. It is also cheap when the prefix is cached — a compaction call then costs about the same as any other tool call.

Compaction runs on the **`fast` role** with thinking **off** and `max_tokens` 1024. Today that resolves to the same 27B model, so a compaction is a real 27B call and its cost shows up in telemetry; §18 breaks token spend down by role precisely so that the moment compaction and summarisation become a visible share, pointing `DAKCODER_MODEL_FAST` at `Phi-4-mini-instruct` is an obvious, evidence-backed one-line change (§4.2).

The recap is **structured**, not prose:

```markdown
## Recap (turns 1–14, compacted)
Goal:            add Pension resource with CRUD + status filter on List
Plan step:       6 of 8 — wiring FxHandler
Files created:   core/domain/pension.go, db/pensions.sql, repo/postgres/pension.go,
                 handler/response/pension.go, handler/pension.go
Files modified:  handler/request.go, bootstrap/bootstrapper.go
Decisions:       table = pensions (user-confirmed); status is oneof=active|suspended|closed;
                 List filter added to PensionListParams, not to the URI
Verified:        gofmt clean, go build clean at turn 12
Open:            rules_lint fx-registration — handler present but missing ResultTags
Do not retry:    hand-editing request_*_validator.go (generated; run govalid_gen)
```

Two properties matter:

- **"Do not retry" is explicit.** Recording dead ends is what stops the post-compaction agent from cheerfully repeating them.
- **The recap is persisted** to `.dakcoder/session-<id>/recap.md` and the plan to `.dakcoder/session-<id>/plan.md` (both git-ignored automatically). This is the write-context-outside-the-window pattern, and it is what the Oracle→Postgres migration skill set does with its plan file: a durable artefact that survives a compaction, a restart, or a new session, and that a later run can simply read. For the multi-day `pao` migration mode (§12) this is essential.

### 6.6 Just-in-time retrieval

The agent should hold **references**, not content. `repo_map` returns paths and symbol names; the agent fetches a slice when a step needs it. Cursor reports a 46.9% reduction in total agent tokens from exactly this shift — everything accessible as a file, retrieved on demand, rather than pre-loaded.

For Go this is unusually effective because `gopls` can answer most navigation questions *without* returning file content at all (§8.2): "which types implement `serverHandler.Handler`", "where is `NewUserRepository` referenced", "what is the exported API of package `repo/postgres`". Answers are tens of tokens; the equivalent grep-and-read is thousands.

### 6.7 Sub-context isolation

Two places where a sub-agent with its own clean window pays for itself, both bounded and both Phase 2+:

- **Compliance audit** over a large service: one isolated window per package, returning only a violation list. Keeps 40 packages' worth of source out of the main window.
- **Legacy migration** (§12): one isolated window per handler being migrated, returning a diff plus a compliance verdict.

Both are fan-out-then-synthesise, with the parent holding only the summaries. Deliberately *not* used for the ordinary edit loop — that pays coordination cost for no benefit.

### 6.8 Caching, corrected

Inherit `postgen`'s two-tier cache (Redis when configured, in-process LRU otherwise) with three fixes:

1. **Key by tenant for real.** `read_file` and `repo_map` currently pass `DEFAULT_TENANT = "anonymous"`, so the tenant dimension is dead. Once identity exists (§15) the key must be `u:<sub>:<workspace-hash>:…`.
2. **mtime + size**, not TTL alone, for `repo_map`. A 300 s TTL on a map the agent just invalidated by writing a file is a correctness bug waiting to happen; `postgen` busts on mutation, which is right, but the TTL path can still serve a stale map after an *external* edit.
3. **Add a `search_docs` cache.** The knowledge-base corpus is immutable between releases; cache aggressively with the corpus hash in the key.

---

## 7. Tool catalog

### 7.1 Design rules

Inherit `postgen`'s rules — ≤6 parameters, hand-written schema, ≤200-character descriptions written as *instructions to the model*, `{ok, content, mutations[]}` results, fail loud rather than corrupt — and add two:

- **Mode filtering is a guarantee, not a hint.** `postgen` already proves this out: the Planner physically cannot write because its schema list is filtered, not because the prompt asks nicely. Extend it to every mode. It also directly serves the token budget (S5): no turn ever sees more than ~12 schemas.
- **Blocked calls return the working alternative.** `postgen`'s best small idea: when `run_terminal` refuses `grep`, it says "use `search_repo`". That saves a whole turn. Every refusal must name the right tool.

### 7.2 The catalog

24 tools in the registry; ≤12 visible in any one turn.

| Tool | Purpose | Mutates | Approval | Modes |
|---|---|---|---|---|
| `repo_map` | Module path, package tree, exported symbols, `go.mod` requires, FX providers parsed from `bootstrap/`. Adaptive depth; `package=` narrows. | – | – | P C S V D |
| `read_file(path, start?, end?)` | Read a slice. Line ranges strongly preferred. | – | – | P C S V D |
| `search_repo(pattern, glob?, max?)` | ripgrep content search, pruned tree | – | – | P C V D |
| `search_docs(query)` | BM25 + vector over chunked `skill.md`, `SOP.md`, playbooks, `go.instructions` | – | – | P C S D |
| `go_symbols(query)` | gopls: `go_search` / `go_references` / `go_package_api` / `go_symbol_references` | – | – | P C V D |
| `go_diagnostics(path?)` | gopls type-check diagnostics — **the inner-loop compile signal** | – | – | C V D |
| `rules_lint(paths?, only?)` | AST template-compliance linter (§9) | – | – | P C V D |
| `legacy_audit(paths?)` | Legacy-pattern scan for `pao`-generation code (§12) | – | – | P V |
| `write_file(path, content)` | Create a new file; refuses to overwrite | ✓ | conditional¹ | C S D |
| `patch_file(path, old, new)` | Unique-match replace; fails loud | ✓ | conditional¹ | C D |
| `delete_file(path)` | Delete | ✓ | **always** | C D |
| `gofmt(paths?)` | `gofmt -w` / `goimports` | ✓ | – | C S V D |
| `resource_scaffold(spec)` | The 10-step resource recipe, from `text/template` (§11.1) | ✓ | **yes** | S |
| `project_scaffold(spec)` | Greenfield service (§11.2) | ✓ | **yes** | S |
| `fx_wire(kind, ctor)` | AST-correct insertion into `FxRepo` / `FxHandler` | ✓ | – | S C D |
| `govalid_gen()` | Regenerate `handler/request_*_validator.go` | ✓ | – | S C V D |
| `go_build()` | `go build ./...` — the authoritative gate | – | – | C V D |
| `go_vet()` | `go vet ./...` | – | – | V D |
| `go_test(pattern?)` | `go test ./...` or a package | – | – | V D |
| `golangci_lint()` | If a config exists; non-blocking → warning | – | – | V |
| `go_mod(op, pkg?, ver?)` | `tidy` free; `get`/add direct dep allow-list-enforced | ✓ | conditional | C V D |
| `govulncheck()` | On greenfield scaffolds and dep changes | – | – | V |
| `swagger_check()` | Route `.Name(...)` present, `swagger.generation.mode` set; optionally boot and diff `/docs/v3Doc.json` (§6 of plan.md — **not** a codegen step) | – | – | V |
| `git_status` / `git_diff` / `git_blame` / `git_ops` | Working-tree state; stage/commit/branch/`ensure_agent_branch`. No `push`, no `reset --hard`. | ✓(ops) | conditional² | all |
| `playbook(rule?)` | Failure-class fix recipe (§13.2) | – | – | C V D |
| `run_terminal(argv, timeout?)` | Allow-listed binaries, argv only, no shell | – | conditional | D |

Modes: **P**lanner · **C**oder · **S**caffolder · **V**erifier · **D**ebugger.

¹ Approval required when the path matches `go.mod`, `go.sum`, `main.go`, `bootstrap/**`, `configs/*.yaml`, `db/**.sql`, or any `*_validator.go` (generated — regenerate, never hand-edit).
² Approval for anything rewriting pushed history or force-deleting.

**Deliberately absent:** `sql_migrate`. Applying DDL to a database from an agent turn is the one irreversible action with no `git restore`. Phase 2 at the earliest, behind explicit approval, and only against a dev DSN. In the meantime the agent *writes* the `.sql` file and tells the developer to run it.

### 7.3 The compiler is a tool, not an afterthought

Unlike JavaScript, Go gives a fast authoritative signal. The design consequence: prefer running a check over reasoning about whether code compiles. But **which** check matters enormously for latency — see §8.2.

---

## 8. The Go sidecar

### 8.1 `gotools` — what we build

A single static Go binary, shipped per-platform, speaking MCP over stdio using the official `github.com/modelcontextprotocol/go-sdk`. Struct-based typed inputs/outputs with `jsonschema` tags give us schema generation for free and keep C1 honest.

```go
type LintInput struct {
    Paths []string `json:"paths,omitempty" jsonschema:"workspace-relative files to scope the lint to"`
    Only  []string `json:"only,omitempty"  jsonschema:"subset of rule ids to run"`
}
type LintOutput struct {
    OK         bool        `json:"ok"`
    Count      int         `json:"count"`
    Violations []Violation `json:"violations"`
}
```

It owns:

| Tool | Implementation |
|---|---|
| `rules_lint` | `golang.org/x/tools/go/packages` with `NeedName\|NeedTypes\|NeedSyntax\|NeedTypesInfo`, then per-rule AST + type visitors (§9) |
| `legacy_audit` | Same loader, legacy-pattern rule set (§12.1) |
| `resource_scaffold`, `project_scaffold` | `text/template` over an `embed.FS` of template files mirroring `skill.md` |
| `fx_wire` | Parse `bootstrap/bootstrapper.go`, insert into the correct `fx.Provide` list with the correct annotation shape, re-emit with `go/format` |
| `repo_map` | `go list -json ./...` for the cheap path; `packages.Load` for exported-symbol detail; FX providers by walking `bootstrap/` |

Why Go, restated: these are all *type* questions. And it moves the heaviest CPU work out of the Python process entirely, which is the structural fix for S12.

### 8.2 `gopls mcp` — what we do **not** build

`gopls` ships an MCP server (`gopls mcp`, stable enough from v0.20.0) exposing `go_context`, `go_diagnostics`, `go_file_context`, `go_file_metadata`, `go_package_api`, `go_references`, `go_rename_symbol`, `go_search`, `go_symbol_references`, `go_workspace`, `go_vulncheck`.

**Use it. Do not reimplement it.** Two reasons, one of which is the most important Go-specific latency insight in this plan:

1. **Reported 2–4× fewer tool calls** than grep-based navigation for semantic questions — "which concrete types implement this interface", "trace this call hierarchy", "what does this package export" — because the answer is compiler-grade rather than inferred from text.
2. **The Go toolchain has a very wide cold/warm spread, and the loop must be built around it.** Measured on `new-template` (go 1.25.0, Windows, private modules already in `GOMODCACHE`):

   | Operation | Measured |
   |---|---|
   | `go build ./...` **cold** (`go clean -cache`) | **2 m 30 s** |
   | `go build ./...` warm, nothing changed | **23.4 s** |
   | **`go build ./...` after touching one file** | **4.2 s** |
   | `go vet ./...` warm | **31.7 s** |

   Three corrections to the earlier estimate, all of which change design decisions:

   - **The incremental build is 4.2 s, not 20–60 s.** That is the case the agent actually hits, because it edits one or two files at a time. `go build` is therefore *viable* in the inner loop, and the `gopls` advantage is real but narrower than claimed (~4 s → ~0.3–0.8 s). **This materially de-risks the plan**: if the `gopls` MCP dependency (§8.3) proves awkward, an incremental `go build` is an acceptable fallback rather than a disaster.
   - **`go vet` (31.7 s) is more expensive than `go build`.** It belongs at the gate only, never in the inner loop — and on a 25-turn task, a Verifier that runs `go vet` per step would add ten minutes of pure waiting.
   - **A cold build costs 2 m 30 s**, so build-cache warmth is a first-class operational concern, not an implementation detail. In local mode the developer's cache is usually warm and this is a non-issue. In server mode (Phase 3) an ephemeral workspace with a cold `GOCACHE` would pay 2.5 minutes on its first verification — so the warm-pool design must persist `GOCACHE` **and** `GOMODCACHE` across sessions, not just the module cache. That is a concrete requirement the plan would otherwise have missed.

   These numbers are from one module on one machine and should be re-measured on a large real service (`pao` has ~110 files, protobufs, and Temporal) during the Phase-0 spike.

The resulting loop discipline:

```
edit → gofmt                    (~100 ms)  ← after every edit
     → go_diagnostics           (~300 ms)  ← after every edit batch   [gopls; falls back to go_build]
     → rules_lint               (~200 ms)  ← after every edit batch   [gotools]
     → go build ./...           (~4 s)     ← once per plan step (incremental — cheap enough)
     → go vet, govalid_gen, go_test, swagger_check   ← at the gate ONLY (go vet alone is ~32 s)
```

`gopls` runs in local mode as a child of `dakcoderd`, keyed to the workspace, with an idle timeout. Cost: one warm `gopls` process (~200–600 MB on a large module). Worth it — but see §8.3, because the version we need is not the version that is installed.

### 8.3 The `gopls` MCP dependency is not yet satisfied

The MCP server arrived in `gopls` **v0.20.0**. The version on this machine is **v0.16.2**, where `gopls mcp` is simply an unknown command (verified). So §8.2's fast inner loop depends on an upgrade that is not in place, and the upgrade has its own dependency: `go install golang.org/x/tools/gopls@latest` fetches from the **public** module proxy, and `GOPRIVATE=gitlab.cept.gov.in` covers only the internal host — whether `proxy.golang.org` is reachable, or whether an internal Go module mirror exists, is unverified.

Consequences, ordered:

1. **Phase 0 must verify public module fetch**, not assume it. If it is blocked, we need an internal `GOPROXY` mirror or a vendored `gopls` binary — and that is an infrastructure ask with a lead time, so it should be raised in week one rather than discovered in week four.
2. **`go_diagnostics` must ship with the `go build` fallback from day one**, not as a graceful-degradation afterthought. Given the 4.2 s incremental measurement, the fallback is genuinely acceptable, which is why this is a schedule risk rather than a design risk.
3. `Doctor` reports the `gopls` version and whether MCP is available, and the agent selects its inner-loop strategy from that — so a developer on v0.16.2 gets a working (slightly slower) agent rather than a broken one.

### 8.4 Process supervision

Both sidecars are supervised children: health-checked at startup, restarted with backoff on crash, killed on workspace change, and hard-capped in memory. A sidecar being unavailable degrades gracefully — `rules_lint` unavailable is a warning that blocks the gate; `gopls` unavailable falls back to `go_build` for diagnostics and `search_repo` for navigation, with a visible note that the agent is in slow mode.

---

## 9. Rules engine

### 9.1 Principles inherited from the frontend agent

Three things `postgen` got right and we keep verbatim:

- **Verification is forced by the runtime, not requested in the prompt.** A model saying "done" is not evidence. The gate is code.
- **Verification is scoped to files the agent touched.** Otherwise pre-existing legacy violations trigger an endless parade of unrelated "fixes". Out-of-scope violations are reported and do not block.
- **Observed misbehaviour becomes a machine-checked rule, not another paragraph of prompt.** All three of `postgen`'s new checks (`check-dop-components`, `check-mvc-structure`, `check-stack`) were born this way. Budget for the Go equivalents to emerge in Phase 1–2 and leave room in the rule ids.

### 9.2 The template-compliance suite (corrected)

Every rule cites the `skill.md` section or `SOP.md` clause it enforces, and every rule has a positive fixture (the shipped `user` resource must pass all of them) and a negative fixture.

| Rule id | Detects | Fix strategy |
|---|---|---|
| `layer-sql-boundary` | `squirrel` / `pgx` / `dblib` imported or used outside `repo/postgres` | Move data access into a repository method; the handler calls `h.svc.X(sctx.Ctx, …)` |
| `layer-dto-boundary` | `repo/**` imports `handler` or `handler/response`; a `domain` struct carrying HTTP/DTO types | Repos return `domain.*`; conversion lives in `handler/response` via `New*Response` |
| `handler-signature` | Method is not `(sctx *serverRoute.Context, req T) (*resp.R, error)`; any `*gin.Context`; any `ShouldBind*`; any manual validation. **Accepts `_ struct{}`** for input-less routes. | Rewrite to the DTO signature; delete manual bind/validate — the framework does both |
| `handler-base` | Handler does not embed `*serverHandler.Base`, or the constructor is not `serverHandler.New("Xs").SetPrefix("/v1").AddPrefix(…)` | Add the embed; fix the constructor |
| `routes-in-handler` | A `routes.go` exists, or routes are registered outside `Routes()`, or a route lacks `.Name(...)` | Move routes into the handler's `Routes()`; add `.Name(...)` (swagger depends on it) |
| **`repo-contract`** | **Query not built with `dblib.Psql.*`** (a hand-rolled `sq.StatementBuilder` or raw string SQL); missing `context.WithTimeout(ctx, r.cfg.GetDuration("db.QueryTimeout…"))`; constructor not injecting `*dblib.DB` + `*config.Config` | Use `dblib.Psql`; wrap the timeout; fix the constructor. **Corrected from v1 — see plan.md §6** |
| `repo-rowmapper` | `SelectOne`/`SelectRows` without `pgx.RowToStructByName[domain.X]` | Add the row mapper |
| `repo-norows` | `Update`/`Delete` not converting `RowsAffected() == 0` to `pgx.ErrNoRows` | Return `pgx.ErrNoRows` so the handler surfaces a 404 |
| `domain-tags` | Domain field missing `json`/`db` tags, tags not `snake_case`, or missing `ID`/`CreatedAt`/`UpdatedAt` | Add / normalise |
| **`request-dto`** | Request struct outside `handler/request.go`; missing `validate` tags; `uri:"…"` missing on path params; `form:"…"` missing on query params. **No `ToDomain()` requirement** | Relocate; add tags. **Corrected from v1** |
| `response-dto` | Not embedding `port.StatusCodeAndMessage` (plus `port.MetaDataResponse` for lists); missing `json:",inline"`; missing `New*Response` / `New*sResponse`; timestamps not string-formatted `"2006-01-02 15:04:05"` | Fix embedding and converters |
| `response-status` | Not using the predefined `port.CreateSuccess` / `ListSuccess` / `FetchSuccess` / `UpdateSuccess` / `DeleteSuccess` | Use the predefined constant for the operation |
| `validator-stale` | `validate` tags changed but `*_validator.go` not regenerated (content hash mismatch), or a validator was hand-edited (`DO NOT EDIT` header violated) | Run `govalid_gen` |
| `fx-registration` | New handler or repo missing from `bootstrap/bootstrapper.go`; handler missing `fx.Annotate` + `fx.As(new(serverHandler.Handler))` + `fx.ResultTags(serverHandler.ServerControllersGroupTag)`; repo not in `FxRepo` | `fx_wire` inserts the correct block |
| `error-handling` | Error returned without `log.Error(sctx.Ctx, "…: %v", err)` first; custom status not via `apierrors.HandleErrorWithStatusCodeAndMessage`; `pgx.ErrNoRows` not surfacing as 404 | Apply the SOP error order |
| `dep-allowlist` | A new **direct** dependency outside the allow-list (plan.md §4) | Find an allow-listed equivalent or escalate with a rationale |
| `secrets-in-config` | A **newly added** literal credential in `configs/*.yaml` | Refuse. Pre-existing literals are reported once as an advisory and never echoed (plan.md §6) |
| `config-key-exists` | `cfg.GetString/GetDuration("k")` where `k` is absent from the active env YAML | Add the key or fix the path (casing matters: `db.QueryTimeoutLow`) |
| `swagger-visible` | A route added without `.Name(...)`, or `swagger.generation.mode` unset | Add the name; check the config |
| `file-size` | A Go file over ~600 lines | Split by responsibility (the Go analogue of `check-lines`) |
| `go-idiom` | A curated subset of `go.instructions.md` that is machine-checkable: unchecked errors, `interface{}` instead of `any`, error strings capitalised or punctuated, duplicate `package` declarations, missing `defer` close | Fix per the cited guideline |

`go-idiom` deliberately sits *under* the template rules and is **advisory** (warning, not gate) — with one exception: duplicate `package` declarations are a hard error, and `go.instructions.md` flags them as a recurring LLM failure mode, so they gate.

### 9.3 The verification gate (`verify:all`, corrected)

Ordered, fail-fast, each stage feeding the next. Note the two-speed structure from §8.2, the corrected swagger step, and the `gofmt` correction below.

```
# inner loop — after every edit batch (sub-second)
gofmt -w <mutated>        # AUTO-FIX, not a check — see below
go_diagnostics            # gopls incremental type check (~300 ms)
rules_lint <mutated>      # scoped to touched files (~50–100 ms measured, §8.2)

# gate — once per plan step, and before finalising
go build ./...            # AUTHORITATIVE — nothing else counts until this is clean (~4 s incremental)
govalid_gen               # regenerate validators, then go build again
rules_lint                # full scope, but only touched-file violations block
swagger_check             # route names + generation mode (NOT codegen)
go vet ./...              # ~32 s measured — gate only, NEVER the inner loop
go test ./...             # if tests exist
go mod tidy               # must be a no-op; a diff means deps drifted
golangci-lint run         # if configured — warning only
govulncheck               # greenfield scaffolds and dep changes only
```

**`gofmt` is an auto-fix, not a gate — and it must be scoped.** Two findings force this:

- **The reference template does not pass `gofmt -l`.** Every `.go` file in `new-template` is flagged, because they all use CRLF line endings. A gate that begins with an unconditional `gofmt -l` therefore fails on the *reference resource*, on files the agent never touched. Scoping `gofmt` to mutated paths is the same principle already applied to `rules_lint`, and for the same reason.
- **The model's formatting misses are trivial and mechanical.** In the spike its generated files were `gofmt`-clean except for a missing trailing newline and one struct-tag alignment column — both fixed by a single `gofmt -w`. Spending a turn asking the model to fix its own whitespace would be pure waste.

So: run `gofmt -w` on the mutated files, then proceed. Report a formatting *change* in the diff; never fail a gate on it.

`go vet` moves to the end of the gate because it is the most expensive stage measured (~32 s, more than `go build`), and nothing downstream depends on it.

A change is done only when `go build`, `go vet`, `rules_lint` (in-scope) and `go mod tidy` are clean and validators are current.

**Gate budget**: 2 Coder attempts, then a typed escalation envelope to the Debugger — inherit `postgen`'s shape exactly, including the fresh-working-set property (§6.4) and the no-progress detector (same tool call + args three turns running → stop).

---

## 10. The agent loop

One loop, one system prompt, five mode overlays (§6.4). Modes narrow the tool schema (§7.1) and sharpen the instruction; they do not fork the process.

| Mode | Purpose | Exit |
|---|---|---|
| **Planner** | Read task + `repo_map`; emit ≤8 numbered steps naming real files with `Accepts:` criteria, in text **and** JSON. Read-only tools, enforced by schema filtering. For a resource add, the plan *is* the 10-step recipe. | Plan emitted, or forced-plan on the last budget turn |
| **Scaffolder** | Turn a field spec into files deterministically. The LLM produces the *spec*; `text/template` produces the *code*. | Files written → Verifier |
| **Coder** | Execute one plan step: minimal `patch_file` diffs, `write_file` for new files, inner-loop verify after each batch | Step done, or all steps done → gate |
| **Verifier** | Run the gate (§9.3), emit a structured violation list | Clean → done; dirty → Coder (≤2), then Debugger |
| **Debugger** | Reproduce → consult playbook → localise → hypothesise (1–3 ranked, with evidence) → minimal fix → re-verify (≤3 cycles) → journal | Fixed, budget exhausted, or no progress |

**Doc-Scribe is explicitly cut.** `postgen` planned it and never shipped it, and nobody missed it. If documentation updates are wanted later they are a Coder step, not a mode.

**Multi-process orchestration stays deferred**, exactly as the frontend build concluded. The single loop with a forced verification gate, a rules engine, playbooks, and an approval layer is what actually works. Sub-context isolation (§6.7) covers the two cases where fan-out genuinely helps.

### 10.1 Hand-off envelope

```json
{
  "from": "planner", "to": "scaffolder", "session_id": "…", "step_id": 2,
  "instruction": "Scaffold resource Pension with CRUD + status filter on List",
  "resource": {
    "name": "Pension", "plural": "Pensions", "table": "pensions", "route_base": "/pensions",
    "fields": [
      {"go":"PPONumber","json":"ppo_number","db":"ppo_number","type":"string","validate":"required","sql":"VARCHAR(20) NOT NULL"},
      {"go":"Amount","json":"amount","db":"amount","type":"float64","validate":"required","sql":"DECIMAL(12,2) NOT NULL"},
      {"go":"Status","json":"status","db":"status","type":"string","validate":"oneof=active suspended closed","sql":"VARCHAR(16) NOT NULL"}
    ],
    "operations": ["create","list","get","update","delete"],
    "list_filters": [{"go":"Status","form":"status","type":"string"}]
  },
  "acceptance_criteria": [
    "go build ./... clean", "go vet ./... clean",
    "rules_lint: layer-sql-boundary, handler-signature, repo-contract, fx-registration all pass",
    "FxRepo + FxHandler updated with correct annotations",
    "govalid validators regenerated",
    "POST /v1/pensions visible in /docs/v3Doc.json"
  ],
  "context_refs": ["@skill:repository-pattern", "@skill:handler-pattern", "@sop:validation"]
}
```

---

## 11. Greenfield scaffolding

### 11.1 The 10-step resource recipe

The common case, and the reason a backend agent is worth building: adding a resource to `n-api-template` is a fixed 7-file, mechanically derivable recipe.

1. **Planner** confirms the spec, asking at most four clarifying questions (fields+types, table name, route base, which list filters) and inferring the rest.
2. **Scaffolder** calls `resource_scaffold(spec)`, which emits from `text/template`:
   - `core/domain/pension.go` — struct with `json`+`db` tags, `ID`/`CreatedAt`/`UpdatedAt`
   - `db/pensions.sql` — DDL matching the domain, `serial4` PK, `timestamp DEFAULT now()`
   - `repo/postgres/pension.go` — Create/GetAll/GetByID/Update/Delete on `dblib.Psql` with `context.WithTimeout` and `pgx.RowToStructByName`
   - `handler/request.go` additions — Create/Update/`PensionIDUri`/`ListPensionsParams` with `validate`/`uri`/`form` tags
   - `handler/response/pension.go` — `PensionResponse` + `New*Response`/`New*sResponse` + the five operation envelopes
   - `handler/pension.go` — constructor, `Routes()` with `.Name(...)`, five CRUD methods on the DTO signature
   - `bootstrap/bootstrapper.go` — via `fx_wire`: repo into `FxRepo` (plain `fx.Provide`), handler into `FxHandler` (annotated)
3. **Verifier** runs `govalid_gen` → `go_build` → `go_vet` → `rules_lint` → `swagger_check`.
4. Result: a compiling, wired, swagger-visible resource on `agent/<session-id>`, presented as a reviewable diff.

**The LLM chooses the spec; the scaffolder writes the code.** That is what makes output deterministic, compiler-verifiable, and — critically — **byte-comparable against a golden snapshot** (§20.2), so template drift is caught instantly instead of discovered in review.

### 11.2 Greenfield service

`project_scaffold` lays down `go mod init <name>`, `main.go` with the swagger annotation block, `bootstrap/bootstrapper.go`, all seven `configs/*.yaml`, the `core/`/`handler/`/`repo/`/`db/`/`docs/` skeleton, and one working resource — so the service builds and serves on first run.

**Config secrets policy** (plan.md §6): the scaffolder emits configs with **empty** credential fields and a `README` note, never copies the reference template's committed credentials, and never invents placeholder values that look real.

### 11.3 Variants

Phase 1 ships `default-n-api`. Phase 3 adds the variants derived from `pao` (§12.3). All variants live in a separate maintenance repo under PR review, pinned by hash, with golden tests. The agent runtime never edits a template.

---

## 12. Brownfield and legacy migration (D3)

Most real IT 2.0 Go code looks like `pao-back-end-development`, not like `n-api-template`. This mode is where the near-term value is.

### 12.1 `legacy_audit` — the rule pack

Each rule is a concrete, verified difference between the two templates in this repository.

| Rule id | Signal in `pao` | Target state |
|---|---|---|
| `legacy-lib-generation` | imports `api-server`, `api-db`, `api-log`, `api-bootstrapper`, `api-validation` | the `n-api-*` generation |
| `legacy-routes-file` | `routes/routes.go` exists; `fx.Invoke(routes.Routes)` in `main.go`; `router.Group("/v1")` | per-handler `Routes()`; no `routes.go`; prefix via `SetPrefix`/`AddPrefix` |
| `legacy-gin-handler` | `*gin.Context` in a handler signature; `c.JSON(...)`; `ShouldBind*` | the DTO signature |
| `legacy-manual-validation` | `handler/validator.go`, `NewValidatorService`, `Fxvalidator`, direct `go-playground/validator` use | `validate` tags + `govalid` codegen |
| `legacy-response-helper` | `handleSuccess()` / a `handler/response.go` helper | `port.StatusCodeAndMessage` envelopes + `port.*Success` constants |
| `legacy-swaggo` | `swaggo/swag`, `docs/docs.go`, `swagger.json`/`swagger.yaml`, a `ginSwagger.WrapHandler` route | framework generation via `swagger.generation.mode`; `/docs/v3Doc.json` |
| `legacy-handmade-health` | a hand-written `/healthz` handler with an `isShuttingDown` atomic | `server.healthcheck.expose` + `path` in config |
| `legacy-fx-plain-handler` | handler in `FxHandler` via plain `fx.Provide` (no `fx.Annotate`/`fx.As`/`ResultTags`) | the annotated registration |
| `legacy-go-work` | `go.work` / `go.work.sum` present | single-module layout |
| `legacy-committed-artifacts` | `gin.log`, `cover.html`, `coverage`, `test_results.txt`, `*.pdf`, `preprod.yaml.txt` committed | `.gitignore` them |

### 12.2 Migration mode

Staged, PR-sized, resumable — modelled on the Oracle→Postgres skill set's structure (a persistent, machine-parseable plan file that later runs read).

1. **Inventory.** `legacy_audit` across all packages → write `.dakcoder/migration/plan.md`: every handler and repo, its classification (`MIGRATE` / `SKIP` / `ALREADY_MIGRATED` / `TEST`), the rules it violates, and a proposed order (leaf handlers first, shared helpers last).
2. **Confirm.** Present the classified list; the developer adjusts classification and ordering before anything is written. This step is not optional — a migration the developer did not order is a migration they will not review.
3. **Per-unit migration**, one handler at a time, each in its own isolated sub-context (§6.7) and its own commit:
   - split inline SQL out of the handler into `repo/postgres/` on `dblib.Psql`
   - replace `*gin.Context` + `ShouldBind` with the DTO signature
   - move request structs into `handler/request.go`, add `validate` tags, run `govalid_gen`
   - wrap responses in `port.StatusCodeAndMessage` (+ `MetaDataResponse` for lists)
   - move routes from `routes/routes.go` into `Routes()`, add `.Name(...)`
   - re-register in `FxHandler` with the annotation shape via `fx_wire`
   - verify with the full gate before committing
4. **Finalise.** Delete `routes.go` and `handler/validator.go` once empty; swap the library generation in `go.mod`; drop swaggo; move health to config; `go mod tidy`.
5. **Report.** `.dakcoder/migration/report.md` — what moved, what was skipped and why, what still fails.

The plan file is the durable state. A compaction, a restart, or a new session on Monday picks up from it.

### 12.3 Variants derived from `pao` (Phase 3)

`pao` is the only in-house source for these patterns, which is why D3 keeps it in scope rather than treating it as legacy-only.

| Variant | What it adds, as verified in `pao` |
|---|---|
| `n-api-with-grpc` | `connectrpc.com/connect`; `buf.yaml`/`buf.gen.yaml` v2 with `protoc-gen-go` + `protoc-gen-connect-go` at `paths=source_relative`; `proto/v1/*.proto` → `gen/proto/v1/…connect`; `grpc-server` `HandlerRegistry` + a `bootstrap.AddHandlers` invoke; `bootstrapper.FxGrpc`; `protovalidate` |
| `n-api-with-temporal` | `Fxtemporal`: client (host/port from config, namespace from `temporal-contracts`, optional TLS cert path), `worker.New` with `RegisterWorkflow`/`RegisterActivity`, `fx.Lifecycle` hooks for start/stop, and a logger adapter bridging the SDK to `api-log` |
| `n-api-with-ecms` | `bootstrapper.FxMinIO`; upload via `*multipart.FileHeader` / `[]*multipart.FileHeader` with `form:` tags; download via `port.FileResponse` in both byte-array and `io.ReadCloser` streaming form (both per `SOP.md`) |
| `n-api-with-migrations` | `golang-migrate/migrate/v4` + `tests/migration/NNNNNN_*.up.sql` alongside the raw `db/ddl/*.sql` |
| `n-api-with-integration-tests` | `testcontainers-go` + `testmain_test.go` + per-resource `*_test.go` against a throwaway Postgres |
| `n-api-with-cicd` | `Dockerfile`, `Jenkinsfile`, `Makefile` (build/test/package with `ciBuildVersion` etc.). **Note**: `pao`'s `Makefile` contains a `YOUR_SECRET_KEY=` placeholder — the variant must not carry it forward |

---

## 13. Debug mode

### 13.1 Trigger surfaces

A pasted compiler error, `go test` failure, panic, or FX dependency-graph error; a failed gate stage; or a code action on a `gopls`/`rules_lint` diagnostic in the editor (Part B §11).

### 13.2 Go / template failure-class playbooks

One JSON playbook per class — symptom, root cause, ordered fix steps, preferred tool, watch-outs, before/after — consulted by the Debugger **before** it attempts a fix. This is `postgen`'s best-proven idea: recurring failures get a known-good procedure instead of improvisation, and playbooks grow without re-tuning prompts.

| Class | Diagnosis | Fix |
|---|---|---|
| **Uber-FX** `missing dependencies for function` / `could not build arguments` / `not provided` | The handler or repo is not in `bootstrap/bootstrapper.go`, or the handler lacks `fx.Annotate` + `fx.As(new(serverHandler.Handler))` + `fx.ResultTags(serverHandler.ServerControllersGroupTag)` | `fx_wire` |
| **`pgx.ErrNoRows` surfacing as 500** | Handler not special-casing it as 404 per the SOP order | Apply the SOP error order |
| **Squirrel placeholder / arg-count mismatch** | A hand-rolled `sq.StatementBuilder` instead of `dblib.Psql`, or a `Columns`/`Values` length mismatch | Use `dblib.Psql`; align columns and values |
| **Validation not firing, or 400 on valid input** | `validate` tags changed without regenerating, request struct not in `handler/request.go`, or a hand-edited `*_validator.go` | `govalid_gen` from the handler directory |
| **Route missing from `/docs/v3Doc.json`** | Missing `.Name(...)`, `swagger.generation.mode` unset, or the handler not registered in `FxHandler` | `swagger_check` then fix |
| **`context deadline exceeded` on a query** | `db.QueryTimeoutLow` (2 s) used for something that needs `QueryTimeoutMed` (5 s) | Switch the config key, or fix the query |
| **`go mod tidy` keeps changing files** | An import references a module not in `require`, or a stray direct dep needs allow-list review | `go_mod tidy`; escalate new direct deps |
| **`import cycle not allowed`** | A layer boundary was crossed — usually `repo` importing `handler` | Apply the `layer-dto-boundary` fix |
| **Config key returns the zero value** | Key-path or casing mismatch, or the key is absent from the active env YAML | `config-key-exists` names the exact path |
| **Private module fetch fails** (`gitlab.cept.gov.in`) | `GOPRIVATE`/`GONOSUMDB` unset, or no git credential for the host | Part B's `Doctor` fixes this at source; the playbook names the exact env vars |
| **`connect` / protobuf drift** (variant) | `.proto` edited without `buf generate`; `gen/` stale | Re-run `buf generate`; check `paths=source_relative` |

Journal every resolution to `.dakcoder/bug-journal.md` locally, and — anonymised — to the shared KB so `search_docs` can retrieve it later. Weekly review of the journal is what drives prompt and playbook updates.

---

## 14. Knowledge base

### 14.1 The problem

`skill.md` is 2,339 lines — roughly 30k tokens. It cannot live in a system prompt (§6.1). Nor should it: 95% of it is irrelevant to any given turn.

### 14.2 Progressive disclosure

Adopt the **Agent Skills** shape from awesome-copilot: a small always-loaded index, with detail in bundled reference files fetched on demand.

```
packages/knowledge/
├── SKILL.md                    ~150 lines, always in the prompt: the contract in its shortest
│                               form, plus a table of what to fetch and when
└── references/
    ├── handler-pattern.md      skill.md §"Handler Pattern" + §"Routing Pattern"
    ├── repository-pattern.md   skill.md §"Repository Pattern"
    ├── request-dto.md          skill.md §"Request DTO Pattern" + SOP §Validation
    ├── response-dto.md         skill.md §"Response DTO Pattern" + port/response.go constants
    ├── domain-model.md         skill.md §"Domain Model Pattern" + §"Database Schema"
    ├── bootstrap-fx.md         skill.md §"Bootstrap Configuration" + SOP §bootstrapper
    ├── config-keys.md          the seven configs, key by key, with types and defaults
    ├── file-upload.md          SOP §"Handler with file upload" + §"File as a Response"
    ├── errors.md               skill.md §"Error Handling" + SOP §error order
    ├── worked-example.md       skill.md §"Complete Example Workflow" (the 10 steps)
    ├── legacy-patterns.md      the pao↔n-api delta table (§12.1)
    └── go-idiom.md             the machine-checkable subset of go.instructions.md
```

`search_docs(query)` retrieves chunks; `skill_ref(section)` fetches a named reference whole. The prompt cites sections by handle (`@skill:repository-pattern`), so provenance is preserved without the content being resident.

### 14.3 Retrieval

**Phase 1 is BM25-only.** The corpus is roughly 2,500 lines of highly structured, keyword-dense Go documentation — section headings, type names, function names, import paths. Lexical search is genuinely strong on that shape, it needs no index to maintain or invalidate, and it is auditable. Start there and prove retrieval quality against the eval suite before adding vectors.

**The vector upgrade is provisioned and cheap when wanted.** `qwen3-embedding` is live on the same endpoint under the same key and returns 1024-dimension vectors (verified). Embedding ~200 chunks once per release costs seconds. Switch it on by setting `DAKCODER_MODEL_EMBED=qwen3-embedding` and enabling the hybrid ranker — but only if BM25 measurably misses, which the eval suite will show. `qwen3-reranker` is also available on the same endpoint if precision, rather than recall, turns out to be the problem.

Either way the corpus hash goes in the cache key. The corpus is immutable between releases, so it is safe to cache hard.

**User code is deliberately not embedded server-side.** Under D2 the code is on the laptop; shipping it to a central vector store would undo the point. If semantic search over the user's own repo is wanted later, do it the way Cursor does but keep the index local: chunk with tree-sitter or `go/ast`, embed with a local model, store in a local SQLite/`sqlite-vec` index, and sync incrementally via a Merkle-tree diff so only changed files are re-embedded. Until then, `gopls` symbol search (§8.2) covers the navigation cases better than embeddings would, and for free.

### 14.4 Rules-to-doc coupling

Every rule cites a `skill.md` section. CI pins a hash of `skill.md`/`SOP.md` and **fails when they change without a corresponding rules review**. This is what stops the rules engine silently drifting from the template — and given that two divergences already exist between `skill.md` and the shipped `user` resource (plan.md §6), the check will earn its keep immediately.

---

## 15. Identity (D5)

### 15.1 What we are replacing

A single shared bearer token plus a client-supplied `X-Postgen-User` header. Anyone holding the token can claim any identity, so there is no attribution, no meaningful quota, and no audit trail worth the name. This is the last functional blocker to multi-developer rollout, and it is Part A's to close.

### 15.2 The flow

```
extension                     gateway                         gitlab.cept.gov.in
    │  verifier = random(32) → S256 challenge, state = random(16)
    │─ openExternal /oauth/authorize?client_id&redirect_uri&code_challenge&state&scope ─▶
    │                                                              │ user consents
    │◀── vscode://dop.dakcoder-go/auth/callback?code&state ─────────┘
    │─ POST /v1/auth/exchange {code, code_verifier, state} ─▶│
    │                                                        │─ POST /oauth/token ─▶
    │                                                        │◀─ gitlab access_token ─
    │                                                        │─ GET /api/v4/user ────▶
    │                                                        │─ GET /api/v4/groups ──▶
    │◀── {access_token (JWT, 15m), refresh_token (30d), profile, quota} ─
```

Design points, each for a reason:

- **No client secret in the extension.** Extension code is inspectable; a secret in it is an announcement, not a control. PKCE public client only.
- **The gateway does the token exchange, not the extension.** The extension never holds a GitLab token, and the gateway is the only party that reads group membership. Redirect URI is registered as the `vscode://` handler, with a loopback (`http://127.0.0.1:<port>/callback`) fallback registered too, because URI handlers are unreliable under Remote-SSH and remote containers.
- **Scopes**: `openid profile email read_api`. `read_api` is needed for `/api/v4/groups?min_access_level=…`; nothing needs write scope.
- **Our own JWT**, not GitLab's, for API calls: `sub` = GitLab user id, `preferred_username`, `dop_roles` (from group mapping), 15-minute expiry, refresh at `POST /v1/auth/refresh`, refresh token in VS Code SecretStorage (OS keychain-backed). Rotate refresh tokens on use.
- **Revocation is real**: every refresh re-checks the GitLab user's state, so a blocked GitLab account loses access within 15 minutes without any separate deprovisioning step.
- **RBAC from groups**: membership in `it-2.0-common/*` → `admin` (audit query, org dashboards, template and quota management); membership in `it-2.0/*` → `user` (own sessions, scaffold/modify/debug). Mapping is config, not code.
- **Write tools require a token minted ≤15 minutes ago** (inherited from v1 — a good control, keep it).
- **The IdP sits behind a port.** `IdentityProvider` with `exchange()`, `profile()`, `groups()`. GitLab is adapter one; an India Post SSO OIDC adapter is a config swap, and the tenant extractor stays a single function — exactly as `postgen` designed it.

### 15.3 Local mode and identity

In local mode `dakcoderd` runs on the developer's machine on loopback with a private token, which authenticates the *extension to its own runtime*. That is a different question from who the developer is. `dakcoderd` also holds a `dakcoder` JWT and presents it to the central services (model access, quota, usage ledger, KB).

### 15.4 The model proxy

This is the concrete answer to a problem the LiteLLM arrangement makes unavoidable: **the model API key is a single shared secret** (plan.md §4.5). If the local runtime held it, every laptop could spend the shared GPU budget with no ceiling and no attribution, and the entire quota model in §16 would be decorative.

So model traffic goes through us:

```
dakcoderd (laptop)                gateway (aiops)                  LiteLLM (ai.cept.gov.in)
   │ POST /v1/llm/chat/completions        │                                    │
   │ Authorization: Bearer <dakcoder JWT> │                                    │
   ├─────────────────────────────────────▶│ 1. verify JWT → sub, roles         │
   │                                      │ 2. quota check-and-reserve (§16)   │
   │                                      │ 3. inject Authorization + user=sub │
   │                                      ├───────────────────────────────────▶│
   │◀═══════ SSE passthrough ═════════════│◀═══════ SSE ═══════════════════════│
   │                                      │ 4. tee the final usage chunk       │
   │                                      │ 5. reconcile quota + write ledger  │
```

Design points:

- **Streaming is passed through, not buffered.** The gateway relays SSE chunks as they arrive and *tees* the final `usage` chunk into the ledger. Buffering to read usage first would destroy first-token latency and defeat the whole streaming design.
- **The prompt still leaves the machine.** That is inherent to using a hosted model and is unchanged from any other arrangement — what stays local is the *repository*: files, build output, git history, and the toolchain. Only the assembled prompt slices travel, exactly as before. This distinction has to be stated plainly in the developer-facing documentation, because "local-first" will otherwise be read as "nothing leaves", and that is not what it means.
- **Cost**: one extra internal hop, and it stays. Measure it and fold it into the first-token SLO (§18). If it proves material the answer is to move the gateway closer to the LiteLLM — colocation, warm keep-alive pools — never to push credentials outward (§16.6).
- **Fail closed.** If the gateway is unreachable, the local runtime cannot make model calls. That is the correct behaviour: an agent that keeps working when quota and audit are unavailable is precisely the hole this section closes. The extension surfaces it as a clear offline state (Part B §7.4), not an error dump.
- The proxy exposes only `chat/completions` and `embeddings`. It is not a general-purpose LiteLLM passthrough, and it never forwards a client-supplied `model` outside the configured role set — otherwise a developer could route to a model nobody has budgeted for.

---

## 16. Quota and metering (D6)

### 16.1 Model

| Limit | Default (placeholder — see plan.md §9 Q3) | Purpose |
|---|---|---|
| **Session window** `W` | 5 h, opened by the first agent turn when no window is open | The user-facing unit |
| Tokens per window `T_w` | 1,500,000 | Burst protection inside a window |
| Runs per window `R_w` | 40 | Catches runaway retry loops |
| **Sessions per rolling 7 days** `N` | 12 | The weekly strategic reserve |
| Tokens per rolling 7 days `T_week` | 12,000,000 | The real weekly ceiling |
| **Tokens per rolling 60 min** `T_h` | 600,000 | The hourly guard the owner asked for; catches one pathological task |

Every one of these numbers is a placeholder until Qwen capacity is measured. Ship them as config, publish them in `/v1/health`, and tune after a week of pilot telemetry. Getting the *mechanism* right matters more than the initial values.

### 16.2 Why rolling windows

They are what developers already understand from Claude Code and Cursor: a window opens on the first message, refills on a rolling basis, and a weekly cap sits behind it. Two limits, both explainable in one sentence each, and both visible in the status bar (Part B §7). A pure per-request rate limit is invisible until it fires; a rolling window can be *shown*.

### 16.3 Implementation

- **Redis is the hot path, Postgres is the ledger.** Redis holds the live counters; Postgres holds an append-only `usage_events` table (session, user, turn, model, prompt/completion/cached tokens, timestamp). Redis is rebuildable from Postgres on cold start, so a Redis flush is a performance event, not a billing event.
- **Two structures per tenant**, which is the pattern the metering literature converges on: a sorted-set sliding window for the *count* limits (`ZADD` timestamp, `ZREMRANGEBYSCORE` to trim, `ZCOUNT` to check) and a Lua-scripted GCRA/token bucket for the *token* limits, so check-and-consume is atomic under concurrency. Keyspace `q:{sub}:{window}`.
- **Session windows need a small state machine**, not just a counter: `window_open_at`, `window_expires_at`, `tokens_used`, `runs_used`, and a weekly `sessions_started` ring. Opening a window is itself a metered event.
- **Priority lanes.** Interactive turns outrank background work (compliance audits, nightly eval, migration batches). Background gets its own smaller bucket and is shed first under pressure.
- Keep `postgen`'s idempotency keys (RFC 8594: same body replays, different body → 409) and its `429` shape — `Retry-After`, `X-RateLimit-*` — and add `X-Quota-Window-Reset` plus a one-sentence human reason (C4).

### 16.4 Real token accounting (closing S18)

This is the fix that makes everything above true rather than aspirational.

1. **Send `stream_options: {"include_usage": true}` on every streaming call** (verified working, plan.md §4.2). Without it there is no usage chunk and no accounting — which is exactly why the frontend agent reserves a flat 4,096 tokens and never refunds.
2. **Reserve an estimate** before the call (assembled prompt token count plus `max_tokens`), then **reconcile from the response's `usage`**: `prompt_tokens`, `completion_tokens`, and — critically for reasoning modes — `completion_tokens_details.reasoning_tokens`, which is reported.
3. **Bill cached prefill at a discount, when we can see it.** `prompt_tokens_details.cached_tokens` is currently absent from this endpoint (plan.md §9 Q1), so implement the discount behind a feature flag that activates the moment the field appears, and default to 1.0× until then. The intent is worth keeping visible even while it is dormant: discounting cached prefill turns the quota system into an *incentive* where a session with good context discipline goes further than one without, aligning the quota model with the latency work.
4. **Meter reasoning tokens explicitly.** A thinking-on Planner turn can spend more output on reasoning than on the plan. Attribute it, show it per mode (§18), and make it a first-class line in the ledger — otherwise the cost of §4.4's on/off choices is invisible and the choices become superstition.
5. **Emit a `usage` SSE event per turn** (C5) so the extension can show a live meter and the developer can see the cost of a sloppy `repo_map`.
6. **Write every turn to the ledger**, so cost attribution per user, per team, per mode and per task class is a query rather than a guess.

### 16.5 Admin

Per-group quota tiers (GitLab group → tier), a `quota_grants` table for time-boxed boosts with a reason and an approver, and an admin dashboard showing window utilisation, top consumers, cache-hit rate by user, and shed background work.

### 16.6 Layering under LiteLLM

LiteLLM has its own quota machinery — virtual keys with `rpm_limit`/`tpm_limit` and budgets, per-user and per-team spend tables, and JWT→virtual-key mapping. Exploit it as a **second, independent ceiling**, not as a replacement for ours.

| Layer | Owns | Why it sits there |
|---|---|---|
| **Our gateway** | Session windows, weekly session counts, priority lanes, the developer-facing snapshot, the ledger | Product decisions with a UI (Part B §7). LiteLLM has no concept of a "session window", and the pre-flight estimate needs our task-class history. |
| **LiteLLM** | Hard per-key TPM/RPM and budget ceilings | A backstop that holds even if our gateway has a bug. Defence in depth on a shared GPU is worth the coordination. |

**The proxy (§15.4) is permanent.** Every LiteLLM credential — the shared key today, per-user virtual keys later — lives server-side and is attached by the gateway. Nothing below is an argument for handing a laptop a model credential; the proxy is the control that makes quota and audit unbypassable, and it survives every phase.

- **Phase 1 — one shared key, gateway-only.** The gateway is the sole holder and sets `user=gitlab:<sub>` on every request, so LiteLLM's own spend tables attribute correctly even before per-user keys exist. Works today, depends on nobody.
- **Phase 2 — per-user virtual keys, still gateway-held.** The gateway mints a short-TTL LiteLLM key per developer at sign-in via `/key/generate` (carrying `user_id`, `tpm_limit`, `rpm_limit`, and a budget from their role tier) and **keeps it in its own store, selecting the caller's key per request.** The developer's machine still sees only `/v1/llm/*` and its own JWT. The gain is a hard independent ceiling per user; the hop stays. Requires LiteLLM admin access — plan.md §9 Q2.
- **Phase 2 alternative — JWT→virtual-key mapping.** If the LiteLLM can be configured to accept our JWTs directly, the key-lifecycle problem disappears and our tokens carry the limits. This is the cleanest end state and the one to ask for when raising Q2. Note it does *not* imply removing the proxy either: the gateway still needs to see every call to enforce session windows, reconcile usage, and write the ledger.

If latency through the hop ever proves material, the fix is to move the gateway closer to the LiteLLM — colocate them, or keep a warm keep-alive pool — **not** to push credentials outward.

**Do not enable LiteLLM's response cache for our traffic** (§4.3). And keep our ledger authoritative for reporting: LiteLLM's spend tables are a useful cross-check, not the system of record, because they know nothing about sessions, modes, or task classes.

---

## 17. Security

Inherit `postgen`'s layered controls — they were built to be reviewable and they hold up. Deltas for Go and for this programme:

| Layer | Control |
|---|---|
| **Prompt injection** | Keep the always-on heuristic classifier (~25 patterns, 9 categories, severity-weighted, three-state verdict) with optional ML escalation. **Extend the corpus for Go**: injections in `//` and `/* */` comments, in struct tags, in `go:generate` directives, in `.sql` comments, and in `.proto` comments. Scan **file contents**, not just the task — Go source the agent reads is untrusted input. |
| **Tool allow-listing** | The model cannot emit a call to a tool absent from its schema. Mode filtering makes this a guarantee (§7.1). |
| **Approval gating** | Default `write_side`. Keep the `edit` decision — letting a developer correct the agent's args beats reject-and-re-prompt. Keep `autoApproveTrivialPatches` with its strict heuristics, and add Go-specific protected paths: `go.mod`, `go.sum`, `bootstrap/**`, `configs/**`, `db/**`, `*_validator.go`, `gen/**`. |
| **Command allow-list** | argv-only, no shell, shell-metacharacter refusal. Go allow-list: `go`, `gofmt`, `goimports`, `gopls`, `govalid`, `golangci-lint`, `govulncheck`, `git`, `buf`, plus the cross-platform coreutils shims. Blocked binaries return the working alternative. |
| **Workspace containment** | Every path resolved inside the workspace; absolute paths, drive letters and `..` rejected. Deny-list for Go: `.git`, `vendor`, `bin`, `gen` (generated), `GOMODCACHE`. |
| **Supply chain** | `go_mod` add is allow-list-enforced and approval-gated; `govulncheck` on greenfield and on dep changes; versions pinned; typosquat check against the allow-list. Sidecar binaries are checksum-pinned in the `.vsix` and verified at launch. |
| **Sandbox** | Keep the Podman → Docker → local-subprocess ladder with honest degradation reporting on `/v1/health`. In local mode the effective level is `local` with the allow-list floor. In server mode: rootless, `--cap-drop=ALL`, seccomp default-deny, `--network none` except a control-plane egress rule. Note the known production reality: the non-root service account cannot reach the Docker socket, so plan for `local` and treat the container as an upgrade. |
| **Model credential** | **The LiteLLM key exists only in the gateway's secret store.** Never in a repo, a config file, a built `.vsix`, a bundled wheel, or a developer's machine. Enforced at three points (§4.7): a startup assertion in the local runtime, deletion from the child's spawn env (Part B §4.6), and a CI grep of the shipped artefacts. All model traffic from local runtimes goes through `/v1/llm/*` (§15.4), which is what makes quota and audit unbypassable rather than advisory. Rotate the currently-circulated key before the pilot (plan.md §9 Q4). |
| **Secrets** | The agent never reads `.env`/secret files without explicit approval. `secrets-in-config` blocks *new* literals. **Pre-existing literals in `new-template/configs/*.yaml` — a MinIO access/secret pair, an Aadhaar client secret, a DB password, in both `config.yaml` and `config.prod.yaml` — are never echoed into a prompt, a log, a trace or a diff, and are redacted by the log sink.** Raise the rotation issue with the template owner independently (plan.md §9 Q7); it predates this programme and is not the agent's to fix, but the agent will read those files. |
| **Audit** | Append-only Postgres `events` (timestamp, user, session, tool, params hash, mutations, result hash) plus a WORM mirror. Every file access, command, and tool call attributable to a GitLab identity. Export to SIEM. |
| **Egress policy** | One org-level switch controls whether file content may reach the LLM (default: yes, redacted). Under D2 the code stays local, so the only egress is the prompt. |
| **Standards** | OWASP ASVS L2 for gateway + agent; OWASP API Top 10 on every endpoint; quarterly red-team covering injection and supply chain. |

---

## 18. Observability and SLOs

Reuse the OTel stack the template itself standardises (`api-trace`, `go.opentelemetry.io/otel/*`).

| Signal | Captures |
|---|---|
| **Traces** | extension → gateway → agent → tool → LLM, with span attributes for mode, step, tool, and — new for Go — `go_build` duration and result, `go_diagnostics` duration, and sidecar RPC duration |
| **Metrics** | **`prompt_tokens` per turn (p50/p95), by mode** · **`reasoning_tokens` per turn, by mode** (§4.4) · **token spend split by role** — coder/fast/embed, which is the evidence for switching on a second tier (§4.2) · **`cached_tokens`/`prompt_tokens` = prefix-cache hit rate** *when the field appears* · **`content: null` turns** (must be 0) · **LiteLLM hop latency** · compaction events per task · turns per task · tool latency by tool · `repo_map` duration by repo size · build pass rate · `rules_lint` violations by rule id · first-pass `verify:all` rate · session-window utilisation · shed background work · LLM 4xx/5xx (separating `UnsupportedParamsError`, which is always our bug) |
| **Logs** | structured JSON with correlation id, session, hashed user. Never raw file contents. Redacted Loki sink, batched from a daemon thread, failures counted and dropped — never raised into the agent loop. |
| **Cost** | tokens × rate (0 for self-hosted, tracked for chargeback), split by cached vs uncached prefill |

The leading metrics in that list are the ones that tell us whether §4–§6 worked, and three of them are alerts, not dashboards:

- **Prefix-cache hit rate below 60%** — the canary for someone breaking prefix stability (§6.4). Currently unmeasurable (plan.md §9 Q1); until the field appears, **alert on P95 prompt tokens per coder turn exceeding 24k instead**, which catches the same class of regression from the other side.
- **Any `content: null` turn** — a wasted turn, and a sign a thinking-on mode's `max_tokens` is too tight (§4.4).
- **Capability-probe failure** (§4.5) — the endpoint changed underneath us.

Dashboards: Operator (live sessions, queue, sandbox, Qwen health), **Context** (tokens/turn, cache hit rate, compactions, budget pressure), Quality (`verify:all` pass rate, retries per step, most-violated rules), Cost/quota, Security (failed authn, denied tool calls, injection blocks).

**SLOs** (initial, per §5.3): 99.5% gateway availability; P95 first-token ≤2.5 s warm; P95 prompt tokens per coder turn ≤24k; prefix-cache hit rate ≥80%; P95 per-tool round-trip ≤5 s; P95 `repo_map` ≤1.5 s; median "add a resource" wall-clock ≤90 s; first-pass `verify:all` ≥70% measured weekly.

**nginx configuration is part of the SLO**, not an afterthought. For `/coder/backend/`: `proxy_buffering off`, `proxy_cache off`, `proxy_http_version 1.1`, empty `Connection` header, `chunked_transfer_encoding on`, `proxy_read_timeout 600s`, **and no HTTP/2 on the SSE endpoint** — nginx's HTTP/2 handling regularly breaks long-lived streaming. Heartbeats every 5 s as real events (not comment lines), because some proxies ignore comment-only output — a `postgen` lesson learned from an actual user-reported bug.

---

## 19. Repository layout and packaging

### 19.1 What ships where

One monorepo, **three distributables**, and the split is a security boundary rather than a convenience.

`postgen` ships a single wheel containing everything — `server.py`, `quota.py`, the whole gateway — so every developer's laptop holds the full server codebase. That is benign there because nothing in it reads a secret. It is **not** benign here: our gateway contains the code that reads `DAKCODER_MODEL_API_KEY`, performs the GitLab token exchange, and signs JWTs. Shipping that to laptops would reduce §4.7's invariant from *"the code that reads the key is not on the machine"* to *"the key is not on the machine"* — a much weaker claim, and the first thing a security review will pull on.

| Distributable | Contains | Ships to | In the `.vsix`? |
|---|---|---|---|
| **`dakcoder-agent`** (Python wheel) | The agent loop, context manager (§6), prompts, tool router and Python-side tools, MCP clients for the sidecars, approval dispatcher, injection guard, local SQLite session store, and a **loopback HTTP+SSE endpoint** for the extension | developer laptops **and** the server (server mode) | **yes** |
| **`dakcoder-gateway`** (container image) | FastAPI app, GitLab OAuth exchange + JWT minting, quota + rolling windows (Redis), usage ledger (Postgres), **the model proxy `/v1/llm/*` that holds the LiteLLM key**, the KB index, capability probe, admin endpoints | the server only | **never** |
| **`dakcoder-shared`** (Python wheel) | Config, telemetry, tool JSON Schemas (C1), the SSE event envelope (C2), path-safety helpers | dependency of both | yes (as a dep) |
| **`gotools`** (native binaries) | Rules engine, scaffolders, `fx_wire`, `repo_map` (§8.1) | laptops and server | **yes**, per-platform, checksum-pinned |
| `gopls` | — | — | **no** — discovered or installed (Part B §4.5) |

Three clarifications this makes explicit:

- **"Everything except the gateway" is not quite right.** The local runtime does need a small HTTP + SSE server of its own — that is how the extension talks to it on loopback. But it is a *loopback endpoint*, not the gateway: no auth exchange, no quota enforcement, no ledger, no model key. Same framework, different responsibility.
- **The wheel is not the whole agent.** `gotools` is a Go binary and is packaged alongside the wheel, not inside it. The `.vsix` therefore carries: one wheel + its vendored dependency closure + one `gotools` binary per platform.
- **In server mode (Phase 3) the server runs both packages** — `dakcoder-gateway` in front, `dakcoder-agent` behind it against an ephemeral workspace. That is why the agent has to be deployable both ways, and why it stays a separate package rather than being folded into the gateway.

The payoff: §4.7's invariant becomes structural. There is no code path in the shipped wheel that reads a model credential, because that module is in a package the `.vsix` never contains. Keep the startup assertion and the CI grep anyway — belt and braces on the control that matters most.

### 19.2 Tree

```
dakcoder-go/
├── apps/
│   ├── gateway/                  # → dakcoder-gateway (SERVER ONLY, never in the .vsix)
│   │   ├── auth/                 #   GitLab OAuth exchange, JWT minting  (§15)
│   │   ├── quota/                #   rolling windows, Redis, ledger      (§16)
│   │   ├── llm_proxy/            #   /v1/llm/* — the ONLY reader of the model key (§15.4)
│   │   └── kb/                   #   search_docs corpus + index          (§14)
│   ├── agent/                    # → dakcoder-agent (WHEEL, ships in the .vsix)
│   │   ├── loop.py               # the single loop
│   │   ├── context.py            # ★ budget, caps, slice ledger, compaction  (§6)
│   │   ├── prompts/
│   │   │   ├── system.md         # ONE shared system prompt  (§6.4)
│   │   │   └── modes/            # planner.md scaffolder.md coder.md verifier.md debugger.md
│   │   ├── tools/                # router + Python-side tools + MCP clients
│   │   ├── loopback.py           # the small HTTP+SSE endpoint the extension talks to
│   │   │                         #   — NOT the gateway: no auth, no quota, no key
│   │   └── playbooks/*.json      # one per rule + per failure class  (§13.2)
│   ├── shared/                   # → dakcoder-shared (dependency of BOTH)
│   │   ├── config.py  telemetry.py
│   │   ├── schemas/              # tool JSON Schemas (C1)
│   │   └── envelope.py           # SSE event envelope (C2), path safety
│   └── sandbox-runner/           # server-mode toolchain container orchestration
├── gotools/                      # ★ the Go sidecar — native binary, NOT in the wheel  (§8.1)
│   ├── cmd/gotools/main.go       # MCP server over stdio
│   ├── rules/                    # AST + go/types checks, one file per rule  (§9.2)
│   ├── legacy/                   # legacy-pattern checks  (§12.1)
│   ├── scaffold/                 # text/template + embed.FS
│   ├── fxwire/                   # AST rewrite of bootstrapper.go
│   └── repomap/                  # go list / packages.Load
├── packages/
│   ├── knowledge/                # SKILL.md + references/  (§14.2)
│   ├── schemas/                  # tool JSON Schemas (contract C1) + generated types
│   └── scaffold-templates/       # text/template files mirroring skill.md
├── templates/                    # default-n-api + the pao-derived variants  (§12.3)
├── infra/                        # docker, rancher, nginx, otel collector, dashboards
├── docs/                         # ARCHITECTURE TOOL-CATALOG PROMPTS RULES ONBOARDING RUNBOOKS
└── tests/
    ├── golden/                   # scaffold snapshots (§20.2)
    ├── fixtures/                 # per-rule positive + negative Go fixtures (§20.3)
    ├── eval/                     # task → oracle datasets (§20.1)
    ├── perf/                     # ★ context-budget and latency regression tests (§20.5)
    └── load/                     # k6
```

---

## 20. Testing and evaluation

### 20.1 Eval suite

Machine-graded, not eyeballed — inherit `postgen`'s harness wholesale, including its three execution modes: `live` (real model), `mock` (apply a known fix, to validate the oracle itself), and `none` (negative control, to prove the oracle fails on an untouched starter). That third mode is what stops a green suite from being a lie.

50 tasks at MVP → ~200. Categories: scaffold-resource, scaffold-service, add-endpoint, add-list-filter, add-file-upload, refactor-to-compliance, **legacy-audit**, **migrate-handler**, fix-fx-wiring, fix-layer-boundary, fix-validation, debug-compile-error, debug-pgx-error, debug-fx-graph.

Each task: a starter repo tarball, a natural-language instruction, and an oracle — `verify:all` clean plus a custom assertion (`files_contain` / `files_not_contain` regexes, `file_exists`, `mutation_count` bounds, `session_status`, and e.g. "route `POST /v1/pensions` present in `/docs/v3Doc.json`"). Run on every prompt or model change. **Curate from real IT 2.0 Go merge requests** of the last six months — synthetic tasks pass too easily.

### 20.2 Golden snapshot tests

`resource_scaffold` output must be byte-equal to a golden snapshot modulo resource name. The deterministic scaffolder makes this feasible, and it is the fastest possible detector of template drift.

### 20.3 Rules-engine fixtures

Every rule in §9.2 gets a positive and a negative Go fixture. Two blanket assertions:

- The shipped `new-template` `user` resource **passes every rule**. If it does not, the rule is wrong, not the template.
- The shipped `pao` handlers **trigger the expected `legacy_audit` rules**, and no others.

### 20.4 Offline suites

Mirror `postgen`'s 18 suites (~600 assertions, no LLM, no network, no Docker) and add: context manager (budget arithmetic, caps, slice ledger, compaction triggers), prefix stability (a unit test that **fails if `messages[0..k]` is ever mutated**), sidecar RPC, token reconciliation, and session-window state transitions.

### 20.5 Performance regression tests

New, and non-negotiable given §5. Assert in CI, not in a dashboard:

- `repo_map` on a synthetic 5,000-file repo containing a 100,000-file `vendor/` completes in **<2 s** (this is the S2 regression guard)
- A scripted 25-turn task holds P95 prompt tokens **≤24k**
- Total prefill for that task stays **≤180k tokens**
- Prefix-cache hit rate against a local vLLM **≥80%**
- `repo_map` reads each file at most once (S3 guard, via an I/O counter)

### 20.6 Load and security

k6 at 50 concurrent sessions with sustained tool calls and sandbox spin-up. A prompt-injection corpus including injections embedded in Go comments, struct tags, `go:generate` directives, and `.proto` comments. A supply-chain attempt to coax `go_mod` into adding a non-allow-listed or typosquatted module.

---

## 21. Risks

| Risk | L | I | Mitigation |
|---|---|---|---|
| **The context work is deferred as "optimisation" and the agent ships slow** | **High** | **High** | §6 is Phase 0 scope with CI gates (§20.5), not a Phase-3 nice-to-have. The measured evidence in §5 is the argument to point at when it is proposed as a later phase. |
| **Prefix caching turns out to be off at the vLLM layer** | **Med** | **High** | Cannot be detected from here (plan.md §9 Q1 — highest-priority operational question). Mitigation is that §6 is correct either way: a 32k cap and stable prefixes cut absolute prefill regardless, and they are what will let us prove the delta the day the flag is confirmed. |
| Someone breaks prefix stability | Med | High | The §20.4 mutation test (fails if `messages[0..k]` is mutated); the prompt-token-growth alert (§18) |
| **Reasoning tokens silently eat the output budget** | **Med** | **Med** | §4.4's three hard rules; `content: null` treated as a typed error with retry; per-mode `reasoning_tokens` metering and alerting (§18) |
| **The endpoint changes under us** — LiteLLM upgraded, model swapped, `drop_params` flipped, chat template changed | Med | High | The §4.5 capability probe at startup and in CI. Without it this failure mode presents as inexplicable agent misbehaviour rather than an error. |
| **The shared model key leaks to laptops**, making quota unenforceable | Med | High | §15.4's proxy is the structural fix — the key is never packaged or distributed. Part B §13 forbids a client-side key setting outright. Rotate the currently-circulated key (plan.md §9 Q4). |
| Endpoint bottleneck under load, shared with other tenants | High | High | Connection pooling; the 32k cap cuts prefill demand directly; thinking-off on the highest-volume modes; `search_docs`/`repo_map` caching; priority lanes; background work off-peak; LiteLLM per-key ceilings as a backstop (§16.6). **Answering plan.md §9 Q3 is the real mitigation.** |
| `gopls` memory or instability on large modules | Med | Med | Memory cap, idle timeout, supervised restart, graceful fallback to an incremental `go build` + `search_repo` with a visible slow-mode note (§8.3, §8.4) |
| Agent emits code that compiles but is subtly wrong | Med | High | Verifier + `rules_lint` + eval suite + human diff approval + one-click revert to git HEAD |
| Rules drift from `skill.md`/`SOP.md` | High | Med | Rules cite sections; CI pins the doc hash and fails on divergence (§14.4). Two divergences already exist — the check has immediate work to do. |
| Uber-FX wiring errors frustrate users | Med | Med | `fx_wire` is AST-based; wiring is never hand-generated; a dedicated FX playbook |
| Private module fetch fails | Med | High | D2 makes the developer's own credentials the answer; Part B's `Doctor` verifies `GOPRIVATE`/`GONOSUMDB`/git credentials before the first task |
| `govalid` / toolchain version skew | Med | Med | `Doctor` pins and verifies versions; the sidecar reports what it found; bundle known-good versions in the server-mode image |
| Irreversible operations | Med | High | No `sql_migrate` in MVP; approval gates on `delete_file`, `go_mod` add, history rewrites; `revert` restores to git HEAD |
| Adoption stalls because rules feel punitive | Med | Med | Every violation cites its `skill.md` section and offers a fix; an "explain this rule" command; `go-idiom` is advisory not blocking; out-of-scope legacy violations never block |
| Sensitive code leaks via logs or traces | Low | High | Redaction, per-org egress switch, transcript TTL, audit alerts on unusual export volume. D2 keeps the code local by default. |
