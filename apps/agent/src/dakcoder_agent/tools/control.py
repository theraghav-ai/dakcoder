"""The tools a mode ends its turn with.

They are the whole of Track A item 3, and the reason they exist is worth stating
plainly, because "make the plan a tool call" sounds like a refactor and is not.

A plan used to be *whatever prose the Planner returned*. The loop decided
whether it was a plan by counting lines that began with a number
(``_count_steps``), whether it was a question by counting question marks
(``_asks_the_developer``), whether it was a refusal by matching "I cannot"
(``_refuses_to_plan``), and whether it was really an explanation by matching
twenty verbs against the task (``_is_explanation``). Every one of those was
wrong in the field, and the module's own comment concedes why: *"A description
of a deviation is indistinguishable from a proposal to remove it, and no regex
over prose can separate them."*

So the model says which it is, by calling one of these. The reply is typed,
validated against a schema it was shown, and carries a ``tool_call_id``; the
loop transitions on that event and never reads the prose to find out what
happened. What is deleted along with the guessing: ``_STEP``, ``_count_steps``,
``_PLAN_EDITS``, ``_PLAN_PATH``, ``_ACCEPTS``, ``_STEP_START``, ``_REFUSES``,
``_asks_the_developer``, ``_refuses_to_plan``, ``_restated_the_plan``,
``_plan_targets``, ``_is_scaffold_plan``.

``finish`` is the same idea for ``ask`` and ``agent``, and it is here because
the live endpoint settled an argument. In those two modes "I am finished" meant
*not calling a tool*, and past about six fruitless calls Qwen3.8-27B cannot
produce a non-action: it repeats its last call, 5 times out of 5, and no wording
in the tool's answer changes that. Suppressing the tools is worse -- with
``tool_choice: "none"`` vLLM turns off its tool parser while the schemas stay in
the prompt, so the model's ``<tool_call>`` markup lands in ``content`` as text;
with ``tools: []`` it invents ``Grep`` from another harness. Giving it a call
that *means* stopping works 5/5. That is the whole fix.

None of these tools touches the workspace. They are handlers rather than something the
loop intercepts before dispatch so that argument validation, coercion, the
malformed-arguments message and the tool-result envelope are the same ones every
other tool gets -- an intercept would be a second, quietly different code path
for the one call a run cannot afford to get wrong.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from dakcoder_shared.envelope import ToolResult

from .router import Invocation

__all__ = [
    "DONE",
    "FAILED",
    "HANDLERS",
    "PENDING",
    "SKIPPED",
    "PlanStep",
    "as_meta",
    "steps_from_meta",
]

#: A plan step's status. Four values, and the distinction that earns them is
#: that the first three are facts the *loop* establishes and the fourth is a
#: judgement only the model can make.
#:
#: `_unwritten_targets` used to be the whole of plan state: a set difference
#: between the plan's paths and `router.touched`, computed on demand and stored
#: nowhere. It cannot say "written, and the gate rejected it", it cannot say
#: "decided against, here is why", and because it was never rendered anywhere
#: the model could read, the model's only account of its own progress was its
#: own earlier prose -- which is how a run that had written two files of three
#: reported having written all three.
PENDING = "pending"
DONE = "done"
FAILED = "failed"
SKIPPED = "skipped"

#: How each status is rendered into the state block. Words rather than symbols:
#: this is read by a model, and `[x]` versus `[!]` is a legend it has to hold.
_MARKS = {
    PENDING: "[ todo   ]",
    DONE: "[ done   ]",
    FAILED: "[ FAILED ]",
    SKIPPED: "[ skipped]",
}


@dataclass(frozen=True, slots=True)
class PlanStep:
    """One step, as the model submitted it.

    ``file`` is what makes the plan checkable: at the end of a run the loop can
    say which steps were never written, and it can say it from a field the model
    filled in rather than from a regex over its prose. ``_plan_targets`` used to
    guess this by finding the first path-shaped token in each numbered paragraph,
    which reported a neighbour named as an example as an unwritten target.
    """

    file: str
    action: str
    accepts: str
    #: Where this step has got to. Maintained by the loop from the workspace and
    #: the gate, except ``SKIPPED``, which only the model may set and which
    #: therefore survives every resync. See ``AgentLoop._sync_plan``.
    status: str = PENDING
    #: Why, for the two statuses where "why" is the whole content: the gate stage
    #: that rejected the file, or the model's reason for skipping the step.
    note: str = ""

    @property
    def open(self) -> bool:
        """Whether this step is still work the run owes the developer."""
        return self.status in (PENDING, FAILED)

    def rendered(self, index: int) -> str:
        """One line for the state block, plus what is still outstanding on it.

        The acceptance criterion is dropped once a step is done or skipped: it
        is an instruction for work that is no longer to be done, and every token
        in this block is re-sent on every turn.
        """
        mark = _MARKS.get(self.status, _MARKS[PENDING])
        line = f"{index}. {mark} {self.file} - {self.action}"
        if self.open:
            line += f"\n      Accepts: {self.accepts}"
        if self.note:
            line += f"\n      {self.note}"
        return line


def steps_from_meta(meta: dict[str, Any]) -> tuple[PlanStep, ...]:
    """Rebuild the typed steps from a tool result's ``meta``.

    ``status`` and ``note`` round-trip so a revised plan can carry forward what
    was already established about the steps it keeps. A revision that reset every
    step to pending would tell the model to rewrite files it has already written,
    which is the failure the status field exists to prevent.
    """
    out: list[PlanStep] = []
    for raw in meta.get("steps") or ():
        if not isinstance(raw, dict):
            continue
        status = str(raw.get("status", PENDING)).strip() or PENDING
        out.append(
            PlanStep(
                file=str(raw.get("file", "")).strip(),
                action=str(raw.get("action", "")).strip(),
                accepts=str(raw.get("accepts", "")).strip(),
                status=status if status in _MARKS else PENDING,
                note=str(raw.get("note", "")).strip(),
            )
        )
    return tuple(out)


def as_meta(steps: Sequence[PlanStep]) -> list[dict[str, str]]:
    """The inverse of ``steps_from_meta``, for a result that carries a plan."""
    return [
        {
            "file": s.file,
            "action": s.action,
            "accepts": s.accepts,
            "status": s.status,
            "note": s.note,
        }
        for s in steps
    ]


def submit_plan(inv: Invocation) -> ToolResult:
    """Accept the plan and hand the run on to the acting mode.

    The router has already checked that every step carries a file, an action and
    an acceptance criterion, so there is nothing left to validate here beyond
    the one thing a schema cannot express: a plan with no steps in it.

    The rendered text is what the developer and the next turns read; the typed
    steps travel in ``meta`` for the loop.
    """
    raw = inv.arg("steps") or []
    steps = tuple(
        PlanStep(
            file=str(s.get("file", "")).strip(),
            action=str(s.get("action", "")).strip(),
            accepts=str(s.get("accepts", "")).strip(),
        )
        for s in raw
        if isinstance(s, dict)
    )
    if not steps:
        return ToolResult.failure(
            "submit_plan was called with no steps.",
            fix="Send at least one step, each naming the file it changes, what "
            "changes in it, and how it is checked. If the task genuinely needs "
            "no change, say so in one sentence instead of calling this.",
        )

    summary = str(inv.arg("summary") or "").strip()
    body = "\n".join(step.rendered(i) for i, step in enumerate(steps, 1))
    if summary:
        body = f"{summary}\n\n{body}"

    return ToolResult.success(
        f"Plan accepted, {len(steps)} step(s). Work starts now — you hold the "
        f"write tools from this turn on.\n\n{body}",
        meta={"control": "plan", "summary": summary, "steps": as_meta(steps)},
    )


def revise_plan(inv: Invocation) -> ToolResult:
    """Replace the remaining plan, on the record, without ending the phase.

    The gap this closes is the one the loop had no move for at all. Every escape
    hatch in the loop was a *stop* -- forced ``finish``, the stall ceiling, the
    gate-failure budget -- so a model whose plan was wrong could abandon the run
    or keep pushing the plan that was wrong, and nothing in between. The gate
    said "fix what it found" up to three times and the answer to "this approach
    cannot work" was never available.

    ``ruled_out`` is required, and that is the whole design. A revision with no
    stated reason is a re-roll: the same model, the same context, a fresh guess,
    and nothing stopping it landing on the approach it just abandoned. Requiring
    the reason puts it into ``_State.ruled_out``, which the state block re-sends
    on every turn -- so the next plan is made against an explicit record of what
    has already failed rather than against the same blank slate that produced
    the first one.

    This is *not* a terminal tool. It does not end the phase, does not switch
    modes and does not hand anything to the gate: the acting mode revises and
    carries straight on with the next step. The loop's own replan path -- back
    to the Planner after a second failing gate -- is the involuntary version of
    this, for a model that has not noticed it needs one.
    """
    raw = inv.arg("steps") or []
    steps = tuple(
        PlanStep(
            file=str(s.get("file", "")).strip(),
            action=str(s.get("action", "")).strip(),
            accepts=str(s.get("accepts", "")).strip(),
            status=(
                str(s.get("status", PENDING)).strip()
                if str(s.get("status", PENDING)).strip() in _MARKS
                else PENDING
            ),
            note=str(s.get("note", "")).strip(),
        )
        for s in raw
        if isinstance(s, dict)
    )
    if not steps:
        return ToolResult.failure(
            "revise_plan was called with no steps.",
            fix="Send the whole remaining plan, not just the part that changed -- "
            "this replaces it. Mark work you have already finished `status: done` "
            "and work you have decided against `status: skipped` so it is not "
            "asked for again.",
        )

    ruled_out = str(inv.arg("ruled_out") or "").strip()
    if not ruled_out:
        return ToolResult.failure(
            "revise_plan needs `ruled_out`.",
            fix="Say in one line what you tried and why it cannot work. Without it "
            "this is a fresh guess rather than a revision, and nothing stops the "
            "new plan repeating the approach you are abandoning.",
        )

    body = "\n".join(step.rendered(i) for i, step in enumerate(steps, 1))
    return ToolResult.success(
        f"Plan revised, {len(steps)} step(s). Ruled out: {ruled_out}\n\n{body}\n\n"
        "Carry on from the first step still open. You are still in the acting "
        "phase and still hold the write tools.",
        meta={
            "control": "revise",
            "summary": str(inv.arg("summary") or "").strip(),
            "steps": as_meta(steps),
            "ruled_out": ruled_out,
        },
    )


def ask_developer(inv: Invocation) -> ToolResult:
    """Stop and put the questions to the developer.

    The run ends here, deliberately and cleanly. The questions are the last thing
    on screen, and the developer's answer arrives as a follow-up on this same
    transcript — which is what a continued session is for, and what the Planner
    was waiting on all along.

    The old path reached the same place by accident and much later: a numbered
    list of questions counted as a numbered list of steps, so it was pinned as
    the plan, the Coder found nothing to execute, the gate ran on an untouched
    workspace, and the ladder cycled until the escalation budget ran out — with
    four unanswered questions still on screen and the run reported ``unverified``.
    """
    questions = [str(q).strip() for q in (inv.arg("questions") or []) if str(q).strip()]
    if not questions:
        return ToolResult.failure(
            "ask_developer was called with no questions.",
            fix="Ask at least one, or submit the plan with what you inferred.",
        )
    assumed = str(inv.arg("assumed") or "").strip()

    body = "\n".join(f"{i}. {q}" for i, q in enumerate(questions[:4], 1))
    if assumed:
        body += f"\n\nInferred without asking: {assumed}"

    return ToolResult.success(
        body,
        meta={"control": "ask", "questions": questions[:4], "assumed": assumed},
    )


def finish(inv: Invocation) -> ToolResult:
    """End the turn with an answer.

    The counterpart of `submit_plan` for the modes that were never given one.
    A phase ends when the model says it ends, and it says so the only way this
    model reliably can -- by calling something.

    The answer is echoed straight back rather than summarised. It is what the
    developer reads, and a tool that paraphrased it would be editing the reply.
    """
    answer = str(inv.arg("answer") or "").strip()
    if not answer:
        return ToolResult.failure(
            "finish was called with no answer.",
            fix="Put what you found or did in `answer`; it is what the developer "
            "reads. If something stopped you, say what in `blocked`.",
        )
    blocked = str(inv.arg("blocked") or "").strip()
    body = answer if not blocked else f"{answer}\n\nBlocked: {blocked}"
    return ToolResult.success(
        body, meta={"control": "finish", "answer": answer, "blocked": blocked}
    )


HANDLERS: dict[str, Any] = {
    "submit_plan": submit_plan,
    "revise_plan": revise_plan,
    "ask_developer": ask_developer,
    "finish": finish,
}
