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


async def test_a_follow_up_re_enters_its_mode_rather_than_inheriting_the_last_one(
    client: httpx.AsyncClient, scripted: Loopback
) -> None:
    """A conversation that ended in one mode must not answer the next message
    under it.

    ``_switch`` skips work when the loop is already in the mode asked for, and a
    follow-up builds a *new* loop that defaults to Planner while the context is
    still wherever the previous run left it. Guarding on the loop's copy alone
    left the Debugger's overlay and prompt budget in force under a loop
    dispatching the Planner's tools.
    """
    from dakcoder_agent.modes import Mode

    session = await start(client)
    await settle(session["id"], scripted)
    context = scripted.contexts[session["id"]]
    context.switch_mode(Mode.DEBUGGER, "debugging")
    assert context.mode is Mode.DEBUGGER

    await client.post(
        f"/v1/sessions/{session['id']}/messages",
        json={"text": "and now the handler", "mode": "planner"},
    )
    await settle(session["id"], scripted)

    # Asserted on the overlays rather than on the mode the run ended in: this
    # run advances Planner -> Coder like any other, and the question is whether
    # it *entered* the Planner at all or carried on under the Debugger.
    overlays = [m.source for m in context.build() if m.source.startswith("mode:")]
    assert "mode:planner" in overlays[overlays.index("mode:debugger") :], (
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

    transport = httpx.ASGITransport(app=create_app(scripted))
    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as http:
        response = await http.request(method, path.format(id="whatever"), json={})
    assert response.status_code == 401, f"{method} {path} answered without a token"
