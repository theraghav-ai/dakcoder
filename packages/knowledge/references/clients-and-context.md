---
slug: clients-and-context
handle: "@skill:clients-and-context"
fetch_when: "adding an outbound call, a new client, or anything that takes a context"
---

# Clients, context and deadlines

> **Generated.** Do not edit — run `make knowledge` and commit the result.
> Assembled from the reference template, so it cannot drift from what the linter enforces.

Three review findings that are one defect wearing three hats: clients built per request, dependencies constructed instead of injected, and contexts that do not descend from the request.

Enforced by `client-singleton`, `ctx-propagation`, `external-call-timeout` and `repo-contract`.

## Build clients once, inject them

A client constructed inside a handler is rebuilt on every request, and so is its
connection pool. Nothing is reused, keep-alive never helps, and the pool limits
that exist to protect the upstream service become per-request rather than
per-process.

```go
// WRONG — a new client, and a new pool, per request
func (h *PensionHandler) Fetch(sctx *serverRoute.Context, req FetchRequest) (*resp.R, error) {
    client := resty.New().SetTimeout(15 * time.Second)
    ...
}
```

Provide it once in `bootstrap/` and inject the pointer:

```go
// bootstrap/clients.go
func NewRestyClient(cfg *config.Config) *resty.Client {
    return resty.New().SetTimeout(cfg.GetDuration("http.timeout"))
}

// handler
type PensionHandler struct {
    *serverHandler.Base
    svc  *repo.PensionRepository
    http *resty.Client        // injected
}
```

The same applies to MinIO, Kafka, Redis and gRPC clients. If it holds a
connection pool, it is built once.

## Propagate the context you were given

`context.Background()` in a handler or repository discards the client's
cancellation, the request deadline and the trace id. When the caller hangs up,
the work carries on and nothing links the log lines back to the request.

```go
// WRONG — passes the rule that requires a timeout, and defeats its purpose
ctx, cancel := context.WithTimeout(context.Background(), cfg.GetDuration("db.QueryTimeoutMed"))

// RIGHT
ctx, cancel := context.WithTimeout(ctx, cfg.GetDuration("db.QueryTimeoutMed"))
defer cancel()
```

The wrong form is not hypothetical: it appears 11 times in the legacy corpus,
once with a comment explaining that it was added deliberately to work around an
already-cancelled parent. **If a parent context is cancelled too early, fix the
parent.** Detaching hides the problem and loses the trace.

`context.Background()` is correct in exactly two places: `main.go` and
`bootstrap/`, which own the process lifetime rather than a request.

Name it `ctx`. Not `gctx`, and never `*context.Context` — Context is an
interface, so a pointer to one adds a nil check and buys nothing.

## Bound everything that leaves the process

Every database call already needs a deadline from config, which `repo-contract`
enforces. Outbound HTTP needs one too: an unbounded call to a slow upstream is
how one dependency's bad afternoon becomes an exhausted worker pool here.

Set it on the injected client — `resty.New().SetTimeout(...)` — or pass a
context that already carries a deadline. Take the value from config, so it can
be tuned per environment without a rebuild.
