"""The runtime's OpenAPI document: what ``api/openapi.json`` publishes.

FastAPI's own document gets the parameters right (path, query, headers) and
nothing else: every body and response is an untyped object, because the
handlers read and return plain dicts. This takes that document and fills in the
rest from ``dakcoder_shared.contract.rest``: request and response schemas,
summaries, the token as a security scheme rather than a header parameter, and
the ``{"error": ...}`` body every refusal carries.

Handler docstrings are dropped from the published document. They are written
for whoever maintains the handler, and publishing them would make every comment
edit a contract change.

As little of FastAPI's own output is kept as possible, because the file is
compared byte for byte in CI and CI installs FastAPI unpinned. Its generated
``title`` on every schema is stripped, and its 422 schema is replaced by ours.
What remains from FastAPI is the parameter list.
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI
from fastapi.openapi.utils import get_openapi
from fastapi.routing import APIRoute
from pydantic.json_schema import models_json_schema

from dakcoder_shared.contract import API_VERSION, rest

__all__ = ["build"]

_REF = "#/components/schemas/{model}"


def _ref(model: type[rest.Wire]) -> dict[str, str]:
    return {"$ref": _REF.format(model=model.__name__)}


def build(app: FastAPI) -> dict[str, Any]:
    # The handler's name is the operationId. FastAPI's default appends the path
    # and method, which client generators turn into unreadable method names.
    handlers = {
        f"{method} {route.path}": route.name
        for route in app.routes
        if isinstance(route, APIRoute)
        for method in route.methods
    }
    served = set(handlers)
    undocumented = sorted(served - set(rest.ROUTES))
    unserved = sorted(set(rest.ROUTES) - served)
    if undocumented or unserved:
        raise ValueError(
            "rest.ROUTES does not match the app. "
            f"Served but not in ROUTES: {undocumented}. In ROUTES but not served: {unserved}."
        )

    doc = get_openapi(title="dakcoderd", version=API_VERSION, routes=app.routes)
    for key, route in rest.ROUTES.items():
        method, path = key.split(" ", 1)
        _fill(doc["paths"][path][method.lower()], route, handlers[key])

    _, defs = models_json_schema(
        [(model, "validation") for model in rest.models()], ref_template=_REF
    )
    doc["components"] = {
        "schemas": dict(sorted(_untitled(defs.get("$defs", {})).items())),
        "securitySchemes": {
            "token": {
                "type": "http",
                "scheme": "bearer",
                "description": "The loopback token the extension passes to the runtime at spawn.",
            }
        },
    }
    doc["paths"] = dict(sorted(doc["paths"].items()))
    return doc


def _fill(op: dict[str, Any], route: rest.Route, handler: str) -> None:
    op.pop("description", None)
    op["summary"] = route.summary
    op["operationId"] = handler

    params = [
        _untitled(p)
        for p in op.get("parameters", [])
        if not (p["in"] == "header" and p["name"].lower() == "authorization")
    ]
    if params:
        op["parameters"] = params
    else:
        op.pop("parameters", None)
    # `{}` first: the token is optional on a public route, not absent.
    op["security"] = [{}, {"token": []}] if route.public else [{"token": []}]

    if route.request is not None:
        op["requestBody"] = {
            "required": not route.optional_body,
            "content": {"application/json": {"schema": _ref(route.request)}},
        }
    else:
        op.pop("requestBody", None)

    responses = op.setdefault("responses", {})
    if "422" in responses:
        # A path or query parameter of the wrong type. FastAPI answers these
        # itself, in its own shape rather than the `{"error": ...}` the
        # handlers use.
        responses["422"] = {
            "description": "A parameter could not be read.",
            "content": {"application/json": {"schema": _ref(rest.ValidationFailure)}},
        }
    ok = responses.setdefault("200", {})
    if route.stream:
        ok["description"] = (
            "Server-sent events. Each frame's `event:` is a C2 event type and its "
            "`data:` is that event's payload. Resume with `since_id` or `Last-Event-ID`."
        )
        ok["content"] = {"text/event-stream": {"schema": {"type": "string"}}}
    else:
        assert route.response is not None
        ok["description"] = "OK"
        ok["content"] = {"application/json": {"schema": _ref(route.response)}}
    responses["4XX"] = {
        "description": "Refused. The body says why.",
        "content": {"application/json": {"schema": _ref(rest.Error)}},
    }


def _untitled(node: Any) -> Any:
    """Drop the ``title`` Pydantic and FastAPI put on every schema.

    Only the schema keyword. A property that is itself named ``title``, like
    ``AgendaTask.title``, is a field and stays.
    """
    if isinstance(node, list):
        return [_untitled(item) for item in node]
    if not isinstance(node, dict):
        return node
    return {
        key: (
            {name: _untitled(schema) for name, schema in value.items()}
            if key == "properties"
            else _untitled(value)
        )
        for key, value in node.items()
        if key != "title"
    }
