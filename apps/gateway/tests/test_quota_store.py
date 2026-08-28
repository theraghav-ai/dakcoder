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


# ── the refusal path reports every series, not just the ones before the fault ─


async def test_a_refusal_reports_usage_for_every_check(store) -> None:
    """The bug that took the gateway down for a session.

    The Lua returned as soon as a check failed, so ``used`` held only the
    entries up to the violated index while the Python zipped it against the
    full check list. The first refusal therefore raised IndexError inside the
    policy's guard, which reported it as "the quota store is unreachable" — a
    503, which reads as retryable — so clients retried a refusal that could
    never succeed, and the real cause never reached a response.

    The violated check is deliberately *not* first: with it first the array
    happened to be long enough and the bug hid.
    """
    await store.apply("u", [hour(4_500)], NOW)

    checks = [
        Check(Series.WEEK_TOKENS, 100, 20_000, "tokens this week"),  # passes
        hour(1_000),                                                  # violated
        Check(Series.WINDOW_TOKENS, 100, 10_000, "tokens in this window"),
    ]
    result = await store.apply("u", checks, NOW)

    assert not result.ok
    assert result.violated is not None
    assert result.violated.series is Series.HOUR_TOKENS
    # Every series the caller asked about is accounted for, including the ones
    # after the violation. This is what the policy indexes to build the 429.
    for check in checks:
        assert check.series in result.used, f"{check.series} missing from used"
    assert result.used[Series.HOUR_TOKENS] == 4_500


async def test_a_refusal_after_the_fault_still_writes_nothing(store) -> None:
    """Totalling every check must not have turned pass one into a write path."""
    await store.apply("u", [hour(4_500)], NOW)
    before = await store.usage("u", NOW)

    await store.apply(
        "u",
        [
            Check(Series.WEEK_TOKENS, 100, 20_000, "tokens this week"),
            hour(1_000),
        ],
        NOW,
    )

    assert await store.usage("u", NOW) == before


# ── unmetered series: counted, never refused ────────────────────────────────


async def test_a_zero_limit_counts_without_refusing(store) -> None:
    """0 means unmetered, and unmetered still counts.

    Not a detail: running a pilot unmetered is only useful if the counters run,
    because the numbers recorded are what the real ceiling gets chosen from.
    """
    for _ in range(5):
        result = await store.apply("u", [hour(1_000_000, limit=0)], NOW)
        assert result.ok, "a zero limit must never refuse"

    assert (await store.usage("u", NOW))[Series.HOUR_TOKENS] == 5_000_000


async def test_an_unmetered_series_does_not_mask_a_metered_one(store) -> None:
    """Mixed configuration: one ceiling open, another in force."""
    await store.apply("u", [hour(4_500)], NOW)

    result = await store.apply(
        "u",
        [
            Check(Series.WEEK_TOKENS, 5_000_000, 0, "tokens this week"),  # unmetered
            hour(1_000),                                                   # enforced
        ],
        NOW,
    )
    assert not result.ok
    assert result.violated is not None
    assert result.violated.series is Series.HOUR_TOKENS


# ── a structural guard on the script itself ─────────────────────────────────


def test_pass_one_of_the_script_has_no_early_return() -> None:
    """The shape that caused the incident, asserted directly.

    This is a lint on our own source, not a behavioural test — the behavioural
    ones above need a real Redis and skip without one, which is exactly how the
    bug reached production in the first place. It proves only that the totalling
    loop cannot exit before it has filled ``used`` for every check, which is the
    one property whose absence broke the contract the Python parser relies on.

    Kept because it runs everywhere, in milliseconds, with no server.
    """
    from dakcoder_gateway.quota.store import _APPLY_LUA

    head, marker, tail = _APPLY_LUA.partition("-- pass two")
    assert marker, "the script no longer marks where pass two begins"

    loop_start = head.index("for i = 0, n - 1 do")
    loop_body = head[loop_start:]
    # The single permitted return is the refusal, and it sits *after* the loop.
    guarded_return = loop_body.rindex("return cjson.encode({ok = false")
    loop_end = loop_body.index("\nend\n")
    assert guarded_return > loop_end, (
        "pass one returns from inside the totalling loop. That truncates `used` "
        "to the entries before the violated check, and the Python zips it "
        "against the full check list — an IndexError reported as a Redis outage."
    )
    assert "return" not in loop_body[:loop_end], "no early exit from the totalling loop"


def test_the_script_treats_a_zero_limit_as_unmetered() -> None:
    """Counting must not be conditional on enforcement."""
    from dakcoder_gateway.quota.store import _APPLY_LUA

    assert "limit > 0 and total + amount > limit" in _APPLY_LUA, (
        "a zero limit must skip the refusal check while still totalling"
    )
