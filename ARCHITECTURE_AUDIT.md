# DakCoder — Architecture Audit

Audit date: 2026-09-02. Basis: full read of `apps/agent`, `apps/shared`, `apps/gateway`,
`extension/src`, plus 11 executable reproductions run against the real modules
(see `TEST_PLAN.md`). Everything below distinguishes **CURRENT IMPLEMENTATION**
(what the code does today, with file references) from **RECOMMENDED ARCHITECTURE**
(what should change). Companion documents: `AUDIT.md` (full findings),
`BUGS.md` (actionable table), `CHANGE_PLAN.md` (ordered fixes).

> A note on the documentation: the top-level `README.md` states "The agent loop,
> the tool router, the gateway … and the VS Code extension" are "not built yet".
> All four exist and are substantially complete. Several module docstrings make
> claims the code does not honour (events "persisted", recaps written to
> `.dakcoder/session-<id>/recap.md`, "Resume continues on this same transcript").
> Treat all prose in this repository as hypothesis; this document records what
> was verified.

---

## 1. Component map — CURRENT IMPLEMENTATION

```
VS Code extension (extension/src, TypeScript)
  extension.ts     activation, command registration
  runtime.ts       spawns dakcoderd (venv + wheel), parses {port,pid} from stdout
  client.ts        HTTP + SSE client to the loopback (Bearer loopback token)
  session-state.ts client-side session/event model
  chat.ts + media/chat/chat.js   webview panel
  approvals.ts     approval cards → POST /v1/approvals/{id}
  auth.ts          gateway sign-in (JWT), pushes fresh JWT to POST /v1/credential
        │  HTTP + SSE (127.0.0.1, loopback token)
        ▼
dakcoderd — the local runtime (apps/agent)
  serve.py         entry point: binds port 0, prints it, wires everything
  loopback.py      FastAPI app: /v1/tasks, /v1/sessions/*, /v1/approvals/*, SSE
  session.py       Session (in-memory event log, monotonic ids), SessionStore
  loop.py          AgentLoop — the state machine (one worker thread per run)
  context.py       ContextManager — the only message-list builder
  llm.py           complete(): context + mode config + client → one turn
  modes.py         ASK / PLANNER / AGENT budgets, temperatures, roles
  gate.py          inner_loop (gofmt+lint) and full_gate (13 ordered stages)
  tools/
    router.py      6-check dispatch pipeline, approvals, path confinement
    registry.py    ToolSpec table: schema, modes, approval level
    fs.py          read/write/patch/delete/search
    commands.py    go_*, git_*, run_terminal (argv allow-list, subprocess)
    gotools.py     bridge to the Go sidecar (JSON over stdio)
    control.py     submit_plan / ask_developer / finish (terminal tools)
    knowledge.py   search_docs (BM25 over the bundled corpus)
        │  HTTPS, JWT, metering headers (X-Estimated-Tokens, X-Lane, X-Turn)
        ▼
dakcoder-gateway (apps/gateway) — the only holder of model keys
  app.py           /v1/llm/{path} proxy route, /v1/auth/*, /v1/quota, /v1/health
  auth/            GitLab OAuth, HS256 JWT mint/verify, refresh families
  quota/           reserve → stream → reconcile; MemoryStore or Redis (Lua)
  proxy.py         SSE relay; tees the usage chunk; settles in a sibling task
  ledger.py        usage events → Postgres (or memory)
  routing.py       role → model/endpoint/key table
        │
        ▼
LiteLLM → vLLM → Qwen3.8-27B (max_model_len 262,144)
```

Shared contracts live in `apps/shared` (`dakcoder_shared`): `llm.py` (the HTTP/SSE
client both the agent and gateway-probe use), `envelope.py` (C1 tool results, C2
events, DeltaCoalescer), `tokens.py` (estimator + Calibration), `paths.py`
(Workspace confinement, protected globs), `config.py`.

### Responsibilities, state, concurrency per subsystem

| Subsystem | Owns | Concurrency | Key failure modes found |
|---|---|---|---|
| Extension | UI session cache, credential lifecycle | VS Code event loop | see AUDIT §Frontend |
| loopback.py | `sessions`, `approvals`, `contexts{id→ContextManager}`, `loops{id→AgentLoop}`, credential | asyncio event loop + one thread per run; approvals block the run thread on a `threading.Event` | steer-loss race (BUG L-9); contexts/loops never evicted (L-12); no state survives restart (L-7) |
| session.py | event log (list, monotonic ids), mutation paths, cancel/wind-down flags, steer queue | `threading.Lock` around the log; status fields unguarded | **no persistence at all**, despite docstrings (L-7) |
| loop.py | `_State` (20 fields): ledgers, plan, gate state, phase counters | single worker thread; baseline on a second thread | orphaned batch calls (L-1), phase-counter lockout (L-2), carry self-wipe (L-5) |
| context.py | message list, layers, caps, slices, compaction, calibration | single-threaded (per run) | retention blind to tool-call args (L-3), recap replaced not merged (L-4), orphan-retaining cut (L-6) |
| router.py | `touched`, `mutations`, approval policy | called from run thread AND baseline thread | fresh per message → carried loop state desyncs (L-5) |
| gate.py | Baseline, stage sequence | run thread; baseline thread | baseline join timeout → inconsistent excuses (L-16) |
| shared/llm.py | HTTP client, retries, SSE parse, ToolCall assembly | synchronous, per-call | mid-stream EOF = fake success (SH-1); index-less tool deltas merge calls (SH-3) |
| gateway | quota counters (Redis/mem), auth state (memory), ledger | asyncio, single worker | refresh dead in prod (GW-1), reserve leak on disconnect (GW-2), idempotency race (GW-4) |

Circular dependencies: none of significance. `router.py` ↔ `commands.py` share
`MissingToolchain` by placing it in router (documented). The real coupling issue
is not circular imports but **duplicated state** (see §5).

---

## 2. Request lifecycle — CURRENT IMPLEMENTATION

1. Extension `POST /v1/tasks {task, intent}` → `Loopback.start` → `SessionStore.create`
   → USER event recorded → `_spawn` builds AgentLoop (fresh ContextManager, **fresh
   Router**), starts a daemon worker thread.
2. Worker: `AgentLoop.run` → intent decided (param, or one schema-constrained
   `role="fast"` call; unclassifiable → ASK) → `_switch(PLANNER|ASK)`; AGENT intent
   also launches the baseline thread (`take_baseline`: unscoped go_build/go_vet/
   go_mod-check/rules_lint[,go_test]).
3. Per turn (`_turn`): drain steer → `_gate_stalled` check → `begin_turn` →
   `observe_tool_schemas` → stall/fence handling (may append a user message and
   force `tool_choice`) → maybe `compact` → `complete()` (budget check → `client.chat`
   streaming; deltas coalesced → `on_event` → `call_soon_threadsafe` → session log →
   SSE subscribers) → forced re-ask if a must-call mode emitted prose → append
   assistant (+tool_calls) → answer truncated calls / dispatch batch through
   `Router.dispatch` (intercepts answered from ledgers without dispatch) → results
   appended with `tool_call_id` → terminal tools transition phases → mutation ⇒
   `inner_loop` (gofmt+lint).
4. Phase ends: `submit_plan` pins plan, switches to AGENT (joins baseline);
   `finish` in AGENT → `_verify` → `full_gate` (skipped when `mutations == 0`;
   cached when the workspace is byte-identical) → clean ⇒ DONE, failing ⇒ report
   appended as a user message, bounded by `MAX_GATE_FAILURES`.
5. End: FINISH then END events; `session.finish(result)`; pending approvals
   released. Follow-up (`/messages` on a finished session) reuses the ContextManager
   and `carry_from`s the previous loop; **resume** re-seeds `task + "The previous
   attempt ended: …"` into a **fresh** context (it is a new run, not a continuation).

---

## 3. The state machine — CURRENT IMPLEMENTATION

States: `INTENT(auto|ask|agent)` → `ASK | PLANNER` → (`PLANNER→AGENT` via
submit_plan) → terminal `{DONE, ABORTED, UNVERIFIED, NO_PROGRESS, EXHAUSTED, ERROR}`.
Session statuses mirror outcomes plus RUNNING.

| Current | Event | Next | Mutations | Invariant expected |
|---|---|---|---|---|
| (new) | run(task, intent) | ASK / PLANNER | task pinned; baseline thread (AGENT) | intent fixed before turn 1 ✔ |
| PLANNER | submit_plan ok | AGENT | plan pinned; `research_turns`,`forced_terminal` reset; baseline joined | phase state reset — **partial: `forced`, per-phase nothing else** |
| PLANNER | ask_developer ok | DONE | — | questions on screen ✔ |
| PLANNER | prose after forced re-ask | DONE | — | honest "nothing to plan" ✔ |
| ASK | finish ok / prose | DONE | — | ✔ |
| AGENT | finish ok, targets unwritten | AGENT (refused once) | `finish_refused++` | bounded ✔ (but path comparison unnormalised — BUG L-19) |
| AGENT | finish ok / prose | gate | gate key = (mutations, touched) | gate never on empty change-set ✔ |
| gate | ok | DONE | `gate_failures=0` | ✔ |
| gate | fail | AGENT | report as user msg; `gate_failures++` | **violated when `research_turns≥12`: next turn forces `finish`, model cannot edit (L-2)** |
| any | terminal tool inside a batch | (transition) | **remaining calls orphaned — protocol violation (L-1)** | every declared call answered ✘ |
| any | cancel | ABORTED | pending batch calls answered ✔ | ✔ (this path does it right) |
| any | reply truncated (finish_reason=length) | same | all calls answered with explanation ✔ | **no counter → loops to EXHAUSTED (L-13)** |
| any | stalled ≥2 | forced finish | `must_answer` | ✔ mechanism; wrong message after a *refused* terminal (L-14) |
| any | 3 compactions/8 turns | NO_PROGRESS | — | fires spuriously on write-heavy runs because compaction is impotent there (L-3) |

Impossible/contradictory states found:
- **Forced-finish + "make the edit"**: gate report says "Make the edit"; the same
  turn's `tool_choice` names `finish` (L-2). Reproduced (R11).
- **"Already in context above" for evicted/never-inserted lines** (L-8/L-10, R1/R2).
- `forced` (the once-per-run re-ask) is deliberately run-scoped, but it crosses
  phase boundaries: a Planner that consumed it leaves the acting mode without
  its one re-ask (prior-audit TC-4 — now documented as intent, still cross-phase).

---

## 4. Context lifecycle — CURRENT IMPLEMENTATION

Layers (eviction order): WORKING_SET → RECAP → TASK → MODE → SYSTEM. Task, mode
and system are pinned. Insertion caps per tool (`TOOL_CAPS`, e.g. read_file
48k tokens head-keep) with machine-readable elision markers. Compaction summarises
the evicted working set into a structured `Recap` (schema-constrained call,
`role="summariser"`), retains to a token floor, and `_whole_turn_cut` mostly keeps
call/result pairs together.

What breaks (all reproduced):
- The **loop's read ledger is written from the tool's span, not from what survived
  the insertion cap** — the two disagree the moment a read is elided (R1).
- **Compaction never invalidates the loop ledgers** — reads, cached results,
  truncation marks all survive the eviction of the content they describe (R2).
- **`_retention_cut` and `novel_tokens` count `content` only** while `usage()`
  counts `tool_calls` arguments too — a write-heavy working set is visible to the
  compaction *trigger* and invisible to the compaction *cut* (R3).
- **A second compaction replaces the first recap** instead of folding it in (R5).
- The last-message-never-evicted rule can retain a tool result whose assistant
  was evicted → orphaned `tool_call_id` on the wire (R6).
- Every steer/follow-up/pin rewrites the TASK message, which sits **above** the
  working set — the entire suffix re-prefills (prior CM-6; a real cost, ~200k
  tokens of prefill per steer at high context).

---

## 5. Duplicated state — the load-bearing problem

The prior audit's core claim — *"context manager state and loop ledger state can
disagree about what the model has actually seen"* — is **NOT fixed**. The
mechanism moved but the class survived. Current duplicate representations:

| Fact | Copy 1 | Copy 2 | Divergence trigger | Reproduced |
|---|---|---|---|---|
| Lines of file X shown to the model | context messages (post-cap, post-compaction) | `_State.reads` ledger | insertion cap; compaction; developer edits between messages | R1, R2 |
| Result of call F | context tool message | `_State.last_results[F][:6000]` | 6k char cut; compaction; workspace drift across messages | R1 (cut), code-proven |
| Mutation count | `router.mutations` (reborn 0 per message) | `_State.mutations_seen` (carried) | every follow-up after a mutating run → ledger wipe | R10 |
| Files the run changed | `router.touched` (reborn per message) | `session.mutations` (from events) / plan targets | follow-up: plan says unwritten, session says written | code-proven |
| "The run is over" | `loop.result` | `session.status` (set later, from another thread) | steer between the two → message lost | code-proven race |
| What was already broken | `Baseline` (background thread) | gate verdicts | 180 s join timeout → some gates baselined, some not | code-proven |
| Turn/compaction counts | `context._compactions` | `_State.compactions` (thrash detector) | emergency compaction in `_complete` bypasses the loop's list | code-proven |

**RECOMMENDED**: collapse each row to one authority. Specifically: (a) the read
ledger must be *written by the ContextManager* from what was actually inserted
and *invalidated by it* on compaction/supersede — the loop should query, never
own, "what has the model seen"; (b) `Router` (or at least `touched`/`mutations`)
must live at session scope and be carried with the context, not reborn per
message; (c) `Recap` must be cumulative (merge previous recap fields on each
compaction).

---

## 6. Tool lifecycle — CURRENT vs RECOMMENDED

CURRENT: schema from registry (mode-filtered, handler-filtered) → model calls →
fingerprint intercept (dead-end / cached / re-read) → dispatch (coerce → confine
→ approve → run) → result capped into context with `tool_call_id`. Invariant
"every declared call gets exactly one result" is enforced at *four* independent
call sites (truncation, cancellation mid-batch, normal flow, `_parseable_arguments`
at wire time) and **missed at two**: terminal-tool early return (R4) and the
compaction tail edge (R6).

RECOMMENDED: enforce the invariant structurally, once — before any request is
dispatched, assert (and repair) that every `tool_calls[].id` in the assembled
message list has exactly one following `role:"tool"` message; synthesise
"not run: <reason>" results for any gap. That turns four scattered disciplines
and two bugs into one checkpoint that cannot be bypassed by a new code path.

---

## 7. Session lifecycle, resume, follow-up — CURRENT vs RECOMMENDED

CURRENT: sessions, transcripts, contexts, loops, approvals all live in process
memory. Follow-up = same context + `carry_from` (partially self-defeating, R10)
+ fresh Router. Resume = **new run** on a fresh context seeded with
`task + previous summary`, while the EXHAUSTED message promises "Resume continues
on this same transcript". Restart of `dakcoderd` = total loss (including the
mutation list `revert` depends on).

RECOMMENDED: (a) either make resume a true continuation (reuse context +
carry, exactly the follow-up path with a synthetic user message) or change the
user-facing copy — the current combination is a false promise; (b) persist, at
minimum, the event log and mutation list per session (append-only JSONL under
`.dakcoder/`) so revert and transcript survive a restart — the docstrings already
claim this happens; (c) evict `contexts`/`loops` when `SessionStore` trims or
deletes a session.

## 8. Revert — CURRENT vs RECOMMENDED

CURRENT (`session.py:315-356`): restore touched tracked paths to **HEAD**; delete
touched paths absent from HEAD. This assumes the agent was the only writer since
HEAD. A developer's uncommitted pre-run edit to a file the agent later touched is
destroyed; a developer's *untracked* file the agent merely modified is **deleted**.

RECOMMENDED: snapshot pre-run content of every file at first mutation (the router
already sees every mutation; a `.dakcoder/session-<id>/undo/` copy at
first-touch is cheap) and revert to the snapshot, not to HEAD; at minimum,
`plan_revert` must diff working-tree-vs-HEAD *before* the run (or per mutation
record `MutationKind.CREATE` vs the pre-existing state) and mark mismatches
`blocked` instead of restoring/deleting.

---

## 9. Failure/recovery paths — verified behaviour

- LLM transport error → run ends ERROR with FINISH+END emitted (loopback wraps a
  crashed generator the same way) ✔ — but shared/llm.py can hand back a silently
  truncated success first (SH-1), which no layer above detects.
- Over-budget prompt → one emergency compaction (retain 15%) then ERROR ✔ —
  except write-heavy contexts where the compaction is a no-op (R3) and the run
  dies with a message blaming the working set.
- Approval timeout (600 s, extendable) → refusal, run continues ✔; races between
  decide and timeout resolve as reject-but-client-told-accept (minor).
- Cancellation: checked between turns and before each batch call; abandoned calls
  answered ✔ (the one place the orphan invariant is done right).
- Baseline failure → announced, gate degrades to blame-the-run (documented) ✔;
  join timeout 180 s → *silently* inconsistent between gates ✘.
