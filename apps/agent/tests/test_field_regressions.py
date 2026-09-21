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
from dakcoder_agent.loop import AgentLoop, Intent, Outcome
from dakcoder_agent.modes import Mode
from dakcoder_agent.tools import control
from dakcoder_agent.tools.router import Router
from dakcoder_shared.envelope import EventType
from dakcoder_shared.llm import ChatResult, ToolCall, Usage

from scripted import (  # noqa: E402 - shared scripted model
    TERMINALS,
    ScriptedClient,
    build,
    calls,
    patch,
    plan_call,
    terminal_forces,
)

# Fixtures defined in `scripted` are re-exported here so pytest collects them.
from scripted import gated, planning_router  # noqa: F401,E402


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

    # The tail is permutations rather than fresh rewordings, and deliberately so.
    # Each genuinely new wording introduces a term the corpus has not scored yet
    # ("definition", "member", "attribute"), and every one of those can pull in a
    # section nothing had returned before -- which resets the repeat counter. That
    # made the fixture a hostage to corpus size: it reached three consecutive
    # repeats against a 92-section corpus and stopped reaching them on 2026-09-08,
    # when the migration SOP grew nine sections, without anything in the loop
    # changing. BM25 is a bag of words, so a permutation scores identically to the
    # phrasing above it and cannot reach a section that one did not -- while still
    # being a distinct string, so the duplicate-call guard lets it dispatch.
    # The run therefore exhausts the ladder on the mechanism under test, at any
    # corpus size.
    queries = [
        "api-server Router struct Engine field",
        "api-server Router struct Engine field definition",
        "api-server Router struct definition Engine field",
        "api-server Router Engine struct field def",
        "api-server Router type Engine member",
        "api-server Router Engine attribute",
        "Router Engine api-server attribute",
        "Engine attribute Router api-server",
        "attribute api-server Engine Router",
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


# ── a refused `finish` was cached, so its retry never dispatched ────────────


def test_a_repeated_finish_is_never_answered_from_the_cache(planning_router, gated):
    """The run that read a plan, planned an edit, and made none.

    Field transcript, 2026-09-09. The developer asked for four additions to a
    migration document. The Planner produced a one-step plan naming the file;
    the acting mode called `finish` without writing it; `_phase_ended` sent it
    back once, correctly. Then the model called `finish` again with the same
    answer -- which is exactly what the refusal asked it to do if it disagreed
    -- and `_intercept` answered it from the cache the first dispatch had
    written. It never reached `_phase_ended`, so `finish_refused` stayed at 1
    against a MAX_FINISH_REFUSALS of 1 and the escape was unreachable. Four
    turns of "that is the current answer. Use it and move to the next step"
    replied to a model whose next step was the exit it was being denied.

    Measured before the fix: 1 dispatch, 5 intercepts, 8 turns, NO_PROGRESS.
    """
    # Deliberately not a preamble: `_is_preamble` would send this back too, and
    # this test is about the cache, not the delivery.
    answer = json.dumps({"answer": "Validated. Step 3 names a file that does not exist."})
    loop, _ = build(
        planning_router,
        [plan_call()] + [calls(("finish", answer))] * 6,
        max_turns=10,
    )
    events = list(loop.run("update the doc"))

    finishes = [
        e for e in events
        if e.type is EventType.TOOL_RESULT and e.data.get("name") == "finish"
    ]
    assert finishes, "the run never called finish"
    assert not any(e.data.get("intercept") for e in finishes), (
        "a phase-ending call was answered from a ledger instead of dispatched"
    )
    # The bound in `_phase_ended` is what ends this, and it can only do that if
    # every retry reaches it. Two dispatches: the one that is refused and the
    # one that is honoured.
    assert len(finishes) == 2, f"expected refuse-then-honour, got {len(finishes)}"
    assert loop.result is not None and loop.result.turns < 5, (
        "the run did not stop promptly once the model asked to"
    )


def test_a_terminal_call_leaves_no_cache_entry(planning_router, gated):
    """Belt and braces for the above: nothing writes the entry in the first place."""
    answer = json.dumps({"answer": "Done."})
    loop, _ = build(planning_router, [plan_call(), calls(("finish", answer))], max_turns=6)
    list(loop.run("update the doc"))
    assert not loop.state.last_results, (
        "a terminal call was cached; the next ledger added here would deadlock again"
    )


# ── an edit needs anchor text, whatever the coverage ledger believes ────────


def _read(path: str, **span) -> ChatResult:
    return calls(("read_file", json.dumps({"path": path, **span})))


def test_the_acting_mode_may_re_read_a_file_it_was_sent_to_change(planning_router, gated):
    """Same transcript, the half that stopped the edit from being written.

    `patch_file` takes an `old` that must match the bytes on disk, so the model
    said what it needed and why -- "let me check the end of the file to find the
    right anchor" -- and `_re_reading` refused it because those lines were
    technically still in context, twenty turns and a phase switch back. Four
    refusals across the planning and acting phases; nothing was ever written.

    The narrower span is the point: it is a *different* call from the whole-file
    read, so the exact-repeat cache never sees it and only the coverage ledger
    can answer. That ledger returns no file content at all -- which is what
    leaves a model with nothing to anchor a patch on. The repeat cache is not
    part of this: it replays the text, so a model that asks twice still gets it.

    Coverage refusal only. The call-count backstop is untouched, which is what
    keeps this from buying unbounded turns.
    """
    loop, _ = build(
        planning_router,
        [_read("handler/user.go"), plan_call(), _read("handler/user.go", start=1, end=3)],
        max_turns=8,
    )
    events = list(loop.run("add the Routes method"))

    reads = [
        e for e in events
        if e.type is EventType.TOOL_RESULT and e.data.get("name") == "read_file"
    ]
    assert len(reads) == 2, f"the script did not get its two reads: {len(reads)}"
    # The first is ASK, before a plan exists. The second is the acting mode
    # asking for an anchor inside a file the plan sent it to change.
    assert not reads[1].data.get("intercept"), (
        "the acting mode was refused a re-read of a file the plan sent it to change"
    )


def test_a_file_outside_the_plan_is_still_refused(planning_router, gated):
    """The exemption is scoped to plan targets, and this is the other side of it.

    Identical shape to the test above, pointed at a file no step names. The
    coverage ledger must still answer it, or the fix has simply deleted the
    protection rather than narrowing it.
    """
    other = "bootstrap/bootstrapper.go"
    loop, _ = build(
        planning_router,
        [_read(other), plan_call(), _read(other, start=1, end=1)],
        max_turns=8,
    )
    events = list(loop.run("add the Routes method"))

    reads = [
        e for e in events
        if e.type is EventType.TOOL_RESULT and e.data.get("name") == "read_file"
    ]
    assert len(reads) == 2, f"the script did not get its two reads: {len(reads)}"
    assert reads[1].data.get("intercept") == "re_read", (
        "coverage refusal was dropped for a file the plan never mentions"
    )


# ── a fence must not turn a question into a migration ───────────────────────

#: Twelve searches that each find somewhere new, so every turn genuinely
#: informs the run and the *fence* ends the phase rather than the stall guard.
_DISTINCT = [
    "package domain", "package postgres", "package handler", "package request",
    "package bootstrap", "package main", "GetAll", "GetByID", "Routes",
    "CreateUserRequest", "FxRepo", "FirstName", "serial4", "owns SQL",
]


def _to_the_fence(n: int) -> list:
    return [calls(("search_repo", json.dumps({"pattern": p}))) for p in _DISTINCT[:n]]


def test_a_planner_at_the_fence_may_finish_instead_of_planning(planning_router, gated):
    """The run that read a migration document and wrote a migration.

    Field transcript, 2026-09-09. The developer asked the agent to *validate* a
    plan against the codebase. Twelve turns of reading later the fence fired,
    `_terminal_choice` named `submit_plan` alone, and `_fence_ask` said "submit
    the plan now; this turn accepts only `submit_plan`". The model still knew
    what it had been asked -- its next narration says so in words -- but the
    turn had one legal move, so it wrote an eight-step migration nobody had
    requested, and the loop then spent the rest of the session enforcing it.

    The fence knows the phase has to end. It does not know whether the task was
    work or a question. The model does, so it gets to say.
    """
    from dakcoder_agent.loop import MAX_RESEARCH_TURNS

    answer = json.dumps({"answer": "The plan is accurate except for step 3."})
    loop, client = build(
        planning_router,
        _to_the_fence(MAX_RESEARCH_TURNS) + [calls(("finish", answer))],
        max_turns=MAX_RESEARCH_TURNS + 4,
    )
    events = list(loop.run("validate MIGRATION_PLAN.md against the codebase",
                           intent=Intent.AGENT))

    forced = terminal_forces(client)
    assert forced, "the fence never ended the phase"
    assert set(forced[0]) == TERMINALS, (
        f"the turn that had to end the phase offered {forced[0]}"
    )
    # And the model took the exit that matched the task.
    assert not loop.state.plan, "a validation produced a plan"
    assert not loop.router.touched, "a validation changed a file"
    assert loop.result is not None and loop.result.outcome is Outcome.DONE, (
        loop.result.summary if loop.result else "no result"
    )
    said = [e.data["text"] for e in events if e.type is EventType.ASSISTANT]
    assert any("step 3" in t for t in said), "the answer never reached the developer"


def test_a_forced_plan_does_not_make_the_run_a_failure(planning_router, gated):
    """The other half: when the model *does* write a plan under the fence.

    The widened choice makes this rarer, not impossible -- `submit_plan` is
    still salient to a model that has been reading code, and it happened in
    1 live run in 10. What must not follow is the rest of the field failure:
    a NO_PROGRESS verdict on a plan the developer never asked for.

    The *pushes* still fire, deliberately. `_phase_ended` sends the first
    `finish` back once and the fence says "write them now", and both name
    `finish` as the way to decline -- so a question costs one turn and then
    delivers. Excusing those too was measured and reverted: it took change
    tasks from writing the code 7 times in 8 to once in 3. Only the verdict,
    which has nothing after it, reads `_open_targets`.
    """
    from dakcoder_agent.loop import MAX_RESEARCH_TURNS

    answer = json.dumps({"answer": "Validated. Steps 1-7 hold; step 8 is wrong."})
    loop, _ = build(
        planning_router,
        # One search past the fence, so the turn that must end the phase carries
        # a non-terminal call the narrowed request cannot accept. The stub then
        # answers with the first tool it *does* offer -- `submit_plan` -- which
        # is the field behaviour this test is about.
        _to_the_fence(MAX_RESEARCH_TURNS + 1) + [calls(("finish", answer))] * 3,
        max_turns=MAX_RESEARCH_TURNS + 6,
    )
    list(loop.run("validate the migration plan", intent=Intent.AGENT))

    assert loop.state.plan, "this test is about a plan that did get written"
    assert loop.state.plan_forced, "the plan came off a forced turn and was not marked"
    assert not loop._open_targets(), (
        "the verdict is still holding a question to a plan nobody asked for"
    )
    # Pushed once, and once only -- then believed.
    refusals = [
        m.content for m in loop.context.build()
        if str(m.role) == "user" and "Not yet" in m.content
    ]
    assert len(refusals) <= 1, f"the answer was refused more than once: {len(refusals)}"
    assert loop.result is not None and loop.result.outcome is not Outcome.NO_PROGRESS, (
        loop.result.summary
    )


def test_a_volunteered_plan_is_still_a_commitment(planning_router, gated):
    """The guard is scoped to forced plans, and this is the other side of it."""
    answer = json.dumps({"answer": "Nothing to do."})
    loop, _ = build(
        planning_router, [plan_call()] + [calls(("finish", answer))] * 3, max_turns=10
    )
    list(loop.run("add the Routes method", intent=Intent.AGENT))

    assert loop.state.plan and not loop.state.plan_forced
    assert loop._open_targets() == ["handler/user.go"]
    assert loop.result is not None and loop.result.outcome is Outcome.NO_PROGRESS, (
        "a plan the model volunteered and abandoned is not a run that went well"
    )
    refusals = [
        m.content for m in loop.context.build()
        if str(m.role) == "user" and "Not yet" in m.content
    ]
    assert refusals, "a plan the model volunteered was abandoned without challenge"


def test_a_forced_plan_stops_excusing_the_run_once_anything_is_written(
    planning_router, gated
):
    """A run that has started acting on a plan is working to it, whatever
    produced it -- and half-finished work is what these paths exist to catch."""
    loop, _ = build(planning_router, [plan_call()], max_turns=6)
    list(loop.run("add the Routes method", intent=Intent.AGENT))
    loop.state.plan_forced = True

    assert not loop._open_targets(), "nothing written yet, so nothing is owed"
    loop.router.touched.append("core/domain/user.go")
    assert loop._open_targets() == ["handler/user.go"], (
        "the run started the work and is still answerable for finishing it"
    )


# ── an answer that is only its own opening line ─────────────────────────────


def _finish(answer: str):
    return calls(("finish", json.dumps({"answer": answer})))


PREAMBLE = (
    "I have validated the migration plan against the actual codebase. "
    "Here is my assessment of each step's accuracy:"
)


def test_an_answer_that_is_only_its_opening_line_is_sent_back(planning_router, gated):
    """Twenty turns of work, delivered as a colon.

    Two field runs, three days apart, ended the same way: the model did the
    reading, called `finish`, and put only the sentence that introduces the
    findings into `answer`. The developer's next message in the first was
    "where is the assessment". `answer` is the whole delivery -- there is no
    prose after it the way there is in a chat -- and nothing said so.
    """
    full = "Step 1 is accurate. Step 2 misses that repositories take *gin.Context."
    loop, _ = build(planning_router, [_finish(PREAMBLE), _finish(full)], max_turns=8)
    events = list(loop.run("validate the plan", intent=Intent.ASK))

    sent_back = [
        m.content for m in loop.context.build()
        if str(m.role) == "user" and "opening of something longer" in m.content
    ]
    assert len(sent_back) == 1, "the preamble was not sent back exactly once"
    said = [e.data["text"] for e in events if e.type is EventType.ASSISTANT]
    assert any("gin.Context" in t for t in said), "the real answer never arrived"
    assert loop.result is not None and loop.result.outcome is Outcome.DONE


def test_the_same_answer_sent_again_is_taken_as_final(planning_router, gated):
    """The bounce has to be cheap to reject, or it argues with a model that is right.

    A short answer that trips the detector and *is* the whole answer costs one
    turn: the message says so, and the second identical call is honoured.
    """
    loop, _ = build(planning_router, [_finish(PREAMBLE), _finish(PREAMBLE)], max_turns=8)
    events = list(loop.run("validate the plan", intent=Intent.ASK))

    assert loop.state.preamble_refused == 1, "the bound is one, not a loop"
    said = [e.data["text"] for e in events if e.type is EventType.ASSISTANT]
    assert any(t.startswith("I have validated") for t in said), (
        "the answer was refused twice and never reached the developer"
    )
    assert loop.result is not None and loop.result.outcome is Outcome.DONE


def test_a_short_answer_from_a_run_that_wrote_something_is_not_bounced(
    planning_router, gated
):
    """"Added the Routes method." is a complete answer to a completed edit."""
    loop, _ = build(
        planning_router,
        [plan_call(), patch(), _finish("Here is what changed: handler/user.go.")],
        max_turns=10,
    )
    list(loop.run("add the Routes method", intent=Intent.AGENT))
    assert loop.state.preamble_refused == 0, (
        "a run that wrote a file had its one-line report challenged"
    )


def _evicted_reads(count: int) -> list:
    """A working set of ``count`` read turns, as compaction hands them over."""
    from dakcoder_agent.context import Layer, Message, Role

    out: list = []
    for i in range(count):
        path = f"handler/file{i:03d}.go"
        call = ToolCall(id=f"r{i}", name="read_file", arguments=json.dumps({"path": path}))
        out.append(Message(Role.ASSISTANT, "", Layer.WORKING_SET, tool_calls=(call,), turn=i + 1))
        out.append(
            Message(Role.TOOL, f"{path} (40 lines)\n" + "x" * 400, Layer.WORKING_SET,
                    path=path, tool_call_id=f"r{i}", turn=i + 1)
        )
    return out


# ── a compaction must not throw away what an answering run has found ────────


def test_a_recap_carries_what_the_run_established_not_just_what_it_read(
    planning_router,
):
    """The compaction that made a validation re-read seven files to remember it.

    Field transcript, 2026-09-09: "validate MIGRATION_PLAN.md against the
    codebase, every file". Fifteen files, several over a thousand lines, 189k
    tokens by turn 10. The recap's vocabulary was all about *doing* -- decisions
    taken, files modified, steps verified -- and a run whose work is reading has
    none of those, so nine turns of analysis compacted into a list of filenames.
    Turns 11 to 13 re-read seven of them.
    """
    from dakcoder_agent.loop import _RECAP_PROMPT, _RECAP_SCHEMA

    assert "findings" in _RECAP_SCHEMA["json_schema"]["schema"]["properties"]
    assert "findings" in _RECAP_PROMPT, "the summariser is never asked for them"

    loop, _ = build(planning_router, [])

    class Answering(ScriptedClient):
        """A summariser that reports findings, as the schema now allows."""

        def chat(self, messages, *, response_format=None, **kwargs):
            if (response_format or {}).get("json_schema", {}).get("name") == "recap":
                return ChatResult(
                    content=json.dumps({
                        "goal": "validate the migration plan",
                        "findings": [
                            "handler/objection.go: all 14 handlers take *gin.Context",
                            "go.mod: gin and volatiletech/null are both still required",
                        ],
                    }),
                    finish_reason="stop",
                    usage=Usage(prompt_tokens=10),
                )
            return super().chat(messages, response_format=response_format, **kwargs)

    loop.client = Answering([], kind="question")
    recap = loop._summarise(_evicted_reads(4))

    assert recap.findings, "the summariser reported findings and the recap dropped them"
    assert any("gin.Context" in f for f in recap.findings)
    # And they reach the model, which is the only reason to keep them.
    assert "gin.Context" in recap.markdown()
    assert "Findings" in recap.markdown()


def test_findings_survive_a_second_compaction(planning_router):
    """Long runs are exactly the runs that compact twice, and the merge is what
    carried `do_not_retry` across the second one. Findings need the same."""
    from dakcoder_agent.context import Recap

    first = Recap(goal="validate", findings=("step 1 holds",))
    second = Recap(goal="validate", findings=("step 8 is wrong",))

    merged = second.merge(first)
    assert merged.findings == ("step 1 holds", "step 8 is wrong"), merged.findings


# ── the answer is not the tool result ───────────────────────────────────────


def test_finish_does_not_echo_the_answer_back_into_the_transcript(planning_router):
    """An 8k answer used to land in the transcript twice, then be carried into
    the next message of the session as a worked example of what to say."""
    long_answer = ("The plan is accurate. " + "Detail. " * 400).strip()
    out = planning_router.dispatch("finish", {"answer": long_answer}, mode=Mode.ASK)

    assert out.ok
    assert out.meta["answer"] == long_answer, "the developer's copy must be whole"
    assert len(out.content) < 200, f"the tool result still carries the answer: {len(out.content)}"
    assert "Detail." not in out.content


# ── the decision the run turns on, named on the wire ────────────────────────


def test_a_classified_intent_says_so_and_says_why(planning_router, gated):
    """Twenty turns into an unrequested migration is a late moment to learn
    that a 64-token call decided this was work.

    `_INTENT_SCHEMA` has asked for `why` since the classifier was written and
    the reply went straight in the bin, so the one artefact that could explain
    a misroute never existed. Both facts now ride on every `turn_start`.
    """
    loop, _ = build(planning_router, [calls(("finish", json.dumps({"answer": "x"})))],
                    kind="question", max_turns=4)
    starts = [e for e in loop.run("what does the User model hold?")
              if e.type is EventType.TURN_START]

    assert starts, "no turn was started"
    assert starts[0].data["intent"] == "ask"
    assert starts[0].data["intent_source"] == "classified"
    assert starts[0].data["intent_why"] == "scripted: question", starts[0].data
    assert loop.state.intent_why == "scripted: question"


def test_an_intent_the_developer_gave_is_not_reported_as_a_guess(
    planning_router, gated
):
    """The panel's Ask/Agent toggle is a statement, not a classification, and a
    panel that offers "treating this as work -- switch?" must not offer it to
    someone who just said so."""
    loop, _ = build(planning_router, [calls(("finish", json.dumps({"answer": "x"})))],
                    max_turns=4)
    starts = [e for e in loop.run("tell me about the model", intent=Intent.ASK)
              if e.type is EventType.TURN_START]

    assert starts[0].data["intent_source"] == "given"
    assert starts[0].data["intent_why"] == "", "nothing guessed, so nothing to explain"


# ── the plan on the wire, not a count and some prose ────────────────────────


def test_the_plan_event_carries_the_steps_it_was_given(planning_router, gated):
    """The panel listed two files the run would never touch and hid the one it would.

    `submit_plan` validates that every step names a file, an action and an
    acceptance criterion, and `_normalise_plan` puts each path into the form the
    change set uses. The event then threw all of that away and sent
    `{text, steps: 1}`, leaving the panel to recover it with a regex over the
    rendered prose -- one that matched path-shaped tokens anywhere in a step and
    only knew Go, SQL and YAML.

    On the 2026-09-09 plan, whose one step targeted `MIGRATION_PLAN.md` and
    whose description mentioned two Go files as examples, that produced
    "Files in scope: handler/response.go, helper.go" and no mention of the file
    being written. `_unfinished` had this same bug server-side and fixed it; the
    UI kept the old version (BUG EXT-19).
    """
    loop, _ = build(planning_router, [plan_call()], max_turns=4)
    events = list(loop.run("add the Routes method", intent=Intent.AGENT))

    plans = [e for e in events if e.type is EventType.PLAN]
    assert plans, "no plan was announced"
    items = plans[0].data.get("items")
    assert items, "the plan event still carries only a count and prose"
    assert [i["file"] for i in items] == ["handler/user.go"]
    assert items[0]["action"] and items[0]["accepts"], items[0]
    assert items[0]["status"] == "pending"
    assert items[0]["index"] == 1
    # And the count still agrees with the list, because the panel renders both.
    assert plans[0].data["steps"] == len(items)


def test_a_step_reports_done_from_the_change_set(planning_router, gated):
    """The status the panel shows is derived from a write landing, never from
    the model saying so -- which is the whole reason it is worth sending."""
    loop, _ = build(
        planning_router,
        [plan_call(), patch(), calls(("finish", json.dumps({"answer": "Added it."})))],
        max_turns=8,
    )
    events = list(loop.run("add the Routes method", intent=Intent.AGENT))

    assert "handler/user.go" in loop.router.touched
    assert [s.status for s in loop.state.plan] == ["done"]
    # The last plan event the panel saw carries that status.
    plans = [e for e in events if e.type is EventType.PLAN]
    if len(plans) > 1:
        assert plans[-1].data["items"][0]["status"] == "done"
