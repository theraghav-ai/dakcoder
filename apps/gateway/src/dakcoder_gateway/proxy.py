"""The model proxy: ``/v1/llm/*`` (Part A §15.4).

This is the concrete answer to the problem the LiteLLM arrangement makes
unavoidable: **the model API key is a single shared secret**. If the local
runtime held it, every laptop could spend the shared GPU budget with no ceiling
and no attribution, and every limit in §16 would be decorative. So model traffic
goes through here, and the key exists in exactly one process.

    dakcoderd (laptop)          gateway                    LiteLLM
      │ POST /v1/llm/chat/completions   │                        │
      │ Authorization: dakcoder JWT     │ 1. verify JWT          │
      ├────────────────────────────────▶│ 2. reserve quota       │
      │                                 │ 3. attach the real key │
      │                                 ├───────────────────────▶│
      │◀═══ SSE passthrough ════════════│◀═══ SSE ═══════════════│
      │                                 │ 4. tee the usage chunk │
      │                                 │ 5. reconcile + ledger  │

**Streaming is relayed, never buffered.** Buffering to read the usage chunk
first would destroy first-token latency and defeat the entire streaming design —
the developer would wait for the whole answer to arrive before seeing any of it.
So chunks go out as they arrive and the usage chunk is *teed*: copied on its way
past, acted on afterwards.

**The client's ``model`` is never forwarded.** It names a role — "coder",
"planner", "fast" — which is resolved here against the configured set. Passing
it through would let a developer route to a model nobody has budgeted for, on a
shared GPU, with our key attached.

**Fail closed.** If quota cannot be checked, the request does not go. An agent
that keeps working when quota and audit are unavailable is precisely the hole
this section exists to close.
"""

from __future__ import annotations

import json
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

from .ledger import Ledger, MemoryLedger, UsageEvent
from .quota import Lane, QuotaExceeded, QuotaPolicy, Reservation

__all__ = ["ModelProxy", "ProxyError", "RoleModels", "TeedUsage"]

#: Paths the proxy exposes. Not a general-purpose LiteLLM passthrough: anything
#: not listed here is refused, so a new upstream capability cannot become
#: reachable by accident.
ALLOWED_PATHS = frozenset({"chat/completions", "embeddings"})


class ProxyError(Exception):
    def __init__(self, message: str, *, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


@dataclass(frozen=True, slots=True)
class RoleModels:
    """Role → model name. The only models this gateway will ever ask for."""

    models: dict[str, str] = field(
        default_factory=lambda: {
            "planner": "Qwen3.8-27B",
            "coder": "Qwen3.8-27B",
            "verifier": "Qwen3.8-27B",
            "debugger": "Qwen3.8-27B",
            "fast": "Qwen3.8-27B",
            "summariser": "Qwen3.8-27B",
        }
    )

    def resolve(self, role: str) -> str:
        model = self.models.get(role)
        if model is None:
            raise ProxyError(
                f"{role!r} is not a configured role. Available: "
                f"{', '.join(sorted(self.models))}.",
                status=400,
            )
        return model


@dataclass
class TeedUsage:
    """The usage figures pulled out of a passing stream.

    Collected by reading each chunk as it goes by rather than by holding the
    stream. ``saw_usage`` is tracked separately from the numbers because zero
    tokens and no report are very different facts: the first is a turn that cost
    nothing, the second is accounting that has silently stopped working.
    """

    prompt_tokens: int = 0
    completion_tokens: int = 0
    reasoning_tokens: int = 0
    cached_tokens: int = 0
    saw_usage: bool = False
    model: str = ""
    finish_reason: str = ""

    def observe(self, payload: dict[str, Any]) -> None:
        if payload.get("model"):
            self.model = str(payload["model"])
        for choice in payload.get("choices") or []:
            if choice.get("finish_reason"):
                self.finish_reason = str(choice["finish_reason"])

        usage = payload.get("usage")
        if not usage:
            return
        self.saw_usage = True
        self.prompt_tokens = int(usage.get("prompt_tokens") or 0)
        self.completion_tokens = int(usage.get("completion_tokens") or 0)
        details = usage.get("completion_tokens_details") or {}
        self.reasoning_tokens = int(details.get("reasoning_tokens") or 0)
        prompt_details = usage.get("prompt_tokens_details") or {}
        # Absent from this endpoint today (plan.md §9 Q1). Read anyway, so the
        # day it appears the discount has data rather than needing a code change.
        self.cached_tokens = int(prompt_details.get("cached_tokens") or 0)


class ModelProxy:
    """Meters, relays, and settles one model call."""

    def __init__(
        self,
        upstream: str,
        api_key: str,
        quota: QuotaPolicy,
        *,
        ledger: Ledger | None = None,
        roles: RoleModels | None = None,
        http: Any = None,
        timeout: float = 300.0,
    ) -> None:
        if not api_key:
            raise ValueError(
                "the gateway has no model API key. It is the only process that may "
                "hold one (§15.4); without it there is nothing to proxy."
            )
        self.upstream = upstream.rstrip("/")
        self._api_key = api_key
        self.quota = quota
        self.ledger = ledger or MemoryLedger()
        self.roles = roles or RoleModels()
        self.timeout = timeout
        self._http = http

    # -- the request --------------------------------------------------------

    def prepare(self, path: str, body: dict[str, Any], sub: str) -> dict[str, Any]:
        """Shape the upstream request. The only place the real model is named.

        ``user`` is set to the subject so LiteLLM's own spend tables attribute
        correctly even before per-user virtual keys exist (§16.6, phase 1). It
        costs nothing and it means the cross-check is meaningful from day one.
        """
        if path not in ALLOWED_PATHS:
            raise ProxyError(
                f"/v1/llm/{path} is not proxied. This gateway exposes "
                f"{', '.join(sorted(ALLOWED_PATHS))} only.",
                status=404,
            )

        outgoing = dict(body)
        role = str(outgoing.pop("model", "coder"))
        outgoing["model"] = self.roles.resolve(role)
        outgoing["user"] = sub

        if outgoing.get("stream"):
            # Non-negotiable: without the usage chunk there is no accounting, and
            # quota could only be enforced from reservations — which is exactly
            # the frontend agent's failure (S18).
            options = dict(outgoing.get("stream_options") or {})
            options["include_usage"] = True
            outgoing["stream_options"] = options
        return outgoing

    def headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
        }

    # -- the round trip -----------------------------------------------------

    async def stream(
        self,
        path: str,
        body: dict[str, Any],
        *,
        sub: str,
        estimated: int,
        session_id: str = "",
        turn: int = 0,
        mode: str = "coder",
        lane: Lane = Lane.INTERACTIVE,
        idempotency_key: str | None = None,
    ) -> AsyncIterator[bytes]:
        """Reserve, relay, tee, settle.

        The reservation is taken before a byte is sent and released if the call
        never happens. Settlement runs in a ``finally``: a client that
        disconnects mid-stream has still spent whatever the model produced, and
        losing that would make abandoning turns the cheapest way to use the
        service.
        """
        role = str(body.get("model", "coder"))
        outgoing = self.prepare(path, body, sub)
        reservation = await self.quota.reserve(
            sub, estimated, lane=lane, idempotency_key=idempotency_key, body=body
        )

        teed = TeedUsage()
        started = time.monotonic()
        opened = False

        try:
            async for chunk in self._relay(path, outgoing):
                opened = True
                _observe(chunk, teed)
                yield chunk
        except QuotaExceeded:
            raise
        except Exception as exc:  # noqa: BLE001 - upstream trouble, not the caller's
            if not opened:
                # Nothing was produced, so nothing was spent.
                await self.quota.release(reservation)
                raise ProxyError(f"the model endpoint is unavailable: {exc}", status=502) from exc
            raise
        finally:
            if opened:
                await self._settle(
                    reservation, teed, sub, session_id, turn, mode, role, lane, started
                )

    async def _relay(self, path: str, body: dict[str, Any]) -> AsyncIterator[bytes]:
        client = self._http
        if client is None:
            import httpx

            client = httpx.AsyncClient(timeout=self.timeout, trust_env=False, http2=False)

        async with client.stream(
            "POST", f"{self.upstream}/{path}", json=body, headers=self.headers()
        ) as response:
            if response.status_code >= 400:
                raw = await response.aread()
                raise ProxyError(
                    _upstream_message(response.status_code, raw),
                    status=502 if response.status_code >= 500 else response.status_code,
                )
            async for line in response.aiter_lines():
                # Re-terminated rather than passed through as received. httpx
                # strips the newlines that frame an SSE event, and a relay that
                # forgets to put them back produces a stream that parses as one
                # enormous event — which looks like the model hanging.
                yield (line + "\n").encode("utf-8")

    async def _settle(
        self,
        reservation: Reservation,
        teed: TeedUsage,
        sub: str,
        session_id: str,
        turn: int,
        mode: str,
        role: str,
        lane: Lane,
        started: float,
    ) -> None:
        if not teed.saw_usage:
            # No usage chunk. The reservation stands rather than being refunded:
            # a turn that produced output certainly cost something, and refunding
            # what we cannot measure would make a broken endpoint the cheapest
            # way to use the service. The probe's usage_chunk check exists so
            # this is noticed as an endpoint fault rather than absorbed silently.
            teed.prompt_tokens = reservation.estimated
            teed.completion_tokens = 0

        settlement = await self.quota.reconcile(
            reservation,
            prompt_tokens=teed.prompt_tokens,
            completion_tokens=teed.completion_tokens,
            reasoning_tokens=teed.reasoning_tokens,
            cached_tokens=teed.cached_tokens,
        )
        await self.ledger.record(
            UsageEvent(
                sub=sub,
                session_id=session_id,
                turn=turn,
                model=teed.model or self.roles.resolve(role),
                role=role,
                mode=mode,
                lane=str(lane),
                prompt_tokens=teed.prompt_tokens,
                completion_tokens=teed.completion_tokens,
                reasoning_tokens=teed.reasoning_tokens,
                cached_tokens=teed.cached_tokens,
                billed_tokens=settlement.billed,
                estimated_tokens=settlement.estimated,
                latency_ms=int((time.monotonic() - started) * 1000),
            )
        )


def _observe(chunk: bytes, teed: TeedUsage) -> None:
    """Read a passing SSE line without holding it up."""
    text = chunk.decode("utf-8", "replace").strip()
    if not text.startswith("data:"):
        return
    payload = text[5:].strip()
    if not payload or payload == "[DONE]":
        return
    try:
        teed.observe(json.loads(payload))
    except (json.JSONDecodeError, TypeError, ValueError):
        # A chunk we cannot parse is not a reason to break the stream the client
        # is reading. It costs us this chunk's accounting, not the turn.
        return


def _upstream_message(status: int, raw: bytes) -> str:
    """One sentence about an upstream failure, never its body.

    nginx returns an HTML error page and LiteLLM returns JSON that can echo the
    request. Neither belongs in a message that reaches a log, a trace and a
    developer's screen — the first is noise and the second can carry prompt
    content.
    """
    if status == 429:
        return "the model endpoint is rate limiting; this is upstream of our quota"
    if status == 401 or status == 403:
        return "the gateway's model credential was rejected upstream"
    if status >= 500:
        return f"the model endpoint returned {status}"
    try:
        detail = json.loads(raw).get("error", {}).get("message", "")
    except Exception:  # noqa: BLE001
        detail = ""
    return f"the model endpoint rejected the request ({status})" + (f": {detail[:200]}" if detail else "")
