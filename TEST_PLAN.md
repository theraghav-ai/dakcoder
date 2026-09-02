# DakCoder — Regression & Adversarial Test Plan

Every confirmed bug gets a regression test. The eleven **REPRODUCED** findings
already have executable reproductions written against the real modules during
the audit — checked in under `audit-repros/` (`repro_audit.py` R1–R9,
`repro_carry.py` R10, `repro_lockout.py` R11; run with
`.venv/bin/python audit-repros/repro_audit.py`). Those scripts are the seed of
the tests below (they use a
`ScriptedClient` returning canned `ChatResult`s, a temp `Workspace`, the real
`ContextManager`, `Router`, `AgentLoop`, and real `fs` handlers — no mocking of
the code under test). Current-result column reflects the audited tree.

Existing suite status on Linux: **6 of 704 tests fail** — 3 assert backslash-path
normalisation that the code does not do on POSIX (L-20: keep the tests, fix the
code), 3 are gofmt tests that fail instead of skipping when the Go toolchain is
absent (L-21: convert to skips). The suite must be green before the tests below
are added, or their signal drowns.

Test harness conventions: `tests/regression/` in `apps/agent/tests`;
`ScriptedClient` promoted to a shared fixture; assertions on (a) the wire
(`context.wire()`), (b) events yielded, (c) `loop.state`, (d) the filesystem.

---

## 1. Mandated regression tests (§27 list), mapped

| # | Required test | Covers | Status |
|---|---|---|---|
| 1 | capped read followed by tail read | L-8 (CM-1) | repro exists (R1) |
| 2 | compaction followed by reread | L-10 (CM-2) | repro exists (R2) |
| 3 | write-heavy compaction | L-3 (CM-3) | repro exists (R3) |
| 4 | terminal tool in multi-call batch | L-1 (TC-1) | repro exists (R4) |
| 5 | follow-up with carried state | L-5 (TC-2) | repro exists (R10) |
| 6 | repeated tool result | L-17 (CM-5) | new |
| 7 | refused finish | L-14 (TC-3) | repro exists (R7) |
| 8 | forced flag across phases | TC-4 | new |
| 9 | repeated output truncation | L-13 | repro exists (R8) |
| 10 | resume | RT-1 | new |
| 11 | concurrent session completion | L-9 | new |
| 12 | summarizer failure | (verified safe: fallback recap) | new, pins behaviour |
| 13 | fallback failure | `_complete` OverBudget→compact→retry | new |
| 14 | invalid line range | read_file clamp (verified correct) | new, pins behaviour |

## 2. Test specifications

### test_terminal_tool_in_batch_answers_all_calls (L-1)
- **Setup**: ScriptedClient turn 1 = `[submit_plan(valid), read_file(a.go)]`.
- **Actions**: run with `intent=AGENT` to completion.
- **Expected**: every `tool_calls[].id` in `context.wire()` has exactly one
  `role:"tool"` message; the read_file call is answered ("not run: the phase
  ended" or dispatched).
- **Current**: `t2` orphaned — assistant declares 2 calls, 1 result follows.

### test_capped_read_then_tail_read_dispatches (L-8)
- **Setup**: 8,000-line file (~360KB) so the 48k-token read cap elides the tail.
  Turn 1 whole-file read; turn 2 `read_file(start=6000, end=6500)`.
- **Expected**: the second read **dispatches** (its lines are not in context);
  ledger coverage equals surviving lines only.
- **Current**: refused — "Lines 6000-6500 … already in context above"; ledger
  claims (1, 8000); context contains no line past ~2650.

### test_compaction_invalidates_read_ledger (L-10)
- **Setup**: dispatch a real read of lines 1-300, record it, compact with
  `keep_recent=1`.
- **Expected**: `loop._re_reading(read 1-300)` returns `""` (dispatchable).
- **Current**: refusal text claiming the lines are "in context above".

### test_write_heavy_compaction_frees_tokens (L-3)
- **Setup**: 20 assistant messages, empty content, each with a 40KB `write_file`
  argument blob + small results; `usage().total` ≈ 200k > 0.7×budget.
- **Expected**: after `compact()`, `usage().total` under the retain target and
  `should_compact()` false.
- **Current**: compact evicts **nothing** (0 messages), `compactions` stays 0,
  `should_compact()` still true → loop-level thrash-kill or OverBudget ERROR.

### test_follow_up_carry_survives_first_batch (L-5)
- **Setup**: loop1 with `seen_calls` populated and `mutations_seen=3`; loop2 with
  fresh Router, `carry_from(loop1)`; scripted repeat of a carried call.
- **Expected**: carried `seen_calls` counts survive the first tool batch.
- **Current**: wiped at the top of the first batch (router.mutations 0 ≠ 3).

### test_repeated_result_replay_marks_truncation (L-17)
- **Setup**: tool returning 20KB content; call twice with identical args.
- **Expected**: the intercepted replay either carries the full cached result or
  explicitly says it is a truncated replay.
- **Current**: replays `[:6000]` chars presented as "the current answer".

### test_refused_finish_message_is_accurate (L-14)
- **Setup**: turn 1 `finish(answer="")` (schema-refused).
- **Expected**: the next-turn user message must not claim the call "has already
  been answered"; it should say the arguments were refused and what to send.
- **Current**: "Stop searching. That call has already been answered…".

### test_forced_flag_does_not_cross_phases (TC-4)
- **Setup**: Planner emits prose (forced re-ask consumes `state.forced`),
  submits plan; in AGENT a failing gate then a prose turn.
- **Expected**: the AGENT prose turn after a failing gate is re-asked with
  `tool_choice="required"` (i.e. `_must_call_a_tool()` still true once).
- **Current**: `forced` is run-scoped; the AGENT re-ask never happens.

### test_repeated_truncation_has_hard_stop (L-13)
- **Setup**: every scripted reply `finish_reason="length"` with a cut tool call.
- **Expected**: run ends with a truncation-specific outcome within ~3 such turns.
- **Current**: burns all `max_turns` and exits EXHAUSTED with no mention of
  truncation.

### test_acting_phase_not_locked_out_after_12_turns (L-2)
- **Setup**: submit_plan(1 file) → write it → 12 distinct-file reads → assert on
  the requests' `tool_choice`.
- **Expected**: a turn following a *failing gate* must never carry
  `tool_choice={"function":{"name":"finish"}}` while the gate report says
  "make the edit"; write-tool turns don't advance the fence.
- **Current**: requests 13+ force `finish` unconditionally.

### test_resume_semantics_match_message (RT-1)
- **Setup**: run to EXHAUSTED; `runtime.resume(session)`.
- **Expected**: either the resumed loop's context is the previous context
  (continuation), or the EXHAUSTED summary does not claim "Resume continues on
  this same transcript".
- **Current**: fresh context + task-reseed while the message promises
  continuation.

### test_steer_never_lost_on_finish_race (L-9)
- **Setup**: block the run's last turn on an event; call
  `POST /sessions/{id}/messages` (or `session.steer` directly) after the loop's
  final drain but before `session.finish`; release.
- **Expected**: the text is recorded as a USER event and produces a follow-up run.
- **Current**: silently dropped.

### test_summarizer_failure_falls_back (pins good behaviour)
- **Setup**: summariser client raising; compact.
- **Expected**: fallback Recap with files_read populated; run continues.
- **Current**: passes (verified) — pin it.

### test_over_budget_fallback_failure (13)
- **Setup**: context whose emergency compact cannot reduce (write-heavy, L-3);
  dispatch.
- **Expected (after L-3 fix)**: compaction reduces; run continues.
- **Current**: `Outcome.ERROR` "context cannot be reduced below budget".

### test_invalid_line_ranges (pins good behaviour)
- `start > len` refused with the length; `end > len` clamped and meta.span
  reports the clamp; `start=0`/negative → treated as 1. Current: passes.

### test_recap_accumulates_across_compactions (L-4)
- Two compactions with distinguishable `do_not_retry` entries; both must be in
  the final pinned recap. Current: only the second survives.

### test_compaction_never_retains_orphaned_result (L-6)
- Working set `[assistant(declares k1), tool(k1, huge)]`, `keep_recent=1`.
- Expected: wire has no `role:"tool"` message whose id no assistant declares.
- Current: retained set is exactly one orphaned tool result.

### test_patch_file_non_utf8_preserves_content (TL-1)
- **Setup**: file `package p\n<0xFF 0xFE>\n` (invalid UTF-8 past a clean 8KB probe
  works too); `patch_file` a clean line.
- **Expected**: patch applies (or refuses) with the file's bytes intact.
- **Current**: file truncated to 0 bytes; generic failure returned.

### test_subprocess_timeout_kills_process_tree (TL-5)
- **Setup**: run a script that spawns a sleeping grandchild; 1s timeout.
- **Expected**: no process from the tree survives the TimeoutExpired.
- **Current**: grandchild survives.

### test_revert_blocks_on_pre_run_developer_changes (L-11)
- **Setup**: repo with an uncommitted edit to `a.go` and an untracked `b.go`;
  session mutates both; revert.
- **Expected**: both paths `blocked` (or restored to pre-run content), never
  restored-to-HEAD / deleted.
- **Current**: `a.go` reset to HEAD (edit destroyed), `b.go` unlinked.

### test_plan_paths_normalised (L-19)
- submit_plan step `file="./a.go"`, write `a.go` → `_unwritten_targets()` empty.
  Current: reports `./a.go` unwritten.

### Gateway / shared / extension (from the subsystem audits)
- **SH-1**: stream ends cleanly with no `[DONE]`/finish_reason → client raises
  retryable error (current: silent truncated success).
- **SH-3**: tool-call deltas without `index` → two calls preserved (current:
  merged into slot 0).
- **GW-2**: cancel the request task between reserve and first chunk → quota
  released (current: leaked).
- **GW-4**: two concurrent `remember` with one Idempotency-Key against Redis
  (fakeredis) → one charge (current: two).
- **GW-1**: refresh against `GitLabIdentity` → 200 (current: 501).
- **GW-3**: `X-Estimated-Tokens: abc` / `-5` / bad JSON body → 400 (current: 500).
- **EXT-1/2**: approval extended mid-wait survives past the original 600s
  (integration test against the loopback: extend at t=590, decide at t=650 →
  accepted; current: rejected at 600).
- **EXT-3**: attach to a running session with an already-answered approval →
  no live card (state.test.ts: replayed `tool_pending` marked historical).
- **EXT-4**: webview `lastSeq` from a previous epoch does not drop new events.

## 3. Adversarial / chaos suite (scripted model behaviours, §28)

Run each behaviour against a scripted loop; record outcome, turn count, wire
validity (every declared call answered), and final event pair (FINISH+END).

| Behaviour | Script | Expected containment | Audited result |
|---|---|---|---|
| A: read_file forever (same range) | repeat identical read | intercept → 2 stalls → forced finish | ✔ works (bounded ~4 turns) |
| A': read_file forever (fresh ranges) | walk a huge file 30 lines at a time | per-file read budget (`MIN/MAX_READS`) | ✔ bounded, but interacts with L-8 on capped files |
| B: write_file forever | distinct paths | mutations reset stall ledgers by design → runs to research fence | fence fires at 12 (L-2 side effect) |
| C: submit_plan + read_file batch | one batch | all calls answered | ✘ L-1 |
| D: finish("") | schema refusal | bounded by MAX_FORCED_TERMINAL=2 | ✔ bounded, ✘ message (L-14) |
| E: huge tool call args | 40KB write args ×20 | compaction handles | ✘ L-3 |
| F: same call repeatedly | identical fingerprint | cached replay + supersede | ✔ (replay truncation: L-17) |
| G: call after phase transition | tool from ended phase | mode-refusal, not cached (refused_by_mode) | ✔ |
| H: narration forever | prose in PLANNER | one forced re-ask → honest DONE | ✔ |
| I: truncation forever | finish_reason=length | needs hard stop | ✘ L-13 |
| J: compaction → follow-up → resume | combined | ledgers/context consistent | ✘ L-10, L-5, RT-1 |
| A→B→A→B alternation, same args | two fingerprints | both intercepted → stall counter advances | ✔ detected |
| A→B→A→B with drifting args | vary a param each turn | new fingerprints dispatch forever | contained only by the research fence / max_turns — acceptable, document |

## 4. Invariant checks to add as permanent assertions

1. **Wire invariant** (cheap, every turn, debug builds): each assistant
   `tool_calls[].id` in `context.build()` is followed by exactly one tool message
   with that id; no tool message has an undeclared id. Catches L-1, L-6 and any
   future regression in one place.
2. **Ledger invariant** (test-time): for every `(path, span)` the read ledger
   claims covered, the concatenated live context messages for that path contain
   the span's first and last line. Catches L-8, L-10.
3. **Budget invariant**: `_retention_cut`'s cost model equals `usage()`'s per
   message (property test over random messages with/without tool_calls). Catches
   L-3 and future divergence.
4. **Terminal invariant**: a session whose status is terminal accepts no further
   tool dispatch on its old loop (already true — pin it).
