"""Fill ``extension/runtime-graph`` with the wheels the code-graph pilot needs.

    python scripts/vendor-graphify.py          download what is missing
    python scripts/vendor-graphify.py --check  fail if the directory is incomplete

The extension installs these offline, with ``--no-index --no-deps``, into the
runtime venv -- and only when ``dakcoder.codeGraph.enabled`` is on, so a
developer who never turns the pilot on pays the download and not the install.

**Why a hand-kept list rather than ``pip download graphifyy``.** graphify
declares all 25 of its tree-sitter grammars as hard dependencies, which is 30
wheels and 23 MB for one platform. It loads a grammar only when a file in that
language is found, so the five that cover what these services contain build the
identical graph (measured on pao-back-end-development: 1,373 nodes, 3,738 edges
either way) in 17.7 MB. Installing with ``--no-deps`` is what lets pip accept
the closure short of the grammars nobody here has. numpy looks optional and is
not: ``graphify._minhash`` imports it unconditionally.

**The platform is the runtime's.** ``extension/runtime`` ships cp312 /
win_amd64 wheels (``pydantic_core``, ``pyyaml``), so a venv that can import the
agent at all is that interpreter, and these are downloaded for the same tag.

Network is touched here, at release time, and nowhere else.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TARGET = ROOT / "extension" / "runtime-graph"

PLATFORM = "win_amd64"
PYTHON_VERSION = "3.12"

#: Exact pins. graphify changes its CLI output between minor versions, and
#: ``tools/codegraph.py`` reads that output, so an upgrade is a deliberate edit
#: here followed by ``test_codegraph.py``'s live tests.
PINS = (
    "graphifyy==0.9.67",
    "networkx==3.7",
    "numpy==2.5.3",
    "rapidfuzz==3.14.6",
    "tree-sitter==0.25.2",
    "tree-sitter-go==0.25.0",
    "tree-sitter-python==0.25.0",
    "tree-sitter-typescript==0.23.2",
    "tree-sitter-javascript==0.25.0",
    "tree-sitter-json==0.24.8",
)


def _normal(name: str) -> str:
    return name.lower().replace("-", "_")


def missing() -> list[str]:
    present = {_normal(p.name.split("-")[0]) + "==" + p.name.split("-")[1] for p in TARGET.glob("*.whl")}
    return [pin for pin in PINS if _normal(pin.split("==")[0]) + "==" + pin.split("==")[1] not in present]


def strays() -> list[str]:
    wanted = {_normal(pin.split("==")[0]) + "-" + pin.split("==")[1] for pin in PINS}
    return [
        p.name
        for p in TARGET.glob("*.whl")
        if _normal(p.name.split("-")[0]) + "-" + p.name.split("-")[1] not in wanted
    ]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true", help="verify only; download nothing")
    args = parser.parse_args()

    TARGET.mkdir(parents=True, exist_ok=True)
    extra = strays()
    if extra:
        # A superseded pin left behind would ship, and `--no-deps` would install
        # both versions' files over each other.
        print(f"not in PINS, remove them: {', '.join(extra)}", file=sys.stderr)
        return 1

    needed = missing()
    if args.check:
        if needed:
            print(f"extension/runtime-graph is missing: {', '.join(needed)}", file=sys.stderr)
            return 1
        print(f"extension/runtime-graph is complete ({len(PINS)} wheels)")
        return 0

    if needed:
        subprocess.run(
            [
                sys.executable, "-m", "pip", "download", "--disable-pip-version-check",
                "--no-deps", "--only-binary=:all:",
                "--platform", PLATFORM, "--python-version", PYTHON_VERSION,
                "--dest", str(TARGET), *needed,
            ],
            check=True,
        )
    left = missing()
    if left:
        print(f"still missing after download: {', '.join(left)}", file=sys.stderr)
        return 1
    size = sum(p.stat().st_size for p in TARGET.glob("*.whl")) / 1_048_576
    print(f"extension/runtime-graph: {len(PINS)} wheels, {size:.1f} MB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
