"""Where the context manager, the mode table and the transport meet.

Deliberately thin. Everything here is the wiring between three components that
each own their own decisions — the context manager owns the message list, the
mode table owns budgets and reasoning, the client owns the transport — and the
value of this module is that none of them has to know about the others.

It is not the agent loop. The loop decides *what* to do next; this decides how
one turn is dispatched.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

from dakcoder_shared.config import Deployment, LLMConfig
from dakcoder_shared.llm import ChatResult, LLMClient, Metering

from .context import ContextManager, OverBudgetError
from .modes import Mode, config_for

__all__ = ["TurnResult", "complete", "make_client"]


def make_client(config: LLMConfig, **kwargs: Any) -> LLMClient:
    """Build the client, asserting the credential invariant one more time.

    ``local_config`` already refuses to construct a local configuration holding
    a model key. This is the second of §4.7's three enforcement points, checked
    here because a configuration can be built by hand as well as by that
    factory — and an invariant that only holds on the paved path is not an
    invariant.
    """
    if config.deployment is Deployment.LOCAL and not config.base_url.rstrip("/").endswith("/v1/llm"):
        raise ValueError(
            f"a local runtime must talk to the gateway's /v1/llm proxy, not {config.base_url!r}. "
            "Pointing it at the model directly would need a model credential, which is "
            "exactly the unmetered bypass §15.4 exists to prevent."
        )
    return LLMClient(config, **kwargs)


@dataclass(frozen=True, slots=True)
class TurnResult:
    """One dispatched turn, with what it cost."""

    chat: ChatResult
    mode: Mode
    #: What the context manager estimated before the call.
    estimated_prompt_tokens: int
    #: What the endpoint actually reported.
    actual_prompt_tokens: int

    @property
    def estimate_error(self) -> float:
        """How far the local estimate was out, as a ratio.

        Surfaced because it is the only honest measure of whether the estimator
        is fit for budgeting. It should converge towards 1.0 within a session as
        the calibration takes hold; if it does not, the budget is being enforced
        against a number that means nothing.
        """
        if self.actual_prompt_tokens <= 0:
            return 1.0
        return round(self.estimated_prompt_tokens / self.actual_prompt_tokens, 3)


def complete(
    context: ContextManager,
    client: LLMClient,
    *,
    tools: Sequence[dict[str, Any]] | None = None,
    role: str = "coder",
    enforce_budget: bool = True,
    session_id: str = "",
    on_delta: Callable[[str], None] | None = None,
    tool_choice: str | dict[str, Any] | None = None,
) -> TurnResult:
    """Dispatch one turn from a context manager.

    The budget is checked here rather than left to the endpoint. A prompt that
    exceeds it is a bug in the caller — the context manager exists to prevent
    exactly this — and letting it through means finding out via a 400 from
    someone else's proxy, several seconds later, with the turn already spent.

    The usage is fed straight back into the estimator afterwards. That
    reconciliation is the whole reason ``stream_options.include_usage`` is sent
    on every call, and skipping it here would leave the estimate a guess for the
    life of the process.

    The same estimate goes *out* with the call, as metering headers. This is the
    only place that knows all four facts the gateway needs — which session, which
    turn, which mode, and how big the prompt is — so it is the only place that
    can send them. Without them the gateway reserves against a deliberately
    generous fallback that is never refunded, which is how quota comes to report
    a figure with no relationship to what was actually spent.

    ``on_delta`` is handed straight through to the transport. Nothing is decided
    about it here, because this module is the wiring between three components
    that each own their own decisions and a sink is none of their business.

    ``tool_choice`` likewise. The decision of *when* a tool call is mandatory is
    the loop's -- it is the one component that knows whether this mode may end a
    turn with prose -- and the decision of how to spell it on the wire is the
    client's. This is the seam between them, and it holds nothing of its own.
    """
    mode_config = config_for(context.mode)
    usage = context.usage()

    if enforce_budget and usage.total > usage.budget:
        raise OverBudgetError(
            f"assembled prompt is ~{usage.total:,} tokens against a {usage.budget:,} budget "
            f"for {context.mode}. Compact or narrow the working set before dispatching; "
            f"by-layer: { {str(k): v for k, v in usage.by_layer.items() if v} }"
        )

    result = client.chat(
        context.wire(),
        role=role,
        max_tokens=mode_config.max_tokens,
        enable_thinking=mode_config.enable_thinking,
        tools=tools,
        tool_choice=tool_choice,
        temperature=mode_config.temperature,
        on_delta=on_delta,
        metering=Metering(
            session_id=session_id,
            turn=context.turn,
            mode=str(context.mode),
            estimated_tokens=usage.total,
        ),
    )

    if result.usage.prompt_tokens > 0:
        context.observe_usage(prompt_tokens=result.usage.prompt_tokens)

    # §18 alerts on any non-zero reasoning count in a mode configured for
    # thinking-off, because it means the parameter is not reaching the model.
    # Recorded on the result rather than raised: one anomalous turn is a metric,
    # not a reason to fail the run.
    return TurnResult(
        chat=result,
        mode=context.mode,
        estimated_prompt_tokens=usage.total,
        actual_prompt_tokens=result.usage.prompt_tokens,
    )


def reasoning_leaked(result: TurnResult) -> bool:
    """Whether a thinking-off mode was charged reasoning tokens.

    The §18 alert condition. Non-zero here means ``chat_template_kwargs`` is not
    taking effect, which costs roughly 15x the latency for no quality gain — and
    unlike an outright failure, it presents as the agent simply being slow.
    """
    return not config_for(result.mode).enable_thinking and result.chat.usage.reasoning_tokens > 0
