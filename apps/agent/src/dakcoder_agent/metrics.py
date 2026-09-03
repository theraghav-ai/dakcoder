"""What one run cost, and where it ran out of room.

The runtime already emitted every fact here as it happened — a ``usage`` event
per turn, a ``gate`` event per compaction, a failed ``tool_result`` per
truncated reply. What it never did was *add them up*, so the only way to answer
"is the context window big enough for this codebase" was to read a transcript
and count by eye, one run at a time.

That is the question this module exists to answer with numbers. Each field is
chosen because it is evidence for or against it, and the ones that matter most
are the two that are not about size at all:

``evicted_paths_reread``
    Files compaction threw out that the run then had to read again. This is the
    direct measure of the window being too small: not "the context was full",
    which is a threshold anyone can move, but "the run deleted something it
    turned out to need". A run with a big enough window has none of these.

``intercepted_re_read``
    Reads refused because the content was already in context. The mirror image,
    and the cost of the *defence* against a small window: turns spent asking for
    things the run already had.

Everything is derived from events that are already in ``events.jsonl``, so a
report can be rebuilt for sessions that ran before this module existed — the
only fields it cannot recover for those are the ones nothing recorded, and
``from_events`` says so rather than reporting a zero.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Iterable

__all__ = ["Accumulator", "RunMetrics", "from_events"]


@dataclass
class RunMetrics:
    """One run's context accounting."""

    # -- identity ----------------------------------------------------------
    session_id: str = ""
    task: str = ""
    outcome: str = ""
    turns: int = 0

    # -- what each call cost -----------------------------------------------
    #: Per-turn ``prompt_tokens`` as the endpoint reported them, in order. The
    #: series rather than a mean, because the shape is the finding: a run that
    #: climbs to the ceiling and stays there is a different story from one that
    #: spikes once.
    prompt_tokens: list[int] = field(default_factory=list)
    completion_tokens: list[int] = field(default_factory=list)
    cached_tokens: list[int] = field(default_factory=list)
    reasoning_tokens: int = 0
    #: The prompt ceiling these turns were measured against, so a report does
    #: not have to know which build produced the run.
    budget: int = 0
    #: The model's whole window, prompt and completion together.
    context_window: int = 0

    # -- where it ran out of room ------------------------------------------
    #: One entry per compaction: what it freed, and what it threw away.
    compactions: list[dict[str, Any]] = field(default_factory=list)
    #: Distinct paths evicted by any compaction in this run.
    evicted_paths: list[str] = field(default_factory=list)
    #: Evicted paths the run read again afterwards. The headline number.
    evicted_paths_reread: list[str] = field(default_factory=list)

    #: Replies the output limit cut off mid-call.
    truncations: int = 0
    #: The output ceiling they were cut off by.
    output_limit: int = 0

    #: Calls answered without dispatching, by why.
    intercepted_cached: int = 0
    intercepted_dead_end: int = 0
    intercepted_re_read: int = 0

    # -- what the task actually needed -------------------------------------
    #: Distinct files the run read, and the total bytes it read of them. The
    #: comparison a window claim rests on: a task whose *unique source* does not
    #: fit was never going to fit, whatever the loop did with it.
    files_read: list[str] = field(default_factory=list)
    bytes_read: int = 0
    #: Bytes read of files that were read more than once. Re-reading is the
    #: symptom; this is what it cost.
    bytes_reread: int = 0

    #: Fields this record could not recover, for a session journalled by a build
    #: that did not write them. Empty is the only clean value.
    incomplete: list[str] = field(default_factory=list)

    # -- derived -----------------------------------------------------------

    @property
    def peak_prompt_tokens(self) -> int:
        return max(self.prompt_tokens, default=0)

    @property
    def total_prompt_tokens(self) -> int:
        return sum(self.prompt_tokens)

    @property
    def peak_pct_of_budget(self) -> float:
        return 0.0 if not self.budget else round(100.0 * self.peak_prompt_tokens / self.budget, 1)

    @property
    def peak_pct_of_window(self) -> float:
        window = self.context_window
        return 0.0 if not window else round(100.0 * self.peak_prompt_tokens / window, 1)

    @property
    def pressed_the_ceiling(self) -> bool:
        """Whether this run was shaped by the window rather than by the task.

        Any compaction at all, or a truncation. Both mean something was removed
        or refused to make the turn fit; neither is a judgement about whether
        the run succeeded.
        """
        return bool(self.compactions) or self.truncations > 0

    @property
    def lost_work(self) -> bool:
        """Whether the window demonstrably cost this run something.

        Stronger than ``pressed_the_ceiling``: the run deleted a file it then
        needed again, or had a read refused because the content was already
        held. A window large enough for the task produces neither.
        """
        return bool(self.evicted_paths_reread) or self.intercepted_re_read > 0

    def as_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data.update(
            peak_prompt_tokens=self.peak_prompt_tokens,
            total_prompt_tokens=self.total_prompt_tokens,
            peak_pct_of_budget=self.peak_pct_of_budget,
            peak_pct_of_window=self.peak_pct_of_window,
            pressed_the_ceiling=self.pressed_the_ceiling,
            lost_work=self.lost_work,
        )
        return data


class Accumulator:
    """Builds a :class:`RunMetrics` from events, one at a time.

    One implementation, two drivers. The loop feeds it as it emits, so a live
    run pays no memory for retaining its own transcript; a report feeds it a
    stored journal. Two separate implementations of "add these up" is how the
    live number and the reported one come to disagree, and this is the one
    number a claim about the context window would rest on.

    Memory is bounded by the run's *distinct* files and calls, not by its
    events: nothing here keeps content.
    """

    def __init__(self, session_id: str = "") -> None:
        self.m = RunMetrics(session_id=session_id)
        self._read_counts: dict[str, int] = {}
        self._read_bytes: dict[str, int] = {}
        #: path -> the turn it was last evicted on, so a later read is a
        #: *re*-read rather than the read that first put it in context.
        self._evicted_at: dict[str, int] = {}
        self._call_paths: dict[str, str] = {}
        self._saw_usage = False
        self._saw_metrics = False

    def feed(self, event: dict[str, Any]) -> None:
        kind = str(event.get("type") or "")
        data = event.get("data") or {}
        turn = int(data.get("turn") or 0)
        m = self.m

        if kind == "metrics":
            self._saw_metrics = True
            m.output_limit = int(data.get("output_limit") or m.output_limit)
            m.context_window = int(data.get("context_window") or m.context_window)

        elif kind == "user" and not m.task:
            m.task = str(data.get("text") or "")[:200]

        elif kind == "usage":
            self._saw_usage = True
            m.prompt_tokens.append(int(data.get("prompt_tokens") or 0))
            m.completion_tokens.append(int(data.get("completion_tokens") or 0))
            cached = data.get("cached_tokens")
            m.cached_tokens.append(int(cached) if isinstance(cached, int) else 0)
            m.reasoning_tokens += int(data.get("reasoning_tokens") or 0)
            m.budget = int(data.get("budget") or m.budget)

        elif kind == "gate" and data.get("kind") == "compaction":
            self._compaction(data, turn)

        elif kind == "tool_call":
            path = _path_of(data.get("arguments"))
            if path:
                self._call_paths[str(data.get("id") or "")] = path

        elif kind == "tool_result":
            self._tool_result(data, turn)

        elif kind == "finish":
            m.outcome = str(data.get("outcome") or "")
            m.turns = int(data.get("turns") or turn or 0)

        elif kind == "end" and not m.outcome:
            m.outcome = str(data.get("outcome") or "")

    def _compaction(self, data: dict[str, Any], turn: int) -> None:
        paths = [str(p) for p in (data.get("evicted_paths") or [])]
        before, after = int(data.get("before") or 0), int(data.get("after") or 0)
        self.m.compactions.append(
            {
                "turn": turn,
                "reason": str(data.get("reason") or ""),
                "before": before,
                "after": after,
                "freed": before - after,
                "evicted_messages": int(data.get("evicted_messages") or 0),
                "evicted_paths": paths,
            }
        )
        for path in paths:
            if path not in self.m.evicted_paths:
                self.m.evicted_paths.append(path)
            self._evicted_at[path] = turn

    def _tool_result(self, data: dict[str, Any], turn: int) -> None:
        m = self.m
        if data.get("truncated_by_output_limit"):
            m.truncations += 1
            m.output_limit = int(data.get("output_limit") or m.output_limit)

        intercept = str(data.get("intercept") or "")
        if intercept == "cached":
            m.intercepted_cached += 1
        elif intercept == "dead_end":
            m.intercepted_dead_end += 1
        elif intercept == "re_read":
            m.intercepted_re_read += 1
        elif data.get("intercepted"):
            # Journalled before the reason was recorded. Counted, but not
            # attributed to a reason it does not state.
            m.intercepted_cached += 1
            _note(m, "intercept reason (older journal): counted as cached")

        if str(data.get("name") or "") != "read_file" or not data.get("ok"):
            return
        path = self._call_paths.get(str(data.get("id") or "")) or _path_of(
            data.get("arguments")
        )
        if not path:
            return
        seen = self._read_counts.get(path, 0)
        self._read_counts[path] = seen + 1
        size = _size_of(data)
        self._read_bytes[path] = self._read_bytes.get(path, 0) + size
        if seen:
            m.bytes_reread += size
            evicted_on = self._evicted_at.get(path)
            if evicted_on is not None and turn > evicted_on:
                if path not in m.evicted_paths_reread:
                    m.evicted_paths_reread.append(path)

    def finish(self) -> RunMetrics:
        m = self.m
        m.files_read = sorted(self._read_counts)
        m.bytes_read = sum(self._read_bytes.values())
        if not self._saw_usage:
            _note(m, "no usage events: token counts unavailable")
        if not m.context_window and not self._saw_metrics:
            _note(m, "no metrics event: the model's window was not recorded")
        return m


def from_events(events: Iterable[dict[str, Any]], *, session_id: str = "") -> RunMetrics:
    """Rebuild a run's metrics from its stored events.

    A report covers sessions that predate the ``metrics`` event, and it does so
    through the same :class:`Accumulator` the live run feeds — so the number in
    a run's own record and the number a report computes from its journal are
    produced by one piece of code rather than two that will drift.
    """
    acc = Accumulator(session_id)
    for event in events:
        if isinstance(event, dict):
            acc.feed(event)
    return acc.finish()


def _note(m: RunMetrics, text: str) -> None:
    if text not in m.incomplete:
        m.incomplete.append(text)


def _path_of(arguments: Any) -> str:
    """The `path` argument, whether the event stored it parsed or as a string."""
    import json

    if isinstance(arguments, dict):
        value = arguments.get("path")
        return value if isinstance(value, str) else ""
    if isinstance(arguments, str) and arguments:
        try:
            parsed = json.loads(arguments)
        except ValueError:
            return ""
        return _path_of(parsed)
    return ""


def _size_of(data: dict[str, Any]) -> int:
    """How much this result actually carried.

    ``bytes`` when the result recorded it, and the length of the stored content
    otherwise — which under-reports, because the event caps content at 64,000
    characters. Under-reporting is the right direction for a claim that the
    window is too small.
    """
    meta = data.get("meta")
    declared = meta.get("bytes") if isinstance(meta, dict) else None
    if isinstance(declared, int) and declared >= 0:
        return declared
    return len(str(data.get("content") or ""))
