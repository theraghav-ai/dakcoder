"""The control plane's settings, from the environment.

Everything with a default is a limit or a place, and every limit is a starting
point to tune rather than a measurement: host-plan §7.2 asks for the wall-clock
limit to be sized against a real migration, and nobody has run one hosted yet.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path

__all__ = ["Settings"]


def _int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    return int(raw) if raw else default


def _hours(name: str, default: float) -> timedelta:
    raw = os.environ.get(name, "").strip()
    return timedelta(hours=float(raw) if raw else default)


@dataclass(frozen=True)
class Settings:
    #: Where leases' clones, the registry and the archive live.
    data_dir: Path
    #: What the gateway presents to us. Without it, nothing is answered.
    token: str
    #: Where runners send model traffic, and the credential they send it with.
    gateway_url: str
    runner_jwt: str

    #: `process` (a local dakcoderd per workspace; no isolation, for one
    #: trusted host and for tests) or `docker` (host-plan §7.2's container).
    runner_backend: str = "process"
    runner_command: tuple[str, ...] = ("dakcoderd",)
    runner_args: tuple[str, ...] = ()
    runner_image: str = "dakcoder-runner:latest"
    #: The docker network runners join. It must allow the gateway and nothing
    #: else (§7.2): that is the operator's network policy, not this setting.
    runner_network: str = ""
    runner_cpus: str = "2"
    runner_memory: str = "4g"
    runner_pids: int = 512
    #: A module cache shared by every runner, so a new one does not start with
    #: `go mod download` (§7.2's warm pool, as a warm cache).
    gomodcache: Path | None = None

    #: The repositories a lease may clone, until clones can act as the caller
    #: (§7.1's stopgap). See `repos.Allowlist`.
    allowlist: Path | None = None
    #: The service account that clones, pushes and opens merge requests while
    #: the allowlist is in force. Never given to a runner.
    gitlab_url: str = ""
    gitlab_token: str = ""
    git_name: str = "dakcoder"
    git_email: str = "dakcoder@noreply.invalid"

    lease_ttl: timedelta = field(default=timedelta(days=7))
    runner_idle: timedelta = field(default=timedelta(minutes=30))
    max_leases_per_user: int = 3
    max_running_per_user: int = 2
    max_lease_bytes: int = 4 * 1024**3
    #: How long a hosted approval waits before the run suspends (§10). Never
    #: "forever", as the local default is: here that parks a runner and a lease.
    approval_timeout_s: int = 1800

    @property
    def leases_dir(self) -> Path:
        return self.data_dir / "leases"

    @property
    def archive_dir(self) -> Path:
        return self.data_dir / "archive"

    @property
    def registry(self) -> Path:
        return self.data_dir / "registry.sqlite3"

    @classmethod
    def from_env(cls) -> "Settings":
        def env(name: str, default: str = "") -> str:
            return os.environ.get(name, default).strip()

        command = env("DAKCODER_RUNNER_COMMAND", "dakcoderd")
        cache = env("DAKCODER_RUNNER_GOMODCACHE")
        allowlist = env("DAKCODER_REPO_ALLOWLIST")
        return cls(
            data_dir=Path(env("DAKCODER_AGENTSVC_DATA", "./agentsvc-data")).resolve(),
            token=env("DAKCODER_AGENTSVC_TOKEN"),
            gateway_url=env("DAKCODER_GATEWAY_URL"),
            runner_jwt=env("DAKCODER_RUNNER_JWT"),
            runner_backend=env("DAKCODER_RUNNER_BACKEND", "process"),
            runner_command=tuple(command.split()),
            runner_image=env("DAKCODER_RUNNER_IMAGE", "dakcoder-runner:latest"),
            runner_network=env("DAKCODER_RUNNER_NETWORK"),
            runner_cpus=env("DAKCODER_RUNNER_CPUS", "2"),
            runner_memory=env("DAKCODER_RUNNER_MEMORY", "4g"),
            runner_pids=_int("DAKCODER_RUNNER_PIDS", 512),
            gomodcache=Path(cache) if cache else None,
            allowlist=Path(allowlist) if allowlist else None,
            gitlab_url=env("DAKCODER_GITLAB_URL"),
            gitlab_token=env("DAKCODER_GITLAB_SERVICE_TOKEN"),
            lease_ttl=_hours("DAKCODER_LEASE_TTL_HOURS", 24 * 7),
            runner_idle=_hours("DAKCODER_RUNNER_IDLE_HOURS", 0.5),
            max_leases_per_user=_int("DAKCODER_MAX_LEASES_PER_USER", 3),
            max_running_per_user=_int("DAKCODER_MAX_RUNNING_PER_USER", 2),
            max_lease_bytes=_int("DAKCODER_MAX_LEASE_BYTES", 4 * 1024**3),
            approval_timeout_s=_int("DAKCODER_HOSTED_APPROVAL_TIMEOUT", 1800),
        )
