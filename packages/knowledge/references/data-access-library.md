---
slug: data-access-library
handle: "@skill:data-access-library"
fetch_when: "before editing any repository, or when a lint finding names a legacy library"
---

# The data-access library: api-db and n-api-db

> **Generated.** Do not edit — run `make knowledge` and commit the result.
> Assembled from the reference template, so it cannot drift from what the linter enforces.

There are two database libraries and they are the same code. Knowing that is the difference between a one-line change and a migration nobody schedules.

Enforced by `legacy-lib-generation`; reported by `gotools lib-version-check`.

## Two libraries, one codebase

| | |
|---|---|
| **Old** | `gitlab.cept.gov.in/it-2.0-common/api-db` |
| **New** | `gitlab.cept.gov.in/it-2.0-common/n-api-db` |

Compared module-to-module in the module cache, these are **identical**: the same
file list, the same exported API — all ~120 functions, same names, same
signatures. The only difference is the `module` line in `go.mod`.

`n-api-db` is a re-publish of `api-db` under a new path, not a redesign.

**Migrating is a one-line import change with zero call-site edits.** Nothing
below the import needs to be touched.

## You cannot tell them apart at the call site

Both are imported under the same alias:

```go
dblib "gitlab.cept.gov.in/it-2.0-common/api-db"      // old
dblib "gitlab.cept.gov.in/it-2.0-common/n-api-db"    // new
```

Every `dblib.Psql`, `dblib.SelectRows` and `dblib.QueueExecRow` below that line is
byte-identical either way. **The import line is the only evidence.** When you
need to know which generation a file is on, read the import — never infer it
from the calls.

## The `n-` prefix is not universal

The new generation covers five libraries, and only these five:

| Legacy | Current |
|---|---|
| `api-server` | `n-api-server` |
| `api-db` | `n-api-db` |
| `api-log` | `n-api-log` |
| `api-bootstrapper` | `n-api-bootstrapper` |
| `api-validation` | `n-api-validation` |

`n-api-config` and `n-api-trace` **do not exist**. So `api-config` is the current
config library, not a migration target — do not "modernise" it.

## Two traps in the library

**`dblib.Tx` takes a `*gin.Context`.** It is the obvious-looking transaction
helper and it is unusable here: the template has moved off gin, so calling it
means taking a direct gin dependency in the repository layer, which
`layer-sql-boundary` exists to prevent. Use `dblib.TxExec(ctx, tx, builder)`,
which is context-first.

**`dblib.TimedBatch` is not adopted.** `NewTimedBatch` and the `TimedQueue*`
family exist in both libraries and are used by nothing in the template or in any
reviewed service. Use the raw `pgx.Batch` shape in `@skill:db-performance`.
