"""Shared between the agent and the gateway.

Kept deliberately small. This package is a dependency of both distributables,
so anything added here ships to laptops as well as to the server — and Part A
§19.1's security boundary rests on the gateway's code, which reads the model
credential, never being in the .vsix.
"""

from .tokens import Calibration, estimate_tokens

__all__ = ["Calibration", "estimate_tokens"]
