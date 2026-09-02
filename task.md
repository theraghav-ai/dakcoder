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
