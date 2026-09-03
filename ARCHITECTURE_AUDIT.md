# DakCoder — Architecture Audit

Audit date: 2026-09-02. Basis: full read of `apps/agent`, `apps/shared`, `apps/gateway`,
`extension/src`, plus 11 executable reproductions run against the real modules
(see `TEST_PLAN.md`). Everything below distinguishes **CURRENT IMPLEMENTATION**
(what the code does today, with file references) from **RECOMMENDED ARCHITECTURE**
(what should change). Companion documents: `AUDIT.md` (full findings),
`BUGS.md` (actionable table), `CHANGE_PLAN.md` (ordered fixes),
`task.md` (what has been implemented, and what it changed).

> **Remediation in progress.** This document is kept current as the change plan
> lands: a paragraph describing behaviour that has since been fixed is marked
> **FIXED** with the step number, and the CURRENT description is rewritten to say
> what the code does now. Anything still marked as a defect is still a defect.
> Last synchronised after **Phase 3 complete** — every step of
> `CHANGE_PLAN.md`, including the four rows that were previously deferred
> (L-18, GW-7..13, the EXT UI set, and rehydrating a context from
> `events.jsonl`). What is still open is listed at the end of §10, and it is two
> things: a multi-worker gateway needs a shared store, and the two findings the
> audit itself marked BY-DESIGN.

> A note on the documentation, kept because the lesson outlives the defect: the
> top-level `README.md` stated that the agent loop, the tool router, the gateway
> and the VS Code extension were "not built yet" — all four existed and were
> substantially complete. Three module docstrings made claims the code did not
> honour: events "persisted" (they were appended to a list), recaps written to
> `.dakcoder/session-<id>/recap.md` (nothing ever wrote that file), and "Resume
> continues on this same transcript" (it built a fresh context). All four are
> fixed — the first three by changing the prose, the last by changing the code so
> the prose became true. Treat all prose in this repository as hypothesis; this
> document records what was verified.

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
  session.py       Session (event log, monotonic ids, journalled), SessionStore, revert
  undo.py          UndoStore — pre-run snapshots under .dakcoder/sessions/<id>/undo
  journal.py       events.jsonl + session.json per session; restored at startup
  rehydrate.py     replays a stored transcript back into a ContextManager, so a
                   follow-up after a daemon restart continues the conversation
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
| loopback.py | `sessions`, `approvals`, `contexts{id→ContextManager}`, `loops{id→AgentLoop}`, credential | asyncio event loop + one thread per run; approvals block the run thread on a **polled** `threading.Event` wait, so `/extend` reaches it (1.10) | steer-loss race **fixed (1.9)**; contexts/loops never evicted (L-12); transcripts still do not survive restart (L-7), though revert's pre-images do (1.4) |
| session.py | event log (list, monotonic ids), mutation paths, cancel/wind-down flags, steer queue (open/closed, decided under the lock) | `threading.Lock` around the log and the steer queue; status fields unguarded | transcripts are still process memory (L-7); `UndoStore` under `.dakcoder/sessions/<id>/` is the first thing that is not (1.4) |
| loop.py | `_State` (20 fields): ledgers, plan, gate state, phase counters | single worker thread; baseline on a second thread | orphaned batch calls **fixed (1.1)**, phase-counter lockout **fixed (1.8)**, carry self-wipe open (L-5) |
| context.py | message list, layers, caps, slices, compaction, calibration; **the wire invariant** (`wire()` repairs and reports) | single-threaded (per run) | retention **fixed (1.7)**, recap replaced not merged (L-4), orphan-retaining cut (L-6 — repaired at the wire since 1.2, not yet at the cut) |
| router.py | `touched`, `mutations`, approval policy | called from run thread AND baseline thread | fresh per message → carried loop state desyncs (L-5) |
| gate.py | Baseline, stage sequence | run thread; baseline thread | baseline join timeout → inconsistent excuses (L-16) |
| shared/llm.py | HTTP client, retries, SSE parse, ToolCall assembly | synchronous, per-call | **fixed (1.12)**: EOF without `[DONE]`/`finish_reason` raises retryably, index-less deltas key on `id`, nameless slots are loud |
| gateway | quota counters (Redis/mem), auth state (memory), ledger | asyncio, single worker | **fixed (1.11)**: `recheck` on the protocol and implemented, disconnect releases the reservation, `SET NX GET` for idempotency, hot-path input validated. Per-process reservation state remains (GW-5) |

Circular dependencies: none of significance. `router.py` ↔ `commands.py` share
`MissingToolchain` by placing it in router (documented). The real coupling issue
is not circular imports but **duplicated state** (see §5).

---

## 2. Request lifecycle — CURRENT IMPLEMENTATION

1. Extension `POST /v1/tasks {task, intent}` → `Loopback.start` → `SessionStore.create`
   → USER event recorded → `_spawn` builds AgentLoop (fresh ContextManager, **fresh
   Router**, an `UndoStore` keyed on the session), reopens the steer queue, starts a
   daemon worker thread.
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
   released; the steer queue is closed and anything left in it becomes a
   follow-up run (1.9). Follow-up (`/messages` on a finished session) reuses the ContextManager
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
| gate | fail | AGENT | report as user msg; `gate_failures++`; `research_turns` reset | **FIXED (1.8)** — the fence forces `"required"`, never a named `finish`, while a gate is failing |
| any | terminal tool inside a batch | (transition) | remaining calls answered by `_answer_unrun` | **FIXED (1.1)** — and `wire()` is a checkpoint behind it (1.2) |
| any | cancel | ABORTED | pending batch calls answered ✔ | ✔ (this path does it right) |
| any | reply truncated (finish_reason=length) | same | all calls answered with explanation ✔ | **no counter → loops to EXHAUSTED (L-13)** |
| any | stalled ≥2 | forced finish | `must_answer` | ✔ mechanism; wrong message after a *refused* terminal (L-14) |
| any | 3 compactions/8 turns | NO_PROGRESS | — | fires spuriously on write-heavy runs because compaction is impotent there (L-3) |

Impossible/contradictory states found:
- ~~**Forced-finish + "make the edit"**~~ — **FIXED (1.8)**. `_gate_wants_an_edit()`
  makes the fence force `"required"` rather than the phase's terminal tool
  whenever a failing gate is asking for a change, and a mutating batch resets
  `research_turns` because writing is not research.
- ~~**"Already in context above" for evicted/never-inserted lines**~~ —
  **FIXED (1.5, 1.6)**. The ledger is written from the span that survived the
  insertion cap and rebuilt from `ContextManager.coverage()` after every
  compaction.
- `forced` (the once-per-run re-ask) is deliberately run-scoped, but it crosses
  phase boundaries: a Planner that consumed it leaves the acting mode without
  its one re-ask (prior-audit TC-4 — now documented as intent, still cross-phase).
  Open; step 2.x.

---

## 4. Context lifecycle — CURRENT IMPLEMENTATION

Layers (eviction order): WORKING_SET → RECAP → TASK → DIRECTIVE → MODE →
SYSTEM. Task, directive, mode and system are pinned. Assembly order is *not*
eviction order: `DIRECTIVE` — the plan and the developer's steers — is assembled
below the working set, which is what stopped a steer re-prefilling the
conversation (L-18, §10). Pinned is about eviction; compaction only ever
consumes `_working`. Insertion caps per tool (`TOOL_CAPS`, e.g. read_file
48k tokens head-keep) with machine-readable elision markers. Compaction summarises
the evicted working set into a structured `Recap` (schema-constrained call,
`role="summariser"`), retains to a token floor, and `_whole_turn_cut` mostly keeps
call/result pairs together.

What breaks:
- ~~The loop's read ledger is written from the tool's span~~ — **FIXED (1.5)**.
  `_apply_cap` returns the surviving span, `append_tool_result` stamps it on the
  message as `line_range`, and the elision marker names it so "re-read a narrower
  range" is actionable. A cap that keeps no content line reports `None`, which
  callers must read as *no* coverage — `None` means "the whole file" to
  `_contains`, and passing it through would stub out every earlier read.
- ~~Compaction never invalidates the loop ledgers~~ — **FIXED (1.6)**. `compact()`
  records an `Eviction`; `coverage()` answers what is still held;
  `AgentLoop._forget_evicted` rebuilds the read ledger from it and clears the
  cached-result ledgers, which cannot truthfully say "you already have this"
  after an eviction. `dead_ends` and `seen_calls` survive deliberately.
- ~~`_retention_cut` and `novel_tokens` count `content` only~~ — **FIXED (1.7)**.
  One `_message_cost()`, used by all three.
- ~~A second compaction replaces the first recap~~ (R5) — **FIXED (2.1)**.
  `Recap.merge` folds each recap into the one before it, so `do_not_retry` and
  the decisions survive an arbitrary number of compactions.
- The last-message-never-evicted rule can retain a tool result whose assistant
  was evicted → orphaned `tool_call_id` on the wire (R6). The cut is unchanged
  (step 2.3), but `wire()` now repairs and reports it (1.2), so it can no longer
  reach the endpoint.
- ~~Every steer/follow-up/pin rewrites the TASK message, which sits **above** the
  working set — the entire suffix re-prefills~~ (prior CM-6) — **FIXED (3.3)**,
  after being measured, which is what the change plan asked for. At a
  100-turn context a steer re-prefilled 75,764 of 80,543 tokens (94.1%); it now
  costs 35. The pinned block was split: the task and the acceptance criteria are
  the stable head and stay above the working set, the plan and the directives
  move to `Layer.DIRECTIVE` below it. Numbers and reasoning in §10.

---

## 5. Duplicated state — the load-bearing problem

The prior audit's core claim — *"context manager state and loop ledger state can
disagree about what the model has actually seen"* — is **fixed**, row by row.
It was not, when this section was first written: the mechanism had moved and the
class had survived. The table is kept in full rather than deleted, because which
copy was authoritative is the thing a future change has to preserve:

| Fact | Copy 1 | Copy 2 | Divergence trigger | Status |
|---|---|---|---|---|
| Lines of file X shown to the model | context messages (post-cap, post-compaction) | `_State.reads` ledger | insertion cap; compaction; developer edits between messages | **FIXED for cap + compaction (1.5, 1.6)** — `Message.line_range` now means "the lines in this message", `ContextManager.coverage()` is the authority, and `_forget_evicted` rebuilds the ledger from it. Developer edits between messages remain (L-25, step 2.6) |
| Result of call F | context tool message | `_State.last_results[F][:6000]` | 6k char cut; compaction; workspace drift across messages | **FIXED (1.6, 2.5)** — cleared on eviction, and a replayed body that was cut says so instead of presenting itself as the current answer |
| Mutation count | `router.mutations` | `_State.mutations_seen` | every follow-up after a mutating run → ledger wipe | **FIXED (2.2)** — `carry_from` adopts the previous Router, so both are the session's one count and the follow-up's first batch no longer wipes the ledgers |
| Files the run changed | `router.touched` | `session.mutations` (from events) / plan targets | follow-up: plan says unwritten, session says written | **FIXED (2.2)** — the Router is session-scoped and carried with the context |
| Files the run changed, *before* it changed them | — | `UndoStore` manifest on disk | — | **NEW, and deliberately single-authority (1.4)**: revert reads the snapshot, never HEAD |
| "The run is over" | `loop.result` | `session.status` (set later, from another thread) | steer between the two → message lost | **FIXED (1.9)** — `Session.steer`/`close_steer` decide inside the lock; the status becomes terminal strictly before the queue closes, and a leftover becomes a follow-up |
| What was already broken | `Baseline` (background thread) | gate verdicts | 180 s join timeout → some gates baselined, some not | **FIXED (3.7)** — the thread reference is kept, a late baseline is marked late, and the gates it should have excused are re-verified |
| Turn/compaction counts | `context._compactions` | `_State.compactions` (thrash detector) | emergency compaction in `_complete` bypasses the loop's list | **FIXED (1.6)** — every compaction routes through `AgentLoop._compact` |
| What a message costs | `usage()` | `_retention_cut`, `novel_tokens` | tool-call arguments counted by one and not the others | **FIXED (1.7)** — one `_message_cost()` |
| The conversation itself | `Loopback.contexts[id]` (in RAM) | `events.jsonl` (on disk) | any daemon restart → the record survives, the conversation does not | **FIXED** — `rehydrate.py` rebuilds the context from the transcript; see §7 and §10 |

**RECOMMENDED**: collapse each row to one authority. Specifically: (a) the read
ledger must be *written by the ContextManager* from what was actually inserted
and *invalidated by it* on compaction/supersede — the loop should query, never
own, "what has the model seen"; (b) `Router` (or at least `touched`/`mutations`)
must live at session scope and be carried with the context, not reborn per
message; (c) `Recap` must be cumulative (merge previous recap fields on each
compaction).

**Progress**: all three are done.

(a) The ContextManager is the authority. `_apply_cap` reports the span that
survived, `append_tool_result` stamps it on the message as `line_range`, and
`_re_reading` asks `coverage()` what the model can still see rather than
consulting a remembered answer — so an eviction by any path is visible
immediately, and `_forget_evicted` keeps the persisted ledger honest without the
refusal depending on it having run. `_State.reads` still holds how *often* a file
has been asked for, which is a fact about the run and cannot go stale the same
way. A developer editing a file between messages is caught by mtime at
`carry_from` (2.6), and the model is told rather than silently re-permitted.

(b) `carry_from` adopts the previous Router, so `touched`, `mutations` and the
undo store are session-scoped (2.2).

(c) `Recap.merge` folds each recap into the one before it (2.1).

---

## 6. Tool lifecycle — CURRENT vs RECOMMENDED

CURRENT (after 1.1, 1.2, 1.4): schema from registry (mode-filtered,
handler-filtered) → model calls → fingerprint intercept (dead-end / cached /
re-read) → dispatch (coerce → confine → approve → **snapshot the pre-image if the
tool mutates** → run) → result capped into context with `tool_call_id`, the
message carrying the span that survived the cap.

The invariant "every declared call gets exactly one result" is now enforced in
two places rather than four-and-a-half. `AgentLoop._answer_unrun` is the single
helper every abandoning path calls — cancellation mid-batch, a terminal tool that
ended the phase, the forced-terminal cap — and `ContextManager.wire()` is the
checkpoint behind it: it synthesises `"was not run"` results for any declared-but-
unanswered id, turns an orphaned result into a user message carrying the same
text, and records what it repaired in `wire_repairs`, which the loop emits as an
ERROR event. The repair is a backstop, not the fix: the regression tests assert
`wire_repairs` is empty, so a loop that stops answering its own calls still fails
the suite even though the request would be accepted.

---

## 7. Session lifecycle, resume, follow-up — CURRENT

CURRENT: the transcript, the session summary and the mutation list are on disk
(`journal.py`, `undo.py`) under `.dakcoder/sessions/<id>/`, and `SessionStore`
restores the summaries when the daemon starts; the transcript is read back only
when something asks for one. A session that was running when the process died
comes back ERROR and resumable rather than permanently "running".

**And the conversation comes back too.** `rehydrate.py` replays the stored
events through the ContextManager's own append methods, so a follow-up after a
restart continues where the run left off instead of starting the task again.
Loops and approvals remain process memory, which is the split that is left: the
messages survive a restart, the loop's `_State` ledgers do not, and an in-flight
run does not. The ledger loss is one-directional — the agent may repeat a search
it had already exhausted; it will not skip work it has not done. See §10.

Follow-up = same context + `carry_from`, which now also adopts the previous
Router, so the change set, the mutation count and the undo store are the
session's rather than the message's (2.2). Resume is the same path with a
different message: reused context, carried ledgers, fresh turn budget (2.7).
When the daemon holds no context — after a restart — it rebuilds one from the
transcript; re-seeding the original task is the last resort now rather than the
first, and it remains for the cases where there is genuinely nothing to
continue: no transcript, an unreadable one, or a session that never got a reply.

A message typed while the last turn is in flight is no longer lost (1.9): the
steer queue is opened per run and closed atomically as the run ends, so a
correction is either delivered to the live run or handed back to the loopback,
which turns it into a follow-up on the same context.

The three things this section recommended are all done: (a) resume is a true
continuation, the follow-up path with a different message (2.7); (b) the event
log and the mutation list are on disk, so revert and the transcript survive a
restart and the docstrings that claimed it are true (2.9); (c) `contexts` and
`loops` are evicted when `SessionStore` trims or deletes a session (3.1).

What is left is the run in flight. A process that dies mid-turn loses that turn
— the model call is not idempotent and replaying it is not free — and the
session comes back ERROR and resumable, which is the honest outcome rather than
a pretence of one.

## 8. Revert — CURRENT (rewritten in step 1.4)

CURRENT: `Router.dispatch` copies a path's pre-image to
`.dakcoder/sessions/<id>/undo/` the first time a *mutating* tool touches it —
before the handler runs, first-write-wins, with a JSON manifest recording
`file` / `absent` / `too_large` / `unreadable`. `SessionStore.plan_revert` reads
that manifest:

| pre-run state | revert does |
|---|---|
| a file was there | restore those bytes |
| nothing was there | delete — the run created it |
| too large, unreadable, or **not recorded** | **blocked**, with the reason named |

git is no longer consulted for the decision, which is what makes revert correct
on a dirty tree, correct on an untracked file, and possible at all in a directory
that is not a repository. A path with no snapshot — written by a tool that
bypasses the router's hook, or by a session that predates the store — is refused
rather than guessed at: restoring it to HEAD is a guess with a developer's
uncommitted work as the stake. `revert` returns what it actually did (restored,
deleted, failed) rather than echoing the plan it was handed.

The manifest is on disk, so a daemon restart between the run and the revert no
longer turns "restore what was there" back into "reset to HEAD". `.dakcoder/`
writes a `.gitignore` containing `*` on first use, so none of this appears in the
developer's `git status`.

Residual: `MAX_SNAPSHOT_BYTES` is 2 MB, and a file above it blocks rather than
half-records. Tools that write outside `_confine` (`fx_wire`, `govalid_gen` —
BUG RG-1) produce unsnapshotted mutations, which block; closing RG-1 closes this
too.

---

## 9. Failure/recovery paths — verified behaviour

- LLM transport error → run ends ERROR with FINISH+END emitted (loopback wraps a
  crashed generator the same way) ✔. Since 1.12 a silently truncated success is no
  longer possible: a stream ending without `[DONE]` or a `finish_reason` raises a
  retryable `UpstreamError` instead.
- Over-budget prompt → one emergency compaction (retain 15%) then ERROR ✔, and
  since 1.6 that path routes through `AgentLoop._compact`, so it counts against
  the thrash detector and invalidates the ledgers like any other. Since 1.7 a
  write-heavy context can actually be reduced by it.
- Approval timeout → refusal, run continues ✔. The wait polls, so `/extend`
  works (1.10); the deadline is `DAKCODER_APPROVAL_TIMEOUT`, which the extension
  sets from `dakcoder.approvalTimeoutSeconds` and where `0` genuinely means no
  deadline; a decision arriving after the timeout gets 410 rather than a receipt
  for something that did not happen.
- Cancellation: checked between turns and before each batch call; abandoned calls
  answered ✔ (the one place the orphan invariant is done right).
- Baseline failure → announced, gate degrades to blame-the-run (documented) ✔;
  join timeout 180 s → *silently* inconsistent between gates ✘.


---

## 10. What the remediation changed, and what it did not

Added since the audit, as places where a fact now has one owner:

| Module | Owns | Replaces |
|---|---|---|
| `undo.py` | what was at a path before the run first touched it | inferring it from HEAD at revert time (§8) |
| `journal.py` | the transcript, the session summary, the mutation list, on disk | a list in a process that a VS Code reload restarts |
| `ContextManager.coverage()` | which lines of which files are in the working set | the loop's read ledger answering it from memory. `_re_reading` asks it on every read; `_State.reads` is now only "how often has this file been asked for", which is a fact about the run rather than about the messages |
| `ContextManager.wire()` | the tool-call invariant | four call sites each remembering it, two forgetting |
| `ContextManager._message_cost()` | what a message costs | `usage()` and `_retention_cut` disagreeing |
| `Router.model_mutations` | "has the model changed anything since the gate" | a count the gate's own stages inflated |
| `AgentLoop._answer_unrun()` | answering a batch's abandoned calls | three paths, one of which did it |
| `rehydrate.py` | a conversation rebuilt from the transcript on disk | `follow_up` re-seeding the original task whenever the daemon had restarted (§7) |
| `Layer.DIRECTIVE` | the plan and the developer's directives, below the working set | the same two things inside the pinned `TASK` block, above it (§4) |
| `auth/service._fingerprint` | recognising a refresh token | holding the credential itself as a dictionary key |
| `GitLabIdentity._client` / `aclose` | the adapter's one HTTP connection pool | a fresh `AsyncClient` per call, closed never |

### The four rows that were open, and what closed them

**L-18 — a steer no longer re-prefills the conversation.** The change plan said
measure before moving, so it was measured, with the manager's own
`novel_tokens` ("what a prefix cache actually has to prefill") over a context of
the shape a migration run reaches:

| turns | prompt | a steer re-prefilled | now |
|---|---|---|---|
| 5 | 8,633 | 3,854 (44.6%) | 35 |
| 20 | 19,983 | 15,204 (76.1%) | 35 |
| 50 | 42,693 | 37,914 (88.8%) | 35 |
| 100 | 80,543 | 75,764 (94.1%) | 35 |
| 200 | 156,243 | 151,464 (96.9%) | 35 |

The same sentence appended to the working set cost 11 tokens, so a developer
typing one correction at turn 100 paid to re-read the whole run, and `set_plan`
— which fires on every plan submission — paid the same. The prior audit carried
this as CM-6, accepted by design; what was missing was the number, and the
number is not marginal. The fix splits what was one pinned block: the task
statement and the acceptance criteria never change after `set_task` and stay
where §6.1 put them, while the plan and the directives move to `Layer.DIRECTIVE`
below the working set. Pinned is about eviction, not position — compaction only
ever consumes `_working` — so nothing became evictable, and the instruction the
developer typed most recently now sits closest to the model's next token, which
is where a correction belongs anyway.

**GW-7..13 — the gateway rows, closed as code rather than deferred as a
deployment decision.** Refresh tokens are no longer held at all: the service
stores SHA-256 of one and the plaintext exists only inside the request that
presents it, which is the same discipline as a password store and needs no
infrastructure. The GitLab access token captured at sign-in is still held —
`recheck` has to be able to use it — but is `repr=False`, so it is no longer one
exception context away from a log. `_revoked_families` was a set that only grew;
it expires with the refresh TTL now. The identity adapter built a fresh
`AsyncClient` on every call and closed none, which with GW-1 fixed became one
leaked pool per session per fifteen minutes; it keeps one and the app closes it.
Group membership is read past the first page, bounded at twenty — roles are
mapped from group paths, and a truncated group list is a wrong answer that looks
exactly like a correct one. The quota script's ZSET member ended in
`sha1hex(key .. now .. i)`, a hash of three values already in the member, so two
charges in one timestamp collapsed into a score update and the second was lost;
the nonce comes from the caller now. The upstream pool had no ceiling — writing
an explicit `httpx.Limits` to raise the keep-alive bound silently removed
httpx's own default cap of 100 — and has one. The in-memory ledger, which is
what every deployment without `DAKCODER_POSTGRES_DSN` runs on, was unbounded and
is now a stated window that counts what it drops.

Still open here, and genuinely a deployment decision: reservation and auth state
are per-process, so a multi-worker gateway can still double-charge across
workers. That needs the `QuotaStore` seam extended to sessions, and a Redis to
point it at.

**EXT-11/12/13/14, EXT-19..22 — the UI rows.** The elapsed clock measured the
session rather than the run, so a follow-up on a session opened before lunch
drew "Working… 2h 41m" twenty seconds in; a run boundary now restarts it. The
cache percentage had two denominators — the host divided by the context
*budget*, the webview by `prompt_tokens` — under a comment claiming the wire
carries both figures "so that no two surfaces can round their way to different
answers"; there is one function now, over the prompt. The raw re-emission sat
above the monotonic guard, so a duplicate the state machine correctly refused
was still handed to every consumer, appending the row twice and putting a second
gate-rerun offer on screen. A reconnect spliced streamed text across the outage
hole, because deltas are not replayed and the buffer was not cleared. The gate
re-run's task listener was disposed only on the event it waited for, so every
cancelled run leaked a listener and left its promise pending for ever. Notices
raised while the panel was closed were dropped by `post` and never replayed.
`deactivate` aliased `context.subscriptions`, which VS Code disposes itself
after `deactivate` returns — every disposable in the extension was torn down
twice on shutdown.

**Rehydration — a restart now restores the conversation, not only the record.**
`journal.py` made the transcript survive a daemon restart, which is what the
panel needs; it is not what the agent needs. `follow_up` said so in its own
comment: after a restart there was no context to continue, so it re-seeded the
original task. In practice that meant a developer reloading their VS Code window
at turn 40 and typing "carry on with the repo layer" got an agent that began the
migration again, with the transcript proving it had already read the service on
screen beside it. `rehydrate.py` replays the stored events through the
ContextManager's own append methods — same insertion caps, same read-slice
ledger, same supersession rules, one assembler — and `_spawn` calls it when the
daemon holds no context for a session. Re-seeding the task is the last resort
now rather than the first.

Three properties it is written for. A turn is replayed whole or not at all,
because half a turn is exactly the wire defect `wire()` exists to repair,
manufactured on purpose. The budget bound is deterministic — the newest whole
turns that fit into 55% of the prompt budget, and a message saying how many were
dropped — because summarising would mean a billed model call the developer did
not ask for while they wait for a window to reload. And the call's arguments are
carried to its result, so a restored read keeps its line range and the re-read
intercept still knows the model has seen that file.

What does not come back is the loop's `_State` ledgers: which searches were
exhausted, which reads were refused, how many times a gate failed. Those live in
`AgentLoop.carry_from`, and a restored session starts them empty. The loss is
one-directional — the agent may repeat a search it had already exhausted; it
will not skip work it has not done.

**SH-6 — the one the board would never have reached.** Working `CHANGE_PLAN.md`
end to end closes every row the plan names, which is not the same as every row
in the register: SH-6 is a P1 line in `BUGS.md` that no step claimed. A sweep
over every ID, asking what in the tree answers for it, found it. `DeltaCoalescer`
evaluated its `max_interval` deadline only inside `feed`, and nothing calls
`feed` while the model is silent — so the check ran on the arrival of the next
fragment, describing the gap that had just ended rather than the one currently
open. A model pausing mid-sentence held its last characters until it resumed:
the "reads as a hang" behaviour the docstring directly above the code says the
interval exists to prevent. `flush_due()` is the question a clock can ask, and
`AgentLoop._complete` runs a ticker for the length of a streamed call; the
coalescer took a lock, because the ticker and the stream are different threads.

The lesson is about the method rather than the defect: a change plan is a list
of the work someone decided to do, and the register is the list of what is
wrong. Reconciling the two at the end is a separate step, and it found something.

### Reported from a live run, and not in the audit at all (FS-1..4)

A transcript of turns 29-33 of a run asked for a report over ten files:
`write_file` cut off, `write_file` cut off, `run_terminal cat > report.md`
refused, `write_file` cut off, `write_file` cut off. The model narrated "in
chunks", "in parts", "split it into multiple files and combine" — right three
times — and every attempt failed identically.

The cause is a composition the audit did not look for, because each tool is
correct on its own. A reply carries prose, tool name and the whole `content`
argument inside one `max_tokens` budget (6,144 for `agent`), which caps a single
`write_file` at about 24 KB. `write_file` refuses to overwrite, which is a
deliberate safety property. `patch_file` needs a unique anchor in a file that
already has one, which a new document's first chunk does not. `run_terminal`
cannot write. Three correct tools compose to "documents up to 24 KB only", and
no tool description, refusal or error said so. That is the shape worth
remembering: this audit read components and seams, and this defect is in neither
— it is in what the set of tools can and cannot express.

`write_file` takes `append` now, so chunked writing is uniform and there is no
anchor to guess; an append adds no trailing newline (a chunk boundary can fall
mid-word) and records `MODIFY` rather than `CREATE` (`revert` reads the kind to
choose between restoring bytes and deleting the file). The truncation advice
branches on the shape of the overrun instead of telling a lone oversized call to
send fewer calls. The truncation bound gained a run total, because the streak
resets on any whole reply and one refused call between two oversized ones was
enough to dodge it. And a redirection in argv is answered with the tool that
writes rather than the one that reads.

The `append` parameter cost 41 tokens of stable prefix and broke the ceiling
`test_prompts.py` asserts, which is the tripwire working; the text was tightened
twice and the ceiling then moved deliberately, with the trade written beside it.

The audit's own §12 ("what can safely wait") is where the deferred list came
from. It is now empty apart from the multi-worker gateway store above and the
two rows the audit itself marked BY-DESIGN (GT-2/3, PI-1).
