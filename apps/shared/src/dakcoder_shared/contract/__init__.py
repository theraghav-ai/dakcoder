"""The wire contract, declared once.

Everything a client binds against is described here or derived from here:
the API version, the event types (C2) and the REST route table. ``make
contract`` writes it to ``api/contract.json``, and the extension generates its
TypeScript types from that file rather than keeping its own copy.

The copy it kept had drifted. ``EventType.METRICS`` was emitted for five
releases while the extension's hand-written union did not list it. C2 tolerates
unknown types, so nothing failed. It was a missing feature nobody noticed. In
the same window six routes were added and the only written list of them was a
planning document. Both sides now derive from this module, and a drift check on
each side fails when they disagree.

The document carries a hash of its own content. ``/v1/health`` reports it, so a
client can tell that the runtime speaks a different 1.1 from the one it was
built against. ``api_version`` alone cannot show that, because additive
changes do not bump it.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from typing import Any

from ..envelope import EventType
from . import rest

__all__ = ["API_VERSION", "as_json", "digest", "document"]

#: The contract version clients pin against. Bumped when a response shape
#: changes in a way a client could not have anticipated — never for an additive
#: field, because C2's rule is that unknown types and fields are ignored.
#:
#: **1.1** — the mode vocabulary changed. Five modes (`planner`, `scaffolder`,
#: `coder`, `verifier`, `debugger`) became three (`ask`, `planner`, `agent`), so
#: a 1.0 client's `Mode` union does not contain the values it will now be sent.
#: It degrades rather than crashes — an unknown mode is displayed raw — but the
#: guard exists precisely so that half-working is not the outcome nobody
#: suspects.
#:
#: Additive in the same release, and *not* on their own a reason to bump:
#: `POST /v1/tasks` accepts `intent` (with `mode` still read as a synonym),
#: `POST /v1/credential` is new, `turn_start` carries `intent`, and the tool
#: catalog gained `finish`, `submit_plan` and `ask_developer`.
API_VERSION = "1.1"


def document(routes: Iterable[str]) -> dict[str, Any]:
    """The contract, with ``routes`` as ``"METHOD /path"`` strings.

    Routes are passed in because they belong to the agent's app, and this
    package must not import the agent. Everything is sorted, so reordering a
    declaration does not change the hash.

    The hash also covers every REST model's field names (``rest.fields``), so a
    response that gains or loses a field changes it. The shapes themselves are
    published in ``api/openapi.json``.
    """
    body: dict[str, Any] = {
        "api_version": API_VERSION,
        "events": sorted(str(t) for t in EventType),
        "routes": sorted(set(routes)),
    }
    covered = {**body, "fields": rest.fields()}
    return {"contract": "dakcoder", **body, "hash": digest(covered)}


def digest(body: dict[str, Any]) -> str:
    """A short, stable hash of the contract body."""
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


def as_json(routes: Iterable[str]) -> str:
    return json.dumps(document(routes), indent=2) + "\n"
