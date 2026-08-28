"""Tests for the agent loop.

Driven by a scripted model rather than a fake endpoint. The SSE transport is
already covered in ``test_llm.py``; what matters here is the loop's own
decisions — when it switches mode, when it gives up, and what it refuses to skip.
Putting a real stream underneath these would test the stream three hundred times
and the decisions once.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
import re

import pytest

from dakcoder_agent.context import ContextManager, Layer, Recap
from dakcoder_agent.gate import GATE
from dakcoder_agent.loop import COMPACTION_WINDOW, AgentLoop, Outcome
from dakcoder_agent.modes import Mode
from dakcoder_agent.tools.router import ApprovalRequest, Router
from dakcoder_shared.envelope import EventType, ToolResult
from dakcoder_shared.llm import ChatResult, ToolCall, Usage
from dakcoder_shared.paths import Workspace


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
            # Numbered, so the filler stands for "the model said something" and
            # not "it said the same thing twice". An identical filler trips the
            # repeated-prose detector, which would make a script that simply ran
            # out look like a model stuck in a loop.
            return say(f"nothing further ({self.calls})")
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


def _edit(n: int) -> ChatResult:
    """One real edit, so the run looks like a model working rather than talking.

    Escalation is only worth reaching when each attempt actually changes
    something; a Coder that answers a failing gate with prose three times is
    caught by ``_narrating`` now, and that is a better outcome than five turns
    of ladder. So the tests that mean to exercise the ladder have to edit.
    """
    return calls(("write_file", f'{{"path": "handler/user{n}.go", "content": "package handler"}}'))


def test_a_failing_gate_does_not_finish_the_run(router: Router, gated) -> None:
    gated["fail"] = "go_build"
    agent, events = loop_over(
        router,
        [say("1. Edit handler/user.go"), _edit(1), say("done"), say("the build is broken"),
         _edit(2), say("fixed"), _edit(3), say("still broken"), _edit(4), say("fixed again"),
         _edit(5), say("no"), _edit(6), say("x"), _edit(7), say("y"), _edit(8), say("z")],
        approve=lambda _r: True,
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
    # Each attempt edits. A ladder climbed on prose alone is narration, and
    # `_narrating` ends it before the third rung — deliberately.
    turns = [say("1. Edit handler/user.go")]
    for i in range(10):
        turns += [_edit(i), say(f"turn {i}")]
    client = ScriptedClient(turns)
    agent = AgentLoop(context, client, router, approve=lambda _r: True)
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


# ── triage: not every message is a task ─────────────────────────────────────


def test_a_greeting_ends_the_run_without_a_plan(router: Router, gated) -> None:
    """The seventeen-turn report, as a test.

    A greeting reaches the Planner, which answers it. That answer carries no
    numbered steps, so there is nothing for the Coder to execute — and handing
    it on anyway is what ran the whole engineering ladder against a workspace
    nothing had touched.
    """
    agent, events = loop_over(router, [say("Hello! How can I help?")])

    assert agent.result.outcome == Outcome.DONE
    assert agent.context.turn == 1
    assert of_type(events, EventType.GATE) == []
    assert of_type(events, EventType.PLAN) == []


def test_a_numbered_plan_still_reaches_the_coder(router: Router, gated) -> None:
    """The other side of the same branch, so triage cannot quietly widen."""
    agent, events = loop_over(router, [say("1. Edit handler/user.go"), say("done")])

    assert of_type(events, EventType.PLAN) != []
    assert any(e.data.get("kind") == "full" for e in of_type(events, EventType.GATE))
    assert agent.result.outcome == Outcome.DONE


def test_repeated_prose_stops_the_run(router: Router, gated) -> None:
    """`_stuck` reads tool arguments, so a turn that calls nothing is invisible
    to it. Every rung of the reported ladder was such a turn."""
    gated["fail"] = "go_build"
    agent, _ = loop_over(
        router,
        [say("1. Edit handler/user.go")] + [say("I cannot proceed.")] * 4,
    )
    assert agent.result.outcome == Outcome.NO_PROGRESS


def test_the_same_prose_before_different_calls_is_not_no_progress(
    router: Router, gated
) -> None:
    """The reason the check sits after the tool-call branch and not before it.

    A model that prefixes each edit with one stock sentence is working, not
    stuck, and killing it mid-run would lose the third write.
    """
    agent, events = loop_over(
        router,
        [
            say("1. Edit three files"),
            calls(("write_file", '{"path": "a.go", "content": "package a"}')),
            calls(("write_file", '{"path": "b.go", "content": "package b"}')),
            calls(("write_file", '{"path": "c.go", "content": "package c"}')),
            say("done"),
        ],
        approve=lambda _r: True,
    )
    assert agent.result.outcome is not Outcome.NO_PROGRESS
    assert {"a.go", "b.go", "c.go"} <= set(router.touched)


# ── a gate that did not run is not a gate that passed ───────────────────────


def test_a_skipped_gate_is_not_reported_as_clean(tmp_path, sidecar) -> None:
    """A checkout root has no ``go.mod``, so every Go stage sits out. Reporting
    that as "the gate is clean" would ship unverified code quietly, which is
    worse than the loud wall it replaces."""
    from dakcoder_agent.tools import commands, fs, gotools, knowledge

    (tmp_path / "notes.md").write_text("# notes\n", encoding="utf-8")
    router = Router(
        Workspace.at(tmp_path),
        {**fs.HANDLERS, **knowledge.HANDLERS, **commands.HANDLERS, **gotools.handlers_for(sidecar)},
    )
    agent, _ = loop_over(
        router,
        [
            say("1. Write handler/user.go"),
            calls(("write_file", '{"path": "handler/user.go", "content": "package handler"}')),
            say("done"),
        ],
        approve=lambda _r: True,
    )

    assert agent.result.outcome == Outcome.DONE
    assert "did not run" in agent.result.summary
    assert "the gate is clean" not in agent.result.summary


# ── streaming ───────────────────────────────────────────────────────────────


class StreamingClient(ScriptedClient):
    """A scripted model that also emits its answer a fragment at a time."""

    def chat(self, messages, *, tools=None, on_delta=None, **kwargs) -> ChatResult:
        result = super().chat(messages, tools=tools, **kwargs)
        if on_delta is not None and result.content:
            for word in result.content.split(" "):
                on_delta(word + " ")
        return result


def streaming_loop(router: Router, turns, **kw):
    """A loop whose transient events are collected rather than dropped."""
    relayed = []
    context = ContextManager(mode=Mode.PLANNER, system_prompt="You are dakcoder.")
    agent = AgentLoop(
        context, StreamingClient(turns), router, on_event=relayed.append, **kw
    )
    events = list(agent.run("Add a Pension resource", start=Mode.PLANNER))
    return agent, events, relayed


def test_the_answer_arrives_while_it_is_being_written(router: Router, gated) -> None:
    """The point of the whole path.

    The loop is a generator and its events reach a client by being yielded, which
    works for everything that happens *between* calls and not at all for
    something that happens *during* one. Streamed text takes the other road the
    runtime already had: the same relay the caller uses for yielded events.
    """
    _agent, events, relayed = streaming_loop(router, [say("1. Edit handler/user.go")])

    deltas = [e for e in relayed if e.type is EventType.ASSISTANT_DELTA]
    assert deltas, "nothing streamed"
    # Everything the model said, and nothing it did not. Compared as words
    # because the fragmenting is the scripted client's, not the loop's.
    streamed = "".join(e.data["text"] for e in deltas)
    said = " ".join(e.data["text"] for e in of_type(events, EventType.ASSISTANT))
    assert streamed.split() == said.split()


def test_the_stream_is_coalesced_rather_than_one_frame_per_token(
    router: Router, gated
) -> None:
    """Fix S11. One frame per token is an SSE frame, an IPC message and a repaint
    each, and the frontend agent shipped that and fell measurably behind."""
    sentence = " ".join(f"word{i}" for i in range(60))
    _agent, _events, relayed = streaming_loop(router, [say(sentence)])

    deltas = [e for e in relayed if e.type is EventType.ASSISTANT_DELTA]
    assert 0 < len(deltas) < 60, f"{len(deltas)} frames for 60 words is not coalescing"


def test_the_tail_is_flushed(router: Router, gated) -> None:
    """The classic bug in code shaped like this: everything works except that the
    last sentence never arrives."""
    _agent, _events, relayed = streaming_loop(router, [say("short")])

    deltas = [e for e in relayed if e.type is EventType.ASSISTANT_DELTA]
    assert "".join(e.data["text"] for e in deltas).strip() == "short"


def test_the_authoritative_message_still_arrives_by_yield(router: Router, gated) -> None:
    """Deltas are a view of a turn. The transcript is the `assistant` event, and
    a client that built one from deltas would have no transcript at all after a
    reconnect — they are never stored."""
    _agent, events, relayed = streaming_loop(router, [say("1. Edit handler/user.go")])

    said = of_type(events, EventType.ASSISTANT)
    assert said[0].data["text"] == "1. Edit handler/user.go"
    assert not any(e.type is EventType.ASSISTANT_DELTA for e in events), (
        "a transient event must not travel by yield: it would interleave with "
        "the stored ones and the log would stop being the run"
    )


def test_a_sink_that_breaks_does_not_take_the_run_with_it(router: Router, gated) -> None:
    """Streaming is a view of a turn, never the turn itself. A panel that was
    torn down mid-answer, or an event loop that has closed, must cost the
    developer their live view and nothing else."""
    context = ContextManager(mode=Mode.PLANNER, system_prompt="You are dakcoder.")

    def explode(_event):
        raise RuntimeError("the event loop is closed")

    agent = AgentLoop(
        context,
        StreamingClient([say("1. Edit handler/user.go")]),
        router,
        on_event=explode,
    )
    events = list(agent.run("Add a Pension resource", start=Mode.PLANNER))

    assert agent.result is not None
    assert agent.result.outcome != Outcome.ERROR
    assert of_type(events, EventType.ASSISTANT)[0].data["text"] == "1. Edit handler/user.go"


def test_a_loop_with_no_sink_runs_exactly_as_before(router: Router, gated) -> None:
    """The default is a no-op, so the CLI and every existing test drive the loop
    without knowing streaming exists."""
    _agent, events = loop_over(router, [say("1. Edit handler/user.go")])
    assert of_type(events, EventType.ASSISTANT)


# ── a numbered answer is not a plan ─────────────────────────────────────────


SUMMARY = (
    "So far in this conversation:\n"
    "1. You said hi and I introduced myself.\n"
    "2. You asked how I was and I declined the small talk.\n"
    "3. You asked what this is and I explained the contract.\n"
    "4. Now you are asking what we have discussed."
)


def test_a_coder_that_restates_its_plan_ends_the_run(router: Router, gated) -> None:
    """The reported fault, reduced.

    `_count_steps` is a regex over prose and cannot tell a plan from a numbered
    answer: a reply that happened to enumerate what had been said was read as
    four steps, handed to the Coder, and restated by it. The developer saw the
    same paragraph three times — as the answer, as the plan card, and again from
    the Coder — and then watched a full gate run against a workspace nothing had
    touched.

    No regex over prose reliably separates the two. What the loop can see is the
    mode below finding nothing to do with what it was given.
    """
    agent, events = loop_over(router, [say(SUMMARY), say(SUMMARY)])

    said = [e.data["text"] for e in of_type(events, EventType.ASSISTANT)]
    assert said == [SUMMARY], f"the paragraph was emitted {len(said)} times"
    assert of_type(events, EventType.GATE) == [], "a gate ran on work nobody did"
    assert agent.result.outcome == Outcome.DONE
    assert "not a plan" in agent.result.summary


def test_a_coder_that_actually_works_is_left_alone(router: Router, gated) -> None:
    """The guard must not fire on a Coder that had something to say about the
    plan rather than merely saying it back."""
    agent, events = loop_over(
        router,
        [say("1. Edit handler/user.go\n   Accepts: builds"), say("Edited handler/user.go.")],
    )

    assert [e.data["text"] for e in of_type(events, EventType.ASSISTANT)] == [
        "1. Edit handler/user.go\n   Accepts: builds",
        "Edited handler/user.go.",
    ]
    assert of_type(events, EventType.GATE), "the gate must still run on real work"


def test_a_coder_that_calls_a_tool_is_left_alone(router: Router, gated) -> None:
    """A tool call is work, whatever the prose alongside it says."""
    plan = "1. Read handler/user.go"
    agent, events = loop_over(
        router,
        [say(plan), calls(("read_file", '{"path": "handler/user.go"}')), say("done")],
    )

    assert of_type(events, EventType.TOOL_CALL), "the call never happened"
    assert agent.result.outcome == Outcome.DONE
    assert "not a plan" not in agent.result.summary


# ── the two loops that killed real sessions ─────────────────────────────────


def test_a_repeated_call_is_answered_before_the_run_is_killed(router: Router, gated) -> None:
    """The second identical call is intercepted; only the third ends the run.

    Two sessions died here. The detector fired on the third identical call and
    ended the run, throwing away every turn spent so far — but nothing had told
    the model it was repeating itself, because the tool simply ran again and
    returned the same thing, which is the input that produced the repeat.
    """
    same = ("read_file", '{"path":"handler/user.go"}')
    agent, events = loop_over(router, [calls(same), calls(same), calls(same)], mode=Mode.CODER)

    results = [e.data for e in of_type(events, EventType.TOOL_RESULT)]
    intercepted = [r for r in results if "not re-run" in str(r.get("content", ""))]
    assert intercepted, "the second identical call must be answered, not dispatched again"
    assert intercepted[0]["ok"] is False

    # The third still ends the run: a model that ignores the intervention is
    # genuinely stuck, and the detector is still the backstop.
    assert agent.result.outcome == Outcome.NO_PROGRESS


def test_the_intervention_carries_what_the_call_returned(router: Router, gated) -> None:
    """Telling the model "you repeated yourself" without saying what came back
    leaves it exactly as stuck as it was."""
    missing = ("read_file", '{"path":"handler/does_not_exist.go"}')
    _agent, events = loop_over(router, [calls(missing), calls(missing)], mode=Mode.CODER)

    results = [e.data for e in of_type(events, EventType.TOOL_RESULT)]
    intercepted = [r for r in results if "not re-run" in str(r.get("content", ""))]
    assert intercepted, "expected an interception"


def test_a_truncated_tool_call_is_not_reported_as_bad_json(router: Router, gated) -> None:
    """A reply cut off by max_tokens leaves a JSON prefix — in the field, `{`.

    Dispatching it says "malformed arguments", so the model is told to send
    valid JSON. It did send valid JSON and was interrupted; acting on that
    advice means making the same oversized reply and being cut off again. The
    loop must name the real cause instead.
    """
    truncated = ChatResult(
        tool_calls=[ToolCall(id="t1", name="legacy_audit", arguments="{")],
        finish_reason="length",
        usage=Usage(prompt_tokens=100),
    )
    _agent, events = loop_over(router, [truncated, say("shorter now")], mode=Mode.CODER)

    results = [e.data for e in of_type(events, EventType.TOOL_RESULT)]
    assert results, "the model must be told something"
    text = str(results[0].get("content", ""))
    assert "output limit" in text, f"expected the real cause, got {text!r}"
    assert "malformed" not in text.lower(), "the JSON was not malformed; it was cut off"

    # And the call must not have been dispatched: it cannot succeed.
    dispatched = [e for e in of_type(events, EventType.TOOL_CALL)]
    assert not dispatched, "a truncated call must not be run"


def test_the_summariser_uses_a_role_the_config_accepts() -> None:
    """Compaction asked for a role that does not exist, for its whole life.

    `LLMConfig.model_for` whitelists coder, fast and embed. The model is
    resolved inside `chat()` before any request is sent, so `role="summariser"`
    raised a ValueError that the broad `except` swallowed — and every compaction
    in production returned the fallback recap, whose `do_not_retry` is always
    empty. That field is the one context.py calls "what stops the
    post-compaction agent cheerfully repeating the dead end that got it here".

    No test caught it because every compaction test fakes the client, and a fake
    that ignores `role` cannot fail on a role it was never going to validate.
    This one asserts against the real resolver.
    """
    import inspect

    from dakcoder_agent import loop as loop_module
    from dakcoder_shared.config import Deployment, LLMConfig

    source = inspect.getsource(loop_module.AgentLoop._summarise)
    role = re.search(r'role="([^"]+)"', source)
    assert role, "the summariser must name a role explicitly"

    for deployment in (Deployment.LOCAL, Deployment.GATEWAY):
        config = LLMConfig(base_url="http://x/v1", api_key="k", deployment=deployment)
        # Raises if the role is not one the config knows: the exact failure that
        # was invisible in production.
        config.model_for(role.group(1))


def test_a_configuration_error_in_the_summariser_is_announced() -> None:
    """A permanent failure must not look like a transient one.

    The broad `except` treated a misconfigured role — which degrades every
    compaction for the life of the process — the same as a dropped connection.
    Transport failures still degrade quietly; ours are announced.
    """
    from dakcoder_agent.context import ContextManager, Message, Role

    class Misconfigured:
        def chat(self, *_a, **_k):
            raise ValueError("unknown model role 'summariser'")

    seen: list = []
    agent = AgentLoop(
        ContextManager(system_prompt="sp"),
        Misconfigured(),
        Router(Workspace(Path(__file__).parent)),
        on_event=seen.append,
    )
    recap = agent._summarise([Message(Role.USER, "did a thing", turn=1)])

    assert recap is not None, "a degraded recap still beats ending the run"
    errors = [e for e in seen if "summariser" in str(e.data)]
    assert errors, "a permanent summariser failure must be visible in the transcript"


# ── the review session that ran nineteen turns ──────────────────────────────

QUESTIONS = """`review.md` already exists — a 298-line review of exactly this
codebase. Before I plan, I need to know what you want, because "update" means
several different things here.

1. **Depth.** The current doc is a contract-migration review. Do you also want
   the things it left out — error handling, naming, duplication, concurrency?
2. **Audience.** Is this for you, or for a lead who has not seen the codebase?
3. **Scope.** Keep the existing structure and refresh the numbers, or am I free
   to restructure?
"""


def test_a_planner_that_asks_questions_does_not_hand_them_to_the_coder(
    router: Router, gated
) -> None:
    """The nineteen-turn session, reduced to its first wrong step.

    The Planner did exactly what it is told to do — ask at most four clarifying
    questions — and the loop handed the questions to the Coder as a plan,
    because `_count_steps` counts numbered lines and a numbered list of
    questions is numbered.

    Everything after that followed: the Coder had no step to execute so it
    produced prose, `_verify` ran the gate on a workspace nothing had touched,
    the gate failed on damage that pre-dated the session, the Verifier reported
    it, and the ladder sent it back to the Coder. Ten turns and every escalation
    slot went into that circuit, and it was reported as `unverified` — naming
    the gate as the problem when the problem was a handoff that should never
    have happened, with four unanswered questions still on screen.
    """
    agent, events = loop_over(router, [say(QUESTIONS), say("I'll orient myself first.")])

    assert agent.result.outcome == Outcome.DONE
    assert agent.context.turn == 1, "the questions were handed on anyway"
    assert of_type(events, EventType.PLAN) == [], "a question was pinned as the plan"
    assert of_type(events, EventType.GATE) == [], "a gate ran on work nobody did"
    assert "asked for a decision" in agent.result.summary


def test_a_plan_that_asks_something_and_still_plans_reaches_the_coder(
    router: Router, gated
) -> None:
    """The other side of the branch, so the triage cannot quietly widen.

    A real plan may ask. What makes it a plan is that its steps carry the
    `Accepts:` lines the Planner is told to write, and those are what the check
    keys off — not the absence of a question mark.
    """
    plan = (
        "1. Add handler/pension.go\n"
        "   Accepts: go build is clean\n"
        "2. Wire the route in routes/routes.go\n"
        "   Accepts: GET /pensions answers 200\n\n"
        "Is the table really `pensions`? Should the list filter on status? "
        "I have assumed yes to both."
    )
    agent, events = loop_over(router, [say(plan), say("done")])

    assert of_type(events, EventType.PLAN) != [], "a plan with Accepts lines was read as a question"
    assert agent.result.outcome == Outcome.DONE


def test_two_modes_repeating_themselves_at_each_other_is_no_progress(
    router: Router, gated
) -> None:
    """The circuit itself, in case anything else ever feeds it.

    `said` holds the last three replies whatever mode said them, so an
    alternation reads as `[coder, verifier, coder]` and never reaches three of
    anything — the detector was structurally unable to see the shape that
    actually killed the session. It is the mode-keyed ledger that catches it.
    """
    # `go_build`, not `swagger_check`: the scoped stages skip when the run has
    # touched nothing, and this run deliberately touches nothing. `go_build` is
    # unscoped by design — it is authoritative for the whole module — so it is
    # the stage that still fails here and keeps the circuit turning.
    gated["fail"] = "go_build"
    agent, _ = loop_over(
        router,
        [
            say("1. Edit handler/user.go\n   Accepts: builds"),
            say("I'll orient myself first, then plan the review."),   # coder
            say("Stage that failed: go_build."),                      # verifier
            say("I'll orient myself first, then plan the review."),   # coder, again
        ]
        + [say(f"filler {i}") for i in range(10)],
    )

    assert agent.result.outcome == Outcome.NO_PROGRESS
    assert agent.context.turn == 4, "the ping-pong was allowed to keep going"
    assert "same thing" in agent.result.summary


def test_the_gate_is_not_re_run_against_an_unchanged_workspace(
    router: Router, gated
) -> None:
    """Six identical gate runs is not six pieces of evidence.

    Re-appending an identical report is what kept the circuit alive: the
    Verifier was handed the exact input that produced its last paragraph and
    asked for a different one. When nothing has changed the model is told
    *that*, which is the only new information available.
    """
    runs: list[int] = []
    build = router.handlers["go_build"]
    router.handlers["go_build"] = lambda inv: (runs.append(1), build(inv))[1]
    gated["fail"] = "go_build"

    agent, events = loop_over(
        router,
        [say("1. Edit handler/user.go\n   Accepts: builds")]
        + [say(f"attempt {i}") for i in range(12)],
    )

    # Ends on `_narrating`, not the ladder: this run deliberately never edits,
    # which is now recognised for what it is rather than spent as five rungs.
    assert agent.result.outcome == Outcome.NO_PROGRESS
    assert len(runs) == 1, f"the gate ran {len(runs)} times over one unchanged workspace"
    assert [e for e in of_type(events, EventType.GATE) if e.data.get("cached")], (
        "the client was never told the verdict was a repeat"
    )


def test_the_same_call_across_mode_switches_is_still_no_progress(
    router: Router, gated
) -> None:
    """`_switch` clears `recent`, so a call made once per trip round the ladder
    resets the detector every time and can repeat forever."""
    gated["fail"] = "go_build"
    read = ("read_file", '{"path": "handler/user.go"}')
    agent, _ = loop_over(
        router,
        [
            say("1. Read handler/user.go\n   Accepts: builds"),
            calls(read), say("looked"),      # coder  -> verifier
            say("the build is broken"),      # verifier -> coder
            calls(read), say("looked again"),
            say("still broken"),
            calls(read),
        ],
    )

    assert agent.result.outcome == Outcome.NO_PROGRESS
    assert "read_file" in agent.result.summary


def test_a_call_repeated_after_an_edit_is_re_checking_not_looping(
    router: Router, gated
) -> None:
    """The guard on the guard. Re-reading a file you have just written is the
    normal shape of careful work, and the run-wide ledger must not read it as a
    loop — so a mutation clears it."""
    read = ("read_file", '{"path": "handler/user.go"}')
    agent, _ = loop_over(
        router,
        [
            say("1. Write two files\n   Accepts: builds"),
            calls(read),
            calls(("write_file", '{"path": "a.go", "content": "package a"}')),
            calls(read),
            calls(("write_file", '{"path": "b.go", "content": "package b"}')),
            calls(read),
            say("done"),
        ],
        approve=lambda _r: True,
    )

    assert agent.result.outcome == Outcome.DONE


# ── the whatsapp session that ran seventy-one turns ─────────────────────────


def test_reading_the_same_file_every_few_turns_is_no_progress(
    router: Router, gated
) -> None:
    """The seventy-one-turn session, reduced.

    A Planner asked to write a handler read two files totalling a thousand lines
    against a 24k budget. That tripped compaction, compaction evicted the reads,
    the model re-read them, and round it went for seventy-one turns and
    twenty-five compactions without ever producing a plan. It ended on the turn
    cap and was reported as `exhausted`, which named the turn budget as the
    problem.

    `recent` could not see it: each turn called `repo_map`, then one file, then
    another, so no fingerprint ever appeared three times *in a row* — and
    `_switch` would have cleared the ledger anyway. The run-wide count does see
    it, because nothing was being written between the reads.
    """
    same = ("read_file", '{"path": "handler/whatsapp.go"}')
    other = ("read_file", '{"path": "handler/message.go"}')
    agent, _ = loop_over(
        router,
        [calls(same, other), calls(other, same), calls(same)],
        mode=Mode.PLANNER,
    )

    assert agent.result.outcome == Outcome.NO_PROGRESS
    assert "read_file" in agent.result.summary


def test_compacting_every_other_turn_stops_the_run(router: Router, gated) -> None:
    """The same session by its other signature.

    A model that varies its arguments — `repo_map {}`, then
    `repo_map {"max_tokens": 6000}` — slips past the call ledger while doing
    exactly the same thing. The density of compaction is the signal that does
    not depend on the arguments: three inside eight turns means each one is
    being undone by the turn after it.
    """
    context = ContextManager(mode=Mode.PLANNER, system_prompt="s", compact_at=0.0)
    agent = AgentLoop(
        context,
        # Distinct arguments every turn, so only the compaction density can see
        # it — which is the whole point of the check.
        ScriptedClient([calls(("read_file", f'{{"path": "handler/f{i}.go"}}')) for i in range(12)]),
        router,
    )
    list(agent.run("write a handler", start=Mode.PLANNER))

    assert agent.result.outcome == Outcome.NO_PROGRESS
    assert "compactions" in agent.result.summary
    assert agent.context.turn <= COMPACTION_WINDOW + 1, "it was allowed to keep evicting"


def test_a_follow_up_survives_the_compaction_that_follows_it(
    router: Router, gated
) -> None:
    """The developer's message must not be the first thing thrown away.

    A follow-up lands in the working set, which is the layer compaction
    consumes first — so in a run compacting every other turn it was gone within
    two, and the session carried on with the task it had been redirected away
    from. Steering promises that a wrong turn can be corrected without ending
    the run; a correction deleted two turns later is worse than none, because
    the developer believes it landed.
    """
    context = ContextManager(mode=Mode.CODER, system_prompt="s")
    agent = AgentLoop(context, ScriptedClient([say("ok")]), router)
    list(agent.run("Add a Pension resource", start=Mode.CODER))

    list(agent.run("stop reading and write the handler", continued=True, start=Mode.CODER))
    context.compact(lambda messages: Recap(goal="g"), keep_recent=0)

    pinned = "\n".join(m.content for m in context.build() if m.layer is Layer.TASK)
    assert "stop reading and write the handler" in pinned


def test_the_recap_says_which_files_were_already_read(router: Router, gated) -> None:
    """The field the recap did not have, and why the loop above never ended.

    What a compaction throws away is mostly file reads. A recap that does not
    mention them leaves re-reading as the only rational next move — which puts
    the context straight back over the threshold that fired the compaction.

    They were also mislabelled: `_touched` returned every read path as
    `files_modified`, so the recap told the model it had edited files it had
    only opened.
    """
    context = ContextManager(mode=Mode.PLANNER, system_prompt="s")
    context.set_task("write a whatsapp handler")
    context.begin_turn()
    for i, path in enumerate(("handler/whatsapp.go", "internal/app/service/whatsapp.go")):
        context.append_tool_result("read_file", f"contents of {path}", tool_call_id=str(i), path=path)

    class NoSummariser:
        def chat(self, *_a, **_k):
            raise ConnectionError("the gateway is down")

    agent = AgentLoop(context, NoSummariser(), router)
    recap = agent._summarise([m for m in context.build() if m.path])

    assert recap.files_read == ("handler/whatsapp.go", "internal/app/service/whatsapp.go")
    assert recap.files_modified == (), "a file that was read was reported as modified"
    body = recap.markdown()
    assert "Already read" in body
    assert "handler/whatsapp.go" in body


def test_the_tools_array_is_measured_before_the_compaction_decision(
    router: Router, gated
) -> None:
    """The schemas are part of the prompt this turn sends, so a threshold
    consulted without them is consulted against the wrong number."""
    agent, _ = loop_over(router, [say("1. Edit handler/user.go\n   Accepts: builds"), say("done")])

    assert agent.context.usage().tools > 0, "the tools array was still counted as free"


# ── a plan is a plan however it is formatted ────────────────────────────────


@pytest.mark.parametrize(
    "plan",
    [
        "1. Add the repo function\n   Accepts: go build is clean",
        "**1. Add the repo function**\n   Accepts: go build is clean",
        "### 1. Add the repo function\n   Accepts: go build is clean",
        "## Step 1: Add the repo function\nAccepts: go build is clean",
        "1) Add the repo function\n   Accepts: go build is clean",
    ],
    ids=["bare", "bold", "h3", "step-heading", "paren"],
)
def test_a_formatted_plan_still_reaches_the_coder(router: Router, gated, plan: str) -> None:
    """The defect that cost a session twelve turns and the developer their temper.

    `_count_steps` matched a bare `1. ` at line start and nothing else. The
    Planner produced a real eight-step plan with `Accepts:` on every step and
    wrote the titles as `**1. …**`; zero steps matched, so the loop called it
    "not a plan", ended the run `done`, and the developer's "go" arrived as a
    follow-up that began by planning again. Three more messages and the model
    was still in the Planner, correctly reporting it had no write tools.

    Parametrised over the formats a model actually emits, because the failure
    was never about one of them — it was about the loop insisting on a syntax
    the prompt never specified.
    """
    agent, events = loop_over(router, [say(plan), calls(("read_file", '{"path": "x.go"}'))])

    assert of_type(events, EventType.PLAN), "the plan was not recognised as one"
    assert agent.state.mode is not Mode.PLANNER, "the run never left the Planner"


def test_a_numbered_list_of_questions_is_still_not_a_plan(router: Router, gated) -> None:
    """The other half, which the fix above must not break: questions carry no
    `Accepts:` line, and that is what separates them from steps."""
    agent, events = loop_over(
        router,
        [say("1. What should it do?\n2. What parameters?\n3. What does it return?")],
    )

    assert not of_type(events, EventType.PLAN)
    assert agent.result.outcome == Outcome.DONE
    assert "asked" in agent.result.summary


# ── narrating an edit is not making one ─────────────────────────────────────


def test_a_coder_that_only_talks_about_editing_is_stopped(router: Router, gated) -> None:
    """The six turns that ended the field session, caught on the third.

    Turns 30, 31, 33, 35, 36 and 37 each announced the same edit in slightly
    different words — "Making the edit now", then "I have the exact current
    text. Making the edit now", then the same with the gate restated in front —
    and called nothing. Every detector here compared reply text, so the
    paraphrasing walked straight through all of them. The Verifier eventually
    wrote "I'm stuck in a loop — I keep saying I'll make the edit but not
    actually calling patch_file", which is a diagnosis the loop should not need
    the model to make for it.

    So this counts actions, which cannot be paraphrased.
    """
    gated["fail"] = "go_build"
    agent, _ = loop_over(
        router,
        [
            say("1. Edit handler/user.go\n   Accepts: builds"),
            say("I have the anchor. Making the edit now."),
            say("Stage that failed: go_build."),
            say("I have the exact current text. Making the edit now."),
            say("Still blocked at go_build. My job is to make the edit. Making it now."),
        ]
        + [say(f"and now, edit {i}") for i in range(12)],
    )

    assert agent.result.outcome == Outcome.NO_PROGRESS
    assert "without making one" in agent.result.summary
    assert agent.context.turn < 10, "it was allowed to narrate for ten turns"


def test_the_fourth_read_of_one_file_is_answered_not_run(router: Router, gated) -> None:
    """The Planner read repo/postgres/message.go seven times in eight turns.

    Every read used a different line range, so `_stuck` — which fingerprints the
    whole call — saw seven different calls. The slice ledger kept the context
    from growing and did nothing about the turns, and the run stopped for no
    progress without ever producing a plan.
    """
    reads = [
        calls(("read_file", '{"path": "repo/postgres/message.go", "start": %d, "end": %d}'
               % (i * 60, i * 60 + 120)))
        for i in range(6)
    ]
    _, events = loop_over(router, reads + [say("done")])

    refused = [
        e for e in of_type(events, EventType.TOOL_RESULT)
        if "already read" in str(e.data.get("content", ""))
    ]
    assert refused, "the sixth read of one file was dispatched"


def test_re_reading_a_file_you_just_wrote_is_allowed(router: Router, gated) -> None:
    """Checking your own edit is the correct move, so writing a path clears its
    read ledger."""
    agent, _ = loop_over(
        router,
        [
            say("1. Edit handler/user.go\n   Accepts: builds"),
            calls(("write_file", '{"path": "handler/user.go", "content": "package handler"}')),
            say("done"),
        ],
        approve=lambda _r: True,
    )
    assert "handler/user.go" not in agent.state.reads


# ── what the run leaves behind ──────────────────────────────────────────────

def test_editing_between_turns_resets_the_narration_counter(router: Router, gated) -> None:
    """The counterpart, measured rather than asserted about.

    `_narrating` counts turns in which nothing changed, so the claim that has to
    hold is comparative: a run that edits between its replies must outlive one
    that only talks. Asserting a particular terminal outcome instead would be
    asserting where the *other* stop conditions happen to land, which is a
    different test and a brittle one.
    """
    gated["fail"] = "go_build"
    plan = say("1. Edit handler/user.go\n   Accepts: builds")

    talker, _ = loop_over(router, [plan] + [say(f"thinking about it, {i}") for i in range(14)])

    worker_turns = [plan]
    for i in range(7):
        worker_turns += [_edit(i), say(f"edit {i} is in, moving on")]
    worker, _ = loop_over(router, worker_turns, approve=lambda _r: True)

    assert worker.context.turn > talker.context.turn, (
        f"the run that edited stopped at turn {worker.context.turn}, no later than "
        f"the one that only talked ({talker.context.turn})"
    )
    assert "without making one" in talker.result.summary


def test_the_summary_names_plan_files_the_run_never_wrote(router: Router, gated) -> None:
    """"38 turns · 1 file, blocked at swagger_check" was true and buried what
    went wrong: the repo function landed, the handler never did, and the
    developer was left with a query nothing calls. The blocked stage was
    pre-existing; the unwritten handler was this run's.

    Driven through `_unfinished` directly. Getting a scripted run to exhaust the
    escalation ladder means threading edits past five mode switches, which tests
    the script far more than it tests the sentence.
    """
    context = ContextManager(mode=Mode.CODER, system_prompt="s")
    agent = AgentLoop(context, ScriptedClient([]), router)
    agent.state.plan = (
        "1. Add the repo function to repo/postgres/message.go\n"
        "   Accepts: compiles\n"
        "2. Add the handler to handler/message.go\n"
        "   Accepts: builds\n"
    )
    router.touched.append("repo/postgres/message.go")

    summary = agent._unfinished()

    assert "handler/message.go" in summary
    assert "repo/postgres/message.go" not in summary, "a file that was written was called missing"


def test_nothing_is_reported_missing_when_the_plan_named_no_paths(router: Router) -> None:
    """A plan that names its files by some other convention would otherwise have
    every path reported unwritten, which is noise dressed as a finding."""
    context = ContextManager(mode=Mode.CODER, system_prompt="s")
    agent = AgentLoop(context, ScriptedClient([]), router)
    agent.state.plan = "1. Add the repo function\n2. Add the handler"

    assert agent._unfinished() == ""
