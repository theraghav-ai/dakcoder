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

**It is too big for one plan.** ``submit_plan`` caps at ``MAX_STEPS`` steps; a
service has forty handlers. A plan that tries to hold the whole conversion is a plan
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

import json
import os
import re
import tempfile

from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from .tools.registry import MAX_STEPS

__all__ = [
    "BASE_BRANCH",
    "BIG_FILE",
    "DEFAULT_BRANCH",
    "MIN_PARTS",
    "MIN_PHASES",
    "MigrationState",
    "PROGRESS_PATH",
    "PROTECTED",
    "ARCHIVE_DIR",
    "MARKER_PATH",
    "RECORD_PATH",
    "SETTLED",
    "archive_record",
    "load_record",
    "merged_roadmap",
    "record_removed",
    "save_record",
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
#: 32,768 output tokens, which is roughly 2,400 lines of Go written from
#: scratch, and a conversion also has to *read* the original. 800 lines is the
#: point past which a whole-file step is a step that cannot be finished --
#: `paogen.go` becomes nine steps, each of which is a reply and a checkpoint,
#: and the plan can say which of the nine are done.
#:
#: **The threshold did not move when the budget doubled**, and that is on
#: purpose. It was never 16,384 divided by a line length: the reply also carries
#: the prose and the tool name, the model has to have read the original in an
#: earlier turn and hold it, and a step is a *checkpoint* as well as a unit of
#: output -- nine resumable pieces beat four that each lose more when one fails.
#: What the extra room buys is headroom inside a step, not bigger steps.
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
    #: Every file each phase has been planned to cover, by phase key.
    #:
    #: What a phase *is*, as opposed to what the plan open right now holds. A
    #: phase too big for one plan is planned a file at a time -- that is what
    #: `plan_objection` asks for -- and a phase judged by the plan in hand
    #: closed the moment the first file's steps settled, after which every plan
    #: naming the rest was refused as belonging to a closed phase. Recorded from
    #: every plan submitted for the phase, adopted or refused, and from the
    #: steps trimmed off into a later phase, so none of them can be forgotten.
    backlog: dict[str, list[str]] = field(default_factory=dict)
    #: Of those, the files split across several conversion steps, by phase key.
    #: Their completion is asked of the code (``handler_map``), not only of the
    #: step statuses: a step can settle with methods still on gin.
    split: dict[str, list[str]] = field(default_factory=dict)
    #: Every step any session has planned for this migration, with the status
    #: it last had, keyed by phase, file and part. The plan in hand is one
    #: session's; this is the migration's, and it is what a new session reads
    #: to know that three of paogen.go's nine groups are already done.
    units: dict[str, dict[str, str]] = field(default_factory=dict)
    #: Every file the migration has changed, across sessions.
    files: tuple[str, ...] = ()
    #: Routes the legacy service served before the conversion started.
    routes: int = 0
    #: Every file the migration has to convert, from ``legacy_audit``, with its
    #: kind, its legacy finding count and a status. See ``update_scope``.
    scope: dict[str, dict[str, str]] = field(default_factory=dict)
    #: Set when this state was restored from an earlier session's record
    #: rather than built by this one. Not persisted: it is a fact about the run.
    resumed: bool = False
    #: The migration's branch, when the workspace is currently on another one.
    #: ``branch`` is cleared in that case so the write guard holds, and this is
    #: what the state block names. Not persisted separately.
    expected_branch: str = ""

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

        **And a re-submission never shrinks the record.** See `merged_roadmap`:
        the workspace record of pao-backend went from seven phases to five (the
        two closed ones dropped) to one, because each resumed session re-sent
        only the phases it was thinking about and this installed them whole.
        """
        merged = merged_roadmap(self.phases, phases)
        done = {p.name.strip().lower() for p in self.phases if p.status == "done"}
        self.phases = tuple(
            replace(p, status="done") if p.name.strip().lower() in done else p
            for p in merged
        )

    def evidenced(self, name: str) -> bool:
        """Whether this phase is complete on evidence the loop holds independently.

        One phase has that property and it is the one that kept hanging. The
        branch phase is finished when a branch exists that is not a shared one,
        and `branch` records exactly that -- set from what ``git_ops`` reported
        it had checked out, which is the only statement about the repository
        that cannot be wrong. Everything else about phase completion is inferred
        from whether *file-level* steps have settled, and a branch cut writes no
        file.

        That inference is what failed. A one-step branch plan, skipped because
        the branch already existed, is a file-level accident deciding a
        phase-level question; a branch phase whose step stays open for any
        reason hangs the whole roadmap on bookkeeping while the fact it is
        about sits in this object, true and unread.

        Read off the phase's own typed ``name`` and ``covers`` -- fields the
        model filled in to say what the phase is -- which is the same licence
        ``_step_wants_removal`` takes with ``action``. Narrow on purpose: it
        answers only for a phase that says it is about cutting the branch, and
        every other phase closes the way it always did.
        """
        phase = self.phase_named(name)
        if phase is None:
            return False
        said = f"{phase.name} {phase.covers}".lower()
        if "branch" not in said:
            return False
        # The branch counts when it exists, checked out or not: a resumed run
        # on another branch is told to switch back to it, never to cut another.
        branch = (self.branch or self.expected_branch).strip().lower()
        return bool(branch) and branch not in PROTECTED

    def close_evidenced(self) -> list[str]:
        """Close every leading open phase that is finished on evidence. Their names.

        Only the branch phase has such evidence (see ``evidenced``), and it is
        the phase that kept being redone: it used to close only when a run
        reached `finish`, so a run stopped or cut off after the branch was cut
        left it open -- and every later session was told, in its state block,
        to ask which branch to cut from and to cut it again, on a branch it was
        already standing on.
        """
        closed: list[str] = []
        while (
            (here := self.current) is not None
            and self.evidenced(here[1].name)
            and _branch_only(here[1])
        ):
            if not self.close(here[1].name):
                break
            closed.append(here[1].name)
        return closed

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

    def block(self, named: str = "", *, mode: str = "planner") -> list[str]:
        """The roadmap as a cursor, for the state block, or ``[]``.

        A cursor and not a list, one level up from ``_plan_block`` and for its
        argument: a model shown a seven-item checklist works on seven items. It
        is shown where it is, what this phase breaks into, and what comes next.

        **Written for the phase that reads it.** ``mode`` is the loop's mode
        (``ask``, ``planner`` or ``agent``). The block used to tell every phase
        what only the Planner can do -- "`submit_plan` with the same `phases`"
        -- and in session 9ba77962405b an ASK turn and seven PLANNER turns were
        each told a move they did not hold, in the last position of the prompt.
        """
        if not self.active:
            # Nothing at all for the run this is not about, which is almost
            # every run. The block is rendered on every turn in every mode, and
            # a question that gets migration advice is a question whose answer
            # is now competing for the last position in the prompt.
            return []
        if self.phases and self.resumed and not named:
            return self.resume_block(mode=mode)
        if not self.phases and mode != "planner":
            # The instruction below is a Planner's: only it holds `submit_plan`.
            return ["Migration: under way, with no roadmap recorded yet."]
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
        if self.scope:
            counts: dict[str, list[int]] = {}
            for entry in self.scope.values():
                seen = counts.setdefault(entry.get("kind", "other"), [0, 0])
                seen[1] += 1
                seen[0] += entry.get("status") != "legacy"
            lines.append(
                "  Scope converted: "
                + ", ".join(f"{k} {c[0]}/{c[1]}" for k in KINDS if (c := counts.get(k)))
                + f" (still-legacy files listed in {PROGRESS_PATH})"
            )
        planned_now = {u.get("file") for u in self.units.values() if u.get("phase_key") == _key(phase.name)}
        if later := [f for f in self.backlog.get(_key(phase.name), []) if f not in planned_now]:
            # The remedy names a tool the reading phase holds: `revise_plan` is
            # the acting phase's, and the Planner's way to add a step is the plan.
            remedy = {
                "agent": " One that needs no change: `revise_plan` it in as a skipped "
                "step, saying why.",
                "planner": " Each still gets a step; one that needs no change says so "
                "in its `action`, and the acting phase marks it skipped.",
            }.get(mode, "")
            lines.append(
                f"  Also in this phase, not in this plan ({len(later)}): {_clip(later)}. "
                "Finish this plan first; the phase stays open until they are done."
                + remedy
            )
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
        # "Close this phase and stop" read, to a phase with twelve steps still
        # pending, as an instruction to stop now -- beside a cursor saying "Now:
        # step 1" and a stall message saying "give the developer what you have".
        # What it meant is that the *next* phase is the developer's to open.
        lines.append(
            "  The gate is deferred until the last phase closes: a half-converted "
            "service cannot build, so a failing gate now would say nothing. When "
            "this phase's steps are done the run ends there; the developer decides "
            "when the next phase opens."
        )
        return lines

    def resume_block(self, *, mode: str = "planner") -> list[str]:
        """What a session that picks up an earlier session's migration is told.

        Rendered until the session adopts a plan of its own. Everything in it is
        read off the record -- phases closed by the loop, unit statuses set from
        the change set -- so "where did the last session get to" is answered
        from evidence, not from anyone's summary.
        """
        total = len(self.phases)
        done = [p.name for p in self.phases if p.status == "done"]
        # "Do not re-plan" sat one line above "`submit_plan` ... steps", and
        # beside a plan check demanding a new roadmap of three phases: three
        # instructions, at most two of which could be obeyed at once.
        lines = [
            f"Migration: RESUMING — {len(done)} of {total} phase(s) closed in earlier "
            "sessions" + (f" ({', '.join(done)})" if done else "") + ". The roadmap "
            "below is restored from the record and stays as it is; do not redo a "
            "closed phase.",
        ]
        here = self.current
        if here is None:
            lines.append("  Every phase has closed — the gate runs now.")
            return lines
        index, phase = here
        key = _key(phase.name)
        mine = [u for u in self.units.values() if u.get("phase_key") == key]

        def label(unit: Mapping[str, str]) -> str:
            return unit["file"] + (f" [{unit['part']}]" if unit.get("part") else "")

        lines.append(
            f"  Open: phase {index} of {total} — {phase.name}"
            + (f" ({phase.covers})" if phase.covers else "")
        )
        if settled := list(dict.fromkeys(label(u) for u in mine if u.get("status") in SETTLED)):
            lines.append(f"  Already done in it ({len(settled)}): " + _clip(settled))
        if pending := list(dict.fromkeys(label(u) for u in mine if u.get("status") not in SETTLED)):
            lines.append(f"  Planned, not finished ({len(pending)}): " + _clip(pending))
        planned = {u.get("file") for u in mine}
        if untouched := [f for f in self.backlog.get(key, []) if f not in planned]:
            lines.append(f"  Not planned yet ({len(untouched)}): " + _clip(untouched))
        if summary := self.scope_summary():
            lines.append("  Scope, from legacy_audit:")
            lines.extend(f"    {line}" for line in summary)
        if mode == "planner":
            # Steps only. "The same `phases`" was read as "the phases you mean
            # to work", and a model that sent `phases=[handlers]` replaced a
            # seven-phase roadmap with one phase (9ba77962405b, turn 11).
            lines.append(
                f"  `submit_plan` with `steps` for what is left of {phase.name} only, "
                "each with `phase` set to it. Leave `phases` out: the roadmap is "
                "already recorded. For a split file run `handler_map` first: a group "
                "it marks done needs no step."
            )
        elif mode == "ask":
            lines.append(
                "  This phase answers the developer and cannot write: answer what "
                "they asked. The migration carries on when they ask for it."
            )
        else:
            lines.append(
                f"  Work the steps planned for what is left of {phase.name}; a group "
                "`handler_map` marks done needs no edit."
            )
        if self.expected_branch:
            lines.append(
                f"  Branch: the conversion is on `{self.expected_branch}` and the "
                "workspace is not. The acting phase switches back first "
                f"(`git_ops` op=branch message={self.expected_branch}); writes are "
                "held until then."
            )
        elif self.branch:
            lines.append(f"  Branch: {self.branch}")
        lines.append(f"  Record: {PROGRESS_PATH}")
        return lines

    # -- the migration's own ledger ---------------------------------------

    def note_planned(self, phase: str, files: Sequence[str]) -> None:
        """Remember that ``phase`` covers ``files``, whatever happens to the plan.

        Keyed by the phase's name even before the roadmap naming it is adopted:
        the first submission is often refused, and it is the one that lists
        every file.
        """
        if not _key(phase):
            return
        known = self.backlog.setdefault(_key(phase), [])
        for path in files:
            if path and path not in known and not _bookkeeping(path):
                known.append(path)

    def note_split(self, phase: str, files: Sequence[str]) -> None:
        """Remember that these files are converted across several steps of ``phase``."""
        if not _key(phase):
            return
        known = self.split.setdefault(_key(phase), [])
        for path in files:
            if path and path not in known:
                known.append(path)

    def record_steps(
        self,
        steps: Sequence[Any],
        touched: Sequence[str] = (),
        methods: "Callable[[Any], Sequence[str]] | None" = None,
    ) -> None:
        """Fold the plan in hand into the migration's ledger.

        A step keeps its unit across sessions by key, so re-planning the same
        group in a later session updates its row rather than adding a second.
        The key is the phase, the file, and what the step converts:

        * the handler methods it names, when ``methods`` can say (a split file);
        * else its ``part``, when no other step on the file shares it;
        * else ``part`` and ``action`` together.

        ``part`` alone was the key, and a planner that labels every step of
        paogen.go ``part: "paogen"`` -- which it did -- collapsed nine groups
        into one row: each overwrote the last, and the record showed group 9
        pending and nothing about the eight before it.
        """
        shared: dict[tuple[str, str, str], int] = {}
        for step in steps:
            ident = (
                _key(str(getattr(step, "phase", "") or "")),
                str(getattr(step, "file", "") or "").strip(),
                _key(str(getattr(step, "part", "") or "")),
            )
            shared[ident] = shared.get(ident, 0) + 1
        for step in steps:
            path = str(getattr(step, "file", "") or "").strip()
            if not path:
                continue
            raw_phase = str(getattr(step, "phase", "") or "").strip()
            phase = raw_phase
            if not phase and (current := self.current) is not None:
                phase = current[1].name
            part = str(getattr(step, "part", "") or "").strip()
            action = str(getattr(step, "action", "") or "").strip()
            names: Sequence[str] = ()
            if methods is not None:
                try:
                    names = tuple(methods(step) or ())
                except Exception:  # noqa: BLE001 - a key hint, never a failure
                    names = ()
            if names:
                what = "methods:" + ",".join(sorted(names))
            elif part and shared.get((_key(raw_phase), path, _key(part)), 0) <= 1:
                what = part
            else:
                what = f"{part} {action}".strip()
            key = _unit_key(phase, path, what)
            status = str(getattr(step, "status", "") or "pending")
            was = self.units.get(key)
            if was is not None and was.get("status") in SETTLED and status not in SETTLED:
                # A later plan re-listing finished work does not reopen it: the
                # change set that settled it is still on disk.
                continue
            self.units[key] = {
                "phase": phase,
                "phase_key": _key(phase),
                "file": path,
                "part": part,
                "action": action[:200],
                "status": status,
                "note": str(getattr(step, "note", "") or "")[:200],
                **({"methods": ",".join(names)} if names else {}),
            }
            self.note_planned(phase, [path])
        if touched:
            self.files = tuple(
                dict.fromkeys((*self.files, *(t for t in touched if not _bookkeeping(t))))
            )

    def update_scope(self, by_file: Mapping[str, Any], exists: "Callable[[str], bool]") -> None:
        """Refresh the scope inventory from a ``legacy_audit`` of the whole service.

        The migration's scope is what the *code* says still uses the legacy
        libraries -- every handler, repository, DTO, bootstrap and test file --
        not what some plan happened to name. The record knew only the files a
        plan had listed, so the repository files each handler group calls, the
        bootstrapper and the DTOs appeared nowhere until something wrote them.

        A file seen once stays in the inventory. When the audit stops reporting
        it, it is ``converted`` if it is still there and ``removed`` if not; a
        file never silently drops out of the record.
        """
        now = _now()
        for path, facts in by_file.items():
            if not path or _bookkeeping(path):
                continue
            if isinstance(facts, Mapping):
                count = int(facts.get("count") or 0)
                rules = [str(r) for r in facts.get("rules") or []][:6]
            else:
                count, rules = int(facts or 0), []
            entry = self.scope.setdefault(
                path, {"kind": kind_of(path), "first": str(count), "seen": now}
            )
            entry.update(
                {
                    "findings": str(count),
                    "rules": ",".join(rules),
                    "status": "legacy" if count else "converted",
                }
            )
        for path, entry in self.scope.items():
            if path not in by_file:
                entry["findings"] = "0"
                entry["status"] = "converted" if exists(path) else "removed"

    def scope_summary(self) -> list[str]:
        """One line per kind of file: how many are in scope and which are still legacy."""
        by_kind: dict[str, list[dict[str, str]]] = {}
        for path, entry in self.scope.items():
            by_kind.setdefault(entry.get("kind", "other"), []).append({"path": path, **entry})
        lines = []
        for kind in KINDS:
            entries = by_kind.get(kind)
            if not entries:
                continue
            left = sorted(
                (e for e in entries if e.get("status") == "legacy"),
                key=lambda e: -int(e.get("findings") or 0),
            )
            line = f"{kind}: {len(entries) - len(left)} of {len(entries)} converted"
            if left:
                line += " — still legacy: " + _clip(
                    [f"{e['path']} ({e.get('findings', '?')})" for e in left], 6
                )
            lines.append(line)
        return lines

    def outstanding(
        self, phase: str, converted: "Callable[[str], bool | None] | None" = None
    ) -> list[str]:
        """Files ``phase`` still has to finish before it may close.

        A file is finished when every unit recorded for it in this phase has
        settled -- and, for a file split across conversion steps, when the code
        agrees: ``converted`` answers from ``handler_map``, and ``None`` means
        it could not say, in which case the step statuses stand.
        """
        key = _key(phase)
        left: list[str] = []
        for path in self.backlog.get(key, []):
            units = [
                u for u in self.units.values()
                if u.get("phase_key") == key and u.get("file") == path
            ]
            if not units or any(u.get("status") not in SETTLED for u in units):
                left.append(path)
            elif (
                converted is not None
                and path in self.split.get(key, [])
                and converted(path) is False
            ):
                left.append(path)
        return left

    # -- disk -------------------------------------------------------------

    def as_dict(self, *, ledger: bool = True) -> dict[str, Any]:
        """The state as JSON. ``ledger=False`` is the shape under REST contract.

        ``GET /v1/sessions/{id}/plan`` serves a session's plan with its
        migration, and that shape is contract C3; the ledger (backlog, units,
        files, routes) lives in the workspace record, which is the migration's
        and not any one session's.
        """
        core = {
            "active": self.active,
            "branch": self.branch or self.expected_branch,
            "base": self.base,
            "closed": self.closed,
            "log": list(self.log),
            "phases": [p.as_dict() for p in self.phases],
        }
        if not ledger:
            return core
        return {
            **core,
            "backlog": {k: list(v) for k, v in self.backlog.items()},
            "split": {k: list(v) for k, v in self.split.items()},
            "units": {k: dict(v) for k, v in self.units.items()},
            "files": list(self.files),
            "routes": self.routes,
            "scope": {k: dict(v) for k, v in self.scope.items()},
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "MigrationState":
        phases = tuple(
            Phase.from_dict(p) for p in raw.get("phases") or () if isinstance(p, Mapping)
        )

        def lists(value: Any) -> dict[str, list[str]]:
            if not isinstance(value, Mapping):
                return {}
            return {
                str(k): [str(x) for x in v if x]
                for k, v in value.items()
                if isinstance(v, (list, tuple))
            }

        scope_raw = raw.get("scope")
        scope = (
            {
                str(k): {str(a): str(b) for a, b in v.items()}
                for k, v in scope_raw.items()
                if isinstance(v, Mapping)
            }
            if isinstance(scope_raw, Mapping)
            else {}
        )
        units_raw = raw.get("units")
        units = (
            {
                str(k): {str(a): str(b) for a, b in v.items()}
                for k, v in units_raw.items()
                if isinstance(v, Mapping)
            }
            if isinstance(units_raw, Mapping)
            else {}
        )
        try:
            routes = int(raw.get("routes") or 0)
        except (TypeError, ValueError):
            routes = 0
        return cls(
            active=bool(raw.get("active")) or bool(phases),
            phases=phases,
            branch=str(raw.get("branch") or ""),
            base=str(raw.get("base") or ""),
            closed=int(raw.get("closed") or 0),
            log=tuple(str(x) for x in raw.get("log") or ()),
            backlog=lists(raw.get("backlog")),
            split=lists(raw.get("split")),
            units=units,
            files=tuple(str(x) for x in raw.get("files") or () if x),
            routes=routes,
            scope=scope,
        )


#: The step statuses that settle a unit. ``written`` is not one of them -- see
#: ``AgentLoop._close_phase``.
SETTLED = frozenset({"done", "skipped", "blocked"})


#: Words that say a phase does more than cut the branch. Earlier roadmaps had
#: "branch-and-deps"; closing that one because a branch exists would skip the
#: dependency swap it also holds.
_NOT_ONLY_BRANCH = ("depend", "deps", "go.mod", "swap", "module", "import", "handler", "convert")


def _branch_only(phase: Phase) -> bool:
    said = f"{phase.name} {phase.covers} {phase.parts}".lower()
    return "branch" in said and not any(word in said for word in _NOT_ONLY_BRANCH)


#: What a file in the migration's scope is, by where it lives in the layout
#: the SOP converts. In the order a reviewer reads the conversion.
KINDS = (
    "module", "handler", "repository", "dto", "response", "domain",
    "bootstrap", "entry", "routes", "grpc", "test", "config", "docs", "other",
)


def kind_of(path: str) -> str:
    """The layer a workspace file belongs to, for the scope inventory."""
    p = path.replace("\\", "/").lower()
    name = p.rsplit("/", 1)[-1]
    if name in ("go.mod", "go.sum", "go.work", "go.work.sum"):
        return "module"
    if name.endswith("_test.go") or p.startswith("tests/") or "/tests/" in p:
        return "test"
    if "grpc" in name or p.endswith(".proto") or p.startswith("pb/") or "/pb/" in p:
        return "grpc"
    if p.startswith("handler/request") or name.startswith("request") or "validator" in name:
        return "dto"
    if p.startswith("handler/response"):
        return "response"
    if p.startswith("handler/"):
        return "handler"
    if p.startswith("repo/") or "/repo/" in p:
        return "repository"
    if p.startswith("core/"):
        return "domain"
    if p.startswith("bootstrap/"):
        return "bootstrap"
    if name == "main.go":
        return "entry"
    if p.startswith("routes/") or name == "routes.go":
        return "routes"
    if p.startswith("configs/") or p.startswith("config/"):
        return "config"
    if p.startswith("docs/"):
        return "docs"
    return "other"


def _bookkeeping(path: str) -> bool:
    """The agent's own record and instruction files, never migration work.

    A branch-cut step has no file of its own, so planners fill the field with
    whatever is at hand -- ``AGENTS.md``, ``.dakcoder/routes-before.json`` --
    and those then sat in every phase's backlog as work it had to finish.
    """
    p = path.replace("\\", "/")
    while p.startswith("./"):
        p = p[2:]
    name = p.rsplit("/", 1)[-1]
    return p.startswith(".dakcoder/") or name in (
        "AGENTS.md",
        "AGENTS.local.md",
        "AGENTS.override.md",
        "CLAUDE.md",
    )


def _key(name: str) -> str:
    return " ".join(str(name or "").split()).lower()


def _unit_key(phase: str, path: str, part: str) -> str:
    return f"{_key(phase)}|{path.strip()}|{_key(part)[:120]}"


def _clip(names: Sequence[str], limit: int = 8) -> str:
    shown = ", ".join(names[:limit])
    return shown + (f" and {len(names) - limit} more" if len(names) > limit else "")


#: The migration's own record, workspace-relative, beside the plan document.
#:
#: A migration outlives a session as well as a context window. `/migrate` opens
#: a new session every time, and the roadmap used to live only in that
#: session's `plan.json` -- so every run began at phase one, and its first save
#: overwrote the plan document with "0 of 7 closed". This file is keyed by
#: nothing but the workspace, so whichever session opens next finds it.
RECORD_PATH = ".dakcoder/migration/state.json"

#: Beside the migration folder, not in it: that this workspace has kept a record.
#:
#: What makes deleting the folder mean something. `load_record` falls back to
#: the sessions' own `plan.json` files for a workspace whose migration predates
#: the record, and it did that for *every* workspace with the record missing --
#: so a developer who deleted `.dakcoder/migration/` to start again found it
#: rebuilt, on the next `/migrate`, from whichever session had last held a plan
#: (session 3baf69127eaf restored 9ba77962405b's one-phase roadmap). Once this
#: exists the record is the only source, and a missing record is no migration.
#: Outside the folder so that deleting the folder leaves it; `plan.json` could
#: not carry it, being the C3 shape under contract.
MARKER_PATH = ".dakcoder/migration-record"

#: Where a migration set aside by "start over" goes. Kept, not deleted: it is
#: the only account of what the earlier attempt did.
ARCHIVE_DIR = ".dakcoder/migration-archive"

_MARKER_TEXT = (
    "This workspace keeps its migration record in .dakcoder/migration/.\n"
    "Delete that folder to forget the migration; it is not rebuilt from sessions.\n"
)


def _mark(root: Path) -> None:
    """Best-effort: a missing marker costs the delete-means-reset rule, not a run."""
    marker = Path(root) / MARKER_PATH
    if marker.is_file():
        return
    try:
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(_MARKER_TEXT, encoding="utf-8")
    except OSError:
        pass


def record_removed(root: Path) -> bool:
    """Whether the workspace had a migration record and it has since been deleted."""
    root = Path(root)
    return (root / MARKER_PATH).is_file() and not (root / RECORD_PATH).is_file()


def archive_record(root: Path) -> str:
    """Set the workspace's migration aside, for "start over". Its new home, or ``""``.

    Moved whole -- record, plan document, anything else in the folder -- to a
    timestamped directory under `ARCHIVE_DIR`, and the marker kept, so nothing
    rebuilds it from the sessions afterwards.
    """
    root = Path(root)
    source = root / Path(RECORD_PATH).parent
    _mark(root)
    if not source.is_dir():
        return ""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    target = root / ARCHIVE_DIR / stamp
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        n = 1
        while target.exists():
            n += 1
            target = root / ARCHIVE_DIR / f"{stamp}-{n}"
        os.replace(source, target)
    except OSError:
        return ""
    return target.relative_to(root).as_posix()


def merged_roadmap(recorded: Sequence[Phase], submitted: Sequence[Phase]) -> tuple[Phase, ...]:
    """The roadmap a submission leaves on record.

    Three rules, all about a roadmap already on record, and all measured on the
    same workspace: seven phases, then five, then one.

    * A submission naming only phases the record has is a restatement -- a
      resumed session re-sending the phase it is about to work -- and changes
      nothing. That is the one that shrank seven phases to one.
    * A closed phase is never dropped. Its work is on disk and its branch is
      cut; a roadmap without it sends the next session to redo it. That is the
      one that lost `branch` and `dependencies`.
    * Anything else is a new roadmap for what is still open, installed after
      the closed phases.
    """
    submitted = tuple(submitted)
    if not recorded:
        return submitted
    if not submitted:
        return tuple(recorded)
    names = {_key(p.name) for p in submitted}
    if names <= {_key(p.name) for p in recorded}:
        return tuple(recorded)
    kept = tuple(p for p in recorded if p.status == "done" and _key(p.name) not in names)
    return kept + submitted


def load_record(root: Path) -> MigrationState | None:
    """The workspace's migration record, or ``None`` when there is none.

    Falls back to the sessions' own plan files. Until the record existed the
    roadmap, the branch and the closed phases lived only in each session's
    ``plan.json``, so a workspace whose migration began before it has its
    progress there and nowhere else -- and reading only the record is what sent
    the first run after the upgrade back to "which branch should I cut from?".

    **Only for such a workspace.** Once `MARKER_PATH` exists the record has been
    kept here, and its absence is the developer's decision, not a gap to fill.
    """
    root = Path(root)
    legacy = not (root / MARKER_PATH).is_file()
    try:
        raw = json.loads((root / RECORD_PATH).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return _from_sessions(root) if legacy else None
    if not isinstance(raw, Mapping):
        return _from_sessions(root) if legacy else None
    state = MigrationState.from_dict(raw)
    state.backlog = {
        k: [f for f in v if not _bookkeeping(f)] for k, v in state.backlog.items()
    }
    state.files = tuple(f for f in state.files if not _bookkeeping(f))
    if not state.phases:
        return _from_sessions(root) if legacy else None
    # A record read is a record kept: from here on, deleting it means it.
    _mark(root)
    return state


def _from_sessions(root: Path) -> MigrationState | None:
    """The newest unfinished migration in any session's ``plan.json``, as a record."""
    sessions = root / ".dakcoder" / "sessions"
    try:
        plans = sorted(
            (p for p in sessions.glob("*/plan.json") if p.is_file()),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
    except OSError:
        return None
    for path in plans:
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(raw, Mapping) or not isinstance(raw.get("migration"), Mapping):
            continue
        state = MigrationState.from_dict(raw["migration"])
        if not state.phases or state.complete:
            continue

        class _Step:
            def __init__(self, item: Mapping[str, Any]) -> None:
                for key in ("file", "action", "phase", "part", "status", "note"):
                    setattr(self, key, str(item.get(key) or ""))

        steps = [_Step(s) for s in raw.get("steps") or () if isinstance(s, Mapping)]
        state.record_steps(steps)
        return state
    return None


def save_record(root: Path, state: MigrationState) -> None:
    """Write the record atomically. Best-effort: a lost record costs the resume, not the run."""
    if not state.phases:
        return
    target = Path(root) / RECORD_PATH
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".state-", suffix=".json", dir=str(target.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump({**state.as_dict(), "updated": _now()}, fh, indent=1, sort_keys=True)
            os.replace(tmp, target)
            _mark(Path(root))
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
    except OSError:
        return


def _now() -> str:
    """An ISO timestamp to the minute. Seconds would rewrite the document
    on every turn for no information."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def phases_from_meta(meta: Mapping[str, Any]) -> tuple[Phase, ...]:
    """Rebuild the roadmap from a ``submit_plan`` result's ``meta``."""
    return tuple(
        Phase.from_dict(p) for p in meta.get("phases") or () if isinstance(p, Mapping)
    )


#: What a step says it does, when what it does is bounded by the edit rather
#: than by the file.
#:
#: The size rule below refuses a single step on a file over ``BIG_FILE`` lines,
#: and every word of its reasoning is about *conversion*: the acting phase has
#: 32,768 output tokens, a conversion has to read the original as well, so a
#: 4,000-line handler is five steps rather than one. That is right, and it is
#: right about conversion only.
#:
#: A dependency phase is not conversion. "Replace the api-log import with
#: n-api-log" in a 4,064-line repository file is one `patch_file` with a
#: one-line anchor, and the file's length has nothing to do with whether it
#: fits in a reply. A field session was refused on exactly that, answered
#: correctly -- "phase 2 is about dependencies, not handler conversion; the
#: repo files only need import swaps" -- resubmitted the identical plan, and was
#: refused again, spending both of `MAX_PLAN_OBJECTIONS` and fifty seconds on an
#: objection it could not satisfy and should not have had to.
_BOUNDED_EDITS = (
    "import",
    "dependenc",
    "module path",
    "go.mod",
    "go.sum",
)


#: A step check that is the build: `go_build`, `go vet`, `go test`, "compiles",
#: "builds clean". What `plan_objection` refuses before the last phase.
_BUILD_CHECK = re.compile(
    r"\bgo[_ ]?(?:build|vet|test)\b|\bcompiles?\b|\bbuilds?\b(?: clean| pass| succeed)",
    re.IGNORECASE,
)


def _is_bounded_edit(action: str) -> bool:
    """Whether this step's own description says it is an edit, not a rewrite.

    Read off ``action`` -- a typed field the model filled in to say what the
    step does -- which is the licence ``_step_wants_removal`` takes for the same
    reason. It is not asking what the reply said; it is asking what this step
    claims to be.

    Deliberately narrow. Only the vocabulary of a dependency swap qualifies; a
    step that says it "updates" or "fixes" a 4,000-line file is still a step
    that has to be split, because nothing in those words bounds it.
    """
    said = action.lower()
    return any(marker in said for marker in _BOUNDED_EDITS)


#: What ``handler_map`` says about one file's conversion groups: a list of
#: ``{"methods": [...], "start": int, "end": int, "done": bool}``, or ``None``
#: when the map could not be taken (no sidecar, not a handler file).
Groups = Callable[[str], "list[Mapping[str, Any]] | None"]


def split_files(steps: Sequence[Any], lines: Callable[[str], int]) -> list[str]:
    """The files in ``steps`` that are converted across several steps.

    Over ``BIG_FILE`` lines and not exempt as a bounded edit -- the same test
    the size objection applies, so the loop and the objection agree on which
    files' completion has to be asked of the code.
    """
    bounded: dict[str, bool] = {}
    for step in steps:
        path = str(getattr(step, "file", "") or "")
        if path:
            bounded[path] = bounded.get(path, True) and _is_bounded_edit(
                str(getattr(step, "action", "") or "")
            )
    return [p for p, b in bounded.items() if not b and lines(p) > BIG_FILE]


def _groups_text(path: str, groups: Sequence[Mapping[str, Any]], limit: int = MAX_STEPS) -> str:
    """The open groups of one file, one line each, for an objection to quote."""
    rows = []
    for index, group in enumerate(groups[:limit], 1):
        methods = [str(m) for m in group.get("methods") or ()]
        shown = ", ".join(methods[:8]) + (f" +{len(methods) - 8} more" if len(methods) > 8 else "")
        rows.append(
            f"  {index}. lines {group.get('start', '?')}-{group.get('end', '?')}: {shown}"
        )
    if len(groups) > limit:
        rows.append(
            f"  ...and {len(groups) - limit} more group(s) -- the next plan, once these are done."
        )
    return f"Open groups in {path}, from handler_map:\n" + "\n".join(rows)


def _open_groups(groups: Groups | None, path: str) -> list[Mapping[str, Any]] | None:
    if groups is None:
        return None
    try:
        found = groups(path)
    except Exception:  # noqa: BLE001 - advice from a sidecar, never a precondition
        return None
    if not found:
        return None
    return [g for g in found if not g.get("done")]


def size_objection(
    steps: Sequence[Any],
    lines: Callable[[str], int],
    groups: Groups | None = None,
) -> str:
    """Why a plan gives some large file too few steps to finish, or ``""``.

    The size half of ``plan_objection``, on its own so ``revise_plan`` is held
    to it too: a revision that merged nine groups back into three was the one
    route around it.

    **The groups, when the map can be taken, are the measure** -- not
    ``ceil(lines / BIG_FILE)``. The arithmetic counted helpers and types, which
    no step converts, and could not see that a single 900-line method is one
    group however long it is, so a plan that followed ``handler_map`` exactly
    could be refused for it. It also could not see that half a file was
    already converted by an earlier session, and asked for steps for that too.
    """
    counted: dict[str, int] = {}
    bounded: dict[str, bool] = {}
    for step in steps:
        path = str(getattr(step, "file", "") or "")
        if path:
            counted[path] = counted.get(path, 0) + 1
            # A file is exempt only if *every* step on it is a bounded edit.
            # One conversion step among three import swaps is still a
            # conversion step, and it is the one that cannot finish.
            was = bounded.get(path, True)
            bounded[path] = was and _is_bounded_edit(str(getattr(step, "action", "") or ""))

    # **Enough steps, not merely more than one.** A plan that followed the old
    # advice was accepted at three steps for a 6,571-line handler -- ~2,190
    # lines each, against an acting phase that can emit about 1,200 -- and the
    # run discovered it thirty turns later.
    short: list[tuple[str, int, int, int, list[Mapping[str, Any]] | None]] = []
    for path, given in counted.items():
        if bounded.get(path, False):
            continue
        n = lines(path)
        if n <= BIG_FILE:
            continue
        open_groups = _open_groups(groups, path)
        if open_groups is not None:
            needed = len(open_groups)
            if needed == 0:
                continue  # already converted: any step on it is a re-check
        else:
            needed = -(-n // BIG_FILE)  # ceil, without importing math for it
        # One plan carries MAX_STEPS. A file needing more is planned a plan's
        # worth at a time; the phase stays open on it until the code says it
        # is converted, so the rest cannot be forgotten.
        needed = min(needed, MAX_STEPS)
        if given < needed:
            short.append((path, n, given, needed, open_groups))

    # **The demand has to fit in a submission.** When the phase's big files
    # need more steps between them than one plan holds, ask for a *narrower*
    # plan instead of a longer one: one file, finished, then the next.
    total = sum(needed for _, _, _, needed, _ in short)
    if short and total > MAX_STEPS:
        # The smallest file in the *plan*, not the smallest oversized one: a
        # 695-line file is one step and finishes in a turn, which is what turns
        # a blocked phase into a moving one.
        sizes = {path: lines(path) for path in counted if path and not bounded.get(path, False)}
        path = min(sizes, key=lambda p: (sizes[p], p))
        n = sizes[path]
        open_groups = _open_groups(groups, path) if n > BIG_FILE else None
        needed = (
            min(len(open_groups), MAX_STEPS)
            if open_groups
            else max(1, min(MAX_STEPS, -(-n // BIG_FILE)))
        )
        listing = f"\n\n{_groups_text(path, open_groups)}" if open_groups else ""
        return (
            "this phase is bigger than one plan can hold: its large files need "
            f"{total} steps between them and a plan carries {MAX_STEPS}. Plan one "
            f"file at a time. Send the steps for {path} ({n:,} lines, {needed} "
            f"step{'' if needed == 1 else 's'}) and nothing else -- it is the "
            "smallest, so it finishes first. The other files stay in this phase "
            "and are planned next; the phase does not close without them. One step "
            "per group: `action` naming the group's methods and the repository "
            "methods they call, `part` the group, `accepts` "
            f"`unit_check path={path} methods=<those methods>`" + listing
        )
    if short:
        path, n, given, needed, open_groups = max(short, key=lambda item: item[1])
        had = "one step" if given == 1 else f"{given} steps"
        listing = f"\n\n{_groups_text(path, open_groups)}" if open_groups else (
            f"\n\nSplit it with `handler_map path={path}`: one step per group it lists."
        )
        return (
            f"{path} is {n:,} lines and the plan gives it {had}; it needs at least {needed}. "
            "One reply converts about one group, so fewer steps cannot be finished. "
            "One step per group, each naming in `action` the group's methods and "
            "the repository methods they call, the group in `part`, and `accepts` "
            f"`unit_check path={path} methods=<those methods>`. Each step reads and "
            "patches only its own methods, one `patch_file` per method -- the whole "
            "file is never read or written at once."
            + listing
            + (
                "\n\nAlso short: "
                + ", ".join(f"{p} ({ln:,} lines, {g} of {nd})" for p, ln, g, nd, _ in short if p != path)
                if len(short) > 1
                else ""
            )
        )
    return ""


def plan_objection(
    state: "MigrationState",
    phases: Sequence[Phase],
    steps: Sequence[Any],
    lines: "Callable[[str], int] | None" = None,
    groups: Groups | None = None,
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
    # The roadmap this plan would leave on record -- the same merge `adopt`
    # makes, so the check and the adoption agree about what they are judging.
    roadmap = merged_roadmap(state.phases, phases)
    if not roadmap:
        return (
            "this is a service migration and the plan has no phases. Send `phases` "
            f"as well: at least {MIN_PHASES}, in execution order, each with `covers` "
            f"in one line and `parts` naming at least {MIN_PARTS} sub-categories it "
            "breaks into. The SOP's are branch, dependencies, handlers, DTOs and "
            "validation, bootstrap and FX, tests, swagger"
        )
    # The shape of the roadmap is asked of the *first* roadmap only. Once one is
    # on record it is settled: the resume view says it "stays as it is", and a
    # session that sends steps alone -- as that view asks -- was then told "the
    # roadmap has 1 phase(s) ... send `phases`", a demand only a re-plan could
    # meet. Every resumed session of pao-backend spent both of its objections on
    # it before the third submission was taken as it stood (9ba77962405b turns
    # 7-11, 3baf69127eaf turns 10-15). A roadmap recorded too short is fixed by
    # starting over, which the developer can now ask for.
    first = not state.phases
    if first and len(roadmap) < MIN_PHASES:
        return (
            f"the roadmap has {len(roadmap)} phase(s). A migration is planned in at "
            f"least {MIN_PHASES}, in execution order — a conversion delivered in one "
            "or two lumps is the one that cannot be reviewed and cannot be resumed"
        )
    if first and (thin := [p.name or "(unnamed)" for p in roadmap if len(p.part_list) < MIN_PARTS]):
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

    # A file bigger than one reply is bigger than one step. See
    # `size_objection`, which `revise_plan` is held to as well.
    if lines is not None and (objection := size_objection(steps, lines, groups)):
        return objection

    # A step checked by the build, before the phase in which the build can pass.
    #
    # Session e3edb2434936: the handlers phase split paogen.go into seven steps
    # and checked every one with "go_build passes for the handler package". The
    # acting phase read the first, saw that converting lines 1-800 leaves 5,700
    # lines on gin, concluded -- correctly -- that the package cannot build after
    # any single step, refused all seven as unsatisfiable and stalled until the
    # run was cut off. Nothing in it was wrong except the check.
    #
    # After the dependency swap nothing compiles until every phase is done, so
    # the build is the gate's question, asked once at the end. A step's question
    # is whether *its* methods were converted, and `unit_check` answers that on
    # a package that does not build.
    last = roadmap[-1].name.strip().lower()
    built = [
        s for s in steps
        if str(getattr(s, "phase", "") or "").strip().lower() != last
        and _BUILD_CHECK.search(str(getattr(s, "accepts", "") or ""))
    ]
    if built:
        files = sorted({str(getattr(s, "file", "") or "") for s in built} - {""})
        return (
            f"{len(built)} step(s) are checked with the build ("
            + ", ".join(files[:3])
            + "). The build cannot pass until the migration's last phase: after the "
            "dependency swap the service does not compile until every handler is "
            "converted, so a step checked that way can never be done. Check each "
            "conversion step with `unit_check path=<file> methods=<the methods it "
            "converts>` instead -- it passes on a file whose package does not build. "
            "A half-converted file is the plan working, not a fault. Keep go_build "
            "for the last phase"
        )

    closed = {p.name.strip().lower() for p in state.phases if p.status == "done"}
    # Only when *nothing* in the plan is still open. A plan re-sending a closed
    # phase beside the open one is the ordinary shape of a resumed session --
    # the model re-plans from the roadmap it was given -- and the loop drops
    # the closed steps with a note instead of spending a round trip.
    if spread and spread <= closed:
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
    #
    # Every session's, from the migration's ledger, with the plan in hand
    # folded in first -- not only this session's steps. Rendering the plan in
    # hand alone is what made the document forget the work of every earlier
    # session the moment a new one planned anything.
    rows: list[tuple[str, str, str, str]] = []
    if state.units:
        for unit in state.units.values():
            rows.append(
                (
                    unit.get("file", ""),
                    unit.get("part") or unit.get("phase") or "",
                    unit.get("status", "pending"),
                    unit.get("note") or unit.get("action") or "",
                )
            )
    else:
        for step in plan:
            rows.append((step.file, step.part or step.phase or "", step.status, step.note or step.action or ""))
    if rows:
        out += [
            "## Units",
            "",
            "| Unit | Kind | Classification | Status | Rules | Commit |",
            "|---|---|---|---|---|---|",
        ]
        for path, kind, status, note in rows:
            # SKIP is the view's word for "deliberately excluded", which is
            # exactly what a skipped step is. Everything else is work.
            classification = "SKIP" if status == "skipped" else "MIGRATE"
            note = note.replace("|", "/").replace("\n", " ")
            kind = kind.replace("|", "/")
            out.append(f"| {path} | {kind} | {classification} | {status} | {note[:90]} | |")
        out.append("")

    # -- the scope, from the code ----------------------------------------
    #
    # A list, never a table: the Migration view reads the *first* table with a
    # path-like column as the units, and this must not be mistaken for it.
    if state.scope:
        out += ["## Scope", ""]
        out += [f"- {line}" for line in state.scope_summary()]
        out += [
            "",
            "From `legacy_audit`: every file that still uses the legacy libraries,",
            "by layer, refreshed when a run starts, resumes, or closes a phase.",
            "",
        ]
        legacy = sorted(
            ((p, e) for p, e in state.scope.items() if e.get("status") == "legacy"),
            key=lambda item: (KINDS.index(item[1].get("kind", "other")) if item[1].get("kind", "other") in KINDS else 99, item[0]),
        )
        if legacy:
            out += ["### Still on the legacy libraries", ""]
            for path, entry in legacy:
                rules = entry.get("rules") or ""
                out.append(
                    f"- `{path}` — {entry.get('kind', 'other')}, {entry.get('findings', '?')} finding(s)"
                    + (f" ({rules})" if rules else "")
                )
            out.append("")

    # -- what is left ----------------------------------------------------
    remaining = [p for p in state.phases if p.status != "done"]
    if remaining:
        out += ["## Still to do", ""]
        for phase in remaining:
            out.append(f"- **{phase.name}** — {phase.covers or 'no summary'}")
            if left := state.outstanding(phase.name):
                out.append(f"  - files not finished: {_clip(left, 12)}")
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

    changed = list(dict.fromkeys((*state.files, *touched)))
    if changed:
        out += ["## Files changed by the migration", ""]
        out += [f"- `{path}`" for path in changed[:60]]
        if len(changed) > 60:
            out.append(f"- ...and {len(changed) - 60} more")
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
