"""Tests for the git tools, and for the one distinction they get wrong easily.

``run`` merges stdout and stderr, which is right for the Go toolchain -- a build
failure is both -- and is the thing that makes ``git diff`` ambiguous. git puts
the diff on stdout and its advice on stderr, so a file that differs only in line
endings produces a warning, no diff, and exit 0. Merged, that is a successful
call whose result is one line of advice.

A model reads that as a call that malfunctioned rather than as "nothing
changed", so it repeats the call. The duplicate guard refuses the second and the
no-progress detector ends the run on the third. That sequence was observed in
the field on ``git_diff go.mod``, and these tests are what keep it fixed.
"""

from __future__ import annotations

import pytest

from dakcoder_agent.modes import Mode
from dakcoder_agent.tools import commands
from dakcoder_agent.tools.commands import Completed, _has_diff
from dakcoder_agent.tools.router import Router

CRLF_WARNING = (
    "warning: in the working copy of 'go.mod', LF will be replaced by CRLF "
    "the next time Git touches it"
)

REAL_DIFF = """diff --git a/go.mod b/go.mod
index 1111111..2222222 100644
--- a/go.mod
+++ b/go.mod
@@ -1,3 +1,3 @@
-go 1.21
+go 1.22
"""


def _answer(monkeypatch: pytest.MonkeyPatch, output: str, code: int = 0) -> None:
    """Make the next subprocess return exactly ``output``."""

    def fake_run(argv, cwd, *, timeout=0):
        return Completed(tuple(argv), code, output, 0.01)

    monkeypatch.setattr(commands, "run", fake_run)


# -- the predicate ----------------------------------------------------------


def test_advice_alone_is_not_a_diff() -> None:
    assert not _has_diff(CRLF_WARNING)
    assert not _has_diff("")


def test_a_diff_is_recognised_even_behind_advice() -> None:
    """git prints the warning first, so the check cannot anchor on line one."""
    assert _has_diff(REAL_DIFF)
    assert _has_diff(f"{CRLF_WARNING}\n{REAL_DIFF}")


# -- the tool ---------------------------------------------------------------


def test_a_warning_with_no_diff_reports_no_changes(
    router: Router, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The field failure, exactly: exit 0, a CRLF warning, and no diff."""
    _answer(monkeypatch, CRLF_WARNING)
    out = router.dispatch("git_diff", {"path": "go.mod"}, mode=Mode.AGENT)

    assert out.ok
    assert "no changes" in out.content, (
        "a result that only carries advice reads as a broken call, and the model "
        "retries it until the no-progress detector ends the run"
    )
    assert "go.mod" in out.content
    assert CRLF_WARNING in out.content, "the warning is worth seeing once"
    assert "advice rather than a result" in out.content


def test_an_empty_diff_reports_no_changes(
    router: Router, monkeypatch: pytest.MonkeyPatch
) -> None:
    _answer(monkeypatch, "")
    out = router.dispatch("git_diff", {}, mode=Mode.AGENT)
    assert out.ok
    assert "no changes" in out.content


def test_a_real_diff_is_passed_through_untouched(
    router: Router, monkeypatch: pytest.MonkeyPatch
) -> None:
    _answer(monkeypatch, REAL_DIFF)
    out = router.dispatch("git_diff", {"path": "go.mod"}, mode=Mode.AGENT)
    assert out.ok
    assert "no changes" not in out.content
    assert "+go 1.22" in out.content


def test_a_failing_git_diff_is_still_a_failure(
    router: Router, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The no-changes path is for success only; a real error must not be
    laundered into 'no changes'."""
    _answer(monkeypatch, "fatal: not a git repository", code=128)
    out = router.dispatch("git_diff", {}, mode=Mode.AGENT)
    assert not out.ok
    assert "not a git repository" in out.content


def test_staged_is_named_in_the_no_changes_message(
    router: Router, monkeypatch: pytest.MonkeyPatch
) -> None:
    """'no changes' and 'nothing staged' are different facts."""
    _answer(monkeypatch, "")
    out = router.dispatch("git_diff", {"staged": "true"}, mode=Mode.AGENT)
    assert out.ok
    assert "no staged changes" in out.content
