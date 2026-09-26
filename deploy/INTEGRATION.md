# Integrating with dakcoder

dakcoder is a backend coding agent for IT 2.0 Go microservices. It works on a
server-side clone of a repository you lease, runs a task against it, and hands
back the changes — as a branch, and optionally as a merge request.

This guide is for people writing a client: a desktop application, a portal
backend, an orchestrator, or another agent. It covers every API you can call
from outside this server, and exactly which token each one needs.

If you are a developer using the VS Code extension, you do not need this guide.
The extension handles all of it.

**Base URL**

```
https://ai.cept.gov.in/dakcoder
```

Everything below is relative to it. All requests are HTTPS, and all of them
except the agent card need a bearer token.

---

## 1. Which surface to use

There are two ways in. They reach the same engine — the A2A adapter is a
translation onto the same service layer the REST API uses — so pick on
ergonomics, not capability.

| | **A2A (JSON-RPC)** | **REST** |
|---|---|---|
| Endpoint | `POST /v1/a2a` | `/v1/runtime/v1/…` |
| Shape | One endpoint, JSON-RPC 2.0 | Resource routes |
| Good for | Agents and orchestrators that already speak A2A; "run this and tell me when it's done" | Clients that want the workspace, the approval queue and delivery as separate steps |
| Leasing | Implicit — pass `repo_url` and it leases for you | Explicit — you lease, then run |
| Delivery | Not available; call the REST route | `POST /v1/runtime/v1/sessions/{id}/deliver` |
| Streaming | `message/stream` (SSE) | `GET …/sessions/{id}/events` (SSE) |

A desktop app that just wants "do this task on this repo" should use A2A. A
portal that renders workspaces, approvals and merge requests should use REST.
Mixing them is fine and normal: start over A2A, then deliver over REST. A task
started either way is an ordinary session, visible through both.

---

## 2. Getting a token

Every call carries `Authorization: Bearer <token>`. How you get one depends on
whether you are a machine or a person.

### 2.1 Machine callers — the client-credentials grant

This is the path for a desktop application's backend, a portal backend, an
orchestrator, or another agent.

Ask the dakcoder operator to register you. They will give you a `client_id` and
a `client_secret`. The server stores only a SHA-256 of the secret, so if you
lose it, it must be reissued — it cannot be recovered.

```http
POST /v1/auth/token
Content-Type: application/x-www-form-urlencoded

grant_type=client_credentials&client_id=portal-orchestrator&client_secret=<secret>
```

JSON bodies work too, if that is easier for your HTTP client.

```json
{
  "access_token": "eyJhbGciOiJIUzI1NiIs…",
  "token_type": "Bearer",
  "expires_in": 900,
  "scope": "sessions:read sessions:write workspaces:write agenda:write a2a llm"
}
```

**The token lasts 15 minutes and there is no refresh.** When it expires, request
another — the grant is cheap and stateless. Cache the token for slightly less
than `expires_in` rather than requesting one per call, and re-request on a 401.

A wrong secret is `401 {"error": "invalid_client"}`. An unknown `grant_type` is
`400 {"error": "unsupported_grant_type"}`.

Your identity on the server is `client:<client_id>`. All your model usage is
recorded against it in the usage ledger, separately from every other caller.

### 2.2 People

A person's token comes from signing in with GitLab
(`POST /v1/auth/start` → `/v1/auth/exchange`, PKCE). **Sign-in is not published
on this deployment yet** — see [Current deployment status](#9-current-deployment-status).
Until it is, a person's token is minted by hand on the server by the operator.

A person's token carries every scope except `delegate`.

### 2.3 Scopes

A client is registered with a fixed set of scopes, and its token carries exactly
those. Ask for what you need and no more — a 403 naming a scope is a clearer
failure than an over-broad token.

| Scope | Opens |
|---|---|
| `a2a` | `POST /v1/a2a` — calling dakcoder as another agent |
| `sessions:read` | reading sessions, their events, plans and transcripts |
| `sessions:write` | starting runs, sending messages, answering approvals, stopping runs |
| `workspaces:write` | leasing and releasing workspaces |
| `agenda:write` | proposing and moving work on a workspace's agenda |
| `deliver:mr` | pushing a session's branch and opening a merge request |
| `llm` | the model proxy (`/v1/llm/…`), charged to your quota |
| `delegate` | internal; **never granted to a client** |

**How REST routes map to scopes.** The gateway derives the needed scope from the
method and path, so there is no per-route table to memorise:

- anything ending in `/deliver` → `deliver:mr`
- `POST /v1/workspaces` or `DELETE /v1/workspaces/{id}` → `workspaces:write`
- any `POST` with `agenda` in the path → `agenda:write`
- any other `GET` → `sessions:read`
- any other `POST`/`DELETE` → `sessions:write`

A token without the scope gets `403` with a message naming it.

---

## 3. A2A: the agent-to-agent interface

### 3.1 The agent card

Public, no token:

```
GET /.well-known/agent-card.json
```

It describes dakcoder to other agents: the JSON-RPC endpoint, the transport, the
security scheme, and the skills below. Fetch it rather than hard-coding — it is
generated from the same source as the server's behaviour.

### 3.2 The envelope

All four methods are JSON-RPC 2.0 over `POST /v1/a2a`, with `a2a` scope.

```http
POST /v1/a2a
Authorization: Bearer <token>
Content-Type: application/json
```

| Method | Does |
|---|---|
| `message/send` | Start a run. With `taskId`, send a follow-up to a running one. |
| `message/stream` | The same, answered as a stream of task updates (SSE). |
| `tasks/get` | A run's current state. |
| `tasks/cancel` | Stop a run. |

A **task** is a run. Its `contextId` is the workspace it runs on.

### 3.3 Saying what to work on

The message's `metadata` chooses the workspace. Give **one** of:

| Field | Meaning |
|---|---|
| `workspace_id` | A workspace you already hold. Also accepted as the message's `contextId`. |
| `repo_url` (+ `ref`) | Lease this repository now. Reuses your existing lease of the same repo if you have one. |
| *neither* | You get a **scratch workspace**: an empty directory, no repository. |

**A question does not need a repository.** If you name neither, the run happens
on a scratch workspace. The agent's knowledge base — the template
documentation and the playbooks — ships inside the agent and needs no checkout,
so "what does the template say about the repository pattern?" is answered
normally. The repository-shaped tools simply find an empty tree and say so.

One scratch workspace per caller, reused across questions, and it does not count
against your workspace allowance. Nothing can be delivered from it: there is no
repository to push to, so `deliver` answers `409`. When the task needs a
codebase — reading real handlers, making a change — name a `repo_url`.

Other metadata:

| Field | Default | Meaning |
|---|---|---|
| `skill` | none | One of the card's skills (below). Picks how the agent works. |
| `approval_policy` | `auto_safe` | `auto_safe` or `interactive`. |

**`approval_policy` defaults to `auto_safe` over A2A, deliberately** — an agent
has nobody to ask when the run needs a decision. `auto_safe` auto-approves the
safe operations and rejects the rest. Use `interactive` only if you are going to
poll `/v1/runtime/v1/approvals` and answer; an unanswered approval suspends the
run after 30 minutes.

### 3.4 Skills

| `skill` | Does | Writes? |
|---|---|---|
| `migrate-service` | Applies the full migration SOP to convert a legacy service to n-api-template. Long-running; the gate is deferred to the last phase. | yes |
| `implement-change` | Adds or changes an endpoint, DTO, repository method or domain field, verified by the gate (build, vet, rules_lint, tests). | yes |
| `review-service` | Audits a service against the template contract and reports findings with fixes and citations. | no |
| `answer` | Answers a question about a service or the template, citing the knowledge base and the repository. | no |

Omitting `skill` lets the agent choose. An unknown skill is `-32602`.

### 3.5 Worked example

Start a run on a repository you have not leased yet, and block until it ends:

```json
{
  "jsonrpc": "2.0",
  "id": 1,
  "method": "message/send",
  "params": {
    "message": {
      "role": "user",
      "kind": "message",
      "messageId": "1f3a…",
      "parts": [
        { "kind": "text", "text": "Add a status filter to the Pension list endpoint." }
      ],
      "metadata": {
        "repo_url": "https://gitlab.cept.gov.in/it-2.0/backend/pension-service",
        "ref": "main",
        "skill": "implement-change"
      }
    },
    "configuration": { "blocking": true }
  }
}
```

`"blocking": true` waits for the run to reach a terminal state before answering.
Without it you get the task immediately in `working` state and poll `tasks/get`,
or use `message/stream`.

The result is a task:

```json
{
  "kind": "task",
  "id": "d9a0ac3bd56c",
  "contextId": "27ed5afd8fe1",
  "status": {
    "state": "completed",
    "timestamp": "2026-09-21T04:51:33Z",
    "message": { "role": "agent", "parts": [{ "kind": "text", "text": "1 file(s) changed and the gate is clean:\n  - README.md" }] }
  },
  "metadata": { "workspace_id": "27ed5afd8fe1", "branch": "dakcoder/d9a0ac3bd56c" },
  "artifacts": [
    {
      "artifactId": "d9a0ac3bd56c-changes",
      "name": "changes",
      "parts": [{ "kind": "data", "data": { "files": ["README.md"], "branch": "dakcoder/d9a0ac3bd56c" } }]
    }
  ]
}
```

`metadata.branch` is where the work is. Once delivered, `metadata.merge_request`
carries the merge request URL.

### 3.6 Task states

| State | Means |
|---|---|
| `working` | Running. |
| `input-required` | Running, and blocked on an approval you must answer. |
| `completed` | Finished, gate clean. |
| `canceled` | You cancelled it. |
| `failed` | Ended without a clean result — errored, made no progress, ran out of budget, or failed verification. |

`completed` and `failed` are terminal. `tasks/cancel` on a terminal task is
`-32002`.

### 3.7 Follow-ups

To steer a run in flight, or to continue a finished one, send `message/send`
again with `taskId` set on the message. The text is delivered to that session
rather than starting a new run.

### 3.8 Error codes

| Code | Means |
|---|---|
| `-32700` | Body was not JSON. |
| `-32600` | Not a valid JSON-RPC request. |
| `-32601` | No such method. |
| `-32602` | Bad params — no text part, unknown skill, or no workspace named. |
| `-32603` | Internal error. |
| `-32001` | No such task, or not yours. |
| `-32002` | Task has already ended and cannot be cancelled. |

Refusals from the service layer (quota, allowlist, limits) arrive as JSON-RPC
errors carrying the HTTP status in `data.status`. Their meanings are in
[section 6](#6-limits-and-refusals).

### 3.9 Delivery is not an A2A method

Opening a merge request is a decision, not something the agent does on its own.
A finished task tells you the branch; you then call the REST route:

```http
POST /v1/runtime/v1/sessions/{task_id}/deliver
Authorization: Bearer <token>            # needs deliver:mr
Content-Type: application/json

{ "title": "Add a status filter to Pension list", "description": "…" }
```

If the run did not finish cleanly the route answers `409`. Deliver anyway by
adding `"override": "<why>"` — the reason is recorded on the merge request.

---

## 4. REST: the hosted API

Everything hosted lives under `/v1/runtime/`, and the hosted API's own paths
begin with `v1/`. That is why the working URLs have `v1` twice:

```
https://ai.cept.gov.in/dakcoder/v1/runtime/v1/workspaces
```

The gateway forwards each request with your verified identity attached. You
cannot reach the control plane or a runner directly, and you cannot act as
anyone else.

### 4.1 Workspaces

A workspace is a server-side clone of an allowlisted repository, leased to you
— or, for a question, an empty directory with no repository at all.

| Route | Scope | Does |
|---|---|---|
| `POST /v1/runtime/v1/workspaces` | `workspaces:write` | Lease one. Body: `{"repo_url": "...", "ref": "main"}`, or `{"scratch": true}` for one with no repository. Returns `201` and the workspace. |
| `GET /v1/runtime/v1/workspaces` | `sessions:read` | List the ones you hold. |
| `DELETE /v1/runtime/v1/workspaces/{id}` | `workspaces:write` | Release it. Sessions are archived, the clone deleted. |

```json
{
  "id": "27ed5afd8fe1",
  "repo_url": "https://gitlab.cept.gov.in/it-2.0/backend/pension-service",
  "ref": "main",
  "created_at": 1789966236.83,
  "expires_at": 1790571036.83,
  "last_used_at": 1789966236.83
}
```

Leasing clones the repository, so the first call on a large repo takes a while —
allow a generous client timeout. A repository must be on the operator's
allowlist; otherwise `403`.

A **scratch** workspace (`{"scratch": true}`) clones nothing and is immediate.
It comes back with `"repo_url": "scratch"` and an empty `ref`. Use it when the
task is a question rather than a change. There is one per caller — asking again
returns the same one — it is exempt from the workspace allowance, and nothing
can be delivered from it.

Release what you finish with. Leases are capped per caller, and expire on their
own after 7 days.

### 4.2 Starting a run

```http
POST /v1/runtime/v1/workspaces/{workspace_id}/tasks       # sessions:write
```

| Field | Default | Meaning |
|---|---|---|
| `task` | required | What to do, in prose. |
| `intent` | `auto` | `auto`, `ask` (read-only) or `agent` (may change files). |
| `approval_policy` | `interactive` | `interactive` or `auto_safe`. |
| `approval_timeout` | server default | Seconds an approval waits. Positive number. |
| `acceptance` | `[]` | Acceptance criteria, as strings. |

**Note the default differs from A2A.** Over REST `approval_policy` defaults to
`interactive`, so a non-interactive client should pass `auto_safe` explicitly.

Returns the session. `409` if a run is already going on that workspace — runs on
one workspace share its working tree and go one at a time. Lease the repository
twice to run two at once.

### 4.3 Sessions

| Route | Scope | Does |
|---|---|---|
| `GET /v1/runtime/v1/sessions` | `sessions:read` | Your sessions, newest first. Query: `workspace`, `limit` (1–200, default 50), `before`. A `next` in the response is the `before` for the following page; absent on the last. |
| `GET /v1/runtime/v1/sessions/{id}` | `sessions:read` | Detail: status, summary, mutations, turns, pending approvals. |
| `GET /v1/runtime/v1/sessions/{id}/events` | `sessions:read` | The live event stream (SSE). |
| `GET /v1/runtime/v1/sessions/{id}/transcript` | `sessions:read` | What happened, or what the model saw. |
| `GET /v1/runtime/v1/sessions/{id}/plan` | `sessions:read` | The plan, its statuses and revisions. |
| `POST /v1/runtime/v1/sessions/{id}/messages` | `sessions:write` | Send a follow-up. Body `{"text": "..."}`. |
| `POST /v1/runtime/v1/sessions/{id}/abort` | `sessions:write` | Stop it. |
| `POST /v1/runtime/v1/sessions/{id}/wind-down` | `sessions:write` | Let it finish the current turn, then stop. |
| `POST /v1/runtime/v1/sessions/{id}/revert` | `sessions:write` | Restore what it touched to HEAD. |
| `POST /v1/runtime/v1/sessions/{id}/compact` | `sessions:write` | Compact the context on demand. |
| `POST /v1/runtime/v1/sessions/{id}/deliver` | `deliver:mr` | Push the branch, open or update the merge request. |
| `DELETE /v1/runtime/v1/sessions/{id}` | `sessions:write` | Forget it. |

Session status values are `running`, `done`, `aborted`, `error`, `no_progress`,
`exhausted` and `unverified`. Only `done` is a clean finish.

### 4.4 Approvals

Relevant only under `approval_policy: interactive`.

| Route | Scope | Does |
|---|---|---|
| `GET /v1/runtime/v1/approvals` | `sessions:read` | Everything of yours waiting on a decision. |
| `POST /v1/runtime/v1/approvals/{id}` | `sessions:write` | Answer it. |
| `POST /v1/runtime/v1/approvals/{id}/extend` | `sessions:write` | Give it more time. |

```json
{ "decision": "accept" }
```

`decision` is `accept`, `reject` or `edit`. An `edit` also needs
`"arguments": { … }` — the corrected arguments for the tool call.

`410` means the approval is gone: already answered, timed out, or the run ended.
All three are "too late" rather than something to retry.

**An approval nobody answers suspends the run** after the server's timeout (30
minutes by default). If your client cannot answer approvals, use `auto_safe`.

### 4.5 The agenda

Work the agent proposed for later, per workspace.

| Route | Scope |
|---|---|
| `GET /v1/runtime/v1/workspaces/{id}/agenda` | `sessions:read` |
| `POST /v1/runtime/v1/workspaces/{id}/agenda` | `agenda:write` |
| `POST /v1/runtime/v1/workspaces/{id}/agenda/{task_id}` | `agenda:write` |

---

## 5. The event stream

```http
GET /v1/runtime/v1/sessions/{id}/events
Authorization: Bearer <token>
Accept: text/event-stream
```

Server-sent events, one JSON object per frame. Resume after a drop with
`?since_id=<last id>` or the `Last-Event-ID` header — nothing is lost.

Event types:

| | |
|---|---|
| `turn_start` | A turn began. |
| `assistant_delta` / `assistant` | Streamed text, then the complete message. |
| `tool_call` / `tool_result` | A tool ran, and what it returned. |
| `tool_pending` | A tool is waiting on an approval. |
| `plan` | The plan was created or revised. |
| `gate` | Build, vet, lint or tests ran. |
| `usage` / `metrics` / `quota` | Token usage, timings, quota position. |
| `steer` | A follow-up you sent was taken in. |
| `user` | A message from you was recorded. |
| `finish` / `end` | The run's outcome, then the stream's close. |
| `error` | Something failed. |
| `heartbeat` | Keep-alive. Ignore it. |

Streams are long-lived: a coder turn can generate for minutes. Set a read
timeout of at least 600 seconds, and do not buffer.

Over A2A, `message/stream` wraps the same information as JSON-RPC task updates,
ending with an `artifact-update` frame carrying the changed files.

---

## 6. Limits and refusals

| Status | Means | Do |
|---|---|---|
| `401` | No token, expired, or bad. | Get a new token and retry once. |
| `403` | Scope missing, or the repository is not allowlisted for you. | Read the message. Neither is retryable. |
| `404` | No such session, workspace or approval — or not yours. | — |
| `409` | A run is already going on that workspace; you asked to deliver a run that did not finish cleanly; or you asked to deliver a run from a scratch workspace. | Wait, lease again, pass `override`, or run it on a real repository. |
| `410` | The approval is gone. | Stop waiting on it. |
| `413` | The clone is larger than the server allows. | — |
| `429` | You hit a per-caller limit. | Back off and retry. |
| `502` | The clone or the merge request failed upstream. | Tell the operator. |
| `503` | Quota or audit is unreachable. | **Retry in a minute.** Requests are refused rather than allowed unmetered. |

Default per-caller limits:

| | Default |
|---|---|
| Workspaces held at once | 3 |
| Runs going at once | 2 |
| Clone size | 4 GiB |
| Lease lifetime | 7 days |
| Approval wait before suspending | 30 minutes |

Your quota position is always readable at `GET /v1/quota` (scope `llm`).

---

## 7. The model proxy (optional)

If your client wants the models directly rather than the agent, the gateway
fronts an OpenAI-compatible endpoint:

```http
POST /v1/llm/chat/completions      # scope: llm
GET  /v1/models                    # scope: llm
```

Usage is metered and charged to you exactly as an agent run is. This is the only
way to the models — the key never leaves the gateway.

---

## 8. Unauthenticated routes

| Route | Gives |
|---|---|
| `GET /.well-known/agent-card.json` | The agent card. |
| `GET /v1/health` | Version, capabilities, and the limits in force. |
| `GET /v1/tools` | The tool schemas the agent may call. |

Health is the right readiness probe. Its `capabilities.identity` tells you which
identity provider is live.

---

## 9. Current deployment status

Read this before you build against the guide — a few things above are
implemented but not yet switched on here.

| | Status |
|---|---|
| A2A, hosted REST, event streams, model proxy | **Live.** |
| Client-credentials grant (`/v1/auth/token`) | **Live.** Verified end to end over HTTPS. |
| GitLab sign-in for people | **Not published.** The gateway runs a development identity provider, so sign-in stays loopback-only. People get hand-minted tokens from the operator. |
| Merge request delivery | **Needs a GitLab service token that is not configured yet.** Until it is, `deliver` pushes the branch but opens no merge request, and private repositories cannot be cloned at all. |
| Repository allowlist | Only repositories the operator has listed can be leased. Ask for yours to be added. |
| Browser clients | CORS is not configured. A browser-based client needs its origin allowed first; a server-side client does not. |

None of these change the shapes above. They are switches, not redesigns.

---

## 10. A checklist for a new client

1. Fetch `/.well-known/agent-card.json` and confirm you can reach the server.
2. Ask the operator for a `client_id` and secret, the scopes you need, and your
   repositories on the allowlist.
3. Get a token, and cache it for just under 15 minutes.
4. Pick a surface — A2A for "run this", REST for a UI over workspaces.
5. Decide your approval posture. If you cannot answer approvals, pass
   `auto_safe` explicitly (REST defaults to `interactive`).
6. Handle `401` by re-minting, `429`/`503` by backing off, `403` by stopping.
7. Release workspaces you are finished with.
8. Read `metadata.branch` off a finished task, and deliver deliberately.
