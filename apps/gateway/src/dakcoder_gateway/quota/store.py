"""Where the counters live: the port, an in-memory reference, and Redis.

The contract is one method, and its whole value is in a single word.

    apply(sub, checks, now) -> Applied     # atomically, all or nothing

**Atomically.** Two turns arriving together must not both read "599,000 used"
and both proceed. **All or nothing** matters just as much and is easier to get
wrong: a request refused by the weekly cap must not have already consumed from
the hourly one, or a client that keeps retrying drains a budget it never spent.
Check-then-consume as two calls has both bugs, which is why the port does not
offer them separately — a store cannot be implemented wrongly here without
noticing.

Two implementations, one conformance suite. ``MemoryStore`` is the reference and
is what the tests run against by default: it is exact, it needs no server, and a
test that cannot run on a laptop is a test that stops being run. ``RedisStore``
is the production one, and the same conformance suite runs against it whenever a
server is reachable — which is the only way to know the Lua script and the
Python agree, since nothing else can tell you that.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Protocol

from .model import Check, Limits, Series, WindowState

__all__ = ["Applied", "MemoryStore", "QuotaStore", "RedisStore"]


@dataclass(frozen=True, slots=True)
class Applied:
    """What the store did, or refused to do."""

    ok: bool
    #: Usage *after* applying, or the current usage when refused.
    used: dict[Series, int] = field(default_factory=dict)
    #: When each series next frees capacity.
    reset_at: dict[Series, datetime] = field(default_factory=dict)
    #: The first check that would have been exceeded. None when ok.
    violated: Check | None = None
    window: WindowState | None = None


class QuotaStore(Protocol):
    """The port. Everything the policy needs and nothing it does not."""

    async def apply(
        self, sub: str, checks: Sequence[Check], now: datetime
    ) -> Applied: ...

    async def usage(self, sub: str, now: datetime) -> dict[Series, int]: ...

    async def window(self, sub: str, now: datetime) -> WindowState | None: ...

    async def open_window(self, sub: str, now: datetime) -> WindowState: ...

    async def adjust(self, sub: str, series: Series, delta: int, now: datetime) -> None: ...

    async def remember(self, key: str, body_hash: str, value: Any, ttl: timedelta) -> Any | None:
        """Idempotency. Returns the stored value for a replay, None for a new key.

        Raises ``Conflict`` when the key is reused with a different body — RFC
        8594's rule, and the one that matters: silently returning the first
        result for a second, different request is worse than either replaying or
        refusing, because the caller believes something happened that did not.
        """
        ...


class Conflict(Exception):
    """An idempotency key reused with a different body."""


class ScriptContractError(RuntimeError):
    """The Lua script and this module disagree about the shape of a reply.

    A bug on our side, never an outage, and separated from every connection
    error for that reason. The distinction is not academic: when the script
    truncated its ``used`` array on a refusal, the resulting IndexError was
    caught as a store failure and answered 503 — retryable, no ``reset_at`` —
    so clients retried a refusal that would never succeed, and the real cause
    (a full five-hour window) never appeared in any response.
    """


# ── the reference implementation ────────────────────────────────────────────


class MemoryStore:
    """Exact, single-process, and the reference the conformance suite defines.

    Sliding series are kept as raw ``(timestamp, amount)`` events rather than as
    bucketed counters. Buckets are what a production store does for space, and
    they make the boundary approximate; here exactness is worth more, because
    this is the implementation the tests use to decide what "correct" means.
    """

    def __init__(self, limits: Limits | None = None) -> None:
        self.limits = limits or Limits()
        self._lock = asyncio.Lock()
        self._events: dict[tuple[str, Series], list[tuple[float, int]]] = {}
        self._windows: dict[str, WindowState] = {}
        self._idem: dict[str, tuple[str, Any, float]] = {}

    # -- the atomic operation ---------------------------------------------

    async def apply(self, sub: str, checks: Sequence[Check], now: datetime) -> Applied:
        async with self._lock:
            self._expire_window(sub, now)
            used = self._usage(sub, now)

            for check in checks:
                # limit <= 0 means unmetered: still counted below, never refused.
                if check.limit <= 0:
                    continue
                if used.get(check.series, 0) + check.amount > check.limit:
                    return Applied(
                        ok=False,
                        used=used,
                        reset_at=self._resets(sub, now),
                        violated=check,
                        window=self._windows.get(sub),
                    )

            for check in checks:
                self._record(sub, check.series, now, check.amount)

            after = self._usage(sub, now)
            return Applied(
                ok=True,
                used=after,
                reset_at=self._resets(sub, now),
                window=self._windows.get(sub),
            )

    async def usage(self, sub: str, now: datetime) -> dict[Series, int]:
        async with self._lock:
            self._expire_window(sub, now)
            return self._usage(sub, now)

    async def window(self, sub: str, now: datetime) -> WindowState | None:
        async with self._lock:
            self._expire_window(sub, now)
            return self._windows.get(sub)

    async def open_window(self, sub: str, now: datetime) -> WindowState:
        async with self._lock:
            state = WindowState(opened_at=now, expires_at=now + self.limits.window)
            self._windows[sub] = state
            # A window's own counters start empty; the rolling series do not.
            self._events.pop((sub, Series.WINDOW_TOKENS), None)
            self._events.pop((sub, Series.WINDOW_RUNS), None)
            return state

    async def adjust(self, sub: str, series: Series, delta: int, now: datetime) -> None:
        """Settle a reservation against reality. Signed, and unconditional.

        Positive charges more, negative gives back. Unconditional because by the
        time this is called the tokens are already spent: refusing here would
        only make the counters wrong, and the overshoot correctly bites on the
        *next* reservation instead.

        Recorded as an event rather than by editing the original. Editing would
        mean finding it, and the reservation being settled is rarely the most
        recent one — turns overlap. An event is exact, commutative, and ages out
        of the sliding window alongside what it settles.
        """
        if delta == 0:
            return
        async with self._lock:
            self._record(sub, series, now, delta)

    async def remember(self, key: str, body_hash: str, value: Any, ttl: timedelta) -> Any | None:
        async with self._lock:
            self._sweep_idem()
            existing = self._idem.get(key)
            if existing is None:
                self._idem[key] = (body_hash, value, time.monotonic() + ttl.total_seconds())
                return None
            stored_hash, stored_value, _ = existing
            if stored_hash != body_hash:
                raise Conflict(
                    f"idempotency key {key!r} was already used with a different request body"
                )
            return stored_value

    # -- internals ---------------------------------------------------------

    def _record(self, sub: str, series: Series, now: datetime, amount: int) -> None:
        if amount == 0:
            return
        bucket = self._events.setdefault((sub, series), [])
        bucket.append((now.timestamp(), amount))
        self._trim(sub, series, now)

        if series is Series.WINDOW_TOKENS and sub in self._windows:
            state = self._windows[sub]
            self._windows[sub] = WindowState(
                state.opened_at, state.expires_at,
                max(0, state.tokens_used + amount), state.runs_used,
            )
        elif series is Series.WINDOW_RUNS and sub in self._windows:
            state = self._windows[sub]
            self._windows[sub] = WindowState(
                state.opened_at, state.expires_at,
                state.tokens_used, max(0, state.runs_used + amount),
            )

    def _trim(self, sub: str, series: Series, now: datetime) -> None:
        horizon = self.limits.horizon(series)
        if horizon is None:
            return
        cutoff = (now - horizon).timestamp()
        bucket = self._events.get((sub, series))
        if bucket:
            self._events[(sub, series)] = [e for e in bucket if e[0] > cutoff]

    def _usage(self, sub: str, now: datetime) -> dict[Series, int]:
        out: dict[Series, int] = {}
        for series in Series:
            horizon = self.limits.horizon(series)
            events = self._events.get((sub, series), [])
            if horizon is None:
                out[series] = max(0, sum(a for _t, a in events))
            else:
                cutoff = (now - horizon).timestamp()
                out[series] = max(0, sum(a for t, a in events if t > cutoff))
        return out

    def _resets(self, sub: str, now: datetime) -> dict[Series, datetime]:
        """When each series next frees capacity.

        For a sliding series that is when its *oldest* event ages out, not the
        end of a fixed period — which is the whole difference between a rolling
        window and a bucket, and the number a developer needs in order to decide
        whether to wait.
        """
        out: dict[Series, datetime] = {}
        for series in Series:
            horizon = self.limits.horizon(series)
            if horizon is None:
                state = self._windows.get(sub)
                out[series] = state.expires_at if state else now
                continue
            events = self._events.get((sub, series), [])
            cutoff = (now - horizon).timestamp()
            live = [t for t, _a in events if t > cutoff]
            oldest = min(live) if live else now.timestamp()
            out[series] = datetime.fromtimestamp(oldest, tz=timezone.utc) + horizon
        return out

    def _expire_window(self, sub: str, now: datetime) -> None:
        state = self._windows.get(sub)
        if state is not None and not state.open_at(now):
            del self._windows[sub]
            self._events.pop((sub, Series.WINDOW_TOKENS), None)
            self._events.pop((sub, Series.WINDOW_RUNS), None)

    def _sweep_idem(self) -> None:
        now = time.monotonic()
        for key in [k for k, (_h, _v, exp) in self._idem.items() if exp <= now]:
            del self._idem[key]


# ── the production implementation ───────────────────────────────────────────


#: One script, one round trip, all-or-nothing. Written as a loop over the checks
#: rather than as five specialised branches: the atomicity is the whole point,
#: and a script with branches is a script where one branch forgets to roll back.
#:
#: Sliding series are ZSETs scored by timestamp — ZREMRANGEBYSCORE to trim,
#: then a sum over the members' amounts. Amounts ride in the member name
#: (`<amount>:<uniq>`) because a ZSET has no value field and a second hash per
#: series would put two keys out of one script's reach.
_APPLY_LUA = """
local now = tonumber(ARGV[1])
local n = tonumber(ARGV[2])
local used = {}
local base = 3
local violated = -1

-- pass one: trim and total every series, refusing before anything is written.
--
-- The loop runs to completion even once a check has failed, and that is not
-- tidiness. Returning from inside it left `used` holding only the entries up to
-- the violated index, while the Python zipped it against the full check list —
-- so the first refusal raised IndexError, _guarded caught it as a store outage,
-- and every metered request 503'd while /v1/quota kept answering 200 because it
-- never calls this script. Totalling everything costs one ZRANGE per series on
-- a path that was already reading them.
for i = 0, n - 1 do
  local key     = ARGV[base + i * 4]
  local horizon = tonumber(ARGV[base + i * 4 + 1])
  local amount  = tonumber(ARGV[base + i * 4 + 2])
  local limit   = tonumber(ARGV[base + i * 4 + 3])

  if horizon > 0 then
    redis.call('ZREMRANGEBYSCORE', key, '-inf', now - horizon)
  end
  local total = 0
  local members = redis.call('ZRANGE', key, 0, -1)
  for _, m in ipairs(members) do
    total = total + tonumber(string.match(m, '^(-?%d+)'))
  end
  if total < 0 then total = 0 end
  used[i + 1] = total

  -- A limit of zero or less means count but never refuse, so an unmetered
  -- series still accumulates and can be read from /v1/quota. That is what
  -- makes an unmetered pilot useful: the numbers to set the real limit from.
  if violated < 0 and limit > 0 and total + amount > limit then
    violated = i
  end
end

if violated >= 0 then
  -- Pass two never runs, so the all-or-nothing property still holds: a request
  -- refused by the weekly cap has consumed nothing from the hourly one.
  return cjson.encode({ok = false, index = violated, used = used})
end

-- pass two: nothing can fail from here, so every write lands or none did
for i = 0, n - 1 do
  local key    = ARGV[base + i * 4]
  local amount = tonumber(ARGV[base + i * 4 + 2])
  if amount ~= 0 then
    redis.call('ZADD', key, now, amount .. ':' .. now .. ':' .. i .. ':' .. redis.sha1hex(key .. now .. i))
    redis.call('EXPIRE', key, 86400 * 8)
    used[i + 1] = used[i + 1] + amount
  end
end

return cjson.encode({ok = true, used = used})
"""


class RedisStore:
    """The production store. Same contract, one Lua script.

    Not covered by a test on a laptop, and that is stated rather than papered
    over: the conformance suite runs against it only when ``DAKCODER_REDIS_URL``
    points at a reachable server, and CI provides one. A Redis binding that has
    never spoken to Redis is an untested binding whatever its unit tests say —
    the failure modes here are all in the script, and a mock of Redis would
    execute the script exactly as correctly as the mock was written.
    """

    def __init__(self, client: Any, limits: Limits | None = None, prefix: str = "q") -> None:
        self.client = client
        self.limits = limits or Limits()
        self.prefix = prefix
        self._script = client.register_script(_APPLY_LUA)

    def key(self, sub: str, series: Series) -> str:
        return f"{self.prefix}:{{{sub}}}:{series}"

    async def apply(self, sub: str, checks: Sequence[Check], now: datetime) -> Applied:
        import json

        argv: list[Any] = [now.timestamp(), len(checks)]
        keys: list[str] = []
        for check in checks:
            horizon = self.limits.horizon(check.series)
            key = self.key(sub, check.series)
            keys.append(key)
            argv += [key, int(horizon.total_seconds()) if horizon else 0, check.amount, check.limit]

        raw = await self._script(keys=keys, args=argv)
        result = json.loads(raw)

        # The script promises one `used` entry per check, on both paths. Assert
        # it rather than trusting it: this exact invariant was broken once, and
        # because the mismatch surfaced as an IndexError inside _guarded it was
        # reported as "Redis is unreachable" for a whole session. A named error
        # says which side is wrong.
        totals = result.get("used") or []
        if len(totals) != len(checks):
            raise ScriptContractError(
                f"the quota script returned {len(totals)} total(s) for "
                f"{len(checks)} check(s); the Lua and this parser disagree"
            )
        used = {c.series: int(totals[i]) for i, c in enumerate(checks)}
        window = await self.window(sub, now)

        if not result["ok"]:
            index = int(result["index"])
            if not 0 <= index < len(checks):
                raise ScriptContractError(
                    f"the quota script reported violated index {index}, "
                    f"outside the {len(checks)} check(s) it was given"
                )
            return Applied(
                ok=False,
                used=used,
                reset_at=await self._resets(sub, now, [c.series for c in checks]),
                violated=checks[index],
                window=window,
            )
        return Applied(
            ok=True,
            used=used,
            reset_at=await self._resets(sub, now, [c.series for c in checks]),
            window=window,
        )

    async def usage(self, sub: str, now: datetime) -> dict[Series, int]:
        out: dict[Series, int] = {}
        for series in Series:
            horizon = self.limits.horizon(series)
            key = self.key(sub, series)
            if horizon is not None:
                await self.client.zremrangebyscore(key, "-inf", now.timestamp() - horizon.total_seconds())
            members = await self.client.zrange(key, 0, -1)
            out[series] = max(0, sum(int(_amount_of(m)) for m in members))
        return out

    async def window(self, sub: str, now: datetime) -> WindowState | None:
        raw = await self.client.hgetall(f"{self.prefix}:{{{sub}}}:window")
        if not raw:
            return None
        data = {_text(k): _text(v) for k, v in raw.items()}
        expires = datetime.fromtimestamp(float(data["expires_at"]), tz=timezone.utc)
        if now >= expires:
            await self.client.delete(f"{self.prefix}:{{{sub}}}:window")
            await self.client.delete(self.key(sub, Series.WINDOW_TOKENS))
            await self.client.delete(self.key(sub, Series.WINDOW_RUNS))
            return None
        usage = await self.usage(sub, now)
        return WindowState(
            opened_at=datetime.fromtimestamp(float(data["opened_at"]), tz=timezone.utc),
            expires_at=expires,
            tokens_used=usage[Series.WINDOW_TOKENS],
            runs_used=usage[Series.WINDOW_RUNS],
        )

    async def open_window(self, sub: str, now: datetime) -> WindowState:
        expires = now + self.limits.window
        key = f"{self.prefix}:{{{sub}}}:window"
        await self.client.hset(
            key, mapping={"opened_at": now.timestamp(), "expires_at": expires.timestamp()}
        )
        await self.client.expire(key, int(self.limits.window.total_seconds()) + 60)
        await self.client.delete(self.key(sub, Series.WINDOW_TOKENS))
        await self.client.delete(self.key(sub, Series.WINDOW_RUNS))
        return WindowState(opened_at=now, expires_at=expires)

    async def adjust(self, sub: str, series: Series, delta: int, now: datetime) -> None:
        if delta == 0:
            return
        key = self.key(sub, series)
        await self.client.zadd(
            key, {f"{delta}:{now.timestamp()}:adjust:{uuid.uuid4().hex}": now.timestamp()}
        )

    async def remember(self, key: str, body_hash: str, value: Any, ttl: timedelta) -> Any | None:
        import json

        full = f"{self.prefix}:idem:{key}"
        stored = await self.client.get(full)
        if stored is None:
            await self.client.set(
                full,
                json.dumps({"hash": body_hash, "value": value}),
                ex=int(ttl.total_seconds()),
                nx=True,
            )
            return None
        record = json.loads(_text(stored))
        if record["hash"] != body_hash:
            raise Conflict(
                f"idempotency key {key!r} was already used with a different request body"
            )
        return record["value"]

    async def _resets(
        self, sub: str, now: datetime, series: Sequence[Series]
    ) -> dict[Series, datetime]:
        out: dict[Series, datetime] = {}
        for name in series:
            horizon = self.limits.horizon(name)
            if horizon is None:
                state = await self.window(sub, now)
                out[name] = state.expires_at if state else now
                continue
            oldest = await self.client.zrange(self.key(sub, name), 0, 0, withscores=True)
            score = oldest[0][1] if oldest else now.timestamp()
            out[name] = datetime.fromtimestamp(score, tz=timezone.utc) + horizon
        return out


def _amount_of(member: Any) -> str:
    text = _text(member)
    return text.split(":", 1)[0]


def _text(value: Any) -> str:
    return value.decode() if isinstance(value, (bytes, bytearray)) else str(value)
