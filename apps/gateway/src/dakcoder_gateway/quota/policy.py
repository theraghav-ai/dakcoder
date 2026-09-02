"""The quota policy: what is checked, when, and what happens afterwards.

The shape of a metered turn:

    start_run()   once per agent run — catches runaway retry loops
    reserve()     before dispatch — an estimate, against every limit at once
    reconcile()   after the usage chunk arrives — replace the estimate

**Reconciliation is the point.** The frontend agent reserves a flat 4,096 tokens
per call and never refunds the difference, so a turn that used 300 tokens is
billed 4,096 and the error compounds across forty turns (finding S18). Here the
reservation is provisional and the endpoint's own ``usage`` figure replaces it —
which is the entire reason ``stream_options: {"include_usage": true}`` is sent on
every call, and why the capability probe treats a missing usage chunk as a
failure rather than a curiosity.

**The reservation is not optional, though.** Checking only afterwards would let a
single enormous turn blow through a window that was nearly full, because nothing
would have stopped it. Reserve high, settle true.

**Failure closes.** If the store is unreachable the answer is no. An agent that
keeps working when quota and audit are unavailable is exactly the hole §15.4
closes, and "fail open on infrastructure trouble" is how a control becomes
advisory without anyone deciding that it should.
"""

from __future__ import annotations

import hashlib
import json
import os
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from .model import Check, Lane, Limits, QuotaExceeded, Series, Snapshot, WindowState
from .store import Applied, Conflict, MemoryStore, QuotaStore, ScriptContractError

__all__ = ["QuotaPolicy", "Reservation", "StoreUnavailable"]


class StoreUnavailable(Exception):
    """The counters could not be reached, so the answer is no."""


#: How long the gateway keeps serving after the quota store stops answering.
#:
#: Failing closed is right for an outage and wrong for a blip, and the gateway
#: could not tell them apart: one Redis hiccup 503'd every request, the client
#: retried three times over six seconds, and every run in the building ended
#: ERROR. To the developer that is indistinguishable from the agent being
#: broken, and it is the one failure they cannot act on.
#:
#: So a bounded grace window. Inside it requests are served and every one of
#: them is recorded as *unmetered* — the audit trail says plainly that these
#: turns were not counted, which is the property §15.4 actually needs. Outside
#: it the refusal returns, because an agent that keeps working indefinitely with
#: quota and audit down is the hole §15.4 closes.
#:
#: **Off by default**, and that is a deliberate retreat from the first draft of
#: this fix. The codebase holds an explicit, documented position -- "an agent
#: that keeps working when quota and audit are down is the hole section 15.4
#: closes" -- and three tests assert it. Serving unmetered turns is an operator's
#: decision about their own billing and audit obligations, not one to make on
#: their behalf while fixing a usability report.
#:
#: What the report actually complains about is legibility: the developer cannot
#: tell a quota outage from a broken agent. That is fixed on the client, which
#: now recognises `quota_unavailable`, backs off on `Retry-After` instead of
#: burning three retries in six seconds, and says what happened.
#:
#: Set `DAKCODER_QUOTA_DEGRADE_SECONDS=60` to trade that invariant for
#: availability during a Redis failover. Every turn served inside the window is
#: marked `degraded` on the reservation and reaches the ledger saying so, so the
#: audit trail records what was not counted rather than simply lacking it.
DEGRADE_SECONDS = float(os.environ.get("DAKCODER_QUOTA_DEGRADE_SECONDS", "0") or 0)


@dataclass(frozen=True, slots=True)
class Reservation:
    """A provisional charge, to be settled by ``reconcile``."""

    id: str
    sub: str
    lane: Lane
    estimated: int
    at: datetime
    #: True when the store was unreachable and this turn was served inside the
    #: grace window. Carried all the way to the ledger row, so "we did not count
    #: this" is a recorded fact rather than an inference from a gap.
    degraded: bool = False


@dataclass(frozen=True, slots=True)
class Settlement:
    """What a turn actually cost, after the endpoint reported."""

    reservation_id: str
    estimated: int
    billed: int
    prompt_tokens: int
    completion_tokens: int
    reasoning_tokens: int = 0
    cached_tokens: int = 0
    refunded: int = 0

    @property
    def estimate_error(self) -> float:
        """How far the reservation was out, as a ratio.

        Surfaced because it is the only honest measure of whether the estimator
        is fit to reserve against. Persistently over 1.5 means reservations are
        blocking turns that would have fitted.
        """
        return round(self.estimated / self.billed, 3) if self.billed else 1.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "reservation": self.reservation_id,
            "estimated": self.estimated,
            "billed": self.billed,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "reasoning_tokens": self.reasoning_tokens,
            "cached_tokens": self.cached_tokens,
            "refunded": self.refunded,
            "estimate_error": self.estimate_error,
        }


def _now() -> datetime:
    return datetime.now(tz=timezone.utc)


class QuotaPolicy:
    """Enforces §16's limits against a store."""

    def __init__(
        self,
        store: QuotaStore | None = None,
        limits: Limits | None = None,
        *,
        clock=_now,
    ) -> None:
        self.limits = limits or Limits()
        self.store = store or MemoryStore(self.limits)
        self._clock = clock
        self._open: dict[str, Reservation] = {}
        #: Settled ids, so a double reconcile is caught rather than charged
        #: twice. Kept here rather than on the frozen Reservation, which the
        #: caller holds and could not be updated in place anyway.
        self._settled: set[str] = set()
        #: When the store was first seen to be down in the current outage, or
        #: None. Reset by any successful store call, so an intermittent store
        #: does not accumulate its blips into an expired grace window.
        self._degraded_since: datetime | None = None

    # -- runs ---------------------------------------------------------------

    async def start_run(self, sub: str, lane: Lane = Lane.INTERACTIVE) -> WindowState:
        """Open a run, opening a session window if none is live.

        Opening a window is itself metered against the weekly session count
        (§16.3). Without that, a client that opens and abandons windows gets an
        unlimited number of them, and the weekly reserve — the limit that
        actually shapes behaviour — means nothing.
        """
        now = self._clock()
        window = await self._guarded(self.store.window(sub, now))

        if window is None:
            await self._apply(
                sub,
                [
                    Check(
                        Series.WEEK_SESSIONS,
                        1,
                        self.limits.ceiling(Series.WEEK_SESSIONS, lane),
                        "sessions this week",
                    )
                ],
                now,
                lane,
            )
            window = await self._guarded(self.store.open_window(sub, now))

        await self._apply(
            sub,
            [
                Check(
                    Series.WINDOW_RUNS,
                    1,
                    self.limits.ceiling(Series.WINDOW_RUNS, lane),
                    "runs in this session window",
                )
            ],
            now,
            lane,
        )
        return await self._guarded(self.store.window(sub, now)) or window

    # -- turns --------------------------------------------------------------

    async def reserve(
        self,
        sub: str,
        estimated: int,
        *,
        lane: Lane = Lane.INTERACTIVE,
        idempotency_key: str | None = None,
        body: Any = None,
    ) -> Reservation:
        """Provisionally charge a turn against every token limit at once.

        All-or-nothing, in the store. A turn refused by the weekly cap must not
        have already consumed from the hourly one — otherwise a client that
        retries into a wall drains a budget it never spent.
        """
        if estimated < 0:
            raise ValueError("a reservation cannot be negative")
        now = self._clock()
        reservation_id = uuid.uuid4().hex

        if idempotency_key is not None:
            # Claim the key with the id we are about to use, atomically, before
            # any counter moves. A duplicate delivery then finds the claim and
            # gets the original reservation back rather than charging twice;
            # the same key with a different body raises Conflict (RFC 8594).
            claimed = await self._guarded(
                self.store.remember(
                    idempotency_key, _hash(body), reservation_id, timedelta(hours=24)
                )
            )
            if claimed is not None:
                existing = self._open.get(claimed)
                if existing is not None:
                    return existing
                # The claim is stale: the original was refused, released, or
                # already settled. Nothing was charged that we would be charging
                # twice, so the honest thing is to treat this as a new request
                # rather than hand back a reservation that no longer exists.

        checks = [
            Check(
                Series.WINDOW_TOKENS,
                estimated,
                self.limits.ceiling(Series.WINDOW_TOKENS, lane),
                "tokens in this session window",
            ),
            Check(
                Series.HOUR_TOKENS,
                estimated,
                self.limits.ceiling(Series.HOUR_TOKENS, lane),
                "tokens in the last hour",
            ),
            Check(
                Series.WEEK_TOKENS,
                estimated,
                self.limits.ceiling(Series.WEEK_TOKENS, lane),
                "tokens this week",
            ),
        ]
        degraded = False
        try:
            await self._apply(sub, checks, now, lane)
        except StoreUnavailable:
            if not self._within_grace(now):
                raise
            degraded = True

        reservation = Reservation(
            id=reservation_id, sub=sub, lane=lane, estimated=estimated, at=now,
            degraded=degraded,
        )
        self._open[reservation.id] = reservation
        return reservation

    async def reconcile(
        self,
        reservation: Reservation,
        *,
        prompt_tokens: int,
        completion_tokens: int,
        reasoning_tokens: int = 0,
        cached_tokens: int = 0,
    ) -> Settlement:
        """Replace the estimate with what the endpoint reported.

        Reasoning tokens are billed and attributed separately. A thinking-on
        Planner turn can spend more output on reasoning than on the plan, and if
        that is invisible the cost of §4.4's on/off choices becomes superstition
        rather than a measurement.
        """
        if reservation.id in self._settled:
            raise ValueError(f"reservation {reservation.id} was already reconciled")

        now = self._clock()
        billed = self._bill(prompt_tokens, completion_tokens, cached_tokens)
        delta = billed - reservation.estimated

        # One signed adjustment, whichever way it went. Positive means the
        # estimate was low and the difference is charged — the tokens are spent,
        # and refusing after the fact would only make the counters wrong; the
        # overshoot bites on the *next* reserve(), which is the right place.
        # Negative is the refund the frontend agent never makes (finding S18).
        for series in (Series.WINDOW_TOKENS, Series.HOUR_TOKENS, Series.WEEK_TOKENS):
            try:
                await self._guarded(self.store.adjust(reservation.sub, series, delta, now))
            except StoreUnavailable:
                # Settling is bookkeeping after the fact; the tokens are already
                # spent. Refusing here would leave the reservation open forever
                # and turn a store blip into a permanent leak of the caller's
                # window. The row still records that it went unmetered.
                if not self._within_grace(now):
                    raise
                break

        self._open.pop(reservation.id, None)
        self._settled.add(reservation.id)

        return Settlement(
            reservation_id=reservation.id,
            estimated=reservation.estimated,
            billed=billed,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            reasoning_tokens=reasoning_tokens,
            cached_tokens=cached_tokens,
            refunded=max(0, -delta),
        )

    async def release(self, reservation: Reservation) -> None:
        """Give back a reservation whose call never happened.

        A dispatch that failed before the model saw it — a connection reset, a
        4xx from the proxy — has cost nothing, and holding its reservation until
        the window rolls would make a flaky network look like heavy usage.
        """
        if reservation.id in self._settled:
            return
        now = self._clock()
        for series in (Series.WINDOW_TOKENS, Series.HOUR_TOKENS, Series.WEEK_TOKENS):
            try:
                await self._guarded(
                    self.store.adjust(reservation.sub, series, -reservation.estimated, now)
                )
            except StoreUnavailable:
                if not self._within_grace(now):
                    raise
                break
        self._open.pop(reservation.id, None)
        self._settled.add(reservation.id)

    def _bill(self, prompt: int, completion: int, cached: int) -> int:
        """What a turn costs, with the cached-prefill discount applied.

        Dormant today: `prompt_tokens_details.cached_tokens` is absent from this
        endpoint (plan.md §9 Q1) so `cached` is 0 and the discount cannot bite.
        Written anyway because the intent is worth keeping visible — discounting
        cached prefill makes a session with good context discipline go further
        than one without, which points the quota model at the same behaviour the
        latency work rewards. The day the field appears this is a config change.
        """
        cached = max(0, min(cached, prompt))
        fresh = prompt - cached
        return max(0, int(fresh + cached * self.limits.cached_discount) + max(0, completion))

    # -- reporting ----------------------------------------------------------

    async def snapshot(self, sub: str, lane: Lane = Lane.INTERACTIVE) -> Snapshot:
        now = self._clock()
        used = await self._guarded(self.store.usage(sub, now))
        window = await self._guarded(self.store.window(sub, now))
        return Snapshot(
            sub=sub,
            window_open=window is not None,
            window_expires_at=window.expires_at if window else None,
            used={str(k): v for k, v in used.items()},
            limits={str(s): self.limits.ceiling(s, lane) for s in Series},
            lane=lane,
            window_opened_at=window.opened_at if window else None,
            at=now,
        )

    async def preflight(self, sub: str, estimated: int, lane: Lane = Lane.INTERACTIVE) -> bool:
        """Whether a turn of this size would be admitted, without charging.

        The extension calls this before starting a run (contract C4), so a
        developer learns that a long task will not fit *before* they watch half
        of it happen.
        """
        now = self._clock()
        used = await self._guarded(self.store.usage(sub, now))
        for series in (Series.WINDOW_TOKENS, Series.HOUR_TOKENS, Series.WEEK_TOKENS):
            if used.get(series, 0) + estimated > self.limits.ceiling(series, lane):
                return False
        return True

    # -- internals ----------------------------------------------------------

    async def _apply(
        self, sub: str, checks: Sequence[Check], now: datetime, lane: Lane
    ) -> Applied:
        applied = await self._guarded(self.store.apply(sub, checks, now))
        if not applied.ok:
            violated = applied.violated
            assert violated is not None
            raise QuotaExceeded(
                violated,
                used=applied.used.get(violated.series, 0),
                reset_at=applied.reset_at.get(violated.series, now),
                now=now,
                lane=lane,
            )
        return applied

    #: Failures that mean "our code is wrong", not "the infrastructure is down".
    #:
    #: These are re-raised as themselves so they surface as a 500 and reach the
    #: logs with a traceback. Folding them into StoreUnavailable is what hid a
    #: parsing bug behind "Redis is unreachable" for an entire session: the
    #: gateway 503'd every metered request while /v1/quota answered 200, because
    #: only the metered path ran the code that was broken. Two contradictory
    #: signals, and the honest one was suppressed.
    #:
    #: A 500 is also the safer answer for the client. 503 reads as retryable, so
    #: a client that trusts it retries a request that can never succeed.
    _OUR_BUGS = (TypeError, KeyError, IndexError, AttributeError, ValueError, AssertionError)

    def _within_grace(self, now: datetime) -> bool:
        """Whether a store outage is still young enough to serve through.

        The clock starts at the first failure of an outage, not at process
        start, so a gateway that has been up for a week still gets its full
        window the first time Redis goes away.
        """
        if DEGRADE_SECONDS <= 0:
            return False
        if self._degraded_since is None:
            self._degraded_since = now
            return True
        return (now - self._degraded_since).total_seconds() <= DEGRADE_SECONDS

    async def _guarded(self, awaitable):
        """Fail closed on infrastructure, fail loud on ourselves.

        Every store call goes through here, so there is exactly one place where
        infrastructure trouble is turned into an answer — and the answer is no.
        Scattering try/except through the policy is how one path ends up
        defaulting to "allow" and nobody notices until the audit.

        "Fail closed" still means a refusal for anything that looks like the
        store being unreachable. What changed is that it no longer means a
        refusal for *every* exception: a programming error inside our own
        parsing is not an outage, and reporting it as one costs the operator the
        only clue they had.
        """
        try:
            result = await awaitable
        except (QuotaExceeded, Conflict):
            # Both are answers, not outages. Turning a 409 into a 503 would tell
            # the caller to retry the one request that must not be retried.
            raise
        except ScriptContractError:
            # Ours, and specifically ours: the store answered, we could not read
            # it. Let it out with its traceback intact.
            raise
        except self._OUR_BUGS:
            # A bug in this process. Requests still fail — nothing is allowed
            # unmetered — but they fail as a 500, which is what an unhandled
            # defect is, and which nobody will mistake for a Redis blip.
            raise
        except Exception as exc:  # noqa: BLE001 - a store failure is a refusal
            raise StoreUnavailable(
                "the quota store is unreachable, so this request cannot be metered. "
                "This is an outage of ours, not a problem with the request or with "
                "your code — retry in a minute. Requests are refused rather than "
                "allowed unmetered: an agent that keeps working when quota and audit "
                "are down is the hole §15.4 closes."
            ) from exc
        # The store answered. Whatever outage was in progress is over, so the
        # next one gets a full grace window of its own.
        self._degraded_since = None
        return result


def _hash(body: Any) -> str:
    if body is None:
        return "none"
    raw = body if isinstance(body, (bytes, bytearray)) else json.dumps(
        body, sort_keys=True, separators=(",", ":"), default=str
    ).encode()
    return hashlib.sha256(raw).hexdigest()
