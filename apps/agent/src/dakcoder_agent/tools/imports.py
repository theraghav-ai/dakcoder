"""Which of a service's own packages import which.

The direction is a fact of the service, not of the template. The reference
template has `core/port` import `core/domain`; pao-backend has it the other way
round (`core/domain/publicaccts.go` imports `core/port`). Session 3a82e19883f7
followed the template, wrote a `core/port` file importing `core/domain`, and
closed a cycle nothing reported for thirty-seven turns. `repo_map` did not say
which way the arrows already pointed, so there was nothing to follow instead.

Read off the import blocks with a scan rather than `go list`: it has to answer
on a module that does not currently compile, which is when it matters most.
"""

from __future__ import annotations

import re
from pathlib import Path

__all__ = ["import_hints", "import_summary", "importers", "module_path", "package_imports"]

#: Not the service's packages: runtime history, dependencies, generated trees
#: nobody edits by hand.
_SKIP = frozenset({".git", ".dakcoder", "vendor", "node_modules", "testdata"})

_BLOCK = re.compile(r"^import\s*\((.*?)^\)", re.MULTILINE | re.DOTALL)
_SINGLE = re.compile(r'^import\s+(?:[\w.]+\s+)?"([^"]+)"', re.MULTILINE)
_QUOTED = re.compile(r'"([^"]+)"')

#: Enough for a service's internal graph; a monorepo gets the head and a count.
MAX_EDGES = 40


def module_path(root: Path) -> str:
    """The `module` line of ``root/go.mod``, or ``""``."""
    try:
        for line in (root / "go.mod").read_text(encoding="utf-8").splitlines():
            if line.startswith("module "):
                return line.removeprefix("module ").strip()
    except OSError:
        pass
    return ""


def package_imports(root: Path) -> dict[str, set[str]]:
    """``{package dir: {internal package dirs it imports}}``, test files excluded."""
    module = module_path(root)
    if not module:
        return {}
    prefix = module + "/"
    out: dict[str, set[str]] = {}
    for path in root.rglob("*.go"):
        rel = path.relative_to(root)
        if path.name.endswith("_test.go") or _SKIP.intersection(rel.parts[:-1]):
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        found = set(_SINGLE.findall(text))
        for block in _BLOCK.findall(text):
            found.update(_QUOTED.findall(block))
        here = rel.parent.as_posix()
        here = "." if here in ("", ".") else here
        targets = {imp.removeprefix(prefix) for imp in found if imp.startswith(prefix)}
        targets.discard(here)
        if targets:
            out.setdefault(here, set()).update(targets)
    return out


_SPEC = re.compile(r'^\s*(?:([\w.]+)\s+)?"([^"]+)"\s*$', re.MULTILINE)
_PACKAGE = re.compile(r"^package\s+(\w+)", re.MULTILINE)
_UNDEFINED = re.compile(r"undefined: ([A-Za-z_]\w*)\s*$")


def _go_files(root: Path):
    for path in root.rglob("*.go"):
        rel = path.relative_to(root)
        if _SKIP.intersection(rel.parts[:-1]):
            continue
        try:
            yield rel, path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue


def importers(root: Path, package: str, imported: str) -> list[str]:
    """The files in ``package`` (a module-relative dir) that import ``imported``."""
    module = module_path(root)
    if not module:
        return []
    want = f"{module}/{imported}" if imported != "." else module
    out: list[str] = []
    for rel, text in _go_files(root):
        if rel.parent.as_posix() != package or rel.name.endswith("_test.go"):
            continue
        found = set(_SINGLE.findall(text))
        for block in _BLOCK.findall(text):
            found.update(_QUOTED.findall(block))
        if want in found:
            out.append(rel.as_posix())
    return sorted(out)


def import_hints(output: str, root: Path) -> str:
    """For each ``undefined: name`` a compiler reported, how this service imports ``name``.

    The live replay of session 3a82e19883f7 wrote `postgres.NewEmployeeRepository`
    and `config.…` into a handler and rewrote the whole file four times with the
    same two errors. The service knew both answers: `repo/postgres` declares
    `package repository` and every handler imports it as
    `repo "gotemplate/repo/postgres"`, and `config` is imported in seven files.
    Only bare identifiers are looked up -- `undefined: pkg.Name` is a missing
    symbol, not a missing import.
    """
    names = list(dict.fromkeys(
        m.group(1) for line in output.splitlines() if (m := _UNDEFINED.search(line.strip()))
    ))
    if not names:
        return ""
    module = module_path(root)
    by_name: dict[str, dict[str, int]] = {}
    declared: dict[str, str] = {}
    for rel, text in _go_files(root):
        if (pkg := _PACKAGE.search(text)) and rel.parent.as_posix() not in declared:
            declared[rel.parent.as_posix()] = pkg.group(1)
        blocks = _BLOCK.findall(text) + [m.group(0) for m in _SINGLE.finditer(text)]
        for block in blocks:
            for alias, spec in _SPEC.findall(block.replace("import", " ")):
                name = alias or spec.rsplit("/", 1)[-1]
                if name in ("_", "."):
                    continue
                key = f'{alias} "{spec}"' if alias else f'"{spec}"'
                counts = by_name.setdefault(name, {})
                counts[key] = counts.get(key, 0) + 1
    hints: list[str] = []
    for name in names[:6]:
        if name in by_name:
            spec, count = max(by_name[name].items(), key=lambda kv: kv[1])
            hints.append(f"  {name}: this service imports it as {spec} ({count} file(s))")
            continue
        # A directory named like the identifier whose package is called
        # something else -- `repo/postgres` declaring `package repository`.
        for directory, package in sorted(declared.items()):
            if directory.rsplit("/", 1)[-1] == name and package != name and module:
                path = f"{module}/{directory}"
                usual = [
                    s for counts in by_name.values() for s in counts if s.endswith(f'"{path}"')
                ]
                how = f"; this service imports it as {usual[0]}" if usual else ""
                hints.append(
                    f"  {name}: {directory}/ declares `package {package}`, not `{name}`{how}"
                )
                break
    if not hints:
        return ""
    return "How this service imports the undefined names:\n" + "\n".join(hints)


def import_summary(root: Path) -> dict[str, object]:
    """The internal import graph, as fields `repo_map` merges into its JSON.

    Empty when the module has no internal imports. Fields rather than prose
    because `repo_map` answers in JSON and its readers parse it.
    """
    graph = package_imports(root)
    if not graph:
        return {}
    ordered = sorted(graph.items())
    summary: dict[str, object] = {
        "imports": {src: sorted(dst) for src, dst in ordered[:MAX_EDGES]},
        "imports_note": (
            "package -> the service packages it imports. These are this service's "
            "directions, whatever the template does: a new import against one of "
            "these arrows is an import cycle."
        ),
    }
    if len(ordered) > MAX_EDGES:
        summary["imports_omitted"] = len(ordered) - MAX_EDGES
    mutual = sorted(
        f"{a} <-> {b}" for a, targets in graph.items() for b in targets
        if a < b and a in graph.get(b, set())
    )
    if mutual:
        # Said outright: two packages importing each other do not compile, and
        # the pair is where the fix goes.
        summary["import_cycles"] = mutual
    return summary
