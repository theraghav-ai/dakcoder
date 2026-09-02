# DakCoder — Comprehensive Production Audit

Date: 2026-09-02. Scope: `apps/agent`, `apps/shared`, `apps/gateway`,
`extension/`, against the standard: *can this autonomous coding agent run for a
long time, manipulate a real repository, survive failures and context pressure,
maintain correct state, obey tool protocols, recover from interruption, and
never confuse what the model was told with what the model actually knows?*

Method: full read of the ~10k-line Python core and the ~16k-line extension;
**11 executable reproductions run against the real modules** (each labelled
R1–R11 below; specs in `TEST_PLAN.md`); the project's own 704-test suite
executed (6 failures on Linux); three deep subsystem audits (tools/gate,
gateway/shared, extension). Companion documents: `ARCHITECTURE_AUDIT.md`,
`BUGS.md`, `TEST_PLAN.md`, `CHANGE_PLAN.md`.

Confidence vocabulary: **REPRODUCED** (executed against the real code),
**CODE-PROVEN** (defect fully visible in the cited path), **LIKELY** (mechanism
proven, trigger environmental), **SPECULATIVE** (marked as such, rare).

---

## Executive summary

This is a carefully built system with unusually honest internal documentation,
real measured design decisions, and several hard problems solved correctly:
intent-before-first-turn, typed phase transitions, the baseline/excuses gate
logic, path confinement, append-only history with `tool_call_id` discipline at
most seams, and an event stream with genuine resumption. The test suite is
large (704 tests) and strong on the paths it imagines.

It is **not production-ready as an autonomous agent**, for three classes of
reason:

1. **The prior audit's central problem is not fixed.** "Context manager state
   and loop ledger state can disagree about what the model has actually seen"
   is alive in at least four reproduced forms: the insertion cap is invisible
   to the read ledger (R1), compaction never invalidates the ledgers (R2), the
   retention cut cannot see tool-call arguments the trigger can (R3), and the
   follow-up carry wipes itself against a fresh Router (R10). In each case the
   model is *told* something ("those lines are already in context above") that
   is false, and in two of them the loop then refuses the only action that
   would fix it.

2. **The tool-call protocol invariant is enforced in four places and violated
   in two.** A terminal tool inside a multi-call batch orphans the rest of the
   batch (R4) — after which *every* subsequent request of the session, and every
   follow-up on it, carries a declared call with no result: a strict
   OpenAI-compatible endpoint rejects the whole conversation. An aggressive
   compaction can retain an orphaned tool result (R6).

3. **Recovery and reversal are weaker than their promises.** Nothing is
   persisted (the docstrings say events are; they are not) — a daemon restart
   loses transcripts and the mutation list `revert` depends on. `revert`
   restores to HEAD, destroying a developer's own uncommitted pre-run work.
   `resume` is a fresh context seeded with a summary while the EXHAUSTED
   message promises "Resume continues on this same transcript". `patch_file`
   can truncate a legacy file to zero bytes. The gateway's token refresh is
   dead in production, and its quota can be leaked by a disconnect and
   double-charged by an idempotency race. The extension's approval-timeout
   machinery silently converts slow reviews into rejections under the default
   configuration.

None of these is exotic. Long runs, big files, write-heavy tasks, follow-ups,
restarts, slow reviewers and mid-stream disconnects are the *normal* operating
envelope of this product.

The encouraging half: the defects cluster around a small number of root causes
(§Root causes), the fixes are mostly local (`CHANGE_PLAN.md`), and 11 of the
worst already have executable reproductions to pin them.

---

## Architecture

See `ARCHITECTURE_AUDIT.md` for the full map, per-subsystem responsibilities,
lifecycle traces and the duplicated-state table. Summary: extension (TS) ⇄
`dakcoderd` loopback (FastAPI, 127.0.0.1, one worker thread per run) ⇄ gateway
(auth/quota/ledger/proxy, asyncio, single worker) ⇄ LiteLLM/vLLM. The agent
core is `loop.py` (state machine) + `context.py` (sole message-list owner) +
`tools/router.py` (6-check dispatch) + `gate.py` (deterministic verification).

Documentation drift is itself a finding (**DOC-1**): the README says the loop,
router, gateway and extension are "not built yet" (all exist); `session.py`
claims "Events are persisted before they are sent" (nothing persists);
`context.py` claims recaps are written to `.dakcoder/session-<id>/recap.md`
(never written); `loop.py`'s EXHAUSTED summary claims resume continues the
transcript (it does not).

## State machine

Reverse-engineered table and transition analysis in `ARCHITECTURE_AUDIT.md` §3.
The machine is small and mostly sound: intent is fixed before turn 1; phases
end on typed tool calls; ASK/PLANNER/AGENT tool visibility is a real allow-list
(verified against the registry — no mutating tool is visible outside AGENT);
terminal outcomes emit FINISH+END even on crash (loopback wraps the worker).

Defects found:

- **L-2 (High, REPRODUCED R11)** — `research_turns` counts *every* tool-calling
  turn of the acting phase and resets only on `submit_plan`. After 12 such
  turns with all plan targets touched, every subsequent turn is dispatched with
  `tool_choice={"name":"finish"}`. Combined with a failing gate this is a
  contradiction the model cannot escape: the gate report in the same context
  says "Make the edit, or say plainly what is stopping you" while the request
  forbids every tool but `finish`. The run then burns `MAX_GATE_FAILURES`
  forced finishes and ends UNVERIFIED. It also structurally caps the acting
  phase at ~12 tool turns, against a product that advertises
  `dakcoder.maxTurns` up to 400 for whole-service migrations.
- **L-14 (Med, REPRODUCED R7)** — a *refused* terminal call (`finish("")`)
  routes through the stall path, so the next turn opens "Stop searching. That
  call has already been answered and asking it again returns the same thing" —
  false on every clause; the mechanism (force finish again, bounded at 2) is
  right, the message teaches the model the wrong lesson (prior TC-3:
  partially fixed).
- **TC-4 (prior audit)** — `forced` is deliberately once-per-*run* now
  (documented in `_State`), but it still crosses phase boundaries: a Planner
  that consumed the re-ask leaves the acting mode without its one
  narration-recovery. Classified **partially fixed** (intentional scope,
  unintended cross-phase effect).
- **L-13 (Med, REPRODUCED R8)** — repeated output truncation advances no
  counter (not `stalled_turns`, not `research_turns`); a model that always
  overruns its output budget consumes the entire turn budget before anything
  stops it. The per-turn handling of truncation is exemplary (every declared
  call answered, accurate message, `_parseable_arguments` heals history) — the
  *repetition* is unbounded below max_turns.
- **L-15 (Med, CODE-PROVEN, loop.py:909-920)** — the forced re-ask discards the
  original prose reply after its deltas already streamed to the panel: the UI
  displays text the backend then silently drops, and the model's own turn
  vanishes from its history.
- Impossible-state check: no way found to execute normal agent work after a
  terminal outcome (loopback re-spawns a fresh loop for follow-ups; the old
  generator is exhausted). Cancellation mid-batch answers abandoned calls
  correctly — the one place the orphan invariant is done right.

## Context management

The design (layered budget, insertion caps with actionable markers, slice
supersession by containment, token-floor compaction with a structured recap,
`_whole_turn_cut` keeping call/result pairs together) is genuinely good. The
failures are all at the seams between the context manager and the loop's
private ledgers:

- **L-8 (High, REPRODUCED R1; prior CM-1: STILL EXISTS).** `read_file` has no
  self-cap below the 2MB refusal; the context caps insertion at 48k tokens
  (head-keep) and the marker says "re-read the file with a narrower line
  range". The loop records the ledger from the *tool's* span. On an 8,000-line
  file: context holds ~lines 1-2650, ledger says 1-8000, and the re-read of
  6000-6500 the marker recommends is refused with "Lines 6000-6500 of this
  file are already in context above." Two true-sounding messages that cannot
  both be obeyed — the exact prior-audit pattern.
- **L-10 (High, REPRODUCED R2; prior CM-2: STILL EXISTS).** Compaction evicts
  read contents and the recap says "Re-read one only if you need a line range
  you have not seen" — while `state.reads` survives compaction and refuses
  exactly those re-reads. Post-compaction the model *cannot* recover evicted
  content (the cached `last_results` replay is itself cut to 6,000 chars,
  L-17/CM-5: STILL EXISTS), stalls, and is forced to finish.
- **L-3 (High, REPRODUCED R3; prior CM-3: PARTIALLY FIXED).** `usage()` and
  `should_compact()` now count `tool_calls` arguments (the fixed half), but
  `_retention_cut` and `novel_tokens` cost messages by `content` alone. A
  write-heavy working set (20 × 40KB write_file arguments ≈ 200k tokens,
  contents empty) trips the trigger and is invisible to the cut: compaction
  evicts **zero messages, zero tokens** (R3 measured 201,304 → 201,304), so
  either the thrash detector ends the run NO_PROGRESS with a message blaming
  the working set, or `_complete`'s emergency path exhausts and the run ends
  ERROR "context cannot be reduced below budget". Write-heavy tasks are the
  product's core loop.
- **L-4 (High, REPRODUCED R5).** `compact()` *replaces* `self._recap`; the
  evicted set passed to the summariser never includes the previous recap. The
  first compaction's `do_not_retry` — the field the module's own docstring
  calls the reason the recap exists — is gone after the second compaction.
- **L-6 (Med, REPRODUCED R6).** `_retention_cut` never evicts the last message
  and `_whole_turn_cut` only walks forward: when the whole retained set is the
  results of an evicted assistant, the survivor is an orphaned `role:"tool"`
  message — protocol-invalid.
- **L-27 (Med, CODE-PROVEN; prior CM-4: STILL EXISTS).** The summariser
  transcript renders `content` only; assistant turns that were pure tool calls
  render empty, so exactly the write-heavy histories summarise worst.
- **L-18 (Med; prior CM-6: STILL EXISTS, acknowledged).** `pin_directive` /
  `set_plan` / `switch_mode` rewrite pinned layers *above* the working set;
  every steer, follow-up, plan and mode switch invalidates the prefix cache for
  the entire suffix — at 200k context that is a near-full prefill per steer.
  A cost decision, but an expensive one to take implicitly on every follow-up.
- Cost model: single calibrated chars-per-token ratio, EMA-smoothed, clamped —
  sound (SH-7: unbounded `_history` list is a slow leak). Tool-schema cost
  measured per turn (`observe_tool_schemas`) — the historical zero-default bug
  is genuinely fixed. `estimate_error` surfaced per turn — good.
- Insertion caps: `errors`-strategy keeps every diagnostic-looking line
  *unconditionally* — a log where every line matches a marker (e.g. `go test -v`
  with FAIL per line) exceeds its cap without bound (Low; interacts with TL-6).

## Tool calling

The protocol discipline is better than most agents' — assistant `tool_calls`
travel with the message, results carry real ids, malformed recorded arguments
are healed at wire time (`_parseable_arguments`), truncated replies answer
every declared call, cancellation answers abandoned calls, mode-refusals are
tagged and never cached cross-mode. And yet:

- **L-1 (Critical, REPRODUCED R4; prior TC-1: STILL EXISTS).**
  `[submit_plan, read_file]`: submit_plan succeeds, `_phase_ended` returns, and
  `read_file` is never answered. The orphan is permanent — in the session's
  context for every later turn and every follow-up. Same for
  `[finish, anything]` and for the forced-terminal-cap early return. The
  batch-mix cases demanded by the audit brief (`[read_file, write_file,
  finish]`) orphan everything after the terminal call's index.
- **L-5 (Med, REPRODUCED R10; prior TC-2: PARTIALLY FIXED).** `carry_from`
  exists now and carries the right things — but it carries `mutations_seen`
  into a run whose Router is reborn at 0, so the first tool batch of every
  follow-up after a mutating run trips the "world changed" wipe and clears
  every carried ledger. The comment beside the line believes it prevents
  exactly this. Router state (`touched`, `mutations`) is not carried at all, so
  a follow-up's `_verify`/`_unwritten_targets` see an empty change set against
  a carried plan.
- **L-19 (Med, CODE-PROVEN).** `PlanStep.file` is compared raw against
  normalised `router.touched`: a plan step naming `./handler/user.go` is
  forever "unwritten", which refuses the first `finish` and mis-headlines the
  DONE summary.
- Duplicate calls, unknown tools, alias habits, argument coercion, approval
  re-dispatch with corrected arguments — all verified correct (router six-check
  pipeline; see the tools table in the tools-audit section of `BUGS.md`).

## LLM integration

`shared/llm.py` (client used by the agent through the gateway proxy):

- **SH-1 (High, CODE-PROVEN).** A clean mid-stream EOF — no `[DONE]`, no
  `finish_reason`, no usage chunk — returns a truncated `ChatResult` as
  *success*: partial content, `truncated == False`, zero usage (feeding the
  calibration a free turn). The agent acts on half an answer with no signal.
- **SH-2 (Med).** The retry set is `ConnectError|ConnectTimeout|ReadTimeout`;
  `RemoteProtocolError` and `ReadError` — the canonical upstream-died-mid-SSE
  errors — fail on attempt 1. Backoff/jitter/Retry-After handling is otherwise
  correct and bounded.
- **SH-3 (Med).** Tool-call delta assembly keys slots by `fragment.get("index", 0)`
  — an index-less server folds parallel calls into one slot (concatenated
  arguments diagnosed as "malformed"); a nameless slot is silently dropped; an
  id-less call round-trips as `tool_call_id: ""`.
- Verified correct: duplicate/post-`[DONE]` frames ignored; read-through after
  `[DONE]` deliberately preserves the gateway's settlement window (the two
  sides genuinely interlock); `_looks_cut_off` heuristics; empty-completion
  recovery (one retry, thinking off); `UnsupportedParameterError` never
  retried; the loop's named→required→plain `tool_choice` degradation ladder.
- Output-token exhaustion: per-turn handling correct; repetition unbounded
  (L-13, above). `finish` never dispatched because the reply hit the limit is
  handled (all calls answered, told to shrink) — but again, only per-turn.

## Runtime / sessions

- **L-7 (High, CODE-PROVEN).** No persistence: sessions, events, contexts,
  loops, approvals are process memory; docstrings claim otherwise. Restart
  loses transcripts and the mutation list revert needs.
- **RT-1 (Med, CODE-PROVEN; prior RT-1: STILL EXISTS).** Resume =
  `new run + "The previous attempt ended: <summary>"` on a **fresh** context
  (`continued=False` skips both the context reuse and `carry_from`), while the
  EXHAUSTED summary sold it as a continuation. Follow-up *is* a continuation
  (context reused, carry attempted — see L-5).
- **L-9 (High, CODE-PROVEN).** `message_session` on a running session queues a
  steer; if the run finishes before the next drain, the text is neither
  delivered, nor recorded, nor converted to a follow-up — the developer's
  message vanishes. (Steers are also only recorded in the transcript *when
  drained*.)
- **L-12/L-26 (Med).** `runtime.contexts`/`runtime.loops` are never evicted
  (SessionStore trims sessions; these keep whole message lists forever);
  the in-RAM event log is unbounded and TOOL_RESULT events carry the *uncapped*
  tool content (the context caps its copy; the event log and SSE frame get the
  original).
- Approvals: registration-before-event ordering is right; timeout-as-rejection
  is right; `/extend` is broken at the wait site (EXT-1) and the decide/timeout
  race can tell the client "accept" for a rejection (L-22, Low).
- Unbounded concurrent sessions: `POST /v1/tasks` spawns a thread + baseline
  subprocesses with no cap (Med, local DoS).

## Concurrency

The model is deliberately simple — one worker thread per run, asyncio for HTTP,
`call_soon_threadsafe` into the event log, GIL-protected list appends — and
most of it holds. Races found: steer-loss (L-9, above); worker-thread writes to
`session.status`/`finished_at` unguarded against event-loop readers (benign
today); baseline thread joins with `timeout=180` and **drops the thread
reference** — a slow baseline lands mid-run, so early gates run un-baselined
(blaming the run for pre-existing damage) and later gates run baselined:
inconsistent verdicts within one run (L-16). `take_baseline`'s own
mutations-spanned guard is good, but its `go build` can rewrite `go.sum`
invisibly (GT-1). `asyncio.Queue.put_nowait` is called from the worker thread
only via `call_soon_threadsafe` — correct — except the shutdown fallback, which
records directly (subscribers are gone then; acceptable). Gateway: quota
reserve/settle interlock is well-built for one process; disconnect leak (GW-2),
Redis idempotency race (GW-4) and per-process reservation state (GW-5) break it
at the edges.

## Security

Threat model: the model is untrusted; repository content is untrusted; the
developer is the trust anchor via approvals.

Strong: path confinement (`resolve()`-then-contain, symlink-prefix-safe,
UNC/drive/ADS/reserved-name refusals — traced against constructed escapes and
held); argv-list-only subprocess use, `shell=False` everywhere; model
credentials stripped from child envs and refused in local configs at three
enforcement points; `delete_file` never auto-approvable; loopback token
compared with `compare_digest`; webview XSS surface clean (`textContent`
everywhere, strict CSP, https-only links); PKCE auth with rotation and reuse
detection; gotools checksum verification.

Gaps (full details in `BUGS.md`):
- **TL-7**: `run_terminal` re-opens destructive git (`reset --hard`,
  `clean -fdx`, `push --force`) and takes unconfined path arguments
  (`gofmt -w ../../x`) — approval-gated today, unguarded if ever auto-approved;
  `git_ops`'s "no destructive git is a property of the tool" claim is untrue
  one tool over.
- **RG-1**: `fx_wire`/`govalid_gen` write protected paths with `Approval.NONE`,
  bypassing both the protected-path approval and `_confine`.
- **SH-5b**: protected-glob matching is case-sensitive on a product whose
  primary platform is case-insensitive Windows — `dockerfile`/`GO.MOD` writes
  skip the gate. Plus root-only anchoring of `Dockerfile`/CI globs.
- **EXT-9**: the loopback token — the defence against local processes — is
  generated from `Math.random()`.
- **EXT-10**: `pythonPath`/`gotoolsPath`/`goPath`/`goplsPath` are
  workspace-overridable: a cloned repo's `.vscode/settings.json` picks the
  binaries the extension executes (Workspace Trust is the only gate).
- **TL-9** (flag): child processes inherit all non-model secrets.
- **PI-1** (inherent): file/search content reaches the model unfenced; the
  system prompt contains no data-vs-instructions guidance. Highest-risk sink
  (`run_terminal`) is human-gated — keep it that way (see TL-7).
- Gateway: hot-path 500s from unvalidated input (GW-3), plaintext refresh
  tokens in memory + lost on restart (GW-7), api_key in dataclass reprs
  (GW-14), unauthenticated `/v1/health` disclosure (accepted), no body-size
  cap (GW-6).

## Filesystem

- **TL-1 (Critical, REPRODUCED by the subsystem audit)**: read
  `surrogateescape` / write strict-UTF-8 asymmetry + `write_text` truncating
  before encoding ⇒ `patch_file` on a file with one stray non-UTF-8 byte
  leaves **zero bytes**, a generic failure, and no mutation record (so gate and
  revert never learn). TL-2: all writes non-atomic.
- **L-20 (Med, proven by the project's own failing tests)**: backslash paths
  are not normalised on POSIX — `handler\user.go` becomes a literal filename;
  three shipped tests assert the opposite and fail on Linux.
- Verified correct: read_file's clamped `meta.span` (the ledger's input is
  honest — the dishonesty happens at the context cap, L-8); 2MB/NUL-probe
  refusals; EOL preservation incl. `newline=""` discipline; patch uniqueness
  with excellent miss diagnosis; delete-idempotency; search pruning +
  symlink-dir skip. Note: search follows symlink *files* (suffix-less allowed),
  and the TOCTOU between resolve and open is an accepted residual (documented
  in `BUGS.md` as such).

## Command execution

- **TL-5 (High)**: timeouts kill the direct child only — `go build/test`
  grandchildren (compile/link/test binaries) survive every timeout; no process
  groups anywhere (the sidecar kill has the same gap).
- **TL-6 (Med)**: `capture_output=True` buffers unbounded output in memory;
  the 400KB cap applies post-capture. A runaway test can OOM the runtime.
- **TL-8 (Low)**: allow-list matches argv[0]'s basename; `./go` passes.
- Verified correct: shell-metachar refusal with the right redirect (naming
  `search_repo` for `grep`-shaped attempts), fixed argv lists, per-tool
  timeouts, `go_mod` check-mode restore, `MissingToolchain` classification.

## Git

- **L-11 (Critical)**: revert restores touched paths to HEAD and deletes
  touched paths absent from HEAD — destroying developer pre-run uncommitted
  edits and deleting developer untracked files the agent merely modified. The
  system elsewhere is admirably careful to never assume the repo starts clean
  (the whole Baseline design); revert assumes exactly that.
- `git_ops` itself is minimal by design (branch/add/commit, commit approval-
  gated); the bypass is `run_terminal` (TL-7). `_is_repo` vs cat-file
  distinction (don't delete files because git was absent) is correct and
  tested. Dirty-tree handling at the *gate* is the baseline's job and works;
  concurrent git operations are unguarded but single-run-single-thread in
  practice.

## Frontend / extension

Full findings EXT-1..22 in `BUGS.md`. The clean list is substantial (XSS/CSP,
SSE parser, auth flows, spawn hygiene, memory caps). The themes that matter:

- **Approval integrity** (EXT-1/2/3/5/17): "give me more time" doesn't extend
  the backend wait; the default "wait indefinitely" setting is a fiction over a
  hard 600s server reject with no warning; attaching to a running session
  replays answered approvals as live cards then reports them "recorded as a
  rejection"; the reconcile poll that should discover orphaned approvals can
  never start; polled approvals lose their session id.
- **Event-stream integrity** (EXT-4/13/14): webview seq survives reload while
  the host restarts at 0 (panel silently drops everything); duplicate raw
  re-emission before the monotonic guard; reconnect mid-stream splices
  streamed text around the outage hole.
- **Dead features** (EXT-6/15/16/18): context inspector unreachable, quota
  refresh cycle uncalled (and the backend never emits the QUOTA event the tree
  listens for), gate-rerun offer never invoked, six registered commands not
  contributed (the status bar directs users to a palette entry that does not
  exist).
- Plus L-15's backend half: streamed prose the loop discards after a forced
  re-ask is displayed and then contradicted.

## Performance

- The stable-prefix discipline is undermined at exactly the events it was built
  for: every steer/follow-up/plan/mode-switch rewrites a pinned layer above the
  working set (L-18) — near-full re-prefill at long context, per steer.
- `usage()` re-estimates every message ≥3× per turn (should_compact, complete,
  _usage) — O(context) regex passes; fine today, worth memoising per message.
- `_nearest` rglobs up to 5,000 files per missing path; `search_repo` walks the
  tree per call (documented trade); gate stages that mutate (govalid_gen)
  invalidate the gate cache key every verify (L-29).
- Gateway: full-body copies ×3 per request (GW-6); unbounded upstream pools
  (GW-12).
- Correct and cheap where it counts: intercepts answer repeats without
  dispatch; the gate is scoped and cached-when-unchanged; compaction retains to
  a floor (when it can see the costs — L-3).

## Observability

Good bones: monotonic event ids, per-turn USAGE with budget + estimate_error,
GATE events with stage timings, `context.inspect()` endpoint, metering headers
(session/turn/mode/estimate) to the gateway, `reasoning_leaked` alert.

Gaps: telemetry lies where behaviour lies (a compaction that freed nothing
still reports "compaction"; `_complete`'s emergency compaction bypasses the
loop's counter, L-24; `attempt` is 0-based on the wire and 1-based in the UI,
EXT-7); TOOL events carry no turn id; the QUOTA event type exists and is never
emitted (EXT-15); intercepted calls are visible (`intercepted: true`) but
ledger *wipes* (the L-5 carry wipe, mutation-invalidation) are silent, so the
operator cannot answer "why did it repeat that search"; no correlation id
between a loopback session and gateway ledger rows beyond session_id (fine) —
but nothing records *why* a retry happened at the client. Context inspector
exists server-side and is unreachable in the UI (EXT-6).

## Testing

704 tests; **red on Linux** (6 failures: 3 = the POSIX backslash gap L-20 —
tests right, code wrong; 3 = gofmt tests that fail rather than skip without the
toolchain, L-21 — contradicting the README's "skip cleanly" claim). The suites
are behaviourally written and strong on imagined failure paths (fake LiteLLM
endpoint asserting failure shapes; symlink escapes; quota all-or-nothing;
`[DONE]` read-through). But every high-severity finding in this audit sits in a
gap, and the pattern is consistent: **the tests exercise each component's own
discipline and never the seams** — no test asks "after a capped read, is the
ledger honest?", "after compaction, can the model re-read?", "is the wire valid
after a terminal-in-batch?", "does a disconnect leak the reservation?", "does
extend actually extend?". The budget regression suite simulates costs more
honestly than production ran them (its author noticed this for
`observe_tool_schemas`; the same applies to `_retention_cut` today).
`TEST_PLAN.md` provides the seam tests, the §27 mandated list, and four
permanent invariant checks.

## Adversarial testing

Eleven scripted-model reproductions were executed against the real modules
(R1–R11; behaviour table in `TEST_PLAN.md` §3). Outcomes: repeats, narration,
empty finish, phase-crossing calls and A/B alternation with identical args are
all contained by the intercept/stall/force machinery — that core genuinely
works. Not contained: terminal-in-batch (R4), capped-read tail (R1),
post-compaction re-read (R2), write-heavy compaction (R3), recap accumulation
(R5), compaction orphan (R6), refused-finish messaging (R7), truncation
repetition (R8), follow-up carry (R10), acting-phase lockout (R11), plus the
drifting-args alternation which only the research fence bounds (acceptable,
documented).

## Findings

The complete register with severity, confidence, component, impact and fix is
`BUGS.md` (44 core rows + subsystem lists). Headline set: L-1, TL-1, L-11, L-8,
L-10, L-2, L-3, SH-1, GW-1, L-9, EXT-1/2/3.

## Prior-audit regression check (§39)

| Prior finding | Verdict | Evidence |
|---|---|---|
| CM-1 insertion cap invisible to read ledger | **STILL EXISTS** | REPRODUCED R1; ledger (1,8000) vs context ~(1,2650); re-read refused |
| CM-2 compaction leaves read ledger stale | **STILL EXISTS** | REPRODUCED R2; recap invites the re-read the intercept refuses |
| CM-3 tool-call args ignored by retention | **PARTIALLY FIXED** | `usage()`/trigger fixed; `_retention_cut`/`novel_tokens` still blind — REPRODUCED R3 (0 tokens freed) |
| CM-4 summariser does not see tool calls | **STILL EXISTS** | loop.py:2128 renders `content` only; write-turns summarise empty |
| CM-5 repeated result cut to 6,000 chars | **STILL EXISTS** | loop.py:1258 `[:6000]`, replayed as "the current answer" |
| CM-6 follow-up rewrites stable prefix | **STILL EXISTS** (accepted cost) | pin_directive/set_plan/switch_mode rewrite pinned layers above the working set |
| TC-1 terminal tool orphans batch calls | **STILL EXISTS** | REPRODUCED R4 |
| TC-2 fresh Router loses carried state | **PARTIALLY FIXED → new bug** | `carry_from` added; carried `mutations_seen` vs fresh Router wipes the ledgers on the first batch — REPRODUCED R10; touched/gate state still not carried |
| TC-3 refused finish → wrong instruction | **PARTIALLY FIXED** | bounded force mechanism right; message still wrong — REPRODUCED R7 |
| TC-4 forced flag shared across phases | **PARTIALLY FIXED** | now documented as once-per-run by intent; cross-phase consumption remains |
| RT-1 resume is not a true continuation | **STILL EXISTS** | loopback.py:377 `continued=False`; EXHAUSTED message claims the opposite |

New bugs introduced by the fixes: the L-5 carry wipe (introduced by
`carry_from` + `mutations_seen`); the L-2 lockout (introduced by the
`MAX_RESEARCH_TURNS` fence + named-`finish` forcing); L-14's misleading message
(introduced by routing refused terminals through `must_answer`); L-15's phantom
streamed text (introduced by the forced re-ask). Each fix solved the failure it
targeted and left a sharp edge one interaction away — the argument for the
structural consolidations in `CHANGE_PLAN.md` over more point fixes.

## Root causes

1. **RC-1 — Two owners of "what has the model seen".** ContextManager caps,
   supersedes and evicts; the loop's `_State` ledgers record and refuse; no
   invalidation protocol connects them. → L-8, L-10, L-17, L-25, L-27
   (five findings, one cause). Fix class: ledger written/invalidated by the
   ContextManager; loop queries.
2. **RC-2 — The tool-protocol invariant is a discipline, not a checkpoint.**
   Four call sites do it right; two paths miss. → L-1, L-6. Fix class: one
   pre-dispatch wire assertion/repair.
3. **RC-3 — Two cost models for one budget.** `usage()` vs
   `_retention_cut`/`novel_tokens`. → L-3, L-24. Fix class: one
   `_message_cost()`.
4. **RC-4 — Lifecycle split-brain: context is session-scoped, Router and
   `_State` are message-scoped.** → L-5, L-19's follow-up half, RT-1's ledger
   loss, empty-touched-vs-carried-plan. Fix class: session-scoped Router
   carried with the context.
5. **RC-5 — Phase counters at run scope.** `research_turns` (reset only by
   submit_plan), `forced` (never). → L-2, TC-4.
6. **RC-6 — Recovery promised, not implemented.** No persistence, resume ≠
   continuation, revert assumes agent-only writes since HEAD. → L-7, RT-1,
   L-11, DOC-1.
7. **RC-7 — Subprocess/write hygiene.** No process groups, unbounded capture,
   non-atomic non-mirrored encoding writes. → TL-1/2/5/6.
8. **RC-8 — Single-process assumptions in the gateway.** Memory-resident auth
   and reservation state, check-then-set against Redis. → GW-1/4/5/7.
9. **RC-9 — Stream termination trusted.** EOF as success, index-optional
   deltas. → SH-1/3; EXT-4/14 are the UI's cousins.

## Priority plan

`CHANGE_PLAN.md` sequences the work. P0 = L-1, TL-1, L-11, L-8+L-10, L-2, L-3,
SH-1, GW-1, L-9, EXT-1/2/3 (+ the Phase-0 green-CI step). P1 = the reliability
and honesty set (L-4/5/6/7/13/14/15/17/19/25/27, RT-1, TL-5/6/7, RG-1, SH-2/3,
SH-5b, GW-2/3/4/5, EXT-4/5/9/10). P2 = leaks, observability, perf, dead code,
documentation truth.

---

# Final audit summary (§37)

## Ratings

```
Architecture:          7/10   thoughtful, measured, well-argued; undermined by
                              duplicated state and message-vs-session lifecycle split
Correctness:           5/10   core flows right; 11 reproduced defects in the seams
Reliability:           4/10   long/write-heavy/big-file/follow-up runs — the target
                              workload — each hit a reproduced failure
Tool protocol:         4/10   excellent discipline, two protocol-invalid paths, one
                              of which poisons whole sessions
Context management:    4/10   good design; ledger/context divergence unfixed; recap
                              loses its own reason for existing
Security:              6/10   confinement and approval design genuinely good;
                              run_terminal/protected-path/token-entropy/workspace-
                              settings gaps are all closable
Concurrency:           6/10   simple model, few races; the ones found lose user
                              messages and skew baselines
Performance:           6/10   right instincts (prefix, scoping, caching); prefix
                              invalidated by every steer; some unbounded captures
Observability:         5/10   good event spine; several surfaces lie or are dead
Testing:               6/10   704 behavioural tests; red on Linux; misses every
                              high-severity seam
Production readiness:  4/10   not yet — P0 list is short and concrete
```

## Direct answers

**1. Ten most serious problems.** (1) L-1 terminal-in-batch orphans → session
poisoned at the protocol level; (2) TL-1 patch_file zero-bytes a legacy file;
(3) L-11 revert destroys developer work; (4) L-8+L-10 the model is told evicted/
never-inserted lines are "in context above" and refused the re-read; (5) L-2
acting-phase lockout — forced `finish` while the gate says "make the edit";
(6) L-3 write-heavy contexts cannot compact → run death; (7) SH-1 silent
mid-stream truncation accepted as a complete answer; (8) GW-1 auth refresh dead
in production (every session dies at 15 min); (9) L-9 silent loss of a user's
steering message + L-7 nothing survives a restart; (10) EXT-1/2/3 approval
timeouts silently reject reviewed changes and forge rejection receipts.

**2. Infinite loops.** True infinite loops: none found — max_turns is a global
backstop. Effective loops (budget burned without progress): L-13 repeated
output truncation (to max_turns); L-3 compaction thrash (bounded by the thrash
detector, which then *misdiagnoses*); L-8/L-10 read-refusal ↔ stall cycles
(bounded by forced finish — the run ends, wrongly); drifting-args A/B
alternation (bounded only by the research fence).

**3. Corrupt tool-call history.** L-1 (orphaned batch calls — permanent), L-6
(compaction retains an orphaned result), SH-3 (merged/dropped/id-less calls at
assembly), L-15 (the model's real prose reply never enters history). Healed
cases: truncated arguments (`_parseable_arguments`), cancelled batches.

**4. Context hallucination.** L-8 (ledger claims lines never inserted), L-10 +
L-17 (post-compaction "already in context above" / truncated replay as full
answer), L-25 (stale carried reads after developer edits), L-4 (recap silently
forgets dead ends — the model re-trusts a disproved approach), L-27 (summariser
blind to what the run actually did), SH-1 (half an answer treated as whole).

**5. Unnecessary tool repetition.** L-10/L-17 (model re-asks because the
"answer" it is given is truncated or the content is gone), L-5 (follow-up
ledger wipe re-enables dispatch of exhausted calls), L-14 (refused finish told
it is repeating itself — teaching the wrong correction), EXT-14/L-15 (UI-level
phantom text prompting user re-asks).

**6. `finish` failures.** L-2 (finish forced when finishing is wrong), L-14
(refused finish mis-messaged), L-19 (finish refused over unnormalised plan
paths), L-13 (finish never emitted because output truncates — no dedicated
stop), MAX_FORCED_TERMINAL/refused-terminal machinery itself is sound.

**7. Session corruption.** L-1 (protocol-poisoned transcript carried into every
follow-up), L-7 (restart loses everything), L-9 (lost user message), L-5
(carried ledgers wiped), RT-1 (resume silently discards the conversation),
L-12/L-26 (unbounded retained state).

**8. User-code corruption.** TL-1 (zero-byte truncation), L-11 (revert), TL-2
(non-atomic writes), TL-10 (mixed-EOL restore converts the file), L-20
(backslash-named stray files on POSIX), TL-7 (destructive git via run_terminal,
approval-gated), GT-1 (baseline rewrites go.sum silently).

**9. Security vulnerabilities.** EXT-10 (workspace-settings choose executed
binaries), EXT-9 (Math.random loopback token), TL-7/TL-8 (run_terminal git +
basename allow-list), RG-1 (protected-path approval bypass by two tools),
SH-5b (case-insensitive protected-glob bypass on Windows), GW-3 (hot-path
500s), GW-7 (plaintext refresh tokens in memory), GW-14 (api_key in reprs),
GW-6 (no body cap), PI-1 (unfenced repo content — inherent, mitigated by
approval gates as long as TL-7 is honoured).

**10. Merely performance/UX.** L-18 (prefix re-prefill per steer), L-29 (gate
cache misses), EXT-7/8/11/12 (attempt numbering, bogus gate tables, elapsed
clock, two cache-% denominators), EXT-6/15/16/18 (dead features/commands),
GW-12 (pool bounds), usage() recomputation, `_nearest` rglob cost.

**11. Must fix before real users.** Phase 0 + Phase 1 of `CHANGE_PLAN.md`,
verbatim: green CI; L-1 (+ the wire checkpoint); TL-1/TL-2; L-11; L-8+L-10;
L-3; L-2; L-9; EXT-1/2/3(+5); GW-1..4; SH-1(+2,3). With those, the agent stops
corrupting sessions, files, and developer trust; everything else degrades
gracefully.

**12. Can safely wait.** All of P2: leaks (bounded by daemon restarts today),
observability truth-ups, dead extension features, gateway hygiene beyond the
four above, performance work (L-18 measured first), documentation pass —
plus P1 items whose failure mode is honest degradation (L-4 recap merge, L-27
summariser rendering, RT-1 copy fix if the semantics change waits).
