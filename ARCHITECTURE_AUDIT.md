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
> Last synchronised after **Phase 3** — every step of `CHANGE_PLAN.md` except
> the rows listed as deliberately deferred in `task.md`.

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
  session.py       Session (in-memory event log, monotonic ids), SessionStore, revert
  undo.py          UndoStore — pre-run snapshots under .dakcoder/sessions/<id>/undo
  journal.py       events.jsonl + session.json per session; restored at startup
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

Layers (eviction order): WORKING_SET → RECAP → TASK → MODE → SYSTEM. Task, mode
and system are pinned. Insertion caps per tool (`TOOL_CAPS`, e.g. read_file
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
- **A second compaction replaces the first recap** instead of folding it in (R5).
  Open; step 2.1.
- The last-message-never-evicted rule can retain a tool result whose assistant
  was evicted → orphaned `tool_call_id` on the wire (R6). The cut is unchanged
  (step 2.3), but `wire()` now repairs and reports it (1.2), so it can no longer
  reach the endpoint.
- Every steer/follow-up/pin rewrites the TASK message, which sits **above** the
  working set — the entire suffix re-prefills (prior CM-6; a real cost, ~200k
  tokens of prefill per steer at high context). Open; step 3.3, measure first.

---

## 5. Duplicated state — the load-bearing problem

The prior audit's core claim — *"context manager state and loop ledger state can
disagree about what the model has actually seen"* — is **NOT fixed**. The
mechanism moved but the class survived. Current duplicate representations:

| Fact | Copy 1 | Copy 2 | Divergence trigger | Status |
|---|---|---|---|---|
| Lines of file X shown to the model | context messages (post-cap, post-compaction) | `_State.reads` ledger | insertion cap; compaction; developer edits between messages | **FIXED for cap + compaction (1.5, 1.6)** — `Message.line_range` now means "the lines in this message", `ContextManager.coverage()` is the authority, and `_forget_evicted` rebuilds the ledger from it. Developer edits between messages remain (L-25, step 2.6) |
| Result of call F | context tool message | `_State.last_results[F][:6000]` | 6k char cut; compaction; workspace drift across messages | **partly fixed (1.6)** — cleared on eviction; the 6k cut is still unmarked (L-17, step 2.5) |
| Mutation count | `router.mutations` (reborn 0 per message) | `_State.mutations_seen` (carried) | every follow-up after a mutating run → ledger wipe | open (L-5, step 2.2) |
| Files the run changed | `router.touched` (reborn per message) | `session.mutations` (from events) / plan targets | follow-up: plan says unwritten, session says written | open (step 2.2) |
| Files the run changed, *before* it changed them | — | `UndoStore` manifest on disk | — | **NEW, and deliberately single-authority (1.4)**: revert reads the snapshot, never HEAD |
| "The run is over" | `loop.result` | `session.status` (set later, from another thread) | steer between the two → message lost | **FIXED (1.9)** — `Session.steer`/`close_steer` decide inside the lock; the status becomes terminal strictly before the queue closes, and a leftover becomes a follow-up |
| What was already broken | `Baseline` (background thread) | gate verdicts | 180 s join timeout → some gates baselined, some not | open (L-16, step 3.7) |
| Turn/compaction counts | `context._compactions` | `_State.compactions` (thrash detector) | emergency compaction in `_complete` bypasses the loop's list | **FIXED (1.6)** — every compaction routes through `AgentLoop._compact` |
| What a message costs | `usage()` | `_retention_cut`, `novel_tokens` | tool-call arguments counted by one and not the others | **FIXED (1.7)** — one `_message_cost()` |

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
when something asks for one. Contexts, loops and approvals are still process
memory — a restart loses the *conversation state* and keeps the record of it,
which is the split that matters: revert and the transcript survive, the
in-flight run does not (and a session that was running when the process died
comes back ERROR and resumable rather than permanently "running").

Follow-up = same context + `carry_from`, which now also adopts the previous
Router, so the change set, the mutation count and the undo store are the
session's rather than the message's (2.2). Resume is the same path with a
different message: reused context, carried ledgers, fresh turn budget (2.7). When
the daemon holds no context — after a restart — resume falls back to re-seeding
the original task and says so.

A message typed while the last turn is in flight is no longer lost (1.9): the
steer queue is opened per run and closed atomically as the run ends, so a
correction is either delivered to the live run or handed back to the loopback,
which turns it into a follow-up on the same context.

**Still recommended**: evicting `contexts`/`loops` is done (they are dropped with
their session); what remains is that a *restart* cannot resume the conversation
itself, only the record of it. Rehydrating a ContextManager from `events.jsonl`
is the next step if that becomes worth having.

RECOMMENDED: (a) either make resume a true continuation (reuse context +
carry, exactly the follow-up path with a synthetic user message) or change the
user-facing copy — the current combination is a false promise; (b) persist, at
minimum, the event log and mutation list per session (append-only JSONL under
`.dakcoder/`) so revert and transcript survive a restart — the docstrings already
claim this happens; (c) evict `contexts`/`loops` when `SessionStore` trims or
deletes a session.

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

Deliberately not done, with the reason:

* **L-18** — every steer rewrites the pinned TASK layer above the working set, so
  the suffix re-prefills. The change plan says measure first and nothing has
  measured it; moving directives below the stable head is a prompt-shape change
  that would invalidate the budget regression suite's baselines.
* **GW-7..13** — refresh tokens are still plaintext in memory and still lost on a
  gateway restart; the reservation state is still per-process, so a multi-worker
  deploy can still double-charge. Both need a store, which is a deployment
  decision rather than a code one.
* **EXT-11/12/13/14, EXT-19..22** — elapsed clock, two cache-% denominators,
  duplicate raw re-emission, spliced streaming text after a reconnect, listener
  and notice leaks. All UI correctness, none of it losing work.
* **A ContextManager rehydrated from `events.jsonl`** — the record of a
  conversation survives a restart; the conversation does not. §7.

The audit's own §12 ("what can safely wait") is where these came from, and the
list has not been widened: everything in Phase 0, 1 and 2 landed, and Phase 3
landed apart from the rows above.
