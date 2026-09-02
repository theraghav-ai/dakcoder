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

**A role resolves to a whole route, not just a name.** Model, endpoint and key
all come from ``RoleRouter``, which reads them from the environment, so the
planner can sit on a different model on a different host with a different
credential without a line of this file changing. Every request is metered,
ledgered and quota-checked identically whichever route it takes — the routing
decides where the tokens are bought, never whether they are counted.

**Fail closed.** If quota cannot be checked, the request does not go. An agent
that keeps working when quota and audit are unavailable is precisely the hole
this section exists to close.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

from .ledger import Ledger, MemoryLedger, UsageEvent
from .quota import Lane, QuotaExceeded, QuotaPolicy, Reservation
from .routing import ModelRoute, RoleRouter

__all__ = ["ModelProxy", "ModelRoute", "ProxyError", "RoleRouter", "TeedUsage"]

log = logging.getLogger(__name__)

#: Paths the proxy exposes. Not a general-purpose LiteLLM passthrough: anything
#: not listed here is refused, so a new upstream capability cannot become
#: reachable by accident.
ALLOWED_PATHS = frozenset({"chat/completions", "embeddings"})


class ProxyError(Exception):
    def __init__(self, message: str, *, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


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
        routes: RoleRouter | None = None,
        http: Any = None,
        # Above the agent client's read timeout, so the client is the side that
        # gives up first on a long prefill. Reversed — which is how it shipped,
        # 300 here against 600 there — the gateway cuts a stream whose headers
        # are already sent, and the only way it can report that is an in-band
        # error frame. See `LLMConfig.read_timeout`.
        timeout: float = 600.0,
    ) -> None:
        #: ``upstream`` and ``api_key`` are the single-endpoint form: every role
        #: on one host with one key. A deployment that routes roles differently
        #: passes a ``RoleRouter`` built from the environment instead — see
        #: ``from_routes``, which is what ``deploy/gateway_main.py`` uses.
        self.routes = routes or RoleRouter.uniform(upstream, api_key)
        unpaid = sorted(role for role, route in self.routes.routes.items() if not route.api_key)
        if unpaid:
            raise ValueError(
                f"the gateway has no model API key for {', '.join(unpaid)}. It is the "
                "only process that may hold one (§15.4); without it there is nothing "
                "to proxy."
            )
        self.upstream = self.routes.default.base_url
        self.quota = quota
        self.ledger = ledger or MemoryLedger()
        self.timeout = timeout
        self._http = http
        #: Built lazily and kept, rather than built per request and dropped.
        #:
        #: `_relay` used to construct an `httpx.AsyncClient` inside itself and
        #: never close it, so every model call leaked a connection pool: the
        #: sockets survived until the garbage collector reached them, keep-alive
        #: was never reused, and the gateway paid a fresh TCP and TLS handshake
        #: upstream on every turn of every run.
        self._owned: Any = None
        #: Settlements still in flight. Held as strong references because a task
        #: nobody is holding can be garbage-collected mid-await, and because
        #: ``drain`` needs something to wait on at shutdown.
        self._settling: set[asyncio.Task] = set()

    @classmethod
    def from_routes(cls, routes: RoleRouter, quota: QuotaPolicy, **kwargs: Any) -> ModelProxy:
        """Build from a routing table, which is what a real deployment has."""
        return cls(
            routes.default.base_url, routes.default.api_key, quota, routes=routes, **kwargs
        )

    # -- the request --------------------------------------------------------

    def route_for(self, role: str) -> ModelRoute:
        """Resolve a role, or say what the configured ones are.

        The list is in the error because the alternative is a developer reading
        "not a configured role" and having nowhere to look. Roles are an
        operator's choice now — the set on this gateway is whatever the
        environment declared — so the message has to carry it.
        """
        try:
            return self.routes.resolve(role)
        except KeyError:
            raise ProxyError(
                f"{role!r} is not a configured role. Available: "
                f"{', '.join(sorted(self.routes.routes))}.",
                status=400,
            ) from None

    def prepare(
        self, path: str, body: dict[str, Any], sub: str
    ) -> tuple[dict[str, Any], ModelRoute]:
        """Shape the upstream request, and say where it is going.

        The route comes back with the body because the two are one decision: the
        role names the model *and* the endpoint *and* the key, and a caller that
        could take the body without the route could send it to the wrong one.

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
        route = self.route_for(str(outgoing.pop("model", "coder")))
        outgoing["model"] = route.model
        outgoing["user"] = sub

        if outgoing.get("stream"):
            # Non-negotiable: without the usage chunk there is no accounting, and
            # quota could only be enforced from reservations — which is exactly
            # the frontend agent's failure (S18).
            options = dict(outgoing.get("stream_options") or {})
            options["include_usage"] = True
            outgoing["stream_options"] = options
        return outgoing, route

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
        never happens. Settlement is *scheduled* — on the usage chunk, with the
        ``finally`` as a fallback — and never awaited here: a client that stops
        reading has still spent whatever the model produced, and losing that
        would make abandoning turns the cheapest way to use the service. See
        ``_schedule_settlement`` for why awaiting it in the ``finally`` looks
        right and is not.
        """
        outgoing, route = self.prepare(path, body, sub)
        reservation = await self.quota.reserve(
            sub, estimated, lane=lane, idempotency_key=idempotency_key, body=body
        )

        teed = TeedUsage()
        started = time.monotonic()
        opened = False
        scheduled = False

        try:
            async for chunk in self._relay(path, outgoing, route):
                opened = True
                _observe(chunk, teed)
                if teed.saw_usage and not scheduled:
                    # Settled the moment the numbers exist, not at the end of the
                    # stream. The last thing a client does is stop reading, and a
                    # generator suspended at its final ``yield`` is never resumed:
                    # its ``finally`` runs whenever the object is finalised, which
                    # may be under cancellation, at loop shutdown, or not at all.
                    # The usage chunk arrives one frame before ``[DONE]``, while
                    # this code is still live and being driven — so that is where
                    # the accounting is owed and where it is taken.
                    scheduled = True
                    self._schedule_settlement(
                        reservation, teed, sub, session_id, turn, mode, route, lane, started
                    )
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
            # The fallback, for the ways a stream ends without a usage chunk: an
            # upstream that dropped mid-answer, or one that has stopped reporting
            # usage at all. Both produced tokens, so both are owed a settlement.
            if opened and not scheduled:
                self._schedule_settlement(
                    reservation, teed, sub, session_id, turn, mode, route, lane, started
                )

    # -- settlement ---------------------------------------------------------

    def _schedule_settlement(self, *args: Any) -> None:
        """Hand settlement to a task of the app's own, not this request's.

        This is defect D-1, and the shape of the fix is the whole point. The
        obvious version — ``await self._settle(...)`` right here — reads
        correctly and does not work: a client that closes the response the
        moment it sees ``[DONE]`` makes Starlette cancel the response task, the
        generator's ``finally`` then runs *under cancellation*, and the first
        ``await`` inside it raises ``CancelledError`` immediately. Neither the
        reconcile nor the ledger write completed, so every turn the agent made
        was charged at its estimate for ever and never reached the ledger at
        all. Every proxy test drained the stream to EOF, which is why it passed
        CI for as long as it did.

        A task created here is a sibling, not a child: cancelling the request
        does not touch it. ``drain`` closes the remaining hole, which is the
        process stopping while a settlement is still in flight.
        """
        try:
            task = asyncio.get_running_loop().create_task(self._settle_quietly(*args))
        except RuntimeError:
            # No loop, which means the server is already gone. Nothing can be
            # awaited from here; losing the row is the only option left, and it
            # is one worth a log line rather than a silent drop.
            log.warning("settlement skipped: the event loop is closed")
            return
        self._settling.add(task)
        task.add_done_callback(self._settling.discard)

    async def _settle_quietly(self, *args: Any) -> None:
        """Settle, and never let the failure escape into a bare task.

        An exception in a task nobody awaits surfaces as "Task exception was
        never retrieved" at garbage-collection time, attached to no request and
        with no context. Metering that has stopped working is worth a log line
        that says so.
        """
        try:
            await self._settle(*args)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - a lost row must still be legible
            log.warning("settlement failed: %s", exc)

    async def drain(self, timeout: float = 10.0) -> None:
        """Wait for outstanding settlements. Called at shutdown.

        Without it a process that stops between the last chunk and the ledger
        write loses that turn — the same hole as D-1, just narrower.
        """
        pending = list(self._settling)
        if not pending:
            return
        done, still_running = await asyncio.wait(pending, timeout=timeout)
        if still_running:
            log.warning("%d settlement(s) did not finish before shutdown", len(still_running))

    def _client(self) -> Any:
        """The upstream client: injected for tests, otherwise ours and reused."""
        if self._http is not None:
            return self._http
        if self._owned is None:
            import httpx

            self._owned = httpx.AsyncClient(
                timeout=self.timeout,
                trust_env=False,
                http2=False,
                limits=httpx.Limits(max_keepalive_connections=64, keepalive_expiry=300),
            )
        return self._owned

    async def aclose(self) -> None:
        """Close the upstream client. Called from the app's shutdown hook."""
        owned, self._owned = self._owned, None
        if owned is not None:
            await owned.aclose()

    async def _relay(
        self, path: str, body: dict[str, Any], route: ModelRoute
    ) -> AsyncIterator[bytes]:
        """One client, many endpoints.

        The pool is shared across routes rather than split per endpoint: httpx
        keys keep-alive connections by origin already, so a second upstream gets
        its own connections without a second client — and a second client would
        be a second thing to close, which is how the leak in ``_client`` started.
        """
        client = self._client()

        async with client.stream(
            "POST", route.url(path), json=body, headers=route.headers()
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
        route: ModelRoute,
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
                # What the endpoint said it used, and what we asked for if it
                # said nothing. With roles on different models the two can
                # legitimately differ, and the ledger is the only place that
                # would ever show it.
                model=teed.model or route.model,
                role=route.role,
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
