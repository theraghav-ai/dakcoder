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
    GET  /v1/health                     version, toolchain, readiness
    GET  /v1/tools                      contract C1

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
import secrets
import threading
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from fastapi import FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import JSONResponse, StreamingResponse

from dakcoder_shared.envelope import Event, EventType

from .loop import AgentLoop, Outcome, RunResult
from .modes import Intent
from .session import Session, SessionStore, Status
from .tools.router import ApprovalRequest

__all__ = ["API_VERSION", "Loopback", "PendingApproval", "create_app"]

#: The contract version the extension pins against. Bumped when a response shape
#: changes in a way a client could not have anticipated — never for an additive
#: field, because C2's rule is that unknown types and fields are ignored.
#:
#: **1.1** — the mode vocabulary changed. Five modes (`planner`, `scaffolder`,
#: `coder`, `verifier`, `debugger`) became three (`ask`, `planner`, `agent`), so
#: a 1.0 client's `Mode` union does not contain the values it will now be sent.
#: It degrades rather than crashes — an unknown mode is displayed raw — but the
#: guard exists precisely so that half-working is not the outcome nobody
#: suspects.
#:
#: Additive in the same release, and *not* on their own a reason to bump:
#: `POST /v1/tasks` accepts `intent` (with `mode` still read as a synonym),
#: `POST /v1/credential` is new, `turn_start` carries `intent`, and the tool
#: catalog gained `finish`, `submit_plan` and `ask_developer`.
API_VERSION = "1.1"

#: How long a run waits for an approval before giving up. Long enough for someone
#: to read a seven-file scaffold; short enough that a developer who closed the
#: window does not leave a thread parked until the process ends.
APPROVAL_TIMEOUT = 600.0


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

    def deadline_in(self) -> float:
        """Seconds left to decide, counting any extensions granted."""
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
        self.ready: dict[str, Any] = {"prewarmed": False}
        #: The developer's gateway JWT, as the extension last refreshed it.
        #: Read per request by the LLM client rather than captured at spawn —
        #: see ``POST /v1/credential``. Empty means "whatever the process
        #: started with", which is what ``serve`` falls back to.
        self._credential: str = ""

    def set_credential(self, jwt: str) -> None:
        self._credential = jwt.strip()

    def credential(self) -> str:
        return self._credential

    # -- running a task -----------------------------------------------------

    def start(self, task: str, *, intent: Intent = Intent.AUTO, acceptance=()) -> Session:
        session = self.sessions.create(task)
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
            return self._await_decision(session, request)

        def register(request: ApprovalRequest) -> None:
            """Put the approval in the table before the event announcing it goes out."""
            self.approvals[request.id] = PendingApproval(request.id, session.id, request)

        agent = self.build_loop(session, approve)
        # Set here rather than asked of ``build_loop``, so a factory that knows
        # nothing about sessions stays a factory. Without it every ledger row
        # this run produces is attributed to no session at all.
        agent.session_id = session.id
        if continued:
            # The conversation *is* the context manager. ``build_loop`` hands
            # back a fresh one because most runs want one; a follow-up wants the
            # one that already holds the exchange, and swapping it here keeps
            # ``build_loop`` a factory rather than something that has to know
            # about session lifecycles.
            prior = self.contexts.get(session.id)
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
                session.status = Status.ERROR
                session.summary = summary
                session.finished_at = datetime.now(tz=timezone.utc)
            else:
                if agent.result is not None:
                    session.finish(agent.result)
            finally:
                # Every approval this run was waiting on is released, whichever
                # way the run ended. A crashed run holding a pending approval
                # leaves the extension showing a card nothing will ever answer.
                self._release(session.id)

        threading.Thread(target=run, name=f"dakcoder-{session.id}", daemon=True).start()

    def resume(self, session: Session, *, note: str = "") -> Session:
        """Run the session again, on the same transcript.

        A *resume*, not a new task: the id, the event log and the mutation list
        are the ones the developer was already looking at, so the second attempt
        appears where the first one ended rather than in a new row that shares
        nothing with it. The task is re-seeded with what the run learned, which
        is the difference between resuming and simply retrying.
        """
        if session.running:
            raise RuntimeError("that session is still running")

        parts = [session.task]
        if session.summary:
            parts.append(f"The previous attempt ended: {session.summary}")
        if note:
            parts.append(note)
        task = "\n\n".join(parts)

        session.status = Status.RUNNING
        session.finished_at = None
        session.cancel = threading.Event()
        session.winding_down = threading.Event()
        # A resume is another go at work that did not land, so it is a change
        # request by construction -- there is nothing for the classifier to
        # decide.
        self._spawn(session, task, Intent.AGENT, ())
        return session

    def _await_decision(self, session: Session, request: ApprovalRequest) -> bool:
        """Block the loop thread until the developer decides, or time runs out."""
        # Registered by ``on_pending`` before the event was emitted. Falling
        # back to creating one keeps a caller that drives the loop directly
        # (the tests, the CLI) working.
        pending = self.approvals.get(request.id)
        if pending is None:
            pending = PendingApproval(request.id, session.id, request)
            self.approvals[pending.id] = pending

        if not pending.decided.wait(timeout=pending.deadline_in()):
            # A timeout is a refusal, not an approval. Nobody looked, so nobody
            # agreed — and the failure mode of the opposite choice is a write
            # that happened while the developer was at lunch.
            self.approvals.pop(pending.id, None)
            return False

        self.approvals.pop(pending.id, None)
        if pending.approved and pending.arguments is not None:
            request.arguments.clear()
            request.arguments.update(pending.arguments)
        return pending.approved

    def _release(self, session_id: str) -> None:
        for pending in [p for p in self.approvals.values() if p.session_id == session_id]:
            pending.approved = False
            pending.decided.set()

    def pending_for(self, session_id: str) -> list[PendingApproval]:
        return [p for p in self.approvals.values() if p.session_id == session_id]


def create_app(runtime: Loopback) -> FastAPI:
    app = FastAPI(title="dakcoderd", version=runtime.version)
    app.state.runtime = runtime

    def _missing() -> None:
        raise HTTPException(status_code=404, detail="no such session")

    def authorise(authorization: str | None) -> None:
        """The loopback token.

        Bound to 127.0.0.1, so this is not defending against the network — it is
        defending against *other processes on the same machine*, which on a
        developer laptop includes every npm postinstall script and browser
        extension that can reach localhost. `secrets.compare_digest` because a
        timing side channel on a local socket is entirely practical.
        """
        expected = f"Bearer {runtime.token}"
        if not authorization or not secrets.compare_digest(authorization, expected):
            raise HTTPException(status_code=401, detail="invalid loopback token")

    def session_or_404(session_id: str) -> Session:
        session = runtime.sessions.get(session_id)
        if session is None:
            raise HTTPException(status_code=404, detail=f"no session {session_id}")
        return session

    # -- readiness ----------------------------------------------------------

    @app.get("/v1/health")
    async def health() -> dict[str, Any]:
        """No token required.

        A health check that needs a credential cannot tell the extension whether
        the credential path is the thing that is broken — and this is the
        endpoint it polls for up to sixty seconds while deciding whether the
        runtime came up at all.
        """
        return {
            "ok": True,
            "api_version": API_VERSION,
            "version": runtime.version,
            "workspace": str(runtime.workspace),
            "gateway": runtime.gateway_url,
            "ready": runtime.ready,
            "sessions": {
                "total": len(runtime.sessions.list()),
                "running": sum(1 for s in runtime.sessions.list() if s.running),
            },
        }

    @app.get("/v1/tools")
    async def tools(authorization: str | None = Header(default=None)) -> dict[str, Any]:
        authorise(authorization)
        return runtime.tool_catalog

    # -- the developer's credential -----------------------------------------

    @app.post("/v1/credential")
    async def credential(
        body: dict[str, Any], authorization: str | None = Header(default=None)
    ) -> dict[str, Any]:
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
        authorise(authorization)
        jwt = str(body.get("jwt", "")).strip()
        if not jwt:
            raise HTTPException(status_code=400, detail="jwt is required")
        runtime.set_credential(jwt)
        return {"ok": True, "fingerprint": hashlib.sha256(jwt.encode()).hexdigest()[:12]}

    # -- tasks --------------------------------------------------------------

    @app.post("/v1/tasks")
    async def start_task(
        body: dict[str, Any], authorization: str | None = Header(default=None)
    ) -> dict[str, Any]:
        authorise(authorization)
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
        session = runtime.start(
            task,
            intent=Intent.coerce(body.get("intent") or body.get("mode")),
            acceptance=tuple(body.get("acceptance") or ()),
        )
        return session.as_dict()

    # -- the event stream ---------------------------------------------------

    @app.get("/v1/sessions/{session_id}/events")
    async def events(
        session_id: str,
        request: Request,
        since_id: int = Query(default=0),
        last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
        authorization: str | None = Header(default=None),
    ) -> StreamingResponse:
        """Live events, resumable.

        Part B §14's gap: a dropped connection today loses the live view of a run
        that is still executing server-side, and the developer cannot tell that
        from the run having died. Replaying from ``since_id`` closes it.

        ``Last-Event-ID`` is honoured because that is what a browser's
        ``EventSource`` sends automatically on reconnect — a client that does
        nothing special still resumes correctly.
        """
        authorise(authorization)
        session = session_or_404(session_id)

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
        status: str | None = None, authorization: str | None = Header(default=None)
    ) -> dict[str, Any]:
        authorise(authorization)
        return {"sessions": [s.as_dict() for s in runtime.sessions.list(status=status)]}

    @app.get("/v1/sessions/{session_id}")
    async def get_session(
        session_id: str,
        transcript: bool = False,
        authorization: str | None = Header(default=None),
    ) -> dict[str, Any]:
        authorise(authorization)
        session = session_or_404(session_id)
        payload = session.as_dict(transcript=transcript)
        payload["pending_approvals"] = [
            p.as_dict() for p in runtime.pending_for(session_id)
        ]
        return payload

    @app.delete("/v1/sessions/{session_id}")
    async def delete_session(
        session_id: str, authorization: str | None = Header(default=None)
    ) -> dict[str, Any]:
        authorise(authorization)
        session = session_or_404(session_id)
        if session.running:
            raise HTTPException(status_code=409, detail="abort the session before deleting it")
        runtime.sessions.delete(session_id)
        return {"deleted": session_id}

    @app.post("/v1/sessions/{session_id}/abort")
    async def abort(
        session_id: str, authorization: str | None = Header(default=None)
    ) -> dict[str, Any]:
        authorise(authorization)
        session = session_or_404(session_id)
        session.abort()
        runtime._release(session_id)
        return {"aborting": session_id, "status": str(session.status)}

    # -- revert -------------------------------------------------------------

    @app.get("/v1/sessions/{session_id}/revert")
    async def revert_plan(
        session_id: str, authorization: str | None = Header(default=None)
    ) -> dict[str, Any]:
        """What a revert would do. §12 asks for the confirmation to list the
        exact paths, because "revert my last task" is easy to fire by accident."""
        authorise(authorization)
        return runtime.sessions.plan_revert(session_or_404(session_id)).as_dict()

    @app.post("/v1/sessions/{session_id}/revert")
    async def revert(
        session_id: str, authorization: str | None = Header(default=None)
    ) -> dict[str, Any]:
        authorise(authorization)
        session = session_or_404(session_id)
        if session.running:
            raise HTTPException(
                status_code=409, detail="a running session cannot be reverted; abort it first"
            )
        return runtime.sessions.revert(session).as_dict()

    # -- approvals ----------------------------------------------------------

    @app.get("/v1/approvals")
    async def list_approvals(
        authorization: str | None = Header(default=None),
    ) -> dict[str, Any]:
        authorise(authorization)
        return {"approvals": [p.as_dict() for p in runtime.approvals.values()]}

    @app.post("/v1/approvals/{approval_id}")
    async def decide(
        approval_id: str,
        body: dict[str, Any],
        authorization: str | None = Header(default=None),
    ) -> dict[str, Any]:
        """Accept, reject, or edit.

        ``edit`` replaces the arguments and approves in one step. §9 calls it the
        standout of ``postgen``'s approval card, and the reason is arithmetic:
        correcting a path costs nothing, while rejecting costs a turn and the
        model often makes the same mistake again.
        """
        authorise(authorization)
        pending = runtime.approvals.get(approval_id)
        if pending is None:
            # Gone means answered, timed out, or the run ended. All three are
            # "too late" rather than an error the client should retry.
            raise HTTPException(status_code=410, detail="that approval is no longer pending")

        decision = str(body.get("decision", "reject")).lower()
        if decision not in ("accept", "reject", "edit"):
            raise HTTPException(status_code=400, detail="decision must be accept, reject or edit")

        if decision == "edit":
            arguments = body.get("arguments")
            if not isinstance(arguments, dict):
                raise HTTPException(status_code=400, detail="edit needs an arguments object")
            pending.arguments = arguments

        pending.approved = decision in ("accept", "edit")
        pending.decided.set()
        return {"id": approval_id, "decision": decision}

    @app.post("/v1/sessions/{session_id}/resume")
    async def resume_session(
        session_id: str,
        body: dict[str, Any] | None = None,
        authorization: str | None = Header(default=None),
    ) -> dict[str, Any]:
        """Run a finished session again, on its own transcript.

        Only for the statuses ``Status.resumable`` names. A finished run takes a
        *follow-up* instead: resuming a successful change would re-enter the gate
        loop on something that already passed.
        """
        authorise(authorization)
        session = runtime.sessions.get(session_id) or _missing()
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
        session_id: str,
        body: dict[str, Any],
        authorization: str | None = Header(default=None),
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
        authorise(authorization)
        session = runtime.sessions.get(session_id) or _missing()
        text = str(body.get("text", "")).strip()
        if not text:
            raise HTTPException(status_code=400, detail="text is required")

        if session.running:
            session.steer(text)
        else:
            # No default here. This read `body.get("mode", "planner")`, which
            # made every follow-up an explicit request for the Planner and left
            # `follow_up`'s own default unreachable. Absent means "decide from
            # the conversation", which is what the classifier is for; a client
            # with an Ask/Agent toggle says which and is obeyed.
            requested = body.get("intent") or body.get("mode")
            runtime.follow_up(
                session, text, intent=Intent.coerce(requested) if requested else None
            )
        return session.as_dict()

    @app.post("/v1/sessions/{session_id}/wind-down")
    async def wind_down(
        session_id: str, authorization: str | None = Header(default=None)
    ) -> dict[str, Any]:
        """Stop after the current turn, rather than mid-flight.

        Distinct from abort on purpose: a turn can be several minutes long and
        can be halfway through writing a file, and "let it finish and then stop"
        is a different request from "stop now".
        """
        authorise(authorization)
        session = runtime.sessions.get(session_id) or _missing()
        session.wind_down()
        return {"id": session.id, "winding_down": True}

    @app.get("/v1/sessions/{session_id}/context")
    async def context_inspector(
        session_id: str, authorization: str | None = Header(default=None)
    ) -> dict[str, Any]:
        """What the server currently holds, for the context inspector.

        Reported rather than reconstructed. Contract C5 makes the server
        authoritative on context, and the client has no way to compute this
        anyway: it never sees the message list, the per-mode budgets, or the
        token estimator.
        """
        authorise(authorization)
        runtime.sessions.get(session_id) or _missing()
        context = runtime.contexts.get(session_id)
        if context is None:
            raise HTTPException(
                status_code=404, detail="no context is held for that session any more"
            )
        return context.inspect()

    @app.post("/v1/approvals/{approval_id}/extend")
    async def extend_approval(
        approval_id: str, authorization: str | None = Header(default=None)
    ) -> dict[str, Any]:
        """Give the reviewer more time.

        The runtime releases an unanswered approval and records it as a
        rejection. With no way to extend that, a slow review silently becomes a
        refusal — a WCAG 2.2.1 failure, and the people most likely to exceed ten
        minutes are the ones reviewing the seven-file changesets that matter
        most.
        """
        authorise(authorization)
        pending = runtime.approvals.get(approval_id)
        if pending is None:
            raise HTTPException(status_code=410, detail="that approval is no longer pending")
        pending.extensions += 1
        return {
            "id": approval_id,
            "extensions": pending.extensions,
            "seconds_left": round(pending.deadline_in(), 1),
        }

    @app.exception_handler(HTTPException)
    async def _http_error(_request: Request, exc: HTTPException) -> JSONResponse:
        return JSONResponse(status_code=exc.status_code, content={"error": exc.detail})

    return app


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
