"""The usage ledger: append-only, and the system of record.

Part A §16.4 point 6. Redis is the hot path, Postgres is the ledger — and the
split is deliberate in both directions. Redis holds the live counters, so a
flush is a performance event rather than a billing event, because the counters
are rebuildable from here. Postgres holds every turn, so "what did this team
spend on migrations last month" is a query rather than an archaeology exercise.

**Append-only.** No updates, no deletes. A ledger that can be edited is a ledger
whose history is an opinion, and the whole reason for having one is to answer
questions later that nobody thought to ask at the time.

**LiteLLM's spend tables are a cross-check, not the record** (§16.6). They know
nothing about sessions, modes, or task classes, which are exactly the dimensions
that make the numbers actionable — "the Debugger costs four times the Coder" is
a finding; "user 7 spent 2.1M tokens" is a number.

Reasoning tokens get their own column rather than being folded into completion.
A thinking-on Planner turn can spend more output on reasoning than on the plan,
and if that is not separable the cost of §4.4's on/off choices is invisible —
which turns a measurable decision into a matter of taste.
"""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict, deque
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Protocol

__all__ = ["Ledger", "MemoryLedger", "PostgresLedger", "UsageEvent", "SCHEMA"]

log = logging.getLogger(__name__)

#: How many metered turns the in-memory ledger keeps. Roughly a week of a busy
#: team's turns, and a bound rather than a promise: the durable ledger is
#: Postgres, and this one says out loud that it is a window.
MEMORY_LEDGER_CAPACITY = 50_000


@dataclass(frozen=True, slots=True)
class UsageEvent:
    """One metered turn."""

    sub: str
    session_id: str
    turn: int
    model: str
    role: str
    mode: str
    prompt_tokens: int
    completion_tokens: int
    reasoning_tokens: int = 0
    cached_tokens: int = 0
    billed_tokens: int = 0
    estimated_tokens: int = 0
    lane: str = "interactive"
    #: Free-form, for the task class and anything a later question needs.
    task_class: str = ""
    latency_ms: int = 0
    at: datetime = field(default_factory=lambda: datetime.now(tz=timezone.utc))

    def as_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["at"] = self.at.isoformat()
        return data

    @property
    def estimate_error(self) -> float:
        return round(self.estimated_tokens / self.billed_tokens, 3) if self.billed_tokens else 1.0


#: The table. Kept here rather than in a migration directory because the agent
#: never applies DDL and neither does this — a human runs it, and having the
#: statement next to the code that writes the rows is how they stay in step.
SCHEMA = """
CREATE TABLE IF NOT EXISTS usage_events (
    id                BIGSERIAL PRIMARY KEY,
    sub               TEXT        NOT NULL,
    session_id        TEXT        NOT NULL,
    turn              INTEGER     NOT NULL,
    model             TEXT        NOT NULL,
    role              TEXT        NOT NULL,
    mode              TEXT        NOT NULL,
    lane              TEXT        NOT NULL DEFAULT 'interactive',
    task_class        TEXT        NOT NULL DEFAULT '',
    prompt_tokens     INTEGER     NOT NULL,
    completion_tokens INTEGER     NOT NULL,
    reasoning_tokens  INTEGER     NOT NULL DEFAULT 0,
    cached_tokens     INTEGER     NOT NULL DEFAULT 0,
    billed_tokens     INTEGER     NOT NULL DEFAULT 0,
    estimated_tokens  INTEGER     NOT NULL DEFAULT 0,
    latency_ms        INTEGER     NOT NULL DEFAULT 0,
    at                TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- The three questions this table exists to answer, in the order they get asked:
-- what did this person spend, what did this session cost, and which modes are
-- expensive. An index per question, because a ledger nobody can query quickly
-- is a ledger nobody queries.
CREATE INDEX IF NOT EXISTS usage_events_sub_at   ON usage_events (sub, at DESC);
CREATE INDEX IF NOT EXISTS usage_events_session  ON usage_events (session_id, turn);
CREATE INDEX IF NOT EXISTS usage_events_mode_at  ON usage_events (mode, at DESC);
"""


class Ledger(Protocol):
    async def record(self, event: UsageEvent) -> None: ...

    async def totals(self, sub: str, since: datetime) -> dict[str, int]: ...

    async def by_mode(self, sub: str, since: datetime) -> dict[str, int]: ...


class MemoryLedger:
    """The reference, and what the tests use.

    Also what a cold-start rebuild would read from in a single-process
    deployment. Not a stub: the aggregation logic here is the definition the
    SQL has to match.

    **Bounded, and it says so.** This is the fallback a gateway runs with when
    ``DAKCODER_POSTGRES_DSN`` is unset — which is every deployment that has not
    provisioned Postgres yet — and the list it kept was unbounded, so the
    process that calls itself "the system of record" grew by one dataclass per
    metered turn until it was restarted (BUG GW-13). The cap is the honest
    version of what this class actually is: a window over recent turns, held in
    one process's memory, lost on restart. ``dropped`` counts what fell off the
    front, so the reports it answers can say they are incomplete rather than
    quietly under-reporting.
    """

    def __init__(self, capacity: int = MEMORY_LEDGER_CAPACITY) -> None:
        self.capacity = capacity
        self.events: deque[UsageEvent] = deque(maxlen=capacity)
        #: Turns evicted by the cap. Never resets; a non-zero value means the
        #: totals below are a floor, not a total.
        self.dropped = 0
        self._lock = asyncio.Lock()

    async def record(self, event: UsageEvent) -> None:
        async with self._lock:
            if len(self.events) == self.capacity:
                self.dropped += 1
                if self.dropped == 1 or self.dropped % 1000 == 0:
                    log.warning(
                        "the in-memory ledger is full at %d events and has now dropped %d; "
                        "usage history before the most recent %d turns is gone. Set "
                        "DAKCODER_POSTGRES_DSN to keep it.",
                        self.capacity,
                        self.dropped,
                        self.capacity,
                    )
            self.events.append(event)

    async def totals(self, sub: str, since: datetime) -> dict[str, int]:
        rows = [e for e in self.events if e.sub == sub and e.at >= since]
        return {
            "turns": len(rows),
            "prompt_tokens": sum(e.prompt_tokens for e in rows),
            "completion_tokens": sum(e.completion_tokens for e in rows),
            "reasoning_tokens": sum(e.reasoning_tokens for e in rows),
            "cached_tokens": sum(e.cached_tokens for e in rows),
            "billed_tokens": sum(e.billed_tokens for e in rows),
        }

    async def by_mode(self, sub: str, since: datetime) -> dict[str, int]:
        out: dict[str, int] = defaultdict(int)
        for event in self.events:
            if event.sub == sub and event.at >= since:
                out[event.mode] += event.billed_tokens
        return dict(out)


class PostgresLedger:
    """The production ledger, over asyncpg.

    Not exercised by a test on a laptop, and that is stated rather than implied:
    the conformance suite runs against it only when ``DAKCODER_POSTGRES_DSN``
    points at a reachable server, and CI provides one. The same reasoning as the
    Redis store — a database binding that has never spoken to a database is
    untested whatever its unit tests say.

    A failed write is logged and swallowed. That is the one place in this design
    where something fails open, and it is deliberate: the *quota* decision has
    already been made and enforced by the time a row is written, so losing a row
    costs reporting accuracy, while refusing the turn would cost the developer
    their work over a bookkeeping problem. The counters remain correct; only the
    history has a hole, and the hole is logged.
    """

    def __init__(self, pool: Any, *, on_error=None) -> None:
        self.pool = pool
        self._on_error = on_error or (lambda exc, event: None)
        #: Rows the database refused or never saw. The class fails open by
        #: design — see the docstring — and a hole nobody counts is a hole
        #: nobody finds. ``on_error`` is an operator's hook and may be absent;
        #: this is not.
        self.dropped = 0

    async def record(self, event: UsageEvent) -> None:
        try:
            async with self.pool.acquire() as connection:
                await connection.execute(
                    """
                    INSERT INTO usage_events (
                        sub, session_id, turn, model, role, mode, lane, task_class,
                        prompt_tokens, completion_tokens, reasoning_tokens,
                        cached_tokens, billed_tokens, estimated_tokens, latency_ms, at
                    ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16)
                    """,
                    event.sub, event.session_id, event.turn, event.model, event.role,
                    event.mode, event.lane, event.task_class, event.prompt_tokens,
                    event.completion_tokens, event.reasoning_tokens, event.cached_tokens,
                    event.billed_tokens, event.estimated_tokens, event.latency_ms, event.at,
                )
        except Exception as exc:  # noqa: BLE001 - see the class docstring
            self.dropped += 1
            log.error(
                "the usage ledger dropped a turn for %s (session %s, turn %d): %s. "
                "%d row(s) lost so far; quota counters are unaffected.",
                event.sub,
                event.session_id,
                event.turn,
                exc,
                self.dropped,
            )
            self._on_error(exc, event)

    async def totals(self, sub: str, since: datetime) -> dict[str, int]:
        async with self.pool.acquire() as connection:
            row = await connection.fetchrow(
                """
                SELECT count(*)                     AS turns,
                       coalesce(sum(prompt_tokens), 0)     AS prompt_tokens,
                       coalesce(sum(completion_tokens), 0) AS completion_tokens,
                       coalesce(sum(reasoning_tokens), 0)  AS reasoning_tokens,
                       coalesce(sum(cached_tokens), 0)     AS cached_tokens,
                       coalesce(sum(billed_tokens), 0)     AS billed_tokens
                FROM usage_events WHERE sub = $1 AND at >= $2
                """,
                sub, since,
            )
        return {k: int(v) for k, v in dict(row).items()}

    async def by_mode(self, sub: str, since: datetime) -> dict[str, int]:
        async with self.pool.acquire() as connection:
            rows = await connection.fetch(
                """
                SELECT mode, coalesce(sum(billed_tokens), 0) AS billed
                FROM usage_events WHERE sub = $1 AND at >= $2
                GROUP BY mode ORDER BY billed DESC
                """,
                sub, since,
            )
        return {r["mode"]: int(r["billed"]) for r in rows}
