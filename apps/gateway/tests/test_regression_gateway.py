"""Regression tests for the gateway findings of the 2026-09-02 audit.

GW-1..GW-4, each named for the finding. The audit's observation about the
existing suite applies here too: it exercised each component's own discipline —
quota arithmetic, token minting, SSE relay — and never the seams where a client
disconnects mid-reserve or a header arrives malformed.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest
from fakes import FakeGitLab

from dakcoder_gateway.auth.identity import GitLabIdentity, IdentityError, IdentityProvider
from dakcoder_gateway.auth.service import AuthError, AuthService, verifier_challenge
from dakcoder_gateway.auth.tokens import TokenMinter


# ── GW-1: refresh must work in production, not only in CI ──────────────────


def test_the_identity_protocol_requires_recheck() -> None:
    """`recheck` is part of the port, so its absence is a type error.

    It was discovered with `getattr`, no production adapter implemented it, and
    the only implementation in the tree was the test fake: `/v1/auth/refresh`
    answered 501 in production and 200 in CI. Every session died at the
    fifteen-minute access-token TTL and asked for a full browser sign-in.
    """
    assert "recheck" in IdentityProvider.__protocol_attrs__  # type: ignore[attr-defined]
    assert hasattr(GitLabIdentity, "recheck"), "the production adapter must implement it"


async def test_gitlab_recheck_reads_the_account_back() -> None:
    """The user's own token, not an administrative one."""

    class FakeHTTP:
        def __init__(self) -> None:
            self.tokens: list[str] = []

        async def get(self, url, headers=None, params=None):
            self.tokens.append((headers or {}).get("Authorization", ""))
            body = (
                {"id": 7, "username": "asha", "state": "active"}
                if url.endswith("/api/v4/user")
                else [{"full_path": "it-2.0/pension-api"}]
            )
            return _Response(body)

    http = FakeHTTP()
    identity = GitLabIdentity("https://gitlab.test", "cid", "secret", http=http)

    profile = await identity.recheck("gitlab:7", "the-users-token")

    assert profile.username == "asha"
    assert profile.active
    assert http.tokens == ["Bearer the-users-token"] * 2


async def test_gitlab_recheck_refuses_a_credential_for_another_account() -> None:
    class FakeHTTP:
        async def get(self, url, headers=None, params=None):
            body = (
                {"id": 99, "username": "someone-else", "state": "active"}
                if url.endswith("/api/v4/user")
                else []
            )
            return _Response(body)

    identity = GitLabIdentity("https://gitlab.test", "cid", "secret", http=FakeHTTP())
    with pytest.raises(IdentityError):
        await identity.recheck("gitlab:7", "a-token")


async def test_a_refresh_carries_the_provider_credential(gitlab: FakeGitLab) -> None:
    """Captured at sign-in and carried across rotations — the alternative is an
    administrative GitLab token on the gateway, whose leak is every account."""
    service = AuthService(gitlab, TokenMinter("s" * 32))
    started = service.start("vscode://dop.dakcoder/callback", verifier_challenge("v" * 43))
    session = await service.exchange(
        code="good-code",
        code_verifier="v" * 43,
        state=started["state"],
        redirect_uri="vscode://dop.dakcoder/callback",
    )

    refreshed = await service.refresh(session.refresh_token)

    assert gitlab.rechecks == 1
    assert gitlab.recheck_tokens == ["gitlab-access-token"]
    assert refreshed.access_token
    # And the rotated token still carries it, so the second refresh works too.
    await service.refresh(refreshed.refresh_token)
    assert gitlab.recheck_tokens == ["gitlab-access-token", "gitlab-access-token"]


async def test_a_blocked_account_loses_access_on_refresh(gitlab: FakeGitLab) -> None:
    """The reason refresh re-asks at all. Pinned so the fix does not remove it."""
    from dakcoder_gateway.auth.identity import Profile

    service = AuthService(gitlab, TokenMinter("s" * 32))
    started = service.start("vscode://dop.dakcoder/callback", verifier_challenge("v" * 43))
    session = await service.exchange(
        code="good-code",
        code_verifier="v" * 43,
        state=started["state"],
        redirect_uri="vscode://dop.dakcoder/callback",
    )

    gitlab.profile_data = Profile(
        sub="gitlab:7", username="asha", groups=("it-2.0/pension-api",), active=False
    )
    with pytest.raises(AuthError):
        await service.refresh(session.refresh_token)


# ── GW-4: an idempotency key must survive concurrency ──────────────────────


class _SetNxGetRedis:
    """Just enough Redis to test what our code asks of it.

    `SET key value NX GET` returns the *previous* value (None when there was
    none) and only writes when there was none. Modelled here so the loser of a
    race is observable without a live server.
    """

    def __init__(self) -> None:
        self.store: dict[str, str] = {}
        self.sets = 0

    def register_script(self, _lua):  # the store registers its Lua at construction
        async def _never_called(*_a, **_k):
            raise AssertionError("these tests do not exercise the apply script")

        return _never_called

    async def set(self, key, value, ex=None, nx=False, get=False):
        self.sets += 1
        previous = self.store.get(key)
        if not nx or previous is None:
            self.store[key] = value
        return previous if get else None

    async def get(self, key):
        return self.store.get(key)


async def test_concurrent_deliveries_of_one_key_charge_once() -> None:
    """GET-then-SET was a check-then-act across a network hop: both callers read
    `None`, both wrote, and both were told "this is new" — so the same request
    was dispatched and charged twice, which is the one thing the key prevents."""
    from dakcoder_gateway.quota.store import RedisStore
    from dakcoder_gateway.quota.model import Limits

    client = _SetNxGetRedis()
    store = RedisStore(client, Limits(), prefix="test")

    async def deliver(value: str):
        return await store.remember("k1", "same-hash", {"tag": value}, timedelta(minutes=5))

    first, second = await asyncio.gather(deliver("a"), deliver("b"))

    fresh = [r for r in (first, second) if r is None]
    replays = [r for r in (first, second) if r is not None]
    assert len(fresh) == 1, "exactly one caller may be told the key is new"
    assert len(replays) == 1, "the loser must get the winner's answer, not a second charge"


async def test_a_key_reused_with_a_different_body_is_a_conflict() -> None:
    from dakcoder_gateway.quota.store import Conflict, RedisStore
    from dakcoder_gateway.quota.model import Limits

    store = RedisStore(_SetNxGetRedis(), Limits(), prefix="test")
    assert await store.remember("k", "hash-a", {"v": 1}, timedelta(minutes=5)) is None
    with pytest.raises(Conflict):
        await store.remember("k", "hash-b", {"v": 2}, timedelta(minutes=5))


class _Response:
    def __init__(self, body, status: int = 200) -> None:
        self._body = body
        self.status_code = status

    def json(self):
        return self._body


# ── GW-2: a disconnect must not burn quota ────────────────────────────────


class _SlowUpstream:
    """An upstream that has not produced a byte yet. The window GW-2 lives in."""

    def stream(self, method, url, *, json=None, headers=None):
        return self

    async def __aenter__(self):
        self.status_code = 200
        return self

    async def __aexit__(self, *_exc):
        return False

    async def aiter_lines(self):
        await asyncio.sleep(30)
        yield "data: [DONE]"


async def test_a_disconnect_before_the_first_byte_releases_the_reservation() -> None:
    """The client going away arrives as `CancelledError` — a `BaseException`.

    `except Exception` never saw it and the `finally` had nothing to settle, so
    the reservation sat against the developer's hourly quota at its full
    estimate until the window rolled. Four abandoned runs could 429 a developer
    out of a budget they had not spent.
    """
    from dakcoder_gateway.ledger import MemoryLedger
    from dakcoder_gateway.proxy import ModelProxy
    from dakcoder_gateway.quota import Limits, MemoryStore, QuotaPolicy
    from fakes import API_KEY

    limits = Limits(tokens_per_hour=100_000, tokens_per_window=100_000)
    quota = QuotaPolicy(MemoryStore(limits), limits)
    proxy = ModelProxy(
        "https://ai.cept.gov.in/v1",
        API_KEY,
        quota,
        ledger=MemoryLedger(),
        http=_SlowUpstream(),
    )

    def spent() -> int:
        return sum(v for k, v in used_now.items() if "tokens" in k)

    used_now = (await quota.snapshot("gitlab:7")).used
    before = spent()

    stream = proxy.stream(
        "chat/completions",
        {"model": "coder", "messages": [], "stream": True},
        sub="gitlab:7",
        estimated=40_000,
    )

    async def read() -> None:
        async for _chunk in stream:
            pass

    task = asyncio.create_task(read())
    await asyncio.sleep(0.05)   # long enough to reserve, not long enough to stream
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await stream.aclose()
    await proxy.drain()

    used_now = (await quota.snapshot("gitlab:7")).used
    after = spent()
    assert after <= before, (
        f"an abandoned request kept its 40,000-token reservation: {before} -> {after}"
    )


# ── GW-3: bad input is a 400, not a 500 ───────────────────────────────────


def test_bad_hot_path_input_is_refused_with_a_reason() -> None:
    """A 500 says the gateway is broken. A header typo is not that."""
    from dakcoder_gateway.app import _body, _lane, _tokens, _turn
    from dakcoder_gateway.proxy import ProxyError

    for bad in (b"not json", b"[1,2,3]", b'"a string"'):
        with pytest.raises(ProxyError) as caught:
            _body(bad)
        assert caught.value.status == 400

    for bad in ("abc", "-5", "1e6"):
        with pytest.raises(ProxyError) as caught:
            _tokens(bad)
        assert caught.value.status == 400

    with pytest.raises(ProxyError) as caught:
        _lane("first-class")
    assert caught.value.status == 400

    # Telemetry, so a bad one is dropped rather than refused.
    assert _turn("nonsense") == 0
    assert _turn("-3") == 0
    assert _turn(None) == 0


def test_an_absurd_reservation_is_clamped_not_believed() -> None:
    from dakcoder_gateway.app import MAX_ESTIMATED_TOKENS, _tokens

    assert _tokens("999999999999") == MAX_ESTIMATED_TOKENS
    assert _tokens("4096") == 4096
    assert _tokens(None) == 0
    assert _tokens("") == 0


# ── GW-5 / GW-6 / GW-14: hygiene ──────────────────────────────────────────


def test_the_settled_set_is_bounded() -> None:
    """It answers "have I already settled this?" for a reservation in flight.

    It was an unbounded set holding every reservation id the process had ever
    settled — one more string per turn for the life of the worker.
    """
    from dakcoder_gateway.quota import Limits, MemoryStore, QuotaPolicy
    from dakcoder_gateway.quota.policy import MAX_SETTLED_IDS

    policy = QuotaPolicy(MemoryStore(Limits()), Limits())
    for n in range(MAX_SETTLED_IDS + 500):
        policy._remember_settled(f"r{n}")

    assert len(policy._settled) == MAX_SETTLED_IDS
    assert "r0" not in policy._settled, "the oldest is what a bound drops"
    assert f"r{MAX_SETTLED_IDS + 499}" in policy._settled


def test_a_model_key_is_never_in_a_repr() -> None:
    """The model key is the one secret this gateway exists to hold, and a
    dataclass repr puts it in every log line and traceback that touches a
    route — including the ones an operator pastes into a ticket."""
    from dakcoder_gateway.routing import ModelRoute

    route = ModelRoute(
        role="coder", model="qwen", base_url="https://ai.test/v1", api_key="sk-secret-value"
    )
    assert "sk-secret-value" not in repr(route)
    assert route.api_key == "sk-secret-value", "it must still be readable"


async def test_an_oversized_body_is_refused_before_it_is_held() -> None:
    """The whole body was read into memory before anything looked at it."""
    from dakcoder_gateway.app import MAX_BODY_BYTES, _read_body
    from dakcoder_gateway.proxy import ProxyError

    class Streamed:
        headers = {"content-length": str(MAX_BODY_BYTES + 1)}

        async def stream(self):  # pragma: no cover - refused on the header
            yield b""

    with pytest.raises(ProxyError) as caught:
        await _read_body(Streamed())
    assert caught.value.status == 413

    class Lying:
        headers: dict[str, str] = {}

        async def stream(self):
            for _ in range(MAX_BODY_BYTES // 1024 + 2):
                yield b"x" * 1024

    with pytest.raises(ProxyError) as caught:
        await _read_body(Lying())
    assert caught.value.status == 413, "a body with no content-length is still bounded"
