"""Tests for workspace confinement.

These are the tests that matter most in the tool layer. Everything else here
produces a bad answer when it goes wrong; this produces a read of the
developer's private key, and it does it from one hallucinated path in one turn.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from dakcoder_shared.paths import PathEscape, Workspace, is_protected


@pytest.fixture
def ws(tmp_path: Path) -> Workspace:
    (tmp_path / "handler").mkdir()
    (tmp_path / "handler" / "user.go").write_text("package handler\n")
    return Workspace.at(tmp_path)


# ── what must be allowed ────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "given",
    [
        "handler/user.go",
        "./handler/user.go",
        "handler\\user.go",  # a Windows-shaped path from a Windows-hosted model
    ],
)
def test_ordinary_paths_resolve(ws: Workspace, given: str) -> None:
    assert ws.resolve(given).name == "user.go"


def test_dotdot_is_refused_even_when_it_normalises_back_inside(ws: Workspace) -> None:
    """``handler/../handler/user.go`` lands on a legal file, and is still refused.

    Allowing it would make the rule "resolve, then check", which is one subtle
    bug away from "resolve, then check the wrong thing" — and no legitimate tool
    call contains ``..`` at all. Refusing the syntax outright keeps the rule
    statable in one sentence, which is the only kind of security rule that
    survives contact with a growing codebase.
    """
    with pytest.raises(PathEscape, match="walks above"):
        ws.resolve("handler/../handler/user.go")


def test_a_path_that_does_not_exist_still_resolves(ws: Workspace) -> None:
    """write_file resolves before it creates.

    Requiring existence would be worse than useless: it would push the check to
    after the parent directory had been created, and a parent can be a symlink.
    """
    assert ws.resolve("handler/pension.go").parent.name == "handler"


def test_relative_is_always_posix(ws: Workspace) -> None:
    """The form that crosses the wire, appears in diffs and is matched against
    globs. Three consumers, one separator, or three separate bugs."""
    assert ws.relative(ws.resolve("handler\\user.go")) == "handler/user.go"


# ── syntactic escapes ───────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "given,fragment",
    [
        ("../secrets.txt", "'..'"),
        ("handler/../../etc/passwd", "'..'"),
        ("/etc/passwd", "absolute"),
        ("C:/Windows/System32/config/SAM", "drive"),
        ("c:\\Windows\\win.ini", "drive"),
        ("//server/share/file", "UNC"),
        ("\\\\server\\share\\file", "UNC"),
        ("", "empty"),
        ("   ", "empty"),
    ],
)
def test_syntactic_escapes_are_refused(ws: Workspace, given: str, fragment: str) -> None:
    with pytest.raises(PathEscape) as caught:
        ws.resolve(given)
    assert fragment in caught.value.reason
    assert caught.value.fix, "a refusal with no fix costs the model a whole turn"


@pytest.mark.parametrize(
    "given,fragment",
    [
        ("handler/user.go:secret", "alternate data stream"),
        ("handler./user.go", "dot or space"),
        ("handler/user.go ", "dot or space"),
        ("NUL", "reserved Windows device"),
        ("handler/con.go", "reserved Windows device"),
        ("COM1.txt", "reserved Windows device"),
    ],
)
def test_windows_specific_escapes_are_refused_everywhere(
    ws: Workspace, given: str, fragment: str
) -> None:
    """Enforced on every platform, not only Windows.

    A path written on a Linux CI runner is opened on a developer's Windows
    machine. A rule that only fires where the damage happens is not a rule.
    """
    with pytest.raises(PathEscape) as caught:
        ws.resolve(given)
    assert fragment in caught.value.reason


def test_a_nul_byte_is_refused(ws: Workspace) -> None:
    """Truncates the path at the C layer, so 'handler/user.go\\x00.txt' opens
    'handler/user.go' while every string check saw the harmless-looking name."""
    with pytest.raises(PathEscape, match="NUL"):
        ws.resolve("handler/user.go\x00.txt")


# ── symbolic escapes ────────────────────────────────────────────────────────


@pytest.mark.skipif(
    os.name == "nt" and not os.environ.get("CI"),
    reason="creating a symlink on Windows needs Developer Mode or elevation",
)
def test_a_symlink_out_of_the_tree_is_refused(ws: Workspace, tmp_path: Path) -> None:
    """The escape a purely syntactic check cannot see.

    'escape/passwd' contains no '..' and is not absolute. It normalises clean and
    resolves outside — which is why containment is re-tested *after* resolution
    rather than inferred from the spelling.
    """
    outside = tmp_path.parent / "outside-the-workspace"
    outside.mkdir(exist_ok=True)
    (outside / "passwd").write_text("root:x:0:0")
    try:
        (ws.root / "escape").symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable")

    with pytest.raises(PathEscape) as caught:
        ws.resolve("escape/passwd")
    assert "symbolic link" in caught.value.reason


# ── the protected set ───────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "rel",
    [
        "go.mod",
        "go.sum",
        "main.go",
        "cmd/api/main.go",
        "bootstrap/bootstrapper.go",
        "bootstrap/nested/wire.go",
        "configs/app.yaml",
        "configs/config.prod.yaml",
        "configs/env/prod.yaml",
        "db/users.sql",
        "handler/request_createuserrequest_validator.go",
        ".git/config",
    ],
)
def test_structural_and_generated_paths_need_approval(rel: str) -> None:
    assert is_protected(rel)


@pytest.mark.parametrize(
    "rel",
    [
        "handler/user.go",
        "handler/response/user.go",
        "core/domain/user.go",
        "repo/postgres/user.go",
        "handler/request.go",
        "README.md",
    ],
)
def test_ordinary_source_does_not(rel: str) -> None:
    """The common path must stay uninterrupted.

    An approval prompt on every handler edit trains the developer to click
    through them, and then the prompt on go.mod gets clicked through too.
    """
    assert not is_protected(rel)


def test_the_request_dto_file_itself_is_editable_but_its_validator_is_not() -> None:
    """The distinction the whole generated-code rule rests on: edit the source
    of truth, regenerate the output. Editing the output is silently reverted."""
    assert not is_protected("handler/request.go")
    assert is_protected("handler/request_useriduri_validator.go")
