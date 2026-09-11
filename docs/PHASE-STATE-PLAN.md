# The phase state machine

Date opened: 2026-09-11. Driving evidence: [`error.md`](../error.md), a
four-message migration session that produced two files and then spent
fifty turns re-planning.

**Status: W1–W6 done.** Each work item below carries its own status line.
Section 6 lists what is deliberately still out of scope.

---

## 1. The invariant this is for

The agent is meant to hold this chain:

```
USER GOAL
    ↓
ACTIVE PHASE
    ↓
PHASE ACCEPTANCE CRITERIA
    ↓
REQUIRED MUTATIONS
    ↓
VERIFICATION
    ↓
MARK PHASE COMPLETE
    ↓
NEXT PHASE
```

It currently holds this one:

```
USER GOAL → generate plan → talk about plan → check repo
          → maybe mutate → generate plan again → finish
```

Three of the seven nodes do not exist in the code at all (ACTIVE PHASE,
VERIFICATION, NEXT PHASE), one is unreachable for a third of real plans
(MARK PHASE COMPLETE), and one is decorative (PHASE ACCEPTANCE CRITERIA —
`PlanStep.accepts` is prose that nothing ever evaluates).

## 2. The mechanism, stated once

**The loop has a rich vocabulary for stopping and almost none for advancing.**
Every stuck state — a stall, a repeated call, a refused terminal, a failing
gate — routes to `must_answer`, which forces a prose turn, which in AGENT mode
means `finish`. There is no path that routes a stuck state to *"do the next
concrete thing"*.

So the agent's response to confusion is always to summarise and exit. In
`error.md` that produced the same `finish` answer, byte-identical, six times
across four user messages — including twice *after* the developer asked for
different work.

## 3. Verified defects

Each reproduced against `main` at the time of writing, not inferred from the
transcript. The transcript itself renders every plan step as `—`, which the
panel prints when the agent build predates per-step status, so that run is
older than `main`; both plan-state defects below still reproduce, so the
behaviour recurs.

### D1 — Re-submitting a plan wipes every `done` status

`_adopt_plan` keeps a finished step only when its file is **not** in the
incoming plan:

```python
kept = tuple(s for s in self.state.plan if s.status == "done" and s.file not in incoming)
self.state.plan = kept + tuple(steps)
```

Re-submitting the *same* plan therefore drops every done step and replaces it
with a pending one. Reproduced:

```
after submit   : [('routes/routes.go.bak','pending'), ('migration.md','pending'), ('handler','pending')]
after writes   : [('routes/routes.go.bak','done'),    ('migration.md','done'),    ('handler','pending')]
after RE-submit: [('routes/routes.go.bak','pending'), ('migration.md','pending'), ('handler','pending')]
```

In `error.md` the planner re-submitted the identical eight-step plan on turns
36, 42 and 51. Each time, two files genuinely on disk stopped counting as done.
This is the "generate plan again" arrow, and it erases the phase pointer every
time it fires.

### D2 — A step naming a directory can never be marked done

`_mark_steps`, `_unwritten_targets` and `_is_plan_target` all compare
`step.file == path` against `router.touched`. Three of the eight steps in the
transcript were `handler`, `docs`, `repo/postgres` — directories. Reproduced:

```
writing handler/objection.go marked step 'handler': ['pending']
_unwritten_targets: ['handler']
```

`submit_plan` does not validate that `file` is a file, and `_normalise_plan`
resolves a directory happily. MARK PHASE COMPLETE is unreachable for these
steps by construction.

**It compounds.** `_open_targets` is then permanently non-empty, so
`_why_not_done` refuses the first `finish`, `MAX_FINISH_REFUSALS = 1` is hit,
and *every subsequent `finish` is accepted unconditionally* — whatever is
actually unwritten. That is why turn 24's `finish` went through with six steps
untouched.

### D3 — Every follow-up is forced back into PLANNER

`_run` switches to PLANNER on every AGENT run, unconditionally. PLANNER's
terminals are `submit_plan`, `ask_developer`, `finish`; there is no "the plan
stands, carry on" exit. So "complete the entire phase wise migration plan you
wrote" and "start phase 2" each had exactly one forward move: re-plan. With D1,
each follow-up reset the progress it was asked to continue.

### D4 — `accepts` is never evaluated; `done` means "a write happened"

`_mark_steps(path, "done")` fires on the mutation, before anything checks it.
The gate is global and runs at the end of a phase, not per step. So the chain
is really `REQUIRED MUTATIONS → MARK PHASE COMPLETE`, with VERIFICATION absent.
A file written wrongly is `done`.

### D5 — A bad plan has no advertised exit

`revise_plan` is visible in AGENT. The model *correctly diagnosed* the go.mod
ordering bug in its own `finish` text — "The go.mod cleanup should be the last
step" — and never called it. Nothing in the state block or the mode instruction
points at `revise_plan` when the plan is wrong, so the only advertised exit
from a bad plan is `finish`.

### D6 — A checklist plus a 16k budget is a truncation loop

`_state_block` renders all eight steps every turn. A model handed eight pending
items and a 16,384-token output budget attempts all eight and is cut off. In
`error.md` three replies in a row were truncated mid-tool-call and the run died
`no_progress`.

## 4. Why nothing is imported from Cline for this

Row 8 of [CLINE-COMPARISON.md](CLINE-COMPARISON.md) is right and it cuts
against importing anything: Cline has **no in-loop plan object at all**. Plan
mode is a tool preset plus a command guard plus a system-prompt contract; the
Focus Chain todo list survives only as settings and file helpers in
`apps/vscode`, not in the SDK loop. `AgendaTaskRecord` is a cross-session
backlog of *proposals* — the wrong shape for a committed ordered plan, and
already built here as `plan.AgendaStore`.

The one Cline idea that is directly relevant remains unbuilt: **per-turn
checkpoints**. Without them a phase that half-lands leaves the tree in a state
that is neither phase N-1 nor phase N, and "roll back to the last verified
phase" has no implementation. That is follow-on work, not part of this plan.

## 5. The work

Ordered by value per line changed. Items 1–3 are the ones that would have
changed `error.md`.

### W1 — Prefix matching for directory steps

**Status: done** (`PlanStep.covers`, `tools/control.py`; three call sites in
`loop.py`).

A touched path *under* a step's directory satisfies that step. Applies to
`_mark_steps`, `_unwritten_targets` and `_is_plan_target` — one helper, three
call sites, so they cannot drift apart.

Un-breaks MARK PHASE COMPLETE for directory steps and stops the completion
guard being silenced on turn one.

*Acceptance:* writing `handler/objection.go` marks a step named `handler` as
written; `_unwritten_targets` no longer reports `handler` once any file under
it is touched; a step naming a file is unaffected. **Met** — verified against
the `error.md` plan shape, and `handlers/x.go` / `handler2/x.go` do not match
`handler`, nor does a step naming `.` or `""` match anything by prefix.

*Residual, accepted:* a directory step is coarse — the first write under it
satisfies it, and the loop cannot know `handler` meant eight files. What W1
buys is that the step is *reachable*, which equality never made it. Per-file
tracking requires per-file steps, and W4 makes even the coarse case pass
through a real check.

### W2 — Merge instead of replace in `_adopt_plan`

**Status: done** (`_adopt_plan`, `loop.py`).

Preserve a finished step when the incoming plan names the same file, and drop
the incoming duplicate — the reverse of what happens now. Re-planning must
never un-do work that is on disk.

*Acceptance:* submitting the same plan twice leaves `done` steps `done`; a
genuinely new step is still adopted; a step the model re-words for the same
file keeps its status and takes the new wording. **Met** — all three verified.

The carried set is `done`, `written` and `skipped`. `pending` and `failed` do
not carry: a step the model has just re-stated is one it intends to attempt
again, and a stale `failed` would read as a verdict on an attempt that has not
happened yet.

*Known, unchanged:* a plan naming the same file in two steps (the `error.md`
plan named `routes/routes.go` in steps 1 and 5) gives both steps the same
status. That is a defect in the model's plan rather than in the merge, and it
is no worse than before.

### W3 — The phase cursor

**Status: done** (`AgentLoop.active_step` and `_plan_block`).

`_state_block` stops rendering a checklist and renders one active step:

```
# Current state — turn 14, agent phase
Now: step 3 of 8 — go.mod — remove legacy dependencies
  Accepts: go mod tidy succeeds
  Done: 1 routes/routes.go.bak, 2 migration.md
  Next: 4 main.go
```

The active step is the first that still asks for work. This is the biggest
behavioural lever in the document and it is mostly a rendering change plus an
`active_step` property: a model given one concrete next action does it; a model
given a checklist writes about the checklist and gets truncated (D6).

*Acceptance:* the block names exactly one active step and its acceptance
criterion; completed steps are summarised by number and file, not re-listed in
full; a plan with no open steps says so; an ASK run with no plan is unchanged.
**Met.**

The cursor is *pending first, then written*. A written-but-unverified step is
the cheapest thing in the plan to close, but a pending step is work that has not
started, and a run that polishes while three steps have never been attempted is
the shape of a run that finishes nothing.

**One more contradiction closed while building it.** `_note_tried` marks a step
`failed` only when the blocking stage's output *names its file*, and a build
error routinely names a package or another file entirely. So "all steps settled"
sat directly above "Last gate: FAIL at go_build" — the plan declaring completion
while verification failed, which is precisely the node this document exists to
fix. The block now says *"every step is written, but the gate is failing at
go_build, so the work is not done"* instead.

### W4 — Verification between write and done

**Status: done** (`STEP_STATUSES` gains `written` in `tools/control.py`;
`_verify_written` and `gate.finding_path`).

Add a `written` status between `pending` and `done`:

* a mutation on a step's file sets `written`;
* `_inner_loop` — which already runs after every mutating batch, sub-second,
  scoped to the touched files — promotes `written` to `done` when it comes back
  clean, and leaves it `written` with the reason when it does not.

No extra tool call, no extra model turn. This inserts the VERIFICATION node.

`written` is **not** open for `_unwritten_targets`: the file was written, so
"you planned to write this and did not" is the wrong objection. `_why_not_done`
gains a separate clause naming steps that are written but not clean, bounded by
the existing `MAX_FINISH_REFUSALS`.

*Acceptance:* a write followed by a clean inner gate reaches `done`; a write
followed by a dirty one stays `written` and the state block says why; the
completion guard distinguishes "never written" from "written, not verified".
**Met.**

**Two corrections found while building it**, both of which would have made this
node worse than not having it:

*`report.ok` is the wrong test.* The inner loop's own docstring says nothing in
it blocks, so `ok` — "no stage blocked and none was skipped" — is true of every
inner report. Promotion had to read `warnings`, which is what `_inner_loop`
itself checks one line below the call.

*Per file, not per report.* The first version held every written step open on
any warning anywhere. The inner loop runs `go_diagnostics`, which fails outright
on a machine with no gopls, so on most developer machines every step would have
sat at `written` forever — **defect D2's compounding failure rebuilt in a new
place**: a permanently unsatisfiable condition that spends the completion
guard's single push-back and then silences it. It was caught by four existing
tests going red.

A stage now holds a step open only when one of its findings names a path that
step covers. `gate.finding_path` does that extraction and lives beside
`_stage_findings`, because the keys have two shapes — `rule|path|message` for
`rules_lint`, `path|message` for everything else — and a parser that drifts from
the builder files findings under the wrong heading. It returns `""` for a stage
that failed without keying a file, which is the honest reading: a tool that
could not run objected to nothing.

*Scope, stated:* a step reaching `done` is not a claim the run is correct. It
means the two checks that run in under a second — the formatter and the contract
linter — did not object to what was written. The gate still has the last word.

### W5 — PLANNER re-entry rule

**Status: done** (`_opening_mode`, `loop.py`).

A continued run whose plan still has open steps starts in AGENT with the cursor
set, rather than in PLANNER. `revise_plan` remains the escape, and a run with
no plan, or one whose steps are all closed, plans as it does now.

*Acceptance:* "carry on with the plan" on a half-done plan does not re-plan; a
genuinely new instruction on a finished plan does. **Met.**

Deliberately narrow: it needs a plan, open steps *and* a follow-up. A first
message plans, and a plan whose steps are all settled plans again, because a
developer asking for more work on a finished plan is asking for a new one.
Re-planning stays one tool call away — `revise_plan` is visible in AGENT, and
`_replan` still returns a run to the Planner after two gate failures. What is
gone is being *made* to re-plan in order to reach the work.

### W6 — Point at `revise_plan` when the plan is the problem

**Status: done** (the `finish` refusal in `_phase_ended`).

When the completion guard refuses a `finish`, or when a step has failed twice,
the state block names `revise_plan` as the move. Cheap, and it closes D5.

*Acceptance:* the refusal text names `revise_plan` and what it is for. **Met**,
asserted against the message that actually reaches the model rather than against
the source that produces it.

## 6. What the chain looks like now

```
USER GOAL            the developer's message, classified once
    ↓
ACTIVE PHASE         AgentLoop.active_step, rendered as a cursor every turn
    ↓                (W3) — pending steps first, then unverified ones
PHASE ACCEPTANCE     PlanStep.accepts, shown beside the active step
    ↓
REQUIRED MUTATIONS   a write under the step's path marks it `written`
    ↓                (W1 — directory steps are reachable at last)
VERIFICATION         the inner gate, per file, promotes `written` → `done`
    ↓                (W4 — and only findings naming this step's files count)
MARK PHASE COMPLETE  from the change set and the gate, never from prose;
    ↓                and it refuses to fire while the gate is failing
NEXT PHASE           the cursor advances; a follow-up resumes rather than
                     re-planning (W5), and re-planning preserves what is
                     on disk (W2)
```

Every node now exists. The two that remain weakest are PHASE ACCEPTANCE —
`accepts` is shown to the model but still not machine-checked — and
VERIFICATION, which is the sub-second inner gate rather than a build.

## 7. Out of scope

* Per-turn git checkpoints with compare and partial restore. Still the largest
  single gap; independent of everything here.
* Machine-checkable `accepts` (a step carrying a tool call that must pass).
  Worth having, but W4 gets most of the value without a schema change.
* Anything that needs the hosted runtime in [host-plan.md](../host-plan.md).

## 8. Log

| Date | Item | Note |
|---|---|---|
| 2026-09-11 | — | Document opened; defects D1–D6 verified against `main`. |
| 2026-09-11 | W1 | `PlanStep.covers` prefix matching; D2 closed. Suite: 633 passed, 5 pre-existing failures. |
| 2026-09-11 | W2 | `_adopt_plan` merges instead of replacing; D1 closed. Suite unchanged. |
| 2026-09-11 | W4 | `written` status + `_verify_written` + `gate.finding_path`; D4 closed. First cut held steps open on any warning and broke four tests; corrected to per-file. Suite: 653 passed, 5 pre-existing failures. |
| 2026-09-11 | W3 | `active_step` + `_plan_block`; D6 closed. Also stopped the block declaring completion while the gate fails. Two state-block tests updated to the cursor contract. Suite: 653 passed, 5 pre-existing failures. |
| 2026-09-11 | W5 | `_opening_mode`; D3 closed. Suite: 661 passed. |
| 2026-09-11 | W6 | `revise_plan` named in the finish refusal; D5 closed. Suite: 667 passed, 5 pre-existing failures. |
