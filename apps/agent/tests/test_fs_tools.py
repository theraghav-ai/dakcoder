"""Tests for the filesystem tools.

The line-ending tests carry most of the weight. They are the ones that fail
silently in production and pass trivially in a test suite written on LF files —
which is why the fixture workspace is CRLF throughout.
"""

from __future__ import annotations

import pytest

from dakcoder_agent.modes import Mode
from dakcoder_agent.tools.router import Router
from dakcoder_shared.paths import Workspace


def run(router: Router, tool: str, **args):
    return router.dispatch(tool, args, mode=Mode.CODER)


# ── line endings ────────────────────────────────────────────────────────────


def test_patching_a_crlf_file_leaves_it_crlf(router: Router, workspace: Workspace) -> None:
    """The failure this module exists to prevent.

    Normalising to LF turns a three-line change into a whole-file diff: review
    becomes impossible, git blame becomes useless, and it happens silently on
    the very first edit.
    """
    out = run(
        router,
        "patch_file",
        path="handler/user.go",
        old="func New() *UserHandler { return nil }",
        new="func New() *UserHandler { return &UserHandler{} }",
    )
    assert out.ok

    raw = (workspace.root / "handler/user.go").read_bytes()
    assert b"\r\n" in raw
    assert raw.count(b"\n") == raw.count(b"\r\n"), "a bare LF crept in"


def test_a_new_file_inherits_its_neighbours_line_endings(
    router: Router, workspace: Workspace
) -> None:
    """A model writes LF; the package is CRLF. Without this the new file shows
    as entirely changed the first time anything else touches it."""
    out = run(router, "write_file", path="handler/pension.go", content="package handler\n\nfunc P() {}")
    assert out.ok
    assert b"\r\n" in (workspace.root / "handler/pension.go").read_bytes()


def test_a_new_file_in_a_new_directory_defaults_to_lf(
    router: Router, workspace: Workspace
) -> None:
    out = run(router, "write_file", path="core/port/user.go", content="package port\n")
    assert out.ok
    assert b"\r\n" not in (workspace.root / "core/port/user.go").read_bytes()


def test_a_patch_anchor_written_with_lf_matches_a_crlf_file(router: Router) -> None:
    """The model never emits \\r\\n. If matching were byte-exact, no patch would
    ever apply to the reference template."""
    assert run(
        router, "patch_file", path="handler/request.go", old="type CreateUserRequest struct{}", new="type CreateUserRequest struct{ Name string }"
    ).ok


def test_a_written_file_always_ends_with_a_newline(router: Router, workspace: Workspace) -> None:
    run(router, "write_file", path="core/port/x.go", content="package port")
    assert (workspace.root / "core/port/x.go").read_bytes().endswith(b"\n")


# ── read ────────────────────────────────────────────────────────────────────


def test_read_returns_a_header_naming_the_range(router: Router) -> None:
    out = run(router, "read_file", path="core/domain/user.go", start=1, end=2)
    assert out.content.startswith("core/domain/user.go (lines 1-2 of 6)")


def test_read_does_not_number_the_lines(router: Router) -> None:
    """Numbers would help navigation and would sooner or later end up inside a
    patch anchor, corrupting a file in a way that compiles about half the time.
    The header carries the range instead, which is enough to count from."""
    body = run(router, "read_file", path="handler/user.go").content.split("\n", 1)[1]
    assert body.startswith("package handler")


def test_reading_past_the_end_says_how_long_the_file_is(router: Router) -> None:
    out = run(router, "read_file", path="handler/user.go", start=9000)
    assert not out.ok
    assert "5 lines" in out.content


def test_reading_a_directory_is_refused_with_the_right_tool(router: Router) -> None:
    out = run(router, "read_file", path="handler")
    assert not out.ok
    assert "repo_map" in out.fix


def test_a_binary_file_is_refused(router: Router, workspace: Workspace) -> None:
    (workspace.root / "blob.txt").write_bytes(b"\x00\x01\x02binary")
    out = run(router, "read_file", path="blob.txt")
    assert not out.ok
    assert "binary" in out.content


# ── patch ───────────────────────────────────────────────────────────────────


def test_an_ambiguous_anchor_fails_rather_than_guessing(router: Router) -> None:
    """Picking the first of several matches is how an agent edits the wrong one
    of five methods that share a body. Failing costs one turn; guessing costs a
    debugging session, usually somebody else's."""
    out = run(router, "patch_file", path="repo/postgres/user.go", old="func (r *UserRepository) Get", new="func (r *UserRepository) Fetch")
    assert not out.ok
    assert "appears 2 times" in out.content
    assert "surrounding lines" in out.fix


def test_a_missing_anchor_diagnoses_why(router: Router) -> None:
    """Nearly every miss is one of three causes, each with a different fix. A
    bare "not found" makes the model retry with the same mistake."""
    out = run(router, "patch_file", path="handler/user.go", old="   func New() *UserHandler { return nil }", new="x")
    assert not out.ok
    assert "whitespace" in out.fix


def test_a_no_op_patch_is_refused(router: Router) -> None:
    out = run(router, "patch_file", path="handler/user.go", old="package handler", new="package handler")
    assert not out.ok
    assert "change nothing" in out.content


def test_patching_a_file_that_does_not_exist_names_write_file(router: Router) -> None:
    out = run(router, "patch_file", path="handler/absent.go", old="a", new="b")
    assert not out.ok
    assert "write_file" in out.fix


def test_write_refuses_to_overwrite(router: Router) -> None:
    out = run(router, "write_file", path="handler/user.go", content="package handler")
    assert not out.ok
    assert "patch_file" in out.fix


def test_a_patch_reports_the_line_delta(router: Router) -> None:
    out = run(router, "patch_file", path="handler/user.go", old="func (h *UserHandler) Routes() {}", new="func (h *UserHandler) Routes() {\n\t// routes\n}")
    assert out.ok
    assert "+2 lines" in out.content


# ── search ──────────────────────────────────────────────────────────────────


def test_search_returns_path_line_and_text(router: Router) -> None:
    out = run(router, "search_repo", pattern=r"func \(r \*UserRepository\)")
    assert out.ok
    assert "repo/postgres/user.go:3:" in out.content


def test_a_glob_scopes_the_search_and_star_does_not_cross_directories(
    router: Router, workspace: Workspace
) -> None:
    """fnmatch would let `handler/*.go` match `handler/response/user.go`,
    silently widening every scoped search into an unscoped one."""
    (workspace.root / "handler/response").mkdir()
    (workspace.root / "handler/response/user.go").write_text("package response\nfunc New() {}\n")

    scoped = run(router, "search_repo", pattern="func New", glob="handler/*.go")
    assert "handler/response/user.go" not in scoped.content
    assert "handler/user.go" in scoped.content

    recursive = run(router, "search_repo", pattern="func New", glob="handler/**/*.go")
    assert "handler/response/user.go" in recursive.content


def test_search_caps_results_and_says_so(router: Router) -> None:
    out = run(router, "search_repo", pattern="package", max=1)
    assert out.truncated
    assert "narrow the pattern" in out.content


def test_no_matches_is_a_success_not_a_failure(router: Router) -> None:
    """An empty result is the tool working. Reporting it as failure would make
    the loop retry a search that correctly found nothing."""
    out = run(router, "search_repo", pattern="ZZZNOTHINGZZZ")
    assert out.ok
    assert "no matches" in out.content


def test_pruned_directories_are_never_searched(router: Router, workspace: Workspace) -> None:
    vendor = workspace.root / "vendor" / "x"
    vendor.mkdir(parents=True)
    (vendor / "big.go").write_text("package x\nfunc Needle() {}\n")
    assert "vendor" not in run(router, "search_repo", pattern="Needle").content


def test_an_invalid_regex_is_refused_with_the_reason(router: Router) -> None:
    out = run(router, "search_repo", pattern="func (")
    assert not out.ok
    assert "not a valid regular expression" in out.content


# ── delete ──────────────────────────────────────────────────────────────────


def test_deleting_a_directory_is_refused(router: Router) -> None:
    # approved=True because delete_file can never be auto-approved by policy —
    # that invariant is asserted in test_router; here we want the handler.
    out = router.dispatch(
        "delete_file", {"path": "handler", "reason": "cleanup"}, approved=True
    )
    assert not out.ok
    assert "directory" in out.content


def test_deleting_an_absent_file_is_idempotent(router: Router) -> None:
    out = router.dispatch(
        "delete_file", {"path": "handler/gone.go", "reason": "cleanup"}, approved=True
    )
    assert out.ok
    assert out.mutations == ()


# ── gofmt and line endings ──────────────────────────────────────────────────


def test_gofmt_formats_without_converting_line_endings(router: Router, workspace) -> None:
    """gofmt -w rewrites CRLF as LF. Measured, not assumed.

    That makes the inner loop undo patch_file's line-ending preservation one
    step later — which is worse than never having it, because the unit test for
    patch_file still passes. So gofmt captures each file's ending and restores
    it afterwards: the formatting is kept, the side effect is not.
    """
    path = workspace.root / "handler" / "messy.go"
    path.write_bytes(b"package handler\r\n\r\nfunc  Messy()  {}\r\n")

    out = router.run_gate_tool("gofmt", {"paths": "handler/messy.go"})
    assert out.ok

    raw = path.read_bytes()
    assert b"func Messy() {}" in raw.replace(b"\r\n", b"\n"), "gofmt did not reformat"
    assert raw.count(b"\r\n") == raw.count(b"\n") > 0, "CRLF was not restored"
    assert out.meta["eol_restored"] == 1


def test_a_file_changed_only_by_line_endings_is_not_reported_as_modified(
    router: Router,
) -> None:
    """Every .go file in the reference template is already gofmt-clean apart
    from its CRLF endings, so without this every gofmt run would mark every
    touched file as modified — and the mutation list the gate scopes itself to
    would fill with files nothing happened to."""
    out = router.run_gate_tool("gofmt", {"paths": "handler/user.go"})
    assert out.ok
    assert out.mutations == ()
    assert "already formatted" in out.content


def test_gofmt_leaves_an_lf_file_as_lf(router: Router, workspace) -> None:
    path = workspace.root / "core" / "port" / "x.go"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"package port\n\nfunc  X()  {}\n")

    router.run_gate_tool("gofmt", {"paths": "core/port/x.go"})
    assert b"\r\n" not in path.read_bytes()


def test_unparseable_go_is_not_reported_as_a_gofmt_failure(
    router: Router, workspace
) -> None:
    """A syntax error stops formatting. go_build reports it properly, with a
    line number; failing here would report it twice and less usefully."""
    (workspace.root / "handler" / "broken.go").write_bytes(b"package handler\r\nfunc X( {\r\n")
    out = router.run_gate_tool("gofmt", {"paths": "handler/broken.go"})
    assert out.ok
    assert "go_build" in out.content
