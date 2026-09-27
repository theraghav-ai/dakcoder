"""How a long call learns the developer has stopped the run.

Cancellation used to be observed in two places only: between turns, and
between tool calls. Everything inside a turn ran to its own end first -- a
model call to its 540-second read timeout, a ``go test`` to its 300-second
one -- while the panel said "stopping". The two blocking calls live in two
packages that know nothing about a loop or a session, so the signal travels
the way ``READONLY_MODULES`` does in ``tools/commands.py``: a ``ContextVar``
the loop sets for the thread its run owns, read by whoever is about to block.

``None`` means nothing can stop the call but its own timeout, which is what a
gate test, a script or the gateway gets.
"""

from __future__ import annotations

from collections.abc import Callable
from contextvars import ContextVar

__all__ = ["CANCELLED", "cancelled"]

CANCELLED: ContextVar[Callable[[], bool] | None] = ContextVar(
    "dakcoder_cancelled", default=None
)


def cancelled() -> bool:
    """Whether the run this thread is working for has been stopped."""
    check = CANCELLED.get()
    return bool(check is not None and check())
