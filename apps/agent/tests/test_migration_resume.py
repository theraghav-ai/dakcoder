"""A migration that survives its sessions, and large files that finish.

Two field reports this file answers.

**"Every time I run migration, it starts from phase 1."** `/migrate` opens a new
session each time and the roadmap lived only in that session's `plan.json`, so
every run began with an empty roadmap, re-planned phase one, retook the route
inventory on a half-converted service, and its first save overwrote the plan
document. The migration now has a workspace record that any session resumes.

**"It fails more often in larger files."** Four mechanisms, each pinned here:
a phase planned one file at a time closed after the first file; every
`patch_file` advanced a split step whatever it converted; the objection budget
was spent before the handlers phase; `revise_plan` could merge a split back.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from dakcoder_agent.context import ContextManager
from dakcoder_agent.gate import ROUTES_BEFORE
from dakcoder_agent.loop import AgentLoop, _State
from dakcoder_agent.migration import (
    PROGRESS_PATH,
    RECORD_PATH,
    MigrationState,
    Phase,
    load_record,
    plan_objection,
    progress_document,
    save_record,
    size_objection,
)
from dakcoder_agent.modes import Mode
from dakcoder_agent.plan import PlanRecord
from dakcoder_agent.tools.control import PlanStep
from dakcoder_agent.tools.gotools import Reply, handlers_for
from dakcoder_agent.tools.registry import MAX_STEPS
from dakcoder_agent.tools.router import Router
from dakcoder_shared.envelope import ToolResult
from dakcoder_shared.llm import ToolCall
from dakcoder_shared.paths import Workspace
from scripted import gated, planning_router  # noqa: F401,E402  (fixtures)

ROADMAP = (
    Phase("branch", "cut the branch", "check, confirm, cut"),
    Phase("deps", "swap api-* for n-api-*", "go get, tidy"),
    Phase("handlers", "convert every handler", "imports, Base, Routes"),
    Phase("tests", "fix the tests", "unit, wiring"),
)


def _loop(root: Path) -> AgentLoop:
    loop = AgentLoop.__new__(AgentLoop)
    loop.router = Router(Workspace(root))
    loop.state = _State()
    loop.state.mode = Mode.AGENT
    loop.session_id = "s-now"
    loop.context = ContextManager(mode=Mode.AGENT, system_prompt="s")
    loop._plan_record = PlanRecord()
    return loop


def _resume(loop: AgentLoop) -> bool:
    """What `_run` does for a `/migration` task: switch migration on, then resume.

    `_resume_migration` read the message for "migrate", "continue" and the
    like; since session 8d075515671e only the command resumes one, and `_run`
    has set `active` from it before resume is asked.
    """
    loop.state.migration.active = True
    return loop._resume_migration()


def _half_done(root: Path, *, branch: str = "template-conversion") -> MigrationState:
    """What an earlier session left: branch and deps closed, handlers part-done."""
    state = MigrationState(active=True, branch=branch, base="development", routes=42)
    state.adopt(ROADMAP)
    state.close("branch")
    state.close("deps")
    state.record_steps(
        [
            PlanStep("handler/user.go", "convert GetUser", "unit_check", phase="handlers", status="done"),
            PlanStep("handler/paogen.go", "convert A, B", "unit_check", phase="handlers", part="group 1", status="done"),
            PlanStep("handler/paogen.go", "convert C, D", "unit_check", phase="handlers", part="group 2"),
        ],
        touched=["handler/user.go", "handler/paogen.go"],
    )
    state.note_planned("handlers", ["handler/pension.go"])
    save_record(root, state)
    return state


def _git(root: Path, branch: str) -> None:
    (root / ".git").mkdir(exist_ok=True)
    (root / ".git" / "HEAD").write_text(f"ref: refs/heads/{branch}\n", encoding="utf-8")


def _lines(n: int):
    return lambda path: n


# ── the record ──────────────────────────────────────────────────────────────


def test_the_record_round_trips_the_whole_ledger(tmp_path: Path) -> None:
    saved = _half_done(tmp_path)
    loaded = load_record(tmp_path)
    assert loaded is not None
    assert [p.status for p in loaded.phases] == ["done", "done", "pending", "pending"]
    assert loaded.branch == "template-conversion" and loaded.routes == 42
    assert loaded.units == saved.units
    assert "handler/pension.go" in loaded.backlog["handlers"]
    assert loaded.files == ("handler/user.go", "handler/paogen.go")


def test_the_session_plan_keeps_its_contract_shape(tmp_path: Path) -> None:
    """`GET /v1/sessions/{id}/plan` is contract C3; the ledger is not in it."""
    record = PlanRecord(session_id="s1", migration=_half_done(tmp_path))
    assert set(record.as_dict()["migration"]) == {"active", "branch", "base", "closed", "log", "phases"}


# ── a new session resumes ───────────────────────────────────────────────────


def test_a_new_migration_session_resumes_where_the_last_one_stopped(tmp_path: Path) -> None:
    _half_done(tmp_path)
    _git(tmp_path, "template-conversion")
    loop = _loop(tmp_path)

    assert _resume(loop)

    migration = loop.state.migration
    assert migration.active and migration.resumed
    assert migration.current is not None and migration.current[1].name == "handlers"
    assert migration.branch == "template-conversion"
    assert migration.defers_gate, "a resumed conversion must not run the gate mid-way"
    # The inventory is the legacy service's, taken once; it must not be retaken.
    assert loop.state.routes_saved and loop.state.routes_before == 42


def test_the_resumed_session_is_told_what_is_done_and_what_is_left(tmp_path: Path) -> None:
    _half_done(tmp_path)
    _git(tmp_path, "template-conversion")
    loop = _loop(tmp_path)
    _resume(loop)

    block = "\n".join(loop.state.migration.block(""))
    assert "RESUMING" in block and "2 of 4 phase(s) closed" in block
    assert "phase 3 of 4 — handlers" in block
    assert "handler/user.go" in block and "[group 1]" in block  # done
    assert "[group 2]" in block  # planned, not finished
    assert "handler/pension.go" in block  # not planned yet
    assert "no roadmap yet" not in block


def test_resubmitting_the_roadmap_does_not_reopen_what_an_earlier_session_closed(
    tmp_path: Path,
) -> None:
    _half_done(tmp_path)
    loop = _loop(tmp_path)
    _resume(loop)
    loop.state.migration.adopt(tuple(Phase(p.name, p.covers, p.parts) for p in ROADMAP))
    assert [p.status for p in loop.state.migration.phases][:2] == ["done", "done"]
    objection = plan_objection(
        loop.state.migration,
        (),
        [PlanStep("", "cut the branch", "", phase="branch")],
    )
    assert "already closed" in objection


def test_an_unrelated_task_does_not_pick_up_the_migration(tmp_path: Path) -> None:
    _half_done(tmp_path)
    loop = _loop(tmp_path)
    assert not loop._resume_migration()
    assert not loop.state.migration.active


def test_a_finished_migration_is_not_resumed(tmp_path: Path) -> None:
    state = MigrationState(active=True)
    state.adopt(ROADMAP)
    for phase in ROADMAP:
        state.close(phase.name)
    save_record(tmp_path, state)
    assert not _resume(_loop(tmp_path))


def test_a_resumed_migration_off_its_branch_holds_writes_until_it_switches_back(
    tmp_path: Path,
) -> None:
    _half_done(tmp_path)
    _git(tmp_path, "development")
    loop = _loop(tmp_path)
    _resume(loop)

    migration = loop.state.migration
    assert migration.branch == "" and migration.expected_branch == "template-conversion"
    call = ToolCall(id="c1", name="patch_file", arguments="{}")
    held = loop._migration_guard(call)
    assert held is not None and not held.ok
    assert "git_ops` op=branch message=template-conversion" in held.for_model()
    assert "checks out the existing branch" in held.for_model()


def test_the_routes_file_alone_is_enough_not_to_retake_the_inventory(tmp_path: Path) -> None:
    state = _half_done(tmp_path)
    state.routes = 0
    save_record(tmp_path, state)
    (tmp_path / ROUTES_BEFORE).parent.mkdir(parents=True, exist_ok=True)
    (tmp_path / ROUTES_BEFORE).write_text("{}", encoding="utf-8")
    loop = _loop(tmp_path)
    _resume(loop)
    assert loop.state.routes_saved


def test_a_restart_of_the_same_session_takes_the_ledger_from_the_record(tmp_path: Path) -> None:
    """The session file carries the roadmap without the ledger (contract shape);
    a restore must not lose which files the phase covers."""
    state = _half_done(tmp_path)
    PlanRecord(
        session_id="s-now",
        steps=(PlanStep("handler/paogen.go", "convert C, D", "unit_check", phase="handlers", part="group 2"),),
        migration=state,
    ).save(tmp_path)
    loop = _loop(tmp_path)
    assert loop.restore_plan("s-now")
    assert "handler/pension.go" in loop.state.migration.backlog["handlers"]


# ── the document and the record stay current ────────────────────────────────


def test_progress_is_written_to_both_the_document_and_the_record(tmp_path: Path) -> None:
    loop = _loop(tmp_path)
    loop.state.migration = MigrationState(active=True, branch="template-conversion")
    loop.state.migration.adopt(ROADMAP)
    loop.state.plan = (PlanStep("handler/a.go", "convert A", "unit_check", phase="branch", status="done"),)
    loop._save_progress()

    assert (tmp_path / PROGRESS_PATH).is_file()
    record = json.loads((tmp_path / RECORD_PATH).read_text(encoding="utf-8"))
    assert any(u["file"] == "handler/a.go" for u in record["units"].values())
    # Written at the workspace's .dakcoder, not a nested one.
    assert (tmp_path / ".dakcoder" / ".gitignore").is_file()
    assert not (tmp_path / ".dakcoder" / "migration" / ".dakcoder").exists()


def test_the_document_keeps_every_sessions_units(tmp_path: Path) -> None:
    state = _half_done(tmp_path)
    doc = progress_document(state, [PlanStep("handler/pension.go", "convert P", "unit_check", phase="handlers")])
    assert "handler/user.go" in doc and "group 1" in doc
    assert "files not finished" in doc and "handler/pension.go" in doc
    assert "## Files changed by the migration" in doc


# ── a phase is not the plan in hand ─────────────────────────────────────────


def _handlers_open(root: Path) -> AgentLoop:
    loop = _loop(root)
    loop.session_id = ""
    loop.state.migration = MigrationState(active=True, branch="template-conversion")
    loop.state.migration.adopt(ROADMAP)
    loop.state.migration.close("branch")
    loop.state.migration.close("deps")
    loop._converted = lambda path: None  # no sidecar: statuses decide
    return loop


def test_a_phase_planned_a_file_at_a_time_stays_open_until_every_file_is_done(
    tmp_path: Path,
) -> None:
    loop = _handlers_open(tmp_path)
    # The refused plan named three files; the narrower one covers the smallest.
    loop.state.migration.note_planned("handlers", ["handler/a.go", "handler/b.go", "handler/c.go"])
    loop.state.plan = (PlanStep("handler/a.go", "convert A", "unit_check", phase="handlers", status="done"),)

    assert loop._close_phase() == ""
    assert loop.state.phase_left == ("handler/b.go", "handler/c.go")
    assert loop.state.migration.current[1].name == "handlers"

    loop.state.plan = (
        PlanStep("handler/b.go", "convert B", "unit_check", phase="handlers", status="done"),
        PlanStep("handler/c.go", "no change needed", "", phase="handlers", status="skipped"),
    )
    assert loop._close_phase() == "handlers"
    assert loop.state.phase_left == ()


def test_a_split_file_closes_only_when_the_code_says_it_is_converted(tmp_path: Path) -> None:
    loop = _handlers_open(tmp_path)
    loop.state.migration.note_split("handlers", ["handler/paogen.go"])
    loop.state.plan = (
        PlanStep("handler/paogen.go", "convert A", "unit_check", phase="handlers", part="g1", status="done"),
    )
    loop._converted = lambda path: False
    assert loop._close_phase() == ""
    assert loop.state.phase_left == ("handler/paogen.go",)
    loop._converted = lambda path: True
    assert loop._close_phase() == "handlers"


def test_closing_a_phase_gives_the_next_one_its_own_objection_budget(tmp_path: Path) -> None:
    loop = _handlers_open(tmp_path)
    loop.state.plan_objections = 2
    loop.state.plan = (PlanStep("handler/a.go", "convert A", "unit_check", phase="handlers", status="done"),)
    assert loop._close_phase() == "handlers"
    assert loop.state.plan_objections == 0


def test_the_block_names_the_files_still_to_come_in_the_phase(tmp_path: Path) -> None:
    state = MigrationState(active=True, branch="b")
    state.adopt(ROADMAP)
    state.close("branch")
    state.close("deps")
    state.note_planned("handlers", ["handler/a.go", "handler/b.go"])
    state.record_steps([PlanStep("handler/a.go", "convert", "", phase="handlers")])
    block = "\n".join(state.block("handlers"))
    assert "Also in this phase, not in this plan (1): handler/b.go" in block


# ── the size rule reads the real groups ─────────────────────────────────────


def _groups(*spec: tuple[list[str], bool]):
    return lambda path: [
        {"start": i * 100 + 1, "end": i * 100 + 90, "methods": methods, "done": done}
        for i, (methods, done) in enumerate(spec)
    ]


def _steps(path: str, n: int) -> list[PlanStep]:
    return [PlanStep(path, f"convert group {i}", "unit_check", phase="handlers", part=f"g{i}") for i in range(n)]


def test_the_open_groups_are_the_measure_not_the_line_count() -> None:
    """5,000 lines is seven by arithmetic; three groups is three steps."""
    groups = _groups((["A", "B"], False), (["C"], False), (["D"], False))
    assert size_objection(_steps("handler/big.go", 3), _lines(5000), groups) == ""
    short = size_objection(_steps("handler/big.go", 2), _lines(5000), groups)
    assert "needs at least 3" in short
    assert "1. lines 1-90: A, B" in short, "the objection quotes the groups to plan"


def test_groups_an_earlier_session_converted_need_no_step() -> None:
    groups = _groups((["A"], True), (["B"], True), (["C"], False))
    assert size_objection(_steps("handler/big.go", 1), _lines(5000), groups) == ""
    done = _groups((["A"], True), (["B"], True))
    assert size_objection(_steps("handler/big.go", 1), _lines(5000), done) == ""


def test_a_file_with_more_groups_than_a_plan_holds_is_planned_a_plan_at_a_time() -> None:
    groups = _groups(*[([f"M{i}"], False) for i in range(MAX_STEPS + 5)])
    assert size_objection(_steps("handler/huge.go", MAX_STEPS), _lines(9000), groups) == ""


def test_without_a_map_the_arithmetic_still_holds() -> None:
    assert "needs at least 9" in size_objection(_steps("handler/paogen.go", 1), _lines(6571), None)


def test_revise_plan_cannot_merge_a_split_back_together(tmp_path: Path) -> None:
    loop = _handlers_open(tmp_path)
    loop.state.revisions = 0
    loop._line_count = lambda path: 6571
    loop._groups = lambda path: None
    loop.state.plan = tuple(_steps("handler/paogen.go", 9))
    before = loop.state.plan
    merged = [
        {"file": "handler/paogen.go", "action": "convert everything", "accepts": "unit_check", "phase": "handlers"}
    ]
    events = list(loop._revised(ToolResult.success("revised", meta={"steps": merged, "reason": "faster"})))
    assert events == []
    assert loop.state.plan == before
    assert "That revision was not adopted" in loop.context.build()[-1].content


# ── a write is not a step ───────────────────────────────────────────────────


def _split_plan(loop: AgentLoop) -> None:
    loop.state.plan = (
        PlanStep("handler/paogen.go", "convert GetA, GetB", "unit_check", phase="handlers", part="g1"),
        PlanStep("handler/paogen.go", "convert GetC", "unit_check", phase="handlers", part="g2"),
    )


def test_a_split_step_stays_put_until_its_methods_are_converted(tmp_path: Path) -> None:
    loop = _handlers_open(tmp_path)
    _split_plan(loop)
    loop._step_methods = lambda step: ["GetA", "GetB"] if step.part == "g1" else ["GetC"]
    verdict = {"left": ["GetB"]}
    loop._unit_state = lambda path, methods: (not verdict["left"], list(verdict["left"]))

    loop._mark_steps("handler/paogen.go", "written")  # patched GetA only
    first, second = loop.state.plan
    assert first.status == "pending" and "1 of 2 methods converted" in first.note
    assert "GetB" in first.note
    assert second.status == "pending" and second.note == "", "the write did not land on step 2"
    assert loop.active_step[0] == 1

    verdict["left"] = []
    loop._mark_steps("handler/paogen.go", "written")  # patched GetB
    assert loop.state.plan[0].status == "written"
    assert loop.active_step[0] == 2


def test_without_the_sidecar_a_split_step_keeps_the_old_rule(tmp_path: Path) -> None:
    loop = _handlers_open(tmp_path)
    _split_plan(loop)
    loop._step_methods = lambda step: ["GetA"]
    loop._unit_state = lambda path, methods: None
    loop._mark_steps("handler/paogen.go", "written")
    assert loop.state.plan[0].status == "written"


def test_a_finish_is_not_taken_as_covering_a_split_files_open_steps(tmp_path: Path) -> None:
    loop = _handlers_open(tmp_path)
    _split_plan(loop)
    loop.state.plan = (replace_status(loop.state.plan[0], "done"), loop.state.plan[1])
    loop.router.touched.append("handler/paogen.go")
    assert loop._unwritten_targets() == ["handler/paogen.go"]


def replace_status(step: PlanStep, status: str) -> PlanStep:
    from dataclasses import replace

    return replace(step, status=status)


def test_step_methods_come_from_the_map_not_from_prose(tmp_path: Path) -> None:
    loop = _handlers_open(tmp_path)
    loop._handler_map = lambda path: {"methods": [{"name": "GetA"}, {"name": "GetB"}, {"name": "Post"}]}
    step = PlanStep("handler/x.go", "convert GetA and GetB (the Getters), then Post later", "", part="g1")
    assert loop._step_methods(step) == ["GetA", "GetB", "Post"]
    assert loop._step_methods(PlanStep("handler/x.go", "convert the getters", "")) == []


# ── the bridge keeps the facts ──────────────────────────────────────────────


class _Sidecar:
    def __init__(self, text: str) -> None:
        self.text = text

    def call(self, tool, arguments):
        return Reply(self.text)


def test_the_call_map_facts_travel_as_data(tmp_path: Path) -> None:
    payload = {
        "file": "handler/x.go",
        "lines": 1200,
        "converted": 1,
        "methods": [{"name": "GetA", "shape": "converted", "start": 1, "end": 40, "routes": ["/a"]}],
        "groups": [{"start": 1, "end": 40, "methods": ["GetA"], "done": True, "repo": ["R.Get"]}],
        "report": "handler/x.go: ...",
    }
    handlers = handlers_for(_Sidecar(json.dumps(payload)))
    from dakcoder_agent.tools.router import Invocation
    from dakcoder_agent.tools import registry

    result = handlers["handler_map"](
        Invocation(spec=registry.REGISTRY["handler_map"], arguments={"path": "handler/x.go"}, workspace=Workspace(tmp_path))
    )
    facts = result.meta["callmap"]
    assert facts["groups"] == [{"start": 1, "end": 40, "methods": ["GetA"], "done": True}]
    assert facts["methods"][0] == {"name": "GetA", "shape": "converted", "start": 1, "end": 40}
    assert result.content.startswith("handler/x.go")


# ── end to end: two sessions, one migration ─────────────────────────────────


def test_a_second_session_plans_the_next_phase_not_phase_one(gated, planning_router) -> None:
    """The report itself: run /migrate twice and the second run starts at phase 1.

    Two separate sessions -- separate loops, separate session ids, nothing
    carried in memory -- over one workspace. The first closes the branch phase;
    the second is handed the roadmap again by the model and must adopt the next
    phase's steps, with the branch phase still closed and the record saying so.
    """
    from scripted import build, calls, patch

    roadmap = [{"name": p.name, "covers": p.covers, "parts": p.parts} for p in ROADMAP]
    first_plan = json.dumps(
        {
            "summary": "convert the service",
            "phases": roadmap,
            "steps": [
                {"file": "handler/user.go", "action": "convert it", "accepts": "unit_check",
                 "phase": "branch", "part": "cut"}
            ],
        }
    )
    one, _ = build(
        planning_router,
        [calls(("submit_plan", first_plan)), patch(), calls(("finish", json.dumps({"answer": "phase done"})))],
        migration=True,
    )
    one.session_id = "session-one"
    one.state.migration.branch = "template-conversion"
    list(one.run("/migration migrate this service to the n-api template"))
    root = planning_router.workspace.root
    record = load_record(root)
    assert record is not None and record.phase_named("branch").status == "done"
    assert "1 of 4 closed" in (root / PROGRESS_PATH).read_text(encoding="utf-8")

    # A brand-new session. The model re-sends the roadmap and asks for the
    # branch phase again, as it would with nothing to tell it otherwise.
    second_plan = json.dumps(
        {
            "summary": "convert the service",
            "phases": roadmap,
            "steps": [
                {"file": "handler/user.go", "action": "convert it", "accepts": "unit_check",
                 "phase": "branch", "part": "cut"},
                {"file": "go.mod", "action": "swap the api-* imports", "accepts": "unit_check",
                 "phase": "deps", "part": "go get"},
            ],
        }
    )
    two, _ = build(
        planning_router,
        [calls(("submit_plan", second_plan)), calls(("finish", json.dumps({"answer": "stopping"})))],
        migration=True,
        max_turns=3,
    )
    two.session_id = "session-two"
    list(two.run("/migration migrate this service to the n-api template"))

    migration = two.state.migration
    assert migration.phase_named("branch").status == "done", "the closed phase was reopened"
    assert migration.branch == "template-conversion"
    assert two.state.plan and {s.phase for s in two.state.plan} == {"deps"}
    doc = (root / PROGRESS_PATH).read_text(encoding="utf-8")
    assert "1 of 4 closed" in doc, "the second session overwrote the record with phase one"


# ── the branch phase is not redone ──────────────────────────────────────────


def test_a_branch_phase_closes_the_moment_the_branch_exists() -> None:
    state = MigrationState(active=True)
    state.adopt(ROADMAP)
    assert state.close_evidenced() == []
    state.branch = "migrate-to-n-api"
    assert state.close_evidenced() == ["branch"]
    assert state.current[1].name == "deps"


def test_a_combined_branch_and_deps_phase_is_not_closed_by_the_branch_alone() -> None:
    state = MigrationState(active=True, branch="migrate-to-n-api")
    state.adopt((Phase("branch-and-deps", "cut the branch, swap api-* deps", "cut, go get"), *ROADMAP[2:]))
    assert state.close_evidenced() == []


def test_a_shared_branch_is_not_evidence() -> None:
    state = MigrationState(active=True, branch="main")
    state.adopt(ROADMAP)
    assert state.close_evidenced() == []


def test_a_record_left_by_a_run_stopped_after_the_cut_resumes_past_the_branch(tmp_path: Path) -> None:
    """The field report: the branch was cut and recorded, the run was stopped
    before `finish`, and every later /migrate asked which branch to cut from."""
    state = MigrationState(active=True, branch="migrate-to-n-api", base="main")
    state.adopt(ROADMAP)
    save_record(tmp_path, state)
    _git(tmp_path, "migrate-to-n-api")
    loop = _loop(tmp_path)
    assert _resume(loop)
    assert loop.state.migration.phase_named("branch").status == "done"
    assert loop.state.migration.current[1].name == "deps"
    assert load_record(tmp_path).phase_named("branch").status == "done", "written back at once"
    block = "\n".join(loop.state.migration.block(""))
    assert "Open: phase 2 of 4 — deps" in block


def test_off_its_branch_the_phase_stays_closed_and_the_run_switches_back(tmp_path: Path) -> None:
    state = MigrationState(active=True, branch="migrate-to-n-api", base="main")
    state.adopt(ROADMAP)
    save_record(tmp_path, state)
    _git(tmp_path, "main")
    loop = _loop(tmp_path)
    _resume(loop)
    migration = loop.state.migration
    assert migration.phase_named("branch").status == "done", "the branch exists; it is not re-cut"
    assert migration.expected_branch == "migrate-to-n-api"


def test_the_git_ops_result_closes_the_branch_phase_mid_run(planning_router, gated) -> None:
    from scripted import build, calls

    (planning_router.workspace.root / ".git").mkdir(exist_ok=True)
    planning_router.handlers["git_ops"] = lambda inv: ToolResult.success(
        "Switched to a new branch 'migrate-to-n-api'",
        meta={"branch": "migrate-to-n-api", "base": "main"},
    )
    loop, _ = build(
        planning_router,
        [calls(("git_ops", json.dumps({"op": "branch", "message": "migrate-to-n-api", "base": "main"})))],
        max_turns=1,
    )
    loop.state.migration = MigrationState(active=True)
    loop.state.migration.adopt(ROADMAP)
    loop.state.mode = Mode.AGENT
    list(loop._turn())
    assert loop.state.migration.phase_named("branch").status == "done"
    assert any("phase is complete" in (m.content or "") for m in loop.context.build())


def test_a_migration_from_before_the_record_is_imported_from_its_session(tmp_path: Path) -> None:
    """Workspaces whose migration began before the record existed have their
    progress only in a session's plan.json."""
    old = MigrationState(active=True, branch="migrate-to-n-api-template", base="development")
    old.adopt(ROADMAP)
    old.close("branch")
    PlanRecord(
        session_id="e3edb2434936",
        steps=(PlanStep("go.mod", "swap the api-* modules", "unit_check", phase="deps", status="done"),),
        migration=old,
    ).save(tmp_path)
    loaded = load_record(tmp_path)
    assert loaded is not None
    assert loaded.branch == "migrate-to-n-api-template"
    assert loaded.phase_named("branch").status == "done"
    assert any(u["file"] == "go.mod" and u["status"] == "done" for u in loaded.units.values())


# ── the record holds the whole migration, not only what plans named ─────────


def test_steps_sharing_a_part_label_do_not_overwrite_each_other() -> None:
    """The field record: every paogen step had `part: "paogen"`, and nine
    groups collapsed into one row holding group 9."""
    state = MigrationState(active=True)
    state.adopt(ROADMAP)
    state.record_steps(
        [
            PlanStep("handler/paogen.go", "Convert group 1 methods GetA, GetB", "", phase="handlers", part="paogen", status="done"),
            PlanStep("handler/paogen.go", "Convert group 2 methods GetC", "", phase="handlers", part="paogen"),
        ]
    )
    rows = [u for u in state.units.values() if u["file"] == "handler/paogen.go"]
    assert len(rows) == 2
    assert sorted(u["status"] for u in rows) == ["done", "pending"]


def test_split_steps_are_keyed_by_the_methods_they_convert() -> None:
    state = MigrationState(active=True)
    state.adopt(ROADMAP)
    methods = lambda step: ["GetA", "GetB"] if "GetA" in step.action else ["GetC"]  # noqa: E731
    state.record_steps(
        [PlanStep("handler/paogen.go", "convert GetA, GetB", "", phase="handlers", part="g1", status="done")],
        methods=methods,
    )
    # A later session re-plans the same group in other words; same row.
    state.record_steps(
        [PlanStep("handler/paogen.go", "port GetB and GetA to the template", "", phase="handlers", part="first")],
        methods=methods,
    )
    rows = [u for u in state.units.values() if u["file"] == "handler/paogen.go"]
    assert len(rows) == 1 and rows[0]["status"] == "done" and rows[0]["methods"] == "GetA,GetB"


def test_the_agents_own_files_are_never_phase_work() -> None:
    state = MigrationState(active=True)
    state.adopt(ROADMAP)
    state.record_steps(
        [
            PlanStep("AGENTS.md", "cut the branch", "", phase="branch"),
            PlanStep(".dakcoder/routes-before.json", "record the routes", "", phase="branch"),
            PlanStep("go.mod", "swap modules", "", phase="deps"),
        ],
        touched=[".dakcoder/migration/plan.md", "go.mod"],
    )
    assert state.backlog.get("branch", []) == []
    assert state.backlog["deps"] == ["go.mod"]
    assert state.files == ("go.mod",)
    assert state.outstanding("branch") == []


def test_an_old_record_is_cleaned_on_load(tmp_path: Path) -> None:
    state = MigrationState(active=True, branch="b")
    state.adopt(ROADMAP)
    state.backlog = {"branch": ["AGENTS.md", ".dakcoder/routes-before.json"], "deps": ["AGENTS.md", "go.mod"]}
    state.files = (".dakcoder/migration/plan.md", "go.mod")
    save_record(tmp_path, state)
    loaded = load_record(tmp_path)
    assert loaded.backlog == {"branch": [], "deps": ["go.mod"]}
    assert loaded.files == ("go.mod",)


def _audit(**counts: int) -> dict:
    return {path.replace("__", "/").replace("_go", ".go"): {"count": n, "rules": ["legacy-gin-handler"]} for path, n in counts.items()}


def test_the_scope_is_every_legacy_file_by_layer(tmp_path: Path) -> None:
    state = MigrationState(active=True)
    state.adopt(ROADMAP)
    by_file = {
        "handler/paogen.go": {"count": 33, "rules": ["legacy-gin-handler"]},
        "repo/postgres/paogen.go": {"count": 3, "rules": ["legacy-db"]},
        "bootstrap/bootstrapper.go": {"count": 1, "rules": ["legacy-fx"]},
        "handler/validator.go": {"count": 2, "rules": []},
        "main.go": {"count": 2, "rules": []},
    }
    state.update_scope(by_file, lambda p: True)
    kinds = {p: e["kind"] for p, e in state.scope.items()}
    assert kinds == {
        "handler/paogen.go": "handler",
        "repo/postgres/paogen.go": "repository",
        "bootstrap/bootstrapper.go": "bootstrap",
        "handler/validator.go": "dto",
        "main.go": "entry",
    }
    summary = "\n".join(state.scope_summary())
    assert "repository: 0 of 1 converted" in summary and "repo/postgres/paogen.go (3)" in summary


def test_a_file_the_audit_stops_reporting_is_converted_or_removed_never_forgotten() -> None:
    state = MigrationState(active=True)
    state.adopt(ROADMAP)
    state.update_scope(
        {"handler/a.go": {"count": 4}, "go.work": {"count": 1}, "repo/a.go": {"count": 2}}, lambda p: True
    )
    state.update_scope({"repo/a.go": {"count": 1}}, lambda p: p != "go.work")
    assert state.scope["handler/a.go"]["status"] == "converted"
    assert state.scope["go.work"]["status"] == "removed"
    assert state.scope["repo/a.go"]["status"] == "legacy" and state.scope["repo/a.go"]["findings"] == "1"
    assert state.scope["repo/a.go"]["first"] == "2"


def test_the_scope_is_in_the_record_and_the_document_but_not_as_a_units_table(tmp_path: Path) -> None:
    state = MigrationState(active=True, branch="b")
    state.adopt(ROADMAP)
    state.update_scope({"repo/postgres/paogen.go": {"count": 3, "rules": ["legacy-db"]}}, lambda p: True)
    state.record_steps([PlanStep("handler/paogen.go", "convert", "", phase="handlers")])
    save_record(tmp_path, state)
    assert load_record(tmp_path).scope["repo/postgres/paogen.go"]["kind"] == "repository"
    doc = progress_document(state, [])
    assert "## Scope" in doc and "`repo/postgres/paogen.go` — repository, 3 finding(s)" in doc
    # The Migration view reads the first table with a path column as the units.
    first_table = doc.index("| Unit |")
    assert first_table < doc.index("## Scope")
    assert sum(1 for line in doc.splitlines() if line.startswith("|---")) == 1


def test_the_turn_block_carries_the_scope_as_counts() -> None:
    state = MigrationState(active=True, branch="b")
    state.adopt(ROADMAP)
    state.close("branch")
    state.update_scope({"handler/a.go": {"count": 2}, "repo/a.go": {"count": 1}}, lambda p: True)
    block = "\n".join(state.block("deps"))
    assert "Scope converted: handler 0/1, repository 0/1" in block


def test_the_route_count_is_read_from_the_saved_inventory(tmp_path: Path) -> None:
    loop = _loop(tmp_path)
    loop.state.migration = MigrationState(active=True, branch="b")
    loop.state.migration.adopt(ROADMAP)
    target = tmp_path / ROUTES_BEFORE
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps({"routes": [{"path": "/a"}, {"path": "/b"}, {"path": "/c"}]}), encoding="utf-8")
    loop._save_progress()
    assert load_record(tmp_path).routes == 3


def test_the_loop_takes_the_scope_from_the_audit(tmp_path: Path) -> None:
    loop = _loop(tmp_path)
    loop.state.migration = MigrationState(active=True)
    loop.state.migration.adopt(ROADMAP)
    loop.router.handlers["legacy_audit"] = lambda inv: ToolResult.success(
        "26 findings", meta={"by_file": {"repo/postgres/x.go": {"count": 2, "rules": ["legacy-db"]}}}
    )
    loop._refresh_scope()
    assert loop.state.migration.scope["repo/postgres/x.go"]["kind"] == "repository"


def test_the_audit_bridge_reports_findings_per_file(tmp_path: Path) -> None:
    from dakcoder_agent.tools import registry
    from dakcoder_agent.tools.router import Invocation

    payload = {
        "count": 3,
        "violations": [
            {"rule": "legacy-gin-handler", "path": "handler/a.go", "line": 3, "message": "m"},
            {"rule": "legacy-db", "path": "handler/a.go", "line": 9, "message": "n"},
            {"rule": "legacy-db", "path": "repo/a.go", "line": 1, "message": "o"},
        ],
    }
    handler = handlers_for(_Sidecar(json.dumps(payload)))["legacy_audit"]
    result = handler(Invocation(spec=registry.REGISTRY["legacy_audit"], arguments={}, workspace=Workspace(tmp_path)))
    assert result.meta["by_file"] == {
        "handler/a.go": {"count": 2, "rules": ["legacy-gin-handler", "legacy-db"]},
        "repo/a.go": {"count": 1, "rules": ["legacy-db"]},
    }
