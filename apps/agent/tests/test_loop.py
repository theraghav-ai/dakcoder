"""Tests for the agent loop.

Deliberately much smaller than what it replaces, and testing different things.

The old file was 2,193 lines against a loop whose transitions were regexes over
prose, and it is the exhibit the failure report leads with: 776 green tests that
"verify the heuristics react correctly to the exact replies they were written
for". Every one of those replies was chosen by the person writing the heuristic.
It never once caught the classifier on a phrasing nobody had thought of, because
there was nobody else to think of one.

So what is asserted here is the loop's *decisions*, each of which is now a fact
about the run rather than a judgement about text:

* which mode a message enters, and that a question never reaches the gate;
* that a plan arrives as a tool call and nothing reads prose to find one;
* that a mode which must call a tool is made to, rather than counted;
* that the gate runs only on work that was actually done;
* that nothing the loop adds to the transcript is fabricated.

Driven by a scripted model rather than a fake endpoint. The SSE transport is
covered in ``test_llm.py``; putting a real stream underneath these would test
the stream three hundred times and the decisions once.
"""

from __future__ import annotations

import json
from collections.abc import Sequence

import pytest

from dakcoder_agent.context import PINNED_LAYERS, ContextManager, Layer
from dakcoder_agent.gate import GATE
from dakcoder_agent.loop import (
    MAX_FINISH_REFUSALS,
    MAX_GATE_FAILURES,
    MAX_RESEARCH_TURNS,
    STALLS_BEFORE_ANSWER,
    MAX_READS,
    MAX_STALLED_TURNS,
    AgentLoop,
    Intent,
    Outcome,
)
from dakcoder_agent.modes import Mode
from dakcoder_agent.tools import control
from dakcoder_agent.tools.router import Router
from dakcoder_shared.envelope import EventType, ToolResult
from dakcoder_shared.llm import ChatResult, ToolCall, Usage
from dakcoder_shared.paths import Workspace


from scripted import (  # noqa: E402 - the shared scripted model
    PLAN,
    ScriptedClient,
    build,
    calls,
    patch,
    plan_call,
    say,
)

# Fixtures defined in `scripted` are re-exported here so pytest collects them.
from scripted import gated, planning_router, written  # noqa: F401,E402


# ── intent, decided before the first turn ───────────────────────────────────


def test_a_question_answers_read_only_and_never_reaches_the_gate(
    planning_router: Router, gated, written
) -> None:
    """Section 2 of the report, as one test.

    "Explain me this project" used to enter the Planner, and any answer that
    happened to be numbered was pinned as a plan, handed to a Coder with nothing
    to execute, and gated on an untouched workspace -- seventy seconds, a
    pre-existing `go_vet` failure adopted as the run's own, and `go mod tidy`
    rewriting `go.mod` on a run that was asked a question. 17 of 24 realistic
    read-only prompts went that way.
    """
    loop, _client = build(
        planning_router,
        [
            calls(("read_file", '{"path":"handler/user.go"}')),
            say("It declares package handler."),
        ],
        kind="question",
    )
    list(loop.run("explain handler/user.go", intent=Intent.AUTO))

    assert loop.result.outcome == Outcome.DONE
    assert loop.state.mode is Mode.ASK
    assert loop.state.last_gate is None, "a question must never run the gate"
    assert not loop.router.touched, "a question must never change a file"


def test_a_read_only_mode_is_offered_no_write_tool(planning_router: Router) -> None:
    """The guarantee is the schema list, not the prompt. Asking a mode not to
    write is a hope; not giving it the capability is a property."""
    loop, client = build(planning_router, [say("done")], kind="question")
    list(loop.run("what does this service do", intent=Intent.AUTO))

    offered = set(client.seen_tools[0])
    assert "read_file" in offered
    for writer in (
        "write_file",
        "patch_file",
        "delete_file",
        "resource_scaffold",
        "run_terminal",
    ):
        assert writer not in offered


def test_a_change_plans_first_then_acts(planning_router: Router, gated, written) -> None:
    loop, _client = build(planning_router, [plan_call(), patch(), say("done")], kind="change")
    events = list(loop.run("add a Routes method to handler/user.go", intent=Intent.AUTO))

    assert [s.file for s in loop.state.plan] == ["handler/user.go"]
    assert loop.state.mode is Mode.AGENT
    assert any(e.type is EventType.PLAN for e in events)
    assert loop.result.outcome == Outcome.DONE


def test_an_explicit_intent_skips_the_classifier(planning_router: Router) -> None:
    """The panel's Ask/Agent toggle answers the question directly, and a stated
    answer must not be second-guessed by a model call nobody needs."""
    loop, client = build(planning_router, [say("nothing to change")], kind="change")
    list(loop.run("hello", intent=Intent.ASK))

    assert loop.state.intent is Intent.ASK
    assert client.calls == 1, "no classifier call when the intent was stated"


def test_a_classifier_that_fails_answers_read_only(planning_router: Router) -> None:
    """The asymmetry, which is the whole reason the fallback goes this way: a
    wrong "question" costs the developer one word, and a wrong "change" costs
    unrequested edits to files nobody mentioned, found later in a diff."""

    class Broken(ScriptedClient):
        def chat(self, messages, *, response_format=None, **kwargs):
            if response_format is not None:
                raise RuntimeError("the gateway is down")
            return super().chat(messages, **kwargs)

    client = Broken([say("here is the answer")])
    loop = AgentLoop(
        ContextManager(mode=Mode.ASK, system_prompt="s"), client, planning_router, max_turns=4
    )
    list(loop.run("migrate the objection handler", intent=Intent.AUTO))

    assert loop.state.intent is Intent.ASK
    assert loop.result.outcome == Outcome.DONE


# ── the plan is a tool call ─────────────────────────────────────────────────


def test_the_plan_is_read_from_the_tool_call_not_from_prose(
    planning_router: Router, gated, written
) -> None:
    """`_count_steps` counted lines beginning with a digit, so a numbered
    *explanation* was a plan and a plan written as "**1. Add...**" was not."""
    loop, _client = build(planning_router, [plan_call(), patch(), say("done")])
    list(loop.run("add Routes", intent=Intent.AGENT))

    step = loop.state.plan[0]
    assert (step.file, step.accepts) == ("handler/user.go", "go build ./... clean")
    pinned = [m for m in loop.context.build() if m.layer in PINNED_LAYERS]
    assert any("handler/user.go" in m.content for m in pinned), "the plan is pinned"


def test_a_numbered_explanation_is_not_a_plan(planning_router: Router, gated) -> None:
    """The exact reply that used to be executed: a description of a deviation
    reads identically to a proposal to remove it, and the run went off to
    migrate a hundred routes nobody had asked it to touch."""
    loop, _client = build(
        planning_router,
        [
            say(
                "1. Creates the Temporal worker.\n"
                "2. Registers the workflow.\n"
                "   Accepts: it starts.\n"
                "3. Wires the lifecycle hooks."
            )
        ]
        * 3,
    )
    list(loop.run("explain the bootstrapper", intent=Intent.AGENT))

    assert loop.state.plan == (), "prose is never a plan"
    assert loop.state.last_gate is None
    assert not loop.router.touched


def test_asking_the_developer_ends_the_run_with_the_questions_on_screen(
    planning_router: Router,
) -> None:
    loop, _client = build(
        planning_router,
        [
            calls(
                (
                    "ask_developer",
                    json.dumps({"questions": ["Which table?", "What route base?"]}),
                )
            )
        ],
    )
    list(loop.run("add a resource", intent=Intent.AGENT))

    assert loop.result.outcome == Outcome.DONE
    assert "asked for a decision" in loop.result.summary
    assert not loop.router.touched


# ── the tool call is forced, not counted ────────────────────────────────────


def test_a_planner_that_narrates_is_re_asked_with_the_call_required(
    planning_router: Router, gated, written
) -> None:
    """Nine turns of "Making the edit now" with no tool call appear in one
    38-turn field transcript. The loop counted three of them and ended the run;
    vLLM has supported `tool_choice: "required"` throughout."""
    loop, client = build(
        planning_router, [say("I will now write the plan."), plan_call(), patch(), say("done")]
    )
    list(loop.run("add Routes", intent=Intent.AGENT))

    assert "required" in client.tool_choices
    assert loop.state.plan, "the forced turn produced the plan"


def test_a_planner_that_will_not_call_a_tool_twice_ends_honestly(
    planning_router: Router,
) -> None:
    """Forced once; a second refusal is information, not a reason to keep
    asking. The run says what happened rather than manufacturing a plan."""
    loop, client = build(
        planning_router, [say("There is nothing to plan."), say("There is nothing to plan.")]
    )
    list(loop.run("check the handler", intent=Intent.AGENT))

    assert client.tool_choices.count("required") == 1, (
        "the force is once per run: the first refusal may be a turn whose call was "
        "never emitted, the second is an answer"
    )
    assert loop.result.outcome == Outcome.DONE
    assert "no plan was submitted" in loop.result.summary


def test_an_acting_turn_that_ends_with_prose_is_not_forced(
    planning_router: Router, gated, written
) -> None:
    """Prose is how the acting mode says "I am done, run the gate". Forcing a
    tool call there would make finishing impossible."""
    loop, client = build(planning_router, [plan_call(), patch(), say("Done.")])
    list(loop.run("add Routes", intent=Intent.AGENT))

    assert "required" not in client.tool_choices
    assert loop.result.outcome == Outcome.DONE


# ── the gate judges this run's work, or it does not run ─────────────────────


def test_a_run_that_wrote_nothing_cannot_fail(planning_router: Router, gated) -> None:
    """The invariant the report asks for and could not find in the code.

    `_verify` used to run whenever an acting mode ended a turn without a tool
    call -- including the turn where it said "there is nothing to do here".
    """
    gated["fail"] = "go_build"
    loop, _client = build(
        planning_router, [plan_call(), say("Nothing needs changing after all.")]
    )
    list(loop.run("add Routes", intent=Intent.AGENT))

    assert loop.state.last_gate is None, "no mutations, no gate"
    # And the report is honest about which it was. "Nothing was changed" and
    # "nothing needed changing" are different claims and the developer acts on
    # the second: this plan named a file and did not write it, so the run is
    # unstarted rather than finished, and says so.
    assert loop.result.outcome == Outcome.NO_PROGRESS
    assert "handler/user.go" in loop.result.summary, loop.result.summary


def test_a_failing_gate_comes_back_to_the_same_mode(
    planning_router: Router, gated, written
) -> None:
    """No Verifier, no Debugger, no ladder. The model that made the change reads
    the failure -- which is how every mature agent does it, and how a human
    does it."""
    gated["fail"] = "go_build"
    loop, _client = build(
        planning_router, [plan_call(), patch(), say("Done."), patch("handler/user.go")]
    )
    list(loop.run("add Routes", intent=Intent.AGENT))

    assert loop.state.mode is Mode.AGENT, "the mode never changes on a failing gate"
    report = [m for m in loop.context.build() if "gate ran on your change" in m.content]
    assert report, "the failure comes back as an ordinary message"
    assert str(report[0].role) == "user"


def test_a_gate_that_will_not_come_clean_stops_after_a_bounded_number_of_tries(
    planning_router: Router, gated, written
) -> None:
    gated["fail"] = "go_build"
    loop, _client = build(
        planning_router,
        [plan_call(), patch()] + [say("I cannot fix this.")] * 8,
        max_turns=20,
    )
    list(loop.run("add Routes", intent=Intent.AGENT))

    # Prose every turn, so each one reaches `_verify` and spends a gate attempt.
    # The other way out -- a model that answers a blocked gate by calling tools,
    # and so never reaches `_verify` at all -- is `_gate_stalled`, covered
    # separately below.
    assert loop.result.outcome == Outcome.UNVERIFIED
    assert loop.state.gate_failures > MAX_GATE_FAILURES


def test_a_failure_that_predates_the_run_does_not_block_it(
    planning_router: Router, gated, written
) -> None:
    """Root cause 1 of the report, as one test.

    `go_vet` was blocking, unscoped and unbaselined, so a pre-existing tab in a
    struct tag failed every run on the legacy corpus. The run was told the
    failure was its own and spent two Coder attempts and three Debugger cycles
    on a file it had never opened; 100% of coding tasks on that corpus ended
    `unverified`, `no_progress`, or in the ladder.
    """
    gated["pre_existing"] = "go_vet"
    loop, _client = build(planning_router, [plan_call(), patch(), say("Done.")])
    list(loop.run("add Routes", intent=Intent.AGENT))

    vet = next(r for r in loop.state.last_gate.results if r.name == "go_vet")
    assert not vet.ok, "the finding is still reported"
    assert not vet.blocking, "but it is not this run's to fix"
    assert loop.result.outcome == Outcome.DONE


def test_the_gate_is_not_re_run_on_an_unchanged_workspace(
    planning_router: Router, gated, written
) -> None:
    """It is a function of the files and the toolchain, so an unchanged pair
    cannot produce a different verdict -- it can only spend another build."""
    gated["fail"] = "go_build"
    loop, _client = build(
        planning_router, [plan_call(), patch(), say("Done."), say("Still done.")]
    )
    events = list(loop.run("add Routes", intent=Intent.AGENT))

    full = [e for e in events if e.type is EventType.GATE and e.data.get("kind") == "full"]
    assert sum(1 for e in full if not e.data.get("cached")) == 1


# ── nothing fabricated, nothing deleted ─────────────────────────────────────


def test_every_tool_message_answers_a_call_the_assistant_actually_made(
    planning_router: Router, gated, written
) -> None:
    """17 call sites used to append `role: tool` messages attributed to tools
    that never ran -- `repo_map` and `go_build` carrying paragraphs of
    instructions, with no `tool_call_id`. Malformed against a strict endpoint,
    and worse as a prompt: it teaches the model that `go_build` replies in
    prose.
    """
    gated["fail"] = "go_build"
    loop, _client = build(
        planning_router,
        [
            plan_call(),
            calls(("search_docs", '{"query":"repository timeouts"}')),
            patch(),
            say("Done."),
            say("Still stuck."),
        ],
    )
    list(loop.run("add Routes", intent=Intent.AGENT))

    declared = {call.id for message in loop.context.build() for call in message.tool_calls}
    for message in loop.context.build():
        if str(message.role) == "tool":
            assert message.tool_call_id, f"fabricated: {message.content[:60]!r}"
            assert message.tool_call_id in declared


def test_the_head_carries_one_mode_instruction(
    planning_router: Router, gated, written
) -> None:
    loop, _client = build(planning_router, [plan_call(), patch(), say("done")])
    list(loop.run("add Routes", intent=Intent.AGENT))

    overlays = [m for m in loop.context.build() if m.layer is Layer.MODE]
    assert len(overlays) == 1
    assert "Agent mode" in overlays[0].content


# ── the ledgers answer rather than end the run ──────────────────────────────


def test_a_repeated_call_is_answered_from_the_previous_result(
    planning_router: Router,
) -> None:
    loop, _client = build(
        planning_router,
        [
            calls(("search_repo", '{"pattern":"Routes"}')),
            calls(("search_repo", '{"pattern":"Routes"}')),
            say("done"),
        ],
        kind="question",
    )
    events = list(loop.run("where are the routes", intent=Intent.ASK))

    intercepted = [
        e for e in events if e.type is EventType.TOOL_RESULT and e.data.get("intercepted")
    ]
    assert intercepted, "the repeat was answered, not dispatched again"
    assert intercepted[0].data["ok"], "a cached answer is a success, not a failure"


def test_a_repeat_loop_is_broken_before_it_can_end_the_session(
    planning_router: Router,
) -> None:
    """The failure the developer reported, as the test that would have caught it.

    A model asking one call with one set of arguments over and over used to burn
    six turns being answered from the ledger and then have the whole session
    killed with `no_progress` -- in the field, twice, once after the work was
    finished and committed. The answers were correct every time; nothing in them
    could make the model stop, because the move it needed was to stop calling
    tools and no message can compel that while a tool schema is on the table.

    It is now broken at the second stalled turn by dispatching the next one with
    `tool_choice: "none"`, and `MAX_STALLED_TURNS` is never reached.
    """
    repeat = calls(("search_repo", json.dumps({"pattern": "Routes"})))
    loop, client = build(
        planning_router, [repeat] * 12, kind="question", max_turns=20
    )
    list(loop.run("where are the routes", intent=Intent.ASK))

    assert loop.result.outcome == Outcome.DONE, loop.result.summary
    assert loop.state.stalled_turns < MAX_STALLED_TURNS, (
        f"the run reached {loop.state.stalled_turns} stalled turns; it should have "
        "been made to answer at " + str(STALLS_BEFORE_ANSWER)
    )
    assert any(isinstance(c, dict) for c in client.tool_choices), (
        "the stalled turn was not forced to call `finish`"
    )
    # And it cost a handful of turns, not the whole budget.
    assert loop.result.turns <= STALLS_BEFORE_ANSWER + 3, loop.result.turns


def test_a_run_that_will_not_answer_even_when_forced_still_stops(
    planning_router: Router,
) -> None:
    """The backstop, still there.

    `tool_choice: "none"` is enforced by the endpoint, so the turn after a stall
    cannot call a tool -- but a proxy that drops the parameter, or a server that
    ignores it, would put the loop back where it was. `MAX_STALLED_TURNS` is what
    catches that, and it must still end the run rather than spin.
    """

    class Defiant(ScriptedClient):
        """A server that accepts `tool_choice` and does not honour it."""

        def chat(self, messages, *, tool_choice=None, **kwargs):
            return super().chat(messages, **kwargs)

    repeat = calls(("search_repo", json.dumps({"pattern": "Routes"})))
    client = Defiant([repeat] * 12, kind="question")
    loop = AgentLoop(
        ContextManager(mode=Mode.ASK, system_prompt="s"),
        client,
        planning_router,
        max_turns=20,
    )
    list(loop.run("where are the routes", intent=Intent.ASK))

    assert loop.result.outcome == Outcome.NO_PROGRESS
    assert loop.state.stalled_turns >= MAX_STALLED_TURNS


def test_a_read_that_asks_for_lines_already_in_context_is_not_dispatched(
    planning_router: Router, written
) -> None:
    """The same window twice is answered from what is already above."""
    same = calls(("read_file", json.dumps({"path": "handler/user.go"})))
    loop, _client = build(planning_router, [same, same, say("done")], kind="question")
    events = list(loop.run("read the handler", intent=Intent.ASK))

    told = [
        e
        for e in events
        if e.type is EventType.TOOL_RESULT and e.data.get("intercepted")
    ]
    assert told, "the second read of the same lines was dispatched again"


def test_a_large_file_is_not_cut_off_after_ten_windows(
    planning_router: Router, workspace: Workspace
) -> None:
    """The 6,571-line handler, as a regression.

    The read budget was a flat ten calls per path, counted without looking at
    the ranges -- so a model working through `handler/paogen.go` in thirty-line
    windows was refused on its eleventh, having been shown about 280 of its
    6,571 lines, and told that reading it again "is not going to show you
    anything those did not". It was going to show it the other ninety-six per
    cent.

    The budget scales with the file now, and a range that reaches past what has
    been delivered is dispatched however many reads have come before.
    """
    big = workspace.root / "handler" / "huge.go"
    big.parent.mkdir(parents=True, exist_ok=True)
    big.write_text(
        "package handler" + chr(10) + (chr(10).join(f"// line {i}" for i in range(7000))),
        encoding="utf-8",
    )

    windows = MAX_RESEARCH_TURNS - 1
    reads = [
        calls(
            (
                "read_file",
                json.dumps(
                    {"path": "handler/huge.go", "start": 1 + i * 30, "end": 30 + i * 30}
                ),
            )
        )
        for i in range(windows)
    ]
    loop, _client = build(
        planning_router, reads, kind="question", max_turns=windows + 4
    )
    events = list(loop.run("walk through the handler", intent=Intent.ASK))

    refused = [
        e
        for e in events
        if e.type is EventType.TOOL_RESULT and e.data.get("intercepted")
    ]
    assert not refused, f"a fresh window was refused: {[e.data for e in refused][:2]}"

    ledger = loop.state.reads["handler/huge.go"]
    assert ledger.calls == windows
    assert ledger.budget() > windows, "a 7,000-line file must be worth more than 10 reads"
    assert ledger.covered_lines() >= windows * 30 - 30



def test_a_phase_that_never_stops_researching_is_made_to_finish(
    planning_router: Router, gated, written
) -> None:
    """The 19-turn Planner, and the fence around the cliff.

    Measured against the live endpoint by replaying a real transcript at
    increasing depth: the model stays sensible through five consecutive fruitless
    tool calls and at **six** repeats its last call 5 times out of 5, never
    recovering. Forcing the terminal tool rescues a run already over the edge;
    this stops it going over.

    A Planner is pointed at `submit_plan` rather than `finish`, because a plan
    submitted under protest is a better thing to argue with than nineteen more
    turns of reading.
    """
    read = calls(("read_file", json.dumps({"path": "handler/user.go"})))
    loop, client = build(
        planning_router,
        # Each read is a *different* file, so nothing is a repeat and the stall
        # detector never fires. Only the research bound can stop this.
        [
            calls(("read_file", json.dumps({"path": "handler/user.go", "start": i, "end": i + 5})))
            for i in range(1, 60, 2)
        ],
        max_turns=MAX_RESEARCH_TURNS + 6,
    )
    list(loop.run("migrate this service to the template", intent=Intent.AGENT))

    forced = [c for c in client.tool_choices if isinstance(c, dict)]
    assert forced, "a phase called tools forever and was never made to finish"
    assert forced[0]["function"]["name"] == "submit_plan"
    assert loop.state.research_turns <= MAX_RESEARCH_TURNS + 1, loop.state.research_turns


def test_a_finish_that_abandons_the_plan_is_sent_back_once(
    planning_router: Router, gated, written
) -> None:
    """The failure that giving `agent` a terminal tool created.

    Measured live: two runs in three called `finish` on their *first* acting
    turn -- "I have gathered all the necessary details to write the migration
    plan" -- having written nothing. Finishing had become the easiest move in
    the room.

    Sent back once, naming the file. The second `finish` is believed, because
    this reads paths out of the plan and is not the arbiter of whether a step
    was still needed.
    """
    quit_early = calls(("finish", json.dumps({"answer": "I have what I need."})))
    loop, _client = build(
        planning_router, [plan_call(), quit_early, patch(), say("done")], max_turns=10
    )
    list(loop.run("add Routes", intent=Intent.AGENT))

    pushed = [
        m for m in loop.context.build() if "Not yet. Your plan set out to write" in m.content
    ]
    assert pushed, "a finish that wrote nothing was accepted"
    assert "handler/user.go" in pushed[0].content
    assert loop.router.touched == ["handler/user.go"], "the push did not get the work done"


def test_a_second_finish_is_believed(
    planning_router: Router, gated, written
) -> None:
    """The bound. The model may legitimately have decided against a step, and
    after one push it is taken at its word rather than argued with."""
    quit_early = calls(
        ("finish", json.dumps({"answer": "Done.", "blocked": "the file is not needed"}))
    )
    loop, _client = build(planning_router, [plan_call()] + [quit_early] * 4, max_turns=10)
    list(loop.run("add Routes", intent=Intent.AGENT))

    assert loop.state.finish_refused == MAX_FINISH_REFUSALS
    assert loop.result.outcome in (Outcome.DONE, Outcome.NO_PROGRESS)
    assert not loop.router.touched


# ── the loops the field found ───────────────────────────────────────────────


def test_a_repeated_call_supersedes_its_own_earlier_answer(
    planning_router: Router,
) -> None:
    """The transcript must never demonstrate the behaviour it is asking to stop.

    Measured on the live endpoint: **one** (repeated call -> "answered from the
    previous result") pair in history and the model moves on 5/5; **two** and it
    repeats the call 5/5 forever, whatever the answer says. Two field runs died
    exactly there -- one asked `git_ops commit` seven times after committing
    successfully, the other asked one `search_repo` eight times -- with every
    repeat answered correctly into a transcript that told it to do it again.
    """
    repeat = calls(("search_repo", json.dumps({"pattern": "Routes"})))
    loop, _client = build(
        planning_router, [repeat, repeat, repeat, say("done")], kind="question", max_turns=8
    )
    list(loop.run("where are the routes", intent=Intent.ASK))

    live = [
        m
        for m in loop.context.build()
        if str(m.role) == "tool" and "asked again with the same arguments" in m.content
    ]
    stubbed = [
        m
        for m in loop.context.build()
        if str(m.role) == "tool" and m.content.startswith("[search_repo was asked again")
    ]
    assert len(live) <= 1, "the intercept pattern accumulated in the transcript"
    assert stubbed, "the earlier answer was not superseded"
    # Superseded in place, never removed: the wire stays well-formed.
    declared = {c.id for m in loop.context.build() for c in m.tool_calls}
    for message in loop.context.build():
        if str(message.role) == "tool":
            assert message.tool_call_id in declared


def test_a_stalled_turn_is_followed_by_one_forced_to_call_finish(
    planning_router: Router,
) -> None:
    """A model repeating one call is out of *moves it recognises*, not ideas.

    Measured on the live endpoint at the depth where the loop forms: no wording
    rescues it (5/5 repeat), suppressing the tools makes it emit `<tool_call>`
    markup as prose, and offering `finish` unforced is ignored. Naming `finish`
    in `tool_choice` ends the turn 5/5. That is what this asserts is wired.
    """
    repeat = calls(("search_repo", json.dumps({"pattern": "Routes"})))
    loop, client = build(
        planning_router,
        [repeat] * 4 + [say("I have what I need.")],
        kind="question",
        max_turns=10,
    )
    list(loop.run("where are the routes", intent=Intent.ASK))

    forced = [c for c in client.tool_choices if isinstance(c, dict)]
    assert forced, "the run was never made to answer"
    assert forced[0]["function"]["name"] == "finish"
    assert loop.result.outcome == Outcome.DONE


def test_a_blocked_gate_stops_the_run_even_when_the_model_keeps_calling_tools(
    planning_router: Router, gated, written
) -> None:
    """The hole that swallowed a whole run.

    `_gate_failed` is reached only from `_verify`, which is reached only from a
    turn that called **no** tool. So a model that answers a blocked gate by
    calling tools -- any tools -- was never counted against the gate budget,
    never re-asked and never stopped. A field transcript blocked at `rules_lint`
    on turn 45 and then spent twenty turns on `go_build`, `git_status` and
    `git_ops commit` with `gate_failures` stuck at 1, ending `no_progress` at
    the turn cap -- which named the wrong thing entirely.
    """
    gated["fail"] = "go_build"
    noise = calls(("git_status", "{}"))
    loop, _client = build(
        planning_router,
        [plan_call(), patch(), say("Done.")] + [noise] * 12,
        max_turns=24,
    )
    list(loop.run("add Routes", intent=Intent.AGENT))

    assert loop.result.outcome == Outcome.UNVERIFIED
    assert "go_build" in loop.result.summary
    assert "changed no file" in loop.result.summary


def test_a_run_that_stalls_with_work_on_disk_says_what_it_did(
    planning_router: Router, gated, written
) -> None:
    """`no_progress` on its own is a report about the loop, and in the field it
    was wrong about the run: nine files written, built, committed, and reported
    to the developer as having made no progress."""
    repeat = calls(("search_repo", json.dumps({"pattern": "Routes"})))
    loop, _client = build(
        planning_router,
        [plan_call(), patch()] + [repeat] * 10,
        max_turns=20,
    )
    list(loop.run("add Routes", intent=Intent.AGENT))

    assert loop.router.touched
    assert "handler/user.go" in loop.result.summary, loop.result.summary


# ── approval and cancellation ───────────────────────────────────────────────


def test_a_denied_approval_leaves_the_workspace_alone(
    planning_router: Router, gated, written
) -> None:
    """`bootstrap/bootstrapper.go` is structural, so writing it is conditional
    on the developer saying yes. An ordinary handler file is not, which is the
    point of the distinction: the approval layer must cost nothing on the
    bread-and-butter edit and everything on the composition root."""
    loop, _client = build(
        planning_router,
        [
            plan_call(),
            calls(
                (
                    "patch_file",
                    json.dumps(
                        {
                            "path": "bootstrap/bootstrapper.go",
                            "old": "package bootstrap",
                            "new": "package bootstrap // x",
                        }
                    ),
                )
            ),
            say("I was not allowed to."),
        ],
        approve=lambda _r: False,
        max_turns=8,
    )
    events = list(loop.run("add Routes", intent=Intent.AGENT))

    assert any(e.type is EventType.TOOL_PENDING for e in events)
    assert not loop.router.touched
    assert loop.state.last_gate is None, "a refused write leaves nothing to gate"
    # The plan named a file that was never written, so the run reports itself as
    # unstarted rather than done -- which is what it is, whoever decided it.
    assert loop.result.outcome == Outcome.NO_PROGRESS


def test_stopping_mid_batch_answers_the_calls_it_abandoned(
    planning_router: Router, written
) -> None:
    """An aborted session is resumable, so an unanswered call is carried into
    the resume and every later request with it."""
    stop = {"now": False}
    batch = calls(
        ("read_file", '{"path":"handler/user.go"}'),
        ("read_file", '{"path":"handler/user.go","start":1,"end":1}'),
    )
    loop, _client = build(
        planning_router,
        [batch],
        kind="question",
        max_turns=4,
        cancelled=lambda: stop["now"],
    )

    for event in loop.run("read it", intent=Intent.ASK):
        if event.type is EventType.TOOL_RESULT:
            stop["now"] = True

    declared = {c.id for m in loop.context.build() for c in m.tool_calls}
    answered = {m.tool_call_id for m in loop.context.build() if str(m.role) == "tool"}
    assert declared <= answered, "every declared call must have an answer"
    assert loop.result.outcome == Outcome.ABORTED
