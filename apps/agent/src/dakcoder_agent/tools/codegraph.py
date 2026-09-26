"""The code graph: graphify, spoken through its command line.

graphify (``pip install graphifyy``) parses the workspace with tree-sitter into a
graph of files, functions and types joined by call, reference and import edges,
and answers structural questions against it -- what a symbol touches, what
touches it, how one reaches another. Those questions cost the model a chain of
``search_repo`` and ``read_file`` calls today; one graph lookup is a few hundred
tokens.

**A pilot, off by default.** ``DAKCODER_CODE_GRAPH=1`` turns it on. Off, no
handler is registered, so ``Router.schemas_for`` hides the spec and the tool
costs no prompt tokens in any mode.

**Why the CLI rather than graphify's MCP server.** ``graphify.serve`` needs the
async ``mcp`` SDK, the dependency ``gotools.py`` already declined to vendor for
the offline install. The CLI exposes the same traversals (``explain``,
``affected``, ``path``, ``query``), a process per call costs about half a second
against a graph this size, and there is no long-lived process to supervise.

**Code only, always.** ``--code-only`` is local AST extraction with no LLM and
no key, which is what the no-model-credential invariant in ``serve.py`` and the
"user code is not sent server-side" rule both require. The docs half of
graphify is covered by ``search_docs`` already.

**Built lazily, refreshed on staleness.** The graph is built on first use and
rebuilt when any source file is newer than it -- which catches the model's own
edits and the developer's edits in the editor alike, without a hook on every
mutation paying for a rebuild nobody asked for.

**Two Windows facts, both measured.** graphify re-execs itself through
``os.execvpe`` to pin ``PYTHONHASHSEED`` unless the variable is already set, and
on Windows that emulated exec crashed with an access violation in about half of
``extract`` runs -- and returned before the child finished when it did not.
Setting the seed here skips the re-exec entirely (15/15 clean afterwards). And
its output is UTF-8 that a cp1252 console cannot encode, hence ``PYTHONUTF8``.
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import subprocess
import sys
import threading
from pathlib import Path
from collections.abc import Mapping
from typing import Any

from dakcoder_shared.envelope import ToolResult

from .fs import PRUNE
from .router import Invocation

__all__ = ["CodeGraph", "CodeGraphError", "QueryFailed", "enabled", "handlers_for"]

#: The switch. Read at wiring time, so flipping it needs a runtime restart --
#: which the extension does whenever a setting it passes through changes.
ENV_FLAG = "DAKCODER_CODE_GRAPH"
#: An interpreter that has graphify installed, when the runtime's own does not.
PYTHON_ENV = "DAKCODER_GRAPHIFY_PYTHON"

#: Under ``.dakcoder/`` because that is already excluded from hosted clones
#: (``agentsvc/repos.py``) and pruned from every search and listing (``fs.PRUNE``).
OUT_DIR = Path(".dakcoder") / "graphify"
GRAPH = OUT_DIR / "graphify-out" / "graph.json"

BUILD_TIMEOUT = 300.0
QUERY_TIMEOUT = 60.0

#: What counts as source for the staleness check. Broader than Go on purpose:
#: graphify indexes these too, and a stale edge into a Python helper is as
#: wrong as one into a Go file.
SOURCE_SUFFIXES = frozenset(
    {".go", ".py", ".ts", ".tsx", ".js", ".mjs", ".java", ".kt", ".rs", ".cs", ".proto"}
)

OPS = ("explain", "callers", "path", "query")

#: graphify's own default, stated so the cap in ``projection.TOOL_CAPS`` and
#: this cannot drift apart silently. Not a model parameter: see the spec.
QUERY_BUDGET = 1500

#: How many same-named nodes to list before asking for a file to narrow it.
MAX_CANDIDATES = 8

#: Appended when a reverse traversal comes back empty. graphify does not
#: resolve a call made through a struct field or an interface -- in this
#: corpus, ``uh.svc.ObjectionCreationRepo(...)`` in a handler produces no edge
#: -- and that is the handler-to-repository chain every n-api-template service
#: is built from. "No callers" there is a gap in the graph, not a fact about
#: the code, and a model told it plainly would delete live code on it.
_NO_CALLERS = (
    "The graph does not see calls made through struct fields or interfaces "
    "(e.g. h.svc.{name}()), so this is not proof nothing calls it."
)


class CodeGraphError(RuntimeError):
    """graphify is missing or would not build: the graph is unavailable."""

    #: Set when graphify is not installed at all. The router's generic
    #: exception branch reads it and answers with the environment message.
    missing: bool = False


class QueryFailed(CodeGraphError):
    """One query failed against a graph that is otherwise working."""


def enabled(env: dict[str, str] | None = None) -> bool:
    return (env if env is not None else os.environ).get(ENV_FLAG, "").strip() == "1"


def _find_python() -> str | None:
    explicit = os.environ.get(PYTHON_ENV, "").strip()
    if explicit:
        return explicit if Path(explicit).is_file() else None
    if importlib.util.find_spec("graphify") is not None:
        return sys.executable
    return None


class CodeGraph:
    """One workspace's graph, built on demand and queried through the CLI.

    The lock serialises builds: two tool calls that both find the graph stale
    would otherwise run two extractions into the same cache directory.
    """

    def __init__(self, root: Path, python: str | None = None) -> None:
        self.root = root
        self.python = python if python is not None else _find_python()
        self.graph = root / GRAPH
        self._lock = threading.Lock()
        self._cache: tuple[float, dict[str, dict[str, Any]], list[dict[str, Any]]] | None = None

    # -- lifecycle --------------------------------------------------------

    def ensure(self) -> bool:
        """Build the graph if it is missing or stale. True if a build ran."""
        with self._lock:
            if self.graph.is_file() and not self.stale():
                return False
            self._build()
            return True

    def stale(self) -> bool:
        try:
            built = self.graph.stat().st_mtime
        except OSError:
            return True
        return any(mtime > built for mtime in _source_mtimes(self.root))

    def _build(self) -> None:
        proc = self._exec(
            ["extract", str(self.root), "--code-only", "--out", str(self.root / OUT_DIR)],
            BUILD_TIMEOUT,
        )
        if proc.returncode != 0 or not self.graph.is_file():
            raise CodeGraphError(
                f"graphify could not build the code graph (exit {proc.returncode}): "
                f"{_last_line(proc.stderr) or _last_line(proc.stdout)}"
            )
        # graphify skips writing when nothing it indexes changed, which would
        # leave the graph older than an edit to a file it ignores and make every
        # later call rebuild. Touching it records "checked at this time".
        os.utime(self.graph)

    # -- querying ---------------------------------------------------------

    def run(self, *args: str) -> str:
        self.ensure()
        proc = self._exec([*args, "--graph", str(self.graph)], QUERY_TIMEOUT)
        if proc.returncode != 0:
            # The graph is fine; this question was not. graphify exits 1 for an
            # ambiguous name, and reporting that as "unavailable" is what made a
            # field run abandon the graph for the whole session.
            raise QueryFailed(
                self._relative((proc.stdout.strip() + "\n" + proc.stderr.strip()).strip())
                or f"graphify {args[0]} exited {proc.returncode}"
            )
        return self._relative(proc.stdout.strip())

    # -- resolution and paths, read from graph.json directly -----------------
    #
    # graphify matches names fuzzily, by score. For `explain` and `affected`
    # that is only a nuisance -- an exact node id goes straight through -- but
    # `path` scores its endpoints even when handed ids, so on a service with a
    # REST and a gRPC variant of every repository it can anchor on either one
    # and warn "ambiguous" about a question that was not. Resolving here makes
    # the endpoints exact, and a breadth-first search over a graph this size
    # costs a few milliseconds.

    def resolve(self, symbol: str) -> list[dict[str, Any]]:
        """Every node ``symbol`` could mean. Empty when nothing matches exactly."""
        nodes, _ = self._load()
        if symbol in nodes:
            return [nodes[symbol]]
        where, _, name = symbol.rpartition("::")
        where = where.replace("\\", "/").strip("/")

        def matching(label: str) -> list[dict[str, Any]]:
            wanted = _norm(label)
            return [
                n for n in nodes.values()
                if _norm(n.get("label", "")) == wanted
                and (not where or (n.get("source_file") or "").endswith(where))
            ]

        # The whole name first: `gin.Context` is a label in its own right, and
        # splitting it would match every `Context` in the graph.
        found = matching(name)
        owner, dot, member = name.rpartition(".")
        if not found and dot and owner:
            # `Type.Method`: graphify labels a method `.Method()` and carries
            # the receiver type, lowercased, in its id.
            found = matching(member)
            found = [n for n in found if _norm(owner).split("/")[-1] in n["id"]] or found
        return found

    def path(self, start: str, goal: str) -> tuple[list[tuple[str, dict[str, Any], str]], bool] | None:
        """Shortest path as (from, edge, to) hops, and whether it follows edge direction."""
        _, edges = self._load()
        forward: dict[str, list[tuple[str, dict[str, Any]]]] = {}
        both: dict[str, list[tuple[str, dict[str, Any]]]] = {}
        for edge in edges:
            forward.setdefault(edge["source"], []).append((edge["target"], edge))
            both.setdefault(edge["source"], []).append((edge["target"], edge))
            both.setdefault(edge["target"], []).append((edge["source"], edge))
        for adjacency, directed in ((forward, True), (both, False)):
            hops = _bfs(adjacency, start, goal)
            if hops is not None:
                return hops, directed
        return None

    def node(self, node_id: str) -> dict[str, Any]:
        return self._load()[0].get(node_id, {"id": node_id, "label": node_id})

    def _load(self) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]]]:
        self.ensure()
        stamp = self.graph.stat().st_mtime
        if self._cache is None or self._cache[0] != stamp:
            data = json.loads(self.graph.read_text(encoding="utf-8"))
            nodes = {n["id"]: n for n in data.get("nodes", []) if "id" in n}
            edges = [e for e in data.get("links", data.get("edges", [])) if "source" in e and "target" in e]
            self._cache = (stamp, nodes, edges)
        return self._cache[1], self._cache[2]

    def _exec(self, args: list[str], timeout: float) -> subprocess.CompletedProcess[str]:
        if self.python is None:
            # Worded for the developer as much as the model: the model relays it.
            # The extension installs graphify itself when the pilot is enabled,
            # so reaching here there means that install failed or this build
            # carries no graph wheels; `pip install` is the checkout's answer.
            exc = CodeGraphError(
                "graphify is not installed for this runtime. In VS Code the extension "
                "installs it when dakcoder.codeGraph.enabled is on; if it did not, the "
                "dakcoder output says why. Outside VS Code, `pip install graphifyy` or "
                f"set {PYTHON_ENV}."
            )
            exc.missing = True
            raise exc
        env = dict(os.environ, PYTHONUTF8="1", PYTHONIOENCODING="utf-8", PYTHONHASHSEED="0")
        try:
            return subprocess.run(  # noqa: S603 - argv list, shell=False
                [self.python, "-m", "graphify", *args],
                cwd=self.root,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=env,
                timeout=timeout,
                shell=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise TimeoutError(f"graphify {args[0]} took longer than {timeout:.0f}s") from exc

    def _relative(self, text: str) -> str:
        """Absolute paths are the developer's disk layout; the model gets workspace paths."""
        for form in {str(self.root), self.root.as_posix()}:
            text = text.replace(form + os.sep, "").replace(form + "/", "").replace(form, ".")
        return text


def _source_mtimes(root: Path):
    stack = [root]
    while stack:
        current = stack.pop()
        try:
            entries = list(os.scandir(current))
        except OSError:
            continue
        for entry in entries:
            try:
                if entry.is_dir(follow_symlinks=False):
                    if entry.name not in PRUNE and entry.name != "graphify-out":
                        stack.append(Path(entry.path))
                elif os.path.splitext(entry.name)[1].lower() in SOURCE_SUFFIXES:
                    yield entry.stat().st_mtime
            except OSError:
                continue


def _last_line(text: str | None) -> str:
    lines = [line for line in (text or "").splitlines() if line.strip()]
    return lines[-1][:300] if lines else ""


def _not_found(text: str) -> bool:
    return text.startswith(("No unique node match", "No node matching", "No node found"))


#: graphify's truncation notices, written for its own CLI and MCP server.
_TRUNCATED_TOP = re.compile(r"^\[!\] TRUNCATED: showing (\d+) of (\d+) (nodes|lines).*$", re.M)
_TRUNCATED_END = re.compile(r"^\.\.\. \(truncated .*$", re.M)
_BUDGET_NOTE = re.compile(r"^.*raising --budget further will not shrink it.*$\n*", re.M)


def _our_advice(text: str) -> str:
    """Replace graphify's narrowing advice with this tool's.

    It says "raise the token budget (CLI: --budget) or narrow the query (e.g.
    context_filter=['call'], or get_node...)" -- three parameters `code_graph`
    does not have. Handed advice it could not follow, a field run re-worded its
    query twice and then sent the same one again until the repeat ledger
    answered, and ended the turn on a preamble. The move that does exist is to
    stop asking broad questions and look at one node.
    """
    def top(m: re.Match[str]) -> str:
        return (
            f"[!] Overview only: {m.group(1)} of {m.group(2)} {m.group(3)} shown. "
            "Re-asking op=query in other words gives another overview like this one. "
            "To go deeper, call code_graph op=explain on a node listed here, or "
            "op=path between two of them."
        )

    text = _BUDGET_NOTE.sub("", text)
    text = _TRUNCATED_TOP.sub(top, text)
    return _TRUNCATED_END.sub("... (more nodes not shown; use op=explain on one above)", text)


def _norm(label: str) -> str:
    """`.GetAll()` and `GetAll` are the same name to a developer."""
    label = label.strip().lower()
    if label.endswith("()"):
        label = label[:-2]
    return label.lstrip(".")


def _bfs(adjacency, start: str, goal: str):
    """Fewest hops from ``start`` to ``goal`` as (from, edge, to) triples."""
    if start == goal:
        return []
    came: dict[str, tuple[str, dict[str, Any]]] = {}
    frontier, seen = [start], {start}
    while frontier:
        following = []
        for current in frontier:
            for nxt, edge in adjacency.get(current, ()):
                if nxt in seen:
                    continue
                seen.add(nxt)
                came[nxt] = (current, edge)
                if nxt == goal:
                    hops, at = [], goal
                    while at != start:
                        prev, via = came[at]
                        hops.append((prev, via, at))
                        at = prev
                    return hops[::-1]
                following.append(nxt)
        frontier = following
    return None


def _where(node: Mapping[str, Any]) -> str:
    file, line = node.get("source_file") or "", node.get("source_location") or ""
    return f"{file}:{line}" if file and line else file


def _candidates(symbol: str, found: list[dict[str, Any]]) -> ToolResult:
    """Several nodes share the name -- a REST and a gRPC repository, say.

    A success, not a failure: the graph answered, and the answer is a choice.
    Each line is already in the form the next call takes.
    """
    name = symbol.rpartition("::")[2]
    lines = [f"`{symbol}` matches {len(found)} nodes. Call again with one of these as symbol:"]
    for node in sorted(found, key=_where)[:MAX_CANDIDATES]:
        file = node.get("source_file") or ""
        label = (node.get("label") or name).lstrip(".").removesuffix("()")
        pinned = f"{file}::{label}" if file else node["id"]
        lines.append(f"  {pinned}    ({_where(node) or 'external'})")
    if len(found) > MAX_CANDIDATES:
        lines.append(f"  ... and {len(found) - MAX_CANDIDATES} more; add a file to narrow it.")
    return ToolResult.success("\n".join(lines), fix="Pass symbol as '<file>::<name>' from the list.")


def _render_path(graph: CodeGraph, hops, directed: bool, start: str, goal: str) -> str:
    first, last = graph.node(start), graph.node(goal)
    if not hops:
        return f"{first.get('label')} and {last.get('label')} are the same node."
    how = (
        "following the edges' direction"
        if directed
        else "ignoring direction (nothing leads there forward), so read each arrow"
    )
    lines = [
        f"Path from {first.get('label')} ({_where(first)}) to {last.get('label')} "
        f"({_where(last)}): {len(hops)} hop(s), {how}."
    ]
    for frm, edge, to in hops:
        label = f"{edge.get('relation', 'related')} [{edge.get('confidence', '?')}]"
        link = f"--{label}-->" if edge["source"] == frm else f"<--{label}--"
        at = _where(edge)
        lines.append(
            f"  {graph.node(frm).get('label')} {link} {graph.node(to).get('label')}"
            + (f"   ({at})" if at else "")
        )
    return "\n".join(lines)


# ── the handler ─────────────────────────────────────────────────────────────


def handlers_for(graph: CodeGraph | None) -> dict[str, Any]:
    def pin(symbol: str) -> str | ToolResult:
        """The node id for ``symbol``, a candidates list, or the name untouched.

        Untouched when nothing matches exactly, so graphify's own fuzzy match
        still gets a chance at a near miss.
        """
        found = graph.resolve(symbol)
        if len(found) == 1:
            return found[0]["id"]
        if len(found) > 1:
            return _candidates(symbol, found)
        return symbol

    def code_graph(inv: Invocation) -> ToolResult:
        assert graph is not None
        op = inv.arg("op")
        symbol = (inv.arg("symbol") or "").strip()
        target = (inv.arg("to") or "").strip()
        question = (inv.arg("question") or "").strip()

        if op in ("explain", "callers", "path") and not symbol:
            return ToolResult.failure(
                f"code_graph op={op} needs symbol.",
                fix="Pass the bare name, e.g. symbol='CreateObjectionHandler'.",
            )
        if op == "path" and not target:
            return ToolResult.failure(
                "code_graph op=path needs both symbol and to.",
                fix="Pass the start as symbol and the end as to.",
            )
        if op == "query" and not question:
            return ToolResult.failure(
                "code_graph op=query needs question.",
                fix="Ask in words, or use op=explain with a symbol name.",
            )

        try:
            if op == "query":
                text = _our_advice(graph.run("query", question, "--budget", str(QUERY_BUDGET)))
            else:
                start = pin(symbol)
                if isinstance(start, ToolResult):
                    return start
                if op == "explain":
                    text = graph.run("explain", start)
                elif op == "callers":
                    # The relation list is the same fourteen names on every call:
                    # ~40 tokens of boilerplate per answer, and nothing to act on.
                    text = "\n".join(
                        line
                        for line in graph.run("affected", start).splitlines()
                        if not line.startswith("Relations:")
                    )
                else:
                    text = _path(graph, pin, symbol, target, start)
                    if isinstance(text, ToolResult):
                        return text
        except QueryFailed as exc:
            # The graph works; this question did not. Not a dead end: a
            # different name, or the same one pinned to a file, can succeed.
            return ToolResult.failure(
                f"code_graph {op}: {exc}",
                fix="Try a more specific symbol ('<file>::<name>'), op=query, or search_repo.",
            )
        except (CodeGraphError, TimeoutError) as exc:
            # Unlike a missing gotools, a missing graph has a full substitute,
            # so this names it rather than telling the model to stop. Marked a
            # dead end so a repeat is answered from the ledger, not re-run.
            return ToolResult.failure(
                str(exc),
                fix="The code graph is unavailable this session; use search_repo and read_file.",
                meta={"dead_end": "the code graph is unavailable"},
            )

        if _not_found(text):
            return ToolResult.success(
                text,
                fix=(
                    "The name is not a node in the graph. Try op=query with the name, "
                    "or search_repo for it."
                ),
            )
        if op == "callers" and "No affected nodes" in text:
            name = symbol.rpartition("::")[2].split(".")[-1].removesuffix("()")
            return ToolResult.success(
                text + "\n" + _NO_CALLERS.format(name=name),
                fix=f"Confirm with search_repo pattern '\\.{name}\\(' before relying on it.",
            )
        return ToolResult.success(text)

    return {"code_graph": code_graph}


def _path(graph: CodeGraph, pin, symbol: str, target: str, start: str) -> str | ToolResult:
    """op=path, answered here rather than by `graphify path` (see `resolve`)."""
    goal = pin(target)
    if isinstance(goal, ToolResult):
        return goal
    nodes, _ = graph._load()
    unknown = [name for name, nid in ((symbol, start), (target, goal)) if nid not in nodes]
    if unknown:
        return f"No node matching {', '.join(repr(u) for u in unknown)} found."
    found = graph.path(start, goal)
    if found is None:
        return f"No path between {symbol} and {target}, in either direction."
    hops, directed = found
    return _render_path(graph, hops, directed, start, goal)
