"""Tests for the role routing table.

The thing being protected here is an operational property, not a code path:
changing which model answers as the Planner — or moving it to another host on
another key — must be a change to ``deploy/dakcoder.env`` and a restart. Every
test below is that sentence in one form or another, plus the two ways it can go
quietly wrong: a role left without a credential, and a name the two halves of
the system disagree about.
"""

from __future__ import annotations

import pytest

from dakcoder_gateway.routing import (
    DEFAULT_BASE_URL,
    DEFAULT_MODEL,
    ModelRoute,
    RoleRouter,
    RoutingError,
)
from dakcoder_shared.config import ROLES, MissingCredential, local_config

BASE = {"DAKCODER_MODEL_API_KEY": "sk-shared"}


# ── the default: one endpoint, one key, one model ───────────────────────────


def test_a_deployment_that_configures_nothing_behaves_as_it_always_did() -> None:
    """Three variables is the common case and it must stay three variables."""
    router = RoleRouter.from_env(BASE)

    for role in ROLES:
        route = router.resolve(role)
        assert route.model == DEFAULT_MODEL
        assert route.base_url == DEFAULT_BASE_URL
        assert route.api_key == "sk-shared"
        assert route.overrides == ()

    # One upstream, so the capability probe makes one pass, not eight.
    assert len(router.endpoints()) == 1


def test_the_default_model_is_one_variable_for_every_role() -> None:
    router = RoleRouter.from_env({**BASE, "DAKCODER_MODEL": "Qwen3-Next-80B"})
    assert set(router.models.values()) == {"Qwen3-Next-80B"}


# ── the point of the exercise ───────────────────────────────────────────────


def test_a_role_can_have_its_own_model_endpoint_and_key() -> None:
    """All three, independently, from the environment. This is the whole
    feature: no edit to the proxy, the shared config or the probe wiring."""
    router = RoleRouter.from_env(
        {
            **BASE,
            "DAKCODER_MODEL_BASE_URL": "http://127.0.0.1:4000/v1",
            "DAKCODER_MODEL_PLANNER": "Qwen3-235B-A22B",
            "DAKCODER_MODEL_PLANNER_BASE_URL": "http://10.0.0.9:4000/v1/",
            "DAKCODER_MODEL_PLANNER_API_KEY": "sk-planner",
        }
    )

    planner = router.resolve("planner")
    assert planner.model == "Qwen3-235B-A22B"
    assert planner.base_url == "http://10.0.0.9:4000/v1"  # the trailing slash is ours to drop
    assert planner.api_key == "sk-planner"

    # And nothing else moved with it.
    coder = router.resolve("coder")
    assert (coder.model, coder.base_url, coder.api_key) == (
        DEFAULT_MODEL,
        "http://127.0.0.1:4000/v1",
        "sk-shared",
    )


def test_a_role_inherits_whatever_it_does_not_name() -> None:
    """The common override is a model, on the endpoint and key already
    configured. Making that require all three would make the cheap change
    expensive, which is how a table like this stops being used."""
    router = RoleRouter.from_env({**BASE, "DAKCODER_MODEL_FAST": "Phi-4-mini-instruct"})

    fast = router.resolve("fast")
    assert fast.model == "Phi-4-mini-instruct"
    assert fast.api_key == "sk-shared"
    assert fast.base_url == DEFAULT_BASE_URL
    assert fast.overrides == ("model",)


def test_what_was_overridden_is_recorded_so_it_can_be_published() -> None:
    """/v1/models answers "did my change take effect?" — which is otherwise
    answerable only by watching behaviour, the thing this replaces."""
    router = RoleRouter.from_env(
        {**BASE, "DAKCODER_MODEL_ASK": "Phi-4", "DAKCODER_MODEL_ASK_API_KEY": "sk-ask"}
    )
    assert router.resolve("ask").as_dict() == {
        "model": "Phi-4",
        "endpoint": DEFAULT_BASE_URL,
        "overrides": ["model", "api_key"],
    }


def test_a_published_route_never_carries_its_key() -> None:
    router = RoleRouter.from_env({**BASE, "DAKCODER_MODEL_CODER_API_KEY": "sk-secret"})
    assert "sk-secret" not in repr(router.as_dict())
    assert "sk-shared" not in repr(router.as_dict())
    assert "sk-shared" not in repr(router.models)


# ── failing at startup, not at the first turn ───────────────────────────────


def test_a_role_with_no_key_anywhere_stops_the_gateway_starting() -> None:
    """It would otherwise present as a 502 hours later, to whichever developer
    happened to reach that role first, with nothing tying it to a variable."""
    with pytest.raises(MissingCredential) as caught:
        RoleRouter.from_env({})

    assert "planner" in str(caught.value)
    # And says exactly which variable to set, for one role or for all of them.
    assert "DAKCODER_MODEL_API_KEY" in str(caught.value)
    assert "DAKCODER_MODEL_PLANNER_API_KEY" in str(caught.value)


def test_per_role_keys_alone_are_enough() -> None:
    """A deployment where every endpoint has its own credential should not have
    to invent a shared one it never uses."""
    env = {f"DAKCODER_MODEL_{role.upper()}_API_KEY": f"sk-{role}" for role in ROLES}
    router = RoleRouter.from_env(env)
    assert router.resolve("planner").api_key == "sk-planner"
    assert router.resolve("embed").api_key == "sk-embed"


# ── extra roles ─────────────────────────────────────────────────────────────


def test_declaring_extra_roles_adds_to_the_vocabulary_rather_than_replacing_it() -> None:
    """Read as a replacement, `planner,coder` would silently unconfigure `fast`
    and `embed` — roles the runtime calls on its own — and the first compaction
    of the day would fail with "not a configured role"."""
    router = RoleRouter.from_env(
        {**BASE, "DAKCODER_MODEL_ROLES": "planner,reviewer", "DAKCODER_MODEL_REVIEWER": "Phi-4"}
    )
    assert router.resolve("reviewer").model == "Phi-4"
    assert set(ROLES) <= set(router.routes)


def test_a_role_name_that_would_collide_with_its_own_variable_is_refused() -> None:
    with pytest.raises(RoutingError, match="cannot be a role name"):
        RoleRouter.from_env({**BASE, "DAKCODER_MODEL_ROLES": "roles"})


def test_a_role_name_that_is_not_a_variable_name_is_refused() -> None:
    with pytest.raises(RoutingError, match="cannot be a role name"):
        RoleRouter.from_env({**BASE, "DAKCODER_MODEL_ROLES": "my role!"})


# ── what the probe has to cover ─────────────────────────────────────────────


def test_endpoints_are_deduplicated_and_labelled_by_the_roles_that_share_them() -> None:
    """Six roles on one host is one probe pass. A role moved elsewhere is a
    second one — which is the point: an endpoint nobody probes is an endpoint
    whose drift presents as inexplicable agent behaviour."""
    router = RoleRouter.from_env(
        {
            **BASE,
            "DAKCODER_MODEL_PLANNER_BASE_URL": "http://10.0.0.9:4000/v1",
            "DAKCODER_MODEL_FAST": "Phi-4-mini-instruct",
        }
    )
    endpoints = dict(router.endpoints())

    assert len(endpoints) == 3, endpoints
    assert "planner" in endpoints
    assert "fast" in endpoints
    # Everything left over shares the default, and is probed once between them.
    assert any("coder" in label and "ask" in label for label in endpoints)


def test_a_route_probes_its_own_model_whatever_the_probe_calls_the_role() -> None:
    """The probe's checks name `coder` and `fast` because that is what they were
    written against. A route for `planner` still has to be exercised as itself."""
    config = ModelRoute("planner", "Qwen3-235B", "http://10.0.0.9:4000/v1", "sk-p").as_config()

    assert config.base_url == "http://10.0.0.9:4000/v1"
    assert config.api_key == "sk-p"
    assert config.model_for("planner") == "Qwen3-235B"
    assert config.model_for("coder") == "Qwen3-235B"
    assert config.model_for("fast") == "Qwen3-235B"


# ── the two halves agree ────────────────────────────────────────────────────


def test_every_role_the_runtime_may_send_is_one_the_gateway_can_route() -> None:
    """A name known to one side and not the other is a turn that fails with "not
    a configured role". The summariser sent exactly such a name for its whole
    life, and every compaction in production silently returned a canned recap."""
    router = RoleRouter.from_env(BASE)
    local = local_config("https://aiops.cept.gov.in/coder/backend", "jwt", env={})

    for role in ROLES:
        assert local.model_for(role) == role
        assert router.resolve(role).model
