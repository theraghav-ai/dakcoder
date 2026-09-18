"""Hosted, an approval nobody answers suspends the run (host-plan §10, R9).

Locally a timed-out approval is a refusal: the developer is there and chose not
to look, and the run carries on without the change. Hosted, it most often means
nobody is there, and an indefinite wait parks a runner, a lease and a disk
quota. So a hosted run stops instead, keeps its lease and its journal, and ends
`aborted`, which is resumable. Resuming proposes the change again.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import httpx
import pytest

from dakcoder_agent import loopback
from dakcoder_agent.loop import RunResult
from dakcoder_agent.loopback import Loopback, create_app
from dakcoder_agent.tools.router import ApprovalRequest
from dakcoder_shared.envelope import Event, EventType
from wirecheck import CheckedTransport, event_problem

TOKEN = "tok"


class AsksOnce:
    """Raises one approval and finishes as if it had carried on."""

    def __init__(self, approve) -> None:
        self.approve = approve
        self.answer: bool | None = None
        self.result: RunResult | None = None

        class _Context:
            turn = 1

            def attach_journal(self, _journal) -> None:
                pass

        class _Router:
            touched = ()

        self.context, self.router = _Context(), _Router()

    def run(self, task, **_kw):
        call = ApprovalRequest("patch_file", {"path": "go.mod"}, reason="r", paths=("go.mod",))
        self.on_pending(call)
        yield Event(EventType.TOOL_PENDING, {**call.as_dict(), "turn": 1})
        self.answer = self.approve(call)
        self.result = RunResult("done", "carried on without it", 1, ())
        yield Event(EventType.FINISH, self.result.as_dict())
        yield Event(EventType.END, self.result.as_dict())


async def run(tmp_path: Path, *, hosted: bool, body: dict) -> tuple[Loopback, dict, AsksOnce]:
    agents: list[AsksOnce] = []

    def build(_session, approve):
        agents.append(AsksOnce(approve))
        return agents[-1]

    runtime = Loopback(tmp_path, build, token=TOKEN, suspend_on_timeout=hosted)
    async with httpx.AsyncClient(
        transport=CheckedTransport(create_app(runtime)),
        base_url="http://127.0.0.1",
        headers={"Authorization": f"Bearer {TOKEN}"},
    ) as http:
        started = await http.post("/v1/tasks", json={"task": "x", **body})
        assert started.status_code == 200, started.text
        session = started.json()
        for _ in range(300):
            held = runtime.sessions.get(session["id"])
            if not held.running and any(e.type is EventType.END for e in held.events):
                break
            await asyncio.sleep(0.01)
        detail = (await http.get(f"/v1/sessions/{session['id']}")).json()
    return runtime, detail, agents[0]


async def test_a_hosted_run_suspends_when_nobody_answers(tmp_path: Path) -> None:
    runtime, detail, agent = await run(tmp_path, hosted=True, body={"approval_timeout": 0.05})
    assert agent.answer is False, "the change was not made"
    assert detail["status"] == "aborted" and detail["resumable"], "suspended, not failed"
    assert detail["summary"].startswith("suspended:")
    assert runtime.approvals == {}

    suspended = [
        e for e in runtime.sessions.get(detail["id"]).events
        if e.type is EventType.GATE and e.data.get("kind") == "suspended"
    ]
    assert len(suspended) == 1 and suspended[0].data["tool"] == "patch_file"
    assert event_problem(Event(EventType.GATE, suspended[0].data)) is None


async def test_a_local_run_still_treats_silence_as_a_refusal(tmp_path: Path) -> None:
    runtime, detail, agent = await run(tmp_path, hosted=False, body={"approval_timeout": 0.05})
    assert agent.answer is False
    assert detail["status"] == "done", "the developer was there; the run carried on"
    assert not any(
        e.data.get("kind") == "suspended" for e in runtime.sessions.get(detail["id"]).events
    )


def test_a_task_cannot_wait_longer_than_the_runtime_allows(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(loopback, "APPROVAL_TIMEOUT", 60.0)
    runtime = Loopback(tmp_path, lambda _s, _a: None, token=TOKEN)
    patient = runtime.sessions.create("x", approval_timeout=86_400)
    brisk = runtime.sessions.create("y", approval_timeout=5)
    unsaid = runtime.sessions.create("z")
    assert runtime._timeout_for(patient) == 60.0, "the hosted maximum bounds it"
    assert runtime._timeout_for(brisk) == 5
    assert runtime._timeout_for(unsaid) is None, "the runtime's own, unchanged"


@pytest.mark.parametrize("value", [-1, 0, "soon", True])
async def test_a_nonsense_timeout_is_refused(tmp_path: Path, value) -> None:
    runtime = Loopback(tmp_path, lambda _s, _a: None, token=TOKEN)
    async with httpx.AsyncClient(
        transport=CheckedTransport(create_app(runtime)),
        base_url="http://127.0.0.1",
        headers={"Authorization": f"Bearer {TOKEN}"},
    ) as http:
        response = await http.post("/v1/tasks", json={"task": "x", "approval_timeout": value})
    assert response.status_code == 400
