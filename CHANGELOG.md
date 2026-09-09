# Changelog

## 0.3.5 — 2026-09-09

Extension `0.3.5`, `dakcoder-agent` `0.3.5`, `dakcoder-shared` `0.3.5`,
`dakcoder-gateway` `0.3.5`. Runtime API unchanged at **1.1**.

**No deploy order.** The `plan` and `turn_start` events gained fields; nothing
was removed or renamed and the gateway is untouched, so a 0.3.4 gateway serves
a 0.3.5 runtime and the reverse. An older panel ignores the new fields; a 0.3.5
panel falls back to the old parse when a runtime does not send them.

Two field runs, three days apart, both asking the agent to *validate* a
migration document against the codebase. Both ended without an answer: one
after 25 turns of `no_progress`, the other having started executing an
eight-step migration nobody had asked for. This release is the account of why,
and every prompt in it was chosen by measurement against the live endpoint
rather than by argument — three of the fixes below read plausibly and scored
zero, and are documented as such in `docs/ENDPOINT-CAPABILITIES.md`.

### Fixed — a question routed as work

The intent classifier enumerated the *subjects* of a change — "a feature, a
fix, a migration, a refactor" — so "validate the migration plan" matched on the
noun and the verb was never consulted. Measured against 60 labelled requests,
three samples each: **44/60**.

It now asks one question — *after a perfect reply, is any file in the
repository different?* — which is the decision the loop is actually making, and
which puts the awkward boundary in the right place for free: writing findings
into `AUDIT.md` is a change however analytical the request sounds. **60/60**,
including on eight verbs the prompt does not name (diagnose, critique,
sanity-check, map out), so it is not winning by vocabulary.

- **`why` is required, and short.** The field was in the schema from the start,
  optional, and the model omitted it on every classification — so the one
  artefact that could explain a misroute never existed. Requiring it cost 2
  cases in 60: left alone the model writes forty words, the reply lands on the
  token boundary, the JSON is cut, and an unparseable classification falls back
  to ASK — safe, but silently wrong on a change. Asking for eight words *in the
  prompt* fixed it; `maxLength` on the schema did nothing, because guided
  decoding here does not enforce it.
- **`turn_start` carries `intent_source` and `intent_why`.** Whether a person
  said this was work or a 64-token call guessed it, and on what grounds.
  Twenty turns into an unrequested migration is a late moment to find out.

Measured end to end at production settings, ten runs: routing correct 10/10,
and a validation now touches **no files at all** — structurally, because the
answering mode holds no write tools.

### Fixed — a fence that offered one move

`_terminal_choice` named `submit_plan`, so a Planner twelve turns into
validating a document had exactly one legal call and wrote a migration plan.
The turn now offers all three terminals with `tool_choice: "required"` over a
tool list narrowed to them, which constrains the reply as tightly as naming one
without deciding *which* ending the task deserves.

The widened choice alone did nothing: **the text decides it.** A fence message
listing the three calls and what each is for scored `finish` **0/10** on a
validation — the Planner overlay says "plan the work" and one closing paragraph
does not outweigh it. Posing it as a single question also scored 0/10. Telling
the model to re-read what the developer actually asked for, and naming their
verbs, scores **9/10**, with change tasks still **10/10** on `submit_plan`.

- **`plan_forced`.** A plan the fence extracted, on a run that has written
  nothing since, is not a commitment, and all three enforcement paths now say
  so. Scoping this to the verdict alone was tried and reverted — it protected
  change tasks (5/5) and took the field scenario to **0/5**, because the pushes
  are what turn a validation into a migration. A "reconsider before writing"
  message was also tried and reverted: it read as an invitation to stop and
  took change tasks from 4/4 to 2/4.

### Fixed — twenty turns of work delivered as a colon

Both field runs ended by funnelling everything into one `finish` whose `answer`
held only the sentence that introduces the findings. In the first, the
developer's next message was "where is the assessment".

- **A preamble is sent back, once.** An answer that ends on a colon, or
  promises findings and is too short to have delivered them, is returned with
  one instruction. Cheap to reject on purpose: re-sending the same text
  unchanged is taken as final, so a wrong guess costs a turn rather than an
  argument.
- **The cap is 24,000 characters, not 6,000.** A live run delivered an answer
  of exactly 6,000 — meaning it was cut, after the tokens were already spent.
  Three numbers had to move together and now cannot drift apart: the constant,
  the schema's `maxLength`, and the sentence in the tool description the model
  actually reads.
- **`finish` no longer echoes the answer into the transcript.** It appeared
  twice — once as the call's arguments, once as its result — and a session that
  reuses its context carried both into the next message, demonstrating to the
  model that the way to respond is to repeat what it said last time. One field
  run did exactly that.
- **Ask mode says what "in full" means.** Letting findings accumulate as prose
  with `finish` as a summary was on the roadmap twice as the real fix for the
  single-string channel. Measured, it was **worse than the status quo** (3.7/6
  against 4.0/6): told the detail was already above it, the model wrote a
  summary of a summary. The channel was not the problem. What the text never
  said is that the developer sees no tool calls, so anything outside `answer`
  reaches nobody, and that the things which turned out fine still have to be
  reported by name. That is worth **4.8/6 against 2.0/6**, in two fewer turns
  and a fifth of the characters — an instruction to answer "in full" with no
  account of what full means is as easily satisfied by padding as by checking.

### Fixed — the panel showed files the run would never touch

The `plan` event carried `{text, steps: N}` and left the panel to recover the
rest with a regex over the rendered prose — one that matched path-shaped tokens
anywhere in a step and only knew Go, SQL and YAML. On the field plan, whose one
step targeted `MIGRATION_PLAN.md` and whose description mentioned two Go files
as examples, it rendered "Files in scope: handler/response.go, helper.go" and
never named the file being written. `_unfinished` had this same bug server-side
and fixed it; the UI kept the old version.

The event now carries the typed steps `submit_plan` already validated. Both
parsers consume them, per-step status comes from the change set — a step is
done when a write lands on its file, never because the model said so — and the
footnote explaining that no field carries status now appears only against a
runtime old enough for that to be true.

### Fixed — a re-read the acting mode needed

A `read_file` on a file the plan sets out to change is exempt from the coverage
refusal while acting. `patch_file` takes an `old` that must match the bytes on
disk, and "you have already seen those lines" is not the same as having them
accurate enough to anchor an edit — twenty turns and a phase switch back, under
a summariser. Coverage only; the call-count backstop is untouched.

### Fixed — a compaction that threw away the analysis

The recap's vocabulary was about *doing* — decisions taken, files modified,
steps verified — and a run whose work is reading has none of those. A validation
of fifteen files compacted at turn 10 into a list of filenames, and the three
turns after it re-read seven of them to recover what it had already found.
`findings` is now part of the recap, its schema and its merge.

## 0.3.4 — 2026-09-09

Extension `0.3.4`, `dakcoder-agent` `0.3.4`, `dakcoder-shared` `0.3.4`,
`dakcoder-gateway` `0.3.4`. Runtime API unchanged at **1.1**.

**No deploy order.** Nothing on the wire changed and the gateway is untouched;
a 0.3.3 gateway serves a 0.3.4 runtime and the reverse.

One field run, two causes. The developer asked for four additions to a
migration document. The run read the plan, planned the edit, and wrote
nothing — twenty-five turns, ending `no_progress`.

### Fixed — a phase-ending call is never answered from a ledger

`_intercept` ran ahead of the terminal check and its `continue` skipped it, so
`finish` was cached like a search result. That only mattered on the one path
where a `finish` is *sent back*: `_phase_ended` refuses a `finish` that left
plan targets unwritten, once, and asks the model to call it again if it
disagrees. The retry it asked for was answered from the cache and never
reached the check that would have let it through, so `finish_refused` stuck at
1 against a `MAX_FINISH_REFUSALS` of 1 and the escape was unreachable.

What the model read, having asked to stop: *"that is the current answer. The
call ran earlier, nothing in the workspace has changed since... Use it and
move to the next step."* There was no next step. It called `finish` again.
Four more times, until the turn budget ran out.

- `_intercept` returns `None` for anything in `_TERMINAL`. A repeated terminal
  call is a signal — the model is trying to stop — and the bounded refusals in
  `_phase_ended` are what answer it.
- Terminal results are no longer written to `last_results` at all. Belt and
  braces, but the entry that deadlocked the run is one no ledger should be
  able to find again.

Measured on the repro: 1 dispatch and 5 intercepts across 8 turns, before;
2 dispatches across 3 turns, after.

### Fixed — an edit needs anchor text, whatever the coverage ledger believes

The same run, the half that stopped the edit being written. `patch_file` takes
an `old` that must match the bytes on disk, and the model said what it needed
and why — *"let me check the end of the file to find the right anchor"*.
`_re_reading` refused it, because those lines were technically still in
context twenty turns and a phase switch back, and returned no file content at
all. Four refusals across the planning and acting phases.

A read of a file the plan sets out to change is now exempt from the coverage
refusal while in `AGENT` mode. Coverage only: the call-count backstop is
untouched, so this buys the acting mode at least `MIN_READS` looks at a file
it is supposed to edit rather than unbounded turns. A file no step names is
still refused exactly as before.

The exact-repeat cache is deliberately not part of this — it replays the file
text, so a model that asks twice still gets what it came for.

## 0.3.3 — 2026-09-04

Extension `0.3.3`, `dakcoder-agent` `0.3.3`, `dakcoder-shared` `0.3.3`,
`dakcoder-gateway` `0.3.3`. Runtime API unchanged at **1.1**.

**No deploy order.** Nothing on the wire changed and the gateway is untouched;
a 0.3.2 gateway serves a 0.3.3 runtime and the reverse.

The residuals of the 2 September audit's re-check, closed.

### Added — a task state machine, and the block that shows it

The third review's root cause, in its words: *a correct control state machine
and no task state machine, and the task state it does hold is never shown to
the model.* Six changes, in the order that review ranked them.

- **A turn-scoped state block.** `ContextManager.set_state` pins a block the
  loop rebuilds every turn from ground truth — the plan with each step's
  status, the files written this run (`router.touched`), the last gate verdict
  and the turn it ran on, and what has been ruled out — rendered last in the
  volatile layer, so it costs its own ~150 tokens and never a re-prefill.
  Derived, never model-written: a checklist the model maintains can lie; a
  block rendered from the change set cannot. Empty on a question with nothing
  to report.
- **Plan steps carry a status.** `PlanStep.status` is `pending`, `done`,
  `failed` or `skipped`, with a `note`. `done` when a mutation lands on the
  step's file; `failed` when a gate failure after an edit names it; `skipped`
  only by the model, through `revise_plan`. "What is left" is no longer a set
  difference that could not say *attempted and failed*.
- **Informed is not dispatched.** A call counts as progress only when its
  result body is one the run has not seen (the same search under other words
  returns the same body), it is not an empty finding (`search_repo` now
  reports `hits: 0`), and the overlap test did not say it repeats. That test
  — the citation-set comparison `search_docs` has had all along — now runs on
  `search_repo`'s `path:line` keys too. The stall counter, and the forced
  `finish` wired to it, now measure what their docstrings claim.
- **A replan path.** On the second gate failure *after an edit*, the loop
  sends the run back to the Planner once with a `# What has been tried` block
  instead of the same instruction a third time; a plan that comes back keeps
  the steps already done and gets the full gate bound. A Planner that declines
  after being sent back ends `unverified`, not `done`. `revise_plan` is the
  model's own pivot in the acting phase: the remaining steps replaced, the
  reason recorded, at most twice a run.
- **Batches are bounded.** `parallel_tool_calls` cannot be sent to an endpoint
  with `drop_params` off, so the bound lives in the loop: a call repeated in
  one reply runs once, calls past `MAX_CALLS_PER_BATCH` are answered "not run",
  and `finish` sent alongside other calls is refused with the instruction to
  send it alone after the results arrive. The wire stays coherent throughout.
- **`finish` is bounded, and a cut-off write names its file.** `finish.answer`
  carries `maxLength` and the handler caps it, saying so where the developer
  reads it. The truncation message names the file the cut-off write was for,
  says it is unchanged, and restates what has landed on disk.
- **A live task suite.** `test_live_tasks.py` drives the loop over six real
  tasks on a fixture service against the live endpoint (`DAKCODER_LIVE=1`,
  like `test_live_endpoint.py`) and asserts on the run's metrics record and
  change set, never on prose. A scripted model cannot rephrase a search, claim
  work it did not do, or spin; those are the three failure modes the changes
  above are for, and this is what makes them falsifiable.

### Fixed

- **The summariser is handed pieces, never the whole eviction.** The evicted
  set went to the recap call whole, whatever its size: three capped reads were
  one ~577,000-character request, and on the emergency 15% path the eviction
  could exceed the summariser's own window, fail, and fall back in silence.
  Every message is now bounded first (a tool result keeps its head and tail,
  prose its head), the transcript is split at message boundaries into pieces of
  at most `_TRANSCRIPT_CHARS`, and each piece's recap is folded into the next
  through `Recap.merge` — Aider's recursive summary and langmem's running
  summary, applied to a structured recap. At most `_MAX_RECAP_CALLS` calls per
  compaction; older pieces beyond that are digested deterministically (which
  tools were called on which files) and the recap says so. Every failed piece
  is now an `error` event, not only the ones that were programming mistakes.
  See D-95.
- **The Planner's research fence asks only for what it forces.** It said
  "submit the plan now, or ask the developer what you cannot infer" on a turn
  whose `tool_choice` named `submit_plan` alone. The text now matches the
  constraint, and tells the model to carry an open question as a stated
  assumption in the step, where the developer sees it in the plan card.
- **An empty read range is refused, not clamped.** `read_file(start=100,
  end=50)` returned line 100 as a success. It is now a refusal with a
  `dead_end` mark, so a repeat is answered from the ledger.
- **A second `UnsupportedParameterError` ends the run instead of escaping it.**
  The fallback dispatch ran inside the `except` handler, so a refusal of the
  degraded choice skipped every handler below and left `_complete` as a crash
  the runtime dressed up after the fact. The fallback is a loop now, and a
  refusal with nothing left to fall back to takes the ordinary error path.
- **A steer restarts the gate-stall clock.** A run standing in front of a
  failing gate ended on the very turn the developer's correction arrived, with
  the message appended to a context nothing would read again. A drained steer
  resets `idle_since_gate`; the bound is unchanged.
- **A session's status flips after its last event lands.** `session.finish`
  ran on the worker thread while `finish` and `end` were still queued for the
  event loop, so a client connecting in that window replayed a transcript with
  no `end`, saw "not running", and closed. The status update is now queued
  behind the events on the same loop.
- Test hygiene: `test_zz_debug_gate` (printed, asserted nothing) is gone, and
  `test_write_preserves_the_executable_bit` skips on Windows, where NTFS has no
  executable bit and the suite was red on the project's primary platform.

## 0.3.2 — 2026-09-03

Extension `0.3.2`, `dakcoder-agent` `0.3.2`, `dakcoder-shared` `0.3.2`,
`dakcoder-gateway` `0.3.2`. Runtime API unchanged at **1.1**.

**No deploy order.** Nothing on the wire changed and the gateway is untouched;
this is the verification gate's accounting and the sentence the loop wraps it
in. A 0.3.1 gateway serves a 0.3.2 runtime and the reverse.

### Fixed

- **One new compile error no longer re-charges the run with every old one.**
  The baseline excused a stage all-or-nothing, so a single genuine error in the
  file this run wrote made the whole of `go build ./...` this run's failure
  again — forty errors on a legacy service, none of them marked, under `go
  build`'s own advice to "fix the first error listed". That first error is
  whichever package sorts first, which is somebody else's redeclaration from
  two years ago. A field run followed the instruction exactly as written: it
  spent its turns reading a file it was not allowed to touch, edited nothing,
  and was stopped by the stall guard for standing still in front of a gate it
  had been told to clear. `Baseline.charge` now answers with the *set* of
  findings this run is answerable for, the report prints those under the fix
  hint and the rest under **"Already failing before this run changed anything"**,
  and the loop's covering sentence names that heading as explicitly not the
  model's to fix. The stage still blocks — the build really is broken — but it
  blocks on one error in `handler/laptop.go` instead of on the module's history.
- **A baseline that failed for a reason it could not name no longer poisons the
  gate.** `take_baseline` runs `go build` under `-mod=readonly` so measuring
  cannot rewrite `go.sum`; the gate stage runs without it. On a module with an
  incomplete `go.sum` the two therefore fail *differently* — module resolution
  against compiler errors — so the subtraction cancelled nothing and the run was
  charged for every pre-existing error. Such a failure is now recorded as
  `unkeyable` and excuses the stage whole, which is the honest reading of a
  stage that was already red on arrival. The same path covers a baseline that
  timed out or found no toolchain.
- **The "failing before, with nothing keyable" branch could not be reached.**
  It tested `findings.get(tool) is None`, and `_take_baseline` writes
  `findings[tool]` exactly when `passed[tool]` is False — so the case it was
  written for arrived as an *empty* key set and fell through to blocking
  everything. It now tests the set itself, and `charge` distinguishes "measured,
  nothing charged" (excused) from "never measured" (`None`, charged whole),
  which a plain falsiness test read as the same answer.

## 0.3.1 — 2026-09-03

Extension `0.3.1`, `dakcoder-agent` `0.3.1`, `dakcoder-shared` `0.3.1`,
`dakcoder-gateway` `0.3.1`. Runtime API unchanged at **1.1**.

**Deploy the gateway before the extension.** A 0.3.1 runtime dispatches Ask
turns as the `ask` role, which a 0.3.0 gateway's table does not contain — every
Ask turn against one would come back "'ask' is not a configured role". The
reverse skew is safe: a 0.3.0 runtime only ever sends `coder`, `fast` and
`embed`, all of which a 0.3.1 gateway routes.

### Added

- **A model, an endpoint and a key per role, all from `deploy/dakcoder.env`.**
  `DAKCODER_MODEL_PLANNER=Qwen3-235B-A22B` and a restart is now the whole of
  "put the Planner on a bigger model"; add `_BASE_URL` and `_API_KEY` and it
  can live on another host on another credential. Anything a role does not name
  it inherits from `DAKCODER_MODEL`, `DAKCODER_MODEL_BASE_URL` and
  `DAKCODER_MODEL_API_KEY`, so a one-model deployment sets three variables and
  is done. The roles are `planner`, `coder`, `ask`, `fast`, `summariser`,
  `embed`, plus `verifier` and `debugger` kept for older clients;
  `DAKCODER_MODEL_ROLES` adds more. See `deploy/README.md` and D-94.
- **`GET /v1/models`** (authenticated) — the routing table in force: role,
  model, endpoint, and which of the three the role overrode. Never the keys.
  `/v1/health` gains role → model, and `deploy/status.sh` prints it.

### Fixed

- **A flaky extension test** that failed roughly seven runs in eight, blocking
  `npm run package`. `clears finished_at when a follow-up puts the session back
  to running` built its fixture with `created_at` and `finished_at` both taken
  `now`, so the frozen clock and the fresh one were the same 0ms and the
  assertion turned on which millisecond each `toISOString()` landed in. It also
  asserted the wrong thing — `elapsedMs >= stopped` asks a new run's clock to be
  at least as long as the old run's, which a follow-up to a two-hour run would
  fail on entirely correct behaviour.
- **Every turn dispatched as `coder` whatever mode it was in**, so `planner`
  and `ask` were role-table entries nothing could reach — planning and
  answering were billed, logged and routed as coding. A mode now carries its
  own role.
- **Compaction recaps ask for `summariser` again.** The role was moved to
  `fast` because the client's vocabulary was three names long and this was not
  one of them. Both halves now read the same `ROLES` tuple, with a test
  asserting they agree, so the name that §6.5 specifies resolves — and the
  summariser can be pointed at a small model without moving the intent
  classifier with it.
- **The capability probe covers every distinct endpoint**, not just the
  default. A role pointed elsewhere was previously unprobed, which is the
  failure mode §4.5 exists to prevent. Endpoints shared between roles still
  cost one pass.
- **The credential guards match `DAKCODER_MODEL*_API_KEY` by shape** rather
  than from a fixed list — in the launcher, the extension's spawn, the
  runtime's own startup refusal and the `.vsix` scanner. A per-role key is
  still a key only the gateway may hold, and a list would have gone quietly out
  of date the first time a role was added.
- **A role with no key anywhere stops the gateway at startup**, naming the role
  and the variable, instead of surfacing as a 502 hours later to whichever
  developer reached that role first.

## 0.3.0 — 2026-09-02

Runtime API **1.1**. Extension `0.3.0`, `dakcoder-agent` `0.3.0`,
`dakcoder-shared` `0.2.0`.

The first release where the loop's constants come from measurements against the
live endpoint rather than from argument. What that measurement found is written
up in [docs/ENDPOINT-CAPABILITIES.md](docs/ENDPOINT-CAPABILITIES.md), and the
executable half is `apps/agent/tests/test_live_endpoint.py` behind
`DAKCODER_LIVE=1`.

### Breaking

- **Five modes became three.** `planner`, `scaffolder`, `coder`, `verifier` and
  `debugger` are now `ask`, `planner` and `agent`. Retired names are still
  accepted on the wire and coerced, and a stored session that carries one still
  reads as English in the panel. Runtime API moves to 1.1 for this: a 1.0 client
  is sent mode values its union does not contain.
- **`dakcoder.defaultMode`** is now `auto` / `ask` / `agent`, defaulting to
  `auto`. The old values still work. `multi` used to map onto `planner`, which
  was also the backend default — so the server could not tell "let the agent
  choose" from "the developer asked to plan", and every message, question or
  not, entered the mode whose instruction is *emit numbered steps*.
- **Settings removed** that nothing read: `runtimeIdleTimeoutMinutes`,
  `showTokenMeter`, `verbose`. `DAKCODER_MODE` is no longer set in the child
  environment; nothing read that either.

### Added

- **`finish`** — a terminal tool for `ask` and `agent`, alongside `submit_plan`
  and `ask_developer`. This is the release's central change. In those two modes
  "I am finished" used to mean *not calling a tool*, and past about six fruitless
  calls Qwen3.8-27B cannot produce a non-action: it repeats its last call. The
  planning phase never had the problem because it had typed terminal actions.
- `POST /v1/credential` — the extension pushes a fresh gateway JWT, so a daemon
  that outlives its token recovers instead of 401-ing every call until restart.
- `tool_choice`, `response_format` and `parallel_tool_calls` on `LLMClient`.
  None of the three had a single use anywhere before.
- `docs/ENDPOINT-CAPABILITIES.md` and `test_live_endpoint.py`.

### Fixed

- **The verifier appearing to run forever.** The inner loop appended ~1,000
  tokens after *every* edit, headlined "199 blocking and 480 advisory findings
  across 49 files" with examples from files the run had never opened — 98% of
  the warnings were outside the change. The model went off to fix them. Now
  scoped and baselined: **816 tokens → 0** when the findings predate the run.
- **The run-start baseline: 80.1s → 6.4s.** `go_test` was 74 of those seconds and
  is taken only when the tests can actually run. The container check was
  `shutil.which("docker")`, which finds Docker Desktop's binary with the daemon
  stopped; it runs `docker info` now, cached per process.
- **`rules_lint` could not fail, then could not pass.** It parsed a body that has
  been rendered prose since `_render_lint` landed, so the check fell back to
  `result.ok` and the blocking contract-lint stage could only fail on a sidecar
  crash. Fixing that without a baseline made it block every run on a legacy
  service. It is now baselined on rule *classes*, so a new file written in the
  house style is not charged for a pattern the house has used thirty times.
- **`swagger_check`'s baseline was empty on every run ever.** `_list(None)`
  returned `["None"]`, so the unscoped call — the one the baseline makes — was
  scoped to a file named `"None"` and matched nothing.
- **`go mod tidy` no longer writes `go.mod` at the gate.** It snapshots, runs,
  compares and restores. A run that asked a question used to finish "1 file
  changed: go.mod", written by the gate's own diagnostic.
- **The gate never ran on an empty change set.** A run that wrote nothing cannot
  fail.
- **The read budget counts lines, not calls.** Ten reads per file cut a model off
  at ~280 lines of a 6,571-line handler. It now tracks delivered line intervals
  and refuses only a range already in context; the ceiling scales with the file.
- **`search_repo` says what it searched.** "no matches for 'Routes' in 0 files …
  that is your answer: it does not" is now "nothing was searched: the glob
  matched no files".
- **A crashed run emits `finish`,** and the panel treats `end` as terminal. A
  backend exception used to freeze the panel on "Working…" forever and swallow
  the next message.
- Pre-flight failures render into the panel; four slash commands that dispatched
  to unregistered ids; a dead daemon retrying forever; no request timeouts; no
  in-flight guard on submit; phantom approvals replayed from finished sessions.
- Mid-stream gateway `event: error` frames raised instead of becoming an empty
  answer; client and gateway timeouts aligned so the client gives up first;
  `EmptyCompletionError` recovered whether or not thinking was requested.
- `gotools` discovery matched no binary that exists in this repository.
- `patch_file(new="")` — a deletion — was refused as a missing argument.
- Token calibration compared message content against a `prompt_tokens` that
  includes tool schemas, so every estimate ran high.

### Known and deliberate

- The depth-6 repeat rate is **not** deterministic (4–5 of 5 across sessions),
  and better tool wording helps variably. Only a named `tool_choice` has been
  5/5 every time. The loop forces rather than argues for that reason.
- `tool_choice: "none"` and `tools: []` are both unusable on this endpoint — the
  first leaks `<tool_call>` markup into `content`, the second does that *or*
  refuses with "I don't have access to file system tools". Neither is a way to
  ask for prose.
