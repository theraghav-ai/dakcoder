"""The agent loop (Part A section 10).

One loop, one system prompt, five mode overlays. Modes narrow the tool schema and
sharpen the instruction; they do not fork the process and they do not each get
their own system prompt. That last part is finding S6, and it cost the frontend
agent three cold prefills per task.

The loop is a **generator of events**, not a function that returns an answer.
That shape falls out of contract C2: the extension renders a run as it happens,
and a loop that only speaks at the end would have to buffer everything and then
replay it. It also makes the loop testable without a server — a test drives it by
iterating, which is exactly what the transport does.

Four properties are load-bearing, and each has a specific failure it prevents.

**The gate runs, whatever the model says.** Not because the model asked, and not
because the prompt told it to. "It said it was done and it wasn't" is the failure
this design exists to prevent, and the only way to prevent it is to not ask.

**Failure has a budget.** Two Coder attempts against a failing gate, then the
Debugger. Without a limit a loop retries the same wrong fix until the token
budget runs out, which is the same outcome an hour later.

**No progress stops the run.** The same tool with the same arguments three turns
running means the model is stuck, and the loop can see that when the model
cannot. Inherited from ``postgen``, where it was the single most effective stop
condition.

**Approval is asked, not assumed.** The loop yields a ``tool_pending`` event and
consults a callback. The default denies, because a runtime that silently
auto-approves is one where the approval layer is decoration.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, field
from typing import Any

from dakcoder_shared.envelope import Event, EventType, ToolResult
from dakcoder_shared.llm import LLMClient, ToolCall

from .context import ContextManager, OverBudgetError
from .gate import GateReport, full_gate, inner_loop
from .llm import TurnResult, complete, reasoning_leaked
from .modes import Mode
from .prompts import mode_instruction, system_prompt
from .tools.router import ApprovalRequest, Router

__all__ = ["AgentLoop", "Approver", "Outcome", "RunResult", "deny_all", "system_prompt"]

#: Asked before a mutating call the developer has not pre-approved. Returns True
#: to let it through. Blocking is the caller's choice — the loop is a generator
#: and will simply wait.
Approver = Callable[[ApprovalRequest], bool]


def deny_all(_request: ApprovalRequest) -> bool:
    """The default. A runtime that silently approves is one with no approval layer."""
    return False


class Outcome:
    DONE = "done"
    #: The gate never came clean within the attempt budget.
    UNVERIFIED = "unverified"
    #: The same call three turns running.
    NO_PROGRESS = "no_progress"
    #: Turn or token budget reached.
    EXHAUSTED = "exhausted"
    #: The transport or a sidecar failed in a way the model cannot act on.
    ERROR = "error"


@dataclass(frozen=True, slots=True)
class RunResult:
    outcome: str
    summary: str
    turns: int
    mutations: tuple[str, ...] = ()
    gate: GateReport | None = None

    @property
    def ok(self) -> bool:
        return self.outcome == Outcome.DONE

    def as_dict(self) -> dict[str, Any]:
        return {
            "outcome": self.outcome,
            "summary": self.summary,
            "turns": self.turns,
            "mutations": list(self.mutations),
            "gate": self.gate.as_dict() if self.gate else None,
        }


@dataclass
class _State:
    """Everything the loop tracks that is not in the context manager."""

    mode: Mode = Mode.PLANNER
    #: Consecutive Coder attempts against a failing gate.
    attempts: int = 0
    #: Debugger cycles.
    cycles: int = 0
    #: Fingerprints of the last few tool calls, for the no-progress detector.
    recent: list[str] = field(default_factory=list)
    plan: str = ""
    last_gate: GateReport | None = None
    dependencies_changed: bool = False


MAX_ATTEMPTS = 2
MAX_DEBUG_CYCLES = 3
NO_PROGRESS_REPEATS = 3


class AgentLoop:
    """One run, from task to verified change."""

    def __init__(
        self,
        context: ContextManager,
        client: LLMClient,
        router: Router,
        *,
        approve: Approver = deny_all,
        max_turns: int = 40,
    ) -> None:
        self.context = context
        self.client = client
        self.router = router
        self.approve = approve
        self.max_turns = max_turns
        self.state = _State()
        self.result: RunResult | None = None

    # -- the run ----------------------------------------------------------

    def run(
        self,
        task: str,
        *,
        acceptance: Sequence[str] = (),
        start: Mode = Mode.PLANNER,
    ) -> Iterator[Event]:
        """Drive the run, yielding events as they happen."""
        self.context.set_task(task, acceptance=acceptance)
        self._switch(start)

        for _ in range(self.max_turns):
            if self.result is not None:
                break
            yield from self._turn()

        if self.result is None:
            self.result = RunResult(
                Outcome.EXHAUSTED,
                f"stopped after {self.context.turn} turns without a clean gate",
                self.context.turn,
                tuple(self.router.touched),
                self.state.last_gate,
            )

        yield Event(EventType.FINISH, self.result.as_dict())
        yield Event(EventType.END, {})

    # -- one turn ---------------------------------------------------------

    def _turn(self) -> Iterator[Event]:
        turn = self.context.begin_turn()
        yield Event(
            EventType.TURN_START,
            {"turn": turn, "mode": str(self.state.mode), "attempt": self.state.attempts},
        )

        if self.context.should_compact():
            yield from self._compact()

        try:
            result = complete(
                self.context,
                self.client,
                tools=self.router.schemas_for(self.state.mode),
            )
        except OverBudgetError as exc:
            # The context manager exists to prevent this, so reaching it means
            # compaction could not free enough. Compacting harder and retrying
            # once is worth a turn; failing the run outright is not.
            yield Event(EventType.GATE, {"kind": "compaction", "reason": "over budget"})
            self.context.compact(self._summarise, retain_pct=0.15)
            try:
                result = complete(
                    self.context,
                    self.client,
                    tools=self.router.schemas_for(self.state.mode),
                )
            except OverBudgetError:
                self.result = RunResult(
                    Outcome.ERROR, f"context cannot be reduced below budget: {exc}",
                    self.context.turn, tuple(self.router.touched),
                )
                return
        except Exception as exc:  # noqa: BLE001 - the transport failing is not the model's fault
            yield Event(EventType.ERROR, {"message": str(exc)})
            self.result = RunResult(
                Outcome.ERROR, str(exc), self.context.turn, tuple(self.router.touched)
            )
            return

        yield from self._usage(result)

        if result.chat.content:
            self.context.append_assistant(result.chat.content)
            yield Event(EventType.ASSISTANT, {"text": result.chat.content})

        if result.chat.tool_calls:
            yield from self._tool_calls(result.chat.tool_calls)
            return

        # No tool calls: the model has said its piece, so the mode is over.
        yield from self._advance(result)

    def _usage(self, result: TurnResult) -> Iterator[Event]:
        usage = self.context.usage()
        payload = {
            "prompt_tokens": result.actual_prompt_tokens,
            "completion_tokens": result.chat.usage.completion_tokens,
            "cached_tokens": result.chat.usage.cached_tokens,
            "budget_used_pct": round(usage.used_pct, 1),
            "estimate_error": result.estimate_error,
        }
        if reasoning_leaked(result):
            # Section 18's alert. Non-zero reasoning in a thinking-off mode means
            # chat_template_kwargs is not reaching the model: ~15x the latency
            # for no quality gain, and it presents as the agent simply being slow
            # rather than as a failure.
            payload["reasoning_leaked"] = result.chat.usage.reasoning_tokens
        yield Event(EventType.USAGE, payload)

    # -- tools ------------------------------------------------------------

    def _tool_calls(self, calls: Sequence[ToolCall]) -> Iterator[Event]:
        mutated = False

        for call in calls:
            # Fingerprinted from the raw string, not the parsed object. Parsing
            # can raise on malformed arguments, and a model that sends the same
            # malformed arguments three turns running is precisely the case the
            # no-progress detector is for — so it must not be the case that
            # crashes it.
            fingerprint = f"{call.name}:{call.arguments}"
            if self._stuck(fingerprint):
                self.result = RunResult(
                    Outcome.NO_PROGRESS,
                    f"{call.name} called with identical arguments "
                    f"{NO_PROGRESS_REPEATS} turns running",
                    self.context.turn,
                    tuple(self.router.touched),
                    self.state.last_gate,
                )
                return

            yield Event(
                EventType.TOOL_CALL,
                {"id": call.id, "name": call.name, "arguments": _safe_args(call)},
            )

            outcome = self.router.dispatch(call.name, call.arguments, mode=self.state.mode)

            if isinstance(outcome, ApprovalRequest):
                yield Event(EventType.TOOL_PENDING, outcome.as_dict())
                if self.approve(outcome):
                    outcome = self.router.dispatch(
                        call.name, call.arguments, mode=self.state.mode, approved=True
                    )
                else:
                    outcome = ToolResult.failure(
                        f"{call.name} was not approved by the developer.",
                        fix="Explain why the change is needed, or take a different "
                        "approach that does not touch that file.",
                    )

            assert isinstance(outcome, ToolResult)
            mutated = mutated or bool(outcome.mutations)
            if call.name == "go_mod":
                self.state.dependencies_changed = True

            self.context.append_tool_result(
                call.name,
                outcome.for_model(),
                tool_call_id=call.id,
                path=_slice_path(call, outcome),
            )
            yield Event(
                EventType.TOOL_RESULT,
                {"id": call.id, "name": call.name, **outcome.as_dict()},
            )

        if mutated:
            yield from self._inner_loop()

    def _inner_loop(self) -> Iterator[Event]:
        """Format and lint what was just written, sub-second.

        The result goes into context as a tool message rather than being merely
        reported, because its whole purpose is to be in front of the model on the
        next turn while the edit is still what it is thinking about.
        """
        report = inner_loop(self.router, self.router.touched)
        yield Event(EventType.GATE, {"kind": "inner", **report.as_dict()})
        if not report.ok or report.warnings:
            self.context.append_tool_result("rules_lint", report.summary())

    # -- mode transitions -------------------------------------------------

    def _advance(self, result: TurnResult) -> Iterator[Event]:
        """Decide what happens after a turn that called no tools."""
        mode = self.state.mode
        text = result.chat.content or ""

        if mode is Mode.PLANNER:
            self.state.plan = text
            self.context.set_plan(text)
            yield Event(EventType.PLAN, {"text": text, "steps": _count_steps(text)})
            self._switch(Mode.SCAFFOLDER if _is_scaffold_plan(text) else Mode.CODER)
            return

        if mode in (Mode.SCAFFOLDER, Mode.CODER, Mode.DEBUGGER):
            yield from self._verify()
            return

        if mode is Mode.VERIFIER:
            # The Verifier reports; it never fixes. Reaching here means the gate
            # was already run and the model has explained the failure, so the
            # next decision is whose problem it is.
            yield from self._route_failure(text)
            return

    def _verify(self) -> Iterator[Event]:
        """Run the gate and act on it. The one thing the model cannot skip."""
        report = full_gate(
            self.router,
            self.router.touched,
            dependencies_changed=self.state.dependencies_changed,
        )
        self.state.last_gate = report
        yield Event(EventType.GATE, {"kind": "full", **report.as_dict()})

        if report.ok:
            self.result = RunResult(
                Outcome.DONE,
                self._done_summary(report),
                self.context.turn,
                tuple(self.router.touched),
                report,
            )
            return

        self.context.append_tool_result("go_build", report.summary())
        self._switch(Mode.VERIFIER)

    def _route_failure(self, verdict: str) -> Iterator[Event]:
        """Coder twice, then the Debugger, then stop.

        The escalation matters more than the numbers. A third identical Coder
        attempt on a gate that has failed twice is not more likely to work — the
        model is applying the same understanding to the same evidence. The
        Debugger has different instructions and a playbook, which is a different
        understanding rather than another try.
        """
        del verdict  # already in context; the routing decision is ours

        if self.state.attempts < MAX_ATTEMPTS:
            self.state.attempts += 1
            self._switch(Mode.CODER)
            return

        if self.state.cycles < MAX_DEBUG_CYCLES:
            self.state.cycles += 1
            self._switch(Mode.DEBUGGER)
            return

        report = self.state.last_gate
        self.result = RunResult(
            Outcome.UNVERIFIED,
            "the gate did not come clean after "
            f"{MAX_ATTEMPTS} coder attempts and {MAX_DEBUG_CYCLES} debug cycles"
            + (f"; blocked at {report.blocked_by.name}" if report and report.blocked_by else ""),
            self.context.turn,
            tuple(self.router.touched),
            report,
        )
        yield Event(EventType.ERROR, {"message": self.result.summary})

    def _switch(self, mode: Mode) -> None:
        if mode is self.state.mode and self.context.turn > 0:
            return
        self.state.mode = mode
        self.state.recent.clear()
        self.context.switch_mode(mode, mode_instruction(mode))

    # -- helpers ----------------------------------------------------------

    def _stuck(self, fingerprint: str) -> bool:
        self.state.recent.append(fingerprint)
        del self.state.recent[:-NO_PROGRESS_REPEATS]
        return (
            len(self.state.recent) == NO_PROGRESS_REPEATS
            and len(set(self.state.recent)) == 1
        )

    def _compact(self) -> Iterator[Event]:
        before = self.context.usage().total
        recap = self.context.compact(self._summarise)
        yield Event(
            EventType.GATE,
            {
                "kind": "compaction",
                "before": before,
                "after": self.context.usage().total,
                "turns": recap.turns if hasattr(recap, "turns") else None,
            },
        )

    def _summarise(self, text: str) -> str:
        """Summarise the working set for a recap.

        Runs through the same client and counts against the same quota, because
        pretending compaction is free is how a run's real cost becomes
        unmeasurable. Falls back to the raw tail if the call fails: a degraded
        recap beats ending the run over a summarisation.
        """
        try:
            reply = self.client.chat(
                [
                    {
                        "role": "user",
                        "content": "Summarise this agent transcript for a handover. Keep "
                        "file paths, decisions and their reasons, and anything still "
                        "unresolved. Drop tool output that has been superseded.\n\n"
                        + text,
                    }
                ],
                role="summariser",
                max_tokens=1024,
                enable_thinking=False,
            )
            return reply.content or text[-4000:]
        except Exception:  # noqa: BLE001 - a degraded recap beats ending the run
            return text[-4000:]

    def _done_summary(self, report: GateReport) -> str:
        files = self.router.touched
        if not files:
            return "nothing needed changing; the gate is clean"
        listed = "\n".join(f"  - {p}" for p in files)
        return f"{len(files)} file(s) changed and the gate is clean:\n{listed}"


def _safe_args(call: ToolCall) -> Any:
    """The arguments for display, tolerating malformed JSON.

    The router will reject bad arguments with a message the model can act on.
    Raising here instead would kill the run over the very thing the run is
    supposed to recover from.
    """
    try:
        return call.parsed()
    except ValueError:
        return {"_raw": call.arguments[:500]}


def _slice_path(call: ToolCall, result: ToolResult) -> str | None:
    """The file a tool result is *about*, for the slice ledger.

    Only ``read_file`` supersedes: re-reading a file replaces the older slice
    rather than stacking beside it. A mutation is not a supersede — the record
    that a file was written is not made obsolete by writing it again.
    """
    if call.name != "read_file" or not result.ok:
        return None
    parsed = _safe_args(call)
    path = parsed.get("path") if isinstance(parsed, dict) else None
    return path if isinstance(path, str) else None


def _count_steps(plan: str) -> int:
    import re

    return len(re.findall(r"^\s*\d+[.)]\s", plan, flags=re.MULTILINE))


def _is_scaffold_plan(plan: str) -> bool:
    """Whether the plan is the 10-step resource recipe.

    Keyed off the tool the plan names rather than off prose. A plan that says
    "create a new resource" in English but never mentions the scaffolder is a
    plan to write seven files by hand, and routing it to the Scaffolder would
    hand it a mode whose only tools are the ones it did not ask for.
    """
    lowered = plan.lower()
    return "resource_scaffold" in lowered or "project_scaffold" in lowered
