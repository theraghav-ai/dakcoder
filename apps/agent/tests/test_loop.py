"""Tests for the agent loop.

Driven by a scripted model rather than a fake endpoint. The SSE transport is
already covered in ``test_llm.py``; what matters here is the loop's own
decisions — when it switches mode, when it gives up, and what it refuses to skip.
Putting a real stream underneath these would test the stream three hundred times
and the decisions once.
"""

from __future__ import annotations

from collections.abc import Sequence

import pytest

from dakcoder_agent.context import ContextManager
from dakcoder_agent.gate import GATE
from dakcoder_agent.loop import AgentLoop, Outcome
from dakcoder_agent.modes import Mode
from dakcoder_agent.tools.router import ApprovalRequest, Router
from dakcoder_shared.envelope import EventType, ToolResult
from dakcoder_shared.llm import ChatResult, ToolCall, Usage


class ScriptedClient:
    """Returns preset turns, and records the tool schemas it was offered."""

    def __init__(self, turns: Sequence[ChatResult]) -> None:
        self.turns = list(turns)
        self.seen_tools: list[list[str]] = []
        self.calls = 0

    def chat(self, messages, *, tools=None, **kwargs) -> ChatResult:
        self.calls += 1
        self.seen_tools.append([t["function"]["name"] for t in (tools or [])])
        if not self.turns:
            return say("nothing further")
        return self.turns.pop(0)


def say(text: str) -> ChatResult:
    return ChatResult(content=text, finish_reason="stop", usage=Usage(prompt_tokens=100))


def calls(*specs: tuple[str, str]) -> ChatResult:
    return ChatResult(
        tool_calls=[
            ToolCall(id=f"chatcmpl-tool-{i:02x}", name=name, arguments=args)
            for i, (name, args) in enumerate(specs)
        ],
        finish_reason="tool_calls",
        usage=Usage(prompt_tokens=100),
    )


@pytest.fixture
def gated(router: Router):
    """Scripted gate stages, so a run's outcome is set by the test not the toolchain."""
    state = {"fail": None}

    def make(name):
        def handler(_inv, _name=name):
            if state["fail"] == _name:
                return ToolResult.failure(f"{_name}: boom")
            return ToolResult.success(f"{_name}: clean")

        return handler

    for stage in GATE:
        router.handlers[stage.tool] = make(stage.tool)
    for name in ("gofmt", "rules_lint", "go_diagnostics"):
        router.handlers[name] = make(name)
    return state


def loop_over(router: Router, turns, *, mode=Mode.PLANNER, **kw) -> tuple[AgentLoop, list]:
    context = ContextManager(mode=mode, system_prompt="You are dakcoder.")
    agent = AgentLoop(context, ScriptedClient(turns), router, **kw)
    events = list(agent.run("Add a Pension resource", acceptance=["go build clean"], start=mode))
    return agent, events


def kinds(events) -> list[str]:
    return [str(e.type) for e in events]


def of_type(events, wanted: EventType):
    return [e for e in events if e.type is wanted]


# ── the shape of a run ──────────────────────────────────────────────────────


def test_a_plan_then_a_clean_gate_finishes(router: Router, gated) -> None:
    agent, events = loop_over(
        router,
        [say("1. Edit handler/user.go\n   Accepts: builds"), say("done")],
    )
    assert agent.result.outcome == Outcome.DONE
    assert "finish" in kinds(events)
    assert kinds(events)[-1] == "end"


def test_the_planner_hands_off_to_the_coder(router: Router, gated) -> None:
    agent, events = loop_over(router, [say("1. Edit handler/user.go"), say("edited")])
    plan = of_type(events, EventType.PLAN)
    assert plan and plan[0].data["steps"] == 1
    assert agent.state.mode is not Mode.PLANNER


def test_a_plan_naming_the_scaffolder_routes_there(router: Router, gated) -> None:
    """Keyed off the tool the plan names rather than off prose. A plan that says
    "create a resource" in English but never mentions the scaffolder is a plan to
    write seven files by hand."""
    context = ContextManager(mode=Mode.PLANNER, system_prompt="s")
    client = ScriptedClient([say("1. Call resource_scaffold with the Pension spec"), say("done")])
    agent = AgentLoop(context, client, router)
    list(agent.run("t"))

    assert "resource_scaffold" in client.seen_tools[1], (
        "the second turn should have been offered the scaffolder's tools"
    )
    assert "write_file" in client.seen_tools[1]
    assert "patch_file" not in client.seen_tools[1], "the Scaffolder does not patch"


def test_a_plan_that_only_describes_a_resource_in_prose_goes_to_the_coder(
    router: Router, gated
) -> None:
    """The counterpart. Routing prose to the Scaffolder would hand it a mode
    whose only tools are the ones the plan did not ask for."""
    context = ContextManager(mode=Mode.PLANNER, system_prompt="s")
    client = ScriptedClient([say("1. Create a new Pension resource by hand"), say("done")])
    agent = AgentLoop(context, client, router)
    list(agent.run("t"))
    assert "patch_file" in client.seen_tools[1]


def test_each_turn_is_offered_only_its_modes_tools(router: Router, gated) -> None:
    context = ContextManager(mode=Mode.PLANNER, system_prompt="s")
    client = ScriptedClient([say("1. Edit handler/user.go"), say("done")])
    agent = AgentLoop(context, client, router)
    list(agent.run("t"))

    planner_tools = set(client.seen_tools[0])
    assert "write_file" not in planner_tools
    assert "read_file" in planner_tools
    assert "write_file" in set(client.seen_tools[1])


# ── the gate is not optional ────────────────────────────────────────────────


def test_the_gate_runs_even_when_the_model_says_it_is_done(
    router: Router, gated
) -> None:
    """The failure this whole design exists to prevent. The model asserting
    success is not evidence of success, and the only way to not rely on it is to
    not ask."""
    ran: list[str] = []
    original = router.run_gate_tool
    router.run_gate_tool = lambda name, args=None: (ran.append(name), original(name, args))[1]

    loop_over(router, [say("1. Edit handler/user.go"), say("All done, everything works!")])
    assert "go_build" in ran


def test_a_failing_gate_does_not_finish_the_run(router: Router, gated) -> None:
    gated["fail"] = "go_build"
    agent, events = loop_over(
        router,
        [say("1. Edit handler/user.go"), say("done"), say("the build is broken"), say("fixed"),
         say("still broken"), say("fixed again"), say("no"), say("x"), say("y"), say("z")],
    )
    assert agent.result.outcome == Outcome.UNVERIFIED
    assert any(e.data.get("kind") == "full" for e in of_type(events, EventType.GATE))


def test_the_gate_result_reaches_the_model(router: Router, gated) -> None:
    """Reported *and* appended. A gate failure the model cannot see is a gate
    failure it cannot fix."""
    gated["fail"] = "go_build"
    agent, _ = loop_over(router, [say("1. Edit handler/user.go"), say("done"), say("I see")])
    transcript = "\n".join(m.content for m in agent.context.build())
    assert "gate: FAIL" in transcript


def test_failure_escalates_coder_twice_then_the_debugger(router: Router, gated) -> None:
    """A third identical Coder attempt on a twice-failed gate is not more likely
    to work — same understanding, same evidence. The Debugger has a playbook,
    which is a different understanding rather than another try."""
    gated["fail"] = "go_vet"
    seen: list[str] = []

    context = ContextManager(mode=Mode.PLANNER, system_prompt="s")
    client = ScriptedClient([say("1. Edit handler/user.go")] + [say(f"turn {i}") for i in range(20)])
    agent = AgentLoop(context, client, router)
    for _ in agent.run("t"):
        seen.append(str(agent.state.mode))

    assert "coder" in seen and "verifier" in seen and "debugger" in seen
    assert agent.result.outcome == Outcome.UNVERIFIED


# ── stop conditions ─────────────────────────────────────────────────────────


def test_the_same_call_three_turns_running_stops_the_run(router: Router, gated) -> None:
    """The loop can see this when the model cannot. Without it a stuck run burns
    the whole token budget arriving at the same place."""
    repeat = calls(("read_file", '{"path": "handler/user.go"}'))
    agent, _ = loop_over(router, [say("1. Read it"), repeat, repeat, repeat, repeat])
    assert agent.result.outcome == Outcome.NO_PROGRESS
    assert "read_file" in agent.result.summary


def test_two_different_calls_are_not_no_progress(router: Router, gated) -> None:
    agent, _ = loop_over(
        router,
        [
            say("1. Read"),
            calls(("read_file", '{"path": "handler/user.go"}')),
            calls(("read_file", '{"path": "go.mod"}')),
            calls(("read_file", '{"path": "handler/user.go"}')),
            say("done"),
        ],
    )
    assert agent.result.outcome == Outcome.DONE


def test_the_turn_budget_is_enforced(router: Router, gated) -> None:
    gated["fail"] = "go_build"
    context = ContextManager(mode=Mode.CODER, system_prompt="s")
    agent = AgentLoop(context, ScriptedClient([]), router, max_turns=3)
    list(agent.run("t", start=Mode.CODER))
    assert agent.context.turn == 3
    assert agent.result.outcome == Outcome.EXHAUSTED


def test_a_transport_failure_ends_the_run_with_an_error_event(router: Router) -> None:
    class Broken:
        def chat(self, *_a, **_k):
            raise ConnectionError("the gateway is down")

    context = ContextManager(mode=Mode.CODER, system_prompt="s")
    agent = AgentLoop(context, Broken(), router)
    events = list(agent.run("t", start=Mode.CODER))

    assert agent.result.outcome == Outcome.ERROR
    assert of_type(events, EventType.ERROR)
    assert kinds(events)[-1] == "end"


# ── tools and approval ──────────────────────────────────────────────────────


def test_a_tool_call_emits_call_then_result(router: Router, gated) -> None:
    _, events = loop_over(
        router,
        [say("1. Read"), calls(("read_file", '{"path": "handler/user.go"}')), say("done")],
    )
    sequence = kinds(events)
    assert sequence.index("tool_call") < sequence.index("tool_result")


def test_an_unapproved_call_is_refused_with_something_to_do_next(
    router: Router, gated
) -> None:
    """The default approver denies. The model is told why and what else it can
    try, rather than being left to repeat the call."""
    asked: list[ApprovalRequest] = []

    def refuse(request: ApprovalRequest) -> bool:
        asked.append(request)
        return False

    _, events = loop_over(
        router,
        [
            say("1. Edit go.mod"),
            calls(("patch_file", '{"path": "go.mod", "old": "module pisapi", "new": "module x"}')),
            say("understood"),
        ],
        approve=refuse,
    )

    assert asked and "go.mod" in asked[0].reason
    assert of_type(events, EventType.TOOL_PENDING)
    result = of_type(events, EventType.TOOL_RESULT)[0]
    assert result.data["ok"] is False
    assert "not approved" in result.data["content"]


def test_an_approved_call_goes_through(router: Router, gated, workspace) -> None:
    _, events = loop_over(
        router,
        [
            say("1. Edit go.mod"),
            calls(("patch_file", '{"path": "go.mod", "old": "module pisapi", "new": "module x"}')),
            say("done"),
        ],
        approve=lambda _r: True,
    )
    assert of_type(events, EventType.TOOL_RESULT)[0].data["ok"] is True
    assert "module x" in (workspace.root / "go.mod").read_text()


def test_an_edit_triggers_the_inner_loop(router: Router, gated) -> None:
    _, events = loop_over(
        router,
        [
            say("1. Write"),
            calls(("write_file", '{"path": "handler/pension.go", "content": "package handler"}')),
            say("done"),
        ],
    )
    inner = [e for e in of_type(events, EventType.GATE) if e.data.get("kind") == "inner"]
    assert inner


def test_a_read_only_turn_does_not(router: Router, gated) -> None:
    """The inner loop exists to check edits. Running it after a read would put a
    lint report into context that describes nothing that changed."""
    _, events = loop_over(
        router,
        [say("1. Read"), calls(("read_file", '{"path": "handler/user.go"}')), say("done")],
    )
    assert not [e for e in of_type(events, EventType.GATE) if e.data.get("kind") == "inner"]


def test_malformed_tool_arguments_do_not_kill_the_run(router: Router, gated) -> None:
    """The router rejects them with a message the model can act on. Raising here
    would end the run over exactly the thing it should recover from."""
    agent, events = loop_over(
        router, [say("1. Read"), calls(("read_file", "{not json")), say("sorry")]
    )
    assert agent.result.outcome != Outcome.ERROR
    assert of_type(events, EventType.TOOL_RESULT)[0].data["ok"] is False


# ── contract C2 ─────────────────────────────────────────────────────────────


def test_every_event_type_emitted_is_in_the_contract(router: Router, gated) -> None:
    _, events = loop_over(
        router,
        [say("1. Write"), calls(("write_file", '{"path": "a/b.go", "content": "package b"}')),
         say("done")],
    )
    for event in events:
        assert event.type in set(EventType)


def test_a_run_always_ends_with_finish_then_end(router: Router, gated) -> None:
    """The extension closes the stream on `end`. A run that stops without it
    leaves a spinner turning forever."""
    for turns in ([say("1. x"), say("done")], [], [calls(("nope", "{}"))]):
        _, events = loop_over(router, turns)
        assert kinds(events)[-2:] == ["finish", "end"]


def test_usage_is_reported_every_turn(router: Router, gated) -> None:
    """Contract C5: the server owns the budget and emits a usage event per turn.
    The extension only displays it."""
    _, events = loop_over(router, [say("1. x"), say("done")])
    usage = of_type(events, EventType.USAGE)
    assert len(usage) == 2
    assert "budget_used_pct" in usage[0].data
