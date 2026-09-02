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
