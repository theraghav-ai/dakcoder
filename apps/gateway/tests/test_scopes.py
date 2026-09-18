"""What a token may be used for (host-plan §8): runners, machines, people."""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path

import httpx
import pytest
from fastapi.routing import APIRoute

from dakcoder_gateway.app import Gateway, create_app
from dakcoder_gateway.auth import AuthService, TokenMinter
from dakcoder_gateway.auth.clients import Clients, digest
from dakcoder_gateway.auth.scopes import runtime_scope
from dakcoder_gateway.quota import Limits, MemoryStore, QuotaPolicy
from dakcoder_gateway.runtime import RuntimeProxy
from dakcoder_shared.contract import CALLER_HEADER

from fakes import SECRET, FakeGitLab

MINTER = TokenMinter(SECRET)


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def person(sub: str = "alice") -> dict[str, str]:
    return bearer(MINTER.mint(sub=sub, username=sub, roles=()))


def scoped(scope: str, sub: str = "svc") -> dict[str, str]:
    return bearer(MINTER.mint(sub=sub, username=sub, roles=(), scope=scope))


@pytest.fixture
def upstream_seen():
    seen: list[dict] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append({"method": request.method, "path": request.url.path,
                     "caller": request.headers.get(CALLER_HEADER)})
        return httpx.Response(200, json={"ok": True})

    return seen, RuntimeProxy("http://up", "up-token", transport=httpx.MockTransport(handle))


@pytest.fixture
def clients_file(tmp_path: Path) -> Path:
    path = tmp_path / "clients.json"
    path.write_text(
        json.dumps(
            [
                {"client_id": "dashboard", "secret_sha256": digest("s3cret"),
                 "scopes": ["sessions:read"], "owner": "Portal Team"},
                {"client_id": "agentsvc", "secret_sha256": digest("delegator"),
                 "scopes": ["delegate"], "owner": "Ops"},
            ]
        ),
        encoding="utf-8",
    )
    return path


@pytest.fixture
def gw(upstream_seen, clients_file):
    _, proxy = upstream_seen
    limits = Limits(tokens_per_hour=50_000, tokens_per_window=50_000, runs_per_window=3)
    gateway = Gateway(
        AuthService(FakeGitLab(), MINTER),
        QuotaPolicy(MemoryStore(limits), limits),
        runtime=proxy,
        clients=Clients.load(clients_file),
    )
    return create_app(gateway)


def http(app) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://gw")


# ── runners ─────────────────────────────────────────────────────────────────


async def test_a_runners_token_reaches_the_model_and_nothing_else(gw, upstream_seen) -> None:
    seen, _ = upstream_seen
    runner = scoped("llm", sub="alice")
    async with http(gw) as client:
        quota = await client.get("/v1/quota", headers=runner)
        runtime = await client.get("/v1/runtime/v1/sessions", headers=runner)
        start = await client.post("/v1/runtime/v1/workspaces", headers=runner, json={})
        a2a = await client.post("/v1/a2a", headers=runner, json={})
        delegate = await client.post("/v1/auth/delegate", headers=runner, json={"sub": "bob"})
    assert quota.status_code == 200, "its own quota, as its owner"
    assert [r.status_code for r in (runtime, start, a2a, delegate)] == [403, 403, 403, 403]
    assert seen == [], "a stolen runner token cannot act as its owner on the hosted side"


async def test_only_the_control_plane_may_delegate(gw) -> None:
    async with http(gw) as client:
        by_person = await client.post("/v1/auth/delegate", headers=person(), json={"sub": "bob"})
        by_service = await client.post(
            "/v1/auth/delegate", headers=scoped("delegate", "client:agentsvc"),
            json={"sub": "alice", "hours": 1000},
        )
    assert by_person.status_code == 403, "a person's token never implies `delegate`"
    assert by_service.status_code == 200
    minted = by_service.json()
    claims = MINTER.verify(minted["access_token"])
    assert claims.sub == "alice", "minted as the owner, so the owner's quota is charged"
    assert claims.scopes == frozenset({"llm"}) and claims.roles == ("runner",)
    assert minted["expires_in"] == int(timedelta(hours=24).total_seconds()), "capped"


# ── machines ────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("form", [False, True], ids=["json", "form"])
async def test_a_registered_client_gets_a_token_with_its_scopes(gw, form) -> None:
    fields = {"grant_type": "client_credentials", "client_id": "dashboard", "client_secret": "s3cret"}
    async with http(gw) as client:
        response = await (client.post("/v1/auth/token", data=fields) if form
                          else client.post("/v1/auth/token", json=fields))
    assert response.status_code == 200, response.text
    claims = MINTER.verify(response.json()["access_token"])
    assert claims.sub == "client:dashboard" and claims.roles == ("machine",)
    assert claims.scopes == frozenset({"sessions:read"})


@pytest.mark.parametrize(
    ("fields", "status", "error"),
    [
        ({"grant_type": "client_credentials", "client_id": "dashboard", "client_secret": "wrong"}, 401, "invalid_client"),
        ({"grant_type": "client_credentials", "client_id": "nobody", "client_secret": "s3cret"}, 401, "invalid_client"),
        ({"grant_type": "password", "client_id": "dashboard", "client_secret": "s3cret"}, 400, "unsupported_grant_type"),
    ],
    ids=["bad-secret", "unknown-client", "wrong-grant"],
)
async def test_a_client_that_cannot_prove_itself_gets_nothing(gw, fields, status, error) -> None:
    async with http(gw) as client:
        response = await client.post("/v1/auth/token", json=fields)
    assert (response.status_code, response.json()["error"]) == (status, error)


async def test_a_read_only_client_can_read_and_nothing_more(gw, upstream_seen) -> None:
    seen, _ = upstream_seen
    reader = scoped("sessions:read", "client:dashboard")
    async with http(gw) as client:
        listed = await client.get("/v1/runtime/v1/sessions", headers=reader)
        lease = await client.post("/v1/runtime/v1/workspaces", headers=reader, json={})
        run = await client.post("/v1/runtime/v1/workspaces/w/tasks", headers=reader, json={})
        deliver = await client.post("/v1/runtime/v1/sessions/s/deliver", headers=reader, json={})
        agenda = await client.post("/v1/runtime/v1/workspaces/w/agenda", headers=reader, json={})
    assert listed.status_code == 200
    assert [r.status_code for r in (lease, run, deliver, agenda)] == [403, 403, 403, 403]
    assert [s["path"] for s in seen] == ["/v1/sessions"]


async def test_a_person_may_do_what_a_person_may(gw, upstream_seen) -> None:
    async with http(gw) as client:
        for method, path in [("GET", "v1/sessions"), ("POST", "v1/workspaces"), ("POST", "v1/sessions/s/deliver")]:
            response = await client.request(method, f"/v1/runtime/{path}", headers=person(), json={})
            assert response.status_code == 200, (method, path)


@pytest.mark.parametrize(
    ("method", "path", "scope"),
    [
        ("GET", "v1/sessions/s/events", "sessions:read"),
        ("POST", "v1/sessions/s/messages", "sessions:write"),
        ("POST", "v1/workspaces", "workspaces:write"),
        ("DELETE", "v1/workspaces/w", "workspaces:write"),
        ("POST", "v1/workspaces/w/tasks", "sessions:write"),
        ("POST", "v1/workspaces/w/agenda/t", "agenda:write"),
        ("POST", "v1/sessions/s/deliver", "deliver:mr"),
        ("POST", "v1/approvals/a", "sessions:write"),
    ],
)
def test_each_hosted_request_needs_the_scope_for_what_it_does(method, path, scope) -> None:
    assert runtime_scope(method, path) == scope


def test_a_client_registry_names_who_answers_for_each_client(tmp_path: Path) -> None:
    path = tmp_path / "c.json"
    path.write_text(json.dumps([{"client_id": "x", "secret_sha256": digest("s"), "scopes": ["a2a"]}]))
    with pytest.raises(ValueError):
        Clients.load(path)
    path.write_text(json.dumps([{"client_id": "x", "secret_sha256": digest("s"), "scopes": ["root"], "owner": "O"}]))
    with pytest.raises(ValueError):
        Clients.load(path)


def test_every_authenticated_route_says_what_scope_it_needs(gw) -> None:
    """A route added with the plain caller dependency would accept a runner's
    token, and a runner's token is the one most likely to be stolen."""

    def names(route: APIRoute) -> set[str]:
        found, pending = set(), list(route.dependant.dependencies)
        while pending:
            dep = pending.pop()
            if dep.call is not None:
                found.add(dep.call.__name__)
            pending.extend(dep.dependencies)
        return found

    unscoped = [
        route.path
        for route in gw.routes
        if isinstance(route, APIRoute)
        and "caller" in names(route)
        and not any(n.startswith("needs_") for n in names(route))
        # Checks its scope itself, once the path says which one it needs.
        and route.path != "/v1/runtime/{path:path}"
    ]
    assert unscoped == []
