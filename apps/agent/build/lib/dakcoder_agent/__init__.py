"""The dakcoder agent: context management, the loop, and the tool router.

Ships in the .vsix and runs on the developer's machine under local-first (D2).
It holds no model credential — model traffic goes through the gateway's
/v1/llm/* proxy, which is what makes quota and audit unbypassable rather than
advisory (Part A §15.4).
"""

from .context import ContextManager, Layer, Message, Recap, Role, Usage
from .modes import Mode, ModeConfig, config_for

__all__ = [
    "ContextManager",
    "Layer",
    "Message",
    "Mode",
    "ModeConfig",
    "Recap",
    "Role",
    "Usage",
    "config_for",
]
