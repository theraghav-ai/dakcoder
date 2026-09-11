"""Work that outlives a turn (the plan) and work that outlives a run (the agenda).

The plan was the best-designed part of the loop and the least durable: a tuple
in a process, lost on a daemon restart, with no record of how it got where it
is. The agenda is the thing that did not exist -- a run that notices real work
outside its scope had two options, do it or mention it in prose that scrolls
away, and neither leaves anything anyone can act on.
"""

from __future__ import annotations

import json

import pytest

from dakcoder_agent.plan import (
    AGENDA_STATES,
    MAX_AGENDA_TASKS,
    AgendaStore,
    AgendaTask,
    PlanRecord,
)
from dakcoder_agent.tools.control import PlanStep


def _steps() -> tuple[PlanStep, ...]:
    return (
        PlanStep("handler/pension.go", "add the resource", "it builds"),
        PlanStep("repo/postgres/pension.go", "add the queries", "go vet is clean"),
    )


# ── the plan ────────────────────────────────────────────────────────────────


def test_a_plan_round_trips_through_disk(tmp_path) -> None:
    record = PlanRecord(session_id="s1").record(_steps(), "migrate pensions")
    record.save(tmp_path)

    back = PlanRecord.load(tmp_path, "s1")

    assert back is not None
    assert back.summary == "migrate pensions"
    assert [s.file for s in back.steps] == [s.file for s in _steps()]
    assert len(back.revisions) == 1


def test_statuses_survive_a_restart(tmp_path) -> None:
    """The statuses were derived from the change set, and the change set is on
    disk -- so restoring them restores a conclusion, not a guess."""
    steps = list(_steps())
    record = PlanRecord(session_id="s2").record(steps, "migrate pensions")
    steps[0] = PlanStep(steps[0].file, steps[0].action, steps[0].accepts, status="done")
    record.with_steps(steps).save(tmp_path)

    back = PlanRecord.load(tmp_path, "s2")

    assert back is not None
    assert [s.status for s in back.steps] == ["done", "pending"]
    assert [s.file for s in back.open_steps] == ["repo/postgres/pension.go"]


def test_a_status_change_is_not_a_revision(tmp_path) -> None:
    """The loop sets `done` on every mutation; recording each as a revision
    would bury the two or three that are actually revisions."""
    record = PlanRecord(session_id="s3").record(_steps(), "one")
    record = record.with_steps(_steps()).with_steps(_steps())

    assert len(record.revisions) == 1


def test_a_revision_records_its_cause_and_reason(tmp_path) -> None:
    record = PlanRecord(session_id="s4").record(_steps(), "one")
    record = record.record(
        _steps()[:1], "one", cause="revised", reason="the repo layer already had it"
    )

    assert [r.cause for r in record.revisions] == ["submitted", "revised"]
    assert record.revisions[-1].reason == "the repo layer already had it"


def test_a_forced_plan_stays_forced_across_a_restart(tmp_path) -> None:
    """A forced plan is not a commitment (BUG L-28), so a resumed run must not
    start enforcing one the developer never asked for."""
    record = PlanRecord(session_id="s5", forced=True).record(_steps(), "extracted")
    record.save(tmp_path)

    back = PlanRecord.load(tmp_path, "s5")

    assert back is not None and back.forced


def test_an_unreadable_plan_is_absent_rather_than_fatal(tmp_path) -> None:
    target = PlanRecord.path_for(tmp_path, "s6")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("{ this is not json", encoding="utf-8")

    assert PlanRecord.load(tmp_path, "s6") is None


def test_a_plan_with_no_session_writes_nothing(tmp_path) -> None:
    """A loop driven by a test or a CLI has no directory; inventing one would
    put files wherever the working directory happened to be."""
    PlanRecord().record(_steps(), "x").save(tmp_path)

    assert not (tmp_path / ".dakcoder").exists()


# ── the agenda ──────────────────────────────────────────────────────────────


def test_a_proposal_round_trips(tmp_path) -> None:
    store = AgendaStore(tmp_path)
    task = AgendaTask.propose(
        "audit the other handlers for the same N+1",
        why="handler/objection.go:412 does what handler/pension.go:88 just stopped doing",
        paths=["handler/objection.go"],
        priority=2,
        origin_session="s1",
    )

    added = store.add(task)

    assert added is not None
    assert [t.title for t in store.open_tasks()] == [task.title]
    assert store.open_tasks()[0].why.startswith("handler/objection.go:412")


def test_the_same_work_is_not_proposed_twice(tmp_path) -> None:
    """An agent that notices the same thing on three runs has noticed one thing,
    and three identical rows is how a backlog stops being read."""
    store = AgendaStore(tmp_path)
    store.add(AgendaTask.propose("Audit The Handlers"))

    assert store.add(AgendaTask.propose("audit the handlers  ")) is None
    assert len(store.open_tasks()) == 1


def test_resolved_work_does_not_block_a_new_proposal(tmp_path) -> None:
    store = AgendaStore(tmp_path)
    first = store.add(AgendaTask.propose("audit the handlers"))
    assert first is not None
    store.move(first.id, "done", by="dev")

    assert store.add(AgendaTask.propose("audit the handlers")) is not None


def test_a_person_moves_the_state(tmp_path) -> None:
    store = AgendaStore(tmp_path)
    task = store.add(AgendaTask.propose("split the god handler"))
    assert task is not None

    moved = store.move(task.id, "approved", by="uideveloper", note="next sprint")

    assert moved is not None
    assert moved.state == "approved"
    assert moved.decided_by == "uideveloper"
    assert moved.note == "next sprint"
    assert store.load()[0].state == "approved"


def test_an_unknown_state_is_refused() -> None:
    with pytest.raises(ValueError):
        AgendaTask.propose("x").moved_to("running")


def test_moving_a_task_that_is_not_there_says_so(tmp_path) -> None:
    assert AgendaStore(tmp_path).move("nope", "approved") is None


def test_open_tasks_come_back_best_first(tmp_path) -> None:
    store = AgendaStore(tmp_path)
    store.add(AgendaTask.propose("low", priority=5))
    store.add(AgendaTask.propose("high", priority=1))
    store.add(AgendaTask.propose("middle", priority=3))

    assert [t.title for t in store.open_tasks()] == ["high", "middle", "low"]


def test_open_work_is_never_dropped_by_the_cap(tmp_path) -> None:
    """An agenda that silently forgets approved work is worse than no agenda,
    because a developer believes it is recorded."""
    store = AgendaStore(tmp_path)
    tasks = [AgendaTask.propose(f"open {n}") for n in range(MAX_AGENDA_TASKS + 20)]
    resolved = [AgendaTask.propose(f"done {n}").moved_to("done") for n in range(50)]

    store.save([*tasks, *resolved])

    kept = store.load()
    assert len([t for t in kept if t.open]) == len(tasks)
    assert len([t for t in kept if not t.open]) == 0, "no room was left for resolved ones"


def test_the_cap_keeps_the_newest_resolved_work(tmp_path) -> None:
    store = AgendaStore(tmp_path)
    resolved = [AgendaTask.propose(f"done {n}").moved_to("done") for n in range(MAX_AGENDA_TASKS + 10)]

    store.save(resolved)

    assert len(store.load()) == MAX_AGENDA_TASKS


def test_a_corrupt_agenda_reads_as_empty(tmp_path) -> None:
    store = AgendaStore(tmp_path)
    store.path.parent.mkdir(parents=True, exist_ok=True)
    store.path.write_text("[[[", encoding="utf-8")

    assert store.load() == []


def test_a_task_without_a_title_is_not_a_task() -> None:
    assert AgendaTask.from_dict({"id": "a", "title": ""}) is None
    assert AgendaTask.from_dict({"id": "", "title": "a"}) is None


def test_priority_is_clamped_on_the_way_in_and_out() -> None:
    assert AgendaTask.propose("x", priority=99).priority == 5
    assert AgendaTask.propose("x", priority=-3).priority == 1
    revived = AgendaTask.from_dict({"id": "a", "title": "b", "priority": 42})
    assert revived is not None and revived.priority == 5


def test_the_agenda_file_is_readable_by_a_person(tmp_path) -> None:
    """One file rather than a row per task, because the whole point is that
    somebody opens it -- and a team may want it committed."""
    store = AgendaStore(tmp_path)
    store.add(AgendaTask.propose("split the god handler", why="6,571 lines"))

    parsed = json.loads(store.path.read_text(encoding="utf-8"))

    assert [t["title"] for t in parsed["tasks"]] == ["split the god handler"]
    assert parsed["tasks"][0]["state"] in AGENDA_STATES
