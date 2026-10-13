"""Tests for the verification gate.

The gate's value is entirely in its ordering and its scoping, so that is what
these check: that a failure stops the sequence, that the expensive stage runs
last, and that both scoped stages get the mutation list rather than the whole
tree.
"""

from __future__ import annotations

import json

import pytest

from dakcoder_agent.gate import (
    GATE,
    Baseline,
    _NO_MODULE,
    _finding_keys,
    full_gate,
    inner_loop,
    take_baseline,
)
from dakcoder_agent.tools.router import Router
from dakcoder_shared.envelope import ToolResult
from dakcoder_shared.paths import Workspace


class Recorder:
    """Registers scripted handlers and records what the gate asked of each."""

    def __init__(self, router: Router) -> None:
        self.router = router
        self.calls: list[tuple[str, dict]] = []
        self.answers: dict[str, ToolResult] = {}
        for name in {stage.tool for stage in GATE} | {"gofmt", "go_compile", "rules_lint", "go_diagnostics"}:
            self._install(name)

    #: What a passing stage returns, per tool.
    #:
    #: `rules_lint` carries `meta` because the real one does: `gotools._report`
    #: copies the sidecar's counts there beside the rendered prose, and
    #: `_stage_passed` reads them. A stub returning bare text was modelling a
    #: tool that does not exist -- and while `_lint_is_clean` fell back to
    #: `result.ok`, which is True whenever the sidecar ran at all, the
    #: difference was invisible: the blocking contract-lint stage could not fail
    #: on a finding either way. That is defect T1.
    CLEAN_META: dict[str, dict] = {
        "rules_lint": {"violations": 0, "out_of_scope": 0, "warnings": 0, "files_scanned": 1},
    }

    def _install(self, name: str) -> None:
        def handler(inv, _name=name):
            self.calls.append((_name, dict(inv.arguments)))
            return self.answers.get(
                _name,
                ToolResult.success(
                    f"{_name}: clean", meta=dict(self.CLEAN_META.get(_name, {}))
                ),
            )

        self.router.handlers[name] = handler

    def lint_finds(self, count: int, body: str) -> None:
        """A lint that ran and found something. Not a lint that broke."""
        self.answers["rules_lint"] = ToolResult.success(
            body, meta={"violations": count, "files_scanned": 1}
        )

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
    full_gate(router, ["handler/request/request.go"])
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

# -- the baseline ------------------------------------------------------------
#
# Scoping the compliance stage to touched files cured seven of the eight legacy
# handlers. The eighth is whichever one the task is about, so a vertical slice
# stayed unshippable: the gate blocked on damage the change did not cause, on
# every attempt, until the escalation budget ran out. These cover the other half.


def _configs(workspace: Workspace) -> None:
    """A configs/ directory, so the absent-config branch is not what blocks."""
    (workspace.root / "configs").mkdir(parents=True, exist_ok=True)
    (workspace.root / "configs" / "config.yaml").write_text("app: {}\n", encoding="utf-8")


def _violation(path: str, line: int = 12) -> dict:
    return {
        "rule": "routes-in-handler",
        "path": path,
        "line": line,
        "message": "handler declared here has no Routes() method",
        "fix": "add a Routes() []serverRoute.Route method",
    }


def _lint(sidecar, *violations: dict) -> None:
    sidecar.answer(
        "rules_lint",
        json.dumps(
            {"ok": not violations, "count": len(violations), "violations": list(violations)}
        ),
    )


def test_a_pre_existing_violation_blocks_when_nothing_has_been_recorded(
    router: Router, sidecar, workspace: Workspace
) -> None:
    """The baseline is taken from this: with no baseline, it blocks."""
    _configs(workspace)
    _lint(sidecar, _violation("handler/message.go"))
    out = router.run_gate_tool("swagger_check", {})
    assert not out.ok
    assert out.meta["violations"], "the keys the baseline is made of"


def test_a_pre_existing_violation_in_a_touched_file_does_not_block(
    router: Router, sidecar, workspace: Workspace
) -> None:
    """The defect this exists for.

    A full vertical slice *has* to edit a legacy handler, which puts that
    handler in scope and turns its pre-existing violation into this run's
    blocker -- permanently, because ``touched`` is append-only.
    """
    _configs(workspace)
    _lint(sidecar, _violation("handler/message.go"))

    baseline = router.run_gate_tool("swagger_check", {}).meta["violations"]
    after = router.run_gate_tool(
        "swagger_check", {"paths": "handler/message.go", "baseline": baseline}
    )

    assert after.ok, "the change was correct and could not be shipped"
    assert "already missing Routes()" in after.content
    assert after.meta["pre_existing"] == 1


def test_the_baseline_holds_after_the_edit_moves_the_line(
    router: Router, sidecar, workspace: Workspace
) -> None:
    """Inserting a function moves every violation below it.

    A key carrying a line number would stop matching at exactly the moment the
    run did the work it was asked to do.
    """
    _configs(workspace)
    _lint(sidecar, _violation("handler/message.go", line=12))
    baseline = router.run_gate_tool("swagger_check", {}).meta["violations"]

    _lint(sidecar, _violation("handler/message.go", line=97))
    after = router.run_gate_tool(
        "swagger_check", {"paths": "handler/message.go", "baseline": baseline}
    )
    assert after.ok


def test_a_violation_this_run_introduced_still_blocks(
    router: Router, sidecar, workspace: Workspace
) -> None:
    """The baseline excuses what was already there and nothing else."""
    _configs(workspace)
    _lint(sidecar, _violation("handler/message.go"))
    baseline = router.run_gate_tool("swagger_check", {}).meta["violations"]

    _lint(sidecar, _violation("handler/message.go"), _violation("handler/pension.go"))
    after = router.run_gate_tool(
        "swagger_check",
        {"paths": "handler/message.go,handler/pension.go", "baseline": baseline},
    )
    assert not after.ok, "a handler this run added without Routes() is this run to fix"
    assert "handler/pension.go" in after.content


def test_an_absent_paths_argument_scopes_to_nothing_rather_than_to_None(
    router: Router, sidecar, workspace: Workspace
) -> None:
    """The bug that kept `swagger_check`'s baseline empty for its whole life.

    `_list` turned a missing argument into `["None"]` via `str(None)`, so an
    *unscoped* `swagger_check` -- the call `take_baseline` makes, and the only
    one that can see what was already broken -- was silently scoped to a file
    named "None". It matched nothing, every real finding was reported
    out-of-scope, and the baseline came back with zero violations. Every legacy
    handler's missing `Routes()` then blocked the gate as though this change had
    caused it: precisely the failure the baseline exists to prevent, arriving
    through the baseline.
    """
    from dakcoder_agent.tools.gotools import _list

    assert _list(None) == []
    assert _list("") == []
    assert _list("handler/user.go") == ["handler/user.go"]
    assert _list(["a.go", "b.go"]) == ["a.go", "b.go"]

    _configs(workspace)
    _lint(sidecar, _violation("handler/message.go"))
    unscoped = router.run_gate_tool("swagger_check", {})
    assert unscoped.meta["violations"], "the unscoped baseline saw nothing"
    assert "None" not in json.dumps(sidecar.calls), "a path called 'None' reached the sidecar"


def test_the_gate_hands_the_baseline_to_the_stage(gate: Recorder, router: Router) -> None:
    """Threaded end to end, not merely accepted by the signature."""
    full_gate(
        router,
        ["handler/user.go"],
        baseline=Baseline(compliance=frozenset({"r|handler/user.go|m"}), taken=True),
    )
    args = dict(gate.calls)["swagger_check"]
    assert args["baseline"] == ["r|handler/user.go|m"]
    assert args["paths"] == "handler/user.go"


def test_a_stage_that_was_already_failing_is_advisory_not_blocking(
    gate: Recorder, router: Router
) -> None:
    """The single most expensive defect in the report, as a regression.

    `go_vet` was blocking, unscoped and unbaselined, so a pre-existing tab in a
    struct tag failed every run on the legacy corpus -- and the run was told the
    failure was its own, then spent two Coder attempts and three Debugger cycles
    trying to fix a file it had never opened.
    """
    finding = "core/domain/transferentry.go:19:2: struct field tag not compatible with reflect"
    gate.fails("go_vet", finding)
    baseline = Baseline(
        findings={"go_vet": _finding_keys(finding)}, passed={"go_vet": False}, taken=True
    )

    report = full_gate(router, ["handler/user.go"], baseline=baseline)

    vet = next(r for r in report.results if r.name == "go_vet")
    assert not vet.ok, "the finding is still reported"
    assert not vet.blocking, "but it is not this run's to fix"
    assert report.ok, "a run whose only failure predates it has passed"


def test_a_finding_this_run_added_still_blocks(gate: Recorder, router: Router) -> None:
    """The baseline excuses what was already there and nothing else."""
    was = "core/domain/transferentry.go:19:2: struct field tag not compatible"
    now = was + chr(10) + "handler/user.go:41:1: unreachable code"
    gate.fails("go_vet", now)
    baseline = Baseline(
        findings={"go_vet": _finding_keys(was)}, passed={"go_vet": False}, taken=True
    )

    report = full_gate(router, ["handler/user.go"], baseline=baseline)
    assert not report.ok
    assert report.blocked_by.name == "go_vet"


def test_a_lint_finding_the_service_already_makes_elsewhere_does_not_block(
    gate: Recorder, router: Router
) -> None:
    """The regression that cost a whole run, and the one my own T1 fix caused.

    `_lint_is_clean` could never fail on a finding -- it parsed a body that has
    been rendered prose since `_render_lint` landed, so the decode raised every
    time and the check fell back to `result.ok`, which is True whenever the
    sidecar ran. Fixing that without also baselining the stage turned "the
    headline promise is inert" into "no legacy service can ever clear the gate":
    a field run wrote nine correct files and was blocked by 98 findings, most of
    them in `handler/paogen.go`, which it never opened.

    A vertical slice writes a *new* file, so its findings are new keys however
    faithfully it copied the file next door -- which is what the system prompt
    tells it to do when the contract is silent. So the rule class carries the
    judgement: `domain-tags` on a new handler, in a service whose thirty
    existing handlers all trip `domain-tags`, is the house style and not this
    change's regression.
    """
    gate.answer(
        "rules_lint",
        ToolResult.success(
            "3 blocking finding(s)",
            meta={
                "violations": 3,
                "violation_keys": [
                    "domain-tags|core/domain/employee.go|EmployeeRequest is missing ID",
                    "handler-signature|handler/employee.go|takes *gin.Context",
                ],
                "violation_rules": ["domain-tags", "handler-signature"],
            },
        ),
    )
    baseline = Baseline(
        # Nothing about `employee.go` -- it did not exist when the run started.
        findings={"rules_lint": frozenset({"domain-tags|core/domain/objection.go|x"})},
        passed={"rules_lint": False},
        rule_classes={"rules_lint": frozenset({"domain-tags", "handler-signature"})},
        taken=True,
    )

    report = full_gate(router, ["core/domain/employee.go"], baseline=baseline)

    lint = next(r for r in report.results if r.name == "rules_lint")
    assert not lint.ok, "the findings are still reported"
    assert not lint.blocking, "the service already violates both rules elsewhere"
    assert report.ok


def test_a_lint_rule_this_change_introduced_still_blocks(
    gate: Recorder, router: Router
) -> None:
    """What the baseline must not excuse: a kind of violation nothing in the
    service was making until this change made it."""
    gate.answer(
        "rules_lint",
        ToolResult.success(
            "1 blocking finding(s)",
            meta={
                "violations": 1,
                "violation_keys": [
                    "layer-sql-boundary|handler/employee.go|SQL in the handler layer"
                ],
                "violation_rules": ["layer-sql-boundary"],
            },
        ),
    )
    baseline = Baseline(
        findings={"rules_lint": frozenset({"domain-tags|core/domain/objection.go|x"})},
        passed={"rules_lint": False},
        rule_classes={"rules_lint": frozenset({"domain-tags"})},
        taken=True,
    )

    report = full_gate(router, ["handler/employee.go"], baseline=baseline)
    assert not report.ok
    assert report.blocked_by.name == "rules_lint"


def test_a_baseline_taken_after_an_edit_is_discarded(
    gate: Recorder, router: Router
) -> None:
    """The race that moving the baseline off the critical path introduced.

    It is measured on a background thread so the planning phase does not wait
    six seconds for it, which makes "before the run touched anything" a race
    rather than a fact -- and losing it is silent and exactly backwards: the
    snapshot picks up the run's own breakage and the gate then excuses it. Found
    in a scripted run where the first edit landed inside those six seconds; it
    would have reached the field as a gate that waves through the thing it was
    asked to catch.

    The loop closes the race by joining before it enters the writing mode. This
    is the second lock: a baseline whose measurement spans a mutation is thrown
    away rather than trusted.
    """
    def editing(_inv):
        # A real `write_file`, dispatched through the router mid-measurement, so
        # the counters move the way they move in a live run. This test used to
        # bump `router.mutations` by hand, and passed whatever the router
        # actually counted.
        router.dispatch(
            "write_file",
            {"path": "handler/late.go", "content": "package handler\n"},
            approved=True,
        )
        return ToolResult.success("go_build: clean")

    router.handlers["go_build"] = editing

    baseline = take_baseline(router, include_tests=False)
    assert not baseline.taken, "a baseline that spans an edit must not be trusted"
    assert not baseline.passed
    assert "written while it was being measured" in baseline.why_not
    # And an untaken baseline excuses nothing, so the run is still answerable.
    assert not baseline.excuses("go_build", frozenset({"x|y|z"}))


def test_a_baseline_survives_a_documentation_write_during_measurement(
    gate: Recorder, router: Router
) -> None:
    """05cd8b8369aa: the run wrote AGENTS.md while the baseline was measuring.

    Nothing any stage reads changed, and discarding the snapshot for it left
    the gate charging the service's own go.mod drift to the run."""

    def documenting(_inv):
        router.dispatch(
            "write_file", {"path": "AGENTS.md", "content": "# notes\n"}, approved=True
        )
        return ToolResult.success("go_build: clean")

    router.handlers["go_build"] = documenting

    baseline = take_baseline(router, include_tests=False)
    assert router.mutations == 1 and router.code_mutations == 0
    assert baseline.taken, baseline.why_not


def test_a_baseline_that_was_never_taken_excuses_nothing(
    gate: Recorder, router: Router
) -> None:
    """"We did not look" and "nothing was wrong" must not read the same.

    A default `Baseline()` is the first, and reading it as the second would
    quietly excuse every failure on a run whose baseline thread had not
    finished.
    """
    gate.fails("go_vet", "handler/user.go:41:1: unreachable code")
    report = full_gate(router, ["handler/user.go"], baseline=Baseline())
    assert not report.ok
    assert report.blocked_by.name == "go_vet"

def test_the_model_cannot_pass_a_baseline() -> None:
    """A baseline asserted by the model would clear the gate by claiming its own
    violations were always there. It is accepted from the harness only."""
    from dakcoder_agent.tools.registry import get as spec_for
    from dakcoder_agent.tools.router import _ArgError, _coerce

    spec = spec_for("swagger_check")
    assert spec is not None
    assert "baseline" not in spec.parameters.get("properties", {}), (
        "a gate parameter in the model-facing schema is an invitation"
    )
    assert _coerce(spec, {"baseline": ["x"]}, gate=True) == {"baseline": ["x"]}
    with pytest.raises(_ArgError):
        _coerce(spec, {"baseline": ["x"]})

# -- what a failure stops, and what it does not ------------------------------


def test_a_build_failure_still_stops_everything(gate: Recorder, router: Router) -> None:
    """The one dependency that is real: `go vet` over code that does not compile
    is pages of consequences of the error the model already has to fix."""
    gate.fails("go_build", "handler/user.go:12: undefined: Pension")
    report = full_gate(router, ["handler/user.go"])
    assert not report.ok
    assert "go_vet" in report.not_run


def test_a_swagger_failure_does_not_stop_the_stages_it_has_nothing_to_do_with(
    gate: Recorder, router: Router
) -> None:
    """The field transcript ended "blocked at swagger_check" with go_vet,
    go_test, go mod tidy, golangci_lint and govulncheck all "not run" -- none of
    which depends on the API document being complete. The run was reported
    unverified without ever asking the stages that would have judged the change
    that was actually made."""
    gate.fails("swagger_check", "handler/user.go has no Routes() method")
    report = full_gate(router, ["handler/user.go"])

    assert not report.ok, "a blocking failure is still a failure"
    assert report.blocked_by.name == "swagger_check"
    assert report.not_run == (), "five stages were abandoned for no reason"
    assert "go_vet" in gate.order
    assert "go_mod" in gate.order



# -- what the run is answerable for, finding by finding -----------------------


#: A legacy service's `go build ./...`: two errors that predate the run, one the
#: run's own new file. The shape the field report arrived in.
_MIXED_BUILD = """# gitlab.cept.gov.in/it-2.0/pao/handler
handler/request_product.go:45:6: convertProductRequestToDomain redeclared in this block
\thandler/request_product.go:12:6: other declaration of convertProductRequestToDomain
handler/laptop.go:88:2: undefined: laptopRepo"""

_PRE_EXISTING_BUILD = """# gitlab.cept.gov.in/it-2.0/pao/handler
handler/request_product.go:45:6: convertProductRequestToDomain redeclared in this block
\thandler/request_product.go:12:6: other declaration of convertProductRequestToDomain"""


def _build_baseline() -> Baseline:
    return Baseline(
        findings={"go_build": _finding_keys(_PRE_EXISTING_BUILD)},
        passed={"go_build": False},
        taken=True,
    )


def test_a_new_error_does_not_recharge_the_run_with_the_old_ones(
    gate: Recorder, router: Router
) -> None:
    """The defect this file's baseline was supposed to prevent and did not.

    Excusing was stage-at-a-time, so one new compile error in the run's own file
    made every pre-existing error in the module this run's failure again -- and
    the report then said "fix the first error listed", which `go build ./...`
    orders by package, so the first error listed was somebody else's
    redeclaration from two years ago. The field run went to fix it, edited
    nothing it was allowed to edit, and was stopped for standing still.
    """
    gate.fails("go_build", _MIXED_BUILD)

    report = full_gate(router, ["handler/laptop.go"], baseline=_build_baseline())

    build = next(r for r in report.results if r.name == "go_build")
    assert build.blocked, "the run did break the build and must be told so"

    mine, _, already = build.content.partition("Already failing before this run")
    assert "handler/laptop.go:88:2: undefined: laptopRepo" in mine
    assert "redeclared in this block" not in mine, (
        "a pre-existing error was handed over as this change's work"
    )
    assert "redeclared in this block" in already, "and it is still reported"
    assert "other declaration of" in already
    assert "# gitlab.cept.gov.in/it-2.0/pao/handler" in mine, (
        "an error with no package header above it is one the model cannot place"
    )


def test_the_fix_hint_sits_with_the_findings_it_points_at(
    gate: Recorder, router: Router
) -> None:
    """"Fix the first error listed" is only sound advice under a list that is
    this run's. Under the whole build log it points at the wrong file."""
    gate.answer(
        "go_build",
        ToolResult.failure(
            _MIXED_BUILD,
            fix="Fix the first error listed; later ones are often consequences of it.",
        ),
    )

    report = full_gate(router, ["handler/laptop.go"], baseline=_build_baseline())

    content = next(r for r in report.results if r.name == "go_build").content
    assert content.index("Fix the first error listed") < content.index(
        "Already failing before this run"
    )


def test_only_pre_existing_errors_still_excuse_the_whole_stage(
    gate: Recorder, router: Router
) -> None:
    """Splitting the report must not cost the excuse that was already working."""
    gate.fails("go_build", _PRE_EXISTING_BUILD)

    report = full_gate(router, ["handler/laptop.go"], baseline=_build_baseline())

    build = next(r for r in report.results if r.name == "go_build")
    assert not build.ok and not build.blocking
    assert report.ok, "a run whose only failure predates it has passed"
    assert report.not_run == (), "an excused stage must not halt the sequence"


def test_a_baseline_failure_that_named_no_file_excuses_the_stage(
    router: Router
) -> None:
    """The baseline poisoning the gate instead of excusing it.

    `take_baseline` runs `go build` under `-mod=readonly` so it cannot rewrite
    `go.sum` while measuring; the gate stage runs without it. On a module with an
    incomplete `go.sum` the two therefore fail differently -- module resolution
    against compiler errors -- and subtracting one from the other cancels
    nothing. Every compile error was then charged to a run that caused none of
    them.
    """
    readonly = "go: updates to go.mod needed; to update it:\n\tgo mod tidy"
    baseline = take_baseline(
        _scripted(router, {"go_build": ToolResult.failure(readonly)}), include_tests=False
    )

    assert "go_build" in baseline.unkeyable
    assert baseline.charge("go_build", _finding_keys(_MIXED_BUILD)) == frozenset()


def test_a_baseline_that_failed_on_findings_still_charges_the_new_ones(
    router: Router
) -> None:
    """The other half of it: an unkeyable failure excuses the stage whole, and a
    keyable one must go on excusing exactly what it named."""
    baseline = take_baseline(
        _scripted(router, {"go_build": ToolResult.failure(_PRE_EXISTING_BUILD)}),
        include_tests=False,
    )

    assert "go_build" not in baseline.unkeyable
    assert baseline.charge("go_build", _finding_keys(_MIXED_BUILD)) == frozenset(
        {"handler/laptop.go|undefined: laptopRepo"}
    )


def _scripted(router: Router, answers: dict[str, ToolResult]) -> Router:
    """A router whose gate tools answer from ``answers``, defaulting to clean."""
    recorder = Recorder(router)
    for name, result in answers.items():
        recorder.answer(name, result)
    return router


# ── session 05cd8b8369aa: a change that is only documentation ────────────────


def test_a_change_set_of_documentation_runs_no_go_stage(gate: Recorder, router: Router) -> None:
    """05cd8b8369aa: asked for an AGENTS.md, the run was handed `go mod tidy`'s
    verdict on the service's own go.mod and edited it until nothing built."""
    report = full_gate(router, ["AGENTS.md", "docs/notes.md"])

    assert gate.calls == [], f"a Go stage ran on prose: {gate.order}"
    assert report.ok
    assert all(r.skipped.startswith("only documentation changed") for r in report.results)


def test_documentation_is_gated_when_the_module_embeds_files(
    gate: Recorder, router: Router, workspace: Workspace
) -> None:
    """The one way a Markdown file reaches the compiler."""
    (workspace.root / "docs.go").write_text(
        'package main\n\nimport _ "embed"\n\n//go:embed AGENTS.md\nvar notes string\n',
        encoding="utf-8",
    )
    full_gate(router, ["AGENTS.md"])
    assert "go_build" in gate.order


def test_prose_beside_go_is_gated_as_usual(gate: Recorder, router: Router) -> None:
    full_gate(router, ["AGENTS.md", "handler/user.go"])
    assert "go_build" in gate.order


def test_a_requirement_flipping_to_direct_is_a_tidy_finding() -> None:
    """It produced no key, so the baseline read "tidy failing, on nothing" and
    `Baseline.charge` excused the stage whole -- new drift included."""
    from dakcoder_agent.tools.commands import _mod_findings

    before = "module m\n\nrequire (\n\tgithub.com/google/uuid v1.6.0 // indirect\n\tx.test/a v1.0.0\n)\n"
    after = "module m\n\nrequire (\n\tgithub.com/google/uuid v1.6.0\n\tx.test/b v1.0.0\n)\n"

    assert _mod_findings(before, after) == ["+x.test/b", "-x.test/a", "~github.com/google/uuid"]


def test_a_module_problem_that_predates_the_run_does_not_block_swagger_check(
    gate: Recorder, router: Router
) -> None:
    """Found by the real-toolchain replay: a service with no configs/ had every
    Go change blocked at swagger_check, as the run's own failure."""
    problem = "there is no configs/ directory, so generation cannot be enabled"
    gate.answer(
        "swagger_check",
        ToolResult.failure(
            f"swagger_check found 1 problem(s):\n  - {problem}",
            meta={"violations": [], "problems": [problem]},
        ),
    )
    baseline = take_baseline(router, include_tests=False)
    assert baseline.passed["swagger_check"] is False

    report = full_gate(router, ["handler/user.go"], baseline=baseline)
    swagger = next(r for r in report.results if r.name == "swagger_check")
    assert not swagger.blocking, swagger.content


def test_a_generated_validator_that_redeclares_a_hand_written_one_is_found(tmp_path) -> None:
    """The happy path was red on this at HEAD: govalid wrote a `Validate` for the
    template's `ListUsersParams`, which already has one, and the gate's own
    rebuild failed on a file the run never wrote."""
    from dakcoder_agent.tools.commands import _redeclared_validators

    (tmp_path / "request.go").write_text(
        "package request\n\ntype ListUsersParams struct{}\n\n"
        "func (p *ListUsersParams) Validate() error { return nil }\n",
        encoding="utf-8",
    )
    (tmp_path / "request_listusersparams_validator.go").write_text(
        "package request\n\nfunc (s *ListUsersParams) Validate() error { return nil }\n",
        encoding="utf-8",
    )
    (tmp_path / "request_createuser_validator.go").write_text(
        "package request\n\nfunc (s *CreateUser) Validate() error { return nil }\n",
        encoding="utf-8",
    )

    created = ["request_createuser_validator.go", "request_listusersparams_validator.go"]
    assert _redeclared_validators(tmp_path, created) == ["request_listusersparams_validator.go"]
    # Only what this call created is ever a candidate.
    assert _redeclared_validators(tmp_path, []) == []
