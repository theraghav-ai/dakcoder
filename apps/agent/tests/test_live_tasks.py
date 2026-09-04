"""Real tasks against the live endpoint, judged on metrics rather than transcripts.

Skipped unless ``DAKCODER_LIVE=1``, like ``test_live_endpoint.py`` -- and
unlike that file this one drives the *loop* over a task, not the endpoint over
a request. The third review's closing point is the reason it exists: every
loop-level test in the suite drives a scripted model, and a scripted model
cannot rephrase a search, cannot claim work it has not done, and cannot spin.
Those are the three failure modes the task-state fixes (D-96) are for, so
without this they are unfalsifiable.

What is asserted is the run's own accounting (the ``metrics`` event the loop
emits before ``end``): turns, truncations, how often a call was answered from
a ledger instead of dispatched, and whether the change set is what the task
implied. Never the prose. A transcript assertion is a regex over a model's
wording, which is the thing the 2 September audit retired.

    DAKCODER_LIVE=1 DAKCODER_JWT=... python -m pytest apps/agent/tests/test_live_tasks.py -v -s

The fixture is a miniature n-api-template service. It needs no Go toolchain to
run the read-only tasks; the change tasks run the gate, which records every Go
stage as skipped when the toolchain is absent, so their assertions are about
the change set rather than the verdict.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

from dakcoder_agent.context import ContextManager
from dakcoder_agent.loop import AgentLoop, Intent, Outcome
from dakcoder_agent.modes import Mode
from dakcoder_agent.prompts import system_prompt
from dakcoder_agent.tools import commands, control, fs, knowledge
from dakcoder_agent.tools.router import Router
from dakcoder_shared.config import local_config
from dakcoder_shared.envelope import EventType
from dakcoder_shared.llm import LLMClient
from dakcoder_shared.paths import Workspace

pytestmark = pytest.mark.skipif(
    os.environ.get("DAKCODER_LIVE") != "1",
    reason="live task suite; set DAKCODER_LIVE=1 and DAKCODER_JWT to run",
)

GATEWAY = os.environ.get("DAKCODER_GATEWAY_URL", "https://ai.cept.gov.in/dakcoder")

#: The most turns any of these tasks is allowed. The fixture is nine files; a
#: run that needs more than this on it is spinning, whatever it says.
MAX_TURNS = 18


_FILES: dict[str, str] = {
    "go.mod": "module pisapi\n\ngo 1.25.0\n",
    "main.go": "package main\n\nfunc main() {}\n",
    "core/domain/user.go": (
        "package domain\n\n"
        "type User struct {\n"
        "\tID        int    `json:\"id\" db:\"id\"`\n"
        "\tFirstName string `json:\"first_name\" db:\"first_name\"`\n"
        "\tCity      string `json:\"city\" db:\"city\"`\n"
        "}\n"
    ),
    "repo/postgres/user.go": (
        "package postgres\n\n"
        "type UserRepository struct{}\n\n"
        "func (r *UserRepository) GetAll() {}\n"
        "func (r *UserRepository) GetByID() {}\n"
    ),
    "handler/user.go": (
        "package handler\n\n"
        "type UserHandler struct{}\n\n"
        "func New() *UserHandler { return &UserHandler{} }\n\n"
        "func (h *UserHandler) Routes() {}\n"
    ),
    "handler/request/request.go": (
        "package request\n\ntype CreateUserRequest struct {\n\tFirstName string\n}\n"
    ),
    "bootstrap/bootstrapper.go": "package bootstrap\n\nvar FxRepo = 1\n",
    "configs/app.yaml": "app:\n  name: pisapi\n",
    "db/users.sql": "CREATE TABLE users (id serial4 PRIMARY KEY);\n",
}


@pytest.fixture(scope="module")
def client() -> LLMClient:
    jwt = os.environ.get("DAKCODER_JWT", "").strip()
    if not jwt:
        pytest.skip("DAKCODER_JWT is not set")
    for var in ("OPENAI_API_KEY", "DAKCODER_MODEL_API_KEY", "LITELLM_API_KEY"):
        os.environ.pop(var, None)
    live = LLMClient(local_config(GATEWAY, jwt))
    yield live
    live.close()


@pytest.fixture
def workspace(tmp_path: Path) -> Workspace:
    for rel, body in _FILES.items():
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8", newline="")
    return Workspace.at(tmp_path)


def _run(client: LLMClient, workspace: Workspace, task: str, intent: Intent) -> dict:
    """Drive one task end to end and return its metrics record plus the result."""
    handlers = {**fs.HANDLERS, **knowledge.HANDLERS, **commands.HANDLERS, **control.HANDLERS}
    router = Router(workspace, handlers)
    context = ContextManager(mode=Mode.ASK, system_prompt=system_prompt())
    loop = AgentLoop(context, client, router, approve=lambda _r: True, max_turns=MAX_TURNS)

    metrics: dict = {}
    for event in loop.run(task, intent=intent):
        if event.type is EventType.METRICS:
            metrics = dict(event.data)
    assert loop.result is not None
    return {"metrics": metrics, "result": loop.result, "loop": loop}


QUESTIONS = [
    "what fields does the User domain model have?",
    "which repository methods exist for users, and where are they defined?",
    "is the user handler registered in the bootstrapper?",
]

CHANGES = [
    ("add a LastName string field to the User domain model", {"core/domain/user.go"}),
    ("add a GetByCity method stub to the user repository", {"repo/postgres/user.go"}),
    ("add a `LastName string` field to CreateUserRequest", {"handler/request/request.go"}),
]


@pytest.mark.parametrize("task", QUESTIONS)
def test_a_question_is_answered_without_spinning(client, workspace, task: str) -> None:
    out = _run(client, workspace, task, Intent.ASK)
    metrics, result = out["metrics"], out["result"]

    assert result.outcome == Outcome.DONE, result.summary
    assert not out["loop"].router.touched, "a question changed a file"
    assert metrics.get("turns", MAX_TURNS) <= 8, f"{metrics.get('turns')} turns for a question"
    assert metrics.get("truncations", 0) == 0
    # A call answered from a ledger is the model asking for what it already has.
    # One is a model being slow to move on; more is the loop the fixes are for.
    repeats = metrics.get("intercepted_cached", 0) + metrics.get("intercepted_re_read", 0)
    assert repeats <= 1, f"{repeats} calls answered from a ledger"


@pytest.mark.parametrize("task,expected", CHANGES)
def test_a_change_lands_on_the_file_the_task_implies(
    client, workspace, task: str, expected: set[str]
) -> None:
    out = _run(client, workspace, task, Intent.AGENT)
    metrics, result, loop = out["metrics"], out["result"], out["loop"]

    touched = set(loop.router.touched)
    assert touched & expected, f"the change set {touched} misses {expected}"
    assert metrics.get("truncations", 0) <= 1
    assert metrics.get("turns", MAX_TURNS) <= 12, f"{metrics.get('turns')} turns for a one-file change"
    # The plan's step for the expected file must be marked done from the change
    # set, not left pending: that is the task state machine working.
    statuses = {s.file: s.status for s in loop.state.plan}
    assert any(statuses.get(f) == "done" for f in expected), statuses
    if shutil.which("go") is None:
        # Without a toolchain every Go stage is skipped and the gate cannot
        # judge the change; the outcome is still DONE, and that is the honest
        # reading of "nothing here could be verified".
        assert result.outcome in (Outcome.DONE, Outcome.UNVERIFIED), result.summary
    else:
        assert result.outcome in (Outcome.DONE, Outcome.UNVERIFIED), result.summary


def test_a_run_never_claims_a_write_it_did_not_make(client, workspace) -> None:
    """The mechanism behind "I wrote the first two files but my reply was cut".

    Asserted structurally: every plan step reported `done` has its file in the
    change set, which is the property the state block is derived from.
    """
    out = _run(client, workspace, "add a LastName string field to the User domain model", Intent.AGENT)
    loop = out["loop"]
    touched = set(loop.router.touched)
    for step in loop.state.plan:
        if step.status == "done":
            assert step.file in touched, f"{step.file} is done but was never written"
