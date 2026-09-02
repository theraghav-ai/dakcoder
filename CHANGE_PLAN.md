# DakCoder — Ordered Change Plan

Sequenced so that (1) data-loss and protocol-corruption fixes land first, (2)
each step is independently shippable and testable, (3) architectural
consolidation (steps 10–12) happens once the behaviour it must preserve is
pinned by the regression tests in `TEST_PLAN.md`. Write the failing test before
each step; the test names below refer to that plan.

---

## Phase 0 — restore the test signal

### Step 0.1 — make the suite green on Linux
- **Files**: `apps/shared/src/dakcoder_shared/paths.py`; `apps/agent/tests/test_fs_tools.py`
- **Change**: normalise `\` → `/` in `Workspace.resolve`/`relative` on POSIX
  (the failing tests already specify the behaviour); add
  `pytest.mark.skipif(shutil.which("gofmt") is None)` to the three gofmt tests.
- **Deps**: none. **Regression**: existing failing tests turn green.
- **Risk**: low — a path containing a literal backslash character on POSIX is
  no longer addressable; acceptable (no legitimate repo file uses one).
- **Rollback**: revert; tests return to red.

## Phase 1 — stop losing data and corrupting protocol (P0)

### Step 1.1 — answer every call in a batch that hits a terminal tool (L-1)
- **Files**: `apps/agent/src/dakcoder_agent/loop.py` — `_tool_calls`, `_TERMINAL` branch (~line 1304)
- **Change**: before `return` (both the `_phase_ended` and forced-terminal-cap
  paths), append a tool result for every `calls[index+1:]`:
  `"{name} was not run: the {tool} call in the same reply ended the phase."` —
  mirroring the existing cancellation block at loop.py:1155-1170.
- **Deps**: none. **Regression**: `test_terminal_tool_in_batch_answers_all_calls`.
- **Risk**: minimal — strictly adds messages that make the wire valid.
- **Rollback**: revert the block.

### Step 1.2 — wire-invariant checkpoint (structural fix for the same class)
- **Files**: `context.py` (new method `assert_wire_coherent()` or repair pass in
  `wire()`), `llm.py` `complete()`
- **Change**: before dispatch, scan the assembled list; synthesise
  `"not run: <reason>"` results for any declared-but-unanswered id; log an ERROR
  event when the repair fires (it should never fire once 1.1 and 2.3 land).
- **Deps**: 1.1 (so the repair is a backstop, not the fix).
- **Regression**: invariant check #1 in TEST_PLAN §4.
- **Risk**: low; repair is additive. **Rollback**: disable the repair flag.

### Step 1.3 — patch_file/write_file: surrogateescape + atomic write (TL-1, TL-2)
- **Files**: `tools/fs.py` (`write_file`, `patch_file`), a small `_atomic_write(path, text, eol)` helper
- **Change**: write with `encoding="utf-8", errors="surrogateescape"`; write to
  `path.with_suffix(path.suffix + ".dakcoder-tmp")` then `os.replace`.
- **Deps**: none. **Regression**: `test_patch_file_non_utf8_preserves_content`.
- **Risk**: low. Windows `os.replace` over an open file can fail — catch and
  fall back to direct write with the content already encoded (encode-before-open
  alone removes the truncation bug).
- **Rollback**: revert helper call sites.

### Step 1.4 — revert must not destroy developer work (L-11)
- **Files**: `session.py` (`plan_revert`, `revert`), `tools/router.py` (first-mutation hook)
- **Change**: minimal fix first: at `plan_revert`, for each path compare the
  *pre-run* state: if the session's first mutation of the path was
  `MutationKind.MODIFY` but the path is untracked in HEAD → `blocked`
  ("existed before the run but is not in HEAD"); if `git status` showed the path
  dirty pre-run → `blocked`. Requires recording, per session, each path's
  pre-run dirty/untracked state at first mutation — add a snapshot hook in the
  router (copy pre-image to `.dakcoder/session-<id>/undo/<path>` at first touch)
  and make `revert` restore from the snapshot when present.
- **Deps**: none. **Regression**: `test_revert_blocks_on_pre_run_developer_changes`.
- **Risk**: medium — new on-disk artefacts; keep snapshots bounded (skip >2MB,
  report blocked instead). **Rollback**: revert to blocked-only behaviour
  (never destructive), not to the old restore-to-HEAD.

### Step 1.5 — read ledger must reflect what survived the cap (L-8)
- **Files**: `context.py` (`append_tool_result` / `_apply_cap`), `loop.py` (`_record_read`, `_tool_calls`)
- **Change**: `_apply_cap` computes, for head-strategy read_file content, the
  surviving line range; `append_tool_result` returns `(message, surviving_range)`
  (or sets it on the message); `_tool_calls` records the ledger from the
  surviving range and sets `state.truncated_at[fingerprint]` when the cap fired
  so a wider ask re-dispatches. The elision marker already names the file; add
  the surviving range to it ("kept lines 1-2650 of 1-8000").
- **Deps**: none. **Regression**: `test_capped_read_then_tail_read_dispatches`.
- **Risk**: medium — touches the cap/ledger seam; the new marker text changes
  prompts (verify against `test_prompts.py` budget assertions).
- **Rollback**: revert; behaviour returns to refusing tail reads.

### Step 1.6 — compaction invalidates loop ledgers (L-10, part of L-17/L-25)
- **Files**: `loop.py` (`_compact`, `_summarise`), `context.py` (`compact` returns evicted paths/ids)
- **Change**: `compact()` already knows the evicted messages; return (or expose)
  the evicted `(path, line_range)` set and tool_call_ids. `_compact` then:
  drop `state.reads[path]` spans covered only by evicted messages (simplest
  correct version: drop the whole entry for every evicted path — the recap
  already names them); drop `last_results`/`echoes`/`truncated_at` entries whose
  backing message was evicted.
- **Deps**: 1.5 (shared seam). **Regression**: `test_compaction_invalidates_read_ledger`.
- **Risk**: low — worst case is a permitted re-read that costs tokens.
- **Rollback**: revert; ledgers go stale again.

### Step 1.7 — retention cut sees tool-call arguments (L-3)
- **Files**: `context.py` — `_retention_cut` (~1178), `novel_tokens` (~1010)
- **Change**: cost each message as `estimate(content) + Σ estimate(name+arguments)`
  — the exact formula `usage()` uses (factor a `_message_cost(msg)` helper used
  by all three).
- **Deps**: none. **Regression**: `test_write_heavy_compaction_frees_tokens`,
  invariant #3. **Risk**: low — compaction gets more aggressive on write-heavy
  sets, which is the point. **Rollback**: revert helper.

### Step 1.8 — un-jam the acting phase (L-2)
- **Files**: `loop.py` — `_turn` fence block (~834), `_tool_calls`, `_gate_failed`
- **Change** (minimal): (a) reset `research_turns = 0` whenever a batch mutates
  (`if mutated:` in `_tool_calls`) — writing is progress, not research; (b) in
  the fence, when `state.last_gate` is failing and workspace unchanged, force
  `"required"` (not named `finish`) so the model can edit; (c) reset
  `research_turns` in `_gate_failed` so the fix window reopens after each gate
  verdict.
- **Deps**: none. **Regression**: `test_acting_phase_not_locked_out_after_12_turns`;
  re-run behaviour B in the chaos suite. **Risk**: medium — loosens a
  loop-containment bound; the stall ledgers and MAX_GATE_FAILURES still bound
  the run. Watch `no_progress` rates after deploy.
- **Rollback**: revert (a)-(c) independently.

### Step 1.9 — never lose a steer (L-9)
- **Files**: `loopback.py` (`_spawn.run` finally-block, `message_session`), `session.py`
- **Change**: record a USER event at `session.steer()` time; in the worker's
  `finally`, `leftover = session.drain_steer()`; if non-empty, schedule
  `follow_up` with the joined text (via `loop.call_soon_threadsafe`).
- **Deps**: none. **Regression**: `test_steer_never_lost_on_finish_race`.
- **Risk**: low — double-delivery is prevented because drain is atomic.
- **Rollback**: revert; race returns.

### Step 1.10 — approval timeout integrity (EXT-1, EXT-2, EXT-3, EXT-5)
- **Files**: `loopback.py` `_await_decision` (~390); `extension/src/approvals.ts`, `session-state.ts`, `extension.ts`
- **Change**: backend — poll-loop the wait
  (`while not decided.wait(min(5, deadline_in())): if deadline_in()<=0: break`)
  so `/extend` works; honour a `DAKCODER_APPROVAL_TIMEOUT` env (0 = no timeout)
  to match the extension's advertised default. Extension — parse
  `seconds_left`/`session_id` in `asApproval`; mark replayed `tool_pending`
  historical on the attach path (ids ≤ server max at attach); run one reconcile
  pass at activation and on attach.
- **Deps**: none. **Regression**: EXT integration tests in TEST_PLAN §2.
- **Risk**: low-medium (extension state machine); ship backend first.
- **Rollback**: each side independently revertible.

### Step 1.11 — gateway auth refresh + quota integrity (GW-1, GW-2, GW-3, GW-4)
- **Files**: `gateway/auth/identity.py` (+protocol), `proxy.py`, `app.py`, `quota/store.py`
- **Change**: implement `GitLabIdentity.recheck` and add `recheck` to the
  IdentityProvider protocol; add `except BaseException` release in the stream
  generator (shielded settle); validate body/headers → 400 and clamp numerics;
  Redis `SET key val NX GET` in `remember` and honour the loser.
- **Deps**: none. **Regression**: GW tests in TEST_PLAN §2.
- **Risk**: low-medium; each is a contained change. **Rollback**: per-change.

### Step 1.12 — LLM client: fail on silent mid-stream EOF; retry mid-stream drops (SH-1, SH-2, SH-3)
- **Files**: `apps/shared/src/dakcoder_shared/llm.py`
- **Change**: at stream end without `[DONE]`/finish_reason → raise retryable
  `UpstreamError`; add `RemoteProtocolError`/`ReadError` to the retryable set;
  tool-call slot key falls back to `id` when `index` absent; a nameless
  assembled slot becomes a loud failure (event + error) rather than a silent drop.
- **Deps**: none. **Regression**: SH tests. **Risk**: medium — a benign server
  that never sends `[DONE]` would start erroring; gate behind one retry first.
- **Rollback**: flag off the EOF check.

## Phase 2 — reliability and honesty (P1)

### Step 2.1 — cumulative recap (L-4)
`context.compact`: pass the previous `Recap` into the summarise seam; union
`do_not_retry`/`decisions`/`files_*` (bounded: keep newest N of each).
Regression: `test_recap_accumulates_across_compactions`. Risk: low.

### Step 2.2 — carry that survives (L-5) + session-scoped Router
Minimal: in `loopback._spawn` (continued branch) set
`agent.state.mutations_seen = 0` to match the fresh Router — one line, kills the
wipe. Proper: keep the Router in `runtime.loops`' entry and reuse it on
follow-up so `touched`/`mutations`/gate keys carry (aligns L-19's session view).
Regression: `test_follow_up_carry_survives_first_batch`. Risk: minimal/medium.

### Step 2.3 — compaction orphan edge (L-6)
`_whole_turn_cut`: if the surviving head at `limit` is a result whose call was
evicted, walk the cut **back** to include its assistant instead (accept the
token overshoot), or stub the result's `tool_call_id` linkage by synthesising
the assistant message. Regression: `test_compaction_never_retains_orphaned_result`.

### Step 2.4 — truncation counter (L-13)
`_State.truncated_turns`; increment in `_answer_truncated`, reset on any
complete reply; at 3, force the phase's terminal tool with a message naming the
output limit. Regression: `test_repeated_truncation_has_hard_stop`.

### Step 2.5 — honest messages (L-14, L-17, RT-1 copy)
Refused-terminal path sets a distinct flag so the follow-up message says "your
{tool} call was refused: {reason}; call it again with valid arguments".
Cached-replay bodies of truncated caches say "first 6,000 characters of the
earlier result". EXHAUSTED summary stops claiming resume is a continuation
(until 2.7 makes it one). Regression: `test_refused_finish_message_is_accurate`,
`test_repeated_result_replay_marks_truncation`.

### Step 2.6 — plan-path normalisation (L-19) and follow-up read staleness (L-25)
Normalise `PlanStep.file` through the workspace at `submit_plan`; on follow-up,
invalidate `state.reads` entries whose file mtime changed since the previous
run finished (store mtimes in the ledger at record time).

### Step 2.7 — resume as continuation (RT-1)
Route `resume` through the `continued=True` path (reuse context + carry) with
the note as the user message; keep the fresh-turn-budget property. Risk: medium
(changes resume semantics); guarded by `test_resume_semantics_match_message`.

### Step 2.8 — process hygiene (TL-5, TL-6, TL-7, TL-8)
`commands.run`: `start_new_session=True`, kill process group on timeout
(Windows: `CREATE_NEW_PROCESS_GROUP` + `taskkill /T`); stream-capped output;
drop `git` from `ALLOWED_BINARIES` (route via git_ops) or denylist destructive
subcommands; reject path separators in argv[0].

### Step 2.9 — persistence (L-7)
Append-only JSONL per session under `.dakcoder/sessions/<id>/events.jsonl` +
`mutations.json`, written from `session.record` (best-effort, never blocking the
run); `SessionStore` loads summaries lazily on start. Makes revert and
transcripts survive restart and makes the docstrings true. Risk: medium (I/O on
the hot path — buffer and flush per turn).

### Step 2.10 — extension P1s (EXT-4, EXT-9, EXT-10)
Epoch nonce for the webview seq; `randomBytes` loopback token;
`"scope": "machine"` on the four executable-path settings.

### Step 2.11 — summariser sees tool calls (L-27); forced-prose retention (L-15)
Render `[assistant] called write_file(path=..., 214 lines)` into the summariser
transcript; append the discarded pre-force prose as the assistant message
before the forced result (or emit a retraction event the UI honours).

## Phase 3 — P2 cleanups
Leak fixes (L-12 contexts/loops eviction, L-26 event-payload caps, SH-7
`_history`, gateway `_settled`, EXT-20 `answered` set); observability (turn ids
on tool events, count emergency compactions L-24, QUOTA event or delete the
tree machinery EXT-15, attempt off-by-one EXT-7, forced_tool_call rendering
EXT-8); perf (gate-mutation-aware gate key L-29, directive placement L-18 —
measure before moving); gateway hygiene batch (GW-5..14); dead-code
reconciliation in the extension (EXT-6, EXT-16, EXT-18); documentation truth
pass (DOC-1: README, session.py, context.py, loop.py claims).

## Sequencing summary

```
0.1 ──────────────────────────────────────────────► green CI
1.1 → 1.2                (protocol)                 independent of everything
1.3, 1.4                 (data loss)                independent
1.5 → 1.6                (ledger truth)             pair; land together
1.7                      (retention)                independent
1.8                      (lockout)                  after chaos-suite baseline
1.9                      (steer)                    independent
1.10, 1.11, 1.12         (approvals / gateway / client) parallel tracks
2.x                      after their 1.x deps; 2.2-minimal can ship with 1.x
3.x                      opportunistic
```

Every step: land the failing regression test in the same change; the chaos
suite (TEST_PLAN §3) re-runs after 1.8 and 2.4 since those alter loop-containment
bounds.
