"""Seams around a tool call, and the rule for running a batch of them at once.

Why a seam
----------
The loop's behaviour around a tool call is good and it is hard-wired. Six checks
in the router, three intercept ledgers, batch rules, overlap detection, the gate,
the inner loop -- every one of them is a branch in ``_tool_calls`` or a method
called from it. That is right for the ones that are load-bearing, and wrong as
the *only* way to add anything: a team that wants a linter run after every edit,
or a policy that a particular path is never written on this repository, has to
edit ``loop.py`` to get it.

Cline's ``beforeTool``/``afterTool`` hooks are the shape worth taking. A hook
sees the call before it is dispatched and may rewrite its arguments, deny it, or
attach a note; it sees the result afterwards and may replace it or attach a
note. That is enough to express a policy, a linter, a redaction pass or an audit
log without touching the loop.

Two rules make it safe to have at all:

**A hook cannot silently succeed at something it did not do.** A denial produces
a tool result the model reads, exactly as a router refusal does, so the
transcript stays coherent and the model is told why.

**A hook cannot impersonate a tool.** Notes are appended as ``role: user``
messages wrapped in a ``<hook_context>`` block with the attributes sanitised,
because a hook that could emit a ``role: tool`` message could put words in a
tool's mouth -- and the model has no way to tell the difference.

Parallel reads
--------------
The second half of this file is one predicate: whether a batch may run at once.
Calls run sequentially on the worker thread, which is correct for writes -- two
``patch_file`` calls against one file must not interleave, and the undo store's
first-write-wins depends on the order -- and needlessly slow for a batch of six
``read_file`` calls that touch nothing.

``parallel_safe`` is deliberately conservative. A tool qualifies only when it
mutates nothing, needs no approval, and runs in this process: the sidecar and
gopls providers hold their own state and serialise their own work, so running
six of them at once buys queueing rather than concurrency, and ``go build``
against one module is a lock either way.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

from dakcoder_shared.envelope import ToolResult
from dakcoder_shared.llm import ToolCall

from .modes import Mode

__all__ = [
    "AfterTool",
    "BeforeTool",
    "HookContext",
    "Hooks",
    "MAX_PARALLEL_CALLS",
    "ToolHook",
    "hook_context_block",
    "parallel_safe",
]

#: How many read-only calls may run at once.
#:
#: The batch cap is six (``MAX_CALLS_PER_BATCH``), so this only ever bites when
#: every call in a full batch is a read. Four rather than six because the work
#: is IO-bound on one developer's disk and the marginal thread past four buys
#: contention; it is a ceiling on threads, not on calls.
MAX_PARALLEL_CALLS = 4

#: Providers whose work is already serialised somewhere else. Running several
#: at once queues them, and queueing them inside a thread pool makes the
#: failure modes harder to read without making anything faster.
_SERIAL_PROVIDERS = frozenset({"gotools", "gopls"})

#: Attribute values in a hook context block are sanitised to this, so a hook
#: cannot close the tag and write markup of its own into the prompt.
_ATTR = re.compile(r"[^A-Za-z0-9_.:/@ -]+")


@dataclass(frozen=True, slots=True)
class HookContext:
    """What a hook is told about the call it is looking at."""

    call: ToolCall
    #: The parsed arguments, after the router would have coerced them. A hook
    #: sees what the tool will see, not the raw JSON string.
    arguments: dict[str, Any]
    mode: Mode
    turn: int
    #: Free-form, for a host that wants to pass its own state through.
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class BeforeTool:
    """A hook's decision about a call that has not run yet.

    Every field is optional and the default is "carry on". A hook that returns
    ``None`` and a hook that returns ``BeforeTool()`` mean the same thing, which
    keeps the common case free of ceremony.
    """

    #: Replace the arguments. The call is dispatched with these instead.
    arguments: dict[str, Any] | None = None
    #: Refuse the call. The model is told this, in a tool result, so the
    #: transcript stays coherent and the refusal is actionable.
    deny: str = ""
    #: What to do instead, rendered with the denial.
    fix: str = ""
    #: Skip the call without treating it as a refusal -- the answer is already
    #: known. Produces a successful tool result carrying this text.
    answer: str = ""
    #: A note to put in front of the model after the call's result.
    note: str = ""

    @property
    def stops(self) -> bool:
        return bool(self.deny or self.answer)


@dataclass(frozen=True, slots=True)
class AfterTool:
    """A hook's decision about a result that has just arrived."""

    #: Replace the result entirely. Use sparingly: the model is being told the
    #: tool said something it did not.
    result: ToolResult | None = None
    #: A note to put in front of the model after the result.
    note: str = ""


#: A hook is a callable. Two shapes, distinguished by which list it is
#: registered on, because a before-hook and an after-hook see different things
#: and a single signature taking an optional result reads worse at every call
#: site than two clear ones.
ToolHook = Callable[..., Any]


class Hooks:
    """The registered hooks for one run.

    Empty by default and cheap when empty: the loop's fast path is a length
    check. Hooks run in registration order; the first ``deny`` or ``answer``
    stops the chain, because a call refused by one policy must not then be
    rewritten by the next.
    """

    __slots__ = ("_before", "_after")

    def __init__(self) -> None:
        self._before: list[Callable[[HookContext], BeforeTool | None]] = []
        self._after: list[Callable[[HookContext, ToolResult], AfterTool | None]] = []

    def before(self, hook: Callable[[HookContext], BeforeTool | None]) -> None:
        self._before.append(hook)

    def after(self, hook: Callable[[HookContext, ToolResult], AfterTool | None]) -> None:
        self._after.append(hook)

    def __bool__(self) -> bool:
        return bool(self._before or self._after)

    @property
    def any_before(self) -> bool:
        return bool(self._before)

    @property
    def any_after(self) -> bool:
        return bool(self._after)

    def run_before(self, context: HookContext) -> BeforeTool:
        """Fold the before-hooks into one decision.

        A hook that raises is ignored, loudly in the logs and silently to the
        run. That is the same trade the journal makes: a broken hook must cost
        the hook, never the work the developer is waiting for -- and a hook is
        by definition something a team added after the fact, so it is the part
        most likely to be wrong.
        """
        arguments = context.arguments
        rewritten = False
        notes: list[str] = []
        for hook in self._before:
            try:
                decision = hook(context)
            except Exception:  # noqa: BLE001 - a broken hook must not fail a run
                _log_failure("before", hook)
                continue
            if decision is None:
                continue
            if decision.note:
                notes.append(decision.note)
            if decision.stops:
                return BeforeTool(
                    arguments=decision.arguments,
                    deny=decision.deny,
                    fix=decision.fix,
                    answer=decision.answer,
                    note="\n\n".join(notes),
                )
            if decision.arguments is not None:
                # Each hook sees what the one before it produced, so a chain
                # reads as a pipeline rather than as several hooks arguing over
                # the original.
                arguments = decision.arguments
                rewritten = True
                context = HookContext(
                    call=context.call,
                    arguments=arguments,
                    mode=context.mode,
                    turn=context.turn,
                    extra=context.extra,
                )
        # Tracked with a flag, not by comparing identity against
        # ``context.arguments`` -- ``context`` is rebound above, so that
        # comparison is against the rewrite itself and reports every rewrite as
        # "unchanged".
        return BeforeTool(
            arguments=arguments if rewritten else None,
            note="\n\n".join(notes),
        )

    def run_after(self, context: HookContext, result: ToolResult) -> AfterTool:
        """Fold the after-hooks into one decision, threading the result through."""
        notes: list[str] = []
        current = result
        for hook in self._after:
            try:
                decision = hook(context, current)
            except Exception:  # noqa: BLE001 - a broken hook must not fail a run
                _log_failure("after", hook)
                continue
            if decision is None:
                continue
            if decision.note:
                notes.append(decision.note)
            if decision.result is not None:
                current = decision.result
        return AfterTool(
            result=None if current is result else current,
            note="\n\n".join(notes),
        )


def _log_failure(phase: str, hook: Any) -> None:
    import logging

    logging.getLogger(__name__).warning(
        "%s-tool hook %s raised; the call proceeded without it",
        phase,
        getattr(hook, "__qualname__", hook),
        exc_info=True,
    )


def hook_context_block(source: str, call: ToolCall, note: str) -> str:
    """Wrap a hook's note so it cannot be read as something a tool said.

    Attributes are sanitised, not escaped: a hook naming itself
    ``x" tool_name="read_file`` would otherwise write a second attribute into
    the block, and a hook that can forge a tool name can tell the model that
    ``go_build`` passed.

    The block goes in as ``role: user``, because no tool produced it. That is
    the same rule the retrieval-overlap note follows, and for the same reason:
    a ``role: tool`` message with no ``tool_call_id`` is malformed on the wire
    and a lie in the transcript.
    """
    clean_source = _ATTR.sub("", source)[:64] or "hook"
    clean_name = _ATTR.sub("", call.name)[:64]
    clean_id = _ATTR.sub("", call.id)[:64]
    return (
        f'<hook_context source="{clean_source}" tool_name="{clean_name}" '
        f'tool_call_id="{clean_id}">\n{note.strip()}\n</hook_context>'
    )


# ── parallel execution ──────────────────────────────────────────────────────


def parallel_safe(spec: Any) -> bool:
    """Whether a tool may run beside others in the same batch.

    The tool says so, on its spec, and then three conditions are re-checked here
    because a flag and a fact should not be able to disagree:

    * **It mutates nothing.** Two writes against one file must not interleave,
      and the undo store's first-write-wins rule is about *order*, so a
      concurrent second write could snapshot bytes the first one had already
      changed.
    * **It needs no approval.** Approval blocks the worker thread on a decision
      that arrives over HTTP; blocking four of them at once would present the
      developer with four cards for one batch and hold three threads waiting on
      the answers to the others.
    * **It is not the gate's.** The gate runs its own sequence, fail-fast and
      in order, and that order is the whole design.

    The flag itself is opt-in rather than derived, because every derivation that
    looks obvious is wrong: "does not mutate" admits ``go_build``, which spawns
    the Go toolchain, and ``finish``, which ends the phase; "runs in this
    process" admits ``go_build`` again and excludes nothing useful. See
    ``ToolSpec.parallel``.

    A spec this cannot classify -- anything without the attributes, which is
    what a test double usually is -- is not safe. The conservative answer is the
    status quo, and the status quo is correct if slow.
    """
    try:
        if not getattr(spec, "parallel", False):
            return False
        if getattr(spec, "mutates", True) or getattr(spec, "gate_only", False):
            return False
        approval = getattr(spec, "approval", None)
        return approval is None or str(approval) == "none"
    except Exception:  # noqa: BLE001 - an unclassifiable tool runs in sequence
        return False


def parallel_batch(calls: Sequence[ToolCall], lookup: Callable[[str], Any]) -> bool:
    """Whether this whole batch can run at once.

    All or nothing, deliberately. Splitting a batch into a parallel group and a
    serial one would reorder results relative to the calls that produced them,
    and the transcript's value depends on a read appearing where the model asked
    for it. Two calls minimum: a pool for one call is overhead with no upside.
    """
    if len(calls) < 2:
        return False
    return all(parallel_safe(lookup(call.name)) for call in calls)
