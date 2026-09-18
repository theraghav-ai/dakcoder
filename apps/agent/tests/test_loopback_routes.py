"""The routes added so the extension does not have to lie.

Each of these exists because a surface of the interface design had nothing behind
it. The tests assert the *contract the client was designed against*, not merely
that a handler returns 200 — a route that answers with the wrong shape is worse
than a missing one, because the client believes it.
"""

from __future__ import annotations

import asyncio

import httpx
import pytest

from dakcoder_agent.loopback import Loopback
from dakcoder_agent.session import Status

from test_loopback import TOKEN, client, scripted, settle, start  # noqa: F401
from wirecheck import CheckedTransport


# ── steering ────────────────────────────────────────────────────────────────


async def test_a_correction_can_be_queued_while_the_run_is_going(
    client: httpx.AsyncClient, scripted: Loopback
) -> None:
    """The gap this closes: before it, the only way to disagree with a run was
    Stop, which ends it and throws away every turn of context it had built."""
    session = await start(client)
    response = await client.post(
        f"/v1/sessions/{session['id']}/messages",
        json={"text": "use the repository, not raw SQL in the handler"},
    )
    # The scripted run is quick, so it may already have finished; either the
    # correction was queued, or the session closed first. Both are correct
    # answers — what must never happen is a 404 or a 500.
    assert response.status_code in (200, 409), response.text
    if response.status_code == 200:
        assert response.json()["queued"] >= 1


async def test_an_empty_correction_is_refused(
    client: httpx.AsyncClient, scripted: Loopback
) -> None:
    session = await start(client)
    response = await client.post(f"/v1/sessions/{session['id']}/messages", json={"text": "   "})
    assert response.status_code == 400


async def test_a_message_to_a_finished_session_continues_the_conversation(
    client: httpx.AsyncClient, scripted: Loopback
) -> None:
    """The second question must be answered by something that heard the first.

    Before this, a message to a finished session was a 409 and the extension's
    only recourse was to start a new one — which is why two messages in one
    conversation arrived as two sessions, neither of which knew about the other.
    """
    session = await start(client)
    await settle(session["id"], scripted)

    response = await client.post(
        f"/v1/sessions/{session['id']}/messages", json={"text": "and now the handler"}
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["id"] == session["id"], "a follow-up must not mint a new session"
    assert body["status"] == "running"
    assert body["turns"] == 2, "the opening message and the follow-up"


async def test_a_follow_up_keeps_the_context_the_first_message_built(
    client: httpx.AsyncClient, scripted: Loopback
) -> None:
    """Contract C5: the server is authoritative on context, and a conversation
    that resets it between messages has no memory at all."""
    session = await start(client)
    await settle(session["id"], scripted)
    before = scripted.contexts[session["id"]]

    await client.post(
        f"/v1/sessions/{session['id']}/messages", json={"text": "and now the handler"}
    )
    await settle(session["id"], scripted)

    assert scripted.contexts[session["id"]] is before, "the follow-up rebuilt the context"
    assert any(
        m.source == "user" and "and now the handler" in m.content for m in before.build()
    ), "the follow-up never reached the model"


async def test_a_follow_up_decides_its_own_mode_rather_than_inheriting_the_last_one(
    client: httpx.AsyncClient, scripted: Loopback
) -> None:
    """A follow-up is a new request, not a continuation of the last phase.

    Two bugs met here. `follow_up` defaulted to the Planner, so a session that
    had produced a plan and stopped answered "go" by planning it again; the fix
    for that returned *the mode the previous run ended in*, so a conversation
    that had finished in the Debugger answered its next message with the
    Debugger's overlay, budget and tool set whatever the message said. Neither
    is a decision about what was asked.

    Now the intent is decided per message -- stated by the client, or classified
    -- and the mode follows from that. Here the client states it.
    """
    from dakcoder_agent.modes import Mode

    session = await start(client)
    await settle(session["id"], scripted)
    context = scripted.contexts[session["id"]]
    # The previous run left the conversation in the acting mode.
    context.switch_mode(Mode.AGENT, "still acting")
    assert context.mode is Mode.AGENT

    await client.post(
        f"/v1/sessions/{session['id']}/messages",
        json={"text": "what does the handler do", "intent": "ask"},
    )
    await settle(session["id"], scripted)

    overlays = [m.source for m in context.build() if m.source.startswith("mode:")]
    assert overlays[-1] == "mode:ask", (
        f"the follow-up never left the previous run's mode: {overlays}"
    )


async def test_the_developers_own_messages_are_in_the_transcript(
    client: httpx.AsyncClient, scripted: Loopback
) -> None:
    """Otherwise re-opening a session shows the agent talking to itself."""
    session = await start(client)
    await settle(session["id"], scripted)
    await client.post(
        f"/v1/sessions/{session['id']}/messages", json={"text": "and now the handler"}
    )
    await settle(session["id"], scripted)

    full = (await client.get(f"/v1/sessions/{session['id']}?transcript=true")).json()
    said = [e["data"]["text"] for e in full["transcript"] if e["type"] == "user"]
    assert said == [session["task"], "and now the handler"]


async def test_the_loop_reads_a_queued_correction_at_the_top_of_a_turn() -> None:
    """The mechanism, tested where it lives rather than through HTTP timing."""
    from dakcoder_agent.session import Session

    session = Session(id="s1", task="t", workspace="w")
    session.steer("stop using raw SQL")
    session.steer("and wire it into FX")

    assert session.queued == 2
    assert session.drain_steer() == ["stop using raw SQL", "and wire it into FX"]
    assert session.queued == 0, "draining twice must not replay the same correction"


# ── wind-down ───────────────────────────────────────────────────────────────


async def test_wind_down_is_a_different_request_from_abort(
    client: httpx.AsyncClient, scripted: Loopback
) -> None:
    """A turn can be minutes long and halfway through a file. "Let it finish and
    then stop" is not the same ask as "stop now", and neither substitutes."""
    session = await start(client)
    response = await client.post(f"/v1/sessions/{session['id']}/wind-down")
    assert response.status_code == 200
    assert response.json()["winding_down"] is True

    stored = scripted.sessions.get(session["id"])
    assert stored.winding_down.is_set()
    assert not stored.cancel.is_set(), "wind-down must not abandon work in flight"


# ── resume ──────────────────────────────────────────────────────────────────


async def test_resume_refuses_a_running_session(
    client: httpx.AsyncClient, scripted: Loopback
) -> None:
    session = await start(client)
    response = await client.post(f"/v1/sessions/{session['id']}/resume")
    if response.status_code != 409:
        # It finished before we asked; that is a scheduling race, not a bug.
        await settle(session["id"], scripted)
        return
    assert "still running" in response.json()["error"]


async def test_resume_refuses_a_finished_session_and_names_the_alternative(
    client: httpx.AsyncClient, scripted: Loopback
) -> None:
    """`done` is deliberately not resumable: re-running a successful change would
    re-enter the gate loop on something that already passed. The refusal has to
    say what to do instead, or it is just a dead button."""
    session = await start(client)
    await settle(session["id"], scripted)
    stored = scripted.sessions.get(session["id"])
    stored.status = Status.DONE

    response = await client.post(f"/v1/sessions/{session['id']}/resume")
    assert response.status_code == 409
    assert "follow-up" in response.json()["error"]


async def test_resume_runs_again_on_the_same_transcript(
    client: httpx.AsyncClient, scripted: Loopback
) -> None:
    """A resume, not a new task: the id and the event log are the ones the
    developer was already looking at."""
    session = await start(client)
    await settle(session["id"], scripted)
    stored = scripted.sessions.get(session["id"])
    stored.status = Status.EXHAUSTED
    stored.summary = "stopped after 40 turns without a clean gate"
    events_before = len(stored.events)

    response = await client.post(
        f"/v1/sessions/{session['id']}/resume", json={"note": "try the repository layer first"}
    )
    assert response.status_code == 200, response.text
    assert response.json()["id"] == session["id"], "a resume must not mint a new session"

    await settle(session["id"], scripted)
    assert len(stored.events) > events_before, "the second attempt appends to the same log"


async def test_resume_of_an_unknown_session_is_a_404(client: httpx.AsyncClient) -> None:
    assert (await client.post("/v1/sessions/nope/resume")).status_code == 404


# ── the context inspector ───────────────────────────────────────────────────


async def test_the_context_route_reports_what_the_server_holds(
    client: httpx.AsyncClient, scripted: Loopback
) -> None:
    """Reported, not reconstructed. The client never sees the message list, the
    per-mode budgets or the token estimator, so it could not compute this even if
    contract C5 let it."""
    session = await start(client)
    await settle(session["id"], scripted)

    response = await client.get(f"/v1/sessions/{session['id']}/context")
    assert response.status_code == 200, response.text
    body = response.json()

    for key in ("mode", "turn", "total_tokens", "budget", "used_pct", "by_layer", "compactions"):
        assert key in body, f"the inspector was designed against {key}"
    assert body["budget"] > 0
    assert isinstance(body["by_layer"], dict)


async def test_the_context_route_404s_for_an_unknown_session(client: httpx.AsyncClient) -> None:
    assert (await client.get("/v1/sessions/nope/context")).status_code == 404


# ── approval extension ──────────────────────────────────────────────────────


async def test_an_approval_can_be_given_more_time(scripted: Loopback) -> None:
    """Otherwise a slow review silently becomes a rejection — WCAG 2.2.1, and the
    people most likely to exceed ten minutes are reviewing the changesets that
    matter most."""
    from dakcoder_agent.loopback import APPROVAL_TIMEOUT, PendingApproval
    from dakcoder_agent.tools.router import ApprovalRequest

    request = ApprovalRequest("patch_file", {"path": "configs/app.yaml"}, "touches a config")
    pending = PendingApproval(request.id, "s1", request)

    first = pending.deadline_in()
    assert first <= APPROVAL_TIMEOUT

    pending.extensions += 1
    assert pending.deadline_in() > first
    assert pending.deadline_in() <= APPROVAL_TIMEOUT * 2


async def test_extending_a_gone_approval_is_410_not_404(client: httpx.AsyncClient) -> None:
    """Gone means answered, timed out, or the run ended — all "too late" rather
    than an error the client should retry."""
    response = await client.post("/v1/approvals/deadbeef/extend")
    assert response.status_code == 410


async def test_the_approval_id_survives_the_round_trip(scripted: Loopback) -> None:
    """The defect this closes: the runtime used to mint its own id *after* the
    loop had already announced the approval, so the two never matched — and the
    event carried no id at all."""
    from dakcoder_agent.loopback import PendingApproval
    from dakcoder_agent.tools.router import ApprovalRequest

    request = ApprovalRequest("delete_file", {"path": "handler/old.go"}, "deletes a file")
    pending = PendingApproval(request.id, "s1", request)

    assert pending.id == request.id
    assert pending.as_dict()["id"] == request.as_dict()["id"]


# ── the routes the extension pins against ───────────────────────────────────


@pytest.mark.parametrize(
    "method,path",
    [
        ("POST", "/v1/sessions/{id}/messages"),
        ("POST", "/v1/sessions/{id}/wind-down"),
        ("POST", "/v1/sessions/{id}/resume"),
        ("GET", "/v1/sessions/{id}/context"),
    ],
)
async def test_every_new_route_requires_the_loopback_token(
    scripted: Loopback, method: str, path: str
) -> None:
    """An unauthenticated loopback port is reachable by every other process on
    the machine, which is the threat this token exists for."""
    from dakcoder_agent.loopback import create_app

    transport = CheckedTransport(create_app(scripted))
    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as http:
        response = await http.request(method, path.format(id="whatever"), json={})
    assert response.status_code == 401, f"{method} {path} answered without a token"


# ── streaming on the wire ───────────────────────────────────────────────────


def _stored(session_id: str, runtime: Loopback):
    return runtime.sessions.get(session_id)


async def test_a_transient_event_does_not_advance_the_relay_cursor() -> None:
    """The defect the first streamed turn produced, and it cost the `usage` frame.

    A transient event is never stored, so it is never given an id of its own: it
    carries the id the *next* stored event will get. The relay skipped anything
    whose id it had already seen, so relaying a delta marked that id as seen and
    the event it actually belonged to was dropped — silently, and only ever on a
    turn that streamed.
    """
    import asyncio

    from dakcoder_agent.session import Session
    from dakcoder_shared.envelope import Event, EventType

    from dakcoder_agent.loopback import _stream

    session = Session(id="s1", task="t", workspace="w")
    session.record(Event(EventType.TURN_START, {"turn": 1}))

    class NeverDisconnected:
        async def is_disconnected(self) -> bool:
            return False

    frames: list[str] = []
    stream = _stream(session, 0, NeverDisconnected())

    async def pump() -> None:
        async for chunk in stream:
            frames.append(chunk.decode("utf-8"))

    task = asyncio.create_task(pump())
    await asyncio.sleep(0)

    session.record(Event(EventType.ASSISTANT_DELTA, {"text": "half an "}))
    session.record(Event(EventType.USAGE, {"prompt_tokens": 900}))
    session.record(Event(EventType.END, {}))
    await asyncio.wait_for(task, timeout=2.0)

    body = "".join(frames)
    assert "assistant_delta" in body, "the delta never reached the wire"
    assert "usage" in body, "the delta swallowed the event whose id it borrowed"


def test_a_transient_frame_is_not_a_place_to_resume_from() -> None:
    """The SSE spec's rule, and the reason for it here.

    A frame with no ``id:`` leaves the client's last event id where it was. A
    delta must omit it: it carries the id of an event that has not been sent
    yet, so a client that remembered it would resume *past* something it never
    saw.
    """
    from datetime import datetime, timezone

    from dakcoder_agent.session import StoredEvent
    from dakcoder_shared.envelope import EventType

    at = datetime.now(tz=timezone.utc)
    delta = StoredEvent(id=7, type=EventType.ASSISTANT_DELTA, data={"text": "x"}, at=at)
    message = StoredEvent(id=7, type=EventType.ASSISTANT, data={"text": "x"}, at=at)

    assert "id:" not in delta.sse()
    assert "id: 7" in message.sse()


async def test_a_streamed_turn_leaves_the_transcript_alone(
    client: httpx.AsyncClient, scripted: Loopback
) -> None:
    """Deltas are relayed and never stored. A transcript built from them would be
    empty after a reconnect, which is why the `assistant` message is the one
    every client treats as authoritative."""
    session = await start(client)
    await settle(session["id"], scripted)

    full = (await client.get(f"/v1/sessions/{session['id']}?transcript=true")).json()
    kinds = [e["type"] for e in full["transcript"]]
    assert "assistant_delta" not in kinds
    assert "assistant" in kinds


async def test_a_follow_up_with_no_mode_carries_on_where_the_conversation_is(
    client: httpx.AsyncClient, scripted: Loopback
) -> None:
    """"go" must not mean "plan it again".

    ``follow_up`` took care to preserve the context and then threw away the
    other half of where the run had got to: the mode defaulted to Planner, in
    the signature *and* again in the route. So a session that had produced a
    plan and stopped answered "go" by entering the Planner and re-orienting.
    The field transcript shows that happening to "go", "do it" and "you are not
    writing anything" in turn, with the model politely re-reading the same files
    each time.

    Absent means carry on. A client that names a mode is still obeyed — the test
    above covers that.
    """
    from dakcoder_agent.modes import Mode

    session = await start(client)
    await settle(session["id"], scripted)
    context = scripted.contexts[session["id"]]
    context.switch_mode(Mode.AGENT, "coding")

    response = await client.post(
        f"/v1/sessions/{session['id']}/messages", json={"text": "go"}
    )
    assert response.status_code == 200, response.text
    await settle(session["id"], scripted)

    overlays = [m.content for m in context.build() if m.source.startswith("mode:")]
    assert overlays, "no mode overlay was ever appended"
    assert "Plan first" not in overlays[-1], (
        "a bare follow-up re-entered the Planner instead of carrying on"
    )


# ── the agenda: work proposed but not yet done ──────────────────────────────


async def test_the_agenda_starts_empty(client: httpx.AsyncClient, scripted: Loopback) -> None:
    response = await client.get("/v1/agenda")
    assert response.status_code == 200
    assert response.json() == {"tasks": [], "state": "open"}


async def test_work_can_be_proposed_and_read_back(
    client: httpx.AsyncClient, scripted: Loopback
) -> None:
    """The gap this closes: a run that notices a fourth N+1 while fixing three
    had two options, do it or say it in prose that scrolls away."""
    posted = await client.post(
        "/v1/agenda",
        json={
            "title": "audit the other handlers for the same N+1",
            "why": "handler/objection.go:412 does what handler/pension.go:88 just stopped doing",
            "paths": ["handler/objection.go"],
            "priority": 2,
        },
    )
    assert posted.status_code == 200, posted.text
    task = posted.json()
    assert task["state"] == "proposed"

    listed = await client.get("/v1/agenda")
    assert [t["id"] for t in listed.json()["tasks"]] == [task["id"]]


async def test_a_proposal_needs_a_title(client: httpx.AsyncClient, scripted: Loopback) -> None:
    response = await client.post("/v1/agenda", json={"why": "no title"})
    assert response.status_code == 400


async def test_the_same_work_is_refused_twice(
    client: httpx.AsyncClient, scripted: Loopback
) -> None:
    await client.post("/v1/agenda", json={"title": "split the god handler"})
    again = await client.post("/v1/agenda", json={"title": "Split The God Handler"})
    assert again.status_code == 409


async def test_a_person_approves_or_drops_it(
    client: httpx.AsyncClient, scripted: Loopback
) -> None:
    """The state is moved by a person. That is the whole reason the agenda is
    separate from the plan, whose statuses are derived from the change set."""
    task = (await client.post("/v1/agenda", json={"title": "split the god handler"})).json()

    approved = await client.post(
        f"/v1/agenda/{task['id']}", json={"state": "approved", "by": "dev", "note": "next sprint"}
    )
    assert approved.status_code == 200
    assert approved.json()["state"] == "approved"
    assert approved.json()["decided_by"] == "dev"

    dropped = await client.post(f"/v1/agenda/{task['id']}", json={"state": "dropped"})
    assert dropped.json()["state"] == "dropped"
    assert (await client.get("/v1/agenda")).json()["tasks"] == []


async def test_an_unknown_state_is_refused(
    client: httpx.AsyncClient, scripted: Loopback
) -> None:
    task = (await client.post("/v1/agenda", json={"title": "x"})).json()
    response = await client.post(f"/v1/agenda/{task['id']}", json={"state": "running"})
    assert response.status_code == 400


async def test_moving_a_task_that_is_not_there_is_a_404(
    client: httpx.AsyncClient, scripted: Loopback
) -> None:
    response = await client.post("/v1/agenda/nope", json={"state": "approved"})
    assert response.status_code == 404


async def test_the_agenda_needs_a_token(scripted: Loopback) -> None:
    from dakcoder_agent.loopback import create_app

    transport = CheckedTransport(create_app(scripted))
    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as anon:
        assert (await anon.get("/v1/agenda")).status_code == 401


# ── the plan, after the process that made it is gone ────────────────────────


async def test_a_session_with_no_plan_says_so(
    client: httpx.AsyncClient, scripted: Loopback
) -> None:
    session = await start(client)
    await settle(session["id"], scripted)
    response = await client.get(f"/v1/sessions/{session['id']}/plan")
    assert response.status_code in (200, 404)


async def test_the_plan_route_reads_from_disk(
    client: httpx.AsyncClient, scripted: Loopback
) -> None:
    """From disk rather than from the live loop, so it answers after a restart
    and for a session this daemon has never run -- which is most of them."""
    from dakcoder_agent.plan import PlanRecord
    from dakcoder_agent.tools.control import PlanStep

    session = await start(client)
    await settle(session["id"], scripted)
    PlanRecord(session_id=session["id"]).record(
        (PlanStep("handler/pension.go", "add it", "it builds"),), "migrate pensions"
    ).save(scripted.workspace)

    response = await client.get(f"/v1/sessions/{session['id']}/plan")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["summary"] == "migrate pensions"
    assert [s["file"] for s in body["steps"]] == ["handler/pension.go"]
    assert len(body["revisions"]) == 1


# ── compacting on demand ────────────────────────────────────────────────────


async def test_the_compact_command_finally_has_something_behind_it(
    client: httpx.AsyncClient, scripted: Loopback
) -> None:
    """The extension has had a `dakcoder.compactContext` command with no route,
    so the command could not do what its name says."""
    session = await start(client)
    await settle(session["id"], scripted)

    response = await client.post(f"/v1/sessions/{session['id']}/compact")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["strategy"] == "basic"
    assert body["after"] <= body["before"]


async def test_compacting_an_unknown_session_is_a_404(
    client: httpx.AsyncClient, scripted: Loopback
) -> None:
    assert (await client.post("/v1/sessions/nope/compact")).status_code == 404


# ── the canonical transcript, end to end ────────────────────────────────────


async def test_a_run_leaves_a_canonical_transcript_on_disk(
    client: httpx.AsyncClient, scripted: Loopback
) -> None:
    """The record `events.jsonl` was never quite: the conversation the model was
    actually having, with tool results whole."""
    session = await start(client)
    await settle(session["id"], scripted)

    from dakcoder_agent.journal import Journal

    records = Journal(scripted.workspace, session["id"]).read_records()

    assert records, "the run wrote no canonical transcript"
    assert {r["role"] for r in records} & {"assistant", "tool"}
    assert all("seq" in r for r in records)
    assert [r["seq"] for r in records] == sorted(r["seq"] for r in records)


async def test_the_two_views_are_both_available_and_different(
    client: httpx.AsyncClient, scripted: Loopback
) -> None:
    session = await start(client)
    await settle(session["id"], scripted)

    canonical = await client.get(f"/v1/sessions/{session['id']}/transcript?view=canonical")
    model = await client.get(f"/v1/sessions/{session['id']}/transcript?view=model")

    assert canonical.status_code == 200, canonical.text
    assert model.status_code == 200, model.text
    # The projection carries the pinned head; the record does not, because the
    # head is derived rather than something that happened.
    assert any(m["role"] == "system" for m in model.json()["messages"])
    assert all(r["role"] != "system" for r in canonical.json()["messages"])


async def test_the_transcript_route_needs_a_session(
    client: httpx.AsyncClient, scripted: Loopback
) -> None:
    assert (await client.get("/v1/sessions/nope/transcript")).status_code == 404
