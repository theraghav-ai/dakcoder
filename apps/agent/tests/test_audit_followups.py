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
    MAX_CALLS_PER_BATCH,
    STALLS_BEFORE_ANSWER,
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
    # Each search finds a different place, so every turn genuinely informs the
    # run and the fence -- not the stall guard -- is what ends the phase.
    found = [
        "package domain", "package postgres", "package handler", "package request",
        "package bootstrap", "package main", "GetAll", "GetByID", "Routes",
        "CreateUserRequest", "FxRepo", "FirstName",
    ]
    assert len(found) >= MAX_RESEARCH_TURNS
    reads = [
        calls(("search_repo", json.dumps({"pattern": pattern})))
        for pattern in found[:MAX_RESEARCH_TURNS]
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


# ── the task state machine, and the block that shows it ─────────────────────
#
# The third review's root cause: "a correct control state machine and no task
# state machine, and the task state it does hold -- router.touched, state.plan,
# state.last_gate -- is never shown to the model." One test per fix, in the
# order the review ranked them.


def _state_blocks(loop) -> list[str]:
    return [m.content for m in loop.context.build() if m.source == "directive"]


def test_the_state_block_shows_the_change_set_the_plan_and_the_gate(
    planning_router: Router, gated, written
) -> None:
    """Fix 1. The model's only evidence about its own progress was the
    transcript, including its own statements of intent."""
    gated["fail"] = "go_build"
    loop, _client = build(planning_router, [plan_call(), patch(), say("Done.")])
    list(loop.run("add Routes", intent=Intent.AGENT))

    block = _state_blocks(loop)[-1]
    assert "# Current state" in block
    assert "Written this run: handler/user.go" in block
    assert "[done] handler/user.go" in block, "the step is done because the file was written"
    assert "Last gate: FAIL at go_build" in block
    assert "gate failed at go_build" in block, "the failure is listed under what was tried"


def test_the_state_block_is_silent_on_a_question(planning_router: Router) -> None:
    """A read-only run with nothing to report gets no block: it is not noise."""
    loop, _client = build(
        planning_router,
        [calls(("read_file", '{"path":"handler/user.go"}'))],
        kind="question",
    )
    list(loop.run("what does the handler do", intent=Intent.ASK))
    assert not any("# Current state" in b for b in _state_blocks(loop))


def test_the_state_block_is_rebuilt_from_ground_truth_not_from_prose(
    planning_router: Router, gated, written
) -> None:
    """The model claiming it wrote a file changes nothing in the block."""
    loop, _client = build(
        planning_router,
        [plan_call(), say("I have written handler/user.go and it builds.")],
    )
    list(loop.run("add Routes", intent=Intent.AGENT))
    block = _state_blocks(loop)[-1]
    assert "Written this run: nothing yet" in block
    assert "[pending] handler/user.go" in block


# ── Fix 2: plan steps carry a status ────────────────────────────────────────


def test_a_plan_step_moves_from_pending_to_done_when_its_file_is_written(
    planning_router: Router, gated, written
) -> None:
    loop, _client = build(planning_router, [plan_call(), patch(), say("Done.")])
    events = list(loop.run("add Routes", intent=Intent.AGENT))
    assert loop.state.plan[0].status == "done"
    assert not any(
        e.type is EventType.GATE and e.data.get("kind") == "replan" for e in events
    )


def test_a_gate_failure_marks_the_step_it_names_failed(
    planning_router: Router, gated, written
) -> None:
    """`failed` is a status the set difference could not represent."""
    from dakcoder_shared.envelope import ToolResult

    # Fails only once the run has changed something, like the `gated` fixture:
    # a stage that fails during the baseline too is excused as pre-existing.
    planning_router.handlers["go_build"] = lambda inv: (
        ToolResult.failure("handler/user.go:3:1: undefined: Routes")
        if planning_router.mutations > 0
        else ToolResult.success("go build: clean")
    )
    loop, _client = build(planning_router, [plan_call(), patch(), say("Done.")])
    list(loop.run("add Routes", intent=Intent.AGENT))
    step = loop.state.plan[0]
    assert step.status == "failed", step
    assert "go_build" in step.note
    assert loop._unwritten_targets() == [], "a written-then-failed step is not never written"


# ── Fix 3: informed is not dispatched ───────────────────────────────────────


def test_an_empty_search_does_not_count_as_progress(planning_router: Router) -> None:
    """A search that finds nothing is a finding, not progress -- and rephrasing
    it forever used to reset the stall counter every time."""
    empties = [
        calls(("search_repo", json.dumps({"pattern": f"NoSuchSymbol{i}"})))
        for i in range(STALLS_BEFORE_ANSWER + 1)
    ]
    loop, client = build(planning_router, empties, kind="question", max_turns=8)
    list(loop.run("where is NoSuchSymbol used", intent=Intent.ASK))
    assert any(isinstance(c, dict) for c in client.tool_choices), (
        "the empty searches were counted as progress, so the run was never made to answer"
    )


def test_the_same_result_under_different_arguments_is_not_progress(
    planning_router: Router,
) -> None:
    """`Handler` and `handler` are two fingerprints and one set of places."""
    same_places = [
        calls(("search_repo", json.dumps({"pattern": "func New"}))),
        calls(("search_repo", json.dumps({"pattern": "func New\\("}))),
        calls(("search_repo", json.dumps({"pattern": "func N[e]w"}))),
    ]
    loop, _client = build(planning_router, same_places, kind="question", max_turns=8)
    list(loop.run("where is New defined", intent=Intent.ASK))
    told = [
        m.content for m in loop.context.build()
        if m.source == "user" and "same lines under different words" in m.content
    ]
    assert told, "the run was never told its searches returned the same places"


# ── Fix 4: a replan path ────────────────────────────────────────────────────


def test_a_second_gate_failure_after_an_edit_sends_the_run_back_to_plan(
    planning_router: Router, gated, written
) -> None:
    """Every exit used to be a stop. The second failure after an edit is the
    strongest evidence a run produces that its approach is wrong."""
    gated["fail"] = "go_build"
    second = calls(
        (
            "submit_plan",
            json.dumps(
                {
                    "steps": [
                        {
                            "file": "repo/postgres/user.go",
                            "action": "move the Routes wiring here",
                            "accepts": "go build",
                        }
                    ]
                }
            ),
        )
    )
    loop, client = build(
        planning_router,
        [plan_call(), patch(), say("Done."), patch(), say("Done again."), second],
        max_turns=16,
    )
    events = list(loop.run("add Routes", intent=Intent.AGENT))

    replans = [e for e in events if e.type is EventType.GATE and e.data.get("kind") == "replan"]
    assert len(replans) == 1, "exactly one loop-initiated replan"
    assert replans[0].data["tried"], "the replan carries what was tried"
    asked = [m.content for m in loop.context.build() if "# What has been tried" in m.content]
    assert asked and "gate failed at go_build" in asked[0]
    plans = [e for e in events if e.type is EventType.PLAN]
    assert len(plans) == 2, "the revised plan was adopted"
    assert loop.state.replans == 1
    statuses = [(s.file, s.status) for s in loop.state.plan]
    assert statuses == [("handler/user.go", "done"), ("repo/postgres/user.go", "pending")], (
        "a done step survives the replan; the new step starts pending"
    )


def test_a_planner_that_declines_after_a_replan_does_not_report_done(
    planning_router: Router, gated, written
) -> None:
    gated["fail"] = "go_build"
    loop, _client = build(planning_router, [], max_turns=16)

    class Declining(ScriptedClient):
        """Refuses the forced submit_plan by answering in prose again."""

        def chat(self, messages, *, tool_choice=None, **kwargs):
            if tool_choice is not None and loop.state.replans:
                return say("There is nothing sensible left to plan.")
            return super().chat(messages, tool_choice=tool_choice, **kwargs)

    loop.client = Declining([plan_call(), patch(), say("Done."), patch()])
    list(loop.run("add Routes", intent=Intent.AGENT))
    assert loop.result.outcome == Outcome.UNVERIFIED
    assert "revised plan was asked for" in loop.result.summary


def test_the_model_can_revise_the_plan_and_the_reason_is_remembered(
    planning_router: Router, gated, written
) -> None:
    revision = calls(
        (
            "revise_plan",
            json.dumps(
                {
                    "reason": "the Routes method belongs on the handler, not the repo",
                    "steps": [
                        {"file": "handler/user.go", "action": "add Routes", "accepts": "go build"},
                        {
                            "file": "repo/postgres/user.go",
                            "action": "no change",
                            "accepts": "n/a",
                            "status": "skipped",
                            "note": "the repo has no routes",
                        },
                    ],
                }
            ),
        )
    )
    loop, _client = build(planning_router, [plan_call(), revision, patch(), say("Done.")])
    events = list(loop.run("add Routes", intent=Intent.AGENT))

    plans = [e for e in events if e.type is EventType.PLAN]
    assert len(plans) == 2, "the revision is a plan event too"
    statuses = {s.file: s.status for s in loop.state.plan}
    assert statuses["repo/postgres/user.go"] == "skipped"
    assert statuses["handler/user.go"] == "done"
    assert any("plan revised" in t for t in loop.state.tried)
    assert loop.result.outcome == Outcome.DONE, loop.result.summary


# ── Fix 5: batches are bounded ──────────────────────────────────────────────


def test_a_reply_that_repeats_a_call_in_the_same_batch_runs_it_once(
    planning_router: Router,
) -> None:
    from scripted import assert_wire_is_coherent

    read = ("read_file", '{"path":"handler/user.go"}')
    loop, _client = build(planning_router, [calls(read, read, read)], kind="question")
    events = list(loop.run("show me the handler", intent=Intent.ASK))
    results = [e for e in events if e.type is EventType.TOOL_RESULT]
    assert sum(1 for r in results if r.data.get("dispatched") is False) == 2
    assert_wire_is_coherent(loop.context.wire())


def test_a_reply_past_the_batch_cap_is_answered_not_dispatched(
    planning_router: Router,
) -> None:
    from scripted import assert_wire_is_coherent

    many = [
        ("search_repo", json.dumps({"pattern": f"p{i}"}))
        for i in range(MAX_CALLS_PER_BATCH + 2)
    ]
    loop, _client = build(planning_router, [calls(*many)], kind="question")
    events = list(loop.run("find things", intent=Intent.ASK))
    not_run = [
        e for e in events
        if e.type is EventType.TOOL_RESULT and e.data.get("dispatched") is False
    ]
    assert len(not_run) == 2
    assert_wire_is_coherent(loop.context.wire())


# ── Fix 6: finish is bounded and never batched ──────────────────────────────


def test_finish_sent_with_other_calls_is_refused_and_the_others_run(
    planning_router: Router,
) -> None:
    loop, _client = build(
        planning_router,
        [
            calls(("read_file", '{"path":"handler/user.go"}'), ("finish", '{"answer":"x"}')),
            calls(("finish", '{"answer":"the handler has one method"}')),
        ],
        kind="question",
    )
    events = list(loop.run("what is in the handler", intent=Intent.ASK))
    refused = [
        e for e in events
        if e.type is EventType.TOOL_RESULT
        and e.data["name"] == "finish"
        and e.data.get("dispatched") is False
    ]
    assert refused and "on its own" in refused[0].data["content"]
    assert loop.result.outcome == Outcome.DONE
    assert loop.state.forced_terminal == 0, "a batched finish is not a schema refusal"


def test_a_finish_answer_is_capped_and_says_so(planning_router: Router) -> None:
    from dakcoder_agent.tools.control import MAX_ANSWER_CHARS

    out = planning_router.dispatch(
        "finish", {"answer": "y" * (MAX_ANSWER_CHARS + 500)}, mode=Mode.ASK
    )
    assert out.ok
    assert "answer cut at" in out.content
    assert out.meta["answer_cut"] == 500


def test_a_cut_off_write_names_the_file_and_what_has_landed(
    planning_router: Router, gated, written
) -> None:
    cut = ChatResult(
        tool_calls=[
            ToolCall(
                id="w1",
                name="write_file",
                arguments='{"path":"handler/new.go","content":"package ha',
            )
        ],
        finish_reason="length",
        usage=Usage(prompt_tokens=100),
    )
    loop, _client = build(planning_router, [plan_call(), patch(), cut, say("Done.")])
    list(loop.run("add Routes", intent=Intent.AGENT))
    told = [
        m.content for m in loop.context.build()
        if m.source == "tool:write_file" and "cut off" in m.content
    ]
    assert told, "the truncated call was not answered"
    assert "handler/new.go" in told[0]
    assert "Written this run so far: handler/user.go" in told[0]
