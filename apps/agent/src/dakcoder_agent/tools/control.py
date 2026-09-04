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

from dataclasses import dataclass
from typing import Any

from dakcoder_shared.envelope import ToolResult

from .router import Invocation

__all__ = ["HANDLERS", "PlanStep", "steps_from_meta"]


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
    #: Where the step stands. Set by the loop from ground truth -- ``done`` when
    #: a mutation lands on ``file``, ``failed`` when a gate failure names it --
    #: and by the model only for ``skipped``, through ``revise_plan``. A plan
    #: used to be a tuple written once at submission and never touched again,
    #: and "what is left to do" was a set difference against the change set that
    #: could not say *attempted and failed* or *deliberately skipped*.
    status: str = "pending"
    #: One line on why, for ``failed`` and ``skipped``.
    note: str = ""

    def rendered(self, index: int) -> str:
        return f"{index}. {self.file} — {self.action}\n   Accepts: {self.accepts}"

    @property
    def open(self) -> bool:
        """Whether the step still asks for work: pending, or tried and failed."""
        return self.status in ("pending", "failed")


#: The statuses a step may carry, and the two the model may set itself.
STEP_STATUSES = ("pending", "done", "failed", "skipped")
MODEL_STATUSES = ("pending", "skipped")

#: How much of a `finish` answer reaches the developer. It is what they read,
#: and it goes out as one tool call: an unbounded answer is the reply most
#: likely to be cut off by the output limit -- and the one call that should
#: never be. Roughly 900 words.
MAX_ANSWER_CHARS = 6_000


def steps_from_meta(meta: dict[str, Any]) -> tuple[PlanStep, ...]:
    """Rebuild the typed steps from a tool result's ``meta``."""
    out: list[PlanStep] = []
    for raw in meta.get("steps") or ():
        if not isinstance(raw, dict):
            continue
        status = str(raw.get("status", "pending") or "pending").strip().lower()
        out.append(
            PlanStep(
                file=str(raw.get("file", "")).strip(),
                action=str(raw.get("action", "")).strip(),
                accepts=str(raw.get("accepts", "")).strip(),
                status=status if status in STEP_STATUSES else "pending",
                note=str(raw.get("note", "") or "").strip(),
            )
        )
    return tuple(out)


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
        meta={
            "control": "plan",
            "summary": summary,
            "steps": [
                {"file": s.file, "action": s.action, "accepts": s.accepts} for s in steps
            ],
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
    cut = 0
    if len(answer) > MAX_ANSWER_CHARS:
        # Bounded, and the bound is stated where the developer reads it. The
        # schema asks for it too (`maxLength`), which an endpoint with guided
        # decoding enforces on a named choice; this is the half that holds when
        # it does not.
        cut = len(answer) - MAX_ANSWER_CHARS
        answer = answer[:MAX_ANSWER_CHARS].rstrip() + f"\n\n[answer cut at {MAX_ANSWER_CHARS:,} characters; {cut:,} more were sent]"
    blocked = str(inv.arg("blocked") or "").strip()
    body = answer if not blocked else f"{answer}\n\nBlocked: {blocked}"
    return ToolResult.success(
        body,
        meta={"control": "finish", "answer": answer, "blocked": blocked, "answer_cut": cut},
    )


def revise_plan(inv: Invocation) -> ToolResult:
    """Replace what is left of the plan, and say why.

    The model's own pivot. Every escape hatch the loop had was a stop -- a forced
    ``finish``, a turn cap, a gate bound -- and nothing in the run could say
    "that approach failed; here is a different one". The plan was immutable from
    submission to death.

    Steps already ``done`` are kept by the loop; what is sent here replaces the
    rest. A step may arrive ``skipped`` with a note, which is the one status the
    model is trusted to set: it is a decision about the work, and the loop is
    not the arbiter of whether a step was still needed. ``reason`` is recorded
    under "what has been tried" and shown on every later turn, so the same dead
    end is not walked twice.
    """
    raw = inv.arg("steps") or []
    steps = tuple(
        PlanStep(
            file=str(s.get("file", "")).strip(),
            action=str(s.get("action", "")).strip(),
            accepts=str(s.get("accepts", "")).strip(),
            status=(
                str(s.get("status", "pending") or "pending").strip().lower()
                if str(s.get("status", "pending") or "pending").strip().lower() in MODEL_STATUSES
                else "pending"
            ),
            note=str(s.get("note", "") or "").strip(),
        )
        for s in raw
        if isinstance(s, dict)
    )
    reason = str(inv.arg("reason") or "").strip()
    if not steps:
        return ToolResult.failure(
            "revise_plan was called with no steps.",
            fix="Send the remaining steps, or mark the ones you are dropping as "
            "skipped with a note. If the work is finished, call `finish` instead.",
        )
    if not reason:
        return ToolResult.failure(
            "revise_plan needs a reason.",
            fix="Say in `reason` what was tried and why it did not work; it is kept "
            "so the same approach is not tried again.",
        )

    body = "\n".join(step.rendered(i) for i, step in enumerate(steps, 1))
    skipped = [s for s in steps if s.status == "skipped"]
    head = f"Plan revised because: {reason}"
    if skipped:
        head += "\nSkipped: " + ", ".join(f"{s.file} ({s.note or 'no reason given'})" for s in skipped)
    return ToolResult.success(
        f"{head}\n\n{body}",
        meta={
            "control": "revise",
            "reason": reason,
            "steps": [
                {
                    "file": s.file,
                    "action": s.action,
                    "accepts": s.accepts,
                    "status": s.status,
                    "note": s.note,
                }
                for s in steps
            ],
        },
    )


HANDLERS: dict[str, Any] = {
    "submit_plan": submit_plan,
    "ask_developer": ask_developer,
    "finish": finish,
    "revise_plan": revise_plan,
}
