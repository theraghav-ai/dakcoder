"""Filesystem tools: read, write, patch, delete, search.

Two decisions shape everything here.

**Line endings are preserved, never normalised.** Every ``.go`` file in the
reference template uses CRLF. A tool that reads CRLF and writes LF turns a
three-line change into a whole-file diff, which makes review impossible and
makes ``git blame`` useless — and it does it silently, on the first edit. So
files are read, converted to LF for matching, and written back in whatever they
arrived as. This mirrors ``gopatch.DetectEOL``/``ToLF``/``ApplyEOL`` on the Go
side; the two must agree, because both edit the same files in the same session.

**Search is pure Python, not ripgrep.** ``rg`` is faster and the plan names it,
but "use rg when installed, fall back otherwise" means the same pattern can
match differently depending on the machine: Rust's regex crate has no
backreferences and no lookaround, Python's has both, and character classes
differ. A pattern that works for one developer and silently returns nothing for
another is worse than being uniformly slower. The services this agent works on
are a few thousand files; Python handles that in well under a second with the
prune list below.
"""

from __future__ import annotations

import re
from pathlib import Path

from dakcoder_shared.envelope import Mutation, MutationKind, ToolResult
from dakcoder_shared.paths import glob_match

from .router import Invocation

__all__ = ["delete_file", "patch_file", "read_file", "search_repo", "write_file", "HANDLERS"]

#: Directories never searched or listed. Everything here is either generated,
#: vendored, or someone else's code — and all of it is enormous relative to its
#: value in a search result.
PRUNE = frozenset(
    {
        ".git",
        ".idea",
        ".vscode",
        "node_modules",
        "vendor",
        "bin",
        "dist",
        "build",
        "__pycache__",
        ".pytest_cache",
        ".dakcoder",
        "testdata",
    }
)

#: Extensions worth searching. An allow-list rather than a deny-list: a new
#: binary format someone commits should be invisible by default, not visible
#: until somebody notices and adds it.
TEXT_SUFFIXES = frozenset(
    {
        ".go",
        ".mod",
        ".sum",
        ".sql",
        ".yaml",
        ".yml",
        ".json",
        ".md",
        ".txt",
        ".tmpl",
        ".proto",
        ".toml",
        ".sh",
        ".ps1",
        ".env",
        ".gitignore",
        ".gitattributes",
        ".dockerignore",
        "",
    }
)

MAX_FILE_BYTES = 2_000_000
_BINARY_PROBE = 8192


class BinaryFile(ValueError):
    pass


def _read_text(path: Path) -> str:
    """Read a file as text, refusing binaries and over-large files.

    The size check comes first because reading a 400 MB file to discover it is
    binary is itself the problem. ``surrogateescape`` keeps a file with one
    stray byte readable rather than failing the whole call — the agent's job is
    to work on real repositories, which contain files nobody has looked at in
    years.
    """
    size = path.stat().st_size
    if size > MAX_FILE_BYTES:
        raise BinaryFile(f"{path.name} is {size:,} bytes, too large to read into context")
    with path.open("rb") as fh:
        head = fh.read(_BINARY_PROBE)
    if b"\x00" in head:
        raise BinaryFile(f"{path.name} looks binary")
    # newline="" disables universal-newline translation. Without it Python turns
    # every CRLF into LF on the way in, _detect_eol then sees a pure-LF file, and
    # the write-back converts the whole thing — which is precisely the failure
    # this module exists to prevent, hidden one layer further down.
    with path.open("r", encoding="utf-8", errors="surrogateescape", newline="") as fh:
        return fh.read()


def _detect_eol(text: str) -> str:
    """The file's dominant line ending.

    Mixed endings exist and are not an error to fix here — a file that is 90%
    CRLF gets CRLF, and the three LF lines someone's editor left behind stay as
    they are rather than becoming a diff nobody asked for.
    """
    crlf = text.count("\r\n")
    lf = text.count("\n") - crlf
    return "\r\n" if crlf > lf else "\n"


def _to_lf(text: str) -> str:
    return text.replace("\r\n", "\n")


def _apply_eol(text: str, eol: str) -> str:
    return _to_lf(text).replace("\n", eol) if eol != "\n" else _to_lf(text)


def _sibling_eol(path: Path, default: str = "\n") -> str:
    """What a *new* file in this directory should use.

    A new ``.go`` file written with LF into a CRLF package is a file that shows
    as entirely changed the first time anything touches it. Matching the
    neighbours is the only answer that does not require a policy nobody set.
    """
    parent = path.parent
    if not parent.is_dir():
        return default
    for sibling in sorted(parent.glob(f"*{path.suffix}"))[:5]:
        if sibling.is_file() and sibling != path:
            try:
                return _detect_eol(_read_text(sibling))
            except (OSError, BinaryFile):
                continue
    return default


# ── read ────────────────────────────────────────────────────────────────────


def read_file(inv: Invocation) -> ToolResult:
    """Read a slice of one file.

    Line numbers are deliberately *not* prefixed to the content. They would help
    the model navigate, but ``patch_file`` matches text exactly, and a model that
    has just read numbered lines will sooner or later include a number in a patch
    anchor — corrupting the file in a way that compiles about half the time. The
    header carries the range instead, which is enough to count from.
    """
    path = inv.absolute()
    if path.is_dir():
        return ToolResult.failure(
            f"{inv.path()} is a directory.",
            fix="Use repo_map to list a directory, or read_file on one file in it.",
        )

    text = _read_text(path)
    lines = _to_lf(text).split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    total = len(lines)

    start = inv.arg("start") or 1
    end = inv.arg("end") or total
    if start > total:
        return ToolResult.failure(
            f"{inv.path()} has {total} lines; start={start} is past the end.",
            fix=f"Read from line 1, or up to {total}.",
        )
    start = max(1, start)
    end = min(total, max(start, end))

    body = "\n".join(lines[start - 1 : end])
    span = f"lines {start}-{end} of {total}" if (start, end) != (1, total) else f"{total} lines"
    header = f"{inv.path()} ({span})"
    return ToolResult.success(f"{header}\n{body}", meta={"lines": total})


# ── write ───────────────────────────────────────────────────────────────────


def write_file(inv: Invocation) -> ToolResult:
    """Create a new file. Refuses to overwrite."""
    rel = inv.path()
    path = inv.absolute()

    if path.exists():
        return ToolResult.failure(
            f"{rel} already exists; write_file will not overwrite it.",
            fix="Use patch_file to change it, or delete_file first if it should go.",
        )

    content = inv.arg("content", "")
    path.parent.mkdir(parents=True, exist_ok=True)
    eol = _sibling_eol(path)
    body = _apply_eol(content, eol)
    if body and not body.endswith(eol):
        # Go tooling, POSIX convention and every diff viewer expect it, and
        # gofmt would add it on the next run anyway — producing a spurious
        # one-line diff attributed to formatting rather than to this write.
        body += eol
    path.write_text(body, encoding="utf-8", newline="")

    return ToolResult.success(
        f"wrote {rel} ({len(content.splitlines())} lines)",
        mutations=[Mutation(rel, MutationKind.CREATE)],
        meta={"eol": "crlf" if eol == "\r\n" else "lf"},
    )


def patch_file(inv: Invocation) -> ToolResult:
    """Replace one exact, unique string.

    The uniqueness requirement is the whole design. A patch that matches twice is
    ambiguous, and picking the first occurrence is how an agent edits the wrong
    method of five that share a body. Failing loud costs one turn; guessing
    costs a debugging session, usually somebody else's.
    """
    rel = inv.path()
    path = inv.absolute()

    if not path.exists():
        return ToolResult.failure(
            f"{rel} does not exist.",
            fix="Use write_file to create it.",
        )

    original = _read_text(path)
    eol = _detect_eol(original)
    text = _to_lf(original)
    old = _to_lf(inv.arg("old", ""))
    new = _to_lf(inv.arg("new", ""))

    if not old:
        return ToolResult.failure(
            "patch_file needs the text to replace.",
            fix="Pass old with the exact lines to change.",
        )
    if old == new:
        return ToolResult.failure(
            "old and new are identical, so this patch would change nothing.",
            fix="Check whether the edit is already applied; read_file to confirm.",
        )

    count = text.count(old)
    if count == 0:
        return ToolResult.failure(
            f"that text does not appear in {rel}.",
            fix=_why_no_match(text, old, rel),
        )
    if count > 1:
        return ToolResult.failure(
            f"that text appears {count} times in {rel}; patch_file needs a unique match.",
            fix="Include the surrounding lines — the function signature above it, "
            "or the closing brace below — until the anchor is unique.",
        )

    patched = text.replace(old, new, 1)
    path.write_text(_apply_eol(patched, eol), encoding="utf-8", newline="")

    delta = len(new.split("\n")) - len(old.split("\n"))
    change = f"{delta:+d} lines" if delta else "same line count"
    return ToolResult.success(
        f"patched {rel} ({change})",
        mutations=[Mutation(rel, MutationKind.MODIFY)],
    )


def _why_no_match(text: str, old: str, rel: str) -> str:
    """Diagnose a failed anchor rather than just reporting it.

    Nearly every miss is one of three things, and each has a different fix. A
    bare "not found" makes the model re-read the file and try again, usually
    with the same mistake; naming the cause fixes it in one turn.
    """
    if old.strip() and old.strip() in text:
        return (
            "The text is there but the leading or trailing whitespace differs. "
            "Copy the indentation exactly as read_file returned it."
        )
    collapsed = re.sub(r"\s+", " ", old.strip())
    if collapsed and re.sub(r"\s+", " ", text).count(collapsed):
        return (
            "The text is there but wrapped differently. Match the line breaks as "
            "they appear in the file."
        )
    first = old.strip().split("\n")[0].strip()
    if first and first in text:
        return (
            f"The first line ({first[:60]!r}) is present but the rest is not — the "
            "file has changed since you read it. Re-read that region first."
        )
    return f"Read {rel} again; it may not contain what you expect."


def delete_file(inv: Invocation) -> ToolResult:
    rel = inv.path()
    path = inv.absolute()
    if path.is_dir():
        return ToolResult.failure(
            f"{rel} is a directory; delete_file only removes files.",
            fix="Delete the files individually, or leave the directory in place.",
        )
    if not path.exists():
        return ToolResult.success(f"{rel} was already gone")
    path.unlink()
    return ToolResult.success(
        f"deleted {rel}",
        mutations=[Mutation(rel, MutationKind.DELETE)],
    )


# ── search ──────────────────────────────────────────────────────────────────


def search_repo(inv: Invocation) -> ToolResult:
    """Regex search over the workspace, pruned and capped."""
    pattern = inv.arg("pattern", "")
    try:
        rx = re.compile(pattern)
    except re.error as exc:
        # Deterministic: the same pattern will fail to parse the same way, so
        # a repeat is answered from the loop's ledger rather than re-parsed.
        return ToolResult.failure(
            f"{pattern!r} is not a valid regular expression: {exc}.",
            fix="Escape the special characters, or search for a plain substring.",
            meta={"dead_end": f"the pattern {pattern!r} does not parse"},
        )

    glob = inv.arg("glob") or ""
    limit = inv.arg("max") or 40
    root = inv.workspace.root

    hits: list[str] = []
    scanned = 0
    truncated = False

    for path in _walk(root):
        rel = path.relative_to(root).as_posix()
        if glob and not glob_match(rel, glob):
            continue
        try:
            text = _read_text(path)
        except (OSError, BinaryFile):
            continue
        scanned += 1
        for number, line in enumerate(_to_lf(text).split("\n"), start=1):
            if rx.search(line):
                hits.append(f"{rel}:{number}: {line.strip()[:200]}")
                if len(hits) >= limit:
                    truncated = True
                    break
        if truncated:
            break

    if not hits:
        # Zero hits with somewhere to go next, not zero hits full stop.
        #
        # A one-line "no matches" is correct and useless: in the field a model
        # answered it by re-phrasing the same search until the run died. What
        # it lacked was anything to aim the next search at, so the answer now
        # carries the workspace's own top level — the model can see what the
        # repository actually contains and search inside something real.
        top = _top_level(root)
        body = f"no matches for {pattern!r} in {scanned} files."
        if top:
            body += (
                "\n\nThe workspace's top level, for aiming the next search:\n"
                + "\n".join(f"  {entry}" for entry in top)
                + "\nLoosen the pattern, or scope a new one with glob."
            )
        return ToolResult.success(body, meta={"scanned": scanned})

    header = f"{len(hits)} match(es) in {scanned} files"
    if truncated:
        header += f" (stopped at {limit}; narrow the pattern or pass glob for the rest)"
    return ToolResult.success(
        header + "\n" + "\n".join(hits),
        truncated=truncated,
        meta={"scanned": scanned, "hits": len(hits)},
    )


def _top_level(root: Path) -> list[str]:
    """The workspace's first level, directories first, prune list applied."""
    try:
        entries = sorted(root.iterdir(), key=lambda p: (p.is_file(), p.name.lower()))
    except OSError:
        return []
    shown: list[str] = []
    for entry in entries:
        if entry.is_dir():
            if entry.name not in PRUNE:
                shown.append(f"{entry.name}/")
        elif entry.suffix.lower() in TEXT_SUFFIXES:
            shown.append(entry.name)
        if len(shown) >= 15:
            break
    return shown


def _walk(root: Path):
    """Depth-first walk with the prune list applied to directories, not results.

    Pruning at the directory level rather than filtering matches afterwards is
    the difference between skipping ``vendor/`` and reading all of it to throw it
    away.
    """
    stack = [root]
    while stack:
        current = stack.pop()
        try:
            entries = sorted(current.iterdir())
        except OSError:
            continue
        for entry in entries:
            if entry.is_dir():
                if entry.name not in PRUNE and not entry.is_symlink():
                    stack.append(entry)
            elif entry.suffix.lower() in TEXT_SUFFIXES:
                yield entry


HANDLERS = {
    "read_file": read_file,
    "write_file": write_file,
    "patch_file": patch_file,
    "delete_file": delete_file,
    "search_repo": search_repo,
}
