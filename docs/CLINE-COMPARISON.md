# dakcoder vs Cline: state and loop comparison

Date: 2026-09-09. Cline read from `github.com/cline/cline` at commit
`fee4fb96f2c946a54d4f083eebba6b6b4b73cf9b` (main, 2026-09-09).

**Read this first.** Cline has been rewritten since the architecture most blog
posts describe. The agent loop now lives in an SDK package
(`sdk/packages/agents/src/agent-runtime.ts`), session persistence, compaction,
checkpoints and the hub daemon live in `sdk/packages/core`, and the VS Code
extension (`apps/vscode`) is a thin host that translates SDK events into its old
`ClineMessage` rows. The `Task` class in `src/core/task/index.ts` that the
repo's own `.clinerules/cline-overview.md` still describes no longer exists.
Everything below is against the code on `main`, not the write-ups.

Your side is `apps/agent/src/dakcoder_agent` (`loop.py`, `context.py`,
`session.py`, `journal.py`, `rehydrate.py`, `undo.py`, `tools/router.py`,
`tools/control.py`, `gate.py`, `loopback.py`, `serve.py`) and `extension/src`.

---

## 0. What has since been built

**Read this before section 1.** Sections 1 and 2 describe dakcoder as it was on
2026-09-09. The analysis still holds as reasoning; several of its findings have
since been acted on, and the rows they describe no longer match the code. What
changed, and where:

| Was | Now | Files |
|---|---|---|
| One `ContextManager` list; compaction replaced it with a recap | Append-only `Transcript`, whole tool results, never rewritten | `transcript.py` |
| Compaction rewrote history | Compaction writes a `CompactionState` sidecar; the projection applies it; a prefix hash refuses one that no longer describes the records under it | `compaction.py` |
| Caps, slice supersession and repeat-collapse applied at insertion | All three applied at projection, so the record keeps the full text | `projection.py` |
| Nine loop ledgers invalidated by hand on every eviction | The loop asks the projection: `context.coverage()`, `visible_results`, `visible_bodies`. `_forget_evicted` is four lines | `loop.py`, `projection.py` |
| `_State`, 38 flat fields | Five groups — `TaskState`, `CallLedger`, `ReadState`, `GateState`, `Progress` — behind a name table | `loopstate.py` |
| Tool behaviour hard-wired in `_tool_calls` | `beforeTool`/`afterTool` seam; a hook may rewrite, deny, answer or annotate, and cannot impersonate a tool | `hooks.py` |
| Every call dispatched in sequence | A batch that is all pure lookups runs at once, bounded at four threads; `ToolSpec.parallel` is opt-in per tool | `hooks.py`, `tools/registry.py` |
| Plan lived in a tuple, lost on restart | `PlanRecord` with step statuses and a revision history, on disk, restored on resume | `plan.py` |
| No cross-session backlog | `AgendaTask` / `AgendaStore`: the agent proposes, a person approves | `plan.py` |
| An endpoint context-length 400 ended the run `ERROR` | Classified, compacted deterministically, retried once, and only if the prompt actually shrank | `loop.py` |
| `_digest` buried inside the summariser | `basic` is a named strategy: no model call, selectable | `compaction.py` |
| Three separate "is this run done" checks | One `_why_not_done()` predicate | `loop.py` |
| `dakcoder.compactContext` had no route | `POST /v1/sessions/{id}/compact` | `loopback.py` |
| "What did the model see at turn 30" had no answer | `GET /v1/sessions/{id}/transcript`, `view=canonical` or `view=model` | `loopback.py` |

New tests: `test_transcript.py`, `test_hooks.py`, `test_plan.py`,
`test_recovery.py`, plus route tests in `test_loopback_routes.py`.

**Still outstanding** from section 3: per-turn git checkpoints with compare and
partial restore (item 1, and still the clearest single gap), session ownership
and optimistic locking, approvals as routed state, the three runtime roles,
read-only subagents, and the hosted-product items. Those are the ones that need
the runtime in [host-plan.md](../host-plan.md) rather than a change inside the
loop.

---

## 1. Summary

| # | Dimension | Your agent | Cline | Edge |
|---|---|---|---|---|
| 1 | Canonical transcript | One `ContextManager` list; compaction replaces the working set with a recap; durable record is `events.jsonl` | Append-only `AgentMessage[]` that compaction never rewrites; compacted view stored beside it as a sidecar | Cline |
| 2 | Working context | Six-layer build, per-mode budget, insertion caps, slice ledger, calibrated estimate, state block every turn | `prepareTurn`: sidecar projection, then `MessageBuilder` truncation; 3 chars/token, no calibration | Yours |
| 3 | Session state | `Session` in process, `session.json` summary; RUNNING becomes ERROR on restart | SQLite index with optimistic locking, JSON manifest, hub-owned sessions survive client exit | Cline |
| 4 | Turn state | `_State` with 38 fields and 21 named bounds; `max_turns` 40 (10 to 400) | `AgentRuntime.state`: iteration, pending calls, usage, last error; no iteration cap by default | Yours for bounds, Cline for shape |
| 5 | Tool-call state | Router with six ordered checks, batch rules, intercept ledgers, coverage intervals, forced `tool_choice` | Tool policies, before/after hooks, parallel execution, loop detector 3/5, mistake tracker | Yours |
| 6 | Tool-result state | Capped at insertion, slice supersession, `wire()` coherence repair | Stored in full, truncated at projection, outdated-read rewrite, hook context blocks | Even |
| 7 | Completion state | `finish` tool, plan-abandonment refusal, deterministic gate with baseline | `submit_and_exit` with `completesRun`, `requireCompletionTool`, `completionGuard`, exactly-once `task.completed` | Yours |
| 8 | Plan state | Typed `PlanStep` with status, `revise_plan`, loop-initiated replan | Plan/Act/YOLO as tool presets plus a command guard; no in-loop plan object; agenda tasks are a cross-session backlog | Yours |
| 9 | File/change state | `touched`/`mutations` counters, per-path pre-image snapshot, whole-session revert, mtime check on follow-up | Per-run git checkpoint in private refs with untracked files; restore files, task, or both; compare diff | Cline |
| 10 | Context budget | Window arithmetic: 219,136 prompt + 32,768 output + 10,240 reserve; EMA calibration against real `prompt_tokens` | Trigger at 0.9 of max input, target 0.7, preserve 20k recent; 3 chars/token flat | Yours |
| 11 | Compaction | Retain 35% by tokens, structured `Recap`, chunked summariser, ledger invalidation, thrash detector | Agentic or basic strategy, overflow recovery, sidecar keyed by prefix hash, manual `/compact`, imported-history fold | Even, different strengths |
| 12 | Message projection | `wire()` for the model; the event stream is the display | Three projections: provider, display wrapper type, `ClineMessage` with `seq`/`epoch` | Cline |
| 13 | Persistence | Best-effort journal, `session.json`, undo manifest; loop ledgers not persisted | `sessions.db` + manifest + messages file + compaction sidecar + checkpoint metadata, atomic writes | Cline |
| 14 | Extension state | `RunState` derived from SSE with `since_id` resume; server-authoritative context | `SdkController` plus coordinators; `StateManager` cache; convergent `ClineMessage` merge | Even |
| 15 | Runtime state | One daemon per window, worker thread per run, approval blocks the thread | Hub daemon, spoke workers, local/hub/remote modes, capability brokerage across clients | Cline |

Rough score: you lead on the loop and the working context (rows 2, 4, 5, 7, 8,
10). Cline leads on durability and the things around the loop (rows 1, 3, 9,
12, 13, 15). That is the same split the caf comparison found, from a different
codebase. Your loop is the better loop. Your state is the less durable state.

---

## 2. Row by row

### 2.1 Canonical transcript

**Yours.** [`ContextManager`](../apps/agent/src/dakcoder_agent/context.py) owns
one list of frozen `Message` objects in layers. The working set is append-only
until `compact()` evicts a prefix of it and installs a `Recap` message in the
`RECAP` layer. After that the canonical list *is* the compacted list: the
evicted messages exist only in `.dakcoder/sessions/<id>/events.jsonl`. Restoring
after a restart (`rehydrate.py`) replays events, not the recap, so a restored
session is "newest whole turns that fit 55% of the budget" rather than "what
the model was looking at".

**Cline.** `AgentRuntime.state.messages` is append-only for the life of the
conversation. Compaction never touches it. `createCompactionStateAwarePrepareTurn`
(`sdk/packages/core/src/extensions/context/compaction.ts`) writes a
`SessionCompactionState` sidecar: the compacted messages plus
`source_message_count` and a `source_prefix_hash` over the canonical prefix
they replace. On every turn the sidecar is projected over the canonical list
(`compacted + canonical[count:]`); if the hash no longer matches, the sidecar
is ignored and a fresh compaction runs. Synthetic messages the runtime injects
(reminders, hook contexts, compaction notices) carry `metadata.userRunSpan: 0`
and `displayRole: "system"` so they reach the model but not the transcript UI.

**Take-away.** Cline's split makes three things cheap that are expensive for
you: restoring the exact compacted context after a restart, "restore task to
before this compaction", and auditing what the model actually saw. Your journal
already holds the canonical record; what is missing is persisting the recap and
the retained-set boundary as a sidecar so `rehydrate` can rebuild the compacted
view instead of re-deriving from raw turns.

### 2.2 Working context

**Yours.** `build()` assembles `system -> mode -> task -> recap -> working set
-> plan & directives`, with the volatile block last so a steer costs 11 tokens
of prefill instead of re-prefilling the conversation (D-30 to D-35 in
[ARCHITECTURE.md](ARCHITECTURE.md)). Tool results are capped at insertion
(`ToolCap`), superseded file slices collapse to stubs, `wire()` repairs any
declared-but-unanswered call and reports the repair as an error event,
`Calibration` folds real `prompt_tokens` back into the estimate, and
`_state_block()` renders plan, change set, last gate and ruled-out items every
turn.

**Cline.** `prepareTurn` is a host-supplied projection hook: it receives the
canonical messages and returns what to send, and the runtime states outright
that the result "does not replace the canonical runtime transcript". Core wires
two stages: the compaction projection above, then `MessageBuilder`
(`sdk/packages/core/src/session/services/message-builder.ts`, 1,727 lines) which
truncates tool results to 8,000 chars, file content to 50,000, assistant text to
200,000, repeated tool-call markup to 12,000, caps total text at 6 MB, rewrites
earlier reads of a re-read file to `[outdated - see the latest file content]`
(batched in 64 KB steps to protect prefix caches), and synthesises a result for
any orphaned tool call. Token estimation is `ceil(chars / 3)` everywhere, chosen
to over-count. There is no layering, no pinned task block, and no state block;
the system prompt carries a workspace metadata JSON and the mode contract.

**Take-away.** Yours is the stronger design here. Two Cline details are worth
lifting: the outdated-read rewrite is your slice supersession applied at
projection time rather than insertion time, which keeps the full read in the
canonical record; and `MessageBuilder` batching rewrites to protect the prefix
cache is the same concern your `novel_tokens` metric measures.

### 2.3 Session state

**Yours.** [`Session`](../apps/agent/src/dakcoder_agent/session.py) holds
status, events with monotonic ids, mutations, cancel and wind-down flags, and
the steer queue. `SessionStore` keeps up to 200 in memory and restores
summaries from `session.json` at startup; any session that was RUNNING when the
daemon died is marked ERROR with the summary "the runtime stopped while this run
was in flight". Contexts and loops are held per session in `Loopback.contexts`
and `Loopback.loops` and dropped on forget.

**Cline.** `ActiveSession` (`sdk/packages/core/src/types/session.ts`) carries the
runtime, pending prompts with `delivery: "queue" | "steer"`, the compaction
state and a write queue for it, per-turn usage baselines, team run tracking,
and two exactly-once flags (`submitAndExitObserved`, `taskCompletedEmitted`).
Persisted as a `SessionRow` in `~/.cline/data/sessions/sessions.db` with a
`statusLock` for optimistic concurrency (`withOccRetry`, four retries), plus a
zod-validated `SessionManifest` JSON, a messages file and an optional
compaction sidecar. A stale-session reconciler marks rows whose `pid` is dead.
Under the hub, a session is owned by the daemon and a spoke worker, so a client
closing does not end the run and another client can attach mid-flight.

**Take-away.** The concrete gap is that your RUNNING-to-ERROR rule is correct
for a per-window daemon and wrong for the hosted service in
[host-plan.md](../host-plan.md). Cline's `statusLock` and pid-based reconciler
are the minimum you need once two processes can see one session.

### 2.4 Turn state

**Yours.** `_State` in [`loop.py`](../apps/agent/src/dakcoder_agent/loop.py) is
38 fields (its docstring says 20). Every bound is a named constant with the
field measurement that set it: `MAX_STALLED_TURNS` 6, `STALLS_BEFORE_ANSWER` 2,
`MAX_RESEARCH_TURNS` 12, `MAX_FORCED_TERMINAL` 2, `MAX_TRUNCATED_TURNS` 3,
`MAX_TRUNCATIONS` 6, `MAX_GATE_FAILURES` 3, `MAX_FINISH_REFUSALS` 1,
`MAX_REPLANS` 1, `MAX_REVISIONS` 2, `MAX_CALLS_PER_BATCH` 6, compaction thrash
3-in-8. `max_turns` defaults to 40 and is clamped to 10..400 in `serve.py`.
Outcomes: `done`, `aborted`, `unverified`, `no_progress`, `exhausted`, `error`.

**Cline.** `AgentRuntime.state` is small: `runId`, `status`, `iteration`,
`messages`, `pendingToolCalls`, `usage`, `lastError`, `lastErrorClass`, and one
`overflowRecoveryAttempted` flag. The loop is `while (maxIterations === undefined
|| iteration < maxIterations)`, and `maxIterations` is undefined by default. The
CLI sets `maxConsecutiveMistakes` to 3, core defaults to 6. `apiTimeoutMs`
defaults to 180 s. Result status is `completed | aborted | failed`; the legacy
facade adds `max_iterations` and `mistake_limit`. Everything stateful beyond
that is pushed into per-session components: `MistakeTracker`,
`LoopDetectionTracker`, `MessageBuilder`, the compaction pipeline, checkpoint
hooks.

**Take-away.** Do not copy Cline's bounds; yours are measured and theirs are
defaults. Do copy the shape: Cline's loop is 2,188 lines because the ledgers and
trackers are separate objects installed as hooks. Your `_intercept`,
`_re_reading`, `_overlap`, `_record_read` and the seven counters that feed
`_gate_stalled` would be one `Ledgers` component with `inspect(call)` and
`observe(call, result)` methods, and `_State` would shrink to what the loop
itself decides on.

### 2.5 Tool-call state

**Yours.** [`Router.dispatch`](../apps/agent/src/dakcoder_agent/tools/router.py)
runs six checks in a fixed order (exists, visible in mode, implemented,
arguments valid, paths confined, approval needed), every refusal carries a
`fix`. The loop adds batch rules (duplicate in one reply answered once,
`finish` alongside other calls refused, calls past six answered "not run"),
three intercept ledgers (dead ends, cached results with partial marking,
coverage-based re-read refusal), body digests for "informed", overlap detection
for `search_docs` and `search_repo`, and the forced `tool_choice` escape. Calls
run sequentially on the worker thread.

**Cline.** Streamed `tool-call-delta` parts are assembled per call; a call with
unparseable JSON is recorded in `metadata.invalidToolCalls` and answered with an
error result rather than dispatched. `prepareToolExecution` then runs
`beforeTool` hooks (may rewrite input, change policy, skip, or append context),
resolves `toolPolicies` (`enabled`, `autoApprove`, with a `*` wildcard), and
calls `requestToolApproval` if not auto-approved. Execution is sequential by
default or `Promise.all` in parallel mode (`maxParallelToolCalls` 8).
`LoopDetectionTracker` (`sdk/packages/core/src/runtime/safety/loop-detection.ts`)
compares tool name plus a key-sorted JSON signature of the input; the CLI
enables it at soft 3 (notice injected) and hard 5 (stop through the mistake
path). `MistakeTracker` counts API errors, invalid calls, and iterations where
every tool failed, then asks the host `onConsecutiveMistakeLimitReached` to
continue with guidance or stop.

**Take-away.** Yours is far more specific, and correctly so for a 27B model.
Two things to borrow: `beforeTool`/`afterTool` as a hook seam (your gate and
inner loop are hard-wired where a hook would do), and parallel execution for a
batch that is all reads.

### 2.6 Tool-result state

**Yours.** `append_tool_result` caps at insertion via `ToolCap`, records the
surviving line span so the coverage ledger is written from what is in context,
supersedes older slices of the same file, and every declared call gets exactly
one result (`_answer_unrun` for the abandoned ones, `wire()` as the backstop).
Overlap notes go in as `role: user` because no tool produced them.

**Cline.** One `tool` message per call carrying a `tool-result` part with
`output` and `isError`. Skipped calls (policy, denied approval, parse error,
provider-executed) still produce an error result so the transcript stays
coherent. `afterTool` hooks may replace the result or append context; appended
contexts are batched into one `user` message after the results, wrapped in
`<hook_context source= tool_name= tool_call_id=>` with sanitised attributes so
hook output cannot spoof the block. Truncation happens at projection, and a
missing result on resume is synthesised as "Tool execution was interrupted".

**Take-away.** Even. You cap early and keep budgets honest; Cline keeps the full
result and trims on the way out. If you adopt a canonical/projection split
(2.1), your caps move to projection time and the journal keeps whole results.

### 2.7 Completion state

**Yours.** [`finish`](../apps/agent/src/dakcoder_agent/tools/control.py) ends a
phase because the model cannot reliably stop by not calling a tool. In AGENT
mode `finish` hands over to the gate, which never runs on an empty change set,
caches its verdict by `(model_mutations, touched)`, and reports pre-existing
failures as advisory against a baseline taken before the first edit. A `finish`
that abandons plan targets is sent back once. `_done_summary` distinguishes
"stages passed" from "stages did not apply".

**Cline.** A run completes when the assistant returns no tool calls, or when a
tool with `lifecycle.completesRun` (`submit_and_exit`, off in the act preset,
on in yolo) returns without error. `completionPolicy.requireCompletionTool`
injects a `[SYSTEM] This run is not complete until you call ...` reminder and
loops; `completionGuard()` can veto completion with a nudge (used for in-flight
team tasks). `ActiveSession` guarantees exactly one `task.completed` per session
between the tool observer and the teardown fallback. There is no verification
step; correctness is the model's claim plus whatever the user runs.

**Take-away.** Yours is ahead for a coding agent because of the gate. Cline's
`completionGuard` is a cleaner version of your `MAX_FINISH_REFUSALS` push-back:
a predicate that returns the reason the run is not done, or nothing.

### 2.8 Plan state

**Yours.** `submit_plan` returns typed `PlanStep(file, action, accepts, status,
note)`. The loop sets `done` from the change set and `failed` from gate output;
the model may set `skipped` through `revise_plan` with a reason that lands in
the `tried` list. A second gate failure after an edit triggers one loop-initiated
replan carrying "what has been tried". The plan renders in the pinned
`DIRECTIVE` layer and in the state block.

**Cline.** Plan mode is a tool preset (`ToolPresets.plan`: no `editor`,
`run_commands` allowed) plus a command-guard hook that hard-blocks mutating
shell commands, plus a system-prompt contract; the CLI exposes
`switch_to_act_mode`, the extension expects the user to flip the toggle. User
messages are wrapped in `<user_input mode="plan|act|yolo">` and a `<mode_notice>`
marks a switch. There is no plan object inside the loop; the old Focus Chain
todo list survives only as settings and file helpers in `apps/vscode`, not in
the SDK loop. Separately, `AgendaTaskRecord` (`sdk/packages/shared/src/tasks.ts`)
is a persistent cross-session backlog (todo, follow-up, handoff, reminder) with
approval, revision, priority and run records in `tasks.db`, driven by the hub
and the kanban app.

**Take-away.** Yours wins inside a run. Cline's agenda is a different thing:
work the agent proposes for later, approved by a person, run unattended. That
maps onto your hosted plan, not onto `loop.py`.

### 2.9 File/change state

**Yours.** `Router` counts `mutations` and `model_mutations` (gate-tool writes
excluded), keeps `touched`, and calls `UndoStore.capture` before the first
write to a path so revert restores the developer's bytes rather than HEAD
(BUG L-11). Snapshots over 2 MB are recorded as blocked. Revert is
whole-session and refuses while running. Stale reads are detected by mtime only
when a follow-up starts (`_drop_stale_reads`).

**Cline.** `createCheckpointHooks` (`sdk/packages/core/src/hooks/checkpoint-hooks.ts`)
snapshots the worktree before and after each run into private refs
(`refs/cline/...`) using a per-session scratch `GIT_INDEX_FILE` so untracked
files are included without re-hashing the tree every turn, with a HEAD
fallback and telemetry per snapshot. Checkpoint metadata (`latest`, `history`)
lives in the session manifest. Restore runs inside a transaction
(`stash push --include-untracked` to a private ref, then commit or rollback)
and can restore files, messages, or both; compare produces per-file left/right
content. The docs still say "shadow git repository"; the code now uses the real
repo's object store with private refs. `FileContextTracker` (chokidar watchers
that flag files edited outside the agent) exists in `apps/vscode` but is only
consumed by mentions, not by the SDK loop.

**Take-away.** This is Cline's clearest lead. Per-turn checkpoints with compare
and partial restore is what makes auto-approve tolerable, and your gate already
knows exactly when a mutation batch lands (`_inner_loop`), which is the natural
checkpoint boundary.

### 2.10 Context budget

**Yours.** `ModeConfig` refuses to construct if
`prompt_budget + max_tokens + OUTPUT_RESERVE > CONTEXT_WINDOW` (262,144 = 219,136
+ 32,768 + 10,240). Compaction fires at 70% of the prompt budget. Tool schema
tokens are observed per turn. `Calibration` is an EMA (0.2) of chars-per-token
against the endpoint's `prompt_tokens`, and `estimate_error` is emitted on every
usage event.

**Cline.** `compaction-shared.ts`: max input is the model's `maxInputTokens`
or `contextWindow × 0.9`, default 128,000; trigger at 0.9 of that, target 0.7,
`DEFAULT_PRESERVE_RECENT_TOKENS` 20,000, summary output 4,096. The estimate is
`ceil(chars / 3)` over the JSON-serialised request (system prompt, messages,
tools), deliberately pessimistic, never calibrated. Output budget is
`maxTokensPerTurn` per config, not derived from the window.

**Take-away.** Yours is better engineered. One Cline idea is worth keeping: the
trigger is computed over the *whole request* including tool schemas and
system prompt (`estimateRequestInputTokens`), so overhead is never charged to
the wrong side. You already do this via `observe_tool_schemas`.

### 2.11 Compaction

**Yours.** `compact(retain_pct=0.35)` cuts at a whole-turn boundary, summarises
the evicted set with a schema-constrained recap (goal, decisions, verified,
open items, `do_not_retry`), chunks the transcript into 40,000-char pieces with
at most four summariser calls and a deterministic digest for the rest, merges
into the previous recap, invalidates the loop ledgers for evicted content, and
ends the run if three compactions land within eight turns. Emergency
compaction at 15% retention on `OverBudgetError`. No manual compaction route
on the loopback.

**Cline.** Two built-in strategies behind one `prepareTurn`: `agentic` (LLM
summary with a budget-projected input, files section guaranteed, cut at a safe
boundary preserving 20k recent tokens) and `basic` (deterministic: drop or
truncate old tool results, merge adjacent user turns, dropped-work summaries
listing tool activity, keep the last three assistant texts). Agentic failure
falls back to basic. Three modes: `auto` at the trigger, `manual` via
`/compact`, and `overflow_recovery` when the provider rejects the request as
too large, which forces basic compaction and retries once, refusing to retry
unless the request actually shrank. A `pre_compact` hook fires first. An
imported transcript from another agent is folded into a summary on first turn.
Results are persisted as the sidecar described in 2.1.

**Take-away.** Even. Yours has the better summary schema and the only ledger
invalidation. Cline has three things you lack: a deterministic strategy as an
explicit tier, reactive recovery from an endpoint context-length 400 (today
`_complete` turns any exception into `ERROR`), and a persisted compacted view.

### 2.12 Message projection

**Yours.** One projection: `wire()` renders the built list to OpenAI-shaped
dicts and repairs the tool-call invariant. The event stream (`assistant`,
`tool_call`, `tool_result`, `gate`, `plan`, `usage`, transient deltas) is what
the extension renders; `rehydrate` reads it back into the context.

**Cline.** Three. Provider: `prepareTurn` then `MessageBuilder` then
`ai-sdk-format`. Display: `projectSessionMessagesForDisplay` returns a
`SessionDisplayMessage` wrapper that is deliberately not assignable to
`Message`, so a display projection cannot leak into `initialMessages` or a
provider request; it also expands provider-executed tool activity into
`tool_use`/`tool_result` pairs. Host: `message-translator.ts` (2,799 lines)
maps SDK events and messages to `ClineMessage` rows keyed by `ts` with a `seq`
freshness counter and an `epoch` fence so the webview can merge replicas.
`userRunSpan` metadata separates real user turns from synthetic ones.

**Take-away.** The typed display wrapper is a cheap idea: your `Message` is
frozen, but nothing stops a caller feeding a rendered transcript back in.

### 2.13 Persistence

**Yours.** `Journal` buffers 32 events and flushes at turn end, mutation, and
finish; a failed write marks the journal broken for the process. `session.json`
is rewritten atomically. Undo manifests are JSON per session. Loop ledgers,
the recap, and the retained-set boundary are not persisted. Test count on the
agent: 478.

**Cline.** `UnifiedSessionPersistenceService` over a `SessionPersistenceAdapter`
(SQLite locally, remote artifact uploader for hosted). Per session:
`sessions.db` row, `SessionManifest` JSON, messages file, compaction sidecar,
checkpoint metadata in manifest metadata, hook logs. Writes go through
`atomic-file.ts`. Task settings, provider settings and global settings are
separate JSON files under `~/.cline/data`; the VS Code `StateManager` caches
them in memory with 500 ms debounced writes and documents that two windows do
not see each other's changes until restart. Legacy `tasks/<id>/` directories
are migrated on open.

**Take-away.** Cline persists three things you do not: the compacted view,
per-turn checkpoints, and an index. The index matters the moment sessions
outlive one process.

### 2.14 Extension state

**Yours.** [`RunState`](../extension/src/session-state.ts) is the single
derivation of every number on screen, fed by SSE with `since_id` resumption,
backoff from 250 ms, a 500-row transcript cap, and twin-suppression for
`assistant`/`plan` and `error`/`finish` pairs. Context is server-authoritative
(contract C5) via `GET /v1/sessions/{id}/context`. Approvals, doctor,
diagnostics, wizard and trees are separate modules.

**Cline.** `SdkController` (2,396 lines) plus nine coordinators (task start,
task control, follow-up, mode, compaction, diff edit, session events,
interaction, telemetry). `StateManager` is a process singleton with global,
workspace, task and secret caches. The webview receives `ClineMessage` rows and
merges by `ts` identity with `seq` and `epoch`, which is how it survives
reconnects and history reloads. Task history supports search, sort by cost,
favourites, and per-task size.

**Take-away.** Even. Your extension already has the property Cline's `seq`/
`epoch` exists to provide, through stored event ids. Cline's history search and
favourites are UI polish, not architecture.

### 2.15 Runtime state

**Yours.** `serve.py` builds one `Loopback` per window: a FastAPI app bound to
port 0, one shared `LLMClient`, a `Router` and `ContextManager` per session, a
worker thread per run, events marshalled to the asyncio loop, approvals blocked
on the worker thread with a polled deadline. No model credential is allowed in
the process.

**Cline.** `ClineCore` selects `local`, `hub`, or `remote`. The hub is a
singleton daemon on `127.0.0.1:25463` discovered through lock files; it
coordinates sessions, routes events and approvals, and brokers capabilities
(a VS Code client advertises `open-file` and `reveal-diff`, a CLI advertises
`shell`); spokes are worker processes that run `@cline/core` loops. Clients
attach and detach without stopping the run. Approval requests are routed to
whichever attached client can answer. Cron, agenda automation, connectors
(Telegram, Slack) and plugins run against the same hub.

**Take-away.** This is the architecture [host-plan.md](../host-plan.md) is
reaching for. The specific pieces to lift are the three roles (coordinate,
execute, participate), capability advertisement at client registration, and
approval routing through the coordinator rather than a blocked thread.

---

## 3. What to take from Cline

Ordered by value to a Go coding agent on one endpoint, not by how impressive
the feature is.

### Do now

1. **Per-turn checkpoints with compare and partial restore.** Replace or extend
   [`undo.py`](../apps/agent/src/dakcoder_agent/undo.py): snapshot the
   worktree into a private ref after every mutation batch (your `_inner_loop`
   boundary), record `{latest, history}` in `session.json`, add
   `GET /v1/sessions/{id}/checkpoints`, `POST .../checkpoints/{ref}/restore`
   with `files | task | both`, and a compare endpoint. Keep the pre-image
   snapshots for non-git workspaces. This is what makes
   `dakcoder.autoApproveTrivialPatches` safe to widen.

2. **Persist the compacted view as a sidecar.** After `compact()`, write the
   recap and the retained-set boundary (message count plus a hash of the
   canonical prefix) beside `events.jsonl`. Teach `rehydrate` to project it
   over the journal when the hash matches, and fall back to today's
   newest-turns-that-fit when it does not. Ledger invalidation stays as it is.

3. **Reactive overflow recovery.** In `_complete`, classify an endpoint 400
   whose message names context length, run `_compact(retain_pct=0.15)` once,
   and retry only if the assembled prompt actually shrank. Today that error
   ends the run as `ERROR` even though the machinery to recover exists.

4. **A `completionGuard` predicate.** Fold `_unwritten_targets`,
   `_gate_wants_an_edit` and the finish-refusal count into one function that
   returns "why this run is not done" or nothing, called from `_phase_ended`
   and from the forced-terminal path. It is the same logic in one place.

### Do before hosting

5. **Session ownership and optimistic locking.** A `status_lock` column and a
   pid-or-heartbeat reconciler, so two processes cannot both believe they own
   a session and a dead worker's session is failed by someone else rather than
   by the next restart of the same process.

6. **Approvals as routed state, not a blocked thread.** The hub routes a
   pending approval to any attached client that advertised the capability.
   Combined with the caf finding (park and resume), this is the one change
   that makes the hosted plan possible.

7. **Three runtime roles.** Coordinator, executor, participant. Your loopback
   is all three today. Splitting execution into a worker process is what lets
   a window reload not kill a run.

### Worth having

8. **Hook seams.** `beforeTool`, `afterTool`, `prompt_submit`, `pre_compact`,
   session start and shutdown, with file-based hook scripts under a project
   directory. Your gate, inner loop and playbooks would all be expressible as
   hooks, and a team could add a linter without editing `loop.py`.

9. **Read-only subagents.** A bounded, read-only child loop (read, search,
   list definitions, read-only commands, skills) that returns a short report
   and the file paths worth reading next. For a 6,571-line handler this is
   cheaper than a dozen `read_file` windows in the main context. Budget it
   through the same gateway metering with the parent session id.

10. **Parallel execution of read-only batches.** Your batch cap is six and the
    calls run in sequence on one thread. Reads and searches can run together;
    writes stay serial.

11. **Deterministic compaction as an explicit tier.** You have `_digest` as a
    fallback inside the summariser. Promote it to a named strategy that can be
    selected when the summariser is unavailable or the run is in recovery.

12. **Manual compaction route.** `POST /v1/sessions/{id}/compact`, so the
    extension's `dakcoder.compactContext` command can do what its name says.

### Later, tied to the hosted product

13. **Agenda tasks and unattended runs.** Cline's `AgendaTaskRecord` with
    approval, revision, priority, expiry and run claims is the shape of "the
    agent proposes follow-up work, a person approves it, a cron runs it".
14. **Session import with history-origin metadata**, if developers arrive with
    transcripts from other tools.
15. **History search, sort by cost, favourites** in the extension.

---

## 4. What not to take

- **No default iteration cap.** `AgentRuntime` runs until the model stops or a
  mistake limit trips. Your `max_turns` with a 400 ceiling is right.
- **Flat 3 chars per token.** Your calibrated estimate with a stated reserve is
  better; keep it.
- **Summariser-only compaction with no ledger invalidation.** Cline has no
  ledgers to invalidate. You do, and `_forget_evicted` is the reason a
  post-compaction run does not re-read what it was just told it had.
- **Loop detection by consecutive identical signature only.** Yours judges on
  coverage, body digests and overlap sets; theirs would miss the `Handler`,
  `handler`, `Handler\(` case your field transcript died on.
- **Modes as prompt contracts.** Cline's plan mode relies on a system-prompt
  promise plus a command guard. Your mode is a router allow-list, which is a
  guarantee rather than a request.
- **The stale `.clinerules/cline-overview.md`.** It documents a Task class that
  no longer exists. Your `_State` docstring has the same problem at smaller
  scale (20 claimed, 38 present).

---

## 5. Where you are ahead, so it is not cargo-culted away

- Intent classified before the first turn with a schema-constrained call, and
  the cheap-mistake asymmetry that makes ASK the fallback.
- Typed terminal tools with a named `tool_choice` as the measured escape from
  a repeating model.
- A deterministic gate with a pre-run baseline, cached by workspace state, that
  never runs on an empty change set.
- Coverage-interval read ledgers, partial-result marking, dead-end memory, and
  overlap detection on both search tools.
- The state block: the loop's own view of the task rendered to the model every
  turn, derived from ground truth the model cannot edit.
- Window arithmetic enforced in the config type, per-mode output budgets, and
  calibration against real usage with `estimate_error` on every turn.
- `wire()` coherence repair that reports itself as a bug rather than hiding.
- A revert that restores the developer's bytes, refuses on a dirty tree it did
  not snapshot, and says why.

---

## 6. File map

### Cline (paths relative to the cline checkout)

| Concern | File |
|---|---|
| Agent loop | `sdk/packages/agents/src/agent-runtime.ts` |
| Loop config, events, hooks, result types | `sdk/packages/shared/src/agents/types.ts` |
| Session orchestration, tracker wiring | `sdk/packages/core/src/runtime/orchestration/session-runtime-orchestrator.ts` |
| Runtime builder, tool presets, completion policy | `sdk/packages/core/src/runtime/orchestration/runtime-builder.ts`, `sdk/packages/core/src/extensions/tools/presets.ts` |
| Loop and mistake trackers | `sdk/packages/core/src/runtime/safety/loop-detection.ts`, `mistake-tracker.ts` |
| Provider projection | `sdk/packages/core/src/session/services/message-builder.ts` |
| Compaction | `sdk/packages/core/src/extensions/context/compaction.ts`, `agentic-compaction.ts`, `basic-compaction.ts`, `compaction-shared.ts`, `budget-projection/` |
| Compaction sidecar | `sdk/packages/core/src/session/models/session-compaction.ts` |
| Display projection | `sdk/packages/core/src/session/display-messages.ts`, `user-run-messages.ts` |
| Checkpoints | `sdk/packages/core/src/hooks/checkpoint-hooks.ts`, `session/checkpoint-restore.ts`, `session/checkpoint-diff.ts` |
| Persistence | `sdk/packages/core/src/session/services/persistence-service.ts`, `stores/session-manifest-store.ts`, `models/session-manifest.ts`, `models/session-row.ts`, `types/session.ts` |
| Hooks contract | `sdk/packages/shared/src/hooks/events.ts` |
| Agenda tasks | `sdk/packages/shared/src/tasks.ts`, `sdk/packages/core/src/tasks/` |
| Token estimate | `sdk/packages/shared/src/llms/tokens.ts` |
| Core entry and hub | `sdk/packages/core/src/ClineCore.ts`, `runtime/host/local-runtime-host.ts`, `hub/`, `docs/sdk/architecture/hub-spoke.mdx` |
| VS Code host | `apps/vscode/src/sdk/SdkController.ts`, `message-translator.ts`, `sdk-task-history.ts`, `apps/vscode/src/core/storage/StateManager.ts` |

### dakcoder

| Concern | File |
|---|---|
| Loop and `_State` | [`apps/agent/src/dakcoder_agent/loop.py`](../apps/agent/src/dakcoder_agent/loop.py) |
| Context manager | [`apps/agent/src/dakcoder_agent/context.py`](../apps/agent/src/dakcoder_agent/context.py) |
| Modes and budgets | [`apps/agent/src/dakcoder_agent/modes.py`](../apps/agent/src/dakcoder_agent/modes.py) |
| Session, journal, restore | [`session.py`](../apps/agent/src/dakcoder_agent/session.py), [`journal.py`](../apps/agent/src/dakcoder_agent/journal.py), [`rehydrate.py`](../apps/agent/src/dakcoder_agent/rehydrate.py) |
| Undo | [`undo.py`](../apps/agent/src/dakcoder_agent/undo.py) |
| Router and control tools | [`tools/router.py`](../apps/agent/src/dakcoder_agent/tools/router.py), [`tools/control.py`](../apps/agent/src/dakcoder_agent/tools/control.py) |
| Gate | [`gate.py`](../apps/agent/src/dakcoder_agent/gate.py) |
| Runtime | [`loopback.py`](../apps/agent/src/dakcoder_agent/loopback.py), [`serve.py`](../apps/agent/src/dakcoder_agent/serve.py) |
| Extension state | [`extension/src/session-state.ts`](../extension/src/session-state.ts), [`extension/src/client.ts`](../extension/src/client.ts) |
