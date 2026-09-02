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

from dakcoder_agent.context import ContextManager, Layer
from dakcoder_agent.gate import GATE
from dakcoder_agent.loop import (
    MAX_GATE_FAILURES,
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


class ScriptedClient:
    """Returns preset turns, and records what it was asked with.

    ``response_format`` is the tell for a structured call -- the intent
    classifier and the compaction summariser -- and those are answered from a
    canned object rather than from the script, so a test's turns line up with
    the turns it actually cares about.
    """

    def __init__(self, turns: Sequence[ChatResult], *, kind: str = "change") -> None:
        self.turns = list(turns)
        self.seen_tools: list[list[str]] = []
        self.tool_choices: list[str | None] = []
        self.calls = 0
        #: What the intent classifier answers.
        self.kind = kind

    def chat(
        self, messages, *, tools=None, tool_choice=None, response_format=None, **kwargs
    ) -> ChatResult:
        self.calls += 1
        if response_format is not None:
            name = response_format.get("json_schema", {}).get("name")
            body = {"kind": self.kind} if name == "intent" else {"goal": "scripted"}
            return ChatResult(
                content=json.dumps(body), finish_reason="stop", usage=Usage(prompt_tokens=10)
            )
        self.seen_tools.append([t["function"]["name"] for t in (tools or [])])
        self.tool_choices.append(tool_choice)
        if not self.turns:
            # Numbered, so filler stands for "the model said something" rather
            # than "it said the same thing twice".
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


#: A one-step plan, as `submit_plan` takes it.
PLAN = json.dumps(
    {
        "steps": [
            {
                "file": "handler/user.go",
                "action": "add the Routes method",
                "accepts": "go build ./... clean",
            }
        ]
    }
)


def plan_call() -> ChatResult:
    return calls(("submit_plan", PLAN))


def patch(path: str = "handler/user.go") -> ChatResult:
    return calls(
        (
            "patch_file",
            json.dumps(
                {"path": path, "old": "package handler", "new": "package handler // x"}
            ),
        )
    )


@pytest.fixture
def written(workspace: Workspace) -> Workspace:
    """A workspace the gate will actually gate.

    The ``go.mod`` matters: every Go stage is guarded on the workspace root
    being a module, and without one they all record "workspace root has no
    go.mod" and the report comes back clean. A test asserting on a failing gate
    would then be asserting on a gate that never ran.
    """
    (workspace.root / "go.mod").write_text(
        "module example.test" + chr(10) * 2 + "go 1.24" + chr(10),
        encoding="utf-8",
    )
    (workspace.root / "handler").mkdir(parents=True, exist_ok=True)
    (workspace.root / "handler" / "user.go").write_text(
        "package handler" + chr(10), encoding="utf-8"
    )
    (workspace.root / "bootstrap").mkdir(parents=True, exist_ok=True)
    (workspace.root / "bootstrap" / "bootstrapper.go").write_text(
        "package bootstrap" + chr(10), encoding="utf-8"
    )
    return workspace


@pytest.fixture
def planning_router(router: Router) -> Router:
    """The shared router, plus the two tools that end the planning phase."""
    router.handlers.update(control.HANDLERS)
    return router


@pytest.fixture
def gated(planning_router: Router):
    """Scripted gate stages, so a run's outcome is set by the test not the toolchain.

    A named stage fails **only once the run has changed something**, and that is
    not a convenience -- it is what a failure the run is answerable for looks
    like. The gate takes a baseline before the first edit and reports a stage
    that was already failing as advisory, so a stage scripted to fail from the
    start is correctly excused and the gate comes back clean. Modelling "the
    change broke it" is the only way to test a gate that blocks.

    ``state["pre_existing"]`` is the other half: a stage that fails throughout,
    which must never block.
    """
    state: dict[str, str | None] = {"fail": None, "pre_existing": None}

    for name in {stage.tool for stage in GATE} | {"gofmt", "rules_lint", "go_diagnostics"}:

        def handler(inv, _name=name):
            broke_it = state["fail"] == _name and planning_router.mutations > 0
            if broke_it or state["pre_existing"] == _name:
                return ToolResult.failure(f"{_name}: boom")
            meta = {"violations": 0} if _name == "rules_lint" else {}
            return ToolResult.success(f"{_name}: clean", meta=meta)

        planning_router.handlers[name] = handler
    return state


def build(
    router: Router,
    turns: Sequence[ChatResult],
    *,
    kind: str = "change",
    max_turns: int = 12,
    approve=lambda _r: True,
    cancelled=lambda: False,
) -> tuple[AgentLoop, ScriptedClient]:
    client = ScriptedClient(turns, kind=kind)
    context = ContextManager(mode=Mode.ASK, system_prompt="You are dakcoder.")
    loop = AgentLoop(
        context,
        client,
        router,
        approve=approve,
        cancelled=cancelled,
        max_turns=max_turns,
    )
    return loop, client


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
    pinned = [m for m in loop.context.build() if m.layer is Layer.TASK]
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

    assert client.tool_choices.count("required") == 1
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

    assert loop.result.outcome == Outcome.DONE
    assert loop.state.last_gate is None, "no mutations, no gate"
    assert "nothing was changed" in loop.result.summary


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


def test_turns_that_only_repeat_earlier_calls_end_the_run(planning_router: Router) -> None:
    """Counted as *turns that added nothing*, not as occurrences of one call.
    Two field runs died under the old rule on a third identical search."""
    repeat = calls(("search_repo", '{"pattern":"Routes"}'))
    loop, _client = build(
        planning_router, [repeat] * (MAX_STALLED_TURNS + 2), kind="question", max_turns=20
    )
    list(loop.run("where are the routes", intent=Intent.ASK))

    assert loop.result.outcome == Outcome.NO_PROGRESS
    assert loop.state.stalled_turns >= MAX_STALLED_TURNS


def test_reading_one_file_forever_is_answered_with_what_is_already_there(
    planning_router: Router, written
) -> None:
    reads = [
        calls(("read_file", json.dumps({"path": "handler/user.go", "start": i, "end": i + 1})))
        for i in range(1, MAX_READS + 3)
    ]
    loop, _client = build(
        planning_router, reads, kind="question", max_turns=MAX_READS + 5
    )
    events = list(loop.run("read the handler", intent=Intent.ASK))

    told = [
        e
        for e in events
        if e.type is EventType.TOOL_RESULT and "already read" in str(e.data.get("content", ""))
    ]
    assert told


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
    assert loop.result.outcome == Outcome.DONE
    assert "nothing was changed" in loop.result.summary


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
