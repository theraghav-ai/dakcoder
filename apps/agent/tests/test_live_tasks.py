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
import json
from unittest import mock

from dakcoder_agent import loop as loop_module
from dakcoder_agent.loop import (
    MAX_PREAMBLE_REFUSALS,
    AgentLoop,
    Intent,
    Outcome,
    _is_preamble as is_preamble,
)
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


# ── the field failure, end to end ───────────────────────────────────────────

#: A plan document and a service that plainly does not match it. Small on
#: purpose: the field run met 1,800-line handlers and spent its whole budget
#: reading them, and what is being tested here is the *decision* at the end of
#: the reading, not how long the reading takes.
_PLAN_DOC = """# Migration plan

1. Swap every api-* import for its n-api-* equivalent, and drop gin.
2. Delete routes/routes.go; each handler declares its own routes.
3. Handlers take (sctx *route.Context, req T) and return (*resp.R, error).
4. Repositories take context.Context, not *gin.Context.
"""

_LEGACY = {
    "MIGRATION_PLAN.md": _PLAN_DOC,
    "routes/routes.go": (
        "package routes" + chr(10) * 2
        + "func Routes(r *gin.Engine, h *handler.UserHandler) {" + chr(10)
        + chr(9) + "r.GET(" + chr(34) + "/users" + chr(34) + ", h.List)" + chr(10)
        + "}" + chr(10)
    ),
    "handler/objection.go": (
        "package handler" + chr(10) * 2
        + "func (h *ObjectionHandler) Create(ctx *gin.Context) {}" + chr(10) * 2
        + "func (h *ObjectionHandler) List(ctx *gin.Context) {}" + chr(10)
    ),
}


@pytest.fixture
def legacy(workspace: Workspace) -> Workspace:
    for rel, body in _LEGACY.items():
        path = workspace.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8", newline="")
    return workspace


def _drive(client, workspace, task, intent, *, fence: int | None = None):
    """Run one task, returning the events that say what the developer got.

    ``fence`` lowers `MAX_RESEARCH_TURNS` for the run. The fixture is nine
    files; the field service was fifteen with 1,800-line handlers, and what is
    under test is the *decision at the fence*, not how many turns of reading it
    takes to get there. Lowering it puts the real model in front of the real
    fence with the real prompts, which is the integration that matters.
    """
    handlers = {**fs.HANDLERS, **knowledge.HANDLERS, **commands.HANDLERS, **control.HANDLERS}
    router = Router(workspace, handlers)
    context = ContextManager(mode=Mode.ASK, system_prompt=system_prompt())
    loop = AgentLoop(context, client, router, approve=lambda _r: True, max_turns=MAX_TURNS)

    said: list[str] = []
    finishes: list[str] = []
    with mock.patch.object(loop_module, "MAX_RESEARCH_TURNS", fence or loop_module.MAX_RESEARCH_TURNS):
        for event in loop.run(task, intent=intent):
            if event.type is EventType.ASSISTANT:
                said.append(str(event.data.get("text", "")))
            elif event.type is EventType.TOOL_CALL and event.data.get("name") == "finish":
                args = event.data.get("arguments")
                if isinstance(args, str):
                    try:
                        args = json.loads(args)
                    except ValueError:
                        args = {}
                finishes.append(str((args or {}).get("answer", "")))
    return {"loop": loop, "said": said, "finishes": finishes, "result": loop.result}


#: Trials per behavioural claim below. These assert a *rate*, not an outcome:
#: the runs vary, the fence decision is a model decision, and a single-sample
#: assertion on one would be a coin flip dressed as a test. Same reasoning as
#: `test_live_endpoint.SAMPLES`.
FENCE_TRIALS = int(os.environ.get("DAKCODER_FENCE_TRIALS", "3"))


def test_a_validation_routed_as_work_is_not_turned_into_one(client, legacy):
    """The 2026-09-09 field failure, against the live endpoint.

    The developer asked the agent to *validate* a migration plan. The run was
    routed to the Planner -- the classifier reads "migration" as work, and the
    panel toggle can say so outright -- read to the research fence, and was
    offered exactly one legal move: `submit_plan`. So it wrote an eight-step
    migration nobody asked for, and every enforcement path downstream held it
    to that plan. Both field runs ended the same way: no answer, and in one of
    them the agent phase began executing the migration.

    `Intent.AGENT` is passed deliberately. Fixing the classifier is worth doing
    and is not what this asserts: the run has to survive being routed wrongly.

    What is measured is what the developer gets. The fence is lowered because
    the fixture is nine files and the field service was fifteen with 1,800-line
    handlers; the decision at the fence is the subject, not the reading.

    Measured 2026-09-09, 15 runs: 11 delivered a real answer and changed
    nothing, against a field baseline of 0 for 2. The residual is the misroute
    itself. Nothing pushes a forced plan any more, but nothing stops a model
    that decides to work it either -- the acting overlay says "make the edit"
    -- and that is the classifier's to prevent, not this guard's.
    """
    task = "read MIGRATION_PLAN.md and validate it against the codebase. every file"
    good, notes = 0, []
    for _ in range(FENCE_TRIALS):
        out = _drive(client, legacy, task, Intent.AGENT, fence=4)
        loop, result = out["loop"], out["result"]
        delivered = out["finishes"][-1] if out["finishes"] else ""
        clean = (
            not loop.router.touched
            and result.outcome is not Outcome.NO_PROGRESS
            and len(delivered) > 500
            and not is_preamble(delivered)
        )
        good += clean
        notes.append(
            f"{result.outcome}/{result.turns}t/{len(loop.router.touched)}f/"
            f"{len(delivered)}ch"
        )
        # Whatever it decided, a forced plan must never be enforced against a
        # run that wrote nothing. That part is deterministic.
        if loop.state.plan_forced and not loop.router.touched:
            assert not loop._open_targets(), f"a forced plan is being enforced: {notes}"

    assert good > FENCE_TRIALS // 2, (
        f"only {good}/{FENCE_TRIALS} runs delivered an answer without doing "
        f"unrequested work: {notes}"
    )


def test_a_change_task_at_the_fence_still_writes_the_code(client, legacy):
    """The control, and it is the one that must not regress.

    Two interventions aimed at the validation case were tried here and both
    were reverted on this measurement. Asking the model to reconsider before
    its first write took change tasks from 4/4 to 2/4. Narrowing `plan_forced`
    to the verdict alone protected change (5/5) and took validation to 0/5.

    Measured 2026-09-09, 17 runs of the shipped configuration: 14 wrote the
    code, including a clean 6/6.
    """
    task = "add a LastName string field to the User domain model and to CreateUserRequest"
    wrote, notes = 0, []
    for _ in range(FENCE_TRIALS):
        out = _drive(client, legacy, task, Intent.AGENT, fence=4)
        loop = out["loop"]
        wrote += bool(loop.router.touched)
        notes.append(f"{out['result'].outcome}/{sorted(loop.router.touched)}")

    assert wrote > FENCE_TRIALS // 2, (
        f"only {wrote}/{FENCE_TRIALS} change tasks wrote anything: {notes}"
    )
