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
from dakcoder_agent.loop import _ACCEPTS, AgentLoop
from dakcoder_agent.modes import Mode
from dakcoder_agent.tools.router import Router
from dakcoder_shared.envelope import EventType
from dakcoder_shared.llm import ChatResult, ToolCall, Usage


def say(text: str) -> ChatResult:
    return ChatResult(content=text, finish_reason="stop", usage=Usage(prompt_tokens=100))


class Once:
    """Replies with a script, then with distinct filler so nothing loops."""

    def __init__(self, turns: Sequence[ChatResult]) -> None:
        self.turns = list(turns)
        self.n = 0

    def chat(self, messages, *, tools=None, **kwargs) -> ChatResult:
        self.n += 1
        return self.turns.pop(0) if self.turns else say(f"nothing further ({self.n})")


def drive(client, task: str, router: Router, *, max_turns: int = 12):
    loop = AgentLoop(
        ContextManager(mode=Mode.PLANNER, system_prompt="You are dakcoder."),
        client,
        router,
        approve=lambda _r: True,
        max_turns=max_turns,
    )
    events = list(loop.run(task))
    modes = sorted({e.data["mode"] for e in events if e.type is EventType.TURN_START})
    return modes, events


# ── an explanation must never reach the Coder ───────────────────────────────

#: The shape the Planner reaches for when asked to describe wiring code. Every
#: paragraph opens with a third-person verb, which is what `_PLAN_EDITS` matched.
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

READ_ONLY_TASKS = [
    "explain the bootstrapper and tell me how it deviates from the new template",
    "explain me this project",
    "what all have been done in this repo",
    "give me an overview of the repo",
    "analyse the objection handler",
    "review the bootstrapper",
    "explain what the build does",
    "walk me through the transfer entry flow",
    "summarize the repo structure",
]


@pytest.mark.parametrize("task", READ_ONLY_TASKS)
@pytest.mark.parametrize("answer", [VERB_FIRST_ANSWER, BOLD_ANSWER], ids=["verb-first", "bold"])
def test_a_question_is_answered_and_never_executed(task, answer, router: Router):
    """A read-only request must terminate in the Planner.

    Before: 13 of these 18 combinations reached the Coder. The verb-first answer
    failed for *every* task, because `_PLAN_EDITS` had no trailing word boundary
    and matched "Creates"/"Registers"/"Wires"/"Updates". The Coder then had
    nothing to execute, `_verify` ran the gate on an untouched workspace, and in
    one field transcript the run spent 22 further turns on a pre-existing defect
    in a file the task never mentioned.
    """
    modes, _ = drive(Once([say(answer)]), task, router)
    assert modes == ["planner"], f"{task!r} reached {modes}"
    assert not router.touched


#: Requests that must still be executed. The guard against over-correcting: an
#: adversarial review of the first attempt at this fix measured 14 of 20
#: compound requests flipping from executed to answered.
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


@pytest.mark.parametrize("task", WORK_TASKS)
def test_a_request_for_work_still_reaches_the_coder(task, router: Router):
    """The other half of the same judgement, and the one easy to lose.

    A false "read-only" costs the developer one word ("go"); a false "work"
    costs unrequested edits. But a classifier that answers everything is not a
    fix, so both directions are asserted together.
    """
    plan = (
        "1. `core/domain/employee.go` - add the Employee struct.\n"
        "   Accepts: go build passes.\n"
        "2. `repo/postgres/employee.go` - add the repository.\n"
        "   Accepts: go build passes.\n"
    )
    modes, _ = drive(Once([say(plan)]), task, router)
    assert "coder" in modes, f"{task!r} was answered instead of executed ({modes})"


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

    loop._switch(Mode.VERIFIER)
    refused = list(loop._tool_calls([ToolCall(id="p1", name="patch_file", arguments=args)]))
    result = [e for e in refused if e.type is EventType.TOOL_RESULT][-1]
    assert result.data["ok"] is False
    assert "not available in verifier mode" in result.data["content"]

    loop._switch(Mode.CODER)
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
                              mode=Mode.VERIFIER)
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


@pytest.mark.parametrize(
    ("line", "matches"),
    [
        ("  - Accepts: build passes", True),
        ("        - Accepts: nested under a numbered step", True),
        ("   > - Accepts: quoted", True),
        ("- [ ] Accepts: a checkbox list", True),
        ("1. Accepts: a bare number", True),
        ("**Accepts:** bold", True),
        ("\t\tAccepts: tabs", True),
        # Not a plan signal. `_ACCEPTS` is the whole reply-side test in
        # `_is_explanation`, so a table row would pin an explanation that
        # documents the step format and hand it to the Coder.
        ("| Accepts: | the criterion |", False),
        ("Accepts are discussed in the section below", False),
        ("the plan Accepts: inline prose", False),
    ],
)
def test_the_accepts_line_matches_what_a_planner_writes(line, matches):
    assert bool(_ACCEPTS.search(line)) is matches


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
        def __init__(self) -> None:
            self.n = 0
            self.offered: list[bool] = []

        def chat(self, messages, *, tools=None, **kwargs):
            self.n += 1
            names = {t["function"]["name"] for t in (tools or [])}
            if self.n == 1:
                return say("1. `handler/user.go` - add the handler.\n   Accepts: builds")
            self.offered.append("search_docs" in names)
            if "search_docs" not in names:
                return say(f"nothing further ({self.n})")
            return ChatResult(
                tool_calls=[ToolCall(
                    id=f"s{self.n}", name="search_docs",
                    arguments=json.dumps({"query": queries[min(self.n - 2, len(queries) - 1)]}),
                )],
                finish_reason="tool_calls",
                usage=Usage(prompt_tokens=100),
            )

    client = Rephraser()
    context = ContextManager(mode=Mode.PLANNER, system_prompt="sys")
    loop = AgentLoop(context, client, searching, approve=lambda _r: True, max_turns=20)
    list(loop.run("write a new api that stores employee details"))

    told = [
        m.content for m in context.build()
        if m.source == "tool:search_docs"
        and ("same sections" in m.content or "does not cover" in m.content)
    ]
    assert told, "the run was never told it was getting the same sections back"
    assert any("does not cover" in t for t in told), "the corpus was never declared exhausted"
    assert False in client.offered, "search_docs was never withdrawn"


# ── a compound request is work, and a conjoined noun phrase is not ──────────


@pytest.mark.parametrize(
    ("task", "is_work"),
    [
        # "review X and fix Y" is how developers actually ask. The object rule
        # that keeps "the update flow" a noun rejected the bare noun after the
        # second verb, so 6 of these 8 were answered instead of executed.
        ("review the objection handler and fix compilation errors", True),
        ("explain the bootstrapper, then migrate it to the new template", True),
        ("look at routes.go and add pagination", True),
        ("check the handler then write unit tests", True),
        ("review this file and remove dead code", True),
        ("describe the flow and please fix vet errors", True),
        ("explain the repo and add employee crud", True),
        ("summarize routes.go then register the new handler", True),
        # Two work words conjoined behind one determiner are a noun phrase.
        ("describe the create and update handlers", False),
        ("explain the add and remove handlers", False),
        ("what are the create and delete endpoints", False),
        # Nouns that happen to spell a work verb.
        ("explain what the build does", False),
        ("explain the change detection logic", False),
        ("explain the port mapping and the wire protocol", False),
        ("what does the register do", False),
        ("describe the update flow and the build step", False),
        ("explain the next change in the pipeline", False),
        ("what is the generate step", False),
        # A pasted list is data, not a command, even when a line is a work word.
        ("explain these fields:\n- id\n- update\n- port", False),
        ("describe the pipeline steps:\n1. build\n2. test\n3. deploy", False),
    ],
)
def test_a_work_word_counts_only_where_a_command_could_begin(task, is_work):
    from dakcoder_agent.loop import _asks_for_work

    assert _asks_for_work(task) is is_work


def test_courtesy_does_not_latch_write_authorisation_for_the_session():
    """`_SAYS_GO` is ORed over `context.directives`, which `pin_directive` keeps
    for the session. An earlier draft matched "thanks", "and" and "so", so one
    courtesy reply after an answered question authorised writes for every later
    question in that session.
    """
    from dakcoder_agent.loop import _asks_for_work

    for courtesy in ["thanks", "thank you", "and", "so", "right?", "already", "hmm"]:
        assert _asks_for_work("explain the bootstrapper", (courtesy,)) is False, courtesy
    for affirmative in ["go", "go ahead", "yes", "do it", "proceed", "ok"]:
        assert _asks_for_work("explain the bootstrapper", (affirmative,)) is True, affirmative


# ── a question stays a question however its answer is phrased ───────────────


#: The Planner's real answer to "how does it deviate from the new template".
#: Naming a deviation means naming the change it implies, so the answer is
#: indistinguishable from a plan by any regex over prose: `_PLAN_EDITS` matches
#: "4. Register" and `_ACCEPTS` matches the line under it.
DEVIATION_ANSWER = """## What the bootstrapper does

1. `Fxvalidator` invokes handler.NewValidatorService.
2. `FxRepo` provides all nine repositories.

## How it deviates from the new template

4. Register the eight handlers with fx.Annotate instead of plain fx.Provide.
   Accepts: rules_lint(only=fx-registration) returns 0 findings
5. Remove the TransferentryRepoInstance package-level global.
"""

REAL_PLAN = "1. `core/domain/employee.go` - add the struct.\n   Accepts: go build passes\n"


@pytest.mark.parametrize("reply", [DEVIATION_ANSWER, REAL_PLAN, VERB_FIRST_ANSWER, BOLD_ANSWER],
                         ids=["deviation", "plan-shaped", "verb-first", "bold"])
@pytest.mark.parametrize("task", READ_ONLY_TASKS + [
    "explain the bootsrapper used in this code. also tell how it deviates from new template",
    "describe the create and update handlers",
    "explain how the routes are registered",
])
def test_a_question_is_answered_whatever_the_reply_looks_like(task, reply):
    """The field failure the reply test could not survive.

    Asked to explain the bootstrapper and say how it deviates, the Planner
    answered exactly that -- and the run went off to migrate a hundred routes,
    ending blocked on a pre-existing go_vet failure, with the explanation the
    developer wanted replaced on screen by a plan they never asked for.
    """
    from dakcoder_agent.loop import _is_explanation

    assert _is_explanation(task, reply) is True


@pytest.mark.parametrize("task", WORK_TASKS + [
    "explain the bootstrapper then migrate it to the n-api template",
    "explain the bootstrapper, then migrate it to the new template",
    "check the handler then write unit tests",
    "look at routes.go and add pagination",
])
def test_a_request_for_work_is_never_answered_whatever_the_reply_looks_like(task):
    """The other side, and the reason dropping the reply test is safe: the work
    test carries the whole judgement now, so it has to catch the preamble form.
    """
    from dakcoder_agent.loop import _is_explanation

    assert _is_explanation(task, DEVIATION_ANSWER) is False
    assert _is_explanation(task, REAL_PLAN) is False
