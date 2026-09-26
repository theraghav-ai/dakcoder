"""The migration's call map, from the Python side: when it is offered, the plan
check it replaces, and what the acting phase is told about its step.

Session e3edb2434936 is the case all of this is for. Its handlers phase split
`handler/paogen.go` into seven steps and checked each with "go_build passes for
the handler package". After the dependency swap the service does not build
until every phase is done, so the acting phase refused all seven -- correctly
-- and the run stalled with nothing converted.

The sidecar half (`handler_map`, `unit_check`, `impact`) is tested in Go, in
`gotools/internal/callmap`, against the same legacy service.
"""

from __future__ import annotations

from dakcoder_shared.envelope import ToolResult

from dakcoder_agent.loop import MIGRATION_TOOLS
from dakcoder_agent.migration import MigrationState, Phase, plan_objection
from dakcoder_agent.modes import Mode
from dakcoder_agent.tools import registry
from dakcoder_agent.tools.control import PlanStep
from scripted import build, say  # noqa: E402
from scripted import gated, planning_router  # noqa: F401,E402

ROADMAP = (
    Phase("branch", "cut the branch", "check, confirm, cut"),
    Phase("deps", "swap api-* for n-api-*", "go get, tidy"),
    Phase("handlers", "convert every handler", "paogen, objection"),
    Phase("verify", "build and route check", "go_build, routes"),
)


def _state() -> MigrationState:
    return MigrationState(active=True)


def _step(accepts: str, phase: str = "handlers", file: str = "handler/objection.go") -> PlanStep:
    return PlanStep(file=file, action="convert CreateObjectionHandler", accepts=accepts, phase=phase, part="create")


# ── the plan check ──────────────────────────────────────────────────────────


def test_a_mid_migration_step_checked_by_the_build_is_sent_back() -> None:
    objection = plan_objection(_state(), ROADMAP, [_step("go_build passes for the handler package")])

    assert "cannot pass until the migration's last phase" in objection
    assert "unit_check path=<file>" in objection


def test_every_spelling_of_a_build_check_is_caught() -> None:
    for accepts in ("go build ./...", "go_vet clean", "go test ./handler/...", "the package compiles",
                    "builds clean"):
        assert "unit_check" in plan_objection(_state(), ROADMAP, [_step(accepts)]), accepts


def test_the_last_phase_may_be_checked_by_the_build() -> None:
    objection = plan_objection(
        _state(), ROADMAP, [_step("go_build passes", phase="verify", file="main.go")]
    )
    assert "unit_check" not in objection


def test_a_unit_check_step_is_accepted() -> None:
    step = _step("unit_check path=handler/objection.go methods=CreateObjectionHandler says DONE")
    assert plan_objection(_state(), ROADMAP, [step]) == ""


def test_the_split_advice_names_handler_map() -> None:
    step = _step("unit_check path=handler/paogen.go methods=A", file="handler/paogen.go")
    objection = plan_objection(_state(), ROADMAP, [step], lines=lambda path: 6571)
    assert "handler_map path=handler/paogen.go" in objection


# ── when the tools are offered ──────────────────────────────────────────────


def _loop_with_callmap(planning_router):
    loop, _ = build(planning_router, [say("noop")])
    for name in MIGRATION_TOOLS:
        loop.router.register(name, lambda inv: ToolResult.success("ok"))
    return loop


def _offered(loop) -> set[str]:
    return {t["function"]["name"] for t in loop._tools()}


def test_the_call_map_is_not_offered_outside_a_migration(planning_router) -> None:
    """~340 prompt tokens a turn that no other task should pay."""
    loop = _loop_with_callmap(planning_router)
    for mode in (Mode.ASK, Mode.PLANNER, Mode.AGENT):
        loop.state.mode = mode
        assert not _offered(loop) & MIGRATION_TOOLS, mode


def test_the_call_map_is_offered_in_every_mode_of_a_migration(planning_router) -> None:
    """The planner needs handler_map to split a file; the acting phase needs
    unit_check to know a step is done."""
    loop = _loop_with_callmap(planning_router)
    loop.state.migration.active = True
    for mode in (Mode.PLANNER, Mode.AGENT):
        loop.state.mode = mode
        assert MIGRATION_TOOLS <= _offered(loop), mode


def test_the_call_map_specs_are_sidecar_lookups() -> None:
    for name in MIGRATION_TOOLS:
        spec = registry.REGISTRY[name]
        assert spec.provider is registry.Provider.GOTOOLS
        assert not spec.mutates and spec.parallel


# ── what the acting phase is told ───────────────────────────────────────────


def test_a_mid_migration_step_says_how_it_is_checked(planning_router) -> None:
    """Said on the step because the plan may predate the objection -- the field
    run's did -- and a plan that says go_build still reaches the acting phase."""
    loop = _loop_with_callmap(planning_router)
    loop.state.migration.active = True
    loop.state.migration.adopt(ROADMAP)
    loop.state.plan = (_step("go_build passes for the handler package", file="handler/paogen.go"),)

    block = "\n".join(loop._plan_block())
    assert "does not build, by design" in block
    assert "unit_check path=handler/paogen.go" in block
