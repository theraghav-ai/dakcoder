"""Sessions: the event log, resumption, abort, and revert.

Part B §12 calls sessions "``postgen``'s strongest reliability story", and §14
names the one gap in it: the SSE client parses a clean stream well but has no
resumption path, so a dropped connection loses the live view of a run that is
still executing. Every event here carries a monotonic id and is kept, so
``since_id`` replay is a query rather than a rewrite.

**Events are persisted before they are sent.** The order matters. Sending first
and recording afterwards means a crash between the two produces an event the
client saw and the log does not have — and then resumption silently skips it,
which is worse than losing the connection, because nothing looks wrong.

**Revert restores from git, not from a snapshot.** §12: restore every path the
session touched to HEAD, deleting files that have no baseline. Reading HEAD at
revert time rather than copying files at write time means no memory is held for
a revert that will probably never happen, and it is the same content git would
give a developer typing the command themselves — which is what they will compare
it against.
"""

from __future__ import annotations

import asyncio
import subprocess
import threading
import uuid
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path
from typing import Any

from dakcoder_shared.envelope import Event, EventType

from .loop import Outcome, RunResult

__all__ = ["Session", "SessionStore", "Status", "StoredEvent", "RevertPlan"]


class Status(StrEnum):
    RUNNING = "running"
    DONE = "done"
    UNVERIFIED = "unverified"
    NO_PROGRESS = "no_progress"
    EXHAUSTED = "exhausted"
    ERROR = "error"
    ABORTED = "aborted"

    @classmethod
    def of(cls, outcome: str) -> Status:
        try:
            return cls(outcome)
        except ValueError:
            return cls.ERROR

    @property
    def resumable(self) -> bool:
        """§12: resume for escalated / failed / max_turns / aborted.

        Not for ``done``: a finished session takes a *follow-up*, which is a new
        turn on the existing transcript rather than a retry of the last one. The
        distinction matters because resuming a successful run would re-enter the
        gate loop on a change that already passed.
        """
        return self in (Status.UNVERIFIED, Status.NO_PROGRESS, Status.EXHAUSTED,
                        Status.ERROR, Status.ABORTED)


@dataclass(frozen=True, slots=True)
class StoredEvent:
    """One event, with the id a client resumes from."""

    id: int
    type: EventType
    data: dict[str, Any]
    at: datetime

    def as_dict(self) -> dict[str, Any]:
        return {"id": self.id, "type": str(self.type), "data": self.data,
                "at": self.at.isoformat()}

    def sse(self) -> str:
        """Encode with an ``id:`` line so the browser sends ``Last-Event-ID``.

        Without the id the reconnect has nothing to resume from and the client
        is back to guessing whether the run died.
        """
        import json

        body = json.dumps(self.data, separators=(",", ":"), ensure_ascii=False)
        return f"id: {self.id}\nevent: {self.type}\ndata: {body}\n\n"


@dataclass(frozen=True, slots=True)
class RevertPlan:
    """What a revert would do, listed before it does it.

    §12 asks for the confirmation to list the exact paths, because "revert my
    last task" is easy to fire by accident. So the plan is a value the caller can
    show, and applying it is a separate call.
    """

    session_id: str
    restore: tuple[str, ...] = ()
    delete: tuple[str, ...] = ()
    #: Paths that changed but cannot be reverted, with the reason.
    blocked: tuple[tuple[str, str], ...] = ()

    @property
    def empty(self) -> bool:
        return not (self.restore or self.delete)

    def as_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "restore": list(self.restore),
            "delete": list(self.delete),
            "blocked": [{"path": p, "reason": r} for p, r in self.blocked],
        }


@dataclass
class Session:
    """One run, live or finished."""

    id: str
    task: str
    workspace: str
    status: Status = Status.RUNNING
    created_at: datetime = field(default_factory=lambda: datetime.now(tz=timezone.utc))
    finished_at: datetime | None = None
    summary: str = ""
    events: list[StoredEvent] = field(default_factory=list)
    #: Paths mutated, in order, for revert and for the gate's scoping.
    mutations: list[str] = field(default_factory=list)
    #: Set when the developer asks it to stop. Checked by the loop at two points.
    cancel: threading.Event = field(default_factory=threading.Event)
    #: Set by "stop after this turn". Unlike ``cancel`` this is read only
    #: between turns, so work already in flight completes rather than being
    #: abandoned halfway through a file.
    winding_down: threading.Event = field(default_factory=threading.Event)
    #: Corrections typed while the run is going. Drained by the loop at the top
    #: of each turn.
    _steer: list[str] = field(default_factory=list)

    _next_id: int = 1
    _subscribers: list[asyncio.Queue] = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    # -- events ------------------------------------------------------------

    def record(self, event: Event) -> StoredEvent:
        """Persist an event and hand it to every live subscriber.

        Persisted first. A crash between sending and recording would produce an
        event the client saw and the log does not have, and resumption would then
        silently skip it — a hole that looks like nothing is wrong.

        Transient events (``assistant_delta``, ``heartbeat``) are relayed but not
        stored: they are superseded by the ``assistant`` message that follows, so
        replaying them on reconnect would re-type an answer the client already
        has in full.
        """
        with self._lock:
            stored = StoredEvent(
                id=self._next_id,
                type=event.type,
                data=event.data,
                at=datetime.now(tz=timezone.utc),
            )
            if not event.transient:
                self._next_id += 1
                self.events.append(stored)

            if event.type is EventType.TOOL_RESULT:
                for mutation in event.data.get("mutations") or []:
                    path = mutation.get("path")
                    if path and path not in self.mutations:
                        self.mutations.append(path)

            subscribers = list(self._subscribers)

        for queue in subscribers:
            queue.put_nowait(stored)
        return stored

    def since(self, event_id: int) -> list[StoredEvent]:
        with self._lock:
            return [e for e in self.events if e.id > event_id]

    def subscribe(self) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue()
        with self._lock:
            self._subscribers.append(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        with self._lock:
            if queue in self._subscribers:
                self._subscribers.remove(queue)

    # -- lifecycle ---------------------------------------------------------

    def finish(self, result: RunResult) -> None:
        self.status = Status.of(result.outcome)
        self.summary = result.summary
        self.finished_at = datetime.now(tz=timezone.utc)
        for path in result.mutations:
            if path not in self.mutations:
                self.mutations.append(path)

    def abort(self) -> None:
        self.cancel.set()

    def wind_down(self) -> None:
        self.winding_down.set()

    def steer(self, text: str) -> None:
        with self._lock:
            self._steer.append(text)

    def drain_steer(self) -> list[str]:
        with self._lock:
            queued, self._steer = self._steer, []
        return queued

    @property
    def queued(self) -> int:
        return len(self._steer)

    @property
    def running(self) -> bool:
        return self.status is Status.RUNNING

    def as_dict(self, *, transcript: bool = False) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "id": self.id,
            "task": self.task,
            "workspace": self.workspace,
            "status": str(self.status),
            "created_at": self.created_at.isoformat(),
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
            "summary": self.summary,
            "mutations": list(self.mutations),
            "events": len(self.events),
            "resumable": self.status.resumable,
            "queued": self.queued,
            "winding_down": self.winding_down.is_set(),
        }
        if transcript:
            payload["transcript"] = [e.as_dict() for e in self.events]
        return payload


class SessionStore:
    """Sessions for this runtime, and the git operations revert needs."""

    def __init__(self, workspace: Path, *, limit: int = 200) -> None:
        self.workspace = workspace
        self.limit = limit
        self._sessions: dict[str, Session] = {}

    def create(self, task: str) -> Session:
        session = Session(id=uuid.uuid4().hex[:12], task=task, workspace=str(self.workspace))
        self._sessions[session.id] = session
        self._trim()
        return session

    def get(self, session_id: str) -> Session | None:
        return self._sessions.get(session_id)

    def list(self, *, status: str | None = None) -> list[Session]:
        sessions = sorted(self._sessions.values(), key=lambda s: s.created_at, reverse=True)
        if status:
            sessions = [s for s in sessions if str(s.status) == status]
        return sessions

    def delete(self, session_id: str) -> bool:
        return self._sessions.pop(session_id, None) is not None

    def _trim(self) -> None:
        if len(self._sessions) <= self.limit:
            return
        finished = sorted(
            (s for s in self._sessions.values() if not s.running),
            key=lambda s: s.created_at,
        )
        for session in finished[: len(self._sessions) - self.limit]:
            del self._sessions[session.id]

    # -- revert ------------------------------------------------------------

    def plan_revert(self, session: Session) -> RevertPlan:
        """Work out what a revert would do, without doing it.

        Refuses a running session (§12's guard) by returning everything as
        blocked rather than by raising, so the caller can show the reason in the
        same list it would have shown the paths in.
        """
        if session.running:
            return RevertPlan(
                session.id,
                blocked=tuple(
                    (p, "the session is still running") for p in session.mutations
                ),
            )

        restore: list[str] = []
        delete: list[str] = []
        blocked: list[tuple[str, str]] = []

        for path in session.mutations:
            tracked = self._in_head(path)
            if tracked is None:
                blocked.append((path, "this workspace is not a git repository"))
            elif tracked:
                restore.append(path)
            else:
                # No baseline in HEAD means the session created it. §12: delete.
                delete.append(path)
        return RevertPlan(session.id, tuple(restore), tuple(delete), tuple(blocked))

    def revert(self, session: Session) -> RevertPlan:
        plan = self.plan_revert(session)
        if plan.blocked or plan.empty:
            return plan

        if plan.restore:
            self._git("checkout", "HEAD", "--", *plan.restore)
        for path in plan.delete:
            target = self.workspace / path
            if target.is_file():
                target.unlink()
        return plan

    def _in_head(self, path: str) -> bool | None:
        """Whether HEAD has this path. ``None`` when there is no repository."""
        if not self._is_repo():
            return None
        result = self._git("cat-file", "-e", f"HEAD:{path}")
        return result is not None and result.returncode == 0

    def _is_repo(self) -> bool:
        """Asked once, explicitly, rather than inferred from another command.

        The first version folded this into the exit code of `cat-file`, which
        conflated "the file is not in HEAD" with "this is not a repository" —
        and those lead to opposite actions. The first means the session created
        the file, so revert deletes it; the second means revert cannot run at
        all. Deleting a developer's file because git was absent is the kind of
        mistake there is no undo for.
        """
        result = self._git("rev-parse", "--git-dir")
        return result is not None and result.returncode == 0

    def _git(self, *args: str) -> subprocess.CompletedProcess | None:
        """Run git, or return None when it is not usable here."""
        try:
            return subprocess.run(  # noqa: S603 - argv list, shell=False
                ["git", *args],
                cwd=self.workspace,
                capture_output=True,
                text=True,
                timeout=60,
                check=False,
                shell=False,
            )
        except (FileNotFoundError, subprocess.SubprocessError):
            return None


def replay(events: Iterable[StoredEvent]) -> str:
    """Encode stored events as an SSE stream, for a resumption response."""
    return "".join(event.sse() for event in events)
