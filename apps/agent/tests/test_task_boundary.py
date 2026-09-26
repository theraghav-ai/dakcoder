"""Where one task ends and the next begins, inside one conversation.

Session 256a4ac40856 is the field report. One session: write AGENTS.md, three
questions, then "employee basic details, all crud, new handler file, new
database ddl". Nothing marked the end of the first task, so:

* the pinned ``# Task`` was still "Create or improve AGENTS.md", and the model
  answered the CRUD request with the AGENTS.md summary -- three times;
* the adopted plan put the finished AGENTS.md step in front of the new work;
* every result listed AGENTS.md as changed by the CRUD task, and the gate was
  scoped to it.

A follow-up now starts a new task when the last one is over, and the tests
below pin both halves: what a new task must not inherit, and what a
continuation ("try again", an answer, open steps, a migration) must keep.
"""

from __future__ import annotations

import json

from dakcoder_agent.loop import _TASK_SCOPED, AgentLoop, _State
from dakcoder_agent.messages import Layer
from dakcoder_agent.migration import MigrationState, Phase
from dakcoder_agent.modes import Intent, Mode
from dakcoder_agent.tools.control import PlanStep
from scripted import build, calls, gated, planning_router  # noqa: F401  (fixtures)


def _write(path: str, content: str = "package x\n"):
    return calls(("write_file", json.dumps({"path": path, "content": content})))


def _plan(*files: str, summary: str = "") -> str:
    return json.dumps(
        {
            "summary": summary,
            "steps": [{"file": f, "action": f"create {f}", "accepts": "exists"} for f in files],
        }
    )


def _finish(answer: str):
    return calls(("finish", json.dumps({"answer": answer})))


def _follow_up(previous: AgentLoop, turns, **kw) -> AgentLoop:
    """The next message of the same session, the way `Loopback._spawn` builds it."""
    nxt, _ = build(previous.router, turns, **kw)
    nxt.context = previous.context
    nxt.session_id = previous.session_id
    nxt.carry_from(previous)
    return nxt


def _first_task(router) -> AgentLoop:
    loop, _ = build(
        router,
        [
            calls(("submit_plan", _plan("NOTES.md", summary="Write the notes file."))),
            _write("NOTES.md", "# notes\n"),
            _finish("Created NOTES.md."),
        ],
    )
    loop.session_id = "s-boundary"
    list(loop.run("Create NOTES.md describing the service.", intent=Intent.AGENT))
    assert loop.result is not None and loop.result.outcome == "done", loop.result
    return loop


def _pinned_task(loop: AgentLoop) -> str:
    return next(m.content for m in loop.context.build() if m.layer is Layer.TASK)


# ── the field report, end to end ────────────────────────────────────────────


def test_a_new_request_after_a_finished_task_is_a_new_task(planning_router, gated) -> None:
    first = _first_task(planning_router)

    second = _follow_up(
        first,
        [
            calls(("submit_plan", _plan("handler/employee.go", summary="Employee CRUD."))),
            _write("handler/employee.go"),
            _finish("Added the employee handler."),
        ],
    )
    list(second.run("employee basic details, all crud, new handler file", continued=True,
                    intent=Intent.AGENT))

    # The pinned task is the new request; the old one is context, labelled.
    task = _pinned_task(second)
    assert task.startswith("# Task\nemployee basic details")
    assert "finished; context only" in task and "Create NOTES.md" in task
    # The plan is the new task's alone.
    assert [s.file for s in second.state.plan] == ["handler/employee.go"]
    # And so is the change set the gate and the result report.
    assert second.router.touched == ["handler/employee.go"]
    assert second.result is not None
    assert "NOTES.md" not in second.result.mutations
    # The old plan is archived, not lost.
    causes = [r.cause for r in second._plan_record.revisions]
    assert "new task" in causes


def test_a_step_on_a_file_the_last_task_finished_is_not_born_done(planning_router, gated) -> None:
    """The quieter half of the bug: `_adopt_plan` carried a settled status onto
    any new step naming the same file, so the new task's edit to a file the old
    task had written was 'done' before it started."""
    first = _first_task(planning_router)
    second = _follow_up(first, [calls(("submit_plan", _plan("NOTES.md", summary="Rewrite it.")))],
                        max_turns=1)
    list(second.run("rewrite NOTES.md in the new house style", continued=True, intent=Intent.AGENT))
    assert [s.status for s in second.state.plan] == ["pending"]


def test_a_new_task_forgets_the_last_tasks_ledgers(planning_router, gated) -> None:
    first = _first_task(planning_router)
    first.state.tried.append("turn 3: gate failed at go_build")
    first.state.finish_refused = 1
    first.state.reply_repeats = 3
    first.state.removed.add("old.go")

    second = _follow_up(first, [_finish("ok")], max_turns=2)
    list(second.run("add a health endpoint", continued=True, intent=Intent.ASK))

    assert second.state.tried == []
    assert second.state.finish_refused == 0
    assert second.state.reply_repeats == 0
    assert second.state.removed == set()


def test_a_new_task_keeps_what_the_session_learned(planning_router, gated) -> None:
    first = _first_task(planning_router)
    first.state.answered = "use the development branch"
    reads = dict(first.state.reads)

    second = _follow_up(first, [_finish("ok")], max_turns=2)
    list(second.run("what does the bootstrapper do?", continued=True, intent=Intent.ASK))

    assert second.state.answered == "use the development branch"
    assert second.state.reads == reads
    assert second.context.said[0] == "Create NOTES.md describing the service."


# ── what stays one task ─────────────────────────────────────────────────────


def _loop_after(outcome: str = "done") -> AgentLoop:
    loop = AgentLoop.__new__(AgentLoop)
    loop.state = _State()
    loop.state.previous_outcome = outcome
    return loop


def test_the_rule() -> None:
    assert _loop_after("done")._starts_new_task("add an audit log")

    unfinished = _loop_after("unverified")
    assert not unfinished._starts_new_task("add an audit log"), "an unfinished task continues"

    for phrase in ("try again", "Please continue", "fix it", "ok, retry"):
        assert not _loop_after("done")._starts_new_task(phrase), phrase

    open_steps = _loop_after("done")
    open_steps.state.plan = (PlanStep("a.go", "do it", "x"),)
    assert not open_steps._starts_new_task("something else")

    written = _loop_after("done")
    written.state.plan = (PlanStep("a.go", "do it", "x", status="written"),)
    assert not written._starts_new_task("something else")

    asked = _loop_after("done")
    asked.state.awaiting = Intent.AGENT
    assert not asked._starts_new_task("the development branch")

    migrating = _loop_after("done")
    migrating.state.migration = MigrationState(active=True, phases=(Phase("a"), Phase("b"), Phase("c")))
    assert not migrating._starts_new_task("next phase")

    # A restart: no outcome in this process, and nothing outstanding.
    assert _loop_after("")._starts_new_task("add an audit log")


def test_try_again_after_a_failed_gate_keeps_the_plan(planning_router, gated) -> None:
    first = _first_task(planning_router)
    first.result = first.result.__class__(
        "unverified", "gate failed", first.result.turns, first.result.mutations, None
    )
    plan = first.state.plan
    second = _follow_up(first, [_finish("done now")], max_turns=2)
    list(second.run("try again", continued=True, intent=Intent.AGENT))
    assert [s.file for s in second.state.plan] == [s.file for s in plan] == ["NOTES.md"]
    assert _pinned_task(second).startswith("# Task\nCreate NOTES.md"), "the task did not change"
    assert "NOTES.md" in second.router.touched


def test_every_task_scoped_field_is_a_real_field() -> None:
    """A name here that the state does not have would reset nothing, silently."""
    fresh = _State()
    for name in _TASK_SCOPED:
        getattr(fresh, name)


def test_the_classifier_still_reads_the_whole_conversation(planning_router, gated) -> None:
    first = _first_task(planning_router)
    second = _follow_up(first, [_finish("ok")], max_turns=2)
    list(second.run("do it for the handlers too", continued=True, intent=Intent.ASK))
    assert second.context.said[-2:] == (
        "Create NOTES.md describing the service.",
        "do it for the handlers too",
    )
    assert second.state.mode in (Mode.ASK, Mode.PLANNER, Mode.AGENT)
