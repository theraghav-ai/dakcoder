---
slug: db-performance
handle: "@skill:db-performance"
fetch_when: "writing a repository method that touches the database more than once"
---

# Database round trips: batching, transactions and N+1

> **Generated.** Do not edit — run `make knowledge` and commit the result.
> Assembled from the reference template, so it cannot drift from what the linter enforces.

Database round trips were the largest single category in the review of 41 services — 748 findings across 33 of them.

Enforced by `repo-batch-in-loop`, `repo-multi-roundtrip`, `repo-transaction-scope`, `repo-raw-rows`, `repo-select-star` and `repo-sql-now`; profiled by `gotools db-roundtrip-audit`.

## The batch shape

This is the endorsed form. Use it exactly.

```go
batch := &pgx.Batch{}

updateBuilder := dblib.Psql.Update("pao.pfms_main").Set("closing_bal", req.ClosingBal).Where(...)
if err := dblib.QueueExecRow(batch, updateBuilder); err != nil {
    return err
}

for _, t := range request {
    insertBuilder := dblib.Psql.Insert("pao.pfms_detail").Columns(...).Values(...)
    if err := dblib.QueueExecRow(batch, insertBuilder); err != nil {
        return err
    }
}

results := r.db.SendBatch(ctx, batch)
if results != nil {
    defer results.Close()
    for i := 0; i < batch.Len(); i++ {
        if _, err := results.Exec(); err != nil {
            return err
        }
    }
}
```

**Drain the results; do not shortcut with `SendBatch(ctx, batch).Close()`.** The
short form reports that *something* failed. The drain loop reports *which
statement* failed. That difference is the whole reason this shape was chosen.

For reads, `dblib.QueueReturnBulk(batch, q, pgx.RowToStructByName[domain.X], &dst)`
is the equivalent, and it keeps the by-name mapping `repo-rowmapper` requires.

## Batch is not a transaction

They solve different problems and are not substitutes.

| | Batch | Transaction |
|---|---|---|
| Buys you | fewer round trips | all-or-nothing |
| Use when | the statements are independent | they must commit or roll back together |

They compose: where both properties are wanted, queue the batch **inside** a
transaction. A batch on its own is not an atomicity guarantee.

Two corollaries:

- A transaction around a **single** statement buys nothing. One statement is
  already atomic in Postgres.
- "Use batch instead of transaction" — which the review says bluntly, 100 times
  — means *where atomicity was not required in the first place*. Do not remove a
  transaction that is holding several writes together.

## N+1: the one that gates

A query inside a loop costs one round trip per element, so the request's cost is
the size of its input:

```go
for _, id := range ids {
    row, err := dblib.SelectOne(ctx, r.db, q(id), m)   // N round trips
}
```

Queue them into a batch, or rewrite as a single query with `WHERE id = ANY($1)`.
This is the only rule in the family that blocks: the others cost latency, this
one costs availability.

## Thresholds

| Calls in one method | What happens |
|---|---|
| 1 | nothing |
| 2 | noted — a batch may be possible |
| 3 or more | batching recommended **where feasible** |

Never mandatory. When the second query needs the first one's result there is
nothing to batch, and that is a perfectly good answer.

## Also in this family

- **Name your columns.** No `SELECT *`: a new column silently changes every
  result. `dblib.GenerateColumnsFromStruct(domain.X{})` keeps the list and the
  struct in step.
- **Never scan by hand.** No `rows.Next()`, no `QueryRow(...).Scan(...)`. Both
  bind by position, so adding a column shifts every field after it with no error
  raised. Use `dblib.SelectRows` with `pgx.RowToStructByName`.
- **Timestamps come from Go.** `time.Now()` once at the top of the function, not
  `NOW()` in the SQL — the database clock and the request clock differ, and
  inside a batch `NOW()` returns the transaction start time for every statement.
- **No stored procedures.** Invisible to the compiler, to review and to git.
