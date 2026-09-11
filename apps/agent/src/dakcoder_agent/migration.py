"""What a legacy-to-template migration changes about the way the loop runs.

A migration is not a large task. It is a task with a *different shape*, and five
of its properties break rules the rest of the loop is built on:

**It is red in the middle, on purpose.** The dependency swap lands in the first
phase and the last handler is converted in the sixth; between those two points
``go build`` cannot succeed, because half the service imports ``api-server`` and
half imports ``n-api-server``. The verification gate exists to say whether *this
run's* work is sound, and during a migration it can only ever say "no" --
seventy seconds spent reporting a failure that is the plan working as intended.
The baseline does not rescue it either: one taken mid-migration absorbs the
run's own breakage, so the gate would go from always-failing to always-passing,
which is worse. So the gate is **deferred** until the last phase closes, and
then it runs *without* a baseline, because a converted service that does not
build has not been converted.

**It is too big for one plan.** ``submit_plan`` caps at eight steps; a service
has forty handlers. A plan that tries to hold the whole conversion is a plan
whose cursor never advances and whose steps are directories. So the roadmap --
the phases, in order -- is held separately from the steps, and the step list is
only ever *the phase that is open*.

**Its phases are not atomic.** "Convert every handler" is a phase; it is also
eight distinct kinds of edit (the imports, the ``Base`` embed, ``Routes()``, the
DTO parameter, the error returns...). The roadmap therefore carries
sub-categories per phase, so the plan for a phase is written against a breakdown
that already exists rather than invented at the moment the phase opens.

**One file can be bigger than one reply.** A 6,571-line handler with fifty
methods is not a step; it is eight or nine, over the same path, each converting
the methods it names. A field run stopped exactly there -- four things converted,
then "the total volume exceeds what can be reliably done in a single session",
which was true and which its plan had no way to express. So a step naming a file
over ``BIG_FILE`` lines is refused at plan time, and a write lands on the step
being worked rather than on every step that names the file.

**It must not start on the branch the developer is standing on.** The SOP's
first step is cutting ``template-conversion`` from ``development`` so the
conversion is reviewable and revertible as one unit. That is a decision about
somebody else's repository, so it is confirmed rather than assumed, and until it
is made the run holds no write.

Everything here is state plus predicates over it. The loop does the acting; this
module answers *what kind of run is this, where is it, and what does that
forbid* -- in one place, so the rules cannot drift apart across the call sites
that read them. ``progress_document`` is the sixth thing here and the one the
model reads rather than obeys: a migration outlives a context window, so where
the last session got to has to be on disk.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any, Callable, Mapping, Sequence

__all__ = [
    "BASE_BRANCH",
    "BIG_FILE",
    "DEFAULT_BRANCH",
    "MIN_PARTS",
    "MIN_PHASES",
    "MigrationState",
    "PROGRESS_PATH",
    "PROTECTED",
    "Phase",
    "phases_from_meta",
    "plan_objection",
    "progress_document",
]

#: The branch the SOP cuts from, and the name it cuts.
BASE_BRANCH = "development"
DEFAULT_BRANCH = "template-conversion"

#: Branches a migration may not write on. Not a security boundary -- the agent
#: cannot push -- but the difference between a conversion a reviewer can read as
#: one diff and forty commits interleaved with everybody else's work.
PROTECTED = ("development", "develop", "main", "master", "release")

#: The roadmap has to be a roadmap. Two phases is a task with a comma in it, and
#: the point of phasing a migration is that the developer sees the shape of it
#: before the first file changes. The SOP has seven.
MIN_PHASES = 3

#: And each phase has to be broken down. A phase named "handlers" with nothing
#: under it is the eight-step plan this module exists to prevent, one
#: indirection later.
MIN_PARTS = 2

#: Lines above which one file is more than one step.
#:
#: The number comes from a field run that stopped dead: it converted four things
#: and then reported that `handler/paogen.go` was 6,571 lines with about fifty
#: handler methods, `handler/publicacct.go` and `handler/transferentry.go` were
#: comparable, and "the total volume exceeds what can be reliably done in a
#: single session". Every word of that was true, and the plan had one step for
#: each of those files.
#:
#: A step is a unit of work that has to fit in one reply. The acting phase gets
#: 16,384 output tokens, which is roughly 1,200 lines of Go written from
#: scratch, and a conversion also has to *read* the original. 800 lines is the
#: point past which a whole-file step is a step that cannot be finished --
#: `paogen.go` becomes nine steps, each of which is a reply and a checkpoint,
#: and the plan can say which of the nine are done.
BIG_FILE = 800


@dataclass(frozen=True, slots=True)
class Phase:
    """One phase of the roadmap: a name, what it covers, and its breakdown.

    ``parts`` is a comma-separated string rather than a list, and that is a
    concession to the model that has to emit it: an array nested inside an array
    of objects is the schema shape this endpoint gets wrong most often, and a
    breakdown is a line of text in every SOP a person ever wrote. It is split on
    read, so nothing downstream cares.
    """

    name: str
    covers: str = ""
    parts: str = ""
    #: ``pending`` or ``done``. Set by the loop when every step carrying this
    #: phase's name has settled, never by the model saying so -- the same rule
    #: the step statuses follow, for the same reason.
    status: str = "pending"

    @property
    def part_list(self) -> tuple[str, ...]:
        return tuple(p.strip() for p in self.parts.split(",") if p.strip())

    def as_dict(self) -> dict[str, str]:
        return {
            "name": self.name,
            "covers": self.covers,
            "parts": self.parts,
            "status": self.status,
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "Phase":
        status = str(raw.get("status") or "pending").strip().lower()
        return cls(
            name=str(raw.get("name") or "").strip(),
            covers=str(raw.get("covers") or "").strip(),
            parts=str(raw.get("parts") or "").strip(),
            status=status if status in ("pending", "done") else "pending",
        )


@dataclass
class MigrationState:
    """Where a migration is, and what that forbids this turn.

    Held on ``TaskState`` and carried between messages with the plan, because a
    migration outlives a message by definition: the roadmap is agreed on message
    one and phase five lands on message nine.
    """

    #: Whether this session is converting a service. Set from the classifier on
    #: the first message and then from the roadmap, which is the stronger
    #: evidence: a session with phases recorded is a migration whatever a later
    #: 160-token classification of "carry on" decides.
    active: bool = False
    #: The roadmap, in order.
    phases: tuple[Phase, ...] = ()
    #: The branch the work is on, once it exists, and what it was cut from.
    branch: str = ""
    base: str = ""
    #: How many phases have closed with a report to the developer. Counted so a
    #: checkpoint can say which one it is without two places recomputing it and
    #: disagreeing.
    closed: int = 0

    # -- where it is ------------------------------------------------------

    @property
    def current(self) -> tuple[int, Phase] | None:
        """The phase being worked, as ``(1-based index, phase)``, or ``None``.

        The first phase not yet done. ``None`` means every phase has closed,
        which is the one condition under which the gate runs.
        """
        for index, phase in enumerate(self.phases, 1):
            if phase.status != "done":
                return index, phase
        return None

    @property
    def complete(self) -> bool:
        return bool(self.phases) and self.current is None

    @property
    def defers_gate(self) -> bool:
        """Whether the verification gate must not run yet.

        The most consequential predicate here, and deliberately *not* "is this a
        migration". A migration whose last phase has closed is a service that is
        supposed to build, and skipping the gate there would turn a deferral
        into a permanent exemption -- the run would report DONE on a conversion
        nobody had compiled.
        """
        return self.active and not self.complete

    def phase_named(self, name: str) -> Phase | None:
        key = name.strip().lower()
        return next((p for p in self.phases if p.name.strip().lower() == key), None)

    # -- moving it --------------------------------------------------------

    def adopt(self, phases: Sequence[Phase]) -> None:
        """Install a roadmap, keeping the status of phases already closed.

        The rule ``_adopt_plan`` follows, for the reason it follows it: a
        re-submitted roadmap is the common case -- every phase opens with a
        ``submit_plan`` -- and a re-submission that reset the closed phases
        would send the run back to phase one with the work already on disk.
        """
        done = {p.name.strip().lower() for p in self.phases if p.status == "done"}
        self.phases = tuple(
            replace(p, status="done") if p.name.strip().lower() in done else p
            for p in phases
        )

    def close(self, name: str) -> bool:
        """Mark one phase done. ``True`` if that changed anything."""
        key = name.strip().lower()
        before = self.phases
        self.phases = tuple(
            replace(p, status="done") if p.name.strip().lower() == key else p
            for p in self.phases
        )
        if self.phases != before:
            self.closed += 1
            return True
        return False

    # -- rendering --------------------------------------------------------

    def working(self, named: str = "") -> tuple[int, Phase] | None:
        """The phase the plan is actually on, as ``(index, phase)``, or ``None``.

        ``current`` is the first phase not yet done, which is the roadmap's
        opinion. ``named`` is the phase the plan's steps carry, which is the
        plan's -- and the plan wins, because it is the commitment. They differ
        legitimately: a developer who has already cut the branch has the run
        open at phase two while phase one is still pending, and a loop that
        insisted on the roadmap's answer would close the wrong phase when that
        work settled.
        """
        if named:
            key = named.strip().lower()
            for index, phase in enumerate(self.phases, 1):
                if phase.name.strip().lower() == key and phase.status != "done":
                    return index, phase
        return self.current

    def block(self, named: str = "") -> list[str]:
        """The roadmap as a cursor, for the state block, or ``[]``.

        A cursor and not a list, one level up from ``_plan_block`` and for its
        argument: a model shown a seven-item checklist works on seven items. It
        is shown where it is, what this phase breaks into, and what comes next.
        """
        if not self.active:
            # Nothing at all for the run this is not about, which is almost
            # every run. The block is rendered on every turn in every mode, and
            # a question that gets migration advice is a question whose answer
            # is now competing for the last position in the prompt.
            return []
        if not self.phases:
            # The instruction lives here, not in the mode overlay, and the
            # difference is what it costs: an overlay is paid on every turn of
            # every task, and this is true of one task in fifty. It says what the
            # next call has to contain, which is the only thing a Planner about
            # to submit an eight-step plan for a forty-file conversion needs.
            return [
                "Migration: no roadmap yet. `submit_plan` needs `phases` — the "
                f"conversion in at least {MIN_PHASES} phases, in order, each with "
                f"`parts` naming at least {MIN_PARTS} sub-categories — and `steps` "
                "for the first phase only. Read @skill:legacy-migration for the "
                "phases and the rules.",
                "  A file over "
                f"{BIG_FILE:,} lines is split across several steps, each naming the "
                "methods or the line range it converts. One step per file does not "
                "survive a 6,500-line handler.",
            ]
        total = len(self.phases)
        here = self.working(named)
        if here is None:
            return [f"Migration: all {total} phase(s) closed — the gate runs now."]
        index, phase = here
        head = f"Migration: phase {index} of {total} — {phase.name}"
        if phase.covers:
            head += f" ({phase.covers})"
        lines = [head]
        if parts := phase.part_list:
            lines.append("  Parts: " + ", ".join(parts))
        nxt = next(
            (
                (i, p)
                for i, p in enumerate(self.phases, 1)
                if i != index and p.status != "done"
            ),
            None,
        )
        lines.append(
            f"  Then: phase {nxt[0]} — {nxt[1].name}"
            if nxt
            else "  Then: nothing — this is the last phase still open."
        )
        if self.branch:
            lines.append(f"  Branch: {self.branch}" + (f" (cut from {self.base})" if self.base else ""))
        lines.append(
            "  The gate is deferred until the last phase closes: a half-converted "
            "service cannot build, so a failing gate now would say nothing. Close "
            "this phase and stop; the developer decides when the next one opens."
        )
        return lines

    # -- disk -------------------------------------------------------------

    def as_dict(self) -> dict[str, Any]:
        return {
            "active": self.active,
            "branch": self.branch,
            "base": self.base,
            "closed": self.closed,
            "phases": [p.as_dict() for p in self.phases],
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "MigrationState":
        phases = tuple(
            Phase.from_dict(p) for p in raw.get("phases") or () if isinstance(p, Mapping)
        )
        return cls(
            active=bool(raw.get("active")) or bool(phases),
            phases=phases,
            branch=str(raw.get("branch") or ""),
            base=str(raw.get("base") or ""),
            closed=int(raw.get("closed") or 0),
        )


def phases_from_meta(meta: Mapping[str, Any]) -> tuple[Phase, ...]:
    """Rebuild the roadmap from a ``submit_plan`` result's ``meta``."""
    return tuple(
        Phase.from_dict(p) for p in meta.get("phases") or () if isinstance(p, Mapping)
    )


def plan_objection(
    state: "MigrationState",
    phases: Sequence[Phase],
    steps: Sequence[Any],
    lines: "Callable[[str], int] | None" = None,
) -> str:
    """Why this plan is not a migration plan, or ``""``.

    Read by the loop when a migration's ``submit_plan`` comes back, and answered
    to the model as an ordinary message. It is not in the ``submit_plan`` handler
    because the handler cannot know which kind of run it is in, and a tool that
    refused every unphased plan in the product would refuse the ordinary ones
    too.

    The objections are ordered the way a plan fails them, and exactly one is
    returned: a model handed four complaints at once answers the last.
    """
    roadmap = tuple(phases) or state.phases
    if not roadmap:
        return (
            "this is a service migration and the plan has no phases. Send `phases` "
            f"as well: at least {MIN_PHASES}, in execution order, each with `covers` "
            f"in one line and `parts` naming at least {MIN_PARTS} sub-categories it "
            "breaks into. The SOP's are branch, dependencies, handlers, DTOs and "
            "validation, bootstrap and FX, tests, swagger"
        )
    if len(roadmap) < MIN_PHASES:
        return (
            f"the roadmap has {len(roadmap)} phase(s). A migration is planned in at "
            f"least {MIN_PHASES}, in execution order — a conversion delivered in one "
            "or two lumps is the one that cannot be reviewed and cannot be resumed"
        )
    if thin := [p.name or "(unnamed)" for p in roadmap if len(p.part_list) < MIN_PARTS]:
        return (
            "these phases name no breakdown: "
            + ", ".join(thin[:4])
            + f". Every phase needs `parts` listing at least {MIN_PARTS} "
            "sub-categories, comma-separated — the kinds of edit it contains — so "
            "the plan for it is written against a breakdown instead of invented "
            "when the phase opens"
        )

    names = {p.name.strip().lower() for p in roadmap}
    if unnamed := [s for s in steps if not str(getattr(s, "phase", "") or "").strip()]:
        return (
            "every step has to say which phase it belongs to, and "
            f"{len(unnamed)} of {len(steps)} do not. Set `phase` on each step to one "
            "of: " + ", ".join(p.name for p in roadmap)
        )
    stray = sorted(
        {
            str(s.phase).strip()
            for s in steps
            if str(s.phase).strip().lower() not in names
        }
    )
    if stray:
        return (
            "these steps name a phase that is not in the roadmap: "
            + ", ".join(stray[:4])
            + ". Use one of: "
            + ", ".join(p.name for p in roadmap)
        )

    spread = {str(s.phase).strip().lower() for s in steps}
    if len(spread) > 1:
        return (
            f"this plan spans {len(spread)} phases at once. Plan one phase at a time: "
            "the steps for the phase that is open now, and the roadmap for the rest. "
            "A migration submitted as one plan is a migration nobody sees until it "
            "is finished"
        )

    # A file bigger than one reply is bigger than one step.
    #
    # This is the objection that would have changed the field run: it converted
    # four things and then stopped, correctly, because its plan had one step for
    # `handler/paogen.go` and that file is 6,571 lines. A step is a unit of work
    # that has to fit in a reply; a whole-file step on a file that size is a
    # step nothing can finish, and the run discovers it only after reading the
    # file. Split at plan time and each piece is a reply, a checkpoint and a
    # line in the progress record that says it is done.
    if lines is not None:
        counted = {}
        for step in steps:
            path = str(getattr(step, "file", "") or "")
            if path:
                counted.setdefault(path, 0)
                counted[path] += 1
        big = [
            (path, n)
            for path in counted
            if counted[path] == 1 and (n := lines(path)) > BIG_FILE
        ]
        if big:
            worst = max(big, key=lambda item: item[1])
            return (
                f"{worst[0]} is {worst[1]:,} lines and the plan gives it one step. "
                "One reply cannot convert it, so that step can never be finished. "
                "Split it: several steps naming the same file, each saying in "
                "`action` which methods or which line range it converts, and put "
                "the group in `part`. "
                + (
                    ", ".join(f"{p} ({n:,} lines)" for p, n in big[:3])
                    + " all need this."
                    if len(big) > 1
                    else "Roughly one step per "
                    + f"{BIG_FILE:,} lines."
                )
            )

    closed = {p.name.strip().lower() for p in state.phases if p.status == "done"}
    if spread & closed:
        return (
            "these steps belong to "
            + next(iter(spread & closed))
            + ", which has already closed. Plan the next phase that is still open: "
            + ", ".join(p.name for p in roadmap if p.name.strip().lower() not in closed)
        )
    return ""


#: Where the progress record lives, workspace-relative.
#:
#: In `.dakcoder` beside the session plans rather than at the repository root,
#: because it is the agent's bookkeeping and not an artefact of the conversion
#: -- a migration that left a `MIGRATION.md` in the diff would be asking a
#: reviewer to review it.
PROGRESS_PATH = ".dakcoder/migration.md"


def progress_document(
    state: "MigrationState",
    plan: Sequence[Any],
    touched: Sequence[str] = (),
    routes: int = 0,
) -> str:
    """The migration's progress, as a document, from ground truth.

    Written by the loop and never by the model, which is the whole of its value.
    A field session wrote its own ``migration.md`` by hand, in prose, and then
    read it back as evidence of work it had not done; this one is rendered from
    the roadmap and the step statuses, both of which come from the change set.

    It exists because a migration outlives a context window. Ten thousand lines
    across seven files is more than one session, so the question every later
    session opens with is *where did the last one get to* -- and the honest
    answers available before this were the transcript, which compaction eats,
    and the model's own summary, which is prose. One `read_file` on this answers
    it in a few hundred tokens.
    """
    out = ["# Migration progress", ""]
    out.append(
        f"Branch: `{state.branch}`"
        + (f", cut from `{state.base}`" if state.base else "")
        if state.branch
        else "Branch: not cut yet."
    )
    out.append(
        f"Routes recorded before the migration: {routes}"
        if routes
        else "Routes recorded before the migration: none — the inventory was not taken, "
        "so a lost route will not be caught automatically."
    )
    out.append("")

    if state.phases:
        done = sum(1 for p in state.phases if p.status == "done")
        out.append(f"## Phases — {done} of {len(state.phases)} closed")
        out.append("")
        for index, phase in enumerate(state.phases, 1):
            mark = "x" if phase.status == "done" else " "
            out.append(f"- [{mark}] **{index}. {phase.name}** — {phase.covers}")
            if parts := phase.part_list:
                out.append(f"      parts: {', '.join(parts)}")
        out.append("")

    if plan:
        here = state.working(_plan_phase(plan))
        out.append(
            f"## Steps — phase {here[0]}, {here[1].name}" if here else "## Steps"
        )
        out.append("")
        for index, step in enumerate(plan, 1):
            mark = "x" if step.status in ("done", "skipped") else " "
            where = f" *({step.part})*" if step.part else ""
            out.append(
                f"- [{mark}] {index}. `{step.file}`{where} — {step.action}"
                + (f"  → **{step.status}**" if step.status not in ("done", "pending") else "")
            )
            if step.note:
                out.append(f"      note: {step.note}")
        out.append("")

    if remaining := [p.name for p in state.phases if p.status != "done"]:
        out.append("## Still to do")
        out.append("")
        out.extend(f"- {name}" for name in remaining)
        out.append("")

    if touched:
        out.append("## Files this session changed")
        out.append("")
        out.extend(f"- `{path}`" for path in touched[:60])
        if len(touched) > 60:
            out.append(f"- ...and {len(touched) - 60} more")
        out.append("")

    out.append(
        "> Written by dakcoder from the plan and the change set. Edits here are "
        "overwritten; change the plan instead."
    )
    return "\n".join(out) + "\n"


def _plan_phase(plan: Sequence[Any]) -> str:
    """The phase a plan's steps carry. Mirrors ``AgentLoop._plan_phase``."""
    for step in plan:
        if getattr(step, "open", False) and getattr(step, "phase", ""):
            return str(step.phase)
    return next((str(s.phase) for s in plan if getattr(s, "phase", "")), "")
