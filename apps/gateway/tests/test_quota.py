"""Tests for quota and metering.

The reconciliation tests carry the most weight: they are the ones that pin the
fix for finding S18, and they are the difference between accounting that tracks
reality and accounting that drifts a little further from it every turn.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from dakcoder_gateway.quota import (
    Check,
    Conflict,
    Lane,
    Limits,
    MemoryStore,
    QuotaExceeded,
    QuotaPolicy,
    Series,
    StoreUnavailable,
)

START = datetime(2026, 8, 25, 9, 0, tzinfo=timezone.utc)


class Clock:
    """A hand-wound clock.

    Rolling windows are defined by the passage of time, so testing them against
    the wall clock means either sleeping for an hour or not testing them. This
    is the only way the horizon behaviour gets exercised at all.
    """

    def __init__(self, at: datetime = START) -> None:
        self.at = at

    def __call__(self) -> datetime:
        return self.at

    def advance(self, **kw) -> None:
        self.at += timedelta(**kw)


def policy(clock: Clock, **overrides) -> QuotaPolicy:
    limits = Limits(
        tokens_per_window=10_000,
        runs_per_window=3,
        sessions_per_week=2,
        tokens_per_week=30_000,
        tokens_per_hour=5_000,
        **overrides,
    )
    return QuotaPolicy(MemoryStore(limits), limits, clock=clock)


@pytest.fixture
def clock() -> Clock:
    return Clock()


# ── reserve and reconcile ───────────────────────────────────────────────────


async def test_an_over_reservation_is_refunded(clock: Clock) -> None:
    """Finding S18, as a test.

    The frontend agent reserves a flat 4,096 per call and never refunds, so a
    turn that used 300 tokens is billed 4,096 — and across forty turns the error
    is larger than the usage.
    """
    p = policy(clock)
    await p.start_run("u")
    reservation = await p.reserve("u", 4_000)

    settlement = await p.reconcile(reservation, prompt_tokens=800, completion_tokens=120)

    assert settlement.billed == 920
    assert settlement.refunded == 3_080
    snapshot = await p.snapshot("u")
    assert snapshot.used[str(Series.HOUR_TOKENS)] == 920


async def test_an_under_reservation_is_charged(clock: Clock) -> None:
    """The other direction, and the one easy to leave out.

    The tokens are already spent by the time we know, so refusing would only
    make the counters wrong. The overshoot is charged and bites on the next
    reservation, which is the right place for it.
    """
    p = policy(clock)
    await p.start_run("u")
    reservation = await p.reserve("u", 500)

    settlement = await p.reconcile(reservation, prompt_tokens=2_000, completion_tokens=400)

    assert settlement.billed == 2_400
    assert settlement.refunded == 0
    snapshot = await p.snapshot("u")
    assert snapshot.used[str(Series.HOUR_TOKENS)] == 2_400


async def test_reconciling_twice_is_refused(clock: Clock) -> None:
    """A double settlement would charge the same turn twice, and nothing else
    in the system would notice."""
    p = policy(clock)
    await p.start_run("u")
    reservation = await p.reserve("u", 1_000)
    await p.reconcile(reservation, prompt_tokens=500, completion_tokens=100)

    with pytest.raises(ValueError, match="already reconciled"):
        await p.reconcile(reservation, prompt_tokens=500, completion_tokens=100)


async def test_a_call_that_never_happened_is_released(clock: Clock) -> None:
    """A connection reset before the model saw the request has cost nothing.
    Holding its reservation until the window rolls would make a flaky network
    look like heavy usage."""
    p = policy(clock)
    await p.start_run("u")
    reservation = await p.reserve("u", 3_000)
    assert (await p.snapshot("u")).used[str(Series.HOUR_TOKENS)] == 3_000

    await p.release(reservation)
    assert (await p.snapshot("u")).used[str(Series.HOUR_TOKENS)] == 0


async def test_reasoning_tokens_are_attributed_separately(clock: Clock) -> None:
    """A thinking-on Planner turn can spend more output on reasoning than on the
    plan. If that is invisible, the cost of §4.4's on/off choices becomes
    superstition rather than a measurement."""
    p = policy(clock)
    await p.start_run("u")
    reservation = await p.reserve("u", 4_000)

    settlement = await p.reconcile(
        reservation, prompt_tokens=900, completion_tokens=2_500, reasoning_tokens=2_100
    )
    assert settlement.reasoning_tokens == 2_100
    assert settlement.as_dict()["reasoning_tokens"] == 2_100


# ── the cached-prefill discount ─────────────────────────────────────────────


async def test_cached_tokens_are_billed_in_full_by_default(clock: Clock) -> None:
    """plan.md §9 Q1: the field is absent from this endpoint, so the discount is
    dormant. Defaulting to a discount we cannot verify would under-bill."""
    p = policy(clock)
    await p.start_run("u")
    reservation = await p.reserve("u", 2_000)

    settlement = await p.reconcile(
        reservation, prompt_tokens=1_000, completion_tokens=100, cached_tokens=800
    )
    assert settlement.billed == 1_100


async def test_the_discount_applies_the_day_the_field_appears(clock: Clock) -> None:
    """A config change, not a code change — which is the point of writing it now.

    Discounting cached prefill makes a session with good context discipline go
    further than one without, pointing the quota model at the same behaviour the
    latency work rewards.
    """
    p = policy(clock, cached_discount=0.2)
    await p.start_run("u")
    reservation = await p.reserve("u", 2_000)

    settlement = await p.reconcile(
        reservation, prompt_tokens=1_000, completion_tokens=100, cached_tokens=800
    )
    # 200 fresh + 800 x 0.2 + 100 completion
    assert settlement.billed == 460


# ── the limits ──────────────────────────────────────────────────────────────


async def test_the_hourly_guard_catches_one_pathological_task(clock: Clock) -> None:
    p = policy(clock)
    await p.start_run("u")
    for _ in range(2):
        r = await p.reserve("u", 2_000)
        await p.reconcile(r, prompt_tokens=2_000, completion_tokens=0)

    with pytest.raises(QuotaExceeded) as caught:
        await p.reserve("u", 2_000)
    assert caught.value.check.series is Series.HOUR_TOKENS


async def test_a_rolling_hour_really_rolls(clock: Clock) -> None:
    """The difference between a rolling window and a bucket, and the reason the
    reset time is when the *oldest* event ages out rather than a fixed boundary."""
    p = policy(clock)
    await p.start_run("u")
    r = await p.reserve("u", 4_500)
    await p.reconcile(r, prompt_tokens=4_500, completion_tokens=0)

    with pytest.raises(QuotaExceeded):
        await p.reserve("u", 1_000)

    clock.advance(minutes=61)
    assert await p.reserve("u", 1_000)


async def test_runs_per_window_catches_a_retry_loop(clock: Clock) -> None:
    p = policy(clock)
    for _ in range(3):
        await p.start_run("u")

    with pytest.raises(QuotaExceeded) as caught:
        await p.start_run("u")
    assert caught.value.check.series is Series.WINDOW_RUNS


async def test_opening_a_window_is_itself_metered(clock: Clock) -> None:
    """Otherwise a client that opens and abandons windows gets an unlimited
    number of them, and the weekly reserve means nothing."""
    p = policy(clock)
    await p.start_run("a")
    clock.advance(hours=6)
    await p.start_run("a")
    clock.advance(hours=6)

    with pytest.raises(QuotaExceeded) as caught:
        await p.start_run("a")
    assert caught.value.check.series is Series.WEEK_SESSIONS


async def test_a_window_expires_and_its_counters_reset(clock: Clock) -> None:
    p = policy(clock)
    await p.start_run("u")
    r = await p.reserve("u", 3_000)
    await p.reconcile(r, prompt_tokens=3_000, completion_tokens=0)
    assert (await p.snapshot("u")).used[str(Series.WINDOW_TOKENS)] == 3_000

    clock.advance(hours=6)
    snapshot = await p.snapshot("u")
    assert not snapshot.window_open
    assert snapshot.used[str(Series.WINDOW_TOKENS)] == 0
    # The rolling week does not reset with the window. That is the whole point
    # of having both: a window is a burst allowance, the week is the real cap.
    assert snapshot.used[str(Series.WEEK_TOKENS)] == 3_000


# ── priority lanes ──────────────────────────────────────────────────────────


async def test_background_work_is_shed_before_interactive(clock: Clock) -> None:
    """A nightly audit and a developer at their keyboard have very different
    tolerances for being told to come back later."""
    p = policy(clock)
    await p.start_run("u")

    r = await p.reserve("u", 1_200, lane=Lane.BACKGROUND)
    await p.reconcile(r, prompt_tokens=1_200, completion_tokens=0)

    with pytest.raises(QuotaExceeded) as caught:
        await p.reserve("u", 500, lane=Lane.BACKGROUND)
    assert "shed first" in caught.value.reason

    # The same usage leaves the interactive lane plenty of room.
    assert await p.reserve("u", 500, lane=Lane.INTERACTIVE)


# ── what a refusal says (contract C4) ───────────────────────────────────────


async def test_a_refusal_says_what_was_used_and_what_was_asked(clock: Clock) -> None:
    p = policy(clock)
    await p.start_run("u")
    r = await p.reserve("u", 4_000)
    await p.reconcile(r, prompt_tokens=4_000, completion_tokens=0)

    with pytest.raises(QuotaExceeded) as caught:
        await p.reserve("u", 2_000)

    reason = caught.value.reason
    assert "4,000 of 5,000" in reason
    assert "needs 2,000" in reason
    assert "frees up in" in reason


async def test_a_request_bigger_than_the_limit_says_waiting_will_not_help(
    clock: Clock,
) -> None:
    """Every other refusal is answered by waiting. Telling someone to wait for
    something that will never happen is the worst possible answer — they wait."""
    p = policy(clock)
    await p.start_run("u")

    with pytest.raises(QuotaExceeded) as caught:
        await p.reserve("u", 9_000)

    assert caught.value.impossible
    assert "waiting will not help" in caught.value.reason
    assert caught.value.as_dict()["requested"] == 9_000


async def test_a_refusal_carries_every_header_c4_requires(clock: Clock) -> None:
    p = policy(clock)
    await p.start_run("u")

    with pytest.raises(QuotaExceeded) as caught:
        await p.reserve("u", 99_000)

    headers = caught.value.headers()
    for name in (
        "Retry-After",
        "X-RateLimit-Limit",
        "X-RateLimit-Remaining",
        "X-RateLimit-Reset",
        "X-Quota-Window-Reset",
    ):
        assert name in headers
    assert int(headers["Retry-After"]) >= 1


# ── all-or-nothing ──────────────────────────────────────────────────────────


async def test_a_refused_reservation_consumes_nothing(clock: Clock) -> None:
    """The property that makes retrying safe.

    Without it, a request refused by the weekly cap has already consumed from
    the hourly one — so a client that retries into a wall drains a budget it
    never spent.
    """
    p = policy(clock)
    await p.start_run("u")
    before = (await p.snapshot("u")).used

    for _ in range(5):
        with pytest.raises(QuotaExceeded):
            await p.reserve("u", 99_000)

    assert (await p.snapshot("u")).used == before


async def test_concurrent_reservations_cannot_both_squeeze_in(clock: Clock) -> None:
    """Two turns arriving together must not both read "4,000 used" and proceed.

    This asserts the property at the *policy* level. The store-level proof — that
    the atomicity is real and not an artefact of a single-threaded event loop —
    is in test_quota_store.py, which runs the same assertion against a
    deliberately non-atomic store and requires it to fail.
    """
    p = policy(clock)
    await p.start_run("u")

    async def attempt() -> bool:
        try:
            await p.reserve("u", 3_000)
            return True
        except QuotaExceeded:
            return False

    results = await asyncio.gather(*(attempt() for _ in range(5)))
    assert sum(results) == 1, f"hour limit is 5,000; {sum(results)} reservations of 3,000 got in"


# ── idempotency ─────────────────────────────────────────────────────────────


async def test_a_duplicate_delivery_is_not_charged_twice(clock: Clock) -> None:
    p = policy(clock)
    await p.start_run("u")
    body = {"messages": [{"role": "user", "content": "hi"}]}

    first = await p.reserve("u", 2_000, idempotency_key="k1", body=body)
    second = await p.reserve("u", 2_000, idempotency_key="k1", body=body)

    assert first.id == second.id
    assert (await p.snapshot("u")).used[str(Series.HOUR_TOKENS)] == 2_000


async def test_the_same_key_with_a_different_body_is_a_conflict(clock: Clock) -> None:
    """RFC 8594's rule. Silently replaying the first result for a second,
    different request is worse than either replaying or refusing: the caller
    believes something happened that did not."""
    p = policy(clock)
    await p.start_run("u")
    await p.reserve("u", 1_000, idempotency_key="k1", body={"a": 1})

    with pytest.raises(Conflict):
        await p.reserve("u", 1_000, idempotency_key="k1", body={"a": 2})


async def test_a_conflict_is_not_reported_as_an_outage(clock: Clock) -> None:
    """Turning a 409 into a 503 would tell the caller to retry the one request
    that must not be retried."""
    p = policy(clock)
    await p.start_run("u")
    await p.reserve("u", 1_000, idempotency_key="k1", body={"a": 1})

    with pytest.raises(Conflict):
        await p.reserve("u", 1_000, idempotency_key="k1", body={"a": 2})


# ── fail closed ─────────────────────────────────────────────────────────────


async def test_an_unreachable_store_refuses_rather_than_allows(clock: Clock) -> None:
    """The hole §15.4 closes. An agent that keeps working when quota and audit
    are unavailable is exactly what the proxy exists to prevent."""

    class Broken(MemoryStore):
        async def apply(self, *_a, **_k):
            raise ConnectionError("redis is down")

    limits = Limits()
    p = QuotaPolicy(Broken(limits), limits, clock=clock)

    with pytest.raises(StoreUnavailable):
        await p.reserve("u", 100)


async def test_the_failure_message_says_why_it_refuses(clock: Clock) -> None:
    class Broken(MemoryStore):
        async def usage(self, *_a, **_k):
            raise ConnectionError("redis is down")

    limits = Limits()
    p = QuotaPolicy(Broken(limits), limits, clock=clock)

    with pytest.raises(StoreUnavailable, match="refused rather than allowed unmetered"):
        await p.snapshot("u")


# ── the snapshot the extension renders ──────────────────────────────────────


async def test_the_snapshot_names_the_tightest_limit(clock: Clock) -> None:
    """The status bar has room for one number. Showing the weekly total while
    the hourly guard is about to fire would be worse than showing nothing."""
    p = policy(clock)
    await p.start_run("u")
    r = await p.reserve("u", 4_800)
    await p.reconcile(r, prompt_tokens=4_800, completion_tokens=0)

    name, ratio = (await p.snapshot("u")).tightest
    assert name == str(Series.HOUR_TOKENS)
    assert ratio == pytest.approx(0.96)


async def test_preflight_answers_without_charging(clock: Clock) -> None:
    """Contract C4: the extension pre-flights before starting a run, so a
    developer learns a long task will not fit before watching half of it."""
    p = policy(clock)
    await p.start_run("u")

    assert await p.preflight("u", 4_000)
    assert not await p.preflight("u", 6_000)
    assert (await p.snapshot("u")).used[str(Series.HOUR_TOKENS)] == 0


async def test_the_snapshot_serialises_for_the_wire(clock: Clock) -> None:
    p = policy(clock)
    await p.start_run("u")
    payload = (await p.snapshot("u")).as_dict()

    assert payload["window_open"] is True
    assert payload["window_expires_at"]
    assert "tightest" in payload
    assert set(payload["limits"]) == {str(s) for s in Series}


async def test_limits_are_published_so_the_numbers_are_never_a_guess() -> None:
    """Every number is a placeholder until Qwen capacity is measured (§9 Q3).
    Publishing them at /v1/health is what makes tuning them a config change
    rather than an archaeology exercise."""
    published = Limits().as_dict()
    assert published["tokens_per_hour"] == 600_000
    assert published["cached_discount"] == 1.0
