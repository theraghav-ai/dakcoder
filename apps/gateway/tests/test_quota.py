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
    UNLIMITED,
    Check,
    ConfigError,
    Conflict,
    Lane,
    Limits,
    MemoryStore,
    QuotaExceeded,
    QuotaPolicy,
    ScriptContractError,
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


async def test_the_snapshot_carries_the_nested_view_the_extension_renders(clock: Clock) -> None:
    """The status bar reads ``window``/``week``/``hour`` and ``tightest.name``;
    a payload with only the flat counters rendered as a TypeError on every
    refresh. Both envelopes are emitted, and they must agree."""
    p = policy(clock)
    await p.start_run("u")
    clock.advance(minutes=10)
    payload = (await p.snapshot("u")).as_dict()

    window = payload["window"]
    assert window["cap"] == 10_000 and window["runs"] == {"used": 1, "cap": 3}
    assert window["opened_at"] == payload["window_opened_at"]
    assert window["expires_in"] == int(timedelta(hours=5).total_seconds()) - 600
    assert payload["week"] == {"used": 0, "cap": 30_000, "sessions": {"used": 1, "cap": 2}}
    assert payload["hour"] == {"used": 0, "cap": 5_000}

    tightest = payload["tightest"]
    assert tightest["name"] == tightest["limit"] == str(Series.WEEK_SESSIONS)
    assert tightest["pct"] == tightest["used_pct"] == 50.0
    assert (tightest["used"], tightest["cap"]) == (1, 2)

    clock.advance(hours=6)  # the window has lapsed
    assert (await p.snapshot("u")).as_dict()["window"] is None


async def test_limits_are_published_so_the_numbers_are_never_a_guess() -> None:
    """Every number is a placeholder until Qwen capacity is measured (§9 Q3).
    Publishing them at /v1/health is what makes tuning them a config change
    rather than an archaeology exercise."""
    published = Limits().as_dict()
    assert published["tokens_per_hour"] == 600_000
    assert published["cached_discount"] == 1.0


# ── configuration: unmetered now, ceilings later ────────────────────────────


def test_unmetered_limits_never_refuse(clock: Clock) -> None:
    """The configuration to run under while the agent is being tuned."""
    limits = Limits.unmetered()
    assert not limits.any_enforced
    for series in Series:
        assert limits.ceiling(series, Lane.INTERACTIVE) == UNLIMITED
        assert limits.ceiling(series, Lane.BACKGROUND) == UNLIMITED, (
            "a share of nothing is still nothing; background must not be capped "
            "into a hard refusal while interactive runs open"
        )


async def test_an_unmetered_policy_counts_but_never_refuses(clock: Clock) -> None:
    limits = Limits.unmetered()
    p = QuotaPolicy(MemoryStore(limits), limits, clock=clock)

    await p.start_run("u")
    for _ in range(20):
        await p.reserve("u", 5_000_000)

    snap = await p.snapshot("u")
    assert snap.used[str(Series.WINDOW_TOKENS)] == 100_000_000, (
        "an unmetered series must still count — those numbers are what the real "
        "ceiling gets chosen from"
    )


async def test_one_guard_can_stay_on_while_budgets_are_open(clock: Clock) -> None:
    """The mixed configuration the env file documents."""
    limits = Limits.unmetered(runs_per_window=2)
    p = QuotaPolicy(MemoryStore(limits), limits, clock=clock)

    await p.start_run("u")
    await p.start_run("u")
    with pytest.raises(QuotaExceeded) as caught:
        await p.start_run("u")
    assert caught.value.check.series is Series.WINDOW_RUNS

    # ...while the token budgets stayed open throughout.
    await p.reserve("u", 50_000_000)


def test_from_env_reads_ceilings() -> None:
    limits = Limits.from_env(
        {
            "DAKCODER_QUOTA_TOKENS_PER_HOUR": "1_200_000",
            "DAKCODER_QUOTA_RUNS_PER_WINDOW": "80",
            "DAKCODER_QUOTA_WINDOW_HOURS": "3",
        }
    )
    assert limits.tokens_per_hour == 1_200_000
    assert limits.runs_per_window == 80
    assert limits.window == timedelta(hours=3)
    # Unset values keep their defaults rather than becoming zero, which would
    # silently open a ceiling nobody meant to open.
    assert limits.tokens_per_week == Limits().tokens_per_week


def test_enforce_false_opens_every_ceiling() -> None:
    limits = Limits.from_env({"DAKCODER_QUOTA_ENFORCE": "false"})
    assert not limits.any_enforced


def test_an_explicit_value_survives_the_master_switch() -> None:
    """`ENFORCE=false` means "open the budgets", not "ignore what I wrote"."""
    limits = Limits.from_env(
        {"DAKCODER_QUOTA_ENFORCE": "off", "DAKCODER_QUOTA_RUNS_PER_WINDOW": "40"}
    )
    assert limits.runs_per_window == 40
    assert limits.tokens_per_hour == UNLIMITED
    assert limits.any_enforced


def test_off_and_unlimited_are_accepted_as_values() -> None:
    limits = Limits.from_env(
        {"DAKCODER_QUOTA_TOKENS_PER_HOUR": "off", "DAKCODER_QUOTA_TOKENS_PER_WEEK": "unlimited"}
    )
    assert limits.tokens_per_hour == UNLIMITED
    assert limits.tokens_per_week == UNLIMITED


def test_an_unparseable_setting_raises_rather_than_defaulting() -> None:
    """A quota that ignores its configuration is worse than one that will not
    start: the operator believes a limit is in force and learns otherwise from
    the bill."""
    with pytest.raises(ConfigError):
        Limits.from_env({"DAKCODER_QUOTA_TOKENS_PER_HOUR": "600k"})
    with pytest.raises(ConfigError):
        Limits.from_env({"DAKCODER_QUOTA_ENFORCE": "maybe"})


def test_health_says_whether_quota_is_enforced() -> None:
    """A payload of zeroes is ambiguous — unmetered, or misconfigured to refuse
    everything? The one moment somebody reads this is when that matters."""
    published = Limits.unmetered().as_dict()
    assert published["enforced"] is False
    assert str(Series.HOUR_TOKENS) in published["unmetered_series"]

    assert Limits().as_dict()["enforced"] is True
    assert Limits().as_dict()["unmetered_series"] == []


# ── our bugs are not outages ────────────────────────────────────────────────


async def test_a_parsing_bug_is_not_reported_as_an_outage(clock: Clock) -> None:
    """The second half of the gateway incident.

    ``_guarded`` caught every exception and answered StoreUnavailable, so an
    IndexError in our own reply parsing was reported as "Redis is unreachable"
    — a 503, which reads as retryable — for what looked like a whole session.
    The operator saw /v1/quota return 200 while every POST /v1/llm 503'd, which
    is a contradiction that pointed away from the real cause.
    """

    class BrokenStore(MemoryStore):
        async def apply(self, sub, checks, now):
            raise IndexError("list index out of range")

    limits = Limits()
    p = QuotaPolicy(BrokenStore(limits), limits, clock=clock)

    with pytest.raises(IndexError):
        await p.reserve("u", 100)


async def test_an_unreachable_store_still_fails_closed(clock: Clock) -> None:
    """The behaviour that must not have regressed: infrastructure trouble is
    still a refusal, never an unmetered success."""

    class DownStore(MemoryStore):
        async def apply(self, sub, checks, now):
            raise ConnectionError("connection refused")

    limits = Limits()
    p = QuotaPolicy(DownStore(limits), limits, clock=clock)

    with pytest.raises(StoreUnavailable):
        await p.reserve("u", 100)


async def test_a_script_contract_error_is_ours_not_redis(clock: Clock) -> None:
    class DisagreeingStore(MemoryStore):
        async def apply(self, sub, checks, now):
            raise ScriptContractError("the Lua and this parser disagree")

    limits = Limits()
    p = QuotaPolicy(DisagreeingStore(limits), limits, clock=clock)

    with pytest.raises(ScriptContractError):
        await p.reserve("u", 100)


def test_real_redis_failures_are_still_outages_not_bugs() -> None:
    """The guard that keeps the fix from becoming a new bug.

    ``_guarded`` now lets programming errors through as a 500. That is only
    safe while no genuine Redis failure mode is a subclass of one of them — if
    ``ConnectionError`` were, say, an ``OSError`` that also inherited
    ``ValueError``, this change would silently convert every outage from a
    fail-closed 503 into a 500 and take the retry semantics with it.

    Asserted against the real exception hierarchy rather than assumed.
    """
    rex = pytest.importorskip("redis.exceptions")

    for name in (
        "RedisError",
        "ConnectionError",
        "TimeoutError",
        "ResponseError",
        "BusyLoadingError",
        "AuthenticationError",
        "InvalidResponse",
        "NoScriptError",
    ):
        exc = getattr(rex, name, None)
        if exc is None:
            continue
        assert not issubclass(exc, QuotaPolicy._OUR_BUGS), (
            f"redis.exceptions.{name} would be re-raised as a 500 instead of "
            "failing closed with a 503"
        )

    assert not issubclass(asyncio.TimeoutError, QuotaPolicy._OUR_BUGS)
    assert not issubclass(OSError, QuotaPolicy._OUR_BUGS)
