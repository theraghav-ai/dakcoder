"""The objective is the session's, not the latest message's.

Every test here comes from the second field session (``debug1.txt``): a
four-message migration, forty-nine turns, and no migration at its end.

Five runs, five intent classifications, three of them wrong and all three in
the same direction -- work in progress read as a question. A question runs in
ASK, and ASK has no write tools *and no* ``submit_plan``, so the best plan of
the session was produced in a read-only phase and could only leave it as a
``finish`` string. The developer's answer to the agent's own four questions was
scored "Asking for validation, not code changes"; the instruction "actually
perform the migration" was scored "Asking for confirmation of a constraint",
and the model's own first words that turn were *"You want me to actually
perform the migration, not just write the plan."*

The second half of the file is the other failure in the same log, which is not
about routing at all: a ``finish`` forced at a 119,000-token context returned
7,036 tokens of one clause repeated about a hundred times. The developer was
shown 24,000 characters of it, and because a ``finish`` answer travels as the
assistant's tool-call arguments, the run then carried it for fifteen more
turns.
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

from dakcoder_shared.paths import Workspace

from dakcoder_agent.context import ContextManager
from dakcoder_agent.loop import (
    DEGENERATE_CHARS,
    MAX_DEGENERATE_REFUSALS,
    AgentLoop,
    _State,
    _is_degenerate,
)
from dakcoder_agent.modes import Intent, Mode
from dakcoder_agent.plan import PlanRecord
from dakcoder_agent.tools.control import PlanStep
from dakcoder_agent.tools.router import Router
from scripted import ScriptedClient, build, calls, patch, plan_call, say  # noqa: E402
from scripted import gated, planning_router, written  # noqa: F401,E402


#: The plan shape the field session reached: a step that is genuinely open.
FIELD_PLAN = (
    PlanStep("migration.md", "write the phase-wise plan", "migration.md exists"),
    PlanStep("handler/objection.go", "migrate the handler", "go build clean"),
)


def _bare() -> AgentLoop:
    """A loop with no client, for the predicates that take no turns."""
    loop = AgentLoop.__new__(AgentLoop)
    loop.router = Router(Workspace(Path.cwd()))
    loop.state = _State()
    loop.state.mode = Mode.AGENT
    loop.session_id = ""
    loop.context = ContextManager(mode=Mode.AGENT, system_prompt="s")
    loop._plan_record = PlanRecord()
    return loop


class _Classifying(ScriptedClient):
    """The scripted model, keeping the prompt the classifier was sent.

    The *count* lives on `ScriptedClient` now, because more than one suite asks
    whether the classifier ran. This one kept its own and incremented it beside
    the base class's, so every classification was counted twice and the
    assertions here read one call as two.
    """

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.intent_prompts: list[str] = []

    def chat(self, messages, *, response_format=None, **kwargs):
        if (response_format or {}).get("json_schema", {}).get("name") == "intent":
            self.intent_prompts.append(messages[-1]["content"])
        return super().chat(messages, response_format=response_format, **kwargs)


def _session(router, turns, *, kind="question"):
    """A loop whose classifier answers ASK -- so consulting it is visible in the
    verdict, not only in the counter."""
    loop, _ = build(router, turns, max_turns=10)
    loop.client = _Classifying(turns, kind=kind)
    return loop, loop.client


def _follow_up(previous, turns, *, kind="question"):
    nxt, _ = build(previous.router, turns, max_turns=10)
    nxt.client = _Classifying(turns, kind=kind)
    nxt.context = previous.context
    nxt.carry_from(previous)
    return nxt, nxt.client


# ── the answer to a question the agent asked ────────────────────────────────


def test_the_answer_to_a_question_continues_the_run_that_asked(
    planning_router: Router, gated
) -> None:
    asking, _ = _session(
        planning_router,
        [calls(("ask_developer", json.dumps({"questions": ["Which template?"]})))],
    )
    list(asking.run("migrate the service", intent=Intent.AGENT))
    assert asking.state.awaiting is Intent.AGENT, "the question did not record its intent"

    answering, client = _follow_up(asking, [plan_call(), patch(), say("done")])
    list(answering.run("the n-api-template, Go 1.25", continued=True))

    assert answering.state.intent is Intent.AGENT
    assert answering.state.intent_source == "answer"
    assert client.classifications == 0, "the one follow-up whose intent is known was guessed"


def test_the_question_marker_is_spent_once(planning_router: Router, gated) -> None:
    """It answers the next message and nothing after it, so a developer who then
    asks something genuinely read-only still gets a read-only run."""
    asking, _ = _session(
        planning_router,
        [calls(("ask_developer", json.dumps({"questions": ["Which table?"]})))],
    )
    list(asking.run("add Routes", intent=Intent.AGENT))

    answering, _ = _follow_up(asking, [plan_call(), patch(), say("done")])
    list(answering.run("the users table", continued=True))
    assert answering.state.awaiting is Intent.AUTO, "the marker outlived the answer"


# ── a follow-up on work that is still open ──────────────────────────────────


def test_a_follow_up_on_open_work_is_not_re_guessed(planning_router: Router, gated) -> None:
    """"Now start step 2", scored as a question, is a migration that stops."""
    first, _ = _session(planning_router, [plan_call(), say("planned")], kind="change")
    list(first.run("add Routes", intent=Intent.AGENT))
    assert any(step.open for step in first.state.plan)

    nxt, client = _follow_up(first, [patch(), say("done")])
    list(nxt.run("now start step 2", continued=True))

    assert nxt.state.intent is Intent.AGENT
    assert nxt.state.intent_source == "session"
    assert client.classifications == 0


def test_a_forced_plan_does_not_settle_the_objective() -> None:
    """BUG L-28, one level up. A plan the research fence extracted is not a
    commitment, and pinning every later message in the session to AGENT off the
    back of one would be worse than pinning the plan itself."""
    loop = _bare()
    list(loop._adopt_plan(FIELD_PLAN, "migrate"))
    loop.state.plan_forced = True

    assert not loop._work_in_flight()


def test_a_forced_plan_that_has_been_acted_on_does_settle_it(
    planning_router: Router, gated, written
) -> None:
    """The moment anything is written the distinction lapses, exactly as it does
    in `_open_targets`: a run acting on a plan is working to it."""
    loop, _ = _session(planning_router, [plan_call(), patch(), say("done")], kind="change")
    list(loop.run("add Routes", intent=Intent.AGENT))
    loop.state.plan_forced = True
    loop.state.plan = tuple(replace(s, status="pending") for s in loop.state.plan)

    assert loop.router.touched
    assert loop._work_in_flight()


def test_a_finished_plan_is_classified_again(planning_router: Router, gated) -> None:
    """A developer asking something once the work is settled is genuinely
    ambiguous, and that is what the classifier is for."""
    loop = _bare()
    list(loop._adopt_plan(FIELD_PLAN, "migrate"))
    for step in FIELD_PLAN:
        loop._mark_steps(step.file, "done")

    assert not loop._work_in_flight()


def test_a_first_message_is_always_classified(planning_router: Router, gated) -> None:
    loop, client = _session(planning_router, [say("it registers eight handlers")])
    list(loop.run("what does bootstrapper.go do?"))

    assert loop.state.intent is Intent.ASK
    assert loop.state.intent_source == "classified"
    assert client.classifications == 1


# ── what the classifier is shown ────────────────────────────────────────────


def test_the_classifier_is_shown_what_the_agent_last_said(
    planning_router: Router, gated
) -> None:
    """`directives` is the developer's side only, so "do it, and skip the gates
    until the end" arrived with no trace that the agent had just put a ten-phase
    migration on screen -- and was scored on its grammar."""
    loop, _ = _session(planning_router, [say("I would migrate the handlers in five phases.")])
    list(loop.run("what would a migration involve?", intent=Intent.ASK))

    nxt, client = _follow_up(loop, [say("ok")])
    list(nxt.run("do it, but skip the gates until the end", continued=True))

    assert client.intent_prompts, "the classifier was never asked"
    assert "THE AGENT'S LAST REPLY" in client.intent_prompts[0]
    assert "five phases" in client.intent_prompts[0]


# ── an answer that stopped being language ───────────────────────────────────


#: The repeating unit, verbatim from the field log.
_LOOPED = "1 routes file, 1 temporal_instrument file, 1 main file, 1 bootstrap file, "


def test_the_field_degenerate_answer_is_caught() -> None:
    assert _is_degenerate("I have read the entire codebase. " + _LOOPED * 300)


def test_legitimate_repetitive_answers_are_not() -> None:
    """The shapes most likely to look like a loop and are not. Measured 8-gram
    ratios: a 300-row table 0.61, a 200-item checklist 0.80, prose 0.999 --
    against the field answer's 0.012."""
    table = "\n".join(
        f"| handler/file{i:03d}.go | migrate {i} methods to the new signature | clean |"
        for i in range(300)
    )
    checklist = "\n".join(
        f"- [ ] handler/{n}{i:02d}.go: signature migrated, validation migrated"
        for i in range(40)
        for n in ("paogen", "objection", "publicacct", "transferentry", "objectionfile")
    )
    assert not _is_degenerate(table)
    assert not _is_degenerate(checklist)


def test_a_short_answer_is_never_degenerate() -> None:
    """Below `DEGENERATE_CHARS` the ratio means nothing, and a developer can
    read past a short answer that repeats itself anyway."""
    short = "done. " * 40
    assert len(short) < DEGENERATE_CHARS
    assert not _is_degenerate(short)


def test_a_degenerate_finish_is_sent_back_once_then_taken(
    planning_router: Router, gated, written
) -> None:
    """And the push-back names the actual mistake, so the model does not go
    looking for work that is not missing."""
    looped = json.dumps({"answer": "I have read the codebase. " + _LOOPED * 300})
    loop, _client = build(
        planning_router,
        [plan_call(), patch(), calls(("finish", looped)), calls(("finish", looped))],
        max_turns=10,
    )
    list(loop.run("add Routes", intent=Intent.AGENT))

    pushed = [
        m.content
        for m in loop.context.build()
        if m.content.startswith("Your `answer` repeated")
    ]
    assert len(pushed) == 1, "a looped answer was accepted, or argued with twice"
    assert "keep the answer short" in pushed[0]
    assert loop.state.degenerate_refused == MAX_DEGENERATE_REFUSALS


# ── what a read-only finish tells the developer ─────────────────────────────


def test_an_ask_finish_reports_work_the_session_never_did(
    planning_router: Router, gated
) -> None:
    """One field run ended "the migration.md file is written and ready for
    execution" with a migration nobody had started. ASK cannot be pushed back --
    it has no write tools, so the push-back would be unsatisfiable -- but it can
    say so in the summary."""
    loop, _ = _session(planning_router, [])
    list(loop._adopt_plan(FIELD_PLAN, "migrate"))
    loop._mark_steps("migration.md", "done")

    answering, _ = _follow_up(
        loop, [calls(("finish", json.dumps({"answer": "All done."})))]
    )
    list(answering.run("summarise what you did", intent=Intent.ASK, continued=True))

    assert answering.result is not None
    assert "never wrote" in answering.result.summary
    assert "handler/objection.go" in answering.result.summary
