"""The control plane's HTTP API (host-plan §6).

The shapes under ``/v1/sessions`` are the runtime's: those routes are forwarded
to the session's runner, so one generated client serves the extension, a portal
and this. What is new is the workspace around them.

Ownership, as in the runtime, is a dependency and never a lookup in a handler:
``workspace`` for a route with a workspace in its path, ``session_runner`` for
one with a session, ``approval_runner`` for an approval. A test in
``test_agentsvc.py`` walks the route table and fails on
any route that does not use them.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from dakcoder_shared.callers import Authenticator, Caller, Unauthorised
from dakcoder_shared.contract import API_VERSION
from dakcoder_shared.forwarding import Unreachable, Upstream

from .a2a import A2A
from .runners import Runner
from .service import Refused, Service
from .store import Lease

__all__ = ["create_app"]


def _safe(path: str) -> str:
    """A path suffix to forward, or a 404. A `..` segment is a way out of the
    session it was forwarded under."""
    if "\\" in path or any(s in ("", ".", "..") for s in path.split("/")):
        raise Refused(404, "no such route")
    return path


def create_app(
    service: Service,
    *,
    authenticate: Authenticator,
    on_start: Callable[[], Awaitable[None]] | None = None,
) -> FastAPI:
    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        if on_start is not None:
            await on_start()
        yield
        await service.runners.aclose()
        if service.gitlab is not None:
            await service.gitlab.aclose()

    app = FastAPI(title="dakcoder control plane", version=API_VERSION, lifespan=lifespan)
    app.state.service = service
    adapter = A2A(service)

    # -- errors -------------------------------------------------------------

    @app.exception_handler(Refused)
    async def _refused(_request: Request, exc: Refused) -> JSONResponse:
        return JSONResponse(status_code=exc.status, content={"error": exc.detail})

    @app.exception_handler(Unreachable)
    async def _unreachable(_request: Request, exc: Unreachable) -> JSONResponse:
        return JSONResponse(status_code=502, content={"error": str(exc)})

    @app.exception_handler(HTTPException)
    async def _http(_request: Request, exc: HTTPException) -> JSONResponse:
        return JSONResponse(status_code=exc.status_code, content={"error": exc.detail})

    # -- who is calling, and what is theirs ---------------------------------

    def caller(request: Request) -> Caller:
        try:
            who = authenticate(request.headers)
        except Unauthorised as exc:
            raise HTTPException(status_code=401, detail=str(exc)) from None
        if who.local:
            # The control plane is hosted by definition: there is no local
            # developer to be, and an empty `sub` would own nothing.
            raise HTTPException(status_code=401, detail="no caller")
        return who

    def workspace(workspace_id: str, who: Caller = Depends(caller)) -> Lease:
        return service.owned_lease(who.sub, workspace_id)

    async def session_runner(session_id: str, who: Caller = Depends(caller)) -> Runner:
        return await service.session_runner(who.sub, session_id)

    async def approval_runner(approval_id: str, who: Caller = Depends(caller)) -> Runner:
        return await service.approval_runner(who.sub, approval_id)

    async def forward(runner: Runner, request: Request, path: str, sub: str) -> Response:
        upstream = await runner.upstream.open(
            request.method,
            path,
            sub=sub,
            query=request.url.query,
            body=await request.body(),
            headers=request.headers,
        )
        return StreamingResponse(
            Upstream.relay(upstream),
            status_code=upstream.status_code,
            headers=Upstream.response_headers(upstream),
        )

    # -- readiness ----------------------------------------------------------

    @app.get("/v1/health")
    async def health(request: Request) -> dict[str, Any]:
        payload: dict[str, Any] = {"ok": True, "service": "agentsvc", "api_version": API_VERSION}
        try:
            who = authenticate(request.headers)
        except Unauthorised:
            return payload
        if not who.local:
            leases = service.store.leases(who.sub)
            payload["workspaces"] = len(leases)
            payload["runners"] = sum(1 for lease in leases if service.runners.get(lease.id))
        return payload

    # -- workspaces ---------------------------------------------------------

    @app.post("/v1/workspaces", status_code=201)
    async def lease(body: dict[str, Any], who: Caller = Depends(caller)) -> dict[str, Any]:
        leased = await service.lease(who.sub, str(body.get("repo_url") or ""), str(body.get("ref") or ""))
        return leased.public()

    @app.get("/v1/workspaces")
    async def leases(who: Caller = Depends(caller)) -> dict[str, Any]:
        return {"workspaces": [lease.public() for lease in service.store.leases(who.sub)]}

    @app.delete("/v1/workspaces/{workspace_id}")
    async def release(lease: Lease = Depends(workspace)) -> dict[str, Any]:
        await service.release(lease.owner, lease.id)
        return {"released": lease.id}

    @app.post("/v1/workspaces/{workspace_id}/tasks")
    async def start_task(body: dict[str, Any], lease: Lease = Depends(workspace)) -> Response:
        status, session = await service.start_task(lease.owner, lease.id, body)
        return JSONResponse(status_code=status, content=session)

    @app.api_route("/v1/workspaces/{workspace_id}/agenda", methods=["GET", "POST"])
    async def agenda(request: Request, lease: Lease = Depends(workspace)) -> Response:
        runner = await service.runner_for(lease)
        return await forward(runner, request, "v1/agenda", lease.owner)

    @app.post("/v1/workspaces/{workspace_id}/agenda/{task_id}")
    async def agenda_task(task_id: str, request: Request, lease: Lease = Depends(workspace)) -> Response:
        runner = await service.runner_for(lease)
        return await forward(runner, request, f"v1/agenda/{_safe(task_id)}", lease.owner)

    # -- sessions -----------------------------------------------------------

    @app.get("/v1/sessions")
    async def sessions(workspace: str | None = None, who: Caller = Depends(caller)) -> dict[str, Any]:
        return {"sessions": await service.sessions(who.sub, workspace)}

    @app.post("/v1/sessions/{session_id}/deliver")
    async def deliver(
        session_id: str,
        body: dict[str, Any],
        who: Caller = Depends(caller),
        _runner: Runner = Depends(session_runner),
    ) -> dict[str, Any]:
        """Push the session's branch and open or update its merge request (§7.4)."""
        return await service.deliver(
            who.sub,
            session_id,
            title=str(body.get("title") or ""),
            description=str(body.get("description") or ""),
            override=str(body.get("override") or ""),
        )

    @app.api_route("/v1/sessions/{session_id}", methods=["GET", "DELETE"])
    async def session(
        session_id: str,
        request: Request,
        who: Caller = Depends(caller),
        runner: Runner = Depends(session_runner),
    ) -> Response:
        response = await forward(runner, request, f"v1/sessions/{session_id}", who.sub)
        if request.method == "DELETE" and response.status_code == 200:
            await service.forget(who.sub, session_id)
        return response

    @app.api_route("/v1/sessions/{session_id}/{rest:path}", methods=["GET", "POST"])
    async def session_route(
        session_id: str,
        rest: str,
        request: Request,
        who: Caller = Depends(caller),
        runner: Runner = Depends(session_runner),
    ) -> Response:
        return await forward(runner, request, f"v1/sessions/{session_id}/{_safe(rest)}", who.sub)

    # -- approvals ----------------------------------------------------------

    @app.get("/v1/approvals")
    async def approvals(who: Caller = Depends(caller)) -> dict[str, Any]:
        return {"approvals": await service.approvals(who.sub)}

    @app.post("/v1/approvals/{approval_id}")
    async def decide(
        approval_id: str,
        request: Request,
        who: Caller = Depends(caller),
        runner: Runner = Depends(approval_runner),
    ) -> Response:
        return await forward(runner, request, f"v1/approvals/{_safe(approval_id)}", who.sub)

    @app.post("/v1/approvals/{approval_id}/extend")
    async def extend(
        approval_id: str,
        request: Request,
        who: Caller = Depends(caller),
        runner: Runner = Depends(approval_runner),
    ) -> Response:
        return await forward(runner, request, f"v1/approvals/{_safe(approval_id)}/extend", who.sub)

    # -- other agents (§5.4) -------------------------------------------------

    @app.post("/v1/a2a")
    async def a2a(request: Request, who: Caller = Depends(caller)) -> Response:
        return await adapter.handle(who.sub, await request.body())

    return app
