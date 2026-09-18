"""Other agents calling dakcoder, over A2A's JSON-RPC (host-plan §5.4).

An adapter, not a second implementation: every method is a translation onto
the same service layer the REST API uses, and every run is an ordinary session
on an ordinary lease, visible to the caller through ``/v1/sessions`` like any
other.

    message/send     start a run (or, with a taskId, send it a follow-up)
    message/stream   the same, answered as a stream of task updates
    tasks/get        a run's state
    tasks/cancel     stop a run

A task is a session; its ``contextId`` is the workspace it runs on. Which
workspace is the caller's to say, in the message's ``metadata``: a
``workspace_id`` they already hold, or a ``repo_url`` (and ``ref``) to lease,
reusing a lease of the same repository if they have one. ``skill`` picks one of
the card's skills; ``approval_policy`` defaults to ``auto_safe``, because an
agent has nobody to answer an approval (§10).

Delivery is not an A2A method. A finished task says which branch holds its
changes, and ``POST /v1/sessions/{id}/deliver`` is the caller's decision, as it
is for a person (§7.4).
"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from typing import Any

from fastapi.responses import JSONResponse, Response, StreamingResponse

from dakcoder_shared.contract.card import SKILL_INTENTS

from .service import Refused, Service

__all__ = ["A2A"]

# JSON-RPC's own codes, and A2A's.
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603
TASK_NOT_FOUND = -32001
TASK_NOT_CANCELABLE = -32002

#: Session status -> A2A task state.
STATES = {
    "running": "working",
    "done": "completed",
    "aborted": "canceled",
    "error": "failed",
    "no_progress": "failed",
    "exhausted": "failed",
    "unverified": "failed",
}
TERMINAL = {"completed", "canceled", "failed", "rejected"}


class RpcError(Exception):
    def __init__(self, code: int, message: str, data: Any = None) -> None:
        super().__init__(message)
        self.code, self.message, self.data = code, message, data


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _agent_message(text: str, task_id: str, context_id: str) -> dict[str, Any]:
    return {
        "kind": "message",
        "role": "agent",
        "messageId": uuid.uuid4().hex,
        "taskId": task_id,
        "contextId": context_id,
        "parts": [{"kind": "text", "text": text}],
    }


class A2A:
    def __init__(self, service: Service, *, blocking_wait: float = 55.0) -> None:
        self.service = service
        self.blocking_wait = blocking_wait

    # -- the envelope -------------------------------------------------------

    async def handle(self, sub: str, raw: bytes) -> Response:
        request_id: Any = None
        try:
            try:
                request = json.loads(raw or b"")
            except ValueError:
                raise RpcError(PARSE_ERROR, "the request is not JSON") from None
            if not isinstance(request, dict) or request.get("jsonrpc") != "2.0":
                raise RpcError(INVALID_REQUEST, "not a JSON-RPC 2.0 request")
            request_id = request.get("id")
            method = request.get("method")
            params = request.get("params") or {}
            if not isinstance(params, dict):
                raise RpcError(INVALID_PARAMS, "params must be an object")
            if method == "message/stream":
                return await self._stream(sub, request_id, params)
            handler = {
                "message/send": self._send,
                "tasks/get": self._get,
                "tasks/cancel": self._cancel,
            }.get(method)
            if handler is None:
                raise RpcError(METHOD_NOT_FOUND, f"no method {method!r}")
            return JSONResponse({"jsonrpc": "2.0", "id": request_id, "result": await handler(sub, params)})
        except RpcError as exc:
            return JSONResponse(_error(request_id, exc))

    # -- methods ------------------------------------------------------------

    async def _send(self, sub: str, params: dict[str, Any]) -> dict[str, Any]:
        task_id = await self._start(sub, params)
        if (params.get("configuration") or {}).get("blocking"):
            await self._settle(sub, task_id, self.blocking_wait)
        return await self._task(sub, task_id)

    async def _get(self, sub: str, params: dict[str, Any]) -> dict[str, Any]:
        return await self._task(sub, self._id(params))

    async def _cancel(self, sub: str, params: dict[str, Any]) -> dict[str, Any]:
        task_id = self._id(params)
        task = await self._task(sub, task_id)
        if task["status"]["state"] in TERMINAL:
            raise RpcError(TASK_NOT_CANCELABLE, f"task {task_id} has already ended")
        runner = await self._runner(sub, task_id)
        await runner.upstream.call("POST", f"v1/sessions/{task_id}/abort", sub=sub)
        await self._settle(sub, task_id, 10.0)
        return await self._task(sub, task_id)

    async def _stream(self, sub: str, request_id: Any, params: dict[str, Any]) -> Response:
        task_id = await self._start(sub, params)
        first = await self._task(sub, task_id)
        runner = await self._runner(sub, task_id)
        upstream = await runner.upstream.open("GET", f"v1/sessions/{task_id}/events", sub=sub)

        def frame(result: dict[str, Any]) -> bytes:
            body = {"jsonrpc": "2.0", "id": request_id, "result": result}
            return f"data: {json.dumps(body)}\n\n".encode()

        async def updates() -> AsyncIterator[bytes]:
            context = first["contextId"]
            yield frame(first)
            try:
                async for kind, data in _events(upstream):
                    update = _update(kind, data, task_id, context)
                    if update is None:
                        continue
                    yield frame(update)
                    if update.get("final"):
                        break
            finally:
                await upstream.aclose()
            final = await self._task(sub, task_id)
            for artifact in final.get("artifacts", []):
                yield frame(
                    {"kind": "artifact-update", "taskId": task_id, "contextId": context,
                     "artifact": artifact, "lastChunk": True}
                )

        return StreamingResponse(
            updates(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    # -- onto the service ---------------------------------------------------

    @staticmethod
    def _id(params: dict[str, Any]) -> str:
        task_id = params.get("id")
        if not isinstance(task_id, str) or not task_id:
            raise RpcError(INVALID_PARAMS, "params.id is required")
        return task_id

    async def _runner(self, sub: str, task_id: str):
        try:
            return await self.service.session_runner(sub, task_id)
        except Refused as exc:
            raise _rpc(exc, not_found=TASK_NOT_FOUND) from None

    async def _start(self, sub: str, params: dict[str, Any]) -> str:
        message = params.get("message")
        if not isinstance(message, dict):
            raise RpcError(INVALID_PARAMS, "params.message is required")
        text = "\n".join(
            str(p.get("text", "")) for p in message.get("parts") or [] if p.get("kind") == "text"
        ).strip()
        if not text:
            raise RpcError(INVALID_PARAMS, "the message has no text part")
        meta = {**(params.get("metadata") or {}), **(message.get("metadata") or {})}

        if message.get("taskId"):
            task_id = str(message["taskId"])
            runner = await self._runner(sub, task_id)
            status, body = await runner.upstream.call(
                "POST", f"v1/sessions/{task_id}/messages", sub=sub, json={"text": text}
            )
            if status != 200:
                raise RpcError(INTERNAL_ERROR, str(body.get("error")), {"status": status})
            return task_id

        skill = meta.get("skill")
        if skill is not None and skill not in SKILL_INTENTS:
            raise RpcError(INVALID_PARAMS, f"no skill {skill!r}; the agent card lists them")
        policy = str(meta.get("approval_policy") or "auto_safe")
        workspace = await self._workspace(sub, meta, message.get("contextId"))
        body = {"task": text, "approval_policy": policy}
        if skill:
            body["intent"] = SKILL_INTENTS[skill]
        try:
            status, session = await self.service.start_task(sub, workspace, body)
        except Refused as exc:
            raise _rpc(exc) from None
        if status != 200:
            raise RpcError(INTERNAL_ERROR, str(session.get("error")), {"status": status})
        return str(session["id"])

    async def _workspace(self, sub: str, meta: dict[str, Any], context_id: Any) -> str:
        """The lease to run on: named, the task's context, or leased now."""
        for candidate in (meta.get("workspace_id"), context_id):
            if candidate and self.service.store.lease(str(candidate), sub):
                return str(candidate)
        repo_url = str(meta.get("repo_url") or "")
        if not repo_url:
            raise RpcError(
                INVALID_PARAMS,
                "say which repository: metadata.workspace_id for one you hold, or "
                "metadata.repo_url (and ref) to lease one",
            )
        ref = str(meta.get("ref") or "")
        for lease in self.service.store.leases(sub):
            if lease.repo_url == repo_url and (not ref or lease.ref == ref):
                return lease.id
        try:
            return (await self.service.lease(sub, repo_url, ref)).id
        except Refused as exc:
            raise _rpc(exc) from None

    async def _settle(self, sub: str, task_id: str, seconds: float) -> None:
        deadline = asyncio.get_running_loop().time() + seconds
        while asyncio.get_running_loop().time() < deadline:
            if (await self._task(sub, task_id))["status"]["state"] in TERMINAL:
                return
            await asyncio.sleep(0.25)

    async def _task(self, sub: str, task_id: str) -> dict[str, Any]:
        try:
            row, lease = self.service.owned_session(sub, task_id)
        except Refused as exc:
            raise _rpc(exc, not_found=TASK_NOT_FOUND) from None
        runner = await self._runner(sub, task_id)
        status, detail = await runner.upstream.call("GET", f"v1/sessions/{task_id}", sub=sub)
        if status != 200:
            raise _rpc(Refused(status, str(detail.get("error"))), not_found=TASK_NOT_FOUND)

        state = STATES.get(str(detail.get("status")), "unknown")
        if state == "working" and detail.get("pending_approvals"):
            state = "input-required"
        task: dict[str, Any] = {
            "kind": "task",
            "id": task_id,
            "contextId": lease.id,
            "status": {"state": state, "timestamp": _now()},
            "metadata": {"workspace_id": lease.id, "branch": row.branch},
        }
        if state in TERMINAL and detail.get("summary"):
            task["status"]["message"] = _agent_message(str(detail["summary"]), task_id, lease.id)
        if detail.get("mutations"):
            task["artifacts"] = [
                {
                    "artifactId": f"{task_id}-changes",
                    "name": "changes",
                    "description": "Files the run changed. Deliver them as a merge request "
                    "with POST /v1/sessions/{id}/deliver.",
                    "parts": [{"kind": "data", "data": {"files": detail["mutations"], "branch": row.branch}}],
                }
            ]
        delivery = self.service.store.delivery(task_id)
        if delivery is not None and delivery.mr_url:
            task["metadata"]["merge_request"] = delivery.mr_url
        return task


async def _events(response) -> AsyncIterator[tuple[str, dict[str, Any]]]:
    """(type, data) for each frame of a runtime's event stream."""
    kind, data = "", ""
    async for line in response.aiter_lines():
        if not line:
            if kind:
                try:
                    yield kind, json.loads(data or "{}")
                except ValueError:
                    pass
            kind, data = "", ""
        elif line.startswith("event:"):
            kind = line[6:].strip()
        elif line.startswith("data:"):
            data += line[5:].strip()


def _update(kind: str, data: dict[str, Any], task_id: str, context: str) -> dict[str, Any] | None:
    """A runtime event as an A2A status update, or None for one a caller does
    not need."""

    def status(state: str, text: str = "", *, final: bool = False) -> dict[str, Any]:
        body: dict[str, Any] = {"state": state, "timestamp": _now()}
        if text:
            body["message"] = _agent_message(text, task_id, context)
        return {"kind": "status-update", "taskId": task_id, "contextId": context,
                "status": body, "final": final}

    if kind == "turn_start":
        return status("working")
    if kind == "assistant" and data.get("text"):
        return status("working", str(data["text"]))
    if kind == "tool_pending":
        return status("input-required", str(data.get("reason", "")))
    if kind == "finish":
        return status(STATES.get(str(data.get("outcome")), "failed"), str(data.get("summary", "")), final=True)
    return None


def _rpc(exc: Refused, *, not_found: int = INTERNAL_ERROR) -> RpcError:
    code = not_found if exc.status in (404, 410) else INTERNAL_ERROR
    return RpcError(code, exc.detail, {"status": exc.status})


def _error(request_id: Any, exc: RpcError) -> dict[str, Any]:
    error: dict[str, Any] = {"code": exc.code, "message": exc.message}
    if exc.data is not None:
        error["data"] = exc.data
    return {"jsonrpc": "2.0", "id": request_id, "error": error}
