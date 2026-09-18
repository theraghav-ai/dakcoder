"""The control plane, end to end: lease, run, deliver, release (host-plan Phase 2)."""

from __future__ import annotations

import asyncio
import time
from datetime import date, timedelta
from pathlib import Path

import pytest
from fastapi.routing import APIRoute

from dakcoder_agentsvc.repos import AllowedRepo, Allowlist, Git
from dakcoder_agentsvc.service import Service

from support import BASE, StandInAgent, as_caller, remote_branches, remote_files


async def lease(http, remote: str) -> dict:
    response = await http.post("/v1/workspaces", json={"repo_url": remote, "ref": BASE})
    assert response.status_code == 201, response.text
    return response.json()


async def run(http, workspace: str, task: str) -> dict:
    response = await http.post(f"/v1/workspaces/{workspace}/tasks", json={"task": task})
    assert response.status_code == 200, response.text
    return response.json()


async def settled(http, session_id: str) -> dict:
    for _ in range(200):
        detail = (await http.get(f"/v1/sessions/{session_id}")).json()
        if detail.get("status") != "running" and detail.get("events", 0) >= 3:
            return detail
        await asyncio.sleep(0.02)
    raise AssertionError(f"session {session_id} never settled")


# ── leases ──────────────────────────────────────────────────────────────────


async def test_a_lease_is_a_mirror_and_a_working_copy(app, remote, service: Service) -> None:
    async with as_caller(app, "alice") as alice:
        leased = await lease(alice, remote)
    assert "path" not in leased, "a caller is never shown where it lives on the server"
    stored = service.store.lease(leased["id"], "alice")
    lease_dir = Path(stored.path)
    assert (Git.mirror(lease_dir) / "HEAD").is_file()
    assert (Git.worktree(lease_dir) / "handler" / "user.go").is_file()
    assert "service-token" not in (Git.mirror(lease_dir) / "config").read_text(), (
        "the credential is never written into git's config"
    )


async def test_only_listed_repositories_can_be_leased(app, service: Service, remote) -> None:
    async with as_caller(app, "alice") as alice:
        other = await alice.post("/v1/workspaces", json={"repo_url": "https://gitlab/other.git"})
        service.allowlist = Allowlist(
            [AllowedRepo(repo=remote, owner="Ops", expires=date.today() - timedelta(days=1))]
        )
        expired = await alice.post("/v1/workspaces", json={"repo_url": remote, "ref": BASE})
    assert other.status_code == 403
    assert expired.status_code == 403, "an entry past its expiry is refused like any other"


async def test_a_caller_may_hold_only_so_many_leases(app, remote) -> None:
    async with as_caller(app, "alice") as alice:
        await lease(alice, remote)
        await lease(alice, remote)
        third = await alice.post("/v1/workspaces", json={"repo_url": remote, "ref": BASE})
    assert third.status_code == 429


async def test_another_callers_workspace_does_not_exist(app, remote) -> None:
    async with as_caller(app, "alice") as alice:
        theirs = await lease(alice, remote)
    async with as_caller(app, "bob") as bob:
        assert (await bob.get("/v1/workspaces")).json()["workspaces"] == []
        for method, path, body in [
            ("POST", f"/v1/workspaces/{theirs['id']}/tasks", {"task": "write x.go"}),
            ("GET", f"/v1/workspaces/{theirs['id']}/agenda", None),
            ("DELETE", f"/v1/workspaces/{theirs['id']}", None),
        ]:
            response = await bob.request(method, path, json=body)
            assert response.status_code == 404, (method, path, response.text)


# ── runs ────────────────────────────────────────────────────────────────────


async def test_a_run_belongs_to_its_caller_all_the_way_down(app, remote, backend) -> None:
    async with as_caller(app, "alice") as alice:
        workspace = await lease(alice, remote)
        session = await run(alice, workspace["id"], "write handler/pension.go")
        detail = await settled(alice, session["id"])
        listed = (await alice.get("/v1/sessions")).json()["sessions"]
    async with as_caller(app, "bob") as bob:
        assert (await bob.get(f"/v1/sessions/{session['id']}")).status_code == 404
        assert (await bob.get(f"/v1/sessions/{session['id']}/transcript")).status_code == 404
        assert (await bob.get("/v1/sessions")).json()["sessions"] == []

    assert detail["status"] == "done"
    assert session["workspace_id"] == workspace["id"]
    assert [s["id"] for s in listed] == [session["id"]]
    runtime = backend.runtimes[workspace["id"]]
    assert runtime.sessions.get(session["id"]).owner == "alice", "the runner knows the caller too"


async def test_one_run_at_a_time_on_a_workspace(app, remote) -> None:
    async with as_caller(app, "alice") as alice:
        workspace = await lease(alice, remote)
        first = await run(alice, workspace["id"], "wait")
        second = await alice.post(f"/v1/workspaces/{workspace['id']}/tasks", json={"task": "noop"})
        StandInAgent.release.set()
        await settled(alice, first["id"])
    assert second.status_code == 409


async def test_the_event_stream_comes_through(app, remote) -> None:
    async with as_caller(app, "alice") as alice:
        workspace = await lease(alice, remote)
        session = await run(alice, workspace["id"], "write a.go")
        await settled(alice, session["id"])
        stream = await alice.get(f"/v1/sessions/{session['id']}/events")
    assert stream.headers["content-type"].startswith("text/event-stream")
    assert "event: finish" in stream.text and "event: end" in stream.text


async def test_a_forwarded_path_cannot_climb_out_of_its_session(app, remote) -> None:
    async with as_caller(app, "alice") as alice:
        workspace = await lease(alice, remote)
        session = await run(alice, workspace["id"], "noop")
        response = await alice.get(f"/v1/sessions/{session['id']}/..%2F..%2Fapprovals")
    assert response.status_code == 404


# ── the working tree's discipline, and delivery ─────────────────────────────


async def test_each_session_delivers_only_its_own_changes(app, remote, gitlab) -> None:
    async with as_caller(app, "alice") as alice:
        workspace = await lease(alice, remote)
        a = await run(alice, workspace["id"], "write handler/a.go")
        await settled(alice, a["id"])
        # B starts from the base: A's change goes to A's branch, not into B's tree.
        b = await run(alice, workspace["id"], "write handler/b.go")
        await settled(alice, b["id"])

        delivered_b = await alice.post(
            f"/v1/sessions/{b['id']}/deliver", json={"title": "Add b", "description": "B."}
        )
        delivered_a = await alice.post(f"/v1/sessions/{a['id']}/deliver", json={"title": "Add a"})
        again = await alice.post(f"/v1/sessions/{a['id']}/deliver", json={"title": "Add a, retitled"})

    assert delivered_a.status_code == delivered_b.status_code == 200, delivered_a.text
    base = sorted(remote_files(remote, BASE))
    assert sorted(remote_files(remote, f"dakcoder/{a['id']}")) == sorted([*base, "handler/a.go"])
    assert sorted(remote_files(remote, f"dakcoder/{b['id']}")) == sorted([*base, "handler/b.go"])

    methods = [m for m, _path, _ in gitlab.calls if m != "GET"]
    assert methods == ["POST", "POST", "PUT"], "a second delivery updates, never opens another"
    assert again.json()["mr_url"] == delivered_a.json()["mr_url"]
    opened = [body for m, _p, body in gitlab.calls if m == "POST"][0]
    assert (opened["source_branch"], opened["target_branch"]) == (f"dakcoder/{b['id']}", BASE)


async def test_a_run_that_did_not_finish_cleanly_needs_a_stated_reason(app, remote, gitlab) -> None:
    async with as_caller(app, "alice") as alice:
        workspace = await lease(alice, remote)
        failed = await run(alice, workspace["id"], "fail")
        await settled(alice, failed["id"])
        (Path(alice._transport.app.state.service.store.lease(workspace["id"], "alice").path)
         / "repo" / "fix.go").write_text("package main\n", encoding="utf-8")
        refused = await alice.post(f"/v1/sessions/{failed['id']}/deliver", json={"title": "Fix"})
        overridden = await alice.post(
            f"/v1/sessions/{failed['id']}/deliver",
            json={"title": "Fix", "override": "the gate's linter is broken upstream"},
        )
    assert refused.status_code == 409
    assert overridden.status_code == 200, overridden.text
    body = [b for m, _p, b in gitlab.calls if m == "POST"][0]["description"]
    assert "the gate's linter is broken upstream" in body


async def test_nothing_to_deliver_and_nothing_while_running(app, remote) -> None:
    async with as_caller(app, "alice") as alice:
        workspace = await lease(alice, remote)
        idle = await run(alice, workspace["id"], "noop")
        await settled(alice, idle["id"])
        nothing = await alice.post(f"/v1/sessions/{idle['id']}/deliver", json={"title": "x"})
        busy = await run(alice, workspace["id"], "wait")
        running = await alice.post(f"/v1/sessions/{busy['id']}/deliver", json={"title": "x"})
        StandInAgent.release.set()
        await settled(alice, busy["id"])
    assert nothing.status_code == 409
    assert running.status_code == 409


async def test_the_runner_never_pushes_and_never_holds_the_credential(app, remote, service, backend) -> None:
    """The branch reaches the remote only through delivery, and the runner's
    working copy has no route to the remote with a credential in it."""
    async with as_caller(app, "alice") as alice:
        workspace = await lease(alice, remote)
        session = await run(alice, workspace["id"], "write x.go")
        await settled(alice, session["id"])
    assert remote_branches(remote) == [BASE]
    worktree_config = (
        Path(service.store.lease(workspace["id"], "alice").path) / "repo" / ".git" / "config"
    ).read_text()
    assert "service-token" not in worktree_config and "gitlab.example" not in worktree_config


# ── release, and the reaper ─────────────────────────────────────────────────


async def test_releasing_keeps_the_sessions_and_drops_the_clone(app, remote, service, settings) -> None:
    async with as_caller(app, "alice") as alice:
        workspace = await lease(alice, remote)
        session = await run(alice, workspace["id"], "write a.go")
        await settled(alice, session["id"])
        lease_dir = Path(service.store.lease(workspace["id"], "alice").path)
        released = await alice.delete(f"/v1/workspaces/{workspace['id']}")
        after = await alice.get(f"/v1/sessions/{session['id']}")
    assert released.status_code == 200
    assert not lease_dir.exists()
    archived = settings.archive_dir / "alice" / workspace["id"] / session["id"]
    assert archived.is_dir(), "the transcript outlives the clone"
    assert after.status_code == 410


async def test_a_workspace_is_not_released_under_a_running_session(app, remote) -> None:
    async with as_caller(app, "alice") as alice:
        workspace = await lease(alice, remote)
        busy = await run(alice, workspace["id"], "wait")
        refused = await alice.delete(f"/v1/workspaces/{workspace['id']}")
        StandInAgent.release.set()
        await settled(alice, busy["id"])
    assert refused.status_code == 409


async def test_the_reaper_stops_idle_runners_and_retires_expired_leases(app, remote, service, backend) -> None:
    async with as_caller(app, "alice") as alice:
        workspace = await lease(alice, remote)
        session = await run(alice, workspace["id"], "write a.go")
        await settled(alice, session["id"])

    later = time.time() + service.settings.runner_idle.total_seconds() + 5
    service.clock = lambda: later
    reaped = await service.reap()
    assert reaped["stopped"] == [workspace["id"]] and reaped["expired"] == []
    assert service.runners.get(workspace["id"]) is None

    async with as_caller(app, "alice") as alice:
        detail = await alice.get(f"/v1/sessions/{session['id']}")
    assert detail.status_code == 200, "a reaped runner starts again, and remembers the session"
    assert backend.started.count(workspace["id"]) == 2

    much_later = time.time() + service.settings.lease_ttl.total_seconds() + 5
    service.clock = lambda: much_later
    reaped = await service.reap()
    assert reaped["expired"] == [workspace["id"]]


async def test_the_reaper_never_stops_a_running_session(app, remote, service) -> None:
    async with as_caller(app, "alice") as alice:
        workspace = await lease(alice, remote)
        busy = await run(alice, workspace["id"], "wait")
        later = time.time() + service.settings.lease_ttl.total_seconds() + 5
        service.clock = lambda: later
        reaped = await service.reap()
        StandInAgent.release.set()
        service.clock = time.time
        await settled(alice, busy["id"])
    assert (reaped["stopped"], reaped["expired"]) == ([], [])
    assert reaped["refreshed"] == [workspace["id"]], "a long run is exactly when its token runs out"


# ── the route table (§8, as in the runtime) ─────────────────────────────────


def dependencies(route: APIRoute) -> set[str]:
    names: set[str] = set()
    pending = list(route.dependant.dependencies)
    while pending:
        dependant = pending.pop()
        if dependant.call is not None:
            names.add(dependant.call.__name__)
        pending.extend(dependant.dependencies)
    return names


def test_every_route_takes_what_it_names_through_an_ownership_check(app) -> None:
    rules = {"{workspace_id}": "workspace", "{session_id}": "session_runner", "{approval_id}": "approval_runner"}
    unscoped = [
        (route.path, needed)
        for route in app.routes
        if isinstance(route, APIRoute)
        for marker, needed in rules.items()
        if marker in route.path and needed not in dependencies(route)
    ]
    assert unscoped == []
    anonymous = [
        route.path
        for route in app.routes
        if isinstance(route, APIRoute) and route.path != "/v1/health" and "caller" not in dependencies(route)
    ]
    assert anonymous == []


# ── whose account a hosted run is charged to (host-plan §8) ─────────────────


async def test_a_runner_calls_the_model_as_its_leases_owner(app, remote, backend, service) -> None:
    async with as_caller(app, "alice") as alice:
        workspace = await lease(alice, remote)
        session = await run(alice, workspace["id"], "noop")
        await settled(alice, session["id"])
    assert backend.credentials[workspace["id"]] == "delegated:alice", (
        "one service token for every runner would charge one account for every hosted run"
    )
    assert backend.runtimes[workspace["id"]].credential() == "delegated:alice"


async def test_a_runners_token_is_replaced_before_it_expires(app, remote, backend, service) -> None:
    async with as_caller(app, "alice") as alice:
        workspace = await lease(alice, remote)
        await run(alice, workspace["id"], "noop")
    runner = service.runners.get(workspace["id"])
    backend.runtimes[workspace["id"]].set_credential("old")
    runner.credential_expires = time.time() + 60
    refreshed = await service.refresh_credentials()
    assert refreshed == [workspace["id"]]
    assert backend.runtimes[workspace["id"]].credential() == "delegated:alice"
    assert runner.credential_expires > time.time() + 3600


async def test_without_a_credential_for_runners_nothing_starts(app, remote, service) -> None:
    from dakcoder_agentsvc.credentials import Credentials

    service.credentials = Credentials("http://gateway")
    async with as_caller(app, "alice") as alice:
        workspace = await lease(alice, remote)
        response = await alice.post(f"/v1/workspaces/{workspace['id']}/tasks", json={"task": "x"})
    assert response.status_code == 503


async def test_the_shared_token_fallback_still_works(app, remote, service, backend) -> None:
    from dakcoder_agentsvc.credentials import Credentials

    service.credentials = Credentials("http://gateway", static="one-token-for-all")
    async with as_caller(app, "alice") as alice:
        workspace = await lease(alice, remote)
        await run(alice, workspace["id"], "noop")
    assert backend.credentials[workspace["id"]] == "one-token-for-all"


async def test_the_session_list_pages_over_everything_the_caller_ran(app, remote, service) -> None:
    """A runner keeps its latest 200; the registry keeps them all (§9.1)."""
    from dakcoder_agentsvc.store import SessionRow

    async with as_caller(app, "alice") as alice:
        workspace = await lease(alice, remote)
        for i in range(5):
            service.store.add_session(
                SessionRow(f"s{i}", workspace["id"], "alice", f"dakcoder/s{i}", f"task {i}", 1000.0 + i, {})
            )
        first = (await alice.get("/v1/sessions", params={"limit": 2})).json()
        second = (await alice.get("/v1/sessions", params={"limit": 2, "before": first["next"]})).json()
        last = (await alice.get("/v1/sessions", params={"limit": 2, "before": second["next"]})).json()

    assert [s["id"] for s in first["sessions"]] == ["s4", "s3"]
    assert [s["id"] for s in second["sessions"]] == ["s2", "s1"]
    assert [s["id"] for s in last["sessions"]] == ["s0"] and "next" not in last
