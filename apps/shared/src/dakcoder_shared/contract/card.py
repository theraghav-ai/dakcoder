"""The agent card: what dakcoder offers other agents, before they call it (host-plan §5).

**Not the tool catalogue.** The catalogue (C1) is the model's: 38 tools, which
change whenever one is added. The card is a public promise, served without a
token at a well-known URL, and it lists *skills*: things a caller can ask for.
A caller submits tasks, never tool calls, so publishing `write_file` here would
be both a reconnaissance leak and a promise the API does not make (§5.1).

Generated from this module and ``API_VERSION``, so its version cannot go stale;
published as ``api/agent-card.json`` and checked like the rest of the contract.

Each skill maps onto an intent the runtime already has (``SKILL_INTENTS``). The
card exposes neither intents nor modes, so the mapping can change without
breaking a caller.

``protocolVersion`` is pinned rather than tracking the specification, which is
still moving (§13, question 7).
"""

from __future__ import annotations

import json
from typing import Any

from . import API_VERSION

__all__ = ["A2A_PROTOCOL", "PUBLIC_URL", "SKILLS", "SKILL_INTENTS", "as_json", "card"]

#: The published origin: `dakcoder.serverGatewayUrl`'s default in the shipped
#: extension (§1.3), not the `aiops.cept.gov.in/coder/backend` the first draft
#: of the plan used.
PUBLIC_URL = "https://ai.cept.gov.in/dakcoder"

A2A_PROTOCOL = "0.3.0"

SKILLS: list[dict[str, Any]] = [
    {
        "id": "migrate-service",
        "name": "Convert a legacy service to n-api-template",
        "description": (
            "Applies the migration SOP end to end: branch from the base the caller names, "
            "dependency swap, handler conversion, govalid regeneration, test harness, swagger "
            "verification. Long: the gate is deferred until the last phase closes. Delivered "
            "as a branch and a merge request."
        ),
        "tags": ["go", "migration", "n-api-template"],
        "examples": ["Convert pao-back-end-development to the new template"],
    },
    {
        "id": "implement-change",
        "name": "Implement a change in a Go service",
        "description": (
            "Adds or changes an endpoint, DTO, repository method or domain field, verified by "
            "the gate (build, vet, rules_lint, tests). Delivered as a branch and a merge request."
        ),
        "tags": ["go", "codegen"],
        "examples": ["Add a Pension resource with CRUD and a status filter"],
    },
    {
        "id": "review-service",
        "name": "Audit a service against the template contract",
        "description": (
            "Runs the compliance rule set and the legacy, temporal, validation and DB round-trip "
            "audits, and reports findings with fixes and citations. Read-only."
        ),
        "tags": ["go", "review"],
    },
    {
        "id": "answer",
        "name": "Answer a question about a service or the template",
        "description": "Read-only. Cites the knowledge base and the repository.",
        "tags": ["qa"],
    },
]

#: Skill -> the runtime's intent. `ask` is read-only; `agent` plans and acts.
SKILL_INTENTS = {
    "migrate-service": "agent",
    "implement-change": "agent",
    "review-service": "ask",
    "answer": "ask",
}


def card(base_url: str = PUBLIC_URL) -> dict[str, Any]:
    base = base_url.rstrip("/")
    return {
        "protocolVersion": A2A_PROTOCOL,
        "name": "dakcoder",
        "description": (
            "Backend coding agent for IT 2.0 Go microservices on n-api-template: migrates "
            "legacy api-* services, implements endpoints, and answers questions about the "
            "template contract. Works on a server-side clone of a repository the caller may "
            "lease, and delivers changes as merge requests."
        ),
        "url": f"{base}/v1/a2a",
        "preferredTransport": "JSONRPC",
        "version": f"{API_VERSION}.0",
        "provider": {"organization": "CEPT IT 2.0", "url": "https://cept.gov.in"},
        "capabilities": {
            "streaming": True,
            "pushNotifications": False,
            "stateTransitionHistory": False,
        },
        "defaultInputModes": ["text/plain"],
        "defaultOutputModes": ["text/plain", "application/json"],
        "securitySchemes": {
            "dakcoderJwt": {"type": "http", "scheme": "bearer", "bearerFormat": "JWT"}
        },
        "security": [{"dakcoderJwt": []}],
        "skills": SKILLS,
    }


def as_json(base_url: str = PUBLIC_URL) -> str:
    return json.dumps(card(base_url), indent=2) + "\n"
