---
slug: logging
handle: "@skill:logging"
fetch_when: "adding a log line, or deciding where to report an error"
---

# Logging: where, at what level, and what never goes in

> **Generated.** Do not edit — run `make knowledge` and commit the result.
> Assembled from the reference template, so it cannot drift from what the linter enforces.

Log once, in the layer that has the request, at a level someone can read during an incident, and never log the payload.

Enforced by `repo-no-logging`, `no-sensitive-logging`, `log-level-hygiene`, `error-handling` and `no-fmt-print`.

## Log in the handler, not the repository

The handler knows the route, the request id and the user. The repository knows
none of those, so a line written there is a message with no context attached —
and a repository that both logs and returns an error produces two entries for
one failure.

```go
// repository: return the error, say nothing
func (r *PensionRepository) FetchByID(ctx context.Context, id int64) (domain.Pension, error) {
    return dblib.SelectOne(ctx, r.db, q, pgx.RowToStructByName[domain.Pension])
}

// handler: log once, where the context is
p, err := h.svc.FetchByID(sctx.Ctx, req.ID)
if err != nil {
    log.Error(sctx.Ctx, "fetch pension %d: %v", req.ID, err)
    return nil, err
}
```

The review found 135 log calls inside one service's repository layer, 66 of them
in a single file.

## Levels

`Info` is the stream someone reads at 3am during an incident. Keep it to
events, not to values.

| Level | For |
|---|---|
| `Error` | a request failed; always paired with the error being returned |
| `Warn` | something degraded but the request succeeded |
| `Info` | a business event worth counting |
| `Debug` | anything you needed while writing the code |

Never `fmt.Println`. It has no level, no timestamp, no service name and no
request id, so in a container it is either invisible or it is noise in the
middle of structured JSON.

## Never log the payload

Logging a whole request or response is how personal data reaches the logs
without anyone deciding to put it there. Log identifiers — the record id — not
values.

These must never appear in a log line:

    password  token  authorization  secret  otp
    aadhaar (all spellings: aadhaar, aadhar, adhar)
    pan  mobile  phone  email  dob
    account_number  ifsc  card  cvv  pin  upi_id

The list is configurable per service in `.dakcoder/gotools.yaml` under
`sensitive_fields`, because services carry fields this list has not met.

One reason the spelling note matters: production request structs spell the same
identity number four ways — `AadhaarNumber`, `Aadhaar_Number`,
`AadharNumber`, `ReceiverAdharNo`. Matching one spelling misses three.
