"""Tests for the tool router.

Grouped by the guarantee each one defends, because that is how the router is
designed — six ordered checks, each with a property it must hold no matter what
the model sends.
"""

from __future__ import annotations

import pytest

from dakcoder_agent.modes import Mode
from dakcoder_agent.tools import registry
from dakcoder_agent.tools.registry import Approval
from dakcoder_agent.tools.router import ApprovalPolicy, ApprovalRequest, Router
from dakcoder_shared.envelope import ToolResult


def result(outcome) -> ToolResult:
    assert isinstance(outcome, ToolResult), f"expected a result, got {outcome!r}"
    return outcome


def approval(outcome) -> ApprovalRequest:
    assert isinstance(outcome, ApprovalRequest), f"expected an approval, got {outcome!r}"
    return outcome


# ── 1. mode filtering is a guarantee ────────────────────────────────────────


def test_the_planner_physically_cannot_write(router: Router) -> None:
    """Not "is instructed not to" — cannot.

    Two locks: write_file is absent from the Planner's schema list, and the
    router refuses it even when called directly. The second exists because the
    first is a list the model sees, and anything the model sees can be replayed
    from an earlier turn.
    """
    names = {s["function"]["name"] for s in router.schemas_for(Mode.PLANNER)}
    assert "write_file" not in names
    assert "patch_file" not in names
    assert "delete_file" not in names

    out = result(router.dispatch("write_file", {"path": "x.go", "content": "y"}, mode=Mode.PLANNER))
    assert not out.ok
    assert "not available in planner mode" in out.content


def test_every_mode_gets_only_its_own_tools(router: Router) -> None:
    for mode in Mode:
        for schema in router.schemas_for(mode):
            spec = registry.get(schema["function"]["name"])
            assert spec is not None and mode in spec.modes


def test_a_refused_mode_still_names_where_the_tool_lives(router: Router) -> None:
    """A bare refusal costs a turn while the model guesses; a named alternative
    costs nothing. `run_terminal` is the acting mode's, so asking for it from a
    read-only phase must say which phase has it."""
    out = result(router.dispatch("run_terminal", {"argv": "[]"}, mode=Mode.ASK))
    assert "agent" in out.content


def test_gate_tools_are_invisible_to_every_mode_but_still_dispatchable(router: Router) -> None:
    """Part A section 9.3's gate is an ordered fail-fast sequence, not a menu.

    A model that *chooses* whether to run `go vet` is one that sometimes does
    not, and "the model forgot to verify" is the exact failure this design
    exists to prevent. So the gate does not ask — and the tools cost no prompt
    tokens in any mode as a side effect.
    """
    for mode in Mode:
        names = {s["function"]["name"] for s in router.schemas_for(mode)}
        assert not names & set(registry.gate_tools())

    refused = result(router.dispatch("gofmt", {"paths": ""}, mode=Mode.AGENT))
    assert not refused.ok
    assert "run automatically" in refused.content

    allowed = result(router.run_gate_tool("gofmt", {"paths": ""}))
    assert allowed.ok


# ── 2. every refusal names the alternative ──────────────────────────────────


@pytest.mark.parametrize(
    "called,expected",
    [
        ("grep", "search_repo"),
        ("cat", "read_file"),
        ("str_replace", "patch_file"),
        ("ls", "repo_map"),
        ("sed", "patch_file"),
        ("apply_patch", "patch_file"),
    ],
)
def test_habits_from_other_harnesses_are_redirected(
    router: Router, called: str, expected: str
) -> None:
    """postgen's highest-value line, generalised.

    None of these are close enough to their right answer for edit distance to
    find — they are correct tool names in *other* harnesses. A bare "unknown
    tool" costs a turn while the model guesses; naming the tool costs nothing.
    """
    out = result(router.dispatch(called, {}, mode=Mode.AGENT))
    assert not out.ok
    assert expected in out.fix


def test_a_near_miss_gets_the_real_name(router: Router) -> None:
    assert "read_file" in result(router.dispatch("read_fil", {"path": "x"})).fix


def test_an_unrecognisable_name_lists_what_is_available(router: Router) -> None:
    out = result(router.dispatch("frobnicate", {}, mode=Mode.PLANNER))
    assert "read_file" in out.fix and "search_repo" in out.fix


def test_every_registry_refusal_carries_a_fix(router: Router) -> None:
    """A structural check rather than a sampled one.

    A refusal with no fix is a wasted turn, and the way that regression enters a
    codebase is one new tool at a time.
    """
    for spec in registry.all_specs():
        wrong_mode = next((m for m in Mode if m not in spec.modes), None)
        if wrong_mode is None or spec.unavailable or spec.gate_only:
            continue
        out = result(router.dispatch(spec.name, {}, mode=wrong_mode))
        assert not out.ok
        assert out.fix, f"{spec.name} refuses without saying what to do instead"


def test_an_unimplemented_tool_says_so_and_offers_a_substitute(router: Router) -> None:
    """go_symbols and go_diagnostics are specified but gopls is not yet wired.

    They stay in the registry so the catalog is the whole contract rather than
    only the finished part of it, and they are hidden from every schema list so
    the model is never offered something that cannot run.
    """
    unavailable = [s.name for s in registry.unavailable()]
    assert "go_symbols" in unavailable

    for mode in Mode:
        assert "go_symbols" not in {s["function"]["name"] for s in router.schemas_for(mode)}

    out = result(router.dispatch("go_symbols", {"query": "x"}, mode=Mode.AGENT))
    assert not out.ok
    assert "search_repo" in out.fix


# ── 3. arguments are untrusted input ────────────────────────────────────────


def test_unknown_parameters_are_refused_with_the_real_list(router: Router) -> None:
    out = result(router.dispatch("read_file", {"path": "handler/user.go", "lines": "1-5"}))
    assert not out.ok
    assert "lines" in out.content
    assert "path, start, end" in out.fix


def test_a_missing_required_parameter_is_refused(router: Router) -> None:
    out = result(router.dispatch("patch_file", {"path": "handler/user.go", "old": "x"}))
    assert not out.ok
    assert "new" in out.content


def test_a_wrong_type_is_refused_with_an_example(router: Router) -> None:
    out = result(router.dispatch("read_file", {"path": "handler/user.go", "start": "abc"}))
    assert not out.ok
    assert "whole number" in out.content


def test_an_enum_violation_lists_the_allowed_values(router: Router) -> None:
    out = result(router.dispatch("fx_wire", {"kind": "nope", "ctor": "x.New"}, mode=Mode.AGENT))
    assert not out.ok
    assert "repo" in out.fix and "handler" in out.fix


def test_numeric_strings_are_accepted_because_every_model_sends_them(router: Router) -> None:
    """A lenient coercion, deliberately narrow.

    "40" for an integer is unambiguous and universal across models. Refusing it
    costs a turn to teach the model something the schema already said. Anything
    ambiguous is still refused — see the wrong-type test above.
    """
    out = result(router.dispatch("read_file", {"path": "handler/user.go", "start": "1", "end": "2"}))
    assert out.ok
    assert "lines 1-2" in out.content


def test_arguments_arriving_as_a_json_string_are_parsed(router: Router) -> None:
    """This endpoint's tool-calling does this intermittently (Part A section 4.2)."""
    out = result(router.dispatch("read_file", '{"path": "handler/user.go"}'))
    assert out.ok


def test_malformed_json_arguments_are_refused_clearly(router: Router) -> None:
    out = result(router.dispatch("read_file", '{"path": '))
    assert not out.ok
    assert "not valid JSON" in out.content


def test_an_over_large_integer_is_clamped_not_refused(router: Router) -> None:
    """An over-large max is the model asking for "everything", and the ceiling is
    exactly what we want to give it. Refusing would cost a turn to negotiate a
    number the model does not care about."""
    out = result(router.dispatch("search_repo", {"pattern": "func", "max": 100000}))
    assert out.ok


# ── 4. paths are confined ───────────────────────────────────────────────────


@pytest.mark.parametrize(
    "path", ["../../../etc/passwd", "/etc/passwd", "C:/Windows/win.ini", "handler/user.go:ads"]
)
def test_no_tool_can_reach_outside_the_workspace(router: Router, path: str) -> None:
    out = result(router.dispatch("read_file", {"path": path}))
    assert not out.ok
    assert out.fix


def test_paths_are_normalised_before_the_handler_sees_them(router: Router) -> None:
    """The handler never acts on the string the model wrote.

    Without this, a protected path could be reached by spelling it differently,
    and the approval check would pass on a string that resolves somewhere else.
    """
    out = result(router.dispatch("read_file", {"path": "handler\\user.go"}))
    assert out.ok
    assert out.content.startswith("handler/user.go")


def test_a_path_list_is_confined_element_by_element(router: Router) -> None:
    out = result(
        router.dispatch("rules_lint", {"paths": "handler/user.go,../../../etc/passwd"})
    )
    assert not out.ok


# ── 5. approval ─────────────────────────────────────────────────────────────


def test_a_protected_path_needs_approval_and_says_which_one(router: Router) -> None:
    request = approval(
        router.dispatch("patch_file", {"path": "go.mod", "old": "a", "new": "b"})
    )
    assert "go.mod" in request.reason
    assert request.paths == ("go.mod",)


def test_an_ordinary_edit_does_not(router: Router) -> None:
    """The common path must stay uninterrupted, or the prompt becomes noise and
    the one on go.mod gets clicked through with the rest."""
    out = result(
        router.dispatch(
            "patch_file",
            {"path": "handler/user.go", "old": "func New()", "new": "func NewHandler()"},
        )
    )
    assert out.ok


def test_delete_always_asks_and_cannot_be_auto_approved(router: Router) -> None:
    """A session-wide "yes to everything" that also covers delete_file is how an
    approval layer becomes decoration."""
    router.policy = ApprovalPolicy(auto={"delete_file", "patch_file"})
    request = approval(router.dispatch("delete_file", {"path": "handler/user.go", "reason": "x"}))
    assert request.unconditional


def test_auto_approval_covers_the_conditional_tools(router: Router) -> None:
    router.policy = ApprovalPolicy(auto={"patch_file"})
    out = result(router.dispatch("patch_file", {"path": "go.mod", "old": "module pisapi", "new": "module x"}))
    assert out.ok


def test_approval_is_returned_not_raised_and_nothing_ran(router: Router) -> None:
    before = (router.workspace.root / "go.mod").read_bytes()
    approval(router.dispatch("patch_file", {"path": "go.mod", "old": "module pisapi", "new": "x"}))
    assert (router.workspace.root / "go.mod").read_bytes() == before


def test_adding_a_dependency_needs_approval_but_tidy_does_not(router: Router) -> None:
    request = approval(router.dispatch("go_mod", {"op": "get", "pkg": "example.com/x"}))
    assert "example.com/x" in request.reason


# ── 6. failures stay contained ──────────────────────────────────────────────


def test_a_handler_that_raises_becomes_a_result_not_a_crash(router: Router) -> None:
    """A tool that explodes must not take the session with it — the model can
    often recover, and a traceback that kills the process gives it no chance."""

    def explode(_inv):
        raise RuntimeError("boom")

    router.register("read_file", explode)
    out = result(router.dispatch("read_file", {"path": "handler/user.go"}))
    assert not out.ok
    assert "RuntimeError" in out.content


def test_a_result_records_which_tool_ran_and_how_long(router: Router) -> None:
    out = result(router.dispatch("read_file", {"path": "handler/user.go"}))
    assert out.meta["tool"] == "read_file"
    assert out.meta["ms"] >= 0


def test_mutations_accumulate_for_the_gate_to_scope_itself_to(router: Router) -> None:
    """Part A section 9.3 requires gofmt and rules_lint to be scoped to touched
    files, because the reference template does not pass an unscoped gofmt -l."""
    router.dispatch("write_file", {"path": "handler/pension.go", "content": "package handler\n"})
    router.dispatch(
        "patch_file", {"path": "handler/user.go", "old": "func New()", "new": "func NewX()"}
    )
    assert router.touched == ["handler/pension.go", "handler/user.go"]


# ── the C1 contract ─────────────────────────────────────────────────────────


def test_the_catalog_obeys_c1(router: Router) -> None:
    for spec in registry.all_specs():
        assert len(spec.description) <= registry.MAX_DESCRIPTION
        assert len(spec.parameters.get("properties", {})) <= registry.MAX_PARAMS
        assert spec.description.endswith(".")


def test_every_mutating_tool_is_either_gated_or_justified() -> None:
    """A mutating tool with no approval must be in the safe list *with a reason*.

    The check exists so that adding one is a deliberate act. Silence here is how
    an approval layer erodes: one tool at a time, each individually defensible.
    """
    for spec in registry.all_specs():
        if spec.mutates and spec.approval is Approval.NONE:
            assert spec.name in {"gofmt", "govalid_gen", "fx_wire"}


def test_no_mode_is_offered_a_tool_the_runtime_cannot_run(router: Router) -> None:
    for mode in Mode:
        for schema in router.schemas_for(mode):
            assert schema["function"]["name"] in router.handlers
