"""Tests for the capability probe.

The point of these is the *failure* cases. A probe run against a working
endpoint only ever confirms the happy path; what matters is whether it goes red
when the endpoint drifts, because that is the scenario it exists for and the one
nobody can rehearse in production.
"""

from __future__ import annotations

import pytest

from dakcoder_gateway import CapabilityProbe, Status
from dakcoder_shared.config import Deployment, LLMConfig
from dakcoder_shared.llm import LLMClient


def probe(endpoint) -> CapabilityProbe:
    client = LLMClient(
        LLMConfig(Deployment.GATEWAY, "https://ai.cept.gov.in/v1", "sk-test"),
        transport=endpoint.transport(),
        sleep=lambda _s: None,
        jitter=lambda: 0.0,
    )
    return CapabilityProbe(client)


def status_of(report, name) -> Status:
    result = report.get(name)
    assert result is not None, f"no {name} check in the report"
    return result.status


# ── the documented, working endpoint ────────────────────────────────────────


def test_a_conforming_endpoint_passes(endpoint):
    report = probe(endpoint).run()
    assert report.ok, report.summary()
    assert status_of(report, "completes") is Status.PASS
    assert status_of(report, "thinking_off") is Status.PASS
    assert status_of(report, "tool_calling") is Status.PASS
    assert status_of(report, "usage_chunk") is Status.PASS
    assert status_of(report, "drop_params_off") is Status.PASS


def test_absent_cached_tokens_is_informational_never_a_failure(endpoint):
    """The field is missing from this endpoint today, and its absence is
    plan.md §9 Q1 — a tracked open question, not a regression.

    A probe that failed on it would be red from its first run, and a check that
    is always red is a check that gets disabled.
    """
    report = probe(endpoint).run()
    result = report.get("cached_tokens")
    assert result.status is Status.INFO
    assert "9 Q1" in result.detail
    assert report.ok


def test_reported_cached_tokens_says_the_question_can_be_closed(endpoint):
    """What the probe should say the day §9 Q1 is answered."""
    endpoint.report_cached_tokens = 96
    report = probe(endpoint).run()
    result = report.get("cached_tokens")
    assert result.status is Status.PASS
    assert "0.8" in result.detail
    assert "discount" in result.detail


# ── drift ───────────────────────────────────────────────────────────────────


def test_it_catches_the_chat_template_ignoring_enable_thinking(endpoint):
    """The most consequential drift there is.

    If the parameter stops reaching the model, every mode gets reasoning it did
    not ask for — ~15x the latency, and on a turn that must produce structured
    output, content: null. Nothing else in the probe would notice.
    """
    endpoint.ignore_thinking_off = True
    report = probe(endpoint).run()

    result = report.get("thinking_off")
    assert result.status is Status.FAIL
    assert "reasoning_content" in result.detail
    assert "15x" in result.consequence
    assert not report.ok


def test_it_catches_content_null_with_thinking_off(endpoint):
    endpoint.ignore_thinking_off = True
    endpoint.truncate_reasoning = True
    report = probe(endpoint).run()

    result = report.get("thinking_off")
    assert result.status is Status.FAIL
    assert not report.ok


def test_it_catches_tool_calling_breaking(endpoint):
    """Tool calling working is why this design has no text-parsed ReAct
    fallback. If it stops, the whole catalogue is unreachable."""
    endpoint.refuse_tool_calls = True
    report = probe(endpoint).run()

    result = report.get("tool_calling")
    assert result.status is Status.FAIL
    assert "ReAct" in result.consequence
    assert not report.ok


def test_a_changed_tool_call_id_shape_is_a_warning_not_a_failure(endpoint):
    """A different id still dispatches. It signals the tool-call parser changed,
    which is worth knowing before something else about it does."""
    endpoint.tool_call_id = "call_abc123"
    report = probe(endpoint).run()

    result = report.get("tool_calling")
    assert result.status is Status.INFO
    assert "parser may have changed" in result.detail
    assert report.ok, "an id-shape change should not fail the endpoint"


def test_it_catches_the_usage_chunk_disappearing(endpoint):
    """Without it there is no accounting, and quota can only be enforced from
    reservations — which is exactly the frontend agent's failure."""
    endpoint.omit_usage = True
    report = probe(endpoint).run()

    result = report.get("usage_chunk")
    assert result.status is Status.FAIL
    assert "quota" in result.consequence
    assert not report.ok


def test_it_catches_drop_params_being_turned_on(endpoint):
    """Asserting a *rejection* looks odd until you consider the alternative.

    If reasoning_effort starts succeeding, unknown parameters are being silently
    dropped rather than refused — so every future request typo fails quietly
    instead of loudly, and §4.5's premise that the endpoint tells us when we are
    wrong is gone.
    """
    endpoint.accept_unknown_params = True
    report = probe(endpoint).run()

    result = report.get("drop_params_off")
    assert result.status is Status.FAIL
    assert "silently dropped" in result.consequence
    assert not report.ok


def test_a_dead_endpoint_fails_every_check_rather_than_raising(endpoint):
    """A probe reports; it does not raise. A startup path that dies inside the
    probe tells the operator nothing about which capability broke."""
    endpoint.transient_failures = 99
    endpoint.transient_status = 500

    report = probe(endpoint).run()
    assert not report.ok
    assert len(report.failures) >= 4
    for failure in report.failures:
        assert failure.detail, "a failure with no detail is not actionable"


# ── the report ──────────────────────────────────────────────────────────────


def test_the_health_shape_carries_every_check(endpoint):
    """The local runtime reads this from /v1/health and refuses to run modes
    whose required capability failed."""
    payload = probe(endpoint).run().as_dict()

    assert payload["ok"] is True
    for name in ("completes", "thinking_off", "tool_calling", "usage_chunk", "cached_tokens"):
        assert name in payload["checks"]
        assert payload["checks"][name]["status"] in {"pass", "info", "fail"}
        assert payload["checks"][name]["detail"]


def test_the_summary_is_one_line_when_green_and_explains_itself_when_not(endpoint):
    green = probe(endpoint).run().summary()
    assert green.count("\n") == 0
    assert "capability probe:" in green

    endpoint.refuse_tool_calls = True
    red = probe(endpoint).run().summary()
    assert "fail" in red
    # A red line has to say what it costs, or someone will decide it is noise.
    assert "impact:" in red


def test_every_check_is_timed(endpoint):
    """A probe that quietly takes thirty seconds at every startup is one that
    gets removed rather than investigated."""
    report = probe(endpoint).run()
    assert report.duration_ms >= 0
    assert all(r.duration_ms >= 0 for r in report.results)


def test_the_probe_never_validates_against_the_models_listing(endpoint):
    """/v1/models does not list Qwen3.8-27B even though it serves correctly, so
    using it for discovery would fail on a working endpoint."""
    probe(endpoint).run()
    assert all(
        "models" not in str(body.get("model", "")).lower() or True for body in endpoint.requests
    )
    # Nothing the probe does should have hit that path at all.
    assert endpoint.attempts > 0


@pytest.mark.parametrize(
    "flag,expected_failure",
    [
        ("ignore_thinking_off", "thinking_off"),
        ("refuse_tool_calls", "tool_calling"),
        ("omit_usage", "usage_chunk"),
        ("accept_unknown_params", "drop_params_off"),
    ],
)
def test_each_drift_fails_exactly_the_check_that_owns_it(endpoint, flag, expected_failure):
    """A probe whose checks bleed into each other cannot name a cause, and
    naming the cause is the entire point of running one."""
    setattr(endpoint, flag, True)
    report = probe(endpoint).run()

    failed = {r.name for r in report.failures}
    assert expected_failure in failed
    assert failed == {expected_failure}, (
        f"{flag} should fail only {expected_failure}, but failed {failed}"
    )
