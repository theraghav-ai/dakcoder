"""The phase state machine: active phase, verification, mark complete, next.

Every test here comes from one field session (`error.md`): a four-message
migration that produced two files and then spent fifty turns re-planning. The
chain the agent is meant to hold is

    USER GOAL -> ACTIVE PHASE -> ACCEPTANCE -> MUTATIONS -> VERIFICATION
              -> MARK COMPLETE -> NEXT PHASE

and three of those nodes did not exist in the code. See
``docs/PHASE-STATE-PLAN.md`` for the defects each of these pins down.
"""

from __future__ import annotations

import json
from pathlib import Path

from dakcoder_shared.paths import Workspace

from dakcoder_agent.context import ContextManager
from dakcoder_agent.gate import GateReport, StageResult
from dakcoder_agent.loop import MAX_RETRIEVAL_REPEATS, AgentLoop, _FORCE_FINISH, _State
from dakcoder_agent.modes import Intent, Mode
from dakcoder_agent.plan import PlanRecord
from dakcoder_agent.tools.control import MAX_STEP_PATHS, PlanStep, split_paths
from dakcoder_agent.tools.router import Router
from scripted import build, calls, patch, plan_call, say  # noqa: E402
from scripted import gated, planning_router, written  # noqa: F401,E402


def _loop() -> AgentLoop:
    loop = AgentLoop.__new__(AgentLoop)
    loop.router = Router(Workspace(Path.cwd()))
    loop.state = _State()
    loop.state.mode = Mode.AGENT
    loop.session_id = ""
    loop.context = ContextManager(mode=Mode.AGENT, system_prompt="s")
    loop._plan_record = PlanRecord()
    return loop


#: The plan shape the field session actually submitted: two file steps and one
#: naming a directory.
FIELD_PLAN = (
    PlanStep("routes/routes.go.bak", "back up the routes file", "the .bak exists"),
    PlanStep("migration.md", "write the phase-wise plan", "migration.md exists"),
    PlanStep("handler", "migrate all 8 handlers", "they compile"),
)


def _clean() -> GateReport:
    return GateReport(
        results=(StageResult(name="gofmt", ok=True, blocking=False, content="clean"),)
    )


def _dirty(stage: str = "rules_lint", path: str = "migration.md") -> GateReport:
    """An inner-loop finding *about ``path``*.

    Non-blocking, because nothing in the inner loop blocks -- which is why the
    promotion test reads `warnings` and not `ok`. The key shapes are the two
    `_stage_findings` really produces: `rule|path|message` for `rules_lint`,
    `path|message` for everything else.
    """
    key = f"domain-tags|{path}|bad" if stage == "rules_lint" else f"{path}|bad"
    return GateReport(
        results=(
            StageResult(
                name=stage,
                ok=False,
                blocking=False,
                content=f"{stage}: one finding",
                findings=frozenset({key}),
            ),
        )
    )


def _unavailable(stage: str = "go_diagnostics") -> GateReport:
    """A stage that could not run at all.

    The shape a developer machine with no gopls produces on every single edit.
    It must hold nothing back: a tool that could not run objected to nothing.
    """
    return GateReport(
        results=(
            StageResult(
                name=stage,
                ok=False,
                blocking=False,
                content=f"{stage} is not available: gopls is not on PATH",
                findings=frozenset({f"{stage} is not available: gopls is not on PATH"}),
            ),
        )
    )


# ── D2: a directory step must be reachable ──────────────────────────────────


def test_a_write_under_a_directory_step_satisfies_it() -> None:
    """`handler` could never be marked, because a write to
    `handler/objection.go` is not equal to `handler`."""
    assert PlanStep("handler", "a", "b").covers("handler/objection.go")
    assert PlanStep("handler", "a", "b").covers("handler")


def test_a_neighbouring_directory_does_not_satisfy_it() -> None:
    step = PlanStep("handler", "a", "b")
    assert not step.covers("handlers/x.go")
    assert not step.covers("handler2/x.go")


def test_a_step_naming_the_workspace_root_covers_nothing_by_prefix() -> None:
    """A step that matched every path in the repository is not a plan step, it
    is the absence of one."""
    assert not PlanStep(".", "a", "b").covers("handler/x.go")
    assert not PlanStep("", "a", "b").covers("handler/x.go")


def test_a_file_step_is_unaffected() -> None:
    step = PlanStep("go.mod", "a", "b")
    assert step.covers("go.mod")
    assert not step.covers("go.mod.bak")
    assert not step.covers("sub/go.mod")


def test_a_directory_step_stops_poisoning_the_unwritten_list() -> None:
    """The compounding failure: a permanently unwritten step spent the single
    `MAX_FINISH_REFUSALS` push-back on turn one and left every later `finish`
    accepted unconditionally, whatever was really outstanding."""
    loop = _loop()
    list(loop._adopt_plan(FIELD_PLAN, "migrate"))
    assert loop._unwritten_targets() == [
        "routes/routes.go.bak",
        "migration.md",
        "handler",
    ]

    loop.router.touched.append("handler/objection.go")
    loop._mark_steps("handler/objection.go", "written")

    assert "handler" not in loop._unwritten_targets()


# ── D1: re-planning must not un-do work that is on disk ─────────────────────


def test_resubmitting_the_same_plan_keeps_what_is_done() -> None:
    """The field session re-submitted an identical eight-step plan three times;
    each time the two files genuinely written stopped counting as done."""
    loop = _loop()
    list(loop._adopt_plan(FIELD_PLAN, "migrate"))
    for path in ("routes/routes.go.bak", "migration.md"):
        loop.router.touched.append(path)
        loop._mark_steps(path, "done")

    list(loop._adopt_plan(FIELD_PLAN, "migrate"))

    assert [s.status for s in loop.state.plan] == ["done", "done", "pending"]


def test_a_reworded_step_keeps_its_status_and_takes_the_new_wording() -> None:
    loop = _loop()
    list(loop._adopt_plan(FIELD_PLAN, "migrate"))
    loop._mark_steps("migration.md", "done")

    reworded = (
        FIELD_PLAN[0],
        PlanStep("migration.md", "write a better plan", "migration.md exists"),
        FIELD_PLAN[2],
    )
    list(loop._adopt_plan(reworded, "migrate"))

    step = next(s for s in loop.state.plan if s.file == "migration.md")
    assert step.status == "done"
    assert step.action == "write a better plan"


def test_a_genuinely_new_step_is_still_adopted() -> None:
    loop = _loop()
    list(loop._adopt_plan(FIELD_PLAN, "migrate"))
    loop._mark_steps("migration.md", "done")

    list(loop._adopt_plan((*FIELD_PLAN, PlanStep("main.go", "drop the import", "builds")), "m"))

    assert [s.file for s in loop.state.plan][-1] == "main.go"
    assert loop.state.plan[-1].status == "pending"


def test_a_settled_step_the_new_plan_forgets_is_kept() -> None:
    """Dropping it would make the same claim the old rule made: that nothing
    happened."""
    loop = _loop()
    list(loop._adopt_plan(FIELD_PLAN, "migrate"))
    loop._mark_steps("migration.md", "done")

    list(loop._adopt_plan((PlanStep("main.go", "drop the import", "builds"),), "m"))

    assert ("migration.md", "done") in [(s.file, s.status) for s in loop.state.plan]


def test_a_failed_step_is_retried_rather_than_carried() -> None:
    """A step the model has just re-stated is one it intends to attempt again,
    and a stale `failed` would read as a verdict on an attempt that has not
    happened yet."""
    loop = _loop()
    list(loop._adopt_plan(FIELD_PLAN, "migrate"))
    loop._mark_steps("migration.md", "failed", "the gate blocked at go_build")

    list(loop._adopt_plan(FIELD_PLAN, "migrate"))

    step = next(s for s in loop.state.plan if s.file == "migration.md")
    assert step.status == "pending"
    assert step.note == ""


# ── D4: verification between write and done ─────────────────────────────────


def test_a_write_reaches_written_not_done() -> None:
    """`done` used to mean "a write happened", so a file written wrongly was a
    completed step."""
    loop = _loop()
    list(loop._adopt_plan(FIELD_PLAN, "migrate"))
    loop._mark_steps("migration.md", "written")

    assert next(s for s in loop.state.plan if s.file == "migration.md").status == "written"


def test_a_clean_inner_gate_promotes_written_to_done() -> None:
    loop = _loop()
    list(loop._adopt_plan(FIELD_PLAN, "migrate"))
    loop._mark_steps("migration.md", "written")

    loop._verify_written(_clean())

    assert next(s for s in loop.state.plan if s.file == "migration.md").status == "done"


def test_a_dirty_inner_gate_leaves_it_written_and_says_why() -> None:
    loop = _loop()
    list(loop._adopt_plan(FIELD_PLAN, "migrate"))
    loop._mark_steps("migration.md", "written")

    loop._verify_written(_dirty("gofmt"))

    step = next(s for s in loop.state.plan if s.file == "migration.md")
    assert step.status == "written"
    assert "gofmt" in step.note


def test_a_finding_about_another_file_does_not_hold_a_step_back() -> None:
    """Per file, not per report. A lint finding in a file this step never
    touched is not this step's problem."""
    loop = _loop()
    list(loop._adopt_plan(FIELD_PLAN, "migrate"))
    loop._mark_steps("migration.md", "written")

    loop._verify_written(_dirty("rules_lint", path="handler/objection.go"))

    assert next(s for s in loop.state.plan if s.file == "migration.md").status == "done"


def test_a_tool_that_could_not_run_holds_nothing_back() -> None:
    """The failure mode that would have made this node worse than not having
    it. `go_diagnostics` fails outright with no gopls, so "any warning holds
    every written step" would strand every step on most developer machines --
    defect D2's compounding failure rebuilt in a new place."""
    loop = _loop()
    list(loop._adopt_plan(FIELD_PLAN, "migrate"))
    loop._mark_steps("migration.md", "written")

    loop._verify_written(_unavailable())

    assert next(s for s in loop.state.plan if s.file == "migration.md").status == "done"


def test_a_finding_under_a_directory_step_holds_that_step_back() -> None:
    loop = _loop()
    list(loop._adopt_plan(FIELD_PLAN, "migrate"))
    loop._mark_steps("handler/objection.go", "written")

    loop._verify_written(_dirty("rules_lint", path="handler/objection.go"))

    assert next(s for s in loop.state.plan if s.file == "handler").status == "written"


def test_verification_does_not_disturb_other_statuses() -> None:
    loop = _loop()
    list(loop._adopt_plan(FIELD_PLAN, "migrate"))
    loop._mark_steps("migration.md", "written")
    loop._mark_steps("handler/objection.go", "failed", "blocked at go_build")

    loop._verify_written(_clean())

    statuses = {s.file: s.status for s in loop.state.plan}
    assert statuses["migration.md"] == "done"
    assert statuses["handler"] == "failed"
    assert statuses["routes/routes.go.bak"] == "pending"


def test_pre_existing_lint_findings_do_not_hold_a_step_back() -> None:
    """A legacy file's violations are not this step's failure to fix, and the
    old-news test is the same one the message to the model uses."""
    loop = _loop()
    loop.state.baseline.findings["rules_lint"] = frozenset({"domain-tags|migration.md|bad"})
    loop.state.baseline.rule_classes["rules_lint"] = frozenset({"domain-tags"})
    loop.state.baseline.passed["rules_lint"] = False
    object.__setattr__(loop.state.baseline, "taken", True)
    list(loop._adopt_plan(FIELD_PLAN, "migrate"))
    loop._mark_steps("migration.md", "written")

    loop._verify_written(_dirty("rules_lint"))

    assert next(s for s in loop.state.plan if s.file == "migration.md").status == "done"


def test_a_written_step_is_not_an_unwritten_target() -> None:
    """"You planned to write this and did not" is the wrong objection about a
    file that is on disk."""
    loop = _loop()
    list(loop._adopt_plan(FIELD_PLAN, "migrate"))
    loop.router.touched.append("migration.md")
    loop._mark_steps("migration.md", "written")

    assert "migration.md" not in loop._unwritten_targets()


def test_the_completion_guard_distinguishes_unwritten_from_unverified() -> None:
    loop = _loop()
    list(loop._adopt_plan((FIELD_PLAN[1],), "migrate"))
    loop.router.touched.append("migration.md")
    loop._mark_steps("migration.md", "written")
    loop._verify_written(_dirty("gofmt"))

    reason = loop._why_not_done()

    assert "migration.md" in reason
    assert "written" in reason and "not" in reason
    assert "planned to write" not in reason


# ── D6: the active phase, rendered as a cursor ──────────────────────────────


def _block(loop) -> str:
    return "\n".join(loop._plan_block())


def test_the_block_names_one_active_step_not_a_checklist() -> None:
    """A model handed eight pending items attempts all eight. The field session
    died with three replies in a row cut off mid-tool-call against the
    16,384-token output budget of the time, having written two files out of
    eight steps."""
    loop = _loop()
    list(loop._adopt_plan(FIELD_PLAN, "migrate"))

    block = _block(loop)

    assert "Now: step 1 of 3 — routes/routes.go.bak" in block
    assert "Accepts: the .bak exists" in block
    # The other two are not spelled out in full; only the one that comes next.
    assert "migrate all 8 handlers" not in block
    assert "Next: step 2 — migration.md" in block


def test_the_cursor_advances_as_steps_settle() -> None:
    loop = _loop()
    list(loop._adopt_plan(FIELD_PLAN, "migrate"))
    loop._mark_steps("routes/routes.go.bak", "done")

    block = _block(loop)

    assert "Now: step 2 of 3 — migration.md" in block
    assert "Done: 1 routes/routes.go.bak" in block


def test_a_failed_step_is_the_active_one_and_carries_its_reason() -> None:
    loop = _loop()
    list(loop._adopt_plan(FIELD_PLAN, "migrate"))
    loop._mark_steps("routes/routes.go.bak", "failed", "go_build, turn 3")

    block = _block(loop)

    assert "Now: step 1 of 3 [failed] — routes/routes.go.bak" in block
    assert "Note: go_build, turn 3" in block


def test_pending_work_comes_before_unverified_work() -> None:
    """A written-but-unverified step is the cheapest thing in the plan to close,
    and a run that polished while three steps had never been attempted is the
    shape of a run that finishes nothing."""
    loop = _loop()
    list(loop._adopt_plan(FIELD_PLAN, "migrate"))
    loop._mark_steps("routes/routes.go.bak", "written")

    assert loop.active_step is not None
    index, step = loop.active_step
    assert (index, step.file) == (2, "migration.md")


def test_an_unverified_step_becomes_active_once_nothing_is_pending() -> None:
    loop = _loop()
    list(loop._adopt_plan(FIELD_PLAN, "migrate"))
    loop._mark_steps("routes/routes.go.bak", "written")
    loop._mark_steps("migration.md", "done")
    loop._mark_steps("handler/objection.go", "done")

    assert loop.active_step is not None
    assert loop.active_step[1].file == "routes/routes.go.bak"
    assert "Now: step 1 of 3 [written]" in _block(loop)


def test_the_last_step_says_it_is_the_last() -> None:
    loop = _loop()
    list(loop._adopt_plan((FIELD_PLAN[0],), "migrate"))

    assert "Next: nothing — this is the last step." in _block(loop)


def test_a_settled_plan_says_so() -> None:
    loop = _loop()
    list(loop._adopt_plan(FIELD_PLAN, "migrate"))
    for step in FIELD_PLAN:
        loop._mark_steps(step.file, "done")

    assert loop.active_step is None
    assert "all 3 step(s) settled" in _block(loop)


def test_a_settled_plan_does_not_declare_completion_while_the_gate_fails() -> None:
    """MARK PHASE COMPLETE must not fire ahead of VERIFICATION. `_note_tried`
    only marks a step failed when the blocking stage names its file, and a build
    error often names a package or another file entirely -- so "all settled"
    next to "Last gate: FAIL" is a reachable contradiction."""
    loop = _loop()
    list(loop._adopt_plan((FIELD_PLAN[1],), "migrate"))
    loop._mark_steps("migration.md", "done")
    loop.state.last_gate = GateReport(
        results=(
            StageResult(name="go_build", ok=False, blocking=True, content="boom"),
        )
    )

    block = _block(loop)

    assert "the gate is failing at go_build" in block
    assert "the work is not done" in block
    assert "all 1 step(s) settled" not in block


# ── D3: a follow-up must not be forced to re-plan ───────────────────────────


def test_a_first_message_plans() -> None:
    loop = _loop()
    assert loop._opening_mode(Intent.AGENT, continued=False) is Mode.PLANNER


def test_a_question_never_plans() -> None:
    loop = _loop()
    assert loop._opening_mode(Intent.ASK, continued=True) is Mode.ASK


def test_a_follow_up_on_an_open_plan_goes_straight_to_work() -> None:
    """"Complete the migration plan you wrote" and "start phase 2" each left the
    model exactly one forward move: write the plan again. PLANNER has no "the
    plan stands, carry on" exit."""
    loop = _loop()
    list(loop._adopt_plan(FIELD_PLAN, "migrate"))

    assert loop._opening_mode(Intent.AGENT, continued=True) is Mode.AGENT


def test_a_follow_up_on_a_finished_plan_plans_again() -> None:
    """A developer asking for more work on a finished plan is asking for a new
    one."""
    loop = _loop()
    list(loop._adopt_plan(FIELD_PLAN, "migrate"))
    for step in FIELD_PLAN:
        loop._mark_steps(step.file, "done")

    assert loop._opening_mode(Intent.AGENT, continued=True) is Mode.PLANNER


def test_a_follow_up_with_no_plan_at_all_plans() -> None:
    loop = _loop()
    assert loop._opening_mode(Intent.AGENT, continued=True) is Mode.PLANNER


# ── D5: a bad plan needs an advertised exit ─────────────────────────────────


def test_the_finish_refusal_names_revise_plan(planning_router, gated, written) -> None:
    """A field run diagnosed its own plan correctly in the prose of a `finish`
    -- "the go.mod cleanup should be the last step" -- and never called
    `revise_plan`, because nothing had ever named it as the answer to "the plan
    is wrong". It had two advertised exits and both were "stop"."""
    quit_early = calls(("finish", json.dumps({"answer": "I have what I need."})))
    loop, _client = build(
        planning_router, [plan_call(), quit_early, patch(), say("done")], max_turns=10
    )
    list(loop.run("add Routes", intent=Intent.AGENT))

    pushed = [m.content for m in loop.context.build() if m.content.startswith("Not yet.")]

    assert pushed, "a finish that wrote nothing was accepted"
    assert "revise_plan" in pushed[0]
    assert "the *plan* is what is wrong" in pushed[0]


# ── D7: the four ways the fixes above still let the loop form ───────────────
#
# The second field session on the same transcript. Every node above was in
# place and the run still spent fifteen turns restating one plan step, because
# the nodes interacted: a cursor that could not move, a stall turn that took
# the write tools away, a step no write could satisfy, and a `written` status
# with only one way out of it.


def _globbed() -> tuple[PlanStep, ...]:
    """The plan shape the second field session submitted: two of its eight
    steps were globs, and `covers` matched neither."""
    return (
        PlanStep("migration.md", "write the plan", "migration.md exists"),
        PlanStep("handler/response/*.go", "response wire types", "they exist"),
        PlanStep("repo/postgres/*.go", "migrate the repositories", "dblib.Psql"),
    )


def test_a_glob_step_is_satisfied_by_a_write_under_it() -> None:
    """`_normalise_plan` keeps the `*` verbatim, so a glob step matched neither
    by equality nor by prefix -- unsatisfiable for the life of the session."""
    step = PlanStep("handler/response/*.go", "a", "b")

    assert step.covers("handler/response/user.go")
    assert step.covers("handler/response/sub/user.go")
    assert not step.covers("handler/response/user.md")
    assert not step.covers("handler/request.go")


def test_the_directory_and_file_cases_are_unchanged_by_globbing() -> None:
    assert PlanStep("handler", "a", "b").covers("handler/objection.go")
    assert not PlanStep("handler", "a", "b").covers("handlers/x.go")
    assert PlanStep("go.mod", "a", "b").covers("go.mod")
    assert not PlanStep("go.mod", "a", "b").covers("go.mod.bak")


def test_a_glob_plan_can_be_finished(planning_router: Router, gated) -> None:
    """The compounding failure, end to end. Two glob steps kept `_open_targets`
    permanently non-empty, so every `finish` was refused once on an objection
    the model could not satisfy and every run reported "those were never
    written" under a clean gate."""
    def write(path: str):
        return calls(("write_file", json.dumps({"path": path, "content": "package x\n"})))

    plan = json.dumps({
        "steps": [{"file": s.file, "action": s.action, "accepts": s.accepts} for s in _globbed()],
        "summary": "migrate",
    })
    loop, _client = build(planning_router, [
        calls(("submit_plan", plan)),
        write("migration.md"),
        write("handler/response/user.go"),
        write("repo/postgres/paogen.go"),
        calls(("finish", json.dumps({"answer": "all three steps are written."}))),
    ], max_turns=10)
    list(loop.run("migrate the service", intent=Intent.AGENT))

    assert [s.status for s in loop.state.plan] == ["done", "done", "done"]
    assert "never written" not in loop.result.summary


def test_the_cursor_says_how_long_it_has_been_on_this_step() -> None:
    """The block was byte-identical every turn while a step stayed pending, and
    a standing order in the recency slot with nothing in it that moves is one
    the model obeys from the top each turn -- fifteen verbatim restatements."""
    loop = _loop()
    list(loop._adopt_plan(FIELD_PLAN, "migrate"))

    loop.context._turn = 3
    assert "On this step since" not in _block(loop), "the turn it lands says nothing"

    loop.context._turn = 9
    line = next(ln for ln in loop._plan_block() if "On this step since" in ln)

    assert "turn 3" in line and "6 turn(s)" in line
    assert "nothing has been written to routes/routes.go.bak yet" in line


def test_the_cursor_resets_when_the_step_changes() -> None:
    loop = _loop()
    list(loop._adopt_plan(FIELD_PLAN, "migrate"))
    loop.context._turn = 3
    _block(loop)
    loop.context._turn = 9
    assert "6 turn(s)" in _block(loop)

    loop._mark_steps("routes/routes.go.bak", "done")
    assert "On this step since" not in _block(loop)
    loop.context._turn = 11
    assert "2 turn(s)" in _block(loop)


def test_a_stalled_acting_turn_keeps_its_write_tools(
    planning_router: Router, gated, written
) -> None:
    """The turn that produced "I am in a read-only phase and cannot write
    files". A stall cut the tool list to `finish` alone, which with work still
    named is a turn the model can only answer by abandoning it -- and it
    answered accurately."""
    search = calls(("search_repo", json.dumps({"pattern": "^type .* struct"})))
    repeat = calls(("search_repo", json.dumps({"pattern": "^type .* struct"})))
    loop, client = build(
        planning_router,
        [plan_call(), search, repeat, repeat, patch(), say("done")],
        max_turns=10,
    )
    list(loop.run("add Routes", intent=Intent.AGENT))

    forced = [
        offered
        for choice, offered in zip(client.tool_choices, client.seen_tools)
        if choice == "required"
    ]

    assert forced, "the stall never reached a forced turn"
    for offered in forced:
        assert "write_file" in offered and "patch_file" in offered
        assert set(offered) != {"finish"}
    pushed = [m.content for m in loop.context.build() if m.content.startswith("Stop searching.")]
    assert pushed and "Write it now" in pushed[0]


def test_a_clean_gate_settles_a_step_stranded_at_written(
    planning_router: Router, gated, written
) -> None:
    """`_verify_written` runs only from `_inner_loop`, which runs only after a
    mutating batch -- so a step whose file the run never touched again sat at
    `written` forever: `open` is false so nothing asked for it, while
    `_why_not_done` objected to it and the cursor stayed pinned to it."""
    loop, _client = build(
        planning_router, [plan_call(), patch(), say("done")], max_turns=10
    )
    list(loop.run("add Routes", intent=Intent.AGENT))
    assert loop.state.last_gate is not None and loop.state.last_gate.ok

    loop._mark_steps("handler/user.go", "written", "gofmt is not clean on it yet")
    loop._settle_written()

    step = next(s for s in loop.state.plan if s.file == "handler/user.go")
    assert (step.status, step.note) == ("done", "")


def test_the_finish_result_does_not_claim_delivery(planning_router: Router) -> None:
    """It said "the developer has your reply", which the handler cannot know:
    `_phase_ended` reads the plan and the gate after this result is already in
    the transcript, and a `finish` that walks away from unwritten work is sent
    straight back. The model held both statements at once."""
    out = planning_router.dispatch("finish", {"answer": "the migration is done."}, mode=Mode.AGENT)

    assert out.ok
    assert "has your reply" not in out.content
    assert "decided after this call" in out.content
    assert len(out.content) < 200, "echoing the answer put it in the transcript twice"


# ── a `file` field naming several files ─────────────────────────────────────
#
# The third shape `covers` could not read, after the directory and the glob, and
# the one that cost a whole first phase. A migration plan submitted
# `"go.work, go.work.sum"`, `"cover.html, coverage, gin.log"` and
# `"docs/docs.go, docs/swagger.json"` as three of its seven steps, and each of
# those strings matched nothing at all: not the delete that carried the step
# out, not the write that followed it, not the cursor's own "has anything landed
# on this step" line. The run deleted `go.work`, was told the plan did not ask
# for that and wrote it back, read its step saying to delete it, and deleted it
# again -- four times in eight turns, every turn a real mutation.


def test_a_comma_separated_field_is_a_list_of_paths() -> None:
    assert split_paths("go.work, go.work.sum") == ("go.work", "go.work.sum")
    assert split_paths("cover.html, coverage, gin.log") == (
        "cover.html",
        "coverage",
        "gin.log",
    )
    assert split_paths("docs/docs.go, docs/swagger.json") == (
        "docs/docs.go",
        "docs/swagger.json",
    )


def test_the_comma_form_does_not_ask_the_tokens_to_look_like_paths() -> None:
    """`coverage` is a real file with no extension, and it was in the field that
    produced this bug. Requiring a dot or a slash of every token would refuse
    exactly the case."""
    assert split_paths("cover.html, coverage, gin.log")[1] == "coverage"


def test_an_ordinary_step_is_left_alone() -> None:
    for one in ("handler/paogen.go", "handler", "handler/response/*.go", "go.mod"):
        assert split_paths(one) == (one,), one


def test_whitespace_splits_only_when_every_token_is_path_shaped() -> None:
    """A directory step is one bare word and `my docs/readme.md` is one path
    with a space in it. Splitting either invents steps nothing can satisfy."""
    assert split_paths("a.go b.go") == ("a.go", "b.go")
    assert split_paths("my docs/readme.md") == ("my docs/readme.md",)
    assert split_paths("the handler package") == ("the handler package",)


def test_a_manifest_is_not_a_step() -> None:
    """Past `MAX_STEP_PATHS` this is a file list pasted into the wrong box, and
    splitting it would put a dozen pending items in front of a model whose whole
    problem is attempting everything it is shown."""
    many = ", ".join(f"f{i}.go" for i in range(MAX_STEP_PATHS + 1))
    assert split_paths(many) == (many,)


def test_a_blank_field_names_nothing() -> None:
    assert split_paths("") == ()
    assert split_paths("   ") == ()


def test_the_plan_splits_a_multi_file_step_into_reachable_steps() -> None:
    """Through `_normalise_plan`, which is where both `submit_plan` and
    `revise_plan` pass. Each path becomes its own step, carrying the same
    action, phase and acceptance -- so each is closable on its own and the run
    can say which of the three artefacts it actually removed."""
    loop = _loop()
    steps = loop._normalise_plan(
        (
            PlanStep(
                "go.work, go.work.sum",
                "Delete go.work and go.work.sum",
                "they are gone",
                phase="deps",
                part="artifacts",
            ),
        )
    )

    assert [s.file for s in steps] == ["go.work", "go.work.sum"]
    assert all(s.phase == "deps" and s.part == "artifacts" for s in steps)
    assert all(s.action == "Delete go.work and go.work.sum" for s in steps)
    assert steps[0].covers("go.work") and steps[1].covers("go.work.sum")


def test_a_split_step_is_marked_by_the_write_that_lands_on_it() -> None:
    """The join that was broken. `_mark_steps` filters by `covers`, so nothing
    the run did could ever change the status of the unsplit step."""
    loop = _loop()
    loop.state.plan = loop._normalise_plan(
        (PlanStep("go.work, go.work.sum", "delete both", "gone", phase="deps"),)
    )

    loop._mark_steps("go.work", "written")

    assert [s.status for s in loop.state.plan] == ["written", "pending"]


def test_a_duplicate_path_in_the_field_becomes_one_step() -> None:
    """Two steps on one file are two cursors on it, and the second could never
    be closed by a write the first consumed."""
    loop = _loop()
    steps = loop._normalise_plan(
        (PlanStep("go.work, go.work", "delete it", "gone"),)
    )
    assert [s.file for s in steps] == ["go.work"]


# ── the cacheable head of the request ───────────────────────────────────────
#
# The server reuses the KV state of a *prefix*, so the saving turns entirely on
# the head of the request being byte-identical between turns. The message layers
# are ordered for that and measured (`ContextManager.build`). The head in front
# of them was not: the chat template serialises the tool schemas ahead of the
# system message, so the tools array is position zero — and two rules rewrote it
# mid-run, each costing a full re-prefill that nothing reported.
#
# On this manager's own `novel_tokens`, a break at turn 20 re-prefills ~15,000
# tokens and at turn 100 ~76,000.


def _headed() -> AgentLoop:
    loop = _loop()
    loop.state.mode = Mode.AGENT
    return loop


def _tools_named(*names: str) -> list[dict]:
    return [{"type": "function", "function": {"name": n, "parameters": {}}} for n in names]


def test_the_first_turn_writes_the_cache_rather_than_breaking_it() -> None:
    loop = _headed()
    assert loop._note_prefix(_tools_named("read_file", "write_file")) == ""
    assert loop.state.prefix_break == ""


def test_an_unchanged_head_is_not_a_break() -> None:
    loop = _headed()
    tools = _tools_named("read_file", "write_file")
    loop._note_prefix(tools)
    assert loop._note_prefix(list(tools)) == ""


def test_dropping_a_tool_is_reported_as_a_break() -> None:
    """The `search_docs` withdrawal that used to happen at a repeat cap."""
    loop = _headed()
    loop._note_prefix(_tools_named("read_file", "search_docs", "write_file"))

    assert loop._note_prefix(_tools_named("read_file", "write_file")) == "tools"
    assert loop.state.prefix_break == "tools"


def test_narrowing_to_the_terminals_is_reported_as_a_break() -> None:
    """The other one: a forced-answer turn used to send a one-tool list."""
    loop = _headed()
    loop._note_prefix(_tools_named("read_file", "write_file", "finish"))
    assert loop._note_prefix(_tools_named("finish")) == "tools"


def test_reordering_the_tools_is_a_break() -> None:
    """Order is part of the token stream, so it is part of the prefix."""
    loop = _headed()
    loop._note_prefix(_tools_named("read_file", "write_file"))
    assert loop._note_prefix(_tools_named("write_file", "read_file")) == "tools"


def test_a_mode_switch_is_reported() -> None:
    """Unavoidable and correct — different modes need different tools — but it
    is still a cold prefill, and a number nobody sees is a number nobody
    budgets for."""
    loop = _headed()
    tools = _tools_named("read_file")
    loop._note_prefix(tools)
    loop.state.mode = Mode.PLANNER
    assert loop._note_prefix(tools) == "mode"


def test_the_first_part_to_move_is_the_one_reported() -> None:
    """A change in the tools invalidates the system message after it whether or
    not that also changed; naming all three would be three findings about one
    event."""
    loop = _headed()
    loop._note_prefix(_tools_named("read_file"))
    loop.state.mode = Mode.PLANNER
    assert loop._note_prefix(_tools_named("write_file")) == "tools"


# ── and the two rules that were breaking it ─────────────────────────────────


def test_search_docs_stays_in_the_schema_past_the_repeat_cap(planning_router) -> None:
    """It used to be withdrawn here, which moved position zero of the prompt."""
    loop = _loop()
    loop.router = planning_router
    loop.state.mode = Mode.ASK

    before = [t["function"]["name"] for t in loop._tools()]
    assert "search_docs" in before, "the fixture does not offer the tool under test"

    loop.state.retrieval_repeats = MAX_RETRIEVAL_REPEATS + 1
    assert [t["function"]["name"] for t in loop._tools()] == before, (
        "the tool list moved to enforce a repeat cap, which re-prefills the prompt"
    )


def test_a_single_terminal_mode_names_the_tool_instead_of_narrowing(planning_router) -> None:
    """`required` over one tool and a named choice over all of them are the same
    constraint; only one of them moves the prompt."""
    loop = _loop()
    loop.router = planning_router
    loop.state.mode = Mode.AGENT
    tools = loop._tools()

    offered, choice = loop._terminal_request(tools, "required")

    assert offered == tools, "the list was narrowed, which re-prefills the whole prompt"
    assert choice == {"type": "function", "function": {"name": "finish"}}


def test_the_planner_still_narrows_because_it_has_three_answers(planning_router) -> None:
    """Its terminals are three different answers to "what was this task?", and
    naming one of them is the mistake that wrote an unrequested migration
    (BUG L-28). One re-prefill is the price of not making that choice."""
    loop = _loop()
    loop.router = planning_router
    loop.state.mode = Mode.PLANNER
    tools = loop._tools()

    offered, choice = loop._terminal_request(tools, "required")

    names = {t["function"]["name"] for t in offered}
    assert names <= {"submit_plan", "ask_developer", "finish"}
    assert len(names) > 1, "the planner must keep its choice"
    assert choice == "required"


def test_a_named_force_is_left_alone(planning_router) -> None:
    """The second force already names `finish`; it needs no list at all."""
    loop = _loop()
    loop.router = planning_router
    loop.state.mode = Mode.AGENT
    tools = loop._tools()

    offered, choice = loop._terminal_request(tools, _FORCE_FINISH)

    assert offered == tools
    assert choice == _FORCE_FINISH
