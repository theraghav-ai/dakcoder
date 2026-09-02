"""Which model answers for which role, where it lives, and what pays for it.

Everything a role's traffic needs is one record — model name, endpoint, key —
and all three come from the environment. That is the whole point: switching the
planner to a different model on a different host with a different key is three
lines in ``deploy/dakcoder.env`` and a restart, not an edit in the proxy, an
edit in the shared config, an edit in the probe wiring, and a deploy.

    DAKCODER_MODEL_BASE_URL          the default endpoint, for every role
    DAKCODER_MODEL_API_KEY           the default key, for every role
    DAKCODER_MODEL                   the default model, for every role

    DAKCODER_MODEL_PLANNER           …and the planner's own model
    DAKCODER_MODEL_PLANNER_BASE_URL  …its own endpoint
    DAKCODER_MODEL_PLANNER_API_KEY   …its own key

Anything a role does not name itself it inherits from the default, so the common
deployment — one LiteLLM, one key, one model — sets three variables and is done.

**The roles are fixed, the routing is not.** ``ROLES`` is the vocabulary both
halves of the system share: the runtime sends one of those names as ``model``
and the gateway resolves it here. A developer still cannot name a model, which
is the control §15.4 exists for — they name a role, and an operator decides what
that role costs.

**A key per role is still a key the gateway alone holds.** Splitting the
credential does not weaken the invariant; it widens the shape that has to be
kept off a laptop, which is why ``leaked_model_credentials`` matches the pattern
rather than a list of names.
"""

from __future__ import annotations

import os
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from dakcoder_shared.config import ROLES, Deployment, LLMConfig, MissingCredential

__all__ = ["DEFAULT_BASE_URL", "DEFAULT_MODEL", "ModelRoute", "RoleRouter", "RoutingError"]

#: The endpoint and model this deployment has always used, kept as the defaults
#: so an environment that sets neither behaves exactly as it did before.
DEFAULT_BASE_URL = "https://ai.cept.gov.in/v1"
DEFAULT_MODEL = "Qwen3.8-27B"

_ROLE_NAME = re.compile(r"^[a-z][a-z0-9_-]*$")

#: ``DAKCODER_MODEL_ROLES`` declares extra roles; a role called ``roles`` would
#: read its model from that same variable. One collision, refused by name rather
#: than left to be discovered as a role whose model is the word "planner,coder".
RESERVED_ROLE_NAMES = frozenset({"roles"})


class RoutingError(RuntimeError):
    """The routing table in the environment does not describe a usable gateway."""


def env_prefix(role: str) -> str:
    """The variable stem for a role: ``planner`` -> ``DAKCODER_MODEL_PLANNER``."""
    return "DAKCODER_MODEL_" + role.upper().replace("-", "_")


@dataclass(frozen=True, slots=True)
class ModelRoute:
    """One role's whole answer to "which model, where, on whose key"."""

    role: str
    model: str
    base_url: str
    api_key: str
    #: Which of the three the role named for itself, rather than inheriting.
    #: Published so an operator can see that an override took effect without
    #: reading a log or inferring it from behaviour — the same reason the quota
    #: limits are published at /v1/health.
    overrides: tuple[str, ...] = ()

    def url(self, path: str) -> str:
        return f"{self.base_url}/{path.lstrip('/')}"

    def headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
        }

    @property
    def endpoint(self) -> tuple[str, str, str]:
        """What makes two routes the same upstream, for probing and pooling."""
        return (self.base_url, self.model, self.api_key)

    def as_config(self, **overrides) -> LLMConfig:
        """This route as an ``LLMConfig``, for the capability probe.

        The probe asks for roles ``coder`` and ``fast`` by name because that is
        what its checks are written against. Mapping both — and this route's own
        role — onto this route's model means the probe exercises *this* model on
        *this* endpoint whatever the route is called.
        """
        return LLMConfig(
            deployment=Deployment.GATEWAY,
            base_url=self.base_url,
            api_key=self.api_key,
            models={self.role: self.model, "coder": self.model, "fast": self.model},
            model_coder=self.model,
            model_fast=self.model,
            model_embed=self.model,
            **overrides,
        )

    def as_dict(self) -> dict[str, object]:
        """Never the key. The endpoint, because "did my override take?" is the
        question this answers, and it is asked from behind authentication."""
        return {
            "model": self.model,
            "endpoint": self.base_url,
            "overrides": list(self.overrides),
        }


@dataclass(frozen=True, slots=True)
class RoleRouter:
    """Role -> route. The only models this gateway will ever ask for."""

    routes: Mapping[str, ModelRoute]
    #: What an unnamed role would have inherited. Kept for the places that need
    #: "the endpoint this gateway mostly talks to" — a startup log line, the
    #: proxy's two-argument form — without picking a role arbitrarily.
    default: ModelRoute

    def resolve(self, role: str) -> ModelRoute:
        route = self.routes.get(str(role).strip().lower())
        if route is None:
            raise KeyError(role)
        return route

    @property
    def models(self) -> dict[str, str]:
        """Role -> model name. Safe to publish; no endpoint, no credential."""
        return {role: route.model for role, route in self.routes.items()}

    def endpoints(self) -> list[tuple[str, ModelRoute]]:
        """The distinct upstreams, labelled by the roles that share them.

        The capability probe runs against each one. A second endpoint that
        rejects ``chat_template_kwargs`` or has stopped sending the usage chunk
        is exactly the drift the probe exists to catch, and probing only the
        default would leave every role pointed elsewhere unchecked — silently,
        which is the failure mode §4.5 is written against.
        """
        grouped: dict[tuple[str, str, str], list[str]] = {}
        for role, route in self.routes.items():
            grouped.setdefault(route.endpoint, []).append(role)
        return [(",".join(roles), self.routes[roles[0]]) for roles in grouped.values()]

    def as_dict(self) -> dict[str, dict[str, object]]:
        return {role: route.as_dict() for role, route in self.routes.items()}

    # -- construction -------------------------------------------------------

    @classmethod
    def uniform(
        cls,
        base_url: str,
        api_key: str,
        *,
        model: str = DEFAULT_MODEL,
        roles: Iterable[str] = ROLES,
    ) -> RoleRouter:
        """One endpoint, one key, one model for every role."""
        base = base_url.rstrip("/")
        return cls(
            routes={
                role: ModelRoute(role, model, base, api_key) for role in dict.fromkeys(roles)
            },
            default=ModelRoute("default", model, base, api_key),
        )

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> RoleRouter:
        """Read the routing table. The one place these variables are named.

        Fails at startup rather than at the first turn of the first run. A role
        with no key would present as a 502 hours later, on whichever developer
        happened to trigger that role first, and the cause would be invisible
        from the symptom.
        """
        env = os.environ if env is None else env

        def read(name: str, fallback: str = "") -> str:
            return str(env.get(name, "") or "").strip() or fallback

        base_url = read("DAKCODER_MODEL_BASE_URL", DEFAULT_BASE_URL).rstrip("/")
        api_key = read("DAKCODER_MODEL_API_KEY")
        model = read("DAKCODER_MODEL", DEFAULT_MODEL)

        # Additive, never a replacement. Read as a replacement, an operator who
        # set `planner,coder` would silently unconfigure `fast` and `embed` —
        # roles the runtime calls on its own, so the first compaction of the day
        # would fail with "not a configured role" and nothing would connect that
        # to a variable listing two roles that both work.
        extra: list[str] = []
        for name in read("DAKCODER_MODEL_ROLES").split(","):
            role = name.strip().lower()
            if not role or role in ROLES or role in extra:
                continue
            if role in RESERVED_ROLE_NAMES or not _ROLE_NAME.match(role):
                raise RoutingError(
                    f"{role!r} cannot be a role name. Use lower-case letters, digits, "
                    f"'-' or '_', starting with a letter, and not "
                    f"{', '.join(sorted(RESERVED_ROLE_NAMES))} — its variables would "
                    f"collide with DAKCODER_MODEL_ROLES itself."
                )
            extra.append(role)

        routes: dict[str, ModelRoute] = {}
        for role in (*ROLES, *extra):
            stem = env_prefix(role)
            own_model = read(stem)
            own_url = read(f"{stem}_BASE_URL")
            own_key = read(f"{stem}_API_KEY")
            routes[role] = ModelRoute(
                role=role,
                model=own_model or model,
                base_url=(own_url or base_url).rstrip("/"),
                api_key=own_key or api_key,
                overrides=tuple(
                    aspect
                    for aspect, value in (
                        ("model", own_model),
                        ("endpoint", own_url),
                        ("api_key", own_key),
                    )
                    if value
                ),
            )

        unpaid = [role for role, route in routes.items() if not route.api_key]
        if unpaid:
            raise MissingCredential(
                f"no model API key for {', '.join(unpaid)}. The gateway is the only "
                f"component that holds one, and without it there is nothing to proxy. "
                f"Set DAKCODER_MODEL_API_KEY for every role, or "
                f"{env_prefix(unpaid[0])}_API_KEY for just this one."
            )

        return cls(routes=routes, default=ModelRoute("default", model, base_url, api_key))
