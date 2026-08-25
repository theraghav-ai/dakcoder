"""The dakcoder gateway. Server only.

This package holds the code that reads DAKCODER_MODEL_API_KEY, performs the
GitLab token exchange and signs JWTs. It is never packaged into the .vsix, and
that is a structural boundary rather than a convention: Part A §19.1's argument
is that shipping it would weaken the invariant from "the code that reads the key
is not on the machine" to merely "the key is not on the machine".
"""

from .probe import CapabilityProbe, ProbeReport, ProbeResult, Status

__all__ = ["CapabilityProbe", "ProbeReport", "ProbeResult", "Status"]
