"""The dakcoder gateway. Server only.

This package holds the code that reads the model credentials — the shared
``DAKCODER_MODEL_API_KEY`` and any per-role ``DAKCODER_MODEL_<ROLE>_API_KEY`` —
performs the GitLab token exchange and signs JWTs. It is never packaged into the
.vsix, and that is a structural boundary rather than a convention: Part A
§19.1's argument
is that shipping it would weaken the invariant from "the code that reads the key
is not on the machine" to merely "the key is not on the machine".
"""

from .probe import CapabilityProbe, EndpointProbes, ProbeReport, ProbeResult, Status
from .routing import ModelRoute, RoleRouter

__all__ = [
    "CapabilityProbe",
    "EndpointProbes",
    "ModelRoute",
    "ProbeReport",
    "ProbeResult",
    "RoleRouter",
    "Status",
]
