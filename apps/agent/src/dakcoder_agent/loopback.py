"""The loopback: the small HTTP+SSE endpoint the extension talks to.

**This is not the gateway.** No auth beyond a loopback token, no quota, no model
key. It runs on the developer's machine, binds to 127.0.0.1, and its only job is
to let the extension drive a run and watch it happen. Model traffic still leaves
through the gateway's ``/v1/llm`` proxy, which is what keeps quota and audit
unbypassable (Part A §15.4).

    POST /v1/tasks                      start a run; returns the session
    GET  /v1/sessions/{id}/events       live SSE, resumable with since_id
    GET  /v1/sessions                   the tree the extension renders
    GET  /v1/sessions/{id}              detail, optionally with the transcript
    POST /v1/sessions/{id}/abort        stop it
    POST /v1/sessions/{id}/revert       restore what it touched to HEAD
    POST /v1/approvals/{id}             accept / reject / edit
    GET  /v1/sessions/{id}/transcript   what happened, or what the model saw
    POST /v1/sessions/{id}/compact      compact the context on demand
    GET  /v1/sessions/{id}/plan         the plan, its statuses and its revisions
    GET  /v1/agenda                     work proposed for later
    POST /v1/agenda                     propose some
    POST /v1/agenda/{id}                approve, drop or complete it
    GET  /v1/health                     version, toolchain, readiness
    GET  /v1/tools                      contract C1

That list is a sample. The full table is in ``api/contract.json``, generated
from this app's own routes and checked in CI.

**The loop is synchronous and this is not.** Two bridges are needed and both are
places where a naive version breaks quietly:

* The run executes in a worker thread and pushes events across with
  ``call_soon_threadsafe``. Running it inline would block the event loop for the
  whole task — including the abort endpoint, which is precisely the one that has
  to answer while a run is in flight.
* An approval blocks the loop thread on a ``threading.Event`` that the HTTP
  handler sets. That is why the loop must not run on the event loop: waiting for
  a decision that arrives over HTTP would otherwise deadlock the server that has
  to deliver it.

**Version pinning is a first-class response field.** Part B §15: silent version
skew across a client/server boundary is the failure that costs the most support
time, so ``/v1/health`` reports the API version and the extension refuses to
proceed on a mismatch rather than failing later in a way nobody can attribute.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
import os
import secrets
import tempfile
import threading
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.routing import APIRoute

from dakcoder_shared import contract
from dakcoder_shared.contract import API_VERSION
from dakcoder_shared.envelope import Event, EventType

from .compaction import CompactionState
from .context import Recap
from .debug import DebugLog
from .journal import Journal
from .loop import AgentLoop, Outcome, RunResult
from . import openapi
from .callers import Authenticator, Caller, Unauthorised, loopback_token
from .modes import Intent
from .policies import AUTO_SAFE, INTERACTIVE, POLICIES, auto_safe
from .plan import AGENDA_STATES, AgendaStore, AgendaTask, PlanRecord
from .rehydrate import rehydrate, restorable, restore_canonical
from .session import Session, SessionStore, Status
from .transcript import Transcript
from .tools.router import ApprovalRequest

__all__ = [
    "API_VERSION",
    "Loopback",
    "PendingApproval",
    "create_app",
    "published_contract",
    "published_openapi",
    "route_table",
]

log = logging.getLogger(__name__)

#: How long a run waits for an approval before giving up. Long enough for someone
#: to read a seven-file scaffold; short enough that a developer who closed the
#: window does not leave a thread parked until the process ends.
#:
#: Overridable, and ``0`` means no timeout at all. The extension's setting
#: documents "0 waits indefinitely" and the backend hard-rejected at ten minutes
#: regardless (BUG EXT-2), so the advertised default was a fiction over a silent
#: auto-rejection of changes the developer was in the middle of reviewing. One of
#: the two had to become true; this is the half that can be.
def _approval_timeout() -> float:
    raw = os.environ.get("DAKCODER_APPROVAL_TIMEOUT", "").strip()
    try:
        seconds = float(raw) if raw else 600.0
    except ValueError:
        return 600.0
    return max(0.0, seconds)


APPROVAL_TIMEOUT = _approval_timeout()

#: How often a blocked run re-reads the deadline while waiting. Short enough
#: that an extension granted at t=590 is seen before the original deadline
#: passes, long enough that a parked approval costs nothing measurable.
APPROVAL_POLL = 5.0


@dataclass
class PendingApproval:
    """One decision the run is blocked on."""

    id: str
    session_id: str
    request: ApprovalRequest
    #: Set by the HTTP handler; the loop thread is waiting on it.
    decided: threading.Event = field(default_factory=threading.Event)
    approved: bool = False
    #: An `edit` decision: corrected arguments. §9 calls this the standout —
    #: fixing the agent's arguments beats rejecting and re-prompting, and it
    #: keeps the developer in the loop without costing a turn.
    arguments: dict[str, Any] | None = None
    at: datetime = field(default_factory=lambda: datetime.now(tz=timezone.utc))
    #: Extended by ``POST /v1/approvals/{id}/extend``. A hard release that turns
    #: a slow review into a rejection is a WCAG 2.2.1 failure — the user cannot
    #: adjust the limit — and the people most likely to exceed ten minutes are
    #: the ones reviewing the changesets that matter most.
    extensions: int = 0
    #: Set when the wait gave up. Read by ``decide`` so a decision that arrives
    #: after the run has already recorded a rejection is told so, instead of
    #: being answered "accepted" for a call that never ran (BUG L-22).
    timed_out: bool = False

    def deadline_in(self) -> float:
        """Seconds left to decide, counting any extensions granted.

        ``math.inf`` when no timeout is configured, so the one place that waits
        does not need a second code path for it.
        """
        if APPROVAL_TIMEOUT <= 0:
            return math.inf
        spent = (datetime.now(tz=timezone.utc) - self.at).total_seconds()
        return max(0.0, APPROVAL_TIMEOUT * (1 + self.extensions) - spent)

    def as_dict(self) -> dict[str, Any]:
        """Includes how long is left to decide.

        Without it the client has to start its own countdown from the moment it
        happened to see the approval, which is wrong by however long the panel
        was hidden — and the number it shows is then a guess about the server's
        clock rather than a report of it.
        """
        return {
            "id": self.id,
            "session_id": self.session_id,
            "seconds_left": round(self.deadline_in(), 1),
            "extensions": self.extensions,
            **self.request.as_dict(),
        }


class Loopback:
    """The runtime the extension drives. One workspace, many sessions."""

    def __init__(
        self,
        workspace: Path,
        build_loop: Callable[[Session, Callable[[ApprovalRequest], bool]], AgentLoop],
        *,
        token: str = "",
        tool_catalog: dict[str, Any] | None = None,
        version: str = "dev",
        gateway_url: str = "",
    ) -> None:
        self.workspace = workspace
        self.build_loop = build_loop
        # A random token, generated by the extension and passed in the spawn
        # environment. It authenticates the *extension to its own runtime* — a
        # different question from who the developer is (§15.3), and the only one
        # a process on loopback can answer.
        self.token = token or secrets.token_urlsafe(32)
        self.sessions = SessionStore(workspace)
        self.tool_catalog = tool_catalog or {}
        self.version = version
        self.gateway_url = gateway_url
        self.approvals: dict[str, PendingApproval] = {}
        self.contexts: dict[str, Any] = {}
        #: The loop that last ran for each session, so a follow-up can inherit
        #: its ledgers the way it already inherits its context.
        self.loops: dict[str, Any] = {}
        # Dropped together with the session they belong to. These hold the whole
        # message list and the whole ledger set, so they are the expensive half
        # of a session and were the half nothing ever released (BUG L-12).
        self.sessions.on_forget = self._forget
        self.ready: dict[str, Any] = {"prewarmed": False}
        #: Tool → version, set once by ``toolchain.probe_in_background``. Empty
        #: until then, and ``/v1/health`` leaves the field out while it is.
        self.toolchain: dict[str, str | None] = {}
        #: The developer's gateway JWT, as the extension last refreshed it.
        #: Read per request by the LLM client rather than captured at spawn —
        #: see ``POST /v1/credential``. Empty means "whatever the process
        #: started with", which is what ``serve`` falls back to.
        self._credential: str = ""

    def set_toolchain(self, versions: dict[str, str | None]) -> None:
        self.toolchain = dict(versions)

    def set_credential(self, jwt: str) -> None:
        self._credential = jwt.strip()

    def credential(self) -> str:
        return self._credential

    # -- running a task -----------------------------------------------------

    def start(
        self,
        task: str,
        *,
        intent: Intent = Intent.AUTO,
        acceptance=(),
        owner: str = "",
        approval_policy: str = INTERACTIVE,
    ) -> Session:
        session = self.sessions.create(task, owner=owner, approval_policy=approval_policy)
        # Recorded before the loop is spawned, so the developer's own words are
        # the first row of the transcript rather than something only the panel
        # that happened to be open at the time remembers.
        session.record(Event(EventType.USER, {"text": task, "turn": 0}))
        self._spawn(session, task, intent, tuple(acceptance))
        return session

    def _resume_intent(self, session: Session) -> Intent:
        """What a follow-up on this conversation is asking for.

        ``AUTO``, always, and that is the change. The old version returned the
        *mode the previous run ended in* -- so a conversation that had finished
        in the Debugger answered its next message with the Debugger's overlay,
        its budget and its tool set, whatever the message said. It was written to
        fix the opposite bug (a session that had just produced a plan re-planned
        it on "go") and it fixed that one by hard-coding the other.

        Neither is a decision about what was asked. A follow-up is a new
        request, and the classifier sees the conversation as well as the message
        -- which is exactly what it needs to tell "go" after a plan from "go" as
        a topic. Where the run resumes follows from that, not from where it
        stopped.
        """
        del session
        return Intent.AUTO

    def follow_up(
        self, session: Session, text: str, *, intent: Intent | None = None
    ) -> Session:
        """Another message in the same conversation.

        This is what a chat panel does when a run has finished and the developer
        types again. It is deliberately *not* ``start``: a new session would give
        the model a new context, so the second question would be answered by
        something that had never seen the first one — and it is deliberately not
        ``resume``, which re-seeds the original task to have another go at work
        that did not land.

        The session's context manager is reused, so the working set — every file
        already read, every answer already given — carries forward, and the
        budget and compaction machinery see one growing conversation rather than
        a series of amnesiac ones.
        """
        if session.running:
            raise RuntimeError("that session is still running")

        session.status = Status.RUNNING
        session.finished_at = None
        session.summary = ""
        session.cancel = threading.Event()
        session.winding_down = threading.Event()
        session.record(Event(EventType.USER, {"text": text, "turn": session.turns}))
        self._spawn(
            session, text, intent or self._resume_intent(session), (), continued=True
        )
        return session

    def _spawn(
        self,
        session: Session,
        task: str,
        intent: Intent,
        acceptance: tuple[str, ...],
        *,
        continued: bool = False,
    ) -> None:
        """Build a loop for this session and run it on a worker thread.

        Factored out of ``start`` so ``resume`` and ``follow_up`` drive the
        identical path. Two code paths that both "run a session" drift, and the
        one that drifts is always the one nobody demos.
        """
        loop = asyncio.get_running_loop()

        def approve(request: ApprovalRequest) -> bool:
            if session.approval_policy == AUTO_SAFE:
                # Decided now, by rule, and written into the transcript: a run
                # nobody watched can still be read (policies.py).
                approved, reason = auto_safe(request)
                self.approvals.pop(request.id, None)
                emit(
                    Event(
                        EventType.GATE,
                        {
                            "kind": "auto_approval",
                            "id": request.id,
                            "tool": request.tool,
                            "paths": list(request.paths),
                            "approved": approved,
                            "reason": reason,
                        },
                    )
                )
                return approved
            return self._await_decision(session, request)

        def register(request: ApprovalRequest) -> None:
            """Put the approval in the table before the event announcing it goes out."""
            self.approvals[request.id] = PendingApproval(request.id, session.id, request)

        session.reopen_steer()
        agent = self.build_loop(session, approve)
        # Set here rather than asked of ``build_loop``, so a factory that knows
        # nothing about sessions stays a factory. Without it every ledger row
        # this run produces is attributed to no session at all.
        agent.session_id = session.id
        # The canonical transcript goes to the same directory as the event
        # stream, and for the reason the two files are different: `events.jsonl`
        # is what the panel replays, `transcript.jsonl` is what the model was
        # actually talking to. Only the second can restore a conversation
        # exactly, and only if it is written as it happens.
        if session.journal is not None:
            agent.context.attach_journal(session.journal)
        # Full-fidelity turn recording, when DAKCODER_DEBUG is set. Attached
        # here because this is where the session id and the workspace are both
        # known, and it writes beside the transcript and the plan.
        agent._debug = DebugLog.for_session(self.workspace, session.id)
        if agent._debug is not None:
            log.info("debug recording to %s", agent._debug.path)
        if continued:
            # The conversation *is* the context manager. ``build_loop`` hands
            # back a fresh one because most runs want one; a follow-up wants the
            # one that already holds the exchange, and swapping it here keeps
            # ``build_loop`` a factory rather than something that has to know
            # about session lifecycles.
            prior = self.contexts.get(session.id) or self._restore_context(session, agent)
            if prior is not None:
                agent.context = prior
            # And the ledgers, for the same reason. The working set remembering
            # a search while the ledger that knows it was exhausted starts empty
            # is how "where is the plan?" reproduced the previous message's loop
            # verbatim. See ``AgentLoop.carry_from`` for what does and does not
            # travel.
            previous = self.loops.get(session.id)
            if previous is not None:
                agent.carry_from(previous)
            else:
                # No loop in this process means a restart. The context comes
                # back from the canonical transcript above; the plan comes back
                # from its own file, because it is the one piece of the run's
                # state that a developer can see on screen and that the agent
                # would otherwise have forgotten -- "step 4 of 7" in the panel
                # beside an agent starting again from step 1.
                agent.restore_plan(session.id)
        self.loops[session.id] = agent
        agent.on_pending = register
        agent.cancelled = session.cancel.is_set
        agent.winding_down = session.winding_down.is_set
        agent.steer = session.drain_steer
        # Held so the context inspector can report what the server actually
        # holds. Contract C5 makes the server authoritative on context, and a
        # client that reconstructs it will eventually disagree.
        self.contexts[session.id] = agent.context

        def emit(event: Event) -> None:
            """Hand an event to the event loop, or record it directly if the
            loop has gone.

            A server shutting down while a run is in flight closes the loop, and
            an unguarded `call_soon_threadsafe` then raises *inside the worker
            thread* — killing it with an unhandled exception and leaving the
            session stuck at "running" forever. Recording directly keeps the
            transcript complete for whoever reads it next; there is simply
            nobody live to deliver it to.
            """
            try:
                loop.call_soon_threadsafe(session.record, event)
            except RuntimeError:
                session.record(event)

        # The road transient events take. The loop is a generator and streamed
        # text happens while it is blocked inside a completion, so deltas cannot
        # travel by yield; they go straight to the same relay the yielded events
        # use, from the same thread, which is what keeps them in order.
        agent.on_event = emit

        def settle(update: Callable[[], None]) -> None:
            """Apply the session's terminal status *after* its last event lands.

            The events travel to the transcript by ``call_soon_threadsafe``; the
            status used to be flipped right here on the worker thread. So for a
            moment the session read as finished while its ``finish`` and ``end``
            were still queued as callbacks, and a client that connected in that
            window replayed a transcript with no ``end``, saw "not running", and
            closed -- the run looked as though it had died mid-sentence. The
            test suite had to work around the same gap (``settle`` in
            ``test_loopback``). Queued behind the events on the same loop, the
            status cannot be observed ahead of the record that justifies it.
            """
            try:
                loop.call_soon_threadsafe(update)
            except RuntimeError:
                update()

        def run() -> None:
            try:
                for event in agent.run(
                    task, acceptance=acceptance, intent=intent, continued=continued
                ):
                    emit(event)
            except Exception as exc:  # noqa: BLE001 - a crashed run must still close
                # A `finish` as well as an `end`, and in that order.
                #
                # This emitted `error` then `end` and nothing else, and every
                # client derives "the run is over" from `finish`. So a crashed
                # run left the panel on "Working..." forever, swallowed the next
                # message as a mid-run correction, and gave the developer no
                # sign that anything had gone wrong. The extension now treats
                # `end` as terminal too, but a run that failed should say so in
                # the same shape a run that succeeded does -- a client should
                # not have to reconstruct the outcome from the absence of an
                # event.
                summary = f"the run failed: {exc}"
                emit(Event(EventType.ERROR, {"message": summary}))
                failed = RunResult(
                    Outcome.ERROR,
                    summary,
                    getattr(agent.context, "turn", 0),
                    tuple(agent.router.touched),
                )
                emit(Event(EventType.FINISH, failed.as_dict()))
                emit(Event(EventType.END, failed.as_dict()))

                def failed_status() -> None:
                    session.status = Status.ERROR
                    session.summary = summary
                    session.finished_at = datetime.now(tz=timezone.utc)

                settle(failed_status)
            else:
                result = agent.result
                if result is not None:
                    settle(lambda: session.finish(result))
            finally:
                # Every approval this run was waiting on is released, whichever
                # way the run ended. A crashed run holding a pending approval
                # leaves the extension showing a card nothing will ever answer.
                self._release(session.id)
                self._rescue_steers(session, loop)

        threading.Thread(target=run, name=f"dakcoder-{session.id}", daemon=True).start()

    def _rescue_steers(self, session: Session, loop: asyncio.AbstractEventLoop) -> None:
        """Turn a correction the run never read into the next message.

        A developer typing while the last turn is in flight used to lose the
        message outright (BUG L-9): ``message_session`` saw ``running``, queued
        it, and the run finished before the next drain — so it was never
        delivered, never recorded, and never became a follow-up. Silence was the
        whole of the feedback.

        Closing the queue is atomic with taking what is in it, so the window
        does not simply move: anything posted after this point is refused by
        ``session.steer`` and the endpoint sends it as a follow-up itself.
        """
        leftover = session.close_steer()
        if not leftover:
            return

        text = "\n\n".join(leftover)

        def deliver() -> None:
            if session.running:
                # Something restarted the session between the worker ending and
                # this callback. Re-queueing keeps the message in the run that
                # is now live rather than starting a third one.
                if not session.steer(text):
                    return
                return
            try:
                self.follow_up(session, text)
            except RuntimeError:
                # The session is gone or already running again; the message is
                # still in the transcript as a USER event either way.
                pass

        try:
            loop.call_soon_threadsafe(deliver)
        except RuntimeError:
            # The event loop has closed (shutdown). Record it so the transcript
            # shows what the developer typed, even though nothing can run it.
            session.record(Event(EventType.USER, {"text": text, "turn": session.turns}))

    def resume(self, session: Session, *, note: str = "") -> Session:
        """Run the session again, on the same transcript and the same context.

        A *resume*, not a new task: the id, the event log and the mutation list
        are the ones the developer was already looking at, so the second attempt
        appears where the first one ended rather than in a new row that shares
        nothing with it.

        **And the same conversation.** This used to build a run on a *fresh*
        context seeded with ``task + "The previous attempt ended: …"`` while the
        EXHAUSTED message on screen promised "Resume continues on this same
        transcript with a fresh turn budget" (BUG RT-1). Every file the run had
        read, every answer it had been given and every ledger it had built were
        discarded, and the developer's evidence that this was not so — the
        transcript, still on screen — was the same session's event log. A run
        that exhausted its turns at the point of writing the last file resumed by
        re-reading the service from scratch.

        It is the follow-up path now, with the note as the message: context
        reused, ledgers carried, Router carried (so the gate still knows what
        this session changed), and a fresh turn budget because the loop is new.
        The only thing that differs from a follow-up is what the message says —
        which is the honest difference between "carry on" and "here is something
        else".
        """
        if session.running:
            raise RuntimeError("that session is still running")

        parts = []
        if session.summary:
            parts.append(f"The previous attempt ended: {session.summary}")
        if note:
            parts.append(note)
        parts.append(
            "Carry on from where that left off. Everything above is still your "
            "work; do not start again from the beginning."
        )
        message = "\n\n".join(parts)

        session.status = Status.RUNNING
        session.finished_at = None
        session.cancel = threading.Event()
        session.winding_down = threading.Event()
        session.record(Event(EventType.USER, {"text": message, "turn": session.turns}))
        # A resume is another go at work that did not land, so it is a change
        # request by construction -- there is nothing for the classifier to
        # decide.
        #
        # `continued=True` wants a context to reuse. The daemon holds one only
        # for a session it has run since it started; after a restart — which a
        # VS Code window reload causes — `_spawn` rebuilds it from the session's
        # own transcript instead. Re-seeding the original task is the last
        # resort now rather than the first, and it stays because a transcript
        # can be missing, unreadable, or too short to be a conversation.
        self._spawn(session, message, Intent.AGENT, (), continued=True)
        return session

    def _restore_context(self, session: Session, agent: Any) -> Any:
        """Rebuild this session's conversation from disk, or return ``None``.

        The daemon holds a context only for a session it has run since it
        started, and a VS Code window reload restarts the daemon. So the case
        this covers is the ordinary one: the developer reloads at turn 40 and
        types "carry on with the repo layer". Before this, that answer came from
        a context seeded with the original task and nothing else — the agent
        began the migration again, having forgotten every file it had read,
        while the transcript proving otherwise was on screen beside it.

        `journal.py` made the *record* survive a restart. This makes the
        conversation survive one. What does not come back is the loop's own
        ledgers — which searches were exhausted, which reads were refused — and
        the direction of that loss is safe: the agent may repeat a search, never
        skip work it has not done. See `rehydrate` for the rest.

        Best-effort in the same sense the journal is: a transcript that cannot be
        read costs the continuation, not the run. The caller falls back to
        re-seeding the task, which is what it did before.
        """
        try:
            # The canonical transcript first. It is the conversation the run was
            # actually having -- the same records, with the same compaction
            # sidecar projected over them -- rather than a reconstruction from
            # the event stream, which is what the fallback below produces. A
            # session recorded before the transcript existed has no such file,
            # and falls through.
            if session.journal is not None:
                canonical = restore_canonical(
                    session.journal,
                    context=agent.context,
                    task=session.task,
                    acceptance=tuple(getattr(session, "acceptance", ()) or ()),
                )
                if canonical is not None:
                    log.info(
                        "restored %s from its canonical transcript: %d record(s)",
                        session.id,
                        canonical.events,
                    )
                    self.contexts[session.id] = canonical.context
                    return canonical.context

            session.hydrate()
            events = [
                {"type": str(event.type), "data": event.data} for event in session.events
            ]
            if not restorable(events):
                return None
            restored = rehydrate(
                events,
                context=agent.context,
                task=session.task,
                acceptance=tuple(getattr(session, "acceptance", ()) or ()),
            )
        except Exception:  # noqa: BLE001 - a lost transcript must not fail a run
            log.warning("could not restore %s from disk", session.id, exc_info=True)
            return None

        if restored.turns == 0:
            return None
        log.info(
            "restored %s from disk: %d turn(s) of %d event(s)%s",
            session.id,
            restored.turns,
            restored.events,
            "" if restored.complete else f", {restored.dropped_turns} dropped for budget",
        )
        self.contexts[session.id] = restored.context
        return restored.context

    def _await_decision(self, session: Session, request: ApprovalRequest) -> bool:
        """Block the loop thread until the developer decides, or time runs out."""
        # Registered by ``on_pending`` before the event was emitted. Falling
        # back to creating one keeps a caller that drives the loop directly
        # (the tests, the CLI) working.
        pending = self.approvals.get(request.id)
        if pending is None:
            pending = PendingApproval(request.id, session.id, request)
            self.approvals[pending.id] = pending

        # Polled rather than waited once, so `/extend` can actually extend
        # (BUG EXT-1). A single `wait(timeout=deadline_in())` computed its
        # timeout before the extension existed: the counter went up, the UI
        # showed minutes remaining, and the run rejected the approval at the
        # original deadline anyway. The deadline is re-read every poll, so
        # granting time works whenever it is granted.
        while not pending.decided.wait(timeout=min(APPROVAL_POLL, pending.deadline_in())):
            if pending.deadline_in() <= 0:
                # A timeout is a refusal, not an approval. Nobody looked, so
                # nobody agreed — and the failure mode of the opposite choice is
                # a write that happened while the developer was at lunch.
                self.approvals.pop(pending.id, None)
                pending.timed_out = True
                return False

        self.approvals.pop(pending.id, None)
        if pending.approved and pending.arguments is not None:
            request.arguments.clear()
            request.arguments.update(pending.arguments)
        return pending.approved

    def _forget(self, session_id: str) -> None:
        """Release everything this runtime holds for a session the store dropped."""
        self.contexts.pop(session_id, None)
        self.loops.pop(session_id, None)
        for approval_id in [
            key for key, p in self.approvals.items() if p.session_id == session_id
        ]:
            self.approvals.pop(approval_id, None)

    def _release(self, session_id: str) -> None:
        for pending in [p for p in self.approvals.values() if p.session_id == session_id]:
            pending.approved = False
            pending.decided.set()

    def pending_for(self, session_id: str) -> list[PendingApproval]:
        return [p for p in self.approvals.values() if p.session_id == session_id]


def create_app(runtime: Loopback, *, authenticate: Authenticator | None = None) -> FastAPI:
    """The runtime's HTTP API.

    ``authenticate`` decides who a request is from. The default is the loopback
    token, which has one caller; a hosted runtime passes one that has many.
    """
    app = FastAPI(title="dakcoderd", version=runtime.version)
    app.state.runtime = runtime
    authenticate = authenticate or loopback_token(lambda: runtime.token)

    # -- who is calling, and what is theirs ---------------------------------
    #
    # Every route but /v1/health takes its caller from `caller`; every route
    # with a session or an approval in its path takes it from `owned` or
    # `owned_approval`, never from the store directly. test_tenancy walks the
    # route table and fails on any route that does not, because a route added
    # without the filter is a cross-tenant leak (host-plan §8), and six routes
    # once arrived in a single release.

    def caller(request: Request) -> Caller:
        try:
            return authenticate(request.headers)
        except Unauthorised as exc:
            raise HTTPException(status_code=401, detail=str(exc)) from None

    def owned(session_id: str, who: Caller = Depends(caller)) -> Session:
        """The caller's session, or 404.

        Someone else's session is a 404 and not a 403. A 403 would confirm that
        the id exists, which is already more than a stranger should learn.
        """
        session = runtime.sessions.get(session_id)
        if session is None or not who.owns(session):
            raise HTTPException(status_code=404, detail=f"no session {session_id}")
        return session

    def owned_approval(approval_id: str, who: Caller = Depends(caller)) -> PendingApproval:
        """The caller's pending approval, or 410, the answer for one already
        gone: someone else's must be indistinguishable from that."""
        pending = runtime.approvals.get(approval_id)
        session = runtime.sessions.get(pending.session_id) if pending else None
        if pending is None or session is None or not who.owns(session):
            raise HTTPException(status_code=410, detail="that approval is no longer pending")
        return pending

    # -- readiness ----------------------------------------------------------

    @app.get("/v1/health")
    async def health(request: Request) -> dict[str, Any]:
        """No token required — for the liveness half.

        A health check that needs a credential cannot tell the extension whether
        the credential path is the thing that is broken, and this is the endpoint
        it polls for up to sixty seconds while deciding whether the runtime came
        up at all. So "is it alive, and what does it speak" stays open.

        Everything that describes *this developer's machine* — which directory is
        open, which gateway it talks to, how many sessions are running — needs
        the token. Any process on the box could read all of it, and "which
        repository is this person working on" is not a liveness fact (BUG L-30).
        """
        payload: dict[str, Any] = {
            "ok": True,
            "api_version": API_VERSION,
            # Unauthenticated, like api_version. It describes the code, not the
            # machine, and a client needs it before it has a token.
            "contract_hash": app.state.contract["hash"],
            "version": runtime.version,
        }
        try:
            who = authenticate(request.headers)
        except Unauthorised:
            return payload
        mine = runtime.sessions.list(owner=who.sub)
        if who.local:
            # Where the runtime's files are and which gateway it uses are facts
            # about the developer's own machine, for the developer. A hosted
            # caller is shown neither: they are paths on a shared server (§6).
            payload.update({"workspace": str(runtime.workspace), "gateway": runtime.gateway_url})
        payload.update(
            {
                "ready": runtime.ready,
                "sessions": {
                    "total": len(mine),
                    "running": sum(1 for s in mine if s.running),
                },
            }
        )
        if runtime.toolchain:
            payload["toolchain"] = runtime.toolchain
        return payload

    @app.get("/v1/tools", dependencies=[Depends(caller)])
    async def tools() -> dict[str, Any]:
        return runtime.tool_catalog

    # -- the developer's credential -----------------------------------------

    @app.post("/v1/credential", dependencies=[Depends(caller)])
    async def credential(body: dict[str, Any]) -> dict[str, Any]:
        """Replace the JWT the runtime authenticates to the gateway with.

        The daemon outlives the token it was spawned with. It used to be baked
        into the HTTP client's default headers at construction, so from the
        moment it expired every model call was a 401 — non-retryable, so every
        task ended ERROR — and restarting the runtime was the only cure. The
        extension is the only party that can mint a fresh one, so it pushes it
        here; the client asks for the current value on every request.

        Nothing is echoed back but a fingerprint. A token in a response body is
        a token in a log.
        """
        jwt = str(body.get("jwt", "")).strip()
        if not jwt:
            raise HTTPException(status_code=400, detail="jwt is required")
        runtime.set_credential(jwt)
        return {"ok": True, "fingerprint": hashlib.sha256(jwt.encode()).hexdigest()[:12]}

    # -- tasks --------------------------------------------------------------

    @app.post("/v1/tasks")
    async def start_task(body: dict[str, Any], who: Caller = Depends(caller)) -> dict[str, Any]:
        task = str(body.get("task", "")).strip()
        if not task:
            raise HTTPException(status_code=400, detail="task is required")

        # `intent`, with `mode` still accepted for a client that has not been
        # rebuilt. They are the same field on the wire and always were: the old
        # `mode` default was "planner", which is also the backend default, so
        # the server could not tell "let the agent choose" from "the developer
        # asked for the Planner" -- and the answer it picked, for every message,
        # was the phase that plans. `Intent.coerce` maps every retired name onto
        # what it actually asked for.
        policy = str(body.get("approval_policy") or INTERACTIVE)
        if policy not in POLICIES:
            raise HTTPException(
                status_code=400, detail=f"approval_policy must be one of {', '.join(POLICIES)}"
            )
        session = runtime.start(
            task,
            intent=Intent.coerce(body.get("intent") or body.get("mode")),
            acceptance=tuple(body.get("acceptance") or ()),
            owner=who.sub,
            approval_policy=policy,
        )
        return session.as_dict()

    # -- the event stream ---------------------------------------------------

    @app.get("/v1/sessions/{session_id}/events")
    async def events(
        request: Request,
        since_id: int = Query(default=0),
        last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
        session: Session = Depends(owned),
    ) -> StreamingResponse:
        """Live events, resumable.

        Part B §14's gap: a dropped connection today loses the live view of a run
        that is still executing server-side, and the developer cannot tell that
        from the run having died. Replaying from ``since_id`` closes it.

        ``Last-Event-ID`` is honoured because that is what a browser's
        ``EventSource`` sends automatically on reconnect — a client that does
        nothing special still resumes correctly.
        """
        resume_from = since_id
        if last_event_id and last_event_id.isdigit():
            resume_from = max(resume_from, int(last_event_id))

        return StreamingResponse(
            _stream(session, resume_from, request),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    # -- the sessions tree --------------------------------------------------

    @app.get("/v1/sessions")
    async def list_sessions(
        status: str | None = None, who: Caller = Depends(caller)
    ) -> dict[str, Any]:
        return {
            "sessions": [
                s.as_dict() for s in runtime.sessions.list(status=status, owner=who.sub)
            ]
        }

    @app.get("/v1/sessions/{session_id}")
    async def get_session(
        transcript: bool = False, session: Session = Depends(owned)
    ) -> dict[str, Any]:
        payload = session.as_dict(transcript=transcript)
        payload["pending_approvals"] = [
            p.as_dict() for p in runtime.pending_for(session.id)
        ]
        return payload

    @app.delete("/v1/sessions/{session_id}")
    async def delete_session(session: Session = Depends(owned)) -> dict[str, Any]:
        if session.running:
            raise HTTPException(status_code=409, detail="abort the session before deleting it")
        runtime.sessions.delete(session.id)
        return {"deleted": session.id}

    @app.post("/v1/sessions/{session_id}/abort")
    async def abort(session: Session = Depends(owned)) -> dict[str, Any]:
        session.abort()
        runtime._release(session.id)
        return {"aborting": session.id, "status": str(session.status)}

    # -- revert -------------------------------------------------------------

    @app.get("/v1/sessions/{session_id}/revert")
    async def revert_plan(session: Session = Depends(owned)) -> dict[str, Any]:
        """What a revert would do. §12 asks for the confirmation to list the
        exact paths, because "revert my last task" is easy to fire by accident."""
        return runtime.sessions.plan_revert(session).as_dict()

    @app.post("/v1/sessions/{session_id}/revert")
    async def revert(session: Session = Depends(owned)) -> dict[str, Any]:
        if session.running:
            raise HTTPException(
                status_code=409, detail="a running session cannot be reverted; abort it first"
            )
        return runtime.sessions.revert(session).as_dict()

    # -- approvals ----------------------------------------------------------

    @app.get("/v1/approvals")
    async def list_approvals(who: Caller = Depends(caller)) -> dict[str, Any]:
        mine = {s.id for s in runtime.sessions.list(owner=who.sub)}
        return {
            "approvals": [
                p.as_dict() for p in runtime.approvals.values() if p.session_id in mine
            ]
        }

    @app.post("/v1/approvals/{approval_id}")
    async def decide(
        body: dict[str, Any], pending: PendingApproval = Depends(owned_approval)
    ) -> dict[str, Any]:
        """Accept, reject, or edit.

        ``edit`` replaces the arguments and approves in one step. §9 calls it the
        standout of ``postgen``'s approval card, and the reason is arithmetic:
        correcting a path costs nothing, while rejecting costs a turn and the
        model often makes the same mistake again.

        A missing approval is a 410 (from ``owned_approval``): gone means
        answered, timed out, or the run ended, and all three are "too late"
        rather than an error the client should retry.
        """
        decision = str(body.get("decision", "reject")).lower()
        if decision not in ("accept", "reject", "edit"):
            raise HTTPException(status_code=400, detail="decision must be accept, reject or edit")

        if decision == "edit":
            arguments = body.get("arguments")
            if not isinstance(arguments, dict):
                raise HTTPException(status_code=400, detail="edit needs an arguments object")
            pending.arguments = arguments

        if pending.timed_out or pending.deadline_in() <= 0:
            # The run has already recorded this as a rejection and moved on.
            # Reporting the developer's "accept" back to them would be a receipt
            # for something that did not happen (BUG L-22).
            raise HTTPException(
                status_code=410,
                detail="that approval timed out and was recorded as a rejection",
            )

        pending.approved = decision in ("accept", "edit")
        pending.decided.set()
        return {"id": pending.id, "decision": decision}

    @app.post("/v1/sessions/{session_id}/resume")
    async def resume_session(
        body: dict[str, Any] | None = None, session: Session = Depends(owned)
    ) -> dict[str, Any]:
        """Run a finished session again, on its own transcript.

        Only for the statuses ``Status.resumable`` names. A finished run takes a
        *follow-up* instead: resuming a successful change would re-enter the gate
        loop on something that already passed.
        """
        if session.running:
            raise HTTPException(status_code=409, detail="that session is still running")
        if not session.status.resumable:
            raise HTTPException(
                status_code=409,
                detail=f"a {session.status} session is not resumable; start a follow-up task",
            )
        note = str((body or {}).get("note", "")).strip()
        return runtime.resume(session, note=note).as_dict()

    @app.post("/v1/sessions/{session_id}/messages")
    async def message_session(
        body: dict[str, Any], session: Session = Depends(owned)
    ) -> dict[str, Any]:
        """Send the session another message, whatever state it is in.

        One endpoint rather than two, and the state decides what the message
        means:

        * **running** — it queues as a correction the run reads before its next
          turn. Without that, the only way to disagree with a run in progress is
          Stop, which ends it and discards every turn of context it had built,
          and a correction that arrives after the run is not a correction.
        * **finished** — it is the next message in the conversation, and the run
          starts again on the same context.

        The caller cannot make that decision without a race: a run can end
        between reading the status and posting the message, and a client that
        guessed wrong would get a 409 for a message the developer had already
        typed. Here the branch is taken under the same view of the session that
        acts on it.
        """
        text = str(body.get("text", "")).strip()
        if not text:
            raise HTTPException(status_code=400, detail="text is required")

        # `steer` answers from inside the session's lock, so "the run is taking
        # corrections" and "this correction is queued" are one observation
        # rather than two with a race between them. A run that has just ended
        # refuses, and the message becomes the next one in the conversation
        # instead of disappearing (BUG L-9).
        if not (session.running and session.steer(text)):
            # No default here. This read `body.get("mode", "planner")`, which
            # made every follow-up an explicit request for the Planner and left
            # `follow_up`'s own default unreachable. Absent means "decide from
            # the conversation", which is what the classifier is for; a client
            # with an Ask/Agent toggle says which and is obeyed.
            requested = body.get("intent") or body.get("mode")
            try:
                runtime.follow_up(
                    session, text, intent=Intent.coerce(requested) if requested else None
                )
            except RuntimeError:
                # The worker closed the correction queue and something restarted
                # the session before this request got here. Honest and
                # retryable, rather than a 500 the client cannot interpret.
                raise HTTPException(
                    status_code=409, detail="that session is still running; send it again"
                ) from None
        return session.as_dict()

    @app.post("/v1/sessions/{session_id}/wind-down")
    async def wind_down(session: Session = Depends(owned)) -> dict[str, Any]:
        """Stop after the current turn, rather than mid-flight.

        Distinct from abort on purpose: a turn can be several minutes long and
        can be halfway through writing a file, and "let it finish and then stop"
        is a different request from "stop now".
        """
        session.wind_down()
        return {"id": session.id, "winding_down": True}

    @app.get("/v1/sessions/{session_id}/context")
    async def context_inspector(session: Session = Depends(owned)) -> dict[str, Any]:
        """What the server currently holds, for the context inspector.

        Reported rather than reconstructed. Contract C5 makes the server
        authoritative on context, and the client has no way to compute this
        anyway: it never sees the message list, the per-mode budgets, or the
        token estimator.
        """
        context = runtime.contexts.get(session.id)
        if context is None:
            raise HTTPException(
                status_code=404, detail="no context is held for that session any more"
            )
        return context.inspect()

    @app.get("/v1/sessions/{session_id}/transcript")
    async def session_transcript(
        view: str = Query(default="model"),
        limit: int = Query(default=200),
        session: Session = Depends(owned),
    ) -> dict[str, Any]:
        """What happened, or what the model saw. They are different, and both exist.

        ``view=canonical`` is the record: every message in the order it was
        appended, tool results **whole**, nothing elided, nothing hidden by a
        compaction. ``view=model`` is the projection -- the bytes that actually
        went on the wire on the most recent turn, with the caps applied, the
        superseded reads stubbed and the recap standing in for the turns it
        replaced.

        Before the split there was no way to ask either question after the first
        compaction, because the answer to both had been overwritten by the same
        list. Being able to put them side by side is most of what makes a run
        that went wrong diagnosable.

        Reads from the live context when the daemon holds one and from disk when
        it does not, so it answers for a session this process has never run.
        """
        session_id = session.id
        limit = max(1, min(2_000, limit))

        context = runtime.contexts.get(session_id)
        if context is not None:
            records = context.transcript.records
            sidecar = context.compaction
        else:
            journal = Journal(runtime.workspace, session_id)
            raw = journal.read_records()
            if not raw:
                raise HTTPException(
                    status_code=404, detail="that session has no canonical transcript"
                )
            records = Transcript.from_records(raw).records
            sidecar = CompactionState.from_dict(journal.read_compaction() or {})

        if view == "canonical":
            rows = [
                {
                    "seq": r.seq,
                    "role": str(r.role),
                    "turn": r.turn,
                    "tool": r.tool,
                    "path": r.path,
                    "visibility": str(r.visibility),
                    "characters": len(r.content),
                    "content": r.content,
                }
                for r in records[-limit:]
            ]
        elif context is not None:
            rows = [
                {
                    "seq": m.seq,
                    "role": str(m.role),
                    "layer": str(m.layer),
                    "turn": m.turn,
                    "path": m.path,
                    "line_range": list(m.line_range) if m.line_range else None,
                    "characters": len(m.content),
                    "content": m.content,
                }
                for m in context.view().messages[-limit:]
            ]
        else:
            raise HTTPException(
                status_code=409,
                detail="no context is held for that session; ask for view=canonical",
            )

        return {
            "session_id": session_id,
            "view": view,
            "records": len(records),
            "returned": len(rows),
            "compaction": sidecar.as_dict() if sidecar else None,
            "messages": rows,
        }

    @app.post("/v1/sessions/{session_id}/compact")
    async def compact_now(
        strategy: str = Query(default="basic"),
        retain: float = Query(default=0.35),
        session: Session = Depends(owned),
    ) -> dict[str, Any]:
        """Compact this session's context on demand.

        The extension has had a ``dakcoder.compactContext`` command with nothing
        behind it: there was no route, so the command could not do what its name
        says. There is one now, and the default is the deterministic strategy --
        a developer asking for a compaction did not ask to be billed for a
        summariser call, and the tier exists precisely so that they need not be.

        Refused while a run is in flight. Compaction changes the sidecar, and a
        turn assembling its request against the old one would be reading a
        context that moved underneath it. There is no reason to allow it: the
        run compacts itself at the threshold.
        """
        session_id = session.id
        if session.status is Status.RUNNING:
            raise HTTPException(
                status_code=409,
                detail="the run is in flight; it compacts itself when it needs to",
            )
        context = runtime.contexts.get(session_id)
        if context is None:
            raise HTTPException(
                status_code=404, detail="no context is held for that session any more"
            )
        loop = runtime.loops.get(session_id)
        summariser = loop._summarise if loop is not None else (lambda _m: Recap())
        before = context.usage().total
        recap = context.compact(
            summariser,
            retain_pct=max(0.05, min(0.9, retain)),
            strategy="agentic" if strategy == "agentic" else "basic",
        )
        context.persist()
        return {
            "session_id": session_id,
            "strategy": strategy,
            "before": before,
            "after": context.usage().total,
            "evicted_messages": context.last_eviction.messages,
            "evicted_paths": list(context.last_eviction.paths),
            "goal": recap.goal,
        }

    @app.get("/v1/sessions/{session_id}/plan")
    async def session_plan(session: Session = Depends(owned)) -> dict[str, Any]:
        """This session's plan, its step statuses and how it got here.

        From disk rather than from the live loop, so it answers after a restart
        and for a session this daemon has never run -- which is most of them,
        and all the interesting ones.
        """
        record = PlanRecord.load(runtime.workspace, session.id)
        if record is None:
            raise HTTPException(status_code=404, detail="that session has no plan")
        return record.as_dict()

    # The agenda belongs to the workspace, not to a session or a caller, so its
    # routes need a caller and nothing more. In a hosted deployment, who may
    # read a workspace's agenda is who holds its lease (host-plan §9.2).

    @app.get("/v1/agenda", dependencies=[Depends(caller)])
    async def list_agenda(state: str = Query(default="open")) -> dict[str, Any]:
        """The repository's backlog: work proposed but not yet done.

        ``state=open`` is the default because that is the question anyone
        actually has. The others are there so a UI can show what was dropped
        without a second endpoint.
        """
        store = AgendaStore(runtime.workspace)
        tasks = store.open_tasks() if state == "open" else [
            t for t in store.load() if state == "all" or t.state == state
        ]
        return {"tasks": [t.as_dict() for t in tasks], "state": state}

    @app.post("/v1/agenda", dependencies=[Depends(caller)])
    async def add_agenda_task(request: Request) -> dict[str, Any]:
        """Propose work for later.

        Deliberately open to the developer as well as to the agent. The agent is
        the one most likely to notice a fourth N+1 while fixing three; the
        developer is the one who will be reading this list on Monday.
        """
        body = await request.json()
        if not isinstance(body, dict) or not str(body.get("title") or "").strip():
            raise HTTPException(status_code=400, detail="a task needs a title")
        task = AgendaTask.propose(
            str(body["title"]),
            why=str(body.get("why") or ""),
            paths=[str(p) for p in body.get("paths") or () if p],
            priority=int(body.get("priority") or 3),
            origin_session=str(body.get("session_id") or ""),
        )
        added = AgendaStore(runtime.workspace).add(task)
        if added is None:
            raise HTTPException(
                status_code=409,
                detail="that work is already on the agenda, or the agenda could not be written",
            )
        return added.as_dict()

    @app.post("/v1/agenda/{task_id}", dependencies=[Depends(caller)])
    async def move_agenda_task(task_id: str, request: Request) -> dict[str, Any]:
        """Approve, drop or complete a proposal.

        The state is moved by a person, which is the whole point of the agenda
        existing separately from the plan: a plan step's status is derived from
        the change set and this one is a decision.
        """
        body = await request.json()
        state = str((body or {}).get("state") or "")
        if state not in AGENDA_STATES:
            raise HTTPException(
                status_code=400, detail=f"state must be one of {', '.join(AGENDA_STATES)}"
            )
        moved = AgendaStore(runtime.workspace).move(
            task_id,
            state,
            by=str((body or {}).get("by") or "developer"),
            note=str((body or {}).get("note") or ""),
        )
        if moved is None:
            raise HTTPException(status_code=404, detail="no such task, or it could not be written")
        return moved.as_dict()

    @app.post("/v1/approvals/{approval_id}/extend")
    async def extend_approval(
        pending: PendingApproval = Depends(owned_approval),
    ) -> dict[str, Any]:
        """Give the reviewer more time.

        The runtime releases an unanswered approval and records it as a
        rejection. With no way to extend that, a slow review silently becomes a
        refusal — a WCAG 2.2.1 failure, and the people most likely to exceed ten
        minutes are the ones reviewing the seven-file changesets that matter
        most.
        """
        pending.extensions += 1
        return {
            "id": pending.id,
            "extensions": pending.extensions,
            "seconds_left": round(pending.deadline_in(), 1),
        }

    @app.exception_handler(HTTPException)
    async def _http_error(_request: Request, exc: HTTPException) -> JSONResponse:
        return JSONResponse(status_code=exc.status_code, content={"error": exc.detail})

    # After every route above is registered, so the table in the contract is
    # read from this app and cannot disagree with it.
    app.state.contract = contract.document(route_table(app))

    def documented() -> dict[str, Any]:
        # The runtime's own /openapi.json serves the published document, not
        # FastAPI's untyped one. Built on first request and kept.
        if app.openapi_schema is None:
            app.openapi_schema = openapi.build(app)
        return app.openapi_schema

    app.openapi = documented  # type: ignore[method-assign]
    return app


def route_table(app: FastAPI) -> list[str]:
    """Every route the app serves, as ``"METHOD /path"``.

    FastAPI's own ``/docs`` and ``/openapi.json`` are not ``APIRoute``s, so they
    are left out. They are not part of the contract.
    """
    return sorted(
        f"{method} {route.path}"
        for route in app.routes
        if isinstance(route, APIRoute)
        for method in route.methods
    )


def published_contract() -> str:
    """The contract as ``make contract`` writes it to ``api/contract.json``.

    Built from a real app on an empty workspace, so the published route table
    comes from the same ``create_app`` the runtime serves.
    """
    with tempfile.TemporaryDirectory() as tmp:
        app = create_app(Loopback(Path(tmp), lambda _session, _approve: None))
        return contract.as_json(route_table(app))


def published_openapi() -> str:
    """The REST reference as ``make contract`` writes it to ``api/openapi.json``."""
    with tempfile.TemporaryDirectory() as tmp:
        app = create_app(Loopback(Path(tmp), lambda _session, _approve: None))
        return json.dumps(openapi.build(app), indent=2, ensure_ascii=False) + "\n"


async def _stream(session: Session, since_id: int, request: Request) -> AsyncIterator[bytes]:
    """Replay, then follow.

    The backlog is drained before subscribing, and the subscription is taken
    *first* so nothing recorded between the two is lost. Doing it the other way
    round leaves a gap exactly the width of the replay — which is longest for the
    clients that most need resumption.
    """
    queue = session.subscribe()
    try:
        for event in session.since(since_id):
            yield event.sse().encode("utf-8")

        seen = session.events[-1].id if session.events else since_id

        if not session.running and queue.empty():
            # Already finished and fully replayed. Close rather than hold the
            # connection open forever: a client watching a finished session is
            # reading history, and an SSE stream that never ends looks to the
            # extension exactly like a run still in progress.
            return

        while True:
            if await request.is_disconnected():
                return
            try:
                event = await asyncio.wait_for(queue.get(), timeout=15.0)
            except TimeoutError:
                # A comment frame. Proxies and load balancers close idle
                # connections, and a run can legitimately think for a minute.
                yield b": keep-alive\n\n"
                if not session.running:
                    return
                continue

            if _is_transient(event):
                # Relayed without touching the cursor. A transient event is not
                # stored, so it is not given an id of its own: it carries the id
                # the *next* stored event will get. Advancing `seen` past it
                # therefore swallows that event — the first streamed turn cost
                # the `usage` frame exactly this way, and the meter simply
                # stopped moving.
                yield event.sse().encode("utf-8")
                continue
            if event.id <= seen:
                continue
            seen = event.id
            yield event.sse().encode("utf-8")

            if event.type is EventType.END:
                return
    finally:
        session.unsubscribe(queue)


def _is_transient(event) -> bool:
    return event.type in (EventType.ASSISTANT_DELTA, EventType.HEARTBEAT)
