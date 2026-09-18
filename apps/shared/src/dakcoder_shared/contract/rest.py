"""The REST half of the wire contract: every route, its request and its response.

These models describe the loopback runtime's routes. They do not serve them.
The handlers still read plain dicts and return plain dicts, and that is
deliberate:

* A ``response_model`` would make FastAPI filter and coerce every response, so
  a field missing from a model would silently vanish from the wire.
  ``response_model=None`` is worse: it switches to a serialiser that raises on
  ``inf``, which is what ``seconds_left`` is when approvals have no timeout.
* A Pydantic request body would turn today's ``400 {"error": ...}`` into
  FastAPI's ``422 {"detail": [...]}``, which no client handles.

So the models are held true another way. ``ROUTES`` must name every route the
app serves, and nothing else. The test transport validates every response the
route tests receive against its model, with unknown fields rejected. A field the
runtime starts sending without documenting it fails a test; it cannot reach a
client first.

The published schema is open (no ``additionalProperties: false``), because C2
lets a newer runtime add fields and requires clients to ignore them. Only the
tests are strict.

Enumerations the agent owns (``Status``, ``Intent``, ``Mode``, the agenda and
step states) are written out here as literals, because this package must not
import the agent. ``test_contract.py`` fails if they drift from the originals.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated, Any, Literal, TypeVar, Union

from pydantic import BaseModel, ConfigDict, Field, model_validator
from pydantic.json_schema import SkipJsonSchema

__all__ = ["ROUTES", "Route", "Wire", "fields", "models"]

# ── plumbing ────────────────────────────────────────────────────────────────


def _open(schema: dict[str, Any]) -> None:
    schema.pop("additionalProperties", None)


def _no_default(schema: dict[str, Any]) -> None:
    schema.pop("default", None)


#: Marks a field as ``Absent[...]``. Read by ``Wire`` to reject an explicit null.
_ABSENT = object()

T = TypeVar("T")

#: A field that is sometimes missing but never null. ``workspace`` on
#: ``/v1/health``, for example, is left out without a token rather than sent as
#: null. Published as a plain optional field of type ``T``.
Absent = Annotated[
    Union[T, SkipJsonSchema[None]],
    Field(default=None, json_schema_extra=_no_default),
    _ABSENT,
]


class Wire(BaseModel):
    """Strict in validation, open in the published schema."""

    model_config = ConfigDict(extra="forbid", json_schema_extra=_open)

    @model_validator(mode="before")
    @classmethod
    def _absent_is_not_null(cls, data: Any) -> Any:
        if isinstance(data, dict):
            for name, info in cls.model_fields.items():
                if _ABSENT in info.metadata and name in data and data[name] is None:
                    raise ValueError(f"{name} may be absent but is never null")
        return data


# ── enumerations the agent owns ─────────────────────────────────────────────

Status = Literal["running", "done", "unverified", "no_progress", "exhausted", "error", "aborted"]
Intent = Literal["auto", "ask", "agent"]
Mode = Literal["ask", "planner", "agent"]
AgendaState = Literal["proposed", "approved", "done", "dropped"]
StepStatus = Literal["pending", "written", "done", "failed", "skipped", "blocked"]

# ── requests ────────────────────────────────────────────────────────────────


class CredentialRequest(Wire):
    jwt: str = Field(description="A fresh gateway JWT. Only a fingerprint is echoed back.")


class TaskRequest(Wire):
    task: str
    intent: Absent[Intent]
    mode: Absent[str] = Field(
        default=None, deprecated=True, description="Read as `intent` by older clients."
    )
    acceptance: Absent[list[str]]


class DecisionRequest(Wire):
    decision: Literal["accept", "reject", "edit"] = "reject"
    arguments: Absent[dict[str, Any]] = Field(
        default=None, description="Required for `edit`: the corrected arguments."
    )


class ResumeRequest(Wire):
    note: Absent[str]


class MessageRequest(Wire):
    text: str
    intent: Absent[Intent]
    mode: Absent[str] = Field(
        default=None, deprecated=True, description="Read as `intent` by older clients."
    )


class AgendaProposal(Wire):
    title: str
    why: Absent[str]
    paths: Absent[list[str]]
    priority: Absent[int] = Field(default=None, description="1 (highest) to 5. Defaults to 3.")
    session_id: Absent[str] = Field(default=None, description="The session that proposed it.")


class AgendaMove(Wire):
    state: AgendaState
    by: Absent[str]
    note: Absent[str]


# ── responses ───────────────────────────────────────────────────────────────


class Error(Wire):
    error: str


class ValidationFailure(Wire):
    """FastAPI's own 422, for a path or query parameter of the wrong type."""

    detail: list[dict[str, Any]]


class Readiness(Wire):
    prewarmed: bool
    latency_ms: Absent[int]
    reason: Absent[str]


class SessionCounts(Wire):
    total: int
    running: int


class Health(Wire):
    """The fields after `version` need the token and are absent without it."""

    ok: bool
    api_version: str
    contract_hash: str
    version: str
    workspace: Absent[str]
    gateway: Absent[str]
    ready: Absent[Readiness]
    sessions: Absent[SessionCounts]


class Tool(Wire):
    name: str
    description: str
    parameters: dict[str, Any]
    modes: list[str]
    mutates: bool
    approval: Literal["none", "conditional", "always"]
    provider: Literal["python", "gotools", "gopls"]
    gate_only: Absent[bool]
    unavailable: Absent[str]
    instead: Absent[str]


class ToolLimits(Wire):
    max_params: int
    max_description: int


class ToolCatalog(Wire):
    """Contract C1. The published copy is `api/tool-catalog.json`."""

    component: Absent[str]
    version: Absent[str]
    contract: str
    limits: Absent[ToolLimits]
    visible_per_mode: Absent[dict[str, list[str]]]
    tools: list[Tool]


class CredentialAccepted(Wire):
    ok: bool
    fingerprint: str


class WireEvent(Wire):
    """One stored event (C2). `type` is a lower bound: ignore unknown types."""

    id: int
    type: str
    data: dict[str, Any]
    at: str


class Session(Wire):
    id: str
    task: str
    workspace: str
    status: Status
    created_at: str
    finished_at: str | None
    summary: str
    mutations: list[str]
    events: int
    resumable: bool
    queued: int = Field(description="Corrections typed during the run and not yet read.")
    turns: int = Field(description="Messages the developer has sent in this conversation.")
    winding_down: bool


class Approval(Wire):
    id: str
    session_id: str
    seconds_left: float | None = Field(description="Null when approvals have no timeout.")
    extensions: int
    tool: str
    arguments: dict[str, Any]
    reason: str
    paths: list[str]
    unconditional: bool
    protected: list[str]


class SessionDetail(Session):
    transcript: Absent[list[WireEvent]] = Field(
        default=None, description="Present with `?transcript=true`."
    )
    pending_approvals: list[Approval]


class SessionList(Wire):
    sessions: list[Session]


class Deleted(Wire):
    deleted: str


class Aborting(Wire):
    aborting: str
    status: Status


class Blocked(Wire):
    path: str
    reason: str


class RevertPlan(Wire):
    session_id: str
    restore: list[str]
    delete: list[str]
    blocked: list[Blocked] = Field(description="Changed, but cannot be reverted.")


class ApprovalList(Wire):
    approvals: list[Approval]


class Decision(Wire):
    id: str
    decision: Literal["accept", "reject", "edit"]


class WindingDown(Wire):
    id: str
    winding_down: bool


class ContextSnapshot(Wire):
    mode: Mode
    turn: int
    total_tokens: int
    budget: int
    used_pct: float
    tool_schema_tokens: int
    by_layer: dict[str, int]
    messages: int
    compactions: int
    stale_slices: int
    calibrated: bool
    prefix: str
    canonical_records: int
    compacted_records: int
    compaction_stale: bool
    elided_records: int
    elided_lines: int
    collapsed_echoes: int


class Recap(Wire):
    goal: str
    plan_step: str
    files_created: list[str]
    files_modified: list[str]
    files_read: list[str]
    decisions: list[str]
    findings: list[str]
    verified: list[str]
    open_items: list[str]
    do_not_retry: list[str]
    turns: list[int] = Field(description="First and last turn the recap covers.")


class CompactionState(Wire):
    recap: Recap
    source_seq: int
    source_prefix_hash: str
    source_count: int
    source_tokens: int
    source_paths: list[str]
    strategy: str
    turn: int
    created_at: str
    generation: int


class CanonicalRow(Wire):
    seq: int
    role: str
    turn: int
    tool: str
    path: str | None
    visibility: str
    characters: int
    content: str


class ModelRow(Wire):
    seq: int
    role: str
    layer: str
    turn: int
    path: str | None
    line_range: list[int] | None
    characters: int
    content: str


class TranscriptView(Wire):
    """`view=canonical` returns `CanonicalRow` rows, anything else `ModelRow` rows."""

    session_id: str
    view: str
    records: int
    returned: int
    compaction: CompactionState | None
    messages: list[CanonicalRow | ModelRow]


class CompactionReport(Wire):
    session_id: str
    strategy: str
    before: int
    after: int
    evicted_messages: int
    evicted_paths: list[str]
    goal: str


class PlanStep(Wire):
    file: str
    action: str
    accepts: str
    phase: str
    part: str
    status: StepStatus
    note: str


class PlanRevision(Wire):
    at: str
    cause: str = Field(description="`submitted`, `replanned` (the loop) or `revised` (the model).")
    summary: str
    reason: str
    steps: list[PlanStep]


class Phase(Wire):
    name: str
    covers: str
    parts: str = Field(description="Comma-separated.")
    status: Literal["pending", "done"]


class Migration(Wire):
    active: bool
    branch: str
    base: str
    closed: int
    log: list[str]
    phases: list[Phase]


class PlanRecord(Wire):
    session_id: str
    summary: str
    forced: bool
    updated_at: str
    migration: Migration
    steps: list[PlanStep]
    revisions: list[PlanRevision]


class AgendaTask(Wire):
    id: str
    title: str
    why: str
    state: AgendaState
    paths: list[str]
    priority: int
    origin_session: str
    created_at: str
    updated_at: str
    decided_by: str
    note: str


class AgendaList(Wire):
    tasks: list[AgendaTask]
    state: str


class Extended(Wire):
    id: str
    extensions: int
    seconds_left: float | None = Field(description="Null when approvals have no timeout.")


# ── the route table ─────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Route:
    summary: str
    #: ``None`` only for the event stream, which is not JSON.
    response: type[Wire] | None
    request: type[Wire] | None = None
    #: The request body may be left out entirely.
    optional_body: bool = False
    #: Answers without a token (with less in the response).
    public: bool = False
    #: ``text/event-stream`` rather than JSON.
    stream: bool = False


#: Every route the runtime serves. A route missing here, or listed here and not
#: served, fails ``test_contract.py``.
ROUTES: dict[str, Route] = {
    "GET /v1/health": Route("Liveness, API version and contract hash", Health, public=True),
    "GET /v1/tools": Route("The model-facing tool catalogue (C1)", ToolCatalog),
    "POST /v1/credential": Route("Replace the gateway JWT", CredentialAccepted, CredentialRequest),
    "POST /v1/tasks": Route("Start a run", Session, TaskRequest),
    "GET /v1/sessions/{session_id}/events": Route(
        "The session's events as SSE, resumable", None, stream=True
    ),
    "GET /v1/sessions": Route("List sessions", SessionList),
    "GET /v1/sessions/{session_id}": Route("One session, optionally with its transcript", SessionDetail),
    "DELETE /v1/sessions/{session_id}": Route("Delete a finished session", Deleted),
    "POST /v1/sessions/{session_id}/abort": Route("Stop a run now", Aborting),
    "GET /v1/sessions/{session_id}/revert": Route("What a revert would do", RevertPlan),
    "POST /v1/sessions/{session_id}/revert": Route("Revert what the session changed", RevertPlan),
    "GET /v1/approvals": Route("Approvals waiting for a decision", ApprovalList),
    "POST /v1/approvals/{approval_id}": Route("Accept, reject or edit", Decision, DecisionRequest),
    "POST /v1/approvals/{approval_id}/extend": Route("Give the reviewer more time", Extended),
    "POST /v1/sessions/{session_id}/resume": Route(
        "Run a failed or stopped session again", Session, ResumeRequest, optional_body=True
    ),
    "POST /v1/sessions/{session_id}/messages": Route(
        "Correct a running session, or follow up on a finished one", Session, MessageRequest
    ),
    "POST /v1/sessions/{session_id}/wind-down": Route("Stop after the current turn", WindingDown),
    "GET /v1/sessions/{session_id}/context": Route("The live context, for the inspector", ContextSnapshot),
    "GET /v1/sessions/{session_id}/transcript": Route(
        "What happened, or what the model saw", TranscriptView
    ),
    "POST /v1/sessions/{session_id}/compact": Route("Compact the context now", CompactionReport),
    "GET /v1/sessions/{session_id}/plan": Route("The plan, its statuses and revisions", PlanRecord),
    "GET /v1/agenda": Route("Work proposed for later", AgendaList),
    "POST /v1/agenda": Route("Propose work for later", AgendaTask, AgendaProposal),
    "POST /v1/agenda/{task_id}": Route("Approve, drop or complete a proposal", AgendaTask, AgendaMove),
}


def models() -> list[type[Wire]]:
    """Every model the routes use, directly. Nested ones come with them."""
    seen: dict[str, type[Wire]] = {
        "Error": Error,
        "ValidationFailure": ValidationFailure,
        "WireEvent": WireEvent,
    }
    for route in ROUTES.values():
        for model in (route.request, route.response):
            if model is not None:
                seen[model.__name__] = model
    return list(seen.values())


def fields() -> dict[str, list[str]]:
    """Every wire model's field names, nested ones included.

    What the contract hash covers of the REST shapes. Names rather than the JSON
    schema, because the schema's exact text depends on the installed Pydantic,
    and a hash that changed with a library upgrade would warn about a contract
    change that did not happen.
    """
    out: dict[str, list[str]] = {}
    pending: list[type[Wire]] = [Wire]
    while pending:
        for model in pending.pop().__subclasses__():
            out[model.__name__] = sorted(model.model_fields)
            pending.append(model)
    return dict(sorted(out.items()))
