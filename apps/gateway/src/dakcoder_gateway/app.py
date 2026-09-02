"""The gateway's HTTP surface.

Everything the extension and the local runtime talk to, and nothing else. The
routes are few on purpose: each one is a place where a credential, a quota
decision or an identity claim crosses a boundary, and a surface that grows
casually is one where the next thing to cross is something nobody decided about.

    POST /v1/auth/start      issue a state, build the authorize URL   (C3)
    POST /v1/auth/exchange   code -> session                          (C3)
    POST /v1/auth/refresh    rotate, re-checking the account          (C3)
    GET  /v1/quota           the window snapshot                      (C4)
    GET  /v1/health          capabilities and the limits in force
    GET  /v1/tools           the tool schemas                         (C1)
    POST /v1/llm/{path}      the model proxy                          (§15.4)

**Errors are translated in one place.** Each domain raises its own exception —
``AuthError``, ``QuotaExceeded``, ``ProxyError``, ``StoreUnavailable`` — and the
handlers below are the only code that knows what status code each deserves.
Scattering that decision through the routes is how one path ends up returning
200 with an error body, and clients then treat every response as suspect.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import Depends, FastAPI, Header, Request
from fastapi.responses import JSONResponse, StreamingResponse

from .auth import AuthError, AuthService, Claims
from .ledger import Ledger, MemoryLedger
from .probe import CapabilityProbe
from .proxy import ModelProxy, ProxyError
from .quota import Lane, QuotaExceeded, QuotaPolicy, StoreUnavailable
from .quota.store import Conflict

__all__ = ["Gateway", "create_app"]


class Gateway:
    """The wiring. Constructed once at startup and shared by every request."""

    def __init__(
        self,
        auth: AuthService,
        quota: QuotaPolicy,
        proxy: ModelProxy | None = None,
        *,
        ledger: Ledger | None = None,
        probe: CapabilityProbe | None = None,
        tool_catalog: dict[str, Any] | None = None,
        version: str = "dev",
    ) -> None:
        self.auth = auth
        self.quota = quota
        self.proxy = proxy
        self.ledger = ledger or MemoryLedger()
        self.probe = probe
        self.tool_catalog = tool_catalog or {}
        self.version = version
        #: Filled by the startup probe, so /v1/health answers instantly rather
        #: than making every caller wait on an upstream round trip.
        self.capabilities: dict[str, Any] = {"status": "not probed"}


def create_app(gateway: Gateway) -> FastAPI:
    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        """Nothing to do on the way up; on the way down, let the accounting land.

        Settlement is deliberately not awaited inside the request that caused it
        — see ``ModelProxy._schedule_settlement`` for why awaiting it there looks
        right and does not work — which leaves exactly one window where a turn
        can still be lost: the server stopping while a reconcile is in flight.
        This closes it.
        """
        yield
        if gateway.proxy is not None:
            await gateway.proxy.drain()
            # And the upstream connection pool, after the settlements that may
            # still be using it.
            await gateway.proxy.aclose()

    app = FastAPI(title="dakcoder gateway", version=gateway.version, lifespan=lifespan)
    app.state.gateway = gateway

    # -- error translation --------------------------------------------------

    @app.exception_handler(AuthError)
    async def _auth_error(_request: Request, exc: AuthError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status,
            content={"error": "unauthorized", "reason": str(exc), "retryable": exc.retryable},
        )

    @app.exception_handler(QuotaExceeded)
    async def _quota_error(_request: Request, exc: QuotaExceeded) -> JSONResponse:
        # Contract C4: every 429 carries Retry-After, the rate-limit headers, the
        # window reset, and a one-sentence human reason.
        return JSONResponse(status_code=429, content=exc.as_dict(), headers=exc.headers())

    @app.exception_handler(StoreUnavailable)
    async def _store_error(_request: Request, exc: StoreUnavailable) -> JSONResponse:
        # 503, not 500. The request is refused because it cannot be *metered*,
        # which is a temporary condition of ours rather than a fault in the
        # request — and the distinction tells the client to retry later rather
        # than to change what it sent.
        return JSONResponse(
            status_code=503,
            content={"error": "quota_unavailable", "reason": str(exc)},
            headers={"Retry-After": "30"},
        )

    @app.exception_handler(Conflict)
    async def _conflict(_request: Request, exc: Conflict) -> JSONResponse:
        return JSONResponse(status_code=409, content={"error": "conflict", "reason": str(exc)})

    @app.exception_handler(ProxyError)
    async def _proxy_error(_request: Request, exc: ProxyError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status, content={"error": "upstream", "reason": str(exc)}
        )

    # -- authentication -----------------------------------------------------

    async def caller(
        request: Request, authorization: str | None = Header(default=None)
    ) -> Claims:
        return request.app.state.gateway.auth.verify(authorization)

    # -- identity (C3) ------------------------------------------------------

    @app.post("/v1/auth/start")
    async def auth_start(body: dict[str, Any]) -> dict[str, Any]:
        redirect = str(body.get("redirect_uri", ""))
        challenge = str(body.get("code_challenge", ""))
        if not redirect or not challenge:
            raise AuthError("redirect_uri and code_challenge are both required", status=400)
        return gateway.auth.start(redirect, challenge)

    @app.post("/v1/auth/exchange")
    async def auth_exchange(body: dict[str, Any]) -> dict[str, Any]:
        session = await gateway.auth.exchange(
            code=str(body.get("code", "")),
            code_verifier=str(body.get("code_verifier", "")),
            state=str(body.get("state", "")),
            redirect_uri=str(body.get("redirect_uri", "")),
        )
        payload = session.as_dict()
        payload["quota"] = (await gateway.quota.snapshot(session.profile.sub)).as_dict()
        return payload

    @app.post("/v1/auth/refresh")
    async def auth_refresh(body: dict[str, Any]) -> dict[str, Any]:
        session = await gateway.auth.refresh(str(body.get("refresh_token", "")))
        return session.as_dict()

    # -- quota (C4) ---------------------------------------------------------

    @app.get("/v1/quota")
    async def quota_snapshot(
        claims: Claims = Depends(caller), lane: str = "interactive"
    ) -> dict[str, Any]:
        return (await gateway.quota.snapshot(claims.sub, Lane(lane))).as_dict()

    @app.post("/v1/quota/preflight")
    async def quota_preflight(
        body: dict[str, Any], claims: Claims = Depends(caller)
    ) -> dict[str, Any]:
        """Would a run of this size be admitted? Asked before starting one, so a
        developer learns a long task will not fit before watching half of it."""
        estimated = int(body.get("estimated_tokens", 0))
        lane = Lane(str(body.get("lane", "interactive")))
        ok = await gateway.quota.preflight(claims.sub, estimated, lane)
        return {
            "ok": ok,
            "estimated_tokens": estimated,
            "quota": (await gateway.quota.snapshot(claims.sub, lane)).as_dict(),
        }

    @app.post("/v1/runs")
    async def start_run(
        body: dict[str, Any], claims: Claims = Depends(caller)
    ) -> dict[str, Any]:
        """Open a run, which opens a session window if none is live."""
        lane = Lane(str(body.get("lane", "interactive")))
        window = await gateway.quota.start_run(claims.sub, lane)
        return {
            "window_opened_at": window.opened_at.isoformat(),
            "window_expires_at": window.expires_at.isoformat(),
            "runs_used": window.runs_used,
        }

    # -- introspection ------------------------------------------------------

    @app.get("/v1/health")
    async def health() -> dict[str, Any]:
        """Capabilities and the limits in force.

        The limits are published because every number in §16.1 is a placeholder
        until Qwen capacity is measured. Publishing them makes tuning a config
        change that anyone can verify took effect, rather than something you
        infer from behaviour.
        """
        return {
            "ok": gateway.capabilities.get("ok", False),
            "version": gateway.version,
            "capabilities": gateway.capabilities,
            "limits": gateway.quota.limits.as_dict(),
        }

    @app.get("/v1/tools")
    async def tools() -> dict[str, Any]:
        """Contract C1. The gateway routes against these, the extension renders
        approvals from them, and the model is sent them verbatim."""
        return gateway.tool_catalog

    # -- the model proxy (§15.4) --------------------------------------------

    @app.post("/v1/llm/{path:path}")
    async def llm(
        path: str,
        request: Request,
        claims: Claims = Depends(caller),
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    ) -> StreamingResponse:
        if gateway.proxy is None:
            raise ProxyError("this gateway has no model proxy configured", status=503)

        body = json.loads(await request.body() or b"{}")
        estimated = int(request.headers.get("X-Estimated-Tokens", 0)) or _estimate(body)
        lane = Lane(request.headers.get("X-Lane", "interactive"))

        stream = gateway.proxy.stream(
            path,
            body,
            sub=claims.sub,
            estimated=estimated,
            session_id=request.headers.get("X-Session-Id", ""),
            turn=int(request.headers.get("X-Turn", 0)),
            mode=request.headers.get("X-Mode", "coder"),
            lane=lane,
            idempotency_key=idempotency_key,
        )

        # Pull the first chunk *before* returning a response.
        #
        # Everything that can go wrong before the model produces a byte — quota
        # refused, an unknown role, an unreachable upstream — happens inside this
        # generator. Once StreamingResponse has been returned the status line is
        # already on the wire, and an exception raised then cannot change it:
        # Starlette says "Caught handled exception, but response already
        # started" and the client sees a 200 that stops mid-stream. Priming it
        # here means those failures are still ordinary status codes.
        #
        # After this point the response really has started, so a later failure
        # is reported *in* the stream as a C2 `error` event. That is what the
        # event type is for.
        try:
            first = await stream.__anext__()
        except StopAsyncIteration:
            first = b""

        return StreamingResponse(
            _with_error_events(first, stream),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                # nginx buffers proxied responses by default, which would hold
                # every chunk until the stream ended — turning a streaming
                # endpoint into a slow non-streaming one, silently.
                "X-Accel-Buffering": "no",
            },
        )

    return app


async def _with_error_events(first: bytes, rest) -> Any:
    """Relay the stream, turning a mid-flight failure into a C2 ``error`` event.

    The status code is spent by the time this runs. The choice is between the
    connection dropping — which a client cannot tell from a network fault, and
    will usually retry, doubling the cost of whatever went wrong — and saying so
    in the one channel still open. The envelope has an ``error`` type precisely
    for this, and a client that follows C2 will already render it.
    """
    if first:
        yield first
    try:
        async for chunk in rest:
            yield chunk
    except Exception as exc:  # noqa: BLE001 - the status code is already sent
        payload = json.dumps(
            {"error": type(exc).__name__, "reason": str(exc)[:400]},
            separators=(",", ":"),
        )
        yield f"event: error\ndata: {payload}\n\n".encode()


def _estimate(body: dict[str, Any]) -> int:
    """A fallback reservation when the client did not send one.

    Deliberately generous. The client knows its assembled prompt exactly and
    should send ``X-Estimated-Tokens``; this is for the case where it did not,
    and an under-reservation there would let an unmeasured turn through. Over-
    reserving is refunded on reconcile, which is the whole point of having that
    step.
    """
    text = json.dumps(body.get("messages", []), default=str)
    return len(text) // 3 + int(body.get("max_tokens", 4096))
