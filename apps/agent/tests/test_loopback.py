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
import threading
from datetime import datetime, timedelta, timezone
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
from dakcoder_agent.tools.router import ApprovalRequest, Router
from dakcoder_agent.undo import UndoStore
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
        # Per session, exactly as `serve.build_loop` does it: the pre-run
        # snapshot is what `revert` restores from, and a fixture without one
        # would exercise a revert path production never takes.
        router.undo = UndoStore(workspace.root, session.id)
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
    """Let the worker thread finish, *and* its events reach the transcript.

    `session.running` is set on the worker thread while the events travel by
    `call_soon_threadsafe`, so the status can go terminal with the last few
    records still queued as callbacks. Waiting on the status alone let a test
    read a transcript that was missing its final `assistant` and `end` — rarely,
    and only under whatever scheduling the rest of the suite happened to
    produce. `end` is the run's own statement that it is over, so that is what
    is waited for.
    """
    for _ in range(tries):
        session = runtime.sessions.get(session_id)
        if session and not session.running:
            if any(str(e.type) == "end" for e in session.events):
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


async def test_revert_works_outside_a_git_repository(
    client: httpx.AsyncClient, scripted: Loopback, workspace
) -> None:
    """git is no longer the source of truth, so its absence no longer blocks.

    Revert used to ask HEAD what was at each path and had to give up without a
    repository. The run now records what it found before it wrote (BUG L-11),
    which is both safer on a dirty tree and available in a directory that was
    never a repository at all.
    """
    scripted._plan["turns"] = [
        plan_for("handler/pension.go", "add the handler"),
        calls(("write_file", json.dumps({"path": "a/b.go", "content": "package b"}))),
        say("done"),
    ]
    session = await start(client)
    await settle(session["id"], scripted)
    assert (workspace.root / "a/b.go").exists()

    plan = (await client.post(f"/v1/sessions/{session['id']}/revert")).json()
    assert plan["blocked"] == []
    assert "a/b.go" in plan["delete"]
    assert not (workspace.root / "a/b.go").exists()


async def test_revert_blocks_a_path_it_has_no_snapshot_for(
    client: httpx.AsyncClient, scripted: Loopback, git_workspace
) -> None:
    """The safe direction when the run does not know what was there.

    A path can reach this state through a tool that writes without going through
    the router's snapshot, or through a session that predates the store. The old
    behaviour — restore it from HEAD — is a guess with a developer's uncommitted
    work as the stake, so it is refused with the reason said out loud.
    """
    session = await start(client)
    await settle(session["id"], scripted)

    stored = scripted.sessions.get(session["id"])
    stored.mutations.append("handler/user.go")  # nothing snapshotted this one

    plan = (await client.post(f"/v1/sessions/{session['id']}/revert")).json()
    reasons = {b["path"]: b["reason"] for b in plan["blocked"]}
    assert "handler/user.go" in reasons
    assert "no pre-run snapshot" in reasons["handler/user.go"]


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


async def test_steer_never_lost_on_finish_race(
    client: httpx.AsyncClient, scripted: Loopback
) -> None:
    """A message typed as the run ends becomes the next message, not silence.

    `message_session` saw `running`, queued a steer, and the run finished before
    the next drain: the text was never delivered, never recorded, and never
    became a follow-up (BUG L-9). The window is closed atomically now — the
    queue refuses once the run has ended — so the endpoint sends it as a
    follow-up instead.
    """
    session = await start(client)
    await settle(session["id"], scripted)

    stored = scripted.sessions.get(session["id"])
    # The exact window: the worker has marked the run finished and closed the
    # correction queue, and the developer's message is already in flight.
    stored.close_steer()

    response = await client.post(
        f"/v1/sessions/{session['id']}/messages", json={"text": "and add the index"}
    )
    assert response.status_code == 200
    await settle(session["id"], scripted)

    transcript = (
        await client.get(f"/v1/sessions/{session['id']}?transcript=1")
    ).json()
    assert "and add the index" in json.dumps(transcript), (
        "the developer's message must reach the transcript one way or another"
    )


async def test_a_leftover_steer_starts_a_follow_up(
    client: httpx.AsyncClient, scripted: Loopback
) -> None:
    """What the worker owes the developer when it finds an undrained correction."""
    session = await start(client)
    await settle(session["id"], scripted)

    stored = scripted.sessions.get(session["id"])
    stored.reopen_steer()
    assert stored.steer("one more thing")

    runtime_loop = asyncio.get_running_loop()
    scripted._rescue_steers(stored, runtime_loop)
    await asyncio.sleep(0)
    await settle(session["id"], scripted)

    transcript = (
        await client.get(f"/v1/sessions/{session['id']}?transcript=1")
    ).json()
    assert "one more thing" in json.dumps(transcript)


# ── approvals: EXT-1, EXT-2, L-22 ──────────────────────────────────────────


def test_extending_an_approval_extends_the_wait(scripted: Loopback) -> None:
    """"Give me more time" has to reach the thread that is counting.

    `/extend` incremented a counter the blocked `Event.wait(timeout=...)` had
    already read: the UI showed minutes remaining and the run rejected the
    approval at the original deadline anyway (BUG EXT-1). The wait polls now, so
    the deadline is re-read while it is still being waited on.
    """
    from dakcoder_agent import loopback as lb

    session = scripted.sessions.create("scaffold the service")
    request = ApprovalRequest("write_file", {"path": "a.go"}, reason="protected path")
    pending = lb.PendingApproval(request.id, session.id, request)
    scripted.approvals[pending.id] = pending

    # Already past the original deadline, and extended.
    pending.at = datetime.now(tz=timezone.utc) - timedelta(seconds=lb.APPROVAL_TIMEOUT + 5)
    assert pending.deadline_in() == 0
    pending.extensions = 1
    assert pending.deadline_in() > 0, "an extension must put time back on the clock"

    decided: list[bool] = []
    waiter = threading.Thread(target=lambda: decided.append(
        scripted._await_decision(session, request)))
    waiter.start()
    pending.approved = True
    pending.decided.set()
    waiter.join(timeout=10)

    assert decided == [True]


async def test_a_decision_after_the_timeout_is_refused(
    client: httpx.AsyncClient, scripted: Loopback
) -> None:
    """A receipt saying "accepted" for a call the run rejected is worse than a 410."""
    from dakcoder_agent import loopback as lb

    session = scripted.sessions.create("scaffold the service")
    request = ApprovalRequest("write_file", {"path": "a.go"}, reason="protected path")
    pending = lb.PendingApproval(request.id, session.id, request)
    pending.timed_out = True
    scripted.approvals[pending.id] = pending

    response = await client.post(
        f"/v1/approvals/{pending.id}", json={"decision": "accept"}
    )
    assert response.status_code == 410
    assert "timed out" in response.json()["error"]


def test_a_zero_timeout_means_no_deadline(monkeypatch) -> None:
    """The extension's setting documents "0 waits indefinitely"; it now does."""
    from dakcoder_agent import loopback as lb

    monkeypatch.setattr(lb, "APPROVAL_TIMEOUT", 0.0)
    request = ApprovalRequest("write_file", {"path": "a.go"}, reason="protected path")
    pending = lb.PendingApproval(request.id, "s", request)
    pending.at = datetime.now(tz=timezone.utc) - timedelta(hours=4)
    assert pending.deadline_in() == float("inf")


# ── a restart keeps the conversation, not only the record ──────────────────


async def test_a_second_daemon_continues_the_conversation_from_disk(
    scripted: Loopback, client: httpx.AsyncClient, workspace, router: Router
) -> None:
    """The end-to-end shape of the reload a developer actually does.

    Run a task, throw the runtime away exactly as a VS Code window reload does,
    build a second one over the same workspace, and ask it to continue. Before
    this, the second runtime held no context for the session, so `follow_up`
    re-seeded the original task and the agent began again — with the transcript
    that proved it had already done the work on screen beside it.
    """
    from dakcoder_agent.context import ContextManager
    from dakcoder_agent.modes import Mode

    session = await start(client, "Add a Pension resource")
    await settle(session["id"], scripted)

    first = scripted.sessions.get(session["id"])
    assert first is not None and first.journal is not None
    first.journal.flush()
    stored = first.journal.read_events()
    assert any(e.get("type") == "tool_result" for e in stored), "there is a conversation on disk"

    class _Loop:
        """All `_restore_context` asks of a loop is somewhere to put the messages."""

        context = ContextManager(mode=Mode.ASK, system_prompt="sys")

    def build(session, approve):  # pragma: no cover - never run in this test
        raise AssertionError("the restore path must not need a model")

    # A new daemon over the same workspace: nothing carried in memory.
    second = Loopback(workspace.root, build, token=TOKEN, version="1.2.3")
    assert second.contexts == {}, "a fresh daemon holds nothing"

    restored = second.sessions.get(session["id"])
    assert restored is not None, "the session itself came back from disk"

    rebuilt = second._restore_context(restored, _Loop())

    assert rebuilt is not None, "the conversation was rebuilt, not re-seeded"
    text = "\n".join(m.content for m in rebuilt.build())
    assert "Add a Pension resource" in text
    assert "handler/pension.go" in text, "the work the first run did is still in context"
    rebuilt.wire()
    assert rebuilt.wire_repairs == (), "and the rebuilt wire is well formed"
    assert second.contexts[restored.id] is rebuilt, "and it is held for the follow-up"
