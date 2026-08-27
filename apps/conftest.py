"""A fake LiteLLM endpoint, faithful to plan.md §4.2.

Why a fake rather than the real endpoint
----------------------------------------
Against production a test can only ever confirm the happy path. Against this it
can assert the *failure* paths: that the probe catches a missing usage chunk,
that the client raises a typed error on ``content: null``, that a 429 is retried
and a 400 is not, that an HTML error page is reduced to one sentence.

Those are the behaviours worth having tests for, because they are the ones
nobody exercises by hand and the ones that decide whether an endpoint change
surfaces as a clear failure or as a week of confusing agent behaviour.

Every response shape here is taken from plan.md §4.2's verified capability
matrix — including the two that are documented *absences*: no
``prompt_tokens_details``, and ``reasoning_effort`` returning a 400.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Callable

import httpx
import pytest

# ── SSE construction ────────────────────────────────────────────────────────


def sse(*chunks: dict[str, Any]) -> bytes:
    """Render chunks as an SSE body, terminated the way the endpoint does."""
    body = "".join(f"data: {json.dumps(c)}\n\n" for c in chunks)
    return (body + "data: [DONE]\n\n").encode()


#: Distinguishes "no content key in this delta" from "content: null".
#: The real endpoint emits the second explicitly when reasoning consumed the
#: output budget, and a helper that cannot express it cannot test the check
#: that exists for it.
_UNSET: Any = object()


def delta(content: Any = _UNSET, **extra: Any) -> dict[str, Any]:
    d: dict[str, Any] = {}
    if content is not _UNSET:
        d["content"] = content
    d.update(extra)
    return {"model": "Qwen3.8-27B", "choices": [{"index": 0, "delta": d}]}


def finish(reason: str = "stop") -> dict[str, Any]:
    return {"model": "Qwen3.8-27B", "choices": [{"index": 0, "delta": {}, "finish_reason": reason}]}


def usage_chunk(
    prompt: int = 120,
    completion: int = 20,
    reasoning: int = 0,
    cached: int | None = None,
) -> dict[str, Any]:
    """The final chunk.

    ``cached`` defaults to None because the real endpoint does not report
    ``prompt_tokens_details`` at all — that absence is plan.md §9 Q1, and a fake
    that invented the field would hide the very gap the design works around.
    """
    payload: dict[str, Any] = {
        "model": "Qwen3.8-27B",
        "choices": [],
        "usage": {
            "prompt_tokens": prompt,
            "completion_tokens": completion,
            "total_tokens": prompt + completion,
            "completion_tokens_details": {"reasoning_tokens": reasoning},
        },
    }
    if cached is not None:
        payload["usage"]["prompt_tokens_details"] = {"cached_tokens": cached}
    return payload


def tool_call_chunks(
    name: str = "rules_lint",
    args: str = '{"paths":["handler/user.go"]}',
    call_id: str = "chatcmpl-tool-9f2c1ab4",
) -> list[dict[str, Any]]:
    """A tool call, split across frames the way a real stream delivers it.

    Split deliberately: arguments arrive as fragments, and a client that reads
    only the first frame gets a valid-looking JSON prefix rather than an error.
    """
    half = len(args) // 2
    return [
        {
            "model": "Qwen3.8-27B",
            "choices": [{
                "index": 0,
                "delta": {
                    "content": None,
                    "tool_calls": [{
                        "index": 0,
                        "id": call_id,
                        "type": "function",
                        "function": {"name": name, "arguments": args[:half]},
                    }],
                },
            }],
        },
        {
            "model": "Qwen3.8-27B",
            "choices": [{
                "index": 0,
                "delta": {"tool_calls": [{"index": 0, "function": {"arguments": args[half:]}}]},
            }],
        },
        finish("tool_calls"),
    ]


# ── the fake endpoint ───────────────────────────────────────────────────────


@dataclass
class FakeEndpoint:
    """A configurable stand-in for LiteLLM + vLLM.

    Defaults to the documented, working behaviour; each flag turns on one
    specific drift so a test can assert it is caught.
    """

    #: Emit reasoning_content and content: null, as the model does by default
    #: when enable_thinking is not switched off.
    ignore_thinking_off: bool = False
    #: Reasoning consumed the whole output budget: finish_reason=length with
    #: nothing usable. §4.4's rule 3.
    truncate_reasoning: bool = False
    #: Drop the final usage chunk, as an endpoint without include_usage would.
    omit_usage: bool = False
    #: Report prompt_tokens_details.cached_tokens — what happens the day §9 Q1
    #: is answered.
    report_cached_tokens: int | None = None
    #: Accept reasoning_effort instead of rejecting it, i.e. drop_params ON.
    accept_unknown_params: bool = False
    #: Never emit tool_calls, however the request is shaped.
    refuse_tool_calls: bool = False
    #: A tool-call id that does not match the documented shape.
    tool_call_id: str = "chatcmpl-tool-9f2c1ab4"
    #: Fail this many times with `transient_status` before succeeding.
    transient_failures: int = 0
    transient_status: int = 503
    #: Answer with an nginx-style HTML error page rather than JSON.
    html_errors: bool = False

    requests: list[dict[str, Any]] = field(default_factory=list)
    #: The headers of each request, so the metering the gateway bills on can be
    #: asserted. They are not in the body on purpose — the body is forwarded to
    #: the model, and none of this is the model's business.
    headers: list[dict[str, str]] = field(default_factory=list)
    attempts: int = 0

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.attempts += 1
        body = json.loads(request.content or b"{}")
        self.requests.append(body)
        self.headers.append(dict(request.headers))

        if self.attempts <= self.transient_failures:
            if self.html_errors:
                return httpx.Response(
                    self.transient_status,
                    headers={"Retry-After": "1", "content-type": "text/html"},
                    content=(
                        f"<html><head><title>{self.transient_status} Service Temporarily "
                        f"Unavailable</title></head><body><center>"
                        f"<h1>{self.transient_status} Service Temporarily Unavailable</h1>"
                        f"</center><hr><center>nginx/1.24.0</center></body></html>"
                    ).encode(),
                )
            return httpx.Response(
                self.transient_status,
                headers={"Retry-After": "1"},
                json={"error": {"message": "upstream temporarily unavailable"}},
            )

        unknown = set(body) - _KNOWN_PARAMS
        if unknown and not self.accept_unknown_params:
            # drop_params is off on this proxy, so an unknown parameter is
            # refused rather than silently ignored.
            return httpx.Response(400, json={"error": {
                "message": (
                    f"litellm.UnsupportedParamsError: Qwen3.8-27B does not support "
                    f"parameters: {sorted(unknown)}. To drop unsupported params from "
                    f"the call, set litellm.drop_params=True."
                ),
                "type": "UnsupportedParamsError",
            }})

        if body.get("max_tokens", 0) > 262_144:
            return httpx.Response(400, json={"error": {
                "message": (
                    "This model's maximum context length is 262144 tokens. "
                    f"However, you requested {body['max_tokens']} tokens."
                ),
            }})

        asked_for_thinking = bool((body.get("chat_template_kwargs") or {}).get("enable_thinking"))
        # `ignore_thinking_off` models a chat template that stopped honouring the
        # parameter, so reasoning is emitted whatever was asked for.
        reasoning_on = asked_for_thinking or self.ignore_thinking_off

        if self.truncate_reasoning and reasoning_on:
            return self._stream(
                delta(reasoning_content="Let me think about this. " * 40),
                delta(content=None),
                finish("length"),
                usage_chunk(completion=4000, reasoning=4000, cached=self.report_cached_tokens),
            )

        # Reasoning and tool calling are independent on a real endpoint: a model
        # emitting reasoning_content can still emit tool_calls. Collapsing them
        # here would make a thinking-template regression look as though it broke
        # tool calling too, and the probe would then name two causes for one
        # fault.
        chunks: list[dict[str, Any]] = []
        if reasoning_on:
            chunks.append(delta(reasoning_content="Considering the request. "))
        reasoning_tokens = 60 if reasoning_on else 0

        if body.get("tools") and not self.refuse_tool_calls:
            chunks += tool_call_chunks(call_id=self.tool_call_id)
            chunks.append(usage_chunk(
                completion=52, reasoning=reasoning_tokens, cached=self.report_cached_tokens
            ))
            return self._stream(*chunks)

        chunks += [delta(content="ready"), finish("stop")]
        chunks.append(usage_chunk(
            completion=88 if reasoning_on else 20,
            reasoning=reasoning_tokens,
            cached=self.report_cached_tokens,
        ))
        return self._stream(*chunks)

    def _stream(self, *chunks: dict[str, Any]) -> httpx.Response:
        if self.omit_usage:
            chunks = tuple(c for c in chunks if "usage" not in c)
        return httpx.Response(
            200, headers={"content-type": "text/event-stream"}, content=sse(*chunks)
        )

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)


#: Parameters the endpoint accepts. Anything else is an UnsupportedParamsError,
#: which is the behaviour the probe asserts is still in force.
_KNOWN_PARAMS = {
    "model", "messages", "temperature", "max_tokens", "stream",
    "stream_options", "chat_template_kwargs", "tools", "tool_choice", "user",
}


@pytest.fixture
def endpoint() -> FakeEndpoint:
    return FakeEndpoint()


@pytest.fixture
def no_sleep() -> Callable[[float], None]:
    """Retry backoff without the wait.

    The delays are real (1.5s, 3.5s) and asserting on them matters; waiting for
    them does not, and a suite that takes ten seconds to test retry is one
    people stop running.
    """
    return lambda _seconds: None
