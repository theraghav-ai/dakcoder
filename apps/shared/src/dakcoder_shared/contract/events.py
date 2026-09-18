"""The event half of the wire contract (C2): what each event's ``data`` holds.

``envelope.EventType`` names the events; this says what each one carries. The
runtime builds these payloads as dicts at about forty call sites, most of them
in ``loop.py``, and nothing described the result except a hand-written copy in
the extension, which had drifted.

Held true the same way as ``rest.py``. An autouse fixture in the agent's tests
validates every event an ``AgentLoop`` emits, through its generator and its
out-of-band sink, against the model for its type, with unknown fields rejected.
The loopback's own events (``user``, and a crashed run's ``error``, ``finish``
and ``end``) are checked in ``test_contract.py``.

``gate`` is not one shape. It is about ten, distinguished by ``kind``: the
verification gate's reports, and the loop's announcements about compaction,
replanning and migration phases. It is modelled as that union.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal, Union

from pydantic import Field, RootModel

from ..envelope import EventType
from .rest import Absent, Intent, Mode, StepStatus, Wire

__all__ = ["PAYLOADS", "Outcome", "models"]

#: How a run ended. ``Status`` without ``running``.
Outcome = Literal["done", "aborted", "unverified", "no_progress", "exhausted", "error"]


class UserPayload(Wire):
    """A message the developer sent: the task, a follow-up or a correction."""

    text: str
    turn: int


class TurnStartPayload(Wire):
    turn: int
    mode: Mode
    intent: Intent
    intent_source: str = Field(description="Whether a person chose the intent or it was classified.")
    intent_why: str
    attempt: int = Field(description="The attempt about to be made: 1, then 2 after a failed gate.")


class TextPayload(Wire):
    """`assistant` (the whole reply) and `assistant_delta` (a streamed piece of it)."""

    text: str


class ToolCallPayload(Wire):
    id: str
    name: str
    arguments: Any = Field(description="As the model sent them, parsed when they parse.")
    turn: int


class ToolPendingPayload(Wire):
    """An approval being raised. `id` is what `POST /v1/approvals/{id}` takes."""

    id: str
    tool: str
    arguments: dict[str, Any]
    reason: str
    paths: list[str]
    unconditional: bool
    protected: list[str]
    turn: int


class Mutation(Wire):
    path: str
    kind: Literal["create", "modify", "delete"]
    protected: bool = Field(description="Computed by the runtime. Never recompute it client-side.")


class ToolResultPayload(Wire):
    """A tool call's outcome, or a call that was answered without being run.

    `mutations` is absent when nothing was dispatched: a call skipped, refused
    for the output limit, or answered from a ledger.
    """

    id: str
    name: str
    ok: bool
    content: str
    turn: int
    mutations: Absent[list[Mutation]]
    fix: Absent[str]
    truncated: Absent[bool]
    ms: Absent[int] = Field(default=None, description="Server-measured; excludes the approval wait.")
    meta: Absent[dict[str, Any]]
    dispatched: Absent[bool] = Field(default=None, description="False when the call never ran.")
    hooked: Absent[bool] = Field(default=None, description="A hook answered in the tool's place.")
    intercepted: Absent[bool] = Field(
        default=None, description="Answered from a ledger. `ok` stays true: the content is current."
    )
    intercept: Absent[str]
    arguments: Absent[Any]
    truncated_by_output_limit: Absent[bool]
    output_limit: Absent[int]


class PlanItem(Wire):
    index: int
    file: str
    action: str
    accepts: str
    status: StepStatus
    note: str


class PlanPayload(Wire):
    text: str
    steps: int
    items: list[PlanItem]


# ── gate: one event type, many shapes ───────────────────────────────────────


class GateStage(Wire):
    name: str
    ok: bool
    blocking: bool
    skipped: str = Field(description="Why the stage did not run. Empty when it ran.")
    seconds: float
    content: Absent[str] = Field(default=None, description="Only for a stage that failed.")
    truncated: Absent[bool]


class GateReport(Wire):
    ok: bool
    seconds: float
    stages: list[GateStage]
    not_run: list[str]
    blocked_by: str


class GateRun(GateReport):
    """The verification gate ran: `inner` over the files just changed, `full` at the end."""

    kind: Literal["inner", "full"]
    cached: Absent[bool] = Field(default=None, description="A full gate answered from its last run.")


class GateForcedToolCall(Wire):
    kind: Literal["forced_tool_call"]
    mode: str


class GateToolChoiceUnsupported(Wire):
    kind: Literal["tool_choice_unsupported"]
    value: str


class GateOverflowRecovery(Wire):
    kind: Literal["overflow_recovery"]
    before: int
    after: int
    retrying: bool


class GatePhase(Wire):
    """A migration phase closed and the gate is deferred until the last one."""

    kind: Literal["phase"]
    deferred: bool
    closed: str
    phase: str
    index: int
    phases: int


class GateRoutes(Wire):
    """The route inventory was saved before a migration touched anything."""

    kind: Literal["routes"]
    saved: str
    routes: int
    unresolved: int


class GateReplan(Wire):
    kind: Literal["replan"]
    reason: str
    tried: list[str]


class GateCompaction(Wire):
    kind: Literal["compaction"]
    reason: str
    strategy: str
    before: int
    after: int
    turns: list[int] | None
    evicted_messages: int
    evicted_paths: list[str]


class GatePayload(
    RootModel[
        Annotated[
            Union[
                GateRun,
                GateForcedToolCall,
                GateToolChoiceUnsupported,
                GateOverflowRecovery,
                GatePhase,
                GateRoutes,
                GateReplan,
                GateCompaction,
            ],
            Field(discriminator="kind"),
        ]
    ]
):
    """One of several shapes, told apart by `kind`."""


# ── the rest ────────────────────────────────────────────────────────────────


class UsagePayload(Wire):
    prompt_tokens: int
    completion_tokens: int
    cached_tokens: int | None = Field(
        description="Null until the endpoint reports it. Show it as not reported, not as 0%."
    )
    budget: int
    budget_used_pct: float
    reasoning_tokens: int
    estimate_error: float
    prefix_break: str
    reasoning_leaked: Absent[int] = Field(
        default=None, description="Only on the anomaly: reasoning charged in a thinking-off mode."
    )


class QuotaPayload(Wire):
    """A signal to re-read `GET /v1/quota`. It carries no numbers on purpose."""

    reason: str


class SteerPayload(Wire):
    """A correction the running loop has read."""

    text: str
    turn: int


class RunResultPayload(Wire):
    """`finish`, then `end`: how the run ended."""

    outcome: Outcome
    summary: str
    turns: int
    mutations: list[str]
    gate: GateReport | None


class CompactionRecord(Wire):
    turn: int
    reason: str
    before: int
    after: int
    freed: int
    evicted_messages: int
    evicted_paths: list[str]


class MetricsPayload(Wire):
    """What the whole run cost and where it ran out of room. Sent once, before `end`."""

    session_id: str
    task: str
    outcome: str
    turns: int
    prompt_tokens: list[int]
    completion_tokens: list[int]
    cached_tokens: list[int]
    reasoning_tokens: int
    budget: int
    context_window: int
    compactions: list[CompactionRecord]
    evicted_paths: list[str]
    evicted_paths_reread: list[str]
    truncations: int
    output_limit: int
    intercepted_cached: int
    intercepted_dead_end: int
    intercepted_re_read: int
    files_read: list[str]
    bytes_read: int
    bytes_reread: int
    incomplete: list[str]
    peak_prompt_tokens: int
    total_prompt_tokens: int
    peak_pct_of_budget: float
    peak_pct_of_window: float
    pressed_the_ceiling: bool
    lost_work: bool


class ErrorPayload(Wire):
    message: str
    where: Absent[str] = Field(default=None, description="What failed, when it was not the run itself.")
    effect: Absent[str] = Field(default=None, description="What the run did instead.")
    kind: Absent[str]


class HeartbeatPayload(Wire):
    """Declared and never emitted. The stream keeps itself alive with SSE comment
    frames (`: keep-alive`), which are not events."""


#: Every event type's payload. ``test_contract`` fails if a type is missing.
PAYLOADS: dict[EventType, type[Wire] | type[GatePayload]] = {
    EventType.USER: UserPayload,
    EventType.TURN_START: TurnStartPayload,
    EventType.ASSISTANT: TextPayload,
    EventType.ASSISTANT_DELTA: TextPayload,
    EventType.TOOL_CALL: ToolCallPayload,
    EventType.TOOL_PENDING: ToolPendingPayload,
    EventType.TOOL_RESULT: ToolResultPayload,
    EventType.PLAN: PlanPayload,
    EventType.GATE: GatePayload,
    EventType.USAGE: UsagePayload,
    EventType.QUOTA: QuotaPayload,
    EventType.FINISH: RunResultPayload,
    EventType.METRICS: MetricsPayload,
    EventType.ERROR: ErrorPayload,
    EventType.STEER: SteerPayload,
    EventType.HEARTBEAT: HeartbeatPayload,
    EventType.END: RunResultPayload,
}


def models() -> list[type]:
    """Every payload model, once each."""
    return list(dict.fromkeys(PAYLOADS.values()))
