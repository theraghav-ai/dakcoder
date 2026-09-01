"""``dakcoderd`` — the process the extension spawns.

Part B §4: the extension finds a Python, creates a venv, installs the bundled
wheel, generates a random loopback token, spawns this, and waits for
``/v1/health`` for up to sixty seconds. Everything here is shaped by that
sentence.

**The port is printed on stdout, immediately.** Binding to port 0 and reporting
what the OS gave us is the only arrangement that works when two VS Code windows
open at once; a fixed port turns the second one into a confusing failure. The
line is printed before the server starts serving so the parent has it even if
startup then fails.

**Prewarm is on by default.** Part B §3.3 notes the extension currently spawns
with ``--no-prewarm`` and recommends turning it on: a four-token probe in a
background thread costs nothing a developer can perceive and moves cold start
out of the first request — which is the one they are watching.

**It holds no model credential, and refuses to start if one is present.** The
gateway is the only process that may hold one (§15.4). A local runtime with a
key would be an unmetered bypass around quota and audit, and "it should not be
there" is not a control.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
from pathlib import Path
from typing import Any

from dakcoder_shared.config import local_config
from dakcoder_shared.paths import Workspace

from .context import ContextManager
from .llm import make_client
from .loop import AgentLoop
from .loopback import Loopback, create_app
from .modes import Mode
from .prompts import system_prompt
from .tools import commands, fs, knowledge
from .tools.catalog import as_json
from .tools.gotools import GoTools, handlers_for
from .tools.router import Router

__all__ = ["build", "main"]


#: The turn budget's floor, default and ceiling. 40 is sized for a single
#: vertical slice; a whole-service migration under the conversion SOP touches
#: every handler and legitimately needs several times that, which is what the
#: `dakcoder.maxTurns` setting (carried here as DAKCODER_MAX_TURNS) is for. The
#: ceiling is a cost backstop, not a safety net -- the no-progress machinery is
#: what catches a run going in circles; the turn budget only bounds what an
#: honest long run may spend.
MIN_TURNS, DEFAULT_TURNS, MAX_TURNS = 10, 40, 400


def max_turns_from_env() -> int:
    """DAKCODER_MAX_TURNS, clamped to something the runtime will stand behind.

    Garbage falls back to the default rather than erroring: the variable comes
    from a settings field, and a typo there should cost the typo'd value, not
    the whole runtime.
    """
    raw = os.environ.get("DAKCODER_MAX_TURNS", "")
    try:
        return min(MAX_TURNS, max(MIN_TURNS, int(raw)))
    except ValueError:
        return DEFAULT_TURNS


def build(
    workspace: Path,
    *,
    gateway_url: str,
    jwt: str,
    loopback_token: str,
    version: str = "dev",
) -> tuple[Loopback, GoTools]:
    """Wire the runtime. Returns the sidecar too, so the caller can close it."""
    space = Workspace.at(workspace)
    sidecar = GoTools(space.root)

    handlers = {
        **fs.HANDLERS,
        **knowledge.HANDLERS,
        **commands.HANDLERS,
        **handlers_for(sidecar),
    }

    # The credential invariant, checked here rather than trusted. `local_config`
    # raises if any model key is in the environment — including one a developer
    # exported for an unrelated project, which is exactly the case a policy
    # document would not catch.
    config = local_config(gateway_url, jwt)

    turns = max_turns_from_env()

    def build_loop(session, approve):
        router = Router(space, handlers)
        context = ContextManager(mode=Mode.PLANNER, system_prompt=system_prompt())
        return AgentLoop(
            context,
            make_client(config),
            router,
            approve=approve,
            cancelled=session.cancel.is_set,
            max_turns=turns,
        )

    runtime = Loopback(
        space.root,
        build_loop,
        token=loopback_token,
        tool_catalog=json.loads(as_json(version)),
        version=version,
        gateway_url=gateway_url,
    )
    return runtime, sidecar


def prewarm(runtime: Loopback, config) -> None:
    """A tiny completion in a background thread, to move cold start off turn one.

    Failure is recorded, never raised. A runtime that refuses to start because
    the gateway was briefly unreachable is worse than one that starts and says
    so — the developer can sign in, fix a proxy setting, or simply wait, and
    none of those are possible if the process exited.
    """

    def probe() -> None:
        import time

        started = time.monotonic()
        try:
            with make_client(config) as client:
                client.chat(
                    [{"role": "user", "content": "ok"}],
                    role="fast",
                    max_tokens=4,
                    enable_thinking=False,
                )
        except Exception as exc:  # noqa: BLE001 - see the docstring
            runtime.ready = {"prewarmed": False, "reason": str(exc)[:200]}
            return
        runtime.ready = {
            "prewarmed": True,
            "latency_ms": int((time.monotonic() - started) * 1000),
        }

    threading.Thread(target=probe, name="dakcoder-prewarm", daemon=True).start()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="dakcoderd", description="the dakcoder runtime")
    parser.add_argument("--workspace", default=".", help="the repository to work in")
    parser.add_argument("--host", default="127.0.0.1", help="always loopback in practice")
    parser.add_argument(
        "--port",
        type=int,
        default=0,
        help="0 asks the OS for a free port, which is what lets two windows coexist",
    )
    parser.add_argument("--no-prewarm", action="store_true")
    parser.add_argument("--version-string", default=os.environ.get("DAKCODER_VERSION", "dev"))
    args = parser.parse_args(argv)

    gateway_url = os.environ.get("DAKCODER_GATEWAY_URL", "")
    jwt = os.environ.get("DAKCODER_JWT", "")
    token = os.environ.get("DAKCODER_GATEWAY_TOKEN", "")

    if not gateway_url:
        print(
            "DAKCODER_GATEWAY_URL is not set. Model traffic goes through the "
            "gateway's /v1/llm proxy; without it there is nowhere to send a turn.",
            file=sys.stderr,
        )
        return 2
    if not token:
        print(
            "DAKCODER_GATEWAY_TOKEN is not set. It authenticates the extension to "
            "its own runtime, and an unauthenticated loopback port is reachable by "
            "every other process on this machine.",
            file=sys.stderr,
        )
        return 2

    try:
        runtime, sidecar = build(
            Path(args.workspace).resolve(),
            gateway_url=gateway_url,
            jwt=jwt,
            loopback_token=token,
            version=args.version_string,
        )
    except Exception as exc:  # noqa: BLE001 - startup failure must be legible
        print(f"dakcoderd could not start: {exc}", file=sys.stderr)
        return 1

    import socket

    import uvicorn

    # Bound here rather than by uvicorn, so the port is known before serving and
    # can be printed. The extension is parsing stdout with a sixty-second timeout;
    # a port it learns about only after the server is up is a race it loses on a
    # slow machine.
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((args.host, args.port))
    # Listening before announcing the port. A parent that connects the moment it
    # reads the line would otherwise get a refusal on a socket that is bound but
    # not yet accepting — a race that only shows up on a fast machine, which is
    # the worst kind to only show up on.
    sock.listen(128)
    port = sock.getsockname()[1]

    print(json.dumps({"port": port, "pid": os.getpid(), "version": args.version_string}),
          flush=True)

    if not args.no_prewarm:
        prewarm(runtime, local_config(gateway_url, jwt))

    app = create_app(runtime)
    # `Server.run(sockets=[...])` rather than `uvicorn.run(fd=...)`: passing a
    # file descriptor works on POSIX and silently fails on Windows, where socket
    # handles are not file descriptors. The primary platform here is Windows 11,
    # so the portable form is the only one worth having.
    server = uvicorn.Server(
        uvicorn.Config(app, log_level="warning", access_log=False, lifespan="on")
    )
    try:
        server.run(sockets=[sock])
    finally:
        sidecar.close()
        sock.close()
    return 0


if __name__ == "__main__":  # pragma: no cover - entry point
    raise SystemExit(main())
