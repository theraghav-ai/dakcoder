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

from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
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
    "steps_for_phase",
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

#: How many log lines the plan document keeps. A migration is seven
#: phases; a log that grew without bound would be the longest thing in a
#: document whose value is being short enough to read.
MAX_LOG = 40

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
    #: What has happened, newest last. Appended by `close`, so the document
    #: can say when each phase finished rather than only that it did.
    log: tuple[str, ...] = ()

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
            phase = self.phase_named(name)
            self.log = (
                *self.log,
                f"{_now()} — phase {self.closed} of {len(self.phases)}, "
                f"{phase.name if phase else name}, closed",
            )[-MAX_LOG:]
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
        lines.append(f"  Plan and progress: {PROGRESS_PATH} (written for you, kept current)")
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
            "log": list(self.log),
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
            log=tuple(str(x) for x in raw.get("log") or ()),
        )


def _now() -> str:
    """An ISO timestamp to the minute. Seconds would rewrite the document
    on every turn for no information."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


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

    # A plan that spans phases is *trimmed*, not refused. See `steps_for_phase`.
    spread = {str(s.phase).strip().lower() for s in steps}

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


#: Where the plan document lives, workspace-relative.
#:
#: **This exact path is a contract with the extension.** `MIGRATION_PLAN_PATH`
#: in `extension/src/wizard.ts` is the same string: the Migration view watches
#: it, parses it, renders a row per unit, and its empty state tells the
#: developer the plan will be written here. Nothing had ever written it. So the
#: view was permanently empty, the welcome text pointed at a file that did not
#: exist, and a developer whose migration was under way had -- exactly as
#: reported -- nowhere to see the plan or track progress.
#:
#: Under `.dakcoder` rather than at the repository root because it is the
#: agent's record, not an artefact of the conversion: a migration that left a
#: `MIGRATION.md` in its own diff would be asking a reviewer to review it.
PROGRESS_PATH = ".dakcoder/migration/plan.md"


def steps_for_phase(phases: Sequence[Phase], steps: Sequence[Any]) -> tuple[list[Any], list[Any]]:
    """Split a submitted plan into the open phase's steps and the rest.

    Trimming rather than refusing, and the difference is a run that starts
    against one that argues.

    A model asked for a seven-phase roadmap and "the steps for the phase that is
    open" sends the roadmap and the steps for the first two, because it has just
    thought about both and the second is the interesting one. That is a good
    plan submitted in the wrong shape, and a field session spent its whole
    budget on it: the plan was refused, the model re-planned, the refusal fired
    again, and between the two it fixated on a branch it could not cut. Nothing
    was ever adopted, so the run never reached the phase that acts.

    So the open phase's steps are adopted and the rest are handed back to the
    caller to report. Nothing is lost that was not going to be re-planned
    anyway: the later phase opens with its own `submit_plan`, against a
    workspace this phase will have changed.

    The open phase is the one the earliest step names, not the roadmap's first
    pending one -- a developer whose branch already exists opens at phase two,
    and taking the roadmap's answer would trim away everything they asked for.
    """
    ordered = list(steps)
    first = next((str(s.phase).strip().lower() for s in ordered if str(s.phase).strip()), "")
    if not first:
        return ordered, []
    # Whichever phase comes first in the *roadmap* among those the steps name,
    # so a plan listing phase two before phase one is still trimmed to phase one.
    named = {str(s.phase).strip().lower() for s in ordered if str(s.phase).strip()}
    for phase in phases:
        key = phase.name.strip().lower()
        if key in named and phase.status != "done":
            first = key
            break
    keep = [s for s in ordered if str(s.phase).strip().lower() == first]
    rest = [s for s in ordered if str(s.phase).strip().lower() != first]
    return keep, rest


def progress_document(
    state: "MigrationState",
    plan: Sequence[Any],
    touched: Sequence[str] = (),
    routes: int = 0,
) -> str:
    """The migration plan and where it has got to, as one document.

    Rendered from the roadmap and the step statuses -- both ground truth, both
    derived from the change set -- and never from anything the model says about
    its own progress. A field session wrote its own ``migration.md`` by hand, in
    prose, and then read it back as evidence of work it had not done.

    It exists because a migration outlives a context window. Ten thousand lines
    across seven files is more than one session, so the question every later
    session opens with is *where did the last one get to*, and the honest
    answers available before this were the transcript, which compaction eats,
    and the model's own summary, which is prose.

    **The shape is a contract with the extension.** The Migration view parses
    this file: it takes the first markdown table carrying a path-like column and
    reads a unit from every row. Two consequences, and getting either wrong
    empties the view:

    * the Units table below is the only table here with a ``Unit`` column;
    * nothing else in the document may be a ``- [ ]`` task line, because the
      parser reads *every* one of those as a unit too. The phases are a numbered
      list for that reason, not a checklist.
    """
    done = sum(1 for p in state.phases if p.status == "done")
    out = [
        "# Migration plan",
        "",
        "Generated by dakcoder. Edits are overwritten on the next turn — to change",
        "the work, say so in the chat and the plan is rewritten from there.",
        "",
    ]

    out.append(
        f"- **Branch:** `{state.branch}`" + (f", cut from `{state.base}`" if state.base else "")
        if state.branch
        else "- **Branch:** not cut yet — nothing is written until it exists."
    )
    out.append(
        f"- **Phases:** {done} of {len(state.phases)} closed"
        if state.phases
        else "- **Phases:** not planned yet"
    )
    out.append(
        f"- **Routes recorded before the migration:** {routes} — a route lost in "
        "conversion is caught when the last phase closes"
        if routes
        else "- **Routes recorded before the migration:** none. A route lost in "
        "conversion will not be caught automatically."
    )
    out.append(f"- **Updated:** {_now()}")
    out.append("")

    # -- the phases ------------------------------------------------------
    #
    # Numbered, never checkboxed: a `- [ ]` line is a unit to the view's parser,
    # and a phase rendered that way would appear in the tree as a file that does
    # not exist.
    if state.phases:
        out += ["## Phases", ""]
        here = state.working(_plan_phase(plan))
        for index, phase in enumerate(state.phases, 1):
            covered = [
                s
                for s in plan
                if str(getattr(s, "phase", "")).strip().lower() == phase.name.strip().lower()
            ]
            settled = sum(1 for s in covered if s.status in ("done", "skipped"))
            if phase.status == "done":
                mark = "**done**"
            elif here is not None and here[0] == index:
                mark = "**open**"
            elif covered and settled == len(covered):
                # Every step in it is settled and nothing has closed it yet.
                # Saying "pending" there would have the document contradict the
                # line under it, which is how a progress record stops being
                # read.
                mark = "ready to close"
            else:
                mark = "pending"
            out.append(f"{index}. **{phase.name}** — {phase.covers or 'no summary'} · {mark}")
            if parts := phase.part_list:
                out.append(f"   - Parts: {', '.join(parts)}")
            if covered:
                out.append(f"   - Steps: {settled} of {len(covered)} settled")
        out.append("")

    # -- the units, in the shape the view reads --------------------------
    if plan:
        out += [
            "## Units",
            "",
            "| Unit | Kind | Classification | Status | Rules | Commit |",
            "|---|---|---|---|---|---|",
        ]
        for step in plan:
            kind = step.part or step.phase or ""
            # SKIP is the view's word for "deliberately excluded", which is
            # exactly what a skipped step is. Everything else is work.
            classification = "SKIP" if step.status == "skipped" else "MIGRATE"
            note = (step.note or step.action or "").replace("|", "/")
            out.append(
                f"| {step.file} | {kind} | {classification} | {step.status} | "
                f"{note[:90]} | |"
            )
        out.append("")

    # -- what is left ----------------------------------------------------
    remaining = [p for p in state.phases if p.status != "done"]
    if remaining:
        out += ["## Still to do", ""]
        for phase in remaining:
            out.append(f"- **{phase.name}** — {phase.covers or 'no summary'}")
        out += [
            "",
            "Each phase is planned when it opens, against the workspace the phase",
            "before it left. Say when to start the next one.",
            "",
        ]
    elif state.phases:
        out += [
            "## Still to do",
            "",
            "Nothing — every phase has closed. The verification gate runs on the",
            "whole conversion, including the route check against the inventory taken",
            "before it started.",
            "",
        ]

    if touched:
        out += ["## Files changed this session", ""]
        out += [f"- `{path}`" for path in touched[:60]]
        if len(touched) > 60:
            out.append(f"- ...and {len(touched) - 60} more")
        out.append("")

    if state.log:
        out += ["## Log", ""]
        out += [f"- {entry}" for entry in state.log]
        out.append("")

    return "\n".join(out).rstrip() + "\n"


def _plan_phase(plan: Sequence[Any]) -> str:
    """The phase a plan's steps carry. Mirrors ``AgentLoop._plan_phase``."""
    for step in plan:
        if getattr(step, "open", False) and getattr(step, "phase", ""):
            return str(step.phase)
    return next((str(s.phase) for s in plan if getattr(s, "phase", "")), "")
