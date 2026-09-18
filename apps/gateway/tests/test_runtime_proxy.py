"""The gateway fronting a hosted runtime (host-plan §3, Phase 1).

Two kinds of test. Against a recording stand-in, to see exactly what reaches the
runtime: whose token, which caller, which headers. And end to end, through the
real runtime app started as it is hosted (``gateway_forwarded``), to see that a
caller signed in at the gateway reaches their own sessions and nobody else's.
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

from dakcoder_agent.callers import gateway_forwarded
from dakcoder_agent.loopback import Loopback
from dakcoder_agent.loopback import create_app as create_runtime
from dakcoder_gateway.app import Gateway, create_app
from dakcoder_gateway.auth import AuthService, TokenMinter
from dakcoder_gateway.quota import Limits, MemoryStore, QuotaPolicy
from dakcoder_gateway.runtime import RuntimeProxy, RuntimeRefused
from dakcoder_shared.contract import CALLER_HEADER

from fakes import SECRET, FakeGitLab

RUNTIME_TOKEN = "the-runtime-token"


def jwt(sub: str) -> dict[str, str]:
    token = TokenMinter(SECRET).mint(sub=sub, username=sub, roles=())
    return {"Authorization": f"Bearer {token}"}


def gateway_with(runtime: RuntimeProxy | None, **kw) -> Gateway:
    limits = Limits(tokens_per_hour=50_000, tokens_per_window=50_000, runs_per_window=3)
    return Gateway(
        AuthService(FakeGitLab(), TokenMinter(SECRET)),
        QuotaPolicy(MemoryStore(limits), limits),
        runtime=runtime,
        **kw,
    )


def client_for(gateway: Gateway) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(gateway)), base_url="http://gw")


# ── what reaches the runtime ────────────────────────────────────────────────


@pytest.fixture
def recorded():
    """A stand-in runtime that remembers every request and answers simply."""
    seen: list[dict] = []
    app = FastAPI()

    @app.api_route("/{path:path}", methods=["GET", "POST", "DELETE"])
    async def anything(path: str, request: Request):
        seen.append(
            {
                "method": request.method,
                "path": path,
                "query": request.url.query,
                "headers": {k.lower(): v for k, v in request.headers.items()},
                "body": await request.body(),
            }
        )
        if path.endswith("/events"):
            return StreamingResponse(
                iter([b"id: 1\nevent: user\ndata: {}\n\n"]),
                media_type="text/event-stream",
                headers={"X-Accel-Buffering": "no", "Cache-Control": "no-cache"},
            )
        return JSONResponse({"error": "no session x"}, status_code=404 if "missing" in path else 200)

    proxy = RuntimeProxy("http://runtime", RUNTIME_TOKEN, transport=httpx.ASGITransport(app=app))
    return proxy, seen


async def test_the_runtime_gets_its_own_token_and_the_verified_caller(recorded) -> None:
    proxy, seen = recorded
    async with client_for(gateway_with(proxy)) as gw:
        response = await gw.post(
            "/v1/runtime/v1/tasks?x=1",
            json={"task": "add a handler"},
            headers={
                **jwt("alice"),
                # A client naming its own caller is the whole attack.
                CALLER_HEADER: "bob",
                "X-Dakcoder-Anything": "no",
                "Cookie": "session=abc",
                "Last-Event-ID": "7",
            },
        )
    assert response.status_code == 200

    (request,) = seen
    headers = request["headers"]
    assert headers["authorization"] == f"Bearer {RUNTIME_TOKEN}", "never the caller's JWT"
    assert headers[CALLER_HEADER.lower()] == "alice", "the verified sub, not the one sent"
    assert "x-dakcoder-anything" not in headers and "cookie" not in headers
    assert headers["last-event-id"] == "7", "resuming a stream must still work"
    assert (request["method"], request["path"], request["query"]) == ("POST", "v1/tasks", "x=1")
    assert request["body"] == b'{"task":"add a handler"}'


async def test_nothing_reaches_the_runtime_without_a_verified_caller(recorded) -> None:
    proxy, seen = recorded
    async with client_for(gateway_with(proxy)) as gw:
        assert (await gw.get("/v1/runtime/v1/sessions")).status_code == 401
        assert (
            await gw.get("/v1/runtime/v1/sessions", headers={"Authorization": f"Bearer {RUNTIME_TOKEN}"})
        ).status_code == 401, "the runtime's token is not a way in"
    assert seen == []


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "docs"),
        ("GET", "openapi.json"),
        ("POST", "v1/credential"),
        ("GET", "v1//sessions"),
    ],
)
async def test_paths_outside_the_runtime_api_are_not_forwarded(recorded, method, path) -> None:
    proxy, seen = recorded
    async with client_for(gateway_with(proxy)) as gw:
        response = await gw.request(method, f"/v1/runtime/{path}", headers=jwt("alice"), json={})
    assert response.status_code == 404
    assert seen == []


@pytest.mark.parametrize("path", ["v1/../docs", "v1/./sessions", "v1\\..\\docs", "v2/sessions"])
def test_a_path_that_would_normalise_elsewhere_is_refused(path) -> None:
    """Checked directly: an HTTP client normalises dot segments before sending,
    so these cannot be exercised through one, and a server might not."""
    with pytest.raises(RuntimeRefused):
        RuntimeProxy.check("GET", path)


async def test_the_event_stream_passes_through_unbuffered(recorded) -> None:
    proxy, _ = recorded
    async with client_for(gateway_with(proxy)) as gw:
        response = await gw.get("/v1/runtime/v1/sessions/s1/events", headers=jwt("alice"))
    assert response.headers["content-type"].startswith("text/event-stream")
    assert response.headers["x-accel-buffering"] == "no", "or nginx holds the stream"
    assert response.text == "id: 1\nevent: user\ndata: {}\n\n"


async def test_the_runtimes_refusals_come_back_as_they_are(recorded) -> None:
    proxy, _ = recorded
    async with client_for(gateway_with(proxy)) as gw:
        response = await gw.get("/v1/runtime/v1/sessions/missing", headers=jwt("alice"))
    assert response.status_code == 404
    assert response.json() == {"error": "no session x"}


async def test_a_runtime_that_does_not_answer_is_a_502() -> None:
    def refuse(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    proxy = RuntimeProxy("http://runtime", RUNTIME_TOKEN, transport=httpx.MockTransport(refuse))
    async with client_for(gateway_with(proxy)) as gw:
        response = await gw.get("/v1/runtime/v1/sessions", headers=jwt("alice"))
    assert response.status_code == 502
    assert response.json()["error"] == "runtime"


async def test_a_gateway_fronting_no_runtime_has_no_such_route() -> None:
    async with client_for(gateway_with(None)) as gw:
        assert (await gw.get("/v1/runtime/v1/sessions", headers=jwt("alice"))).status_code == 404


def test_a_runtime_proxy_needs_the_runtimes_token() -> None:
    with pytest.raises(ValueError):
        RuntimeProxy("http://runtime", "")


# ── end to end, through the real runtime ────────────────────────────────────


@pytest.fixture
def hosted(tmp_path: Path):
    runtime = Loopback(tmp_path, lambda _s, _a: None, token=RUNTIME_TOKEN)
    app = create_runtime(runtime, authenticate=gateway_forwarded(lambda: runtime.token))
    proxy = RuntimeProxy("http://runtime", RUNTIME_TOKEN, transport=httpx.ASGITransport(app=app))
    return runtime, gateway_with(proxy)


async def test_a_caller_reaches_their_own_sessions_and_no_one_elses(hosted) -> None:
    runtime, gateway = hosted
    mine = runtime.sessions.create("alice's task", owner="alice")
    theirs = runtime.sessions.create("bob's task", owner="bob")

    async with client_for(gateway) as gw:
        listed = await gw.get("/v1/runtime/v1/sessions", headers=jwt("alice"))
        own = await gw.get(f"/v1/runtime/v1/sessions/{mine.id}", headers=jwt("alice"))
        other = await gw.get(
            f"/v1/runtime/v1/sessions/{theirs.id}",
            headers={**jwt("alice"), CALLER_HEADER: "bob"},
        )
        health = (await gw.get("/v1/runtime/v1/health", headers=jwt("alice"))).json()

    assert [s["id"] for s in listed.json()["sessions"]] == [mine.id]
    assert own.status_code == 200
    assert other.status_code == 404, "naming bob in a header does not make alice bob"
    assert "workspace" not in health, "a hosted caller is not shown the server's paths"


# ── CORS ────────────────────────────────────────────────────────────────────


async def test_only_listed_origins_may_call_from_a_browser() -> None:
    gateway = gateway_with(None, cors_origins=("https://portal.example",))
    preflight = {
        "Access-Control-Request-Method": "GET",
        "Access-Control-Request-Headers": "authorization",
    }
    async with client_for(gateway) as gw:
        allowed = await gw.options("/v1/quota", headers={"Origin": "https://portal.example", **preflight})
        refused = await gw.options("/v1/quota", headers={"Origin": "https://evil.example", **preflight})
    assert allowed.headers.get("access-control-allow-origin") == "https://portal.example"
    assert "access-control-allow-origin" not in refused.headers


async def test_no_origins_means_no_cors_at_all() -> None:
    async with client_for(gateway_with(None)) as gw:
        response = await gw.get("/v1/health", headers={"Origin": "https://portal.example"})
    assert "access-control-allow-origin" not in response.headers


def test_a_wildcard_origin_is_refused() -> None:
    with pytest.raises(ValueError):
        gateway_with(None, cors_origins=("*",))


# ── the agent card and A2A (host-plan §5) ───────────────────────────────────


async def test_the_agent_card_is_served_to_anyone_once_something_is_fronted(recorded) -> None:
    proxy, _ = recorded
    async with client_for(gateway_with(proxy, public_url="https://example.test/dakcoder")) as gw:
        served = await gw.get("/.well-known/agent-card.json")
    assert served.status_code == 200, "discovery comes before sign-in"
    assert served.json()["url"] == "https://example.test/dakcoder/v1/a2a"
    async with client_for(gateway_with(None)) as gw:
        assert (await gw.get("/.well-known/agent-card.json")).status_code == 404, (
            "a card whose endpoint would 404 is a false promise"
        )


async def test_a2a_reaches_the_control_plane_for_the_verified_caller(recorded) -> None:
    proxy, seen = recorded
    body = {"jsonrpc": "2.0", "id": 1, "method": "tasks/get", "params": {"id": "x"}}
    async with client_for(gateway_with(proxy)) as gw:
        anonymous = await gw.post("/v1/a2a", json=body)
        signed_in = await gw.post("/v1/a2a", json=body, headers={**jwt("agent-7"), CALLER_HEADER: "bob"})
    assert anonymous.status_code == 401
    assert signed_in.status_code == 200
    (request,) = seen
    assert request["path"] == "v1/a2a"
    assert request["headers"][CALLER_HEADER.lower()] == "agent-7"


async def test_a_compressed_answer_reaches_the_client_readable() -> None:
    """`Content-Encoding` is not passed back, so the body must be decoded on the
    way through. Relaying the raw bytes handed clients gzip they could not read."""
    import gzip

    def compressed(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=gzip.compress(b'{"sessions": []}'),
            headers={"Content-Encoding": "gzip", "Content-Type": "application/json"},
        )

    proxy = RuntimeProxy("http://runtime", RUNTIME_TOKEN, transport=httpx.MockTransport(compressed))
    async with client_for(gateway_with(proxy)) as gw:
        response = await gw.get("/v1/runtime/v1/sessions", headers=jwt("alice"))
    assert response.json() == {"sessions": []}
