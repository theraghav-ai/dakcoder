"""Tests for the verification gate.

The gate's value is entirely in its ordering and its scoping, so that is what
these check: that a failure stops the sequence, that the expensive stage runs
last, and that both scoped stages get the mutation list rather than the whole
tree.
"""

from __future__ import annotations

import json

import pytest

from dakcoder_agent.gate import GATE, _NO_MODULE, full_gate, inner_loop
from dakcoder_agent.tools.router import Router
from dakcoder_shared.envelope import ToolResult
from dakcoder_shared.paths import Workspace


class Recorder:
    """Registers scripted handlers and records what the gate asked of each."""

    def __init__(self, router: Router) -> None:
        self.router = router
        self.calls: list[tuple[str, dict]] = []
        self.answers: dict[str, ToolResult] = {}
        for name in {stage.tool for stage in GATE} | {"gofmt", "rules_lint", "go_diagnostics"}:
            self._install(name)

    def _install(self, name: str) -> None:
        def handler(inv, _name=name):
            self.calls.append((_name, dict(inv.arguments)))
            return self.answers.get(_name, ToolResult.success(f"{_name}: clean"))

        self.router.handlers[name] = handler

    def fails(self, name: str, content: str = "boom") -> None:
        self.answers[name] = ToolResult.failure(content)

    def answer(self, name: str, result: ToolResult) -> None:
        self.answers[name] = result

    @property
    def order(self) -> list[str]:
        return [name for name, _ in self.calls]


@pytest.fixture
def gate(router: Router) -> Recorder:
    return Recorder(router)


# ── ordering ────────────────────────────────────────────────────────────────


def test_the_gate_runs_in_the_specified_order(gate: Recorder, router: Router) -> None:
    full_gate(router, ["handler/user.go"])
    assert gate.order[:4] == ["go_build", "govalid_gen", "go_build", "rules_lint"]


def test_go_vet_runs_after_the_cheap_stages(gate: Recorder, router: Router) -> None:
    """It was measured at about 32 seconds — more than go build — and nothing
    downstream depends on it, so it costs nothing to move it late and costs half
    a minute per failing run to leave it early."""
    full_gate(router, ["handler/user.go"])
    assert gate.order.index("go_vet") > gate.order.index("rules_lint")


def test_a_failing_build_stops_everything_after_it(gate: Recorder, router: Router) -> None:
    """go build is authoritative. Running go vet over code that does not compile
    produces pages of errors that are all consequences of the one error the model
    already has to fix."""
    gate.fails("go_build", "handler/user.go:12: undefined: Pension")
    report = full_gate(router, ["handler/user.go"])

    assert not report.ok
    assert gate.order == ["go_build"]
    assert "go_vet" in report.not_run
    assert report.blocked_by.name == "go_build"


def test_validators_are_regenerated_then_the_build_is_rechecked(
    gate: Recorder, router: Router
) -> None:
    """Regenerating against a renamed field breaks the build, and that break is
    the signal — not an accident to be tolerated."""
    assert gate.order == []
    full_gate(router, ["handler/request.go"])
    assert gate.order.count("go_build") == 2
    assert gate.order.index("govalid_gen") < gate.order.index("go_build", 1)


def test_a_non_blocking_failure_does_not_stop_the_gate(gate: Recorder, router: Router) -> None:
    """golangci-lint is advisory by design. A style finding that blocks a
    correct, contract-compliant change is a tax on every task."""
    gate.fails("golangci_lint", "unused variable")
    report = full_gate(router, ["handler/user.go"])
    assert report.ok
    assert [w.name for w in report.warnings] == ["golangci_lint"]


# ── scoping ─────────────────────────────────────────────────────────────────


def test_both_scoped_stages_get_only_the_touched_files(gate: Recorder, router: Router) -> None:
    """Part A section 9.3: every file in the reference template fails an
    unscoped gofmt -l because they all use CRLF, so an unscoped format touches
    files the agent never went near."""
    inner_loop(router, ["handler/user.go", "core/domain/user.go"])
    scoped = dict(gate.calls)
    assert scoped["gofmt"]["paths"] == "handler/user.go,core/domain/user.go"
    assert scoped["rules_lint"]["paths"] == "handler/user.go,core/domain/user.go"


def test_non_go_files_are_not_sent_to_gofmt(gate: Recorder, router: Router) -> None:
    inner_loop(router, ["db/pensions.sql", "handler/user.go"])
    assert dict(gate.calls)["gofmt"]["paths"] == "handler/user.go"


def test_nothing_touched_means_the_scoped_stages_are_skipped(
    gate: Recorder, router: Router
) -> None:
    report = inner_loop(router, [])
    assert gate.order == []
    assert all(r.skipped for r in report.results if r.name in ("gofmt", "rules_lint"))


# ── what counts as passing ──────────────────────────────────────────────────


def test_lint_findings_block_even_though_the_tool_succeeded(
    gate: Recorder, router: Router
) -> None:
    """rules_lint returns ok=True with a body describing violations — a finding
    is the tool working. The gate has to read the body, not the verdict."""
    gate.answer(
        "rules_lint",
        ToolResult.success(json.dumps({"ok": False, "count": 2, "violations": [{}, {}]})),
    )
    report = full_gate(router, ["handler/user.go"])
    assert not report.ok
    assert report.blocked_by.name == "rules_lint"


def test_pre_existing_violations_in_untouched_files_do_not_block(
    gate: Recorder, router: Router
) -> None:
    """Blocking on out_of_scope_count would make the first change to any legacy
    service impossible — which is precisely the codebase this agent exists for."""
    gate.answer(
        "rules_lint",
        ToolResult.success(json.dumps({"ok": True, "count": 0, "out_of_scope_count": 47})),
    )
    assert full_gate(router, ["handler/user.go"]).ok


def test_a_tidy_that_changes_go_mod_blocks(gate: Recorder, router: Router) -> None:
    """At the gate, tidy must be a no-op. A diff means the dependency set
    drifted, which is a review decision rather than something to fix in place."""
    gate.answer(
        "go_mod",
        ToolResult.success("go mod tidy changed go.mod", meta={"changed": True}),
    )
    report = full_gate(router, ["handler/user.go"])
    assert not report.ok
    assert report.blocked_by.name == "go_mod tidy"


def test_a_clean_tidy_passes(gate: Recorder, router: Router) -> None:
    gate.answer("go_mod", ToolResult.success("no change", meta={"changed": False}))
    assert full_gate(router, ["handler/user.go"]).ok


# ── conditional stages ──────────────────────────────────────────────────────


def test_govulncheck_only_runs_when_dependencies_changed(
    gate: Recorder, router: Router
) -> None:
    full_gate(router, ["handler/user.go"])
    assert "govulncheck" not in gate.order

    gate.calls.clear()
    full_gate(router, ["handler/user.go"], dependencies_changed=True)
    assert "govulncheck" in gate.order


def test_tests_are_skipped_when_there_are_none(gate: Recorder, router: Router) -> None:
    report = full_gate(router, ["handler/user.go"])
    stage = next(r for r in report.results if r.name == "go_test")
    assert stage.skipped == "no test files"


def test_tests_run_when_they_exist(gate: Recorder, router: Router, workspace) -> None:
    (workspace.root / "handler" / "user_test.go").write_text("package handler\n")
    full_gate(router, ["handler/user.go"])
    assert "go_test" in gate.order


def test_the_unwired_gopls_stage_is_skipped_with_its_reason(
    gate: Recorder, router: Router
) -> None:
    del router.handlers["go_diagnostics"]
    report = inner_loop(router, ["handler/user.go"])
    stage = next(r for r in report.results if r.name == "go_diagnostics")
    assert stage.skipped == "gopls not wired"


# ── the report ──────────────────────────────────────────────────────────────


def test_the_summary_shows_every_stage_and_the_failure_in_full(
    gate: Recorder, router: Router
) -> None:
    """Passing stages are one line because their content is "clean"; the failing
    one is included whole because it is what the model has to act on."""
    gate.fails("go_vet", "handler/user.go:9: unreachable code")
    summary = full_gate(router, ["handler/user.go"]).summary()

    assert summary.startswith("gate: FAIL")
    assert "ok go_build" in summary
    assert "FAIL go_vet" in summary
    assert "unreachable code" in summary


def test_the_inner_loop_never_blocks(gate: Recorder, router: Router) -> None:
    """Its findings are information for the next turn, not a verdict on this
    one. Blocking here would stop the model mid-edit on a file it has not
    finished writing."""
    gate.fails("rules_lint", "layer-sql-boundary")
    report = inner_loop(router, ["repo/postgres/user.go"])
    assert report.ok
    assert report.warnings


def test_the_go_stages_skip_without_a_module_at_the_root(tmp_path, sidecar) -> None:
    """Every Go stage runs ``./...``, which needs a ``go.mod`` at the working
    directory. Without one the toolchain refuses the pattern itself, and the
    loop reads that as a defect in code it never compiled.

    Guarding only ``go_build`` would promote ``swagger_check`` to first blocker
    and replay the same unclearable ladder, so both scoped stages are asserted
    here by name.
    """
    from dakcoder_agent.tools import commands, fs, gotools, knowledge

    (tmp_path / "handler").mkdir()
    (tmp_path / "handler" / "user.go").write_text("package handler\n", encoding="utf-8")
    router = Router(
        Workspace.at(tmp_path),
        {**fs.HANDLERS, **knowledge.HANDLERS, **commands.HANDLERS, **gotools.handlers_for(sidecar)},
    )
    Recorder(router)

    report = full_gate(router, ["handler/user.go"])

    assert report.ok
    assert report.not_run == ()
    skipped = {r.name for r in report.results if r.skipped == _NO_MODULE}
    assert {"go_build", "go_vet", "swagger_check", "rules_lint", "go_mod tidy"} <= skipped
    # Nothing ran, so nothing may claim to have passed.
    assert all(r.skipped for r in report.results)



def test_swagger_check_is_scoped_to_what_the_run_touched(gate: Recorder, router: Router) -> None:
    """The stage that made a legacy service unfixable.

    It was the one lint stage in the gate that ran unscoped. The service in the
    field has eight handlers predating the contract, none with a `Routes()`
    method, so an unscoped check returned all eight as blocking — and a change
    touching two files failed on seven it had never opened. The Verifier
    reported them as pre-existing twice; the ladder sent it back to the Coder
    anyway, because a blocking stage blocks whatever the report says.

    `_swagger_check` already stated the principle for its config half — "block
    on what this run did, report what was already broken". It just was not
    applied to the half that blocks.
    """
    full_gate(router, ["handler/user.go", "repo/postgres/user.go"])

    args = dict(next(a for name, a in gate.calls if name == "swagger_check"))
    assert args.get("paths"), "swagger_check was handed the whole workspace"
    assert "handler/user.go" in args["paths"]


def test_swagger_check_sits_out_a_run_that_touched_no_go_files(
    gate: Recorder, router: Router
) -> None:
    """A run that changed nothing has nothing for a scoped stage to check, and
    saying so beats failing it on damage that was there beforehand."""
    gate.fails("swagger_check")
    report = full_gate(router, [])

    stage = next(r for r in report.results if r.name == "swagger_check")
    assert stage.skipped, "an empty scope still ran the check"
    assert "swagger_check" not in gate.order


def test_an_advisory_failure_is_reported_even_when_something_blocks(
    gate: Recorder, router: Router
) -> None:
    """`govalid_gen` failed on every attempt of every run in the field session
    and was never once diagnosed.

    It is non-blocking, so it never became `blocked_by`; and the summary carried
    advisory output only when nothing blocked, which on a legacy service is
    never. So it showed in the panel as a red mark with nothing behind it, and
    its output reached the model zero times. A stage that fails silently for a
    whole session is worse than one that does not run.
    """
    # The field ordering: `go_build` passes, the advisory stage fails, and a
    # later blocking stage is what stops the gate. Failing `go_build` instead
    # would stop before the advisory stage ever ran.
    gate.fails("govalid_gen", "cannot regenerate: renamed field Foo")
    gate.fails("swagger_check", "handler/user.go has no Routes() method")

    summary = full_gate(router, ["handler/user.go"]).summary()

    assert "no Routes() method" in summary, "the blocking failure must still lead"
    assert "cannot regenerate: renamed field Foo" in summary, (
        "the advisory failure was dropped because something else blocked"
    )
    assert "not blocking" in summary
