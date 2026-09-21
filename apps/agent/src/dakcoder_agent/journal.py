"""What a session leaves on disk.

Nothing did. `session.py`'s own docstring said "Persisted first. A crash between
sending and recording would produce an event the client saw and the log does not
have" — and the word meant "appended to a list in this process". A daemon
restart, which a VS Code reload causes, took every transcript with it, and with
them the mutation list `revert` reads (BUG L-7, DOC-1).

Two files per session, under `.dakcoder/sessions/<id>/`:

* `events.jsonl` — one JSON object per stored event, append-only, in id order.
  Transient events are not in it for the same reason they are not in the
  in-memory log: they are superseded by the `assistant` message that follows.
* `session.json` — the summary a list view needs (task, status, timing, the
  mutation list). Rewritten whenever it changes, which is a handful of times per
  run.

Three properties this is written for, in order:

**It must never fail a run.** Every write is best-effort. A full disk, a
read-only checkout or a permission error costs the transcript, not the work the
developer is waiting for.

**It must never slow a turn.** Events are buffered and flushed at the points
where the run is already waiting on something slower than a disk — the end of a
turn, the end of the run — rather than on every append.

**Reading it back must be cheap.** `SessionStore` restores summaries at
startup and reads `events.jsonl` only when someone asks for a transcript. A
hundred finished sessions is a hundred small JSON files, not a hundred
transcripts.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from .undo import ensure_private, session_dir

__all__ = ["Journal", "restore_summaries"]

#: How many events may sit in the buffer before a flush is forced. A turn is
#: roughly five to fifteen events, so this is "about two turns" — small enough
#: that a hard kill loses very little, large enough that the disk is touched a
#: few times a turn rather than a few times a second.
FLUSH_AT = 32


class Journal:
    """The on-disk record of one session."""

    def __init__(self, workspace: Path, session_id: str) -> None:
        self.root = session_dir(workspace, session_id)
        self._events = self.root / "events.jsonl"
        self._meta = self.root / "session.json"
        self._buffer: list[str] = []
        #: Set once a write has failed. Retrying every event on a read-only
        #: checkout would put an exception in the hot path a few times a second.
        self._broken = False

    # -- writing -----------------------------------------------------------

    def append(self, payload: dict[str, Any]) -> None:
        """Buffer one stored event."""
        if self._broken:
            return
        self._buffer.append(json.dumps(payload, separators=(",", ":"), default=str))
        if len(self._buffer) >= FLUSH_AT:
            self.flush()

    def flush(self) -> None:
        """Write what is buffered. Best-effort, and silent about it."""
        if self._broken or not self._buffer:
            return
        pending, self._buffer = self._buffer, []
        try:
            ensure_private(self.root.parents[2])
            self.root.mkdir(parents=True, exist_ok=True)
            with self._events.open("a", encoding="utf-8") as fh:
                fh.write("\n".join(pending) + "\n")
        except OSError:
            self._broken = True

    def write_meta(self, summary: dict[str, Any]) -> None:
        """Replace the session summary. Called when something about it changes."""
        if self._broken:
            return
        try:
            ensure_private(self.root.parents[2])
            self.root.mkdir(parents=True, exist_ok=True)
            tmp = self._meta.with_suffix(".json.tmp")
            tmp.write_text(
                json.dumps(summary, indent=1, sort_keys=True, default=str), encoding="utf-8"
            )
            tmp.replace(self._meta)
        except OSError:
            self._broken = True

    # -- reading -----------------------------------------------------------

    def read_events(self) -> list[dict[str, Any]]:
        """Every event this session recorded, in order.

        A truncated last line — the shape a hard kill leaves — is dropped rather
        than raised on: a transcript missing its final event is worth having.
        """
        out: list[dict[str, Any]] = []
        try:
            with self._events.open("r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        parsed = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(parsed, dict):
                        out.append(parsed)
        except OSError:
            return []
        return out

    def read_meta(self) -> dict[str, Any] | None:
        try:
            parsed = json.loads(self._meta.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return parsed if isinstance(parsed, dict) else None


def restore_summaries(workspace: Path) -> Iterator[dict[str, Any]]:
    """Every session this workspace has a record of, newest first.

    Summaries only. The transcript is read when someone asks for it, so starting
    the daemon in a workspace with a hundred finished sessions costs a hundred
    small reads rather than a hundred transcripts.
    """
    root = workspace / ".dakcoder" / "sessions"
    try:
        entries = sorted(root.iterdir())
    except OSError:
        return

    found: list[dict[str, Any]] = []
    for entry in entries:
        if not entry.is_dir():
            continue
        meta = Journal(workspace, entry.name).read_meta()
        if meta and meta.get("id"):
            found.append(meta)

    found.sort(key=lambda m: str(m.get("created_at") or ""), reverse=True)
    yield from found


def parse_time(raw: Any) -> datetime | None:
    """An ISO timestamp as written by ``as_dict``, or ``None``."""
    if not isinstance(raw, str) or not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
