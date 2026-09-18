"""`auto_safe`: approvals for a run with nobody to ask (host-plan §10)."""

from __future__ import annotations

import asyncio
from pathlib import Path

import httpx
import pytest

from dakcoder_agent.loop import RunResult
from dakcoder_agent.loopback import Loopback, create_app
from dakcoder_agent.policies import auto_safe
from dakcoder_agent.tools.router import ApprovalRequest
from dakcoder_shared.envelope import Event, EventType
from wirecheck import CheckedTransport, event_problem

TOKEN = "tok"


def request(tool: str, *paths: str, **arguments) -> ApprovalRequest:
    return ApprovalRequest(tool, dict(arguments), reason="r", paths=tuple(paths))


@pytest.mark.parametrize(
    ("call", "approved"),
    [
        (request("resource_scaffold", "handler/pension.go", "repo/postgres/pension.go"), True),
        (request("git_ops", op="commit", message="add pension"), True),
        (request("run_terminal", argv="go generate ./..."), True),
        (request("patch_file", "go.mod"), False),
        (request("delete_file", "handler/old.go"), False),
        (request("go_mod", op="get", pkg="github.com/x/y"), False),
    ],
    ids=["scaffold", "commit", "terminal", "protected", "delete", "new-dependency"],
)
def test_what_auto_safe_approves_and_refuses(call: ApprovalRequest, approved: bool) -> None:
    decision, reason = auto_safe(call)
    assert decision is approved, reason
    assert call.tool in reason


class AsksTwice:
    """An agent that raises two approvals, one auto_safe approves and one it
    refuses, and records what it was told."""

    def __init__(self, approve) -> None:
        self.approve = approve
        self.answers: list[bool] = []
        self.result: RunResult | None = None

        class _Context:
            turn = 1

            def attach_journal(self, _journal) -> None:
                pass

        class _Router:
            touched = ()

        self.context, self.router = _Context(), _Router()

    def run(self, task, **_kw):
        for call in (request("resource_scaffold", "handler/a.go"), request("delete_file", "b.go")):
            self.on_pending(call)
            yield Event(EventType.TOOL_PENDING, {**call.as_dict(), "turn": 1})
            self.answers.append(self.approve(call))
        self.result = RunResult("done", "ok", 1, ())
        yield Event(EventType.FINISH, self.result.as_dict())
        yield Event(EventType.END, self.result.as_dict())


async def run_with(tmp_path: Path, policy: str | None) -> tuple[Loopback, dict, AsksTwice]:
    agents: list[AsksTwice] = []

    def build(_session, approve):
        agents.append(AsksTwice(approve))
        return agents[-1]

    runtime = Loopback(tmp_path, build, token=TOKEN)
    transport = CheckedTransport(create_app(runtime))
    async with httpx.AsyncClient(
        transport=transport, base_url="http://127.0.0.1", headers={"Authorization": f"Bearer {TOKEN}"}
    ) as http:
        body = {"task": "unattended"}
        if policy:
            body["approval_policy"] = policy
        started = await http.post("/v1/tasks", json=body)
        assert started.status_code == 200, started.text
        session = started.json()
        for _ in range(200):
            held = runtime.sessions.get(session["id"])
            if not held.running and any(e.type is EventType.END for e in held.events):
                break
            await asyncio.sleep(0.01)
    return runtime, session, agents[0]


async def test_an_auto_safe_run_is_answered_at_once_and_says_so(tmp_path: Path) -> None:
    runtime, session, agent = await run_with(tmp_path, "auto_safe")
    assert agent.answers == [True, False]
    assert runtime.approvals == {}, "nothing left waiting for a person who is not there"

    decisions = [
        e.data for e in runtime.sessions.get(session["id"]).events
        if e.type is EventType.GATE and e.data.get("kind") == "auto_approval"
    ]
    assert [(d["tool"], d["approved"]) for d in decisions] == [
        ("resource_scaffold", True),
        ("delete_file", False),
    ]
    assert all(event_problem(Event(EventType.GATE, d)) is None for d in decisions)


async def test_the_policy_survives_a_restart(tmp_path: Path) -> None:
    runtime, session, _ = await run_with(tmp_path, "auto_safe")
    restored = Loopback(tmp_path, lambda _s, _a: None, token=TOKEN)
    assert restored.sessions.get(session["id"]).approval_policy == "auto_safe"


async def test_an_unknown_policy_is_refused(tmp_path: Path) -> None:
    runtime = Loopback(tmp_path, lambda _s, _a: None, token=TOKEN)
    async with httpx.AsyncClient(
        transport=CheckedTransport(create_app(runtime)),
        base_url="http://127.0.0.1",
        headers={"Authorization": f"Bearer {TOKEN}"},
    ) as http:
        response = await http.post("/v1/tasks", json={"task": "x", "approval_policy": "yes_to_all"})
    assert response.status_code == 400
