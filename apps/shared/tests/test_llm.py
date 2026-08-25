"""Tests for the LLM client, against the fake endpoint in ../conftest.py."""

from __future__ import annotations

import httpx
import pytest

from dakcoder_shared.config import (
    CredentialLeak,
    Deployment,
    LLMConfig,
    MissingCredential,
    gateway_config,
    local_config,
)
from dakcoder_shared.llm import (
    BACKOFF_SECONDS,
    EmptyCompletionError,
    LLMClient,
    UnsupportedParameterError,
    UpstreamError,
    strip_html,
)


def client(endpoint, *, deployment=Deployment.GATEWAY, sleep=None, **cfg) -> LLMClient:
    config = LLMConfig(
        deployment=deployment,
        base_url="https://ai.cept.gov.in/v1",
        api_key="sk-test",
        **cfg,
    )
    return LLMClient(
        config,
        transport=endpoint.transport(),
        sleep=sleep or (lambda _s: None),
        jitter=lambda: 0.0,
    )


# ── the credential invariant ────────────────────────────────────────────────


def test_a_local_runtime_refuses_to_start_holding_a_model_key():
    """One of §4.7's three enforcement points.

    A stale variable in a developer's shell must not become an unmetered bypass
    around the entire quota system.
    """
    for var in ("DAKCODER_MODEL_API_KEY", "OPENAI_API_KEY", "LITELLM_API_KEY"):
        with pytest.raises(CredentialLeak, match="bypass"):
            local_config("https://aiops.cept.gov.in/coder/backend", "jwt", env={var: "sk-leaked"})


def test_a_local_runtime_needs_a_jwt():
    with pytest.raises(MissingCredential):
        local_config("https://aiops.cept.gov.in/coder/backend", "", env={})


def test_a_local_runtime_points_at_the_gateway_proxy_not_the_model():
    cfg = local_config("https://aiops.cept.gov.in/coder/backend/", "jwt-abc", env={})
    assert cfg.base_url == "https://aiops.cept.gov.in/coder/backend/v1/llm"
    assert cfg.api_key == "jwt-abc"
    assert cfg.deployment is Deployment.LOCAL


def test_the_gateway_refuses_to_start_without_a_key():
    with pytest.raises(MissingCredential, match="DAKCODER_MODEL_API_KEY"):
        gateway_config(env={})


def test_the_two_deployments_differ_only_in_what_they_authenticate_to():
    """What makes the invariant cheap enough to hold permanently: the agent loop
    is identical in both modes."""
    gw = gateway_config(env={"DAKCODER_MODEL_API_KEY": "sk-real"})
    local = local_config("https://aiops.cept.gov.in/coder/backend", "jwt", env={})
    assert gw.model_for("coder") == local.model_for("coder")
    assert gw.temperature_for("coder") == local.temperature_for("coder")
    assert gw.api_key != local.api_key


def test_models_resolve_by_role_never_by_a_bare_name():
    cfg = LLMConfig(Deployment.GATEWAY, "u", "k", model_fast="Phi-4-mini-instruct")
    assert cfg.model_for("fast") == "Phi-4-mini-instruct"
    assert cfg.model_for("coder") == "Qwen3.8-27B"
    with pytest.raises(ValueError, match="unknown model role"):
        cfg.model_for("planner")


# ── request shaping ─────────────────────────────────────────────────────────


def test_every_load_bearing_parameter_is_sent(endpoint):
    with client(endpoint, user="gitlab:4102") as c:
        c.chat([{"role": "user", "content": "hi"}], max_tokens=4096, enable_thinking=False)

    sent = endpoint.requests[0]
    assert sent["stream"] is True
    # Without this there is no usage chunk and therefore no accounting at all.
    assert sent["stream_options"] == {"include_usage": True}
    # The only lever for reasoning on this endpoint; reasoning_effort is refused.
    assert sent["chat_template_kwargs"] == {"enable_thinking": False}
    # Attribution at the proxy, before per-user virtual keys exist.
    assert sent["user"] == "gitlab:4102"
    assert sent["max_tokens"] == 4096
    assert sent["model"] == "Qwen3.8-27B"


def test_transport_is_configured_for_nginx_streaming(endpoint):
    """HTTP/1.1 and no inherited proxy, both deliberate.

    nginx's HTTP/2 handling is a known source of buffering and dropped streams
    on long-lived responses, and inheriting HTTP(S)_PROXY for an internal host
    is the misconfiguration that cost the frontend agent real latency.
    """
    with client(endpoint) as c:
        assert c._client.trust_env is False


# ── streaming ───────────────────────────────────────────────────────────────


def test_content_deltas_are_accumulated(endpoint):
    with client(endpoint) as c:
        result = c.chat([{"role": "user", "content": "hi"}], max_tokens=64, enable_thinking=False)
    assert result.content == "ready"
    assert result.finish_reason == "stop"
    assert result.model == "Qwen3.8-27B"


def test_tool_call_fragments_are_reassembled(endpoint):
    """Arguments arrive split across frames; reading only the first yields a
    valid-looking JSON prefix rather than an error."""
    tools = [{"type": "function", "function": {"name": "rules_lint", "parameters": {}}}]
    with client(endpoint) as c:
        result = c.chat(
            [{"role": "user", "content": "lint"}],
            max_tokens=256, enable_thinking=False, tools=tools,
        )

    assert result.finish_reason == "tool_calls"
    assert len(result.tool_calls) == 1
    call = result.tool_calls[0]
    assert call.name == "rules_lint"
    assert call.parsed() == {"paths": ["handler/user.go"]}
    assert call.id.startswith("chatcmpl-tool-")


def test_a_pure_tool_call_turn_is_not_mistaken_for_an_empty_completion(endpoint):
    """A tool call legitimately carries no content."""
    tools = [{"type": "function", "function": {"name": "rules_lint", "parameters": {}}}]
    with client(endpoint) as c:
        result = c.chat(
            [{"role": "user", "content": "lint"}],
            max_tokens=256, enable_thinking=False, tools=tools,
        )
    assert result.content == ""
    assert result.tool_calls


def test_usage_is_read_from_the_final_chunk(endpoint):
    with client(endpoint) as c:
        result = c.chat([{"role": "user", "content": "hi"}], max_tokens=64, enable_thinking=False)
    assert result.usage.prompt_tokens == 120
    assert result.usage.completion_tokens == 20
    assert result.usage.total_tokens == 140


def test_absent_cached_tokens_is_none_not_zero(endpoint):
    """'Not reported' and 'nothing was cached' are different facts, and
    plan.md §9 Q1 turns on the difference."""
    with client(endpoint) as c:
        result = c.chat([{"role": "user", "content": "hi"}], max_tokens=64, enable_thinking=False)
    assert result.usage.cached_tokens is None
    assert result.usage.cache_hit_rate is None


def test_cached_tokens_are_read_when_the_endpoint_starts_reporting_them(endpoint):
    """What happens the day §9 Q1 is answered."""
    endpoint.report_cached_tokens = 96
    with client(endpoint) as c:
        result = c.chat([{"role": "user", "content": "hi"}], max_tokens=64, enable_thinking=False)
    assert result.usage.cached_tokens == 96
    assert result.usage.cache_hit_rate == 0.8


def test_reasoning_tokens_are_metered(endpoint):
    """Charged against the output budget, and metered per mode so §4.4's on/off
    choices have evidence rather than superstition behind them."""
    with client(endpoint) as c:
        result = c.chat([{"role": "user", "content": "hi"}], max_tokens=6144, enable_thinking=True)
    assert result.usage.reasoning_tokens == 60
    assert result.reasoning


def test_a_malformed_frame_does_not_lose_the_turn(endpoint):
    def handler(request):
        body = (
            b'data: {"choices":[{"delta":{"content":"re"}}]}\n\n'
            b"data: not json at all\n\n"
            b'data: {"choices":[{"delta":{"content":"ady"},"finish_reason":"stop"}]}\n\n'
            b'data: {"choices":[],"usage":{"prompt_tokens":10,"completion_tokens":2}}\n\n'
            b"data: [DONE]\n\n"
        )
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=body)

    c = LLMClient(
        LLMConfig(Deployment.GATEWAY, "https://x/v1", "k"),
        transport=httpx.MockTransport(handler),
        sleep=lambda _s: None,
    )
    with c:
        assert c.chat([{"role": "user", "content": "hi"}], max_tokens=16).content == "ready"


# ── the empty completion ────────────────────────────────────────────────────


def test_an_empty_completion_is_a_typed_error(endpoint):
    """§4.4 rule 3. A typed error rather than an empty string, because the two
    need completely different handling."""
    endpoint.truncate_reasoning = True
    with client(endpoint) as c:
        with pytest.raises(EmptyCompletionError) as exc:
            c.chat(
                [{"role": "user", "content": "hi"}],
                max_tokens=6144, enable_thinking=True, recover_empty=False,
            )
    assert exc.value.finish_reason == "length"
    assert exc.value.reasoning_tokens == 4000


def test_recovery_retries_with_thinking_off_not_with_a_bigger_budget(endpoint):
    """The spike's lesson: a bigger budget is what produced the 31-second run
    for the same 330-character answer."""
    endpoint.truncate_reasoning = True
    with client(endpoint) as c:
        result = c.chat(
            [{"role": "user", "content": "hi"}], max_tokens=6144, enable_thinking=True,
        )

    assert result.recovered_from_empty
    assert result.content == "ready"

    first, second = endpoint.requests
    assert first["chat_template_kwargs"]["enable_thinking"] is True
    assert second["chat_template_kwargs"]["enable_thinking"] is False
    # The budget is unchanged: it was never the problem.
    assert second["max_tokens"] == first["max_tokens"]


def test_recovery_is_not_attempted_when_thinking_was_already_off(endpoint):
    """Nothing left to turn off; retrying would just spend another turn."""
    endpoint.ignore_thinking_off = True
    endpoint.truncate_reasoning = True

    def handler(request):
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=(
                b'data: {"choices":[{"delta":{"content":null},"finish_reason":"length"}]}\n\n'
                b"data: [DONE]\n\n"
            ),
        )

    c = LLMClient(
        LLMConfig(Deployment.GATEWAY, "https://x/v1", "k"),
        transport=httpx.MockTransport(handler),
        sleep=lambda _s: None,
    )
    with c, pytest.raises(EmptyCompletionError):
        c.chat([{"role": "user", "content": "hi"}], max_tokens=64, enable_thinking=False)


# ── retry ───────────────────────────────────────────────────────────────────


def test_transient_failures_are_retried_with_backoff(endpoint):
    endpoint.transient_failures = 2
    slept: list[float] = []

    with client(endpoint, sleep=slept.append) as c:
        result = c.chat([{"role": "user", "content": "hi"}], max_tokens=64, enable_thinking=False)

    assert result.content == "ready"
    assert result.attempts == 3
    assert slept == list(BACKOFF_SECONDS)


def test_an_unsupported_parameter_is_never_retried(endpoint):
    """Always our bug, never transient. Retrying spends a second learning
    nothing — and drop_params being off is what makes it loud."""
    with client(endpoint) as c:
        body = c.build_request([{"role": "user", "content": "hi"}], max_tokens=16, enable_thinking=False)
        body["reasoning_effort"] = "low"
        with pytest.raises(UnsupportedParameterError):
            c._send(body)

    assert endpoint.attempts == 1


def test_a_non_retryable_4xx_is_not_retried(endpoint):
    endpoint.transient_failures = 1
    endpoint.transient_status = 403
    with client(endpoint) as c, pytest.raises(UpstreamError) as exc:
        c.chat([{"role": "user", "content": "hi"}], max_tokens=64, enable_thinking=False)
    assert exc.value.status == 403
    assert endpoint.attempts == 1


def test_retries_are_bounded(endpoint):
    endpoint.transient_failures = 99
    with client(endpoint, max_attempts=3) as c, pytest.raises(UpstreamError):
        c.chat([{"role": "user", "content": "hi"}], max_tokens=64, enable_thinking=False)
    assert endpoint.attempts == 3


def test_an_html_error_page_is_reduced_to_one_sentence(endpoint):
    """nginx answers some failures with a full HTML page; putting that into a
    prompt or a chat bubble buries the one useful line in markup."""
    endpoint.transient_failures = 99
    endpoint.transient_status = 502
    endpoint.html_errors = True

    with client(endpoint) as c, pytest.raises(UpstreamError) as exc:
        c.chat([{"role": "user", "content": "hi"}], max_tokens=64, enable_thinking=False)

    detail = exc.value.detail
    assert "<" not in detail and ">" not in detail
    assert "502 Service Temporarily Unavailable" in detail
    assert len(detail) < 200


def test_strip_html_handles_a_bare_page():
    out = strip_html("<html><body><h1>504 Gateway Time-out</h1>\n<hr>nginx</body></html>")
    assert out.startswith("504 Gateway Time-out")
    assert "<" not in out


def test_the_context_ceiling_is_reported_as_a_plain_error(endpoint):
    """262,144 is a ceiling, not a target — the agent caps itself at 32k — but
    an over-large request should still fail legibly."""
    with client(endpoint) as c, pytest.raises(UpstreamError) as exc:
        c.chat([{"role": "user", "content": "hi"}], max_tokens=300_000, enable_thinking=False)
    assert "262144" in exc.value.detail
