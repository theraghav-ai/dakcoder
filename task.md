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
