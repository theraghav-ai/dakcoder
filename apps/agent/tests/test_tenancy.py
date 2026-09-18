"""One caller must never reach another's sessions (host-plan §8, §14).

The plan calls this "the one place in the plan where getting it wrong is a
security incident rather than a bug", and the way it goes wrong is not a broken
filter but a missing one: a route added later that reads the session store
directly. So the first test here walks the route table, and fails on any route
that does not take its session through ``owned``. A hand-maintained list of
routes that must be scoped would not have survived: six routes once arrived in
one release while the only document listing them stood still.
"""

from __future__ import annotations

import inspect
from pathlib import Path

import httpx
import pytest
from fastapi.routing import APIRoute

from dakcoder_agent.callers import Caller, Unauthorised
from dakcoder_agent.loopback import Loopback, PendingApproval, create_app
from dakcoder_agent.session import SessionStore
from dakcoder_agent.tools.router import ApprovalRequest
from test_loopback import scripted, settle  # noqa: F401 - fixture
from wirecheck import CheckedTransport

ALICE, BOB = "Bearer alice", "Bearer bob"


def two_tenants(authorization: str | None) -> Caller:
    """A stand-in for a hosted authenticator: two callers, told apart by token."""
    if authorization in (ALICE, BOB):
        return Caller(sub=authorization.split()[1])
    raise Unauthorised("who are you")


@pytest.fixture
def app(tmp_path: Path):
    return create_app(Loopback(tmp_path, lambda _s, _a: None), authenticate=two_tenants)


def dependencies(route: APIRoute) -> set[str]:
    """The name of every dependency a route resolves, however deep."""
    names: set[str] = set()
    pending = list(route.dependant.dependencies)
    while pending:
        dependant = pending.pop()
        if dependant.call is not None:
            names.add(dependant.call.__name__)
        pending.extend(dependant.dependencies)
    return names


def routes(app) -> list[APIRoute]:
    return [r for r in app.routes if isinstance(r, APIRoute)]


# ── the route table ─────────────────────────────────────────────────────────


def test_every_session_route_takes_its_session_from_owned(app) -> None:
    unscoped = [
        f"{sorted(r.methods)} {r.path}"
        for r in routes(app)
        if "{session_id}" in r.path and "owned" not in dependencies(r)
    ]
    assert unscoped == [], (
        "these routes reach a session without the ownership check; take it as "
        "`session: Session = Depends(owned)`"
    )


def test_every_approval_route_takes_its_approval_from_owned_approval(app) -> None:
    unscoped = [
        f"{sorted(r.methods)} {r.path}"
        for r in routes(app)
        if "{approval_id}" in r.path and "owned_approval" not in dependencies(r)
    ]
    assert unscoped == []


def test_every_route_but_health_knows_its_caller(app) -> None:
    anonymous = [
        f"{sorted(r.methods)} {r.path}"
        for r in routes(app)
        if r.path != "/v1/health" and "caller" not in dependencies(r)
    ]
    assert anonymous == []


def test_no_handler_reads_the_authorization_header_itself(app) -> None:
    """Only `caller` and health may. A handler that checks the header on its own
    is one that can check it differently, or not at all."""
    readers = [
        r.path
        for r in routes(app)
        if r.path != "/v1/health" and "authorization" in inspect.signature(r.endpoint).parameters
    ]
    assert readers == []


# ── two callers ─────────────────────────────────────────────────────────────


def client(app, token: str) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=CheckedTransport(app),
        base_url="http://127.0.0.1",
        headers={"Authorization": token},
    )


def concrete(path: str, session_id: str) -> str:
    return path.replace("{session_id}", session_id)


#: A body every POST route will accept far enough to reach its checks.
BODY = {"text": "hello", "note": "", "decision": "reject"}


async def test_another_callers_session_does_not_exist(app) -> None:
    """Every session route, found by walking the table, answers bob with the
    same 404 it gives for an id that was never issued. Not a 403: that would
    confirm the id is real."""
    runtime: Loopback = app.state.runtime
    theirs = runtime.sessions.create("alice's task", owner="alice")

    async with client(app, BOB) as bob:
        for route in routes(app):
            if "{session_id}" not in route.path:
                continue
            for method in route.methods:
                response = await bob.request(
                    method,
                    concrete(route.path, theirs.id),
                    json=BODY if method == "POST" else None,
                )
                assert response.status_code == 404, (method, route.path, response.text)
                assert response.json() == {"error": f"no session {theirs.id}"}, (
                    "indistinguishable from a session that does not exist"
                )


async def test_a_caller_lists_and_counts_only_their_own(app) -> None:
    runtime: Loopback = app.state.runtime
    mine = runtime.sessions.create("alice's task", owner="alice")
    runtime.sessions.create("bob's task", owner="bob")

    async with client(app, ALICE) as alice:
        listed = (await alice.get("/v1/sessions")).json()["sessions"]
        detail = await alice.get(f"/v1/sessions/{mine.id}")
        health = (await alice.get("/v1/health")).json()

    assert [s["id"] for s in listed] == [mine.id]
    assert detail.status_code == 200
    assert health["sessions"]["total"] == 1


async def test_another_callers_approval_is_already_gone(app) -> None:
    runtime: Loopback = app.state.runtime
    theirs = runtime.sessions.create("alice's task", owner="alice")
    request = ApprovalRequest(tool="write_file", arguments={"path": "a.go"}, reason="r")
    runtime.approvals[request.id] = PendingApproval(
        id=request.id, session_id=theirs.id, request=request
    )

    async with client(app, BOB) as bob:
        listed = (await bob.get("/v1/approvals")).json()["approvals"]
        decided = await bob.post(f"/v1/approvals/{request.id}", json={"decision": "accept"})
        extended = await bob.post(f"/v1/approvals/{request.id}/extend")
    async with client(app, ALICE) as alice:
        own = (await alice.get("/v1/approvals")).json()["approvals"]

    assert listed == []
    assert decided.status_code == 410 and extended.status_code == 410
    assert not runtime.approvals[request.id].decided.is_set(), "bob's answer must not land"
    assert runtime.approvals[request.id].extensions == 0
    assert [a["id"] for a in own] == [request.id]


async def test_a_task_belongs_to_whoever_started_it(scripted: Loopback) -> None:
    app = create_app(scripted, authenticate=two_tenants)
    async with client(app, ALICE) as alice:
        started = (await alice.post("/v1/tasks", json={"task": "add a handler"})).json()
        await settle(started["id"], scripted)
    async with client(app, BOB) as bob:
        assert (await bob.get(f"/v1/sessions/{started['id']}")).status_code == 404
    assert scripted.sessions.get(started["id"]).owner == "alice"


async def test_an_unknown_caller_is_refused(app) -> None:
    async with client(app, "Bearer mallory") as mallory:
        assert (await mallory.get("/v1/sessions")).status_code == 401
        health = (await mallory.get("/v1/health")).json()
    assert "sessions" not in health, "liveness only, as for any caller without a token"


# ── ownership outlives the process ──────────────────────────────────────────


def test_ownership_survives_a_restart(tmp_path: Path) -> None:
    store = SessionStore(tmp_path)
    session = store.create("alice's task", owner="alice")
    local = store.create("the developer's task")

    restored = SessionStore(tmp_path)
    assert restored.get(session.id).owner == "alice"
    assert restored.get(local.id).owner == "", "a local session stays the local developer's"
