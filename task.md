# DakCoder — audit remediation task board

Working against `AUDIT.md` / `BUGS.md` / `CHANGE_PLAN.md` / `TEST_PLAN.md`
(audit dated 2026-09-02). Every step lands its regression test in the same
change. `ARCHITECTURE_AUDIT.md` is updated as each structural claim in it stops
being true.

Status key: `[ ]` pending · `[~]` in progress · `[x]` done

## Phase 0 — restore the test signal

- [x] 0.1 green suite on Linux (L-20 backslash normalisation, L-21 gofmt skips)

## Phase 1 — stop losing data and corrupting protocol (P0)

- [x] 1.1  answer every call in a batch that hits a terminal tool (L-1)
- [x] 1.2  wire-invariant checkpoint before dispatch (L-1/L-6 class)
- [x] 1.3  patch_file/write_file: surrogateescape + atomic write (TL-1, TL-2)
- [x] 1.4  revert must not destroy developer work (L-11)
- [x] 1.5  read ledger reflects what survived the insertion cap (L-8)
- [x] 1.6  compaction invalidates loop ledgers (L-10, L-17/L-25 part)
- [x] 1.7  retention cut sees tool-call arguments (L-3)
- [x] 1.8  un-jam the acting phase (L-2)
- [x] 1.9  never lose a steer (L-9)
- [x] 1.10 approval timeout integrity (EXT-1, EXT-2, EXT-3, EXT-5)
- [x] 1.11 gateway auth refresh + quota integrity (GW-1, GW-2, GW-3, GW-4)
- [x] 1.12 LLM client: fail on silent mid-stream EOF; retry mid-stream drops (SH-1, SH-2, SH-3)

## Phase 2 — reliability and honesty (P1)

- [x] 2.1  cumulative recap (L-4)
- [x] 2.2  carry that survives + session-scoped Router (L-5)
- [x] 2.3  compaction orphan edge (L-6)
- [x] 2.4  truncation counter (L-13)
- [x] 2.5  honest messages (L-14, L-17, RT-1 copy)
- [x] 2.6  plan-path normalisation (L-19) + follow-up read staleness (L-25)
- [x] 2.7  resume as continuation (RT-1)
- [x] 2.8  process hygiene (TL-5, TL-6, TL-7, TL-8)
- [x] 2.9  persistence (L-7)
- [x] 2.10 extension P1s (EXT-4, EXT-9, EXT-10)
- [x] 2.11 summariser sees tool calls (L-27); forced-prose retention (L-15)
- [x] 2.12 protected-glob case-insensitivity (SH-5b); registry approval bypass (RG-1)

## Phase 3 — P2 cleanups

- [x] 3.1 leaks (L-12 contexts/loops eviction, L-26 event payload caps, SH-7, GW `_settled`)
- [x] 3.2 observability (turn ids on tool events, L-24 emergency compaction count, EXT-7/8/15)
- [x] 3.3 perf (L-29 gate key; L-18 measured, then moved — see the log)
- [x] 3.4 gateway hygiene batch — GW-5..14; the multi-worker store is what remains
- [x] 3.5 extension (EXT-6, EXT-16, EXT-18, then EXT-11/12/13/14/19-22)
- [x] 3.8 rehydration — a restart restores the conversation, not only the record
- [x] 3.9 SH-6 — the delta interval flush needs something to ask it the time

## Phase 4 — reported from a live run (not in the audit)

- [x] 4.1 FS-1 a file larger than one reply could not be written at all
- [x] 4.2 FS-2 the truncation advice could not work for a single oversized call
- [x] 4.3 FS-3 the truncation bound resets, so alternating thrash is unbounded
- [x] 4.4 FS-4 a shell redirection is answered with the tool that reads
- [x] 4.5 output budgets raised; the window arithmetic made a checked invariant
- [x] 4.6 run accounting: per-call tokens, truncations, and the evidence report
- [x] 3.6 documentation truth pass (DOC-1) — done early: the four false claims
- [x] 3.7 residual loop rows (L-16, L-22, L-23, L-30, TL-10, GT-1)

## Regression tests (TEST_PLAN §1-2) — landed with their step

- [x] test_terminal_tool_in_batch_answers_all_calls (1.1)
- [x] test_capped_read_then_tail_read_dispatches (1.5)
- [x] test_compaction_invalidates_read_ledger (1.6)
- [x] test_write_heavy_compaction_frees_tokens (1.7)
- [x] test_follow_up_carry_survives_first_batch (2.2)
- [x] test_repeated_result_replay_marks_truncation (2.5)
- [x] test_refused_finish_message_is_accurate (2.5)
- [x] test_forced_flag_does_not_cross_phases (TC-4)
- [x] test_repeated_truncation_has_hard_stop (2.4)
- [x] test_resume_semantics_match_message (2.7)
- [x] test_steer_never_lost_on_finish_race (1.9)
- [x] test_summarizer_failure_falls_back (pin)
- [x] test_over_budget_fallback_failure (1.7)
- [x] test_invalid_line_ranges (pin)
- [x] test_acting_phase_not_locked_out_after_12_turns (1.8)
- [x] test_recap_accumulates_across_compactions (2.1)
- [x] test_compaction_never_retains_orphaned_result (2.3)
- [x] test_patch_file_non_utf8_preserves_content (1.3)
- [x] test_subprocess_timeout_kills_process_tree (2.8)
- [x] test_revert_blocks_on_pre_run_developer_changes (1.4) — landed as `test_revert_restores_pre_run_developer_changes` (+3 more)
- [x] test_plan_paths_normalised (2.6)
- [x] SH-1 / SH-3 client tests (1.12)
- [x] GW-1..4 tests (1.11)
- [x] EXT-1/2/3/4 tests (1.10, 2.10)
- [x] invariant checks 1-4 (TEST_PLAN §4)

## Where this stands

**Suite: 854 tests, green** (it was 704 with 6 failing). Extension `npm run
verify` green end to end: typecheck, 69 tests, bundle, credential scan, 59/59
commands in both directions, l10n, gotools manifest.

Every row of `CHANGE_PLAN.md` has landed, including the four that were carried
as deliberately-deferred after Phase 3. What closed them:

| Row | What it took |
|---|---|
| L-18 | Measured first, as the plan asked. At a 100-turn context a steer re-prefilled 75,764 of 80,543 tokens (94.1%); it costs 35 now. The pinned block is split — task and acceptance stay above the working set, plan and directives move to `Layer.DIRECTIVE` below it. |
| GW-7..13 | Refresh tokens are stored as SHA-256 verifiers, not credentials; the provider token is out of reprs; revoked families expire; the identity adapter keeps and closes one HTTP client; groups are read past page one; the quota script's ZSET nonce comes from the caller; the upstream pool has a ceiling; the in-memory ledger is bounded and counts what it drops. |
| EXT-11/12/13/14, EXT-19..22 | Elapsed clock, one cache denominator, dedup above the re-emission, streaming buffer cleared on reconnect, task listener settled two ways, notices held for a closed panel, and `deactivate` no longer double-disposes every subscription. |
| Context rehydration | `rehydrate.py` replays `events.jsonl` through the ContextManager's own append methods. A restart now restores the conversation. |
| SH-6 | Found on a second sweep, not from the board — the row was in `BUGS.md`'s P1 table and in no step of the change plan. `max_interval` had nothing calling it while the model was silent. A ticker for the length of a streamed call, and a thread-safe coalescer. |

**What is still open, and why.**

| Row | Why |
|---|---|
| Multi-worker gateway state | Reservations and auth sessions are per-process, so two workers can double-charge across each other. This one genuinely is a deployment decision: it needs the `QuotaStore` seam extended to sessions and a Redis to point it at. |
| The loop's `_State` after a restart | The messages come back; which searches were exhausted and which reads were refused do not. The loss is one-directional — the agent may repeat a search, never skip work it has not done. |
| GT-2/3, PI-1 | The audit marked them BY-DESIGN and they still are. |
| R11 | Assessed, not fixed. See below; the reasoning has not changed. |

Two things worth knowing before the next run against a real repository:

* **The undo store writes into the workspace** (`.dakcoder/sessions/<id>/undo/`),
  one pre-image per mutated file, capped at 2 MB each. `.dakcoder/` ignores
  itself, but it is real disk in the developer's tree.
* **Approval deadlines are now one number.** `dakcoder.approvalTimeoutSeconds`
  reaches the runtime as `DAKCODER_APPROVAL_TIMEOUT` at spawn, so it takes effect
  when the runtime restarts, and `0` genuinely means no deadline on both sides.

---

## Log — audit remediation

### 4.6 — the accounting a claim about the window can rest on · done

Asked for: log prompt tokens per call, count truncations, and enough detail on
this server to support the claim that a 262k window is not enough for large
codebase tasks.

Most of the facts were already being emitted — a `usage` event per turn, a
`gate` event per compaction, a failed `tool_result` per truncated reply. Three
things were missing, and the third is the one that matters.

**Nothing added them up.** Answering "is this window big enough" meant reading a
transcript and counting by eye, one run at a time. `metrics.py` is the record:
one `RunMetrics` per run, emitted as a `metrics` event before `end` so it lands
in `events.jsonl`, and summarised as one line in `runtime.log`.

**Two facts were prose rather than data.** A truncated reply was a
`tool_result` with `ok: false` and an English sentence, so counting output-limit
hits meant string-matching the event stream — which is not a thing a report
should have to do about its own events. And all three intercept ledgers reported
as a single `intercepted: true`, though only one of them says anything about the
window: a refused re-read is a turn spent because content had to be kept out of
the prompt, while a cached repeat is the model being slow to move on. Both are
structured now (`truncated_by_output_limit`, `intercept: cached|dead_end|re_read`).

**The runtime configured no logging at all.** Every `log.info` and `log.warning`
in the package went to the root logger's default handler and was discarded;
uvicorn had its own level and that was the only thing anybody saw. So the line
this step adds would have gone nowhere. `serve._configure_logging` sends
dakcoder's loggers to stderr — which `start.sh` already redirects to
`deploy/logs/runtime.log` — with `DAKCODER_LOG_LEVEL` for per-turn detail.

**Pressure and loss are kept apart, because a claim depends on it.** *Pressure*
is a compaction firing or a reply being cut off: real, but a threshold can be
moved and a budget retuned, so on its own it argues about tuning. *Loss* is a
file evicted and then read again, or a read refused because the content was
already held — a window large enough for the task produces none of it at any
threshold. `lost_work` is the second, and it is the number the argument rests on.

One deliberate piece of care: a second read of a file is only counted as a
re-read *after an eviction of that file*. Two reads with no compaction between
them is a model being repetitive, and counting that as evidence would inflate
exactly the number the claim depends on.

**What the task actually needed.** `read_file` now records the true byte count in
`meta`, because the event stream caps content at 64,000 characters and a report
asking "how much source did this task need" would otherwise be measuring the
cap. `scripts/context-report.py` totals the unique source a run had to read and
puts it against the prompt budget, so a task whose *files alone* exceed the
window is arithmetic rather than an argument. It counts only `read_file` bytes —
not the system prompt, the schemas, the plan, the assistant messages or any
other tool result — so it is a floor on what the task required.

On a simulated whole-service migration the report reads: 180 turns, peak prompt
233,000 (89% of the window), 5 compactions discarding 755k tokens, 40 files
evicted and then read again, 2.6 MB of source read = ~816k tokens of files
alone against a 235.5k budget — *does not fit*.

**One accumulator, two drivers.** The loop feeds it live through a single funnel
in `run()` — a thin wrapper over `_run`, because events come from a dozen nested
generators and a tee anywhere else could be missed by omission, which is exactly
how the tool-call invariant became a discipline two paths forgot (L-1). A report
feeds it a stored journal. Two implementations of "add these up" is how the live
number and the reported one come to disagree, and a test asserts they do not.

The accumulator holds counters and path sets, never content, so a run pays
bounded memory and retains no transcript. The event is `metrics`, which both the
extension and the webview ignore by contract C2 — verified, since an unknown
type reaching a renderer is how this kind of addition breaks a panel.

`deploy/README.md` documents all three levels, including the SQL against the
gateway's Postgres ledger — the billing-grade record, with the endpoint's own
token counts rather than the agent's estimate. One of those queries measures the
estimator error directly, which is also how to decide whether `OUTPUT_RESERVE`
(step 4.5) is larger than it needs to be.

Tests: seven in `test_regression_audit.py`, including one that runs the report
as a subprocess against a journal with the truncated last line a hard kill
leaves.

**Amended after a redeploy showed nothing.** The first cut logged only at the
*end* of a run, so a restarted runtime that had not completed one looked exactly
like a runtime where the change had not landed — and the runtime's whole log,
across every restart, had been one `{"port": ...}` line. Two additions, both
about being able to tell those apart:

* a **startup line** naming the level and the budget arithmetic, so a redeploy
  proves the plumbing on its own and records what configuration a run was made
  under;
* a **per-call line** — prompt tokens against the budget, completion, cached,
  and the estimate ratio — which is what the original request asked for first
  and what makes a run visible while it is climbing rather than only once it
  has arrived.

Verified against the live endpoint rather than a stub: a second runtime on a
spare port, a real task, `Qwen3.6-35B` answering, and the three kinds of line in
`runtime.log`.

### 4.5 — the output budgets, and the arithmetic nobody was checking · done

Asked for directly: raise the output limits, since a 245,760 prompt budget
against a 262,144 window leaves 16,384 and only 6,144 was being used.

The observation is right, and the reason it matters is FS-1 above. A reply has
to hold the prose, the tool name and the whole of every argument, so
``max_tokens`` is the real limit on what a single call can carry. At 6,144 the
largest file ``write_file`` could create in one call was about 21 KB. ``append``
removed the hard floor, but every chunk is a turn and every turn is a full
prefill, so the ceiling still costs real money.

**Where the room came from.** Not from the 16,384. That number is not spare: the
design reserves ~10k of it for two things the budgets cannot see — the chat
template's own wrapping, and the fact that ``prompt_budget`` is enforced against
an *estimate*. The estimator is calibrated per session against the endpoint's
real ``prompt_tokens``, so in steady state it tracks; the reserve is for when it
does not, and its bounds alone permit 2.0 to 6.0 chars per token. Spending it is
the change that passes every test and then fails on the *last* turn of a long
run, where the prompt is largest and a context-length 400 is not retryable — the
run dies having done the work.

So the room came out of the prompt ceiling, which makes it a stated trade rather
than a borrowed margin:

    CONTEXT_WINDOW  262,144
    - max_tokens     16,384   (agent, was 6,144)
    - OUTPUT_RESERVE 10,240   (unchanged)
    = PROMPT_BUDGET 235,520   (was 245,760, -4.2%)

ask and planner go 4,096 -> 8,192. A plan is one ``submit_plan`` call whose
arguments hold every step, so it has the same shape of limit as a write.

**What the prompt cut costs: measured, not assumed.** The worry was that a lower
ceiling compacts earlier and ``test_budget_regression.py`` goes red on its own
terms. It does not, and the reason is the interesting part — the simulation
peaks at ~115k against a ceiling of 235,520, so a run is nowhere near either the
old number or the new one, and cutting 4.2% off a ceiling nothing reaches costs
nothing:

    P95 prompt   113,961 -> 114,811   (+0.7%)
    novel total  846,221 -> 846,666   (+445 tokens)
    compactions        0 ->       0   (unchanged)

What it buys: the largest single ``write_file`` goes from about 21 KB to about
56 KB.

**The part worth keeping.** ``prompt_budget`` and ``max_tokens`` were
independent numbers in separate paragraphs, and the only place their sum ever
appeared was a sentence of prose. Nothing evaluated it. ``ModeConfig`` checks it
now: prompt + output + reserve must fit the window, or the config refuses to
load where it is written rather than 400-ing on the last turn of a long run. A
property nothing evaluates is a comment, and this one was about to be edited by
hand.

Two related fixes fell out. ``CONTEXT_WINDOW`` and the gateway probe's
``EXPECTED_MAX_MODEL_LEN`` are the same deployment fact in two packages, so a
test pins them equal. And the probe's comment said "the agent caps its own
prompts at 32k", which stopped being true when the budget was re-based on the
model's window and was wrong by a factor of seven by the time anyone read it.

Two tests also changed rather than broke: both asserted the literal ``4096``
where they meant "the planner's own budget", so they failed on a retune that
said nothing about them. They read the config now.

Tests: four in `test_regression_audit.py` for the window invariant.

### 4.1-4.4 — the write loop, reported from a live run · done

Not from the audit. A transcript was handed over showing turns 29 to 33 of a run
asked for a report over ten repository files: `write_file` cut off, `write_file`
cut off, `run_terminal cat > report.md` refused, `write_file` cut off,
`write_file` cut off. The model's own narration across those turns was "Let me
write the report in chunks", "Let me write it in parts", "Let me split it into
multiple files and combine" — the right idea, three times, and each attempt came
back identically.

It was right and the tools were wrong. Four defects, and the first is the cause.

**FS-1 — a file larger than one reply was not writable.** A model's whole reply
— prose, tool name and the entire `content` argument, JSON-escaped — has to fit
one `max_tokens` budget, 6,144 for the acting mode. That caps a single
`write_file` at roughly 24 KB of text. Measured on report-shaped markdown: 16 KB
fits at 4,240 tokens, 32 KB does not at 8,472. And there was no second way in.
`write_file` refuses to overwrite, which is a deliberate and correct safety
property; `patch_file` needs a unique anchor *in a file that already has one*,
which the first chunk of a new document does not have; `run_terminal` cannot
write. The three tools compose to "documents up to 24 KB only", and nothing said
so.

`write_file` takes `append` now. Safe by construction — it adds at the end and
can destroy nothing — and it makes chunked writing uniform: the same call for
the first chunk and every one after it, with no anchor to guess. Two details
that would have been bugs. An append adds **no** trailing newline, where a
create does: a chunk boundary can fall mid-word and a newline inserted there
splits it. And an append to an existing file records `MODIFY`, not `CREATE`,
because `revert` reads that kind to decide between restoring bytes and deleting
the file — a wrong kind there would have revert delete a file the run did not
create.

Verified end to end: the 64 KB report that could not be written at all now goes
in twelve appends and comes back byte-identical.

**FS-2 — the advice could not work.** A cut-off reply got one paragraph
whatever its shape: "Make the next reply shorter: fewer tool calls in one turn,
and less prose before them. One call is enough." That is correct when a batch of
five was cut off in the fifth. It is useless when the reply held *one* call
whose single argument is the thing that does not fit, because there is nothing
left to remove — and the transcript is four turns of a model following it
exactly and being cut off in the same place. The message branches now: a lone
content-bearing write is told the content itself is too large and given the
chunked call to make, and everything else still gets the shorter-reply advice,
which for a batch is right.

**FS-3 — the bound could be dodged.** `MAX_TRUNCATED_TURNS` is three, and the
streak resets on any reply that arrives whole. Turn 30 — the refused
`run_terminal` — was a whole reply, so the streak never got past one and the run
was bounded by nothing but `max_turns`. That is not an unlucky shape; casting
about for another way to send something too large is exactly what puts an
ordinary call between two oversized ones. There is a run total now,
`MAX_TRUNCATIONS = 6`, which never resets, and the run ends on either bound
naming which one it hit.

**FS-4 — the last exit was mislabelled.** `_TERMINAL_ALTERNATIVES` is keyed on
the binary alone, so `cat > report.md` — a write — was answered "Use
read_file.": advice for the opposite operation, handed to a run that had
exhausted its ways to write a large file and was trying the shell as a last
resort. Wrong advice at the end of a dead end is worse than none. A redirection
token in argv is now recognised and answered with the tool that writes, plus the
fact that `>` was passed to the process as a literal argument because argv never
goes through a shell. A plain `cat go.mod` still says `read_file`.

**What it cost.** The `append` parameter pushed the agent mode's stable prefix
from 3,771 to 3,812 tokens, over the 3,800 ceiling `test_prompts.py` asserts —
which is the tripwire working as its own comment says it should. The text was
tightened twice, `write_file`'s description stopped repeating "use patch_file
for that" (`ToolSpec.instead` and the runtime refusal both already say it, and
neither is in the prefix), and what was left was a decision rather than a
rounding error: the ceiling moved to 3,850 with the reasoning recorded beside
it. The 41 tokens buy the model knowing *before* it tries that a large file can
be chunked. The alternative is not free — without the hint it discovers the wall
by hitting it, at a full 6,144-token reply plus a prefill per attempt, and the
reported run spent four turns doing that and never got there. The schema text is
in the stable prefix, so it is a cache hit after the first call of a run; the
wasted turn is not.

Tests: six in `test_fs_tools.py` for the append path, four in
`test_regression_audit.py` for the advice, the bound and the refusal.

### 3.9 — SH-6, found by re-reading the register rather than the board · done

This one was not on the board. It is a P1 row in `BUGS.md` and no step of
`CHANGE_PLAN.md` claimed it, so working the plan end to end was never going to
reach it; it turned up on a sweep that walked every ID in the register and asked
what in the tree answers for it. Three had nothing: two were Phase 0 fixes that
predate the citation convention, and this was real.

`DeltaCoalescer` flushes on either of two triggers, and its own docstring says
which one matters: "``max_interval`` — enough time passed that holding it would
look like a stall. This is the one that matters: without it, a model that pauses
mid-sentence leaves the last few characters buffered indefinitely, which reads as
a hang rather than as latency."

The deadline was evaluated in exactly one place: inside `feed`. Nothing calls
`feed` while the model is silent — that is what silent means — so the check only
ever ran on the *arrival of the next fragment*, which makes it a statement about
the gap that just ended rather than the one currently open. A model that stopped
mid-sentence with thirty characters buffered emitted nothing at all until it
started again, and then emitted them late. The paragraph describing the failure
was sitting directly above the code that shipped it, which is the whole reason
the audit's confidence column exists.

Two changes. `flush_due()` is the question a clock can ask — flush only if the
buffer is non-empty *and* the interval has passed — and `AgentLoop._complete`
runs a daemon thread that asks it every half-interval for the length of one
streamed call, stopping before the tail flush so the last fragment is emitted
once. The coalescer is thread-safe now, because the ticker and the stream are
different threads and a buffer two threads append to and drain without a lock
loses text — invisibly, since the run still finishes and the `assistant` message
at the end is complete, so only the streamed view would be wrong.

Cost: one wake-up every 40 ms while a call is in flight, and nothing between
calls. The full suite runs in the same time it did before.

Tests: `test_a_pause_mid_sentence_does_not_hold_the_text`,
`test_the_interval_flush_does_not_fire_early`,
`test_an_idle_coalescer_has_nothing_to_flush`, and
`test_the_ticker_and_the_stream_cannot_lose_text`, which races 2,000 fragments
against a spinning ticker and asserts the concatenation is exact.

### 3.3 (second half) — L-18, measured and then moved · done

The change plan's instruction was "measure before moving", and it was the right
instruction: the row had been carried through two audits as accepted-by-design
on the strength of an estimate. So it was measured, with the ContextManager's
own `novel_tokens` — the method whose docstring calls itself "what a prefix
cache actually has to prefill" — over a context of the shape a migration run
reaches (a system prompt, a mode overlay, then N turns of assistant-with-a-read
and its result):

```
turns   prompt      a steer re-prefills          after
    5    8,633    3,854  (44.6%)                    35
   20   19,983   15,204  (76.1%)                    35
   50   42,693   37,914  (88.8%)                    35
  100   80,543   75,764  (94.1%)                    35
  200  156,243  151,464  (96.9%)                    35
```

The same sentence appended to the working set instead cost 11 tokens. A
developer typing one correction at turn 100 was paying to re-read the entire
conversation, and `set_plan` — which fires on every plan submission — was paying
the same. That is not a cost worth accepting; it is the cost the prefix
discipline exists to avoid, incurred at exactly the events the discipline was
built for.

What moved is narrower than "move the directives". The pinned block held four
things and only two of them mutate: the task statement and the acceptance
criteria are written once by `set_task` and never again, so they are the stable
head and stay where §6.1 put them. The plan and the directives move to a new
`Layer.DIRECTIVE`, assembled *after* the working set. Pinned is about eviction,
not position — compaction only ever consumes `_working` — so nothing became
evictable, which is the property the old placement was defending. And the
instruction the developer typed most recently now sits closest to the model's
next token, which is where a correction belongs.

Two tests changed rather than broke, and both were asserting the placement
rather than the property: one wanted the plan text inside the `TASK` message,
the other wanted it in `Layer.TASK` specifically. They assert pinning now.

Tests: `test_a_steer_does_not_reprefill_the_whole_conversation`,
`test_pinning_a_plan_does_not_reprefill_either`,
`test_the_directive_layer_is_pinned_even_though_it_is_last`.

### 3.4 — the gateway rows, as code rather than as a deployment decision · done

The previous note said GW-7..13 "need a store — a deployment decision, not a
code one". That was true of one of them and a dodge for the rest.

**GW-7.** The refresh tokens were the *keys* of a dict: thirty-day credentials
held in plaintext, in the process that also holds the model API keys, for as
long as they are valid. Nothing in the service ever needs a token back — refresh
only has to *recognise* one — so it stores SHA-256 of it and the plaintext lives
only inside the request that presented it. Unsalted and uniterated, deliberately
and unlike a password hash: these are 256 bits from `secrets.token_urlsafe`,
there is no dictionary to attack, and the lookup is on the hot path of every
refresh. The GitLab access token beside it *is* still held, because `recheck`
has to use it, but it is `repr=False` now — a dataclass renders every field, and
that one was one exception context away from a log. `_revoked_families` was a
set that only grew; it expires with the refresh TTL.

**GW-9** was survivable only while GW-1 was broken. `_client()` built a fresh
`httpx.AsyncClient` on every call and never kept or closed one; with refresh
actually working that is a leaked connection pool per session per fifteen
minutes, for the life of the process. It keeps one, builds it under a lock, and
the app closes it at shutdown — but only if it built it, because a client passed
in by a test belongs to the test.

**GW-10.** `/api/v4/groups` asked for `per_page=100` and read one page. Roles
are mapped from group paths, so a developer in more than a hundred groups —
ordinary in a large GitLab, where every project's parent counts — could lose the
one group that grants their role and be told they had no entitlement. A
truncated group list is a wrong answer that looks exactly like a correct one.
Paged, bounded at twenty pages so an IdP that never returns a short page cannot
turn a sign-in into a loop.

**GW-8** is the one I would have missed reading quickly. The quota script's ZSET
member was `amount:now:i:sha1hex(key .. now .. i)` — and hashing three values
already present in the member adds no entropy whatsoever. `ZADD` on an existing
member updates its score instead of adding a row, so two charges for one series
in the same timestamp collapsed into one and the second was silently lost. The
nonce comes from the caller now, one per invocation, which is what `adjust`
directly beneath it had always done.

**GW-12** is the same shape of mistake in a different library. The pool was
built with `httpx.Limits(max_keepalive_connections=64, keepalive_expiry=300)`,
and constructing a `Limits` at all replaces httpx's default — which caps
connections at 100 — with whatever the object says. An unset `max_connections`
on an explicit `Limits` is `None`, meaning no cap. So the line written to *raise*
the keep-alive ceiling silently removed the connection ceiling, and the failure
mode is a gateway running out of file descriptors under concurrency and looking
like the model endpoint refusing connections.

**GW-13.** `MemoryLedger` is what every deployment without
`DAKCODER_POSTGRES_DSN` runs on, and the class that calls itself "the system of
record" kept an unbounded list. It is a bounded window now that counts what it
drops, and says so in a log the first time and every thousandth. `PostgresLedger`
still fails open on a write — the quota decision is already enforced, and
refusing a turn over bookkeeping would cost the developer their work — but it
counts and logs the hole rather than only handing it to an optional callback.

What is genuinely left is the multi-worker case: reservations and auth sessions
are per-process. That one does need a shared store, and the seam to extend is
`QuotaStore`.

Tests: eleven, in `test_regression_gateway.py`, one per row.

### 3.5 (second half) — the extension UI rows · done

**EXT-11.** `_startedAt` was set once with `??=` and never moved, so the elapsed
clock measured the *session* rather than the run: a follow-up sent twenty
seconds ago on a session opened three hours earlier drew "Elapsed 3h" in the
status bar, and `extension.ts` derives the panel's clock from the same number. A
run boundary — a hydrate that finds the session running again, or a `turn_start`
arriving after a `finish` — starts a new clock.

**EXT-12** is the one with the comment. `contextMeter` divides by `budget`,
`cacheLabel` divides by `budget`, and `chat.js` divides by `prompt_tokens` — so
the status bar and the panel printed different cache percentages for the same
turn, directly beneath a docstring saying the wire carries both figures "so that
no two surfaces can round their way to different answers". `prompt_tokens` is
the right denominator: the question is what fraction of the prompt we just sent
came from cache, and the budget is not what we sent. One function now.

**EXT-13.** `receiveEmitter.fire` sat above the monotonic guard, so a duplicate
the state machine correctly refused was still handed to every `onDidReceive`
consumer — the panel appended the row twice, the three trees applied it twice,
and a repeated `gate` event put a second re-run offer in front of the developer.
The dedup has to be the first thing that happens to a duplicate, not the last.
The transient events still bypass it, because they carry the id of the message
they precede.

**EXT-14.** Deltas are not persisted and not replayed, so reconnecting resumes
from `lastId` and every delta emitted during the outage is gone. The buffer was
not cleared, so the pre-outage half-sentence and the post-reconnect deltas were
concatenated into one continuous sentence with no sign anything was missing.
Cleared on a dropped link; the authoritative `assistant` event carries the whole
message and arrives on the new connection.

**EXT-19.** The gate re-run awaited `onDidEndTaskProcess` and disposed the
listener only on the event it was waiting for. A task the developer cancels, or
one whose shell will not spawn, ends with `onDidEndTask` and no process event at
all — so every cancelled re-run leaked a listener for the life of the window and
left its promise pending for ever. Two listeners, one settle, and a shutdown
hook that removes itself.

**EXT-20/21.** The `answered` set was unbounded beside a `receipts` map that
carried the comment explaining why it should not be. And `post` returns early
without a view, while `flush` spliced the queue *before* checking for one —
so a notice raised before the panel was first opened, or while it was closed,
or in the 16 ms between a post and its flush, was simply never said. That
includes the notice reporting an approval released as a rejection, which is the
one a developer most needs to have seen. Notices are held, bounded, and drained
after the transcript on the next `replay`; the flush checks first and splices
second.

**EXT-22.** `deactivate` iterated `context.subscriptions`, which VS Code
disposes itself once `deactivate` returns — every disposable in the extension
was torn down twice on shutdown, and the ones that did not survive it failed
inside a `catch` where nobody would see them. The only thing that needs ordered
shutdown is the runtime, because it owns a child process the host will not kill;
that is what the list holds now. While there: `resolveWebviewView` pushed its
listeners onto the *provider's* disposables, so a view hidden and shown five
times had five live `onDidReceiveMessage` handlers and one `submit` started five
runs. They belong to the view.

Tests: six in `state.test.ts` (69 total, was 63).

### 3.8 — a restart restores the conversation, not only the record · done

Step 2.9 made the transcript survive a daemon restart. That is what the *panel*
needs. It is not what the agent needs, and `follow_up` said so in its own
comment: with no context to continue, it re-seeded the original task —
"degraded, but not silently". In practice, degraded means a developer reloads
their VS Code window at turn 40 of a migration, types "carry on with the repo
layer", and gets an agent that begins the migration again, with the transcript
proving it had already read the whole service on screen beside it. A window
reload is not an unusual event.

`rehydrate.py` replays the stored events through the ContextManager's own append
methods rather than deserialising them into messages. That is the whole design
decision: there is one assembler (§6.4), and a restored context goes through the
same insertion caps, the same read-slice ledger and the same supersession rules
as a live one.

Three things it is careful about.

*A turn is replayed whole or not at all.* Half a turn is an assistant whose tool
calls have no results, or results whose calls nothing declares — precisely the
wire defect `wire()` exists to repair, manufactured on purpose at restore time.
The test asserts `wire_repairs` is empty after a restore, including the
budget-truncated one.

*The budget bound is deterministic.* A 400-turn session does not fit in a
prompt, and the obvious answer — summarise the rest — means a billed model call
the developer did not ask for, at the moment they are waiting for a window to
finish reloading. So it keeps the newest whole turns that fit in 55% of the
budget (not more: the run that follows needs room, and a context restored to the
compaction threshold would compact on its first turn, turning "continue where
you left off" into "summarise where you left off") and says in a message the
model reads how many turns it dropped and where the rest is.

*The call's arguments are carried to its result.* A `tool_result` event carries
the content and the id but not the arguments — only the intercepted path repeats
them — and the arguments are where `path` and the line range live. Without that
link every restored read claims no coverage, and the re-read intercept, which
asks the context rather than the loop since RC-1, would let the agent re-read
every file it already had one turn after being restored. This is the part I got
wrong first: the fixture passed `arguments` on the result, which real events do
not carry, and the test failed for the right reason.

What does not come back is the loop's `_State`: which searches were exhausted,
which reads were refused, how many times a gate failed. Those live in
`carry_from` and a restored session starts them empty. The loss is
one-directional and worth stating plainly — the agent may repeat a search it had
already exhausted; it will not skip work it has not done.

Re-seeding the original task is still there, as the last resort rather than the
first: no transcript, an unreadable one, or a session that never got a reply.

Tests: five in `test_regression_audit.py` and one end-to-end in
`test_loopback.py` that runs a task, throws the runtime away, builds a second
one over the same workspace and checks the conversation comes back.


### Verification against the audit's own reproductions

`audit-repros/repro_audit.py` (R1–R9) and `repro_carry.py` (R10) all report
**not reproduced**. R2 initially still fired, and it was right to: it drives
`ContextManager.compact` directly, so it bypassed the loop's `_forget_evicted`.
That is a fair criticism of where the fix sat — the loop was invalidating a
ledger it also owned. `_re_reading` now asks `context.coverage()` what the model
can still see and uses the stored ledger only for "how often has this been
asked", which is the split the audit's root cause RC-1 prescribes: the loop
queries, the context answers. Any eviction by any path is visible immediately,
and `_forget_evicted` is now belt-and-braces that keeps the persisted ledger
honest rather than the thing the refusal depends on.

**R11 is assessed, not fixed, and deliberately so.** Its reproduction condition
is "a named `finish` tool_choice appears at all", which is broader than the
defect. The defect is the *contradiction* — that turn arriving while a failing
gate in the same context says "make the edit" — and `_gate_wants_an_edit` closes
it (`test_a_failing_gate_is_never_answered_by_a_forced_finish`). What R11 still
shows is the fence ending a phase after twelve turns that wrote nothing, with
every plan target already written, which is the fence working as designed. All
three remedies `BUGS.md` prescribes for L-2 are implemented; the fourth thing the
audit *observes* — that twelve turns is a small budget for a whole-service
migration — is a measurement, not a bug fix, and raising a loop-containment bound
without one is how the bounds got where they are.
`test_the_research_fence_still_ends_a_phase_that_only_reads` pins that reading so
the next person knows it was considered.

### TC-4 — the narration re-ask is per phase · done

`state.forced` resets at `submit_plan`. It was scoped to the run, and the
argument behind that scope was always about one mode relitigating one decision —
"a Planner that has decided there is nothing to plan says so, is forced,
complies, says so again". Applying it across the phase boundary handed the
acting mode a phase with no narration recovery at all, which is where narration
costs the most: "Making the edit now" with no tool call is a turn in which
nothing was edited.

Test: `test_forced_flag_does_not_cross_phases`.

### 3.5 — features that existed and could not be reached · done

**EXT-18** Six commands had handlers and no palette entry — including
`dakcoder.stopTask`, which the status bar's own tooltip tells the developer to
run. `check-commands.mjs` checked one direction only, so it passed while the
gap was real; it checks both now, and says what to do about a command that
deliberately should not appear (contribute it with a `commandPalette` entry of
`"when": "false"`, the way the context-menu commands already do).

**EXT-16** `diagnostics.register` returns the service "so activation can call
`audit()` and `offerGateRerun()`", and the return value went straight into
`subscriptions.push`: nothing kept it, so the offer to re-run a blocked gate
stage locally had no caller. Activation keeps the reference and calls it on a
`gate` event. It declines quietly for a clean gate, a gate with no blocking
stage, and a stage this build does not recognise — the additive-only rule.

**EXT-6** `ContextTree.setSession` had exactly one caller, inside the command
that reveals the view. Opening the view the way a tree view is normally opened —
clicking it in the sidebar — showed "No session selected" for ever. It now
follows whatever the panel is showing.

`npm run verify` is green end to end: typecheck, 63 tests, bundle, credential
scan, 59/59 commands both directions, l10n (770 strings), gotools manifest.

### Test plan §1 and §4 — the pins · done

The behaviours the audit found *correct* are now held in place:
`test_summarizer_failure_falls_back`, `test_over_budget_fallback_recovers`,
`test_invalid_line_ranges`, `test_cancelled_batch_still_answers_every_call`,
`test_ordinary_git_still_runs`.

All four §4 invariants are asserted: #1 the wire invariant
(`assert_wire_is_coherent`, used by five tests), #2 the ledger invariant
(`test_the_read_ledger_only_claims_what_the_context_holds` — every span the
ledger claims is really in a live message), #3 the budget invariant
(`test_the_cut_and_the_budget_agree_on_every_message`), #4 the terminal invariant
(`test_a_terminal_session_dispatches_nothing_more`).

One deviation from TEST_PLAN §2, recorded rather than smoothed over: it expected
`read_file(start=0)` to be clamped to line 1. The router refuses it at coercion
with the rule it broke and a `dead_end` mark, which is better — a model that
asked for line 0 made a mistake worth telling it about, and silently reading
something else teaches it nothing. The test pins the real behaviour.

**Suite: 808 tests, green.** It was 704 with 6 failing.

### 3.1, 3.2, 3.3, 3.7 and part of 3.4 — the P2 sweep · done

**Leaks (3.1).** `SessionStore.on_forget` fires when a session is trimmed or
deleted, and the loopback drops that session's context, loop and any stale
approvals with it — they hold the whole message list and the whole ledger set,
and were the expensive half of a session that nothing ever released (L-12).
`ToolResult.as_dict` caps content at `MAX_EVENT_CONTENT`: the context caps its
copy at insertion, so an uncapped 400KB build log went into the in-RAM event log,
the transcript and the SSE frame at full size — three copies of something the
model never saw in full (L-26). `Calibration._history` is gone; it was a list
nothing read, growing by one float per turn (SH-7). `QuotaPolicy._settled` is a
bounded `OrderedDict` (GW-5).

**Observability (3.2).** Tool events carry `turn`, so a transcript can be grouped
without inferring the turn from the position of the last `turn_start` — which a
reconnect makes wrong. `turn_start.attempt` is the attempt *about to be made*
rather than the count of failures behind it, so the first gate is attempt 1 on
the wire as it already was in the UI (EXT-7). The webview no longer draws a gate
grid for an event with no stages, which is what `forced_tool_call`,
`tool_choice_unsupported` and `wire_repair` all are (EXT-8) — tested on the
presence of stages rather than a list of known kinds, which would break again on
the next kind added. A `QUOTA` event is emitted when a run ends: the type existed
and nothing ever emitted one, so the status bar's listener was unreachable and
the figure on screen was whatever the 60-second poll had last seen — including
after the poll stopped (EXT-15). It is transient, because it carries no data:
the gateway owns the numbers.

**Perf (3.3).** `Router.model_mutations` excludes gate-attributed mutations, so
the gate cache key stops moving every time gofmt or govalid_gen writes something
(L-29) — and a gate-attributed edit no longer resets the counter that notices a
run standing still in front of a failing gate. Total `mutations` still drives
ledger invalidation, because a gate that rewrote a file did change the world.
L-18 (directive placement) is left alone deliberately: the plan says measure
first, and nothing here has measured it.

**Residuals (3.7).** `_await_baseline` keeps the thread reference when the join
times out, so a slow baseline is waited for again rather than landing mid-run and
leaving the gates on either side of it disagreeing about what was already broken
(L-16). An open-ended read of a file whose length nothing has reported dispatches
instead of being answered by one covered line (L-23). `/v1/health` answers
liveness without a token and everything about the developer's machine only with
one (L-30). `take_baseline` runs with `-mod=readonly`, so `go build` cannot add a
checksum to `go.sum` while measuring the workspace it is about to compare against
(GT-1). The gofmt EOL restore leaves mixed-EOL files alone rather than converting
every LF line in a half-converted repository (TL-10).

**Gateway (part of 3.4).** `ModelRoute.api_key` is `repr=False` — the model key
is the one secret this gateway exists to hold, and a dataclass repr puts it in
every log line and traceback that touches a route (GW-14). `/v1/llm` reads the
body with a 16 MB ceiling, streamed, so an oversized body is refused at the point
it exceeds the limit rather than after all of it is held (GW-6).

Not done, and honestly so: GW-7 (refresh tokens are still plaintext in memory and
still lost on restart), GW-8..13, EXT-6/16/18 (dead features), L-18. They are the
rows whose failure mode is honest degradation, which is where `AUDIT.md` §12 puts
them.

### 2.10 + 2.11 + 2.12 — the rest of P1 · done

**EXT-4** `init` carries an epoch (`pid-timestamp`) and the webview resets
`lastSeq` when it changes. `seq` counts inside the extension host and the
webview's state outlives it: a window reload restarted the host at 0 while the
panel still remembered 214, so every event afterwards failed `seq > lastSeq` and
was dropped — the panel looked alive and received nothing until the new host had
produced 215 events of its own.

**EXT-9** The loopback token comes from `randomBytes(32)`. It was
`sha256(Date.now() : Math.random() : pid)`, none of which is secret:
`Math.random()` is a fast PRNG whose state is recoverable from a few outputs, the
clock is public and the pid is in `ps`. That token is the whole of the defence
the design claims against other local processes.

**EXT-10** `pythonPath`, `goPath`, `goplsPath` and `gotoolsPath` are
`"scope": "machine"`. A cloned repository's `.vscode/settings.json` chose which
binaries the extension executed, with Workspace Trust as the only gate.

**L-27** `_rendered` puts `called write_file(path=…)` into the summariser's
transcript. An assistant turn that is purely tool calls has an empty `content`,
so every edit the run made rendered as a blank line — and the histories that
summarised worst were exactly the write-heavy ones the recap matters most for.
Arguments are truncated at 200 characters with the real length named: which file
was written is the fact worth carrying, and the content is in the workspace.

**L-15** The prose from a turn that was forced to re-ask travels with the forced
reply. Its deltas had already streamed to the panel, so discarding it displayed
text the backend then dropped — and the model's own turn was absent from its
history, so it could not see that it had narrated and been asked again.

**SH-5b** `is_protected` matches case-insensitively. The primary platform's
filesystem is case-insensitive, so `dockerfile` and `GO.MOD` address exactly the
files the globs name and a write to either skipped the approval gate. On Linux
this refuses slightly more than it must, which is the safe direction for a gate
whose job is to make a human look.

**RG-1** A mutation whose path the tool only names in its *result* — `fx_wire`,
`govalid_gen` — is recorded in the undo manifest as `UNRECORDED`. It cannot be
snapshotted (the target is not known before the run), so revert still blocks; it
now blocks with the real reason rather than the generic one, and says so after a
restart too. The approval exception itself stays as designed and documented in
`_SAFE_MUTATORS`.

Tests: `test_protected_globs_match_case_insensitively` (5 spellings),
`test_an_unsnapshotted_mutation_is_recorded_as_such`,
`test_the_summariser_sees_what_the_run_did`,
`test_the_summariser_transcript_does_not_carry_a_whole_write`,
`test_a_forced_re_ask_keeps_the_prose_it_streamed`; extension typecheck and
63 tests green.

### 2.9 — something survives a restart · done

New module `journal.py`. Two files per session under `.dakcoder/sessions/<id>/`:
`events.jsonl` (append-only, one stored event per line, written inside the same
lock that assigns the id, so the file is in id order for the same reason the list
is) and `session.json` (the summary a list view needs, rewritten when it
changes). `SessionStore` restores the summaries at startup and
`Session.hydrate()` reads a transcript only when one is asked for — a daemon
starting in a workspace with a hundred finished sessions reads a hundred small
JSON files, not a hundred transcripts.

Three properties it is written for, in order: it must never fail a run (every
write is best-effort; a read-only checkout costs the transcript, not the work),
it must never slow a turn (buffered, flushed at the points the run is already
waiting on something slower — a mutation, the end of the run), and reading it
back must be cheap.

A session that was RUNNING when the process died comes back as ERROR and
resumable. Nothing is driving it, and leaving it "running" makes it unresumable,
undeletable and permanently in the way.

`SessionStore(..., persist=False)` exists for callers that should not leave
directories behind — a unit test constructing a store against the repo root, for
instance, which is how this was caught.

**DOC-1, done here** because the claims were about this code. `session.py` said
"Events are persisted before they are sent" and meant "appended to a list";
`context.py` said the recap was written to `.dakcoder/session-<id>/recap.md` and
nothing ever wrote that file; `session.py` still described revert as
restoring from git; `README.md` said the loop, the router, the gateway and the
extension were "not built yet". All four now say what the code does.

Tests: `test_a_transcript_survives_a_restart`,
`test_a_run_interrupted_by_a_restart_is_not_left_running`,
`test_revert_works_after_a_restart`,
`test_a_journal_that_cannot_write_does_not_fail_the_run`.

### 2.6 + 2.8 — paths that match, and processes that stop · done

**2.6 (L-19)** `_normalise_plan` puts every `PlanStep.file` through
`workspace.relative(workspace.resolve(...))` at `submit_plan`, which is the form
`router.touched` records — `_confine` rewrites every path argument before a
handler sees it. `./handler/user.go` and `handler\user.go` compared unequal to
the `handler/user.go` the write produced, so a step spelled either way was
"never written" for the life of the run: it refused the first `finish` and
mis-headlined the DONE summary. A path that will not resolve is kept verbatim —
it is the model's text and the developer should see what was planned.

**2.6 (L-25)** `_ReadLedger.mtime` is recorded at every read, and `carry_from`
drops the coverage of any file whose mtime moved, telling the model in a user
message that what is above is the older version. Between two messages the
developer is doing their own work — reading the diff, fixing a line — and the
carried ledger refused the re-read of a file whose contents had moved. By mtime
rather than content hash: this runs once per follow-up over every file the
conversation has read, and a stat is the cheap question; an unknown mtime on
either side keeps the entry rather than discarding the ledger.

**2.8 (TL-5)** Children start in their own process group
(`start_new_session` / `CREATE_NEW_PROCESS_GROUP`) and a timeout kills the group
(`killpg` / `taskkill /T /F`). `subprocess.run(timeout=…)` killed the direct
child, and the direct child of `go build` is a supervisor — the compiler, the
linker and the test binaries are grandchildren, and every one of them survived
every timeout while the agent reported the build as stopped.

**2.8 (TL-6)** Output is drained on a reader thread into a buffer bounded at
`MAX_CAPTURE_BYTES`, with `stderr=STDOUT` so the kernel interleaves the two in
the order they were produced. Past the cap the bytes are read and discarded
rather than stored — and read rather than ignored, because a child whose pipe
fills blocks forever and would hang the timeout too. The process is not killed
for being verbose: that would throw away the test results the developer asked
for, and the timeout is the bound on how long it may go on.

**2.8 (TL-7)** `run_terminal` refuses the destructive `git` subcommands by name
(`push`, `reset --hard`, `clean`, `rebase`, `filter-branch`, `gc`,
`reflog expire`, and `checkout`/`restore` unless they are creating a branch).
`git_ops` documents "no push, no reset --hard, no rebase … a property of the tool
rather than a policy in the prompt" — it was a property of *that* tool and of
nothing else, because `git` is on the allow-list. What is refused is what the
reflog cannot recover or what other people can see.

**2.8 (TL-8)** A binary named by path is refused. The allow-list read
`Path(argv[0]).name`, so `./go` and `subdir/go` passed while naming a binary in
the repository the model can write to.

Tests: `test_plan_paths_normalised` (both spellings),
`test_an_unresolvable_plan_path_is_kept_verbatim`,
`test_a_follow_up_re_reads_a_file_the_developer_changed`,
`test_subprocess_timeout_kills_process_tree` (a real grandchild, POSIX),
`test_capture_is_bounded`, `test_destructive_git_is_refused` (5 spellings),
`test_ordinary_git_still_runs`, `test_a_binary_named_by_path_is_refused`.

Also fixed a latent race in the `settle` test helper: it waited on
`session.running`, which the worker sets while the last events are still queued
as `call_soon_threadsafe` callbacks, so a transcript could be read without its
final `assistant` and `end`. It waits for `end` now.

### 2.1–2.5, 2.7 — the run stops contradicting itself · done

**2.1 (L-4)** `Recap.merge` folds the previous recap into the new one, oldest
first, bounded at `MAX_RECAP_ITEMS` per field, with `turns` spanning both. A
compaction replaces the pinned recap and the evicted set handed to the summariser
never contains the previous one, so the first compaction's `do_not_retry` — the
field the class's own docstring calls the reason it exists — vanished at the
second, and long runs are exactly the runs that compact twice.

**2.2 (L-5)** `carry_from` adopts the previous loop's Router. The Router was
rebuilt per message while the ledgers were carried, and those two facts destroyed
each other: a carried `mutations_seen` of 3 met a Router at 0, the follow-up's
first batch read that as "the world changed", and every carried ledger was wiped
— by the line whose comment says it prevents exactly this. `mutations_seen` is
now read off the shared Router, so the two agree by construction. It also gives
the follow-up a real change set: `_unwritten_targets` was comparing a carried
plan against an empty one, and the gate was scoping itself to nothing.

**2.3 (L-6)** `_whole_turn_cut` steps *back* to the declaring assistant when the
forward walk cannot clear an orphan. Two correct rules — keep a call with its
results, never evict the last message — met and produced a retained set that was
one orphaned `role: tool` message. The token overshoot is the lesser cost.

**2.4 (L-13)** `_State.truncated_turns`, incremented in `_answer_truncated` and
cleared by any complete reply; at `MAX_TRUNCATED_TURNS` (3) the run ends naming
the output limit and the mode. The per-turn handling was already careful — every
declared call answered, the cause named accurately — but nothing counted the
repetition, so a model that always overran spent all 40 (or 400) turns doing it
and ended EXHAUSTED without truncation ever being mentioned.

**2.5 (L-14, L-17)** `_State.answer_because` carries *why* a turn is being made
to answer, so a refused terminal call is no longer told "that call has already
been answered and asking it again returns the same thing" — false on every
clause, and it points at the repetition when the arguments are the problem. And
`partial_results` records when a cached result was cut at `CACHED_RESULT_CHARS`,
so the replay says "the first 6,000 characters of 41,204" instead of presenting
the head as the whole answer.

**2.7 (RT-1)** `resume` is the follow-up path: same context, ledgers carried,
Router carried, fresh turn budget, with the note as the message. It used to build
a run on a *fresh* context seeded with `task + "The previous attempt ended: …"`
while the EXHAUSTED message promised "Resume continues on this same transcript" —
so a run that exhausted its turns at the point of writing the last file resumed
by re-reading the service from scratch. When the daemon holds no context (after a
restart) it falls back to re-seeding the task, and the message says so rather
than pretending. The EXHAUSTED copy now describes what happens.

Tests: `test_recap_accumulates_across_compactions`, `test_recap_merge_is_bounded`,
`test_follow_up_carry_survives_first_batch`,
`test_a_follow_up_sees_what_the_session_already_wrote`,
`test_compaction_never_retains_orphaned_result`,
`test_repeated_truncation_has_hard_stop`,
`test_a_complete_reply_clears_the_truncation_streak`,
`test_refused_finish_message_is_accurate`,
`test_repeated_result_replay_marks_truncation`,
`test_resume_semantics_match_message`.

### 1.12 — the transport stops lying about how a stream ended · done

**SH-1** A stream that reaches EOF with no `[DONE]` and no `finish_reason` now
raises a retryable `UpstreamError` instead of returning the partial content as a
success with `truncated == False` and zero usage. The zero usage was its own
harm: it fed the calibration a free turn. A stream that reported a
`finish_reason` has said what it needed to and is still accepted without
`[DONE]`.

**SH-2** `RemoteProtocolError` and `ReadError` join the retryable transport set.
They are what "the upstream died mid-SSE" actually raises — a vLLM worker
restarting, a proxy dropping a long connection — so the retry machinery was
missing its main customer and failed on attempt one.

**SH-3** Tool-call slots key on `index` when the endpoint sends one and on the
call `id` when it does not. Defaulting a missing index to 0 folded every parallel
call into one slot: two calls arrived as one with both argument strings
concatenated, which the loop reported to the model as malformed arguments for a
reply that was fine. A slot that accumulated arguments and never a name is now a
loud failure rather than a silent drop — it is a call the model made that the
client cannot deliver, and dropping it made the turn look like it had asked for
less than it did.

Risk noted in the plan: a benign server that never sends `[DONE]` *and* never
sends a `finish_reason` would now error. The retry (`502`,
`kind="incomplete_stream"`) gives it a second chance before the run sees
anything, which is the guard the plan asked for.

Tests: six in `apps/shared/tests/test_llm.py`, including the indexed path pinned
so the SH-3 fix cannot regress the normal case.

### 1.11 — the gateway · done

**GW-1** `recheck` is on the `IdentityProvider` protocol and `GitLabIdentity`
implements it. It was found with `getattr`, no production adapter had it, and the
only implementation in the tree was the test fake — so `/v1/auth/refresh`
answered 501 in production and 200 in CI, every session died at the
fifteen-minute access-token TTL, and the developer went through a full browser
OAuth flow four times an hour. The recheck uses the *user's own* GitLab token,
captured at sign-in and carried across rotations on the refresh record: the
alternative is an administrative GitLab token on the gateway, whose leak would be
every account rather than one. The identity is verified, not assumed — a token
that now answers for a different account is refused.

**GW-2** A client that goes away before the first byte arrives as
`CancelledError`, a `BaseException`: `except Exception` never saw it and the
`finally` had nothing to settle, so a 40,000-token reservation sat against the
developer's hourly quota until the window rolled. The `finally` now schedules a
release (a sibling task, for the same reason settlement is one — this block can
run under cancellation, where the first `await` raises immediately).

**GW-3** The hot path validates instead of trusting: a malformed body, a
non-numeric or negative `X-Estimated-Tokens`, an unknown `X-Lane` are 400s with a
reason. Each used to raise straight out of the handler as a 500, which tells an
authenticated caller nothing and lets a header typo flood the error budget. A
negative estimate would have *credited* the caller's window. Reservations are
clamped at `MAX_ESTIMATED_TOKENS`; `X-Turn` is telemetry, so a bad one is dropped
rather than refused.

**GW-4** `remember` uses `SET … NX GET`, so the winner is decided server-side in
one round trip. GET-then-SET was a check-then-act across a network hop: two
concurrent deliveries of one key both read `None`, both wrote, and both were told
"this is new" — the same request dispatched and charged twice, which is the one
thing an idempotency key exists to prevent. A redis-py too old for `get=True`
falls back to the two-step with the loss of guarantee stated in the code rather
than assumed away.

Tests: `apps/gateway/tests/test_regression_audit.py` — 10 tests, including a
pre-first-byte disconnect against a slow upstream (verified red before the fix:
`0 -> 120000` tokens reserved and never returned) and a concurrent
`asyncio.gather` on one idempotency key.

### 1.10 — an approval means what the developer thinks it means · done

Four defects, one story: a reviewer taking their time had their review turned
into a rejection, and was then told it had been recorded as one.

**EXT-1** `_await_decision` polls (5s) and re-reads `deadline_in()` each time, so
`/extend` reaches the thread that is counting. The single
`wait(timeout=deadline_in())` computed its timeout before the extension existed —
the counter went up, the UI showed minutes remaining, and the run rejected at the
original deadline anyway.

**EXT-2** The deadline is one number now instead of two unrelated ones.
`APPROVAL_TIMEOUT` reads `DAKCODER_APPROVAL_TIMEOUT` (0 = no timeout,
`deadline_in()` returns `math.inf`); the extension passes
`dakcoder.approvalTimeoutSeconds` to the runtime as that variable; and
`asApproval` stops discarding the server's `seconds_left`, `extensions` and
`session_id`, which `protocol.ts` has declared all along (that dropped
`session_id` was EXT-17 as well). `deadlineFor()` prefers the server's countdown
and falls back to the local setting; `reconcile` re-anchors a held approval's
deadline on what the runtime says is left. The setting's text no longer promises
something the backend contradicted.

**EXT-3** `openSession` fetches the transcript for a *running* session too.
Hydrating from a tree summary left the event cursor at 0, so the stream replayed
the whole transcript through the live path and every stored `tool_pending` in it
became a card with Accept and Reject on it — for approvals already answered —
followed five seconds later by "recorded as a rejection" for each. A transcript
is history whatever the session's status.

**EXT-5** `ApprovalService.discover()` runs a reconcile pass and then starts
polling, and is called on every session attach. `ensurePolling` returned early
when nothing was pending, so the reconcile's documented job — finding approvals
raised before this window was listening — was unreachable, and a reloaded or
second window left the run blocked until its deadline.

**L-22** (picked up here because it is the same code): `POST /v1/approvals/{id}`
returns 410 when the approval has already timed out, instead of answering
"accept" for a call the run recorded as rejected.

Deviation from the plan: the reconcile pass runs on attach but *not* at
activation — activation has a 50 ms budget and the runtime is not spawned yet, so
there is nothing to poll. Every path that opens or starts a session discovers.

Tests: `test_extending_an_approval_extends_the_wait`,
`test_a_decision_after_the_timeout_is_refused`,
`test_a_zero_timeout_means_no_deadline`, and
`does not raise an answered approval replayed from a running transcript`
(extension `state.test.ts`, 63 pass).

### 1.8 — the acting phase can act again · done

Three changes, all of them about the same contradiction: a failing gate report
in the transcript saying *"Make the edit, or say plainly what is stopping you"*
while the same turn's `tool_choice` named `finish` and forbade every other tool.

1. A batch that mutates resets `research_turns`. Writing is not research; the
   fence exists to stop a phase spent reading and never deciding, and a turn
   that changed a file has decided. Counting write turns capped the acting phase
   at ~12 tool turns on a product that advertises `dakcoder.maxTurns` to 400.
2. `_gate_wants_an_edit()` — a failing gate with attempts left — makes the fence
   force `"required"` instead of the phase's terminal tool, so a tool call is
   still mandatory but the model may choose `patch_file`. The message names the
   blocking stage and says the gate is a function of the files.
3. `_gate_failed` resets `research_turns`: a gate verdict is new information and
   the work it asks for is fresh work. The run is still bounded, by
   `MAX_GATE_FAILURES` and by `_gate_stalled`, both of which count turns that
   changed nothing — so loosening this bound did not remove a bound.

Tests: `test_acting_phase_not_locked_out_after_twelve_turns`,
`test_a_failing_gate_is_never_answered_by_a_forced_finish`,
`test_a_gate_verdict_reopens_the_fix_window`.

### 1.9 — a steering message cannot vanish · done

`Session.steer` now answers from inside the lock and returns whether the
correction was actually queued; `close_steer` atomically stops taking them and
hands back whatever was never drained. The endpoint's `session.running` check
and its append used to be two separate observations of a value the worker thread
changes, so a message typed while the last turn was in flight went onto a queue
nothing would read again — never delivered, never recorded, never a follow-up.

Two paths now cover the window. `message_session` falls through to `follow_up`
when the queue refuses, and the worker's `finally` calls `_rescue_steers`, which
turns anything left in the queue into a follow-up run on the same context. The
ordering is what makes this airtight: the status becomes terminal (in
`session.finish`, or in the crash handler) strictly before the queue closes, so
a caller that sees `running` is talking to a queue that is still open. The
impossible remainder returns 409 "send it again" rather than a 500.

Tests: `test_a_steer_queued_after_the_run_ends_is_refused_not_swallowed`,
`test_steer_never_lost_on_finish_race`, `test_a_leftover_steer_starts_a_follow_up`.

### 1.5 + 1.6 + 1.7 — one answer to "what has the model seen" · done

The prior audit's central finding, in three parts. All three are the same shape:
two components hold the same fact and nothing tells one when the other changes.

**1.5 (L-8)** `_apply_cap` now returns *what survived* alongside the capped
text, and `append_tool_result` stores that on the message's `line_range` — so
`Message.line_range` means "the lines in this message" rather than "the lines
the tool returned". Everything downstream reads it: the slice ledger, the loop's
read ledger, the re-read intercept. Before this, `read_file` on an 8,000-line
file put ~2,650 lines in context and recorded 1-8,000 in the ledger, so the
elision marker's "re-read with a narrower line range" and the intercept's "lines
6000-6500 are already in context above" were both delivered and neither could be
obeyed. The marker now also names the surviving span, because "read a narrower
range" is not actionable unless the model can tell which range is missing.

A cap that keeps no content line at all reports `None`, which callers must read
as *no* coverage — `None` would otherwise mean "whole file" to `_contains` and
stub out every earlier read of that file.

**1.6 (L-10)** `ContextManager.compact` records an `Eviction` (paths, tool call
ids, messages, tokens) and `ContextManager.coverage()` answers "which lines of
which files are in the working set right now". `AgentLoop._forget_evicted`
rebuilds the read ledger from that coverage — so a file with surviving reads
keeps them and only the evicted spans become askable again — and clears
`last_results` / `echoes` / `truncated_at`, which exist to say "you already have
this above" and cannot say it truthfully after an eviction. `seen_calls` and
`dead_ends` survive on purpose: a dead end is a fact about the world, not about
the transcript.

The emergency compaction in `_complete` now routes through `_compact` too, which
was L-24 (invisible to the thrash detector) and would otherwise have become the
one path that evicts without invalidating.

**1.7 (L-3)** `_message_cost` is the only answer to "what does this message
cost", used by `usage()`, `_retention_cut` and `novel_tokens`. Twenty
`write_file` calls carrying 40KB of arguments each are 200k tokens to the
compaction trigger; they used to be *zero* to the compaction cut, so compaction
fired every turn, evicted nothing, and the run died as NO_PROGRESS blaming the
working set or as ERROR "context cannot be reduced below budget". Write-heavy
runs are the product's core loop.

Tests: `test_capped_read_then_tail_read_dispatches`,
`test_the_elision_marker_names_what_survived`,
`test_compaction_invalidates_read_ledger`,
`test_compaction_keeps_coverage_that_survived`,
`test_write_heavy_compaction_frees_tokens`,
`test_the_cut_and_the_budget_agree_on_every_message` (TEST_PLAN invariant #3).

### 1.3 — writes that cannot empty a file · done

`fs._write_text` replaces both `Path.write_text` call sites. It encodes with the
same `errors="surrogateescape"` the read uses, *then* writes — the old order
opened (and truncated) the file before the strict-UTF-8 encoder raised, so a
repository file with one stray byte past the 8KB binary probe was left at zero
bytes with a generic failure and no mutation record, which meant neither the
gate nor `revert` ever learned it had been emptied (BUG TL-1). The write goes to
a sibling `.dakcoder-tmp` and is renamed, so a crash or a full disk leaves the
old file rather than half of a new one (TL-2), and the original's mode is copied
onto the replacement so a patched shell script keeps its executable bit.

`os.replace` failing (Windows, file held open by an editor) falls back to a
direct `write_bytes` of the already-encoded data — no longer the
truncate-then-raise path.

Tests: `test_patch_file_non_utf8_preserves_content` (red before: *"UnicodeEncode
Error: surrogates not allowed"*, file emptied), plus atomicity, the executable
bit, and no temp file left behind.

### 1.4 — revert restores what was there, not what HEAD has · done

New module `undo.py`. The router snapshots each path's pre-image the first time
a *mutating* tool touches it (`spec.mutates`, before the handler runs,
first-write-wins), into `.dakcoder/sessions/<id>/undo/` with a JSON manifest.
`SessionStore.plan_revert` now reads that manifest instead of asking HEAD:

| pre-run state | revert does |
|---|---|
| a file was there | restore those bytes |
| nothing was there | delete — the run created it |
| too large / unreadable / **not recorded** | **blocked**, with the reason said out loud |

That closes both halves of L-11: a developer's uncommitted edit to a file the
agent later touched is no longer reset to HEAD, and a developer's *untracked*
file the agent merely modified is no longer deleted (HEAD not having it never
meant the run created it). It also makes revert work outside a git repository at
all, which is why `test_revert_outside_a_git_repository_is_blocked_not_attempted`
became `test_revert_works_outside_a_git_repository` — the snapshot is a stronger
source of truth than HEAD, so git's absence is no longer a reason to give up. A
path with *no* snapshot is refused rather than guessed at
(`test_revert_blocks_a_path_it_has_no_snapshot_for`).

`revert` now returns what it actually did — restored, deleted, and anything that
failed — rather than echoing the plan it was handed.

The manifest is on disk, so a daemon restart between the run and the revert no
longer silently turns "restore what was there" back into "reset to HEAD" (a
first slice of L-7). `.dakcoder/` writes a `.gitignore` containing `*` on first
use, so the runtime state does not appear in the developer's `git status`.

Tests: `test_revert_restores_pre_run_developer_changes`,
`test_revert_keeps_a_developers_untracked_file`,
`test_revert_deletes_only_what_the_run_created`,
`test_the_snapshot_is_the_first_state_not_the_latest`.

### 1.1 + 1.2 — the tool-call invariant · done

**1.1** `_answer_unrun` is now the single place a batch's abandoned calls are
answered, and all three paths that abandon calls go through it: cancellation
mid-batch (which already did this), a terminal tool that ended the phase with
calls behind it, and the forced-terminal cap. The two that returned without
answering were BUG L-1 — and L-1 is not a cosmetic gap, because the message list
is append-only: one orphan sits in the working set for every later turn of the
session and every follow-up built on the same context, and a strict
OpenAI-compatible endpoint rejects each of them.

**1.2** `ContextManager.wire()` now repairs the invariant on the one path every
request passes through. A declared call with no result gets a synthesised
"was not run" result placed at the end of its assistant's block (not immediately
after the assistant — a batch's results are not contiguous, because a
retrieval-overlap note is a `role: user` message appended between two results of
the same batch). A result whose call nothing declares becomes a user message
carrying the same text: dropping it would edit what the model was told, keeping
it as `role: tool` would be malformed. `context.wire_repairs` records what was
repaired and the loop turns it into an ERROR event — a silent recovery for an
invariant violation is how the violation survives to the next release.

Deviation from the plan: none, but note the repair is deliberately *not* the
fix. Both regression tests assert `wire_repairs` is empty, so if the loop stops
answering its own calls the tests fail even though the request would still be
accepted.

Tests: `test_terminal_tool_in_batch_answers_all_calls`,
`test_finish_in_batch_answers_all_calls`,
`test_cancelled_batch_still_answers_every_call` (pins the path that was already
right), `test_wire_repairs_an_orphaned_call_and_says_so`,
`test_wire_keeps_an_orphaned_result_as_prose`. Verified red before the fix and
green after.

Also: `ScriptedClient` and friends moved to `apps/agent/tests/scripted.py` so the
regression suite drives the same stub as the behavioural one (TEST_PLAN's
"promoted to a shared fixture"), and `calls()` now issues process-unique tool
ids — restarting at zero every turn made two batches in one run share ids, which
reads as "answered twice" to any check written against the transcript.

### 0.1 — green suite on Linux · done

`Workspace.resolve` now maps `\` to `/` before the string reaches `Path`
(`_separators`). On POSIX `Path("handler\user.go")` is one filename with a
backslash in it, so a Windows-shaped path from the model created a stray file
the developer cannot open, and the read ledger, `router.touched` and the gate
scope each disagreed about which file had been touched. The three tests that
already asserted this behaviour pass; the trade (a POSIX file whose name really
contains a backslash is no longer addressable) is the one the rest of the system
already makes — `relative()` returns POSIX form, the protected globs are POSIX,
the wire is POSIX.

The three gofmt tests now carry `@needs_gofmt`, which skips when `gofmt` is
absent and raises under `DAKCODER_REQUIRE_INTEGRATION` — the same switch
`test_gotools_bridge.py` uses, so CI still fails on a missing toolchain instead
of going quietly green. `test_gofmt_leaves_an_lf_file_as_lf` was marked too: it
passed without the binary only because its assertion is vacuous when nothing is
formatted.

Suite: **green** (was 6 failed).

---

# Fixing dakcoder — implementation log

Working against `agent-failure-report.md` (commit `2c197a7`). Track A of §10 in
full, plus every individually-actionable defect from the L / E / T tables.

Status key: `[ ]` not started · `[~]` in progress · `[x]` done

---

## Phase 1 — Transport and tools (§6, T1–T12)

- [x] T2  mid-stream gateway `event: error` must raise, not become an empty answer
- [x] T3  `EmptyCompletionError` recovered whether or not thinking was on
- [x] T10 `required` accepts `""`; boolean parameters get a real boolean type
- [x] T11 token calibration counts tool schemas and `tool_calls` arguments
- [x] T12 close the proxy's client, close `LLMClient`, stop swallowing errors
- [x] T7  a missing `go` binary is reported as a missing toolchain, not a missing file
- [x] T6  `gotools` discovery finds the platform-suffixed dev binaries
- [x] T1  `_lint_is_clean` reads `meta`, so the contract lint can actually fail
- [x] T4  the JWT is refreshed rather than fixed at daemon spawn
- [x] T5  the gateway degrades instead of failing closed on its quota store

## Phase 2 — The gate (§3, Track A #1)

- [x] G1 never run the gate on an empty change set
- [x] G2 `go_test` advisory unless it can actually run; scoped to touched packages
- [x] G3 baseline `go_build`, `go_vet`, `go mod tidy`; report only *new* findings
- [x] G4 `go mod tidy` never mutates `go.mod` at the gate

## Phase 3 — Intent decided before the first turn (§2, Track A #2)

- [x] I1 explicit Ask / Agent toggle on the wire; `auto` classifies once, up front
- [x] I2 delete `_ASKS_TO_BE_TOLD`, `_COMMAND_LEAD`, `_WORK_VERB`, `_WORK_OBJECT`,
        `_CONJOINED_NOUNS`, `_SAYS_GO`, `_is_explanation`, `_is_read_only_task`
- [x] I3 a question runs one read-only loop and stops when the model stops calling tools

## Phase 4 — The plan is a tool call (Track A #3)

- [x] P1 `submit_plan(steps)` and `ask_developer(questions)`
- [x] P2 delete `_STEP`, `_count_steps`, `_PLAN_EDITS`, `_ACCEPTS`,
        `_asks_the_developer`, `_refuses_to_plan`, `_restated_the_plan`

## Phase 5 — Force the tool call (Track A #4)

- [x] F1 `tool_choice` / `response_format` / `parallel_tool_calls` on the client
- [x] F2 re-ask with `tool_choice: "required"` after a narration turn
- [x] F3 delete `_narrating`, `MAX_IDLE_EXECUTING`, `EXECUTING_RESEARCH_*`, `PLANNER_RESEARCH_*`

## Phase 6 — One executing mode (Track A #5)

- [x] M1 collapse Coder / Scaffolder / Verifier / Debugger into `agent`
- [x] M2 delete the ladder: `attempts`, `cycles`, `blocked_stage`, `route_mutations`
- [x] M3 the overlay stack cannot stack: one mode message, replaced in place

## Phase 7 — Stop fabricating and stop rewriting (Track A #6)

- [x] N1 every nudge is a `role: user` message; no `role: tool` without a `tool_call_id`
- [x] N2 stop deleting history (`collapse`, `dup_results`, `discard`)

## Phase 8 — The extension (§5, Track A #7)

- [x] E1 `end` without `finish` is finished
- [x] E2 every failure is rendered into the panel
- [x] E3 the four dead slash commands
- [x] E4 a dead daemon stops retrying
- [x] E5 request timeouts and an in-flight guard
- [x] E6 no phantom approvals on a finished session

## Phase 9 — Rebuild

- [x] R1 rebuild the runtime wheel
- [x] R2 rebuild the extension bundle and the `.vsix`

---

## Log

### Phase 1 — done

**T2** `_consume_stream` now raises `UpstreamError` on a `data:` frame carrying
`error`. The gateway sends exactly that when it fails after its headers are out;
the client folded it into nothing and returned `ChatResult("")`, which `_advance`
read as "no plan was needed" and ended the run `DONE` with an empty panel. The
two timeouts that made it common are also aligned: the client now gives up at
540 s against the gateway's 600 s, so the client is the side that times out and
gets a clean retry instead of an in-band error frame.

**T3** `EmptyCompletionError` is recovered whether or not *we* asked for
thinking. The old guard re-raised when `enable_thinking` was False — but that is
the cause, not the trigger: if the endpoint ignores `chat_template_kwargs`,
reasoning runs in a thinking-off mode, eats the 2,048-token Verifier budget, and
returns `content: null`. That reached `_turn`'s catch-all and ended the run
`ERROR`. The retry restates thinking-off and lifts the budget to 6,144.

**T4** The JWT is read per request through a `credential` callable instead of
being baked into the client's default headers at construction. `POST
/v1/credential` lets the extension push a fresh one, and `serve.py` now builds
**one** client for the process rather than one per run (which also fixes the
per-run handshake and the client nobody closed).

**T5** The gateway degrades for a bounded window instead of failing closed
instantly. `DEGRADE_SECONDS` (default 60, `DAKCODER_QUOTA_DEGRADE_SECONDS`, 0
restores the old behaviour) serves through a Redis blip and marks every such
reservation `degraded=True` so the audit trail records that those turns went
unmetered. A real outage still stops the service within a minute.

**T6** `_find_binary` looked for `gotools` / `gotools.exe`, neither of which
exists in this repository — the dev build is `gotools-dev.exe` and the shipped
one is `gotools-win32-x64.exe`. It now composes the platform-suffixed name,
walks up for a real `gotools/` or `bin/` directory instead of the hard-coded
`parents[5]` that lands in site-packages, and flags a missing binary so the
"sidecar is not installed" message — previously unreachable, because that branch
only fires on `FileNotFoundError` — actually reaches the model. Verified: it now
resolves `gotools/gotools-dev.exe` from the checkout.

**T7** `commands.run` raises `MissingToolchain` (in `router.py`, to avoid the
import cycle) instead of a bare `FileNotFoundError`, so a missing `go` is
reported as a missing toolchain rather than as "go does not exist. The closest
files that do: …".

**T1** `_lint_is_clean` reads the counts from `result.meta`, which `_report` has
always put there, instead of `json.loads`-ing a body that has been rendered
prose since `_render_lint` landed. The blocking contract-lint stage could
previously only fail on a sidecar crash — the product's headline promise was
inert at the gate.

**T10** `required` no longer treats `""` as missing, so `patch_file(new="")` — a
deletion — is possible. `_coerce_one` understands `"boolean"`, and `git_diff.staged`
is typed as one instead of a string whose description asked for the word "true".

**T11** `observe_usage` counted message content against a `prompt_tokens` that
includes tool schemas and `tool_calls` arguments, so the ratio was dragged to its
floor and every estimate ran high — over-reserving against the quota the panel
then mis-reported. Both sides now describe the same prompt.

**T12** The proxy builds its `httpx.AsyncClient` once and closes it in the
lifespan hook; `LLMClient` is per-process and closed at shutdown.

### Phases 2-7 - done

**G1 - the gate never runs on an empty change set.** `_verify` returns
immediately when `router.mutations == 0`. This is the invariant the report asks
for and could not find in the code: *a run that wrote nothing cannot fail*. The
gate used to run whenever an acting mode ended a turn without a tool call,
including the turn where it said "there is nothing to do here" - so an
explanation question ran a 70-second gate on an untouched workspace, adopted a
pre-existing `go_vet` failure as its own, and had `go mod tidy` rewrite `go.mod`
on the way past. That is the whole of transcript 1's "32 turns - 1 file: go.mod".

**G2 - `go_test` is scoped and cannot block on the machine.** The stage now runs
only the packages this run changed (`GateContext.touched_packages`), and
downgrades itself to advisory when those tests import testcontainers/dockertest
and no container runtime is on PATH. A test that cannot run has not failed; it
has not been asked. `go_vet` is scoped the same way. `commands._patterns` splits
a multi-package pattern into separate argv entries so the scoping actually
reaches the toolchain.

**G3 - `go_build`, `go_vet`, `go_test` and `go mod tidy` are baselined.**
`gate.take_baseline` records what was already broken before the run touched
anything, keyed on path plus message so a line number moving does not lose the
match. A blocking stage whose every finding was already present is reported as
advisory instead of failing the change. The baseline runs on a background thread
at run start - `go vet` alone is ~30s and the first gate is many turns away - and
`_verify` joins it before reading. Only a run that may write takes one.

**G4 - `go mod tidy` checks without writing.** The gate passes a gate-only
`check` parameter; `go_mod` snapshots `go.mod` and `go.sum` as bytes, runs tidy,
compares, and puts them back. The verdict is identical and the repository is
untouched. A gate that edits the repository to find out whether the repository
needs editing is not a gate.

**I1/I2/I3 - intent is decided before the first turn.** New `Intent` enum
(`auto` / `ask` / `agent`) on the wire, with every retired mode name coerced onto
it. `auto` runs one `role="fast"` call with a two-key JSON schema and ~64 output
tokens, and is given the conversation as well as the message so a follow-up "go"
classifies against what was just described. It falls back to ASK, because the
asymmetry is real: a wrong "question" costs the developer one word, a wrong
"change" costs unrequested edits found later in a diff. Deleted with the guessing:
`_ASKS_TO_BE_TOLD`, `_ASKS_FOR_WORK`, `_ASKS_FOR_WORK_LOOSE`, `_COMMAND_LEAD`,
`_WORK_VERB`, `_WORK_OBJECT`, `_CONJOINED_NOUNS`, `_INSTRUCTION_LEAD`,
`_GO_AHEAD`, `_SAYS_GO`, `_is_explanation`, `_is_read_only_task`,
`_asks_for_work`, `_one_asks_for_work`. A question now runs one read-only loop
and stops when the model stops calling tools.

**P1/P2 - the plan is a tool call.** `submit_plan(steps[{file,action,accepts}])`
and `ask_developer(questions)`, in `tools/control.py`, offered only in PLANNER.
The router gained array and nested-object coercion so the schema is enforced
rather than hoped for. Deleted: `_STEP`, `_count_steps`, `_PLAN_EDITS`,
`_PLAN_PATH`, `_STEP_START`, `_ACCEPTS`, `_REFUSES`, `_asks_the_developer`,
`_refuses_to_plan`, `_restated_the_plan`, `_plan_targets`, `_is_scaffold_plan`,
`_unstarted_work`. `_unwritten_targets` now reads the typed `file` field the
model filled in instead of matching path-shaped tokens in prose.

**F1/F2/F3 - the tool call is forced, not counted.** `tool_choice`,
`response_format` and `parallel_tool_calls` are now sent by `LLMClient`
(previously zero uses anywhere in `apps/`). A PLANNER turn with no tool call, and
an AGENT turn with no tool call while a failing gate stands unedited, are
re-asked once with `tool_choice: "required"`. Deleted: `_narrating`,
`MAX_IDLE_EXECUTING`, `EXECUTING_RESEARCH_NUDGE/LIMIT`,
`PLANNER_RESEARCH_NUDGE/LIMIT`, `_LOOKUP_TOOLS`, `_repeating`, `said`, `echoes`,
`MODE_ECHO_LIMIT`, `NO_PROGRESS_REPEATS`.

**M1/M2/M3 - one executing mode.** Five modes became three: `ask`, `planner`,
`agent`. Coder, Scaffolder, Verifier and Debugger are one `agent` that holds
every tool - including `go_vet` and `go_test`, which the Coder was judged by and
could not run. The escalation ladder is gone (`_route_failure`, `attempts`,
`cycles`, `blocked_stage`, `route_mutations`, `MAX_ATTEMPTS`,
`MAX_DEBUG_CYCLES`); a failing gate comes back to the same mode as an ordinary
user message, bounded by `MAX_GATE_FAILURES` failing gates with nothing edited
between them. `MAX_MODE_MESSAGES` is 1, so the pinned head carries the
instruction in force and nothing else - no stack, and no "this replaces the
above" preamble arguing with four stale overlays.

**N1 - nothing is fabricated.** Every nudge is `append_user`. There is no
`append_tool_result` call left without a real `tool_call_id`, where there were 17.

**N2 - nothing is deleted.** `collapse`, `dup_results` and `intercepts` are gone
and `context.discard` is no longer called. `ContextManager.SUPERSEDE_SLICES` is
off: the slice ledger was written for a 32,768-token budget and at 245,760 it
only rewrites messages the model has already been shown.

`_State` is 20 fields, down from 34. `loop.py` is 1,644 lines, down from 2,749.

Smoke-tested end to end against a scripted client: a question answers in 2 turns
with no gate and no write tools offered; a change plans, patches and gates; an
acting turn that wrote nothing does not gate; a narrating planner is re-asked
with `tool_choice: required` and then submits.

### Phase 8 - done

**E1 - `end` is terminal.** `session-state.ts` treated `end` as a no-op and
cleared `running` only on `finish`; a crashed run emits `error` then `end` and
never a `finish`, so any backend exception froze the panel on "Working..."
forever and swallowed the next message as a correction against a session that
had stopped. `end.outcome` - which carries the answer - was ignored outright. It
is now read, approvals and in-flight rows are cleared, and the follower detaches.
The server was fixed at the same end: a crashed run emits a real `finish` before
its `end`, so a client should not have to reconstruct the outcome from a missing
event.

**E2 - every failure reaches the panel.** `ready()` had four paths that returned
false with nothing on screen but a toast or a log line, and `chatView.setOffline`
was never called from it. All four now render. `reportRunError` takes the panel
and notes into the transcript as well as toasting.

**E3 - the dead slash commands.** `/service`, `/migrate`, `/debug` and `/test`
dispatched to command ids that were never registered, and `executeCommand`'s
rejection was discarded - so they did nothing, silently, and two of them are
among the four suggestions an empty panel offers. `/migrate` and `/debug` were
typos for `migrateHandler` and `debugDiagnostic` and are corrected; `/service`
and `/test` are things the agent does as an ordinary request and now go to it.
`/explain` opened a *rule document*; it asks the model. `/rule` keeps the lookup.
Anything unrecognised is submitted rather than dropped, and a command that fails
says so.

**E4 - a dead daemon stops retrying.** `isPermanent` only recognises an
`HttpError`, and a runtime that has exited answers with a `TypeError` from
`fetch`, so the event stream reconnected forever at 15-second intervals behind a
spinner nothing could resolve. `MAX_RECONNECT_ATTEMPTS` (12, about two minutes)
ends it with a message that names the cause. The runtime also logs a child exit
that happens after the port was announced, which it previously returned on in
silence.

**E5 - timeouts and an in-flight guard.** `Rest.request` had no timeout at all;
it now bounds every ordinary call (the event stream stays exempt) and
`dakcoder.requestTimeoutSeconds` - declared in package.json and read by nobody -
is what sets it. `submit` holds a guard for the length of its round trip, so a
second Enter during the very first (venv creation, offline pip install) no longer
starts a second conversation whose `showSession` wipes the first.

**E6 - no phantom approvals.** A `tool_pending` replayed from a finished run is
marked `historical` by the host and drawn without buttons, so opening an old
session no longer fills the panel with live Accept/Reject pairs that do nothing
and then toasts "released by the runtime... recorded as a rejection" for each of
them. `DAKCODER_MODE`, set in the child environment and read by nobody, is gone,
as are the three settings nothing read (`runtimeIdleTimeoutMinutes`,
`showTokenMeter`, `verbose`).

**Also:** `dakcoder.defaultMode` is now `auto` / `ask` / `agent` and maps to the
`intent` field. `modeFor` used to map "multi" - "let the agent choose" - onto
`planner`, which is also the backend default, so the server could not tell those
two apart and every message entered the mode whose instruction is "emit numbered
steps". That single line is the head of the causal chain in section 2 of the
report. `RuntimeClient.setCredential` pushes a fresh JWT on every task.

Typecheck clean; `check:commands` (53/53 registered), `check:l10n`,
`check:credentials` and `check:gotools` all pass.

### The test suite

Not a phase in the report, but the report's §8 is emphatic that the suite could
not fail — "776 green tests drive a `ScriptedClient`… It has never once been
failed by the model, the gate on a real repository, or the classifier on a
phrasing nobody had thought of" — and a suite that will not even *collect* is
worse than a shallow one, so it had to be dealt with.

**637 passed, 16 skipped, 0 failed** (was 776 passed against the old design).
The drop is almost entirely `test_loop.py`: 2,193 lines testing regexes that no
longer exist, replaced by 23 tests of the loop's *decisions*. What changed:

- `test_loop.py` rewritten. Asserts which mode a message enters, that a question
  never reaches the gate, that a plan comes from a tool call, that a narrating
  planner is re-asked with `tool_choice: required`, that a run which wrote
  nothing cannot fail, that a pre-existing failure does not block, and that
  every `role: tool` message answers a call the assistant actually made.
- `test_field_regressions.py`: the 26 classifier cases are gone — they tested a
  regex — and the phrasing corpus survives as documentation on tests that assert
  *routing* instead. Added one for the thing the code still owns: the classifier
  is given the conversation, not just the message, so "go" after an answer is
  classifiable at all (this is what `_SAYS_GO` was, and its own comment conceded
  that one false match "authorises writes for every later question in that
  session").
- `test_gate.py`: three new baseline regressions — a failure that predates the
  run is advisory, one the run added still blocks, and a baseline that was never
  taken excuses nothing ("we did not look" and "nothing was wrong" must not read
  the same).
- `test_happy_path.py`: the real end-to-end run against the real sidecar and Go
  toolchain now scripts `submit_plan`, and the two `go.mod` tests assert the new
  contract — the gate reports the template's drift and does not write it.
- `test_context.py`: two stubs in the gate and loopback fixtures were returning
  `rules_lint` results with no `meta`, which is a tool that does not exist —
  and, while `_lint_is_clean` fell back to `result.ok`, the difference was
  invisible. That stub is why T1 could hide.

**One disagreement with the report, recorded rather than quietly resolved.**
§10 Track A item 6 says to drop the slice-stub behaviour: "keep it only if you
keep the 32k budget; at 245k it has no purpose". Measured, it has one — with it
off, `test_budget_regression` puts P95 at 166,801 tokens against a 128,000
target and the raw reduction falls from 2.4x to 1.6x. What the report is right
about is the *bug*, and that is separately fixed: the version that broke two
field runs superseded on the path alone and told the model to "re-read if
needed" while the repeat ledger refused exactly that. `_supersede_slice` now
requires containment and says where the lines are. Nothing is removed from
history; the message keeps its index and its `tool_call_id`. That is a different
thing from the `collapse`/`discard` ledgers, which deleted assistant messages and
their results by identity, and which are gone. `SUPERSEDE_SLICES` is a switch so
the trade can be re-made.

**One retreat from my own first draft.** T5's first fix made the gateway serve
unmetered for 60s when the quota store is down. The codebase holds an explicit
position against that — "an agent that keeps working when quota and audit are
down is the hole §15.4 closes" — and three tests assert it. Serving unmetered
turns is an operator's decision about their own billing and audit obligations,
so `DEGRADE_SECONDS` now defaults to **0** and the mechanism is opt-in via
`DAKCODER_QUOTA_DEGRADE_SECONDS`. What the report actually complains about is
legibility, and that is fixed on the client: `quota_unavailable` is recognised,
`Retry-After` is honoured when it is longer than our own backoff (a 30s failover
used to burn three retries in six seconds and end the run `ERROR`), and the
message says it is the gateway's quota service rather than your code.

### Phase 9 - rebuilt

`apps/*/build/` was deleted before building. It held the retired prompt files
(`coder.md`, `verifier.md`, `debugger.md`, `scaffolder.md`) and `python -m build`
copies from there, so the first wheel shipped four mode overlays that no longer
exist. This is the same stale-copy trap §8 of the report records a 38-agent
fan-out falling into.

- `dakcoder_shared-0.1.1-py3-none-any.whl` - carries `tool_choice`,
  `SLOW_TO_CLEAR`, the 540s read timeout.
- `dakcoder_agent-0.2.11-py3-none-any.whl` - carries `tools/control.py`, exactly
  three mode overlays, and a `Mode` of `ASK`/`PLANNER`/`AGENT`.
- `extension/dist/extension.js` rebuilt; `l10n/bundle.l10n.json` regenerated
  (767 strings).
- `extension/dakcoder-go-0.2.11.vsix` repackaged, 21.2 MB, and verified to
  contain the rebuilt wheels rather than the previous ones.

Checks: `npm run verify` green end to end - typecheck, 62 extension tests,
bundle, credential scan (17 packaged files), 53/53 commands registered, l10n
current, gotools manifest current (4 binaries). Python: **637 passed, 16
skipped, 0 failed**. The runtime builds against the real `pao-back-end-development`
checkout and resolves the sidecar.

---

## What was not done

**Track B.** The report recommends rebuilding on the Claude Agent SDK or Pydantic
AI with `gotools mcp` registered in `.vscode/mcp.json`, and replacing 15,000
lines of `extension/src` with a Chat Participant. That is a rewrite, not a fix,
and its first-choice form is unavailable here anyway - the SDK's loop wants
Anthropic models and this gateway serves Qwen3.8-27B. Its fallback ("Pydantic AI
or a 200-line tool-runner on your existing `LLMClient` with `tool_choice` and
JSON-schema output; the point is one loop, typed transitions, no regex") is what
Track A items 2-6 have now built in place: one loop, three tool allow-lists,
transitions from typed tool calls, `tool_choice` and `response_format` on the
wire, and no regex classifier. The remaining Track B items are genuine strategic
choices for you rather than defects:

- registering `gotools mcp` in `.vscode/mcp.json` so Copilot and Claude Code can
  use the same 30 rules;
- moving the knowledge base into `AGENTS.md` / `CLAUDE.md` so it is read rather
  than searched;
- the Chat Participant API in place of the custom webview.

**The `_lint_is_clean` fallback is now strict.** With no counts in `meta` and a
body that is not JSON, a blocking `rules_lint` stage fails rather than passes.
That is the right direction for the stage the product's promise rests on, but it
means a `rules_lint` producer that forgets to set `meta` will block the gate. The
real path sets it twice over (`gotools._report` copies the counts, and
`_render_lint` opens with "clean"), and both are asserted.


---

# Round 2 — what `error.md` showed

Two runs in that transcript ended `Stopped - no progress` and one read was
refused after ten windows of a 6,571-line file. Five distinct causes, and one of
them was mine.

## R2.1 - `rules_lint` could fail but had no baseline (a regression I introduced)

`_lint_is_clean` used to parse a body that has been rendered prose since
`_render_lint` landed, so the decode raised on every call and the check fell back
to `result.ok`, which is True whenever the sidecar ran. Fixing that (T1) without
also baselining the stage turned *"the headline promise is inert"* into *"no
legacy service can ever clear the gate"*: turn 45 of the transcript shows 98
blocking findings, most of them in `handler/paogen.go`, which the run never
opened.

Every other blocking stage got a baseline in round 1. This one did not.

- `_report` now returns `violation_keys` (`rule|path|message`, the shape
  `swagger_check` has always used) and `violation_rules` in `meta`.
- `rules_lint` joins `_BASELINE_STAGES`, measured **unscoped**, because the
  point is to learn which rules the service already violates *anywhere*.
- `Baseline.excuses` gained a second test. Key comparison alone excuses nothing
  on a vertical slice: a new file's findings are new keys however faithfully it
  copied the file next door — which is what the system prompt tells it to do
  ("when the contract is silent, copy the shape of the nearest existing
  resource"). So a finding is also excused when its **rule class** was already
  violated somewhere in the module. What still blocks is a rule *nothing* was
  violating and this change now does.

Measured on `pao-back-end-development`: 199 findings in two touched legacy files,
all advisory; a synthetic `layer-sql-boundary` finding still blocks.

## R2.2 - `swagger_check`'s baseline was empty on every run ever

`_list(None)` returned `["None"]` via `str(None)`. `_swagger_check` builds its
`paths` from `_list(inv.arg("paths"))`, and `arg` returns `None` when the caller
passed none — so the **unscoped** call, which is the one `take_baseline` makes
and the only one that can see what was already broken, was silently scoped to a
file named `"None"`. It matched nothing, all nine real findings were reported
out-of-scope, and the baseline came back empty. Every legacy handler's missing
`Routes()` then blocked the gate as this change's fault: exactly the failure the
baseline exists to prevent, arriving through the baseline.

With both fixed, the full gate on `pao-back-end-development` scoped to two
touched legacy files now returns **PASS** — `rules_lint`, `go_vet` and
`go mod tidy` advisory, `swagger_check` clean. It returned `blocked_by:
rules_lint` before.

## R2.3 - the gate budget could not be spent while the model kept calling tools

`_gate_failed` is reached only from `_verify`, which is reached only from a turn
that called **no** tool. So a model that answers a blocked gate by calling tools
was never counted against `MAX_GATE_FAILURES`, never re-asked, never stopped. In
the transcript the gate blocked on turn 45 and the run spent turns 46-65 on
`go_build`, `git_status` and `git_ops commit` with `gate_failures` stuck at 1,
ending at the turn cap. `_gate_stalled` now counts turns that changed no file
while a failing gate stands, and ends `unverified` naming the stage.

## R2.4 - the repeat intercept was feeding the loop it was built to stop

Round 1 removed `collapse`/`intercepts` under "stop deleting history". That
removal reintroduced a measured failure the report itself records: **one**
(repeated call -> "answered from the previous result") pair in history and the
model moves on 5/5; **two** and it repeats the call 5/5 forever, whatever the
answer says. Both transcript loops are that shape — `git_ops commit` seven
times, `search_repo` eight times, every repeat answered correctly into a
transcript that told it to do it again.

`ContextManager.supersede` replaces the earlier answer's *content* in place. The
message keeps its index, its role and its `tool_call_id`, so nothing is orphaned
and the wire stays well-formed — the same discipline `_supersede_slice` uses,
and a different thing from the ledgers that deleted messages by identity.

And the escalation was wrong even when it fired. A model repeating one call is
out of *moves it recognises*, not out of ideas, and the move it needs — stop
calling tools and say where you are — is one no message can make it take while a
tool schema is on the table. After `STALLS_BEFORE_ANSWER` (2) the next turn is
dispatched with `tool_choice: "none"`, so prose is the only reply available, and
the loop already knows what prose means: in ASK it is the answer, in AGENT it is
"I am done, run the gate". The mirror of the `tool_choice: "required"` re-ask,
with a `tools=[]` fallback if the endpoint refuses the parameter.

## R2.5 - the read budget counted calls, not lines

`MAX_READS = 10` per path, ignoring the ranges entirely. `handler/paogen.go` is
6,571 lines; the run was cut off on its eleventh thirty-line window having seen
about 280 of them — four per cent — and told that reading it again "is not going
to show you anything those did not".

`_ReadLedger` holds the delivered line ranges as merged intervals, recorded from
the *result's* span rather than the request (the tool clamps to the file). A read
is refused only when its range is already inside the union; the call ceiling is a
backstop that scales with the file (`LINES_PER_READ = 150`, floor 10, cap 60), so
a 6,571-line file is worth 44 reads instead of 10.

## R2.6 - the outcome was a lie

`no_progress` is a report about the loop. The first transcript had written nine
files, built them, regenerated the swagger docs and committed — and was reported
as having made no progress. `_stalled` now leads with the gate verdict when one
is failing, and otherwise names the files on disk and what the gate last said
about them.

**645 passed, 16 skipped, 0 failed**, including seven new regressions taken
directly from the transcript.


## R2.7 - verified against the reported shape, and one more found doing it

Asked directly whether the repeat loop still kills the session, I found the test
I had written for it was weak: `ScriptedClient` ignored `tool_choice`, so it
asserted the parameter was *sent* and proved nothing about what it does. Making
the stub honour `tool_choice` — the endpoint enforces it, so the stub should —
changed three outcomes, and each was informative:

- The repeat loop now ends `done` in 4 turns instead of `no_progress` at 6.
  `MAX_STALLED_TURNS` is never reached.
- The blocked-gate case stops via the gate budget when the model answers in
  prose, and via `_gate_stalled` when it answers with tool calls. Two paths, two
  tests.
- **The planner force was not once per run**, as its own docstring claimed:
  `state.forced` was reset every turn, so a Planner that had decided there was
  nothing to plan said so, was forced, complied with a call it did not need,
  said so again, and was forced again — two model calls a turn to relitigate a
  decision it had already made twice. Now once per run: the first refusal may be
  a turn whose call was never emitted, the second is an answer.

Traced end to end through the real loop, both shapes from the transcript:

    where is the cbds loop        (ASK, one search_repo forever)
      turn 1  dispatched search_repo
      turn 2  intercepted
      turn 3  intercepted          <- stall 2, next turn is forced to answer
      turn 4  model answers
      done - "answered", 4 turns   (was: no_progress at 6)

    add an employee resource      (AGENT, git_ops commit forever after the work)
      turn 1  submit_plan
      turn 2  patch_file
      turn 3  git_ops
      turn 4  intercepted
      turn 5  intercepted          <- stall 2
      turn 6  model answers -> FULL GATE ok
      done - "1 file(s) changed and the gate is clean", 6 turns
                                   (was: no_progress at 65)

And the backstop is still tested: a server that accepts `tool_choice` and does
not honour it still hits `MAX_STALLED_TURNS` and stops, rather than spinning.

**646 passed, 16 skipped, 0 failed.** Wheels and `.vsix` rebuilt.


---

# Round 3 — verified against the live gateway

A valid JWT changed how this works. Everything below was measured against
`ai.cept.gov.in/dakcoder` and Qwen3.8-27B, not inferred. The capability matrix is
in [docs/ENDPOINT-CAPABILITIES.md](docs/ENDPOINT-CAPABILITIES.md) and the
executable half is `apps/agent/tests/test_live_endpoint.py`, behind
`DAKCODER_LIVE=1`.

## What the live model settled

**The model is not the problem.** It honours `tool_choice` in every working
mode, returns parallel calls, and scored 3/3 on schema-constrained intent
classification including the bare `"go"`.

**Both of my tool-suppression levers are broken.**
- `tool_choice: "none"` returns zero tool calls and puts `<tool_call>` markup in
  `content`. That was round 2's fix, and it has a 100% failure rate here — it is
  what produced the `<toolcall>` text served to the developer as an answer.
- `tools: []`, which I proposed as the replacement, leaks the same way *and*
  invents `<function=Grep>` with `output_mode` — Claude Code's tool, remembered
  from training.

The mechanism is visible in the id shapes: `auto` returns `call_<hex>` (vLLM's
tool parser), `required`/named return `chatcmpl-tool-<hex>` (guided decoding).
`"none"` disables the parser while leaving the schemas in the prompt, so the
model writes the call and nothing is listening.

**The loop was a cliff, not a slope.** Replaying the real transcript at
increasing depth: sensible through five fruitless calls, and at six it repeats
its last call 4–5 times in 5 and does not recover.

**No wording is dependable at that depth**, including an explicit *"do not
search for it again"* — 5/5 loop in one session, 0/5 in another. **A named
`tool_choice` on a terminal tool has been 5/5 in every session measured.**

Which is the whole diagnosis in one sentence: **`ask` and `agent` had no way to
say "I am finished"** — finishing meant *not* calling a tool, a non-action this
model cannot reliably produce. `planner` never had the problem because
`submit_plan` and `ask_developer` gave it typed terminal actions.

## The six fixes

**1 — `finish` for `ask` and `agent`.** A terminal tool alongside `submit_plan`,
in every mode. Stall recovery is now `tool_choice: {name: "finish"}` plus the
message, never suppression. *Live: `hi` answers in 1 turn; `explain the
bootstrapper` in 6 (was 10); `does any handler have Routes()` in 5, no loop.*

**2 — the inner loop stopped drowning the model.** It appended ~1,000 tokens
after **every edit**, headlined "199 blocking and 480 advisory findings across
49 files" with examples from `handler/paogen.go` — a file the run never opened.
98% of those warnings were outside the change. `_render_lint` now quotes only
files in scope and counts the rest in one line, and the run-start baseline is
consulted here too. *Measured: 816 tokens → 0 when the findings predate the run.*
**This is the "verifier running till 85".**

**3 — the baseline went from 80.1s to 6.4s.** `go_test` was 74 of those seconds
and is now taken only when the tests can actually run. The container probe was
`shutil.which("docker")`, which finds Docker Desktop's binary with the daemon
stopped — it runs `docker info` now, cached. *Same gate verdict, 12x faster.*

**4 — a research bound at 12 turns per phase.** A fence around the measured
cliff at six. When the acting mode hits it with unwritten plan targets the
message names them and forces `required` (write it) rather than `finish` (leave).

**5 — the ledgers carry across messages**, as the context already did. This is
why "where is the plan?" replayed the previous message's loop verbatim.
`dead_ends` and `last_results` deliberately do *not* carry: you edit files
between messages and nothing watches for that.

**6 — `search_repo` says what it searched.** "no matches for 'Routes' in 0
files … that is your answer: it does not" is now "nothing was searched: the glob
matched no files, so this says nothing about whether 'Routes' exists". *Live: 5/5
correct vs 3/5 at the first step. Zero effect at depth 6 — which is why it is
sixth, not first.*

## Two bugs the live testing found that I had shipped

**The baseline raced the run.** Moving it to a background thread made "before
the run touched anything" a race, and losing it is silent and backwards: the
snapshot picks up the run's own breakage and the gate then excuses it. Caught in
a scripted run where the first edit landed inside the six seconds. Now the thread
is joined before entering the writing mode, *and* a baseline whose measurement
spans a mutation is discarded.

**`finish` made quitting the easiest move.** Two runs in three then called it on
their first acting turn — "I have gathered all the necessary details to write the
migration plan" — having written nothing. A `finish` that abandons the plan is
now sent back once, naming the unwritten files; a second is believed.

## Three corrections the live testing forced on me

**"No wording works" was too strong.** The same explicit instruction got 5/5
loop in one session and 0/5 in another. Wording helps and cannot be depended on;
only the named `tool_choice` has been 5/5 in every session. The live test now
asserts the *comparison* (forcing is never worse) rather than a flaky negative,
and `docs/ENDPOINT-CAPABILITIES.md` records both numbers.

**`tools=[]` has two failure modes, not one.** Sometimes it writes markup for a
foreign tool; sometimes it refuses outright — *"I don't have access to file
system tools or code search capabilities in this environment"*, which is L11
word for word. The test asserts the property that rules the lever out (the reply
is not usable as an answer) rather than either symptom.

**Three "agent failures" were my harness crashing.** A `UnicodeEncodeError` on a
`→` in a cp1252 console killed the live runner mid-run, and I nearly recorded
three clean runs as failures. The runner now forces UTF-8 on stdout. Worth
stating because it is the same mistake in miniature as everything else here:
a tool broke, and the first instinct was to blame what the tool was watching.

## Honest status

**651 unit tests pass. 9 of 9 live endpoint tests pass.**

Live scenarios against `pao-back-end-development` and the real model:

| scenario | before | now |
|---|---|---|
| `hi` | (n/a) | **1 turn** |
| `explain the bootstrapper…` | 10 turns | **6 turns**, no gate, no writes |
| `does any handler have Routes()?` | looped to `no_progress` | **5 turns**, answered |
| `write a migration.md…` | 29 turns, no file, `<toolcall>` as the answer | **3 of 3 runs wrote it** — 16/17/28 turns, `done`, clean gate, 13–17 KB |

The 28-turn run is the research bound doing its job in both phases rather than
anything going wrong: 12 planning turns, forced `submit_plan`, then the acting
phase reading and writing.

I am not going to claim this is the final fix for everything — that claim is what
produced rounds 1 and 2. What is different now is that the next problem is
diagnosable in minutes instead of days: there is a live harness, a capability
matrix with dates on it, and every constant in the loop traces to a measurement
rather than to an argument.


---

## Release 0.3.0

Versions were stuck at 0.2.11 across every build this session, which is exactly
how a reinstall silently keeps the old copy — VS Code keys the extension on
version. Bumped coherently:

| artifact | was | now |
|---|---|---|
| extension | 0.2.11 | **0.3.0** |
| `dakcoder-agent` | 0.2.11 | **0.3.0** |
| `dakcoder-shared` | 0.1.1 | **0.2.0** |
| runtime API | 1.0 | **1.1** |

The API bump is not cosmetic. `API_VERSION` is documented as moving only "when a
response shape changes in a way a client could not have anticipated", and the
mode vocabulary did exactly that: a 1.0 client's `Mode` union does not contain
`ask` or `agent`. It degrades rather than crashes, but the guard exists so that
half-working is not the outcome nobody suspects. The additive changes in the
same release — `intent` on `POST /v1/tasks`, `POST /v1/credential`, `intent` on
`turn_start`, the three control tools — would not on their own have justified it.

Added `CHANGELOG.md`; there wasn't one.

Rebuilt from a clean tree (`apps/*/build` removed first — that directory shipped
four retired prompt files in an earlier build):

- `dakcoder_agent-0.3.0-py3-none-any.whl`, `dakcoder_shared-0.2.0-py3-none-any.whl`
- `extension/dist/extension.js`, l10n bundle (767 strings)
- `extension/dakcoder-go-0.3.0.vsix`, 21.2 MB, old `.vsix` removed

Verified rather than assumed:

- The wheels **install into a clean venv** and resolve `dakcoder-shared 0.2.0`
  on their own; `dakcoderd` entry point present; the installed package reports
  API 1.1, three modes, three intents, `finish` in all of them.
- The `.vsix` carries **only** the 0.3.0/0.2.0 wheels — no stale copies — and
  its bundled runtime reports 1.1, matching the bundled extension.
- The venv cache key changed (`runtime-e9ff7c16…` → `runtime-5b4ef360…`), so the
  stale venv holding the pre-round-3 agent is replaced and pruned on first run.
- 651 unit tests, `npm run verify` green end to end (typecheck, 62 extension
  tests, credential scan, 53/53 commands, l10n, gotools manifest).

Install:

```bash
code --uninstall-extension dop.dakcoder-go
code --install-extension "D:/desktop/dakcoder-go/extension/dakcoder-go-0.3.0.vsix"
```

Then reload the window. The version change means VS Code will not serve a cached
copy this time.
