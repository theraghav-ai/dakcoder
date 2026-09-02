"""Pre-run snapshots, so a revert restores the developer's file rather than HEAD.

Revert used to work by asking git: restore every touched path to HEAD, delete
every touched path HEAD does not have. That is correct exactly when the agent
was the only thing that wrote to the tree since the last commit — and the rest
of this system is careful never to assume that. The whole Baseline design in
``gate.py`` exists because a repository does not start clean; a developer's
uncommitted work in progress is the normal state of the file the agent is being
asked to change.

So the two failures it produced were not edge cases (BUG L-11):

* a developer's uncommitted edit to a file the agent later touched was
  **destroyed** — reset to HEAD along with the agent's change;
* a developer's *untracked* file that the agent merely modified was **deleted**,
  because HEAD does not have it and the git rule reads "absent from HEAD" as
  "the run created it".

The fix is to stop inferring the pre-run state and record it. The router copies
each path's pre-image the first time a mutating tool touches it, and revert
restores that. It makes revert correct on a dirty tree, correct on an untracked
file, and correct in a directory that is not a git repository at all.

The manifest is JSON on disk rather than a dict in memory because the thing it
protects is a developer's work: a daemon restart between the run and the revert
must not turn "restore what was there" back into "reset to HEAD".
"""

from __future__ import annotations

import json
import shutil
from enum import StrEnum
from pathlib import Path

__all__ = ["MAX_SNAPSHOT_BYTES", "PreState", "UndoStore", "ensure_private", "session_dir"]

#: Above this a pre-image is not copied. A 40 MB generated file in the working
#: tree should not cost 40 MB of snapshot per session, and the honest answer for
#: one is to block its revert rather than to half-record it.
MAX_SNAPSHOT_BYTES = 2_000_000


class PreState(StrEnum):
    """What was at a path before the run first touched it."""

    #: A file was there and its bytes are in the snapshot directory.
    FILE = "file"
    #: Nothing was there. The run created it, so a revert deletes it.
    ABSENT = "absent"
    #: A file was there and was too large to copy. Revert must block on it.
    TOO_LARGE = "too_large"
    #: A file was there and could not be read (permissions, a race). Block.
    UNREADABLE = "unreadable"
    #: The run changed this path and no pre-image was taken, because the tool
    #: that changed it did not say which path it would change until afterwards
    #: (`fx_wire`, `govalid_gen` — BUG RG-1). Recorded rather than left absent so
    #: a revert after a restart can say *why* it is blocking, rather than falling
    #: back to the generic "nothing here knows".
    UNRECORDED = "unrecorded"


def session_dir(workspace: Path, session_id: str) -> Path:
    """Where a session's own artefacts live. One place, so cleanup is one rule."""
    return workspace / ".dakcoder" / "sessions" / session_id


def ensure_private(workspace: Path) -> None:
    """Make ``.dakcoder/`` ignore itself.

    The snapshots live inside the developer's working tree — they have to, to be
    on the same filesystem as the files they mirror — and a tool that leaves an
    untracked directory in ``git status`` after every run has changed the
    developer's repository whether it meant to or not. A ``.gitignore``
    containing ``*`` inside our own directory ignores it without touching
    theirs.
    """
    marker = workspace / ".dakcoder" / ".gitignore"
    if marker.exists():
        return
    try:
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text("# Written by dakcoder. This directory is runtime state.\n*\n",
                          encoding="utf-8")
    except OSError:
        pass


class UndoStore:
    """Pre-images for one session's mutated paths.

    Capture is first-write-wins: the second edit of a file must not overwrite the
    snapshot with the agent's own first edit, which would make the revert restore
    the agent's work instead of the developer's.
    """

    def __init__(self, workspace: Path, session_id: str) -> None:
        self.root = session_dir(workspace, session_id) / "undo"
        self.workspace = workspace
        self._manifest_path = self.root / "manifest.json"
        self._manifest: dict[str, str] | None = None

    # -- reading -----------------------------------------------------------

    @property
    def manifest(self) -> dict[str, str]:
        if self._manifest is None:
            try:
                loaded = json.loads(self._manifest_path.read_text(encoding="utf-8"))
                self._manifest = {str(k): str(v) for k, v in loaded.items()}
            except (OSError, ValueError):
                self._manifest = {}
        return self._manifest

    def state(self, rel: str) -> PreState | None:
        """What was at ``rel`` before the run, or ``None`` if nothing recorded it."""
        raw = self.manifest.get(rel)
        try:
            return PreState(raw) if raw else None
        except ValueError:
            return None

    # -- writing -----------------------------------------------------------

    def capture(self, rel: str) -> None:
        """Record what is at ``rel`` now, unless this session already has it.

        Best-effort by design: a snapshot that fails must never fail the tool
        call the developer asked for. The cost of failing here is that revert
        blocks on that path, which is the safe direction.
        """
        if rel in self.manifest:
            return

        source = self.workspace / rel
        try:
            if not source.exists() or source.is_dir():
                self._record(rel, PreState.ABSENT)
                return
            if source.stat().st_size > MAX_SNAPSHOT_BYTES:
                self._record(rel, PreState.TOO_LARGE)
                return
            ensure_private(self.workspace)
            target = self.root / "files" / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
            self._record(rel, PreState.FILE)
        except OSError:
            self._record(rel, PreState.UNREADABLE)

    def note_unsnapshotted(self, rel: str) -> None:
        """Record that the run changed ``rel`` without a pre-image.

        Called after a tool that only names its target in its result. Never
        overwrites a real snapshot: the first write of a path is the one that
        matters, and a tool that mutates a file another tool had already
        snapshotted is covered by that snapshot.
        """
        if rel in self.manifest:
            return
        self._record(rel, PreState.UNRECORDED)

    def restore(self, rel: str) -> bool:
        """Put the pre-image back. False when there is nothing to put back."""
        if self.state(rel) is not PreState.FILE:
            return False
        source = self.root / "files" / rel
        target = self.workspace / rel
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
        except OSError:
            return False
        return True

    def _record(self, rel: str, state: PreState) -> None:
        self.manifest[rel] = str(state)
        try:
            ensure_private(self.workspace)
            self.root.mkdir(parents=True, exist_ok=True)
            self._manifest_path.write_text(
                json.dumps(self.manifest, indent=1, sort_keys=True), encoding="utf-8"
            )
        except OSError:
            # In memory only, then. Revert within this process still works; a
            # revert after a restart blocks, which is the safe direction.
            pass
