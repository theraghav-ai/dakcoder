"""The quota model: limits, windows, and what a refusal has to say.

Part A section 16. Two ideas do most of the work.

**Rolling windows, because they can be shown.** A pure per-request rate limit is
invisible until it fires, and then it is indistinguishable from a bug. A window
that opens on the first turn, holds for five hours and refills on a rolling basis
is something a status bar can display and a developer already understands from
Claude Code and Cursor — so the limit stops being a surprise and becomes a
budget they can spend deliberately.

**Reserve, then reconcile.** The frontend agent reserves a flat 4,096 tokens per
call and never refunds the difference, so its accounting drifts further from
reality with every turn (finding S18). Here an estimate is reserved before the
call and *replaced* by the endpoint's own usage figure afterwards. Everything in
this package exists to make that second step possible.

Every number below is a placeholder until Qwen capacity is measured (plan.md
§9 Q3). They are config, they are published at ``/v1/health``, and they are meant
to be tuned after a week of pilot telemetry. Getting the mechanism right matters
more than the initial values — a wrong number is a config change, a wrong
mechanism is a rewrite.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any

__all__ = [
    "Check",
    "Lane",
    "Limits",
    "QuotaExceeded",
    "Series",
    "Snapshot",
    "WindowState",
]


class Lane(StrEnum):
    """Priority lane. Background work is shed before interactive work.

    Not a nicety: a nightly compliance audit and a developer waiting at their
    keyboard have very different tolerances for being told to come back later,
    and without lanes the audit can exhaust the window that the developer needs
    at nine the next morning.
    """

    INTERACTIVE = "interactive"
    BACKGROUND = "background"


class Series(StrEnum):
    """The counted series. One key per series per subject.

    Named rather than ad-hoc strings because these become Redis keyspace, appear
    in the ``/v1/quota`` payload the extension renders, and are the labels on
    every 429 — three consumers who have to agree.
    """

    #: Tokens inside the current session window.
    WINDOW_TOKENS = "window_tokens"
    #: Agent runs inside the current session window.
    WINDOW_RUNS = "window_runs"
    #: Tokens in the last rolling 60 minutes.
    HOUR_TOKENS = "hour_tokens"
    #: Tokens in the last rolling 7 days.
    WEEK_TOKENS = "week_tokens"
    #: Session windows opened in the last rolling 7 days.
    WEEK_SESSIONS = "week_sessions"


@dataclass(frozen=True, slots=True)
class Limits:
    """The configured ceilings. Every value is a placeholder (plan.md §9 Q3)."""

    #: The user-facing unit. Opens on the first turn when none is open.
    window: timedelta = timedelta(hours=5)
    #: Burst protection inside a window.
    tokens_per_window: int = 1_500_000
    #: Catches runaway retry loops.
    runs_per_window: int = 40
    #: The weekly strategic reserve.
    sessions_per_week: int = 12
    #: The real weekly ceiling.
    tokens_per_week: int = 12_000_000
    #: The hourly guard; catches one pathological task.
    tokens_per_hour: int = 600_000

    #: Background work may use at most this share of each token limit, so it is
    #: refused while interactive work still passes. That *is* "shed first":
    #: giving background its own smaller bucket means pressure lands on it
    #: before it reaches anyone waiting.
    background_share: float = 0.25

    #: What a cached prefill token is billed as. 1.0 means no discount, which is
    #: today's answer because `prompt_tokens_details.cached_tokens` is absent
    #: from this endpoint (plan.md §9 Q1). Kept visible while dormant on purpose:
    #: the intent is to make good context discipline go further, which aligns the
    #: quota model with the latency work, and a flag is a one-line change the day
    #: the field appears.
    cached_discount: float = 1.0

    #: Rolling horizons, separated so a test can compress them.
    hour: timedelta = timedelta(hours=1)
    week: timedelta = timedelta(days=7)

    def ceiling(self, series: Series, lane: Lane) -> int:
        base = {
            Series.WINDOW_TOKENS: self.tokens_per_window,
            Series.WINDOW_RUNS: self.runs_per_window,
            Series.HOUR_TOKENS: self.tokens_per_hour,
            Series.WEEK_TOKENS: self.tokens_per_week,
            Series.WEEK_SESSIONS: self.sessions_per_week,
        }[series]
        if lane is Lane.BACKGROUND and series is not Series.WEEK_SESSIONS:
            return int(base * self.background_share)
        return base

    def horizon(self, series: Series) -> timedelta | None:
        """How far back the series counts. ``None`` means the session window."""
        return {
            Series.WINDOW_TOKENS: None,
            Series.WINDOW_RUNS: None,
            Series.HOUR_TOKENS: self.hour,
            Series.WEEK_TOKENS: self.week,
            Series.WEEK_SESSIONS: self.week,
        }[series]

    def as_dict(self) -> dict[str, Any]:
        """Published at /v1/health, so the numbers in force are never a guess."""
        return {
            "window_seconds": int(self.window.total_seconds()),
            "tokens_per_window": self.tokens_per_window,
            "runs_per_window": self.runs_per_window,
            "sessions_per_week": self.sessions_per_week,
            "tokens_per_week": self.tokens_per_week,
            "tokens_per_hour": self.tokens_per_hour,
            "background_share": self.background_share,
            "cached_discount": self.cached_discount,
        }


@dataclass(frozen=True, slots=True)
class Check:
    """One limit this request would touch.

    A request is checked against several at once, and every one of them must be
    applied or none — otherwise a request refused by the weekly cap has already
    consumed from the hourly one, and repeated refusals drain a budget nobody
    spent. That all-or-nothing property is the store's contract, not the
    policy's.
    """

    series: Series
    amount: int
    limit: int
    #: Completed into "you have used X of Y ..." on a refusal.
    label: str


@dataclass(frozen=True, slots=True)
class WindowState:
    """A session window, as the state machine sees it.

    Opening one is itself a metered event (§16.3): the weekly session count is
    the strategic reserve, so a client that opens and abandons windows must not
    get an unlimited number of them.
    """

    opened_at: datetime
    expires_at: datetime
    tokens_used: int = 0
    runs_used: int = 0

    def open_at(self, now: datetime) -> bool:
        return now < self.expires_at

    def remaining(self, now: datetime) -> timedelta:
        return max(self.expires_at - now, timedelta(0))


@dataclass(frozen=True, slots=True)
class Snapshot:
    """What ``GET /v1/quota`` returns and the status bar renders (contract C4)."""

    sub: str
    window_open: bool
    window_expires_at: datetime | None
    used: dict[str, int] = field(default_factory=dict)
    limits: dict[str, int] = field(default_factory=dict)
    lane: Lane = Lane.INTERACTIVE

    @property
    def tightest(self) -> tuple[str, float]:
        """The limit closest to being hit, and how close.

        The status bar has room for one number. Showing the *tightest* one means
        the developer sees the constraint that will actually stop them, rather
        than the weekly total that looks comfortable while the hourly guard is
        about to fire.
        """
        worst, ratio = "", 0.0
        for name, limit in self.limits.items():
            if limit <= 0:
                continue
            share = self.used.get(name, 0) / limit
            if share > ratio:
                worst, ratio = name, share
        return worst, round(ratio, 4)

    def as_dict(self) -> dict[str, Any]:
        name, ratio = self.tightest
        return {
            "sub": self.sub,
            "lane": str(self.lane),
            "window_open": self.window_open,
            "window_expires_at": (
                self.window_expires_at.isoformat() if self.window_expires_at else None
            ),
            "used": dict(self.used),
            "limits": dict(self.limits),
            "tightest": {"limit": name, "used_pct": round(ratio * 100, 1)},
        }


class QuotaExceeded(Exception):
    """A refusal, carrying everything contract C4 requires of a 429.

    The human reason is a field rather than something the caller composes,
    because it is the only part a developer reads. "Rate limit exceeded" tells
    them nothing they can act on; "you have used 612,000 of 600,000 tokens in the
    last hour — this resets at 14:20" tells them whether to wait or to stop.
    """

    def __init__(
        self,
        check: Check,
        *,
        used: int,
        reset_at: datetime,
        now: datetime,
        lane: Lane = Lane.INTERACTIVE,
    ) -> None:
        self.check = check
        self.used = used
        self.reset_at = reset_at
        self.lane = lane
        self.retry_after = max(1, int((reset_at - now).total_seconds()))

        #: True when the request could not fit even in an empty window. Worth
        #: separating: every other refusal is answered by waiting, and telling
        #: someone to wait for something that will never happen is the worst
        #: possible answer — they will wait.
        self.impossible = check.amount > check.limit

        self.reason = (
            f"you have used {used:,} of {check.limit:,} {check.label}, "
            f"and this needs {check.amount:,} more"
        )
        if self.impossible:
            self.reason += (
                f". That is more than the whole {check.limit:,} limit, so waiting will "
                "not help — the request has to be smaller."
            )
        else:
            self.reason += f". Capacity frees up in {_human(reset_at - now)}."
            if lane is Lane.BACKGROUND:
                self.reason += (
                    " Background work has a smaller share so interactive turns keep "
                    "working; it is shed first when a window is under pressure."
                )
        super().__init__(self.reason)

    def headers(self) -> dict[str, str]:
        return {
            "Retry-After": str(self.retry_after),
            "X-RateLimit-Limit": str(self.check.limit),
            "X-RateLimit-Remaining": "0",
            "X-RateLimit-Reset": str(int(self.reset_at.timestamp())),
            "X-Quota-Window-Reset": self.reset_at.isoformat(),
            "X-Quota-Limit-Name": str(self.check.series),
        }

    def as_dict(self) -> dict[str, Any]:
        return {
            "error": "quota_exceeded",
            "limit": str(self.check.series),
            "used": self.used,
            "allowed": self.check.limit,
            "reset_at": self.reset_at.isoformat(),
            "retry_after": self.retry_after,
            "requested": self.check.amount,
            "impossible": self.impossible,
            "reason": self.reason,
        }


def _human(delta: timedelta) -> str:
    seconds = max(0, int(delta.total_seconds()))
    if seconds < 90:
        return f"{seconds} seconds"
    minutes = seconds // 60
    if minutes < 90:
        return f"{minutes} minutes"
    hours = minutes / 60
    if hours < 48:
        return f"{hours:.1f} hours".replace(".0 ", " ")
    return f"{hours / 24:.1f} days".replace(".0 ", " ")
