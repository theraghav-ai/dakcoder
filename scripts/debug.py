#!/usr/bin/env python3
"""Read a session's full-fidelity turn recording, from a checkout.

``dakcoder_agent`` is not installed in this repository's virtualenv -- the
runtime the extension spawns has its own venv under the extension's global
storage, and the sources here are on ``PYTHONPATH`` only under pytest. So the
module's own ``python -m dakcoder_agent.debug`` works for the runtime and not
for a developer standing in the checkout, which is where you are when you want
to read one of these.

This is the two-line wrapper that closes that gap, following
``scripts/context-report.py``:

    python scripts/debug.py                       # which sessions are recorded
    python scripts/debug.py latest                # the newest one, per turn
    python scripts/debug.py <session-id>
    python scripts/debug.py latest --prompt 14    # the exact request sent
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "apps" / "agent" / "src"), str(ROOT / "apps" / "shared" / "src")]

from dakcoder_agent.debug import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
