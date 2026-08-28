"""The LLM client: request shaping, streaming, retry and usage accounting.

Built on httpx rather than the OpenAI SDK
-----------------------------------------
Part A §4.7 sketches this with ``openai.OpenAI``. The endpoint is
OpenAI-compatible HTTP with SSE, and everything that actually matters here is
about *what request is made* and *how the stream is consumed*:
``chat_template_kwargs`` for reasoning control, ``stream_options.include_usage``
for accounting, ``trust_env=False`` so the corporate proxy is never inherited,
HTTP/1.1 because nginx's HTTP/2 handling breaks long-lived streams, and a retry
policy we own rather than one the SDK owns.

None of that needs the SDK, and dropping it removes a package plus its
dependency closure from the wheels Part B §4.3 has to vendor into the ``.vsix``
for offline install. The request shape is unchanged, so the seam is still there
if the SDK is ever wanted.

What this module refuses to do
------------------------------
It does not read a credential from the environment. It is handed an
``LLMConfig``, which is where the "the key lives in one place" invariant is
enforced. A transport that could quietly find a key would make that invariant a
comment.
"""

from __future__ import annotations

import json
import random
import re
import time
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, field
from typing import Any

import httpx

from .config import Deployment, LLMConfig

__all__ = [
    "LLMClient",
    "ChatResult",
    "Metering",
    "Usage",
    "ToolCall",
    "EmptyCompletionError",
    "UnsupportedParameterError",
    "UpstreamError",
]

#: Retry backoff, from §4.7. Jitter is added on top so a fleet of laptops
#: recovering from the same 503 does not resynchronise into a thundering herd.
BACKOFF_SECONDS = (1.5, 3.5)

#: Status codes worth a second attempt. A non-429 4xx never is: LiteLLM's
#: UnsupportedParamsError is a bug in our request, and retrying it just spends a
#: second discovering that again.
RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})

_HTML_TAG = re.compile(r"<[^>]+>")
_WHITESPACE = re.compile(r"\s+")


@dataclass(frozen=True, slots=True)
class Metering:
    """What the gateway needs to meter and attribute one call.

    Sent as headers rather than in the body, because the body is forwarded
    upstream and none of this is the model's business — and because the gateway
    reads them *before* it decides whether the call may go at all.

    Omitting them is not neutral. Without ``estimated_tokens`` the gateway falls
    back to a deliberately generous guess, so every reservation over-charges
    against every limit and the calibration loop that is meant to close the gap
    has nothing to learn from; without ``session_id``, ``turn`` and ``mode`` the
    ledger — the system of record for "what did this team spend on migrations
    last month" — holds rows that cannot answer the question it exists for.
    """

    #: The agent session this turn belongs to.
    session_id: str = ""
    #: Which turn of that session. 1-based; 0 means "not part of a run".
    turn: int = 0
    #: The agent mode, so spend can be attributed per phase.
    mode: str = ""
    #: The caller's own estimate of the assembled prompt, in tokens. The caller
    #: knows this exactly — it assembled the prompt — so a zero here is a caller
    #: that did not bother rather than a call that cannot be estimated.
    estimated_tokens: int = 0
    #: Priority lane. Background work is shed before interactive work.
    lane: str = "interactive"

    def headers(self) -> dict[str, str]:
        out: dict[str, str] = {"X-Lane": self.lane}
        if self.session_id:
            out["X-Session-Id"] = self.session_id
        if self.turn > 0:
            out["X-Turn"] = str(self.turn)
        if self.mode:
            out["X-Mode"] = self.mode
        if self.estimated_tokens > 0:
            out["X-Estimated-Tokens"] = str(self.estimated_tokens)
        return out


class UpstreamError(RuntimeError):
    """A non-retryable HTTP failure from the proxy or the model."""

    def __init__(self, status: int, detail: str) -> None:
        super().__init__(f"upstream returned {status}: {detail}")
        self.status = status
        self.detail = detail


class UnsupportedParameterError(UpstreamError):
    """LiteLLM rejected a parameter.

    Its own class because it is always *our* bug, never a transient condition.
    ``drop_params`` is off on this proxy, so an unknown parameter 400s rather
    than being silently ignored — which is the right behaviour and is what makes
    the startup capability probe load-bearing rather than decorative.
    """


class EmptyCompletionError(RuntimeError):
    """The model returned ``content: null``.

    A typed error rather than an empty string, because the two need completely
    different handling and treating the first as the second is how a wasted turn
    becomes an invisible one.

    This is §4.4's rule 3. It happens when a reasoning block consumes the entire
    output budget: ``finish_reason: "length"`` with nothing usable. The fix is to
    retry with thinking **off**, not with a bigger budget — a bigger budget is
    what produced the spike's 31-second run.
    """

    def __init__(self, finish_reason: str, reasoning_tokens: int) -> None:
        super().__init__(
            f"model returned content: null (finish_reason={finish_reason!r}, "
            f"reasoning_tokens={reasoning_tokens}); the reasoning block consumed "
            f"the output budget"
        )
        self.finish_reason = finish_reason
        self.reasoning_tokens = reasoning_tokens


@dataclass(frozen=True, slots=True)
class Usage:
    """Token accounting for one turn.

    Read from the stream's final chunk. ``stream_options.include_usage`` is sent
    on every call for exactly this: without it there is no usage chunk, which is
    why the frontend agent reserves a flat 4,096 tokens and never refunds.
    """

    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    #: Charged against the output budget, not the prompt budget. Metered per
    #: mode so §4.4's on/off choices have evidence behind them rather than
    #: becoming superstition.
    reasoning_tokens: int = 0
    #: Absent from this endpoint today (plan.md §9 Q1), so ``None`` means
    #: "not reported" and is deliberately distinct from zero. Billing the cached
    #: discount is behind a flag that activates the day the field appears.
    cached_tokens: int | None = None

    @property
    def cache_hit_rate(self) -> float | None:
        if self.cached_tokens is None or self.prompt_tokens <= 0:
            return None
        return round(self.cached_tokens / self.prompt_tokens, 4)


@dataclass(frozen=True, slots=True)
class ToolCall:
    id: str
    name: str
    arguments: str

    def parsed(self) -> dict[str, Any]:
        try:
            return json.loads(self.arguments or "{}")
        except json.JSONDecodeError as exc:
            raise ValueError(f"tool call {self.name} has malformed arguments: {exc}") from exc


@dataclass
class ChatResult:
    """One completed turn."""

    content: str = ""
    reasoning: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    finish_reason: str = ""
    usage: Usage = field(default_factory=Usage)
    model: str = ""
    #: How many HTTP attempts this turn cost, for telemetry.
    attempts: int = 1
    #: True when the turn was retried with thinking off after an empty
    #: completion. Counted, because §18 wants zero of these.
    recovered_from_empty: bool = False

    @property
    def truncated(self) -> bool:
        """Whether the model ran out of output budget mid-reply.

        Worth a name because of what it does to a *tool call*. Arguments arrive
        as streamed fragments and are concatenated, so a turn cut off partway
        through one leaves a valid-looking JSON prefix — in the wild, the single
        character ``{``. Parsing it raises "Expecting property name enclosed in
        double quotes", which reads as the model having emitted bad JSON.

        It has not. It emitted correct JSON and was interrupted. Telling it to
        send valid JSON is advice it cannot act on, because the fix is to
        produce *less* output, not different output — so it makes the same
        oversized reply, is cut off at the same place, and the run dies against
        the no-progress detector three turns later. That is the loop this
        property exists to let the caller break.
        """
        return self.finish_reason == "length"

    def incomplete_tool_calls(self) -> list[ToolCall]:
        """Tool calls whose arguments did not survive the cut.

        ``truncated`` is the declared signal and the one to trust. It is not the
        only one, which is how the case this method exists for got through
        anyway: a server reported ``finish_reason: "tool_calls"`` on a reply cut
        off inside a call, the arguments arrived as the bare character ``{``,
        and with nothing saying it had been interrupted the router answered
        "arguments are not valid JSON" — the exact advice the model cannot act
        on.

        So arguments that are unparseable *and shaped like a prefix* — braces
        opened and never closed, or a string left hanging — count here whatever
        the finish reason claims. Arguments unparseable for any other reason are
        genuinely malformed and stay with the router, which has a message for
        them.
        """
        out = []
        for call in self.tool_calls:
            try:
                call.parsed()
            except ValueError:
                if self.truncated or _looks_cut_off(call.arguments):
                    out.append(call)
        return out


def _looks_cut_off(arguments: str) -> bool:
    """Whether unparseable arguments are a *prefix* of valid JSON rather than junk.

    A single scan, tracking string state so a brace inside a quoted value is not
    counted as structure. Ending with depth still open, or inside a string, or
    on a trailing escape, means the text stopped early — which is what being
    interrupted looks like and is not what a model emitting bad JSON produces.
    """
    text = arguments.strip()
    if not text.startswith(("{", "[")):
        return False

    depth = 0
    in_string = False
    escaped = False
    for char in text:
        if escaped:
            escaped = False
        elif char == "\\":
            escaped = True
        elif char == '"':
            in_string = not in_string
        elif not in_string:
            if char in "{[":
                depth += 1
            elif char in "}]":
                depth -= 1
                if depth < 0:
                    # More closers than openers is malformed, not truncated.
                    return False
    return depth > 0 or in_string or escaped


def strip_html(body: str, *, limit: int = 300) -> str:
    """Reduce an upstream HTML error page to one sentence.

    nginx and LiteLLM both answer some failures with a full HTML page. Putting
    that into an error message — and from there into a prompt or a chat bubble —
    buries the one useful line in markup.
    """
    text = _WHITESPACE.sub(" ", _HTML_TAG.sub(" ", body)).strip()
    return text[:limit] + ("…" if len(text) > limit else "")


class LLMClient:
    """A process-wide client for one endpoint.

    One instance per process, not one per run. The frontend agent constructs its
    client inside ``agent.run``, paying a TCP and TLS handshake for every task
    with no connection reuse (finding S10).
    """

    def __init__(
        self,
        config: LLMConfig,
        *,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
        jitter: Callable[[], float] = lambda: random.uniform(0.0, 0.5),
    ) -> None:
        self._config = config
        self._sleep = sleep
        self._jitter = jitter
        self._client = httpx.Client(
            base_url=config.base_url,
            timeout=httpx.Timeout(
                connect=config.connect_timeout,
                read=config.read_timeout,
                write=config.write_timeout,
                pool=5.0,
            ),
            # nginx's HTTP/2 handling is a known source of buffering and dropped
            # streams on long-lived responses; HTTP/1.1 with keep-alive plus
            # `proxy_buffering off` is the combination that works.
            http2=False,
            limits=httpx.Limits(max_keepalive_connections=32, keepalive_expiry=300),
            # Encodes the launcher lesson in code rather than a shell script:
            # the corporate proxy must never be inherited for internal hosts.
            trust_env=False,
            transport=transport,
            headers={"Authorization": f"Bearer {config.api_key}"},
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> LLMClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    @property
    def config(self) -> LLMConfig:
        return self._config

    # ── the request ─────────────────────────────────────────────────────────

    def build_request(
        self,
        messages: Sequence[dict[str, Any]],
        *,
        role: str = "coder",
        max_tokens: int,
        enable_thinking: bool,
        tools: Sequence[dict[str, Any]] | None = None,
        temperature: float | None = None,
    ) -> dict[str, Any]:
        """Assemble the request body.

        Separated from sending so a test — and the capability probe — can assert
        on the exact shape without a round trip. Every field here is
        load-bearing; the comments say which and why.
        """
        body: dict[str, Any] = {
            "model": self._config.model_for(role),
            "messages": list(messages),
            "temperature": (
                temperature if temperature is not None else self._config.temperature_for(role)
            ),
            "max_tokens": max_tokens,
            "stream": True,
            # Without this there is no usage chunk and therefore no accounting.
            "stream_options": {"include_usage": True},
            # Reasoning control. Verified working on this endpoint; note that
            # `reasoning_effort` is *rejected* here, so this is the only lever.
            "chat_template_kwargs": {"enable_thinking": enable_thinking},
        }
        if tools:
            body["tools"] = list(tools)
        if self._config.user:
            # Attribution at the proxy, so LiteLLM's own spend tables are
            # per-user even before per-user virtual keys exist.
            body["user"] = self._config.user
        return body

    def chat(
        self,
        messages: Sequence[dict[str, Any]],
        *,
        role: str = "coder",
        max_tokens: int = 4096,
        enable_thinking: bool = False,
        tools: Sequence[dict[str, Any]] | None = None,
        temperature: float | None = None,
        recover_empty: bool = True,
        metering: Metering | None = None,
        on_delta: Callable[[str], None] | None = None,
    ) -> ChatResult:
        """Run one turn.

        On an empty completion, retries **once with thinking off**. Not with a
        larger budget: reasoning expands to fill whatever it is given, so a
        larger budget is what produced the spike's 31-second run for the same
        330-character answer.

        ``metering`` is what the gateway bills and attributes this turn by. It
        is optional because a probe or a one-off script has nothing meaningful
        to put in it; a turn of an actual run always should.

        ``on_delta`` is called with each fragment of *content* as it arrives.
        This is the seam the streaming path was missing. The request has always
        been made with ``stream: True`` and the gateway has always relayed chunk
        by chunk, but this method folded the stream into one value and returned
        at EOF, so nothing downstream could see a turn until it was over.

        Reasoning fragments are deliberately not offered. Thinking is off in
        every mode, so a reasoning feed here would be the symptom of a fault
        rather than a thing to render, and it is already reported as a number by
        ``usage.reasoning_tokens``.
        """
        body = self.build_request(
            messages,
            role=role,
            max_tokens=max_tokens,
            enable_thinking=enable_thinking,
            tools=tools,
            temperature=temperature,
        )
        headers = metering.headers() if metering else None
        try:
            return self._send(body, headers, on_delta)
        except EmptyCompletionError:
            if not recover_empty or not enable_thinking:
                raise
            retry = dict(body)
            retry["chat_template_kwargs"] = {"enable_thinking": False}
            # Still streamed. An empty completion is by definition one that
            # produced no content, so there is nothing already said that the
            # recovered answer would be talking over.
            result = self._send(retry, headers, on_delta)
            result.recovered_from_empty = True
            return result

    # ── transport ───────────────────────────────────────────────────────────

    def _send(
        self,
        body: dict[str, Any],
        headers: dict[str, str] | None = None,
        on_delta: Callable[[str], None] | None = None,
    ) -> ChatResult:
        last: Exception | None = None
        # Whether any fragment has actually reached the caller. Not "which
        # attempt is this": a status-code failure is raised before a single line
        # is read, so the ordinary retry — a 503 or a 429 — has said nothing and
        # its replacement is free to stream normally.
        spoken = False

        def sink(fragment: str) -> None:
            nonlocal spoken
            spoken = True
            on_delta(fragment)  # type: ignore[misc] - only built when on_delta exists

        for attempt in range(1, self._config.max_attempts + 1):
            try:
                # An attempt that follows one which already spoke stays silent.
                #
                # It would be re-generating text the caller has been handed, and
                # deltas are append-only on the wire: there is no way to un-say
                # them. Only a failure part-way through a stream can reach here
                # having spoken. Staying silent leaves that partial answer on
                # screen for a moment, and the authoritative message at the end
                # of the turn replaces it, which is what that message is for.
                result = self._stream_once(
                    body, headers, None if (spoken or on_delta is None) else sink
                )
                result.attempts = attempt
                return result
            except UnsupportedParameterError:
                # Always our bug. Retrying spends a second learning nothing.
                raise
            except UpstreamError as exc:
                if exc.status not in RETRYABLE_STATUS or attempt == self._config.max_attempts:
                    raise
                last = exc
            except (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout) as exc:
                if attempt == self._config.max_attempts:
                    raise
                last = exc

            delay = BACKOFF_SECONDS[min(attempt - 1, len(BACKOFF_SECONDS) - 1)]
            self._sleep(delay + self._jitter())

        raise last or RuntimeError("request failed with no recorded cause")

    def _stream_once(
        self,
        body: dict[str, Any],
        headers: dict[str, str] | None = None,
        on_delta: Callable[[str], None] | None = None,
    ) -> ChatResult:
        with self._client.stream(
            "POST", "/chat/completions", json=body, headers=headers
        ) as response:
            if response.status_code >= 400:
                response.read()
                raise self._error_for(response)
            return _consume_stream(response.iter_lines(), on_delta)

    @staticmethod
    def _error_for(response: httpx.Response) -> UpstreamError:
        raw = response.text
        detail = raw
        try:
            payload = response.json()
            # Two error envelopes reach this method. Upstream (OpenAI-shaped)
            # sends ``error: {message: ...}``; our own gateway sends
            # ``error: "<kind>", reason: "<sentence>"``. Assuming the first
            # shape turned every gateway refusal — a 429 on quota, a 401 on an
            # expired token — into ``'str' object has no attribute 'get'``,
            # which is the one message that says nothing about what happened.
            error = payload.get("error")
            detail = (
                (error.get("message") if isinstance(error, dict) else None)
                or payload.get("reason")
                or (error if isinstance(error, str) else None)
                or payload.get("detail")
                or json.dumps(payload)[:300]
            )
        except (json.JSONDecodeError, ValueError):
            detail = strip_html(raw)

        if response.status_code == 400 and "unsupportedparams" in detail.lower().replace(" ", ""):
            return UnsupportedParameterError(response.status_code, detail)
        return UpstreamError(response.status_code, detail)


def _consume_stream(
    lines: Iterator[str], on_delta: Callable[[str], None] | None = None
) -> ChatResult:
    """Fold an SSE stream into one result, offering content as it passes.

    Tool-call fragments arrive indexed and split across chunks, so they are
    accumulated by index and assembled at the end. Doing it any other way
    produces arguments that are valid-looking JSON prefixes. Content is
    different: a fragment is complete the moment it arrives, so it is handed to
    ``on_delta`` on the way past as well as accumulated.

    The callback is not guarded here. The caller whose sink can fail is the one
    that knows what a failure means, and swallowing it at this depth would hide
    the symptom that is hardest to notice from outside: a panel that has quietly
    stopped streaming.
    """
    result = ChatResult()
    content: list[str] = []
    reasoning: list[str] = []
    calls: dict[int, dict[str, str]] = {}
    saw_content_key = False
    done = False

    for line in lines:
        line = line.strip()
        if not line or not line.startswith("data:"):
            continue
        payload = line[len("data:") :].strip()
        if payload == "[DONE]":
            # Noted, not broken on. Breaking here closes the response while the
            # server is still inside the `finally` that reconciles quota and
            # writes the ledger row: Starlette sees the disconnect, cancels the
            # response task, and the first `await` in that block raises
            # `CancelledError`. The reservation is then never reconciled and the
            # turn never reaches the ledger — which is defect D-1, and it made
            # abandoning the last two bytes of a stream the cheapest way to use
            # the service. Reading on to EOF costs nothing: `[DONE]` is the last
            # frame, so this is one more iteration that ends the loop anyway.
            done = True
            continue
        if done:
            # Anything after `[DONE]` is not part of this completion.
            continue
        try:
            chunk = json.loads(payload)
        except json.JSONDecodeError:
            # A malformed frame is a proxy bug, not a reason to lose the turn.
            continue

        if model := chunk.get("model"):
            result.model = model

        # The usage chunk carries no choices, and arrives last.
        if usage := chunk.get("usage"):
            details = usage.get("completion_tokens_details") or {}
            prompt_details = usage.get("prompt_tokens_details") or {}
            result.usage = Usage(
                prompt_tokens=usage.get("prompt_tokens", 0) or 0,
                completion_tokens=usage.get("completion_tokens", 0) or 0,
                total_tokens=usage.get("total_tokens", 0) or 0,
                reasoning_tokens=details.get("reasoning_tokens", 0) or 0,
                # None, not 0. "Not reported" and "nothing was cached" are
                # different facts, and plan.md §9 Q1 turns on the difference.
                cached_tokens=prompt_details.get("cached_tokens"),
            )

        for choice in chunk.get("choices") or []:
            if reason := choice.get("finish_reason"):
                result.finish_reason = reason
            delta = choice.get("delta") or {}

            if "content" in delta:
                saw_content_key = True
                if delta["content"]:
                    content.append(delta["content"])
                    if on_delta is not None:
                        on_delta(delta["content"])
            if delta.get("reasoning_content"):
                reasoning.append(delta["reasoning_content"])

            for fragment in delta.get("tool_calls") or []:
                index = fragment.get("index", 0)
                slot = calls.setdefault(index, {"id": "", "name": "", "arguments": ""})
                if fragment.get("id"):
                    slot["id"] = fragment["id"]
                fn = fragment.get("function") or {}
                if fn.get("name"):
                    slot["name"] = fn["name"]
                if fn.get("arguments"):
                    slot["arguments"] += fn["arguments"]

    result.content = "".join(content)
    result.reasoning = "".join(reasoning)
    result.tool_calls = [
        ToolCall(id=slot["id"], name=slot["name"], arguments=slot["arguments"])
        for _, slot in sorted(calls.items())
        if slot["name"]
    ]

    # An empty completion is a wasted turn, and it has to be visible as one.
    # Guarded on having actually seen a content key so that a pure tool-call
    # turn — which legitimately carries no content — is not mistaken for it.
    if not result.content and not result.tool_calls and saw_content_key:
        raise EmptyCompletionError(result.finish_reason, result.usage.reasoning_tokens)

    return result
