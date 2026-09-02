"""Workspace confinement for path arguments.

Every path that reaches a tool comes from model output, and model output is
untrusted input. Not because the model is adversarial — because it is *wrong*
sometimes, and because a prompt-injected repository file can make it wrong on
purpose. ``read_file("../../.ssh/id_rsa")`` is one hallucinated path away at any
moment, and the agent runs on a developer machine with that developer's
credentials.

The rule is single and absolute: a tool path is resolved against the workspace
root, and anything that lands outside is refused. Not sanitised, not clamped —
refused, with the reason named. Silently rewriting a traversal into something
safe teaches the model nothing and hides the attempt.

Three classes of escape are handled, because handling only the obvious one is
the usual mistake:

* **Syntactic** — ``..``, absolute paths, drive letters, UNC shares.
* **Symbolic** — a path that normalises clean but resolves through a symlink
  pointing out of the tree. Caught by resolving *then* re-testing containment,
  which is the only order that works.
* **Windows-specific** — reserved device names (``CON``, ``NUL``, ``COM1``),
  alternate data streams (``file.go:hidden``), and trailing dots or spaces that
  Win32 strips after validation. These are only exploitable on one platform,
  but the agent's primary platform is Windows 11, so they are not hypothetical.

The Windows rules are enforced on every platform. A repository is shared; a path
written on Linux is opened on Windows, and a rule that only fires on the machine
where it is already too late is not a rule.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path, PurePosixPath

__all__ = ["PROTECTED_GLOBS", "PathEscape", "Workspace", "glob_match", "is_protected"]

#: Win32 strips trailing dots and spaces from a component *after* validating it,
#: so "handler." and "handler" address the same file while comparing unequal.
_TRAILING = re.compile(r"[. ]$")

#: Reserved device names. Opening one gets a device, not a file, and on some
#: paths that blocks the process rather than erroring.
_DEVICES = frozenset(
    ["con", "prn", "aux", "nul"]
    + [f"com{n}" for n in range(1, 10)]
    + [f"lpt{n}" for n in range(1, 10)]
)

#: Paths whose modification needs explicit approval (Part A section 7.2, note 1).
#: Each one either changes what the program *is* rather than what it does, or is
#: generated and must be regenerated rather than edited.
PROTECTED_GLOBS: tuple[str, ...] = (
    "**/go.mod",
    "**/go.sum",
    "**/main.go",
    "**/bootstrap/**",
    "**/configs/**",
    "**/db/**",
    "**/*_validator.go",
    ".gitlab-ci.yml",
    "Dockerfile",
    ".git/**",
)


class PathEscape(ValueError):
    """A path argument that would leave the workspace.

    Carries the fix as a separate field because the message goes back to the
    model as a tool error, and a refusal that does not say what to do instead
    costs a whole turn (Part A section 7.1).
    """

    def __init__(self, given: str, reason: str, fix: str) -> None:
        super().__init__(f"refused path {given!r}: {reason}. {fix}")
        self.given = given
        self.reason = reason
        self.fix = fix


@dataclass(frozen=True, slots=True)
class Workspace:
    """A resolved workspace root, and the only way to turn an argument into a path."""

    root: Path

    @classmethod
    def at(cls, root: str | Path) -> Workspace:
        resolved = Path(root).expanduser().resolve()
        if not resolved.is_dir():
            raise ValueError(f"workspace root {resolved} is not a directory")
        return cls(resolved)

    # -- the one entry point ---------------------------------------------

    def resolve(self, given: str) -> Path:
        """Resolve a tool-supplied path, or refuse it.

        Returns an absolute path guaranteed to be inside the workspace. The file
        need not exist: ``write_file`` resolves before creating, and a check that
        required existence would be trivially bypassed by writing *through* a
        symlinked parent.
        """
        self._check_syntax(given)

        candidate = (self.root / self._separators(given)).resolve()
        if not self._contains(candidate):
            symbolic = self._looks_symbolic(given)
            raise PathEscape(
                given,
                "it resolves outside the workspace"
                + (" through a symbolic link" if symbolic else ""),
                f"paths must stay under {self.root.name}/. Use a workspace-relative path.",
            )
        return candidate

    def relative(self, path: Path) -> str:
        """The workspace-relative POSIX form, for display and for ``mutations[]``.

        POSIX separators on every platform: this string crosses the wire to the
        extension, appears in diffs, and is matched against globs. One separator
        everywhere, or three subtly different bugs.
        """
        return path.resolve().relative_to(self.root).as_posix()

    def exists(self, given: str) -> bool:
        try:
            return self.resolve(given).exists()
        except PathEscape:
            return False

    # -- internals -------------------------------------------------------

    @staticmethod
    def _separators(given: str) -> str:
        """A single separator before the path reaches ``Path``.

        A model hosted on Windows writes ``handler\\user.go`` whichever platform
        the runtime is on, and on POSIX ``Path`` reads that as one filename with a
        backslash in it: the tool creates a stray literal-backslash file, the
        ledger and the gate scope disagree about which file was touched, and the
        mutation the developer sees is a file they cannot open. Backslash is a
        legal POSIX filename character, so this trades an unreachable spelling
        for a path that means the same thing on both platforms — the trade the
        rest of the system already assumes (``relative`` returns POSIX form, the
        globs are POSIX, the wire is POSIX).
        """
        return given.replace("\\", "/")

    def _contains(self, candidate: Path) -> bool:
        try:
            candidate.relative_to(self.root)
        except ValueError:
            return False
        return True

    def _looks_symbolic(self, given: str) -> bool:
        """Whether the refusal was a symlink rather than plain ``..``.

        Only sharpens the message. A traversal the model wrote by mistake and a
        traversal through a planted symlink deserve different wording, because
        only one of them is the model's to fix.
        """
        walk = self.root
        for part in PurePosixPath(given.replace("\\", "/")).parts:
            walk = walk / part
            if walk.is_symlink():
                return True
        return False

    @staticmethod
    def _check_syntax(given: str) -> None:
        if not given or not given.strip():
            raise PathEscape(given, "it is empty", "Pass a workspace-relative path.")
        if "\x00" in given:
            raise PathEscape(given, "it contains a NUL byte", "Pass a plain relative path.")

        normalised = given.replace("\\", "/")

        if normalised.startswith("//"):
            raise PathEscape(given, "it is a UNC network path", "Pass a workspace-relative path.")
        if normalised.startswith("/"):
            raise PathEscape(
                given, "it is absolute", "Pass a path relative to the workspace root."
            )
        if re.match(r"^[A-Za-z]:", normalised):
            raise PathEscape(
                given,
                "it names a drive",
                "Pass a path relative to the workspace root, without a drive letter.",
            )

        for part in normalised.split("/"):
            if part in ("", "."):
                continue
            if part == "..":
                raise PathEscape(
                    given,
                    "it walks above the workspace with '..'",
                    "Tools can only reach files inside the workspace.",
                )
            if ":" in part:
                raise PathEscape(
                    given,
                    "it uses an alternate data stream (':')",
                    "Pass a plain file path.",
                )
            if _TRAILING.search(part):
                raise PathEscape(
                    given,
                    "a segment ends in a dot or space, which Windows silently strips",
                    "Remove the trailing character; it does not address the file you mean.",
                )
            if part.split(".")[0].lower() in _DEVICES:
                raise PathEscape(
                    given,
                    f"{part!r} is a reserved Windows device name",
                    "Rename the file; opening it would open a device, not a file.",
                )


def is_protected(rel: str) -> bool:
    """Whether a workspace-relative path needs approval before it is written.

    Matched against the path the tool will actually act on, never the argument as
    written, so no spelling of a protected path can slip past.

    **Case-insensitively**, because the primary platform is Windows and its
    filesystem is case-insensitive: ``dockerfile`` and ``GO.MOD`` address exactly
    the files ``Dockerfile`` and ``go.mod`` name, and a case-sensitive match let a
    write to either skip the approval gate entirely (BUG SH-5b). On Linux the two
    spellings are genuinely different files, so this refuses a little more than it
    must there — which is the safe direction for a gate whose whole job is to
    make a human look.
    """
    lowered = rel.lower()
    return any(glob_match(lowered, pattern.lower()) for pattern in PROTECTED_GLOBS)


@lru_cache(maxsize=256)
def _glob_regex(pattern: str) -> re.Pattern[str]:
    """Translate a glob to an anchored regex.

    Written out rather than delegated to ``fnmatch`` or ``PurePath.match``,
    because both get the two cases that matter here wrong.

    * ``fnmatch`` lets ``*`` cross ``/``, so ``configs/*.yaml`` would match
      ``configs/env/prod.yaml`` — silently widening every scoped pattern.
    * ``PurePath.match`` anchors at the right, so ``go.mod`` matches
      ``vendor/x/go.mod``, and before Python 3.13 its ``**`` is not recursive at
      all — so ``**/bootstrap/**`` matches the directory but not anything two
      levels inside it. That is the failure mode where a check looks correct and
      protects nothing.

    One translation, used for approval globs and for ``search_repo``'s scoping,
    so a pattern means the same thing in both places.
    """
    out: list[str] = []
    i = 0
    while i < len(pattern):
        char = pattern[i]
        if pattern.startswith("**/", i):
            out.append("(?:[^/]+/)*")
            i += 3
        elif pattern.startswith("**", i):
            out.append(".*")
            i += 2
        elif char == "*":
            out.append("[^/]*")
            i += 1
        elif char == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(char))
            i += 1
    return re.compile("".join(out) + r"\Z")


def glob_match(rel: str, pattern: str) -> bool:
    """Whole-path glob match where ``*`` stops at a separator and ``**`` does not."""
    return _glob_regex(pattern).match(rel) is not None
