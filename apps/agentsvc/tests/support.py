"""Helpers and fixtures for the control plane (conftest.py registers the fixtures).

The runners here are the real hosted runtime (``dakcoder_agent.loopback`` with
``gateway_forwarded``), in process, one per lease, over the lease's real working
copy. Only the agent inside is a stand-in: it does what the task text says,
which is enough to exercise every path a real run takes through the control
plane (a change written, a run failed, a run still going) without a model.

The remote is a real git repository on disk, so clone, snapshot, reset and push
are real git.
"""

from __future__ import annotations

import json
import subprocess
import threading
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest

from dakcoder_agent.loop import RunResult
from dakcoder_agent.loopback import Loopback
from dakcoder_agent.loopback import create_app as create_runtime
from dakcoder_agentsvc.app import create_app
from dakcoder_agentsvc.config import Settings
from dakcoder_agentsvc.gitlab import GitLab
from dakcoder_agentsvc.repos import Allowlist, AllowedRepo, Git
from dakcoder_agentsvc.runners import Runners
from dakcoder_agentsvc.service import Service
from dakcoder_agentsvc.store import Store
from dakcoder_shared.callers import CALLER_HEADER, gateway_forwarded
from dakcoder_shared.envelope import Event, EventType

CP_TOKEN = "control-plane-token"
BASE = "development"


def git(*args: str, cwd: Path | None = None) -> str:
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
        cwd=cwd, capture_output=True, text=True, check=True,
    ).stdout.strip()


@pytest.fixture
def remote(tmp_path: Path) -> str:
    """A repository standing in for GitLab: `development` with two files."""
    origin = tmp_path / "gitlab" / "group" / "service.git"
    origin.parent.mkdir(parents=True)
    git("init", "--bare", "-b", BASE, str(origin))
    seed = tmp_path / "seed"
    git("clone", str(origin), str(seed))
    git("checkout", "-b", BASE, cwd=seed)
    (seed / "main.go").write_text("package main\n", encoding="utf-8")
    (seed / "handler").mkdir()
    (seed / "handler" / "user.go").write_text("package handler\n", encoding="utf-8")
    git("add", "-A", cwd=seed)
    git("commit", "-m", "base", cwd=seed)
    git("push", "origin", BASE, cwd=seed)
    return origin.as_uri()


def remote_path(url: str) -> str:
    from urllib.parse import unquote, urlparse
    from urllib.request import url2pathname

    return url2pathname(unquote(urlparse(url).path))


def remote_files(url: str, branch: str) -> list[str]:
    return git("--git-dir", remote_path(url), "ls-tree", "-r", "--name-only", branch).split()


def remote_branches(url: str) -> list[str]:
    listed = git("--git-dir", remote_path(url), "branch", "--format=%(refname:short)")
    return sorted(listed.split())


# ── a stand-in agent ────────────────────────────────────────────────────────


class _Context:
    turn = 1

    def attach_journal(self, _journal) -> None:
        pass


class _Router:
    def __init__(self) -> None:
        self.touched: tuple[str, ...] = ()


class StandInAgent:
    """Does what the task says. `write a/b.go` writes it; `fail` ends in error;
    `wait` runs until `release` is set; anything else changes nothing."""

    release = threading.Event()

    def __init__(self, worktree: Path) -> None:
        self.worktree = worktree
        self.context = _Context()
        self.router = _Router()
        self.result: RunResult | None = None

    def run(self, task: str, **_kw: Any):
        words = task.split()
        outcome = "done"
        written: list[str] = []
        if words[:1] == ["write"]:
            for path in words[1:]:
                target = self.worktree / path
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(f"// written for: {task}\n", encoding="utf-8")
                written.append(path)
        elif words[:1] == ["fail"]:
            outcome = "error"
        elif words[:1] == ["wait"]:
            StandInAgent.release.wait(10)
        self.router.touched = tuple(written)
        self.result = RunResult(outcome, f"{outcome}: {task}", 1, tuple(written))
        yield Event(EventType.FINISH, self.result.as_dict())
        yield Event(EventType.END, self.result.as_dict())


class InProcessBackend:
    """Runners as in-process ASGI apps: the real hosted runtime, no sockets."""

    def __init__(self) -> None:
        self.apps: dict[str, Any] = {}
        self.runtimes: dict[str, Loopback] = {}
        self.started: list[str] = []

    def start(self, lease_id: str, worktree: Path, token: str) -> tuple[str, Any]:
        runtime = Loopback(worktree, lambda _s, _a: StandInAgent(worktree), token=token)
        url = f"http://runner-{lease_id}-{len(self.started)}"
        self.apps[url] = create_runtime(runtime, authenticate=gateway_forwarded(lambda: token))
        self.runtimes[lease_id] = runtime
        self.started.append(lease_id)
        return url, url

    def stop(self, handle: str) -> None:
        self.apps.pop(handle, None)

    def alive(self, handle: str) -> bool:
        return handle in self.apps

    def transport_for(self, url: str) -> httpx.AsyncBaseTransport:
        return httpx.ASGITransport(app=self.apps[url])


class FakeGitLab:
    """GitLab's merge request API, remembering every call."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, Any]] = []
        self.open: dict[str, dict[str, Any]] = {}

    def handle(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else None
        self.calls.append((request.method, request.url.path, body or dict(request.url.params)))
        if request.method == "GET":
            source = request.url.params.get("source_branch")
            return httpx.Response(200, json=[self.open[source]] if source in self.open else [])
        if request.method == "POST":
            mr = {"iid": len(self.open) + 1, "web_url": f"https://gitlab/mr/{len(self.open) + 1}"}
            self.open[body["source_branch"]] = mr
            return httpx.Response(201, json=mr)
        iid = int(request.url.path.rsplit("/", 1)[-1])
        return httpx.Response(200, json={"iid": iid, "web_url": f"https://gitlab/mr/{iid}"})


@pytest.fixture
def backend() -> InProcessBackend:
    return InProcessBackend()


@pytest.fixture
def gitlab() -> FakeGitLab:
    return FakeGitLab()


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        data_dir=tmp_path / "data",
        token=CP_TOKEN,
        gateway_url="http://gateway",
        runner_jwt="runner-jwt",
        max_leases_per_user=2,
        max_running_per_user=2,
    )


@pytest.fixture
def service(settings: Settings, remote: str, backend: InProcessBackend, gitlab: FakeGitLab) -> Service:
    settings.leases_dir.mkdir(parents=True)
    settings.archive_dir.mkdir(parents=True)
    allowlist = Allowlist(
        [AllowedRepo(repo=remote, owner="Ops Person", expires=date.today() + timedelta(days=30))]
    )
    StandInAgent.release.clear()
    return Service(
        settings,
        Store(settings.data_dir / "registry.sqlite3"),
        allowlist,
        Git(),
        Runners(backend, transport_for=backend.transport_for),
        GitLab("https://gitlab.example", "service-token", transport=httpx.MockTransport(gitlab.handle)),
    )


@pytest.fixture
def app(service: Service):
    return create_app(service, authenticate=gateway_forwarded(lambda: CP_TOKEN))


def as_caller(app, sub: str) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://agentsvc",
        headers={"Authorization": f"Bearer {CP_TOKEN}", CALLER_HEADER: sub},
    )
