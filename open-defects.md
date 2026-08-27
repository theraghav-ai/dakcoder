# Open defects

Found while bringing the server side up on VMAIPROD1 (2026-08-26). Each entry
says what is wrong, how it was measured, and what it costs.

**Nothing is open.** D-1 was fixed on 2026-08-27 and is kept below with its
measurement, because the measurement is what makes the fix reviewable. The two
defects fixed on 2026-08-26 to make the stack run at all are recorded after it.

---

## D-1 · Settlement is skipped on every agent turn — **fixed 2026-08-27**

**Severity: high.** The usage ledger had no record of real agent work, and quota
was never reconciled.

**Fixed by** ARCHITECTURE D-83 (settlement is scheduled on the usage chunk, from
live code, onto a task the app owns; `LLMClient` reads the stream to EOF) and
D-84 (the client sends the metering headers the gateway had always read and
nothing had ever sent). Regression tests:
`apps/gateway/tests/test_proxy.py::test_a_client_that_stops_reading_at_done_is_still_settled`,
which abandons the stream the way the real client did, and
`apps/shared/tests/test_llm.py::test_the_stream_is_read_to_the_end_rather_than_abandoned_at_done`.
Every other proxy test drains to EOF, which is why this passed CI for as long as
it did.

### What happened

`ModelProxy.stream` reserved quota, relayed the stream, and settled in a
`finally`:

```python
# apps/gateway/src/dakcoder_gateway/proxy.py
finally:
    if opened:
        await self._settle(reservation, teed, sub, session_id, turn, mode, role, lane, started)
```

`_settle` awaits `quota.reconcile(...)` and then `ledger.record(...)`.

The client — `LLMClient._consume_stream` in `apps/shared/src/dakcoder_shared/llm.py`
— broke out of the loop when it saw `[DONE]`:

```python
if payload == "[DONE]":
    break
```

and then closed the response. Starlette saw the disconnect and cancelled the
response task. The generator's `finally` ran under cancellation, so the first
`await` inside it raised `CancelledError` immediately. Neither the reconcile nor
the ledger write completed.

The docstring stated the opposite intent, which is what made this a defect rather
than a design choice:

> Settlement runs in a `finally`: a client that disconnects mid-stream has still
> spent whatever the model produced, and losing that would make abandoning turns
> the cheapest way to use the service.

A cancelled task cannot await, so the `finally` could not deliver that guarantee.

### Measured

Both calls are identical apart from who reads the stream. Counters taken before
and after each:

| Caller | Redis `hour_tokens` entries | `usage_events` rows |
|---|---|---|
| `LLMClient` (breaks on `[DONE]`) | **+1** — reserve only, no reconcile | **+0** |
| `curl` (reads to EOF) | +2 — reserve *and* reconcile | +1 |

Reproduce:

```bash
. deploy/shellenv.sh
before_z=$(docker exec dakmithra_redis redis-cli -n 3 zcard 'q:{dev:localdev}:hour_tokens')
before_r=$(docker exec dakcoder-postgres psql -tA -U postgres -d dakcoder -c "select count(*) from usage_events;")

env -u DAKCODER_MODEL_API_KEY .venv/bin/python - <<'PY'
import os
from dakcoder_shared.config import local_config
from dakcoder_agent.llm import make_client
cfg = local_config("http://127.0.0.1:8790", os.environ["DAKCODER_JWT"])
with make_client(cfg) as c:
    c.chat([{"role": "user", "content": "hi"}], role="fast", max_tokens=4, enable_thinking=False)
PY

docker exec dakmithra_redis redis-cli -n 3 zcard 'q:{dev:localdev}:hour_tokens'   # now +2
docker exec dakcoder-postgres psql -tA -U postgres -d dakcoder -c "select count(*) from usage_events;"  # now +1
```

Observed in the wild too: a ~25-turn planner run produced 179 events and dozens
of `200 OK` lines in the gateway log, and **zero** rows in `usage_events`.

### What it cost

1. **No usage history for agent work.** The ledger is described as the system of
   record — "what did this team spend on migrations last month" — and it held
   only turns made by something that read the stream to EOF. Every turn the agent
   itself made was missing. LiteLLM's spend tables still saw the traffic, but
   they know nothing about session, mode or task class, which is precisely why
   the ledger exists (§16.6).
2. **Quota was charged at the estimate, permanently.** The reservation was never
   reconciled, so an over-estimate was never refunded and an under-estimate never
   made up. `_estimate`'s fallback is deliberately generous, so in practice users
   were over-charged against every limit — and the calibration loop that is
   supposed to close that gap had no measurements to learn from. Compounded by
   the missing `X-Estimated-Tokens` header (D-84): the generous fallback was in
   force on every call, not only on the ones that forgot.
3. **The `saw_usage` fallback never fired**, so a genuinely broken endpoint that
   stopped emitting usage chunks would have looked identical to normal operation.

### Why the obvious fix was not the fix

`asyncio.shield` around the `await` was the first thing tried and is not enough.
The `finally` is not guaranteed to run at a useful moment at all: a generator
suspended at its last `yield` — which is exactly where one ends up when the
client stops reading after `[DONE]` — is finalised whenever the object is
collected, which can be under cancellation, at loop shutdown, or never.

What ships instead schedules settlement from live code, on the usage chunk, one
frame before `[DONE]`, onto a task created on the app's loop. A task created
there is a sibling of the request rather than a child, so cancelling the request
does not touch it. The `finally` stays as the fallback for streams that end
*without* a usage chunk. `ModelProxy.drain`, awaited in the app's lifespan,
closes the last window: the process stopping while a reconcile is in flight.

---

## Fixed already (context)

These two were fixed on 2026-08-26 because nothing ran without them.

- **`LLMConfig.model_for` returned a model name to local runtimes.** D-59 says
  the client names a role and the gateway names the model; resolving it locally
  sent `model: "Qwen3.8-27B"`, which the proxy reads as a role and refuses. Every
  call from `dakcoderd` failed with `'Qwen3.8-27B' is not a configured role`.
  Local deployments now return the role. `apps/shared/src/dakcoder_shared/config.py`.

- **`LLMClient._error_for` assumed the OpenAI error envelope.** Upstream sends
  `error: {message: ...}`; our own gateway sends `error: "<kind>", reason: "..."`.
  Assuming the first shape turned every gateway refusal — a 429 on quota, a 401
  on an expired token — into `'str' object has no attribute 'get'`, which is how
  the defect above stayed hidden. Both shapes are read now.
  `apps/shared/src/dakcoder_shared/llm.py`.

---

## Not a defect, but worth knowing

- **Three tests cannot pass on Linux.** `test_ordinary_paths_resolve[handler\user.go]`,
  `test_relative_is_always_posix` and `test_paths_are_normalised_before_the_handler_sees_them`
  assert that a backslash is a path separator. On Linux it is a legal filename
  character, so `Workspace.resolve` is right and the tests are Windows-only.
  591 tests, these 3 fail. They should be marked `skipif` rather than left to
  fail on every non-Windows run.

- **The capability probe reports two `info` findings** against LiteLLM +
  vLLM 0.23.0 on this host: tool-call ids do not match the documented
  `chatcmpl-tool-<hex>` shape, and `prompt_tokens_details.cached_tokens` is
  absent so prefix-cache hit rate is not measurable. Both are upstream
  observations the probe exists to surface, not faults in this code.

