"""The toolchain this runtime builds with, as ``/v1/health`` reports it.

A hosted runner and a developer's machine must build with the same Go and the
same linters, or a gate that passes on one fails on the other (host-plan §7.3).
Nothing reported the versions, so a mismatch could only be found by reading two
terminals side by side.

Probed once, in the background, after the server is up. Each tool costs a
process start, and ``/v1/health`` is what the extension polls while it waits
for the runtime to come up.
"""

from __future__ import annotations

import logging
import re
import shutil
import subprocess
import threading
from collections.abc import Callable

from .tools import gotools

__all__ = ["PROBES", "probe", "probe_in_background"]

log = logging.getLogger(__name__)

#: The first dotted version in a line of output.
_VERSION = r"v?(\d+\.\d+(?:\.\d+)?)"

#: Each tool: how to ask its version, and where the version is in the answer.
#: ``None`` for the pattern means "report that it is installed and no more":
#: better than a number taken from the wrong line.
PROBES: dict[str, tuple[tuple[str, ...], str | None]] = {
    "go": (("go", "version"), r"go(\d+\.\d+(?:\.\d+)?)"),
    "gotools": (("gotools", "version"), _VERSION),
    "golangci-lint": (("golangci-lint", "--version"), _VERSION),
    # Prints the Go it was built with first; its own version follows the `@`.
    "govulncheck": (("govulncheck", "-version"), r"govulncheck@v(\d+\.\d+(?:\.\d+)?)"),
    "govalid": (("govalid",), None),
    "swag": (("swag", "--version"), _VERSION),
    "buf": (("buf", "--version"), _VERSION),
    "git": (("git", "--version"), _VERSION),
}

INSTALLED = "installed"


def probe(
    *,
    which: Callable[[str], str | None] = shutil.which,
    run: Callable[[list[str]], str] | None = None,
) -> dict[str, str | None]:
    """Each tool's version, ``"installed"`` when it has no readable one, or None.

    Never raises. A tool that hangs or fails to start is reported as missing,
    because that is what it is to the gate.
    """
    run = run or _output
    found: dict[str, str | None] = {}
    for name, (argv, pattern) in PROBES.items():
        binary = gotools._find_binary() if name == "gotools" else which(argv[0])
        if not binary:
            found[name] = None
            continue
        if pattern is None:
            found[name] = INSTALLED
            continue
        try:
            text = run([binary, *argv[1:]])
        except (OSError, subprocess.SubprocessError) as exc:
            log.info("toolchain: %s did not answer: %s", name, exc)
            found[name] = None
            continue
        match = re.search(pattern, text)
        word = text.strip()
        if match:
            found[name] = match.group(1)
        elif word and len(word) <= 20 and not any(c.isspace() for c in word):
            # An unstamped build says `dev`, which is the parity fact itself.
            found[name] = word
        else:
            found[name] = INSTALLED
    return found


def probe_in_background(sink: Callable[[dict[str, str | None]], None]) -> None:
    def work() -> None:
        try:
            sink(probe())
        except Exception:  # noqa: BLE001 - a health detail must not take anything down
            log.warning("toolchain probe failed", exc_info=True)

    threading.Thread(target=work, name="dakcoder-toolchain", daemon=True).start()


def _output(argv: list[str]) -> str:
    result = subprocess.run(argv, capture_output=True, text=True, timeout=10, check=False)
    return f"{result.stdout}\n{result.stderr}"
