# dakcoder-go — Program Index & Shared Contracts

> **Project**: IT 2.0 **Backend** Coding Agent (`dakcoder-go` — name pending §9 Q1)
> **Audience**: India Post IT 2.0 (DOP) developers building Go REST APIs on `n-api-template`
> **Scope**: Go / Golang backend only.
> **Status**: Draft v2 — supersedes the single-file v1 plan
> **Date**: 25 August 2026

This file is the **shared context and index**. The plan proper is split in two, because the two halves ship on different cadences, are owned by different skills, and bind to each other only through the contracts in §7:

| Document | Covers | Owner |
|---|---|---|
| **[plan-backend-agent.md](plan-backend-agent.md)** — Part A | The agent server: LLM layer, context/token engineering, agent loop, tool catalog, Go sidecar, rules engine, scaffolders, auth/quota server side, observability, eval | Platform / agent team |
| **[plan-vscode-extension.md](plan-vscode-extension.md)** — Part B | The VS Code extension: local runtime, Go toolchain preflight, GitLab OAuth, quota UX, chat/diff/approval UI, Go-specific editor surfaces, packaging | Client team |

Read §1–§8 here first. Everything in Part A and Part B assumes it.

---

## 1. What we are building, in one paragraph

A coding agent that knows the `n-api-template` contract — the layered `domain`/`port`/`repo`/`handler` split, the Uber-FX composition root, the `govalid` request-DTO pipeline, the `dblib.Psql` repository idiom, the `port.StatusCodeAndMessage` response envelope — and turns *"add a `Pension` resource with CRUD and a status filter"* into a compiling, FX-wired, swagger-visible set of Go files, verified by the compiler and a static template linter before a human ever sees the diff. It runs on India Post infrastructure against the self-hosted Qwen endpoint, is driven from a VS Code extension, and by default executes entirely on the developer's own machine.

It is a **sibling** of `postgen`, the frontend agent, which is in production. We fork its spine rather than rebuild it. The genuinely new engineering is: the Go rules engine, the deterministic scaffolders, the legacy-migration rule pack, real identity and quota, and — the biggest single lever — **context and token discipline**, which is where `postgen` is currently slow.

---

## 2. Locked decisions

These were confirmed with the programme owner on 25 Aug 2026. Part A and Part B are written against them; changing one is a re-plan, not a tweak.

| # | Decision | Rationale |
|---|---|---|
| **D1** | **Fork `postgen` (Python/FastAPI) for the spine; write the Go-native analysis as a separate Go binary (`gotools`) exposed over MCP/stdio** | ~10k lines of gateway/sessions/quota/sandbox/injection-guard/resume/telemetry are language-agnostic and production-tested. But layer-boundary and signature checks need `go/types`, not regex — that must be Go. Two processes, one clean seam. |
| **D2** | **Local-first is the primary execution model**; server-side workspace is a fully-specified opt-in mode, not an afterthought | DOP source never has to leave the laptop, which is the objection that killed Copilot/Cursor for this use case. It also makes the `GOPRIVATE`/`gitlab.cept.gov.in` module-fetch problem disappear — the developer's own git credentials already work. |
| **D3** | **The old `pao-back-end-development` template is handled in two phases**: MVP recognises and migrates its patterns; a later phase scaffolds its extra surfaces (gRPC/Connect, Temporal, ECMS/MinIO, migrations, integration tests, CI) as template variants | Most real IT 2.0 Go code today looks like `pao`, not like `n-api-template`. Migration is where the near-term value is. Scaffolding gRPC and Temporal from day one would triple the MVP. |
| **D4** | **Two plan documents + this index** | Part A and Part B have different reviewers and different release trains. |
| **D5** | **Identity comes from self-hosted GitLab** (`gitlab.cept.gov.in`) via OAuth 2.0 Authorization Code + PKCE; group membership drives RBAC | It is demonstrably live — every template and both agents are hosted on it. No new infrastructure, no waiting on a central SSO decision. The IdP is behind a port so India Post SSO can replace it as config. |
| **D6** | **Quota is Claude-style rolling windows**: a session window opened by the first turn, session and token caps inside it, plus rolling hourly and weekly ceilings | Predictable for developers, industry-standard, and it protects a single shared GPU far better than a per-request rate limit. |
| **D7** | **One model for every task — `Qwen3.8-27B` via the LiteLLM proxy at `https://ai.cept.gov.in/v1`.** The tiering is built as configuration from day one (`DAKCODER_MODEL_CODER` / `_FAST` / `_EMBED`), all three defaulting to the same model, so a second tier is an env change and not a code change. | Simplest operational story now. The seam is free to build and expensive to retrofit — and the endpoint already serves `Phi-4-mini-instruct` and `qwen3-embedding` on the same key, so the tiering can be switched on later without new infrastructure (§4.2). |

---

## 3. Source inputs

| Source | Path | What it contributes |
|---|---|---|
| **New reference template** | `new-template/` (module `pisapi`, `go 1.25.0`) | The canonical contract. `skill.md` (2,339 lines) is the pattern reference; `SOP.md` (244 lines) is the migration standard; the `user` resource is the golden fixture every rule must pass. |
| **Old production service** | `pao-back-end-development/` (module `gotemplate`) | The brownfield corpus, and the source of every "legacy pattern" rule. Also the only place in-house where gRPC/Connect, Temporal, ECMS/MinIO, golang-migrate, testcontainers, Dockerfile/Jenkinsfile/Makefile patterns exist. |
| **Frontend agent** | `D:\desktop\dakcode-vsextension` (`postgen` 0.4.0 / wheel 0.1.3) | The spine we fork, the extension we fork, and — read honestly — the performance case study. See §5, §6. |
| **awesome-copilot** | `D:\desktop\awesome-copilot` | `instructions/go.instructions.md` (373 lines of idiomatic-Go rules to sit *under* the template rules); `instructions/go-mcp-server.instructions.md` (the Go MCP SDK patterns we build `gotools` with); the **Agent Skills** folder format (`SKILL.md` + `references/`) as the progressive-disclosure pattern for our 2,339-line knowledge base; the 8-skill Oracle→Postgres migration set as the structural template for our `pao`→`n-api` migration mode. |

---

## 4. Runtime facts

### 4.1 The model endpoint

`dakcoder-go` does **not** talk to vLLM directly. It talks to a **LiteLLM proxy**, which fronts several vLLM instances. This is a different arrangement from the frontend agent's (which points straight at `127.0.0.1:8011`) and it changes what we control — see §4.4.

```yaml
base_url:  https://ai.cept.gov.in/v1        # LiteLLM proxy
api_key:   ${DAKCODER_MODEL_API_KEY}        # NEVER a literal in code, config, or the .vsix — §4.5
model:     Qwen3.8-27B
```

### 4.2 Verified capabilities

Probed against the live endpoint on 25 Aug 2026. Every row below was tested, not assumed.

| Capability | Result |
|---|---|
| Backend | vLLM **0.23.0** behind LiteLLM; distinct `system_fingerprint` per model ⇒ separate vLLM instances |
| `max_model_len` | **262,144** (surfaced verbatim in the 400 on an over-large `max_tokens`) |
| **Native tool calling** | **Works.** `finish_reason: "tool_calls"`, well-formed `tool_calls[]`, ids shaped `chatcmpl-tool-<hex>`. No text-parsed ReAct fallback needed. |
| **`Qwen3.8-27B` is a reasoning model** | By default it emits `reasoning_content` and **`content: null`**. A `max_tokens` too small to finish reasoning yields a completely wasted turn (`finish_reason: "length"`, nothing usable). **This is the most consequential finding of this revision** — see §4.3. |
| **Reasoning is switchable** | `chat_template_kwargs: {"enable_thinking": false}` works. Measured on one identical tool-calling request: thinking **on** = 340 prompt + 88 completion; **off** = 304 prompt + 52 completion, *same tool call*. The thinking chat template alone adds ~37 prompt tokens. |
| `reasoning_effort` | **Rejected** — LiteLLM 400s with `UnsupportedParamsError`. `drop_params` is **off** on this proxy, so unknown params fail loudly rather than being dropped. Send only known-good params and probe capabilities at startup. |
| `stream_options: {"include_usage": true}` | **Works** — a final chunk carries `usage`, including `completion_tokens_details.reasoning_tokens`. This closes the frontend agent's total-blindness-to-token-usage gap (§5). |
| **`prompt_tokens_details.cached_tokens`** | **Absent.** Either vLLM lacks `--enable-prompt-tokens-details` or LiteLLM strips it. **We currently cannot measure prefix-cache hits.** Open question §9 Q2. |
| `user` field | Accepted — usable for per-request attribution. |
| Also on the same endpoint & key | `Phi-4-mini-instruct` (tested: good, terse summaries — the natural future `fast` tier), `qwen3-embedding` (tested: 1024-dim), `qwen3-reranker`, `Qwen3.6-35B`, `Gemma-4-26B-A4B`, `Llama-3-Patronus-Lynx-8B-Instruct`, `sarvam-translate` |
| `/v1/models` | **Does not list `Qwen3.8-27B`**, yet the model serves correctly. Do not use the model list for capability discovery or preflight validation — it is incomplete. |

### 4.3 Reasoning is a per-mode decision

Because reasoning is a request parameter, it becomes a free per-mode tuning knob — and getting it wrong is expensive in both directions. Detail and rationale in [Part A §4.4](plan-backend-agent.md).

| Mode | `enable_thinking` | Why |
|---|---|---|
| Planner | **on** | One turn, and plan quality is what the whole run depends on |
| Debugger | **on** | Ranking hypotheses against evidence is exactly the task reasoning is for |
| Scaffolder / Coder / Verifier | **off** | Mechanical edits and tool dispatch. Measured: identical tool call for ~20% fewer tokens and no reasoning latency. |

Hard rule: **any thinking-on call gets `max_tokens ≥ 2048`.** A truncated reasoning block returns `content: null` and burns the turn for nothing.

### 4.4 What the LiteLLM hop costs us

The v1 plan assumed we owned the vLLM launch command. We do not. Consequences:

- **We cannot set `--enable-prefix-caching`, `--enable-prompt-tokens-details`, `--tool-call-parser`, or `--kv-cache-dtype`.** Tool calling demonstrably works, so the parser is right. Prefix caching is unverifiable from here and is the single largest available latency win — hence §9 Q2, which is now the highest-priority operational question in the programme.
- **LiteLLM's own response cache must be off (or scoped) for agent traffic.** It is an exact-match cache; returning a cached completion for a near-identical agent context would be a correctness bug, not an optimisation.
- **LiteLLM brings quota features we should exploit rather than duplicate**: virtual keys with `rpm_limit`/`tpm_limit`/budgets, per-user and per-team spend tracking, and JWT→virtual-key mapping. See [Part A §16.6](plan-backend-agent.md) for how this layers under our session-window model instead of competing with it.
- **One extra network hop.** Both hosts are internal, so this should be small, but it must be measured and included in the first-token SLO.

### 4.5 The API key is a shared secret

The key supplied is a **single shared LiteLLM key**. Two non-negotiables follow:

1. **It never appears in a git-tracked file, a plan document, a built `.vsix`, or a developer's machine.** It lives in the server's secret store and is read as `DAKCODER_MODEL_API_KEY`.
2. **Under D2 (local-first), model traffic is proxied through our gateway** — the local runtime authenticates with the developer's `dakcoder` JWT and the gateway holds the LiteLLM key. Shipping the shared key to laptops would be an unmetered bypass around the entire quota system, and would make per-user attribution impossible. [Part A §15.4](plan-backend-agent.md) specifies this; it is the reason it is specified at all.

Because the key has been circulated in plain text to author this plan, it should be **rotated** before the pilot, and the replacement issued as a server-only secret.

### 4.6 Gateways and shared state

```yaml
frontend_gateway: https://aiops.cept.gov.in/coder/frontend/   # existing, for reference
backend_gateway:  https://aiops.cept.gov.in/coder/backend/    # to be provisioned
postgres: cept-aiops-postgres   # per-app DB + login role convention
redis:    cept-aiops-redis      # authenticated; frontend holds db 1 — take a different index
```

**Launcher lesson to inherit**: unset the corporate `HTTP(S)_PROXY` and set `NO_PROXY` for internal hosts. The frontend agent's proxy misconfiguration cost it real latency until this was fixed.

### 4.7 Template contract

The `n-api-*` library generation, not the older `api-*` one:

```
gitlab.cept.gov.in/it-2.0-common/n-api-bootstrapper   v0.0.14   bootstrapper.New().Options(...).WithContext(ctx).Run()
gitlab.cept.gov.in/it-2.0-common/n-api-server         v0.0.17   serverHandler.Base/Handler, serverRoute.Route/Context
gitlab.cept.gov.in/it-2.0-common/n-api-db             (dblib)   dblib.Psql builders + Insert/SelectOne/SelectRows/Update/Delete
gitlab.cept.gov.in/it-2.0-common/n-api-log            v0.0.1    log.Error/Info(ctx, fmt, ...)
gitlab.cept.gov.in/it-2.0-common/n-api-validation     v0.0.3    govalid codegen
gitlab.cept.gov.in/it-2.0-common/api-config           v0.0.17   *config.Config
gitlab.cept.gov.in/it-2.0-common/api-errors                     apierrors.HandleErrorWithStatusCodeAndMessage
github.com/Masterminds/squirrel + github.com/jackc/pgx/v5 + go.uber.org/fx
```

---

## 5. What changed in the frontend agent since v1

v1 was drafted ~10 July 2026 against an earlier `postgen`. The current state (work summary dated 27 July 2026, extension 0.4.0) differs materially.

| Area | v1 assumed | Actual today |
|---|---|---|
| **Execution model** | "Server sends tool calls; the extension executes them locally" | **No such split exists.** *Local* mode = the extension boots an entire Python gateway **on the developer's machine** from a bundled wheel and talks to it on loopback. *Cloud* mode = the gateway runs server-side against a **server-side workspace mirror** (`POSTGEN_WORKSPACE_ROOT` maps the client's folder *name*). Local is the default. |
| Tool count | unspecified | **19** tools, schemas published at `GET /v1/tools` |
| Rules engine | "13 `check-*.js` scripts ported" | **16** checks in pure Python (`rules.py`, 1,126 lines); 3 are new and were *born from observed agent misbehaviour*: `check-dop-components`, `check-mvc-structure`, `check-stack` |
| Playbooks | proposed | **10** curated JSON fix-recipes, consulted by the Debugger via a `playbook` tool |
| Greenfield | "a template_scaffold tool" | **66-file** `default-it20` scaffold producing a deployable repo in one call |
| Roles | Planner/Coder/Reviewer/Debugger/Scaffolder/Doc-Scribe | **Three** prompts (Planner → Coder/Reviewer → Debugger). Reviewer is folded into a runtime-forced verify gate. Scaffolder and Doc-Scribe were never built. |
| Recovery | "resume sessions" | Resume **plus** multi-turn conversation follow-ups, in-composer message **steering**, one-click **revert to git HEAD**, RFC-8594 **idempotency keys**, explicit abort |
| Robustness | — | Transient 429/502/503/504 retried 3× with backoff; upstream HTML error pages stripped to one sentence; cancel honoured **mid-stream**; cancel checked before **each** tool call in a batch; Debugger no-progress detector (same call 3 turns → stop); independent Debugger turn cap (12); Planner forced-plan on its last turn; runtime **adopted across VS Code windows** via a lock file so two windows share one SQLite DB |
| Observability | proposed | OTel spans, opt-in **Grafana Loki** sink with redaction + batching, `/v1/health` reporting sandbox degradation + prewarm + cache + quota, `/v1/quota` |
| Quality | proposed | **18** offline suites (~600 assertions, no LLM/network/Docker), **20**-task eval harness with machine-checked oracles in 3 modes (live/mock/none), 2 CI workflows |
| Deployment | proposed | **In production** on the aiops host: nginx `location /coder/frontend/` with `proxy_buffering off` and 600 s timeouts; dedicated Postgres DB + role; authenticated Redis db 1; direct vLLM on loopback |
| Sandbox | "rootless Podman/Docker" | Degrades to `sandbox=local` in production — the non-root service account cannot reach the Docker socket. The allow-list + no-shell floor still applies. |
| **Auth** | "India Post SSO, PKCE" | **Not built.** A single shared bearer token (`POSTGEN_GATEWAY_TOKEN`) plus an `X-Postgen-User` header that the client sets itself. Anyone holding the token can claim any identity. "Sign In" is an input box that stores that token. This is the only remaining functional blocker to multi-developer rollout. |

---

## 6. Corrections to the v1 plan

Facts v1 asserted that the templates contradict. Each of these would have produced a **wrong rule** or a **wrong tool**.

| v1 asserted | Verified reality | Consequence |
|---|---|---|
| Repos must call `.PlaceholderFormat(sq.Dollar)` | The template uses `dblib.Psql.Insert/Select/Update/Delete` — a pre-configured Squirrel builder that already carries dollar placeholders. Raw `sq.*` builders do not appear. | Rule `repo-contract` must require **`dblib.Psql`**, and *flag* a hand-rolled `sq.StatementBuilder`. |
| Create-request DTOs need a `ToDomain()` converter | No `ToDomain()` exists anywhere. `handler/user.go` passes fields positionally: `h.svc.CreateUser(sctx.Ctx, req.FirstName, req.LastName, req.Age, req.City, req.Email)`. | Drop the `ToDomain()` requirement. (Optionally propose it as a *future* template improvement — but the agent must generate what the template does, not what we wish it did.) |
| Secrets in configs should be `${ENV_VAR}` placeholders | There is **no env-var interpolation** in the configs, and `configs/config.yaml` **and `config.prod.yaml`** both ship live-looking credentials: a MinIO access/secret key pair, an Aadhaar `CLIENT_SECRET`, and a DB password. | The `secrets-in-config` rule stands, but reframed: the agent must **never propagate or echo** these values, must never add new literals, and should surface a one-time advisory. **Separately and urgently: these credentials are committed to a git repository and should be rotated.** See Part A §17. |
| `swagger_gen` regenerates `docs/v3Doc.json` | Swagger is produced by the **framework at build/run time**, gated by `swagger.generation.mode: "build"` in `configs/config.yaml`, and served from `/docs/v3Doc.json`. There is no generator CLI. (The *old* template used swaggo → `docs/docs.go` + `swagger.json/yaml` + a `ginSwagger` route.) | Replace the `swagger_gen` tool with **`swagger_check`**: confirm the route carries `.Name(...)`, confirm `swagger.generation.mode`, and optionally boot the app to diff `/docs/v3Doc.json`. Add a `legacy-swaggo` migration rule. |
| Model endpoint `aiops.cept.gov.in/qwen/v1`, model `Qwen3.6-27B` | Superseded entirely: `dakcoder-go` uses the **LiteLLM proxy** at `https://ai.cept.gov.in/v1` with model `Qwen3.8-27B` (§4). The frontend agent's endpoint is a *different* deployment and is not ours to inherit. | §4 — and it is a reasoning model, which v1 had no concept of. |
| Libraries `api-server` / `api-db` / `api-bootstrapper` | New template is the `n-api-*` generation; **`api-*` is the legacy generation and is itself a migration signal** | Turns a naming detail into a rule: `legacy-lib-generation`. |
| `govalid` source ambiguous ("skill.md mentions a `twpayne` variant") | SOP.md is unambiguous: `go install gitlab.cept.gov.in/it-2.0-common/n-api-validation/cmd/govalid@latest`, then `govalid ./request.go` **from the handler directory**, with all request structs in `handler/request.go` | Open question **closed**. |
| `GOPRIVATE`/sandbox module fetch "blocks server-mode builds" | Under **D2 (local-first)** the developer's own git credentials fetch the private modules. The problem only returns in the opt-in server mode. | Open question **downgraded** from blocker to Phase-3 scope. |
| Handler methods always take a request DTO | Routes with no input use `_ struct{}`: `func (h *UserHandler) ListUsers(sctx *serverRoute.Context, _ struct{}) (*resp.UsersListResponse, error)` | Rule `handler-signature` must accept `_ struct{}`. |
| Response DTOs carry timestamps | `skill.md` prescribes `CreatedAt`/`UpdatedAt` formatted `"2006-01-02 15:04:05"`; the shipped `UserResponse` **omits both**. The reference resource does not follow the reference doc. | The rule follows `skill.md` (timestamps present, string-formatted) and the mismatch goes to the template council as a defect. Golden tests pin `skill.md`. |

---

## 7. Shared contracts

These five seams are the entire interface between Part A and Part B. Both documents bind to them; neither may change one unilaterally.

**C1 — Tool schema.** OpenAI function-calling JSON Schema, published at `GET /v1/tools`, versioned. Rules: ≤6 parameters, hand-written schema, description ≤200 characters written as an *instruction* to the model, every result carries `{ok, content, mutations[]}`.

**C2 — SSE event envelope.** `event: <type>` + JSON `data`. Types: `turn_start`, `assistant`, `assistant_delta`, `tool_call`, `tool_pending`, `tool_result`, `plan`, `gate`, `usage`, `quota`, `finish`, `error`, `heartbeat`, `end`. Additive only; unknown types must be ignored by the client. `assistant_delta` is transient (never persisted) and **coalesced** server-side (Part A §5, fix S11).

**C3 — Auth.** Extension holds no client secret. It performs PKCE against GitLab, posts `{code, code_verifier, state}` to `POST /v1/auth/exchange`, and receives a `dakcoder` JWT (15 min) + refresh token (30 d). Refresh at `POST /v1/auth/refresh`. `sub` = GitLab user id; `dop_roles` from GitLab group membership. Write-side tools require a token minted ≤15 min ago.

**C4 — Quota.** `GET /v1/quota` returns the session-window snapshot (§ Part A §16 shape). Every `429` carries `Retry-After`, `X-RateLimit-*`, `X-Quota-Window-Reset`, and a one-sentence human reason. The extension shows the snapshot in the status bar and pre-flights before starting a run.

**C5 — Context budget.** The server owns the budget and is authoritative. It emits a `usage` event per turn (`prompt_tokens`, `completion_tokens`, `cached_tokens`, `budget_used_pct`) and a `gate {kind: "compaction"}` event when it compacts. The extension only *displays* these — it never trims context itself.

---

## 8. Programme roadmap

Phases are shared; the exit criteria name which part owns them.

| Phase | Weeks | Part A | Part B | Exit criteria |
|---|---|---|---|---|
| **0 — Foundation** | 1–2 | Fork the spine; strip frontend tools; provision `/coder/backend/` + DB + Redis index; chunk `skill.md`/`SOP.md`; **build the capability probe, the context manager, and the LLM client with per-mode reasoning control** | Fork the extension; rename; vendor wheels into the `.vsix`; `Doctor` for the Go toolchain | Capability probe green in CI; `repo_map` P95 ≤1.5 s on a repo with `vendor/` present; first-run local bootstrap works offline behind the proxy; prefix-cache hit rate measurable (or §9 Q1 escalated with a named owner) |
| **1 — MVP** | 3–8 | `gotools` v1 (rules engine + `resource_scaffold` + `fx_wire`); gopls MCP wired; one system prompt + mode overlays; Planner/Coder/Verifier; **real token accounting**; GitLab OAuth; rolling-window quota | Sign-in, new task, streaming chat, diff approval, resume, quota UX, token meter | `resource_scaffold` → `verify:all` clean unattended ≥70%; 20-task eval green; 5–10 pilot developers active; every session attributable to a GitLab identity |
| **2 — Greenfield, Debug, Legacy** | 9–12 | `project_scaffold`; Debugger + Go/FX playbooks; `legacy_audit` rule pack + `pao`→`n-api` migration mode; `swagger_check`; `govulncheck` | Scaffold wizard, compliance-audit view, quick-fixes on `rules_lint` and gopls diagnostics, migration plan viewer | Greenfield service builds and serves; Debugger resolves the top-5 failure classes ≥60% unattended; a real `pao`-style service migrates in PR-sized commits |
| **3 — Variants & scale** | 13–16 | Template variants from `pao` (gRPC/Connect, Temporal, ECMS/MinIO, migrations, integration tests, CI); server-workspace mode; admin UI; SLO dashboards | Server-mode switch, org dashboards, marketplace/`.vsix` distribution at scale | SLOs met 4 consecutive weeks; division-wide ready |
| **4 — Steady state** | ongoing | Bug-journal-driven prompt updates (weekly); new variants; quarterly red-team | Telemetry-driven UX iteration | — |

---

## 9. Open questions

**Closed by this revision** — `govalid` install source (SOP.md is unambiguous); the swagger generator (framework, config-gated, not a CLI); `GOPRIVATE` blocking server builds (local-first sidesteps it); the identity provider (D5); model access and tool-calling support (§4.2 — verified live); **whether a small model and an embedding model are available** (yes — `Phi-4-mini-instruct` and `qwen3-embedding`, same endpoint, same key, both tested, so D7's deferred tiering carries no infrastructure risk).

**Closed by the spike** ([Part A §4.6](plan-backend-agent.md), run 25 Aug 2026) — the largest question in the programme: **can `Qwen3.8-27B` actually produce template-compliant Go?** Yes. Given the reference `user` resource as context it generated a complete `Pension` resource — domain, DDL, repository, request and response DTOs, handler — that compiles clean, passes `go vet`, and scores **0 violations** against a working AST rules-engine prototype. FX wiring came back byte-correct. Legacy-violation detection found 15 real issues in a `pao`-style handler, including subtleties like `binding:` vs `validate:` tags. Also closed: whether the AST rules-engine approach works — the prototype reports 0 violations on the reference template, 0 on the generated resource, and **1,360 across 12 rules** on the legacy `pao` service, in 50–103 ms.

**Still open, in priority order:**

1. **Prefix caching — is it on?** `prompt_tokens_details.cached_tokens` is absent from responses (§4.2), so we cannot measure it. This is the single largest latency lever available and we are currently blind to it. Ask whoever operates the LiteLLM and the vLLM instances to (a) confirm `--enable-prefix-caching`, (b) add `--enable-prompt-tokens-details`, and (c) confirm LiteLLM passes `prompt_tokens_details` through. **Highest-priority operational question in the programme.**
2. **Who operates `ai.cept.gov.in`?** We need a named contact for §9 Q1, for LiteLLM's response cache being off for our traffic, for per-user virtual keys or JWT→key mapping (Part A §16.6), and for capacity.
3. **Endpoint capacity.** Sustained and peak QPS for `Qwen3.8-27B`, and who else shares it. This sets every number in the quota model, which currently carries placeholders (Part A §16).
4. **API key rotation** (§4.5). The supplied key has been circulated in plain text; the replacement must be issued as a server-only secret.
5. **Naming.** `dakcoder-go` is a placeholder. Does it share a brand with `postgen`? Decide before the GitLab project and the nginx path exist — both are painful to rename.
6. **Template ownership.** Who owns `templates/default-n-api` and keeps it in lockstep with `skill.md`/`SOP.md`? Two divergences are already known (§6: no `ToDomain()`, missing response timestamps).
7. **Committed credentials.** The MinIO keys, Aadhaar client secret and DB password in `new-template/configs/*.yaml` need an owner and a rotation decision. Not an agent question — but the agent will read those files.
8. **Audit retention.** 30 / 90 / 180 days — needs a CERT-In liaison answer.
9. **Rollout scope.** Which GitLab groups map to `user` vs `admin`, and who is in the pilot.

---

## 10. Definition of done (programme)

- `resource_scaffold` passes `verify:all` on every variant, with golden snapshots green.
- Brownfield modification pass rate on the eval suite ≥75%.
- Debug mode resolves the top-5 Go/FX failure classes unattended ≥60%.
- A real `pao`-generation service migrates to template compliance in reviewable commits.
- **Every session is attributable to a GitLab identity**; no shared-token path remains.
- Quota is enforced from **measured** token usage, not reservations.
- Median wall-clock for "add a resource" ≤90 s; P95 prompt tokens per coder turn ≤24k; prefix-cache hit rate ≥80%.
- 50+ active weekly users across ≥3 IT 2.0 Go sub-teams; SLOs met 4 consecutive weeks.
- Security review passed (ASVS L2, supply chain, prompt injection).
- Docs complete: ARCHITECTURE, TOOL-CATALOG, PROMPTS, RULES, ONBOARDING, RUNBOOKS.

---

## 11. Immediate next steps

1. Approve or annotate Part A and Part B.
2. **Find the `ai.cept.gov.in` owner and settle §9 Q1–Q3 in one conversation** — prefix caching, `prompt_tokens_details`, LiteLLM response cache off for our traffic, per-user keys, and capacity. Everything in Part A §5–§6 is designed around prefix reuse; confirming it is on is worth more than any amount of further planning.
3. **Rotate the model API key** (§4.5) and issue the replacement as a server-only secret.
4. Raise the committed-credentials issue (§9 Q7) with the template owner; it is independent of this programme but the agent will read those files.
5. Answer §9 Q5 (naming), then create the GitLab project, register the OAuth application, and provision `/coder/backend/`.
6. Author `docs/tool-catalog.md` — the formal JSON-Schema spec of C1. It is what the gateway, the sidecar, and the extension all bind against.
7. Build the **capability probe** (Part A §4.5) as the first executable artefact: a script that asserts every row of §4.2 and fails loudly on drift. It is ~100 lines, it becomes a CI check, and it means an endpoint change never surprises us mid-sprint.
8. **Promote the spike's rules prototype into `gotools`.** It already covers 14 rules, holds the three §20.3 baseline assertions, and runs in 50–103 ms. It is the highest-value zero-dependency work available and it needs no naming decision, no infrastructure, and no model.
9. Curate the first 20 eval tasks from real IT 2.0 Go merge requests of the last six months.
