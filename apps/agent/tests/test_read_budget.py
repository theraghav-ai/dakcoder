"""What one read costs, and what a session can hold.

From the same field log as ``test_session_objective.py``. Nine whole-file reads
took one run from 17,000 tokens to 168,000 and into a compaction -- which
evicted those nine files, making every one of them worth reading again. The run
went round that circuit twice and the model degenerated at 119,000.

``handler/paogen.go`` is 6,571 lines and cost 46,000 tokens; the projection's
``read_file`` cap is 48,000, so the cap was doing its job and its job was a
fifth of the context window per call. The bound that was missing is at the
tool: a read with no ``end`` now returns a window, and says what it withheld.
"""

from __future__ import annotations

import json

from dakcoder_shared.paths import Workspace

from dakcoder_agent.tools.fs import READ_WINDOW_LINES
from dakcoder_agent.tools.router import Router
from scripted import build, say  # noqa: E402
from scripted import gated, planning_router  # noqa: F401,E402


def _go_file(workspace: Workspace, name: str, lines: int) -> str:
    body = "\n".join(f"// line {i} of a legacy handler" for i in range(1, lines + 1))
    path = workspace.root / "handler"
    path.mkdir(parents=True, exist_ok=True)
    (path / name).write_text(body + "\n", encoding="utf-8")
    return f"handler/{name}"


def test_a_range_less_read_of_a_large_file_is_windowed(
    planning_router: Router, workspace: Workspace
) -> None:
    path = _go_file(workspace, "paogen.go", 6571)

    out = planning_router.dispatch("read_file", {"path": path}, mode="agent")

    assert out.ok
    assert out.meta["span"] == [1, READ_WINDOW_LINES]
    assert out.meta["lines"] == 6571, "the real length is still reported"
    assert f"lines 1-{READ_WINDOW_LINES} of 6571" in out.content


def test_the_window_says_what_it_withheld_and_how_to_get_it(
    planning_router: Router, workspace: Workspace
) -> None:
    """A model that is not told has read the file, as far as it knows."""
    path = _go_file(workspace, "transferentry.go", 3966)

    out = planning_router.dispatch("read_file", {"path": path}, mode="agent")

    assert f"{3966 - READ_WINDOW_LINES:,} more lines" in out.content
    assert f"start={READ_WINDOW_LINES + 1}" in out.content, "the next range is spelled out"


def test_a_small_file_is_returned_whole(
    planning_router: Router, workspace: Workspace
) -> None:
    """99% of this repository's Go corpus is under the window -- median 285
    lines, longest 863 -- so the common case must be untouched."""
    path = _go_file(workspace, "publicacct.go", 695)

    out = planning_router.dispatch("read_file", {"path": path}, mode="agent")

    assert out.meta["span"] == [1, 695]
    assert "more lines" not in out.content
    assert "695 lines" in out.content


def test_an_explicit_end_is_always_honoured(
    planning_router: Router, workspace: Workspace
) -> None:
    """A default, not a ceiling. The projection's cap is still the backstop for
    a range this wide; what this asserts is that the *tool* does not second-guess
    a model that said what it wanted."""
    path = _go_file(workspace, "paogen.go", 6571)

    out = planning_router.dispatch(
        "read_file", {"path": path, "start": 1, "end": 6571}, mode="agent"
    )

    assert out.meta["span"] == [1, 6571]
    assert "more lines" not in out.content


def test_an_open_ended_read_from_a_start_is_windowed_too(
    planning_router: Router, workspace: Workspace
) -> None:
    path = _go_file(workspace, "paogen.go", 6571)

    out = planning_router.dispatch("read_file", {"path": path, "start": 2000}, mode="agent")

    assert out.meta["span"] == [2000, 2000 + READ_WINDOW_LINES - 1]
    assert "start=2800" in out.content


def test_the_window_leaves_the_rest_reachable(
    planning_router: Router, workspace: Workspace
) -> None:
    """The half of BUG L-8 that matters here: what the ledger records has to be
    what the model was actually given, or the next read is refused as already
    seen and the tail of the file is unreachable for the run."""
    from dakcoder_shared.llm import ToolCall

    path = _go_file(workspace, "paogen.go", 6571)
    loop, _client = build(planning_router, [say("noop")])

    out = planning_router.dispatch("read_file", {"path": path}, mode="agent")
    appended = loop.context.append_tool_result(
        "read_file", out.for_model(), tool_call_id="t1", path=path,
        line_range=tuple(out.meta["span"]),
    )
    loop._record_read(path, appended.line_range, int(out.meta["lines"]))

    onward = ToolCall(id="t2", name="read_file", arguments=json.dumps(
        {"path": path, "start": READ_WINDOW_LINES + 1, "end": READ_WINDOW_LINES + 500}))
    assert loop._re_reading(onward) == "", "the lines the window withheld must dispatch"

    seen = ToolCall(id="t3", name="read_file", arguments=json.dumps(
        {"path": path, "start": 10, "end": 20}))
    assert loop._re_reading(seen), "lines that are in context are still refused"


def test_the_field_workload_now_fits(
    planning_router: Router, workspace: Workspace
) -> None:
    """The nine files of the field run, read the way the field run read them.

    Before the window they cost ~170,000 tokens and forced two compactions. The
    assertion is deliberately loose -- this is a budget, not a golden number --
    but an order of magnitude is the point.
    """
    from dakcoder_shared.tokens import estimate_tokens

    sizes = {
        "paogen.go": 6571, "transferentry.go": 3966, "objection.go": 1037,
        "publicacct.go": 695, "objectionfile.go": 223, "user.go": 400,
        "file.go": 180, "grpc.go": 120, "request.go": 90,
    }
    total = 0
    for name, lines in sizes.items():
        path = _go_file(workspace, name, lines)
        out = planning_router.dispatch("read_file", {"path": path}, mode="agent")
        total += estimate_tokens(out.content)

    assert total < 60_000, f"nine reads cost {total:,} tokens"
