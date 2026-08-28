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
    ChatResult,
    EmptyCompletionError,
    LLMClient,
    Metering,
    ToolCall,
    UnsupportedParameterError,
    UpstreamError,
    _consume_stream,
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
    is identical in both modes — same call, same role, same shaping."""
    gw = gateway_config(env={"DAKCODER_MODEL_API_KEY": "sk-real"})
    local = local_config("https://aiops.cept.gov.in/coder/backend", "jwt", env={})
    assert gw.temperature_for("coder") == local.temperature_for("coder")
    assert gw.api_key != local.api_key


def test_a_local_runtime_sends_the_role_and_lets_the_gateway_name_the_model():
    """D-59, on the client side.

    The gateway reads ``model`` as a *role* and refuses anything else — the
    control that stops a developer routing to a model nobody budgeted for. A
    local runtime that resolved the name here would have every call refused with
    "'Qwen3.8-27B' is not a configured role", which is what happened the first
    time the two halves were run against each other.
    """
    local = local_config("https://aiops.cept.gov.in/coder/backend", "jwt", env={})
    assert local.model_for("coder") == "coder"
    assert local.model_for("fast") == "fast"
    with pytest.raises(ValueError, match="unknown model role"):
        local.model_for("planner")

    gw = gateway_config(env={"DAKCODER_MODEL_API_KEY": "sk-real"})
    assert gw.model_for("coder") == "Qwen3.8-27B"


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


# ── metering ────────────────────────────────────────────────────────────────


def test_the_call_carries_what_the_gateway_meters_it_by(endpoint):
    """Defect D-1's other half.

    Without ``X-Estimated-Tokens`` the gateway reserves against a deliberately
    generous fallback and — since the reservation is what the reconcile replaces
    — a run is charged a figure with no relationship to what it spent. Without
    the session, turn and mode the ledger holds rows that cannot answer "what did
    this team spend on migrations last month", which is the question it exists
    for.
    """
    client(endpoint).chat(
        [{"role": "user", "content": "hi"}],
        metering=Metering(session_id="s-1", turn=4, mode="coder", estimated_tokens=9_120),
    )

    sent = endpoint.headers[0]
    assert sent["x-session-id"] == "s-1"
    assert sent["x-turn"] == "4"
    assert sent["x-mode"] == "coder"
    assert sent["x-estimated-tokens"] == "9120"
    assert sent["x-lane"] == "interactive"


def test_metering_omits_what_it_does_not_know_rather_than_sending_zero(endpoint):
    """A zero turn is not turn zero, and an empty session id is not a session.

    The gateway falls back when a header is absent; a header present and wrong
    is one it would believe.
    """
    client(endpoint).chat([{"role": "user", "content": "hi"}], metering=Metering())

    sent = endpoint.headers[0]
    assert "x-turn" not in sent
    assert "x-session-id" not in sent
    assert "x-estimated-tokens" not in sent


def test_a_call_with_no_metering_still_goes(endpoint):
    """The probe and the prewarm have nothing meaningful to attribute."""
    client(endpoint).chat([{"role": "user", "content": "hi"}])
    assert "x-session-id" not in endpoint.headers[0]


def test_the_stream_is_read_to_the_end_rather_than_abandoned_at_done():
    """Defect D-1, at the line that caused it.

    Breaking out of the loop on ``[DONE]`` closes the response while the gateway
    is still inside the block that reconciles quota and writes the ledger row.
    Starlette reads that as a disconnect, cancels the response task, and the
    settlement is lost — so the turn is charged at its estimate for ever and
    never reaches the ledger at all. Reading to the end costs one more iteration,
    because ``[DONE]`` is the last frame.

    Asserted here rather than through the client, because `httpx.MockTransport`
    hands back a whole response and has no socket to leave hanging: the
    behaviour that matters is that the iterator is exhausted.
    """
    frames = [
        'data: {"choices":[{"index":0,"delta":{"content":"ok"}}]}',
        'data: {"usage":{"prompt_tokens":10,"completion_tokens":2}}',
        "data: [DONE]",
    ]
    pulled: list[str] = []

    def lines():
        for frame in frames:
            pulled.append(frame)
            yield frame

    result = _consume_stream(lines())

    assert pulled == frames, "the client hung up before the response ended"
    assert result.content == "ok"
    assert result.usage.prompt_tokens == 10


def test_frames_after_done_belong_to_nobody_and_are_ignored():
    """Draining must not mean *believing* whatever arrives after the end."""
    frames = [
        'data: {"choices":[{"index":0,"delta":{"content":"ok"}}]}',
        "data: [DONE]",
        'data: {"choices":[{"index":0,"delta":{"content":" and more"}}]}',
    ]
    result = _consume_stream(iter(frames))
    assert result.content == "ok"


# -- streaming ---------------------------------------------------------------


def test_content_is_offered_as_it_arrives_rather_than_only_at_the_end(endpoint):
    """The seam the whole streaming path was missing.

    The request has always been made with ``stream: True`` and the gateway has
    always relayed chunk by chunk. This client folded the stream into one value
    and returned at EOF, so nothing downstream could see a turn until it was
    over — while every layer above, right up to the panel's ``assistant_delta``
    renderer, sat built and idle.
    """
    endpoint.content_fragments = ("package ", "handler", " // pension")
    seen = []

    result = client(endpoint).chat([{"role": "user", "content": "hi"}], on_delta=seen.append)

    assert seen == ["package ", "handler", " // pension"]
    assert result.content == "package handler // pension", "streaming disturbed the fold"


def test_a_turn_with_no_sink_still_works(endpoint):
    """The probe, the prewarm and the summariser all call without one."""
    assert client(endpoint).chat([{"role": "user", "content": "hi"}]).content == "ready"


def test_reasoning_is_not_offered_as_content(endpoint):
    """Thinking is off in every mode, so a reasoning feed would be the symptom of
    a fault rather than something to render — and it is already reported as a
    number. A sink that received it would put the model's private deliberation on
    screen as though it were the answer."""
    endpoint.ignore_thinking_off = True
    seen = []

    client(endpoint).chat([{"role": "user", "content": "hi"}], on_delta=seen.append)

    assert seen == ["ready"]
    assert not any("Considering" in fragment for fragment in seen)


def test_a_tool_call_turn_streams_nothing(endpoint):
    """Arguments arrive as JSON split across chunks. Anything rendering them
    would be rendering half-written JSON."""
    seen = []
    result = client(endpoint).chat(
        [{"role": "user", "content": "hi"}],
        tools=[{"type": "function", "function": {"name": "read_file", "parameters": {}}}],
        on_delta=seen.append,
    )

    assert result.tool_calls
    assert seen == []


def test_a_retry_that_had_said_nothing_still_streams(endpoint, no_sleep):
    """The ordinary retry. A 503 or a 429 is raised before a single line is read,
    so nothing has been said and the replacement attempt is free to stream."""
    endpoint.transient_failures = 1
    seen = []

    result = client(endpoint, sleep=no_sleep).chat(
        [{"role": "user", "content": "hi"}], on_delta=seen.append
    )

    assert result.attempts == 2
    assert seen == ["ready"], "an upstream hiccup should not cost the turn its streaming"


def test_a_retry_that_had_already_spoken_stays_silent(no_sleep):
    """The correctness case.

    A failure part-way through a stream is the only way to reach a retry having
    already handed text to the caller, and deltas are append-only: there is no
    way to un-say them. The retry keeps quiet and the authoritative message at
    the end of the turn replaces the partial answer on screen.
    """
    seen = []
    attempts = []

    class Dying(httpx.SyncByteStream):
        """Streams two content frames and then drops, as a read timeout does."""

        def __iter__(self):
            yield b'data: {"choices":[{"index":0,"delta":{"content":"half an "}}]}\n\n'
            yield b'data: {"choices":[{"index":0,"delta":{"content":"answer"}}]}\n\n'
            raise httpx.ReadTimeout("the stream dropped")

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(request)
        if len(attempts) == 1:
            return httpx.Response(
                200, headers={"content-type": "text/event-stream"}, stream=Dying()
            )
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=(
                b'data: {"choices":[{"index":0,"delta":{"content":"the whole answer"}}]}\n\n'
                b'data: {"choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}\n\n'
                b"data: [DONE]\n\n"
            ),
        )

    config = LLMConfig(
        deployment=Deployment.GATEWAY,
        base_url="https://ai.cept.gov.in/v1",
        api_key="sk-test",
    )
    transport = httpx.MockTransport(handler)
    with LLMClient(config, transport=transport, sleep=no_sleep, jitter=lambda: 0.0) as c:
        result = c.chat([{"role": "user", "content": "hi"}], on_delta=seen.append)

    assert len(attempts) == 2
    assert seen == ["half an ", "answer"], f"the retry said it again: {seen}"
    assert result.content == "the whole answer", "and the authoritative text is the whole one"


# ── a cut-off tool call, whatever the finish reason says ────────────────────


def test_a_cut_off_call_is_caught_without_a_length_finish_reason():
    """The case the `truncated` property was written for, arriving unlabelled.

    A server reported `finish_reason: "tool_calls"` on a reply cut off inside a
    call, so the arguments came through as the bare `{` with nothing to say they
    had been interrupted — and the router answered "arguments are not valid
    JSON", which is the one piece of advice the model cannot act on.
    """
    result = ChatResult(
        tool_calls=[ToolCall(id="t1", name="lib_version_check", arguments="{")],
        finish_reason="tool_calls",
    )
    assert [c.name for c in result.incomplete_tool_calls()] == ["lib_version_check"]


def test_a_call_cut_off_inside_a_string_counts_too():
    result = ChatResult(
        tool_calls=[ToolCall(id="t1", name="read_file", arguments='{"path": "handler/whats')],
        finish_reason="tool_calls",
    )
    assert result.incomplete_tool_calls()


def test_genuinely_malformed_arguments_are_left_to_the_router():
    """The router has a message for these, and it is the right one. Claiming
    they were cut off would send the model to make its reply shorter, which
    fixes nothing."""
    for arguments in ("not json at all", "{'path': 'x'}}", "[1, 2]]"):
        result = ChatResult(
            tool_calls=[ToolCall(id="t1", name="read_file", arguments=arguments)],
            finish_reason="tool_calls",
        )
        assert result.incomplete_tool_calls() == [], arguments


def test_complete_arguments_are_never_reported_as_cut_off():
    result = ChatResult(
        tool_calls=[ToolCall(id="t1", name="read_file", arguments='{"path": "a.go"}')],
        finish_reason="length",
    )
    assert result.incomplete_tool_calls() == []
