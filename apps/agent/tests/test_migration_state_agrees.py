"""Sessions 9ba77962405b and 3baf69127eaf: a migration the loop contradicted.

Every message of 9ba77962405b ended the same way. The state block said "Now:
step 1 -- search for each method, read just that method, patch it" to a phase
that held no `patch_file`; the read ledger refused the narrow reads the step
asked for; the plan check demanded a three-phase roadmap of a session told not
to re-plan; and a forced `submit_plan` on a *recorded* migration was treated as
a question nobody asked (BUG L-28), so the acting phase was offered `finish`
alone with twelve steps pending and every follow-up opened read-only.

Separately, the developer could neither start the migration over nor forget it:
"Migrate start from the scratch" resumed the old roadmap, and deleting
`.dakcoder/migration/` was undone from the sessions' plan files (3baf69127eaf).

Each test below is one of those contradictions, stated as the agreement that
now holds.
"""

from __future__ import annotations

import json
from pathlib import Path

from dakcoder_agent.context import ContextManager
from dakcoder_agent.loop import AgentLoop, Intent, _asks_restart
from dakcoder_agent.migration import (
    ARCHIVE_DIR,
    MARKER_PATH,
    PROGRESS_PATH,
    RECORD_PATH,
    MigrationState,
    Phase,
    archive_record,
    load_record,
    merged_roadmap,
    plan_objection,
    record_removed,
    save_record,
)
from dakcoder_agent.modes import Mode
from dakcoder_agent.tools.control import PlanStep
from dakcoder_agent.tools.router import Router, is_history
from dakcoder_shared.envelope import ToolResult
from dakcoder_shared.llm import ToolCall

from scripted import build, calls, say

# Fixtures defined in `scripted` are re-exported here so pytest collects them.
from scripted import gated, planning_router, written  # noqa: F401,E402

SEVEN = (
    Phase("branch", "cut it", "check, cut", status="done"),
    Phase("dependencies", "swap api-*", "get, tidy", status="done"),
    Phase("handlers", "convert handlers", "paogen, transferentry"),
    Phase("dtos-validation", "request DTOs", "dto, validate"),
    Phase("bootstrap-fx", "wire FX", "repo, handler"),
    Phase("tests", "unit tests", "handler, repo"),
    Phase("swagger", "API document", "names, docs"),
)


def _recorded(root: Path, phases=SEVEN) -> MigrationState:
    state = MigrationState(active=True, phases=tuple(phases), branch="migrate-to-n-api")
    save_record(root, state)
    return state


# ── the roadmap on record is never shrunk by a re-submission ────────────────


def test_a_resumed_session_resending_one_phase_leaves_the_roadmap_whole() -> None:
    """Seven phases became one in 9ba77962405b: it sent `phases=[handlers]`."""
    state = MigrationState(active=True, phases=SEVEN)
    state.adopt((Phase("handlers", "convert handlers", "paogen"),))
    assert [p.name for p in state.phases] == [p.name for p in SEVEN]
    assert [p.status for p in state.phases][:2] == ["done", "done"]


def test_a_new_roadmap_keeps_the_phases_already_closed() -> None:
    """Seven became five in 11e91c951efb: the closed branch and dependencies went."""
    fresh = (
        Phase("handlers", "", "a, b"),
        Phase("dtos", "", "a, b"),
        Phase("wiring", "", "a, b"),
    )
    merged = merged_roadmap(SEVEN, fresh)
    assert [p.name for p in merged] == ["branch", "dependencies", "handlers", "dtos", "wiring"]
    assert merged[0].status == merged[1].status == "done"


def test_a_recorded_roadmap_is_not_objected_to_by_its_shape() -> None:
    """The resume view says the roadmap stays; the check agreed only for a first one."""
    one = MigrationState(active=True, phases=(Phase("handlers", "convert", "paogen"),))
    step = PlanStep(
        "handler/paogen.go", "convert group 1", "unit_check path=handler/paogen.go methods=A",
        phase="handlers",
    )
    assert plan_objection(one, (), [step]) == ""
    assert plan_objection(one, (Phase("handlers"),), [step]) == ""

    first = MigrationState(active=True)
    assert "at least" in plan_objection(first, (Phase("handlers", "x", "a, b"),), [step])


# ── deleting the record means it ────────────────────────────────────────────


def _session_plan(root: Path, sid: str, phases=SEVEN) -> None:
    folder = root / ".dakcoder" / "sessions" / sid
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "plan.json").write_text(
        json.dumps(
            {
                "migration": MigrationState(active=True, phases=tuple(phases)).as_dict(ledger=False),
                "steps": [],
            }
        ),
        encoding="utf-8",
    )


def test_a_deleted_record_is_not_rebuilt_from_the_sessions(tmp_path: Path) -> None:
    _recorded(tmp_path)
    _session_plan(tmp_path, "9ba77962405b")
    assert (tmp_path / MARKER_PATH).is_file(), "saving the record marks the workspace"

    (tmp_path / RECORD_PATH).unlink()
    assert record_removed(tmp_path)
    assert load_record(tmp_path) is None, "3baf69127eaf restored the migration from here"


def test_a_workspace_that_never_kept_a_record_still_imports_its_old_sessions(tmp_path: Path) -> None:
    _session_plan(tmp_path, "84f5389e4f23")
    record = load_record(tmp_path)
    assert record is not None and len(record.phases) == len(SEVEN)
    assert (tmp_path / MARKER_PATH).is_file() is False, "importing is not keeping"


def test_starting_over_archives_the_record_and_nothing_brings_it_back(tmp_path: Path) -> None:
    _recorded(tmp_path)
    (tmp_path / PROGRESS_PATH).write_text("# Migration plan\n", encoding="utf-8")
    _session_plan(tmp_path, "9ba77962405b")

    where = archive_record(tmp_path)
    assert where.startswith(ARCHIVE_DIR)
    assert (tmp_path / where / "state.json").is_file(), "set aside, not deleted"
    assert not (tmp_path / RECORD_PATH).exists()
    assert load_record(tmp_path) is None


def test_only_a_message_about_the_migration_can_start_it_over() -> None:
    assert _asks_restart("Migrate start from the scratch. everyhting is new here")
    assert _asks_restart("Start the migration over from scratch: the recorded one is set aside.")
    assert _asks_restart("restart the migration")
    assert not _asks_restart("rewrite handler/paogen.go from scratch")
    assert not _asks_restart("continue the migration")


def test_a_start_over_message_begins_a_migration_with_no_roadmap(planning_router: Router, written) -> None:
    root = planning_router.workspace.root
    _recorded(root)
    loop, _ = build(planning_router, [say("Planning from the start.")], max_turns=2)
    list(loop.run("Migrate start from the scratch. everything is new here", intent=Intent.AGENT))

    assert loop.state.migration.active
    assert not loop.state.migration.phases, "the old roadmap was resumed"
    assert any((root / ARCHIVE_DIR).iterdir())
    said = "\n".join(m.content for m in loop.context.build())
    assert "start the migration over" in said and "history, not state" in said


def test_a_follow_up_after_the_record_was_deleted_forgets_the_migration(
    planning_router: Router, written
) -> None:
    root = planning_router.workspace.root
    first, _ = build(planning_router, [say("ok")], max_turns=1)
    first.state.migration = _recorded(root)
    first.state.plan = (PlanStep("handler/user.go", "convert", "unit_check", phase="handlers"),)

    (root / RECORD_PATH).unlink()

    second, _ = build(planning_router, [say("Here is the answer.")], max_turns=2)
    second.carry_from(first)
    list(second.run("what does this service do?", intent=Intent.ASK, continued=True))

    assert not second.state.migration.active and not second.state.migration.phases
    assert not second.state.plan, "the forgotten migration's plan was carried"
    assert not (root / RECORD_PATH).exists(), "the runtime wrote the record back"


# ── a forced plan for a recorded migration is still the migration ───────────


def _acting(planning_router: Router) -> AgentLoop:
    loop, _ = build(planning_router, [])
    loop.state.mode = Mode.AGENT
    return loop


def test_a_forced_plan_on_a_recorded_migration_is_a_commitment(planning_router: Router) -> None:
    loop = _acting(planning_router)
    loop.state.migration = MigrationState(active=True, phases=SEVEN)
    loop.state.plan = (PlanStep("handler/paogen.go", "convert group 1", "unit_check", phase="handlers"),)
    loop.state.plan_forced = True  # carried from a session before the fix

    assert loop._plan_is_commitment()
    assert loop._open_targets() == ["handler/paogen.go"], "turn 15 offered only finish"
    assert loop._work_in_flight(), "every follow-up opened in PLANNER"


def test_a_forced_plan_with_no_migration_on_record_is_still_guarded(planning_router: Router) -> None:
    """L-28 stays fixed: a question made to plan is not work."""
    loop = _acting(planning_router)
    loop.state.plan = (PlanStep("handler/user.go", "x", "y"),)
    loop.state.plan_forced = True
    assert not loop._plan_is_commitment()
    assert loop._open_targets() == []


# ── each phase is told what it can do ───────────────────────────────────────


def _holding_a_plan(planning_router: Router, mode: Mode) -> AgentLoop:
    loop, _ = build(planning_router, [])
    loop.state.mode = mode
    loop.state.migration = MigrationState(active=True, phases=SEVEN)
    loop.state.plan = (
        PlanStep("handler/paogen.go", "convert group 1: A, B", "unit_check", phase="handlers"),
        PlanStep("handler/paogen.go", "convert group 2: C", "unit_check", phase="handlers"),
    )
    return loop


def test_a_phase_that_cannot_write_is_not_told_to_patch(planning_router: Router) -> None:
    for mode, move in ((Mode.PLANNER, "`submit_plan`"), (Mode.ASK, "answer the developer")):
        block = _holding_a_plan(planning_router, mode)._state_block()
        assert "Plan in hand: step 1 of 2" in block, mode
        assert move in block, mode
        assert "patch_file" not in block and "revise_plan" not in block, mode
        assert "Now: step" not in block, mode


def test_the_acting_phase_is_told_which_methods_are_already_in_context(planning_router: Router) -> None:
    loop = _holding_a_plan(planning_router, Mode.AGENT)
    callmap = {
        "methods": [
            {"name": "A", "start": 10, "end": 40, "shape": "gin", "repo": ["PaogenRepository.GetA"]},
            {"name": "B", "start": 50, "end": 90, "shape": "gin"},
            {"name": "C", "start": 900, "end": 950, "shape": "gin"},
        ]
    }
    loop.router.register(
        "handler_map", lambda inv: ToolResult.success("map", meta={"callmap": callmap})
    )
    body = "\n".join(f"line {n}" for n in range(1, 61))
    loop.context.append_tool_result(
        "read_file", body, tool_call_id="r1", path="handler/paogen.go", line_range=(1, 60)
    )

    block = loop._state_block()
    assert "In context already (1): A 10-40" in block
    assert "Not read yet (1): B 50-90" in block
    assert "PaogenRepository.GetA" in block
    assert "Find each method with `search_repo`" not in block, "the search-then-read order"


def test_the_resume_view_asks_the_planner_for_steps_and_nothing_else(tmp_path: Path) -> None:
    state = MigrationState(active=True, phases=SEVEN, resumed=True)
    planner = "\n".join(state.block(mode="planner"))
    assert "Leave `phases` out" in planner
    assert "same `phases`" not in planner and "Do not re-plan" not in planner
    ask = "\n".join(state.block(mode="ask"))
    assert "submit_plan" not in ask and "answer what they asked" in ask


# ── a refused re-read says where the lines are and what comes next ──────────


def _read(path: str, start: int, end: int) -> ToolCall:
    return ToolCall(id="x", name="read_file", arguments=json.dumps({"path": path, "start": start, "end": end}))


def _having_read(loop: AgentLoop, path: str, span: tuple[int, int]) -> None:
    body = "\n".join(f"line {n}" for n in range(span[0], span[1] + 1))
    loop.context.append_tool_result("read_file", body, tool_call_id="r0", path=path, line_range=span)
    loop._record_read(path, span, 4064)


def test_a_narrow_read_of_a_file_the_conversion_changes_is_served(planning_router: Router) -> None:
    """GetOfficenameRepo's 24 lines, refused because turn 3 read 800 of them."""
    loop = _acting(planning_router)
    loop.state.migration = MigrationState(
        active=True, phases=SEVEN, scope={"repo/postgres/paogen.go": {"kind": "repository"}}
    )
    _having_read(loop, "repo/postgres/paogen.go", (1, 800))
    assert loop._re_reading(_read("repo/postgres/paogen.go", 42, 65)) == ""
    assert loop._re_reading(_read("repo/postgres/paogen.go", 1, 700)), "a wide re-read is still refused"


def test_a_refused_re_read_names_the_phases_next_move(planning_router: Router) -> None:
    loop, _ = build(planning_router, [])
    loop.state.mode = Mode.PLANNER
    _having_read(loop, "repo/postgres/paogen.go", (1, 800))
    refusal = loop._re_reading(_read("repo/postgres/paogen.go", 42, 65))
    assert "in the read_file result for repo/postgres/paogen.go" in refusal
    assert "`submit_plan`" in refusal and "widen it" not in refusal


# ── the developer's latest message is the last thing the model reads ────────


def test_the_latest_message_comes_after_the_state_block() -> None:
    context = ContextManager(mode=Mode.PLANNER, system_prompt="s")
    context.set_task("Migrate the service", acceptance=())
    context.pin_directive("continue working")
    context.pin_directive("what is in the context?")
    context.set_state("# Current state — turn 9\nNow: step 1 of 12")
    last = context.build()[-1].content
    assert last.index("# Current state") < last.index("what is in the context?")
    assert last.rstrip().endswith("the state above is where the work stands.")
    assert "- continue working" in last


# ── the runtime's own history is not the workspace ──────────────────────────


def test_undo_snapshots_are_never_offered_or_read(planning_router: Router) -> None:
    root = planning_router.workspace.root
    old = root / ".dakcoder" / "sessions" / "c83e18571ac3" / "undo" / "files" / ".dakcoder" / "migration"
    old.mkdir(parents=True)
    (old / "plan.md").write_text("# Migration plan (two sessions old)\n", encoding="utf-8")

    missing = planning_router.dispatch("read_file", {"path": ".dakcoder/migration/plan.md"}, mode="planner")
    assert not missing.ok and "sessions" not in missing.content, missing.content

    stale = planning_router.dispatch(
        "read_file",
        {"path": ".dakcoder/sessions/c83e18571ac3/undo/files/.dakcoder/migration/plan.md"},
        mode="planner",
    )
    assert not stale.ok and "runtime's own history" in stale.content
    assert is_history(".dakcoder/migration-archive/20260927/state.json")
    assert not is_history(".dakcoder/migration/plan.md")


# ── the Planner holding a plan is sent to hand it on, not to report ─────────


def test_a_stalled_planner_with_a_plan_in_hand_is_told_to_submit_it(
    planning_router: Router, gated, written
) -> None:
    root = planning_router.workspace.root
    _recorded(root)
    search = calls(("search_repo", json.dumps({"pattern": "package handler"})))
    loop, _ = build(planning_router, [search, search, search, say("done")], max_turns=5)
    loop.state.plan = (PlanStep("handler/user.go", "convert", "unit_check", phase="handlers"),)
    list(loop.run("carry on with the handlers", intent=Intent.AGENT))

    said = "\n".join(m.content for m in loop.context.build())
    assert "End this phase with `submit_plan`" in said
    assert "Give the developer what you have established now" not in said
