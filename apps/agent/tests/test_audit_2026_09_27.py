"""Regressions for the 2026-09-27 audit (``AUDIT-2026-09-27.md`` at the project root).

One test per finding that changed behaviour, each written against the field
session that showed it. The forced-edit fix (F3) has two: the escalation of
`_edit_request` on its own, and a scripted model that keeps reading under
force, which is the loop the five audited sessions all died in.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import httpx
import pytest

from dakcoder_agent.gate import Baseline, _finding_keys, full_gate
from dakcoder_agent.loop import (
    _EDIT_TOOLS,
    MAX_RESEARCH_TURNS,
    Intent,
    Outcome,
    _describe,
    _reads_as_question,
)
from dakcoder_agent.loopback import Loopback, create_app
from dakcoder_agent.loopstate import GateState
from dakcoder_agent.metrics import Accumulator
from dakcoder_agent.modes import Mode
from dakcoder_agent.tools import commands
from dakcoder_agent.tools.control import PlanStep
from dakcoder_agent.tools.router import Router
from dakcoder_shared import cancel
from dakcoder_shared.envelope import ToolResult
from dakcoder_shared.llm import RequestCancelled, _consume_stream
from dakcoder_shared.paths import Workspace

from scripted import (  # noqa: E402 - shared scripted model
    build,
    calls,
    patch,
    plan_call,
    say,
)
from scripted import gated, planning_router, written  # noqa: F401,E402

READS = frozenset({"read_file", "search_repo", "repo_map", "handler_map", "code_graph"})


def _read() -> object:
    return calls(("read_file", json.dumps({"path": "handler/user.go"})))


def _follow_up(previous, turns, *, kind="change"):
    nxt, client = build(previous.router, turns, kind=kind, max_turns=8)
    nxt.context = previous.context
    nxt.carry_from(previous)
    return nxt, client


# ── F3: a turn made to edit is not offered the read it just made ─────────────


def test_an_edit_force_offers_no_read_and_then_names_the_tool(planning_router, written) -> None:
    """3baf69127eaf turns 19-22, 9ae9b5925046 47-55, b593cd7ae46e 52-56 and 77-80:
    `required` over the whole list, answered with the last read each time."""
    loop, _ = build(planning_router, [])
    loop.state.mode = Mode.AGENT
    loop.state.plan = (PlanStep(file="handler/user.go", action="add Routes", accepts="go_build"),)
    tools = loop._tools()

    offered, choice = loop._edit_request(tools)
    names = {t["function"]["name"] for t in offered}
    assert choice == "required"
    assert names <= _EDIT_TOOLS, names
    assert not names & READS, "a read survived the narrowing"
    assert {"patch_file", "write_file", "finish"} <= names, "the edits and the exit stay"

    # Nothing edited since the first force: the second names the tool, and
    # the file exists, so it is `patch_file`.
    _, second = loop._edit_request(tools)
    assert second == {"type": "function", "function": {"name": "patch_file"}}

    # A target that does not exist yet is named `write_file`.
    loop.state.plan = (PlanStep(file="handler/new.go", action="create it", accepts="go_build"),)
    _, third = loop._edit_request(tools)
    assert third == {"type": "function", "function": {"name": "write_file"}}


def test_a_stalled_run_that_keeps_reading_is_forced_to_edit(planning_router, gated, written) -> None:
    """The scripted model answers every turn after the plan with the same read.
    Under the old code that was the whole run; now the forced turn cannot read."""
    loop, client = build(planning_router, [plan_call()] + [_read() for _ in range(12)], max_turns=20)
    list(loop.run("add Routes", intent=Intent.AGENT))

    forced = [
        i for i, choice in enumerate(client.tool_choices)
        if choice == "required" or isinstance(choice, dict)
    ]
    assert forced, "the stall never forced a turn"
    first = client.seen_tools[forced[0]]
    assert not set(first) & READS, f"the forced turn still offered a read: {first}"
    assert "patch_file" in first and "write_file" in first
    assert "handler/user.go" in loop.router.touched, "the force did not produce an edit"
    assert loop.result is not None
    assert "read_file was asked" not in loop.result.summary, loop.result.summary


# ── F1: the baseline is the task's, not the message's ────────────────────────


def test_a_follow_up_keeps_the_baseline_the_task_started_with(
    planning_router, gated, written, monkeypatch
) -> None:
    """b593cd7ae46e: message four broke a file, message five's gate called the
    errors "already present before this run changed anything"."""
    import dakcoder_agent.loop as loop_module

    taken: list[int] = []
    real = loop_module.take_baseline

    def counting(router, **kw):
        taken.append(1)
        return real(router, **kw)

    monkeypatch.setattr(loop_module, "take_baseline", counting)

    first, _ = build(planning_router, [plan_call(), say("later")], max_turns=6)
    list(first.run("add Routes", intent=Intent.AGENT))
    first._await_baseline()
    assert first.result is not None and first.result.outcome is Outcome.NO_PROGRESS
    assert len(taken) == 1 and first.state.baseline.taken

    nxt, _ = _follow_up(first, [patch(), say("done")])
    list(nxt.run("continue", continued=True))
    nxt._await_baseline()
    assert len(taken) == 1, "the follow-up took a second baseline of a workspace the session had edited"
    assert nxt.state.baseline is first.state.baseline


# ── F1/F2 in the gate ────────────────────────────────────────────────────────


def _gate_stub(router: Router, answers: dict[str, ToolResult]) -> list[str]:
    from dakcoder_agent.gate import GATE

    order: list[str] = []
    for name in {stage.tool for stage in GATE} | {"gofmt", "rules_lint", "go_diagnostics"}:

        def handler(inv, _name=name):
            order.append(_name)
            if _name in answers:
                return answers[_name]
            meta = {"violations": 0, "files_scanned": 1} if _name == "rules_lint" else {}
            return ToolResult.success(f"{_name}: clean", meta=meta)

        router.handlers[name] = handler
    return order


def _red_baseline(output: str) -> Baseline:
    return Baseline(
        findings={"go_build": _finding_keys(output)}, passed={"go_build": False}, taken=True
    )


def test_a_compile_error_in_a_file_this_run_edited_is_charged_whatever_the_baseline_says(
    router: Router,
) -> None:
    pre = "# pisapi/handler\nhandler/user.go:4:2: undefined: x\n"
    _gate_stub(router, {"go_build": ToolResult.failure(pre)})

    report = full_gate(router, ["handler/user.go"], baseline=_red_baseline(pre))

    assert report.blocked_by is not None and report.blocked_by.name == "go_build"
    assert "undefined: x" in report.blocked_by.content


def test_vet_and_test_are_not_run_on_packages_that_do_not_compile(
    router: Router, workspace: Workspace
) -> None:
    """9ae9b5925046 and b593cd7ae46e: 72 seconds of go vet and go test on a
    package go_build had already said does not compile."""
    (workspace.root / "handler" / "user_test.go").write_text("package handler\n", encoding="utf-8")
    pre = "# pisapi/handler\nhandler/other.go:4:2: undefined: x\n"
    order = _gate_stub(router, {"go_build": ToolResult.failure(pre)})

    report = full_gate(router, ["handler/user.go"], baseline=_red_baseline(pre))
    by = {r.name: r for r in report.results}

    assert not by["go_build"].blocking, "a pre-existing error in an untouched file was charged"
    assert by["go_vet"].skipped.startswith("not run")
    assert by["go_test"].skipped.startswith("not run")
    assert "go_vet" not in order and "go_test" not in order


def test_a_test_stage_that_only_reproduces_the_excused_build_errors_is_advisory(
    router: Router, workspace: Workspace
) -> None:
    """The contradiction: go_build "advisory, not yours to fix" and go_test
    "FAIL" about the same compiler errors, in one report."""
    (workspace.root / "handler" / "user_test.go").write_text("package handler\n", encoding="utf-8")
    pre = "# pisapi/repo/postgres\nrepo/postgres/other.go:4:2: undefined: x\n"
    test = pre + "FAIL\tpisapi/handler [build failed]\n"
    _gate_stub(router, {"go_build": ToolResult.failure(pre), "go_test": ToolResult.failure(test)})

    report = full_gate(router, ["handler/user.go"], baseline=_red_baseline(pre))
    by = {r.name: r for r in report.results}

    assert not by["go_build"].blocking
    assert not by["go_test"].skipped, "a package that is not red still gets its tests run"
    assert not by["go_test"].blocking, by["go_test"].content
    assert "go_build reported above" in by["go_test"].content


def test_a_real_test_failure_still_blocks_beside_an_excused_build(
    router: Router, workspace: Workspace
) -> None:
    (workspace.root / "handler" / "user_test.go").write_text("package handler\n", encoding="utf-8")
    pre = "# pisapi/repo/postgres\nrepo/postgres/other.go:4:2: undefined: x\n"
    test = "--- FAIL: TestRoutes (0.00s)\n    handler/user_test.go:9: expected 2 routes, got 1\nFAIL\n"
    _gate_stub(router, {"go_build": ToolResult.failure(pre), "go_test": ToolResult.failure(test)})

    report = full_gate(router, ["handler/user.go"], baseline=_red_baseline(pre))
    by = {r.name: r for r in report.results}

    assert not by["go_build"].blocking
    assert by["go_test"].blocked, "a test failure was excused as a build error"


# ── F4: a question asked while work is open is answered ──────────────────────


def test_reads_as_question() -> None:
    assert _reads_as_question("did you fix them all?")
    assert _reads_as_question("explain the employee handler")
    assert _reads_as_question("why is it that you are calling go build again and again")
    assert not _reads_as_question("now start step 2")
    assert not _reads_as_question("add the repo layer too")
    assert not _reads_as_question("")


def test_a_question_asked_while_work_is_open_is_answered_and_the_plan_kept(
    planning_router, gated, written
) -> None:
    """9ae9b5925046 turn 56, b593cd7ae46e turns 58 and 81: three questions
    routed straight back into the acting loop and never answered."""
    first, _ = build(planning_router, [plan_call(), say("later")], max_turns=6)
    list(first.run("add Routes", intent=Intent.AGENT))
    assert any(step.open for step in first.state.plan)

    asked, client = _follow_up(
        first, [say("Not yet: handler/user.go is still open.")], kind="question"
    )
    list(asked.run("did you fix them all?", continued=True))

    assert asked.state.intent is Intent.ASK
    assert asked.state.intent_source == "classified"
    assert client.classifications == 1
    assert asked.state.mode is Mode.ASK
    assert asked.result is not None and asked.result.outcome is Outcome.DONE
    assert any(step.open for step in asked.state.plan), "a question dropped the plan"

    # And an imperative follow-up still continues the plan without asking.
    again, client2 = _follow_up(asked, [patch(), say("done")], kind="question")
    list(again.run("now write it", continued=True))
    assert again.state.intent is Intent.AGENT
    assert again.state.intent_source == "session"
    assert client2.classifications == 0


# ── F5: a Planner made to stop, naming the work left, is not done ────────────


_PATTERNS = [
    "package domain", "package postgres", "package handler", "package request",
    "package bootstrap", "package main", "GetAll", "GetByID", "Routes",
    "CreateUserRequest", "FxRepo", "FirstName", "serial4", "owns SQL",
]


def test_a_planner_made_to_stop_that_names_the_work_left_is_not_done(planning_router, gated) -> None:
    """9ae9b5925046 turn 13: "done: answered; blocked on: Need to create
    handler..." on a request to complete and fix an API."""
    fence = [
        calls(("search_repo", json.dumps({"pattern": p}))) for p in _PATTERNS[:MAX_RESEARCH_TURNS]
    ]
    finish = calls((
        "finish",
        json.dumps({
            "answer": "I could not get to a plan.",
            "blocked": "Need to create handler/employee.go and wire it",
        }),
    ))
    loop, _ = build(planning_router, fence + [finish], max_turns=MAX_RESEARCH_TURNS + 4)
    list(loop.run("complete the employee API and fix all issues", intent=Intent.AGENT))

    assert loop.result is not None
    assert loop.result.outcome is Outcome.NO_PROGRESS, loop.result.summary
    assert "without a plan" in loop.result.summary
    assert "handler/employee.go" in loop.result.summary


# ── F6: the gate's verdict travels with the plan ─────────────────────────────


def test_a_follow_up_remembers_the_gate_verdict(planning_router, gated, written) -> None:
    gated["fail"] = "go_vet"
    first, _ = build(planning_router, [plan_call(), patch(), say("done")], max_turns=8)
    list(first.run("add Routes", intent=Intent.AGENT))
    assert first.state.last_gate is not None and not first.state.last_gate.ok

    nxt, _ = _follow_up(first, [])
    assert nxt.state.last_gate is first.state.last_gate
    assert nxt.state.gate_key == first.state.gate_key
    assert nxt.state.gate_turn == first.state.gate_turn
    assert nxt.state.gate_failures == 0, "the push-back budget is per message"


# ── F7: a stalled run with edits gets one gate ───────────────────────────────


def test_a_stalled_run_with_edits_gets_one_gate_before_it_ends(planning_router, gated, written) -> None:
    """b593cd7ae46e turn 80: three files changed, "the gate has not run on them yet"."""
    loop, _ = build(planning_router, [plan_call(), patch()], max_turns=2)
    list(loop.run("add Routes", intent=Intent.AGENT))
    assert loop.router.touched and loop.state.last_gate is None

    result = loop._stalled()

    assert result.gate is not None and result.gate is loop.state.last_gate
    assert result.gate.ok
    assert "The gate is clean" in result.summary
    assert result.outcome in (Outcome.DONE, Outcome.NO_PROGRESS)


# ── F8: unattended approvals ─────────────────────────────────────────────────


async def test_auto_safe_is_refused_on_a_local_runtime(tmp_path: Path) -> None:
    runtime = Loopback(tmp_path, lambda _session, _approve: None, token="tok")
    transport = httpx.ASGITransport(app=create_app(runtime))
    async with httpx.AsyncClient(
        transport=transport, base_url="http://127.0.0.1", headers={"Authorization": "Bearer tok"}
    ) as http:
        refused = await http.post(
            "/v1/tasks", json={"task": "unattended", "approval_policy": "auto_safe"}
        )
    assert refused.status_code == 403, refused.text


# ── F9: a Stop reaches the subprocess and the stream ─────────────────────────


def test_a_stopped_run_kills_its_subprocess_within_seconds(tmp_path: Path) -> None:
    token = cancel.CANCELLED.set(lambda: True)
    started = time.monotonic()
    try:
        done = commands.run(
            [sys.executable, "-c", "import time; time.sleep(30)"], tmp_path, timeout=60
        )
    finally:
        cancel.CANCELLED.reset(token)
    assert done.cancelled and not done.ok and not done.timed_out
    assert time.monotonic() - started < 15


def test_a_stopped_run_closes_the_model_stream() -> None:
    token = cancel.CANCELLED.set(lambda: True)
    try:
        with pytest.raises(RequestCancelled):
            _consume_stream(iter(['data: {"choices":[{"delta":{"content":"x"}}]}']))
    finally:
        cancel.CANCELLED.reset(token)


def test_without_a_signal_nothing_changes() -> None:
    assert cancel.CANCELLED.get() is None
    assert not cancel.cancelled()


# ── F11: deleting what is not there ──────────────────────────────────────────


def test_deleting_an_absent_file_is_a_dead_end_and_asks_nobody(router: Router) -> None:
    out = router.dispatch("delete_file", {"path": "handler/gone.go", "reason": "cleanup"})
    assert isinstance(out, ToolResult), "an absent path raised an approval card"
    assert not out.ok
    assert out.meta.get("dead_end")
    assert out.mutations == ()


# ── F12 / F13: the metrics record ────────────────────────────────────────────


def test_metrics_know_the_window_and_count_unreported_usage() -> None:
    acc = Accumulator("s")
    acc.feed({"type": "usage", "data": {"prompt_tokens": 1200, "completion_tokens": 10}})
    acc.feed({"type": "usage", "data": {"prompt_tokens": 0, "completion_tokens": 0}})
    record = acc.finish(context_window=262144)

    assert record.prompt_tokens == [1200]
    assert record.unreported_turns == 1
    assert record.context_window == 262144
    assert not any("window was not recorded" in note for note in record.incomplete)
    assert any("reported no usage" in note for note in record.incomplete)


# ── F14: the debug state shows a baseline field by field ─────────────────────


def test_the_debug_state_renders_a_nested_dataclass() -> None:
    group = GateState()
    group.baseline = Baseline(
        findings={"go_build": frozenset({"handler/user.go|undefined: x"})},
        passed={"go_build": False},
        taken=True,
    )
    shown = _describe(group)
    assert shown["baseline"]["taken"] is True
    assert shown["baseline"]["findings"] == {"n": 1, "keys": ["go_build"]}
