"""AGENTS.md: the repository's own instructions for agents, read and kept current.

AGENTS.md is the one instruction file every coding agent now agrees on -- the
spec at https://agents.md, stewarded by the Agentic AI Foundation, and read
natively by Codex, Copilot, Cursor, Jules, Zed, Warp and Claude Code. A team
writes its build commands, conventions and no-go areas there once, and every
agent that opens the repository starts from them instead of rediscovering them
at a hundred thousand prompt tokens a turn.

This module is the whole of it for dakcoder, in three parts.

Reading
-------
``load`` assembles the block the context manager pins under the system prompt:

- **Where.** A user-level file (``$DAKCODER_HOME/AGENTS.md``, default
  ``~/.dakcoder``), then one file per directory from the git root down to the
  workspace root -- Codex's walk, so a service opened as a subdirectory of a
  monorepo still gets the monorepo's rules. Ordered general to specific, so the
  closest file is read last and wins, which is what the spec means by "the
  closest AGENTS.md to the edited file wins".
- **Which file.** Per directory, ``AGENTS.override.md`` if present and not
  empty, else ``AGENTS.md``, else the first fallback (``CLAUDE.md``,
  ``GEMINI.md``, ``AGENT.md``; ``.github/copilot-instructions.md`` at the
  repository root). One per directory, never two copies of one team's rules. A
  personal ``AGENTS.local.md`` is appended after it -- the ``CLAUDE.local.md``
  idea, for preferences that are not the team's.
- **Imports.** ``@path/to/file.md`` pulls another file in, relative to the file
  that names it, four levels deep, cycle-safe, confined to the repository (or
  the user directory, for the user file). Not inside code, and only when the
  file exists, so an e-mail address or ``@skill:...`` is left alone.
- **How much.** 32 KiB altogether (Codex's ``project_doc_max_bytes``), general
  files first; what does not fit is cut at a line and *named*, never dropped in
  silence.

Files *below* the workspace root are not read up front -- a monorepo can have
hundreds. ``NestedInstructions`` surfaces a directory's AGENTS.md the first time
a tool touches a path inside it, as a hook note beside that tool's result, and
again after a compaction has hidden the first one.

Writing
-------
``update_agents_md`` is how a session keeps the file current: add a note, fix
one that turned out wrong, remove a stale one. Every note lives in one fenced
section of the root AGENTS.md (``dakcoder:notes``), under a fixed set of
headings. Everything outside the fence is the developers' and is never
rewritten -- a note that contradicts a developer's line is reported, not
"fixed". ``finish`` carries an optional ``remember`` list that goes through the
same editor, so a run can leave its lesson behind in the call that ends it
without paying a turn for it.

Refused outright: anything shaped like a credential, notes longer than a line,
and growth past a cap -- a memory that only grows stops being read.

Paths
-----
``AGENTS.md`` is committed and shared, so it is written at the workspace root
and never under ``.dakcoder/``, which ignores itself.
"""

from __future__ import annotations

import logging
import os
import re
import tempfile
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from dakcoder_shared.envelope import ToolResult

from .router import Invocation

__all__ = [
    "FILENAME",
    "HANDLERS",
    "Instructions",
    "NestedInstructions",
    "NoteError",
    "SECTIONS",
    "Source",
    "apply_notes",
    "approval_required",
    "enabled",
    "load",
]

log = logging.getLogger(__name__)

FILENAME = "AGENTS.md"
OVERRIDE = "AGENTS.override.md"
LOCAL = "AGENTS.local.md"

#: Read when a directory has no AGENTS.md. The names other agents use for the
#: same file, so a repository set up for one of them is not a blank page here.
#: ``DAKCODER_PROJECT_DOC_FALLBACKS`` replaces the list; empty turns it off.
DEFAULT_FALLBACKS: tuple[str, ...] = ("CLAUDE.md", "GEMINI.md", "AGENT.md")

#: Fallbacks that only mean something at the repository root.
ROOT_FALLBACKS: tuple[str, ...] = (".github/copilot-instructions.md",)

#: The whole pinned block, in bytes of instruction text. Codex's default. About
#: 8,000 tokens at the worst, in a head that is paid once per run and cached.
MAX_BYTES = 32 * 1024

#: One nested file surfaced beside a tool result. Smaller, because it is paid
#: in the working set rather than the cached head.
NESTED_MAX_BYTES = 8 * 1024

#: How deep ``@path`` imports may chain. Claude Code's limit.
IMPORT_DEPTH = 4

#: How far up the walk looks for a git root before giving up on one.
MAX_ANCESTORS = 16

#: Directories whose AGENTS.md files are never surfaced: vendored code carries
#: other projects' instructions, and ``.dakcoder`` is this runtime's own.
SKIP_DIRS = frozenset({".git", ".dakcoder", "vendor", "node_modules", "third_party"})

# ── the managed section ─────────────────────────────────────────────────────

#: The fence around what sessions write. The words in the opening marker are
#: for the developer reading the file; only the prefix is matched.
BEGIN = "<!-- dakcoder:notes:begin"
END = "<!-- dakcoder:notes:end -->"
_BEGIN_LINE = (
    f"{BEGIN} -- kept current by dakcoder sessions; edit freely, one line per note -->"
)
_HEADING = "## Notes from dakcoder sessions"

#: The headings a note may go under, in the order they are written. Fixed, so
#: two sessions filing the same kind of fact put it in the same place, and
#: named after what the AGENTS.md spec recommends a file cover.
SECTIONS: dict[str, str] = {
    "commands": "Commands",
    "conventions": "Conventions",
    "testing": "Testing",
    "architecture": "Architecture",
    "gotchas": "Gotchas",
    "never": "Never",
}

#: One note is one line. A paragraph is a document, and belongs in the part of
#: the file the developers write.
MAX_NOTE_CHARS = 300
#: The section as a whole. Past either, an add is refused until something stale
#: is replaced or removed -- the cap is what keeps the file worth reading.
MAX_NOTES = 60
MAX_SECTION_BYTES = 12 * 1024

#: Shapes a credential takes. Deliberately broad: a false refusal costs the
#: model one rewording, a false accept commits a key to a shared file.
_SECRETS = (
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"\b(?:ghp|gho|ghs|ghu|github_pat)_[A-Za-z0-9_]{20,}"),
    re.compile(r"\bglpat-[A-Za-z0-9_-]{20,}"),
    re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{20,}"),
    re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\."),
    re.compile(
        r"(?i)\b(?:password|passwd|pwd|secret|api[_-]?key|access[_-]?key|token|"
        r"client[_-]?secret)\b\s*[:=]\s*['\"]?[^\s'\"<>{}$]{6,}"
    ),
    re.compile(r"(?i)\b[a-z][a-z0-9+.-]*://[^\s:/@]+:[^\s@/]+@"),
)


def enabled(env: Mapping[str, str] | None = None) -> bool:
    """``DAKCODER_PROJECT_DOCS=0`` turns the whole feature off."""
    env = os.environ if env is None else env
    return env.get("DAKCODER_PROJECT_DOCS", "1").strip().lower() not in ("0", "false", "off", "no")


def approval_required(env: Mapping[str, str] | None = None) -> bool:
    """Whether a session's edit to AGENTS.md waits for the developer.

    ``auto`` by default: the file is committed, so every edit a session makes
    is in the next ``git diff`` and in the chat as the call that made it, and a
    memory that asks permission for every line is one nobody keeps. ``ask``
    (the ``dakcoder.agentsMd.approval`` setting) puts each edit in front of the
    developer first, the way a protected path is.
    """
    env = os.environ if env is None else env
    return env.get("DAKCODER_AGENTS_MD_APPROVAL", "auto").strip().lower() == "ask"


def _max_bytes(env: Mapping[str, str]) -> int:
    raw = env.get("DAKCODER_PROJECT_DOC_MAX_BYTES", "").strip()
    if not raw:
        return MAX_BYTES
    try:
        return max(0, int(raw))
    except ValueError:
        return MAX_BYTES


def _fallbacks(env: Mapping[str, str]) -> tuple[str, ...]:
    raw = env.get("DAKCODER_PROJECT_DOC_FALLBACKS")
    if raw is None:
        return DEFAULT_FALLBACKS
    return tuple(n.strip() for n in raw.split(",") if n.strip() and "/" not in n and "\\" not in n)


def _home(env: Mapping[str, str]) -> Path | None:
    raw = env.get("DAKCODER_HOME", "").strip()
    if raw:
        return Path(raw).expanduser()
    try:
        return Path.home() / ".dakcoder"
    except (RuntimeError, KeyError):
        return None


# ── reading ─────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class Source:
    """One file that went into the block, as the developer would name it."""

    label: str
    scope: str  # "user", "project", "local", "import", "directory"
    bytes: int
    truncated: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "file": self.label,
            "scope": self.scope,
            "bytes": self.bytes,
            "truncated": self.truncated,
        }


@dataclass(frozen=True, slots=True)
class Instructions:
    """The rendered block and what it was built from."""

    text: str
    sources: tuple[Source, ...] = ()
    #: Files that were found and did not fit in the byte budget.
    dropped: tuple[str, ...] = ()
    #: Whether the workspace root has an AGENTS.md of its own.
    exists: bool = False

    def summary(self) -> dict[str, Any]:
        return {
            "sources": [s.as_dict() for s in self.sources],
            "dropped": list(self.dropped),
            "exists": self.exists,
        }


def _clean(text: str) -> str:
    # Normalised on read for the same reason the system prompt is: a head whose
    # bytes depend on the reader's git configuration is a different cache key
    # on a colleague's machine for a file neither of them edited.
    return text.lstrip("\ufeff").replace("\r\n", "\n").replace("\r", "\n").strip()


def _read(path: Path, limit: int) -> tuple[str, bool] | None:
    """The file's text, cut to ``limit`` bytes at a line; ``None`` if unreadable or empty."""
    try:
        if not path.is_file():
            return None
        with path.open("rb") as fh:
            raw = fh.read(limit + 1)
    except OSError:
        return None
    cut = len(raw) > limit
    if cut:
        raw = raw[:limit]
        newline = raw.rfind(b"\n")
        if newline > 0:
            raw = raw[:newline]
    text = _clean(raw.decode("utf-8", errors="replace"))
    if not text:
        return None
    return text, cut


def _git_root(start: Path) -> Path | None:
    here = start
    for _ in range(MAX_ANCESTORS):
        if (here / ".git").exists():
            return here
        if here.parent == here:
            return None
        here = here.parent
    return None


def _within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
    except (OSError, ValueError):
        return False
    return True


def pick(directory: Path, fallbacks: Sequence[str], *, at_root: bool) -> list[tuple[Path, str]]:
    """The instruction files for one directory, in the order they are read.

    At most one team file -- override, then AGENTS.md, then a fallback -- and
    then the personal one. Public because the nested loader and the tests ask
    the same question.
    """
    out: list[tuple[Path, str]] = []
    names = [OVERRIDE, FILENAME, *fallbacks, *(ROOT_FALLBACKS if at_root else ())]
    for name in names:
        candidate = directory / name
        try:
            if candidate.is_file() and candidate.stat().st_size > 0:
                out.append((candidate, "project"))
                break
        except OSError:
            continue
    local = directory / LOCAL
    try:
        if local.is_file() and local.stat().st_size > 0:
            out.append((local, "local"))
    except OSError:
        pass
    return out


# `@path` at the start of a line or after whitespace or an opening bracket,
# naming something with an extension. `@skill:x`, `@user` and `a@b.com` do not
# match: the first has a colon, the second no dot, the third no boundary.
_IMPORT = re.compile(r"(?:^|(?<=[\s(\[]))@((?:~/|\.{1,2}/)?[\w\-./]+\.[A-Za-z0-9]+)(?=$|[\s),.;:\]])", re.M)
_FENCE = re.compile(r"^\s*(```|~~~)")
_SPAN = re.compile(r"`[^`\n]*`")


def _imports(text: str) -> list[str]:
    """The ``@path`` references in ``text``, outside fences and code spans."""
    found: list[str] = []
    fenced = False
    for line in text.split("\n"):
        if _FENCE.match(line):
            fenced = not fenced
            continue
        if fenced:
            continue
        for match in _IMPORT.finditer(_SPAN.sub("", line)):
            if match.group(1) not in found:
                found.append(match.group(1))
    return found


class _Budget:
    def __init__(self, total: int) -> None:
        self.left = total

    def take(self, path: Path) -> tuple[str, bool] | None:
        got = _read(path, self.left)
        if got is None:
            return None
        text, cut = got
        # A file that had to be cut spent the budget, whatever a partial line
        # left over: the next file would otherwise arrive as a few bytes.
        self.left = 0 if cut else self.left - len(text.encode("utf-8"))
        return text, cut


def _label(path: Path, root: Path, home: Path | None) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except (OSError, ValueError):
        pass
    if home is not None:
        try:
            return "~/" + path.resolve().relative_to(home.resolve().parent).as_posix()
        except (OSError, ValueError):
            pass
    try:
        return Path(os.path.relpath(path.resolve(), root.resolve())).as_posix()
    except (OSError, ValueError):
        return path.name


def load(workspace: Path, *, env: Mapping[str, str] | None = None) -> Instructions:
    """Read every instruction file that applies to ``workspace`` and render the block.

    Never raises: a file that cannot be read is a file that is not there, and
    the block always comes back, because it is also where the model is told to
    keep the file current -- a repository without one needs that sentence most.
    """
    env = os.environ if env is None else env
    root = workspace.resolve()
    if not enabled(env):
        return Instructions(text="")

    home = _home(env)
    fallbacks = _fallbacks(env)
    budget = _Budget(_max_bytes(env))
    boundary = _git_root(root) or root

    chain: list[tuple[Path, str]] = []
    if home is not None:
        for name in (OVERRIDE, FILENAME):
            candidate = home / name
            if candidate.is_file() and _read(candidate, 1) is not None:
                chain.append((candidate, "user"))
                break

    directories: list[Path] = [root]
    here = root
    while here != boundary and here.parent != here:
        here = here.parent
        directories.append(here)
    for directory in reversed(directories):
        chain.extend(pick(directory, fallbacks, at_root=directory == boundary))

    sources: list[Source] = []
    sections: list[str] = []
    dropped: list[str] = []
    seen: set[Path] = set()

    def add(path: Path, scope: str, depth: int) -> None:
        resolved = path.resolve()
        if resolved in seen:
            return
        seen.add(resolved)
        label = _label(path, root, home)
        if budget.left <= 0:
            if _read(path, 1) is not None:
                dropped.append(label)
            return
        got = budget.take(path)
        if got is None:
            return
        text, cut = got
        title = {
            "user": f"{label} (yours, every repository)",
            "local": f"{label} (yours, this repository)",
            "import": f"{label} (imported)",
        }.get(scope, label)
        body = text
        if cut:
            body += f"\n\n[{label} was cut here to fit the {_max_bytes(env) // 1024} KiB limit.]"
        sections.append(f"## {title}\n{body}")
        sources.append(Source(label, scope, len(text.encode("utf-8")), cut))
        if depth >= IMPORT_DEPTH:
            return
        fence = home if scope == "user" and home is not None else boundary
        for ref in _imports(text):
            target = (home.parent / ref[2:]) if ref.startswith("~/") and home else path.parent / ref
            if not target.is_file() or not _within(target, fence):
                continue
            add(target, "import", depth + 1)

    for path, scope in chain:
        add(path, scope, 0)

    exists = (root / FILENAME).is_file()
    return Instructions(
        text=render(sections, exists=exists, dropped=dropped),
        sources=tuple(sources),
        dropped=tuple(dropped),
        exists=exists,
    )


_PREAMBLE = (
    "# Project instructions (AGENTS.md)\n"
    "The repository's own instructions for agents. Follow them here: its "
    "commands, conventions and no-go areas, and run the checks they name before "
    "you call work done. Where two disagree, the later (more specific) one wins. "
    "The developer's messages in this session override them, and none of them "
    "relaxes the rules above: the gate, credentials, DDL."
)

_UPKEEP = (
    "Keep AGENTS.md current; the next session starts from it. When you learn "
    "something it would otherwise rediscover -- a command that works, a "
    "convention the developer states, a trap you hit -- record it: "
    "`update_agents_md` mid-run, or `remember` on `finish`. Fix or remove a "
    "note you find wrong. One line each; never a secret or a detail of this "
    "task only."
)


def render(sections: Sequence[str], *, exists: bool, dropped: Sequence[str] = ()) -> str:
    """The block as the model reads it."""
    parts = [_PREAMBLE, _UPKEEP]
    if not exists:
        parts.append(
            "This repository has no AGENTS.md yet. Your first note creates it; "
            "a developer can ask for a full one with /init."
        )
    parts.extend(sections)
    if dropped:
        parts.append(
            "Not loaded, over the size limit: "
            + ", ".join(dropped)
            + ". Read them with read_file if the work is in their directory."
        )
    return "\n\n".join(parts)


# ── nested files, surfaced on first touch ───────────────────────────────────


class NestedInstructions:
    """Surfaces a subdirectory's AGENTS.md when a tool first touches a path in it.

    An after-tool hook, so it costs nothing on a turn that touches no new
    directory and the note arrives next to the result it is about -- which is
    where Claude Code and Gemini put theirs. Surfaced once, and again after a
    compaction, since the recap does not carry the file's text and a rule that
    silently stops applying half-way through a run is worse than one read twice.
    """

    def __init__(
        self,
        workspace: Path,
        *,
        compactions: Callable[[], int] = lambda: 0,
        env: Mapping[str, str] | None = None,
    ) -> None:
        self.root = workspace.resolve()
        self._env = os.environ if env is None else env
        self._fallbacks = _fallbacks(self._env)
        self._compactions = compactions
        self._epoch = 0
        self._shown: set[str] = set()

    def _directories(self, rel: str) -> list[Path]:
        """Directories strictly below the root, down to the one holding ``rel``."""
        target = (self.root / rel).resolve()
        if not _within(target, self.root):
            return []
        parts = target.relative_to(self.root).parts
        if not target.is_dir():
            parts = parts[:-1]
        out: list[Path] = []
        here = self.root
        for part in parts:
            if part in SKIP_DIRS:
                break
            here = here / part
            out.append(here)
        return out

    def notes_for(self, paths: Iterable[str]) -> str:
        """The instruction text a call on ``paths`` has not yet been shown."""
        if not enabled(self._env):
            return ""
        epoch = self._compactions()
        if epoch != self._epoch:
            self._epoch = epoch
            self._shown.clear()
        blocks: list[str] = []
        for rel in paths:
            for directory in self._directories(rel):
                key = directory.relative_to(self.root).as_posix()
                if key in self._shown:
                    continue
                self._shown.add(key)
                for path, _scope in pick(directory, self._fallbacks, at_root=False):
                    got = _read(path, NESTED_MAX_BYTES)
                    if got is None:
                        continue
                    text, cut = got
                    label = path.relative_to(self.root).as_posix()
                    tail = f"\n[{label} was cut at {NESTED_MAX_BYTES // 1024} KiB.]" if cut else ""
                    blocks.append(
                        f"Instructions from {label}, for everything under {key}/ "
                        f"(they add to, and where they differ override, the ones "
                        f"above):\n{text}{tail}"
                    )
        return "\n\n".join(blocks)

    def after(self, context: Any, result: ToolResult) -> Any:
        """The ``Hooks.after`` callable."""
        from ..hooks import AfterTool

        if not result.ok:
            return None
        args = context.arguments if isinstance(context.arguments, dict) else {}
        paths: list[str] = []
        if isinstance(args.get("path"), str):
            paths.append(args["path"])
        if isinstance(args.get("paths"), str):
            paths.extend(p.strip() for p in args["paths"].split(",") if p.strip())
        paths = [p for p in paths if p and not os.path.isabs(p) and ".." not in Path(p).parts]
        if not paths:
            return None
        note = self.notes_for(paths)
        return AfterTool(note=note) if note else None


# ── writing ─────────────────────────────────────────────────────────────────


class NoteError(ValueError):
    """An edit that cannot be made, with what to do instead."""

    def __init__(self, message: str, fix: str = "") -> None:
        super().__init__(message)
        self.fix = fix


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().lower().rstrip(".")


def _note(text: str) -> str:
    """One note, cleaned: a single line, no bullet of its own."""
    line = re.sub(r"\s+", " ", text).strip()
    line = re.sub(r"^(?:[-*+]|\d+[.)])\s+", "", line)
    if not line:
        raise NoteError("The note is empty.", fix="Say the fact in one line.")
    if len(line) > MAX_NOTE_CHARS:
        raise NoteError(
            f"The note is {len(line)} characters; a note is one line of at most {MAX_NOTE_CHARS}.",
            fix="Keep the rule and drop the story of how you found it.",
        )
    for pattern in _SECRETS:
        if pattern.search(line):
            raise NoteError(
                "The note looks like it holds a credential, and AGENTS.md is committed.",
                fix="Name where the value comes from (an env var, a vault path), never the value.",
            )
    return line


@dataclass
class _Section:
    """The fenced block, parsed. Unknown lines are kept where they were."""

    notes: dict[str, list[str]] = field(default_factory=lambda: {k: [] for k in SECTIONS})
    extra: dict[str, list[str]] = field(default_factory=dict)

    @classmethod
    def parse(cls, lines: Sequence[str]) -> _Section:
        out = cls()
        by_title = {v.lower(): k for k, v in SECTIONS.items()}
        current: str | None = None
        for line in lines:
            stripped = line.strip()
            if not stripped or stripped == _HEADING:
                continue
            if stripped.startswith("### "):
                title = stripped[4:].strip()
                current = by_title.get(title.lower(), title)
                if current not in out.notes:
                    out.extra.setdefault(current, [])
                continue
            key = current or "conventions"
            bucket = out.notes.get(key)
            if bucket is not None and stripped.startswith(("- ", "* ")):
                bucket.append(stripped[2:].strip())
            elif bucket is not None:
                bucket.append(stripped)
            else:
                out.extra[key].append(line.rstrip())
        return out

    def count(self) -> int:
        return sum(len(v) for v in self.notes.values())

    def render(self) -> list[str]:
        lines = [_BEGIN_LINE, _HEADING]
        for key, title in SECTIONS.items():
            if self.notes[key]:
                lines.append("")
                lines.append(f"### {title}")
                lines.extend(f"- {n}" for n in self.notes[key])
        for title, body in self.extra.items():
            lines.append("")
            lines.append(f"### {title}")
            lines.extend(body)
        lines.append(END)
        return lines

    def find(self, old: str) -> list[tuple[str, int]]:
        want = _norm(old)
        if not want:
            return []
        exact = [(k, i) for k, v in self.notes.items() for i, n in enumerate(v) if _norm(n) == want]
        if exact:
            return exact
        return [(k, i) for k, v in self.notes.items() for i, n in enumerate(v) if want in _norm(n)]


def _split(text: str) -> tuple[list[str], list[str], list[str]]:
    """The file as (before, fenced, after), by line; fenced is empty if there is no fence."""
    lines = text.split("\n")
    start = next((i for i, line in enumerate(lines) if line.strip().startswith(BEGIN)), None)
    if start is None:
        return lines, [], []
    end = next((i for i in range(start + 1, len(lines)) if lines[i].strip() == END), None)
    if end is None:
        # An unclosed fence: treat everything after it as ours only up to the
        # next heading at level one or two that is not our own, so a developer's
        # later section is never swallowed.
        end = next(
            (
                i
                for i in range(start + 1, len(lines))
                if re.match(r"^#{1,2} ", lines[i]) and lines[i].strip() != _HEADING
            ),
            len(lines),
        )
        return lines[:start], lines[start + 1 : end], lines[end:]
    return lines[:start], lines[start + 1 : end], lines[end + 1 :]


def _new_file(directory: Path, fallbacks: Sequence[str]) -> list[str]:
    lines = [
        "# AGENTS.md",
        "",
        "Instructions for AI coding agents working in this repository "
        "(see https://agents.md). Developers own everything outside the "
        "dakcoder notes section below.",
    ]
    # A repository set up for another agent keeps those rules: AGENTS.md now
    # shadows the fallback, so it imports it instead of silently hiding it.
    for name in (*fallbacks, *ROOT_FALLBACKS):
        if (directory / name).is_file():
            lines += ["", f"@{name}"]
            break
    return lines


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".agents-md-", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as fh:
            fh.write(text)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


@dataclass(frozen=True, slots=True)
class Edit:
    """What an edit did, for the tool result and the event."""

    file: str
    changed: tuple[str, ...]
    skipped: tuple[str, ...]
    created: bool
    total: int


def apply_notes(
    workspace: Path,
    *,
    op: str,
    section: str = "",
    notes: Sequence[str] = (),
    old: str = "",
    scope: str = "project",
    env: Mapping[str, str] | None = None,
) -> Edit:
    """Add, replace or remove notes in the fenced section. Raises ``NoteError``."""
    env = os.environ if env is None else env
    root = workspace.resolve()
    name = LOCAL if scope == "local" else FILENAME
    path = root / name
    named = bool(section)
    section = section or "conventions"
    if section not in SECTIONS:
        raise NoteError(
            f"There is no section {section!r}.",
            fix=f"Use one of: {', '.join(SECTIONS)}.",
        )

    try:
        # Bytes, not `read_text`: universal newlines would hide a CRLF file's
        # line endings, and it would come back rewritten LF throughout.
        raw = path.read_bytes().decode("utf-8", errors="replace") if path.is_file() else ""
    except OSError as exc:
        raise NoteError(f"{name} could not be read: {exc}.") from exc
    newline = "\r\n" if "\r\n" in raw else "\n"
    created = not raw.strip()
    text = _clean(raw)

    if created:
        before = _new_file(root, _fallbacks(env)) if scope != "local" else [
            "# AGENTS.local.md",
            "",
            "Your own instructions for agents in this repository. Keep this file "
            "out of git.",
        ]
        before.append("")
        fenced: list[str] = []
        after: list[str] = []
    else:
        before, fenced, after = _split(text)
        if not fenced and not any(line.strip().startswith(BEGIN) for line in before):
            before = before + [""]
    block = _Section.parse(fenced)
    outside = {_norm(line.lstrip("-*+ ").strip()) for line in (*before, *after) if line.strip()}

    changed: list[str] = []
    skipped: list[str] = []

    if op == "add":
        cleaned = [_note(n) for n in notes if n.strip()]
        if not cleaned:
            raise NoteError("There is nothing to add.", fix="Put the note in `text`.")
        present = {_norm(n) for v in block.notes.values() for n in v} | outside
        for note in cleaned:
            if _norm(note) in present:
                skipped.append(note)
                continue
            present.add(_norm(note))
            block.notes[section].append(note)
            changed.append(note)
        if block.count() > MAX_NOTES:
            raise NoteError(
                f"{name} would hold {block.count()} notes; the limit is {MAX_NOTES}.",
                fix="Replace or remove a stale note first (op=replace or op=remove).",
            )
    elif op in ("replace", "remove"):
        if not old.strip():
            raise NoteError(f"op={op} needs `old`: the note to {op}.")
        hits = block.find(old)
        if not hits:
            if _norm(old) in outside or any(_norm(old) in o for o in outside):
                raise NoteError(
                    f"That line is in the part of {name} the developers write, "
                    "which sessions never edit.",
                    fix="Say in your answer what is wrong with it and what it should say.",
                )
            raise NoteError(
                f"No note in {name} matches {old!r}.",
                fix="Quote the note as it is written in the notes section.",
            )
        if len(hits) > 1:
            raise NoteError(
                f"{len(hits)} notes match {old!r}.",
                fix="Quote more of the one you mean.",
            )
        key, index = hits[0]
        was = block.notes[key].pop(index)
        if op == "replace":
            cleaned = [_note(n) for n in notes if n.strip()]
            if len(cleaned) != 1:
                raise NoteError("op=replace needs exactly one line in `text`.")
            # Kept where it was unless a section was named for it.
            target = section if named else key
            if target == key:
                block.notes[key].insert(index, cleaned[0])
            else:
                block.notes[target].append(cleaned[0])
            changed.append(f"{was} -> {cleaned[0]}")
        else:
            changed.append(was)
    else:
        raise NoteError(f"There is no op {op!r}.", fix="Use add, replace or remove.")

    if not changed:
        return Edit(name, (), tuple(skipped), False, block.count())

    body = block.render()
    size = len("\n".join(body).encode("utf-8"))
    if size > MAX_SECTION_BYTES:
        raise NoteError(
            f"The notes section would be {size // 1024} KiB; the limit is "
            f"{MAX_SECTION_BYTES // 1024} KiB.",
            fix="Replace or remove stale notes first, or shorten this one.",
        )
    while before and not before[-1].strip() and len(before) > 1 and not before[-2].strip():
        before.pop()
    lines = [*before, *body, *after]
    if lines and lines[-1] != "":
        lines.append("")
    _write(path, newline.join(lines))
    return Edit(name, tuple(changed), tuple(skipped), created, block.count())


# ── the tool ────────────────────────────────────────────────────────────────


def _describe(edit: Edit, op: str, section: str) -> str:
    if not edit.changed:
        return (
            f"Nothing changed: {edit.file} already says that. "
            "It is in force; carry on with the task."
        )
    verb = {"add": "Added to", "replace": "Replaced in", "remove": "Removed from"}[op]
    where = f" ({SECTIONS.get(section or 'conventions', section)})" if op == "add" else ""
    listed = "\n".join(f"- {c}" for c in edit.changed)
    lines = [f"{verb} {edit.file}{where}{', which was created' if edit.created else ''}:", listed]
    if edit.skipped:
        lines.append(f"Already there, skipped: {len(edit.skipped)}.")
    lines.append("It applies from now on and is pinned from the next message.")
    return "\n".join(lines)


def update_agents_md(inv: Invocation) -> ToolResult:
    """Add, fix or remove a note in AGENTS.md's dakcoder section."""
    op = str(inv.arg("op") or "add").strip().lower()
    section = str(inv.arg("section") or "").strip().lower()
    scope = str(inv.arg("scope") or "project").strip().lower()
    text = str(inv.arg("text") or "")
    notes = [line for line in text.replace("\r\n", "\n").split("\n") if line.strip()]
    try:
        edit = apply_notes(
            inv.workspace.root,
            op=op,
            section=section,
            notes=notes,
            old=str(inv.arg("old") or ""),
            scope=scope,
        )
    except NoteError as exc:
        return ToolResult.failure(str(exc), fix=exc.fix, meta={"dead_end": str(exc)})
    except OSError as exc:
        return ToolResult.failure(f"AGENTS.md could not be written: {exc}.")
    # No `mutations` on purpose. `router.touched` is what the gate scopes itself
    # to and what the plan's progress is read from; a note about the repository
    # is neither work on it nor something `go build` could judge. The edit is
    # visible where it matters: in this call on the chat, and in `git diff`.
    return ToolResult.success(
        _describe(edit, op, section),
        meta={
            "agents_md": {
                "file": edit.file,
                "op": op,
                "changed": list(edit.changed),
                "created": edit.created,
                "notes": edit.total,
            }
        },
    )


def remember(workspace: Path, notes: Sequence[str]) -> str:
    """``finish``'s ``remember`` field: add each line as a note, never failing the finish.

    Under ``ask`` the notes are not written -- ``finish`` has no approval card
    to hang them on -- and are handed back for the answer instead, so the
    developer sees them and can say yes.
    """
    lines = [n for n in notes if isinstance(n, str) and n.strip()]
    if not lines:
        return ""
    if approval_required():
        listed = "\n".join(f"- {n.strip()}" for n in lines)
        return f"Suggested for AGENTS.md (not saved; approval is on):\n{listed}"
    saved: list[str] = []
    refused: list[str] = []
    for line in lines:
        section, _, rest = line.partition(":")
        key = section.strip().lower()
        note = rest if key in SECTIONS and rest.strip() else line
        key = key if key in SECTIONS and rest.strip() else "conventions"
        try:
            edit = apply_notes(workspace, op="add", section=key, notes=[note])
            saved.extend(edit.changed)
        except (NoteError, OSError) as exc:
            refused.append(f"{line.strip()} ({exc})")
    parts: list[str] = []
    if saved:
        parts.append(f"AGENTS.md: {len(saved)} note(s) saved.")
    if refused:
        parts.append("AGENTS.md: not saved -- " + "; ".join(refused))
    return " ".join(parts)


HANDLERS: dict[str, Any] = {"update_agents_md": update_agents_md}
