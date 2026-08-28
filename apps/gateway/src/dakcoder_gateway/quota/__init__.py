"""Quota and metering (Part A section 16).

Rolling windows, because they can be shown; reserve-and-reconcile, because a
flat reservation that is never refunded drifts further from reality with every
turn (finding S18); and fail-closed, because a control that stops applying when
infrastructure is unwell is a control nobody can rely on.
"""

from .model import (
    UNLIMITED,
    Check,
    ConfigError,
    Lane,
    Limits,
    QuotaExceeded,
    Series,
    Snapshot,
    WindowState,
)
from .policy import QuotaPolicy, Reservation, Settlement, StoreUnavailable
from .store import Applied, Conflict, MemoryStore, QuotaStore, RedisStore, ScriptContractError

__all__ = [
    "UNLIMITED",
    "Applied",
    "Check",
    "ConfigError",
    "Conflict",
    "Lane",
    "Limits",
    "MemoryStore",
    "QuotaExceeded",
    "QuotaPolicy",
    "QuotaStore",
    "RedisStore",
    "Reservation",
    "ScriptContractError",
    "Series",
    "Settlement",
    "Snapshot",
    "StoreUnavailable",
    "WindowState",
]
