# Open defects

Found while bringing the server side up on VMAIPROD1 (2026-08-26). Each entry
says what is wrong, how it was measured, and what it costs. Nothing here is
fixed; the two defects that *were* fixed to make the stack run at all are
recorded at the bottom for context.

---

## D-1 · Settlement is skipped on every agent turn

**Severity: high.** The usage ledger has no record of real agent work, and quota
is never reconciled.

### What happens

`ModelProxy.stream` reserves quota, relays the stream, and settles in a
`finally`:

```python
# apps/gateway/src/dakcoder_gateway/proxy.py
finally:
    if opened:
        await self._settle(reservation, teed, sub, session_id, turn, mode, role, lane, started)
```

`_settle` awaits `quota.reconcile(...)` and then `ledger.record(...)`.

The client — `LLMClient._consume_stream` in `apps/shared/src/dakcoder_shared/llm.py`
— breaks out of the loop when it sees `[DONE]`:

```python
if payload == "[DONE]":
    break
```

and then closes the response. Starlette sees the disconnect and cancels the
response task. The generator's `finally` runs under cancellation, so the first
`await` inside it raises `CancelledError` immediately. Neither the reconcile nor
the ledger write completes.

The docstring states the opposite intent, which is what makes this a defect
rather than a design choice:

> Settlement runs in a `finally`: a client that disconnects mid-stream has still
> spent whatever the model produced, and losing that would make abandoning turns
> the cheapest way to use the service.

A cancelled task cannot await, so the `finally` cannot deliver that guarantee.

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

docker exec dakmithra_redis redis-cli -n 3 zcard 'q:{dev:localdev}:hour_tokens'   # +1
docker exec dakcoder-postgres psql -tA -U postgres -d dakcoder -c "select count(*) from usage_events;"  # unchanged
```

Observed in the wild too: a ~25-turn planner run produced 179 events and dozens
of `200 OK` lines in the gateway log, and **zero** rows in `usage_events`.

### What it costs

1. **No usage history for agent work.** The ledger is described as the system of
   record — "what did this team spend on migrations last month" — and it holds
   only turns made by something that reads the stream to EOF. Every turn the
   agent itself makes is missing. LiteLLM's spend tables still see the traffic,
   but they know nothing about session, mode or task class, which is precisely
   why the ledger exists (§16.6).
2. **Quota is charged at the estimate, permanently.** The reservation is never
   reconciled, so an over-estimate is never refunded and an under-estimate is
   never made up. `_estimate`'s fallback is deliberately generous, so in practice
   users are over-charged against every limit — and the calibration loop that is
   supposed to close that gap has no measurements to learn from.
3. **The `saw_usage` fallback never fires**, so a genuinely broken endpoint that
   stops emitting usage chunks would look identical to normal operation.

### Suggested direction

Settlement must outlive the request. Detaching it from the cancelled task —
`asyncio.shield`, or handing the settlement to a task the app owns and awaits at
shutdown — is the shape of the fix. Both change what happens when the process
stops mid-settlement, so it needs a decision about durability, not just a
one-line edit: a shielded task can still be lost on shutdown, which argues for
recording *before* the reconcile, or for a small outbox.

Worth adding whichever way it goes: a test where the client closes the response
after `[DONE]` and the ledger row is still asserted. Every existing proxy test
drains the stream, which is why this passed CI.

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
