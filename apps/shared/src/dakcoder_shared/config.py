"""Runtime configuration, and the one invariant it exists to enforce.

The model API key is a single shared LiteLLM credential. If a laptop held it,
every developer could spend the shared GPU budget with no ceiling and no
attribution, and the whole quota model in Part A §16 would be decorative. So the
key lives in exactly one place — the gateway's secret store — and this module is
where that is checked rather than assumed.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum

__all__ = [
    "ROLES",
    "Deployment",
    "LLMConfig",
    "MissingCredential",
    "CredentialLeak",
    "leaked_model_credentials",
]

#: Every role either half of the system may name, and the only names the
#: gateway's routing table is built from by default.
#:
#: One tuple, in one place, because the two halves have to agree: the runtime
#: sends a role and the gateway resolves it, so a name known to one and not the
#: other is a turn that fails with "not a configured role". That is not
#: hypothetical — the summariser spent its whole life sending ``summariser``
#: to a client that only accepted ``coder``, ``fast`` and ``embed``, and every
#: compaction in production silently fell back to a canned recap.
#:
#: Adding a name here makes it configurable at the gateway
#: (``DAKCODER_MODEL_<ROLE>``) and requestable from the runtime. Nothing else
#: is needed, and nothing else should be.
ROLES: tuple[str, ...] = (
    "planner",
    "coder",
    "ask",
    "verifier",
    "debugger",
    "fast",
    "summariser",
    "embed",
)


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
    #: Role → model, consulted before the three fields above.
    #:
    #: The three named fields predate per-role routing and are kept because they
    #: read well at a call site that only has one endpoint. This mapping is what
    #: a gateway built from the environment fills in, and it can carry any role
    #: in `ROLES` rather than only the original three — which is the difference
    #: between "the planner can have its own model" and "the planner can have
    #: its own model once someone adds a field and redeploys".
    models: Mapping[str, str] = field(default_factory=dict)

    temperature_coder: float = 0.1
    temperature_fast: float = 0.0

    connect_timeout: float = 5.0
    #: Deliberately *below* the gateway's own upstream timeout, so that on a
    #: prefill long enough to hit a ceiling it is this client that gives up
    #: first. The two used to be 600 here against 300 there, so the gateway
    #: always cut first — and because its headers were already sent, it could
    #: only say so with an in-band `event: error` frame that the client read as
    #: an empty success. A client that times out first raises `ReadTimeout`,
    #: which `_send` retries; a gateway that times out first is a turn silently
    #: lost. Keep this strictly less than `ModelProxy.timeout`.
    read_timeout: float = 540.0
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

        **A local runtime returns the role itself.** D-59: the client names a
        role and the gateway names the model, precisely so a developer cannot
        route to a model nobody budgeted for. Resolving the name here would send
        ``model: "Qwen3.8-27B"`` to the proxy, which reads that field as a role
        and refuses it — so every call from a local runtime failed with "is not
        a configured role". The resolution belongs on the side that holds the
        key, and this method is the seam that says which side that is.

        The local check is against `ROLES` — the shared vocabulary — rather than
        the three roles this class happens to have fields for. A name in that
        tuple is one the gateway's routing table has an entry for, so passing it
        through is safe; a name outside it would be refused upstream anyway, and
        catching the typo here costs a round trip less.
        """
        key = role.strip().lower()
        if self.deployment is Deployment.LOCAL:
            if key not in ROLES:
                raise ValueError(
                    f"unknown model role {role!r}; the roles this system knows are "
                    f"{', '.join(ROLES)}"
                )
            return key

        if key in self.models:
            return self.models[key]
        # The named fields are tiers, not roles: a role this configuration was
        # not given an explicit model for gets the general-purpose one, which is
        # what every role got before any of them could be routed separately. A
        # name outside the vocabulary still raises — that is a typo or a bare
        # model name, and neither should quietly become a request.
        match key:
            case "fast":
                return self.model_fast
            case "embed":
                return self.model_embed
            case _ if key in ROLES:
                return self.model_coder
            case _:
                raise ValueError(
                    f"unknown model role {role!r}; the roles this system knows are "
                    f"{', '.join(ROLES)}"
                )

    def temperature_for(self, role: str) -> float:
        return self.temperature_fast if role == "fast" else self.temperature_coder


#: Variables that must never reach a local runtime.
MODEL_CREDENTIAL_VARS = (
    "DAKCODER_MODEL_API_KEY",
    "OPENAI_API_KEY",
    "LITELLM_API_KEY",
    "ANTHROPIC_API_KEY",
)

#: The per-role form of the same secret: ``DAKCODER_MODEL_PLANNER_API_KEY`` and
#: friends.
#:
#: A fixed list stopped being sufficient the moment a role could carry its own
#: key. A list is only as good as whoever remembers to extend it, and the thing
#: it guards — the one credential that makes quota and audit unbypassable — is
#: the worst possible place to rely on that. So the shape is matched instead of
#: the names enumerated.
MODEL_CREDENTIAL_PATTERN = re.compile(r"^DAKCODER_MODEL(_[A-Z0-9_]+)?_API_KEY$")


def leaked_model_credentials(env: Mapping[str, str]) -> list[str]:
    """Every model credential set in ``env``, named, in a stable order.

    Used by the local runtime to refuse to start and by the launcher to decide
    what to strip. Returning the names rather than a bool is deliberate: the
    developer has to be told *which* variable to remove, and "a model credential
    is set" sends them looking through a shell profile at random.
    """
    found = {
        name
        for name in env
        if name in MODEL_CREDENTIAL_VARS or MODEL_CREDENTIAL_PATTERN.match(name)
    }
    return sorted(name for name in found if str(env.get(name, "") or "").strip())


def gateway_config(env: dict[str, str] | None = None, **overrides) -> LLMConfig:
    """Build the gateway's configuration for a *single* endpoint.

    The shared default: one base URL, one key, one model for every role. A
    gateway that routes roles to different endpoints builds one of these per
    endpoint instead — see ``dakcoder_gateway.routing.RoleRouter``, which owns
    the ``DAKCODER_MODEL_<ROLE>_*`` variables and hands each route back as one
    of these. This stays because a probe, a script or a test with one endpoint
    should not have to think about a routing table.
    """
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
    leaked = leaked_model_credentials(env)
    if leaked:
        raise CredentialLeak(
            f"{', '.join(leaked)} set in a local runtime's environment. Model traffic "
            f"goes through the gateway's /v1/llm proxy, which holds the real key; a "
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
