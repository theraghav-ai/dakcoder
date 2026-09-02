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


# ── GW-7: a refresh token is a credential, not a dictionary key ────────────


async def test_a_refresh_token_is_not_held_in_plaintext(gitlab: FakeGitLab) -> None:
    """Thirty-day credentials sat in a dict, in the process that holds the model
    API keys, for as long as they were valid. The gateway never needs the token
    back — only to recognise one — so it keeps a SHA-256 verifier and the
    plaintext lives only inside the request that presented it."""
    import hashlib

    service = AuthService(gitlab, TokenMinter("s" * 32))
    started = service.start("vscode://dop.dakcoder/callback", verifier_challenge("v" * 43))
    session = await service.exchange(
        code="good-code",
        code_verifier="v" * 43,
        state=started["state"],
        redirect_uri="vscode://dop.dakcoder/callback",
    )

    held = list(service._refresh)
    assert session.refresh_token not in held, "the credential itself must not be stored"
    assert held == [hashlib.sha256(session.refresh_token.encode("ascii")).hexdigest()]

    # And it still works, which is the whole point of a verifier.
    rotated = await service.refresh(session.refresh_token)
    assert rotated.refresh_token != session.refresh_token


async def test_the_provider_credential_is_never_in_a_repr(gitlab: FakeGitLab) -> None:
    """Same discipline as the route's `api_key`. A dataclass renders every field,
    and this one is a live GitLab token: one exception context away from a log."""
    service = AuthService(gitlab, TokenMinter("s" * 32))
    started = service.start("vscode://dop.dakcoder/callback", verifier_challenge("v" * 43))
    await service.exchange(
        code="good-code",
        code_verifier="v" * 43,
        state=started["state"],
        redirect_uri="vscode://dop.dakcoder/callback",
    )
    record = next(iter(service._refresh.values()))
    assert record.provider_token == "gitlab-access-token", "it is still usable"
    assert "gitlab-access-token" not in repr(record)


async def test_a_revoked_family_does_not_live_for_ever(gitlab: FakeGitLab) -> None:
    """It was a set that only grew: one entry per revoked session for the life of
    the process, and reuse detection adds them in cascades."""
    service = AuthService(gitlab, TokenMinter("s" * 32))
    started = service.start("vscode://dop.dakcoder/callback", verifier_challenge("v" * 43))
    session = await service.exchange(
        code="good-code",
        code_verifier="v" * 43,
        state=started["state"],
        redirect_uri="vscode://dop.dakcoder/callback",
    )
    service.revoke(session.refresh_token)
    assert len(service._revoked_families) == 1

    # Past every refresh token the family could still have. Nothing can present
    # one, so nothing needs to remember refusing it.
    for family in service._revoked_families:
        service._revoked_families[family] = 0.0
    service._sweep()
    assert service._revoked_families == {}


# ── GW-8: a lost charge must not hide inside a ZSET member ─────────────────


def test_two_charges_in_one_millisecond_are_two_rows() -> None:
    """The member was `amount:now:i:sha1hex(key .. now .. i)` — a hash of three
    values already in the member, so it added no entropy at all. ZADD on an
    existing member updates its score instead of adding a row, and the second
    charge vanished."""
    import re

    from dakcoder_gateway.quota.store import _APPLY_LUA

    assert "sha1hex" not in _APPLY_LUA, "a hash of the member's own parts is not a nonce"
    assert re.search(r"local nonce = ARGV\[3\]", _APPLY_LUA), "the nonce comes from the caller"
    assert "'ZADD', key, now, amount .. ':' .. now .. ':' .. i .. ':' .. nonce" in _APPLY_LUA


async def test_each_apply_sends_its_own_nonce() -> None:
    """Two invocations, two nonces. One per call is what makes the member unique
    across concurrent settlements of the same series."""
    from datetime import datetime, timezone

    from dakcoder_gateway.quota.model import Check, Series
    from dakcoder_gateway.quota.store import RedisStore

    seen: list[str] = []

    class RecordingClient:
        async def zadd(self, *a, **k):  # pragma: no cover - not reached
            return 1

        async def hgetall(self, *a, **k):
            return {}

        async def zrange(self, *a, **k):
            return []

        def register_script(self, _src):
            async def run(keys=None, args=None):
                seen.append(str(args[2]))
                return '{"ok": true, "used": [0]}'

            return run

    store = RedisStore(RecordingClient())
    now = datetime(2026, 9, 2, tzinfo=timezone.utc)
    checks = [Check(series=Series.HOUR_TOKENS, amount=10, limit=1000, label="tokens an hour")]
    await store.apply("gitlab:7", checks, now)
    await store.apply("gitlab:7", checks, now)

    assert len(seen) == 2
    assert seen[0] != seen[1], "the same nonce twice is the collision this fixes"


# ── GW-9/GW-10/GW-12: transports ───────────────────────────────────────────


async def test_the_identity_adapter_keeps_one_http_client() -> None:
    """It built a fresh AsyncClient per call and closed none, so every exchange,
    profile read and refresh leaked a client and its connection pool."""
    identity = GitLabIdentity("https://gitlab.test", "cid", "secret")
    first = await identity._client()
    second = await identity._client()
    assert first is second, "one client, kept"
    await identity.aclose()
    assert identity._http is None


async def test_an_injected_client_is_not_closed_by_the_adapter() -> None:
    """It belongs to whoever passed it in; closing it would shut a pool somebody
    else is still using."""

    class Injected:
        closed = False

        async def aclose(self) -> None:
            self.closed = True

    injected = Injected()
    identity = GitLabIdentity("https://gitlab.test", "cid", "secret", http=injected)
    assert await identity._client() is injected
    await identity.aclose()
    assert not injected.closed


async def test_group_membership_is_read_past_the_first_page() -> None:
    """Roles are mapped from group paths. The call asked for `per_page=100` and
    read one page, so a developer in more than a hundred groups could silently
    lose the group that grants their role — a wrong answer that looks exactly
    like a correct one."""
    from dakcoder_gateway.auth.identity import GROUP_PAGE_SIZE

    pages = {
        1: [{"full_path": f"it-2.0/g{i}"} for i in range(GROUP_PAGE_SIZE)],
        2: [{"full_path": "it-2.0/the-one-that-grants-access"}],
    }

    class Paged:
        def __init__(self) -> None:
            self.requested: list[int] = []

        async def get(self, url, headers=None, params=None):
            if url.endswith("/api/v4/user"):
                return _Response({"id": 7, "username": "asha", "state": "active"})
            page = int((params or {}).get("page", 1))
            self.requested.append(page)
            return _Response(pages.get(page, []))

    http = Paged()
    identity = GitLabIdentity("https://gitlab.test", "cid", "secret", http=http)
    profile = await identity.profile("a-token")

    assert http.requested == [1, 2], "it stops at the first short page"
    assert "it-2.0/the-one-that-grants-access" in profile.groups
    assert len(profile.groups) == GROUP_PAGE_SIZE + 1


async def test_a_group_read_that_never_ends_is_bounded() -> None:
    """An IdP that always returns a full page must not turn a sign-in into an
    unbounded loop."""
    from dakcoder_gateway.auth.identity import GROUP_PAGE_SIZE, MAX_GROUP_PAGES

    class Endless:
        def __init__(self) -> None:
            self.calls = 0

        async def get(self, url, headers=None, params=None):
            if url.endswith("/api/v4/user"):
                return _Response({"id": 7, "username": "asha", "state": "active"})
            self.calls += 1
            page = int((params or {}).get("page", 1))
            return _Response([{"full_path": f"g{page}/{i}"} for i in range(GROUP_PAGE_SIZE)])

    http = Endless()
    identity = GitLabIdentity("https://gitlab.test", "cid", "secret", http=http)
    profile = await identity.profile("a-token")

    assert http.calls == MAX_GROUP_PAGES
    assert len(profile.groups) == GROUP_PAGE_SIZE * MAX_GROUP_PAGES


def test_the_upstream_pool_has_a_ceiling() -> None:
    """Constructing a `Limits` at all replaces httpx's default cap of 100 with
    whatever the object says, and an unset `max_connections` there means `None`
    — no cap. So writing the object to raise the keep-alive ceiling silently
    removed the connection ceiling."""
    import httpx

    from dakcoder_gateway.proxy import MAX_UPSTREAM_CONNECTIONS, ModelProxy

    proxy = ModelProxy("https://upstream.test", "k", quota=None)  # type: ignore[arg-type]
    client = proxy._client()
    assert isinstance(client, httpx.AsyncClient)
    limits = client._transport._pool  # type: ignore[attr-defined]
    assert limits._max_connections == MAX_UPSTREAM_CONNECTIONS
    assert MAX_UPSTREAM_CONNECTIONS is not None


# ── GW-13: the ledger says what it is ──────────────────────────────────────


async def test_the_in_memory_ledger_is_bounded_and_counts_what_it_drops() -> None:
    """It is the fallback every deployment without Postgres runs on, and it grew
    by one dataclass per metered turn until restart."""
    from dakcoder_gateway.ledger import MemoryLedger, UsageEvent

    ledger = MemoryLedger(capacity=3)
    for turn in range(5):
        await ledger.record(
            UsageEvent(
                sub="gitlab:7",
                session_id="s1",
                turn=turn,
                model="m",
                role="coder",
                mode="coder",
                prompt_tokens=1,
                completion_tokens=1,
                billed_tokens=2,
            )
        )

    assert len(ledger.events) == 3
    assert ledger.dropped == 2, "a report built from this is a floor, and can say so"
    assert [e.turn for e in ledger.events] == [2, 3, 4], "the newest are the ones kept"


async def test_a_dropped_ledger_row_is_counted_not_only_swallowed() -> None:
    """The class fails open by design — the quota decision is already enforced —
    but a hole nobody counts is a hole nobody finds."""
    from dakcoder_gateway.ledger import PostgresLedger, UsageEvent

    class Broken:
        def acquire(self):
            raise RuntimeError("the database is not there")

    ledger = PostgresLedger(Broken())
    await ledger.record(
        UsageEvent(sub="gitlab:7", session_id="s1", turn=1, model="m", role="coder", mode="coder",
                   prompt_tokens=1, completion_tokens=1, billed_tokens=2)
    )
    assert ledger.dropped == 1
