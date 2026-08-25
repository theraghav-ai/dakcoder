"""The prompts, and the discipline that keeps the prefix stable.

One system prompt for every mode. Mode instruction is a *user message appended
after it*, never a different system prompt — that is finding S6, and it cost the
frontend agent three cold prefills per task because ``_run_planner``,
``_run_coder`` and ``_run_debugger`` each assigned a fresh message list with its
own system text.

Files rather than string literals, for two reasons that are not stylistic.
Prompt review is the highest-leverage review there is and a ``.md`` diff is
readable in a way a Python string is not; and the byte content is what the model
sees, so a prompt in a file can be hashed, versioned and pinned by a test — which
is how a change to it becomes a deliberate act rather than a side effect of
editing the module that happened to contain it.
"""

from __future__ import annotations

from functools import lru_cache
from importlib import resources

from ..modes import Mode

__all__ = ["MODE_BUDGET", "SYSTEM_BUDGET", "mode_instruction", "system_prompt"]

#: Part A section 6.1's allocation. Asserted in the tests rather than trusted:
#: a prompt is the easiest thing in a codebase to grow by accident, because
#: every addition to it looks individually reasonable.
SYSTEM_BUDGET = 1_200

#: No published figure for these. Kept small on the same principle as the system
#: prompt, and because a mode overlay that needs three hundred tokens to explain
#: itself is usually a mode that has not been decided.
MODE_BUDGET = 250


@lru_cache(maxsize=1)
def system_prompt() -> str:
    """The shared system prompt. Identical in every mode, for every task.

    Cached because it is read on every run and never changes within a process —
    and because re-reading it would make it possible, in principle, for two turns
    of the same run to see different text, which is exactly the prefix
    instability this module exists to prevent.
    """
    return _read("system.md")


@lru_cache(maxsize=8)
def mode_instruction(mode: Mode | str) -> str:
    """The overlay appended when the loop enters a mode."""
    return _read(f"modes/{Mode(mode)}.md")


def _read(relative: str) -> str:
    package = resources.files(__name__)
    for part in relative.split("/"):
        package = package.joinpath(part)
    # Newlines normalised on read. The file may be checked out CRLF on Windows,
    # and a prefix whose byte content depends on the reader's git configuration
    # is not a stable prefix — it would produce a different cache key on a
    # colleague's machine for a file neither of them edited.
    return package.read_text(encoding="utf-8").replace("\r\n", "\n").strip()
