"""Tests for the loopback: the endpoint the extension drives.

Two things carry the weight. **Resumption**, because Part B §14 names it as the
one real gap in the current client — a dropped connection loses the live view of
a run that is still executing, and the developer cannot tell that from the run
having died. And **abort and approval**, because both cross the boundary between
a synchronous loop on a worker thread and an async server, which is where this
kind of code goes wrong quietly.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
from pathlib import Path

import httpx
import pytest

from dakcoder_agent.context import ContextManager
from dakcoder_agent.loop import AgentLoop
from dakcoder_agent.loopback import API_VERSION, Loopback, create_app
from dakcoder_agent.modes import Mode
from dakcoder_agent.prompts import system_prompt
from dakcoder_agent.session import Status
from dakcoder_agent.tools import control
from dakcoder_agent.tools.router import Router
from dakcoder_shared.llm import ChatResult, ToolCall, Usage

from test_loop import ScriptedClient, calls, say

TOKEN = "loopback-token-for-tests"


def plan_for(path: str, action: str = "edit it") -> ChatResult:
    """The turn that ends the planning phase.

    A plan is a `submit_plan` call now, not a numbered paragraph -- so a test
    that scripts a run has to script the call. That is the point of the change:
    the loop transitions on a typed event, and there is no prose for a test (or
    a model) to phrase in a way the loop misreads.
    """
    return calls(
        (
            "submit_plan",
            json.dumps(
                {"steps": [{"file": path, "action": action, "accepts": "go build clean"}]}
            ),
        )
    )


@pytest.fixture
def scripted(router: Router, workspace):
    """A runtime whose model, and whose gate, are scripts the test writes.

    The gate is stubbed here for the same reason `test_gate.py` stubs it: a real
    `go build` against a fixture workspace takes seconds and fails, so an
    unstubbed gate turns every loopback test into a timing experiment about the
    Go toolchain. The gate's own behaviour is covered where it belongs.
    """
    from dakcoder_agent.gate import GATE
    from dakcoder_shared.envelope import ToolResult

    # `rules_lint` carries `meta`, because the real one does: `gotools._report`
    # copies the sidecar's counts there beside the rendered prose, and
    # `_stage_passed` reads them. A stub returning bare text stands for a tool
    # that does not exist -- and it is what let the blocking contract-lint stage
    # look like it worked while being unable to fail on a finding (defect T1).
    def clean(name: str):
        meta = {"violations": 0, "files_scanned": 1} if name == "rules_lint" else {}
        return lambda _inv, _n=name, _m=meta: ToolResult.success(f"{_n}: clean", meta=dict(_m))

    for stage in GATE:
        router.handlers[stage.tool] = clean(stage.tool)
    for name in ("gofmt", "rules_lint", "go_diagnostics"):
        router.handlers[name] = clean(name)

    router.handlers.update(control.HANDLERS)

    # The plan arrives as a tool call, and the acting phase then writes the file
    # it named. A run whose plan names a file and never writes it says so rather
    # than reporting the untouched repository's clean gate as success.
    plan = {
        "turns": [
            calls(
                (
                    "submit_plan",
                    json.dumps(
                        {
                            "steps": [
                                {
                                    "file": "handler/pension.go",
                                    "action": "add the Pension handler",
                                    "accepts": "go build ./... clean",
                                }
                            ]
                        }
                    ),
                )
            ),
            calls(
                ("write_file", '{"path": "handler/pension.go", "content": "package handler"}')
            ),
            say("done"),
        ]
    }

    def build(session, approve):
        context = ContextManager(mode=Mode.ASK, system_prompt=system_prompt())
        return AgentLoop(context, ScriptedClient(plan["turns"]), router, approve=approve)

    runtime = Loopback(
        workspace.root, build, token=TOKEN, version="1.2.3",
        tool_catalog={"contract": "C1", "tools": []},
    )
    runtime._plan = plan  # so a test can rewrite the script before starting
    return runtime


@pytest.fixture
async def client(scripted: Loopback):
    transport = httpx.ASGITransport(app=create_app(scripted))
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://127.0.0.1",
        headers={"Authorization": f"Bearer {TOKEN}"},
    ) as http:
        yield http


async def start(client: httpx.AsyncClient, task: str = "Add a Pension resource") -> dict:
    response = await client.post("/v1/tasks", json={"task": task})
    assert response.status_code == 200, response.text
    return response.json()


async def settle(session_id: str, runtime: Loopback, tries: int = 200) -> None:
    """Let the worker thread finish. The run is scripted, so this is quick."""
    for _ in range(tries):
        session = runtime.sessions.get(session_id)
        if session and not session.running:
            return
        await asyncio.sleep(0.01)


# ── the token ───────────────────────────────────────────────────────────────


async def test_health_needs_no_token(scripted: Loopback) -> None:
    """This is what the extension polls for up to sixty seconds while deciding
    whether the runtime came up. A health check that needs a credential cannot
    tell it whether the credential path is the broken thing."""
    transport = httpx.ASGITransport(app=create_app(scripted))
    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as http:
        response = await http.get("/v1/health")
    assert response.status_code == 200
    assert response.json()["api_version"] == API_VERSION


@pytest.mark.parametrize("header", [None, "Bearer wrong", "Basic x", ""])
async def test_everything_else_needs_the_loopback_token(
    scripted: Loopback, header
) -> None:
    """Bound to 127.0.0.1, so this is not defending against the network — it is
    defending against other processes on the same machine, which on a developer
    laptop includes every postinstall script that can reach localhost."""
    transport = httpx.ASGITransport(app=create_app(scripted))
    headers = {"Authorization": header} if header is not None else {}
    async with httpx.AsyncClient(
        transport=transport, base_url="http://127.0.0.1", headers=headers
    ) as http:
        assert (await http.get("/v1/sessions")).status_code == 401
        assert (await http.post("/v1/tasks", json={"task": "x"})).status_code == 401


async def test_the_api_version_is_reported_for_pinning(client: httpx.AsyncClient) -> None:
    """Part B §15: silent version skew across a client/server boundary is the
    failure that costs the most support time."""
    payload = (await client.get("/v1/health")).json()
    assert payload["api_version"] == API_VERSION
    assert payload["version"] == "1.2.3"


# ── running a task ──────────────────────────────────────────────────────────


async def test_a_task_starts_and_finishes(
    client: httpx.AsyncClient, scripted: Loopback
) -> None:
    session = await start(client)
    await settle(session["id"], scripted)

    detail = (await client.get(f"/v1/sessions/{session['id']}")).json()
    assert detail["status"] in ("done", "unverified")
    assert detail["events"] > 0


async def test_a_task_needs_a_description(client: httpx.AsyncClient) -> None:
    assert (await client.post("/v1/tasks", json={})).status_code == 400


async def test_the_sessions_tree_lists_what_the_extension_renders(
    client: httpx.AsyncClient, scripted: Loopback
) -> None:
    session = await start(client)
    await settle(session["id"], scripted)

    listed = (await client.get("/v1/sessions")).json()["sessions"]
    assert len(listed) == 1
    entry = listed[0]
    assert set(entry) >= {"id", "task", "status", "created_at", "summary", "mutations",
                          "resumable"}


async def test_the_transcript_is_available_on_request(
    client: httpx.AsyncClient, scripted: Loopback
) -> None:
    """Not by default: a sessions tree that fetched every transcript to draw a
    list would make opening the view proportional to the whole history."""
    session = await start(client)
    await settle(session["id"], scripted)

    without = (await client.get(f"/v1/sessions/{session['id']}")).json()
    assert "transcript" not in without

    with_it = (await client.get(f"/v1/sessions/{session['id']}?transcript=true")).json()
    assert len(with_it["transcript"]) == with_it["events"]


# ── the event stream ────────────────────────────────────────────────────────


async def test_a_finished_run_replays_and_closes(
    client: httpx.AsyncClient, scripted: Loopback
) -> None:
    """A client watching a finished session is reading history. A stream that
    never ends looks to the extension exactly like a run still in progress."""
    session = await start(client)
    await settle(session["id"], scripted)

    async with client.stream("GET", f"/v1/sessions/{session['id']}/events") as response:
        assert response.status_code == 200
        body = "".join([chunk async for chunk in response.aiter_text()])

    assert "event: turn_start" in body
    assert "event: end" in body


async def test_every_event_carries_an_id(
    client: httpx.AsyncClient, scripted: Loopback
) -> None:
    """Without it a reconnect has nothing to resume from, and the client is back
    to guessing whether the run died."""
    session = await start(client)
    await settle(session["id"], scripted)

    async with client.stream("GET", f"/v1/sessions/{session['id']}/events") as response:
        body = "".join([chunk async for chunk in response.aiter_text()])

    ids = [line for line in body.split("\n") if line.startswith("id: ")]
    assert ids
    assert [int(i[4:]) for i in ids] == sorted(int(i[4:]) for i in ids)


async def test_since_id_replays_only_what_was_missed(
    client: httpx.AsyncClient, scripted: Loopback
) -> None:
    """Part B §14's gap, closed."""
    session = await start(client)
    await settle(session["id"], scripted)

    everything = (await client.get(f"/v1/sessions/{session['id']}?transcript=true")).json()
    midpoint = everything["transcript"][2]["id"]

    async with client.stream(
        "GET", f"/v1/sessions/{session['id']}/events?since_id={midpoint}"
    ) as response:
        body = "".join([chunk async for chunk in response.aiter_text()])

    resumed = [int(line[4:]) for line in body.split("\n") if line.startswith("id: ")]
    assert resumed
    assert min(resumed) == midpoint + 1


async def test_last_event_id_is_honoured_like_since_id(
    client: httpx.AsyncClient, scripted: Loopback
) -> None:
    """What a browser's EventSource sends automatically on reconnect. A client
    that does nothing special still resumes correctly."""
    session = await start(client)
    await settle(session["id"], scripted)

    async with client.stream(
        "GET",
        f"/v1/sessions/{session['id']}/events",
        headers={"Last-Event-ID": "2"},
    ) as response:
        body = "".join([chunk async for chunk in response.aiter_text()])

    resumed = [int(line[4:]) for line in body.split("\n") if line.startswith("id: ")]
    assert min(resumed) == 3


async def test_the_stream_is_not_buffered_by_a_proxy(
    client: httpx.AsyncClient, scripted: Loopback
) -> None:
    session = await start(client)
    await settle(session["id"], scripted)
    async with client.stream("GET", f"/v1/sessions/{session['id']}/events") as response:
        assert response.headers["x-accel-buffering"] == "no"
        assert response.headers["content-type"].startswith("text/event-stream")


async def test_events_for_an_unknown_session_are_a_404(client: httpx.AsyncClient) -> None:
    assert (await client.get("/v1/sessions/nope/events")).status_code == 404


# ── approval ────────────────────────────────────────────────────────────────


@pytest.fixture
def approving(scripted: Loopback):
    """A script that tries to patch go.mod, which needs approval."""
    scripted._plan["turns"] = [
        plan_for("go.mod", "change the module line"),
        calls(("patch_file", json.dumps(
            {"path": "go.mod", "old": "module pisapi", "new": "module x"}))),
        say("done"),
    ]
    return scripted


async def wait_for_approval(runtime: Loopback, session_id: str, tries: int = 300):
    for _ in range(tries):
        pending = runtime.pending_for(session_id)
        if pending:
            return pending[0]
        await asyncio.sleep(0.01)
    raise AssertionError("no approval was raised")


async def test_a_protected_path_raises_an_approval(
    client: httpx.AsyncClient, approving: Loopback
) -> None:
    session = await start(client)
    pending = await wait_for_approval(approving, session["id"])

    assert "go.mod" in pending.request.reason
    listed = (await client.get("/v1/approvals")).json()["approvals"]
    assert listed[0]["id"] == pending.id


async def test_accepting_lets_the_write_through(
    client: httpx.AsyncClient, approving: Loopback, workspace
) -> None:
    session = await start(client)
    pending = await wait_for_approval(approving, session["id"])

    response = await client.post(f"/v1/approvals/{pending.id}", json={"decision": "accept"})
    assert response.status_code == 200
    await settle(session["id"], approving)

    assert "module x" in (workspace.root / "go.mod").read_text()


async def test_rejecting_keeps_the_file_unchanged(
    client: httpx.AsyncClient, approving: Loopback, workspace
) -> None:
    session = await start(client)
    pending = await wait_for_approval(approving, session["id"])
    before = (workspace.root / "go.mod").read_bytes()

    await client.post(f"/v1/approvals/{pending.id}", json={"decision": "reject"})
    await settle(session["id"], approving)

    assert (workspace.root / "go.mod").read_bytes() == before


async def test_editing_corrects_the_arguments_and_approves(
    client: httpx.AsyncClient, approving: Loopback, workspace
) -> None:
    """§9 calls this the standout, and the reason is arithmetic: correcting a
    path costs nothing, while rejecting costs a turn and the model often makes
    the same mistake again."""
    session = await start(client)
    pending = await wait_for_approval(approving, session["id"])

    await client.post(
        f"/v1/approvals/{pending.id}",
        json={
            "decision": "edit",
            "arguments": {"path": "go.mod", "old": "module pisapi", "new": "module corrected"},
        },
    )
    await settle(session["id"], approving)

    assert "module corrected" in (workspace.root / "go.mod").read_text()


async def test_an_answered_approval_is_gone(
    client: httpx.AsyncClient, approving: Loopback
) -> None:
    """410, not 404: gone means answered, timed out, or the run ended. All three
    are "too late" rather than something the client should retry."""
    session = await start(client)
    pending = await wait_for_approval(approving, session["id"])
    await client.post(f"/v1/approvals/{pending.id}", json={"decision": "reject"})
    await settle(session["id"], approving)

    again = await client.post(f"/v1/approvals/{pending.id}", json={"decision": "accept"})
    assert again.status_code == 410


async def test_an_unknown_decision_is_refused(
    client: httpx.AsyncClient, approving: Loopback
) -> None:
    session = await start(client)
    pending = await wait_for_approval(approving, session["id"])
    response = await client.post(f"/v1/approvals/{pending.id}", json={"decision": "maybe"})
    assert response.status_code == 400
    await client.post(f"/v1/approvals/{pending.id}", json={"decision": "reject"})
    await settle(session["id"], approving)


async def test_an_edit_without_arguments_is_refused(
    client: httpx.AsyncClient, approving: Loopback
) -> None:
    session = await start(client)
    pending = await wait_for_approval(approving, session["id"])
    assert (
        await client.post(f"/v1/approvals/{pending.id}", json={"decision": "edit"})
    ).status_code == 400
    await client.post(f"/v1/approvals/{pending.id}", json={"decision": "reject"})
    await settle(session["id"], approving)


# ── abort ───────────────────────────────────────────────────────────────────


async def test_aborting_stops_the_run(
    client: httpx.AsyncClient, approving: Loopback
) -> None:
    """The endpoint has to answer *while* a run is in flight, which is why the
    loop runs on a worker thread rather than inline."""
    session = await start(client)
    await wait_for_approval(approving, session["id"])

    response = await client.post(f"/v1/sessions/{session['id']}/abort")
    assert response.status_code == 200
    await settle(session["id"], approving)

    detail = (await client.get(f"/v1/sessions/{session['id']}")).json()
    assert detail["status"] == "aborted"


async def test_aborting_releases_a_pending_approval(
    client: httpx.AsyncClient, approving: Loopback, workspace
) -> None:
    """A crashed or aborted run holding a pending approval leaves the extension
    showing a card nothing will ever answer."""
    session = await start(client)
    await wait_for_approval(approving, session["id"])
    before = (workspace.root / "go.mod").read_bytes()

    await client.post(f"/v1/sessions/{session['id']}/abort")
    await settle(session["id"], approving)

    assert approving.pending_for(session["id"]) == []
    assert (workspace.root / "go.mod").read_bytes() == before


async def test_an_aborted_session_is_resumable(
    client: httpx.AsyncClient, approving: Loopback
) -> None:
    session = await start(client)
    await wait_for_approval(approving, session["id"])
    await client.post(f"/v1/sessions/{session['id']}/abort")
    await settle(session["id"], approving)

    assert (await client.get(f"/v1/sessions/{session['id']}")).json()["resumable"]


# ── revert ──────────────────────────────────────────────────────────────────


@pytest.fixture
def git_workspace(workspace):
    """The fixture workspace, committed, so revert has a HEAD to restore from."""
    def git(*args):
        return subprocess.run(
            ["git", *args], cwd=workspace.root, capture_output=True, text=True, check=False
        )

    if git("init", "-q").returncode != 0:
        pytest.skip("git is not available")
    git("config", "user.email", "test@example.invalid")
    git("config", "user.name", "test")
    git("add", "-A")
    git("commit", "-q", "-m", "baseline")
    return workspace


async def test_a_revert_plan_lists_the_exact_paths(
    client: httpx.AsyncClient, scripted: Loopback, git_workspace
) -> None:
    """§12 asks for the confirmation to list them, because "revert my last task"
    is easy to fire by accident."""
    scripted._plan["turns"] = [
        plan_for("handler/pension.go", "add the handler"),
        calls(("write_file", json.dumps(
            {"path": "handler/pension.go", "content": "package handler"}))),
        say("done"),
    ]
    session = await start(client)
    await settle(session["id"], scripted)

    plan = (await client.get(f"/v1/sessions/{session['id']}/revert")).json()
    assert "handler/pension.go" in plan["delete"]
    assert plan["blocked"] == []


async def test_revert_deletes_what_the_session_created(
    client: httpx.AsyncClient, scripted: Loopback, git_workspace
) -> None:
    scripted._plan["turns"] = [
        plan_for("handler/pension.go", "add the handler"),
        calls(("write_file", json.dumps(
            {"path": "handler/pension.go", "content": "package handler"}))),
        say("done"),
    ]
    session = await start(client)
    await settle(session["id"], scripted)
    assert (git_workspace.root / "handler/pension.go").exists()

    await client.post(f"/v1/sessions/{session['id']}/revert")
    assert not (git_workspace.root / "handler/pension.go").exists()


async def test_revert_restores_what_the_session_changed(
    client: httpx.AsyncClient, scripted: Loopback, git_workspace
) -> None:
    original = (git_workspace.root / "handler/user.go").read_bytes()
    scripted._plan["turns"] = [
        plan_for("handler/pension.go", "patch the handler"),
        calls(("patch_file", json.dumps(
            {"path": "handler/user.go", "old": "func New()", "new": "func Renamed()"}))),
        say("done"),
    ]
    session = await start(client)
    await settle(session["id"], scripted)
    assert (git_workspace.root / "handler/user.go").read_bytes() != original

    await client.post(f"/v1/sessions/{session['id']}/revert")
    assert (git_workspace.root / "handler/user.go").read_bytes() == original


async def test_a_running_session_cannot_be_reverted(
    client: httpx.AsyncClient, approving: Loopback
) -> None:
    """The guard exists because reverting files a run is still writing produces
    a tree that matches neither."""
    session = await start(client)
    await wait_for_approval(approving, session["id"])

    response = await client.post(f"/v1/sessions/{session['id']}/revert")
    assert response.status_code == 409

    await client.post(f"/v1/sessions/{session['id']}/abort")
    await settle(session["id"], approving)


async def test_revert_outside_a_git_repository_is_blocked_not_attempted(
    client: httpx.AsyncClient, scripted: Loopback
) -> None:
    """Blocked with a reason rather than silently doing nothing: a revert that
    reports success and changes nothing is the worst of both."""
    scripted._plan["turns"] = [
        plan_for("handler/pension.go", "add the handler"),
        calls(("write_file", json.dumps({"path": "a/b.go", "content": "package b"}))),
        say("done"),
    ]
    session = await start(client)
    await settle(session["id"], scripted)

    plan = (await client.post(f"/v1/sessions/{session['id']}/revert")).json()
    assert plan["blocked"]
    assert "git repository" in plan["blocked"][0]["reason"]


# ── housekeeping ────────────────────────────────────────────────────────────


async def test_a_running_session_cannot_be_deleted(
    client: httpx.AsyncClient, approving: Loopback
) -> None:
    session = await start(client)
    await wait_for_approval(approving, session["id"])
    assert (await client.delete(f"/v1/sessions/{session['id']}")).status_code == 409

    await client.post(f"/v1/sessions/{session['id']}/abort")
    await settle(session["id"], approving)


async def test_a_finished_session_can_be_deleted(
    client: httpx.AsyncClient, scripted: Loopback
) -> None:
    session = await start(client)
    await settle(session["id"], scripted)

    assert (await client.delete(f"/v1/sessions/{session['id']}")).status_code == 200
    assert (await client.get(f"/v1/sessions/{session['id']}")).status_code == 404


async def test_the_tool_catalogue_is_served_for_the_approval_ui(
    client: httpx.AsyncClient,
) -> None:
    assert (await client.get("/v1/tools")).json()["contract"] == "C1"
