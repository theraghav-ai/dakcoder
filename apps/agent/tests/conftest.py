"""Fixtures for the tool layer: a throwaway workspace shaped like a real service."""

from __future__ import annotations

from pathlib import Path

import pytest

from dakcoder_agent.tools import commands, fs, gotools, knowledge
from dakcoder_agent.tools.gotools import Reply
from dakcoder_agent.tools.router import Router
from dakcoder_shared.paths import Workspace

#: A miniature n-api-template service. Deliberately CRLF, because the reference
#: template is CRLF and a tool tested only against LF passes every test and then
#: rewrites every file it touches on the first real repository it sees.
_FILES: dict[str, str] = {
    "go.mod": "module pisapi\r\n\r\ngo 1.25.0\r\n",
    "main.go": "package main\r\n\r\nfunc main() {}\r\n",
    "core/domain/user.go": (
        "package domain\r\n\r\n"
        "type User struct {\r\n"
        "\tID        int       `json:\"id\" db:\"id\"`\r\n"
        "\tFirstName string    `json:\"first_name\" db:\"first_name\"`\r\n"
        "}\r\n"
    ),
    "repo/postgres/user.go": (
        "package postgres\r\n\r\n"
        "func (r *UserRepository) GetAll() {}\r\n"
        "func (r *UserRepository) GetByID() {}\r\n"
    ),
    "handler/user.go": (
        "package handler\r\n\r\n"
        "func New() *UserHandler { return nil }\r\n\r\n"
        "func (h *UserHandler) Routes() {}\r\n"
    ),
    "handler/request/request.go": "package request\r\n\r\ntype CreateUserRequest struct{}\r\n",
    "bootstrap/bootstrapper.go": "package bootstrap\r\n\r\nvar FxRepo = 1\r\n",
    "configs/app.yaml": "app:\n  name: pisapi\n",
    "db/users.sql": "CREATE TABLE users (id serial4 PRIMARY KEY);\n",
    "docs/notes.md": "# Notes\n\nThe repository layer owns SQL.\n",
}


@pytest.fixture
def workspace(tmp_path: Path) -> Workspace:
    for rel, body in _FILES.items():
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8", newline="")
    return Workspace.at(tmp_path)


class FakeSidecar:
    """A ``gotools`` stand-in that records calls and answers from a script.

    The real bridge is exercised against the real binary in
    ``test_gotools_bridge.py``. This exists so the *router's* behaviour around
    sidecar tools — argument mapping, mutation recording, approval — can be
    tested without spawning a process per assertion, which would put a 30 ms
    handshake behind every one of them.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.replies: dict[str, Reply] = {}
        self.default = Reply('{"ok":true,"count":0}')

    def call(self, tool: str, arguments) -> Reply:
        self.calls.append((tool, dict(arguments)))
        return self.replies.get(tool, self.default)

    def answer(self, tool: str, text: str, *, is_error: bool = False) -> None:
        self.replies[tool] = Reply(text, is_error)


@pytest.fixture
def sidecar() -> FakeSidecar:
    return FakeSidecar()


@pytest.fixture
def router(workspace: Workspace, sidecar: FakeSidecar) -> Router:
    """A router with every tool wired.

    The command tools are registered too — they are pure dispatch — but tests
    that would actually run ``go build`` are marked slow and skip when the
    toolchain is absent.
    """
    handlers = {
        **fs.HANDLERS,
        **knowledge.HANDLERS,
        **commands.HANDLERS,
        **gotools.handlers_for(sidecar),
    }
    return Router(workspace, handlers)
