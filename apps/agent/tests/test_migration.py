"""The four rules a whole-service migration runs under.

Stated as the developer stated them, because each one is a property the rest of
the loop does not have and would otherwise violate:

1. **The gate does not run until the migration is finished.** Mid-conversion it
   can only fail, on code the plan has not reached.
2. **The plan is phased, and the phases are broken down.** Eight steps cannot
   hold a forty-handler service, so the roadmap is separate and only one phase
   is work at a time.
3. **A repository gets a branch first**, cut from ``development`` so the new
   branch carries that code, and confirmed with the developer rather than
   assumed.
4. **Never the whole codebase in one go.** A phase closes, the developer is
   told, and the run stops there.

See ``apps/agent/src/dakcoder_agent/migration.py`` for why each is where it is.
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from dakcoder_shared.envelope import ToolResult
from dakcoder_shared.paths import Workspace

from dakcoder_agent.context import ContextManager
from dakcoder_agent.gate import ROUTES_BEFORE, GateReport, StageResult
from dakcoder_agent.loop import STUCK_TURNS, MAX_PLAN_OBJECTIONS, AgentLoop, _State
from dakcoder_agent.migration import (
    BIG_FILE,
    MIN_PHASES,
    PROGRESS_PATH,
    MigrationState,
    Phase,
    phases_from_meta,
    plan_objection,
    progress_document,
    steps_for_phase,
)
from dakcoder_agent.modes import Intent, Mode
from dakcoder_agent.plan import PlanRecord
from dakcoder_agent.tools import commands, fs
from dakcoder_agent.tools.control import MODEL_STATUSES, PlanStep, submit_plan
from dakcoder_agent.tools.router import Invocation, Router
from dakcoder_agent.tools import registry
from scripted import build, calls, patch, say  # noqa: E402
from scripted import gated, planning_router, written  # noqa: F401,E402

ROADMAP = [
    {"name": "branch", "covers": "cut the branch", "parts": "check, confirm, cut"},
    {"name": "deps", "covers": "swap api-* for n-api-*", "parts": "go get, tidy, replace"},
    {"name": "handlers", "covers": "convert every handler", "parts": "imports, Base, Routes"},
]


def _clean() -> GateReport:
    return GateReport(
        results=(StageResult(name="gofmt", ok=True, blocking=False, content="clean"),)
    )


def _phases(*, status: str = "pending") -> tuple[Phase, ...]:
    return tuple(Phase(**p, status=status) for p in ROADMAP)


def _loop(root: Path | None = None) -> AgentLoop:
    loop = AgentLoop.__new__(AgentLoop)
    loop.router = Router(Workspace(root or Path.cwd()))
    loop.state = _State()
    loop.state.mode = Mode.AGENT
    loop.session_id = ""
    loop.context = ContextManager(mode=Mode.AGENT, system_prompt="s")
    loop._plan_record = PlanRecord()
    return loop


def _migrating(root: Path | None = None, *, phase: str = "deps") -> AgentLoop:
    """A loop mid-migration: roadmap adopted, branch cut, one phase open."""
    loop = _loop(root)
    loop.state.migration = MigrationState(active=True, branch="template-conversion", base="development")
    loop.state.migration.adopt(_phases())
    if phase != "branch":
        loop.state.migration.close("branch")
    return loop


# ── 1. the gate is deferred ─────────────────────────────────────────────────


def test_a_migration_defers_the_gate_until_the_last_phase_closes() -> None:
    """The predicate the whole rule hangs on, at both ends.

    Deliberately not "is this a migration": a conversion whose last phase has
    closed is a service that is supposed to compile, and a deferral that
    outlived the migration would be a permanent exemption from verification.
    """
    state = MigrationState(active=True)
    assert state.defers_gate, "a migration with no roadmap yet still defers"

    state.adopt(_phases())
    assert state.defers_gate
    for name in ("branch", "deps"):
        state.close(name)
        assert state.defers_gate, f"still open after {name}"

    state.close("handlers")
    assert state.complete
    assert not state.defers_gate, "the last phase closing is what releases the gate"


def test_the_gate_never_runs_while_a_phase_is_open(gated, planning_router) -> None:
    """The rule as the toolchain sees it: no gate stage is dispatched at all.

    Asserted on the stage handlers rather than on the report, because the cost
    this exists to avoid is the seventy seconds, not the verdict.
    """
    ran: list[str] = []
    for name in ("go_build", "go_vet", "go_test", "swagger_check", "go_mod"):
        planning_router.handlers[name] = lambda inv, _n=name: ran.append(_n) or (
            __import__("dakcoder_shared.envelope", fromlist=["ToolResult"]).ToolResult.success("x")
        )

    plan = json.dumps(
        {
            "summary": "convert pisapi",
            "phases": ROADMAP,
            "steps": [
                {
                    "file": "handler/user.go",
                    "action": "convert it",
                    "accepts": "go build",
                    "phase": "branch",
                    "part": "cut",
                }
            ],
        }
    )
    loop, _ = build(
        planning_router,
        [calls(("submit_plan", plan)), patch(), calls(("finish", json.dumps({"answer": "phase done"})))],
        migration=True,
    )
    # The branch rule is not what this test is about; satisfy it up front.
    loop.state.migration.branch = "template-conversion"
    # Intent left AUTO on purpose: the classifier is one of the two things that
    # can say "migration", and this is the path it runs on.
    list(loop.run("migrate pisapi to the n-api template"))

    assert ran == [], f"the gate ran mid-migration: {ran}"
    assert loop.result is not None and loop.result.outcome == "done"
    assert "gate has not run" in loop.result.summary


def test_no_baseline_is_taken_during_a_migration(planning_router, monkeypatch) -> None:
    """And this is the half that is easy to get backwards.

    A baseline records what was broken *before* the run, so the gate charges the
    run only for its own damage. Mid-migration the run's own damage is the
    workspace, so a baseline taken here would hand the final gate the previous
    phase's breakage labelled "not your fault".
    """
    taken: list[str] = []
    monkeypatch.setattr(
        AgentLoop, "_take_baseline", lambda self: taken.append(self.state.intent)
    )

    loop, _ = build(planning_router, [say("thinking")], migration=True, max_turns=1)
    list(loop.run("migrate pisapi"))
    assert taken == [], "a migration took a baseline of its own half-converted state"

    loop2, _ = build(planning_router, [say("thinking")], migration=False, max_turns=1)
    list(loop2.run("add a handler"))
    assert len(taken) == 1, "an ordinary change still takes one"


def test_a_given_intent_still_gets_asked_whether_this_is_a_migration(
    planning_router,
) -> None:
    """The hole `/migrate` fell into, closed at the source.

    A caller that supplies the intent -- the panel's Agent toggle, the
    `/migrate` command, any API client -- used to skip the classifier entirely,
    and the classifier is the only thing that asks whether this is a
    whole-service conversion. So the one entry point named after migrating was
    the one where none of the migration rules engaged.
    """
    loop, client = build(
        planning_router, [say("thinking")], migration=True, max_turns=1
    )
    list(loop.run("Migrate this service to the n-api-template", intent=Intent.AGENT))

    assert client.classifications == 1, "asked exactly once"
    assert loop.state.migration.active, "an explicit agent intent skipped the question"
    assert loop.state.intent is Intent.AGENT
    assert loop.state.intent_source == "given", "the caller's answer still stands"
    assert loop.state.intent_why == "", (
        "a classification the run did not act on must not explain its routing"
    )


def test_the_question_is_not_asked_when_the_roadmap_already_answers_it(
    planning_router,
) -> None:
    """Once per session, not once per message: a migration's later messages are
    'carry on' and 'start phase 3', which no classifier would call a
    conversion."""
    loop, client = build(planning_router, [say("thinking")], migration=True, max_turns=1)
    loop.state.migration.active = True
    list(loop.run("carry on", intent=Intent.AGENT))
    assert client.classifications == 0, "the classifier was asked again"


def test_a_read_only_intent_is_never_asked(planning_router) -> None:
    """ASK cannot write, so the question has nothing to change and would be a
    call spent on every question the product answers."""
    loop, client = build(planning_router, [say("thinking")], migration=True, max_turns=1)
    list(loop.run("what does this service do", intent=Intent.ASK))
    assert client.classifications == 0
    assert not loop.state.migration.active


# ── 2. the plan is phased, and the phases are broken down ───────────────────


def test_a_migration_plan_without_phases_is_sent_back() -> None:
    state = MigrationState(active=True)
    objection = plan_objection(state, (), [PlanStep("a.go", "x", "y")])
    assert "no phases" in objection
    assert str(MIN_PHASES) in objection


def test_a_roadmap_too_short_to_be_a_roadmap_is_sent_back() -> None:
    state = MigrationState(active=True)
    two = (Phase("a", "x", "p, q"), Phase("b", "y", "r, s"))
    assert "phase(s)" in plan_objection(state, two, [])


def test_every_phase_has_to_name_its_sub_categories() -> None:
    """Requirement two's second half: phases are *further* sub-categorised."""
    state = MigrationState(active=True)
    thin = (
        Phase("branch", "cut it", "one"),
        Phase("deps", "swap them", "go get, tidy"),
        Phase("handlers", "convert", "imports, routes"),
    )
    objection = plan_objection(state, thin, [])
    assert "branch" in objection and "no breakdown" in objection


def test_a_plan_spanning_two_phases_is_trimmed_not_refused() -> None:
    """Requirement four, enforced without arguing about it.

    A model asked for a seven-phase roadmap and the steps for the open phase
    sends the roadmap and the steps for the first two, because it has just
    thought about both. That is a good plan in the wrong shape. Refusing it cost
    a field session its whole budget: the plan bounced, the model re-planned,
    the refusal fired again, and nothing was ever adopted — so the run never
    reached the phase that can act.
    """
    state = MigrationState(active=True)
    state.adopt(_phases())
    steps = [
        PlanStep("go.mod", "swap", "tidy", phase="deps"),
        PlanStep("go.sum", "tidy", "tidy", phase="deps"),
        PlanStep("handler/user.go", "convert", "build", phase="handlers"),
    ]
    assert plan_objection(state, (), steps) == "", "a spanning plan is not an objection"

    kept, rest = steps_for_phase(state.phases, steps)
    assert [s.file for s in kept] == ["go.mod", "go.sum"]
    assert [s.file for s in rest] == ["handler/user.go"]


def test_the_trim_follows_the_roadmap_order_not_the_submission_order() -> None:
    state = MigrationState(active=True)
    state.adopt(_phases())
    steps = [
        PlanStep("handler/user.go", "convert", "build", phase="handlers"),
        PlanStep("go.mod", "swap", "tidy", phase="deps"),
    ]
    kept, rest = steps_for_phase(state.phases, steps)
    assert [s.file for s in kept] == ["go.mod"], "the earlier phase is the open one"
    assert [s.file for s in rest] == ["handler/user.go"]


def test_a_closed_phase_is_never_the_one_trimmed_to() -> None:
    state = MigrationState(active=True)
    state.adopt(_phases())
    state.close("branch")
    steps = [
        PlanStep("go.mod", "swap", "tidy", phase="deps"),
        PlanStep("handler/user.go", "convert", "build", phase="handlers"),
    ]
    kept, _ = steps_for_phase(state.phases, steps)
    assert [s.file for s in kept] == ["go.mod"]


def test_an_untagged_plan_is_left_alone() -> None:
    """Nothing to trim to, and dropping every step would be worse than adopting
    a plan in the wrong shape."""
    state = MigrationState(active=True)
    state.adopt(_phases())
    steps = [PlanStep("go.mod", "swap", "tidy")]
    kept, rest = steps_for_phase(state.phases, steps)
    assert kept == steps and rest == []


def test_steps_must_name_a_phase_that_exists() -> None:
    state = MigrationState(active=True)
    state.adopt(_phases())
    assert "not in the roadmap" in plan_objection(
        state, (), [PlanStep("a.go", "x", "y", phase="swagger")]
    )
    assert "say which phase" in plan_objection(state, (), [PlanStep("a.go", "x", "y")])


def test_a_phased_plan_is_accepted_and_travels_in_meta() -> None:
    result = submit_plan(
        _call(
            "submit_plan",
            {
                "summary": "convert pisapi",
                "phases": ROADMAP,
                "steps": [
                    {
                        "file": "go.mod",
                        "action": "swap the dependencies",
                        "accepts": "go mod tidy",
                        "phase": "deps",
                        "part": "go get",
                    }
                ],
            },
            Workspace(Path.cwd()),
        )
    )
    assert result.ok
    assert [p.name for p in phases_from_meta(result.meta)] == ["branch", "deps", "handlers"]
    assert "3 phase(s)" in result.content
    assert "the rest of the roadmap is kept" in result.content
    # The tool does not promise what the loop has not decided. It used to say
    # "Work starts now — you hold the write tools from this turn on", on a call
    # the loop reads afterwards and can refuse.
    assert "Work starts now" not in result.content
    assert "decided after this call" in result.content
    assert "[deps · go get]" in result.content


def test_a_roadmap_is_itself_the_evidence_that_this_is_a_migration() -> None:
    """The second of the two signals, and the one that covers the panel.

    The classifier only runs when the intent is AUTO -- a developer who hits the
    Agent toggle answers the intent question and the classifier is skipped
    entirely -- so a run started that way would never be told it is a migration.
    A plan that arrives with a roadmap is the stronger witness anyway: it is the
    model's own commitment, not a 160-token guess about a sentence.
    """
    loop = _loop()
    loop._baseline_thread = None
    assert not loop.state.migration.active

    list(
        loop._phase_ended(
            "submit_plan",
            _plan_result(
                steps=[
                    {
                        "file": "go.mod",
                        "action": "swap",
                        "accepts": "tidy",
                        "phase": "deps",
                    }
                ],
                phases=ROADMAP,
            ),
        )
    )
    assert loop.state.migration.active
    assert loop.state.migration.defers_gate
    assert [p.name for p in loop.state.migration.phases] == ["branch", "deps", "handlers"]


def test_an_unphased_migration_plan_is_not_adopted(tmp_path: Path) -> None:
    loop = _loop(tmp_path)
    loop._baseline_thread = None
    loop.state.migration.active = True

    events = list(
        loop._phase_ended(
            "submit_plan",
            _plan_result(steps=[{"file": "a.go", "action": "x", "accepts": "y"}]),
        )
    )
    assert events == [], "an unphased migration plan was adopted"
    assert loop.state.plan == ()
    assert loop.state.plan_objections == 1
    asked = loop.context.build()[-1].content
    assert "no phases" in asked and "submit_plan" in asked


def test_an_ordinary_plan_is_never_asked_for_phases(tmp_path: Path) -> None:
    """The check is a migration's, not the product's.

    A three-step bug fix has no phases and demanding them would be ceremony --
    and worse, a push-back the model answers by inventing a roadmap for a task
    that has none.
    """
    loop = _loop(tmp_path)
    loop._baseline_thread = None

    events = list(
        loop._phase_ended(
            "submit_plan",
            _plan_result(steps=[{"file": "a.go", "action": "x", "accepts": "y"}]),
        )
    )
    assert any(e.type == "plan" for e in events)
    assert loop.state.plan_objections == 0


def test_the_objection_is_bounded() -> None:
    """A push-back that never stops asking spends the budget on the shape of the work.

    The failure this codebase keeps rebuilding by accident is a condition the
    model cannot satisfy that also cannot be exhausted. This one can.
    """
    assert MAX_PLAN_OBJECTIONS >= 1
    loop = _loop()
    loop._baseline_thread = None
    loop.state.migration.active = True
    loop.state.plan_objections = MAX_PLAN_OBJECTIONS
    events = list(
        loop._phase_ended(
            "submit_plan",
            _plan_result(steps=[{"file": "a.go", "action": "x", "accepts": "y"}]),
        )
    )
    assert any(e.type == "plan" for e in events), "the plan is adopted once the budget is spent"


def _plan_result(*, steps, phases=()):
    from dakcoder_shared.envelope import ToolResult

    return ToolResult.success(
        "plan", meta={"control": "plan", "summary": "s", "steps": steps, "phases": list(phases)}
    )


# ── the planner is never told to make a call it cannot make ─────────────────
#
# From a field session that deadlocked across six developer messages: the state
# block told the Planner to cut the branch with `git_ops`, `git_ops` is an
# acting tool, and every attempt was refused by mode. The Planner asked the
# developer which branch to cut from four times, was answered every time, and
# never submitted a plan — so it never reached the phase that could have cut it.


def test_the_planner_is_told_to_plan_the_branch_not_to_cut_it(tmp_path: Path) -> None:
    loop = _loop(tmp_path)
    loop.state.mode = Mode.PLANNER
    loop.state.migration = MigrationState(active=True)
    loop.state.migration.adopt(_phases())

    block = loop._state_block()
    assert "no branch yet" in block
    assert "submit_plan" in block
    assert "cannot cut it here" in block
    assert "git_ops` op=branch" not in block, (
        "the planner was told to make a call its own request does not offer"
    )


def test_the_acting_phase_is_told_to_cut_it(tmp_path: Path) -> None:
    loop = _loop(tmp_path)
    loop.state.mode = Mode.AGENT
    loop.state.migration = MigrationState(active=True)
    loop.state.migration.adopt(_phases())

    block = loop._state_block()
    assert "git_ops` op=branch" in block
    assert "held until it exists" in block


def test_a_planner_refused_a_write_tool_is_told_where_the_write_happens() -> None:
    """`git_ops` refused by mode used to answer "a later step in the run will
    make it", which names no move. The later step is on the far side of
    `submit_plan`, and nothing said so."""
    router = Router(Workspace(Path.cwd()))
    outcome = router.dispatch("git_ops", {"op": "branch"}, mode=Mode.PLANNER)

    assert isinstance(outcome, ToolResult) and not outcome.ok
    assert outcome.meta.get("refused_by_mode")
    assert "submit_plan" in outcome.fix
    assert "acting phase" in outcome.fix


def test_submit_plan_does_not_promise_what_the_loop_has_not_decided() -> None:
    """The tool result reaches the model before the loop has read the plan.

    Saying "work starts now, you hold the write tools from this turn on" on a
    plan the loop then refuses is two contradictory statements about one call,
    and the field session believed the wrong one: it went looking for write
    tools it did not have.
    """
    result = submit_plan(
        _call(
            "submit_plan",
            {"steps": [{"file": "a.go", "action": "x", "accepts": "y"}]},
            Workspace(Path.cwd()),
        )
    )
    assert result.ok
    assert "Work starts now" not in result.content
    assert "write tools" not in result.content
    assert "decided after this call" in result.content


# ── a file bigger than one reply is bigger than one step ────────────────────
#
# These come from a field run that did the right thing and still failed: it
# converted `go.mod`, the bootstrapper and one handler, then reported that
# `handler/paogen.go` was 6,571 lines with about fifty handler methods, that
# three more files were comparable, and that "the total volume of code to
# convert (10,000+ lines across 7+ files) exceeds what can be reliably done in a
# single session". Every word of that was true. Its plan had one step per file.


def test_a_file_too_big_for_one_reply_may_not_have_one_step(tmp_path: Path) -> None:
    big = tmp_path / "handler" / "paogen.go"
    big.parent.mkdir(parents=True, exist_ok=True)
    big.write_text("package handler\n" * 6_571, encoding="utf-8")
    loop = _loop(tmp_path)

    objection = plan_objection(
        MigrationState(active=True),
        _phases(),
        [PlanStep("handler/paogen.go", "convert it", "go build", phase="handlers")],
        lines=loop._line_count,
    )
    assert "6,571 lines" in objection
    assert "one step" in objection
    assert "can never be finished" in objection
    assert "line range" in objection


def test_splitting_that_file_across_steps_is_accepted(tmp_path: Path) -> None:
    big = tmp_path / "handler" / "paogen.go"
    big.parent.mkdir(parents=True, exist_ok=True)
    big.write_text("package handler\n" * 6_571, encoding="utf-8")
    loop = _loop(tmp_path)

    steps = [
        PlanStep(
            "handler/paogen.go",
            f"convert methods {n}-{n + 9}",
            "go build",
            phase="handlers",
            part=f"methods {n}-{n + 9}",
        )
        for n in (1, 11, 21)
    ]
    assert (
        plan_objection(MigrationState(active=True), _phases(), steps, lines=loop._line_count)
        == ""
    )


def test_a_small_file_is_never_asked_to_be_split(tmp_path: Path) -> None:
    small = tmp_path / "handler" / "objection.go"
    small.parent.mkdir(parents=True, exist_ok=True)
    small.write_text("package handler\n" * 120, encoding="utf-8")
    loop = _loop(tmp_path)

    assert (
        plan_objection(
            MigrationState(active=True),
            _phases(),
            [PlanStep("handler/objection.go", "convert", "build", phase="handlers")],
            lines=loop._line_count,
        )
        == ""
    )


def test_a_write_lands_on_the_step_being_worked_not_on_every_step(tmp_path: Path) -> None:
    """The half that makes splitting work at all.

    Marking every step that covers the path turned nine steps over one file back
    into one step wearing nine hats: the first `patch_file` closed all of them.
    """
    loop = _migrating(tmp_path, phase="handlers")
    loop.state.plan = tuple(
        PlanStep(
            "handler/paogen.go",
            f"convert methods {n}-{n + 9}",
            "go build",
            phase="handlers",
            part=f"methods {n}-{n + 9}",
        )
        for n in (1, 11, 21)
    )

    # The real sequence: a write marks the step, the inner gate promotes it.
    loop._mark_steps("handler/paogen.go", "written")
    assert [s.status for s in loop.state.plan] == ["written", "pending", "pending"]

    loop._verify_written(_clean())
    assert [s.status for s in loop.state.plan] == ["done", "pending", "pending"]

    loop._mark_steps("handler/paogen.go", "written")
    assert [s.status for s in loop.state.plan] == ["done", "written", "pending"], (
        "the second write did not land on the next slice of the file"
    )


def test_one_step_naming_a_file_still_marks_that_step(tmp_path: Path) -> None:
    loop = _migrating(tmp_path)
    loop.state.plan = (
        PlanStep("go.mod", "swap", "tidy", phase="deps"),
        PlanStep("go.sum", "tidy", "tidy", phase="deps"),
    )
    loop._mark_steps("go.mod", "written")
    assert [s.status for s in loop.state.plan] == ["written", "pending"]


# ── a delete is not a write ─────────────────────────────────────────────────
#
# From the same field session, one turn further on. `write_file` refuses to
# overwrite, so the way to replace a file is to delete it and write it again --
# and the run did the first half to `handler/paogen.go` (6,571 lines),
# `handler/publicacct.go` (695), `handler/transferentry.go` (3,966) and
# `handler/objectionfile.go` (223), announced "now I'll delete it and write the
# converted version" each time, and then moved to the next step. Four files
# gone. Nothing in the run's own state disagreed, because the deletion counted
# as the step's mutation.


def test_a_delete_does_not_satisfy_the_step_it_lands_on(tmp_path: Path) -> None:
    loop = _migrating(tmp_path, phase="handlers")
    loop.state.plan = (
        PlanStep("handler/paogen.go", "convert it to the template", "go build", phase="handlers"),
    )

    loop._mark_steps("handler/paogen.go", "pending", "deleted; its replacement has not been written")
    assert loop.state.plan[0].status == "pending"
    assert loop.state.plan[0].open, "the cursor has to stay on a step whose file was removed"


def test_a_step_that_asked_for_the_removal_is_satisfied_by_it(tmp_path: Path) -> None:
    """Otherwise the guard is a condition no migration can clear.

    `routes/routes.go` and the swaggo artefacts are removed by this conversion
    on purpose, and a step that says so has to be closable by the delete that
    carries it out.
    """
    loop = _migrating(tmp_path)
    loop.state.plan = (
        PlanStep("routes/routes.go", "delete the retired routes file", "it is gone", phase="deps"),
        PlanStep("handler/paogen.go", "convert it", "go build", phase="deps"),
    )
    assert loop._step_wants_removal("routes/routes.go")
    assert not loop._step_wants_removal("handler/paogen.go")


def test_finishing_is_refused_while_a_deleted_file_is_still_missing(tmp_path: Path) -> None:
    loop = _migrating(tmp_path, phase="handlers")
    loop.state.plan = (
        PlanStep("handler/paogen.go", "convert it", "go build", phase="handlers", status="done"),
    )
    loop.state.removed = {"handler/paogen.go"}

    reason = loop._why_not_done()
    assert "you deleted handler/paogen.go" in reason
    assert "simply gone" in reason


def test_the_objection_clears_itself_when_the_file_comes_back(tmp_path: Path) -> None:
    """Checked against the disk, so a file the developer restored is not held against the run."""
    loop = _migrating(tmp_path, phase="handlers")
    loop.state.removed = {"handler/paogen.go"}
    assert loop._deleted_and_not_replaced() == ["handler/paogen.go"]

    target = tmp_path / "handler" / "paogen.go"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("package handler\n", encoding="utf-8")

    assert loop._deleted_and_not_replaced() == []
    assert loop.state.removed == set(), "the ledger drops a path that is back"


def test_deleting_says_what_has_to_happen_next(workspace) -> None:
    """The one mutation whose next step the model routinely does not take."""
    big = workspace.root / "handler" / "paogen.go"
    big.parent.mkdir(parents=True, exist_ok=True)
    big.write_text("package handler\n" * 6_571, encoding="utf-8")

    result = fs.delete_file(_call(
            "delete_file",
            {"path": "handler/paogen.go", "reason": "rewriting it converted"},
            workspace,
        ))
    assert result.ok
    assert "6,571 lines" in result.content
    assert "write the replacement before you do anything else" in result.content
    assert "append=true" in result.content
    assert "does not fit in one reply" in result.content


def test_deleting_a_small_file_says_less(workspace) -> None:
    result = fs.delete_file(_call(
            "delete_file",
            {"path": "handler/user.go", "reason": "retired"},
            workspace,
        ))
    assert result.ok
    assert "does not fit in one reply" not in result.content


def test_a_delete_and_a_write_in_one_run_leaves_nothing_outstanding(
    planning_router, tmp_path: Path
) -> None:
    """The whole sequence, through the loop, as the field run should have gone."""
    workspace_root = planning_router.workspace.root
    (workspace_root / "handler" / "paogen.go").write_text(
        "package handler\n" * 50, encoding="utf-8"
    )

    loop, _ = build(
        planning_router,
        [
            calls(
                (
                    "delete_file",
                    json.dumps(
                        {"path": "handler/paogen.go", "reason": "rewriting it converted"}
                    ),
                )
            ),
            calls(
                (
                    "write_file",
                    json.dumps(
                        {"path": "handler/paogen.go", "content": "package handler\n"}
                    ),
                )
            ),
            calls(("finish", json.dumps({"answer": "converted"}))),
        ],
        migration=True,
    )
    loop.state.migration.branch = "template-conversion"
    loop.state.plan = (
        PlanStep("handler/paogen.go", "convert it", "go build", phase="handlers"),
    )
    # A follow-up on a plan with open steps opens in AGENT, which is the phase
    # that holds the write tools -- and the phase this bug lives in.
    list(loop.run("convert handler/paogen.go", intent=Intent.AGENT, continued=True))

    assert loop.state.removed == set(), "the write cleared the deletion"
    assert (workspace_root / "handler" / "paogen.go").exists()
    assert (workspace_root / "handler" / "paogen.go").read_text(
        encoding="utf-8"
    ).strip() == "package handler", "the replacement was written"


def test_a_delete_with_no_write_is_told_about_on_the_turn_it_happened(
    planning_router,
) -> None:
    """On that turn, because it is the last one holding what was in the file."""
    workspace_root = planning_router.workspace.root
    (workspace_root / "handler" / "paogen.go").write_text(
        "package handler\n" * 50, encoding="utf-8"
    )

    loop, _ = build(
        planning_router,
        [
            calls(
                (
                    "delete_file",
                    json.dumps(
                        {"path": "handler/paogen.go", "reason": "rewriting it converted"}
                    ),
                )
            ),
            say("moving on to the next step"),
        ],
        migration=True,
        max_turns=2,
    )
    loop.state.migration.branch = "template-conversion"
    loop.state.plan = (
        PlanStep("handler/paogen.go", "convert it", "go build", phase="handlers"),
    )
    list(loop.run("convert handler/paogen.go", intent=Intent.AGENT, continued=True))

    told = [
        m.content
        for m in loop.context.build()
        if m.role.value == "user" and "You deleted" in (m.content or "")
    ]
    assert told, "the run was never told the file was gone"
    assert "write it in this turn" in told[0]
    assert "back to pending" in told[0]
    assert loop.state.plan[0].status == "pending"


# ── the routes are recorded before, and checked after ───────────────────────


def test_the_route_inventory_is_taken_before_the_first_write(tmp_path: Path) -> None:
    """On the way past the branch guard, which is the last turn the answer is
    still the legacy service's.

    A phase later it would be a picture of a half-converted service, and
    comparing the finished migration against that passes by construction: the
    routes already lost would not be in the baseline to be missed.
    """
    (tmp_path / ".git").mkdir()
    loop = _loop(tmp_path)
    loop.state.migration = MigrationState(active=True)

    taken: list[dict] = []
    loop.router.run_gate_tool = lambda name, args=None: (
        taken.append({"name": name, "args": args})
        or ToolResult.success("12 route(s)", meta={"routes": 12, "unresolved": 0})
    )
    loop._relay = lambda event: None

    from dakcoder_shared.llm import ToolCall

    loop._migration_guard(ToolCall(id="1", name="write_file", arguments="{}"))

    assert taken == [{"name": "route_inventory", "args": {"save": ROUTES_BEFORE}}]
    assert loop.state.routes_before == 12
    assert loop.state.routes_saved


def test_the_inventory_is_taken_once_not_once_per_write(tmp_path: Path) -> None:
    """A workspace with no sidecar answers the same way every time, and a retry
    on each of forty write calls costs a subprocess apiece to learn it again."""
    (tmp_path / ".git").mkdir()
    loop = _loop(tmp_path)
    loop.state.migration = MigrationState(active=True)
    calls_made: list[str] = []
    loop.router.run_gate_tool = lambda name, args=None: (
        calls_made.append(name) or ToolResult.failure("no sidecar")
    )
    loop._relay = lambda event: None

    from dakcoder_shared.llm import ToolCall

    for _ in range(3):
        loop._migration_guard(ToolCall(id="1", name="write_file", arguments="{}"))
    assert calls_made == ["route_inventory"]


def test_nothing_is_recorded_for_an_ordinary_change(tmp_path: Path) -> None:
    (tmp_path / ".git").mkdir()
    loop = _loop(tmp_path)
    called: list[str] = []
    loop.router.run_gate_tool = lambda name, args=None: (
        called.append(name) or ToolResult.success("x")
    )
    loop._save_routes()
    assert called == []


def test_the_gate_checks_the_routes_when_an_inventory_exists(tmp_path: Path) -> None:
    """Conditioned on the file, which needs no migration flag and is the honest
    test either way: nothing but a migration writes it."""
    from dakcoder_agent.gate import GATE, GateContext

    stage = next(st for st in GATE if st.name == "routes_check")
    router = Router(Workspace(tmp_path))

    assert not stage.when(GateContext(router, ())), "no inventory, no check"

    (tmp_path / ".dakcoder").mkdir(parents=True, exist_ok=True)
    (tmp_path / ROUTES_BEFORE).write_text('{"routes": []}', encoding="utf-8")
    assert stage.when(GateContext(router, ()))
    assert stage.args(GateContext(router, ())) == {"against": ROUTES_BEFORE}
    assert stage.blocking, "a lost endpoint is not advisory"


def test_the_progress_record_says_whether_the_routes_were_recorded(tmp_path: Path) -> None:
    loop = _migrating(tmp_path)
    loop.state.plan = (PlanStep("go.mod", "swap", "tidy", phase="deps"),)

    with_routes = progress_document(loop.state.migration, loop.state.plan, (), 93)
    assert "**Routes recorded before the migration:** 93" in with_routes

    without = progress_document(loop.state.migration, loop.state.plan, (), 0)
    assert "will not be caught automatically" in without


# ── progress survives the context window ────────────────────────────────────


def test_the_plan_document_is_written_where_the_view_and_the_next_session_look(
    tmp_path: Path,
) -> None:
    """A conversion outlives a context window; the transcript does not.

    And the path is the extension's, not one of our own choosing: the Migration
    view watches `.dakcoder/migration/plan.md` and has since it shipped. Nothing
    had ever written it, so the view was permanently empty and a developer
    mid-migration had nowhere to look.
    """
    loop = _migrating(tmp_path)
    loop.state.routes_before = 93
    loop.state.plan = (
        PlanStep("go.mod", "swap the deps", "tidy", phase="deps", status="done"),
        PlanStep("main.go", "drop the routes invoke", "compiles", phase="deps"),
    )
    loop._save_progress()

    assert PROGRESS_PATH == ".dakcoder/migration/plan.md", (
        "the path is a contract with extension/src/wizard.ts"
    )
    doc = (tmp_path / PROGRESS_PATH).read_text(encoding="utf-8")

    # The header a developer opens it for.
    assert "**Branch:** `template-conversion`, cut from `development`" in doc
    assert "**Phases:** 1 of 3 closed" in doc
    assert "**Routes recorded before the migration:** 93" in doc

    # Every phase, with its breakdown and where the work is.
    assert "1. **branch** — cut the branch · **done**" in doc
    assert "2. **deps** — swap api-* for n-api-* · **open**" in doc
    assert "3. **handlers** — convert every handler · pending" in doc
    assert "Parts: imports, Base, Routes" in doc
    assert "Steps: 1 of 2 settled" in doc

    # And the units, in the table the view parses.
    assert "| Unit | Kind | Classification | Status | Rules | Commit |" in doc
    assert "| go.mod | deps | MIGRATE | done |" in doc
    assert "| main.go | deps | MIGRATE | pending |" in doc
    assert "## Still to do" in doc


def test_a_phase_whose_steps_are_all_settled_says_so(tmp_path: Path) -> None:
    """Otherwise the document contradicts the line under it: "pending", above
    "Steps: 4 of 4 settled"."""
    loop = _migrating(tmp_path, phase="handlers")
    loop.state.plan = (
        PlanStep("go.mod", "swap", "tidy", phase="deps", status="done"),
        PlanStep("handler/x.go", "convert", "build", phase="handlers"),
    )
    doc = progress_document(loop.state.migration, loop.state.plan, (), 0)
    assert "**deps** — swap api-* for n-api-* · ready to close" in doc


def test_the_document_never_renders_a_checkbox(tmp_path: Path) -> None:
    """The view's parser reads *every* `- [ ]` line as a unit.

    A phase rendered as a checklist — the obvious way to render one — would
    appear in the Migration tree as a file that does not exist, above the files
    that do. So the phases are a numbered list, and this is the assertion that
    keeps them one.
    """
    loop = _migrating(tmp_path)
    loop.state.plan = (
        PlanStep("go.mod", "swap", "tidy", phase="deps", status="done"),
        PlanStep("main.go", "wire", "build", phase="deps", status="skipped", note="not needed"),
    )
    doc = progress_document(loop.state.migration, loop.state.plan, ("go.mod",), 93)
    assert "- [ ]" not in doc and "- [x]" not in doc


def test_a_skipped_step_is_excluded_in_the_views_own_vocabulary(tmp_path: Path) -> None:
    loop = _migrating(tmp_path)
    loop.state.plan = (
        PlanStep("routes/routes.go", "delete it", "gone", phase="deps", status="skipped"),
    )
    doc = progress_document(loop.state.migration, loop.state.plan, (), 0)
    assert "| routes/routes.go | deps | SKIP | skipped |" in doc


def test_closing_a_phase_is_logged_with_when(tmp_path: Path) -> None:
    """"Where did the last session get to" wants a when as well as a what."""
    loop = _migrating(tmp_path, phase="branch")
    assert loop.state.migration.log == ()
    loop.state.migration.close("branch")

    assert len(loop.state.migration.log) == 1
    entry = loop.state.migration.log[0]
    assert "phase 1 of 3" in entry and "branch" in entry and "closed" in entry

    doc = progress_document(loop.state.migration, (), (), 0)
    assert "## Log" in doc and entry in doc


def test_the_log_survives_a_restart(tmp_path: Path) -> None:
    state = MigrationState(active=True)
    state.adopt(_phases())
    state.close("branch")
    record = PlanRecord(session_id="s1")
    record.migration = state
    record.save(tmp_path)

    back = PlanRecord.load(tmp_path, "s1")
    assert back is not None and back.migration.log == state.log


def test_nothing_is_written_for_an_ordinary_task(tmp_path: Path) -> None:
    loop = _loop(tmp_path)
    loop.state.plan = (PlanStep("a.go", "x", "y"),)
    loop._save_progress()
    assert not (tmp_path / PROGRESS_PATH).exists()


def test_the_document_is_rendered_from_the_plan_not_from_prose(tmp_path: Path) -> None:
    """The field session wrote its own `migration.md` by hand and then read it
    back as evidence of work it had not done. This one cannot say that.

    The file is touched, and the step is still `pending`: what the change set
    holds and what the plan claims are different facts, and only one of them is
    the model's.
    """
    loop = _migrating(tmp_path)
    loop.state.plan = (PlanStep("go.mod", "swap", "tidy", phase="deps"),)
    doc = progress_document(loop.state.migration, loop.state.plan, ("go.mod",))
    assert "| go.mod | deps | MIGRATE | pending |" in doc
    assert "Edits are overwritten" in doc


# ── 3. the branch ───────────────────────────────────────────────────────────


def test_a_write_is_held_until_the_migration_has_a_branch(tmp_path: Path) -> None:
    (tmp_path / ".git").mkdir()
    loop = _loop(tmp_path)
    loop.state.migration = MigrationState(active=True)

    from dakcoder_shared.llm import ToolCall

    held = loop._migration_guard(ToolCall(id="1", name="write_file", arguments="{}"))
    assert held is not None and not held.ok
    # The whole move, in order, because a refusal that does not name the way out
    # costs a turn while the model guesses.
    assert "git_status" in held.fix
    assert "ask_developer" in held.fix
    assert "development" in held.fix
    assert "op=branch" in held.fix


def test_the_tool_that_clears_the_condition_is_never_the_one_held(tmp_path: Path) -> None:
    (tmp_path / ".git").mkdir()
    loop = _loop(tmp_path)
    loop.state.migration = MigrationState(active=True)
    from dakcoder_shared.llm import ToolCall

    assert loop._migration_guard(ToolCall(id="1", name="git_ops", arguments="{}")) is None
    assert loop._migration_guard(ToolCall(id="2", name="read_file", arguments="{}")) is None


def test_nothing_is_held_outside_a_git_repository(tmp_path: Path) -> None:
    """"If a git repo is provided" is the rule, and the absence of one is not a failure."""
    loop = _loop(tmp_path)
    loop.state.migration = MigrationState(active=True)
    from dakcoder_shared.llm import ToolCall

    assert loop._migration_guard(ToolCall(id="1", name="write_file", arguments="{}")) is None


def test_nothing_is_held_for_an_ordinary_change(tmp_path: Path) -> None:
    (tmp_path / ".git").mkdir()
    loop = _loop(tmp_path)
    from dakcoder_shared.llm import ToolCall

    assert loop._migration_guard(ToolCall(id="1", name="write_file", arguments="{}")) is None


def test_the_branch_is_cut_from_the_base_so_it_carries_that_code(workspace) -> None:
    """The point of `base`: the new branch replicates `development`, not HEAD."""
    _git(workspace, "init", "-b", "main")
    _git(workspace, "add", "-A")
    _git(workspace, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-m", "base")
    _git(workspace, "branch", "development")
    (workspace.root / "only-on-development.txt").write_text("x", encoding="utf-8")
    _git(workspace, "checkout", "development")
    _git(workspace, "add", "-A")
    _git(workspace, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-m", "dev only")
    _git(workspace, "checkout", "main")

    result = commands.git_ops(
        _call(
            "git_ops",
            {"op": "branch", "message": "template-conversion", "base": "development"},
            workspace,
        )
    )
    assert result.ok, result.content
    assert result.meta["branch"] == "template-conversion"
    assert result.meta["base"] == "development"
    assert (workspace.root / "only-on-development.txt").exists(), (
        "the new branch did not replicate development's code"
    )


def test_a_base_that_does_not_exist_is_a_named_dead_end(workspace) -> None:
    """It does not quietly fall back to HEAD, which is the wrong branch by definition."""
    _git(workspace, "init", "-b", "main")
    _git(workspace, "add", "-A")
    _git(workspace, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-m", "base")

    result = commands.git_ops(
        _call(
            "git_ops",
            {"op": "branch", "message": "template-conversion", "base": "development"},
            workspace,
        )
    )
    assert not result.ok
    assert "origin/development" in result.content
    assert "ask the developer" in result.fix
    assert result.meta.get("dead_end")


def test_git_status_lists_the_branches_so_the_question_can_be_answered(workspace) -> None:
    """"Does `development` exist" had no tool that could answer it."""
    _git(workspace, "init", "-b", "main")
    _git(workspace, "add", "-A")
    _git(workspace, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-m", "base")
    _git(workspace, "branch", "development")

    result = commands.git_status(
        _call("git_status", {}, workspace)
    )
    assert result.ok
    assert "branches:" in result.content
    assert "development" in result.content


def _call(name: str, arguments: dict, workspace) -> Invocation:
    return Invocation(registry.get(name), arguments, workspace)


def _git(workspace, *args: str) -> None:
    done = commands.run(["git", *args], workspace.root, timeout=60)
    assert done.ok, f"git {' '.join(args)}: {done.output}"


# ── 4. never the whole codebase in one go ───────────────────────────────────


def test_a_phase_closes_only_when_its_own_steps_have_settled(tmp_path: Path) -> None:
    loop = _migrating(tmp_path)
    loop.state.plan = (
        PlanStep("go.mod", "swap", "tidy", phase="deps", status="done"),
        PlanStep("go.sum", "tidy", "tidy", phase="deps", status="pending"),
    )
    assert loop._close_phase() == "", "an open step keeps the phase open"

    loop.state.plan = tuple(replace(s, status="done") for s in loop.state.plan)
    assert loop._close_phase() == "deps"
    assert loop.state.migration.current[1].name == "handlers"


def test_the_plan_decides_which_phase_closes_not_the_roadmap(tmp_path: Path) -> None:
    """They differ legitimately, and the plan is the commitment.

    A developer whose branch already exists has the run open at phase two while
    phase one is still pending. Closing the roadmap's first pending phase when
    that work settled would close the wrong one.
    """
    loop = _loop(tmp_path)
    loop.state.migration = MigrationState(active=True, branch="tc")
    loop.state.migration.adopt(_phases())
    loop.state.plan = (PlanStep("go.mod", "swap", "tidy", phase="deps", status="done"),)

    assert loop.state.migration.current[1].name == "branch", "the roadmap still says branch"
    assert loop._close_phase() == "deps"
    assert loop.state.migration.phase_named("deps").status == "done"
    assert loop.state.migration.phase_named("branch").status == "pending"


def test_a_phase_with_no_steps_tagged_for_it_is_still_reachable(tmp_path: Path) -> None:
    """The fallback, and it matters more than the rule.

    A roadmap under a plan whose steps carry no phase would otherwise be a
    migration that can never reach its last phase — and therefore never runs
    the gate at all. That is the permanently-unsatisfiable shape this codebase
    keeps rebuilding.
    """
    loop = _migrating(tmp_path)
    loop.state.plan = (PlanStep("go.mod", "swap", "tidy", status="done"),)
    assert loop._close_phase() == "deps"


def test_a_closed_phase_ends_the_run_instead_of_opening_the_next(tmp_path: Path) -> None:
    loop = _migrating(tmp_path)
    loop.state.plan = (PlanStep("go.mod", "swap", "tidy", phase="deps", status="done"),)
    loop.router._mutations = getattr(loop.router, "_mutations", 0)

    events = list(loop._phase_checkpoint())

    assert loop.result is not None and loop.result.outcome == "done"
    summary = loop.result.summary
    assert "phase 2 of 3 — deps — is complete" in summary
    assert "Next is phase 3: handlers" in summary
    assert "Say when to open the next phase" in summary, "the developer decides, not the run"
    assert any(e.data.get("kind") == "phase" for e in events)


def test_the_state_block_frames_the_plan_as_one_phase_of_a_migration(tmp_path: Path) -> None:
    loop = _migrating(tmp_path)
    loop.state.plan = (PlanStep("go.mod", "swap the deps", "tidy", phase="deps", part="go get"),)
    block = loop._state_block()

    assert "Migration: phase 2 of 3 — deps" in block
    assert "Parts: go get, tidy, replace" in block
    assert "Then: phase 3 — handlers" in block
    assert "gate is deferred" in block
    assert "of phase deps" in block
    assert "Part: go get" in block


def test_an_unbranched_migration_says_so_in_the_state_block(tmp_path: Path) -> None:
    loop = _loop(tmp_path)
    loop.state.migration = MigrationState(active=True)
    loop.state.migration.adopt(_phases())
    assert "no branch yet" in loop._state_block()


def test_ask_developer_is_offered_mid_migration_and_withheld_otherwise(
    tmp_path: Path, planning_router
) -> None:
    """Requirement four's other half: interact often, from the phase that acts."""
    loop = _migrating(tmp_path)
    loop.router = planning_router
    loop.state.mode = Mode.AGENT
    assert "ask_developer" in [t["function"]["name"] for t in loop._tools()]

    ordinary = _loop(tmp_path)
    ordinary.router = planning_router
    ordinary.state.mode = Mode.AGENT
    assert "ask_developer" not in [t["function"]["name"] for t in ordinary._tools()]
    # Withheld from the schema, not from the router: a replayed stale list still
    # dispatches rather than being refused for a mode it now holds.
    assert Mode.AGENT in registry.get("ask_developer").modes


# ── the roadmap outlives the message ────────────────────────────────────────


def test_the_roadmap_survives_a_restart(tmp_path: Path) -> None:
    """A migration is many messages by construction; a per-message roadmap is none."""
    state = MigrationState(active=True, branch="template-conversion", base="development")
    state.adopt(_phases())
    state.close("branch")
    record = PlanRecord(session_id="s1")
    record.migration = state
    record = record.record((PlanStep("go.mod", "swap", "tidy", phase="deps"),), "convert")
    record.save(tmp_path)

    back = PlanRecord.load(tmp_path, "s1")
    assert back is not None
    assert [p.name for p in back.migration.phases] == ["branch", "deps", "handlers"]
    assert back.migration.current[1].name == "deps"
    assert back.migration.branch == "template-conversion"
    assert back.steps[0].phase == "deps"


def test_re_submitting_the_roadmap_does_not_reopen_closed_phases() -> None:
    """The same rule `_adopt_plan` follows, one level up — and for the same reason.

    Every phase opens with a `submit_plan`, so a re-submitted roadmap is the
    common case, not the exception.
    """
    state = MigrationState(active=True)
    state.adopt(_phases())
    state.close("branch")
    state.adopt(_phases())
    assert state.current[1].name == "deps", "re-planning sent the run back to phase one"


# ── a deletion the plan asked for is not a loss ─────────────────────────────
#
# `removed` is the ledger of files *gone*, and every deletion used to enter it,
# including the ones the plan exists to carry out. `_deleted_and_not_replaced`
# reads it against the disk and nothing else, so a migration that removed
# `routes/routes.go` exactly as its step said had its first `finish` refused
# with "you deleted routes/routes.go ... so that file is simply gone" -- and
# only got past it because `MAX_FINISH_REFUSALS` happens to be 1.


def test_a_deletion_the_step_asked_for_does_not_refuse_the_finish(planning_router) -> None:
    workspace_root = planning_router.workspace.root
    (workspace_root / "routes").mkdir(parents=True, exist_ok=True)
    (workspace_root / "routes" / "routes.go").write_text("package routes\n", encoding="utf-8")

    loop, _ = build(
        planning_router,
        [
            calls(
                (
                    "delete_file",
                    json.dumps({"path": "routes/routes.go", "reason": "retired by the template"}),
                )
            ),
            calls(("finish", json.dumps({"answer": "the routes file is gone."}))),
        ],
        migration=True,
    )
    loop.state.migration.branch = "template-conversion"
    loop.state.plan = (
        PlanStep("routes/routes.go", "delete the retired routes file", "it is gone", phase="deps"),
    )
    list(loop.run("remove the routes file", intent=Intent.AGENT, continued=True))

    assert not (workspace_root / "routes" / "routes.go").exists()
    assert loop.state.removed == set(), "a planned deletion is not an outstanding loss"
    assert loop._why_not_done() == ""
    assert loop.state.plan[0].status in ("written", "done")
    told = [
        m.content
        for m in loop.context.build()
        if m.role.value == "user" and "You deleted" in (m.content or "")
    ]
    assert not told, "the run was told it had lost a file its plan removed on purpose"


def test_an_unplanned_deletion_still_counts_as_a_loss(tmp_path: Path) -> None:
    """The guard this narrows, unchanged. A file deleted with no step asking for
    it and no replacement written is still four handlers gone."""
    loop = _migrating(tmp_path, phase="handlers")
    loop.state.plan = (
        PlanStep("handler/paogen.go", "convert it", "go build", phase="handlers", status="done"),
    )
    loop.state.removed = {"handler/paogen.go"}

    assert "you deleted handler/paogen.go" in loop._why_not_done()


# ── delete, restore, delete ─────────────────────────────────────────────────
#
# Every turn of that cycle mutates the workspace, so `stalled_turns` -- which
# resets on any mutation -- reset on every turn of it. The run oscillated on
# `go.work` for eighteen turns and was ended by the finish-refusal budget rather
# than by any bound that knew what was happening.


def test_a_first_deletion_is_ordinary_work(tmp_path: Path) -> None:
    loop = _migrating(tmp_path)
    assert loop._note_delete("go.work") == 0
    assert loop.state.churn == {}


def test_a_delete_after_a_restore_is_a_cycle(tmp_path: Path) -> None:
    loop = _migrating(tmp_path)
    loop._note_delete("go.work")
    loop._note_write("go.work")

    assert loop._note_delete("go.work") == 1
    assert loop.state.churn["go.work"] == 1

    loop._note_write("go.work")
    assert loop._note_delete("go.work") == 2


def test_writing_a_file_this_run_never_deleted_arms_nothing(tmp_path: Path) -> None:
    loop = _migrating(tmp_path)
    loop._note_write("handler/user.go")
    assert loop._note_delete("handler/user.go") == 0, "a first deletion is not a cycle"


def test_rewriting_the_replacement_does_not_make_a_cycle(tmp_path: Path) -> None:
    """Delete, write, append, patch is one replacement, however many writes it
    takes. What closes a cycle is the *second delete*."""
    loop = _migrating(tmp_path)
    loop._note_delete("handler/paogen.go")
    for _ in range(3):
        assert loop._note_write("handler/paogen.go") == 0
    assert loop.state.churn == {}


def test_the_cycle_survives_a_developer_message(tmp_path: Path) -> None:
    """`carry_from`, because the loop it catches spanned three messages in the
    field -- a counter restarting at each one would read "no cycles yet" on
    every turn the run was most stuck."""
    first = _migrating(tmp_path)
    first._note_delete("go.work")
    first._note_write("go.work")
    first._note_delete("go.work")

    second = _migrating(tmp_path)
    second.carry_from(first)

    assert second.state.churn == {"go.work": 1}
    assert second._note_delete("go.work") == 1, "the ledger came with it"


def _cycling(planning_router, turns, **kw):
    """A run whose plan step covers nothing, on a workspace holding `go.work`.

    The unsplit shape, forced past `_normalise_plan`, so these pin the *guard*
    rather than the repair: a step that covers nothing is still possible
    whenever a path will not resolve.
    """
    (planning_router.workspace.root / "go.work").write_text("go 1.25.0\n", encoding="utf-8")
    loop, _ = build(planning_router, turns, migration=True, **kw)
    loop.state.migration.branch = "template-conversion"
    loop.state.plan = (
        PlanStep("go.work, go.work.sum", "Delete go.work and go.work.sum", "gone", phase="deps"),
    )
    return loop


def _delete(path: str = "go.work"):
    return calls(("delete_file", json.dumps({"path": path, "reason": "the plan says so"})))


def _rewrite(path: str = "go.work"):
    return calls(("write_file", json.dumps({"path": path, "content": "go 1.25.0\n"})))


def test_the_second_delete_is_told_about_as_a_cycle_not_as_a_loss(planning_router) -> None:
    """The two objections cannot both be sent: `removed_open`'s remedy -- write
    it back -- is what produced the second deletion."""
    loop = _cycling(
        planning_router,
        [_delete(), _rewrite(), _delete(), _rewrite(), _delete()],
        max_turns=6,
    )
    list(loop.run("clean up the workspace files", intent=Intent.AGENT, continued=True))

    said = [m.content or "" for m in loop.context.build() if m.role.value == "user"]
    cycle = [m for m in said if "That is a cycle" in m]
    assert cycle, "the run was never told it was going round"
    assert "revise_plan" in cycle[0]
    assert "ends the run as stalled" in cycle[0]
    assert loop.state.churn.get("go.work", 0) >= 1


def test_a_cycle_is_told_about_even_when_the_step_asked_for_the_removal(
    planning_router,
) -> None:
    """A turn that goes round stops counting as progress whatever the plan says,
    so it has to be explained whatever the plan says. A stall counter ticking
    with nothing saying why is the state this whole fix is about."""
    (planning_router.workspace.root / "go.work").write_text("go 1.25.0\n", encoding="utf-8")
    loop, _ = build(
        planning_router,
        [_delete(), _rewrite(), _delete()],
        migration=True,
        max_turns=5,
    )
    loop.state.migration.branch = "template-conversion"
    loop.state.plan = (
        PlanStep("go.work", "delete the workspace file", "it is gone", phase="deps"),
    )
    list(loop.run("drop the workspace file", intent=Intent.AGENT, continued=True))

    said = [m.content or "" for m in loop.context.build() if m.role.value == "user"]
    assert [m for m in said if "That is a cycle" in m], "the cycle went unexplained"
    assert loop.state.churn.get("go.work", 0) >= 1


def test_a_turn_that_only_re_deletes_is_not_progress(planning_router) -> None:
    """The bound that could not see the loop. Every turn of it mutated the
    workspace, so the six-stall ending never came near firing."""
    loop = _cycling(
        planning_router,
        [_delete(), _rewrite(), _delete(), _rewrite(), _delete(), _rewrite()],
        max_turns=8,
    )
    list(loop.run("clean up the workspace files", intent=Intent.AGENT, continued=True))

    assert loop.state.stalled_turns >= 1, "the cycle still reset the stall counter"
    assert loop.state.must_answer or loop.result is not None, "nothing ended the cycle"


def test_the_split_plan_closes_the_step_and_never_cycles(planning_router) -> None:
    """The repair and the guard together, on the plan that produced the bug.
    Split, the deletion satisfies the step it was asked for, so nothing tells
    the model to write the file back and there is no cycle to catch."""
    workspace_root = planning_router.workspace.root
    (workspace_root / "go.work").write_text("go 1.25.0\n", encoding="utf-8")
    (workspace_root / "go.work.sum").write_text("h1:x\n", encoding="utf-8")

    loop, _ = build(
        planning_router,
        [
            _delete("go.work"),
            _delete("go.work.sum"),
            calls(("finish", json.dumps({"answer": "both workspace files are gone."}))),
        ],
        migration=True,
        max_turns=6,
    )
    loop.state.migration.branch = "template-conversion"
    loop.state.plan = loop._normalise_plan(
        (
            PlanStep(
                "go.work, go.work.sum",
                "Delete go.work and go.work.sum, which are local workspace artefacts",
                "both files are gone",
                phase="deps",
            ),
        )
    )
    list(loop.run("drop the go workspace files", intent=Intent.AGENT, continued=True))

    assert not (workspace_root / "go.work").exists()
    assert not (workspace_root / "go.work.sum").exists()
    assert all(s.status in ("written", "done") for s in loop.state.plan), (
        "each path was closed by the deletion that carried it out"
    )
    assert loop.state.churn == {}, "nothing went round"
    assert loop.state.removed == set()
    said = [m.content or "" for m in loop.context.build() if m.role.value == "user"]
    assert not [m for m in said if "You deleted" in m]


# ── an acceptance criterion the acting phase can run ────────────────────────
#
# The same plan accepted all seven of its steps on "legacy_audit reports no
# findings". `legacy_audit` is an ask/planner tool: the acting phase called it,
# was refused, and spent the turn learning that its own plan had given it
# nothing it could check.


def test_a_criterion_naming_a_tool_this_phase_lacks_is_found(tmp_path: Path) -> None:
    loop = _migrating(tmp_path)
    loop.state.plan = (
        PlanStep("go.mod", "swap the deps", "legacy_audit reports no findings", phase="deps"),
        PlanStep("main.go", "rewire it", "go build succeeds", phase="deps"),
    )
    assert loop._cited_but_unrunnable() == ["legacy_audit"]


def test_a_criterion_the_phase_can_run_is_not_commented_on(tmp_path: Path) -> None:
    loop = _migrating(tmp_path)
    loop.state.plan = (
        PlanStep("go.mod", "swap the deps", "go build and go_vet are clean", phase="deps"),
        PlanStep("main.go", "rewire", "rules_lint finds nothing; read_file shows it", phase="deps"),
    )
    assert loop._cited_but_unrunnable() == []


def test_a_gate_tool_is_a_criterion_the_run_does_apply(tmp_path: Path) -> None:
    """The model never calls `gofmt` or `swagger_check` and does not need to --
    the gate runs them on a schedule, so accepting a step on one is fine."""
    loop = _migrating(tmp_path)
    loop.state.plan = (
        PlanStep("main.go", "rewire it", "gofmt is clean and swagger_check passes", phase="deps"),
    )
    assert loop._cited_but_unrunnable() == []


def test_the_plan_is_adopted_and_the_criterion_is_pointed_out(tmp_path: Path) -> None:
    """A note, not an objection: `MAX_PLAN_OBJECTIONS` is two and the migration
    shape objections have first claim on both."""
    loop = _migrating(tmp_path)
    steps = (
        PlanStep("go.mod", "swap the deps", "legacy_audit reports no findings", phase="deps"),
    )
    list(loop._adopt_plan(steps, "migrate"))

    assert loop.state.plan == steps, "the plan stands"
    said = [m.content or "" for m in loop.context.build() if m.role.value == "user"]
    note = [m for m in said if "legacy_audit" in m]
    assert note, "nothing said the criterion could not be applied"
    assert "does not have" in note[0]
    assert "go_build" in note[0]


def test_a_clean_plan_gets_no_note(tmp_path: Path) -> None:
    loop = _migrating(tmp_path)
    list(loop._adopt_plan(
        (PlanStep("go.mod", "swap the deps", "go build succeeds", phase="deps"),), "migrate"
    ))
    said = [m.content or "" for m in loop.context.build() if m.role.value == "user"]
    assert not [m for m in said if "the acting phase does not have" in m]


# ── a failed `go get` says which failure it was ─────────────────────────────
#
# One hint used to be attached to every `go get` failure, whatever the toolchain
# said: "if this is a private module, GOPRIVATE and a git credential must be
# configured". A field run hit `unknown revision` on six modules, was handed the
# private-module hint, and reported to the developer that their GitLab
# credentials were missing. They were not — `go list -m -versions` on the same
# machine answered in full. The run ended blocked on a diagnosis the tool had
# supplied and the evidence contradicted.


def test_a_version_that_does_not_exist_says_so_and_names_the_move() -> None:
    fix, dead_end = commands._why_get_failed(
        "go: gitlab.cept.gov.in/it-2.0-common/n-api-db@v1.0.32: invalid version: "
        "unknown revision v1.0.32",
        "gitlab.cept.gov.in/it-2.0-common/n-api-db",
        "v1.0.32",
    )
    assert "not a published version" in fix
    assert "`version` omitted" in fix
    assert "Do not guess" in fix
    assert dead_end, "the same call fails the same way every time it is asked"


def test_the_version_miss_never_blames_the_credentials() -> None:
    """The whole point. GOPRIVATE was correct; saying otherwise cost the run."""
    fix, _ = commands._why_get_failed(
        "go: unknown revision v1.1.5", "gitlab.cept.gov.in/it-2.0-common/n-api-log", "v1.1.5"
    )
    assert "GOPRIVATE" not in fix
    assert "credential" not in fix


def test_an_unreachable_host_still_gets_the_private_module_hint() -> None:
    for said in (
        "fatal: could not read Username for 'https://gitlab.cept.gov.in': "
        "terminal prompts disabled",
        "dial tcp 10.0.0.1:443: i/o timeout",
        "x509: certificate signed by unknown authority",
    ):
        fix, dead_end = commands._why_get_failed(said, "gitlab.cept.gov.in/it-2.0-common/n-api-db", "")
        assert "GOPRIVATE" in fix, said
        assert not dead_end, "a host that is down now may be up next turn"


def test_reachability_is_read_before_the_version(tmp_path: Path) -> None:
    """A credential failure mentioning a version must not be called a bad tag."""
    fix, _ = commands._why_get_failed(
        "go: n-api-db@v0.0.1: unknown revision v0.0.1\n"
        "\tfatal: could not read Username for 'https://gitlab.cept.gov.in'",
        "gitlab.cept.gov.in/it-2.0-common/n-api-db",
        "v0.0.1",
    )
    assert "GOPRIVATE" in fix


def test_an_unrecognised_failure_points_at_the_output(tmp_path: Path) -> None:
    fix, dead_end = commands._why_get_failed("go: some new thing went wrong", "x/y", "v1")
    assert "output above" in fix
    assert not dead_end, "an unclassified failure is not known to be permanent"


def test_the_dead_end_reaches_the_tool_result(workspace) -> None:
    """Through `_result`, so the loop's ledger can answer the repeat."""
    done = commands.Completed(
        argv=["go", "get", "x@v9"],
        code=1,
        output="go: x@v9: invalid version: unknown revision v9",
        seconds=0.1,
    )
    fix, dead_end = commands._why_get_failed(done.output, "x", "v9")
    out = commands._result(done, what="go get x@v9", fix_on_fail=fix, meta={"dead_end": dead_end})
    assert not out.ok
    assert out.meta.get("dead_end")
    assert "GOPRIVATE" not in out.for_model()


def test_a_timeout_keeps_the_private_module_hint(workspace) -> None:
    """That one is right where it is: a first fetch from gitlab is what hangs."""
    done = commands.Completed(argv=["go", "get", "x"], code=1, output="", seconds=180, timed_out=True)
    out = commands._result(done, what="go get x", fix_on_fail="ignored when it timed out")
    assert "GOPRIVATE" in out.for_model()


# ── the version tool reaches the phase that changes versions ────────────────


def test_lib_version_check_is_offered_mid_migration_and_withheld_otherwise(
    tmp_path: Path, planning_router
) -> None:
    """Its whole subject is the first phase of a conversion, and the phase that
    works that conversion could not call it. Offering it outside one is what the
    original restriction was right about: a library bump in the middle of
    unrelated work turns a review into a regression hunt."""
    loop = _migrating(tmp_path)
    loop.router = planning_router
    loop.state.mode = Mode.AGENT
    assert "lib_version_check" in [t["function"]["name"] for t in loop._tools()]

    ordinary = _loop(tmp_path)
    ordinary.router = planning_router
    ordinary.state.mode = Mode.AGENT
    assert "lib_version_check" not in [t["function"]["name"] for t in ordinary._tools()]
    # Withheld from the schema, not from the router, like `ask_developer`.
    assert Mode.AGENT in registry.get("lib_version_check").modes


def test_the_surveys_still_hold_it(tmp_path: Path) -> None:
    spec = registry.get("lib_version_check")
    assert {Mode.ASK, Mode.PLANNER} <= spec.modes


def test_withholding_does_not_touch_the_other_acting_tools(
    tmp_path: Path, planning_router
) -> None:
    """A set was introduced where there had been one name; the risk is that it
    quietly takes something else with it."""
    ordinary = _loop(tmp_path)
    ordinary.router = planning_router
    ordinary.state.mode = Mode.AGENT
    offered = {t["function"]["name"] for t in ordinary._tools()}
    assert {"write_file", "patch_file", "go_build", "go_mod", "finish"} <= offered


# ── a question that was answered is not asked again ─────────────────────────
#
# `ask_developer` is exempt from all three intercept ledgers, because a repeated
# *terminal* call is a signal rather than a question — a model trying to stop,
# which `_phase_ended`'s bounded refusals answer. That is right for `finish`, and
# it left `ask_developer` as the one call in the system that could repeat
# verbatim forever with nothing counting it.
#
# A field run asked "which n-api-* versions should I use?", was told "use latest
# versions", and asked the identical question twice more. Each answer ended one
# run and started another that re-derived the same question from the same
# unchanged cursor; nothing anywhere recorded that it had been settled.


def _ask(*questions: str) -> ToolResult:
    return ToolResult.success(
        "\n".join(questions), meta={"control": "ask", "questions": list(questions)}
    )


def test_the_first_asking_goes_through(tmp_path: Path) -> None:
    loop = _migrating(tmp_path)
    list(loop._phase_ended("ask_developer", _ask("which versions?")))

    assert loop.result is not None, "the run ends with the question on screen"
    assert loop.state.asked, "the question was not recorded"
    assert loop.state.reasks == 0


def test_the_same_question_after_an_answer_is_sent_back(tmp_path: Path) -> None:
    loop = _migrating(tmp_path)
    list(loop._phase_ended("ask_developer", _ask("which versions?")))
    loop.state.answered = "use latest versions"
    loop.result = None

    list(loop._phase_ended("ask_developer", _ask("which versions?")))

    assert loop.result is None, "the run was ended on a question already answered"
    said = [m.content or "" for m in loop.context.build() if m.role.value == "user"]
    back = [m for m in said if "already asked that" in m]
    assert back, "the model was not told it had been answered"
    assert "use latest versions" in back[0], "the answer was not handed back"
    assert "blocked" in back[0], "the exit from an unanswerable step is not named"


def test_the_repeat_is_recognised_through_rewording(tmp_path: Path) -> None:
    """A model re-asking rarely reproduces its own formatting."""
    loop = _migrating(tmp_path)
    list(loop._phase_ended("ask_developer", _ask("Which versions?", "And the branch?")))
    loop.state.answered = "latest, and cut from main"
    loop.result = None

    list(loop._phase_ended("ask_developer", _ask("and   the BRANCH?", "which versions?")))

    assert loop.result is None, "order and case are not what makes a question different"


def test_a_different_question_is_put_to_the_developer(tmp_path: Path) -> None:
    loop = _migrating(tmp_path)
    list(loop._phase_ended("ask_developer", _ask("which versions?")))
    loop.state.answered = "use latest versions"
    loop.result = None

    list(loop._phase_ended("ask_developer", _ask("which branch should I cut from?")))

    assert loop.result is not None, "a genuinely new question must reach the developer"


def test_the_second_asking_is_believed(tmp_path: Path) -> None:
    """Bounded at one, like every other refusal here: the model may be asking
    something the answer genuinely did not cover."""
    loop = _migrating(tmp_path)
    list(loop._phase_ended("ask_developer", _ask("which versions?")))
    loop.state.answered = "use latest versions"

    loop.result = None
    list(loop._phase_ended("ask_developer", _ask("which versions?")))
    assert loop.result is None, "the first repeat is sent back"

    loop.result = None
    list(loop._phase_ended("ask_developer", _ask("which versions?")))
    assert loop.result is not None, "the second repeat is put to the developer"


def test_asking_something_new_clears_the_previous_answer(tmp_path: Path) -> None:
    """What is outstanding is *this* question, and the old answer is not a reply
    to it."""
    loop = _migrating(tmp_path)
    loop.state.answered = "use latest versions"
    list(loop._phase_ended("ask_developer", _ask("which branch?")))
    assert loop.state.answered == ""


def test_the_ledger_survives_a_developer_message(tmp_path: Path) -> None:
    """A question settled on message two is settled on message five."""
    first = _migrating(tmp_path)
    list(first._phase_ended("ask_developer", _ask("which versions?")))
    first.state.answered = "use latest versions"

    second = _migrating(tmp_path)
    second.carry_from(first)

    assert second.state.asked == first.state.asked
    assert second.state.answered == "use latest versions"


# ── a step that cannot be done ──────────────────────────────────────────────
#
# The status the plan did not have. A step whose premise is false stayed
# `pending`, `active_step` returned it every turn for the rest of the run, and
# `_plan_block` re-rendered it into the recency slot as an instruction the model
# could not carry out. Three field loops have exactly that shape. `skipped` was
# the only word the model could reach for and it means "unnecessary", which a
# blocked step is not.


def test_a_blocked_step_lets_the_cursor_move(tmp_path: Path) -> None:
    loop = _loop(tmp_path)
    loop.state.plan = (
        PlanStep("go.mod", "swap the deps", "go build", status="blocked", note="versions unknown"),
        PlanStep("main.go", "rewire it", "go build"),
    )
    index, step = loop.active_step
    assert (index, step.file) == (2, "main.go"), "the cursor stayed on a step nothing can finish"


def test_a_blocked_step_is_not_open_and_not_done(tmp_path: Path) -> None:
    step = PlanStep("go.mod", "swap", "go build", status="blocked", note="why")
    assert not step.open, "an open blocked step keeps the cursor frozen"
    assert step.status != "done", "blocked must not read as finished"


def test_a_blocked_step_does_not_refuse_the_finish(tmp_path: Path) -> None:
    """Refusing over it would rebuild the permanently-unsatisfiable condition
    the status exists to remove."""
    loop = _loop(tmp_path)
    loop.state.plan = (
        PlanStep("go.mod", "swap", "go build", status="blocked", note="versions unknown"),
    )
    assert loop._why_not_done() == ""


def test_a_blocked_step_is_reported_with_its_reason(tmp_path: Path) -> None:
    loop = _loop(tmp_path)
    loop.state.plan = (
        PlanStep("go.mod", "swap", "go build", status="blocked", note="versions unknown"),
    )
    said = loop._unfinished()
    assert "go.mod" in said and "versions unknown" in said
    assert "Blocked" in said


def test_a_blocked_step_is_not_also_reported_as_never_written(tmp_path: Path) -> None:
    """Two objections about one step read as two problems."""
    loop = _loop(tmp_path)
    loop.state.plan = (
        PlanStep("go.mod", "swap", "go build", status="blocked", note="why"),
    )
    assert loop._unwritten_targets() == []


def test_a_blocked_step_closes_its_phase(tmp_path: Path) -> None:
    """Otherwise one unreachable step holds the whole migration open, which is
    the failure this status exists to end."""
    loop = _migrating(tmp_path)
    loop.state.plan = (
        PlanStep("go.mod", "swap", "go build", phase="deps", status="blocked", note="why"),
    )
    assert loop._close_phase() == "deps"


def test_the_model_may_set_blocked_but_not_done() -> None:
    assert "blocked" in MODEL_STATUSES
    assert "done" not in MODEL_STATUSES
    assert "written" not in MODEL_STATUSES


def test_a_replan_that_renames_a_blocked_step_retries_it(tmp_path: Path) -> None:
    """Re-stating the step *is* the retry."""
    loop = _loop(tmp_path)
    loop.state.plan = (
        PlanStep("go.mod", "swap", "go build", status="blocked", note="versions unknown"),
    )
    list(loop._adopt_plan((PlanStep("go.mod", "swap, with go get", "go build"),), ""))

    assert [s.status for s in loop.state.plan] == ["pending"]


def test_a_replan_that_omits_a_blocked_step_keeps_it(tmp_path: Path) -> None:
    """Not a retraction — and the reason is what the developer needs."""
    loop = _loop(tmp_path)
    loop.state.plan = (
        PlanStep("go.mod", "swap", "go build", status="blocked", note="versions unknown"),
    )
    list(loop._adopt_plan((PlanStep("main.go", "rewire", "go build"),), ""))

    by_file = {s.file: s for s in loop.state.plan}
    assert by_file["go.mod"].status == "blocked"
    assert by_file["go.mod"].note == "versions unknown"


def test_the_block_shows_what_the_cursor_moved_past(tmp_path: Path) -> None:
    loop = _loop(tmp_path)
    loop.state.plan = (
        PlanStep("go.mod", "swap", "go build", status="blocked", note="why"),
        PlanStep("main.go", "rewire", "go build"),
    )
    assert any("Blocked: 1 go.mod" in line for line in loop._plan_block())


# ── the cursor names the exit before the budget runs out ────────────────────


def test_a_stuck_cursor_eventually_names_the_exit(tmp_path: Path) -> None:
    """Everything else the block says is a restatement of an order the model has
    already failed to carry out, and a restated order is what it loops on."""
    loop = _loop(tmp_path)
    step = PlanStep("go.mod", "swap the deps", "go build")
    loop.state.plan = (step,)

    loop.context._turn = 1
    loop._cursor_age(step)
    loop.context._turn = 1 + STUCK_TURNS
    line = loop._cursor_age(step)

    assert "revise_plan" in line
    assert "`blocked`" in line
    assert "not giving up" in line


def test_an_ordinary_step_never_sees_that_line(tmp_path: Path) -> None:
    """Read the file, plan the edit, make it — three turns, and nothing is wrong."""
    loop = _loop(tmp_path)
    step = PlanStep("go.mod", "swap the deps", "go build")
    loop.state.plan = (step,)

    loop.context._turn = 1
    loop._cursor_age(step)
    loop.context._turn = 3
    assert "revise_plan" not in loop._cursor_age(step)


# ── a phase that writes nothing is still a phase ────────────────────────────
#
# The migration's first phase cuts a branch. `git_ops` does that, and cutting a
# branch touches no file, so `router.mutations` stays 0 — and `_verify` returned
# on "nothing was changed, so there was nothing to verify" *before* reaching
# `_phase_checkpoint`, which is the only caller of `_close_phase`.
#
# So the branch phase could never close, from any mode. A field session ran to
# turn 30 with `Phase(name='branch', status='pending')` while the branch existed
# and two `finish` calls had said the phase was complete; the state block told
# the model "Migration: phase 1 of 7 — branch" in the recency slot on every
# turn, and it re-derived "the branch phase is done" in prose each time — twice
# byte-identically, at twenty-five seconds a turn.


def _branching(tmp_path: Path) -> AgentLoop:
    """A migration on its branch phase, with the branch already cut."""
    loop = _migrating(tmp_path, phase="branch")
    loop.state.migration.branch = "migrate-to-n-api-template"
    loop.state.migration.base = "main"
    loop._relay = lambda event: None
    return loop


def test_a_phase_that_wrote_nothing_still_reaches_the_checkpoint(tmp_path: Path) -> None:
    loop = _branching(tmp_path)
    loop.state.plan = (
        PlanStep("go.mod", "Cut the migration branch from main.", "the branch exists",
                 phase="branch", status="skipped"),
    )
    assert loop.router.mutations == 0, "the premise: a branch cut writes no file"

    list(loop._verify())

    assert loop.state.migration.phase_named("branch").status == "done"


def test_the_zero_mutation_report_is_unchanged_outside_a_migration(tmp_path: Path) -> None:
    """The check moved above that one; it must not have replaced it."""
    loop = _loop(tmp_path)
    loop._relay = lambda event: None
    list(loop._verify())

    assert loop.result is not None
    assert "nothing was changed" in loop.result.summary


def test_a_finish_from_the_planner_closes_the_phase(tmp_path: Path) -> None:
    """A migration phase can finish in PLANNER — a branch cut needs no write, so
    its plan settles without the run ever entering AGENT. That `finish` used to
    end the run DONE while touching no phase state at all."""
    loop = _branching(tmp_path)
    loop.state.mode = Mode.PLANNER
    loop.state.plan = (
        PlanStep("go.mod", "Cut the migration branch.", "it exists",
                 phase="branch", status="skipped"),
    )

    list(loop._phase_ended("finish", ToolResult.success(
        "done", meta={"answer": "Phase 1 (branch) is complete.", "blocked": ""}
    )))

    assert loop.state.migration.phase_named("branch").status == "done"


def test_a_finish_from_ask_closes_nothing(tmp_path: Path) -> None:
    """A question asked mid-migration is still a question."""
    loop = _branching(tmp_path)
    loop.state.mode = Mode.ASK
    loop.state.plan = (
        PlanStep("go.mod", "Cut the branch.", "it exists", phase="branch", status="skipped"),
    )

    list(loop._phase_ended("finish", ToolResult.success(
        "answered", meta={"answer": "the branch is migrate-to-n-api-template", "blocked": ""}
    )))

    assert loop.state.migration.phase_named("branch").status == "pending"


# ── a phase closes on its own evidence ─────────────────────────────────────


def test_the_branch_phase_is_evidenced_by_the_branch(tmp_path: Path) -> None:
    state = MigrationState(active=True)
    state.adopt(_phases())
    assert not state.evidenced("branch"), "no branch cut yet"

    state.branch = "migrate-to-n-api-template"
    assert state.evidenced("branch")


def test_a_shared_branch_is_not_evidence(tmp_path: Path) -> None:
    """Standing on `main` is not having cut a migration branch."""
    state = MigrationState(active=True)
    state.adopt(_phases())
    for shared in ("main", "development", "Master"):
        state.branch = shared
        assert not state.evidenced("branch"), shared


def test_no_other_phase_is_evidenced_by_a_branch(tmp_path: Path) -> None:
    state = MigrationState(active=True)
    state.adopt(_phases())
    state.branch = "migrate-to-n-api-template"
    assert not state.evidenced("deps")
    assert not state.evidenced("handlers")


def test_an_open_step_does_not_hold_the_branch_phase_once_it_is_cut(tmp_path: Path) -> None:
    """The steps are the weakest possible signal here: a branch cut writes no
    file for a step to land on."""
    loop = _branching(tmp_path)
    loop.state.plan = (
        PlanStep("go.mod", "Cut the branch.", "it exists", phase="branch"),
    )
    assert loop._close_phase() == "branch"


def test_an_open_step_still_holds_a_phase_with_no_evidence(tmp_path: Path) -> None:
    loop = _migrating(tmp_path, phase="deps")
    loop.state.migration.branch = "migrate-to-n-api-template"
    loop.state.plan = (PlanStep("go.mod", "swap the deps", "go build", phase="deps"),)
    assert loop._close_phase() == ""


# ── an import swap is not a conversion ─────────────────────────────────────
#
# The size rule refuses one step on a file over BIG_FILE lines, and every word
# of its reasoning is about conversion. A dependency phase is not conversion:
# "replace the api-log import with n-api-log" in a 4,064-line repository file is
# one patch_file with a one-line anchor. A field session was refused on exactly
# that, answered correctly — "phase 2 is about dependencies, not handler
# conversion" — resubmitted the identical plan, and was refused again, spending
# both of MAX_PLAN_OBJECTIONS on an objection it could not satisfy.


def _sized(**sizes: int):
    return lambda path: sizes.get(path, 0)


def test_an_import_swap_on_a_big_file_is_allowed() -> None:
    steps = (
        PlanStep("repo/postgres/paogen.go", "Replace api-log imports with n-api-log",
                 "go build", phase="deps"),
    )
    said = plan_objection(
        MigrationState(active=True, phases=_phases()), _phases(), steps,
        lines=_sized(**{"repo/postgres/paogen.go": 4065}),
    )
    assert said == "", said


def test_a_conversion_step_on_a_big_file_is_still_refused() -> None:
    steps = (
        PlanStep("repo/postgres/paogen.go", "Convert every method to dblib.Psql",
                 "go build", phase="deps"),
    )
    said = plan_objection(
        MigrationState(active=True, phases=_phases()), _phases(), steps,
        lines=_sized(**{"repo/postgres/paogen.go": 4065}),
    )
    assert "4,065 lines" in said and "Split it" in said


def test_one_conversion_step_among_import_swaps_is_still_refused() -> None:
    """A file is exempt only if every step on it is a bounded edit."""
    steps = (
        PlanStep("repo/postgres/paogen.go", "Replace api-log imports with n-api-log",
                 "go build", phase="deps"),
        PlanStep("handler/paogen.go", "Rewrite the handlers for serverRoute.Context",
                 "go build", phase="deps"),
    )
    said = plan_objection(
        MigrationState(active=True, phases=_phases()), _phases(), steps,
        lines=_sized(**{"repo/postgres/paogen.go": 4065, "handler/paogen.go": 6572}),
    )
    assert "handler/paogen.go" in said
    assert "repo/postgres/paogen.go" not in said


def test_a_small_file_is_unaffected_either_way() -> None:
    steps = (PlanStep("main.go", "Rewrite the bootstrap", "go build", phase="deps"),)
    said = plan_objection(
        MigrationState(active=True, phases=_phases()), _phases(), steps,
        lines=_sized(**{"main.go": 53}),
    )
    assert said == ""


def test_the_next_message_plans_the_next_phase_not_the_closed_one(tmp_path: Path) -> None:
    """Why no separate fix was needed for "open in AGENT when the phase settles".

    That was on the fix list, and closing the phase subsumes it. Once the branch
    phase is `done` the plan belongs to a *finished* phase, so PLANNER is the
    correct mode for the follow-up — it has the next phase to plan — and the
    state block names that phase rather than the one the developer already
    watched complete. Routing to AGENT here would hand the acting phase a plan
    with nothing open in it.
    """
    loop = _branching(tmp_path)
    loop.state.plan = (
        PlanStep("go.mod", "Cut the branch.", "it exists", phase="branch", status="skipped"),
    )
    list(loop._verify())
    assert loop.state.migration.phase_named("branch").status == "done"

    # The follow-up. No open steps, so `_work_in_flight` is False and the run
    # opens in PLANNER — which is right, because the next phase needs a plan.
    assert not loop._work_in_flight()
    assert loop._opening_mode(Intent.AGENT, continued=True) is Mode.PLANNER
    # And the roadmap has moved, which is the whole of the fix: the model is no
    # longer told it is on a phase it has twice reported finishing.
    assert loop.state.migration.current[1].name == "deps"
    assert any("deps" in line for line in loop.state.migration.block(""))
