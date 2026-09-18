"""One runner per workspace lease: a ``dakcoderd --hosted`` over its clone.

**Per lease, not per session** — a deliberate change from host-plan §7.2's "one
container per session". Sessions on a lease share its working tree and its
``.dakcoder/`` state (the agenda, the migration roadmap, the transcripts), and
two containers writing one working tree would corrupt each other's changes. So
a lease has at most one runner and at most one *running* session at a time,
and the isolation boundary is the lease: one caller, one repository. A caller
who wants two runs at once leases the repository twice.

A runner is started when a lease is first used and stopped when it has been
idle for ``runner_idle``. Stopping loses nothing: the runtime writes every
session to the lease's volume as it goes, and restores them all when it starts
again (``SessionStore.restore``, ``rehydrate``), so a follow-up on a session
whose runner was reaped simply starts one.

Two backends:

``process``
    A local ``dakcoderd`` per lease. **No isolation**: the runner runs as the
    control plane's own user on the same host, and repository code it builds
    can read anything that user can. For a single trusted host and for tests.

``docker``
    §7.2's container: read-only root, no capabilities, no new privileges,
    non-root, CPU / memory / PID limits, the lease's working copy the only
    writable mount besides ``/tmp``. Network policy is the operator's: the
    network named by ``runner_network`` must reach the gateway and nothing
    else, because the runtime's refusals of `curl` are guidance to a model,
    not a control.

Neither backend hands a runner anything but what it needs: its own token, the
runner credential it uses at the gateway, and the toolchain's paths. Never the
control plane's token, never the GitLab service account's.
"""

from __future__ import annotations

import asyncio
import json
import os
import secrets
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from dakcoder_shared.forwarding import Upstream

from .config import Settings

__all__ = ["DockerBackend", "ProcessBackend", "Runner", "Runners", "RunnerFailed"]

#: What a process runner inherits from the control plane's environment. Named,
#: because everything else is the control plane's and a runner must not see it.
_INHERIT = (
    "PATH", "SYSTEMROOT", "WINDIR", "HOME", "USERPROFILE", "TEMP", "TMP", "LANG", "LC_ALL",
    "GOROOT", "GOPATH", "GOCACHE", "GOFLAGS", "GOPROXY", "GOPRIVATE", "GONOSUMDB", "GONOPROXY",
    "GOTOOLS_PATH", "DAKCODER_HOME", "NO_PROXY", "no_proxy", "HTTPS_PROXY", "https_proxy",
    "HTTP_PROXY", "http_proxy", "PYTHONPATH", "VIRTUAL_ENV",
)

#: The port a runner listens on inside its container.
CONTAINER_PORT = 8791


class RunnerFailed(Exception):
    """A runner could not be started. A 503: ours, and possibly temporary."""


@dataclass
class Runner:
    lease_id: str
    url: str
    token: str
    handle: Any
    upstream: Upstream
    started_at: float = field(default_factory=time.time)
    last_used: float = field(default_factory=time.time)


class Backend(Protocol):
    def start(self, lease_id: str, worktree: Path, token: str) -> tuple[str, Any]: ...
    def stop(self, handle: Any) -> None: ...
    def alive(self, handle: Any) -> bool: ...


def runner_env(settings: Settings, token: str) -> dict[str, str]:
    """A runner's whole environment, apart from what `_INHERIT` passes through."""
    env = {
        "DAKCODER_HOSTED": "1",
        "DAKCODER_GATEWAY_URL": settings.gateway_url,
        "DAKCODER_JWT": settings.runner_jwt,
        # The runtime's own token: what this control plane presents to it.
        "DAKCODER_GATEWAY_TOKEN": token,
        # Never "wait forever" hosted (§10): it parks a runner and a lease.
        "DAKCODER_APPROVAL_TIMEOUT": str(settings.approval_timeout_s),
        "PYTHONUNBUFFERED": "1",
    }
    if settings.gomodcache is not None:
        env["GOMODCACHE"] = str(settings.gomodcache)
    return env


class ProcessBackend:
    def __init__(self, settings: Settings, *, ready_timeout: float = 60.0) -> None:
        self.settings = settings
        self.ready_timeout = ready_timeout

    def start(self, lease_id: str, worktree: Path, token: str) -> tuple[str, Any]:
        env = {k: v for k in _INHERIT if (v := os.environ.get(k))}
        env.update(runner_env(self.settings, token))
        argv = [
            *self.settings.runner_command,
            "--hosted",
            "--workspace", str(worktree),
            "--port", "0",
            *self.settings.runner_args,
        ]
        creation = subprocess.CREATE_NEW_PROCESS_GROUP if sys.platform == "win32" else 0
        proc = subprocess.Popen(
            argv,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            text=True,
            creationflags=creation,
        )
        # The runtime prints `{"port": ..}` on its first line of stdout once its
        # socket is bound, which is what the extension waits for too.
        line = _first_line(proc, self.ready_timeout)
        try:
            port = int(json.loads(line)["port"])
        except (ValueError, KeyError, TypeError) as exc:
            proc.kill()
            raise RunnerFailed(f"the runner did not say where it listens: {line[:200]!r}") from exc
        return f"http://127.0.0.1:{port}", proc

    def stop(self, handle: subprocess.Popen) -> None:
        if handle.poll() is None:
            handle.terminate()
            try:
                handle.wait(timeout=10)
            except subprocess.TimeoutExpired:
                handle.kill()

    def alive(self, handle: subprocess.Popen) -> bool:
        return handle.poll() is None


def _first_line(proc: subprocess.Popen, timeout: float) -> str:
    result: list[str] = []

    def read() -> None:
        assert proc.stdout is not None
        result.append(proc.stdout.readline())

    import threading

    reader = threading.Thread(target=read, daemon=True)
    reader.start()
    reader.join(timeout)
    if not result or not result[0]:
        proc.kill()
        raise RunnerFailed("the runner did not start in time")
    return result[0]


class DockerBackend:
    def __init__(self, settings: Settings, *, docker: str = "docker") -> None:
        self.settings = settings
        self.docker = docker

    def run_argv(self, lease_id: str, worktree: Path, token: str) -> list[str]:
        """The whole `docker run`, as data, so it can be read and tested."""
        s = self.settings
        argv = [
            self.docker, "run", "--detach", "--rm",
            "--name", f"dakcoder-runner-{lease_id}",
            "--read-only", "--tmpfs", "/tmp:rw,size=512m",
            "--user", "10001:10001",
            "--cap-drop", "ALL",
            "--security-opt", "no-new-privileges",
            "--pids-limit", str(s.runner_pids),
            "--memory", s.runner_memory,
            "--cpus", s.runner_cpus,
            # Published on loopback only, on a port Docker chooses.
            "--publish", f"127.0.0.1::{CONTAINER_PORT}",
            "--mount", f"type=bind,source={worktree},target=/workspace",
        ]
        if s.gomodcache is not None:
            argv += ["--mount", f"type=bind,source={s.gomodcache},target=/gomodcache"]
        if s.runner_network:
            argv += ["--network", s.runner_network]
        env = runner_env(s, token)
        if s.gomodcache is not None:
            env["GOMODCACHE"] = "/gomodcache"
        for name, value in sorted(env.items()):
            argv += ["--env", f"{name}={value}"]
        argv += [
            s.runner_image,
            "dakcoderd", "--hosted", "--workspace", "/workspace",
            "--host", "0.0.0.0", "--port", str(CONTAINER_PORT),
            *s.runner_args,
        ]
        return argv

    def start(self, lease_id: str, worktree: Path, token: str) -> tuple[str, Any]:
        started = subprocess.run(
            self.run_argv(lease_id, worktree, token), capture_output=True, text=True, check=False
        )
        if started.returncode != 0:
            raise RunnerFailed(f"docker run failed: {started.stderr.strip()[:300]}")
        container = started.stdout.strip()
        published = subprocess.run(
            [self.docker, "port", container, f"{CONTAINER_PORT}/tcp"],
            capture_output=True, text=True, check=False,
        ).stdout.strip().splitlines()
        if not published:
            self.stop(container)
            raise RunnerFailed("the runner container published no port")
        host_port = published[0].rsplit(":", 1)[-1]
        return f"http://127.0.0.1:{host_port}", container

    def stop(self, handle: str) -> None:
        subprocess.run([self.docker, "stop", "--time", "10", handle], capture_output=True, check=False)

    def alive(self, handle: str) -> bool:
        state = subprocess.run(
            [self.docker, "inspect", "--format", "{{.State.Running}}", handle],
            capture_output=True, text=True, check=False,
        )
        return state.stdout.strip() == "true"


def backend_for(settings: Settings) -> Backend:
    if settings.runner_backend == "docker":
        return DockerBackend(settings)
    if settings.runner_backend == "process":
        return ProcessBackend(settings)
    raise ValueError(f"unknown runner backend {settings.runner_backend!r}")


class Runners:
    """The running runners, one per lease, started on demand."""

    def __init__(self, backend: Backend, *, transport_for=None) -> None:
        self.backend = backend
        self._running: dict[str, Runner] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        #: For tests: an httpx transport per runner URL instead of the network.
        self._transport_for = transport_for

    async def ensure(self, lease_id: str, worktree: Path) -> Runner:
        lock = self._locks.setdefault(lease_id, asyncio.Lock())
        async with lock:
            runner = self._running.get(lease_id)
            if runner is not None and await asyncio.to_thread(self.backend.alive, runner.handle):
                runner.last_used = time.time()
                return runner
            if runner is not None:
                await runner.upstream.aclose()
            token = secrets.token_urlsafe(32)
            url, handle = await asyncio.to_thread(self.backend.start, lease_id, worktree, token)
            transport = self._transport_for(url) if self._transport_for else None
            runner = Runner(lease_id, url, token, handle, Upstream(url, token, transport=transport))
            self._running[lease_id] = runner
            await self._wait_healthy(runner)
            return runner

    async def _wait_healthy(self, runner: Runner, attempts: int = 100) -> None:
        for _ in range(attempts):
            try:
                status, _ = await runner.upstream.call("GET", "v1/health", sub="")
                if status == 200:
                    return
            except Exception:  # noqa: BLE001 - not up yet
                pass
            await asyncio.sleep(0.1)
        await self.stop(runner.lease_id)
        raise RunnerFailed("the runner started but never answered")

    def get(self, lease_id: str) -> Runner | None:
        return self._running.get(lease_id)

    def running(self) -> list[Runner]:
        return list(self._running.values())

    async def stop(self, lease_id: str) -> None:
        runner = self._running.pop(lease_id, None)
        if runner is not None:
            await runner.upstream.aclose()
            await asyncio.to_thread(self.backend.stop, runner.handle)

    def idle(self, idle_seconds: float, now: float | None = None) -> list[str]:
        """Leases whose runner nobody has called for ``idle_seconds``.

        Only candidates. A run can go an hour without a client calling it, so
        whether one is still running is the caller's question to ask.
        """
        now = now or time.time()
        return [r.lease_id for r in self._running.values() if now - r.last_used > idle_seconds]

    async def aclose(self) -> None:
        for lease_id in list(self._running):
            await self.stop(lease_id)
