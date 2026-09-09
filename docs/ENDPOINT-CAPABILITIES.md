# What the endpoint actually does

Measured against `https://ai.cept.gov.in/dakcoder` (Qwen3.8-27B behind LiteLLM
behind vLLM) on 2026-09-02. Every row is a live result, not a reading of the
docs.

This file exists because three fixes were shipped on inference about this table
and all three were wrong. If you are about to change how the loop talks to the
model, re-run `DAKCODER_LIVE=1 pytest apps/agent/tests/test_live_endpoint.py`
before you write the code, not after.

## Request parameters

| parameter | verdict | notes |
|---|---|---|
| `tools` | works | schemas as sent; ids are opaque, never parse them |
| `tool_choice` omitted / `"auto"` | works | id shape `call_<hex>` — vLLM's tool *parser* |
| `tool_choice: "required"` | works | forces a call even when the prompt says not to |
| `tool_choice: {name: X}` | works | **the only reliable way to make it stop** — see below |
| `tool_choice: "none"` | **broken** | returns 0 tool calls and puts `<tool_call>` markup in `content` |
| `tools: []` | **broken** | same leak, and it invents tools from training — we saw `<function=Grep>` with `output_mode`, which is Claude Code's signature |
| `response_format` json_schema | works | intent classifier scored 3/3 including the bare `"go"` |
| parallel tool calls | works | 2 in one reply, unprompted |
| `chat_template_kwargs.enable_thinking` | works | thinking off, 0 reasoning tokens |
| `stream_options.include_usage` | works | usage chunk arrives last |
| `reasoning_effort` | rejected | `drop_params` is off, so it 400s — as designed |
| `prompt_tokens_details.cached_tokens` | absent | prefix-cache hit rate is not measurable here |

### Two id shapes, and what they tell you

`auto` returns `call_<hex>`; `required` and named choice return
`chatcmpl-tool-<hex>`. Those are two different code paths inside vLLM — the tool
*parser* for the first, *guided decoding* for the second. Guided decoding also
returns arguments with odd whitespace (`{\n\n"pattern": "x"\n\n}`), which parses
fine and must not be mistaken for a truncated call.

That difference is also the explanation for the `"none"` failure: it disables
the parser while leaving the tool schemas in the prompt template, so the model
emits its native markup and nothing is listening.

## The behaviour that shaped the loop

**This model cannot reliably produce a non-action.** Replaying a real transcript
at increasing depth, 5 samples each:

| consecutive fruitless tool calls in history | what it does next |
|---|---|
| 2 | 5/5 sensible (widens the search) |
| 5 | 5/5 sensible |
| **6** | **4–5 of 5 repeat its last call verbatim** |
| 8 | 5/5 repeats |

A cliff, not a slope — the change between five and six is sharp and repeatable.
The depth-6 rate itself is **not** deterministic: measured 5/5 in one session and
4/5 in another. Treat it as "usually", and do not build anything that needs it to
be "always".

At depth 6, with the original wording:

| what was tried at depth 6 | result |
|---|---|
| the tool's message as it was | 5/5 loop |
| the message without the directory listing | 5/5 loop |
| a message naming the glob and dropping the false claim | 5/5 loop |
| a message saying *"do not search for it again"* in plain words | 5/5 loop, **then 0/5 in a later session** |
| offering a `finish` tool, model's choice | 5/5 loop |
| a user message "stop, answer now" (no `finish` tool) | 0/5 loop — but it keeps acting |
| **user message + a `finish` tool** | **5/5 called finish** |
| **`tool_choice: {name: "finish"}`** | **5/5 called finish, in every session** |

That fourth row is the honest one and it corrects an earlier claim in this file.
Better wording *does* help — 5/5 correct against 3/5 at the first step after a
zero-file answer — and it cannot be depended on. Only the forced named choice has
been 5/5 every time it has been measured, which is why the loop forces rather
than argues, and why the wording fix is still worth having as the thing that
keeps runs out of the trap.

Read the last three rows together, because they are the whole design:

> The instruction alone gives it a *reason* to stop and no *move* that means
> stopping. The tool alone gives it a move and no reason. Together it works
> every time.

Which is why `ask` and `agent` have a `finish` tool at all, and why the stall
recovery is a named `tool_choice` rather than any form of tool suppression. See
`tools/control.py` and `loop._terminal_choice`.

**And why `finish` is refused once when it abandons the plan.** Giving the acting
mode a terminal tool fixed the loop and opened a new failure immediately: two
runs in three then called `finish` on their *first* acting turn — "I have
gathered all the necessary details to write the migration plan" — having written
nothing. Finishing had become the easiest move in the room. `loop._phase_ended`
sends the first such call back naming the unwritten files; a second is believed,
because the model may legitimately have decided against a step.

## Forcing the end of a phase: which text, not whether

Measured 2026-09-09, after a field run turned "validate this migration plan"
into an eight-step migration nobody asked for.

The fence used to force `submit_plan` by name, so a Planner that had spent
twelve turns *validating* a document had one legal move. The fix offers the
three terminals with `tool_choice: "required"` and a narrowed tool list. That
alone did nothing: **the text decides it.** 10 samples per cell, matched
transcripts, `role=planner` at temperature 0.1.

| fence text | validation task → `finish` | change task → `submit_plan` |
|---|---|---|
| lists the three calls and what each is for | **0/10** | 10/10 |
| poses it as one question ("does answering require changing a file?") | **0/10** | 10/10 |
| **tells the model to re-read what the developer asked for** | **9/10** | **10/10** |

The Planner overlay says "plan the work"; a closing paragraph that merely
*offers* an alternative does not outweigh it. Naming the verbs the developer
used — validate, review, audit, check, compare, explain, list — and pointing
back at their own message does. That is the text `loop._fence_ask` ships, and
`test_live_endpoint.py` measures both cells so a drift shows up in the log.

**What to do about a plan the fence extracted was measured three ways.** The
loop cannot tell a question from a job once it is in the acting phase, so all
three are guesses; these are the rates, on the field scenario and on a change
task, with the fence lowered to 4 so it fires on a nine-file fixture.

| `plan_forced` excuses... | validation answered, nothing written | change task wrote the code |
|---|---|---|
| **all three enforcement paths** (shipped) | **11/15** | **14/17** |
| the `_verify` verdict only | **0/5\*** | 5/5 |
| all three, plus a "reconsider" message before the first write | 4/5 | **2/4** |

\* 0/5 measured; every run was pushed into writing files for a request to
*validate* a document, and once anything is written the guard correctly lapses,
so all five then committed to the migration and exhausted their budget.

The middle row was shipped briefly on the reasoning that the fence's "write
them now" and `_phase_ended`'s "not yet" are *recoverable* pushes — each names
`finish` as the way to decline — so only the verdict needed excusing. That
reasoning is wrong in practice: the pushes are what turn a validation into a
migration, and being able to decline is not the same as declining.

The third row is the more interesting failure. Asking the model to reconsider
before its first write reads as an invitation to stop, and change tasks are the
common case. It bought one validation run in five and cost two change runs in
four.

What the shipped row does **not** do is stop a model that works a forced plan
on its own initiative — nothing is pushing it there, and roughly one validation
run in four still goes that way. That is the misroute itself, and it belongs to
the intent classifier: the fix is not being in the acting phase for a question.

## The intent classifier reads the verb, not the subject

Measured 2026-09-09 on 60 labelled requests, 3 samples each, `role=fast`. Six
of the cases are verbatim from the two field runs; the rest are the boundaries
those runs exposed.

| prompt | score |
|---|---|
| shipped until now: enumerates the *subjects* of a change | **44/60** |
| names the interrogative verbs explicitly | 60/60 |
| names them, plus six worked examples | 60/60 |
| **states the test instead of a vocabulary** (shipped) | **60/60** |

The old prompt read *"change -- a feature, a fix, a migration, a refactor"*, so
"validate the migration plan" matched on the noun and the verb was never
consulted. All four of its misses were that shape: `validate`, `audit`,
`compare`, and "what would we need to change to drop gin?".

All three rewrites also handled eight verbs that none of them named -- diagnose,
investigate, trace, critique, sanity-check, evaluate, map out, figure out -- so
the enumerating ones were not winning by their enumeration. The shipped prompt
asks one question instead: **after a perfect reply, is any file different?**
That is the decision the loop is actually making, and it puts the boundary in
the right place for free: writing findings into `AUDIT.md` is a change, however
analytical the request sounds.

**`why` had to be made required, and then made short.** The field was in
`_INTENT_SCHEMA` from the start, optional, and the model omitted it on every
classification -- so the one artefact that could explain a misroute never
existed. Requiring it cost 2 cases in 60: left alone the model writes about
forty words, the reply lands on the token boundary, the JSON is cut, and an
unparseable classification falls back to ASK -- the safe direction, but silently
wrong on a change. Asking for eight words *in the prompt* fixed it (`why` went
from 117 characters average, pinned at its `maxLength`, to 35 average and 48
worst case) and the score returned to 60/60. The `maxLength` on the schema was
not what did it: guided decoding here does not enforce it.

## The answer channel: completeness is a specification, not a shape

Measured 2026-09-09 on the validation task, 3 runs per variant. Scored by
coverage rather than length -- the fixture's plan makes six checkable claims
about the service, and an answer worth reading names them; length alone rewards
padding.

| ASK contract | coverage of `answer` | coverage of everything shown | answer |
|---|---|---|---|
| what shipped: "put it in `finish`, in full" | 4.0/6 | 4.0/6 | 1,131 ch |
| findings accumulate as prose, `finish` summarises | **3.7/6** | 4.3/6 | 1,267 ch |
| **"in full" spelled out** (shipped) | **5.3/6** | **5.3/6** | 2,441 ch |

The middle row was on the roadmap twice as "the real fix for the single-string
channel", on the reasoning that twenty turns of work funnelled through one JSON
string is the wrong shape for an analysis. It measured **worse than the status
quo**. Told the detail was already above it, the model wrote a summary of a
summary: one of its three runs produced a 318-character `finish` that
`_is_preamble` caught, with none of the six claims in it.

The channel was not the problem. What the old text never said is what "in full"
means for a review -- that the developer sees no tool calls, so anything outside
`answer` reaches nobody, and that the things which turned out fine still have to
be reported by name. Saying that is worth 1.3 claims per answer and costs about
40 tokens of overlay.

The overlay budget is 250 tokens and the first draft of this came to 310. The
paragraph was merged into the one it was elaborating rather than the budget
being raised to fit it; the shipped text is exactly 250, and was re-measured
after the tightening rather than assumed to have survived it:

| | coverage of `answer` | answer length | turns |
|---|---|---|---|
| baseline, 4 runs | 2.0/6 | 55 – 24,000 ch | 8.0 |
| **shipped, 4 runs** | **4.8/6** | 724 – 2,418 ch | 6.0 |

The length column is the more interesting one. Two of the four baseline runs
hit the 24,000-character cap and still scored 2/6 and 6/6 -- an instruction to
answer "in full", with no account of what full means, is as easily satisfied by
padding as by checking. The shipped text produces shorter answers that cover
more, in two fewer turns.

## Cost, on `pao-back-end-development`

| what | time |
|---|---|
| full gate, scoped to 2 touched files | 9.1 s |
| run-start baseline | 6.4 s (was 80.1 s — `go_test` alone was 74 of them) |
| inner loop after an edit batch | 0.7 s |
| `go_build` cold | 16.8 s |
| `go_build` warm | 3.4 s |
| `go_vet` scoped | 1.0 s |
| `rules_lint` scoped | 0.3 s |

The gate was never the slow part. What made the verifier *look* slow was the
inner loop appending ~1,000 tokens of findings after every edit — 98% of them in
files the run had never opened — which the model then set about fixing.

## Environment gotchas

- **`shutil.which("docker")` is not a Docker check.** Docker Desktop leaves the
  binary on PATH with the daemon stopped, which is the ordinary state of a
  laptop. `docker info` is the check; `gate._container_runtime` caches it.
- **The `deploy/` JWT expires.** 12 hours by default. The gateway's 401 is clean
  and non-retryable (`"the token has expired; refresh it"`), and the extension
  pushes a fresh one to `POST /v1/credential` on every task. Mint one on the
  host: `deploy/gateway_main.py --mint dev:<user> --mint-hours 12`.

## Re-running this

```bash
export DAKCODER_JWT=$(grep '^DAKCODER_JWT=' deploy/dakcoder.env | cut -d= -f2-)
export DAKCODER_LIVE=1
python -m pytest apps/agent/tests/test_live_endpoint.py -v -s
```

It is skipped without `DAKCODER_LIVE=1`, so it never runs in CI and never gates
a commit. It costs about thirty small completions.
