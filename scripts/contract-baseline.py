#!/usr/bin/env python3
"""Snapshot what a release promises clients: `api/contract-baseline.json`.

    python scripts/contract-baseline.py --release 0.4.9

Run by `scripts/release.py` after the test suite, and by
`make contract-baseline RELEASE=...`. The snapshot is what
`test_contract.py::test_the_contract_only_grows` holds every later change to:
nothing it lists may disappear within the same major API version.

Refuses when the current contract has already dropped something the previous
snapshot promised. Taking the snapshot anyway is how a removal would slip into
a release unnoticed, so this checks for itself rather than trusting that the
test suite ran (`release.py --skip-tests` exists).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT / "apps" / "agent" / "src"), str(ROOT / "apps" / "shared" / "src")]

from dakcoder_agent.loopback import published_contract, published_openapi  # noqa: E402
from dakcoder_shared.contract import compat  # noqa: E402

BASELINE = ROOT / "api" / "contract-baseline.json"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--release", required=True, help="the release this snapshot is of, e.g. 0.4.9")
    args = parser.parse_args()

    contract = json.loads(published_contract())
    openapi = json.loads(published_openapi())
    current = compat.surface(contract, openapi)

    if BASELINE.is_file():
        previous = json.loads(BASELINE.read_text(encoding="utf-8"))
        broken = compat.breaks(previous, current, contract["api_version"])
        if broken:
            print(
                f"refusing: the contract no longer promises {len(broken)} thing(s) that the "
                f"baseline ({previous['release']}) does, within API {contract['api_version']}:",
                file=sys.stderr,
            )
            for fact in broken[:50]:
                print(f"  {fact}", file=sys.stderr)
            print(
                "Put them back, or bump API_VERSION's major version (host-plan §4.6).",
                file=sys.stderr,
            )
            return 1

    BASELINE.write_text(
        compat.baseline(contract, openapi, args.release), encoding="utf-8", newline=""
    )
    print(f"api/contract-baseline.json: {len(current)} facts, release {args.release}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
