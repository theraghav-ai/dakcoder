"""Session 3a82e19883f7, replayed against the real toolchain.

Asked for an employee CRUD API on pao-backend, the run wrote `core/port/employee.go`
importing `core/domain` -- the reference template's direction -- in a service
where `core/domain` already imports `core/port`. That closed an import cycle at
turn 21. Nothing compiled until turn 58. In between, `govalid_gen` failed with
the cycle, the model blamed the request validators and deleted them twice, was
told to write back files it had itself created, had four honest `blocked`
revisions refused as "revised 2 times, not adopted", and ended `unverified` on
a plan step naming a file only the gate generates.

The fixture is that service's shape in miniature: `core/domain` imports
`core/port`. Nothing is faked but the model -- real `go`, the real gotools
sidecar, real file tools. Skipped where Go or the sidecar is missing; failing
instead under DAKCODER_REQUIRE_INTEGRATION.
"""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

import pytest

from dakcoder_agent.context import ContextManager
from dakcoder_agent.loop import MAX_REVISIONS, REQUEST_DTO_FILE, AgentLoop, Intent, Outcome
from dakcoder_agent.modes import Mode
from dakcoder_agent.tools import commands, control, fs, knowledge
from dakcoder_agent.tools.control import PlanStep
from dakcoder_agent.tools.gotools import GoTools, _find_binary, handlers_for
from dakcoder_agent.tools.router import Router
from dakcoder_shared.envelope import EventType
from dakcoder_shared.paths import Workspace

from scripted import ScriptedClient, calls, say

if _find_binary() is None:
    _WHY_NOT = "gotools binary not built; run `make -C gotools build`"
elif shutil.which("go") is None:
    _WHY_NOT = "the Go toolchain is not on PATH"
else:
    _WHY_NOT = ""
if _WHY_NOT and os.environ.get("DAKCODER_REQUIRE_INTEGRATION"):
    raise RuntimeError(f"DAKCODER_REQUIRE_INTEGRATION is set but this cannot run: {_WHY_NOT}")

pytestmark = pytest.mark.skipif(bool(_WHY_NOT), reason=_WHY_NOT)

_FILES: dict[str, str] = {
    "go.mod": "module example.test/svc\n\ngo 1.25\n",
    "main.go": (
        'package main\n\nimport (\n\t"fmt"\n\n\t"example.test/svc/core/domain"\n)\n\n'
        "func main() { fmt.Println(domain.Acct{}) }\n"
    ),
    "core/port/meta.go": (
        "package port\n\n// MetadataRequest pages a list.\n"
        "type MetadataRequest struct {\n\tSkip  int\n\tLimit int\n}\n"
    ),
    # pao-backend's direction: domain imports port.
    "core/domain/accts.go": (
        'package domain\n\nimport "example.test/svc/core/port"\n\n'
        "// Acct is a public account.\ntype Acct struct {\n\tMeta port.MetadataRequest\n}\n"
    ),
}

#: What the field run wrote at turn 21: a port file importing domain.
_CYCLIC_PORT = (
    'package port\n\nimport "example.test/svc/core/domain"\n\n'
    "// EmployeeRequest creates an employee.\n"
    "type EmployeeRequest struct {\n\tAcct domain.Acct\n}\n"
)
_UNCYCLE = {
    "path": "core/port/employee.go",
    "old": 'import "example.test/svc/core/domain"\n\n',
    "new": "",
}
_UNCYCLE_FIELD = {"path": "core/port/employee.go", "old": "Acct domain.Acct", "new": "ID string"}


@pytest.fixture
def service(tmp_path: Path) -> Path:
    root = tmp_path / "svc"
    for rel, body in _FILES.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8", newline="")
    return root


@pytest.fixture
def router(service: Path):
    with GoTools(service) as sidecar:
        yield Router(
            Workspace.at(service),
            {
                **fs.HANDLERS,
                **knowledge.HANDLERS,
                **commands.HANDLERS,
                **control.HANDLERS,
                **handlers_for(sidecar),
            },
        )


def _loop(router: Router, turns, *, max_turns: int = 12) -> AgentLoop:
    return AgentLoop(
        ContextManager(mode=Mode.ASK, system_prompt="You are dakcoder."),
        ScriptedClient(turns),
        router,
        approve=lambda _r: True,
        max_turns=max_turns,
    )


def _plan(*files: str):
    return calls(
        (
            "submit_plan",
            json.dumps(
                {
                    "summary": "employee API",
                    "steps": [
                        {"file": f, "action": f"write {f}", "accepts": "go_build"} for f in files
                    ],
                }
            ),
        )
    )


def _write(path: str, content: str):
    return calls(("write_file", json.dumps({"path": path, "content": content})))


def _finish(answer: str = "done", blocked: str = ""):
    body = {"answer": answer, **({"blocked": blocked} if blocked else {})}
    return calls(("finish", json.dumps(body)))


def _said(agent: AgentLoop) -> str:
    return "\n".join(
        m.content or "" for m in agent.context.build() if m.role.value == "user"
    )


# ── 1. the compiler runs after the edit that breaks it ───────────────────────


def test_the_cycle_is_reported_on_the_turn_it_is_made_and_released_when_fixed(
    router: Router,
) -> None:
    agent = _loop(
        router,
        [
            _plan("core/port/employee.go"),
            _write("core/port/employee.go", _CYCLIC_PORT),
            calls(("patch_file", json.dumps(_UNCYCLE)), ("patch_file", json.dumps(_UNCYCLE_FIELD))),
            _finish(),
            _finish(),
        ],
    )
    events = list(agent.run("add an employee request DTO", intent=Intent.AGENT))

    inner = [e.data for e in events if e.type is EventType.GATE and e.data.get("kind") == "inner"]
    compiled = [next(s for s in g["stages"] if s["name"] == "go_compile") for g in inner]
    # The write that closed the cycle is the batch the compiler objected to.
    assert not compiled[0]["ok"], compiled[0]
    assert "import cycle not allowed" in compiled[0]["content"]
    assert "core/port/employee.go:3:1" in compiled[0]["content"]
    assert "core/domain/accts.go:3:1" in compiled[0]["content"]
    assert "core/port/employee.go" in _said(agent)
    # The fix compiles, and the run ends clean.
    assert compiled[-1]["ok"], compiled[-1]
    assert agent.state.last_build is None
    assert agent.result is not None and agent.result.outcome is Outcome.DONE, agent.result


def test_a_package_the_service_never_compiled_is_not_news_after_every_edit(
    router: Router, service: Path
) -> None:
    """The baseline excuses it, as it excuses the gate's build."""
    (service / "core/port/broken.go").write_text(
        "package port\n\nvar _ = undefinedThing\n", encoding="utf-8"
    )
    agent = _loop(
        router,
        [
            _plan("core/port/employee.go"),
            _write("core/port/employee.go", "package port\n\n// ID names one.\ntype ID string\n"),
            _finish(),
        ],
    )
    list(agent.run("add an employee id", intent=Intent.AGENT))

    assert "undefinedThing" not in _said(agent)
    assert agent.state.last_build is None


# ── 2. repo_map says which way the service's imports point ──────────────────


def test_repo_map_carries_the_import_direction_and_names_a_cycle(
    router: Router, service: Path
) -> None:
    out = router.dispatch("repo_map", {}, mode=Mode.PLANNER)
    payload = json.loads(out.content)
    assert payload["imports"]["core/domain"] == ["core/port"]
    assert "core/port" not in payload["imports"]
    assert "import_cycles" not in payload

    (service / "core/port/employee.go").write_text(_CYCLIC_PORT, encoding="utf-8")
    payload = json.loads(router.dispatch("repo_map", {}, mode=Mode.PLANNER).content)
    assert payload["import_cycles"] == ["core/domain <-> core/port"]


# ── 3. a plan step naming a generated file plans the file that is written ────


def test_a_step_naming_a_generated_validator_becomes_the_request_file(router: Router) -> None:
    agent = _loop(
        router,
        [_plan("core/port/employee.go", "handler/request_employee_validator.go"), say("ok")],
        max_turns=3,
    )
    list(agent.run("add employee validation", intent=Intent.AGENT))

    files = [s.file for s in agent.state.plan]
    assert "handler/request_employee_validator.go" not in files
    assert REQUEST_DTO_FILE in files
    step = next(s for s in agent.state.plan if s.file == REQUEST_DTO_FILE)
    assert "gate generates" in step.note


# ── 4. the exits the run is told to take are taken ──────────────────────────


def test_a_blocked_revision_past_the_cap_is_taken_and_keeps_the_rest_of_the_plan(
    router: Router,
) -> None:
    revise = calls(
        (
            "revise_plan",
            json.dumps(
                {
                    "reason": "the table schema has not been confirmed",
                    "steps": [
                        {
                            "file": "core/port/employee.go",
                            "action": "write it",
                            "accepts": "go_build",
                            "status": "blocked",
                            "note": "the table schema has not been confirmed",
                        }
                    ],
                }
            ),
        )
    )
    agent = _loop(router, [revise, _finish("stopped", blocked="schema")], max_turns=4)
    agent.state.plan = (
        PlanStep("core/port/employee.go", "write it", "go_build"),
        PlanStep("handler/employee.go", "write it", "go_build"),
    )
    agent.state.revisions = MAX_REVISIONS
    list(agent.run("carry on", intent=Intent.AGENT, continued=True))

    said = _said(agent)
    assert "was not adopted" not in said
    by_file = {s.file: s for s in agent.state.plan}
    assert by_file["core/port/employee.go"].status == "blocked"
    assert "schema" in by_file["core/port/employee.go"].note
    assert by_file["handler/employee.go"].status == "pending", "the revision dropped a step"


def test_deleting_a_file_the_task_created_is_not_a_loss(router: Router) -> None:
    """The field run created `handler/request/request.go`, deleted it, and was
    told to write it back -- then had `finish` refused because it was gone."""
    agent = _loop(
        router,
        [
            _plan("core/port/employee.go"),
            _write("core/port/employee.go", "package port\n\n// ID names one.\ntype ID string\n"),
            _write("handler/request/request.go", "package request\n"),
            calls(("delete_file", json.dumps({"path": "handler/request/request.go", "reason": "unused"}))),
            _finish(),
            _finish(),
        ],
    )
    list(agent.run("add an employee id", intent=Intent.AGENT))

    said = _said(agent)
    assert "You deleted handler/request/request.go" not in said
    assert "simply gone" not in said
    assert agent.result is not None and agent.result.outcome is Outcome.DONE, agent.result


def test_deleting_a_file_that_was_there_before_the_task_is_still_a_loss(router: Router) -> None:
    """The guard the previous test must not have removed."""
    agent = _loop(
        router,
        [
            _plan("core/port/employee.go"),
            calls(("delete_file", json.dumps({"path": "core/port/meta.go", "reason": "tidy"}))),
            _finish(),
            _finish(),
        ],
    )
    list(agent.run("tidy the port package", intent=Intent.AGENT))

    assert "You deleted core/port/meta.go" in _said(agent)


def test_a_settle_repeated_word_for_word_does_not_loop(router: Router) -> None:
    """Bounded: the call ledger answers the identical repeat without re-running
    it, the repeat guard fires, and the run ends -- with the step settled."""
    revise = calls(
        (
            "revise_plan",
            json.dumps(
                {
                    "reason": "cannot",
                    "steps": [
                        {
                            "file": "core/port/employee.go",
                            "action": "write it",
                            "accepts": "go_build",
                            "status": "blocked",
                            "note": "cannot",
                        }
                    ],
                }
            ),
        )
    )
    agent = _loop(router, [revise] * 12, max_turns=12)
    agent.state.plan = (PlanStep("core/port/employee.go", "write it", "go_build"),)
    list(agent.run("carry on", intent=Intent.AGENT, continued=True))

    assert agent.result is not None and agent.result.outcome is Outcome.DONE, agent.result
    assert agent.context.turn <= 5, "a repeated settle ran on through the turn budget"
    assert agent.state.plan[0].status == "blocked"


# ── 5. a pre-existing finding the model meets on its own is labelled as such ─


def test_a_vet_finding_the_service_already_had_is_labelled_not_this_tasks(
    router: Router, service: Path
) -> None:
    """Live replay of 05cd8b8369aa: asked only for an AGENTS.md, the model ran
    `go vet ./...` itself, met the service's old struct-tag typo, and patched a
    file the task never named. The baseline knew; the result did not say."""
    (service / "core/domain/te.go").write_text(
        "package domain\n\n// TE is a transfer entry.\ntype TE struct {\n"
        '\tDate string `json\t:"trans_date"`\n}\n',
        encoding="utf-8",
    )
    agent = _loop(
        router,
        [
            _plan("AGENTS.md"),
            _write("AGENTS.md", "# Notes\n"),
            calls(("go_vet", json.dumps({"pattern": "./..."}))),
            _finish(),
            _finish(),
        ],
    )
    list(agent.run("write an AGENTS.md", intent=Intent.AGENT))

    vet = next(
        m.content or "" for m in agent.context.build()
        if m.role.value == "tool" and "struct field tag" in (m.content or "")
    )
    assert "already failing before this run" in vet
    assert agent.result is not None and agent.result.outcome is Outcome.DONE, agent.result


# ── 6. a planner that describes its plan is asked for the plan ──────────────


def test_a_planner_finish_describing_the_work_is_sent_back_for_the_plan(router: Router) -> None:
    """05cd8b8369aa turn 6, and the live replay: the planner ended with "I will
    create AGENTS.md ..." and nothing was written until the developer typed
    "write it". Now that costs one turn, not a round trip to the developer."""
    agent = _loop(
        router,
        [
            _finish("I surveyed the repository and will create AGENTS.md with a project overview."),
            _plan("AGENTS.md"),
            _write("AGENTS.md", "# Notes\n"),
            _finish(),
            _finish(),
        ],
    )
    list(agent.run("Create AGENTS.md at the repository root", intent=Intent.AGENT))

    assert "call `submit_plan` with what you just described" in _said(agent)
    assert (Path(router.workspace.root) / "AGENTS.md").is_file()
    assert agent.result is not None and agent.result.outcome is Outcome.DONE, agent.result


# ── 7. a file this task created is rewritten in one call, not delete + write ─


def test_a_file_the_task_created_is_rewritten_whole_without_a_delete(router: Router) -> None:
    """Live replay of 3a82e19883f7: `write_file` refused to overwrite the run's
    own new repository file, its refusal said "delete_file first", and the run
    went round delete-then-write eight times until the stall guard ended it."""
    first = router.dispatch(
        "write_file", {"path": "core/port/employee.go", "content": "package port\n\nvar A = 1\n"},
        approved=True,
    )
    second = router.dispatch(
        "write_file", {"path": "core/port/employee.go", "content": "package port\n\nvar B = 2\n"},
        approved=True,
    )
    assert first.ok and second.ok, second.content
    assert "replaced" in second.content
    assert (Path(router.workspace.root) / "core/port/employee.go").read_text().count("B = 2") == 1

    # A file that was there before the task is still not overwritten.
    kept = router.dispatch(
        "write_file", {"path": "core/port/meta.go", "content": "package port\n"}, approved=True
    )
    assert not kept.ok and "will not overwrite" in kept.content

    # And a new task starts its own ledger.
    router.begin_task()
    again = router.dispatch(
        "write_file", {"path": "core/port/employee.go", "content": "package port\n"}, approved=True
    )
    assert not again.ok


# ── 8. an undefined package name comes back with the import this service uses ─


def test_an_undefined_package_name_is_answered_with_the_services_own_import(
    router: Router, service: Path
) -> None:
    """Live replay of 3a82e19883f7, run 7: a handler used `postgres.` where
    `repo/postgres` declares `package repository`, and rewrote the whole file
    four times with the same two errors."""
    (service / "repo/postgres").mkdir(parents=True)
    (service / "repo/postgres/acct.go").write_text(
        "package repository\n\n// Repo stores accounts.\ntype Repo struct{}\n", encoding="utf-8"
    )
    (service / "handler").mkdir()
    (service / "handler/acct.go").write_text(
        'package handler\n\nimport repo "example.test/svc/repo/postgres"\n\n'
        "// H handles accounts.\ntype H struct{ R *repo.Repo }\n",
        encoding="utf-8",
    )
    router.dispatch(
        "write_file",
        {
            "path": "handler/employee.go",
            "content": "package handler\n\n// E handles employees.\ntype E struct{ R *postgres.Repo }\n",
        },
        approved=True,
    )
    out = router.run_gate_tool("go_compile", {"pattern": "./handler/..."})

    assert not out.ok
    said = out.for_model()
    assert "repo/postgres/ declares `package repository`" in said
    assert 'repo "example.test/svc/repo/postgres"' in said
    # Hints are advice, never findings: the baseline keys content alone.
    assert "declares" not in out.content


# ── 9. run 8: the cycle names its way out, and a course change is taken ─────


def test_the_cycle_advice_names_the_established_direction_and_the_file_to_fix(
    router: Router, service: Path
) -> None:
    (service / "core/domain/more.go").write_text(
        'package domain\n\nimport "example.test/svc/core/port"\n\n'
        "// More pages more.\ntype More struct{ M port.MetadataRequest }\n",
        encoding="utf-8",
    )
    (service / "core/port/employee.go").write_text(_CYCLIC_PORT, encoding="utf-8")
    out = router.run_gate_tool("go_compile", {"pattern": "./core/port/..."})

    assert not out.ok
    assert "core/domain already imports core/port in this service (2 file(s))" in out.fix
    assert "Rewrite core/port/employee.go without its import of core/domain" in out.fix


def test_a_restated_plan_that_skips_one_step_is_taken_past_the_cap(router: Router) -> None:
    """Run 8 restated the whole plan to skip the file closing the cycle, and was
    refused because three no-op restatements had spent the cap."""
    steps = [
        {"file": "core/port/employee.go", "action": "write it", "accepts": "go_build",
         "status": "skipped", "note": "types moved to the handler; port cannot import domain"},
        {"file": "handler/employee.go", "action": "write it", "accepts": "go_build"},
    ]
    revise = calls(("revise_plan", json.dumps({"reason": "import cycle", "steps": steps})))
    agent = _loop(router, [revise, _finish("stopped", blocked="test")], max_turns=4)
    agent.state.plan = (
        PlanStep("core/port/employee.go", "write it", "go_build"),
        PlanStep("handler/employee.go", "write it", "go_build"),
        PlanStep("routes/routes.go", "wire it", "go_build"),
    )
    agent.state.revisions = MAX_REVISIONS
    list(agent.run("carry on", intent=Intent.AGENT, continued=True))

    by_file = {s.file: s for s in agent.state.plan}
    assert "was not adopted" not in _said(agent)
    assert by_file["core/port/employee.go"].status == "skipped"
    assert by_file["handler/employee.go"].status == "pending"
    assert "routes/routes.go" in by_file, "a step the restatement omitted was dropped"


# ── 10. the regeneration loop: whole rewrites to fix one compile error ──────


def _missing_import(n: int) -> str:
    """A port file that uses `domain` without importing it -- the error run 9
    regenerated a whole file nine times over. ``n`` varies the bytes."""
    return (
        f"package port\n\n// V{n} is version {n}.\n"
        f"type V{n} struct{{ A domain.Acct }}\n"
    )


def test_identical_and_repeated_whole_rewrites_of_a_failing_file_are_refused(
    router: Router,
) -> None:
    agent = _loop(
        router,
        [
            _plan("core/port/employee.go"),
            _write("core/port/employee.go", _missing_import(1)),
            _write("core/port/employee.go", _missing_import(2)),
            # Byte for byte the first version: refused, nothing can change.
            _write("core/port/employee.go", _missing_import(1)),
            # A third distinct whole rewrite while the compiler still fails:
            # refused with the errors and `patch_file` named.
            _write("core/port/employee.go", _missing_import(3)),
            _finish("stopping", blocked="test"),
            _finish("stopping", blocked="test"),
        ],
        max_turns=10,
    )
    list(agent.run("add an employee port type", intent=Intent.AGENT))

    said = "\n".join(
        m.content or "" for m in agent.context.build() if m.role.value == "tool"
    )
    assert "byte for byte what you wrote to core/port/employee.go" in said
    assert "has been written whole 2 times" in said
    assert "patch_file" in said
    assert "undefined: domain" in said


def test_the_rewrite_refusal_is_bounded(router: Router) -> None:
    """Advice with teeth, never a wall: past MAX_REWRITE_REFUSALS it is taken."""
    from dakcoder_agent.loop import MAX_REWRITE_REFUSALS

    writes = [_write("core/port/employee.go", _missing_import(n)) for n in range(1, 4 + MAX_REWRITE_REFUSALS)]
    agent = _loop(router, [_plan("core/port/employee.go"), *writes, _finish("x", blocked="t"), _finish("x", blocked="t")], max_turns=12)
    list(agent.run("add an employee port type", intent=Intent.AGENT))

    assert agent.state.rewrite_refused["core/port/employee.go"] == MAX_REWRITE_REFUSALS
    body = (Path(router.workspace.root) / "core/port/employee.go").read_text(encoding="utf-8")
    assert f"V{3 + MAX_REWRITE_REFUSALS}" in body, "the write past the bound was not taken"


def test_delete_then_rewrite_the_same_bytes_is_refused_too(router: Router) -> None:
    """The same loop in its older shape, before task-created files could be
    overwritten: delete, then write back what had already failed."""
    agent = _loop(
        router,
        [
            _plan("core/port/employee.go"),
            _write("core/port/employee.go", _missing_import(1)),
            calls(("delete_file", json.dumps({"path": "core/port/employee.go", "reason": "redo"}))),
            _write("core/port/employee.go", _missing_import(1)),
            _finish("x", blocked="t"),
            _finish("x", blocked="t"),
        ],
        max_turns=8,
    )
    list(agent.run("add an employee port type", intent=Intent.AGENT))

    said = "\n".join(m.content or "" for m in agent.context.build() if m.role.value == "tool")
    assert "byte for byte what you wrote" in said
