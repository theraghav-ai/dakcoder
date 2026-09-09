# Hosting dakcoder — plan

> **Status: proposal, not yet built.** This is the design for turning `dakcoderd`
> from a per-window loopback daemon into a hosted service that a web portal, a
> desktop app, another agent, and the existing VS Code extension all call — with
> one contract that none of them can drift from.
>
> Companion to [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) §2 (system shape) and
> §7 (the Part B handover). Every route and constant cited below was read out of
> the code, not the plans; where the code and the plans disagree, the code wins
> and the disagreement is called out.

---

## 1. What exists today

Worth stating precisely, because the hosting work is mostly *relocation* of
things that already work, and knowing which is which is what keeps the estimate
honest.

### 1.1 The runtime is already an HTTP server

[`loopback.py`](apps/agent/src/dakcoder_agent/loopback.py) is a FastAPI app with
eighteen routes and a resumable SSE stream. This is the single most important
fact in this document: **the API a web portal needs mostly already exists.**

| Route | Purpose |
|---|---|
| `GET /v1/health` | unauthenticated liveness + `api_version`; authenticated adds workspace/gateway/sessions |
| `GET /v1/tools` | contract C1, the full 37-entry catalogue |
| `POST /v1/credential` | replace the gateway JWT without a restart |
| `POST /v1/tasks` | `{task, intent?, acceptance?}` → session |
| `GET /v1/sessions/{id}/events` | SSE, resumable via `since_id` or `Last-Event-ID` |
| `GET /v1/sessions`, `GET /v1/sessions/{id}` | list / detail (`?transcript=true`) |
| `DELETE /v1/sessions/{id}`, `POST .../abort` | lifecycle |
| `GET/POST /v1/sessions/{id}/revert` | undo plan and apply |
| `POST /v1/sessions/{id}/resume`, `.../messages`, `.../wind-down` | follow-ups |
| `GET /v1/sessions/{id}/context` | context inspector |
| `GET /v1/approvals`, `POST /v1/approvals/{id}`, `.../extend` | human-in-the-loop |

`API_VERSION = "1.1"`, declared at [`loopback.py:83`](apps/agent/src/dakcoder_agent/loopback.py#L83).

### 1.2 The central tier is already multi-tenant

[`apps/gateway`](apps/gateway/src/dakcoder_gateway/) already does the hard parts
of being a hosted service, for model traffic:

- **Identity** — GitLab OAuth + PKCE (`GitLabIdentity`), `TokenMinter` issuing
  15-minute JWTs with a 30-day refresh family, `dop_roles` from GitLab group
  membership (contract C3).
- **Quota** — Redis counters, window/week/hour series, `POST /v1/quota/preflight`,
  429s carrying `Retry-After` (contract C4).
- **Ledger** — Postgres usage accounting.
- **Model proxy** — `POST /v1/llm/*`, the only holder of the LiteLLM key.

So: **auth, quota and metering do not need to be invented. They need to be
extended from "who may spend tokens" to "who may run a session against which
repository".**

### 1.3 What is *not* hosted-ready

| Thing | Today | Why hosting breaks it |
|---|---|---|
| `Loopback` | one workspace, many sessions | a hosted service is many tenants × many workspaces |
| `SessionStore` | in-memory dict + `.dakcoder/sessions/<id>/*.jsonl` in the workspace, `limit=200` | single-node; no tenant scoping; lost on redeploy |
| Auth | one shared random loopback token, `secrets.compare_digest` | authenticates *a process*, not *a person* |
| Workspace | `Workspace.at(local path)` | web and desktop clients have no local repo the server can see |
| Execution | allowlisted binaries (`go`, `git`, `gofmt`, `goimports`, `golangci-lint`, `govulncheck`, `govalid`, `buf`, `swag`) on the host FS | on a shared host this is arbitrary code execution as the service account |
| Approvals | in-process `threading.Event`, `APPROVAL_TIMEOUT=600`, 5 s poll | does not survive a replica restart or cross a load balancer |
| CORS | **absent from both** `loopback.py` and gateway `app.py` | a browser cannot call either |
| SSE auth | `Authorization: Bearer` | browser `EventSource` cannot set headers |
| Contract sync | `protocol.ts` hand-mirrors `envelope.py` | already drifted — see §4.1 |

---

## 2. The one decision hosting forces

ARCHITECTURE.md §2 states the current split as a deliberate decision:

> **file access and command execution are local; auth, quota, the ledger and
> model access are always central.**

Hosting inverts the first half. Once a web portal calls the agent, there is no
developer machine in the loop — the server must hold the repository and run the
Go toolchain itself. That is not a detail to be discovered during implementation;
it is the decision the whole plan hangs off, and it has three consequences:

1. **The deliverable changes.** Locally, the output is edits in the developer's
   working tree, and `revert` restores their bytes. Hosted, the output must be a
   **pushed branch and a merge request** — there is no working tree to edit.
   `GET/POST /v1/sessions/{id}/revert` keeps its shape but changes meaning:
   locally "put my files back", hosted "discard the branch".
2. **Execution becomes a sandboxing problem.** `run_terminal` runs `go build`.
   On a laptop that is the developer's own toolchain doing what they asked. On a
   shared host it is untrusted code — `go build` executes `//go:generate`
   directives and module `init()` from whatever the repository contains.
3. **The threat model changes.** `Workspace` path confinement and
   `PROTECTED_GLOBS` were written against *model error* ("`read_file` is one
   hallucinated path away"). Hosted, they are also load-bearing against a
   *malicious repository* and against tenant-to-tenant escape.

**Recommendation: do not soften D2 — split it.** Keep the local mode exactly as
it is (the extension keeps working, unchanged, on the developer's own files) and
add a *second* deployment mode. Both speak the same contract. This is why §4
comes before everything else.

---

## 3. Target shape

```
   ┌────────────┐   ┌────────────┐   ┌──────────────┐   ┌───────────────┐
   │ VS Code    │   │ Web portal │   │ Desktop app  │   │ Another agent │
   │ extension  │   │ (browser)  │   │ (Electron/…) │   │ (A2A client)  │
   └─────┬──────┘   └─────┬──────┘   └──────┬───────┘   └───────┬───────┘
         │                │                 │                   │
         │  local mode    │   REST + SSE    │                   │ JSON-RPC
         │  (unchanged)   └────────┬────────┘                   │ + agent card
         │                         │                            │
         ▼                         ▼                            ▼
  ┌─────────────┐        ┌──────────────────────────────────────────────┐
  │ dakcoderd   │        │  GATEWAY  (apps/gateway, extended)           │
  │ 127.0.0.1   │        │  auth · quota · ledger · /v1/llm proxy       │
  │ local files │        │  + /.well-known/agent-card.json              │
  └─────────────┘        │  + reverse proxy to the control plane        │
                         └───────────────────┬──────────────────────────┘
                                             ▼
                         ┌──────────────────────────────────────────────┐
                         │  CONTROL PLANE  (apps/agentsvc — new)        │
                         │  tenancy · workspace lease · session registry│
                         │  routes (user, workspace, session) → runner  │
                         └───────────────────┬──────────────────────────┘
                                             ▼
                         ┌──────────────────────────────────────────────┐
                         │  RUNNER  (one container per session)         │
                         │  today's dakcoderd, ~unchanged               │
                         │  git clone · go toolchain · gotools sidecar  │
                         └──────────────────────────────────────────────┘
```

**The load-bearing choice: keep `Loopback` as the per-session runner.** It is
~1,100 lines of tested code that already models "one workspace, many sessions",
already persists a journal, already streams resumably, already handles
approvals. Making it multi-tenant would mean rewriting the most-exercised
component in the system. Instead a thin control plane owns tenancy and hands
each session an isolated runner that thinks it is still on a laptop.

That also means **the local and hosted modes run the same code**, which is what
keeps them honest — a divergence in behaviour would otherwise show up first in
production.

---

## 4. The contract spine — how the extension and the API stay in sync

This is the explicit requirement, so it is Phase 0 and everything else waits on
it. Adding two more clients to a hand-mirrored contract without fixing the
mirroring first is how a small drift becomes four incompatible clients.

### 4.1 The problem is already real, not hypothetical

| Fact | Where |
|---|---|
| `API_VERSION = "1.1"` declared twice, by hand | [`loopback.py:83`](apps/agent/src/dakcoder_agent/loopback.py#L83) and [`protocol.ts:22`](extension/src/protocol.ts#L22) |
| `EventType` declared twice, by hand | `envelope.py` `EventType` (StrEnum) and `protocol.ts` `EventType` (union) |
| **They have already drifted** | Python emits `METRICS = "metrics"`; the TypeScript union does not list it |

And the prose describing the contract has drifted from the contract itself:
`catalog.py`'s own docstring and ARCHITECTURE.md §1 both say "29 tools", while
the generated `docs/tool-catalog.json` carries 37. The *generated* artifact is
correct — which is the argument for generating the rest of it, made by the one
part of the system that already is.

The C2 "ignore unknown types" rule means that drift is *tolerated* rather than
crashing — which is exactly why nobody noticed. Tolerance is the right wire
policy and the wrong development policy: it converts "the extension cannot
render run metrics" from a build failure into a silent missing feature.

Nothing today compares the two sides. `npm run verify` runs typecheck, unit
tests, credential and command checks — none of them can see Python.

### 4.2 The pattern this repo already uses three times

The fix is not novel here; it is the house pattern, applied to a fourth artifact:

| Artifact | Generator | Drift gate |
|---|---|---|
| Tool catalogue (C1) | `make catalog` | `make catalog-check` → `test_catalog.py` |
| Knowledge base | `make knowledge` | `make knowledge-check` |
| Rule citations | `gotools doc-pin` | `gotools doc-check` |
| **Wire contract (C2 + REST + card)** | **`make contract`** ← new | **`make contract-check`** ← new |

### 4.3 Single source of truth

Put it in Python, in `apps/shared`, because Python is the server, already owns
`envelope.py` (C1/C2) and `registry.py` (the 37 tool specs), and is the only
side that can be authoritative at runtime.

```
apps/shared/src/dakcoder_shared/contract/
    __init__.py       API_VERSION, CONTRACT_HASH   ← the only declaration
    events.py         EventType + per-type payload dataclasses
    rest.py           request/response models for every route
    card.py           the agent card, built from registry + modes
    emit.py           the generators
```

`API_VERSION` moves here; `loopback.py` imports it. Payload shapes that today
exist only as `dict[str, Any]` written inline (`turn_start`, `gate`, `usage`,
`finish`, …) get real models — they are already documented in `protocol.ts`, so
this is transcription, not design.

### 4.4 What gets generated

`make contract` writes, from that one source:

| Output | Consumer |
|---|---|
| `docs/api/openapi.json` | pinned FastAPI schema; the REST reference for portal/desktop teams |
| `docs/api/events.json` | C2 event catalogue: type → payload schema |
| `docs/api/agent-card.json` | the served agent card (§5) |
| `packages/contract/src/index.ts` | **TypeScript types + `API_VERSION` + `CONTRACT_HASH`** |
| `packages/contract/package.json` | publishable as `@dakcoder/contract` |

`extension/src/protocol.ts` stops declaring types and re-exports from
`@dakcoder/contract`, keeping only what is genuinely client-side: `parsePlan`,
`normaliseQuota`, `isResumable`. Those are logic, not contract, and they belong
where they are.

### 4.5 Three enforcement layers

Build-time alone is not enough once third parties ship on their own cadence.

1. **Build time.** `make contract-check` regenerates into a temp dir and diffs;
   non-empty diff fails. Wired into the Python suite (like `catalog-check`) *and*
   into `npm run verify`, so neither side can be green alone.
2. **Runtime.** `/v1/health` gains `contract_hash` — a stable hash over the
   generated contract. Clients compare it with their compiled-in value. The
   extension already refuses to connect on `api_version` mismatch
   ([`runtime.ts:262`](extension/src/runtime.ts#L262)); this catches same-version
   drift, which is the case `api_version` misses. **Warn, do not refuse**, when
   only the hash differs: additive changes are legal under C2 and a hard refusal
   would make every additive release a breaking one.
3. **Publication.** `@dakcoder/contract` is versioned with the API. A portal
   pinning `^1.1.0` gets additive changes and a compile error on breaking ones.
   For non-TS clients, `docs/api/openapi.json` drives their own codegen.

### 4.6 Versioning policy

Restate C2's rule as a *tested* invariant rather than a convention:

- **Additive only within a major.** New event types, new optional fields.
- Removing a field or changing its type ⇒ `API_VERSION` major bump ⇒ a
  deprecation window during which both are served.
- A `contract-compat` test asserts the current contract is a superset of the
  previous release's `docs/api/events.json`, so a removal cannot land silently.

---

## 5. The agent card

### 5.1 It is not the tool catalogue

Worth stating because both are "a JSON list of things the agent can do", and
conflating them would leak the internal tool surface to every caller.

| | Tool catalogue (C1) | Agent card |
|---|---|---|
| Audience | the model, inside a run | other agents / clients, before a run |
| Content | 37 tools: `read_file`, `patch_file`, `rules_lint`, … | a handful of *skills*: "migrate a service" |
| Stability | changes whenever a tool is added | changes rarely; it is a public promise |
| Exposure | authenticated | **served unauthenticated** at a well-known URL |

Publishing `write_file` and `run_terminal` to unauthenticated callers would be
both a reconnaissance leak and a promise the API does not make — callers submit
*tasks*, never tool calls.

### 5.2 Where it is served

`GET /.well-known/agent-card.json` on the public origin (the gateway), so
discovery works at the domain root as the A2A convention expects. The gateway
already has an unauthenticated `/v1/health`, so an unauthenticated well-known
route is not a new exposure class.

### 5.3 Draft

Generated by `contract/card.py` from `modes.py` and `registry.py`, so the version
and capability fields cannot go stale:

```json
{
  "protocolVersion": "0.3.0",
  "name": "dakcoder",
  "description": "Backend coding agent for IT 2.0 Go microservices on n-api-template: migrates legacy api-* services, implements endpoints, and answers questions about the template contract.",
  "url": "https://aiops.cept.gov.in/coder/backend/v1/a2a",
  "preferredTransport": "JSONRPC",
  "version": "1.1.0",
  "documentationUrl": "https://aiops.cept.gov.in/coder/docs",
  "provider": { "organization": "CEPT IT 2.0", "url": "https://cept.gov.in" },
  "capabilities": {
    "streaming": true,
    "pushNotifications": false,
    "stateTransitionHistory": true
  },
  "defaultInputModes": ["text/plain", "application/json"],
  "defaultOutputModes": ["text/plain", "application/json"],
  "securitySchemes": {
    "dakcoderJwt": { "type": "http", "scheme": "bearer", "bearerFormat": "JWT" }
  },
  "security": [{ "dakcoderJwt": [] }],
  "skills": [
    {
      "id": "migrate-service",
      "name": "Convert a legacy service to n-api-template",
      "description": "Applies the migration SOP end to end: dependency swap, handler conversion, govalid regeneration, test harness, swagger verification. Opens a branch and a merge request.",
      "tags": ["go", "migration", "n-api-template"],
      "examples": ["Convert pao-back-end-development to the new template"]
    },
    {
      "id": "implement-change",
      "name": "Implement a change in a Go service",
      "description": "Adds or changes an endpoint, DTO, repository method or domain field, verified by the gate (build, vet, rules_lint, tests).",
      "tags": ["go", "codegen"]
    },
    {
      "id": "review-service",
      "name": "Audit a service against the template contract",
      "description": "Runs the 41-rule compliance set and the legacy audit, and reports findings with fixes and citations. Read-only.",
      "tags": ["go", "review"]
    },
    {
      "id": "answer",
      "name": "Answer a question about a service or the template",
      "description": "Read-only. Cites the knowledge base and the repository.",
      "tags": ["qa"]
    }
  ]
}
```

Skills map onto existing machinery: `answer`/`review-service` → `Intent.ASK`,
the other two → `Intent.AGENT`. No new execution paths.

### 5.4 The A2A adapter

A2A's JSON-RPC methods (`message/send`, `message/stream`, `tasks/get`,
`tasks/cancel`) are a thin translation over what exists — `POST /v1/tasks`, the
SSE stream, `GET /v1/sessions/{id}`, `POST .../abort`. Build it as an **adapter
module, not a second implementation**: `apps/agentsvc/a2a.py` calling the same
service layer.

**Sequence it last.** The REST+SSE API is what the portal and desktop app
actually need; A2A is for agent-to-agent callers, which is the smallest audience
and the least settled spec.

---

## 6. The public API

Keep `/v1/*` shapes identical to the loopback ones — that is what lets one
generated client serve all four consumers — and add tenancy in the path.

```
POST   /v1/workspaces                 { repo_url, ref? }  → lease a server-side clone
GET    /v1/workspaces                 the caller's leases
DELETE /v1/workspaces/{wid}           release (deletes the clone)

POST   /v1/workspaces/{wid}/tasks     ≡ today's POST /v1/tasks
GET    /v1/sessions?workspace={wid}   scoped to the caller
GET    /v1/sessions/{id}/events       SSE, unchanged, resumable
...                                   every other session/approval route unchanged

POST   /v1/sessions/{id}/deliver      { title, description } → push branch + open MR
GET    /.well-known/agent-card.json   unauthenticated
POST   /v1/a2a                        JSON-RPC (phase 3)
```

Two routes are dropped for hosted callers: `POST /v1/credential` (the server
holds its own gateway credential) and the workspace fields of `/v1/health`.

### 6.1 Browser specifics

Concrete gaps, all in the "small but blocking" category:

- **CORS.** Neither app installs `CORSMiddleware`. Add it to the hosted app only,
  with an explicit origin allowlist from config — never `*`, since these are
  credentialed requests.
- **`EventSource` cannot send `Authorization`.** Three options; recommend the
  third:
  1. token in query string → lands in access logs; no.
  2. cookie → needs CSRF handling for the whole API; disproportionate.
  3. **`fetch` + `ReadableStream`** in the portal, which can set headers and is
     what any modern SSE client library does. `Last-Event-ID` is already honoured
     for resume, so nothing server-side changes.
- **Proxy buffering.** Already correct: `deploy/nginx-dakcoder.conf` sets
  `proxy_buffering off` and `proxy_read_timeout 660s`. Any new route must live
  under a location block with the same settings.
- **Heartbeats.** The `heartbeat` event already exists; confirm its interval is
  below the shortest idle timeout in the path (nginx 660 s, plus whatever
  corporate proxies impose on portal users).

---

## 7. Workspaces and execution

The hardest part, and the part most likely to be under-estimated.

### 7.1 Where the repository comes from

Clone server-side from GitLab, using the caller's identity. The gateway already
performs GitLab OAuth and holds a `Profile`, so the agent can act **as the user**
rather than as a service account — which is what makes per-repo authorisation
someone else's already-solved problem: if the user cannot clone it, neither can
the agent.

Do **not** add a "upload your repo as a zip" path. It bypasses GitLab's
authorisation entirely and there is no audit trail for what was uploaded.

Lifecycle: lease on `POST /v1/workspaces` → shallow clone → sessions run against
it → `POST /v1/sessions/{id}/deliver` pushes a branch and opens an MR → lease
expires on a TTL and the clone is deleted. Disk is the constraint; a Go service
plus its module cache is not small, so leases need a quota of their own
(§8) and a reaper.

### 7.2 Isolation

`go build` runs arbitrary code from the repository (`//go:generate`, `init()`,
build tags, and the test binary if `go_test` runs). On a shared host, with a
service account that can reach an internal GitLab and an internal LiteLLM, that
is the whole risk in one sentence.

**One container per session**, not per tenant and not per host:

- read-only root FS except the workspace mount and a per-session module cache;
- no network except the gateway (`/v1/llm`) and GitLab — enforced by network
  policy, not by the `curl`/`wget` refusals in `_TERMINAL_ALTERNATIVES`, which
  are model guidance and not a control;
- CPU/memory/PID/disk limits, and a wall-clock kill;
- non-root, no capabilities, seccomp on;
- the container dies with the session.

`ALLOWED_BINARIES` and `Workspace` confinement stay — defence in depth, and they
still produce the good error messages the model needs — but the container is the
actual boundary. The existing comment in `commands.py` ("containers are the
sandbox runner's business") is exactly right, and this is where that runner gets
built.

Warm-pool the containers with the Go module cache pre-seeded, or first-token
latency will be dominated by `go mod download`. ENDPOINT-CAPABILITIES.md records
a 6.4 s run-start baseline and a 16.8 s cold `go_build`; a cold container is
strictly worse than both.

### 7.3 Toolchain parity

`deploy/toolchain/` and `deploy/install-go.sh` already provision Go on the host.
The runner image must pin the same versions as the local mode, or hosted runs
will fail gates that pass locally. Add a `toolchain_versions` block to
`/v1/health` so a mismatch is diagnosable instead of mysterious.

---

## 8. Identity, tenancy and quota

Mostly extension of what the gateway already does.

- **Identity.** Reuse C3 unchanged: PKCE against GitLab, `dakcoder` JWT, `sub` =
  GitLab user id, `dop_roles` from group membership. The hosted agent validates
  the same JWT via `TokenMinter.verify` — no second identity system.
- **Machine callers.** A web portal's backend and an A2A peer are not humans and
  cannot do PKCE. Add a client-credentials grant issuing a JWT with a
  `client_id` subject and an explicit scope set. Keep it a *separate* grant so
  machine tokens are distinguishable in the ledger and can be revoked
  independently.
- **Scopes.** Today's JWT authorises "spend tokens". Hosting needs finer grain:
  `sessions:read`, `sessions:write`, `workspaces:write`, `deliver:mr`. A
  read-only portal dashboard should not be able to open merge requests.
- **Tenant scoping.** Every session row gains `owner_sub` and `workspace_id`, and
  every route filters on the caller's `sub`. This is the highest-risk code in
  the plan — a missing filter is a cross-tenant data leak — so it belongs in one
  dependency (`caller_owns(session_id)`) used by every route, with a test that
  enumerates the route table and fails on any session route that does not depend
  on it. Enumerating routes rather than listing them by hand is what makes the
  test still correct after someone adds route nineteen.
- **Quota.** Existing token/session counters apply unchanged. Add: concurrent
  sessions per user, workspace leases per user, and total runner disk. Reuse
  `POST /v1/quota/preflight` so the portal can grey out "Run" before the user
  waits.

---

## 9. Sessions at scale

- **Durability.** The journal (`.dakcoder/sessions/<id>/events.jsonl`) works
  per-runner but dies with the container. Ship completed transcripts to object
  storage or Postgres on session end, and stream summaries to the control plane
  as they change. Keep the journal as the runner's local write-ahead log — its
  "never fail a run, never slow a turn" properties are worth preserving exactly.
- **Routing.** While a session is live, its SSE stream and its approvals must
  reach *its* runner. Simplest correct answer: the control plane records
  `session_id → runner` and proxies; no sticky-session load balancer config, and
  it keeps runners un-addressable from outside.
- **`limit=200`.** A per-runner cap is fine; the hosted list view must page over
  the durable store instead.
- **Restart semantics.** `SessionStore.restore()` already marks in-flight
  sessions `ERROR` on restart with a clear summary. Keep that behaviour when a
  runner dies — it is the honest answer, and a session stuck in `running` is
  "unresumable, undeletable and permanently in the way", as the code comment
  puts it.

---

## 10. Approvals for non-IDE clients

Approvals are the feature most shaped by the assumption of a developer sitting in
VS Code, and they need the most care.

- The wait is an in-process `threading.Event` with a 600 s timeout. Inside a
  per-session runner that still works — the run and its approvals are in the
  same process. **No redesign needed**, which is a direct dividend of the
  runner-per-session choice.
- But 600 s is tuned for someone watching an editor. A portal user may be on
  another page and a desktop app may be minimised. Make the timeout **policy per
  client class**, sent at task creation, and lean on the existing
  `POST /v1/approvals/{id}/extend`.
- The timeout semantics are already right and should not be re-litigated: a
  hard release that turns a slow review into a rejection is a WCAG 2.2.1
  failure, which is why `extend` exists.
- **Unattended callers.** An A2A peer cannot approve anything. Give tasks an
  `approval_policy`: `interactive` (today), or `auto_safe` — auto-approve
  non-protected mutations, refuse anything matching `PROTECTED_GLOBS`, and record
  every auto-decision in the transcript. Never allow blanket auto-approval of
  protected paths; that is what `PROTECTED_GLOBS` is for.
- Notification: portals need to know an approval is waiting without holding the
  stream. `tool_pending` already carries it on the SSE stream; add optional
  webhooks later if a caller genuinely cannot hold a connection.

---

## 11. What changes in the extension

Deliberately small — this plan should not destabilise a working product.

1. `protocol.ts` re-exports `@dakcoder/contract`; keeps `parsePlan`,
   `normaliseQuota`, `isResumable`. Deletes the hand-maintained type
   declarations (and closes the `metrics` drift).
2. `npm run verify` gains `contract-check`.
3. `client.ts` gains a base-URL notion so the same client speaks to
   `127.0.0.1:<port>` or `https://…/coder/backend`. The route shapes are
   identical, which is the point.
4. A `dakcoder.mode: local | hosted` setting. In hosted mode the extension skips
   the venv/wheel/spawn path in `runtime.ts` entirely and uses the JWT it already
   holds — which also makes it a first-class test client for the hosted API.
5. `doctor.ts` learns the hosted checks (reachability, contract hash, workspace
   lease) alongside the local ones it already runs.

---

## 12. Phases

Ordered so each phase is independently shippable and none is a big-bang.

| Phase | Deliverable | Ships value |
|---|---|---|
| **0. Contract spine** | `dakcoder_shared.contract`, `make contract` / `contract-check`, `@dakcoder/contract`, `contract_hash` on `/v1/health`, extension re-exports | Drift becomes impossible before four clients exist. Fixes `metrics`. |
| **1. Hostable runtime** | CORS, JWT auth adapter alongside the loopback token, tenant-scoped session filter + route-enumerating test, OpenAPI published | The existing API is callable by a non-IDE client |
| **2. Workspaces + runners** | `POST /v1/workspaces`, GitLab clone, per-session container, `deliver` → branch + MR, warm pool | **A web portal can do real work** |
| **3. Agent card + A2A** | `/.well-known/agent-card.json`, `POST /v1/a2a` adapter | Agent-to-agent callers |
| **4. Scale + hardening** | durable transcripts, control-plane routing, lease/concurrency quotas, reaper, toolchain parity in `/v1/health` | Multi-replica, operable |

Phases 0 and 1 touch no execution model and are low risk. **Phase 2 is the
project** — treat the others as small by comparison, and resist the temptation to
start there because it is the visible one.

---

## 13. Risks and open questions

| # | Risk | Mitigation / who decides |
|---|---|---|
| R1 | Sandbox escape from a malicious repo via `go build` | Container per session, no ambient network, non-root. **Needs a security review before Phase 2 ships**, not after. |
| R2 | Cross-tenant leak through a missing session filter | One shared dependency + a test that enumerates routes and fails on any unguarded session route |
| R3 | Disk exhaustion from workspace leases | Lease quota, TTL, reaper, per-lease size cap |
| R4 | Cost: hosted runs are unattended and can be triggered in bulk | Existing quota + new concurrency cap; preflight before accepting a task |
| R5 | Cold-start latency dominates perceived quality | Warm pool with a seeded module cache; measure against the 6.4 s local baseline |
| R6 | Contract drift to third-party clients on their own release cadence | Published versioned package + `contract_hash` warn + a superset compat test |
| R7 | The extension regresses while the hosted mode is built | Local mode stays byte-identical through Phases 0–3; hosted is additive |

**Open questions needing a named owner:**

1. **Is the deliverable a merge request?** §2 assumes yes. If the portal instead
   expects a downloadable patch or an in-browser diff review, Phase 2's output
   half changes. *This is the single biggest requirement gap and should be
   settled before Phase 2 is scoped.*
2. **Who may run against which repository?** Answered by acting as the user
   (§7.1) — needs confirmation that GitLab group permissions are the intended
   authorisation boundary.
3. **Where does this run?** The current host serves the gateway from
   `aiops.cept.gov.in/coder/backend` via nginx. Per-session containers need a
   container runtime and capacity planning that a single VM may not have.
4. **A2A protocol version.** The spec is moving; pin one and record the pinned
   version in the card rather than tracking latest.
5. **Data retention.** Hosted transcripts contain source code. Retention period
   and deletion policy are a compliance decision, not an engineering one.

---

## 14. Verification

Matching the repo's existing habits, so the gates are ones people already run.

- **Contract**: `make contract-check` in both the Python suite and
  `npm run verify`; the superset compat test against the previous release.
- **Tenancy**: route-enumerating test asserting every session route depends on
  `caller_owns`; a two-tenant integration test asserting 404 (not 403 — do not
  confirm existence) across the boundary.
- **Sandbox**: a red-team repository fixture in `audit-repros/` with a hostile
  `//go:generate`, asserting it cannot reach the network, the host FS, or another
  workspace.
- **Parity**: run the existing `test_happy_path.py` against a hosted runner as
  well as a local one. Same code, same gate, same assertions — the strongest
  available evidence that splitting D2 did not fork behaviour.
- **Live**: extend the `DAKCODER_LIVE=1` pattern from
  `test_live_endpoint.py` to a `test_live_hosted.py`, skipped in CI for the same
  reasons.
