"""Session dc45499ea819: a red build nothing in the loop could see.

Three defects compounded into one loop. The model sent the same reply and the
same `go build ./...` from turn 83 to turn 90, and the run ended `no_progress`
with the compiler errors in the turn above every one of those replies:

1. A forced turn at 24 came back padded, was salvaged whole, and wrote its
   file -- and `_note_constraint` read the padding flag alone, so every forced
   turn for the rest of the run went out unforced.
2. Every plan step reached `done` on the inner loop's formatter and linter,
   though each one's criterion was `go_build`, and the builds the model ran
   itself were read by nothing.
3. So the stall escape found nothing outstanding and said "give the developer
   what you have", to a run whose code did not compile.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

from dakcoder_agent.gate import GateReport, build_errors
from dakcoder_agent.loop import AgentLoop, Intent, _State, _build_scope
from dakcoder_agent.loopstate import BuildVerdict
from dakcoder_agent.modes import Mode
from dakcoder_agent.tools.control import PlanStep
from dakcoder_agent.tools.router import Router
from dakcoder_shared.envelope import ToolResult
from dakcoder_shared.llm import ChatResult, ToolCall, Usage

from scripted import build, calls, plan_call

# Fixtures defined in `scripted` are re-exported here so pytest collects them.
from scripted import gated, planning_router, written  # noqa: F401,E402


# ── 1. a salvaged constrained reply is the constraint working ───────────────


def test_a_forced_call_salvaged_from_padding_does_not_switch_forcing_off() -> None:
    from dakcoder_agent.context import ContextManager

    loop = AgentLoop.__new__(AgentLoop)
    loop.state = _State()
    loop.context = ContextManager(mode=Mode.AGENT, system_prompt="s")

    # What `_stream` hands back when the call was whole under the whitespace:
    # padding seen, call trimmed and closed, `finish_reason` "tool_calls".
    salvaged = ChatResult(
        tool_calls=[
            ToolCall(
                id="c1",
                name="write_file",
                arguments=json.dumps({"path": "core/domain/employee.go", "content": "package domain\n"}),
            )
        ],
        finish_reason="tool_calls",
        usage=Usage(),
        degenerate=True,
    )
    loop._note_constraint("required", SimpleNamespace(chat=salvaged))
    assert not loop.state.constraint_failed, "the call landed; forcing still works"

    # And the salvage that failed: the client gives up and reports it as cut off.
    lost = ChatResult(
        tool_calls=[ToolCall(id="c2", name="write_file", arguments='{"path": "a.go", "content": "pa')],
        finish_reason="length",
        usage=Usage(),
        degenerate=True,
    )
    loop._note_constraint("required", SimpleNamespace(chat=lost))
    assert loop.state.constraint_failed


# ── the parser ──────────────────────────────────────────────────────────────


def test_build_errors_reads_windows_paths_and_keeps_the_first_error_per_file() -> None:
    output = (
        "# gotemplate/handler\n"
        "handler\\employee.go:153:30: undefined: port.FetchSuccess\n"
        "handler\\employee.go:211:30: undefined: port.FetchSuccess\n"
        "repo/postgres/employee.go:469: assignment mismatch: 2 variables\n"
        "handler\\employee.go:435:25: too many errors\n"
    )
    assert build_errors(output) == (
        ("handler/employee.go", "undefined: port.FetchSuccess"),
        ("repo/postgres/employee.go", "assignment mismatch: 2 variables"),
    )


def test_build_errors_excuses_what_the_baseline_recorded_and_relativises_absolute_paths() -> None:
    old = "handler\\paogen.go:12:2: declared and not used: x"
    output = f"{old}\nD:\\ws\\svc\\main.go:5:2: undefined: y\ngo: downloading example.test v1\n"
    # `_line_key`'s shape, which is what `take_baseline` stores.
    excused = frozenset({"handler\\paogen.go|declared and not used: x"})
    assert build_errors(output, excused=excused, root="D:\\ws\\svc") == (
        ("main.go", "undefined: y"),
    )
    assert build_errors("go: module lookup disabled by GOPROXY=off\n") == ()


def test_only_a_build_of_the_whole_module_answers_for_it() -> None:
    assert _build_scope("go_build", None) == "module"
    assert _build_scope("run_terminal", ["go", "build", "./..."]) == "module"
    assert _build_scope("run_terminal", ["go.exe", "build", "./handler/..."]) == "package"
    assert _build_scope("run_terminal", ["go", "build"]) == "package"
    assert _build_scope("run_terminal", ["go", "vet", "./..."]) == ""
    assert _build_scope("read_file", None) == ""


# ── 2. a failing build holds the steps it names ─────────────────────────────


def _acting(router: Router) -> AgentLoop:
    loop, _client = build(router, [])
    loop.state.mode = Mode.AGENT
    loop.state.plan = (
        PlanStep(file="handler/user.go", action="add Routes", accepts="go_build", status="done"),
        PlanStep(file="bootstrap/bootstrapper.go", action="wire it", accepts="go_build", status="done"),
    )
    return loop


def _failed_build(body: str, argv=("go", "build", "./...")) -> ToolResult:
    return ToolResult.failure(body, meta={"argv": list(argv), "code": 1})


def test_a_failing_build_puts_the_step_it_names_back_to_written(planning_router: Router) -> None:
    loop = _acting(planning_router)
    call = ToolCall(id="b1", name="go_build", arguments="{}")
    loop._note_build(call, _failed_build("handler\\user.go:3:2: undefined: port.FetchSuccess\n"))

    held, untouched = loop.state.plan
    assert held.status == "written"
    assert held.note.startswith("go build fails on it")
    assert "undefined: port.FetchSuccess" in held.note
    assert untouched.status == "done", "a step the build did not name is not held"
    assert loop._outstanding() == ["handler/user.go"]
    assert loop.state.last_build is not None and loop._build_wants_an_edit()

    # A clean format and lint of the file does not release it: that is not a build.
    loop._verify_written(GateReport())
    assert loop.state.plan[0].status == "written"
    # And `finish` is not refused over it -- the gate it runs builds `./...`.
    assert "formatter" not in loop._why_not_done()

    # A package build passing is not the module building.
    loop._note_build(
        ToolCall(id="b2", name="run_terminal", arguments="{}"),
        ToolResult.success("ok", meta={"argv": ["go", "build", "./bootstrap/..."]}),
    )
    assert loop.state.plan[0].status == "written"

    loop._note_build(ToolCall(id="b3", name="go_build", arguments="{}"), ToolResult.success("go build ./...: clean", meta={"argv": ["go", "build", "./..."]}))
    assert [s.status for s in loop.state.plan] == ["done", "done"]
    assert loop.state.plan[0].note == ""
    assert loop.state.last_build is None


def test_a_build_failure_that_names_no_file_or_times_out_holds_nothing(planning_router: Router) -> None:
    loop = _acting(planning_router)
    call = ToolCall(id="b1", name="go_build", arguments="{}")
    loop._note_build(call, _failed_build("go: gitlab.cept.gov.in/x: unrecognized import path\n"))
    loop._note_build(
        call, ToolResult.failure("go build ./... did not finish", meta={"argv": ["go"], "timeout": True})
    )
    assert loop.state.last_build is None
    assert [s.status for s in loop.state.plan] == ["done", "done"]


def test_mid_migration_a_red_build_is_the_design_and_is_not_recorded(planning_router: Router) -> None:
    loop = _acting(planning_router)
    loop.state.migration = SimpleNamespace(defers_gate=True, active=False)
    loop._note_build(
        ToolCall(id="b1", name="go_build", arguments="{}"),
        _failed_build("handler/user.go:3:2: undefined: gin\n"),
    )
    assert loop.state.last_build is None
    assert loop.state.plan[0].status == "done"


def test_a_step_the_linter_holds_keeps_the_linters_note(planning_router: Router) -> None:
    loop = _acting(planning_router)
    loop.state.plan = (
        PlanStep(
            file="handler/user.go",
            action="add Routes",
            accepts="go_build",
            status="written",
            note="written; rules_lint is not clean on it yet",
        ),
    )
    loop._note_build(
        ToolCall(id="b1", name="go_build", arguments="{}"),
        _failed_build("handler/user.go:3:2: undefined: x\n"),
    )
    assert loop.state.plan[0].note == "written; rules_lint is not clean on it yet"


def test_the_state_block_says_the_build_fails_and_whether_anything_changed_since(
    planning_router: Router,
) -> None:
    loop = _acting(planning_router)
    loop._note_build(
        ToolCall(id="b1", name="go_build", arguments="{}"),
        _failed_build("handler\\user.go:3:2: undefined: port.FetchSuccess\n"),
    )
    block = loop._state_block()
    assert "Last build: FAIL" in block
    assert "handler/user.go: undefined: port.FetchSuccess" in block
    assert "nothing edited since" in block
    assert "all 2 step(s) settled" not in block
    assert "[written]" in block, "the cursor is back on the step the build named"


def test_a_red_build_outside_the_plan_still_says_the_work_is_not_done(planning_router: Router) -> None:
    loop = _acting(planning_router)
    loop._note_build(
        ToolCall(id="b1", name="go_build", arguments="{}"),
        _failed_build("main.go:9:2: undefined: bootstrap.FxEmployee\n"),
    )
    assert [s.status for s in loop.state.plan] == ["done", "done"]
    block = loop._state_block()
    assert "the last build fails, so the work is not done" in block
    assert "settled" not in block


def test_a_follow_up_carries_the_build_that_holds_its_plan(planning_router: Router) -> None:
    first = _acting(planning_router)
    first._note_build(
        ToolCall(id="b1", name="go_build", arguments="{}"),
        _failed_build("handler/user.go:3:2: undefined: x\n"),
    )
    second, _client = build(planning_router, [])
    second.carry_from(first)
    assert second.state.plan[0].status == "written"
    assert second.state.last_build == first.state.last_build


# ── 3. the whole loop: the stall points at the edit ─────────────────────────


def _go_build_reads_the_file(router: Router) -> None:
    """`go_build` that fails while the handler holds the broken line.

    Clean before the first edit, so the baseline has nothing to excuse.
    """

    def handler(inv):
        body = (router.workspace.root / "handler" / "user.go").read_text(encoding="utf-8")
        if "port.FetchSuccess" in body:
            return _failed_build("# example.test/handler\nhandler\\user.go:1:30: undefined: port.FetchSuccess\n")
        return ToolResult.success("go build ./...: clean", meta={"argv": ["go", "build", "./..."]})

    router.handlers["go_build"] = handler


def _patch(old: str, new: str) -> ChatResult:
    return calls(("patch_file", json.dumps({"path": "handler/user.go", "old": old, "new": new})))


def test_a_stall_on_a_red_build_is_forced_at_the_edit_not_at_the_exit(
    planning_router: Router, gated, written
) -> None:
    _go_build_reads_the_file(planning_router)
    loop, client = build(
        planning_router,
        [
            plan_call(),
            _patch("package handler", "package handler // port.FetchSuccess"),
            calls(("go_build", "{}")),
            # The field run: the same build, answered from the cache each time.
            calls(("go_build", "{}")),
            calls(("go_build", "{}")),
            # The forced turn. Scripted as the fix; the stub only lets it through
            # if the request still offers `patch_file`.
            _patch("package handler // port.FetchSuccess", "package handler // fixed"),
            calls(("go_build", "{}")),
            calls(("finish", json.dumps({"answer": "Added Routes."}))),
        ],
        max_turns=12,
    )
    list(loop.run("add Routes", intent=Intent.AGENT))

    forced = [i for i, choice in enumerate(client.tool_choices) if choice == "required"]
    assert forced, "the stall forced a call"
    assert "patch_file" in client.seen_tools[forced[0]], "and left the edit on the table"

    said = "\n".join(m.content for m in loop.context.build())
    assert "go build fails on it" in said
    assert "Edit it now" in said
    assert "Give the developer what you have established now" not in said

    assert loop.state.plan[0].status == "done", "the clean build released the step"
    assert loop.state.last_build is None
    assert loop.result.outcome == "done", loop.result.summary


def test_a_stall_on_a_build_broken_outside_the_plan_is_asked_for_the_fix(
    planning_router: Router, gated, written
) -> None:
    def handler(inv):
        # Broken by the edit, not before it: a build red on arrival is the
        # baseline's to excuse, and correctly holds nothing.
        body = (planning_router.workspace.root / "handler" / "user.go").read_text(encoding="utf-8")
        if "// x" in body:
            return _failed_build("main.go:9:2: undefined: bootstrap.FxEmployee\n")
        return ToolResult.success("go build ./...: clean", meta={"argv": ["go", "build", "./..."]})

    planning_router.handlers["go_build"] = handler
    loop, client = build(
        planning_router,
        [
            plan_call(),
            _patch("package handler", "package handler // x"),
            calls(("go_build", "{}")),
            calls(("go_build", "{}")),
            calls(("go_build", "{}")),
        ],
        max_turns=7,
    )
    list(loop.run("add Routes", intent=Intent.AGENT))

    said = "\n".join(m.content for m in loop.context.build())
    assert "The last `go build`" in said and "main.go: undefined: bootstrap.FxEmployee" in said
    assert "Give the developer what you have established now" not in said
    forced = [i for i, choice in enumerate(client.tool_choices) if choice == "required"]
    assert forced and "patch_file" in client.seen_tools[forced[0]]


def test_a_build_verdict_names_its_first_error_with_the_file() -> None:
    verdict = BuildVerdict(
        tool="go_build", turn=82, mutations=3, errors=(("handler/employee.go", "undefined: x"),)
    )
    assert verdict.first == "handler/employee.go: undefined: x"
    assert verdict.paths == ("handler/employee.go",)
