"""Additive-only, as a test rather than a convention (host-plan §4.6).

C2 allows a release to add event types, routes and fields, and forbids it to
take any away within a major version. ``surface`` lists what the contract
promises a client, one fact per line; ``api/contract-baseline.json`` holds the
facts of the last release; ``breaks`` is every baseline fact the current
contract no longer states. A non-empty answer fails ``test_contract.py``, and
the release script refuses to take a new snapshot over it.

What counts as a promise depends on the direction:

* **What the runtime sends** (responses, event payloads). A field present and
  required must stay present and required, with the same type. Its type may
  not gain ``null``. Enum values may come and go: a client already has to
  tolerate a value it does not know, and one it never sees costs it nothing.
* **What a client sends** (request bodies, parameters). A field or parameter
  may not disappear, change type, become required, or stop accepting a value
  it accepted.

Model names are not promises on the wire and are not compared, except where a
union's members are told apart (``gate``), where a renamed member would read as
a removal. That errs on the side of a false alarm, which costs a conscious
decision rather than a client.
"""

from __future__ import annotations

import json
from typing import Any

__all__ = ["baseline", "breaks", "major", "surface"]


def major(version: str) -> int:
    return int(str(version).split(".", 1)[0])


def surface(contract: dict[str, Any], openapi: dict[str, Any]) -> list[str]:
    """Every fact a client may rely on, sorted."""
    schemas = openapi["components"]["schemas"]
    facts: set[str] = set()
    facts.update(f"event {t}" for t in contract["events"])
    facts.update(f"route {r}" for r in contract["routes"])

    for path, methods in openapi["paths"].items():
        for method, op in methods.items():
            route = f"{method.upper()} {path}"
            for param in op.get("parameters", []):
                where = f"{route} {param['in']} {param['name']}"
                facts.add(f"send {where} : {_type(param.get('schema', {}))}")
                if not param.get("required"):
                    facts.add(f"send {where} optional")
            body = op.get("requestBody", {}).get("content", {}).get("application/json")
            if body:
                if not op["requestBody"].get("required"):
                    facts.add(f"send {route} body optional")
                _walk(body["schema"], f"send {route} body", "send", schemas, facts)
            ok = op.get("responses", {}).get("200", {}).get("content", {})
            if "application/json" in ok:
                _walk(ok["application/json"]["schema"], f"get {route}", "get", schemas, facts)
            for media in ok:
                facts.add(f"get {route} as {media}")

    for event, model in contract.get("payloads", {}).items():
        _walk({"$ref": f"#/components/schemas/{model}"}, f"get event {event}", "get", schemas, facts)
    return sorted(facts)


def breaks(baseline: dict[str, Any], current: list[str], api_version: str) -> list[str]:
    """Baseline facts the current contract no longer states.

    Empty across a major version bump: that is what a major version is for.
    """
    if major(api_version) > major(baseline["api_version"]):
        return []
    return sorted(set(baseline["facts"]) - set(current))


def baseline(contract: dict[str, Any], openapi: dict[str, Any], release: str) -> str:
    return json.dumps(
        {
            "api_version": contract["api_version"],
            "release": release,
            "facts": surface(contract, openapi),
        },
        indent=1,
    ) + "\n"


def _walk(
    schema: dict[str, Any],
    at: str,
    side: str,
    schemas: dict[str, Any],
    facts: set[str],
    depth: int = 0,
) -> None:
    if depth > 12:  # the models have no cycles; this is a guard, not a limit
        return
    if "$ref" in schema:
        _walk(schemas[schema["$ref"].rsplit("/", 1)[-1]], at, side, schemas, facts, depth + 1)
        return
    members = schema.get("anyOf") or schema.get("oneOf")
    if members:
        refs = [m for m in members if "$ref" in m]
        tagged = "oneOf" in schema or len(refs) > 1
        for member in members:
            if member.get("type") == "null":
                continue
            label = f"{at}<{member['$ref'].rsplit('/', 1)[-1]}>" if tagged and "$ref" in member else at
            _walk(member, label, side, schemas, facts, depth + 1)
        return
    if side == "send" and "enum" in schema:
        facts.update(f"{at} accepts {value}" for value in schema["enum"])
    if schema.get("type") == "array":
        _walk(schema.get("items", {}), f"{at}[]", side, schemas, facts, depth + 1)
    elif schema.get("type") == "object" and isinstance(schema.get("additionalProperties"), dict):
        _walk(schema["additionalProperties"], f"{at}{{}}", side, schemas, facts, depth + 1)

    required = set(schema.get("required", ()))
    for name, prop in schema.get("properties", {}).items():
        field = f"{at}.{name}"
        facts.add(f"{field} : {_type(prop, schemas)}")
        if side == "get" and name in required:
            facts.add(f"{field} always")
        if side == "send" and name not in required:
            facts.add(f"{field} optional")
        _walk(prop, field, side, schemas, facts, depth + 1)


def _type(schema: dict[str, Any], schemas: dict[str, Any] | None = None) -> str:
    """A type, coarsely: enough to tell string from number from nullable."""
    if "$ref" in schema:
        target = (schemas or {}).get(schema["$ref"].rsplit("/", 1)[-1], {})
        return "union" if "oneOf" in target else "object"
    members = schema.get("anyOf") or schema.get("oneOf")
    if members:
        return "|".join(sorted({_type(m, schemas) for m in members}))
    if "enum" in schema or "const" in schema:
        return schema.get("type", "string")
    return schema.get("type", "any")
