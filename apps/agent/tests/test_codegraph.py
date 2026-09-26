"""The code-graph pilot: the graphify bridge, its wiring, and its guard rails.

Most of this runs against a scripted stand-in for the graphify CLI, so the
argument mapping, the staleness rule and the empty-callers caveat are tested
without a process per assertion. The ``live`` tests at the bottom run the real
graphify, and skip when it is not installed -- set ``DAKCODER_GRAPHIFY_PYTHON``
to an interpreter that has ``graphifyy`` to include them.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path

import pytest

from dakcoder_agent.modes import Mode
from dakcoder_agent.tools import codegraph, registry
from dakcoder_agent.tools.codegraph import GRAPH, CodeGraph, CodeGraphError
from dakcoder_agent.tools.router import Router
from dakcoder_shared.paths import Workspace


class ScriptedGraph(CodeGraph):
    """A CodeGraph whose CLI is a dictionary. Records every argv it is given."""

    def __init__(
        self,
        root: Path,
        answers: dict[str, str] | None = None,
        *,
        fail: str = "",
        graph: dict | None = None,
    ) -> None:
        super().__init__(root, python="python")
        self.answers = answers or {}
        self.fail = fail
        self.data = graph or {}
        self.calls: list[list[str]] = []

    def _exec(self, args: list[str], timeout: float) -> subprocess.CompletedProcess[str]:
        self.calls.append(args)
        if args[0] == self.fail:
            return subprocess.CompletedProcess(args, 1, "", "boom\n")
        if args[0] == "extract":
            self.graph.parent.mkdir(parents=True, exist_ok=True)
            self.graph.write_text(json.dumps(self.data), encoding="utf-8")
            return subprocess.CompletedProcess(args, 0, "", "")
        return subprocess.CompletedProcess(args, 0, self.answers.get(args[0], ""), "")

    def ops(self) -> list[str]:
        return [argv[0] for argv in self.calls]


def _router(workspace: Workspace, graph: CodeGraph) -> Router:
    return Router(workspace, codegraph.handlers_for(graph))


# ── wiring ──────────────────────────────────────────────────────────────────


def test_the_flag_is_off_unless_set_to_one() -> None:
    assert not codegraph.enabled({})
    assert not codegraph.enabled({"DAKCODER_CODE_GRAPH": "true"})
    assert codegraph.enabled({"DAKCODER_CODE_GRAPH": "1"})


def test_the_tool_is_offered_only_when_a_handler_is_registered(workspace: Workspace) -> None:
    """Off, the pilot must cost the prompt nothing in any mode."""
    bare = Router(workspace, {})
    wired = _router(workspace, ScriptedGraph(workspace.root))
    for mode in Mode:
        assert "code_graph" not in {s["function"]["name"] for s in bare.schemas_for(mode)}
        assert "code_graph" in {s["function"]["name"] for s in wired.schemas_for(mode)}


def test_the_pilot_costs_a_bounded_number_of_prompt_tokens() -> None:
    """What turning the flag on adds to every turn's prefix, in every mode.

    The prefix ceilings in ``test_prompts.py`` measure the default runtime,
    which never sends this schema; this is the other half of that budget.
    """
    import json

    from dakcoder_shared.tokens import estimate_tokens

    cost = estimate_tokens(json.dumps(registry.REGISTRY["code_graph"].schema()))
    assert cost <= 200, f"code_graph schema is {cost} tokens"


def test_the_spec_is_a_serial_read_only_lookup() -> None:
    spec = registry.REGISTRY["code_graph"]
    assert spec.provider is registry.Provider.GRAPHIFY
    assert not spec.mutates and not spec.parallel
    assert spec.instead, "a refusal must name the substitute"


# ── argument mapping ────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("arguments", "argv"),
    [
        ({"op": "explain", "symbol": "Foo"}, ["explain", "Foo"]),
        ({"op": "callers", "symbol": "Foo"}, ["affected", "Foo"]),
        ({"op": "query", "question": "how is x saved"}, ["query", "how is x saved", "--budget", "1500"]),
    ],
)
def test_each_op_maps_onto_its_cli_command(workspace: Workspace, arguments, argv) -> None:
    graph = ScriptedGraph(workspace.root, {argv[0]: "Node: x"})
    result = _router(workspace, graph).dispatch("code_graph", arguments, mode=Mode.ASK)

    assert result.ok, result.content
    query = graph.calls[-1]
    assert query[: len(argv)] == argv
    assert query[-2:] == ["--graph", str(workspace.root / GRAPH)]


@pytest.mark.parametrize(
    "arguments",
    [
        {"op": "explain"},
        {"op": "callers", "symbol": "  "},
        {"op": "path", "symbol": "A"},
        {"op": "query"},
    ],
)
def test_a_missing_operand_is_refused_without_running_anything(
    workspace: Workspace, arguments
) -> None:
    graph = ScriptedGraph(workspace.root)
    result = _router(workspace, graph).dispatch("code_graph", arguments, mode=Mode.ASK)

    assert not result.ok
    assert result.fix
    assert graph.calls == []


# ── building and staleness ──────────────────────────────────────────────────


def test_the_graph_is_built_once_then_reused(workspace: Workspace) -> None:
    graph = ScriptedGraph(workspace.root, {"explain": "Node: x"})
    router = _router(workspace, graph)

    router.dispatch("code_graph", {"op": "explain", "symbol": "A"}, mode=Mode.ASK)
    router.dispatch("code_graph", {"op": "explain", "symbol": "B"}, mode=Mode.ASK)

    assert graph.ops() == ["extract", "explain", "explain"]
    extract = graph.calls[0]
    assert "--code-only" in extract, "the graph must never send code to an LLM"


def test_an_edit_newer_than_the_graph_triggers_a_rebuild(workspace: Workspace) -> None:
    graph = ScriptedGraph(workspace.root, {"explain": "Node: x"})
    graph.ensure()
    assert not graph.stale()

    edited = workspace.root / "handler/user.go"
    later = time.time() + 5
    os.utime(edited, (later, later))

    assert graph.stale()
    assert graph.ensure() is True
    assert graph.ops() == ["extract", "extract"]


def test_files_under_pruned_directories_do_not_make_the_graph_stale(workspace: Workspace) -> None:
    graph = ScriptedGraph(workspace.root)
    graph.ensure()

    vendored = workspace.root / "vendor/x/x.go"
    vendored.parent.mkdir(parents=True)
    vendored.write_text("package x\n", encoding="utf-8")
    later = time.time() + 5
    os.utime(vendored, (later, later))

    assert not graph.stale()


def test_a_failed_build_is_a_dead_end_that_names_the_substitute(workspace: Workspace) -> None:
    graph = ScriptedGraph(workspace.root, fail="extract")
    result = _router(workspace, graph).dispatch(
        "code_graph", {"op": "explain", "symbol": "A"}, mode=Mode.ASK
    )

    assert not result.ok
    assert "search_repo" in result.fix
    assert result.meta.get("dead_end")


def test_graphify_not_installed_says_so_and_names_the_substitute(workspace: Workspace) -> None:
    graph = CodeGraph(workspace.root, python=None)
    graph.python = None
    result = _router(workspace, graph).dispatch(
        "code_graph", {"op": "explain", "symbol": "A"}, mode=Mode.ASK
    )

    assert not result.ok
    assert "pip install graphifyy" in result.content
    assert "search_repo" in result.fix


def test_the_missing_error_is_marked_for_the_router() -> None:
    graph = CodeGraph(Path("."), python=None)
    graph.python = None
    with pytest.raises(CodeGraphError) as caught:
        graph._exec(["explain", "x"], 1)
    assert caught.value.missing


# ── what the model is told ──────────────────────────────────────────────────


def test_no_callers_is_not_presented_as_proof(workspace: Workspace) -> None:
    """graphify misses calls through struct fields -- `h.svc.Repo()` -- which is
    the handler-to-repository chain of every template service. An empty answer
    must say so, or the model will treat live code as dead."""
    graph = ScriptedGraph(
        workspace.root, {"affected": "Affected nodes for .GetAll()\nNo affected nodes found."}
    )
    result = _router(workspace, graph).dispatch(
        "code_graph", {"op": "callers", "symbol": "UserRepository.GetAll()"}, mode=Mode.ASK
    )

    assert result.ok
    assert "not proof" in result.content
    assert "\\.GetAll\\(" in result.fix


def test_an_unknown_symbol_points_at_query_and_search(workspace: Workspace) -> None:
    graph = ScriptedGraph(workspace.root, {"explain": "No node matching 'Nope' found."})
    result = _router(workspace, graph).dispatch(
        "code_graph", {"op": "explain", "symbol": "Nope"}, mode=Mode.ASK
    )

    assert result.ok
    assert "op=query" in result.fix and "search_repo" in result.fix


def test_absolute_paths_are_made_workspace_relative(workspace: Workspace) -> None:
    root = workspace.root
    graph = ScriptedGraph(root, {"query": f"Graph: {root / 'x' / 'graph.json'} (3 nodes)"})
    result = _router(workspace, graph).dispatch(
        "code_graph", {"op": "query", "question": "q"}, mode=Mode.ASK
    )

    assert str(root) not in result.content
    assert "graph.json" in result.content


# ── the real graphify ───────────────────────────────────────────────────────


@pytest.fixture
def live(workspace: Workspace) -> CodeGraph:
    graph = CodeGraph(workspace.root)
    if graph.python is None:
        pytest.skip("graphify is not installed; set DAKCODER_GRAPHIFY_PYTHON to include this")
    return graph


def test_live_build_and_explain(live: CodeGraph, workspace: Workspace) -> None:
    router = _router(workspace, live)
    result = router.dispatch(
        "code_graph", {"op": "explain", "symbol": "GetByID"}, mode=Mode.ASK
    )

    assert live.graph.is_file()
    assert result.ok, result.content
    assert "repo/postgres/user.go" in result.content
    assert str(workspace.root) not in result.content


def test_live_builds_are_repeatable(live: CodeGraph) -> None:
    """The Windows re-exec crash was intermittent, so one clean build proves
    nothing; several in a row is the regression test for the pinned seed."""
    for _ in range(4):
        live.graph.unlink(missing_ok=True)
        assert live.ensure() is True
        assert live.graph.is_file()


# ── names that mean more than one node, and paths ───────────────────────────


def _node(nid: str, label: str, file: str, line: str = "L1") -> dict:
    return {"id": nid, "label": label, "source_file": file, "source_location": line}


def _edge(source: str, target: str, relation: str = "calls") -> dict:
    return {
        "source": source, "target": target, "relation": relation,
        "confidence": "EXTRACTED", "source_file": "x.go", "source_location": "L9",
    }


#: A REST and a gRPC repository with one name, which is every template service.
SERVICE = {
    "nodes": [
        _node("rest_repo", "UserRepository", "repo/postgres/user.go", "L20"),
        _node("grpc_repo", "UserRepository", "repo/postgres/usergrpc.go", "L28"),
        _node("h_create", ".CreateUser()", "handler/user.go", "L40"),
        _node("resp", "NewUserResponse()", "handler/response/user.go"),
        _node("dto", "User", "core/domain/user.go"),
        _node("db", "api-db.DB", ""),
    ],
    "links": [
        _edge("h_create", "resp"),
        _edge("resp", "dto", "references"),
        _edge("rest_repo", "db", "references"),
        _edge("grpc_repo", "db", "references"),
    ],
}


def test_an_ambiguous_name_lists_the_candidates_instead_of_failing(workspace: Workspace) -> None:
    """graphify exits 1 on this, and the first version reported it as the
    graph being unavailable -- so a field run abandoned the graph entirely."""
    graph = ScriptedGraph(workspace.root, graph=SERVICE)
    result = _router(workspace, graph).dispatch(
        "code_graph", {"op": "explain", "symbol": "UserRepository"}, mode=Mode.ASK
    )

    assert result.ok
    assert not result.meta.get("dead_end")
    assert "repo/postgres/user.go::UserRepository" in result.content
    assert "repo/postgres/usergrpc.go::UserRepository" in result.content
    assert graph.ops() == ["extract"], "nothing is asked of graphify until the name is pinned"


def test_a_pinned_name_reaches_graphify_as_its_node_id(workspace: Workspace) -> None:
    graph = ScriptedGraph(workspace.root, {"affected": "Affected nodes for x\n- A"}, graph=SERVICE)
    result = _router(workspace, graph).dispatch(
        "code_graph",
        {"op": "callers", "symbol": "repo/postgres/usergrpc.go::UserRepository"},
        mode=Mode.ASK,
    )

    assert result.ok
    assert graph.calls[-1][:2] == ["affected", "grpc_repo"]


def test_type_dot_method_and_bare_method_names_resolve(workspace: Workspace) -> None:
    graph = ScriptedGraph(workspace.root, {"explain": "Node: x"}, graph=SERVICE)
    for name in ("CreateUser", "CreateUser()", ".CreateUser()"):
        assert [n["id"] for n in graph.resolve(name)] == ["h_create"], name


def test_a_failed_query_is_not_a_dead_end(workspace: Workspace) -> None:
    graph = ScriptedGraph(workspace.root, fail="explain", graph=SERVICE)
    result = _router(workspace, graph).dispatch(
        "code_graph", {"op": "explain", "symbol": "User"}, mode=Mode.ASK
    )

    assert not result.ok
    assert not result.meta.get("dead_end"), "one bad question must not retire the graph"
    assert "<file>::<name>" in result.fix


def test_a_path_follows_edge_direction_when_it_can(workspace: Workspace) -> None:
    graph = ScriptedGraph(workspace.root, graph=SERVICE)
    result = _router(workspace, graph).dispatch(
        "code_graph", {"op": "path", "symbol": "CreateUser", "to": "User"}, mode=Mode.ASK
    )

    assert result.ok, result.content
    assert "2 hop(s), following the edges' direction" in result.content
    assert ".CreateUser() --calls [EXTRACTED]--> NewUserResponse()" in result.content
    assert "path" not in graph.ops(), "paths are computed here, with exact endpoints"


def test_a_path_that_only_exists_backwards_says_so(workspace: Workspace) -> None:
    graph = ScriptedGraph(workspace.root, graph=SERVICE)
    result = _router(workspace, graph).dispatch(
        "code_graph",
        {"op": "path", "symbol": "repo/postgres/user.go::UserRepository",
         "to": "repo/postgres/usergrpc.go::UserRepository"},
        mode=Mode.ASK,
    )

    assert result.ok, result.content
    assert "ignoring direction" in result.content
    assert "<--references [EXTRACTED]--" in result.content


def test_an_ambiguous_path_end_lists_its_candidates(workspace: Workspace) -> None:
    graph = ScriptedGraph(workspace.root, graph=SERVICE)
    result = _router(workspace, graph).dispatch(
        "code_graph", {"op": "path", "symbol": "CreateUser", "to": "UserRepository"}, mode=Mode.ASK
    )

    assert result.ok
    assert "matches 2 nodes" in result.content


def test_an_unknown_path_end_is_reported_by_name(workspace: Workspace) -> None:
    graph = ScriptedGraph(workspace.root, graph=SERVICE)
    result = _router(workspace, graph).dispatch(
        "code_graph", {"op": "path", "symbol": "CreateUser", "to": "Nope"}, mode=Mode.ASK
    )

    assert result.ok
    assert "'Nope'" in result.content
    assert "search_repo" in result.fix


# ── graphify's advice, in this tool's words ─────────────────────────────────

GRAPHIFY_TRUNCATED = (
    "Graph: g.json (1373 nodes) | Traversal: BFS depth=2 | 602 nodes found\n\n"
    "[!] TRUNCATED: showing 61 of 602 nodes (~1500-token budget). The answer may be "
    "among the 541 cut nodes - raise the token budget (CLI: --budget) or narrow the "
    "query (e.g. context_filter=['call'], or get_node for a specific symbol).\n\n"
    "NODE main() [src=main.go loc=L27 community=28]\n"
    "... (truncated - 541 more nodes cut by ~1500-token budget. Narrow with "
    "context_filter=['call'] or use get_node for a specific symbol)"
)


def test_query_truncation_advice_names_moves_this_tool_has(workspace: Workspace) -> None:
    """graphify tells the caller to raise --budget, pass context_filter or call
    get_node -- none of which code_graph offers. A field run, handed that, re-worded
    the query twice, repeated it verbatim, and ended its turn on a preamble."""
    graph = ScriptedGraph(workspace.root, {"query": GRAPHIFY_TRUNCATED})
    result = _router(workspace, graph).dispatch(
        "code_graph", {"op": "query", "question": "entry points"}, mode=Mode.ASK
    )

    assert result.ok
    for foreign in ("--budget", "context_filter", "get_node"):
        assert foreign not in result.content, foreign
    assert "61 of 602 nodes shown" in result.content
    assert "op=explain" in result.content
    assert "NODE main()" in result.content
