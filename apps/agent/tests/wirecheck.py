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

import httpx
from fastapi import FastAPI
from fastapi.routing import APIRoute
from pydantic import ValidationError

from dakcoder_shared.contract import rest

__all__ = ["CheckedTransport", "checked"]

#: Every route key a CheckedTransport has validated a 2xx response for, across
#: the whole test session. ``test_contract`` reads it.
checked: set[str] = set()


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
