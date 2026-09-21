"""Sessions: the event log, resumption, abort, and revert.

Part B §12 calls sessions "``postgen``'s strongest reliability story", and §14
names the one gap in it: the SSE client parses a clean stream well but has no
resumption path, so a dropped connection loses the live view of a run that is
still executing. Every event here carries a monotonic id and is kept, so
``since_id`` replay is a query rather than a rewrite.

**Events are recorded before they are sent.** The order matters. Sending first
and recording afterwards means a crash between the two produces an event the
client saw and the log does not have — and then resumption silently skips it,
which is worse than losing the connection, because nothing looks wrong.

**And written down.** This paragraph used to say "persisted" and mean "appended
to a list in this process": a daemon restart — which a VS Code reload causes —
took every transcript with it, along with the mutation list ``revert`` reads
(BUG L-7). ``journal.py`` now appends each stored event to
``.dakcoder/sessions/<id>/events.jsonl`` and keeps a small ``session.json``
summary beside it, and ``SessionStore`` restores the summaries at startup.
Best-effort throughout: a full disk costs the transcript, never the run.

**And read back as a conversation, not only as a record.** The transcript
surviving a restart is what a panel needs; it is not what the *agent* needs.
``rehydrate.py`` replays those events through the ContextManager's own append
methods, so a follow-up sent after a window reload continues the conversation
instead of starting the task again. What does not come back is the loop's own
ledgers — which searches were exhausted, which reads were refused — and the
direction of that loss is safe: the agent may repeat a search, never skip work
it has not done.

**Revert restores from a pre-run snapshot, not from git.** ``undo.py`` copies a
path's bytes the first time a mutating tool touches it, and ``plan_revert``
reads that. The earlier design restored to HEAD, which is correct only if the
agent was the only writer since the last commit — an assumption the rest of this
system is careful never to make, and one that destroyed a developer's
uncommitted edits and deleted their untracked files (BUG L-11). See §8 of
``ARCHITECTURE_AUDIT.md``.
"""

from __future__ import annotations

import asyncio
import subprocess
import threading
import uuid
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path
from typing import Any

from dakcoder_shared.envelope import TRANSIENT, Event, EventType

from .loop import Outcome, RunResult
from .journal import Journal, parse_time, restore_summaries
from .undo import PreState, UndoStore

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

        Transient events omit it, which is the spec's way of saying "this frame
        is not a place to resume from": a frame with no ``id:`` leaves the last
        event id where it was. They have to. A transient event is never stored,
        so it is never given an id of its own and carries the one the *next*
        stored event will get — and a client that remembered it would resume
        past an event it had never been sent.
        """
        import json

        body = json.dumps(self.data, separators=(",", ":"), ensure_ascii=False)
        head = "" if self.type in TRANSIENT else f"id: {self.id}\n"
        return f"{head}event: {self.type}\ndata: {body}\n\n"


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
    #: Set as the run ends, so a correction typed in the window between the last
    #: drain and the end of the run becomes the next message rather than being
    #: appended to a queue nobody will read again.
    _steer_closed: bool = False

    #: Where this session is written down. ``None`` for a session nobody will
    #: want back — the tests build plenty, and a fixture should not leave
    #: directories in a temp workspace it did not ask for.
    journal: Journal | None = None
    #: True for a session restored from disk whose events have not been read
    #: back yet. The transcript is the expensive part and most callers only want
    #: the summary.
    _events_pending: bool = False

    _next_id: int = 1
    _subscribers: list[asyncio.Queue] = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    # -- events ------------------------------------------------------------

    def record(self, event: Event) -> StoredEvent:
        """Persist an event and hand it to every live subscriber.

        Recorded first, and written to the journal in the same lock, so the file
        is in id order for the same reason the list is. A crash between sending
        and recording would produce an event the client saw and the log does not
        have, and resumption would then silently skip it — a hole that looks like
        nothing is wrong.

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

            mutated = False
            if event.type is EventType.TOOL_RESULT:
                for mutation in event.data.get("mutations") or []:
                    path = mutation.get("path")
                    if path and path not in self.mutations:
                        self.mutations.append(path)
                        mutated = True

            # Written down inside the lock, so the file is in id order for the
            # same reason the list is. Buffered, so this costs a string append
            # per event and a disk write a few times a turn.
            if self.journal is not None and not event.transient:
                self.journal.append(stored.as_dict())
                if mutated:
                    # The mutation list is what `revert` reads, and a revert
                    # after a crash is exactly the revert the developer wants.
                    self.journal.flush()
                    self._write_meta()
                elif event.type in (EventType.FINISH, EventType.END):
                    self.journal.flush()

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
        if self.journal is not None:
            self.journal.flush()
            self._write_meta()

    def _write_meta(self) -> None:
        """Update the on-disk summary. Never the transcript: that is append-only."""
        if self.journal is None:
            return
        self.journal.write_meta(
            {
                "id": self.id,
                "task": self.task,
                "workspace": self.workspace,
                "status": str(self.status),
                "created_at": self.created_at.isoformat(),
                "finished_at": self.finished_at.isoformat() if self.finished_at else None,
                "summary": self.summary,
                "mutations": list(self.mutations),
            }
        )

    def hydrate(self) -> None:
        """Read the transcript back, for a session restored from disk.

        Called when something actually asks for it. A daemon starting in a
        workspace with a hundred finished sessions reads a hundred small
        summaries; it does not read a hundred transcripts.
        """
        with self._lock:
            if not self._events_pending or self.journal is None:
                return
            self._events_pending = False
            restored: list[StoredEvent] = []
            for raw in self.journal.read_events():
                try:
                    restored.append(
                        StoredEvent(
                            id=int(raw["id"]),
                            type=EventType(raw["type"]),
                            data=raw.get("data") or {},
                            at=parse_time(raw.get("at")) or self.created_at,
                        )
                    )
                except (KeyError, TypeError, ValueError):
                    continue
            # Ahead of anything already in memory: a restored session that has
            # been run again holds the new events, and the file holds the old.
            existing = {e.id for e in self.events}
            self.events[:0] = [e for e in restored if e.id not in existing]
            self._next_id = max((e.id for e in self.events), default=0) + 1

    def abort(self) -> None:
        self.cancel.set()

    def wind_down(self) -> None:
        self.winding_down.set()

    def steer(self, text: str) -> bool:
        """Queue a correction for the running loop to read before its next turn.

        Returns False once the run has stopped taking them. That answer has to
        come from inside the lock, because the caller's ``session.running`` check
        and this append were two separate observations of a value the worker
        thread changes: a message posted in that window was appended to a queue
        nothing would ever drain, and was then neither delivered, nor recorded,
        nor turned into a follow-up — the developer's message simply vanished
        (BUG L-9). A False here means "this is the next message, not a
        correction", which the caller can act on.
        """
        with self._lock:
            if self._steer_closed:
                return False
            self._steer.append(text)
            return True

    def drain_steer(self) -> list[str]:
        with self._lock:
            queued, self._steer = self._steer, []
        return queued

    def close_steer(self) -> list[str]:
        """Stop taking corrections, and hand back anything never drained.

        Called once by the worker as the run ends. Whatever comes back was typed
        while the run was alive and read by nobody, so the caller owes the
        developer a follow-up run with it.
        """
        with self._lock:
            self._steer_closed = True
            queued, self._steer = self._steer, []
        return queued

    def reopen_steer(self) -> None:
        """Take corrections again. Called when a session starts another run."""
        with self._lock:
            self._steer_closed = False

    @property
    def queued(self) -> int:
        return len(self._steer)

    @property
    def turns(self) -> int:
        """How many messages the developer has sent in this conversation.

        Counted off the transcript rather than kept as a second field, because a
        counter and a log that are supposed to agree are two things that can
        disagree — and the log is the one clients read.
        """
        with self._lock:
            return sum(1 for e in self.events if e.type is EventType.USER)

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
            "turns": self.turns,
            "winding_down": self.winding_down.is_set(),
        }
        if transcript:
            self.hydrate()
            payload["transcript"] = [e.as_dict() for e in self.events]
            payload["events"] = len(self.events)
        return payload


class SessionStore:
    """Sessions for this runtime, and the git operations revert needs."""

    def __init__(self, workspace: Path, *, limit: int = 200, persist: bool = True) -> None:
        self.workspace = workspace
        self.limit = limit
        self.persist = persist
        self._sessions: dict[str, Session] = {}
        if persist:
            self.restore()

    def restore(self) -> int:
        """Load what previous runs of the daemon left behind.

        A VS Code reload restarts the daemon, and until now that took every
        transcript with it — along with the mutation list `revert` reads, which
        is the one piece of state a developer actually needs *after* something
        has gone wrong (BUG L-7).

        Summaries only; `Session.hydrate` reads a transcript when one is asked
        for. A session that was RUNNING when the process died is marked ERROR:
        nothing is driving it, and leaving it "running" makes it unresumable,
        undeletable and permanently in the way.
        """
        loaded = 0
        for meta in restore_summaries(self.workspace):
            session_id = str(meta.get("id"))
            if not session_id or session_id in self._sessions:
                continue
            status = Status.of(str(meta.get("status") or "error"))
            summary = str(meta.get("summary") or "")
            if status is Status.RUNNING:
                status = Status.ERROR
                summary = summary or "the runtime stopped while this run was in flight"
            created = parse_time(meta.get("created_at")) or datetime.now(tz=timezone.utc)
            self._sessions[session_id] = Session(
                id=session_id,
                task=str(meta.get("task") or ""),
                workspace=str(meta.get("workspace") or self.workspace),
                status=status,
                created_at=created,
                finished_at=parse_time(meta.get("finished_at")),
                summary=summary,
                mutations=[str(m) for m in (meta.get("mutations") or [])],
                journal=Journal(self.workspace, session_id),
                _events_pending=True,
                _steer_closed=True,
            )
            loaded += 1
        self._trim()
        return loaded

    def create(self, task: str) -> Session:
        session_id = uuid.uuid4().hex[:12]
        session = Session(
            id=session_id,
            task=task,
            workspace=str(self.workspace),
            journal=Journal(self.workspace, session_id) if self.persist else None,
        )
        session._write_meta()
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

    #: Called with a session id whenever the store stops holding it, so the
    #: things keyed on that id elsewhere go too. Set by the loopback, which owns
    #: the context and the loop for each session; ``SessionStore`` should not
    #: know what they are.
    on_forget: Callable[[str], None] | None = None

    def delete(self, session_id: str) -> bool:
        gone = self._sessions.pop(session_id, None) is not None
        if gone:
            self._forget(session_id)
        return gone

    def _trim(self) -> None:
        if len(self._sessions) <= self.limit:
            return
        finished = sorted(
            (s for s in self._sessions.values() if not s.running),
            key=lambda s: s.created_at,
        )
        for session in finished[: len(self._sessions) - self.limit]:
            del self._sessions[session.id]
            self._forget(session.id)

    def _forget(self, session_id: str) -> None:
        """Tell the owner of everything else keyed on this session that it is gone.

        The store trimmed itself and `runtime.contexts` / `runtime.loops` kept
        the whole message list and the whole ledger set for every session the
        daemon had ever run (BUG L-12). A long-lived daemon in a busy workspace
        held every conversation it had ever had, and the one thing it definitely
        no longer needed was the one nothing dropped.
        """
        if self.on_forget is None:
            return
        try:
            self.on_forget(session_id)
        except Exception:  # noqa: BLE001 - housekeeping must not fail a request
            pass

    # -- revert ------------------------------------------------------------

    def undo_store(self, session: Session) -> UndoStore:
        """The pre-run snapshots for one session.

        Constructed on demand from the session id rather than held on the
        Session, so a revert works after a daemon restart — which is the case
        the developer is most likely to want it in, and the case the old
        HEAD-based revert was most dangerous in.
        """
        return UndoStore(self.workspace, session.id)

    def plan_revert(self, session: Session) -> RevertPlan:
        """Work out what a revert would do, without doing it.

        Refuses a running session (§12's guard) by returning everything as
        blocked rather than by raising, so the caller can show the reason in the
        same list it would have shown the paths in.

        The pre-run snapshot decides, not HEAD (BUG L-11). Restoring a touched
        path to HEAD is only correct when the agent was the only writer since the
        last commit, and nothing else in this system assumes that: it destroyed a
        developer's uncommitted edit to a file the agent later touched, and
        *deleted* a developer's untracked file the agent merely modified. What
        the run found at a path is a fact the run can record, so it records it,
        and a path with no record is blocked rather than guessed at.
        """
        if session.running:
            return RevertPlan(
                session.id,
                blocked=tuple(
                    (p, "the session is still running") for p in session.mutations
                ),
            )

        undo = self.undo_store(session)
        restore: list[str] = []
        delete: list[str] = []
        blocked: list[tuple[str, str]] = []

        for path in session.mutations:
            match undo.state(path):
                case PreState.FILE:
                    restore.append(path)
                case PreState.ABSENT:
                    delete.append(path)
                case PreState.TOO_LARGE:
                    blocked.append(
                        (path, "it was too large to snapshot before the run changed it")
                    )
                case PreState.UNREADABLE:
                    blocked.append(
                        (path, "its contents could not be read before the run changed it")
                    )
                case PreState.UNRECORDED:
                    blocked.append(
                        (
                            path,
                            "the tool that changed it does not say which file it will "
                            "write until afterwards, so no pre-run copy was taken",
                        )
                    )
                case _:
                    blocked.append((path, self._no_snapshot_reason(path)))

        return RevertPlan(session.id, tuple(restore), tuple(delete), tuple(blocked))

    def _no_snapshot_reason(self, path: str) -> str:
        """Why an unsnapshotted path is not reverted, in the terms the developer needs.

        Deliberately not "restore it from HEAD anyway". A path reaches here when
        it was changed by something that did not run through the router's
        snapshot — a sidecar tool writing a generated file, a run that predates
        this store, a snapshot the disk refused — and in every one of those cases
        the run does not know what was there before. Reverting to HEAD would be a
        guess with a developer's uncommitted work as the stake.
        """
        if not self._is_repo():
            return "there is no pre-run snapshot of it, and this is not a git repository"
        if self._in_head(path):
            return (
                "there is no pre-run snapshot of it; restoring it from HEAD would "
                "also discard any edit made before the run"
            )
        return (
            "there is no pre-run snapshot of it, and it is not in HEAD, so nothing "
            "here knows whether the run created it or only changed it"
        )

    def revert(self, session: Session) -> RevertPlan:
        plan = self.plan_revert(session)
        if plan.blocked or plan.empty:
            return plan

        undo = self.undo_store(session)
        failed: list[tuple[str, str]] = []
        restored: list[str] = []
        for path in plan.restore:
            if undo.restore(path):
                restored.append(path)
            else:
                failed.append((path, "its snapshot could not be read back"))

        deleted: list[str] = []
        for path in plan.delete:
            target = self.workspace / path
            try:
                if target.is_file():
                    target.unlink()
                deleted.append(path)
            except OSError as exc:
                failed.append((path, f"it could not be removed: {exc}"))

        return RevertPlan(session.id, tuple(restored), tuple(deleted), tuple(failed))

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
