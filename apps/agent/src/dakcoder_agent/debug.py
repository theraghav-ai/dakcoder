"""Full-fidelity turn recording: what the model was sent, and what came back.

What this is for
----------------
The event stream answers "what did the agent do". It does not answer the
questions you actually have when a run goes wrong:

* what were the **exact bytes** of the prompt on turn 14;
* what did the loop's own state look like at that moment;
* which ledger answered which call, and what did it say;
* what did the endpoint return, including the parts nothing renders --
  ``finish_reason``, reasoning tokens, how long it took.

None of that was recoverable. ``events.jsonl`` is a display record,
``transcript.jsonl`` is the canonical conversation but not the assembled
request, and the loop's state existed only in memory.

Four seams, not a hundred log lines
-----------------------------------
This module adds no logging to the loop's logic. Everything it records passes
through one of four places that already exist and already funnel:

1. ``AgentLoop.run`` tees every event through one generator -- the funnel that
   was built so the accounting could not miss one. Free.
2. ``llm.complete`` is the *only* place a request leaves the process.
3. ``ContextManager.view`` is the *only* projection, and it already carries
   every derived fact about the context as fields.
4. ``_State.groups`` is the five-way decomposition of the loop's own state.

So the recorder is four calls, and the thing it produces is complete because
those four places are complete.

Why it is cheap
---------------
**The prompt is prefix-stable by design.** That is the whole point of the
layered build -- system, mode and task never move, and the volatile block is
last -- and ``ContextManager.novel_tokens`` exists to measure it. A debug log
can exploit the same property: turn 1 stores the assembled request in full, and
every turn after it stores the number of leading messages that are unchanged
plus the tail that is not.

Measured on the shape a migration run reaches, that is the difference between
~1 MB per turn and ~25 KB per turn. Replay is exact, not approximate:
``wire = previous[:shared] + tail``.

Off by default and free when off: one ``is None`` check per seam. Turn it on
with ``DAKCODER_DEBUG=1``.

Reading it back
---------------
``python -m dakcoder_agent.debug <workspace> <session-id>`` prints a per-turn
report; ``--prompt N`` dumps the reconstructed request for turn N, which is the
one thing no other artefact on disk can give you.
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Iterable, Iterator, Mapping, Sequence
from pathlib import Path
from typing import Any

__all__ = [
    "DebugLog",
    "MAX_DEBUG_BYTES",
    "enabled",
    "prompts",
    "read",
]

#: How much one session's debug log may grow to before it stops recording.
#:
#: A 400-turn run at ~25 KB a turn is ~10 MB, which is fine; a run that somehow
#: defeats the prefix compression is not. When the cap trips the log says so in
#: its last record rather than going quiet, because a truncated debug log that
#: does not admit it is worse than none.
MAX_DEBUG_BYTES = 256 * 1024 * 1024

#: Per-field cap inside a recorded value. The canonical transcript holds tool
#: results whole; this is a debugging view beside it, and a single 400KB build
#: log repeated in both files buys nothing.
MAX_FIELD_CHARS = 200_000


def enabled() -> bool:
    """Whether debug recording is on for this process."""
    raw = os.environ.get("DAKCODER_DEBUG", "").strip().lower()
    return raw not in ("", "0", "false", "no", "off")


def _clip(value: Any) -> Any:
    """Bound one recorded value, saying so where it bites."""
    if isinstance(value, str) and len(value) > MAX_FIELD_CHARS:
        return value[:MAX_FIELD_CHARS] + f"\n[... {len(value) - MAX_FIELD_CHARS:,} more characters]"
    if isinstance(value, Mapping):
        return {k: _clip(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_clip(v) for v in value]
    return value


def _shared_prefix(previous: Sequence[Mapping[str, Any]], current: Sequence[Mapping[str, Any]]) -> int:
    """How many leading messages are byte-identical between two requests.

    The same question ``ContextManager.novel_tokens`` asks, asked of the wire
    dicts rather than of the ``Message`` objects, because the wire is what this
    file stores and a prefix that matches on ``Message`` but not on the rendered
    dict would reconstruct wrongly.
    """
    shared = 0
    for old, new in zip(previous, current):
        if old != new:
            break
        shared += 1
    return shared


class DebugLog:
    """One session's recording. NDJSON, append-only, one record per line.

    NDJSON rather than a structured document for the reason the journal is:
    it is greppable, tailable and diffable with tools that are already on the
    machine, and a run killed mid-write loses its last line rather than the
    file.

    Best-effort throughout, like the journal. A debug recorder that can fail a
    run is a debug recorder nobody dares leave on.
    """

    __slots__ = ("path", "_broken", "_written", "_last_wire", "_started", "_turn")

    def __init__(self, path: Path) -> None:
        self.path = path
        self._broken = False
        self._written = 0
        #: The previous turn's assembled request, for prefix compression.
        self._last_wire: list[dict[str, Any]] = []
        self._started = time.monotonic()
        #: The turn every record is stamped with. Held here rather than passed
        #: in, because the events come off a tee that has no idea which turn it
        #: is on -- and a debug log whose events all land under turn 0 is a flat
        #: list, which is the thing this file exists not to be.
        self._turn = 0

    # -- construction -------------------------------------------------------

    @classmethod
    def for_session(cls, workspace: Path, session_id: str) -> "DebugLog | None":
        """A recorder for this session, or ``None`` when debugging is off."""
        if not enabled() or not session_id:
            return None
        root = Path(workspace) / ".dakcoder" / "sessions" / session_id
        try:
            root.mkdir(parents=True, exist_ok=True)
        except OSError:
            return None
        return cls(root / "debug.jsonl")

    # -- writing ------------------------------------------------------------

    def _write(self, kind: str, payload: Mapping[str, Any]) -> None:
        if self._broken:
            return
        record = {
            "at": round(time.monotonic() - self._started, 4),
            "turn": self._turn,
            "kind": kind,
            **payload,
        }
        try:
            line = json.dumps(record, separators=(",", ":"), default=str)
        except (TypeError, ValueError):  # pragma: no cover - defensive
            line = json.dumps({"kind": kind, "error": "not serialisable"})
        if self._written + len(line) > MAX_DEBUG_BYTES:
            self._broken = True
            line = json.dumps(
                {
                    "kind": "capped",
                    "note": f"debug log reached {MAX_DEBUG_BYTES:,} bytes and stopped recording",
                }
            )
        try:
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")
            self._written += len(line) + 1
        except OSError:
            self._broken = True

    def turn_start(self, *, turn: int, mode: str, state: Mapping[str, Any],
                   context: Mapping[str, Any], plan: Sequence[Mapping[str, Any]]) -> None:
        """The loop's own state and the shape of the context, before anything runs."""
        self._turn = turn
        self._write(
            "turn_start",
            {"turn": turn, "mode": mode, "state": _clip(state), "context": dict(context),
             "plan": [dict(s) for s in plan]},
        )

    def request(
        self,
        wire: Sequence[Mapping[str, Any]],
        *,
        turn: int,
        role: str,
        tools: Sequence[Mapping[str, Any]] | None,
        tool_choice: Any,
        estimated_tokens: int,
        **params: Any,
    ) -> None:
        """The exact request, stored as a delta against the previous one.

        Written *before* the call, so a run that dies inside the endpoint still
        leaves the prompt that killed it.
        """
        current = [dict(m) for m in wire]
        shared = _shared_prefix(self._last_wire, current)
        self._write(
            "request",
            {
                "turn": turn,
                "role": role,
                "tool_choice": tool_choice,
                "estimated_tokens": estimated_tokens,
                "tool_names": [
                    str((t.get("function") or {}).get("name", "")) for t in (tools or ())
                ],
                "messages": len(current),
                # The compression, and the only thing in this file that is not
                # verbatim. `shared` leading messages are byte-identical to the
                # previous request; `tail` is everything after them.
                "shared_prefix": shared,
                "tail": _clip(current[shared:]),
                **params,
            },
        )
        self._last_wire = current

    def response(self, *, turn: int, content: str, tool_calls: Sequence[Any],
                 finish_reason: str, usage: Mapping[str, Any], seconds: float) -> None:
        self._write(
            "response",
            {
                "turn": turn,
                "finish_reason": finish_reason,
                "seconds": round(seconds, 3),
                "usage": dict(usage),
                "content": _clip(content),
                "tool_calls": [
                    {"id": c.id, "name": c.name, "arguments": _clip(c.arguments)}
                    for c in tool_calls
                ],
            },
        )

    def event(self, kind: str, data: Mapping[str, Any]) -> None:
        """One event off the loop's own tee."""
        self._write("event", {"event": kind, "data": _clip(dict(data))})

    def note(self, text: str, **fields: Any) -> None:
        self._write("note", {"text": text, **fields})


# ── reading ─────────────────────────────────────────────────────────────────


def read(path: Path) -> list[dict[str, Any]]:
    """Every record, in order. A truncated last line is dropped, not raised on."""
    out: list[dict[str, Any]] = []
    try:
        with Path(path).open("r", encoding="utf-8") as handle:
            for line in handle:
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


def prompts(records: Iterable[Mapping[str, Any]]) -> Iterator[tuple[int, list[dict[str, Any]]]]:
    """Reconstruct every request, as ``(turn, messages)``.

    Replays the prefix compression: each request is the previous one's first
    ``shared_prefix`` messages followed by its own tail. Exact, because the
    prefix was compared byte for byte when it was written.
    """
    wire: list[dict[str, Any]] = []
    for record in records:
        if record.get("kind") != "request":
            continue
        shared = int(record.get("shared_prefix") or 0)
        tail = list(record.get("tail") or ())
        wire = wire[:shared] + tail
        yield int(record.get("turn") or 0), list(wire)


# ── the CLI ─────────────────────────────────────────────────────────────────


def _render(records: Sequence[Mapping[str, Any]]) -> Iterator[str]:
    """A per-turn report: state in, request out, response back, what it did."""
    by_turn: dict[int, list[Mapping[str, Any]]] = {}
    for record in records:
        by_turn.setdefault(int(record.get("turn") or 0), []).append(record)

    for turn in sorted(by_turn):
        group = by_turn[turn]
        yield f"\n{'=' * 78}\nTURN {turn}\n{'=' * 78}"
        for record in group:
            kind = record.get("kind")
            if kind == "turn_start":
                context = record.get("context") or {}
                yield f"  mode        {record.get('mode')}"
                yield (
                    f"  context     {context.get('total_tokens', 0):,} / "
                    f"{context.get('budget', 0):,} tokens"
                    f" ({context.get('used_pct', 0)}%), {context.get('messages', 0)} messages,"
                    f" {context.get('canonical_records', 0)} canonical records"
                )
                if context.get("compacted_records"):
                    yield (
                        f"  compacted   {context['compacted_records']} record(s) behind a recap"
                        + (" [STALE SIDECAR]" if context.get("compaction_stale") else "")
                    )
                for name, group_state in (record.get("state") or {}).items():
                    yield f"  state.{name:<10} {group_state}"
                for step in record.get("plan") or ():
                    yield f"  plan        [{step.get('status')}] {step.get('file')}"
            elif kind == "request":
                yield (
                    f"  ->  {record.get('messages')} messages"
                    f" ({record.get('shared_prefix')} cached prefix,"
                    f" {len(record.get('tail') or ())} new),"
                    f" ~{record.get('estimated_tokens', 0):,} tok,"
                    f" role={record.get('role')}, choice={record.get('tool_choice')}"
                )
                yield f"      tools   {', '.join(record.get('tool_names') or ()) or 'none'}"
            elif kind == "response":
                usage = record.get("usage") or {}
                yield (
                    f"  <-  {record.get('finish_reason')} in {record.get('seconds')}s,"
                    f" prompt={usage.get('prompt_tokens', 0):,}"
                    f" completion={usage.get('completion_tokens', 0):,}"
                    f" reasoning={usage.get('reasoning_tokens', 0):,}"
                )
                if text := (record.get("content") or "").strip():
                    yield f"      says    {text[:300]}"
                for call in record.get("tool_calls") or ():
                    yield f"      calls   {call.get('name')}({call.get('arguments', '')[:160]})"
            elif kind == "event":
                data = record.get("data") or {}
                name = record.get("event")
                detail = data.get("name") or data.get("kind") or data.get("message") or ""
                yield f"      .  {name} {str(detail)[:160]}"
            elif kind in ("note", "capped"):
                yield f"      !  {record.get('text') or record.get('note')}"


def sessions(workspace: Path) -> list[dict[str, Any]]:
    """Every session in this workspace that has a recording, newest first.

    "How do I get the session id" is the first question anyone asks, and the
    honest answer is that the tool should tell you rather than expect you to go
    looking in a dot-directory. The task text comes from ``session.json``, which
    the journal already writes beside the recording.
    """
    root = Path(workspace) / ".dakcoder" / "sessions"
    found: list[dict[str, Any]] = []
    try:
        entries = list(root.iterdir())
    except OSError:
        return []
    for entry in entries:
        recording = entry / "debug.jsonl"
        if not recording.is_file():
            continue
        summary: dict[str, Any] = {}
        try:
            parsed = json.loads((entry / "session.json").read_text(encoding="utf-8"))
            if isinstance(parsed, dict):
                summary = parsed
        except (OSError, ValueError):
            pass
        found.append(
            {
                "id": entry.name,
                "task": str(summary.get("task") or "")[:70],
                "status": str(summary.get("status") or "?"),
                "created_at": str(summary.get("created_at") or ""),
                "bytes": recording.stat().st_size,
                "mtime": recording.stat().st_mtime,
            }
        )
    found.sort(key=lambda s: s["mtime"], reverse=True)
    return found


def main(argv: Sequence[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(
        prog="python -m dakcoder_agent.debug",
        description="Read a session's full-fidelity turn recording.",
    )
    parser.add_argument(
        "session",
        nargs="?",
        help="the session id, or 'latest'. Omit it to list what is recorded.",
    )
    parser.add_argument(
        "-C",
        "--workspace",
        type=Path,
        default=Path("."),
        help="the workspace holding .dakcoder (default: the current directory)",
    )
    parser.add_argument(
        "--prompt",
        type=int,
        metavar="TURN",
        help="dump the reconstructed request for one turn instead of the report",
    )
    args = parser.parse_args(argv)

    available = sessions(args.workspace)
    if args.session is None or not available:
        if not available:
            print(f"no recordings under {Path(args.workspace).resolve() / '.dakcoder' / 'sessions'}")
            print()
            print("Turn recording on and run the agent again:")
            print("  VS Code  ->  set `dakcoder.debugRecording` to true, then Reload Window")
            print("  by hand  ->  DAKCODER_DEBUG=1 before the runtime starts")
            return 1
        print(f"{'SESSION':<38} {'STATUS':<10} {'SIZE':>9}  TASK")
        for row in available:
            print(
                f"{row['id']:<38} {row['status']:<10} {row['bytes']:>8,}B  {row['task']}"
            )
        print()
        print("Then: python -m dakcoder_agent.debug <session-id>   (or 'latest')")
        return 0

    session = available[0]["id"] if args.session == "latest" else args.session
    path = Path(args.workspace) / ".dakcoder" / "sessions" / session / "debug.jsonl"
    records = read(path)
    if not records:
        print(f"no debug recording at {path}")
        print("known sessions: " + ", ".join(s["id"] for s in available))
        return 1

    if args.prompt is not None:
        for turn, wire in prompts(records):
            if turn == args.prompt:
                print(json.dumps(wire, indent=2))
                return 0
        print(f"turn {args.prompt} has no recorded request")
        return 1

    for line in _render(records):
        print(line)
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI
    raise SystemExit(main())
