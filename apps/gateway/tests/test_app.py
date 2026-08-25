"""Tests for the gateway's HTTP surface.

Driven through the real ASGI app with httpx, so the routing, the dependency
injection and the exception handlers are all exercised. The alternative —
calling the route functions directly — tests everything except the parts that
only exist because it is a web service.
"""

from __future__ import annotations

import httpx
import pytest

from dakcoder_gateway.app import Gateway, create_app
from dakcoder_gateway.auth import AuthService, TokenMinter, verifier_challenge
from dakcoder_gateway.ledger import MemoryLedger
from dakcoder_gateway.proxy import ModelProxy
from dakcoder_gateway.quota import Limits, MemoryStore, QuotaPolicy

from fakes import API_KEY, REDIRECT, SECRET, VERIFIER, FakeGitLab, FakeUpstream


@pytest.fixture
def gateway(upstream: FakeUpstream) -> Gateway:
    limits = Limits(tokens_per_hour=50_000, tokens_per_window=50_000, runs_per_window=3)
    quota = QuotaPolicy(MemoryStore(limits), limits)
    ledger = MemoryLedger()
    return Gateway(
        AuthService(FakeGitLab(), TokenMinter(SECRET)),
        quota,
        ModelProxy("https://ai.cept.gov.in/v1", API_KEY, quota, ledger=ledger, http=upstream),
        ledger=ledger,
        tool_catalog={"contract": "C1", "tools": [{"name": "read_file"}]},
        version="1.0.0-test",
    )


@pytest.fixture
async def client(gateway: Gateway):
    app = create_app(gateway)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://gw") as http:
        yield http


async def sign_in(client: httpx.AsyncClient) -> dict:
    started = await client.post(
        "/v1/auth/start",
        json={"redirect_uri": REDIRECT, "code_challenge": verifier_challenge(VERIFIER)},
    )
    exchanged = await client.post(
        "/v1/auth/exchange",
        json={
            "code": "good-code",
            "code_verifier": VERIFIER,
            "state": started.json()["state"],
            "redirect_uri": REDIRECT,
        },
    )
    assert exchanged.status_code == 200, exchanged.text
    return exchanged.json()


def bearer(session: dict) -> dict[str, str]:
    return {"Authorization": f"Bearer {session['access_token']}"}


# ── sign-in (C3) ────────────────────────────────────────────────────────────


async def test_a_developer_can_sign_in(client: httpx.AsyncClient) -> None:
    session = await sign_in(client)
    assert session["profile"]["username"] == "asha"
    assert session["profile"]["dop_roles"] == ["user"]
    assert session["quota"]["window_open"] is False


async def test_start_requires_both_parameters(client: httpx.AsyncClient) -> None:
    response = await client.post("/v1/auth/start", json={"redirect_uri": REDIRECT})
    assert response.status_code == 400


async def test_a_replayed_state_is_refused_with_401(client: httpx.AsyncClient) -> None:
    started = await client.post(
        "/v1/auth/start",
        json={"redirect_uri": REDIRECT, "code_challenge": verifier_challenge(VERIFIER)},
    )
    state = started.json()["state"]
    payload = {
        "code": "good-code",
        "code_verifier": VERIFIER,
        "state": state,
        "redirect_uri": REDIRECT,
    }
    assert (await client.post("/v1/auth/exchange", json=payload)).status_code == 200
    assert (await client.post("/v1/auth/exchange", json=payload)).status_code == 401


async def test_refresh_rotates_over_http(client: httpx.AsyncClient) -> None:
    session = await sign_in(client)
    response = await client.post(
        "/v1/auth/refresh", json={"refresh_token": session["refresh_token"]}
    )
    assert response.status_code == 200
    assert response.json()["refresh_token"] != session["refresh_token"]


# ── everything else needs a token ───────────────────────────────────────────


@pytest.mark.parametrize(
    "method,path",
    [("get", "/v1/quota"), ("post", "/v1/runs"), ("post", "/v1/llm/chat/completions")],
)
async def test_the_metered_routes_refuse_an_anonymous_caller(
    client: httpx.AsyncClient, method: str, path: str
) -> None:
    """What is being replaced is a shared token plus a client-supplied user
    header — an arrangement where anyone can claim any identity."""
    call = getattr(client, method)
    response = await (call(path) if method == "get" else call(path, json={}))
    assert response.status_code == 401


# The short key is the point: this is a forgery attempt, and PyJWT's
# advice about key length is aimed at people minting real tokens.
@pytest.mark.filterwarnings("ignore::UserWarning")
async def test_a_forged_token_is_refused(client: httpx.AsyncClient) -> None:
    import jwt

    forged = jwt.encode({"sub": "gitlab:1", "iat": 0, "exp": 9_999_999_999}, "wrong", algorithm="HS256")
    response = await client.get("/v1/quota", headers={"Authorization": f"Bearer {forged}"})
    assert response.status_code == 401


# ── quota (C4) ──────────────────────────────────────────────────────────────


async def test_the_quota_snapshot_is_shaped_for_the_status_bar(
    client: httpx.AsyncClient,
) -> None:
    session = await sign_in(client)
    payload = (await client.get("/v1/quota", headers=bearer(session))).json()

    assert payload["sub"] == "gitlab:7"
    assert "tightest" in payload
    assert set(payload["limits"]) >= {"hour_tokens", "week_tokens"}


async def test_preflight_answers_without_charging(client: httpx.AsyncClient) -> None:
    session = await sign_in(client)
    response = await client.post(
        "/v1/quota/preflight", json={"estimated_tokens": 10_000}, headers=bearer(session)
    )
    assert response.json()["ok"] is True
    assert response.json()["quota"]["used"]["hour_tokens"] == 0


async def test_starting_a_run_opens_a_window(client: httpx.AsyncClient) -> None:
    session = await sign_in(client)
    payload = (await client.post("/v1/runs", json={}, headers=bearer(session))).json()
    assert payload["window_expires_at"] > payload["window_opened_at"]
    assert payload["runs_used"] == 1


async def test_exhausting_runs_returns_a_429_with_every_c4_header(
    client: httpx.AsyncClient,
) -> None:
    session = await sign_in(client)
    for _ in range(3):
        await client.post("/v1/runs", json={}, headers=bearer(session))

    response = await client.post("/v1/runs", json={}, headers=bearer(session))
    assert response.status_code == 429
    for header in ("Retry-After", "X-RateLimit-Limit", "X-Quota-Window-Reset"):
        assert header in response.headers
    assert response.json()["reason"]


# ── the model proxy (§15.4) ─────────────────────────────────────────────────


async def test_a_model_call_streams_back(client: httpx.AsyncClient) -> None:
    session = await sign_in(client)
    async with client.stream(
        "POST",
        "/v1/llm/chat/completions",
        json={"model": "coder", "messages": [{"role": "user", "content": "hi"}], "stream": True},
        headers={**bearer(session), "X-Estimated-Tokens": "5000"},
    ) as response:
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        # nginx buffers proxied responses by default, which would hold every
        # chunk until the stream ended — a streaming endpoint turned into a slow
        # non-streaming one, silently.
        assert response.headers["x-accel-buffering"] == "no"
        body = "".join([chunk async for chunk in response.aiter_text()])

    assert "package " in body
    assert API_KEY not in body


async def test_the_call_is_metered_and_ledgered(
    client: httpx.AsyncClient, gateway: Gateway
) -> None:
    session = await sign_in(client)
    async with client.stream(
        "POST",
        "/v1/llm/chat/completions",
        json={"model": "coder", "messages": [], "stream": True},
        headers={**bearer(session), "X-Estimated-Tokens": "5000", "X-Session-Id": "s-9",
                 "X-Turn": "4", "X-Mode": "planner"},
    ) as response:
        [chunk async for chunk in response.aiter_text()]

    assert len(gateway.ledger.events) == 1
    event = gateway.ledger.events[0]
    assert (event.session_id, event.turn, event.mode) == ("s-9", 4, "planner")
    assert event.sub == "gitlab:7"

    snapshot = (await client.get("/v1/quota", headers=bearer(session))).json()
    assert snapshot["used"]["hour_tokens"] == 1_540


async def test_an_unknown_role_is_a_400_not_a_500(client: httpx.AsyncClient) -> None:
    session = await sign_in(client)
    response = await client.post(
        "/v1/llm/chat/completions",
        json={"model": "gpt-4o", "messages": [], "stream": True},
        headers=bearer(session),
    )
    assert response.status_code == 400
    assert "not a configured role" in response.json()["reason"]


async def test_an_unproxied_path_is_a_404(client: httpx.AsyncClient) -> None:
    session = await sign_in(client)
    response = await client.post(
        "/v1/llm/models", json={"model": "coder"}, headers=bearer(session)
    )
    assert response.status_code == 404


async def test_a_request_with_no_estimate_still_reserves(
    client: httpx.AsyncClient, gateway: Gateway
) -> None:
    """A client that forgets the header must not get an unmetered turn. The
    fallback over-reserves, and reconcile gives the difference back."""
    session = await sign_in(client)
    async with client.stream(
        "POST",
        "/v1/llm/chat/completions",
        json={"model": "coder", "messages": [{"role": "user", "content": "x" * 300}],
              "stream": True, "max_tokens": 2048},
        headers=bearer(session),
    ) as response:
        [chunk async for chunk in response.aiter_text()]

    assert gateway.ledger.events[0].estimated_tokens > 2_000


async def test_the_quota_store_being_down_is_a_503_not_a_500(
    client: httpx.AsyncClient, gateway: Gateway
) -> None:
    """The request is refused because it cannot be *metered* — a temporary
    condition of ours, not a fault in what was sent. The distinction tells the
    client to retry later rather than to change the request."""
    session = await sign_in(client)

    async def broken(*_a, **_k):
        raise ConnectionError("redis is down")

    gateway.quota.store.apply = broken

    response = await client.post(
        "/v1/llm/chat/completions",
        json={"model": "coder", "messages": [], "stream": True},
        headers=bearer(session),
    )
    assert response.status_code == 503
    assert response.headers["Retry-After"]


# ── introspection ───────────────────────────────────────────────────────────


async def test_health_publishes_the_limits_in_force(client: httpx.AsyncClient) -> None:
    """Every number in §16.1 is a placeholder until Qwen capacity is measured.
    Publishing them makes tuning something anyone can verify took effect."""
    payload = (await client.get("/v1/health")).json()
    assert payload["version"] == "1.0.0-test"
    assert payload["limits"]["tokens_per_hour"] == 50_000
    assert payload["limits"]["cached_discount"] == 1.0


async def test_health_needs_no_token(client: httpx.AsyncClient) -> None:
    """A health check that needs a credential cannot tell a monitor whether the
    credential path is the thing that is broken."""
    assert (await client.get("/v1/health")).status_code == 200


async def test_the_tool_catalogue_is_published(client: httpx.AsyncClient) -> None:
    payload = (await client.get("/v1/tools")).json()
    assert payload["contract"] == "C1"


async def test_a_failure_before_the_first_byte_is_a_status_code(
    client: httpx.AsyncClient, upstream: FakeUpstream
) -> None:
    """The stream is primed before the response is returned.

    Without that, Starlette has already sent the 200 by the time the generator
    raises, the exception handler cannot run, and the client sees a successful
    response that stops mid-stream.
    """
    upstream.status = 503
    session = await sign_in(client)
    response = await client.post(
        "/v1/llm/chat/completions",
        json={"model": "coder", "messages": [], "stream": True},
        headers=bearer(session),
    )
    assert response.status_code == 502
    assert response.json()["error"] == "upstream"


async def test_a_failure_after_the_first_byte_becomes_an_error_event(
    client: httpx.AsyncClient, upstream: FakeUpstream
) -> None:
    """The status code is already spent, so the failure is reported in the one
    channel still open. Contract C2 has an `error` event type for exactly this,
    and dropping the connection instead would be indistinguishable from a
    network fault — which clients retry, doubling the cost of the failure."""
    upstream.explode_after = 2
    session = await sign_in(client)

    async with client.stream(
        "POST",
        "/v1/llm/chat/completions",
        json={"model": "coder", "messages": [], "stream": True},
        headers=bearer(session),
    ) as response:
        assert response.status_code == 200
        body = "".join([chunk async for chunk in response.aiter_text()])

    assert "package " in body, "the chunks that did arrive should still be relayed"
    assert "event: error" in body
    assert "ConnectionError" in body
