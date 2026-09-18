"""Hold every response the route tests receive to the published contract.

``CheckedTransport`` is ``httpx.ASGITransport`` plus one step: each JSON
response is validated against the model ``rest.ROUTES`` names for its route,
with unknown fields rejected. The route tests already drive the runtime through
every state worth describing (a finished run, a pending approval, a plan, a
compaction), so using this transport in their fixtures turns each of them into
a check that the contract describes what the runtime actually sent.

A field the runtime starts sending without adding it to the contract fails here,
in the test that first sees it.
"""

from __future__ import annotations

import json

import httpx
from fastapi import FastAPI
from fastapi.routing import APIRoute
from pydantic import ValidationError

from dakcoder_shared.contract import events, rest
from dakcoder_shared.envelope import Event

__all__ = ["CheckedTransport", "checked", "checked_events", "event_problem"]

#: Every route key a CheckedTransport has validated a 2xx response for, across
#: the whole test session.
checked: set[str] = set()

#: Every event type ``event_problem`` has validated, across the whole session.
checked_events: set[str] = set()


def event_problem(event: Event) -> str | None:
    """Why ``event`` does not match its payload model, or None when it does.

    Validated as JSON, because JSON is what goes on the wire: the SSE encoder
    is ``json.dumps`` of the payload, and a payload it cannot encode is a bug
    whatever the model says.
    """
    model = events.PAYLOADS.get(event.type)
    if model is None:
        return f"{event.type} has no payload model in contract.events.PAYLOADS"
    try:
        model.model_validate_json(json.dumps(event.data))
    except (TypeError, ValueError) as exc:
        return (
            f"a {event.type} event carried a payload its contract ({model.__name__}) "
            f"does not describe:\n{exc}\n\ndata: {str(event.data)[:2000]}"
        )
    checked_events.add(str(event.type))
    if "kind" in event.data:
        checked_events.add(f"{event.type}:{event.data['kind']}")
    return None


class CheckedTransport(httpx.ASGITransport):
    def __init__(self, app: FastAPI, **kwargs) -> None:
        super().__init__(app=app, **kwargs)
        self._routes = [
            (route, method)
            for route in app.routes
            if isinstance(route, APIRoute)
            for method in route.methods
        ]

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        response = await super().handle_async_request(request)
        if not response.headers.get("content-type", "").startswith("application/json"):
            return response
        body = await response.aread()
        self._check(request.method, request.url.path, response.status_code, body)
        return httpx.Response(
            response.status_code,
            headers=response.headers,
            content=body,
            request=request,
            extensions=response.extensions,
        )

    def _check(self, method: str, path: str, status: int, body: bytes) -> None:
        key = next(
            (
                f"{m} {route.path}"
                for route, m in self._routes
                if m == method and route.path_regex.match(path)
            ),
            None,
        )
        if key is None:
            return  # FastAPI's own /openapi.json and /docs
        spec = rest.ROUTES.get(key)
        assert spec is not None, f"{key} is served but has no entry in rest.ROUTES"

        if 200 <= status < 300:
            model = spec.response
        elif status == 422:
            model = rest.ValidationFailure
        elif 400 <= status < 500:
            model = rest.Error
        else:
            return
        if model is None:
            return
        try:
            model.model_validate_json(body)
        except ValidationError as exc:
            raise AssertionError(
                f"{key} answered {status} with a body its contract ({model.__name__}) "
                f"does not describe:\n{exc}\n\nbody: {body[:2000]!r}"
            ) from None
        if 200 <= status < 300:
            checked.add(key)
