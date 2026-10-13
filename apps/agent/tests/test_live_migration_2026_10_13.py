"""What a live `/migration` of pao-backend did on 2026-10-13, finding by finding.

The live harness drove the real runtime (`serve.build`), the real sidecar and
the real toolchain through six sessions on a copy of the corpus. Branch and
dependencies "closed"; handlers never moved, across 35 minutes. Every test here
is one mechanism from that run (or from the field sessions it reproduced), and
where the mechanism lives in the toolchain the test drives the toolchain.

* f756001b8845 -- the planner's `legacy_audit paths="."` said 0 findings; eleven
  `impact` calls were refused past the batch cap three turns running; the run
  ended "done" over a handler that did not parse; the routes were never recorded.
* 52fd0837801c / 8dbca806ba5e -- `go get` x6 then `go mod tidy`, which removed
  all six, and the dependencies phase closed over a go.mod still on api-*.
* 1b3369d4cfb3 -- told "Already done in it (12)" about twelve blocked steps.
* 11e91c951efb (field) -- finished "blocked" on `search_repo` refusing `path`.
* 37ecc37866e8 (field) -- a plan of handler steps opened handlers with
  dependencies still pending.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from dakcoder_shared.envelope import EventType
from dakcoder_shared.llm import ToolCall
from dakcoder_shared.paths import Workspace

from dakcoder_agent.context import ContextManager
from dakcoder_agent.loop import MAX_CALLS_PER_BATCH, AgentLoop, Intent, _State
from dakcoder_agent.gate import ROUTES_BEFORE
from dakcoder_agent.migration import MigrationState, Phase, plan_objection
from dakcoder_agent.modes import Mode
from dakcoder_agent.plan import PlanRecord
from dakcoder_agent.tools import fs
from dakcoder_agent.tools.control import PlanStep
from dakcoder_agent.tools.gotools import GoTools, _conversion_recipe, _find_binary, handlers_for
from dakcoder_agent.tools.router import Router
from dakcoder_agent.undo import UndoStore
from scripted import build, calls, planning_router  # noqa: F401 - fixtures are used by name

ROADMAP = (
    Phase("branch", "cut the branch", "check, confirm, cut"),
    Phase("dependencies", "swap api-* for n-api-*", "go-mod, imports"),
    Phase("handlers", "convert every handler", "paogen, objection"),
    Phase("swagger", "check the document", "docs, v3doc"),
)

needs_sidecar = pytest.mark.skipif(
    _find_binary() is None or shutil.which("go") is None,
    reason="needs the gotools sidecar and the Go toolchain",
)
needs_gofmt = pytest.mark.skipif(shutil.which("gofmt") is None, reason="needs gofmt")

#: A legacy gin service in miniature: two handler files the audit flags, and a
#: routes file the inventory reads.
LEGACY = {
    "go.mod": "module legacy.test\n\ngo 1.24\n",
    "handler/objection.go": (
        "package handler\n\nimport \"github.com/gin-gonic/gin\"\n\n"
        "type ObjectionHandler struct{}\n\n"
        "func (h *ObjectionHandler) FetchObjectionHandler(ctx *gin.Context) {\n"
        "\tvar req struct{ ID string `uri:\"id\"` }\n"
        "\tif err := ctx.ShouldBindUri(&req); err != nil {\n\t\treturn\n\t}\n"
        "\tctx.JSON(200, req)\n}\n"
    ),
    "handler/paogen.go": (
        "package handler\n\nimport \"github.com/gin-gonic/gin\"\n\n"
        "type PaogenHandler struct{}\n\n"
        "func (h *PaogenHandler) ListPAOHandler(ctx *gin.Context) {\n"
        "\tctx.JSON(200, nil)\n}\n"
    ),
    "routes/routes.go": (
        "package routes\n\nimport (\n\t\"github.com/gin-gonic/gin\"\n\n"
        "\t\"legacy.test/handler\"\n)\n\n"
        "func Register(r *gin.Engine, o *handler.ObjectionHandler, p *handler.PaogenHandler) {\n"
        "\tr.GET(\"/v1/objection/:id\", o.FetchObjectionHandler)\n"
        "\tr.GET(\"/v1/pao\", p.ListPAOHandler)\n}\n"
    ),
}


def _service(root: Path) -> Path:
    for rel, body in LEGACY.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8", newline="")
    return root


def _loop(root: Path, router: Router | None = None) -> AgentLoop:
    loop = AgentLoop.__new__(AgentLoop)
    loop.router = router or Router(Workspace.at(root))
    loop.state = _State()
    loop.state.mode = Mode.AGENT
    loop.session_id = ""
    loop.context = ContextManager(mode=Mode.AGENT, system_prompt="s")
    loop._plan_record = PlanRecord()
    loop._relay = lambda event: None
    return loop


def _migrating(root: Path, router: Router | None = None, *, closed=("branch",)) -> AgentLoop:
    loop = _loop(root, router)
    loop.state.migration = MigrationState(active=True, branch="migration/n-api-template", base="main")
    loop.state.migration.adopt(ROADMAP)
    for name in closed:
        loop.state.migration.close(name)
    return loop


# ── the audit, scoped to a directory ────────────────────────────────────────


@needs_sidecar
@pytest.mark.parametrize("paths", [".", "handler", "handler/"])
def test_an_audit_scoped_to_a_directory_reports_the_findings_in_it(tmp_path: Path, paths: str) -> None:
    """f756001b8845 turn 1: `legacy_audit paths="."` on a service with 132
    findings answered "0 blocking and 0 advisory finding(s) in the 1 file(s)
    you changed", and the planner planned the migration blind."""
    root = _service(tmp_path)
    with GoTools(root) as sidecar:
        router = Router(Workspace.at(root), handlers_for(sidecar))
        unscoped = router.dispatch("legacy_audit", {}, mode=Mode.PLANNER)
        scoped = router.dispatch("legacy_audit", {"paths": paths}, mode=Mode.PLANNER)

    assert unscoped.ok and scoped.ok
    assert "handler/objection.go" in unscoped.content
    assert "handler/objection.go" in scoped.content, scoped.content
    assert not scoped.content.startswith("0 blocking and 0 advisory"), scoped.content
    assert "you changed" not in scoped.content


# ── the routes, recorded and counted ────────────────────────────────────────


@needs_sidecar
def test_the_route_inventory_counts_the_routes_the_sidecar_found(tmp_path: Path) -> None:
    """The bridge read `payload["result"]`, which the sidecar does not send, so
    every inventory counted 0 routes and a lost route could never be found."""
    root = _service(tmp_path)
    with GoTools(root) as sidecar:
        router = Router(Workspace.at(root), handlers_for(sidecar))
        out = router.run_gate_tool("route_inventory", {"save": ROUTES_BEFORE})

    assert out.ok, out.content
    assert out.meta["routes"] == 2, out.content
    assert json.loads((root / ROUTES_BEFORE).read_text(encoding="utf-8"))


def test_the_routes_are_recorded_when_the_branch_was_cut_first(tmp_path: Path) -> None:
    """f756001b8845 did step one right -- `git_ops` op=branch, then edits -- and
    the inventory was never taken: it sat below the guard's "a branch is
    already cut" early return, so only a run that wrote before branching
    recorded anything."""
    (tmp_path / ".git").mkdir()
    loop = _migrating(tmp_path)
    taken: list[str] = []
    loop.router.run_gate_tool = lambda name, args=None: (
        taken.append(name) or _ok(meta={"routes": 93, "unresolved": 0})
    )

    refused = loop._migration_guard(ToolCall(id="1", name="patch_file", arguments="{}"))

    assert refused is None, "a migration on its own branch writes freely"
    assert taken == ["route_inventory"]
    assert loop.state.routes_before == 93


def _ok(content: str = "ok", meta=None):
    from dakcoder_shared.envelope import ToolResult

    return ToolResult.success(content, meta=meta or {})


# ── the batch cap ───────────────────────────────────────────────────────────


def test_a_batch_resent_after_the_cap_runs_the_calls_it_refused(planning_router: Router) -> None:
    """f756001b8845 turns 10-12: eleven `impact` calls, six run, five refused
    "ask again in a later turn". The model asked again by resending all eleven;
    the six answered from the ledger still took the six slots, and the same
    five were refused for three turns running."""
    many = [("search_repo", json.dumps({"pattern": f"p{i}"})) for i in range(MAX_CALLS_PER_BATCH + 3)]
    loop, _client = build(
        planning_router,
        [calls(*many), calls(*many), calls(("finish", '{"answer":"searched"}'))],
        kind="question",
    )
    events = list(loop.run("find things", intent=Intent.ASK))

    results = [e for e in events if e.type is EventType.TOOL_RESULT]
    capped = [r for r in results if "more than" in str(r.data.get("summary") or r.data.get("content") or "")]
    assert len(capped) == 3, "only the first reply runs past the cap"
    patterns_run = {
        json.loads(e.data["arguments"])["pattern"] if isinstance(e.data.get("arguments"), str)
        else e.data["arguments"]["pattern"]
        for e in events
        if e.type is EventType.TOOL_CALL and e.data.get("name") == "search_repo"
    }
    assert patterns_run == {f"p{i}" for i in range(MAX_CALLS_PER_BATCH + 3)}


# ── search_repo(path=) ──────────────────────────────────────────────────────


def test_search_repo_reads_path_as_the_glob_it_means(tmp_path: Path) -> None:
    """11e91c951efb turn 11: "unknown parameter(s) path", and the run finished
    "blocked: search_repo tool returns 'unknown parameter(s) path' error"."""
    root = _service(tmp_path)
    router = Router(Workspace.at(root), dict(fs.HANDLERS))

    in_file = router.dispatch(
        "search_repo", {"pattern": "func \\(h", "path": "handler/objection.go"}, mode=Mode.AGENT
    )
    in_dir = router.dispatch("search_repo", {"pattern": "func \\(h", "path": "handler"}, mode=Mode.AGENT)

    assert in_file.ok, in_file.content
    assert "handler/objection.go" in in_file.content and "paogen" not in in_file.content
    assert in_dir.ok and "handler/paogen.go" in in_dir.content and "routes.go" not in in_dir.content


# ── phases are not skipped ──────────────────────────────────────────────────


def _handler_step() -> PlanStep:
    return PlanStep(
        "handler/objection.go", "convert FetchObjectionHandler",
        "unit_check path=handler/objection.go methods=FetchObjectionHandler",
        phase="handlers", part="objection",
    )


def test_a_plan_that_starts_past_an_open_phase_is_sent_back() -> None:
    """37ecc37866e8 and f756001b8845: steps for handlers alone opened handlers
    with dependencies pending, and handlers were converted to serverRoute
    against a go.mod still on api-*."""
    state = MigrationState(active=True)
    state.adopt(ROADMAP)

    objection = plan_objection(state, ROADMAP, [_handler_step()])

    assert "dependencies" in objection and "cannot be skipped" in objection


def test_the_branch_phase_alone_may_be_passed_over() -> None:
    """The acting phase cuts the branch whatever the plan says, so a plan that
    starts at dependencies is the right plan."""
    state = MigrationState(active=True)
    state.adopt(ROADMAP)
    deps = PlanStep("go.mod", "swap", "go_mod op=get for each", phase="dependencies", part="go-mod")

    assert plan_objection(state, ROADMAP, [deps]) == ""


def test_a_plan_for_the_open_phase_is_not_sent_back() -> None:
    state = MigrationState(active=True)
    state.adopt(ROADMAP)
    state.close("branch")
    state.close("dependencies")

    assert plan_objection(state, ROADMAP, [_handler_step()]) == ""


# ── a file that does not parse ──────────────────────────────────────────────


def _written_broken(root: Path) -> Router:
    """The live model's edit: a struct literal opened and never closed, which
    is how a signature-first conversion of a gin body stops parsing."""
    router = Router(Workspace.at(root), dict(fs.HANDLERS), undo=UndoStore(root, "s1"))
    out = router.dispatch(
        "patch_file",
        {
            "path": "handler/objection.go",
            "old": "\tctx.JSON(200, req)\n",
            "new": "\trequest := Thing{\n\n\tif true {\n\t\treturn\n\t}\n",
        },
        mode=Mode.AGENT,
    )
    assert out.ok, out.content
    return router


@needs_gofmt
def test_finish_is_sent_back_while_a_written_file_does_not_parse(tmp_path: Path) -> None:
    """f756001b8845 ended "done" over `syntax error: unexpected keyword if`; the
    next session's handler_map read the file as "0 handler methods"."""
    root = _service(tmp_path)
    loop = _migrating(root, _written_broken(root), closed=("branch", "dependencies"))

    reason = loop._why_not_done()

    assert "handler/objection.go does not parse" in reason
    assert "restored" in reason


@needs_gofmt
def test_a_file_still_unparseable_at_the_checkpoint_is_restored(tmp_path: Path) -> None:
    root = _service(tmp_path)
    original = (root / "handler" / "objection.go").read_text(encoding="utf-8")
    loop = _migrating(root, _written_broken(root), closed=("branch", "dependencies"))
    loop.state.plan = (
        PlanStep(
            "handler/objection.go", "convert", "unit_check", phase="handlers", status="blocked", note="gave up"
        ),
    )

    restored = loop._restore_unparsed()

    assert restored and restored[0].startswith("handler/objection.go")
    assert (root / "handler" / "objection.go").read_text(encoding="utf-8") == original
    assert loop.state.plan[0].status == "pending"


@needs_gofmt
def test_a_file_that_parses_is_left_alone(tmp_path: Path) -> None:
    root = _service(tmp_path)
    router = Router(Workspace.at(root), dict(fs.HANDLERS), undo=UndoStore(root, "s1"))
    router.dispatch("write_file", {"path": "handler/new.go", "content": "package handler\n"}, mode=Mode.AGENT)
    loop = _migrating(root, router)

    assert loop._unparsed() == []
    assert loop._restore_unparsed() == []


# ── blocked is not done ─────────────────────────────────────────────────────


def _record(state: MigrationState, status: str, note: str = "") -> None:
    state.record_steps(
        [PlanStep("handler/paogen.go", "convert", "unit_check", phase="handlers", part="paogen-step1",
                  status=status, note=note)],
        ["handler/paogen.go"],
    )


def test_the_resume_view_does_not_call_blocked_steps_done() -> None:
    """1b3369d4cfb3 was told "Already done in it (12)" about twelve steps an
    earlier session had blocked on its way out, while unit_check said none of
    their methods were converted."""
    state = MigrationState(active=True)
    state.adopt(ROADMAP)
    state.close("branch")
    state.close("dependencies")
    _record(state, "blocked", "the file has syntax errors")

    view = "\n".join(state.resume_block())

    assert "Already done" not in view
    assert "Blocked by an earlier session, not converted (1)" in view
    assert "syntax errors" in view


def test_a_new_plan_reopens_a_blocked_unit_but_not_a_done_one() -> None:
    state = MigrationState(active=True)
    state.adopt(ROADMAP)
    _record(state, "blocked")
    _record(state, "pending")
    assert [u["status"] for u in state.units.values()] == ["pending"]

    _record(state, "done")
    _record(state, "pending")
    assert [u["status"] for u in state.units.values()] == ["done"]


# ── what unit_check says to do ──────────────────────────────────────────────


def test_a_not_done_unit_check_names_the_edit_for_each_reason() -> None:
    """f756001b8845 converted signatures only -- `req struct{}` over bodies
    still calling ShouldBindJSON -- and 1b3369d4cfb3 skipped a step as "already
    uses n-api signature". The report said what was wrong, never what to write."""
    report = (
        "NOT DONE: handler/paogen.go. parses 0 of 1 methods converted\n"
        "  todo  PaogenHandler.X 1-9: takes *gin.Context; calls ShouldBindUri; writes its own response (handleSuccess)\n"
        "  todo  PaogenRepository.Y (repo/postgres/paogen.go:4): takes *gin.Context -- make it context.Context"
    )
    recipe = _conversion_recipe(report)

    assert "the whole body, not only its signature" in recipe
    for needle in ("sctx *serverRoute.Context", "ShouldBind", "return &apiRsp, nil", "return nil, err", "sctx.Ctx"):
        assert needle in recipe, needle
    assert _conversion_recipe("DONE: handler/paogen.go. 1 of 1 converted") == ""


# ── go mod tidy and the dependency phase ────────────────────────────────────


def _call(name: str, args: dict) -> ToolCall:
    return ToolCall(id="c1", name=name, arguments=json.dumps(args))


def test_go_mod_tidy_waits_for_the_last_phase(tmp_path: Path) -> None:
    """52fd0837801c: `go get` x6, then `go mod tidy`, which removed all six."""
    loop = _migrating(tmp_path)

    assert "waits for the migration's last phase" in loop._tidies_mid_migration(_call("go_mod", {"op": "tidy"}))
    assert loop._tidies_mid_migration(_call("run_terminal", {"argv": ["go", "mod", "tidy"]}))
    assert loop._tidies_mid_migration(_call("go_mod", {"op": "get", "pkg": "x"})) == ""


def test_go_mod_tidy_is_untouched_outside_a_migration(tmp_path: Path) -> None:
    loop = _loop(tmp_path)

    assert loop._tidies_mid_migration(_call("go_mod", {"op": "tidy"})) == ""


GO_MOD_LEGACY = (
    "module pao\n\ngo 1.24\n\nrequire (\n"
    "\tgitlab.cept.gov.in/it-2.0-common/api-config v0.0.17\n"
    "\tgitlab.cept.gov.in/it-2.0-common/api-server v0.0.63\n"
    "\tgitlab.cept.gov.in/it-2.0-common/api-log v1.1.5\n"
    "\tgitlab.cept.gov.in/it-2.0-common/n-api-validation v0.0.9 // indirect\n)\n"
)


def _deps_done(root: Path, go_mod: str, status: str = "done") -> AgentLoop:
    (root / "go.mod").write_text(go_mod, encoding="utf-8")
    loop = _migrating(root)
    loop._save_plan = lambda: None
    loop._refresh_scope = lambda: None
    loop.state.plan = (
        PlanStep("go.mod", "swap api-* for n-api-*", "go_mod get", phase="dependencies",
                 part="go-mod", status=status),
    )
    return loop


def test_the_dependency_phase_stays_open_while_go_mod_is_on_api(tmp_path: Path) -> None:
    """8dbca806ba5e closed dependencies "complete" over that go.mod."""
    loop = _deps_done(tmp_path, GO_MOD_LEGACY)

    assert loop._close_phase() == ""
    assert loop.state.phase_left == ("go.mod",)
    told = [m.content for m in loop.context.build() if m.role.value == "user"]
    assert any("n-api-log, n-api-server" in t and "go mod tidy" in t for t in told), told


def test_the_dependency_phase_closes_once_the_replacements_are_required(tmp_path: Path) -> None:
    swapped = GO_MOD_LEGACY.replace(
        ")\n",
        "\tgitlab.cept.gov.in/it-2.0-common/n-api-server v0.0.23\n"
        "\tgitlab.cept.gov.in/it-2.0-common/n-api-log v0.0.5\n)\n",
    )
    loop = _deps_done(tmp_path, swapped)

    assert loop._close_phase() == "dependencies"


def test_a_blocked_go_mod_step_lets_the_dependency_phase_close(tmp_path: Path) -> None:
    """The bounded exit: a module that cannot be fetched is said, not waited on."""
    loop = _deps_done(tmp_path, GO_MOD_LEGACY, status="blocked")

    assert loop._close_phase() == "dependencies"



# ── a migration's planner plans ─────────────────────────────────────────────


def test_a_migration_planner_that_finishes_without_a_plan_is_sent_back_once(tmp_path: Path) -> None:
    """31f695f60ba1 resumed at handlers, re-read six files for three turns and
    finished "blocked: I cannot determine the exact state of each handler
    method conversion" -- the question handler_map answers -- so the session
    ended "done" with nothing planned."""
    loop = _migrating(tmp_path, closed=("branch", "dependencies"))
    loop.state.mode = Mode.PLANNER
    loop.result = None
    finish = _ok(meta={"answer": "all handlers still need converting", "blocked": "cannot tell what is done"})

    list(loop._phase_ended("finish", finish))

    told = [m.content for m in loop.context.build() if m.role.value == "user"]
    assert loop.result is None, "the run goes on"
    assert any("Phase handlers is open" in t and "submit_plan" in t for t in told), told
    assert loop.state.finish_refused == 1


@needs_sidecar
def test_an_unsplit_migration_step_advances_only_when_unit_check_agrees(tmp_path: Path) -> None:
    """948802ca77ce rewrote a 226-line handler, never ran unit_check, and its
    one step went to done on a clean lint with 0 of 2 methods converted."""
    root = _service(tmp_path)
    with GoTools(root) as sidecar:
        loop = _migrating(root, Router(Workspace.at(root), handlers_for(sidecar)), closed=("branch", "dependencies"))
        loop._save_plan = lambda: None
        loop.state.plan = (
            PlanStep("handler/objection.go", "convert the handler", "unit_check", phase="handlers", part="objection"),
        )

        loop._mark_steps("handler/objection.go", "written")

    step = loop.state.plan[0]
    assert step.status == "pending", step
    assert "0 of 1 methods converted" in step.note and "FetchObjectionHandler" in step.note
