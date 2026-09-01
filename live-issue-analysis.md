# dakcoder run failures: two transcripts, root causes and remediation

Analysis of the transcript in `live-issue-reported.md`, checked against the code
in `apps/agent/src/dakcoder_agent/`. **Line numbers are against the working tree at `loop.py` 2293 lines; Part III records what the update changed.**

---

## 1. Summary

The attached analysis is right about the *shape* of the failure — the run
understood the fix repeatedly and never executed it — and wrong about the cause.
It proposes a role/state desynchronisation between display and execution. There
is no such desynchronisation. The loop has exactly one authoritative mode
(`_State.mode`), the turn label and the dispatch both read it, and tool
permissions are already enforced in the router rather than in prompts.

The real defect is one line:

```python
# loop.py:2023
def _fingerprint(call: ToolCall) -> str:
    ...
    return f"{call.name}:" + json.dumps(kept, sort_keys=True, separators=(",", ":"))
```

The repeat-suppression ledger is keyed on **tool name and arguments only — not
mode** — and at `loop.py:1105` it stores *every* outcome, including failures:

```python
self.state.last_results[fingerprint] = outcome.for_model()[:6000]
```

So a mode-refusal produced in one mode is replayed as the answer in another.
The Verifier's "you are not allowed to patch" was cached, and the Coder was then
handed it, forever, as the result of its own patch attempt.

Everything else in the transcript follows from that.

---

## 2. The causal chain, turn by turn

| Turn | Mode | What actually happened |
|---|---|---|
| 13 | verifier | `patch_file(core/domain/transferentry.go, …)` → `router.dispatch` → `_wrong_mode` → `ToolResult.failure("patch_file is not available in verifier mode (it belongs to coder, debugger)", fix="Instead, use write_file…")`. **Cached** under fingerprint `F` at `loop.py:1105`. No mutation, so the ledger is never cleared (`loop.py:850`). |
| 14 | verifier | Correct report. `_advance` → `_route_failure` → `_switch(CODER)`. |
| 15 | **coder** | Model emits the *same* `patch_file` call. Fingerprint `F` hits `last_results` at `loop.py:967`. **The call is never dispatched.** The Coder is handed the Verifier's refusal text as its own tool result, wrapped in "— that is the current answer." |
| 16 | coder | The model reads that result and says, accurately from its own context, *"I'm in verifier mode, so I cannot apply the fix."* No tool call → `_advance` → `_verify`. |
| — | | `_verify` finds `(router.mutations, router.touched)` unchanged → returns the cached gate → `_switch(VERIFIER)`. |
| 17–19 | verifier | `read_file` **dispatches** (new fingerprint) → `stalled_turns` resets to 0 at `loop.py:1167`. This is why `MAX_STALLED_TURNS = 6` never fires. |
| 20 | coder | Same `patch_file`, same intercept. The UI even prints the giveaway: *"patch_file asked again with the same arguments; answered from the previous result."* |
| 26–28 | verifier | The model follows the cached `fix:` string — *"Instead, use write_file"* — and tries to rewrite the 280-line file. Verifier's `max_tokens` is **2048** (`modes.py`), so the call is truncated mid-arguments three times. |
| 29 | verifier | Truncation leaves `write_file {}`; dispatched, refused by mode. |
| 32 | coder | `_narrating` finally reaches `MAX_IDLE_EXECUTING = 3` and ends the run. |

**The false "✓ ok" is the same bug.** The intercept branch emits
`{"ok": True, "content": "patch_file asked again…"}` at `loop.py:1007`, so the
extension renders a green tick and `ok · 1 line` for a call that never ran. That
is why the transcript shows four successful patches against a file that never
changed.

**`patch_file` itself is not implicated.** `fs.py:266-287` counts occurrences,
refuses zero and refuses more-than-one, and writes before returning
`ToolResult.success`. It cannot report ok without having written. The "success"
came entirely from the cache echo.

---

## 3. Point-by-point on the attached analysis

### Correct

- **§5 — "coder spent turns describing an edit" fires far too late.** True.
  `_narrating` (`loop.py:1684`) is only reached on turns that call *no* tool
  (`loop.py:783` returns early when `tool_calls` is non-empty), so the four
  intercepted `patch_file` turns never incremented `state.idle`. Only the four
  pure-prose turns counted, and it took until turn 32 to reach 3.
- **§10 — no strong progress metric.** Partly. `stalled_turns` exists and is the
  right idea, but any dispatched call resets it, so one `read_file` per cycle
  keeps a dead run alive indefinitely.
- **§13 — context pollution.** Real, though `collapse()` (`loop.py:873`) already
  supersedes repeated intercept pairs. What it cannot remove is the *prose* —
  four near-identical Verifier reports, each a different paraphrase, which is
  precisely why `_repeating`'s `echoes` ledger (keyed on exact normalised text,
  `MODE_ECHO_LIMIT = 2`) never matched.
- **§6 — read-only tasks entering the coding pipeline.** Real. `_advance` does
  guard it (`steps == 0` → `Outcome.DONE`), and it worked at turn 5. It failed at
  turn 10 because `_count_steps` is a regex over prose and an explanatory answer
  with a numbered list is indistinguishable from a plan.

### Incorrect

- **§1/§4 — role/state desync.** There is none. `TURN_START` emits
  `str(self.state.mode)` (`loop.py:578`) and `_tool_calls` dispatches with
  `mode=self.state.mode` (`loop.py:1059`). Both read one field. The label
  "Turn 31 · coder" was accurate; the model's claim to be in verifier mode came
  from poisoned tool output, not from a divergent prompt or tool registry.
- **§3/§8 — tool permissions only enforced by prompt.** They are enforced twice
  already: `schemas_for(mode)` (`router.py:183`) never offers `patch_file` to the
  Verifier, and `dispatch` refuses it outright at `router.py:231`. The
  `ROLE_TOOLS` table the report asks for already exists as `ToolSpec.modes` in
  `registry.py`.
- **§9 — the LLM controls transitions.** It does not. `_advance`, `_verify` and
  `_route_failure` are a deterministic table; the model has no way to request a
  mode.
- **§11 — tool success ≠ mutation.** For this codebase, tool success *is*
  mutation. `ToolResult` already carries `mutations`, and `router.mutations` /
  `router.touched` are what the gate keys on. The observation is right only
  because the "success" was fabricated by the cache.
- **§2 — "your agent retries failed operations."** The opposite: it refuses to
  retry, and that refusal is the bug. A mode-refusal is exactly the case where a
  retry in a *different mode* would have succeeded, and the code even says so —
  `router.py:310` notes that a mode-hidden tool is "deliberately NOT a dead end —
  a mode switch can make it callable". The `dead_ends` ledger honours that.
  `last_results` does not.

---

## 4. Fixes

### F1 — Mode-scope the ledger, and never cache a permission refusal (P0)

Two changes, either of which alone stops this run; both are worth making.

`loop.py:2023` — put the dispatching mode in the key so a refusal earned in one
mode cannot answer a call made in another:

```python
def _fingerprint(call: ToolCall, mode: Mode | None = None) -> str:
    ...
    body = f"{call.name}:" + json.dumps(kept, sort_keys=True, separators=(",", ":"))
    return f"{mode}|{body}" if mode else body
```

Call sites: `loop.py:873` (`sole_fingerprint` / `collapse`) and `loop.py:899` in
`_tool_calls`. Note the trade: mode-scoping alone means a genuine repeat survives
a mode switch, which is mild waste, not a loop.

`loop.py:1105` — do not cache an outcome the model can make succeed by doing
nothing but wait for the loop to switch modes:

```python
if outcome.ok or not outcome.meta.get("refused_by_mode"):
    self.state.last_results[fingerprint] = outcome.for_model()[:6000]
```

with `_wrong_mode` (`router.py:348`) tagging its failure
`meta={"refused_by_mode": True}`. The same tag should suppress the `fix:` string
being replayed cross-mode — see F4.

**Test to add** (`tests/test_loop.py`): drive a Verifier turn calling
`patch_file`, switch to Coder, issue the identical call, and assert
`router.dispatch` was reached and a mutation landed.

### F2 — An intercept must not report `ok: true` (P0)

`loop.py:1007` and `loop.py:936`. The event is what the extension draws, and a
green tick on a call that did not run is the single most misleading thing in the
transcript. Emit `"ok": True, "intercepted": True` and have the extension render
it as a distinct state (e.g. `↺ answered from cache`), or emit `ok: false` with
the reason. The tick is currently indistinguishable from a real patch.

### F3 — Count an intercept-only turn as an idle turn (P1)

`loop.py:783` returns before `_narrating()` whenever the reply contained tool
calls. But a turn in which *every* call was intercepted did nothing —
`_tool_calls` already computes exactly this as `dispatched`. Hoist that:

```python
if result.chat.tool_calls:
    ...
    yield from self._tool_calls(result.chat.tool_calls, assistant_msg)
    if self.state.stalled_turns == 0:
        return
    # nothing dispatched: this turn is as idle as a prose turn
if why := self._narrating():
    ...
```

With F3 alone the run in the transcript ends around turn 20 instead of 32.

### F4 — A mode refusal must not hand out the other mode's tool as advice (P1)

`router.py:356` — when `patch_file` is refused in Verifier mode, `spec.instead`
resolves to *"use write_file to create a file that does not exist yet"*. That is
what sent turns 26–28 into the output limit. For a `mutates` tool refused by
mode, the only correct advice is the one already in the `elif` branch below it:
*"Describe the change you want; a later step in the run will make it."* Check
`spec.mutates` **before** `spec.instead`.

### F5 — Verifier `max_tokens` cannot hold a `write_file` (P1)

`modes.py` gives the Verifier 2048 output tokens. That is right for a mode that
reports, and it is why turns 26–28 truncated. Given F4 removes the reason to
reach for `write_file` there, no change to the budget is needed — but
`incomplete_tool_calls()` handling at `loop.py:750` should count repeats: three
identical truncations in a row is a loop, and it currently costs three turns with
no counter behind it.

### F6 — Fingerprint the *cycle*, not just the call (P1)

The attached report's `LOOP_DETECTED` idea is the right addition, and the state to
hash already exists:

```python
cycle = (
    self.state.blocked_stage,
    self.router.mutations,
    tuple(self.router.touched),
    self.state.mode,
)
```

A repeat of `(blocked_stage, mutations, touched)` across a full
`CODER → VERIFIER → CODER` cycle with `mutations` unchanged means the ladder is
turning without work. Two such cycles should end the run with a message naming
the file and the fix — which is far more useful to the developer than
`Stopped — no progress`. `_verify`'s cached-gate branch (`loop.py:1334`) already
detects half of this; it just does not count.

### F7 — An explanation handed to the Coder (P0)

**This is what started the incident**, and it is more serious than a wasted
handoff. Turns 11–32 — 22 of the run's 32 turns — exist only because a read-only
answer was routed into the writing ladder.

#### What happened

`explain the bootstrapper and tell me how it deviates from the new template` is a
read-only question. The Planner answered it correctly, in numbered prose. Then:

1. `_count_steps` (`loop.py:2089`) counts anything matching `_STEP` — a line
   beginning `1.`, `2)`, `## 3.`, `**4.**` or `Step 5:`. A numbered explanation
   matches perfectly. It returned 10.
2. `_asks_the_developer` did not fire: the answer contained fewer than
   `MIN_QUESTIONS = 2` question marks.
3. `_refuses_to_plan` did not fire: the answer refused nothing.
4. So `_advance` (`loop.py:1318`) called `set_plan(text)` — pinning an
   *explanation* into the task layer that compaction may not evict — emitted
   `PLAN · 10 steps`, and `_switch(Mode.CODER)`.
5. Turn 11, the Coder said the only true thing available: *"no code change was
   requested, so there is nothing to plan or edit."* It called no tool, so
   `_advance` → `_verify`.

#### Why that was catastrophic rather than merely wasteful

`_verify` ran the **full gate on a workspace nothing had touched**. Five of its
stages are unscoped — `go_build`, `go_vet`, `go_test`, `go_mod tidy`,
`golangci_lint` all take `lambda ctx: {}` (`gate.py:303-360`) and run over the
whole module regardless of the change set. Only `rules_lint` and `swagger_check`
are scoped by `_scoped` / `_scoped_with_baseline`, and `_take_baseline`
(`loop.py:1470`) baselines **only** `swagger_check` violations.

So the gate found a pre-existing `go_vet` failure — the malformed struct tag in
`core/domain/transferentry.go`, a file this task never mentioned — and reported
it as this run's blocker. From that moment the run was committed to fixing a
defect the developer had not asked about, in a mode that could not edit, with the
ledger bug (F1) guaranteeing it never would.

`_verify` has a guard for exactly this shape, and it is on the wrong branch:

```python
if report.ok:
    if unstarted := self._unstarted_work():   # loop.py:1384
        ...
self.context.append_tool_result("go_build", report.summary())
self._switch(Mode.VERIFIER)
```

`_unstarted_work` — "the gate came back clean and that is not worth anything yet:
no file has been written this run" — is consulted **only when the gate passes**.
A failing gate on an empty change set falls straight through to the escalation
ladder with no check at all.

#### Three fixes, in order

**F7a — require an `Accepts:` line to count a step.** The Planner prompt already
demands one per step (`prompts/modes/planner.md`), and `_ACCEPTS`
(`loop.py:2217`) already exists — but it is used in exactly one place,
`_asks_the_developer`, and only as a *negative* signal. Make it the positive test:

```python
def _count_steps(plan: str) -> int:
    steps = _STEP.findall(plan)
    if not steps or not _ACCEPTS.search(plan):
        return 0          # numbered prose is not a plan by our own definition
    return len(steps)
```

A numbered explanation never carries `Accepts:`. This alone ends the transcript's
run at turn 10 with `Done · 10 turns` and the explanation on screen, which is the
correct outcome. Guard the change against `_is_scaffold_plan`, which is checked
separately and legitimately has terse steps.

**F7b — never run the full gate on an empty change set.** Move the
`_unstarted_work` check above the `report.ok` branch in `_verify`:

```python
report = full_gate(...)
if not self.router.touched:
    # Nothing was written, so every unscoped stage is reporting the state of the
    # repository, not the state of this run.
    if unstarted := self._unstarted_work():
        self.context.append_tool_result("go_build", unstarted)
        return
    self.result = RunResult(Outcome.DONE, "nothing was changed; …", …)
    return
```

Better still, skip the gate entirely when `router.mutations == 0` — a gate that
cannot attribute its findings to the run is not verifying anything. This is the
single change that would have contained the blast radius even with every other
bug left in place.

**F7c — widen `_restated_the_plan`.** `loop.py:1573` catches precisely this
handoff failure, and only on a *verbatim* restatement:

```python
and (result.chat.content or "").strip() == self.state.plan.strip()
```

The Coder here paraphrased, so it missed. A `difflib.SequenceMatcher` ratio above
~0.85, **or** an executing mode whose first turn calls no tool and whose reply
matches `_REFUSES` / says there is nothing to change, should end the run as
`Outcome.DONE`. That is the backstop for whatever F7a's regex still lets past.

#### On the general shape

Note the guard does work: turn 5 ended `Done · 5 turns` on `explain me this
project`, because that answer happened to be unnumbered. The classifier is a
regex over prose and will always be probabilistic — which is why F7b matters more
than F7a. **The right invariant is not "classify intent perfectly", it is "a run
that wrote nothing cannot fail."**

### F8 — Evidence discipline (P2)

The report's §7 is fair. Two claims in the transcript are stated as fact and are
inferences: *"the service starts but serves nothing"* (from a static
fx-registration lint, with no runtime evidence) and *"go_test is almost certainly
downstream of the same vet failure"*. The second is actually defensible — `go
test` compiles the package and `go vet` runs as part of `go test` — but neither
was labelled. A line in `prompts/system.md` requiring a claim about *runtime*
behaviour to name the observation behind it, or be marked as inference, is cheap.
`prompts/modes/verifier.md` already asks for "what it actually said"; extend it to
"and say which parts are inference".

---

## 5. The Go defect

Unchanged and still trivial — `core/domain/transferentry.go:19` carries a literal
tab inside the struct tag:

```go
TransDate string `json	:"trans_date" validate:"required"`
```

should be

```go
TransDate string `json:"trans_date" validate:"required"`
```

It is worth noting for the eval suite that the agent diagnosed this correctly on
its **first** verifier turn and never revised it. The model was not the problem.

---

## 6. Suggested order of work

1. **F7b** — a run that wrote nothing cannot fail the gate. Smallest change,
   largest blast-radius reduction: it contains this incident on its own.
2. **F1 + F2** — one commit. Stops the deadlock and stops the UI lying about it.
3. **F7a** — `Accepts:` gates the handoff; explanations stop at the Planner.
4. **F3** — ends any surviving variant of this loop inside three turns.
5. **F4** — removes the `write_file` detour.
6. **F6, F7c** — turn "no progress" into a report that names the blocked file.
7. **F5, F8** — precision work, no urgency.

Steps 1–4 are small and mutually independent. Each wants a regression test in
`tests/test_loop.py`; the working tree already has 152 new lines there from the
in-flight `EXECUTING_RESEARCH_*` and `MAX_UNFINISHED_NUDGES` work, which is
adjacent to this but touches neither the ledger nor the handoff.

### The two bugs are independent, and both are needed

| | Read-only routed to Coder | Ledger replays mode refusal |
|---|---|---|
| Fix F7 only | Run ends at turn 10, correctly | A real coding task still deadlocks the moment a mode-refused call is repeated |
| Fix F1 only | Run still enters the ladder, still adopts a pre-existing `go_vet` failure — but now the Coder's patch lands, and it silently fixes a file the developer never asked it to touch |
| Both | Explanations stop at the Planner; coding tasks patch on the first attempt |

The middle row is worth dwelling on: F1 alone converts a visible 32-turn hang
into a **silent unrequested edit**, which is the worse failure. That is the
argument for doing F7b first.

---

# Part II — the write task that never wrote (29 turns, 0 files)

A second transcript: *"write a new api that will store employee details … create
everything required for it including a new schema and table sql scripts."*
Unambiguously a write task. The Planner produced a good 8-step plan at turn 13.
The Coder then spent turns 14–29 calling `search_docs` and nothing else, and the
run ended `Stopped — no progress`.

None of Part I's defects are involved. This is a different failure with five
independent causes.

## 0. The research ceiling exists, but cannot fire in this shape

`loop.py:358` carries `EXECUTING_RESEARCH_NUDGE = 12` / `EXECUTING_RESEARCH_LIMIT
= 16` — a research ceiling for writing modes, written for exactly this failure:

> `_narrating` already catches a Coder that talks instead of editing, but it
> counts turns that called *nothing*, so a Coder that calls `read_file` every
> turn walks past it — twenty-six turns of reading a service's handlers with
> `stalled_turns` at zero the whole way and not one edit at the end of it.

That is this transcript, described in advance. Two problems with it.

**It is mistuned so the limit is unreachable.** `executing_research` increments
once per turn (`loop.py:782`), and the Coder's first turn was 14, so at the top
of turn *N* the counter reads *N* − 14:

| | Threshold | Fires at turn |
|---|---|---|
| `EXECUTING_RESEARCH_NUDGE` | 12 | 26 |
| `EXECUTING_RESEARCH_LIMIT` — withdraws `_LOOKUP_TOOLS`, leaving writing as the only move | 16 | **30** |
| `MAX_STALLED_TURNS` | 6 | **29** |

The stall detector ends the run one turn before the limit that would have forced
a write. The nudge did land at turn 26 and was ignored — turns 26–29 are four
more `search_docs` pairs. **Retune to 12/14**, so the limit sits inside the stall
ceiling's reach rather than beyond it.

**It is uncommitted.** `git diff --stat` shows +205/−9 in `loop.py`, unstaged,
and the tree moved under this analysis while it was being written. Whether the
build that produced the transcript contained the guard cannot be established from
here — but the tuning defect above holds either way, so fix the numbers and
commit it before treating this section as closed.

## 1. The Planner had the right answer, read it, and did not use it

Turn 7: `search_docs resource scaffold new resource ten step recipe` → 130 lines.

`prompts/modes/planner.md` says:

> If the task is to add a resource to an existing service, the plan is the
> ten-step recipe and step one is `resource_scaffold` — say so rather than
> listing seven files to write by hand.

A new resource with a new table and a full domain/repo/handler/response stack is
the canonical case. The plan lists seven files to write by hand. So
`_is_scaffold_plan` (`loop.py:2284`), which keys off the literal string
`resource_scaffold`, returned False; `_advance` (`loop.py:1318`) routed to CODER
instead of SCAFFOLDER; and files that one tool call would have emitted correctly
had to be invented from documentation instead.

**Fix (F10):** the Planner cannot be trusted to self-select this. Detect the
shape from the plan and route on it — a plan naming ≥ 4 of `core/domain/*.go`,
`repo/postgres/*.go`, `handler/*.go`, `handler/response/*.go`,
`bootstrap/bootstrapper.go` for one new noun *is* a resource plan whether or not
it says so. Either route to SCAFFOLDER, or bounce once to the Planner with "this
is the ten-step recipe; step one is `resource_scaffold`."

## 2. The plan was not executable, and nothing checked

`legacy_audit` at turn 6 established that this is a legacy gin service. The plan
then specifies handlers embedding `*serverHandler.Base`, routes on
`serverRoute.Context`, and `port.CreateSuccess` — n-api-template types the
service does not import — and defers the hardest unknown to step 8:

> add the `n-api-server`, `n-api-db`, `n-api-log` modules the new code imports
> (I will name exact versions from the template, not invent them)

That is a precondition for step 1, not a last step. The Coder spent all sixteen
of its turns trying to resolve it: turn 16 `legacy service add new n-api handler
without full migration`, turn 18 `legacy service add single new n-api handler
without migrating whole service`. It was asking the right question. Nothing in
the corpus answers it.

**Fix (F11):** a plan step that adds a dependency, or that uses a package the
module does not require, must be ordered first and must be checked. A cheap
version: after `set_plan`, extract the import paths the plan names and compare
against `go.mod`; if any is absent, the plan's step order is wrong and the
Planner should be told so before the Coder ever runs.

## 3. `search_docs` cannot say "I don't know"

`knowledge.py:296` runs BM25 with `limit=4` and **no score floor** — the only
filter is `if score > 0` (`knowledge.py:158`). Any query sharing one common term
with any section returns the four least-bad sections in the corpus, formatted
identically to a real answer.

So `api-server Router struct Engine field` returned 196 lines of confident,
well-cited prose that did not contain the answer. It returned **the same 196
lines** at turns 21, 22 and 23 for three different phrasings. The model had no
way to distinguish *"here is your answer"* from *"here is the nearest thing in a
corpus that does not cover this"* — the score is computed and thrown away, and
even the `hits` citations go into `meta`, which `router.py:432` strips from what
the model sees.

**Fix (F12):** three changes, all small.

1. Apply a relative floor: drop hits scoring below ~35 % of the top hit, and if
   the top hit itself is weak, return the existing `nothing in the knowledge base
   matches …` message rather than four near-misses.
2. Surface the citations and a confidence word to the model, not just the log —
   `── contract §4.2 (weak match) ──` tells it to stop looking here.
3. When a query returns the identical section set as an earlier query this run,
   say so: *"these are the same four sections `<earlier query>` returned; the
   corpus does not cover this."* That converts an unbounded rephrase loop into
   one sentence.

## 4. Nothing detects "different question, same answer"

`_fingerprint` (`loop.py:2023`) catches the *same* question. The `dup_results`
ledger (`loop.py:1137`) does notice identical returned bytes — but only to prune
context, never to end a turn, and it is switched off entirely when a turn makes
more than one call:

```python
and len(assistant_msg.tool_calls) == 1     # loop.py:1145
```

Every Coder turn here made two. So ten copies of the same 196 lines accumulated
in the working set, and each rephrasing counted as a dispatched call and reset
`stalled_turns` to zero.

**Fix (F13):** make `dup_results` a *progress* signal, not just a context one,
and drop the single-call restriction (collapse per call rather than per turn). A
turn in which every call returned bytes already in context added nothing, exactly
as if every call had been intercepted — it should increment `stalled_turns` the
same way. This is the general form of Part I's F3.

## 5. An executing mode has no way to say "I am blocked"

The Planner has an exit for this: `_asks_the_developer` (`loop.py:2161`) ends the
run `Outcome.DONE` with *"the planner asked for a decision before it could plan;
answer it and the run continues from here."*

The Coder has no equivalent. `_advance` sends CODER unconditionally to `_verify`,
and a tool-free *"I cannot start step 1 until someone decides whether this
service is being migrated"* is counted by `_narrating` and ends the run as
`NO_PROGRESS` after three turns. Saying "I'm blocked" is punished identically to
saying nothing — so the model kept searching, which at least looked like work.

**Fix (F14):** give executing modes a blocked exit. If a Coder turn calls no tool
and its reply names a decision it cannot make, end the run `Outcome.DONE` with
the question on screen and the transcript resumable, the same way the Planner
does. Add one line to `prompts/modes/coder.md`: *"If the step cannot be executed
as written, say which step and what decision is missing, in one line, and stop.
That is a valid ending."*

## 6. The stop message named the wrong problem

> the last 6 tool-calling turns only repeated earlier calls or known dead ends,
> and added nothing new; search_docs was asked 8 times

True, and useless. The developer is told about repetition; the actual state was
*"the Coder is blocked on how to add an n-api handler to a legacy gin service,
the knowledge base does not cover it, and no file has been written."* Every one
of those facts is available to the loop at that moment — `state.plan`,
`router.touched` (empty), and the repeated query text.

**Fix (F15):** when a run ends with `router.touched` empty and a plan set, the
summary should lead with the plan's first unwritten target and the most-repeated
query, not with the repeat count. This is the same reporting gap as Part I's F6.

## Order of work for Part II

1. **Retune `EXECUTING_RESEARCH_LIMIT` to 14 and commit the guard.**
2. **F12** — a `search_docs` that admits ignorance. Highest leverage: it ends
   this loop at turn 20 on its own.
3. **F13** — same-answer turns count as stalled.
4. **F10** — route resource-shaped plans to the Scaffolder.
5. **F14 + F15** — a blocked exit, and a stop message that names the blocker.
6. **F11** — dependency-order check on the plan.

## What the two transcripts have in common

Neither is a model failure. In Part I the model diagnosed the bug on its first
try and was prevented from fixing it; here the model asked the right question
sixteen times and was never told the corpus had no answer.

Both are the same structural gap in different clothes: **a tool result that is
formatted like an answer but is not one** — a cached refusal in Part I, a
below-threshold BM25 hit in Part II — and **no detector for a turn that consumed
a tool call without acquiring information.** `stalled_turns` measures dispatch,
not information. That is the metric to change.

---

# Part III — re-analysis after the update

Re-checked against the working tree at `loop.py` 2293 lines (+205/−9 unstaged vs
`c22a947`). `knowledge.py`, `router.py`, `gate.py`, `fs.py` and the prompts are
unchanged. Test suite: **425 passed, 21 skipped, 0 failed.**

## What changed

`_is_explanation` (`loop.py:2239`) is new, and it is a better fix than the F7a I
proposed. Rather than tightening `_count_steps`, it asks two independent
questions and requires both to agree:

```python
if not _ASKS_TO_BE_TOLD.search(task) or _ASKS_FOR_WORK.search(task):
    return False
return not (_PLAN_EDITS.search(text) or _ACCEPTS.search(text) or _is_scaffold_plan(text))
```

Testing the **task** as well as the reply is the part my version missed. "Explain
the bootstrapper, then migrate it" keeps its plan because `_ASKS_FOR_WORK` sees
`migrate`; a one-step plan reading like a description keeps its plan because the
task asked for work. Verified directly against both transcripts:

| Input | `_count_steps` | `_is_explanation` | Outcome |
|---|---|---|---|
| T1 — *"explain the bootstrapper and tell me how it deviates…"* + numbered explanation | 4 | **True** | ends `DONE`, answer on screen |
| T2 — *"write a new api … create everything required"* + 8-step plan | 2 | **False** | proceeds to Coder, correctly |

Also confirmed present, and previously only in an uncommitted state:
`EXECUTING_RESEARCH_*`, `MAX_UNFINISHED_NUDGES`, `_unwritten_targets`, the
`_SCAFFOLD_TOOLS` one-shot handoff, and the unwritten-targets headline in
`_done_summary`.

## Net effect on the two transcripts

**Transcript I no longer reproduces.** The run now ends at turn 10 with the
explanation on screen and `Reply "go" if you want the changes it implies made`.
The Coder never runs, so the gate never runs on an untouched workspace, so the
pre-existing `go_vet` failure is never adopted and the ledger bug is never
reached.

That is containment, not repair. **Every defect in Part I is still in the code**
— the entry point that reached them is closed. F1 fires on any ordinary coding
task the moment a Verifier reaches for a write tool and the Coder repeats the
call, which needs no misclassified plan to happen.

**Transcript II reproduces unchanged.** Nothing in the update touches it: the
Planner still writes seven files by hand instead of naming `resource_scaffold`,
`search_docs` still has no score floor, and the Coder still has no way to say it
is blocked. Re-run today it stops at turn 29 exactly as before.

## Status of every finding

| | Finding | Status |
|---|---|---|
| **F7a** | Explanation routed to Coder | **Fixed** — `_is_explanation`, verified |
| F15 | Stop message names the blocker | **Partial** — `_done_summary` leads with unwritten targets; the `NO_PROGRESS` message is unchanged |
| **F1a** | `_fingerprint` (`loop.py:2023`) omits mode | Open |
| **F1b** | `last_results[fingerprint]` (`loop.py:1105`) caches refusals | Open |
| **F2** | Intercept emits `ok: True` (`loop.py:1007`) | Open |
| **F7b** | Full gate runs on an untouched workspace; `_unstarted_work` still gated behind `if report.ok:` (`loop.py:1384`) | Open |
| F3 | `_narrating` unreachable on intercept-only turns (`loop.py:783`) | Open |
| F4 | `_wrong_mode` checks `spec.instead` before `spec.mutates` (`router.py:356`) | Open |
| F5 | No counter on repeated `incomplete_tool_calls` (`loop.py:755`) | Open |
| F6 | No cycle-level loop detection | Open |
| F7c | `_restated_the_plan` requires a verbatim match (`loop.py:1573`) | Open |
| F8 | Evidence discipline in prompts | Open |
| F10 | Resource-shaped plans not routed to Scaffolder | Open |
| F11 | No dependency-order check on the plan | Open |
| F12 | `search_docs` has no score floor (`knowledge.py:158`, `:296`) | Open |
| F13 | `dup_results` is not a progress signal (`loop.py:1137-1145`) | Open |
| F14 | No blocked exit for executing modes | Open |
| — | `EXECUTING_RESEARCH_LIMIT = 16` unreachable before `MAX_STALLED_TURNS` | Open |

## Two defects introduced or newly visible

### D1 — `_ACCEPTS` is defined twice (P1)

`loop.py:2158` and `loop.py:2217` both bind `_ACCEPTS`. Python takes the second,
so the first is dead code and `_asks_the_developer` (`loop.py:2161`) silently
switched to the stricter pattern:

```
2158:  ^\s*[-*>\s]*\**\s*Accepts\s*:            # dead
2217:  ^[ \t]{0,6}[-*>]?[ \t]*\**\s*Accepts\s*: # live
```

Measured difference — the live pattern allows at most six leading spaces and one
bullet character:

| Line | old | new |
|---|---|---|
| `  - Accepts: build passes` | ✓ | ✓ |
| `        - Accepts: deeply indented` | ✓ | **✗** |
| `   > - Accepts: x` | ✓ | **✗** |

An eight-space `- Accepts:` is an ordinary sub-bullet under a numbered step. When
it stops matching, a real plan loses the signal that keeps `_asks_the_developer`
from reading it as a bare question — and if that plan also carries two question
marks, the run ends `DONE` having done nothing. Delete one definition; if the
stricter one is intended, widen the indent to `[ \t]{0,12}` and allow repeated
bullet characters.

No test caught it because no test uses a deeply indented `Accepts:` line, and
`ruff` is not installed in this environment (`F811 redefinition` would have
flagged it).

### D2 — `_PLAN_EDITS` verbs have no trailing word boundary (P2)

`loop.py:2104` matches `(add|create|write|…|register|wire|…)` with no `\b` after
the group, so `Creates`, `Registers`, `Wires`, `Updates` all match. Anchored at
the step number, this is usually harmless — but it is the one way
`_is_explanation` still fails:

```
1. Creates the Temporal worker on the PAO task queue.
2. Registers the transfer-entry verification workflow.
3. Wires the start/stop lifecycle hooks.
```

Verified: `_PLAN_EDITS` matches, so `_is_explanation` returns **False** and this
explanation is handed to the Coder. Numbered explanations of Go wiring code start
with exactly these verbs. Adding `\b` after the group narrows it without loss —
the plan steps this must keep matching are imperatives (`add`, `create`), not
third-person present.

## Revised order of work

1. **F7b** — a run that wrote nothing cannot fail the gate. Still the smallest
   change with the largest blast radius, and now the only thing standing between
   a slipped classification (D2) and an unrequested edit.
2. **F1a + F1b + F2** — the Part I deadlock is contained, not fixed.
3. **D1** — one-line deletion.
4. **F12 + F13** — Part II's core; nothing has touched it.
5. **D2, F3, F4** — small hardening.
6. **F10, F11, F14, F6, F5, F7c, F8** — as before.
