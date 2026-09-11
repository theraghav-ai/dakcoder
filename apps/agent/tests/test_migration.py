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
from dakcoder_agent.loop import MAX_PLAN_OBJECTIONS, AgentLoop, _State
from dakcoder_agent.migration import (
    BIG_FILE,
    MIN_PHASES,
    PROGRESS_PATH,
    MigrationState,
    Phase,
    phases_from_meta,
    plan_objection,
    progress_document,
)
from dakcoder_agent.modes import Intent, Mode
from dakcoder_agent.plan import PlanRecord
from dakcoder_agent.tools import commands, fs
from dakcoder_agent.tools.control import PlanStep, submit_plan
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


def test_a_plan_spanning_two_phases_at_once_is_sent_back() -> None:
    """Requirement four, enforced where the work is committed to rather than asked for."""
    state = MigrationState(active=True)
    state.adopt(_phases())
    steps = [
        PlanStep("go.mod", "swap", "tidy", phase="deps"),
        PlanStep("handler/user.go", "convert", "build", phase="handlers"),
    ]
    objection = plan_objection(state, (), steps)
    assert "spans 2 phases" in objection


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
    assert "is not work for this run" in result.content
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
    assert "Routes recorded before the migration: 93" in with_routes

    without = progress_document(loop.state.migration, loop.state.plan, (), 0)
    assert "will not be caught automatically" in without


# ── progress survives the context window ────────────────────────────────────


def test_the_progress_record_is_written_where_the_next_session_can_read_it(
    tmp_path: Path,
) -> None:
    """A conversion outlives a context window; the transcript does not."""
    loop = _migrating(tmp_path)
    loop.state.plan = (
        PlanStep("go.mod", "swap the deps", "tidy", phase="deps", status="done"),
        PlanStep(
            "handler/paogen.go",
            "convert methods 1-10",
            "go build",
            phase="handlers",
            part="methods 1-10",
        ),
    )
    loop._save_progress()

    written_doc = (tmp_path / PROGRESS_PATH).read_text(encoding="utf-8")
    assert "Branch: `template-conversion`, cut from `development`" in written_doc
    assert "[x] **1. branch**" in written_doc, "a closed phase is ticked"
    assert "[ ] **3. handlers**" in written_doc
    assert "1 of 3 closed" in written_doc
    assert "[x] 1. `go.mod`" in written_doc
    assert "*(methods 1-10)*" in written_doc
    assert "## Still to do" in written_doc


def test_nothing_is_written_for_an_ordinary_task(tmp_path: Path) -> None:
    loop = _loop(tmp_path)
    loop.state.plan = (PlanStep("a.go", "x", "y"),)
    loop._save_progress()
    assert not (tmp_path / PROGRESS_PATH).exists()


def test_the_progress_record_is_rendered_from_the_plan_not_from_prose(tmp_path: Path) -> None:
    """The field session wrote its own `migration.md` by hand and then read it
    back as evidence of work it had not done. This one cannot say that."""
    loop = _migrating(tmp_path)
    loop.state.plan = (PlanStep("go.mod", "swap", "tidy", phase="deps"),)
    doc = progress_document(loop.state.migration, loop.state.plan, ("go.mod",))
    assert "[ ] 1. `go.mod`" in doc, "a step nothing has written is not ticked"
    assert "Edits here are overwritten" in doc


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
