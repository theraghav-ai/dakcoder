"""Recovering instead of ending the run.

Two paths that existed as failures with the machinery to fix them sitting
unused: an endpoint refusing a request as too large (ended the run ERROR next to
a compactor), and "why is this run not done" asked in three places with three
answers. Plus the deterministic compaction tier, which was a private fallback
inside the summariser and is now a strategy anyone can select.
"""

from __future__ import annotations

from pathlib import Path

from dakcoder_shared.envelope import EventType
from dakcoder_shared.llm import ChatResult, Usage
from dakcoder_shared.paths import Workspace

from dakcoder_agent.context import ContextManager
from dakcoder_agent.loop import MAX_FINISH_REFUSALS, AgentLoop, _State, _context_length_error
from dakcoder_agent.modes import Mode
from dakcoder_agent.tools.router import Router


# ── classifying the one recoverable 400 ─────────────────────────────────────


def test_the_endpoints_own_phrasings_are_recognised() -> None:
    """Matched on the message, because the status is 400 for a dozen unrelated
    things and this is the one that is recoverable."""
    for message in (
        "This model's maximum context length is 262144 tokens, however you requested 270000",
        "Error code: 400 - {'error': {'message': 'context_length_exceeded'}}",
        "the prompt is too long: 300000 tokens > 262144 maximum",
        "Please reduce the length of the messages.",
        "input is too long for requested model",
        "requested tokens exceeds the maximum allowed",
        "too many tokens in the request",
    ):
        assert _context_length_error(RuntimeError(message)), message


def test_ordinary_failures_are_not_mistaken_for_it() -> None:
    """A false positive costs one deterministic compaction and a retry; getting
    this wrong the other way would retry every transport error."""
    for message in (
        "Connection reset by peer",
        "401 Unauthorized",
        "rate limit exceeded, retry after 30s",
        "tool_choice is not supported by this model",
        "invalid JSON in arguments",
        "",
    ):
        assert not _context_length_error(RuntimeError(message)), message


# ── the recovery itself ─────────────────────────────────────────────────────


class _OverflowingClient:
    """Refuses the first N requests the way the endpoint refuses an over-long one."""

    def __init__(self, refusals: int = 1) -> None:
        self.refusals = refusals
        self.calls = 0

    def chat(self, messages, *, tools=None, **kwargs) -> ChatResult:
        self.calls += 1
        if self.calls <= self.refusals:
            raise RuntimeError(
                "This model's maximum context length is 262144 tokens, "
                "however you requested 301000 tokens"
            )
        return ChatResult(content="recovered", finish_reason="stop",
                          usage=Usage(prompt_tokens=100))


def _loop(client) -> AgentLoop:
    context = ContextManager(mode=Mode.AGENT, system_prompt="s")
    context.set_task("migrate the handler")
    loop = AgentLoop(context, client, Router(Workspace(Path.cwd())))
    for turn in range(14):
        context.begin_turn()
        context.append_tool_result(
            "read_file",
            f"f{turn}.go\n" + "\n".join(f"line {i}" for i in range(3_000)),
            tool_call_id=f"c{turn}",
            path=f"f{turn}.go",
            line_range=(1, 3_000),
        )
    return loop


def test_an_over_long_request_is_recovered_rather_than_ending_the_run() -> None:
    """It used to end the run ERROR with the compactor three lines above."""
    client = _OverflowingClient(refusals=1)
    loop = _loop(client)

    events = list(loop._complete([]))

    kinds = [e.data.get("kind") for e in events if e.type == EventType.GATE]
    assert "overflow_recovery" in kinds, "the 400 was not classified as recoverable"
    assert client.calls == 2, "the request was not retried"
    assert loop.result is None, "the run was ended anyway"


def test_the_recovery_compacts_deterministically() -> None:
    """A run just refused for sending too much must not answer by sending a
    summarisation request built from the same context."""
    client = _OverflowingClient(refusals=1)
    loop = _loop(client)

    list(loop._complete([]))

    assert loop.context.compaction is not None
    assert loop.context.compaction.strategy == "basic"
    # The summariser was never called: every request was the completion itself.
    assert client.calls == 2


def test_the_recovery_happens_at_most_once_per_run() -> None:
    """A second overflow after compacting to 15% is not an estimate that
    drifted -- it is a context that cannot be made to fit, and retrying it is a
    bill with no upside."""
    client = _OverflowingClient(refusals=99)
    loop = _loop(client)

    list(loop._complete([]))

    assert loop.result is not None
    assert client.calls == 2, "it kept retrying a request that could not fit"

    client.calls = 0
    loop.result = None
    list(loop._complete([]))
    assert client.calls == 1, "it recovered twice in one run"


def test_an_ordinary_failure_still_ends_the_run() -> None:
    class _Broken:
        def chat(self, *_a, **_k):
            raise ConnectionError("the gateway is down")

    loop = _loop(_Broken())
    events = list(loop._complete([]))

    assert loop.result is not None
    assert "gateway is down" in loop.result.summary
    assert not [e for e in events if e.data.get("kind") == "overflow_recovery"]


# ── the completion guard ────────────────────────────────────────────────────


def _guard_loop() -> AgentLoop:
    loop = AgentLoop.__new__(AgentLoop)
    loop.router = Router(Workspace(Path.cwd()))
    loop.state = _State()
    return loop


def test_the_guard_is_silent_once_it_has_had_its_say() -> None:
    loop = _guard_loop()
    loop.state.finish_refused = MAX_FINISH_REFUSALS

    assert loop._why_not_done() == "", "the model was pushed back on twice over one move"


def test_the_guard_names_the_unwritten_files(monkeypatch) -> None:
    loop = _guard_loop()
    monkeypatch.setattr(
        AgentLoop, "_open_targets", lambda _s: ["handler/pension.go", "repo/pension.go"]
    )

    reason = loop._why_not_done()

    assert "handler/pension.go" in reason and "repo/pension.go" in reason
    assert "none of them have been written" in reason


def test_the_guard_falls_through_to_the_gate(monkeypatch) -> None:
    """A plan with unwritten targets and a failing gate is one situation, and
    answering it twice reads as two objections to the same move."""
    loop = _guard_loop()
    monkeypatch.setattr(AgentLoop, "_open_targets", lambda _s: [])
    monkeypatch.setattr(AgentLoop, "_gate_wants_an_edit", lambda _s: True)

    assert "verification gate is failing" in loop._why_not_done()


def test_a_run_with_nothing_outstanding_is_done(monkeypatch) -> None:
    loop = _guard_loop()
    monkeypatch.setattr(AgentLoop, "_open_targets", lambda _s: [])
    monkeypatch.setattr(AgentLoop, "_gate_wants_an_edit", lambda _s: False)

    assert loop._why_not_done() == ""
