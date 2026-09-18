"""``dakcoder-agentsvc``: the control plane's entry point.

Configured entirely from the environment (``config.Settings.from_env``), and
refuses to start without the two things it cannot work without: its own token,
which the gateway presents, and a runner credential for the gateway's model
proxy. Listens on loopback only: the gateway is its only client.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys

from dakcoder_shared.callers import gateway_forwarded

from .app import create_app
from .config import Settings
from .gitlab import GitLab
from .repos import Allowlist, Git
from .runners import Runners, backend_for
from .service import Service
from .store import Store

__all__ = ["build", "main"]

log = logging.getLogger("dakcoder_agentsvc")

#: How often the reaper looks for idle runners and expired leases.
REAP_EVERY = 60.0


def build(settings: Settings, *, transport_for=None) -> Service:
    settings.leases_dir.mkdir(parents=True, exist_ok=True)
    settings.archive_dir.mkdir(parents=True, exist_ok=True)
    gitlab = (
        GitLab(settings.gitlab_url, settings.gitlab_token)
        if settings.gitlab_url and settings.gitlab_token
        else None
    )
    return Service(
        settings,
        Store(settings.registry),
        Allowlist.load(settings.allowlist),
        Git(token=settings.gitlab_token, name=settings.git_name, email=settings.git_email),
        Runners(backend_for(settings), transport_for=transport_for),
        gitlab,
    )


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=os.environ.get("DAKCODER_LOG_LEVEL", "info").upper(),
        format="%(asctime)s %(levelname)-7s %(name)s  %(message)s",
        stream=sys.stderr,
    )
    settings = Settings.from_env()
    missing = [
        name
        for name, value in (
            ("DAKCODER_AGENTSVC_TOKEN", settings.token),
            ("DAKCODER_GATEWAY_URL", settings.gateway_url),
            ("DAKCODER_RUNNER_JWT", settings.runner_jwt),
        )
        if not value
    ]
    if missing:
        print(f"dakcoder-agentsvc: not set: {', '.join(missing)}", file=sys.stderr)
        return 2
    if settings.runner_backend == "process":
        log.warning(
            "runners are local processes: repository code they build runs as this "
            "user, unisolated. Use DAKCODER_RUNNER_BACKEND=docker for untrusted code."
        )

    service = build(settings)

    async def reaper() -> None:
        while True:
            await asyncio.sleep(REAP_EVERY)
            try:
                reaped = await service.reap()
                if reaped["stopped"] or reaped["expired"]:
                    log.info("reaper: %s", reaped)
            except Exception:  # noqa: BLE001 - one bad pass must not end the loop
                log.exception("reaper pass failed")

    async def start_reaper() -> None:
        asyncio.get_running_loop().create_task(reaper())

    app = create_app(
        service,
        authenticate=gateway_forwarded(lambda: settings.token),
        on_start=start_reaper,
    )

    import uvicorn

    uvicorn.run(
        app,
        host="127.0.0.1",
        port=int(os.environ.get("DAKCODER_AGENTSVC_PORT", "8792")),
        log_level=os.environ.get("DAKCODER_LOG_LEVEL", "info"),
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
