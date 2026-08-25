"""The store conformance suite.

Every implementation of ``QuotaStore`` must pass this, and the suite runs against
each one that is reachable: ``MemoryStore`` always, ``RedisStore`` when
``DAKCODER_REDIS_URL`` points at a server. That is the only way to know the Lua
script and the Python agree — a mock of Redis would execute the script exactly as
correctly as the mock was written, which is to say not at all.

The suite includes a **deliberately wrong store**, and it is the most important
thing here. A conformance suite that only ever sees correct implementations
proves nothing about itself: it might be asserting things that are true of any
code at all. ``NaiveStore`` checks and then writes with a yield in between, which
is precisely the bug the Lua script exists to prevent — and the suite has to
catch it, or the suite is decoration.
"""

from __future__ import annotations

import asyncio
import os
from datetime import datetime, timedelta, timezone

import pytest

from dakcoder_gateway.quota import Check, Conflict, Limits, MemoryStore, Series
from dakcoder_gateway.quota.store import Applied

NOW = datetime(2026, 8, 25, 9, 0, tzinfo=timezone.utc)
LIMITS = Limits(tokens_per_hour=5_000, tokens_per_week=20_000, tokens_per_window=10_000)


def hour(amount: int, limit: int = 5_000) -> Check:
    return Check(Series.HOUR_TOKENS, amount, limit, "tokens in the last hour")


# ── the implementations under test ──────────────────────────────────────────


class NaiveStore(MemoryStore):
    """Check, yield, write — the bug this whole design is shaped around.

    Present so the conformance suite can be shown to have teeth. It is not a
    strawman: it is what "check the limit, then consume" looks like when the two
    steps are separate calls, which is how most rate limiters are first written
    and how they stay until someone notices the counters do not add up.
    """

    async def apply(self, sub, checks, now) -> Applied:
        used = await self.usage(sub, now)
        for check in checks:
            if used.get(check.series, 0) + check.amount > check.limit:
                return Applied(ok=False, used=used, violated=check)

        await asyncio.sleep(0)  # the network round trip, in miniature

        for check in checks:
            await self.adjust(sub, check.series, check.amount, now)
        return Applied(ok=True, used=await self.usage(sub, now))


async def _redis_store():
    url = os.environ.get("DAKCODER_REDIS_URL")
    if not url:
        return None
    try:
        import redis.asyncio as aioredis

        from dakcoder_gateway.quota import RedisStore

        client = aioredis.from_url(url)
        await client.ping()
        await client.flushdb()
        return RedisStore(client, LIMITS, prefix="test")
    except Exception:  # noqa: BLE001 - an unreachable server is a skip, not a failure
        return None


@pytest.fixture(params=["memory", "redis"])
async def store(request):
    if request.param == "memory":
        yield MemoryStore(LIMITS)
        return
    live = await _redis_store()
    if live is None:
        pytest.skip("DAKCODER_REDIS_URL is not set or the server is unreachable")
    yield live
    await live.client.aclose()


# ── the contract ────────────────────────────────────────────────────────────


async def test_a_permitted_request_is_applied(store) -> None:
    result = await store.apply("u", [hour(1_000)], NOW)
    assert result.ok
    assert (await store.usage("u", NOW))[Series.HOUR_TOKENS] == 1_000


async def test_a_request_over_the_limit_is_refused(store) -> None:
    await store.apply("u", [hour(4_500)], NOW)
    result = await store.apply("u", [hour(1_000)], NOW)

    assert not result.ok
    assert result.violated is not None
    assert result.violated.series is Series.HOUR_TOKENS


async def test_a_refusal_writes_nothing(store) -> None:
    """All-or-nothing. A request refused by the weekly cap must not have already
    consumed from the hourly one, or a client retrying into a wall drains a
    budget it never spent."""
    await store.apply(
        "u",
        [hour(1_000), Check(Series.WEEK_TOKENS, 1_000, 20_000, "tokens this week")],
        NOW,
    )

    refused = await store.apply(
        "u",
        [
            hour(1_000),
            Check(Series.WEEK_TOKENS, 99_000, 20_000, "tokens this week"),
        ],
        NOW,
    )
    assert not refused.ok
    assert refused.violated.series is Series.WEEK_TOKENS

    usage = await store.usage("u", NOW)
    assert usage[Series.HOUR_TOKENS] == 1_000, "the hourly series was charged for a refusal"
    assert usage[Series.WEEK_TOKENS] == 1_000


async def test_concurrent_requests_cannot_both_take_the_last_slot(store) -> None:
    """The property the Lua script exists for, and the one a naive store fails.

    Five requests of 3,000 against a 5,000 ceiling: exactly one may succeed. Any
    other answer means two callers read the same "used" figure and both acted on
    it.
    """
    results = await asyncio.gather(
        *(store.apply("u", [hour(3_000)], NOW) for _ in range(5))
    )
    admitted = sum(1 for r in results if r.ok)
    assert admitted == 1, f"{admitted} of five 3,000-token requests got past a 5,000 limit"
    assert (await store.usage("u", NOW))[Series.HOUR_TOKENS] == 3_000


async def test_the_suite_catches_a_store_that_checks_then_writes() -> None:
    """The test that gives the previous one meaning.

    Without this, a conformance suite that passes tells you nothing: it might be
    asserting things true of any code at all. This shows the atomicity assertion
    fails on the implementation it is meant to reject.
    """
    naive = NaiveStore(LIMITS)
    results = await asyncio.gather(
        *(naive.apply("u", [hour(3_000)], NOW) for _ in range(5))
    )
    admitted = sum(1 for r in results if r.ok)

    assert admitted > 1, (
        "NaiveStore was expected to admit several concurrent requests. If it no "
        "longer does, the concurrency test above has stopped proving anything."
    )


async def test_a_sliding_series_ages_out(store) -> None:
    await store.apply("u", [hour(4_000)], NOW)
    assert (await store.usage("u", NOW))[Series.HOUR_TOKENS] == 4_000

    later = NOW + timedelta(minutes=61)
    assert (await store.usage("u", later))[Series.HOUR_TOKENS] == 0


async def test_an_adjustment_settles_in_both_directions(store) -> None:
    await store.apply("u", [hour(4_000)], NOW)

    await store.adjust("u", Series.HOUR_TOKENS, -3_000, NOW)
    assert (await store.usage("u", NOW))[Series.HOUR_TOKENS] == 1_000

    await store.adjust("u", Series.HOUR_TOKENS, 500, NOW)
    assert (await store.usage("u", NOW))[Series.HOUR_TOKENS] == 1_500


async def test_usage_never_reads_negative(store) -> None:
    """Refunds can outrun charges when a window rolls between the two. A
    negative counter would silently hand out free capacity."""
    await store.adjust("u", Series.HOUR_TOKENS, -5_000, NOW)
    assert (await store.usage("u", NOW))[Series.HOUR_TOKENS] == 0


async def test_a_window_opens_expires_and_clears_its_own_counters(store) -> None:
    window = await store.open_window("u", NOW)
    assert window.expires_at > NOW

    await store.apply("u", [Check(Series.WINDOW_TOKENS, 500, 10_000, "tokens")], NOW)
    assert (await store.usage("u", NOW))[Series.WINDOW_TOKENS] == 500

    after = NOW + LIMITS.window + timedelta(minutes=1)
    assert await store.window("u", after) is None
    assert (await store.usage("u", after))[Series.WINDOW_TOKENS] == 0


async def test_subjects_are_isolated(store) -> None:
    await store.apply("a", [hour(4_000)], NOW)
    assert (await store.usage("b", NOW))[Series.HOUR_TOKENS] == 0
    assert (await store.apply("b", [hour(4_000)], NOW)).ok


# ── idempotency ─────────────────────────────────────────────────────────────


async def test_a_new_key_is_claimed_and_a_repeat_replays(store) -> None:
    assert await store.remember("k", "hash-1", "value-1", timedelta(minutes=5)) is None
    assert await store.remember("k", "hash-1", "value-2", timedelta(minutes=5)) == "value-1"


async def test_the_same_key_with_a_different_body_conflicts(store) -> None:
    await store.remember("k", "hash-1", "value-1", timedelta(minutes=5))
    with pytest.raises(Conflict):
        await store.remember("k", "hash-2", "value-2", timedelta(minutes=5))
