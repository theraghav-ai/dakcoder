"""Other agents calling dakcoder over A2A (host-plan §5.4)."""

from __future__ import annotations

import asyncio
import json

import pytest

from support import BASE, StandInAgent, as_caller


def rpc(method: str, params: dict | None = None, request_id: int = 1) -> dict:
    return {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params or {}}


def send(text: str, remote: str, *, skill: str | None = None, blocking: bool = False, **meta) -> dict:
    metadata = {"repo_url": remote, "ref": BASE, **meta}
    if skill:
        metadata["skill"] = skill
    params = {
        "message": {
            "kind": "message",
            "role": "user",
            "messageId": "m1",
            "parts": [{"kind": "text", "text": text}],
            "metadata": metadata,
        }
    }
    if blocking:
        params["configuration"] = {"blocking": True}
    return rpc("message/send", params)


async def call(http, body: dict) -> dict:
    response = await http.post("/v1/a2a", json=body)
    assert response.status_code == 200, response.text
    return response.json()


async def test_a_sent_message_is_a_task_on_a_leased_workspace(app, remote, service) -> None:
    async with as_caller(app, "agent-7") as agent:
        answer = await call(agent, send("write handler/pension.go", remote, skill="implement-change", blocking=True))
        task = answer["result"]
        again = await call(agent, send("write handler/other.go", remote, blocking=True))
    assert task["kind"] == "task"
    assert task["status"]["state"] == "completed"
    assert task["status"]["message"]["role"] == "agent"
    files = task["artifacts"][0]["parts"][0]["data"]
    assert files["files"] == ["handler/pension.go"] and files["branch"] == f"dakcoder/{task['id']}"
    assert again["result"]["contextId"] == task["contextId"], "the same repository reuses the lease"
    assert len(service.store.leases("agent-7")) == 1


async def test_skills_map_onto_intents_and_approvals_default_to_auto_safe(app, remote) -> None:
    async with as_caller(app, "agent-7") as agent:
        await call(agent, send("noop", remote, skill="review-service", blocking=True))
        await call(agent, send("noop", remote, skill="migrate-service", blocking=True, approval_policy="interactive"))
    assert [(intent, policy) for _t, intent, policy in StandInAgent.seen] == [
        ("ask", "auto_safe"),
        ("agent", "interactive"),
    ]


async def test_a_task_can_be_read_and_cancelled_by_its_caller_only(app, remote) -> None:
    async with as_caller(app, "agent-7") as agent:
        started = (await call(agent, send("wait", remote)))["result"]
        read = await call(agent, rpc("tasks/get", {"id": started["id"]}))
        async with as_caller(app, "agent-8") as other:
            theirs = await call(other, rpc("tasks/get", {"id": started["id"]}))
            refused = await call(other, rpc("tasks/cancel", {"id": started["id"]}))
        cancelled = await call(agent, rpc("tasks/cancel", {"id": started["id"]}))
        StandInAgent.release.set()
        twice = await call(agent, rpc("tasks/cancel", {"id": started["id"]}))

    assert read["result"]["status"]["state"] == "working"
    assert theirs["error"]["code"] == refused["error"]["code"] == -32001, "not found, not forbidden"
    assert cancelled["result"]["status"]["state"] in ("canceled", "completed")
    assert twice["error"]["code"] == -32002


async def test_a_streamed_task_ends_with_a_final_update(app, remote) -> None:
    async with as_caller(app, "agent-7") as agent:
        body = send("write a.go", remote)
        body["method"] = "message/stream"
        response = await agent.post("/v1/a2a", json=body)
    assert response.headers["content-type"].startswith("text/event-stream")
    results = [
        json.loads(line[5:])["result"] for line in response.text.splitlines() if line.startswith("data:")
    ]
    assert results[0]["kind"] == "task"
    finals = [r for r in results if r.get("kind") == "status-update" and r.get("final")]
    assert len(finals) == 1 and finals[0]["status"]["state"] == "completed"
    assert results[-1]["kind"] == "artifact-update"


@pytest.mark.parametrize(
    ("body", "code"),
    [
        (b"not json", -32700),
        ({"jsonrpc": "1.0", "method": "tasks/get"}, -32600),
        (rpc("tasks/list"), -32601),
        (rpc("message/send", {"message": {"parts": []}}), -32602),
        (rpc("tasks/get", {"id": "nope"}), -32001),
    ],
    ids=["parse", "not-2.0", "method", "no-text", "no-task"],
)
async def test_malformed_requests_get_json_rpc_errors(app, body, code) -> None:
    async with as_caller(app, "agent-7") as agent:
        if isinstance(body, bytes):
            response = await agent.post("/v1/a2a", content=body)
        else:
            response = await agent.post("/v1/a2a", json=body)
    assert response.json()["error"]["code"] == code


async def test_an_unknown_skill_and_an_unnamed_repository_are_refused(app, remote) -> None:
    async with as_caller(app, "agent-7") as agent:
        skill = await call(agent, send("x", remote, skill="deploy-to-production"))
        body = send("x", remote)
        body["params"]["message"]["metadata"] = {}
        nowhere = await call(agent, body)
    assert skill["error"]["code"] == -32602
    assert nowhere["error"]["code"] == -32602
