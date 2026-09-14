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
from fnmatch import fnmatchcase
from typing import Any

from dakcoder_shared.envelope import ToolResult

from .router import Invocation

__all__ = ["HANDLERS", "MAX_STEP_PATHS", "PlanStep", "split_paths", "steps_from_meta"]

#: How many paths one ``file`` field may name before it stops being a step.
#:
#: A field naming seven files is not a step with a wide scope, it is a manifest
#: pasted into the wrong box, and splitting it would put seven pending items in
#: front of a model whose whole problem is attempting everything it is shown.
#: Past this it is left verbatim, which matches nothing -- and a plan that
#: matches nothing is now a *stall* rather than a loop, because `_note_delete`
#: catches the cycle. See `split_paths`.
MAX_STEP_PATHS = 6


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
    #: Which phase of a phased plan this step belongs to, and which
    #: sub-category of that phase. Empty for an ordinary task, which is every
    #: task that is not a migration: a three-step bug fix has no phases and
    #: asking for them would be ceremony.
    #:
    #: They are on the *step* rather than in a parallel structure because the
    #: step is what the loop already tracks, saves and restores. A phase is
    #: closed when every step naming it has settled, which is a question this
    #: field makes answerable from the plan the loop is already holding --
    #: rather than from a second list that would have to be kept in step with
    #: it. See ``migration.py``.
    phase: str = ""
    part: str = ""
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
        where = ""
        if self.phase:
            where = f" [{self.phase}" + (f" · {self.part}" if self.part else "") + "]"
        return f"{index}. {self.file}{where} — {self.action}\n   Accepts: {self.accepts}"

    @property
    def open(self) -> bool:
        """Whether the step still asks for work: pending, or tried and failed.

        ``written`` is deliberately not open. The file was written; what is
        outstanding is the verification, and "you planned to write this and did
        not" is the wrong objection to raise about it.

        ``blocked`` is not open either, and that is the whole point of it. A
        step nothing can finish used to have no status that let the cursor move:
        it stayed ``pending``, `active_step` returned it every turn for the rest
        of the run, and `_plan_block` re-rendered it into the recency slot as an
        instruction the model could not carry out. Three field loops have that
        shape -- a step whose premise was false, a cursor that only advances on
        a write, and a model re-issued the same order until the turn budget ran
        out. ``blocked`` is the exit: it does not claim the work was done, it
        carries the reason in ``note``, `_why_not_done` reports it and the run
        summary names it -- but the plan moves on.
        """
        return self.status in ("pending", "failed")

    def covers(self, path: str) -> bool:
        """Whether a change to ``path`` is a change this step asked for.

        Exact match, **or** ``path`` sits under ``file`` when the step names a
        directory. Both sides are normalised workspace-relative POSIX paths --
        ``_normalise_plan`` puts the step in that form and ``_confine`` puts
        every touched path in it -- so this is a string comparison and not a
        filesystem question.

        The directory case is not a nicety. A migration plan routinely names
        ``handler`` or ``repo/postgres`` as a step, and equality made those
        steps *unsatisfiable*: a write to ``handler/objection.go`` never equals
        ``handler``, so the step stayed pending for the life of the run and
        ``_unwritten_targets`` reported it forever. That permanently non-empty
        list spent the single ``MAX_FINISH_REFUSALS`` push-back on turn one and
        left every later ``finish`` accepted unconditionally, whatever was
        actually unwritten.

        A directory step is still coarse: the first write under it satisfies
        it, and the loop cannot know that ``handler`` meant eight files. What it
        buys is that the step is *reachable*, which equality never made it. A
        plan that wants per-file tracking has to name per-file steps.

        **And the same for a glob**, which is the shape the field actually
        produced. A migration plan writes ``handler/response/*.go`` and
        ``repo/postgres/*.go`` at least as readily as it writes a bare
        directory, and those were exactly as unsatisfiable: `_normalise_plan`
        keeps the ``*`` verbatim, so no write ever equalled the step and none
        ever sat under it as a prefix either. A whole field session ran with
        `_open_targets` permanently non-empty because of two glob steps -- every
        `finish` refused once on an objection the model could not satisfy, every
        run summarised as "the plan set out to write ... and those were never
        written" with a clean gate underneath it.

        ``fnmatch`` semantics, deliberately including ``*`` matching ``/``: a
        step that says ``handler/response/*.go`` means the response types
        wherever they land, and a run that put one in a subpackage has done what
        the step asked.

        The empty string and ``.`` cover nothing by prefix. A step that named
        the workspace root would otherwise match every path in it, which is not
        a plan step, it is the absence of one.
        """
        if not self.file or not path:
            return False
        if self.file == path:
            return True
        if any(ch in self.file for ch in "*?["):
            # Case-sensitive on purpose. Both sides are already normalised to
            # the repository's own spelling, and `fnmatch` (without `case`)
            # would fold them through the *host* filesystem's rules -- so the
            # same plan would match differently on Windows and Linux.
            return fnmatchcase(path, self.file)
        root = self.file.rstrip("/")
        if root in ("", "."):
            return False
        return path.startswith(root + "/")


def split_paths(file: str) -> tuple[str, ...]:
    """The paths one ``file`` field names: one for a step, several for a list.

    ``file`` is specified as a single path and the field is checked against the
    change set by `PlanStep.covers`, which knows an exact path, a glob and a
    directory. It does not know a *list*, and a list is what the field gets.

    A migration run submitted ``"go.work, go.work.sum"``, ``"cover.html,
    coverage, gin.log"`` and ``"docs/docs.go, docs/swagger.json"`` as three of
    its seven steps -- because the artefacts genuinely go together and the step
    cap is eight -- and each of those strings matched nothing. Not "matched
    loosely": nothing. So `_step_wants_removal` could not see that the step
    asked for the deletion it had just been given, the loop told the model it
    had lost a file the plan never mentioned, `_mark_steps` could not close the
    step on the write that followed, and `active_step` pinned the cursor there
    for the rest of the run. The model deleted and restored ``go.work`` four
    times in eight turns, obeying both instructions in turn.

    Splitting rather than refusing, for the reason `steps_for_phase` trims
    rather than refuses: that plan is a good plan in the wrong shape, and a
    plan objection spends a round trip and a slot in `MAX_PLAN_OBJECTIONS` to
    ask for a field the model already filled in correctly in substance. Split,
    each path is its own step, each is closable on its own, and the run can say
    which of the three artefacts it actually removed.

    Comma is the separator. A comma in a real repository path is vanishingly
    rare and a comma in this field is a list every time; the caller guards even
    that case by leaving a field that names something on disk alone. Whitespace
    splits only when *every* token looks like a path -- contains ``/`` or ``.``
    -- because a directory step is one bare word and ``"my docs/readme.md"`` is
    one path with a space in it, and splitting either would invent steps that
    can never be satisfied. ``cover.html, coverage, gin.log`` is why the comma
    form does not ask the same: ``coverage`` is a real file with no extension.

    Returns the field unchanged, as a one-tuple, whenever it is not a list.
    """
    text = file.strip()
    if not text:
        return ()
    if "," in text:
        parts = [token.strip() for token in text.split(",")]
    else:
        parts = text.split()
        if len(parts) > 1 and not all(_path_shaped(token) for token in parts):
            return (text,)
    parts = [token for token in parts if token]
    if len(parts) < 2 or len(parts) > MAX_STEP_PATHS:
        return (text,)
    # Deduplicated, preserving order: two identical steps are two cursors on one
    # file, and the second could never be closed by a write the first consumed.
    return tuple(dict.fromkeys(parts))


def _path_shaped(token: str) -> bool:
    """Whether a whitespace-separated token could be a path on its own."""
    return "/" in token or "." in token


#: The statuses a step may carry, and the two the model may set itself.
#:
#: ``written`` sits between ``pending`` and ``done`` and is the verification
#: node the plan did not have: a mutation on the step's file sets it, and only
#: a clean inner gate over that file promotes it to ``done``. Before it,
#: ``done`` meant "a write happened" and a file written wrongly was finished.
#:
#: ``blocked`` is the model's other word, and it means something ``skipped``
#: cannot: *this step cannot be done right now, and here is why*. Skipping
#: claims the work was unnecessary; blocking says it is necessary and
#: unreachable. The distinction matters at the end of the run, where one is a
#: decision and the other is a handover.
STEP_STATUSES = ("pending", "written", "done", "failed", "skipped", "blocked")
MODEL_STATUSES = ("pending", "skipped", "blocked")

#: How much of a `finish` answer reaches the developer.
#:
#: The cap exists because the answer goes out as one tool call and an unbounded
#: one is the reply most likely to meet the output limit. What it must not do is
#: bite on ordinary work: at 6,000 characters it did. A live validation run
#: delivered an answer of exactly 6,000 characters -- meaning it was cut -- and
#: the tokens were already spent by then, so the cap saved nothing and lost the
#: end of the analysis.
#:
#: Three numbers say this and they have to agree: this one, the `maxLength` on
#: `finish`'s schema, and the sentence in its description that the model
#: actually reads. Change one and change all three.
#:
#: Deliberately *not* `loop.CACHED_RESULT_CHARS`, which was also 6,000 and meant
#: something else entirely.
MAX_ANSWER_CHARS = 24_000


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
                phase=str(raw.get("phase", "") or "").strip(),
                part=str(raw.get("part", "") or "").strip(),
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
            phase=str(s.get("phase", "") or "").strip(),
            part=str(s.get("part", "") or "").strip(),
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
    # The roadmap, when there is one. Optional in the schema and checked by the
    # loop rather than here: only the loop knows whether this run is a migration,
    # and a tool that demanded phases of every plan would demand them of the
    # three-step bug fixes too. See `migration.plan_objection`.
    phases = [
        {
            "name": str(p.get("name", "") or "").strip(),
            "covers": str(p.get("covers", "") or "").strip(),
            "parts": str(p.get("parts", "") or "").strip(),
        }
        for p in (inv.arg("phases") or [])
        if isinstance(p, dict) and str(p.get("name", "") or "").strip()
    ]

    body = "\n".join(step.rendered(i) for i, step in enumerate(steps, 1))
    if phases:
        roadmap = "\n".join(
            f"{i}. {p['name']}"
            + (f" — {p['covers']}" if p["covers"] else "")
            + (f"\n   Parts: {p['parts']}" if p["parts"] else "")
            for i, p in enumerate(phases, 1)
        )
        body = f"Phases:\n{roadmap}\n\nSteps for this phase:\n{body}"
    if summary:
        body = f"{summary}\n\n{body}"

    # Recorded, not accepted -- the same distinction `finish` learned the hard
    # way, and for the same reason.
    #
    # This said "Plan accepted ... Work starts now, you hold the write tools
    # from this turn on", which the handler is in no position to know: the loop
    # reads the plan *after* this result is already in the transcript, and a
    # migration plan with no phases, or one naming a file too big to convert in
    # a single reply, is sent straight back. So on exactly the turn that matters
    # the model held two statements about the same call -- "work starts now"
    # from the tool, "that plan was not adopted" from the loop one message later
    # -- and a field session believed the first: it went looking for the write
    # tools it had been promised, was refused `git_ops` by mode five times, and
    # asked the developer the same question four times without ever planning.
    opening = f"submit_plan recorded, {len(steps)} step(s)."
    if phases:
        opening = (
            f"submit_plan recorded: {len(phases)} phase(s), {len(steps)} step(s). Only "
            "the open phase's steps become work; the rest of the roadmap is kept for "
            "when those phases open."
        )
    return ToolResult.success(
        f"{opening} Whether it is adopted, and what it starts, is decided after "
        f"this call.\n\n{body}",
        meta={
            "control": "plan",
            "summary": summary,
            "phases": phases,
            "steps": [
                {
                    "file": s.file,
                    "action": s.action,
                    "accepts": s.accepts,
                    "phase": s.phase,
                    "part": s.part,
                }
                for s in steps
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
    # Recorded, not delivered -- and the difference is the whole point.
    #
    # This used to say "Answered; the developer has your reply", which the
    # handler is in no position to know: `_phase_ended` reads the plan and the
    # gate *after* this result is already in the transcript, and a `finish` that
    # walks away from unwritten work is sent straight back. So on exactly the
    # turn that matters the model held two statements about the same call --
    # "the developer has your reply" from the tool, "Not yet" from the loop one
    # message later -- and acted on neither.
    # One line, because echoing the answer put it in the transcript twice and it
    # came back as a worked example on the next message of the session.
    body = "finish recorded. Whether it ends the phase is decided after this call."
    if cut:
        # The one thing about the answer the model still needs told, because it
        # is the one thing it can act on: the tail did not reach anybody.
        body += f" The last {cut:,} characters did not fit and were cut."
    if blocked:
        body += f" Recorded as blocked on: {blocked}"
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
            phase=str(s.get("phase", "") or "").strip(),
            part=str(s.get("part", "") or "").strip(),
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
                    "phase": s.phase,
                    "part": s.part,
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
