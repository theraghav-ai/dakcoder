"""Tests for the model proxy (Part A §15.4).

The proxy exists for one reason: the model API key is a single shared secret, and
if a laptop held it every limit in §16 would be decorative. So the tests that
matter are the ones asserting the key never leaves, the model is never chosen by
the client, and the accounting survives every way a stream can end.
"""

from __future__ import annotations

import json

import pytest

from dakcoder_gateway.ledger import MemoryLedger
from dakcoder_gateway.proxy import ModelProxy, ProxyError, TeedUsage
from dakcoder_gateway.quota import Lane, Limits, MemoryStore, QuotaExceeded, QuotaPolicy

from fakes import API_KEY, FakeUpstream

@pytest.fixture
def quota() -> QuotaPolicy:
    limits = Limits(tokens_per_hour=100_000, tokens_per_window=100_000)
    return QuotaPolicy(MemoryStore(limits), limits)


@pytest.fixture
def ledger() -> MemoryLedger:
    return MemoryLedger()


@pytest.fixture
def proxy(upstream: FakeUpstream, quota: QuotaPolicy, ledger: MemoryLedger) -> ModelProxy:
    return ModelProxy(
        "https://ai.cept.gov.in/v1", API_KEY, quota, ledger=ledger, http=upstream
    )


async def consume(proxy: ModelProxy, **kw) -> list[bytes]:
    """Read a call to the end, then wait for its settlement.

    The wait is not test scaffolding around a race — it is the contract.
    Settlement is deliberately scheduled onto a task the app owns rather than
    awaited inside the request (defect D-1: a client that closes the response on
    ``[DONE]`` cancels the request task, and a cancelled task cannot await), so
    "the ledger has the row" is a claim about *after the settlement lands*, and
    ``drain`` is how the server itself waits for that at shutdown.
    """
    body = kw.pop("body", {"model": "coder", "messages": [], "stream": True})
    try:
        chunks = [
            chunk
            async for chunk in proxy.stream(
                "chat/completions", body, sub="gitlab:7", estimated=4_000, **kw
            )
        ]
    finally:
        # In the ``finally`` because a stream that fails halfway is precisely the
        # case where settlement still has to happen: the model produced those
        # tokens whatever the connection did afterwards.
        await proxy.drain()
    return chunks


# ── the credential ──────────────────────────────────────────────────────────


def test_the_gateway_refuses_to_start_without_a_key(quota: QuotaPolicy) -> None:
    """It is the only process that may hold one. Without it there is nothing to
    proxy, and starting anyway would produce 502s nobody could explain."""
    with pytest.raises(ValueError, match="only process"):
        ModelProxy("https://ai.cept.gov.in/v1", "", quota)


async def test_the_key_is_attached_upstream_and_appears_nowhere_else(
    proxy: ModelProxy, upstream: FakeUpstream
) -> None:
    chunks = await consume(proxy)

    assert upstream.headers[0]["Authorization"] == f"Bearer {API_KEY}"
    assert API_KEY not in b"".join(chunks).decode()
    assert API_KEY not in json.dumps(upstream.requests[0])


# ── the model is ours to choose ─────────────────────────────────────────────


async def test_the_client_names_a_role_and_the_gateway_names_the_model(
    proxy: ModelProxy, upstream: FakeUpstream
) -> None:
    """Forwarding the client's model would let a developer route to a model
    nobody has budgeted for, on a shared GPU, with our key attached."""
    await consume(proxy, body={"model": "planner", "messages": [], "stream": True})
    assert upstream.requests[0]["model"] == "Qwen3.8-27B"


async def test_an_unknown_role_is_refused_with_the_list(proxy: ModelProxy) -> None:
    with pytest.raises(ProxyError) as caught:
        await consume(proxy, body={"model": "gpt-4o", "messages": [], "stream": True})
    assert "not a configured role" in str(caught.value)
    assert "coder" in str(caught.value)


async def test_only_the_two_declared_paths_are_proxied(proxy: ModelProxy) -> None:
    """Not a general-purpose passthrough: a new upstream capability must not
    become reachable by accident."""
    with pytest.raises(ProxyError) as caught:
        proxy.prepare("models", {}, "gitlab:7")
    assert caught.value.status == 404


async def test_the_subject_is_sent_as_user_for_upstream_attribution(
    proxy: ModelProxy, upstream: FakeUpstream
) -> None:
    """§16.6 phase 1: LiteLLM's own spend tables attribute correctly even before
    per-user virtual keys exist. It costs nothing and makes the cross-check
    meaningful from day one."""
    await consume(proxy)
    assert upstream.requests[0]["user"] == "gitlab:7"


async def test_include_usage_is_forced_on_every_stream(
    proxy: ModelProxy, upstream: FakeUpstream
) -> None:
    """Without the usage chunk there is no accounting, and quota could only be
    enforced from reservations — which is the frontend agent's failure."""
    await consume(proxy, body={"model": "coder", "messages": [], "stream": True})
    assert upstream.requests[0]["stream_options"]["include_usage"] is True


# ── the relay ───────────────────────────────────────────────────────────────


async def test_chunks_are_relayed_as_they_arrive(proxy: ModelProxy) -> None:
    """Buffering to read the usage chunk first would destroy first-token latency
    and defeat the whole streaming design."""
    chunks = await consume(proxy)
    text = b"".join(chunks).decode()
    assert "package " in text
    assert text.index("package ") < text.index("usage")


async def test_the_sse_framing_survives_the_relay(proxy: ModelProxy) -> None:
    """httpx strips the newlines that frame an SSE event. A relay that forgets
    to put them back produces a stream that parses as one enormous event, which
    looks exactly like the model hanging."""
    text = b"".join(await consume(proxy)).decode()
    assert "\n\n" in text
    assert text.endswith("\n")


async def test_an_upstream_error_does_not_leak_its_body(
    proxy: ModelProxy, upstream: FakeUpstream
) -> None:
    """nginx returns HTML and LiteLLM returns JSON that can echo the request.
    Neither belongs in a message that reaches a log, a trace and a screen."""
    upstream.status = 500
    with pytest.raises(ProxyError) as caught:
        await consume(proxy)
    assert "model not found" not in str(caught.value)
    assert caught.value.status == 502


# ── the tee ─────────────────────────────────────────────────────────────────


async def test_usage_is_teed_and_reconciled(
    proxy: ModelProxy, quota: QuotaPolicy
) -> None:
    await consume(proxy)
    snapshot = await quota.snapshot("gitlab:7")
    # Reserved 4,000; the endpoint reported 1,200 + 340.
    assert snapshot.used["hour_tokens"] == 1_540


async def test_every_turn_reaches_the_ledger(
    proxy: ModelProxy, ledger: MemoryLedger
) -> None:
    await consume(proxy, session_id="s-1", turn=3, mode="coder")

    assert len(ledger.events) == 1
    event = ledger.events[0]
    assert event.sub == "gitlab:7"
    assert event.session_id == "s-1"
    assert event.turn == 3
    assert event.prompt_tokens == 1_200
    assert event.billed_tokens == 1_540
    assert event.estimated_tokens == 4_000
    assert event.latency_ms >= 0


async def test_a_client_that_stops_reading_at_done_is_still_settled(
    proxy: ModelProxy, quota: QuotaPolicy, ledger: MemoryLedger
) -> None:
    """Defect D-1, as a test.

    ``LLMClient`` used to break out of its loop the moment it saw ``[DONE]`` and
    close the response. Starlette read that as a disconnect and cancelled the
    response task, so the generator's ``finally`` ran under cancellation and its
    first ``await`` raised ``CancelledError`` — no reconcile, no ledger row. The
    reservation stood for ever, so every agent turn was billed at a deliberately
    generous estimate and the ledger, the system of record, held nothing at all.

    Every other test here drains to EOF, which is exactly why this passed CI. So
    this one abandons the stream the way the real client did.
    """
    async for chunk in proxy.stream(
        "chat/completions",
        {"model": "coder", "messages": [], "stream": True},
        sub="gitlab:7",
        estimated=4_000,
        session_id="s-9",
        turn=2,
    ):
        if b"[DONE]" in chunk:
            break  # and never come back for the rest

    await proxy.drain()

    assert len(ledger.events) == 1, "the turn never reached the ledger"
    # Reconciled to the endpoint's own figures, not left at the 4,000 reserved.
    assert (await quota.snapshot("gitlab:7")).used["hour_tokens"] == 1_540
    assert ledger.events[0].session_id == "s-9"


async def test_reasoning_tokens_are_teed_separately(
    proxy: ModelProxy, upstream: FakeUpstream, ledger: MemoryLedger
) -> None:
    """A thinking-on Planner turn can spend more output on reasoning than on the
    plan. Folding it into completion makes §4.4's choices unmeasurable."""
    upstream.chunks[3] = (
        'data: {"usage":{"prompt_tokens":900,"completion_tokens":2500,'
        '"completion_tokens_details":{"reasoning_tokens":2100}}}'
    )
    await consume(proxy)
    assert ledger.events[0].reasoning_tokens == 2_100


async def test_cached_tokens_are_read_even_though_the_field_is_absent_today(
    proxy: ModelProxy, upstream: FakeUpstream, ledger: MemoryLedger
) -> None:
    """plan.md §9 Q1. Read anyway, so the day it appears the discount has data
    rather than needing a code change."""
    upstream.chunks[3] = (
        'data: {"usage":{"prompt_tokens":1000,"completion_tokens":100,'
        '"prompt_tokens_details":{"cached_tokens":760}}}'
    )
    await consume(proxy)
    assert ledger.events[0].cached_tokens == 760


async def test_a_missing_usage_chunk_does_not_refund_the_reservation(
    proxy: ModelProxy, upstream: FakeUpstream, quota: QuotaPolicy
) -> None:
    """A turn that produced output certainly cost something. Refunding what we
    cannot measure would make a broken endpoint the cheapest way to use the
    service — the probe's usage_chunk check exists so this is noticed as an
    endpoint fault rather than absorbed."""
    upstream.chunks = [c for c in upstream.chunks if "usage" not in c]
    await consume(proxy)

    snapshot = await quota.snapshot("gitlab:7")
    assert snapshot.used["hour_tokens"] == 4_000


async def test_an_unparseable_chunk_does_not_break_the_stream(
    proxy: ModelProxy, upstream: FakeUpstream
) -> None:
    """It costs us that chunk's accounting, not the turn the developer is
    watching."""
    upstream.chunks.insert(1, "data: {not json")
    chunks = await consume(proxy)
    assert b"handler" in b"".join(chunks)


# ── how a stream ends ───────────────────────────────────────────────────────


async def test_a_call_that_never_started_is_refunded(
    proxy: ModelProxy, upstream: FakeUpstream, quota: QuotaPolicy
) -> None:
    """A connection reset before a byte arrived has cost nothing, and holding
    its reservation would make a flaky network look like heavy usage."""
    upstream.explode_after = 0
    with pytest.raises(ProxyError):
        await consume(proxy)

    assert (await quota.snapshot("gitlab:7")).used["hour_tokens"] == 0


async def test_a_stream_that_drops_midway_is_still_billed(
    proxy: ModelProxy, upstream: FakeUpstream, quota: QuotaPolicy, ledger: MemoryLedger
) -> None:
    """The model produced tokens. Losing them would make abandoning turns the
    cheapest way to use the service."""
    upstream.explode_after = 2
    with pytest.raises(ConnectionError):
        await consume(proxy)

    assert (await quota.snapshot("gitlab:7")).used["hour_tokens"] > 0
    assert len(ledger.events) == 1


# ── fail closed ─────────────────────────────────────────────────────────────


async def test_an_exhausted_quota_stops_the_call_before_it_is_sent(
    upstream: FakeUpstream, ledger: MemoryLedger
) -> None:
    limits = Limits(tokens_per_hour=1_000)
    quota = QuotaPolicy(MemoryStore(limits), limits)
    proxy = ModelProxy("https://x/v1", API_KEY, quota, ledger=ledger, http=upstream)

    with pytest.raises(QuotaExceeded):
        await consume(proxy)

    assert upstream.requests == [], "the request reached the model despite being refused"


async def test_background_work_is_metered_in_its_own_lane(
    proxy: ModelProxy, ledger: MemoryLedger
) -> None:
    await consume(proxy, lane=Lane.BACKGROUND)
    assert ledger.events[0].lane == "background"


# ── the tee, in isolation ───────────────────────────────────────────────────


def test_no_report_is_distinguished_from_zero_tokens() -> None:
    """Zero tokens and no report are very different facts: the first is a turn
    that cost nothing, the second is accounting that has stopped working."""
    teed = TeedUsage()
    assert not teed.saw_usage

    teed.observe({"usage": {"prompt_tokens": 0, "completion_tokens": 0}})
    assert teed.saw_usage
    assert teed.prompt_tokens == 0
