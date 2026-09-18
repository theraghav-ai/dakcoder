"""Fixtures for the control plane. The helpers behind them are in `support.py`,
imported by name so they cannot be confused with the agent's own conftest."""

from support import (  # noqa: F401 - fixtures, registered by name
    app,
    backend,
    gitlab,
    remote,
    service,
    settings,
)
