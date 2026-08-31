---
slug: repository-pattern
handle: "@skill:repository-pattern"
fetch_when: "writing a query, or anything that touches the database"
sources:
  - "skill.md §Repository Pattern"
---

# Repository pattern

> **Generated.** Do not edit — run `make knowledge` and commit the result.
> Assembled from the reference template, so it cannot drift from what the linter enforces.

Every repository method builds its query with `dblib.Psql`, takes its deadline from config, and maps rows by name.

`dblib.Psql` is a Squirrel builder pre-configured with `$N` placeholders. A hand-rolled `sq.Insert(...)` defaults to `?` placeholders, which Postgres rejects at runtime rather than at compile time — so the mistake reaches production looking like a driver problem.

Enforced by `repo-contract`, `repo-rowmapper` and `repo-norows`.

## Corrections to the source

The document below is reproduced as written. These parts of it are wrong:

- skill.md's worked example writes `sq.Insert(...).PlaceholderFormat(sq.Dollar)`. That is the older idiom. Use `dblib.Psql.Insert(...)`, which is that builder already configured — the shipped `user` repository does, and `repo-contract` requires it (plan.md §6).
- The shipped `user` repository builds its create and update results from the arguments it was handed rather than from the row it wrote, so `POST` answers with `"id": 0` and a partial `PUT` dereferences a nil pointer and panics. Use `dblib.InsertReturning` / `dblib.UpdateReturning` with a `RETURNING` clause, as `resource_scaffold` does.

## Repository Pattern

*From `skill.md` §Repository Pattern (lines 691–849).*

**Location**: `repo/postgres/{resource}.go`

**Purpose**: Handles all database operations for the resource.

**Pattern**:
```go
package repo

import (
    "context"
    "time"

    sq "github.com/Masterminds/squirrel"
    "github.com/jackc/pgx/v5"
    config "gitlab.cept.gov.in/it-2.0-common/api-config"
    dblib "gitlab.cept.gov.in/it-2.0-common/n-api-db"
    "pisapi/core/domain"
)

type {Resource}Repository struct {
    db  *dblib.DB
    cfg *config.Config
}

func New{Resource}Repository(db *dblib.DB, cfg *config.Config) *{Resource}Repository {
    return &{Resource}Repository{
        db:  db,
        cfg: cfg,
    }
}

const {resource}Table = "{resources}"

// Create inserts a new {resource}
func (r *{Resource}Repository) Create(ctx context.Context, data domain.{Resource}) (domain.{Resource}, error) {
    ctx, cancel := context.WithTimeout(ctx, r.cfg.GetDuration("db.QueryTimeoutLow"))
    defer cancel()

    query := sq.Insert({resource}Table).
        Columns("field1", "field2", "field3").
        Values(data.Field1, data.Field2, data.Field3).
        Suffix("RETURNING id, field1, field2, field3, created_at, updated_at").
        PlaceholderFormat(sq.Dollar)

    var result domain.{Resource}
    err := dblib.Insert(ctx, r.db, query, &result)
    return result, err
}

// FindByID retrieves a {resource} by ID
func (r *{Resource}Repository) FindByID(ctx context.Context, id int64) (domain.{Resource}, error) {
    ctx, cancel := context.WithTimeout(ctx, r.cfg.GetDuration("db.QueryTimeoutLow"))
    defer cancel()

    query := sq.Select("id", "field1", "field2", "field3", "created_at", "updated_at").
        From({resource}Table).
        Where(sq.Eq{"id": id}).
        PlaceholderFormat(sq.Dollar)

    var result domain.{Resource}
    err := dblib.SelectOne(ctx, r.db, query, &result)
    if err != nil {
        if err == pgx.ErrNoRows {
            return result, err
        }
        return result, err
    }
    return result, nil
}

// List retrieves all {resources} with pagination
func (r *{Resource}Repository) List(ctx context.Context, skip, limit int64, orderBy, sortType string) ([]domain.{Resource}, int64, error) {
    ctx, cancel := context.WithTimeout(ctx, r.cfg.GetDuration("db.QueryTimeoutMed"))
    defer cancel()

    // Count query
    countQuery := sq.Select("COUNT(*)").
        From({resource}Table).
        PlaceholderFormat(sq.Dollar)

    var totalCount int64
    err := dblib.SelectOne(ctx, r.db, countQuery, &totalCount)
    if err != nil {
        return nil, 0, err
    }

    // Data query
    query := sq.Select("id", "field1", "field2", "field3", "created_at", "updated_at").
        From({resource}Table).
        OrderBy(orderBy + " " + sortType).
        Limit(uint64(limit)).
        Offset(uint64(skip)).
        PlaceholderFormat(sq.Dollar)

    var results []domain.{Resource}
    err = dblib.SelectRows(ctx, r.db, query, &results)
    if err != nil {
        return nil, 0, err
    }

    return results, totalCount, nil
}

// Update updates a {resource} by ID
func (r *{Resource}Repository) Update(ctx context.Context, id int64, field1, field2 *string, field3 *int) (domain.{Resource}, error) {
    ctx, cancel := context.WithTimeout(ctx, r.cfg.GetDuration("db.QueryTimeoutLow"))
    defer cancel()

    query := sq.Update({resource}Table).
        Set("updated_at", time.Now()).
        Where(sq.Eq{"id": id}).
        PlaceholderFormat(sq.Dollar)

    // Only update non-nil fields
    if field1 != nil {
        query = query.Set("field1", *field1)
    }
    if field2 != nil {
        query = query.Set("field2", *field2)
    }
    if field3 != nil {
        query = query.Set("field3", *field3)
    }

    query = query.Suffix("RETURNING id, field1, field2, field3, created_at, updated_at")

    var result domain.{Resource}
    err := dblib.Update(ctx, r.db, query, &result)
    return result, err
}

// Delete deletes a {resource} by ID
func (r *{Resource}Repository) Delete(ctx context.Context, id int64) error {
    ctx, cancel := context.WithTimeout(ctx, r.cfg.GetDuration("db.QueryTimeoutLow"))
    defer cancel()

    query := sq.Delete({resource}Table).
        Where(sq.Eq{"id": id}).
        PlaceholderFormat(sq.Dollar)

    return dblib.Delete(ctx, r.db, query)
}
```

**Rules**:
- Always inject `*dblib.DB` and `*config.Config`
- Use context with timeout for all queries
- Use Squirrel query builder (alias `sq`)
- Always use `.PlaceholderFormat(sq.Dollar)` for PostgreSQL
- Use `dblib.Insert()`, `dblib.SelectOne()`, `dblib.SelectRows()`, `dblib.Update()`, `dblib.Delete()`
- Handle `pgx.ErrNoRows` for not found errors
- For updates: use pointers for optional fields, only update non-nil fields
- Always set `updated_at` in update queries
- Return domain models, not DTOs

---
