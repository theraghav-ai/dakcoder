"""Regression tests for the residuals of the 2026-09-02 audit, closed on 3 September.

The re-check after the first round of fixes left a short list open: the
summariser was handed the whole evicted set whatever its size, the Planner's
research fence asked for a move the request forbade, an empty read range was
clamped into a one-line success, a second ``UnsupportedParameterError`` escaped
the loop as a crash, a steer arriving on a gate-stalled turn was appended and
never read, and a debug test printed instead of asserting. One test per item,
driven the same way ``test_regression_audit.py`` drives its rows: the real
modules under a scripted model.
"""

from __future__ import annotations

import json

import pytest

from dakcoder_agent.context import Layer, Message, Role
from dakcoder_agent.loop import (
    _MAX_RECAP_CALLS,
    _RECAP_PROMPT,
    _RESULT_HEAD_IN_TRANSCRIPT,
    _RESULT_TAIL_IN_TRANSCRIPT,
    _TRANSCRIPT_CHARS,
    MAX_RESEARCH_TURNS,
    Intent,
    Outcome,
    _rendered,
)
from dakcoder_agent.modes import Mode
from dakcoder_agent.tools.router import Router
from dakcoder_shared.envelope import EventType
from dakcoder_shared.llm import ChatResult, ToolCall, UnsupportedParameterError, Usage
from scripted import (  # noqa: F401 - fixtures are used by name
    ScriptedClient,
    build,
    calls,
    gated,
    patch,
    plan_call,
    planning_router,
    say,
    written,
)


class Recording(ScriptedClient):
    """The scripted model, remembering every recap prompt it was handed."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.recaps: list[str] = []

    def chat(self, messages, *, response_format=None, **kwargs) -> ChatResult:
        if response_format is not None:
            if response_format.get("json_schema", {}).get("name") == "recap":
                self.recaps.append(messages[-1]["content"])
        return super().chat(messages, response_format=response_format, **kwargs)


def _evicted(count: int, *, result_chars: int = 2_000) -> list[Message]:
    """A working set of ``count`` read turns, as compaction would hand them over."""
    out: list[Message] = []
    for i in range(count):
        path = f"handler/file{i:03d}.go"
        call = ToolCall(id=f"r{i}", name="read_file", arguments=json.dumps({"path": path}))
        out.append(Message(Role.ASSISTANT, "", Layer.WORKING_SET, tool_calls=(call,), turn=i + 1))
        out.append(
            Message(
                Role.TOOL,
                f"{path} (40 lines)\n" + ("x" * 79 + "\n") * (result_chars // 80),
                Layer.WORKING_SET,
                path=path,
                line_range=(1, 40),
                tool_call_id=f"r{i}",
                turn=i + 1,
            )
        )
    return out


# ── the summariser is handed pieces, never the whole eviction ───────────────


def test_a_long_eviction_is_summarised_in_bounded_pieces(planning_router: Router) -> None:
    """Three capped reads used to go to the summariser as one ~577,000-character
    request. Each call now takes at most `_TRANSCRIPT_CHARS`, and the pieces are
    folded into one recap."""
    loop, _ = build(planning_router, [])
    client = Recording([], kind="question")
    loop.client = client

    evicted = _evicted(60)
    recap = loop._summarise(evicted)

    assert len(client.recaps) >= 2, "an eviction this size must be split"
    assert len(client.recaps) <= _MAX_RECAP_CALLS
    for prompt in client.recaps:
        assert len(prompt) - len(_RECAP_PROMPT) <= _TRANSCRIPT_CHARS
    assert "handler/file000.go" in recap.files_read
    assert "handler/file059.go" in recap.files_read
    assert recap.turns == (1, 60)


def test_a_huge_eviction_caps_the_number_of_summariser_calls(planning_router: Router) -> None:
    """A compaction that spent twenty model calls would be its own budget problem.
    Past the cap the oldest pieces are digested deterministically, and the recap
    says so rather than pretending the model saw them."""
    loop, _ = build(planning_router, [])
    client = Recording([], kind="question")
    loop.client = client

    recap = loop._summarise(_evicted(400))

    assert len(client.recaps) == _MAX_RECAP_CALLS
    assert any("not summarised by the model" in d for d in recap.decisions)
    assert any("read_file(handler/file000.go)" in d for d in recap.decisions)


def test_a_result_is_clipped_head_and_tail_for_the_handover() -> None:
    """The head is where the facts are (the path and span, the first errors);
    the tail is where a build log ends. The body is what the compaction is
    throwing away, and re-reading it costs the summariser its window."""
    body = "HEAD-" + "m" * 20_000 + "-TAIL"
    message = Message(Role.TOOL, body, Layer.WORKING_SET, path="handler/x.go", tool_call_id="t")

    rendered = _rendered(message)

    assert rendered.startswith("[tool handler/x.go] HEAD-")
    assert rendered.endswith("-TAIL")
    assert "omitted from the handover" in rendered
    assert len(rendered) < _RESULT_HEAD_IN_TRANSCRIPT + _RESULT_TAIL_IN_TRANSCRIPT + 200


def test_a_failed_piece_is_announced_and_the_rest_is_still_summarised(
    planning_router: Router,
) -> None:
    """Every failure is reported. The broad `except` used to return the fallback
    in silence for anything that was not a programming error, so a summariser
    that could not fit its window degraded every compaction without a word."""
    loop, _ = build(planning_router, [])

    class Flaky(Recording):
        def chat(self, messages, *, response_format=None, **kwargs):
            if response_format is not None and not self.recaps:
                self.recaps.append(messages[-1]["content"])
                raise RuntimeError("the summariser is down")
            return super().chat(messages, response_format=response_format, **kwargs)

    loop.client = Flaky([], kind="question")
    seen: list = []
    loop.on_event = seen.append

    recap = loop._summarise(_evicted(60))

    errors = [e for e in seen if e.type is EventType.ERROR and e.data.get("where") == "summariser"]
    assert errors, "a failed summariser call must be announced"
    assert "summariser call failed" in errors[0].data["message"]
    assert "handler/file000.go" in recap.files_read, "the fact of what was evicted survives"
    assert any("could not be summarised" in item for item in recap.open_items)
    assert len(loop.client.recaps) >= 2, "the pieces after the failure were still tried"


# ── the fence names only what it forces ─────────────────────────────────────


def test_the_planner_fence_asks_only_for_what_it_forces(
    planning_router: Router, gated, written
) -> None:
    """The Planner was told "submit the plan now, or ask the developer" on a
    turn whose `tool_choice` named `submit_plan` alone."""
    reads = [
        calls(("search_repo", json.dumps({"pattern": f"needle{i}"})))
        for i in range(MAX_RESEARCH_TURNS)
    ]
    loop, client = build(planning_router, reads, max_turns=MAX_RESEARCH_TURNS + 3)
    list(loop.run("add a status filter to the user list", intent=Intent.AGENT))

    fence = [
        m.content
        for m in loop.context.build()
        if m.source == "user" and "turns calling tools in this phase" in m.content
    ]
    assert fence, "the research fence never fired"
    assert "submit_plan" in fence[0]
    assert "ask the developer" not in fence[0]
    assert {"type": "function", "function": {"name": "submit_plan"}} in client.tool_choices


# ── an empty range is a refusal, not a one-line success ─────────────────────


def test_an_empty_read_range_is_refused_not_clamped(router: Router) -> None:
    out = router.dispatch(
        "read_file", {"path": "handler/user.go", "start": 3, "end": 1}, mode=Mode.AGENT
    )
    assert not out.ok
    assert "before start" in out.content
    assert out.meta.get("dead_end"), "the same arguments fail the same way every time"


# ── an unsupported parameter ends the run; it does not escape it ────────────


def test_an_unsupported_parameter_ends_the_run_rather_than_escaping(
    planning_router: Router,
) -> None:
    """A refusal raised inside the fallback's `except` used to skip every handler
    below it and leave `_complete` as an exception the runtime dressed up as a
    crash. The run now ends ERROR with the endpoint's message on screen."""
    loop, _ = build(planning_router, [])

    class Refusing:
        def chat(self, *_a, **_k):
            raise UnsupportedParameterError(
                400, "litellm.UnsupportedParamsError: Qwen3.8-27B does not support tool_choice"
            )

    loop.client = Refusing()
    events = list(loop.run("what does the handler do", intent=Intent.ASK))

    assert loop.result is not None
    assert loop.result.outcome == Outcome.ERROR
    assert "does not support" in loop.result.summary
    assert any(e.type is EventType.ERROR for e in events)
    assert events[-1].type is EventType.END


def test_a_named_choice_the_endpoint_refuses_falls_back_and_carries_on(
    planning_router: Router, gated, written
) -> None:
    """The first refusal degrades the choice; the degraded request goes out."""
    loop, _ = build(planning_router, [say("I will plan now."), plan_call()])

    class Picky(ScriptedClient):
        def chat(self, messages, *, tool_choice=None, **kwargs):
            if tool_choice is not None:
                raise UnsupportedParameterError(400, "UnsupportedParamsError: tool_choice")
            return super().chat(messages, tool_choice=tool_choice, **kwargs)

    loop.client = Picky([say("I will plan now."), plan_call()])
    events = list(loop.run("add Routes", intent=Intent.AGENT))

    fallbacks = [e for e in events if e.type is EventType.GATE and e.data.get("kind") == "tool_choice_unsupported"]
    assert fallbacks, "the refusal was not recognised as a fallback"
    assert any(e.type is EventType.PLAN for e in events), "the run did not carry on to the plan"


# ── a steer restarts the gate-stall clock ───────────────────────────────────


def test_a_steer_restarts_the_gate_stall_clock(planning_router: Router, gated, written) -> None:
    """A run standing in front of a failing gate used to end on the very turn the
    developer's correction arrived, with the message appended to a context
    nothing would read again. Without the steer this run ends at the top of
    turn 7; with one delivered before turn 6 it runs on."""
    gated["fail"] = "go_build"
    noise = [
        calls(("search_repo", json.dumps({"pattern": f"needle{i}"}))) for i in range(10)
    ]
    loop, _ = build(planning_router, [plan_call(), patch(), say("Done.")] + noise, max_turns=24)

    pending = ["Try removing the unused import instead of adding one."]

    def steer() -> list[str]:
        # Drained at the top of turn 6, which is inside the stall window.
        if loop.context.turn == 5 and pending:
            return [pending.pop()]
        return []

    loop.steer = steer

    events = list(loop.run("add Routes", intent=Intent.AGENT))

    assert loop.result.outcome == Outcome.UNVERIFIED
    assert any(e.type is EventType.STEER for e in events), "the steer never reached the run"
    assert loop.context.turn > 6, "the run ended on the turn the correction arrived"
    assert any(
        m.source == "user" and "unused import" in m.content for m in loop.context.build()
    )
