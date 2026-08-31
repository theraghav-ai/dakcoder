"""The context manager: the only component allowed to build a message list.

Why this is a component and not a helper
----------------------------------------
The frontend agent has no context management inside a run. ``AgentRun.messages``
is append-only across up to forty turns; the only trimming anywhere in that
codebase is a forty-*message* cap that applies solely to resumed sessions. Tool
results enter history untruncated — one ``read_file`` can contribute 25k tokens
and stay there for the rest of the task. ``repo_map`` alone contributes 20-30k,
permanently, from turn one.

Worked out for a 25-turn brownfield task (Part A §5.2)::

    fixed overhead per turn            5,700 tok
    repo_map, resident from turn 1    25,000 tok
    average new content per turn       1,500 tok

    prompt at turn 25  ~ 5,700 + 25,000 + 25 x 1,500        ~    68,000 tok
    total prefill      ~ 25 x 30,700 + 1,500 x (25*26/2)    ~ 1,250,000 tok

Roughly 95% of that is recomputation of a prefix that never changed. None of it
is a criticism of a system that shipped and works — it is what happens when
context is nobody's component. Here it is a component, owned and budgeted, and
``tests/test_budget_regression.py`` is the CI gate that keeps it that way.

The four disciplines
--------------------
**Budget.** A hard prompt cap per mode, allocated across layers in eviction
order. The cap is a *quality* decision as much as a latency one: the
context-rot literature is consistent that accuracy degrades with input length
across every frontier model tested, so a large window is not free even when the
GPU allows it.

**Insertion caps.** Every tool result is capped at the moment it enters history,
not at display time. Elision always leaves a machine-readable marker, so the
model knows it can re-read rather than concluding the content does not exist.

**The file-slice ledger.** An agent that reads a file, patches it, re-reads,
patches, and re-reads again currently keeps three full copies forever. Only the
newest read of each path survives; older ones collapse to a one-line stub. This
bounds the working set by *distinct files touched* rather than by *number of
reads*, and it is the largest single win on edit-heavy tasks.

**Stable-prefix discipline.** One system prompt for every mode, and the message
list is append-only below the pinned head. Mode switches append an instruction
rather than rebuilding the list. The frontend agent assigns a fresh
``run_state.messages`` with a different system prompt in each of ``_run_planner``,
``_run_coder`` and ``_run_debugger`` — three cold prefills per task, by design,
even with prefix caching switched on.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, replace
from enum import StrEnum
from typing import Any, Callable, Iterable, Sequence

from dakcoder_shared.llm import ToolCall
from dakcoder_shared.tokens import Calibration

from .modes import Mode, ModeConfig, config_for

__all__ = [
    "Message",
    "Role",
    "Layer",
    "ToolCap",
    "TOOL_CAPS",
    "Usage",
    "Recap",
    "ContextManager",
    "OverBudgetError",
]


class Role(StrEnum):
    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"


class Layer(StrEnum):
    """Eviction priority, in the order Part A §6.1 allocates them.

    Listed lowest-priority first: the working set is what compaction consumes,
    and the pinned layers are the last things standing.
    """

    WORKING_SET = "working_set"
    RECAP = "recap"
    TASK = "task"
    MODE = "mode"
    SYSTEM = "system"


#: Layers that are never evicted. The task and the acceptance criteria are what
#: the whole run is measured against; an agent that compacts away what it was
#: asked to do will confidently finish something else.
PINNED_LAYERS = frozenset({Layer.SYSTEM, Layer.MODE, Layer.TASK})

#: How many developer directives the pinned task block may carry at once.
#:
#: Bounded because the layer is pinned and a long conversation would otherwise
#: grow it without limit. Six is well past any run that is still going well; the
#: oldest is dropped first, and every one of them is also in the working set
#: until compaction reaches it.
MAX_DIRECTIVES = 6

#: How many mode instructions the pinned head may carry at once.
#:
#: There are five modes and a healthy run visits four or five of them, so this
#: never binds on one. It binds on a run that ping-pongs — and because the layer
#: is pinned, an unbounded one is the one part of the prompt that grows forever
#: and that compaction is forbidden to touch. See ``switch_mode``.
MAX_MODE_MESSAGES = 6


@dataclass(frozen=True, slots=True)
class Message:
    """One message.

    Frozen on purpose. §6.4's rule — the message list is append-only below the
    pinned head, and any mutation of ``messages[0..k]`` is a cache-invalidating
    bug — is easy to state and easy to violate by accident three refactors
    later. Immutability makes the accident impossible rather than merely
    detectable.
    """

    role: Role
    content: str
    layer: Layer = Layer.WORKING_SET
    #: Provenance, for the context inspector (Part B §10.2) and for the ledger.
    source: str = ""
    #: Set on tool results, so a stale slice can be found and replaced.
    path: str | None = None
    tool_call_id: str | None = None
    #: The calls an assistant message made, so its own actions survive into the
    #: next request. Without these every ``role: "tool"`` message that follows
    #: refers to a ``tool_call_id`` no assistant message on the wire declares.
    tool_calls: tuple[ToolCall, ...] = ()
    #: Turn this message was appended on, for the recap and the inspector.
    turn: int = 0

    def wire(self) -> dict[str, Any]:
        """Render to the OpenAI chat shape, dropping our own bookkeeping.

        The assistant's ``tool_calls`` are part of the shape, not bookkeeping.
        Omitting them left every tool result orphaned -- a ``tool_call_id``
        matching no call anywhere in the request, which a strict endpoint
        rejects outright and a lenient one simply cannot make sense of. It also
        rewrote the model's own history: each of its turns came back as a
        paragraph of prose, with results appearing beside them unexplained, so
        the conversation contained no example of the very message shape the
        model was being asked to produce.
        """
        out: dict[str, Any] = {"role": str(self.role), "content": self.content}
        if self.tool_call_id:
            out["tool_call_id"] = self.tool_call_id
        if self.tool_calls:
            out["tool_calls"] = [
                {
                    "id": call.id,
                    "type": "function",
                    "function": {"name": call.name, "arguments": call.arguments},
                }
                for call in self.tool_calls
            ]
        return out


@dataclass(frozen=True, slots=True)
class ToolCap:
    """The insertion cap for one tool (Part A §6.2)."""

    max_tokens: int
    #: How to elide. ``head`` keeps the beginning, ``tail`` the end, and
    #: ``errors`` keeps every line that looks like a compiler diagnostic.
    strategy: str = "tail"
    #: Rendered into the elision marker, telling the model how to get the rest.
    recover: str = ""


#: Per-tool caps. The shapes and strategies are Part A §6.2's; the numbers
#: were re-based when the prompt budget moved from 32,768 to the model window.
#:
#: At 32,768 a 6,000-token ``read_file`` cap was 18% of the budget and the
#: elision marker's advice — "re-read the file with a narrower line range" —
#: was survival. At 245,760 the same cap is 2.4%, and that advice *instructed*
#: the sliced re-reading loop two field transcripts died of. The caps below
#: still exist (an unbounded tool result is how one call eats a context), but
#: they are sized so an ordinary artefact — a whole Go file, a whole build log,
#: a whole search — lands intact.
#:
#: The four review audits and ``lib_version_check`` are deliberately NOT
#: raised: their caps sit above what their renderers emit by design, and the
#: comment on them below still holds — if one ever exceeds its cap, the
#: renderer needs tightening rather than the cap raising.
#:
#: `go_build` and friends get the special strategy for a reason worth stating:
#: their error lines are the single most useful thing in the whole context, and
#: a naive head-or-tail truncation of a long build log throws away exactly the
#: `file:line:col` messages the agent needs while keeping the package list it
#: does not.
TOOL_CAPS: dict[str, ToolCap] = {
    "read_file": ToolCap(48_000, "head", "re-read the file with a narrower line range"),
    "repo_map": ToolCap(16_000, "head", 'call repo_map(package="<dir>") for one package in full'),
    "search_repo": ToolCap(16_000, "head", "narrow the pattern or pass a glob"),
    "go_build": ToolCap(12_000, "errors", "fix the reported errors and re-run"),
    "go_vet": ToolCap(12_000, "errors", "fix the reported findings and re-run"),
    "go_test": ToolCap(12_000, "errors", "re-run with a package pattern to narrow the output"),
    "rules_lint": ToolCap(8_000, "head", "pass `paths` to scope the lint to what you changed"),
    "legacy_audit": ToolCap(8_000, "head", "pass `paths` to scope the audit"),
    "go_diagnostics": ToolCap(8_000, "head", "narrow to one file with `path`"),
    # The review audits. `head` rather than the default `tail` for all four:
    # they are rendered worst-first, so the head is the part worth keeping —
    # and the default of tail-truncating a ranked report keeps the least
    # important findings and drops the N+1.
    #
    # The caps are above what the renderers emit, deliberately. A report whose
    # whole purpose is to survive elision must not be the thing that gets
    # elided; if one ever exceeds its cap, the renderer needs tightening rather
    # than the cap raising.
    "db_roundtrip_audit": ToolCap(2_500, "head", "the worst methods are listed first"),
    "validation_audit": ToolCap(2_500, "head", "fields are grouped by struct"),
    "temporal_audit": ToolCap(2_000, "head", "candidates only; no action is implied"),
    "lib_version_check": ToolCap(1_500, "head", "report only — do not edit go.mod"),
}

#: Everything not named above. §6.2's "everything else".
DEFAULT_TOOL_CAP = ToolCap(8_000, "tail", "call the tool again with narrower arguments")

#: The recap's allocation from §6.1. Reserved when deciding how much of the
#: working set to retain, because the recap grows as history is evicted and
#: budgeting against its current size would leave no room for the one about to
#: replace it.
RECAP_BUDGET_TOKENS = 2_000

#: Lines that must survive an `errors`-strategy elision. A build log is mostly
#: noise around a handful of these.
_DIAGNOSTIC_MARKERS = (
    ".go:",
    "error:",
    "Error:",
    "FAIL",
    "--- FAIL",
    "panic:",
    "cannot use",
    "undefined:",
    "declared and not used",
    "missing dependencies",
    "could not build arguments",
)


class OverBudgetError(RuntimeError):
    """Raised when a prompt cannot be brought inside its budget.

    Distinct from silently truncating: an assembled prompt that quietly dropped
    the task description would produce a confident answer to the wrong question.
    """


@dataclass(frozen=True, slots=True)
class Usage:
    """Per-layer token accounting for one assembled prompt."""

    by_layer: dict[Layer, int]
    total: int
    budget: int
    tools: int = 0

    @property
    def used_pct(self) -> float:
        return 0.0 if self.budget <= 0 else round(100.0 * self.total / self.budget, 1)

    @property
    def over_budget(self) -> bool:
        return self.total > self.budget


@dataclass(frozen=True, slots=True)
class Recap:
    """A structured compaction recap (Part A §6.5).

    Structured, not prose, and with two properties that matter more than the
    summary itself: ``do_not_retry`` records dead ends, which is what stops the
    post-compaction agent cheerfully repeating them; and ``markdown()`` is
    persisted to ``.dakcoder/session-<id>/recap.md``, so a compaction, a restart
    or a new session on Monday can pick it up.
    """

    goal: str = ""
    plan_step: str = ""
    files_created: tuple[str, ...] = ()
    files_modified: tuple[str, ...] = ()
    #: Files whose contents were evicted by this compaction.
    #:
    #: The field the recap did not have, and the omission that made compaction
    #: self-defeating: what a compaction throws away is mostly file reads, and a
    #: recap that does not mention them leaves re-reading as the only rational
    #: next move — which puts the context straight back over the threshold that
    #: fired the compaction. One session went round that circuit twenty-five
    #: times without ever producing a plan.
    #:
    #: Recovered from the evicted messages rather than asked of the summariser,
    #: because it is a fact about what was dropped and the loop already knows it.
    files_read: tuple[str, ...] = ()
    decisions: tuple[str, ...] = ()
    verified: tuple[str, ...] = ()
    open_items: tuple[str, ...] = ()
    do_not_retry: tuple[str, ...] = ()
    turns: tuple[int, int] = (0, 0)

    def markdown(self) -> str:
        lo, hi = self.turns

        def block(label: str, items: Iterable[str]) -> str:
            items = [i for i in items if i]
            if not items:
                return ""
            head = f"{label + ':':17}{items[0]}\n"
            return head + "".join(f"{'':17}{i}\n" for i in items[1:])

        out = [f"## Recap (turns {lo}–{hi}, compacted)\n"]
        if self.goal:
            out.append(f"{'Goal:':17}{self.goal}\n")
        if self.plan_step:
            out.append(f"{'Plan step:':17}{self.plan_step}\n")
        out.append(block("Files created", self.files_created))
        out.append(block("Files modified", self.files_modified))
        if self.files_read:
            out.append(block("Already read", self.files_read))
            out.append(
                f"{'':17}(their contents were compacted away. Re-read one only if you\n"
                f"{'':17}need a line range you have not seen — re-reading all of them\n"
                f"{'':17}is what caused this compaction.)\n"
            )
        out.append(block("Decisions", self.decisions))
        out.append(block("Verified", self.verified))
        out.append(block("Open", self.open_items))
        out.append(block("Do not retry", self.do_not_retry))
        return "".join(out)


# A summariser turns the evicted messages into a Recap. Injected rather than
# imported so the context manager stays testable without a model: §6.5 runs
# compaction on the `fast` role, which today resolves to the same 27B model, so
# a real compaction is a real model call and its cost shows up in telemetry.
Summariser = Callable[[Sequence[Message]], Recap]


class ContextManager:
    """Owns the message list for one run.

    Nothing else assembles messages. That is the whole point: the moment two
    places can append to history, the budget stops being enforceable and the
    prefix stops being stable.
    """

    def __init__(
        self,
        *,
        mode: Mode | str = Mode.CODER,
        system_prompt: str,
        tool_schema_tokens: int = 0,
        calibration: Calibration | None = None,
        compact_at: float = 0.70,
    ) -> None:
        self._config: ModeConfig = config_for(mode)
        self._calibration = calibration or Calibration()
        self._compact_at = compact_at
        self._turn = 0
        self._compactions = 0

        # The pinned head. Byte-identical for every mode and every task in the
        # repository, which is what makes it a permanent prefix-cache hit —
        # roughly 2.4k tokens that never need prefilling again.
        self._system = Message(Role.SYSTEM, system_prompt, Layer.SYSTEM, source="system")
        self._tool_schema_tokens = tool_schema_tokens

        self._mode_messages: list[Message] = []
        #: The last raw mode instruction, for the dedupe in `switch_mode`.
        self._last_mode_instruction = ""
        self._task: Message | None = None
        self._task_text = ""
        self._plan_text = ""
        self._acceptance: tuple[str, ...] = ()
        #: Follow-ups and corrections, pinned. See ``pin_directive``.
        self._directives: list[str] = []
        self._recap: Message | None = None
        self._working: list[Message] = []

        # path -> index into _working of the newest read. The ledger.
        self._slices: dict[str, int] = {}

    # ── properties ──────────────────────────────────────────────────────────

    @property
    def mode(self) -> Mode:
        return self._config.mode

    @property
    def budget(self) -> int:
        return self._config.prompt_budget

    def observe_tool_schemas(self, tokens: int) -> None:
        """Record what the tools array costs on the wire.

        The schemas are part of the prompt and are sent on every call, but the
        constructor takes them as a number the caller supplies once — and the
        runtime never supplied one. ``serve.py`` built every session's context
        with the default of zero, so thirteen Planner schemas, about 1.4k
        tokens, were charged to the endpoint and counted as nothing here.

        Everything downstream is decided against that figure: when to compact,
        how much to retain, and whether ``complete`` refuses the turn. All three
        were being decided against a prompt 1.4k smaller than the one actually
        sent. The budget regression suite passes 1,200 explicitly, which is why
        it never saw this — the simulation was more honest than production.

        Measured per turn rather than fixed, because mode filtering means the
        array differs by mode: the Planner is offered read-only tools and the
        Coder the write ones.
        """
        self._tool_schema_tokens = max(0, tokens)

    @property
    def turn(self) -> int:
        return self._turn

    @property
    def compactions(self) -> int:
        return self._compactions

    # ── assembly ────────────────────────────────────────────────────────────

    def build(self) -> list[Message]:
        """Assemble the message list.

        The only builder. Order is fixed and the head is stable:

            system  ->  mode instructions  ->  task  ->  recap  ->  working set
        """
        out: list[Message] = [self._system]
        out.extend(self._mode_messages)
        if self._task is not None:
            out.append(self._task)
        if self._recap is not None:
            out.append(self._recap)
        out.extend(self._working)
        return out

    def wire(self) -> list[dict[str, Any]]:
        """Assemble in the shape the API expects."""
        return [m.wire() for m in self.build()]

    def prefix_signature(self) -> str:
        """A stable identifier for the cacheable head.

        Exposed so telemetry can alert when it changes. §18 makes a falling
        prefix-cache hit rate an alert rather than a dashboard, and this is the
        signal that says *why* it fell — but note the caveat: mode filtering
        means the tool schemas differ per mode (§7.1) while §6.4 asserts the
        ``system + schemas`` prefix is identical across phases. Both cannot be
        literally true. What is enforced here is the stronger half and the one
        that dominates: the system message is byte-identical across every mode
        and every turn, so a 25-turn run in one mode reuses its prefix
        throughout, and a mode switch costs one prefill rather than a rebuild.
        """
        return f"{len(self._system.content)}:{hash(self._system.content) & 0xFFFFFFFF:08x}"

    # ── appending ───────────────────────────────────────────────────────────

    def set_task(self, task: str, *, plan: str = "", acceptance: Sequence[str] = ()) -> None:
        """Pin the task, the plan and the acceptance criteria.

        Replaced rather than appended, because there is exactly one task per
        run — and it sits above the working set so compaction can never reach
        it.
        """
        self._task_text = task.strip()
        self._plan_text = plan.strip()
        self._acceptance = tuple(acceptance)
        self._rebuild_task()

    def set_plan(self, plan: str) -> None:
        """Pin the plan the Planner produced, keeping the task and criteria.

        A separate method rather than another ``set_task`` call, because the
        caller would otherwise have to hold the task and the acceptance criteria
        itself just to re-supply them — two copies of the same state, one of
        which will eventually be stale.
        """
        self._plan_text = plan.strip()
        self._rebuild_task()

    def pin_directive(self, text: str) -> None:
        """Keep something the developer said where compaction cannot reach it.

        A follow-up and a mid-run correction both arrive as ordinary user
        messages in the working set, which is the layer compaction consumes
        first. So the developer's instruction is the *first* thing thrown away,
        and in a run that compacts every few turns it is gone within two —
        which is how a session answered "hi" by carrying on reading the same
        two files for another forty turns, the message having been deleted from
        the run before the next turn assembled.

        That defeats the point of steering. Its whole promise is that a wrong
        turn at turn 12 can be corrected without ending the run, and a
        correction that evaporates at turn 14 is worse than no correction at
        all: the developer believes the run was redirected.

        Pinned as well as appended, not instead. The working-set copy keeps the
        conversational position — the message sits after the answers it follows
        — and this copy keeps the instruction alive. Deterministic on purpose:
        the alternative was asking the summariser to carry it, and a recap that
        silently degrades to a fallback is exactly the fragility this needs not
        to have.
        """
        directive = text.strip()
        if not directive or directive in self._directives:
            return
        self._directives.append(directive)
        del self._directives[:-MAX_DIRECTIVES]
        self._rebuild_task()

    @property
    def task_text(self) -> str:
        return self._task_text

    @property
    def directives(self) -> tuple[str, ...]:
        return tuple(self._directives)

    @property
    def acceptance(self) -> tuple[str, ...]:
        return self._acceptance

    def _rebuild_task(self) -> None:
        parts = [f"# Task\n{self._task_text}"]
        if self._plan_text:
            parts.append(f"\n# Plan\n{self._plan_text}")
        if self._acceptance:
            criteria = "\n".join(f"- {c}" for c in self._acceptance)
            parts.append(f"\n# Accepts\n{criteria}")
        if self._directives:
            since = "\n".join(f"- {d}" for d in self._directives)
            parts.append(f"\n# Since then, the developer has said\n{since}")
        self._task = Message(Role.USER, "\n".join(parts), Layer.TASK, source="task")

    def switch_mode(self, mode: Mode | str, instruction: str) -> None:
        """Move to a new mode by *appending* its instruction.

        Not by rebuilding the list with a different system prompt. That is
        finding S6, and it is what makes a planner-to-coder handoff cost one
        message rather than a full prefill of everything already in context.

        Bounded, though, which it was not. ``MODE`` is a pinned layer, so
        compaction can never reach it, and a run that walks the escalation
        ladder switches mode on almost every turn: one session reached thirteen
        mode instructions stacked in the head, five of them the Coder's and four
        the Verifier's, each contradicting the one above it. That is an
        un-evictable layer growing without limit, and a prompt whose loudest
        signal is a pile of stale instructions.

        Two bounds, both cheap. Re-stating the instruction already at the bottom
        of the layer adds nothing, so it is skipped — which also keeps the head
        byte-identical across a re-entry and costs no prefill. Beyond
        ``MAX_MODE_MESSAGES`` the oldest is dropped: it is the one furthest from
        what the model is doing now, and paying one prefill to stop the head
        growing forever is the right trade. A healthy run makes four or five
        switches and never reaches it.
        """
        self._config = config_for(mode)
        text = instruction.strip()

        # Bounding the stack was not enough on its own: six overlays is still
        # six sets of instructions, and the model has no way to know which of
        # them is now in force. In the field the Verifier — whose overlay opens
        # "Report; do not fix anything here", and which is handed no write tool
        # — announced "My job is to make the edit" on four separate turns. It
        # was reading the Coder's instruction, which was still sitting in the
        # head two messages above its own.
        #
        # So each overlay after the first says plainly that it replaces what came
        # before. Cheap, and it keeps the append-only discipline: the earlier
        # messages are not rewritten, they are just no longer ambiguous.
        # Compared before the preamble is added, not after. The preamble only
        # appears from the second overlay onwards, so comparing the rendered text
        # would find the first Coder entry ("Execute one plan step.") different
        # from the second ("You are now in coder mode… Execute one plan step.")
        # and append a duplicate — turning the cheapest bound here into a no-op.
        if self._mode_messages and self._last_mode_instruction == text:
            return

        if self._mode_messages:
            text = (
                f"You are now in {self._config.mode} mode. This replaces the mode "
                "instructions above it — where they differ, this one is right and the "
                f"others describe phases that have already happened.\n\n{text}"
            )

        self._mode_messages.append(
            Message(Role.USER, text, Layer.MODE, source=f"mode:{self._config.mode}")
        )
        self._last_mode_instruction = instruction.strip()
        del self._mode_messages[:-MAX_MODE_MESSAGES]

    def begin_turn(self) -> int:
        self._turn += 1
        return self._turn

    def append_assistant(
        self, content: str, *, tool_calls: tuple[ToolCall, ...] = ()
    ) -> Message:
        msg = Message(
            Role.ASSISTANT,
            content,
            Layer.WORKING_SET,
            source="assistant",
            tool_calls=tool_calls,
            turn=self._turn,
        )
        self._working.append(msg)
        return msg

    def append_user(self, content: str) -> Message:
        """A follow-up or a steering message from the developer mid-run."""
        msg = Message(Role.USER, content, Layer.WORKING_SET, source="user", turn=self._turn)
        self._working.append(msg)
        return msg

    def append_tool_result(
        self,
        tool: str,
        content: str,
        *,
        tool_call_id: str = "",
        path: str | None = None,
        line_range: tuple[int, int] | None = None,
    ) -> Message:
        """Append a tool result, capped at insertion.

        Capped *here*, not at display time. The frontend agent caps the SSE
        event at 4,000 characters while ``ToolResult.to_payload()`` applies no
        cap at all, so the developer sees a tidy preview of something that put
        25k tokens into history permanently (finding S8).
        """
        cap = TOOL_CAPS.get(tool, DEFAULT_TOOL_CAP)
        capped = self._apply_cap(content, cap, path=path, line_range=line_range)

        msg = Message(
            Role.TOOL,
            capped,
            Layer.WORKING_SET,
            source=f"tool:{tool}",
            path=path,
            tool_call_id=tool_call_id or None,
            turn=self._turn,
        )

        if path:
            self._supersede_slice(path)
            self._slices[path] = len(self._working)
        self._working.append(msg)
        return msg

    # ── the ledger ──────────────────────────────────────────────────────────

    def _supersede_slice(self, path: str) -> None:
        """Collapse an earlier read of the same path to a one-line stub.

        Replaced in place rather than removed, because removing a message
        renumbers everything after it — and a tool result whose matching
        ``tool_call_id`` has vanished is a malformed conversation, not a
        smaller one.
        """
        index = self._slices.get(path)
        if index is None or index >= len(self._working):
            return
        old = self._working[index]
        if old.content.startswith("[stale read of "):
            return
        self._working[index] = replace(
            old,
            content=(
                f"[stale read of {path} — superseded by a later read in this "
                f"conversation; re-read if needed]"
            ),
        )

    def stale_slices(self) -> int:
        """How many reads the ledger has collapsed. For telemetry."""
        return sum(1 for m in self._working if m.content.startswith("[stale read of "))

    # ── caps ────────────────────────────────────────────────────────────────

    def _apply_cap(
        self,
        content: str,
        cap: ToolCap,
        *,
        path: str | None,
        line_range: tuple[int, int] | None,
    ) -> str:
        tokens = self._calibration.estimate(content)
        if tokens <= cap.max_tokens:
            return content

        lines = content.splitlines()
        if cap.strategy == "errors":
            kept, elided = self._keep_diagnostics(lines, cap.max_tokens)
        elif cap.strategy == "head":
            kept, elided = self._keep_edge(lines, cap.max_tokens, head=True)
        else:
            kept, elided = self._keep_edge(lines, cap.max_tokens, head=False)

        marker = self._marker(elided, cap, path=path, line_range=line_range)
        if cap.strategy == "tail":
            return marker + "\n" + "\n".join(kept)
        return "\n".join(kept) + "\n" + marker

    def _keep_edge(self, lines: list[str], budget: int, *, head: bool) -> tuple[list[str], int]:
        ordered = lines if head else list(reversed(lines))
        kept: list[str] = []
        used = 0
        for line in ordered:
            cost = self._calibration.estimate(line) + 1
            if used + cost > budget:
                break
            kept.append(line)
            used += cost
        if not head:
            kept.reverse()
        return kept, len(lines) - len(kept)

    def _keep_diagnostics(self, lines: list[str], budget: int) -> tuple[list[str], int]:
        """Keep every diagnostic line, then fill with context around them.

        Diagnostics first and unconditionally: they are the agent's best fuel,
        and a build log that elided its own error messages is worse than no
        build log, because the agent will conclude the build passed.
        """
        keep_flags = [any(m in line for m in _DIAGNOSTIC_MARKERS) for line in lines]

        kept_idx: list[int] = []
        used = 0
        for i, line in enumerate(lines):
            if not keep_flags[i]:
                continue
            cost = self._calibration.estimate(line) + 1
            kept_idx.append(i)
            used += cost

        # Then as much surrounding context as fits, nearest-first.
        for i, line in enumerate(lines):
            if keep_flags[i]:
                continue
            cost = self._calibration.estimate(line) + 1
            if used + cost > budget:
                continue
            kept_idx.append(i)
            used += cost

        kept_idx.sort()
        return [lines[i] for i in kept_idx], len(lines) - len(kept_idx)

    @staticmethod
    def _marker(
        elided: int,
        cap: ToolCap,
        *,
        path: str | None,
        line_range: tuple[int, int] | None,
    ) -> str:
        """Render the elision marker.

        Always machine-readable and always actionable. An elision the model
        cannot see is one it treats as absence — it concludes the symbol it was
        looking for does not exist, and plans around a repository that has more
        in it than it was shown.
        """
        where = ""
        if path and line_range:
            where = f" of {path}:{line_range[0]}-{line_range[1]}"
        elif path:
            where = f" of {path}"
        recover = f" — {cap.recover}" if cap.recover else ""
        return f"[... {elided} line(s) elided{where}{recover} ...]"

    # ── budget ──────────────────────────────────────────────────────────────

    def usage(self) -> Usage:
        by_layer: dict[Layer, int] = {layer: 0 for layer in Layer}
        for msg in self.build():
            by_layer[msg.layer] += self._calibration.estimate(msg.content)
        total = sum(by_layer.values()) + self._tool_schema_tokens
        return Usage(
            by_layer=by_layer,
            total=total,
            budget=self.budget,
            tools=self._tool_schema_tokens,
        )

    def should_compact(self) -> bool:
        """Whether the assembled prompt has reached the compaction threshold."""
        return self.usage().total >= self.budget * self._compact_at

    def novel_tokens(self, previous: Sequence[Message] | None) -> int:
        """Tokens in this prompt that were not in the previous one's prefix.

        This is what a prefix cache actually has to prefill, and it is the
        metric the design controls. The raw prompt total is what gets prefilled
        with no cache at all; the truth is between them, and *where* between
        them is plan.md §9 Q1 — ``prompt_tokens_details.cached_tokens`` is absent
        from this endpoint, so the hit rate cannot currently be measured.

        §18 proposes alerting on P95 prompt tokens as a stand-in until that
        field appears. This is a better stand-in: P95 catches a prompt growing,
        but novel tokens catches the thing that actually costs money, which is a
        prefix being *invalidated* — a mutated system message, a mode switch
        inserted in the wrong place, a compaction rewriting the middle of the
        list. Those cost a full prefill while leaving P95 untouched.
        """
        current = self.build()
        if not previous:
            return sum(self._calibration.estimate(m.content) for m in current)

        shared = 0
        for old, new in zip(previous, current):
            if old.content != new.content or old.role is not new.role:
                break
            shared += 1
        return sum(self._calibration.estimate(m.content) for m in current[shared:])

    def observe_usage(self, *, prompt_tokens: int) -> None:
        """Fold a real ``prompt_tokens`` back into the estimate.

        Called once per turn from the streamed usage chunk. This is the whole
        reason ``stream_options: {"include_usage": true}`` is sent on every
        call: without it there is no measurement, and the estimate stays a
        guess for the life of the process.
        """
        chars = sum(len(m.content) for m in self.build())
        self._calibration.observe(estimated_chars=chars, actual_tokens=prompt_tokens)

    # ── compaction ──────────────────────────────────────────────────────────

    def compact(
        self,
        summarise: Summariser,
        *,
        retain_pct: float = 0.35,
        keep_recent: int | None = None,
    ) -> Recap:
        """Summarise the working set and replace it with a recap.

        Summarise, do not truncate. This is the lesson from Cline's move away
        from truncation: truncation silently drops the decision that explains
        the current diff, and the agent then re-derives it wrongly.
        Summarisation preserves it.

        The most recent turns are kept verbatim — the agent is usually mid-edit,
        and a summary of what it did four seconds ago is strictly worse than the
        thing itself.

        **How much is kept is measured in tokens, not messages**, and that
        distinction is the whole reason this signature has a percentage in it.
        Part B §10.4 retires the frontend agent's ``contextMaxMessages`` setting
        on exactly this ground — "a message *count* is the wrong unit; forty
        messages can be 5k tokens or 200k" — and keeping a fixed number of
        recent messages reproduces the mistake one layer down. Four capped
        ``read_file`` results are 24k tokens, which is 73% of a coder budget: a
        count-based compaction hands back a context that is already over the
        70% threshold, so the next turn compacts again. The budget regression
        test caught it thrashing sixteen times in a twenty-five turn run.

        Compacting to a floor instead means compaction is rare, and rarity
        matters twice over: each compaction rewrites the middle of the message
        list, which invalidates every cached prefix below it.

        ``keep_recent`` is accepted for the cases where a caller genuinely wants
        a fixed number — the tests do — but it is not the default and it is not
        what the loop should use.
        """
        if not self._working:
            return Recap(turns=(self._turn, self._turn))

        if keep_recent is not None:
            cut = max(0, len(self._working) - keep_recent)
        else:
            cut = self._retention_cut(retain_pct)

        evicted, retained = self._working[:cut], self._working[cut:]
        if not evicted:
            return Recap(turns=(self._turn, self._turn))

        recap = summarise(evicted)
        self._recap = Message(
            Role.USER, recap.markdown(), Layer.RECAP, source="recap", turn=self._turn
        )
        self._working = retained
        self._reindex_slices()
        self._compactions += 1
        return recap

    def _retention_cut(self, retain_pct: float) -> int:
        """Index of the first message to keep, walking back from the newest.

        Budgeted in tokens against the *whole* prompt, not against the working
        set alone: the pinned head and the recap are what the retained messages
        share the budget with, and ignoring them is how a compaction leaves the
        context above the threshold it just fired at.

        Always keeps at least the most recent message. A compaction that
        summarised away the tool result the agent is currently reacting to would
        be worse than not compacting at all.
        """
        overhead = (
            self._tool_schema_tokens
            + self._calibration.estimate(self._system.content)
            + sum(self._calibration.estimate(m.content) for m in self._mode_messages)
            + (self._calibration.estimate(self._task.content) if self._task else 0)
            # The recap is about to be replaced, so budget for a full-sized one
            # rather than for whatever is there now.
            + RECAP_BUDGET_TOKENS
        )
        allowance = max(0, int(self.budget * retain_pct) - overhead)

        used = 0
        cut = len(self._working)
        for i in range(len(self._working) - 1, -1, -1):
            cost = self._calibration.estimate(self._working[i].content)
            if used + cost > allowance and cut < len(self._working):
                break
            used += cost
            cut = i
        return cut

    def _reindex_slices(self) -> None:
        """Rebuild the ledger after the working set is re-sliced."""
        self._slices = {
            msg.path: i
            for i, msg in enumerate(self._working)
            if msg.path and not msg.content.startswith("[stale read of ")
        }

    # ── inspection ──────────────────────────────────────────────────────────

    def inspect(self) -> dict[str, Any]:
        """A snapshot for the context inspector (Part B §10.2).

        The extension renders this rather than reconstructing it client-side:
        contract C5 makes the server authoritative on context, and a client that
        recomputes it will eventually disagree.
        """
        use = self.usage()
        return {
            "mode": str(self.mode),
            "turn": self._turn,
            "total_tokens": use.total,
            "budget": use.budget,
            "used_pct": use.used_pct,
            "tool_schema_tokens": use.tools,
            "by_layer": {str(k): v for k, v in use.by_layer.items() if v},
            "messages": len(self.build()),
            "compactions": self._compactions,
            "stale_slices": self.stale_slices(),
            "calibrated": self._calibration.calibrated,
            "prefix": self.prefix_signature(),
        }

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<ContextManager {json.dumps(self.inspect(), sort_keys=True)}>"
