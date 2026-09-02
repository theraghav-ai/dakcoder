# Why dakcoder fails on every task

An end-to-end audit of the agent loop, the gate, the tools, the extension, and the process that produced them. Written against the working tree at commit `2c197a7` (2026-09-02). Line numbers refer to that tree.

---

## 1. The answer in one screen

The agent does not fail because of any one bug. It fails because four independent structures each guarantee failure on their own, and every incident of the last nine days has been fixed one symptom at a time inside those structures rather than by removing them.

| # | Root cause | Where | Effect |
|---|---|---|---|
| 1 | **The gate cannot pass on the repositories you run it against.** `go_test` is blocking and unscoped; the legacy corpus's tests need Docker (testcontainers). `go_vet`, `go_build`, `go mod tidy` are also unscoped and have no baseline, so pre-existing damage is charged to the run. | `gate.py:303-375`, `loop.py:1633-1660`, `pao-back-end-development/tests/testmain_test.go` | Every task that reaches `_verify` ends `unverified`, `no_progress`, or in the escalation ladder. On the legacy corpus this is 100% of coding tasks. |
| 2 | **Intent is decided by regexes over prose, after the model has already answered, and every message starts in Planner.** | `loop.py:532`, `loop.py:1372-1486`, `loop.py:2506-2737` | Measured on 40 realistic prompts: 17 of 24 read-only questions are not recognised as read-only. Any of them whose answer is numbered becomes a "plan", enters Coder, runs the full gate on an untouched workspace, and enters the Verifier ladder. That is the "three phases for an explanation". |
| 3 | **The loop is a 34-counter heuristic state machine that fabricates tool results and rewrites its own transcript.** | `loop.py:102-240`, 17 fake `role: tool` messages, `context.py:690`, `context.py:614-670` | The model is asked to reason over a conversation that contradicts itself. Transcripts show the Verifier saying "my job is to make the edit" and the Coder saying "I am in verifier mode". |
| 4 | **The model is never given the two primitives agent loops depend on: forced tool calls and structured output.** | `tool_choice`, `response_format`, `parallel_tool_calls` appear nowhere in `apps/` | A 27B model at temperature 0.1 narrates "Making the edit now" with no tool call; the loop counts three of those and kills the run instead of re-asking with `tool_choice: required`. The plan is free prose parsed by regex. |
| 5 | **The extension hides every failure.** | `extension.ts:386`, `extension.ts:393-401`, `loopback.py:275-277`, `session-state.ts:304,870` | A crashed run leaves the panel on "Working…" forever and swallows the next message. Pre-flight failures produce a toast or a log line, never a chat reply. From the panel, every failure looks like "your message, then nothing". |

Underneath all five: **the test suite is green (776 passed, 26 skipped) because every test drives a scripted stand-in for the model.** The suite verifies that the heuristics react correctly to the exact replies they were written for. Nothing in CI exercises the classifier, the ladder, or the gate against a real model or a real legacy repository.

---

## 2. Trace: what happens to a simple question

### 2.1 "explain me this project" (transcript, `live-issue-reported.md`, turns 2-5)

1. The extension sends `POST /v1/tasks {task, mode: "planner"}`. `modeFor('multi')` maps to `'planner'` (`extension.ts:639-641`), which is also the backend default (`loopback.py:145`, `serve.py:105`), so the server cannot tell "let the agent choose" from "the user asked for the Planner".
2. `AgentLoop.run()` switches to `Mode.PLANNER` unconditionally (`loop.py:552`). The model receives the 791-token system prompt about the n-api-template contract, the Planner overlay ("Plan first… emit at most eight numbered steps"), and 13 read-only tool schemas.
3. Turns 2-3: the model calls `repo_map`, then `read_file` twice. Reasonable.
4. Turn 4: the model answers in prose with bullet points. `_advance` (`loop.py:1372`) runs `_count_steps` → 0 (bullets are not numbered). `_asks_the_developer` is false. `_refuses_to_plan` is false. The `steps == 0` branch at `loop.py:1415` fires: `seen_calls` is non-empty because files were read, and `planner_idle` is 0, so the loop **appends a fabricated `repo_map` tool result** telling the model "You ended that turn with no plan and no tool call… Do one of three things" (`loop.py:1443-1452`) and returns.
5. Turn 5: the model replies "The task was to explain the project, which I did — no code change is needed, so there is nothing to plan." `planner_idle` is now 1, so the run ends `DONE`.

Five model calls for a question that needed three. The `_is_explanation` check that would have ended the run at turn 4 sits at `loop.py:1461`, **after** the `steps == 0` branch, so it is never reached for an unnumbered answer. Every read-only question that needed a tool call and was answered without a numbered list pays this extra turn and this confusing nudge.

### 2.2 "explain the bootstrapper and tell me how it deviates from the new template" (same transcript, turns 6-32)

Turn 10's answer was numbered. At the time, `_count_steps` returned 10, nothing recognised the task as a question, and the explanation was **pinned into the task layer as the plan** (`set_plan`, `loop.py:1483`, into the un-evictable `Layer.TASK` at `context.py:602-612`). The run switched to Coder.

Turn 11: the Coder said "no code change was requested" and called no tool. `_advance` sent a Coder with no tool calls straight to `_verify` (`loop.py:1488-1490`). `_verify` ran **the full gate on a workspace nothing had touched** (`loop.py:1536`; the "nothing was written" guard at `loop.py:1547` is consulted only when the gate passes). The gate took 70 seconds, `go_test` failed after 67.62 s (Docker), `go_vet` failed on a tab character in `core/domain/transferentry.go:19` that predates the session, and **`go mod tidy` rewrote `go.mod`**. The run's final line reads "32 turns · 1 file: go.mod". An explanation question mutated the repository.

From turn 12 the escalation ladder took over: Verifier → Coder → Verifier → Coder, for 21 turns, ending "Stopped — no progress". The mode-refusal cache bug that kept the Coder's patch from landing has since been fixed (`loop.py:1239`), and the `_is_explanation` guard now catches this exact phrasing. Neither fix touches the structure that produced it.

### 2.3 What still happens today, with a phrasing the regex does not know

Run against the current code (`_is_read_only_task`, `loop.py:2688`), these are all classified as **not** read-only:

```
hi                                              is this handler correct?
does the objection handler follow the template? thoughts on the repo structure
any problems with repo/postgres/paogen.go?      list the routes in this service
give me the list of repositories registered in fx
can you see any bugs in transferentry.go         do we use squirrel anywhere
i don't understand the fx.Annotate wrapper       help me understand the repository pattern here
go through the handler and point out template violations
look at main.go                                  find all places gin.Context is used directly
check if go mod tidy is clean                    should I use patch_file or write_file for a new resource
which files would I need to change to add a status filter?   (matched as WORK because of "add a")
```

17 of 24 realistic read-only prompts miss (`_ASKS_TO_BE_TOLD` never fires). 0 of 16 work prompts miss. The classifier is tuned entirely toward the phrasings that appeared in past incidents. For any of the 17, the path is: numbered answer → `set_plan` → Coder → prose → full gate on an empty change set (≈70 s on the legacy corpus) → Verifier → Coder → … → `_narrating` fires at three idle Coder turns → "Stopped — no progress". Around eight turns and two minutes for "list the routes".

---

## 3. The gate cannot pass on your repositories

`GATE` (`gate.py:303-375`) is ten stages. Four of the blocking ones take `lambda ctx: {}` and run over the whole module regardless of what changed:

| Stage | Scoped to touched files | Blocking | Baselined | On `pao-back-end-development` |
|---|---|---|---|---|
| `go_build` | no | yes, halts | no | passes |
| `govalid_gen` | no | advisory | no | fails (no `handler/request/` dir) |
| `rules_lint` | yes | yes | discounts out-of-scope itself | skipped unless `.go` touched |
| `swagger_check` | yes | yes | yes (`_take_baseline`) | 9 pre-existing findings on any legacy gin service |
| **`go_vet`** | **no** | **yes** | **no** | **fails**: pre-existing tab in a struct tag |
| **`go_test`** | **no** | **yes** | **no** | **fails after 67 s**: tests need Docker Postgres via testcontainers |
| `go mod tidy` | no | yes | no | **mutated `go.mod`** on an untouched workspace |
| `golangci_lint` | no | advisory | no | |

`_take_baseline` (`loop.py:1633-1660`) records only `swagger_check` violations. Nothing baselines `go_vet`, `go_test`, or tidy. So on the legacy corpus:

- Every coding task that makes any edit reaches `_verify`, fails `go_test` (Docker) or `go_vet` (pre-existing), and is told the failure is its own.
- The ladder (`loop.py:1662-1723`) then spends two Coder attempts and three Debugger cycles trying to fix a test suite that cannot run on this machine.
- The run ends `unverified`, and the developer reads "the gate did not come clean… blocked at go_test".

**No change to the loop, the prompts, or the model can produce a passing run on this corpus while `go_test` is blocking and Docker is not running.** That single fact accounts for most of the hours spent. The changelog's own "verified live" table records the outcomes honestly: `exhausted @40`, `unverified @38`, `exhausted @40` are listed as improvements over `no_progress @22`.

The gate is also invoked when nothing was written. `_verify` at `loop.py:1536` runs it whenever an executing mode ends a turn without a tool call, including the turn where the Coder says "there is nothing to do here". The invariant the prior analysis asked for, "a run that wrote nothing cannot fail", is still not in the code.

And the one stage that carries the product's promise is inert. `_lint_is_clean` (`gate.py:477-492`) parses `rules_lint` output as JSON, but that output has been rendered prose since `_render_lint` landed (`gotools.py:318-330`). The parse fails, the check falls back to `result.ok`, and `result.ok` is true whenever the sidecar ran. The blocking contract-lint stage can fail only on a sidecar crash, never on a contract violation. So the gate blocks on things the run did not cause and waves through the thing it was built to catch.

---

## 4. The loop: structural defects

`loop.py` is 2,749 lines. 860 of them (31%) are comments explaining a past incident. `_State` has 34 fields, almost all counters or ledgers with their own watermark. The file grew from 504 to 2,749 lines across 20 commits between 25 August and 2 September (+3,087 / −338). `docs/ARCHITECTURE.md` records 24 dated "field failure → new heuristic" entries in the same window. The pattern is the finding: each transcript produced a new detector, and no detector removed the cause of the last one.

| # | Defect | Evidence | Consequence |
|---|---|---|---|
| L1 | Every message starts in Planner with the Planner's tools, before anyone knows whether it is a question. | `loop.py:532,552`; `serve.py:105`; `loopback.py:145` | A greeting costs a full contract-laden prompt. A question is answered by a mode whose instruction is "emit numbered steps". |
| L2 | Intent is inferred from the reply's shape, then from the task's wording, with ~500 lines of regex (`_STEP`, `_PLAN_EDITS`, `_ACCEPTS`, `_REFUSES`, `_ASKS_TO_BE_TOLD`, `_COMMAND_LEAD`, `_WORK_VERB`, `_WORK_OBJECT`, `_CONJOINED_NOUNS`, `_SAYS_GO`). | `loop.py:2309-2737` | 17/24 misses above. The module's own comments admit "no regex over prose can separate them" (`loop.py:2721-2723`) and then add another regex. |
| L3 | Branch order: the `steps == 0` nudge runs before `_is_explanation`. | `loop.py:1415` vs `1461` | One wasted turn and a fabricated tool result on every unnumbered explanation. |
| L4 | The "plan" is whatever prose came back, pinned into the un-evictable task layer for the rest of the session. `follow_up` reuses the same context. | `loop.py:1483`; `context.py:549-612`; `loopback.py:229-237` | An explanation pinned as a plan is read as the plan by every later mode and every later message in that chat. |
| L5 | The full gate runs on an empty change set; the "nothing written" guard is only consulted when the gate passes. | `loop.py:1536-1547` | 70-second gate and an unrequested `go mod tidy` on a question. Pre-existing failures adopted as the run's problem. |
| L6 | Escalation charges an attempt only when a mutation landed; a Coder that never mutates is sent back to the Verifier indefinitely, and the run is actually ended by a text detector. | `loop.py:1697-1710`; `_narrating` at `1911` | The state machine's terminal condition is "the model said something idle three times", not a decision about the work. |
| L7 | 17 nudges are injected as `role: tool` messages attributed to tools that never ran (`repo_map`, `read_file`, `go_build`, `rules_lint`, `resource_scaffold`), with no `tool_call_id`. | `grep append_tool_result loop.py \| grep -v tool_call_id` | Malformed against a strict OpenAI-compatible endpoint (the codebase's own `Message.wire` docstring says so). Teaches the model that `go_build` returns paragraphs of instructions. |
| L8 | History is rewritten mid-run: `collapse`/`discard` delete earlier assistant and tool messages by identity; the slice ledger replaces read results with stubs. | `loop.py:962-978`; `context.py:690-721,767-815` | Invalidates the prefix cache the design says it protects; the model's memory of what it did no longer matches the transcript. |
| L9 | Mode overlays stack in the pinned head (up to 6) with a "this replaces the above" preamble, and the mode flips on almost every turn. | `context.py:614-670`; `error.txt` turns 24-38: c,v,c,c,v,c,c,v,d,d,v,d,v,d,v | The Verifier announces "My job is to make the edit"; the Coder says "I am in verifier mode". Documented in `context.py:640-646` and treated as a prompt problem. |
| L10 | No `tool_choice`, no structured output, no `parallel_tool_calls`. Narration without a tool call is counted and punished (`MAX_IDLE_EXECUTING = 3`). | `grep -r tool_choice apps/` → nothing | Nine narration turns in the 38-turn transcript. The standard fix, re-asking with `tool_choice: required`, is never attempted. |
| L11 | Tool availability contradicts the prompts. The Verifier has no write tool but the system prompt says "asked whether you can change code, say yes". The Coder has `patch_file` but `go_vet`/`go_test` belong to the Debugger only. The Planner truthfully says "I have no write tools" and `_refuses_to_plan` treats that as a refusal to nudge. | `registry.py:544-562`; `system.md:43-45`; `loop.py:1393-1414` | The model is told to lie about its tools, then penalised for telling the truth. The Coder cannot run the checks the gate will fail it on. |
| L12 | "go" is recognised by a regex over pinned directives; the code's own comment notes one false match authorises writes for every later question in the session. | `loop.py:2618-2642` | Session-scoped write authorisation from a one-word pattern match. |
| L13 | The Verifier's `max_tokens` is 2,048; a `write_file` of a 280-line file cannot fit, so the same call was truncated three times in a row with no counter. | `modes.py:127`; transcript turns 26-29 | Three wasted turns per occurrence. |
| L14 | Compaction never worked in production until 28 August (wrong role name caught by a broad `except`); `do_not_retry` was empty for the product's whole life. Then the budget was raised to 245,760 tokens so compaction rarely fires at all. | changelog 2026-08-28; `modes.py:116` | Long runs now accumulate a full-window prompt on a 27B model per turn. |

None of these is exotic. Together they mean the loop's transitions are driven by the *shape of prose*, its memory is edited behind the model's back, and its stop conditions are counts of things the model said. That is not a state machine; it is a heuristic that has been tuned to the last incident.

---

## 5. The extension hides every failure

From the audit of `extension/src`:

| # | Defect | Evidence | Effect |
|---|---|---|---|
| E1 | A crashed run never sends `finish`; the panel's `running` flag only clears on `finish`. The next message is treated as a mid-run correction, the server starts a new run via `follow_up`, and the extension never follows it. | `loopback.py:275-277`; `session-state.ts:304,870,609-614`; `extension.ts:393-401` | Any backend exception (gateway 401, timeout inside the loop) freezes the panel on "Working…" and silently eats the next message. `end.outcome`, which carries the answer, is ignored (`session-state.ts:893`). |
| E2 | Pre-flight failures return silently. Not signed in, sign-in cancelled, runtime failed to start: `submit` returns at `extension.ts:386`. Errors surface as toasts or log lines; `chatView.setOffline` and the offline card are never called. | `extension.ts:130-132,386,643-658`; `chat.ts:281` | From the panel, every failure is "your message, then nothing". |
| E3 | Four slash commands, including two of the four empty-state suggestions, dispatch to commands that do not exist and discard the rejection. `/explain <text>` opens a rule document instead of asking the model. | `chat.js:2002`; `extension.ts:500-513`; `diagnostics.ts:1064` | The suggested first actions do nothing. |
| E4 | A dead daemon retries forever: only `HttpError` is permanent; `fetch failed` backs off indefinitely while `running` is true. Child exit after announcement is ignored. | `session-state.ts:635-660`; `runtime.ts:187` | A spinner that never resolves. |
| E5 | No request timeouts, no in-flight guard. A second Enter during the first run starts a second conversation whose `showSession` wipes the first. First run pays venv creation plus offline pip install plus two 60-second windows with no progress UI. | `client.ts:85-98`; `runtime.ts:100-106,155,202,375-405`; `extension.ts:395-410` | The first-ever interaction can take minutes and show nothing. |
| E6 | Finished sessions replay stored `tool_pending` rows with live Accept/Reject buttons and then toast "released by the runtime… recorded as a rejection" five seconds later. Seven settings are read by nobody. `DAKCODER_MODE` is set and never read. | `chat.js:1310-1327`; `approvals.ts:660-668`; `package.json` | Phantom approvals; inert configuration. |

The transport and event contract between TypeScript and Python are actually consistent; the failure is in what happens on every path that is not the happy path.

---

## 6. Tools and transport

The tool layer is the best-built part of the Python. Schemas are real OpenAI function definitions (`registry.py:158-171`), arguments are validated and coerced (`router.py:748-824`), every exception becomes a `ToolResult` the model can read, and results go back as `role: tool` with the right `tool_call_id`. The defects are at the edges, and several of them produce exactly the "empty answer" the user sees.

| # | Defect | Evidence | Effect |
|---|---|---|---|
| T1 | **The blocking contract-lint stage cannot fail on findings.** `_lint_is_clean` does `json.loads(result.content)`, but `rules_lint` has rendered prose since `_render_lint` landed, so the decode fails and the check falls back to `result.ok`, which is `True` whenever the sidecar ran. | `gate.py:477-492`; `gotools.py:318-330` | The product's headline promise, "verified by a static template linter before a human sees the diff", is inert at the gate. Only a sidecar crash can fail `rules_lint`. |
| T2 | **A mid-stream gateway error becomes a silent empty answer.** The gateway emits `event: error` after headers are sent; the client skips non-`data:` frames and treats a `data:` object without `choices` as a no-op; with no prior content delta, `ChatResult("")` is returned. `_advance` then reads an empty reply as "no plan was needed" and ends `DONE`. The gateway's upstream timeout is 300 s against the agent's 600 s read timeout, so a long prefill is cut by the gateway and lands here. | `app.py:280-299`; `llm.py:562-605,641-642`; `proxy.py:141`; `config.py:66-68` | "Done · N turns" with nothing on screen. |
| T3 | **`EmptyCompletionError` is recovered only when thinking was on.** With thinking off everywhere, an empty completion propagates to `_turn`'s catch-all and ends the run `ERROR`. If the endpoint ignores `chat_template_kwargs`, reasoning eats the Verifier's 2,048-token budget and returns `content: null`, the exact case `modes.py:4-6` warns about. | `llm.py:434-435`; `loop.py:748-753` | Run error on a transport condition the design anticipated. |
| T4 | **The JWT is fixed at daemon spawn and never refreshed.** After expiry every call is a non-retryable 401. Combined with E1, the panel freezes on "Working…". | `llm.py:331`; `serve.py:173` | Every task errors until the runtime is restarted. |
| T5 | **The gateway fails closed on its quota store.** Redis unreachable → 503 on every request → three client retries → run `ERROR` after about six seconds. | `app.py:97-113`; `llm.py:53-58,465-495` | A cache outage reads as "the agent is broken". |
| T6 | **`gotools` discovery is fragile.** `GOTOOLS_PATH` is set by the extension only if found; the fallback walks `parents[5]` from the installed wheel, which lands in site-packages; the checkout's binaries are named `gotools-dev.exe` and `gotools-win32-x64.exe`, matching neither lookup. A missing sidecar surfaces as `repo_map failed: SidecarError: …` with no `fix`; the dedicated "sidecar is not installed" message is unreachable. `planner.md` tells the model to start every task with `repo_map`. | `gotools.py:99-111,161-177,264-288`; `router.py:462-485`; `runtime.ts:304` | On a machine where discovery misses, the first tool call of every task fails and the blocking `rules_lint` stage fails after every edit. |
| T7 | **A missing `go` binary is reported as a missing file** ("go does not exist. The closest files that do: …") and marked a dead end; `go_build` halts the gate. | `commands.py:134-136`; `router.py:462-470`; `gate.py:304-311` | Every run `unverified` with a message pointing at the wrong thing. |
| T8 | **A question mark ends a plan.** `_asks_the_developer` fires when no line *starts* with `Accepts:` and the reply contains two `?`. In `error.txt` turn 14 the Planner emitted eight numbered steps with `Accepts:` mid-paragraph, a query-string `?`, and "correct me if wrong" — the run ended `Done, 14 turns` with no plan pinned, and "go" re-entered a Planner with no write tools. | `loop.py:2405-2430`; `error.txt` turns 14-22 | The eight Planner turns after "go", "do it" and "you are not writing anything". |
| T9 | **Intent words are matched anywhere.** `_ASKS_TO_BE_TOLD` matches `compare`, `evaluate`, `review`, `inspect` in any position, so "Add pagination to the compare endpoint" and "Rename evaluate() to score()" are answered, never executed. | `loop.py:2506-2520` | Work silently downgraded to an answer. |
| T10 | **Schema edge cases.** `required` treats `""` as missing, so `patch_file(old=…, new="")` (a deletion) is refused; `git_diff.staged` is typed as the string `"true"`, so a boolean is refused. | `router.py:807,852-857`; `registry.py:629` | Deletions by patch are impossible; a common call fails on type. |
| T11 | **Token calibration compares unlike units.** `observe_usage` calibrates on message characters only, excluding tool schemas and `tool_calls` arguments, against a `prompt_tokens` that includes them. Estimates run high and are what `X-Estimated-Tokens` reserves against a 600k/hour quota; the panel still shows the retired 32,768 budget ("18k / 32.8k context" in `error.txt`). | `context.py:969-970`; `tokens.py:105`; `quota/model.py:107` | Over-reservation and possible 429s on long runs; a meter that lies. |
| T12 | **Resource leaks and swallowed errors.** The proxy builds a new `httpx.AsyncClient` per request and never closes it; `LLMClient` is built per run and never closed; `_take_baseline`, `_relay`, and `context.discard` swallow exceptions. | `proxy.py:328-332`; `serve.py:108`; `loop.py:1656,1766-1769` | Slow degradation and invisible faults. |

The gateway's design goal, one process holding the model credential with metering on every call, is sound and implemented. It is the coupling of that gateway to a 300-second upstream timeout, a fail-closed quota store, and a client that reads an in-band error as an empty success that turns ordinary infrastructure hiccups into "the agent returned nothing".

---

## 7. The model and the prompts

The deployment is one model for every role: `Qwen3.8-27B` behind LiteLLM and vLLM at `ai.cept.gov.in`, temperature 0.1, thinking off in every mode (D-34), `max_tokens` 2,048–6,144.

Three things follow.

- **Narration instead of tool calls is the dominant model-side failure**, and it is induced as much as inherent. In `error.txt`, nine turns across Coder, Verifier and Debugger read "Making the edit now" with no call. The transcript those turns were produced from contained six stacked mode overlays, fabricated `go_build` results carrying instructions, deleted history, and a Verifier overlay saying "do not fix anything" two messages above a Coder overlay saying "execute". A frontier model would struggle with that transcript; a 27B model at temperature 0.1 will pick whichever instruction it saw most recently.
- **The design compensates for the model with heuristics rather than with the API.** vLLM supports `tool_choice: "required"` and named tool choice, and guided JSON via `response_format`. A Planner asked to emit its plan through a `submit_plan` tool with a JSON schema, or a `finish_answer` tool for questions, removes every regex in §4 L2. A Coder that narrated once and is re-asked with `tool_choice: required` removes `_narrating`, `MAX_IDLE_EXECUTING`, and `EXECUTING_RESEARCH_*`. None of this is used.
- **The system prompt argues with the mode overlays.** "Your tools this turn are the phase, not the limit… say yes and name the phase that does it" (`system.md:43-45`) sits above a Verifier overlay with no write tool, a Planner overlay that says "never answer 'I cannot edit files'", and a Coder that is nonetheless told by `_wrong_mode` which mode does hold the tool. The model's most honest replies ("I have no write tools in this session") are the ones the loop punishes.

The spike that turned thinking off measured JSON emission latency, which was the right measurement for a Scaffolder spec. It was not a measurement of agentic tool-use reliability, which is where this model is now failing.

---

## 8. Why hours of "ultra high" workflows returned nothing

The workflows were pointed at transcripts, and transcripts show symptoms. Each review correctly diagnosed a symptom and produced a local fix inside the same architecture:

- 24 dated changelog entries in nine days, 20 commits to `loop.py`, +3,087 lines. Every fix is a new ledger, counter, regex, or nudge. The two-transcript analysis in `live-issue-analysis.md` lists fifteen fixes F1–F15 and its own status table shows most still open, because they are all patches to the ladder rather than removal of it.
- The right fix was identified and deferred. Decision D-93 in `docs/ARCHITECTURE.md`: "Fixing the classification properly means the Planner marking its own output, which is a prompt-contract change with a budget cost, and is worth doing deliberately rather than as part of a bug fix." It was never done; four more regexes were.
- The improvement signal was unobservable. With `go_test` blocking on a Docker-dependent suite, no loop fix could produce a green run on the legacy corpus. Reviews measured "turns before it died" and "files touched" instead, and runs ending `exhausted @40` were recorded as wins.
- Reviewers read stale code. The 28 August entry records that a 38-agent fan-out read `apps/*/build/` copies "differing from `src/`" and "reported defects against a file nobody ships".
- The tests cannot fail. 776 green tests drive a `ScriptedClient` (`tests/test_loop.py:37`). The suite proves the heuristics respond to the scripted replies they were written for. It has never once been failed by the model, the gate on a real repository, or the classifier on a phrasing nobody had thought of.
- The architecture documents itself as complete. `docs/ARCHITECTURE.md §1` lists every component as **built**, and the README promises a change "verified by the compiler and a static template linter before a human sees the diff". A reviewer given that framing looks for bugs, not for a design that cannot work.

The prior analysis closed with "the model was not the problem". That is correct and it is the point: the model diagnosed the tab character on its first Verifier turn and was prevented from fixing it by the loop, then blamed for the loop's gate.

---

## 9. What mature agents do instead (research summary)

Full brief with sources in the appendix. The short version:

- **One loop, model decides per turn.** Claude Code, Copilot agent mode, Cursor, Cline and Kilo all run a single agentic loop with `tool_choice: auto`; the model calls tools or answers. None runs a fixed planner → coder → verifier pipeline per request. Claude Code's docs: "A question about your codebase might only need context gathering. A bug fix cycles through all three phases repeatedly." ([how Claude Code works](https://code.claude.com/docs/en/how-claude-code-works))
- **No mature agent classifies intent with keywords.** Where a mode exists it is a user toggle (Cline Plan/Act, Cursor Ask/Agent, Copilot Plan agent) implemented as a tool allow-list, not a prompt-time regex. Anthropic recommends a routing step only "where classification can be handled accurately" and then via a cheap structured-output call. ([Building effective agents](https://www.anthropic.com/engineering/building-effective-agents))
- **Verification is a deterministic tool inside the same loop**, plus an optional fresh-context reviewer. Build, vet, lint, and test are tools the model calls and reads; a Stop hook can refuse to end the turn until they pass. A separate LLM "verifier" phase that cannot edit is not how anyone ships. ([Claude Code best practices](https://code.claude.com/docs/en/best-practices))
- **Structured output and strict tools replace prose parsing.** Plans, verdicts and classifications come back as schema-validated JSON. ([strict tool use](https://platform.claude.com/docs/en/agents-and-tools/tool-use/strict-tool-use), [structured outputs](https://platform.claude.com/docs/en/build-with-claude/structured-outputs))
- **VS Code will give you the chat UI, streaming, references and confirmations** through the Chat Participant API and the Language Model Tools API; you may call your own backend. MCP is GA in VS Code and is the right way to expose `gotools` to Copilot, Claude Code, Cursor and any SDK at once. ([Chat Participant API](https://code.visualstudio.com/api/extension-guides/ai/chat), [LM Tools](https://code.visualstudio.com/api/extension-guides/ai/tools), [MCP in VS Code](https://code.visualstudio.com/api/extension-guides/ai/mcp))
- **Frameworks that fit a coding agent with approvals**: Claude Agent SDK (Claude Code's loop as a library: built-in file/shell tools, permission callback, compaction, sessions, in-process MCP tools), Pydantic AI (lightest provider-neutral option with deferred tool approval), OpenAI Agents SDK, LangGraph. CrewAI and AutoGen are the wrong shape; Roo Code is archived; Continue is read-only.

---

## 10. Recommendation

There are two honest paths. Both keep the parts of this repository that are genuinely good: the `gotools` sidecar (30 rules, scaffolders, an MCP server that already exists), the knowledge base, the gateway's auth, quota and ledger, and the deterministic gate stages as *tools*.

### Track A — stabilise what exists (about a week, if you must ship this loop)

1. **Never run the gate on an empty change set, and never block on stages the machine cannot run.** Skip `_verify` entirely when `router.mutations == 0`. Make `go_test` advisory unless Docker is reachable (or scope it to touched packages with `-run`). Baseline `go_vet`, `go_build` and `swagger_check` at run start and report only *new* findings. This alone lets a run finish green on the legacy corpus.
2. **Decide intent before the first turn, not after.** One cheap model call with a JSON schema (`{"kind": "question" | "change" | "unclear"}`) or, simpler, an explicit Ask / Agent toggle in the panel. Delete `_ASKS_TO_BE_TOLD`, `_COMMAND_LEAD`, `_WORK_VERB`, `_WORK_OBJECT`, `_CONJOINED_NOUNS`, `_SAYS_GO`, `_is_explanation`, `_is_read_only_task`. A question runs one read-only loop and stops when the model stops calling tools.
3. **Make the plan a tool call.** Give the Planner `submit_plan(steps: [{file, action, accepts}])` and `ask_developer(questions)`. Delete `_STEP`, `_count_steps`, `_PLAN_EDITS`, `_ACCEPTS`, `_asks_the_developer`, `_refuses_to_plan`, `_restated_the_plan`. Transitions then come from a typed event, never from prose.
4. **Force the tool call when the mode requires one.** After a narration turn in an executing mode, re-issue with `tool_choice: "required"`. Delete `_narrating`, `MAX_IDLE_EXECUTING`, `EXECUTING_RESEARCH_NUDGE/LIMIT`, `PLANNER_RESEARCH_NUDGE/LIMIT`.
5. **Collapse Coder, Verifier and Debugger into one executing mode with every tool**, run the gate as a deterministic step after each edit batch, and put the gate summary in as a normal user message. Delete the ladder, `attempts`, `cycles`, `blocked_stage`, `route_mutations`, and the six-deep overlay stack.
6. **Stop fabricating tool results and stop deleting history.** Nudges go in as `role: user`. Repeats are answered from cache without removing earlier messages. Keep the slice-stub behaviour only if you keep the 32k budget; at 245k it has no purpose.
7. **Fix the extension's failure paths.** Treat `end` without `finish` as finished. Render every error into the panel. Remove the four dead slash commands. Add request timeouts and an in-flight guard.

That gets you a loop that works on the happy path with the current model. It still carries a bespoke agent loop that no one else will maintain.

### Track B — rebuild on standard parts (recommended)

```
VS Code  ──@dakcoder chat participant / LM tools──►  one agentic loop  ──►  gateway (quota, audit)  ──►  model
                                                          │
                              MCP: gotools (lint, audits, scaffold, swagger, fx_wire)
                              MCP/tool: knowledge base (search_docs, playbook)
                              tools: read/edit/patch/search/bash, go build/vet/test as tools
                              Stop hook: the gate, deterministic, refuses to finish red
```

- **Loop**: if Anthropic models are permitted on your gateway, the Claude Agent SDK gives you the loop, built-in file and shell tools, permission callbacks (`plan` mode is your read-only toggle), compaction, sessions and in-process MCP tools, and it runs as the same Python sidecar you already spawn. If the model must stay Qwen, use Pydantic AI or a 200-line tool-runner on your existing `LLMClient` with `tool_choice` and JSON-schema output; the point is one loop, typed transitions, no regex.
- **Tools**: `gotools mcp` already exists. Register it in `.vscode/mcp.json` and every agent in the building can use it today, including Copilot agent mode and Claude Code, with no custom extension at all.
- **Standards**: the knowledge base's contract rules belong in `AGENTS.md` / `.github/instructions/*.instructions.md` / `CLAUDE.md`, where every agent reads them, rather than behind a BM25 search the model has to know to call.
- **UI**: a Chat Participant (`@dakcoder`) gets streaming markdown, references, buttons, progress and confirmations from VS Code. The 15,000 lines of `extension/src` shrink to the participant, the gateway sign-in, and the MCP registration.
- **Gate**: keep every stage, expose each as a tool with an instructive error result, and add one deterministic Stop hook that runs the scoped, baselined set and refuses to end the turn while it is red. That is the "cannot be talked around" property the design wanted, without a Verifier persona.
- **Delete**: `loop.py`'s heuristics, the five modes, the regex classifier, the 34 counters, the fake tool results, the history rewriting, the escalation ladder.

What you would keep is about a third of the Python and all of the Go. What you would lose is the part that has consumed the last nine days.

---

## Appendix A — measurements

| Measurement | Value |
|---|---|
| `loop.py` lines / comment lines | 2,749 / 860 (31%) |
| `_State` fields | 34 |
| Fabricated `role: tool` messages (no `tool_call_id`) | 17 call sites |
| Uses of `tool_choice`, `response_format`, `parallel_tool_calls` in `apps/` | 0 |
| Commits touching `loop.py`, 25 Aug – 2 Sep | 20 (+3,087 / −338) |
| Dated changelog entries, 26 Aug – 1 Sep | 24 |
| Python tests | 776 passed, 26 skipped, 0 failed (model scripted) |
| Read-only prompts misclassified as work | 17 / 24 |
| Work prompts misclassified as read-only | 0 / 16 |
| Transcript 1 (`live-issue-reported.md`) | 32 turns, 1 file changed (`go.mod`, by the gate), `go_test` 67.62 s |
| Transcript 2 (`error.txt`) | 38 turns, 1 file, `unverified`; one file read 8× in 9 turns; user typed "go", "what happened?", "do it", "you are not writing anything" |
| Mode switches, transcript 2 turns 24–38 | 14 in 15 turns |

## Appendix B — research sources

See §9. Primary sources used: Claude Code docs (agent loop, permissions, best practices, VS Code), Anthropic engineering (building effective agents, long-running harnesses), Anthropic API docs (tool use, strict tools, structured outputs, prompt caching, streaming), VS Code extension guides (chat participants, language model tools, MCP, custom instructions, custom agents), OpenAI Agents SDK, Google ADK, LangChain HITL, Pydantic AI deferred tools, Cline, Cursor, Continue, Kilo, Roo repositories.
