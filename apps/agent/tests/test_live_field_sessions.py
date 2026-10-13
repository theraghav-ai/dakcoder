"""The field sessions 05cd8b8369aa and 3a82e19883f7, against the live model.

`test_field_05cd8b8369aa.py` and `test_field_3a82e19883f7.py` script the model,
which proves the harness answers each field move correctly. They cannot prove
the real model makes those moves, or that the fixes hold when it makes others.
This drives the hosted model through the gateway, on a fresh copy of the
`pao-back-end-development` corpus -- a real legacy service, with the same
`core/domain -> core/port` direction the field run broke -- with the real
sidecar, the real toolchain and every approval granted.

One session, two messages, as in the field: the AGENTS.md task, then the
employee CRUD API as a new task. Asserted on outcomes, never on prose: what
changed on disk, whether it compiles, how many turns, and how often the loop
had to push back.

    set -a; . deploy/dakcoder.env; set +a
    DAKCODER_LIVE=1 python -m pytest apps/agent/tests/test_live_field_sessions.py -v -s

Each run's events are written to ``<tmp>/events-*.jsonl`` and the path printed,
so a failure can be read turn by turn.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from dakcoder_agent.context import ContextManager
from dakcoder_agent.loop import AgentLoop, Intent, Outcome
from dakcoder_agent.modes import Mode
from dakcoder_agent.prompts import system_prompt
from dakcoder_agent.tools import agents_md, commands, control, fs, knowledge
from dakcoder_agent.tools.gotools import GoTools, _find_binary, handlers_for
from dakcoder_agent.tools.imports import import_summary
from dakcoder_agent.tools.router import Router
from dakcoder_shared.config import local_config
from dakcoder_shared.envelope import EventType
from dakcoder_shared.llm import LLMClient
from dakcoder_shared.paths import Workspace

GATEWAY = os.environ.get("DAKCODER_GATEWAY_URL", "https://ai.cept.gov.in/dakcoder")
CORPUS = Path(__file__).resolve().parents[3] / "pao-back-end-development"

pytestmark = [
    pytest.mark.skipif(
        os.environ.get("DAKCODER_LIVE") != "1",
        reason="live field replay; set DAKCODER_LIVE=1 and DAKCODER_JWT to run",
    ),
    pytest.mark.skipif(not CORPUS.is_dir(), reason="pao-back-end-development is not in this checkout"),
    pytest.mark.skipif(
        _find_binary() is None or shutil.which("go") is None,
        reason="needs the gotools sidecar and the Go toolchain",
    ),
]

#: The field session's first message, verbatim.
AGENTS_TASK = (Path(__file__).parent / "data" / "agents_md_task.txt")

#: The second, with the developer's answers to the two rounds of questions
#: folded in -- the questions are the model's to ask, and answering them by
#: script would be testing the script.
CRUD_TASK = (
    "i want to write new apis: all CRUD, as a REST API, for a new employee_details "
    "table. Include all the usual details on an employee, decide the primary key "
    "yourself, and create the new table's schema."
)

#: Generous: the field run took 54 turns to fail. A run that needs more than
#: this on a fifteen-file service is spinning, whatever it says.
MAX_TURNS = 80


@pytest.fixture(scope="module")
def client() -> LLMClient:
    jwt = os.environ.get("DAKCODER_JWT", "").strip()
    if not jwt:
        pytest.skip("DAKCODER_JWT is not set")
    live = LLMClient(local_config(GATEWAY, jwt))
    yield live
    live.close()


@pytest.fixture
def service(tmp_path: Path) -> Path:
    work = tmp_path / "pao"
    shutil.copytree(CORPUS, work, ignore=shutil.ignore_patterns(".git", ".dakcoder", "*.exe"))
    subprocess.run(["git", "init", "-q"], cwd=work, check=True)
    subprocess.run(["git", "add", "-A"], cwd=work, check=True)
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "corpus"],
        cwd=work,
        check=True,
    )
    return work


def _developer(request) -> bool:
    """Approves everything except a commit, which neither task asked for.

    Approving that too let the live model commit AGENTS.md on its own, which
    in the product the developer is asked about first."""
    return not (request.tool == "git_ops" and (request.arguments or {}).get("op") == "commit")


def _loop(client: LLMClient, router: Router, root: Path) -> AgentLoop:
    return AgentLoop(
        ContextManager(mode=Mode.ASK, system_prompt=system_prompt()),
        client,
        router,
        approve=_developer,
        max_turns=MAX_TURNS,
        project_docs=lambda: agents_md.load(root),
    )


def _drive(agent: AgentLoop, task: str, log: Path, **kw) -> list:
    events = []
    with log.open("w", encoding="utf-8") as out:
        for event in agent.run(task, **kw):
            events.append(event)
            out.write(json.dumps({"type": event.type.value, "data": event.data}, default=str) + "\n")
    print(f"\n[{task[:40]!r}] events: {log}  outcome: {agent.result}")
    return events


def _pushbacks(agent: AgentLoop, phrase: str) -> int:
    return sum(
        phrase in (m.content or "") for m in agent.context.build() if m.role.value == "user"
    )


def _changed(root: Path) -> set[str]:
    """Every path that differs from the corpus commit, committed since or not."""
    def git(*args: str) -> list[str]:
        done = subprocess.run(["git", *args], cwd=root, capture_output=True, text=True, check=True)
        return [line.strip() for line in done.stdout.splitlines() if line.strip()]

    corpus = git("rev-list", "--max-parents=0", "HEAD")[0]
    paths = {
        *git("diff", "--name-only", corpus),
        *git("ls-files", "--others", "--exclude-standard"),
    }
    # Not a build output: run 6 verified the Makefile's `go build -o GO_FRAME`
    # by running it, which leaves a binary that is not a change to the service.
    return {p for p in paths if not _binary(root / p)}


def _binary(path: Path) -> bool:
    try:
        with path.open("rb") as handle:
            return b"\0" in handle.read(4096)
    except OSError:
        return False


def test_the_two_field_tasks_end_done_on_a_service_that_still_builds(
    client: LLMClient, service: Path, tmp_path: Path
) -> None:
    go_mod = (service / "go.mod").read_bytes()
    with GoTools(service) as sidecar:
        router = Router(
            Workspace.at(service),
            {
                **fs.HANDLERS,
                **knowledge.HANDLERS,
                **commands.HANDLERS,
                **control.HANDLERS,
                **agents_md.HANDLERS,
                **handlers_for(sidecar),
            },
        )

        # -- message 1: AGENTS.md (05cd8b8369aa) ------------------------------
        first = _loop(client, router, service)
        events = _drive(
            first, AGENTS_TASK.read_text(encoding="utf-8"), tmp_path / "events-agents.jsonl",
            intent=Intent.AGENT,
        )
        assert first.result is not None
        assert first.result.outcome is Outcome.DONE, first.result
        assert _changed(service) == {"AGENTS.md"}, _changed(service)
        assert (service / "go.mod").read_bytes() == go_mod
        for gate in (e.data for e in events if e.type is EventType.GATE and e.data.get("kind") == "full"):
            assert all(s["skipped"] for s in gate["stages"]), gate

        # -- message 2: the employee CRUD API, a new task (3a82e19883f7) ------
        second = _loop(client, router, service)
        second.context = first.context
        second.carry_from(first)
        _drive(second, CRUD_TASK, tmp_path / "events-crud.jsonl", continued=True, intent=Intent.AGENT)

    assert second.result is not None
    built = subprocess.run(["go", "build", "./..."], cwd=service, capture_output=True, text=True)
    summary = import_summary(service)
    report = (
        f"outcome={second.result.outcome} turns={second.context.turn}\n"
        f"summary={second.result.summary}\nbuild={built.returncode} {built.stderr[:600]}\n"
        f"cycles={summary.get('import_cycles')} changed={sorted(_changed(service))}"
    )
    print(report)

    # The field failure, outcome by outcome.
    assert "import_cycles" not in summary, report
    assert built.returncode == 0, report
    assert second.result.outcome is Outcome.DONE, report
    # Done means the API exists, not that what was written compiles: run 5
    # ended "done, gate clean" with four of eight planned files never written.
    changed = _changed(service)
    assert any(p.startswith("repo/postgres/") for p in changed), report
    assert any(p.startswith("handler/") and p.endswith(".go") for p in changed), report
    assert "routes/routes.go" in changed, report
    assert "plan is not finished" not in second.result.summary, report
    assert second.context.turn - first.context.turn <= MAX_TURNS, report
    # The push-backs that drove the field run in circles: each may be said, but
    # none may be said over and over.
    assert _pushbacks(second, "was not adopted") <= 1, report
    assert _pushbacks(second, "simply gone") == 0, report
    assert not any(p.endswith("_validator.go") and "/request_" not in p for p in _changed(service)), report
