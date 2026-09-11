"""Work that outlives a turn, and work that outlives a run.

Two things, deliberately separate
---------------------------------
**The plan** is what this run committed to. It already existed as
``_State.plan`` -- typed ``PlanStep``s with a status the loop sets from ground
truth -- and it was the best part of the design and the least durable: a tuple
in a process, lost on a daemon restart, with no record of how it got to where it
is. A developer who reloads their window at step 4 of 7 comes back to an agent
that has the edits on disk and no idea which steps produced them.

``PlanRecord`` is that plan, persisted, with its revisions. Not a second source
of truth: the loop still owns the live plan and still sets ``done`` from the
change set, because a status the model can write is a status the model can get
wrong. This records what the loop decided, so a restart can pick it up and a
developer can read what happened.

**The agenda** is work nobody has committed to yet. A run that notices "the
repo layer has the same N+1 in four other methods" has found real work that is
not this task, and today it has exactly two options: do it (scope creep, in a
run the developer did not ask for it in) or mention it in prose that scrolls
away. Neither produces anything anyone can act on next week.

An ``AgendaTask`` is that observation, written down, with a state a person moves:
proposed by the agent, approved by a developer, then run. It is Cline's
``AgendaTaskRecord`` reduced to the part that works without a hub -- no cron, no
unattended execution, no run claims -- because those need the hosted runtime in
`host-plan.md` and this does not. What it needs is a file and a person.

Why they are not one type
-------------------------
A plan step is a commitment with an acceptance criterion, scoped to a run, whose
status is *derived*. An agenda task is a proposal with no acceptance criterion,
scoped to a repository, whose status is *decided by a person*. Modelling them
together would mean either giving plan steps an approval state nobody sets or
giving agenda tasks a "done because a file changed" rule that is wrong for them.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .tools.control import STEP_STATUSES, PlanStep
from .undo import ensure_private

__all__ = [
    "AGENDA_STATES",
    "AgendaStore",
    "AgendaTask",
    "MAX_AGENDA_TASKS",
    "PlanRecord",
    "PlanRevision",
]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ── the plan ────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class PlanRevision:
    """One version of the plan, and what caused it.

    Kept because "why is the plan different from the one I approved" is a
    question a developer asks, and because a loop-initiated replan and a
    model-initiated ``revise_plan`` are different events that used to leave the
    same trace: none.
    """

    at: str
    #: "submitted", "replanned" (the loop asked), or "revised" (the model did).
    cause: str
    summary: str
    steps: tuple[PlanStep, ...] = ()
    #: The reason the model or the loop gave. Empty for the first submission.
    reason: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "at": self.at,
            "cause": self.cause,
            "summary": self.summary,
            "reason": self.reason,
            "steps": [
                {"file": s.file, "action": s.action, "accepts": s.accepts,
                 "status": s.status, "note": s.note}
                for s in self.steps
            ],
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "PlanRevision":
        return cls(
            at=str(raw.get("at") or ""),
            cause=str(raw.get("cause") or "submitted"),
            summary=str(raw.get("summary") or ""),
            reason=str(raw.get("reason") or ""),
            steps=_steps_from(raw.get("steps")),
        )


def _steps_from(raw: Any) -> tuple[PlanStep, ...]:
    if not isinstance(raw, (list, tuple)):
        return ()
    out: list[PlanStep] = []
    for item in raw:
        if not isinstance(item, Mapping):
            continue
        status = str(item.get("status") or "pending")
        out.append(
            PlanStep(
                file=str(item.get("file") or ""),
                action=str(item.get("action") or ""),
                accepts=str(item.get("accepts") or ""),
                status=status if status in STEP_STATUSES else "pending",
                note=str(item.get("note") or ""),
            )
        )
    return tuple(out)


@dataclass
class PlanRecord:
    """One session's plan, as it stands and as it got here.

    Written whenever the loop adopts or revises a plan, and whenever a step's
    status changes -- which is on every mutation, so it is rewritten a handful
    of times per run rather than per turn. Atomic, like every other small file
    here: a half-written plan read back on restart would be worse than none.
    """

    session_id: str = ""
    summary: str = ""
    steps: tuple[PlanStep, ...] = ()
    revisions: tuple[PlanRevision, ...] = ()
    #: Whether the plan the model produced was volunteered or extracted at the
    #: research fence. ``_open_targets`` is what reads it, and a forced plan is
    #: not a commitment (BUG L-28) -- so it has to survive a restart with the
    #: plan, or the resumed run enforces something nobody asked for.
    forced: bool = False
    updated_at: str = field(default_factory=_now)

    @property
    def open_steps(self) -> tuple[PlanStep, ...]:
        return tuple(s for s in self.steps if s.open)

    def record(
        self,
        steps: Sequence[PlanStep],
        summary: str,
        *,
        cause: str = "submitted",
        reason: str = "",
    ) -> "PlanRecord":
        """Adopt a new set of steps, keeping the old one as a revision."""
        revision = PlanRevision(
            at=_now(), cause=cause, summary=summary, reason=reason, steps=tuple(steps)
        )
        return replace(
            self,
            summary=summary,
            steps=tuple(steps),
            revisions=(*self.revisions, revision),
            updated_at=_now(),
        )

    def with_steps(self, steps: Sequence[PlanStep]) -> "PlanRecord":
        """Update the live steps without adding a revision.

        A status change is not a new plan. The loop sets ``done`` from the
        change set on every mutation, and recording each of those as a revision
        would bury the three that are actually revisions.
        """
        return replace(self, steps=tuple(steps), updated_at=_now())

    def as_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "summary": self.summary,
            "forced": self.forced,
            "updated_at": self.updated_at,
            "steps": [
                {"file": s.file, "action": s.action, "accepts": s.accepts,
                 "status": s.status, "note": s.note}
                for s in self.steps
            ],
            "revisions": [r.as_dict() for r in self.revisions],
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "PlanRecord":
        revisions = tuple(
            PlanRevision.from_dict(r)
            for r in raw.get("revisions") or ()
            if isinstance(r, Mapping)
        )
        return cls(
            session_id=str(raw.get("session_id") or ""),
            summary=str(raw.get("summary") or ""),
            steps=_steps_from(raw.get("steps")),
            revisions=revisions,
            forced=bool(raw.get("forced")),
            updated_at=str(raw.get("updated_at") or _now()),
        )

    # -- disk ---------------------------------------------------------------

    @staticmethod
    def path_for(root: Path, session_id: str) -> Path:
        return root / ".dakcoder" / "sessions" / session_id / "plan.json"

    def save(self, root: Path) -> None:
        """Best-effort, like the journal. A lost plan costs the resume, not the run."""
        if not self.session_id:
            return
        target = self.path_for(root, self.session_id)
        try:
            ensure_private(root / ".dakcoder")
            target.parent.mkdir(parents=True, exist_ok=True)
            tmp = target.with_suffix(".json.tmp")
            tmp.write_text(
                json.dumps(self.as_dict(), indent=1, sort_keys=True, default=str),
                encoding="utf-8",
            )
            tmp.replace(target)
        except OSError:
            return

    @classmethod
    def load(cls, root: Path, session_id: str) -> "PlanRecord | None":
        try:
            raw = json.loads(cls.path_for(root, session_id).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return cls.from_dict(raw) if isinstance(raw, Mapping) else None


# ── the agenda ──────────────────────────────────────────────────────────────

#: The states a task moves through, and who moves it.
#:
#: ``proposed``  the agent found it. Nothing happens until a person looks.
#: ``approved``  a developer said yes. It is now work.
#: ``done``      it was carried out.
#: ``dropped``   a developer said no, or it stopped being true.
#:
#: There is no ``running``. That state belongs to a runtime that can claim a
#: task and be trusted to release it, which is the hosted design in
#: `host-plan.md`; here a task is picked up by a developer starting a session
#: from it, and the session is where "is it running" is answered.
AGENDA_STATES = ("proposed", "approved", "done", "dropped")

#: How many tasks the agenda keeps. Oldest resolved ones are dropped first, so a
#: backlog nobody triages does not grow without bound -- and an agenda too long
#: to read is one nobody triages.
MAX_AGENDA_TASKS = 200


@dataclass(frozen=True, slots=True)
class AgendaTask:
    """One piece of work the agent proposed and a person has not yet run.

    ``why`` is not decoration. A proposal a developer cannot evaluate in one
    line is one they will neither approve nor drop, and an agenda full of those
    is a list nobody reads. The agent has the evidence at the moment it notices;
    a week later nobody does.
    """

    id: str
    title: str
    #: One line: the evidence. "handler/objection.go:412 does the same N+1 as
    #: the one just fixed" beats "consider auditing the handlers".
    why: str = ""
    state: str = "proposed"
    #: Workspace-relative paths the work would touch, if known.
    paths: tuple[str, ...] = ()
    #: 1 (highest) to 5. Set by whoever proposed it; a developer may change it.
    priority: int = 3
    #: The session that proposed it, so its transcript can be found.
    origin_session: str = ""
    created_at: str = field(default_factory=_now)
    updated_at: str = field(default_factory=_now)
    #: Set when a developer approves, drops or completes it.
    decided_by: str = ""
    note: str = ""

    @property
    def open(self) -> bool:
        return self.state in ("proposed", "approved")

    def moved_to(self, state: str, *, by: str = "", note: str = "") -> "AgendaTask":
        if state not in AGENDA_STATES:
            raise ValueError(f"{state!r} is not one of {AGENDA_STATES}")
        return replace(
            self,
            state=state,
            decided_by=by or self.decided_by,
            note=note or self.note,
            updated_at=_now(),
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "why": self.why,
            "state": self.state,
            "paths": list(self.paths),
            "priority": self.priority,
            "origin_session": self.origin_session,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "decided_by": self.decided_by,
            "note": self.note,
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "AgendaTask | None":
        identifier = str(raw.get("id") or "")
        title = str(raw.get("title") or "")
        if not identifier or not title:
            return None
        state = str(raw.get("state") or "proposed")
        try:
            priority = int(raw.get("priority") or 3)
        except (TypeError, ValueError):
            priority = 3
        return cls(
            id=identifier,
            title=title,
            why=str(raw.get("why") or ""),
            state=state if state in AGENDA_STATES else "proposed",
            paths=tuple(str(p) for p in raw.get("paths") or () if p),
            priority=min(5, max(1, priority)),
            origin_session=str(raw.get("origin_session") or ""),
            created_at=str(raw.get("created_at") or _now()),
            updated_at=str(raw.get("updated_at") or _now()),
            decided_by=str(raw.get("decided_by") or ""),
            note=str(raw.get("note") or ""),
        )

    @classmethod
    def propose(
        cls,
        title: str,
        *,
        why: str = "",
        paths: Sequence[str] = (),
        priority: int = 3,
        origin_session: str = "",
    ) -> "AgendaTask":
        return cls(
            id=uuid.uuid4().hex[:12],
            title=title.strip(),
            why=why.strip(),
            paths=tuple(dict.fromkeys(p for p in paths if p)),
            priority=min(5, max(1, priority)),
            origin_session=origin_session,
        )


class AgendaStore:
    """The repository's backlog, in one file.

    One file rather than a row per task, because the whole point is that a
    person reads it: ``.dakcoder/agenda.json`` can be opened, diffed, and
    committed if a team wants the backlog shared. A database would be the right
    answer at a thousand tasks and this is capped at two hundred.

    Every write is read-modify-write and atomic. Two daemons in two windows on
    one workspace can both write it, and the loser of that race loses one
    proposal rather than the file -- which is the correct trade until sessions
    are owned by a coordinator (`host-plan.md`), at which point this moves
    behind it.
    """

    __slots__ = ("root", "_path")

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self._path = self.root / ".dakcoder" / "agenda.json"

    @property
    def path(self) -> Path:
        return self._path

    def load(self) -> list[AgendaTask]:
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        items = raw.get("tasks") if isinstance(raw, Mapping) else raw
        if not isinstance(items, list):
            return []
        out: list[AgendaTask] = []
        for item in items:
            if isinstance(item, Mapping) and (task := AgendaTask.from_dict(item)):
                out.append(task)
        return out

    def save(self, tasks: Iterable[AgendaTask]) -> bool:
        """Write the agenda. ``False`` when it could not be written.

        Unlike the journal this reports failure, because a caller adding a task
        is answering an HTTP request and "it is saved" would be a lie.
        """
        ordered = _bounded(tasks)
        try:
            ensure_private(self.root / ".dakcoder")
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._path.with_suffix(".json.tmp")
            tmp.write_text(
                json.dumps(
                    {"tasks": [t.as_dict() for t in ordered]},
                    indent=1,
                    sort_keys=True,
                    default=str,
                ),
                encoding="utf-8",
            )
            tmp.replace(self._path)
        except OSError:
            return False
        return True

    def add(self, task: AgendaTask) -> AgendaTask | None:
        """Add a proposal, unless the same work is already open.

        De-duplicated on the title, case-folded. An agent that notices the same
        thing on three runs has noticed one thing, and three identical rows is
        how a backlog stops being read.
        """
        tasks = self.load()
        key = task.title.strip().casefold()
        for existing in tasks:
            if existing.open and existing.title.strip().casefold() == key:
                return None
        tasks.append(task)
        return task if self.save(tasks) else None

    def move(self, task_id: str, state: str, *, by: str = "", note: str = "") -> AgendaTask | None:
        tasks = self.load()
        moved: AgendaTask | None = None
        out: list[AgendaTask] = []
        for task in tasks:
            if task.id == task_id:
                moved = task.moved_to(state, by=by, note=note)
                out.append(moved)
            else:
                out.append(task)
        if moved is None:
            return None
        return moved if self.save(out) else None

    def open_tasks(self) -> list[AgendaTask]:
        """What is still waiting, best first: priority, then oldest."""
        return sorted(
            (t for t in self.load() if t.open),
            key=lambda t: (t.priority, t.created_at),
        )


def _bounded(tasks: Iterable[AgendaTask]) -> list[AgendaTask]:
    """Keep every open task and the newest resolved ones, up to the cap.

    Open tasks are never dropped: an agenda that silently forgets approved work
    is worse than no agenda, because a developer believes it is recorded.
    """
    everything = list(tasks)
    open_tasks = [t for t in everything if t.open]
    resolved = sorted(
        (t for t in everything if not t.open), key=lambda t: t.updated_at, reverse=True
    )
    room = max(0, MAX_AGENDA_TASKS - len(open_tasks))
    keep = {id(t) for t in open_tasks} | {id(t) for t in resolved[:room]}
    return [t for t in everything if id(t) in keep]
