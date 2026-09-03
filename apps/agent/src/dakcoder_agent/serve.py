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
import logging
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
from .tools import commands, control, fs, knowledge
from .tools.catalog import as_json
from .tools.gotools import GoTools, handlers_for
from .tools.router import Router
from .undo import UndoStore

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
        # `submit_plan` and `ask_developer`: the two calls that end the planning
        # phase. Ordinary handlers on purpose, so they get the same argument
        # validation and the same result envelope as everything else.
        **control.HANDLERS,
        **handlers_for(sidecar),
    }

    # The credential invariant, checked here rather than trusted. `local_config`
    # raises if any model key is in the environment — including one a developer
    # exported for an unrelated project, which is exactly the case a policy
    # document would not catch.
    config = local_config(gateway_url, jwt)

    turns = max_turns_from_env()

    # One client for the process, not one per run.
    #
    # `build_loop` used to call `make_client` on every task, so every run paid a
    # TCP and TLS handshake with no connection reuse and left a client nobody
    # closed — the exact finding (S10) the class docstring warns about, made by
    # the caller. It also fixed the credential at construction; the holder below
    # is what lets the extension refresh it without a restart.
    holder: dict[str, Any] = {"runtime": None}

    def credential() -> str:
        runtime = holder["runtime"]
        fresh = runtime.credential() if runtime is not None else ""
        return fresh or config.api_key

    client = make_client(config, credential=credential)

    def build_loop(session, approve):
        # The undo store is keyed on the session, so every mutating tool call
        # copies the file's pre-run bytes once before it changes them. That is
        # what lets `revert` put back what the developer had rather than what
        # HEAD has (BUG L-11).
        router = Router(space, handlers, undo=UndoStore(space.root, session.id))
        context = ContextManager(mode=Mode.ASK, system_prompt=system_prompt())
        return AgentLoop(
            context,
            client,
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
    holder["runtime"] = runtime
    # Closed with the sidecar at shutdown; `main` already owns both.
    runtime.close_client = client.close
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


#: What the runtime logs, and where.
#:
#: Nothing configured logging at all, so every `log.info` and `log.warning` in
#: the package went to the root logger's default handler and was discarded --
#: including the per-run accounting that says whether a run was shaped by the
#: context window (see `AgentLoop._metrics`). uvicorn had its own level and that
#: was the only thing anybody saw.
#:
#: stderr, because `deploy/start.sh` redirects it to `deploy/logs/runtime.log`,
#: which is the file an operator already tails. `DAKCODER_LOG_LEVEL=debug` turns
#: on the per-turn detail; the default says enough to notice a problem without
#: making the log unreadable.
LOG_FORMAT = "%(asctime)s %(levelname)-7s %(name)s  %(message)s"


def _configure_logging() -> None:
    level = os.environ.get("DAKCODER_LOG_LEVEL", "info").strip().lower()
    resolved = getattr(logging, level.upper(), None)
    if not isinstance(resolved, int):
        resolved = logging.INFO
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter(LOG_FORMAT, datefmt="%Y-%m-%dT%H:%M:%S"))
    for name in ("dakcoder_agent", "dakcoder_shared"):
        logger = logging.getLogger(name)
        logger.setLevel(resolved)
        # Replaced rather than added to, so a restarted runtime in the same
        # process -- which the tests do -- does not double every line.
        logger.handlers[:] = [handler]
        logger.propagate = False


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
    _configure_logging()

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
        # uvicorn stays at warning: its access log is one line per SSE poll and
        # would bury the run's own record. `_configure_logging` sets the level
        # for dakcoder's loggers, which is the interesting half.
        uvicorn.Config(app, log_level="warning", access_log=False, lifespan="on")
    )
    try:
        server.run(sockets=[sock])
    finally:
        sidecar.close()
        # The shared HTTP client too. Held open, its keep-alive pool survives
        # the process's last request and the sockets are only closed when the
        # interpreter tears down — which on Windows is not guaranteed to be
        # graceful.
        closer = getattr(runtime, "close_client", None)
        if callable(closer):
            try:
                closer()
            except Exception:  # noqa: BLE001 - shutdown must not raise
                pass
        sock.close()
    return 0


if __name__ == "__main__":  # pragma: no cover - entry point
    raise SystemExit(main())
