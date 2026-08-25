"""The three components composed: context manager, mode table, transport."""

from __future__ import annotations

import pytest

from dakcoder_agent import ContextManager, Mode
from dakcoder_agent.context import OverBudgetError
from dakcoder_agent.llm import complete, make_client, reasoning_leaked
from dakcoder_shared.config import Deployment, LLMConfig, local_config
from dakcoder_shared.llm import LLMClient

SYSTEM = "You are dakcoder. " + ("Follow the template contract. " * 30)


def client_for(endpoint) -> LLMClient:
    return LLMClient(
        LLMConfig(Deployment.GATEWAY, "https://ai.cept.gov.in/v1", "sk-test", user="gitlab:7"),
        transport=endpoint.transport(),
        sleep=lambda _s: None,
    )


def context(mode: Mode = Mode.CODER) -> ContextManager:
    cm = ContextManager(mode=mode, system_prompt=SYSTEM, tool_schema_tokens=1200)
    cm.set_task("Add a Pension resource", acceptance=["go build ./... clean"])
    cm.begin_turn()
    return cm


def test_a_turn_dispatches_with_the_modes_own_settings(endpoint):
    cm = context(Mode.PLANNER)
    with client_for(endpoint) as c:
        result = complete(cm, c)

    sent = endpoint.requests[0]
    assert sent["max_tokens"] == 4096            # planner's output budget
    assert sent["chat_template_kwargs"] == {"enable_thinking": False}
    assert sent["temperature"] == 0.1
    assert result.mode is Mode.PLANNER
    assert result.chat.content == "ready"


def test_the_assembled_messages_are_what_gets_sent(endpoint):
    cm = context()
    cm.append_assistant("looking at the handler")
    cm.append_tool_result("rules_lint", "OK — 0 violations")

    with client_for(endpoint) as c:
        complete(cm, c)

    sent = endpoint.requests[0]["messages"]
    assert [m["role"] for m in sent] == ["system", "user", "assistant", "tool"]
    assert sent[0]["content"] == SYSTEM
    # Bookkeeping fields stay ours; the wire shape is the API's.
    assert set(sent[0]) <= {"role", "content", "tool_call_id"}


def test_usage_is_reconciled_into_the_estimator(endpoint):
    """The reason include_usage is sent on every call. Without this the estimate
    stays a guess for the life of the process."""
    cm = context()
    with client_for(endpoint) as c:
        result = complete(cm, c)

    assert result.actual_prompt_tokens == 120
    assert result.estimated_prompt_tokens > 0
    assert cm.inspect()["calibrated"] is True
    assert result.estimate_error > 0


def test_an_over_budget_prompt_is_refused_before_it_is_sent(endpoint):
    """A prompt over budget is a bug in the caller — the context manager exists
    to prevent it. Letting it through means finding out via a 400 from someone
    else's proxy with the turn already spent."""
    cm = context()
    while cm.usage().total <= cm.budget:
        cm.append_tool_result("read_file", "x " * 4000, path=f"f{cm.usage().total}.go")

    with client_for(endpoint) as c, pytest.raises(OverBudgetError, match="budget"):
        complete(cm, c)

    assert endpoint.attempts == 0, "nothing should have been sent"


def test_the_budget_check_can_be_waived_for_the_overflow_recovery_path(endpoint):
    cm = context()
    while cm.usage().total <= cm.budget:
        cm.append_tool_result("read_file", "x " * 4000, path=f"f{cm.usage().total}.go")

    with client_for(endpoint) as c:
        assert complete(cm, c, enforce_budget=False).chat.content == "ready"


def test_tool_calls_round_trip_through_the_context(endpoint):
    cm = context()
    tools = [{"type": "function", "function": {"name": "rules_lint", "parameters": {}}}]

    with client_for(endpoint) as c:
        result = complete(cm, c, tools=tools)

    assert result.chat.tool_calls
    call = result.chat.tool_calls[0]
    assert call.parsed() == {"paths": ["handler/user.go"]}

    cm.append_tool_result("rules_lint", "OK — 0 violations", tool_call_id=call.id)
    assert cm.build()[-1].tool_call_id == call.id


def test_reasoning_leaking_into_a_thinking_off_mode_is_detectable(endpoint):
    """§18's alert condition.

    Non-zero reasoning in a thinking-off mode means chat_template_kwargs is not
    taking effect — roughly 15x the latency for no quality gain, and unlike an
    outright failure it presents as the agent simply being slow.
    """
    endpoint.ignore_thinking_off = True
    cm = context()
    with client_for(endpoint) as c:
        result = complete(cm, c)

    assert result.chat.usage.reasoning_tokens > 0
    assert reasoning_leaked(result), "a thinking-off mode was charged reasoning tokens"


def test_a_conforming_endpoint_leaks_no_reasoning(endpoint):
    cm = context()
    with client_for(endpoint) as c:
        assert not reasoning_leaked(complete(cm, c))


def test_a_local_client_must_point_at_the_gateway_proxy():
    """Pointing it at the model directly would need a model credential — exactly
    the unmetered bypass §15.4 exists to prevent."""
    bad = LLMConfig(Deployment.LOCAL, "https://ai.cept.gov.in/v1", "jwt")
    with pytest.raises(ValueError, match="unmetered bypass"):
        make_client(bad)

    good = local_config("https://aiops.cept.gov.in/coder/backend", "jwt", env={})
    with make_client(good) as c:
        assert c.config.base_url.endswith("/v1/llm")


def test_the_prefix_survives_a_full_turn(endpoint):
    """The property everything in §6.4 is for, asserted end to end rather than
    only in the context manager's own unit tests."""
    cm = context()
    signature = cm.prefix_signature()

    with client_for(endpoint) as c:
        for turn in range(5):
            cm.begin_turn()
            cm.append_assistant(f"step {turn}")
            complete(cm, c)

    assert cm.prefix_signature() == signature
    for sent in endpoint.requests:
        assert sent["messages"][0]["content"] == SYSTEM
