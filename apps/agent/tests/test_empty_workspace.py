"""One field session: f4d8b5061130, four turns, `answered; blocked`.

A caller pasted a Go handler with three syntax errors and named no repository,
so the service ran it on a scratch workspace -- an empty directory. The system
prompt already said code pasted into the message counts as opened. The run
called `repo_map` (``"files":0``), then `search_repo` twice ("It is not in this
workspace."), was told to stop searching and say what it could not find, and
answered that it could not help without the repository. It fixed nothing.

Every signal the run had came from its tools and pointed the wrong way, so the
fix is in what the tools, and the pinned task, say about an empty tree.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from dakcoder_shared.paths import Workspace

from dakcoder_agent.modes import Mode
from dakcoder_agent.tools import fs, gotools
from dakcoder_agent.tools.fs import EMPTY_WORKSPACE, workspace_empty
from dakcoder_agent.tools.router import Router
from scripted import build, calls, forced_args  # noqa: E402

SNIPPET = "currentTime  time.Now()"


@pytest.fixture
def scratch(tmp_path: Path) -> Workspace:
    """A scratch lease as the service leaves it after one run: only journals."""
    (tmp_path / ".dakcoder" / "sessions").mkdir(parents=True)
    return Workspace.at(tmp_path)


@pytest.fixture
def scratch_router(scratch: Workspace, sidecar) -> Router:
    sidecar.answer("repo_map", '{"duration_ms":0,"est_tokens":14,"files":0,"packages":[]}')
    return Router(scratch, {**fs.HANDLERS, **gotools.handlers_for(sidecar)})


def test_a_tree_holding_only_journals_is_empty(scratch: Workspace) -> None:
    assert workspace_empty(scratch.root)


def test_a_repository_is_not_empty(workspace: Workspace) -> None:
    assert not workspace_empty(workspace.root)


def test_search_on_an_empty_tree_points_at_the_message(scratch_router: Router) -> None:
    out = scratch_router.dispatch("search_repo", {"pattern": "Objection"}, mode=Mode.ASK)
    assert out.ok
    assert EMPTY_WORKSPACE in out.content
    assert "It is not in this workspace" not in out.content


def test_search_in_a_repository_is_unchanged(router: Router) -> None:
    out = router.dispatch("search_repo", {"pattern": "ZZZNOTHINGZZZ"}, mode=Mode.ASK)
    assert EMPTY_WORKSPACE not in out.content


def test_repo_map_on_an_empty_tree_points_at_the_message(scratch_router: Router) -> None:
    out = scratch_router.dispatch("repo_map", {}, mode=Mode.ASK)
    assert out.ok
    assert '"files":0' in out.content
    assert EMPTY_WORKSPACE in out.content


def test_the_pinned_task_says_the_workspace_is_empty(scratch_router: Router) -> None:
    """Before the first turn, so no turn is spent discovering it."""
    loop, _client = build(
        scratch_router,
        [calls(("finish", json.dumps(forced_args("finish"))))],
        kind="question",
        max_turns=2,
    )
    list(loop.run(SNIPPET))
    task = loop.context._task.content
    assert SNIPPET in task
    assert EMPTY_WORKSPACE in task


def test_a_task_on_a_repository_is_pinned_as_given(router: Router) -> None:
    loop, _client = build(
        router,
        [calls(("finish", json.dumps(forced_args("finish"))))],
        kind="question",
        max_turns=2,
    )
    list(loop.run("what does handler/user.go do?"))
    assert EMPTY_WORKSPACE not in loop.context._task.content
