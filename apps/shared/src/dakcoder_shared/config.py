"""Runtime configuration, and the one invariant it exists to enforce.

The model API key is a single shared LiteLLM credential. If a laptop held it,
every developer could spend the shared GPU budget with no ceiling and no
attribution, and the whole quota model in Part A §16 would be decorative. So the
key lives in exactly one place — the gateway's secret store — and this module is
where that is checked rather than assumed.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from enum import StrEnum

__all__ = ["Deployment", "LLMConfig", "MissingCredential", "CredentialLeak"]


class Deployment(StrEnum):
    """Where this process is running, which decides what it may hold."""

    #: Inside the gateway. The only place the LiteLLM key is ever read.
    GATEWAY = "gateway"
    #: On the developer's machine. Holds no model credential at all — it
    #: authenticates as the developer and lets the gateway attach the real key.
    LOCAL = "local"


class MissingCredential(RuntimeError):
    """The gateway was started without a model key."""


class CredentialLeak(RuntimeError):
    """A local runtime was handed a model credential.

    Fails at startup rather than quietly using it. Part B §4.6 deletes these
    variables from the child's environment before spawning, so reaching this
    means either that stripping failed or that someone configured a bypass —
    both of which should stop the process, not be worked around.
    """


@dataclass(frozen=True)
class LLMConfig:
    """What the client needs to make a request.

    Two shapes, and the *only* difference between them is what they authenticate
    to. That is what makes the invariant cheap enough to hold permanently rather
    than something traded away for latency later: the agent loop is identical in
    both modes, same request shape, same streaming, same tool calls.
    """

    deployment: Deployment
    base_url: str
    #: Bearer token. The LiteLLM key in the gateway; the developer's dakcoder
    #: JWT in a local runtime.
    api_key: str

    model_coder: str = "Qwen3.8-27B"
    model_fast: str = "Qwen3.8-27B"
    model_embed: str = "Qwen3.8-27B"

    temperature_coder: float = 0.1
    temperature_fast: float = 0.0

    connect_timeout: float = 5.0
    read_timeout: float = 600.0
    write_timeout: float = 30.0

    max_attempts: int = 3
    #: Attribution at the proxy, so spend is per-user even before per-user
    #: virtual keys exist (§16.6 phase 1).
    user: str = ""

    def model_for(self, role: str) -> str:
        """Resolve a model by role.

        Every call site goes through here — never a bare ``cfg.model``. All
        three roles point at the same model today; the seam exists because it is
        nearly free to build and genuinely expensive to retrofit, and switching
        `fast` to Phi-4-mini-instruct later should be an env change rather than
        a refactor. A hard-coded model name anywhere is the bug that makes the
        tiering unusable.
        """
        match role:
            case "coder":
                return self.model_coder
            case "fast":
                return self.model_fast
            case "embed":
                return self.model_embed
            case _:
                raise ValueError(f"unknown model role {role!r}; use coder, fast or embed")

    def temperature_for(self, role: str) -> float:
        return self.temperature_fast if role == "fast" else self.temperature_coder


#: Variables that must never reach a local runtime.
MODEL_CREDENTIAL_VARS = (
    "DAKCODER_MODEL_API_KEY",
    "OPENAI_API_KEY",
    "LITELLM_API_KEY",
    "ANTHROPIC_API_KEY",
)


def gateway_config(env: dict[str, str] | None = None, **overrides) -> LLMConfig:
    """Build the gateway's configuration. Reads the shared model key."""
    env = os.environ if env is None else env
    key = env.get("DAKCODER_MODEL_API_KEY", "").strip()
    if not key:
        raise MissingCredential(
            "DAKCODER_MODEL_API_KEY is not set. The gateway is the only component "
            "that reads it; without it there is no model access."
        )
    return LLMConfig(
        deployment=Deployment.GATEWAY,
        base_url=env.get("DAKCODER_MODEL_BASE_URL", "https://ai.cept.gov.in/v1"),
        api_key=key,
        **overrides,
    )


def local_config(gateway_url: str, jwt: str, env: dict[str, str] | None = None, **overrides) -> LLMConfig:
    """Build a local runtime's configuration, asserting it holds no model key.

    One of the three enforcement points from §4.7. The other two are Part B
    deleting these variables from the spawn environment, and a CI grep of the
    built ``.vsix`` and the packaged wheel — belt and braces on the control that
    matters most.
    """
    env = os.environ if env is None else env
    for var in MODEL_CREDENTIAL_VARS:
        if env.get(var, "").strip():
            raise CredentialLeak(
                f"{var} is set in a local runtime's environment. Model traffic goes "
                f"through the gateway's /v1/llm proxy, which holds the real key; a "
                f"credential here would be an unmetered bypass around quota and audit. "
                f"Part B §4.6 should have stripped it before spawn."
            )
    if not jwt.strip():
        raise MissingCredential("a local runtime needs a dakcoder JWT to reach the gateway")

    return LLMConfig(
        deployment=Deployment.LOCAL,
        base_url=gateway_url.rstrip("/") + "/v1/llm",
        api_key=jwt,
        **overrides,
    )
