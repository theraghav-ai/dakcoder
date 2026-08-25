"""The startup capability probe (Part A §4.5).

Why it exists
-------------
We sit behind someone else's LiteLLM in front of someone else's vLLM. The
endpoint's behaviour can change without notice — an upgrade, a model swap, a
chat-template change, ``drop_params`` being flipped — and every one of those
presents as *inexplicable agent behaviour* rather than as an error. A Planner
that silently returns nothing looks like a prompt problem for about a week.

So every row of plan.md §4.2 is asserted here, at startup and in CI, and drift
becomes an immediate legible failure with a named cause.

What it deliberately does not do
--------------------------------
It never fails on ``prompt_tokens_details.cached_tokens`` being absent. That
field is missing from this endpoint today and its absence is plan.md §9 Q1 — the
highest-priority open operational question — so the probe *records* it either
way and reports it as informational. A probe that failed on it would be red from
the first run, and a check that is always red is a check that gets disabled.

It also does not validate against ``/v1/models``. That listing does not include
``Qwen3.8-27B`` even though the model serves correctly, so using it for
discovery would fail on a working endpoint.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from dakcoder_shared.config import LLMConfig
from dakcoder_shared.llm import (
    EmptyCompletionError,
    LLMClient,
    UnsupportedParameterError,
    UpstreamError,
)

__all__ = ["Status", "ProbeResult", "ProbeReport", "CapabilityProbe"]

#: Documented in plan.md §4.2, surfaced verbatim in the 400 on an over-large
#: max_tokens. A ceiling, not a target — the agent caps its own prompts at 32k.
EXPECTED_MAX_MODEL_LEN = 262_144

#: LiteLLM shapes tool-call ids this way. Checked because a change here means
#: the tool-call parser changed, which is exactly the sort of drift that
#: presents as the agent mysteriously failing to call tools.
TOOL_CALL_ID = re.compile(r"^chatcmpl-tool-[0-9a-f]+$")


class Status(StrEnum):
    PASS = "pass"
    #: Worth knowing, not worth failing. Used for capabilities whose absence is
    #: a tracked open question rather than a regression.
    INFO = "info"
    FAIL = "fail"


@dataclass(frozen=True, slots=True)
class ProbeResult:
    name: str
    status: Status
    detail: str
    #: What this being wrong would cost, so a red line explains itself without
    #: anyone opening the plan.
    consequence: str = ""
    duration_ms: int = 0

    def __str__(self) -> str:
        line = f"[{self.status:4}] {self.name}: {self.detail}"
        if self.status is Status.FAIL and self.consequence:
            line += f"\n         impact: {self.consequence}"
        return line


@dataclass
class ProbeReport:
    results: list[ProbeResult] = field(default_factory=list)
    duration_ms: int = 0

    @property
    def ok(self) -> bool:
        return not any(r.status is Status.FAIL for r in self.results)

    @property
    def failures(self) -> list[ProbeResult]:
        return [r for r in self.results if r.status is Status.FAIL]

    def get(self, name: str) -> ProbeResult | None:
        return next((r for r in self.results if r.name == name), None)

    def as_dict(self) -> dict[str, Any]:
        """The shape ``/v1/health`` serves.

        The local runtime reads this at startup and refuses to run modes whose
        required capability failed — so a chat-template regression surfaces on
        the laptop as a clear refusal, not as a Planner that silently returns
        nothing.
        """
        return {
            "ok": self.ok,
            "duration_ms": self.duration_ms,
            "checks": {
                r.name: {"status": str(r.status), "detail": r.detail, "duration_ms": r.duration_ms}
                for r in self.results
            },
        }

    def summary(self) -> str:
        """One startup log line, plus any failures in full."""
        counts = {s: sum(1 for r in self.results if r.status is s) for s in Status}
        head = (
            f"capability probe: {counts[Status.PASS]} pass, "
            f"{counts[Status.INFO]} info, {counts[Status.FAIL]} fail "
            f"({self.duration_ms}ms)"
        )
        if self.ok:
            return head
        return head + "\n" + "\n".join(str(r) for r in self.failures)


class CapabilityProbe:
    """Asserts every documented endpoint behaviour.

    Runs on the gateway only, because the local runtime holds no model
    credential and therefore cannot reach the endpoint directly (§4.7).
    """

    def __init__(self, client: LLMClient) -> None:
        self._client = client

    @property
    def config(self) -> LLMConfig:
        return self._client.config

    def run(self) -> ProbeReport:
        report = ProbeReport()
        started = time.monotonic()
        for check in (
            self._check_completes,
            self._check_thinking_off_returns_content,
            self._check_tool_calling,
            self._check_usage_chunk,
            self._check_cached_tokens,
            self._check_reasoning_effort_rejected,
        ):
            began = time.monotonic()
            try:
                result = check()
            except Exception as exc:  # noqa: BLE001 - a probe reports, it does not raise
                result = ProbeResult(
                    name=getattr(check, "_probe_name", check.__name__),
                    status=Status.FAIL,
                    detail=f"probe raised {type(exc).__name__}: {exc}",
                    consequence="the endpoint could not be exercised at all",
                )
            report.results.append(
                ProbeResult(
                    name=result.name,
                    status=result.status,
                    detail=result.detail,
                    consequence=result.consequence,
                    duration_ms=int((time.monotonic() - began) * 1000),
                )
            )
        report.duration_ms = int((time.monotonic() - started) * 1000)
        return report

    # ── the checks ──────────────────────────────────────────────────────────

    def _check_completes(self) -> ProbeResult:
        """The model answers at all."""
        result = self._client.chat(
            [{"role": "user", "content": "Reply with the single word: ready"}],
            role="fast",
            max_tokens=32,
            enable_thinking=False,
            recover_empty=False,
        )
        if not result.content.strip():
            return ProbeResult(
                "completes", Status.FAIL, "the model returned no content",
                "nothing works; every mode is dead",
            )
        return ProbeResult(
            "completes", Status.PASS,
            f"model={result.model or self.config.model_for('fast')} answered in "
            f"{result.attempts} attempt(s)",
        )

    def _check_thinking_off_returns_content(self) -> ProbeResult:
        """``enable_thinking: false`` yields non-null content.

        The single most consequential row. Qwen3.8-27B left at its default
        returns ``reasoning_content`` with ``content: null``; every mode here
        depends on the parameter reaching the model and switching that off. If
        the chat template changes and it stops taking effect, every turn returns
        nothing and nothing else in the probe would notice.
        """
        try:
            result = self._client.chat(
                [{"role": "user", "content": "Reply with the single word: ready"}],
                role="coder",
                max_tokens=64,
                enable_thinking=False,
                recover_empty=False,
            )
        except EmptyCompletionError as exc:
            return ProbeResult(
                "thinking_off", Status.FAIL,
                f"content was null with thinking off (finish_reason={exc.finish_reason})",
                "chat_template_kwargs is not reaching the model; every mode returns "
                "content: null and every turn is wasted",
            )
        if result.reasoning:
            return ProbeResult(
                "thinking_off", Status.FAIL,
                f"reasoning_content was emitted ({len(result.reasoning)} chars) despite "
                "enable_thinking=false",
                "the parameter is being ignored; turns cost reasoning tokens nobody "
                "budgeted for and latency rises ~15x",
            )
        return ProbeResult(
            "thinking_off", Status.PASS,
            f"content returned ({len(result.content)} chars), no reasoning emitted",
        )

    def _check_tool_calling(self) -> ProbeResult:
        """Native tool calling works, and the ids are shaped as documented."""
        tools = [{
            "type": "function",
            "function": {
                "name": "rules_lint",
                "description": "Check Go against the n-api-template contract.",
                "parameters": {
                    "type": "object",
                    "properties": {"paths": {"type": "array", "items": {"type": "string"}}},
                },
            },
        }]
        result = self._client.chat(
            [{"role": "user", "content": "Lint handler/user.go. Call the tool."}],
            role="coder",
            max_tokens=256,
            enable_thinking=False,
            tools=tools,
            recover_empty=False,
        )
        if not result.tool_calls:
            return ProbeResult(
                "tool_calling", Status.FAIL,
                f"no tool_calls returned (finish_reason={result.finish_reason!r})",
                "the whole tool catalogue is unreachable; the agent would need a "
                "text-parsed ReAct fallback, which this design does not have",
            )
        call = result.tool_calls[0]
        try:
            call.parsed()
        except ValueError as exc:
            return ProbeResult(
                "tool_calling", Status.FAIL, f"tool call arguments are malformed: {exc}",
                "every tool call would fail to dispatch",
            )
        if not TOOL_CALL_ID.match(call.id):
            # Informational: a different id shape still dispatches. It signals
            # the parser changed, which is worth knowing before something else
            # about it does.
            return ProbeResult(
                "tool_calling", Status.INFO,
                f"tool calling works but id {call.id!r} does not match the documented "
                "chatcmpl-tool-<hex> shape; the tool-call parser may have changed",
            )
        if result.finish_reason != "tool_calls":
            return ProbeResult(
                "tool_calling", Status.INFO,
                f"tool call returned with finish_reason={result.finish_reason!r}, "
                "documented as 'tool_calls'",
            )
        return ProbeResult(
            "tool_calling", Status.PASS,
            f"finish_reason=tool_calls, id={call.id}, name={call.name}",
        )

    def _check_usage_chunk(self) -> ProbeResult:
        """``stream_options.include_usage`` produces a final usage chunk.

        Without it there is no accounting at all, and the quota model becomes
        the fiction it is in the frontend agent — which reserves a flat 4,096
        tokens per call and never reconciles.
        """
        result = self._client.chat(
            [{"role": "user", "content": "Count to three."}],
            role="fast",
            max_tokens=64,
            enable_thinking=False,
            recover_empty=False,
        )
        if result.usage.prompt_tokens <= 0:
            return ProbeResult(
                "usage_chunk", Status.FAIL,
                "the stream carried no usage chunk",
                "no token accounting, no cost attribution, and quota can only be "
                "enforced from reservations rather than from measurement",
            )
        return ProbeResult(
            "usage_chunk", Status.PASS,
            f"prompt={result.usage.prompt_tokens} completion={result.usage.completion_tokens} "
            f"reasoning={result.usage.reasoning_tokens}",
        )

    def _check_cached_tokens(self) -> ProbeResult:
        """Is ``prompt_tokens_details.cached_tokens`` reported?

        Informational in both directions, and never a failure. Its absence is
        plan.md §9 Q1: the single largest latency lever available is one we
        currently cannot measure. Recording it here means the day it appears,
        the probe says so and the cached-prefill discount can be switched on.
        """
        result = self._client.chat(
            [{"role": "user", "content": "Count to three."}],
            role="fast",
            max_tokens=64,
            enable_thinking=False,
            recover_empty=False,
        )
        if result.usage.cached_tokens is None:
            return ProbeResult(
                "cached_tokens", Status.INFO,
                "prompt_tokens_details.cached_tokens is absent — prefix-cache hit rate "
                "is not measurable (plan.md §9 Q1). Using novel-token growth as the proxy.",
            )
        return ProbeResult(
            "cached_tokens", Status.PASS,
            f"reported: {result.usage.cached_tokens} of {result.usage.prompt_tokens} "
            f"(hit rate {result.usage.cache_hit_rate}). §9 Q1 can be closed and the "
            f"cached-prefill discount switched on.",
        )

    def _check_reasoning_effort_rejected(self) -> ProbeResult:
        """``reasoning_effort`` 400s, and ``drop_params`` is off.

        Asserting a *rejection* looks odd until you consider the alternative: if
        this starts succeeding, ``drop_params`` has been turned on, and unknown
        parameters are now being silently dropped rather than refused. Every
        future request typo would then fail quietly instead of loudly, and
        §4.5's whole premise — that the endpoint tells us when we are wrong —
        would be gone.
        """
        body = self._client.build_request(
            [{"role": "user", "content": "hi"}],
            role="fast",
            max_tokens=16,
            enable_thinking=False,
        )
        body["reasoning_effort"] = "low"
        try:
            self._client._send(body)  # noqa: SLF001 - the probe tests the transport
        except UnsupportedParameterError as exc:
            return ProbeResult(
                "drop_params_off", Status.PASS,
                f"reasoning_effort rejected as documented: {exc.detail[:120]}",
            )
        except UpstreamError as exc:
            return ProbeResult(
                "drop_params_off", Status.INFO,
                f"reasoning_effort rejected with {exc.status} but not as an "
                f"UnsupportedParamsError: {exc.detail[:120]}",
            )
        except EmptyCompletionError:
            pass
        return ProbeResult(
            "drop_params_off", Status.FAIL,
            "reasoning_effort was accepted; drop_params appears to be ON",
            "unknown parameters are now silently dropped instead of refused, so a "
            "malformed request fails quietly rather than loudly",
        )


def probe_json(report: ProbeReport) -> str:
    return json.dumps(report.as_dict(), indent=2)
