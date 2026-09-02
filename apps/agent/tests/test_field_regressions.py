"""Regressions from the field, each one a run that actually happened.

Every test here is a probe that FAILED against the commit before its fix, and
the failing measurement is recorded in the docstring. They are kept apart from
``test_loop.py`` because they are not tests of a unit: each drives the real
``AgentLoop`` end to end and asserts the one decision that went wrong.

Four incidents:

* An explanation was pinned as a plan and handed to the Coder (32 turns, 0
  files). Two independent causes -- ``_PLAN_EDITS`` matching the third-person
  forms of its edit verbs, and ``_ASKS_TO_BE_TOLD`` missing half the ways a
  developer asks to be told something.
* A mode refusal cached under a mode-blind fingerprint was replayed to the mode
  that could run the call, so the Coder was handed "not available in verifier
  mode" as the result of its own patch (17 turns, 0 files).
* Three paths orphaned a ``tool_call_id``, which is malformed against a strict
  endpoint and never heals -- ``loopback.follow_up`` carries the poisoned
  ContextManager into every later run in the session.
* ``_ACCEPTS`` was bound twice, so the live pattern was not the one anybody was
  reading, and an ordinary eight-space ``- Accepts:`` stopped matching.
"""

from __future__ import annotations

import ast
import json
from collections.abc import Sequence
from pathlib import Path

import pytest

from dakcoder_agent.context import ContextManager, Recap
from dakcoder_agent.loop import AgentLoop, Intent
from dakcoder_agent.modes import Mode
from dakcoder_agent.tools import control
from dakcoder_agent.tools.router import Router
from dakcoder_shared.envelope import EventType
from dakcoder_shared.llm import ChatResult, ToolCall, Usage


def say(text: str) -> ChatResult:
    return ChatResult(content=text, finish_reason="stop", usage=Usage(prompt_tokens=100))


def calls_json(name: str, arguments: dict) -> ChatResult:
    return ChatResult(
        tool_calls=[ToolCall(id="chatcmpl-tool-00", name=name, arguments=json.dumps(arguments))],
        finish_reason="tool_calls",
        usage=Usage(prompt_tokens=100),
    )


class Once:
    """Replies with a script, then with distinct filler so nothing loops.

    ``kind`` is what the intent classifier answers. It arrives as a separate
    call carrying a ``response_format``, so it never consumes a scripted turn --
    a test's script lines up with the turns it is actually about.
    """

    def __init__(self, turns: Sequence[ChatResult], *, kind: str = "change") -> None:
        self.turns = list(turns)
        self.n = 0
        self.kind = kind
        #: Every prompt the classifier was sent, for the tests that assert on it.
        self.classified: list[str] = []

    def chat(self, messages, *, tools=None, response_format=None, **kwargs) -> ChatResult:
        if response_format is not None:
            if response_format.get("json_schema", {}).get("name") == "intent":
                self.classified.append(messages[-1]["content"])
                return say(json.dumps({"kind": self.kind}))
            return say(json.dumps({"goal": "scripted"}))
        self.n += 1
        return self.turns.pop(0) if self.turns else say(f"nothing further ({self.n})")


def drive(client, task: str, router: Router, *, max_turns: int = 12, intent=Intent.AUTO):
    loop = AgentLoop(
        ContextManager(mode=Mode.ASK, system_prompt="You are dakcoder."),
        client,
        router,
        approve=lambda _r: True,
        max_turns=max_turns,
    )
    events = list(loop.run(task, intent=intent))
    modes = sorted({e.data["mode"] for e in events if e.type is EventType.TURN_START})
    return modes, events


# -- a question is answered; work is executed --------------------------------
#
# These used to assert on ~500 lines of regex over the task and the reply, and
# the report measured what that was worth: 17 of 24 realistic read-only prompts
# were classified as work, and each of those ran the full gate on an untouched
# workspace and entered the escalation ladder.
#
# The classification is a model call now, so the corpus below no longer tests
# *this* code -- it documents the phrasings the regex got wrong, and what is
# asserted is the half the code still owns: given a classification, does the run
# do the right thing, and does the classifier get what it needs to decide.

#: Read-only phrasings a developer types every day. The report measured the
#: regex classifying most of these as work.
READ_ONLY_TASKS = [
    "explain the bootstrapper and tell me how it deviates from the new template",
    "explain me this project",
    "what all have been done in this repo",
    "give me an overview of the repo",
    "analyse the objection handler",
    "review the bootstrapper",
    "explain what the build does",
    "does the objection handler follow the template?",
    "list the routes in this service",
    "which files would I need to change to add a status filter?",
    "is this handler correct?",
    "check if go mod tidy is clean",
]

#: Requests that must still be executed. The guard against over-correcting: a
#: classifier that answers everything is not a fix.
WORK_TASKS = [
    "write a new api that will store employee details, create everything required",
    "add employee crud",
    "write unit tests for the objection handler",
    "fix the vet errors",
    "review the objection handler and fix compilation errors",
    "explain the bootstrapper, then migrate it to the new template",
    "create employee table sql scripts",
    "implement pagination on the list endpoint",
]

#: The shape a model reaches for when asked to describe wiring code. Every
#: paragraph opens with a third-person verb, which is what `_PLAN_EDITS`
#: matched -- so the answer to a question was pinned as a plan and executed.
VERB_FIRST_ANSWER = """Here is what each module does.

1. Creates the Temporal client and the worker on the PAO task queue.
2. Registers the transfer-entry verification workflow and its activity.
3. Wires the start and stop lifecycle hooks onto the fx lifecycle.
4. Updates the health endpoint to report the worker.
"""

BOLD_ANSWER = """## What the bootstrapper does

**1. `Fxvalidator`** - invokes `handler.NewValidatorService`.

**2. `FxRepo`** - provides all nine repositories as plain constructors.

**3. `FxHandler`** - provides all eight handlers.
"""


@pytest.mark.parametrize("task", READ_ONLY_TASKS)
@pytest.mark.parametrize("answer", [VERB_FIRST_ANSWER, BOLD_ANSWER], ids=["verb-first", "bold"])
def test_a_question_is_answered_whatever_its_answer_looks_like(task, answer, router: Router):
    """The reply's shape must not be able to turn an answer into a plan.

    This is the failure the report calls unfixable by wording: "a description of
    a deviation is indistinguishable from a proposal to remove it". The old loop
    ran `_PLAN_EDITS` against the *reply*, matched "Creates"/"Registers"/
    "Wires", pinned the answer as a plan, and went off to migrate a hundred
    routes nobody had asked it to touch.

    Nothing reads the reply now. A question runs one read-only loop and stops.
    """
    modes, _events = drive(Once([say(answer)], kind="question"), task, router)
    assert modes == ["ask"], f"{task!r} reached {modes}"
    assert not router.touched


@pytest.mark.parametrize("task", WORK_TASKS)
def test_a_request_for_work_reaches_the_acting_mode(task, router: Router):
    """The other half of the same judgement, and the one easy to lose.

    A false "question" costs the developer one word; a false "change" costs
    unrequested edits found later in a diff.
    """
    router.handlers.update(control.HANDLERS)
    plan = calls_json(
        "submit_plan",
        {
            "steps": [
                {
                    "file": "core/domain/employee.go",
                    "action": "add the Employee struct",
                    "accepts": "go build passes",
                }
            ]
        },
    )
    modes, _events = drive(Once([plan], kind="change"), task, router)
    assert "agent" in modes, f"{task!r} was answered instead of executed ({modes})"


def test_the_classifier_is_given_the_conversation_as_well_as_the_message(
    router: Router,
) -> None:
    """"go" is a question about nothing and an instruction about whatever was
    just described, so it cannot be classified from the message alone.

    The old loop had `_SAYS_GO` for this: a regex over pinned directives, whose
    own comment concedes that one false match "authorises writes for every later
    question in that session" -- a session-scoped write authorisation from a
    one-word pattern match.
    """
    client = Once([say("answered")], kind="question")
    loop = AgentLoop(
        ContextManager(mode=Mode.ASK, system_prompt="s"), client, router, max_turns=4
    )
    list(loop.run("explain the bootstrapper", intent=Intent.AUTO))
    list(loop.run("go", intent=Intent.AUTO, continued=True))

    assert len(client.classified) == 2
    assert "explain the bootstrapper" in client.classified[1], (
        "the follow-up was classified without the conversation it follows"
    )


# ── a mode refusal is not an answer for the mode that can run the call ──────


def test_a_mode_refusal_is_not_replayed_to_the_mode_that_can_run_it(router: Router, tmp_path):
    """The Part I deadlock, reduced to two dispatches.

    Before: the Coder's `patch_file` was answered from `last_results` with the
    Verifier's refusal -- events ['tool_result'] with no 'tool_call', 0
    mutations. The model then said "I'm in verifier mode, so I cannot apply the
    fix", which was a faithful reading of its own tool output, and the run died
    17 turns later having changed nothing.
    """
    target = Path(router.workspace.root) / "handler" / "user.go"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("package handler\n\nfunc New() {}\n", encoding="utf-8")
    args = json.dumps({"path": "handler/user.go", "old": "func New()", "new": "func New2()"})

    loop = AgentLoop(
        ContextManager(mode=Mode.PLANNER, system_prompt="sys"),
        Once([]),
        router,
        approve=lambda _r: True,
    )

    loop._switch(Mode.ASK)
    refused = list(loop._tool_calls([ToolCall(id="p1", name="patch_file", arguments=args)]))
    result = [e for e in refused if e.type is EventType.TOOL_RESULT][-1]
    assert result.data["ok"] is False
    assert "not available in ask mode" in result.data["content"]

    loop._switch(Mode.AGENT)
    ran = list(loop._tool_calls([ToolCall(id="p2", name="patch_file", arguments=args)]))
    kinds = [e.type for e in ran]
    assert EventType.TOOL_CALL in kinds, "the Coder's call was answered from the ledger"
    assert router.mutations == 1
    assert "func New2()" in target.read_text(encoding="utf-8")


def test_a_mutating_tool_refused_by_mode_is_not_offered_another_write_tool(router: Router):
    """`spec.instead` is right for unavailable and wrong for refused-by-mode.

    Before: `patch_file` refused in Verifier mode answered "Instead, use
    write_file to create a file that does not exist yet" -- the other write
    tool, which that mode also cannot use. The field model followed it and spent
    three turns truncating a 280-line `write_file` against a 2,048-token budget.
    """
    outcome = router.dispatch("patch_file", {"path": "a.go", "old": "x", "new": "y"},
                              mode=Mode.ASK)
    assert outcome.ok is False
    assert outcome.meta.get("refused_by_mode") is True
    assert "write_file" not in outcome.for_model()


# ── the wire invariant: every declared call is answered, and vice versa ─────


def wire_faults(context: ContextManager) -> list[str]:
    """Both directions of the correlation the OpenAI shape requires.

    An assistant message carrying ``tool_calls`` must be followed by one
    ``role:"tool"`` message per ``tool_call_id``, and a result must name a call
    something declared. Either way round is malformed, and ``Message.wire``'s
    docstring records what it costs: one poisoned message 400s every later
    request in the session, "including a plain 'hi'".

    Messages with *no* ``tool_call_id`` are skipped deliberately. The loop
    injects its nudges through ``append_tool_result`` without one -- twelve of
    its seventeen call sites -- so they are ``role:"tool"`` with no correlation
    at all. That is long-standing and out of scope here; this asserts only that
    calls and their answers line up.
    """
    declared: set[str] = set()
    answered: set[str] = set()
    bad: list[str] = []
    for message in context.wire():
        for call in message.get("tool_calls") or ():
            declared.add(call["id"])
        if message.get("role") == "tool" and message.get("tool_call_id"):
            answered.add(message["tool_call_id"])
            if message["tool_call_id"] not in declared:
                bad.append(f"result-with-no-call:{message['tool_call_id']}")
    return bad + [f"call-with-no-result:{i}" for i in sorted(declared - answered)]


def test_a_reply_cut_off_mid_call_still_answers_its_other_calls(router: Router):
    """Before: declared ['good1','cut1'], answered ['cut1'], orphaned ['good1'].

    `incomplete_tool_calls` returns a list and the branch answered `[0]`, so
    every other call in the same reply was left with no tool message.
    """
    mixed = ChatResult(
        tool_calls=[
            ToolCall(id="good1", name="read_file", arguments=json.dumps({"path": "handler/user.go"})),
            ToolCall(id="cut1", name="write_file", arguments='{"path": "a.go", "content": "pack'),
        ],
        finish_reason="length",
        usage=Usage(prompt_tokens=100),
    )
    context = ContextManager(mode=Mode.PLANNER, system_prompt="sys")
    loop = AgentLoop(context, Once([say("1. Edit handler/user.go\n   Accepts: builds"), mixed]),
                     router, approve=lambda _r: True, max_turns=6)
    list(loop.run("edit handler/user.go"))
    assert wire_faults(context) == []


def test_stopping_mid_batch_still_answers_the_calls_it_abandoned(router: Router):
    """Before: a three-call batch cancelled after the first orphaned r1, r2, r3.

    An aborted session is `Status.resumable`, so the malformed transcript was
    carried into the resume and every request made from it.
    """
    batch = ChatResult(
        tool_calls=[
            ToolCall(id=f"r{i}", name="read_file", arguments=json.dumps({"path": "handler/user.go"}))
            for i in (1, 2, 3)
        ],
        finish_reason="tool_calls",
        usage=Usage(prompt_tokens=100),
    )
    context = ContextManager(mode=Mode.PLANNER, system_prompt="sys")
    seen = {"n": 0}

    def cancelled() -> bool:
        seen["n"] += 1
        return seen["n"] > 2

    loop = AgentLoop(context, Once([say("1. Edit handler/user.go\n   Accepts: builds"), batch]),
                     router, approve=lambda _r: True, max_turns=6, cancelled=cancelled)
    list(loop.run("edit handler/user.go"))
    assert wire_faults(context) == []


@pytest.mark.parametrize("assistant_chars", [2_000, 8_000, 20_000])
@pytest.mark.parametrize("result_chars", [2_000, 8_000, 20_000])
def test_compaction_never_cuts_between_a_call_and_its_result(assistant_chars, result_chars):
    """Before: 7 of 16 size combinations left the retained set starting on an orphan.

    `_retention_cut` budgets in tokens and knows nothing about roles, so the
    index it returned landed wherever the allowance ran out -- including between
    an assistant carrying `tool_calls` and the results answering them.
    """
    context = ContextManager(mode=Mode.PLANNER, system_prompt="sys")
    for i in range(14):
        context.begin_turn()
        context.append_assistant(
            "x" * assistant_chars,
            tool_calls=(ToolCall(id=f"t{i}", name="read_file", arguments="{}"),),
        )
        context.append_tool_result("read_file", "y" * result_chars, tool_call_id=f"t{i}")
    context.compact(lambda evicted: Recap(turns=(0, 14)), retain_pct=0.15)
    assert wire_faults(context) == []


# ── one binding, and an indent a real plan actually uses ────────────────────


def test_loop_binds_every_module_level_name_once():
    """`_ACCEPTS` was bound twice; Python took the second, so the pattern anyone
    read was not the pattern that ran. ruff is not installed in every dev
    environment, so the check lives here rather than in a lint config.
    """
    source = Path(__import__("dakcoder_agent.loop", fromlist=["loop"]).__file__)
    tree = ast.parse(source.read_text(encoding="utf-8"))
    seen: dict[str, list[int]] = {}
    for node in tree.body:
        for target in node.targets if isinstance(node, ast.Assign) else ():
            if isinstance(target, ast.Name):
                seen.setdefault(target.id, []).append(node.lineno)
    assert {k: v for k, v in seen.items() if len(v) > 1} == {}


# ── the corpus is asked once, not sixteen times ─────────────────────────────


def test_a_search_that_returns_nothing_new_says_so_and_is_eventually_withdrawn(router: Router):
    """Transcript B: sixteen Coder turns of `search_docs`, 0 files written.

    Every query was worded differently, so every fingerprint was new, so every
    call dispatched and reset `stalled_turns` to zero. Three of them returned
    the same four sections -- the same 196 lines at turns 21, 22 and 23 -- and
    nothing said so.

    A relevance floor cannot fix this. Measured against the real 92-section
    corpus, the query the run died on ("api-server Router struct Engine field")
    scores 28.141, higher than every question the corpus genuinely answers
    ("how do I add a new endpoint" scores 5.330), because it is built from words
    the corpus uses constantly. Term coverage fails the same way: all five of
    its words are in the vocabulary. The reliable signal is the answer repeating,
    not the question scoring.
    """
    from dakcoder_agent.tools import knowledge

    queries = [
        "api-server Router struct Engine field",
        "api-server Router struct Engine field definition",
        "api-server Router struct definition Engine field",
        "api-server Router Engine struct field def",
        "api-server Router type Engine member",
        "api-server Router Engine attribute",
    ]
    handlers = {**router.handlers, **knowledge.handlers_for()}
    searching = Router(router.workspace, handlers)

    class Rephraser:
        """A model that keeps rewording one question the corpus cannot answer."""

        def __init__(self) -> None:
            self.n = 0
            self.offered: list[bool] = []

        def chat(self, messages, *, tools=None, response_format=None, **kwargs):
            if response_format is not None:
                return say(json.dumps({"kind": "question"}))
            self.n += 1
            names = {t["function"]["name"] for t in (tools or [])}
            self.offered.append("search_docs" in names)
            if "search_docs" not in names:
                return say(f"nothing further ({self.n})")
            return ChatResult(
                tool_calls=[
                    ToolCall(
                        id=f"s{self.n}",
                        name="search_docs",
                        arguments=json.dumps(
                            {"query": queries[min(self.n - 1, len(queries) - 1)]}
                        ),
                    )
                ],
                finish_reason="tool_calls",
                usage=Usage(prompt_tokens=100),
            )

    client = Rephraser()
    context = ContextManager(mode=Mode.ASK, system_prompt="sys")
    loop = AgentLoop(context, client, searching, approve=lambda _r: True, max_turns=20)
    list(loop.run("how do repository timeouts work", intent=Intent.ASK))

    # A `role: user` message, not a fabricated `role: tool` one. The old loop
    # appended this as a result attributed to `search_docs` with no
    # `tool_call_id` -- malformed on the wire, and a lie in the transcript that
    # teaches the model `search_docs` replies with advice about itself.
    told = [
        m.content
        for m in context.build()
        if str(m.role) == "user"
        and ("same sections" in m.content or "does not cover" in m.content)
    ]
    assert told, "the run was never told it was getting the same sections back"
    assert any("does not cover" in t for t in told), "the corpus was never declared exhausted"
    assert False in client.offered, "search_docs was never withdrawn"


# ── a compound request is work, and a conjoined noun phrase is not ──────────
