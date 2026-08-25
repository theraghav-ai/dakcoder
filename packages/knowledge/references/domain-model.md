---
slug: domain-model
handle: "@skill:domain-model"
fetch_when: "adding a field, a table, or a new domain type"
sources:
  - "skill.md §Domain Model Pattern"
  - "skill.md §Database Schema"
  - "skill.md §Naming Conventions"
---

# Domain model and database schema

> **Generated.** Do not edit — run `make knowledge` and commit the result.
> Assembled from the reference template, so it cannot drift from what the linter enforces.

A domain model is plain Go: `ID`, the resource's own fields, `CreatedAt` and `UpdatedAt`, each with a `json` and a `db` tag in snake_case.

The `db` tag is load-bearing rather than decorative — `pgx.RowToStructByName` matches result columns to fields through it, so a missing or misspelt tag is a silent scan failure at runtime, not a compile error.

Enforced by `domain-tags` and `layer-dto-boundary`.

## Domain Model Pattern

*From `skill.md` §Domain Model Pattern (lines 642–690).*

**Location**: `core/domain/{resource}.go`

**Purpose**: Represents the business entity with database mapping.

**Pattern**:
```go
package domain

import "time"

type {Resource} struct {
    ID        int64     `json:"id" db:"id"`
    Field1    string    `json:"field1" db:"field1"`
    Field2    string    `json:"field2" db:"field2"`
    Field3    int       `json:"field3" db:"field3"`
    CreatedAt time.Time `json:"created_at" db:"created_at"`
    UpdatedAt time.Time `json:"updated_at" db:"updated_at"`
}
```

**Rules**:
- Use `snake_case` for JSON and DB tags
- Always include `ID`, `CreatedAt`, `UpdatedAt`
- Match DB column names exactly in `db:` tags
- Export all fields (capitalize first letter)
- Use appropriate Go types (int64 for IDs, time.Time for timestamps)

**Example**:
```go
package domain

import "time"

type Product struct {
    ID          int64     `json:"id" db:"id"`
    Name        string    `json:"name" db:"name"`
    Description string    `json:"description" db:"description"`
    Price       float64   `json:"price" db:"price"`
    Stock       int       `json:"stock" db:"stock"`
    CategoryID  int64     `json:"category_id" db:"category_id"`
    CreatedAt   time.Time `json:"created_at" db:"created_at"`
    UpdatedAt   time.Time `json:"updated_at" db:"updated_at"`
}
```

---

## Database Schema

*From `skill.md` §Database Schema (lines 1273–1310).*

**Location**: `db/{resource}.sql`

**Pattern**:
```sql
CREATE TABLE IF NOT EXISTS {resources} (
    id SERIAL PRIMARY KEY,
    field1 VARCHAR(255) NOT NULL,
    field2 VARCHAR(255) NOT NULL,
    field3 INTEGER NOT NULL,
    created_at TIMESTAMP DEFAULT NOW(),
    updated_at TIMESTAMP DEFAULT NOW()
);

-- Add indexes for frequently queried fields
CREATE INDEX IF NOT EXISTS idx_{resources}_field1 ON {resources}(field1);

-- Add unique constraints if needed
ALTER TABLE {resources} ADD CONSTRAINT unique_{resources}_field1 UNIQUE (field1);
```

**Rules**:
- Use `SERIAL` for auto-incrementing IDs
- Always include `created_at` and `updated_at` with `DEFAULT NOW()`
- Use appropriate data types:
  - `VARCHAR(N)` for strings
  - `INTEGER` for whole numbers
  - `DECIMAL(P,S)` for money/decimals
  - `TIMESTAMP` for dates/times
  - `BOOLEAN` for true/false
  - `TEXT` for large text
- Add indexes for foreign keys and frequently queried fields
- Add unique constraints where applicable
- Use `IF NOT EXISTS` to make migrations idempotent

---

## Naming Conventions

*From `skill.md` §Naming Conventions (lines 1311–1344).*

#### Package Names
- `domain` - Business entities
- `handler` - HTTP handlers
- `response` - Response DTOs (subpackage of handler)
- `repo` - Repository interfaces
- `postgres` - PostgreSQL implementations (subpackage of repo)

#### Type Names
- Domain: `{Resource}` (e.g., `User`, `Product`)
- Repository: `{Resource}Repository` (e.g., `UserRepository`)
- Handler: `{Resource}Handler` (e.g., `UserHandler`)
- Request: `Create{Resource}Request`, `Update{Resource}Request`, `{Resource}IDUri`, `List{Resources}Params`
- Response: `{Resource}Response`, `{Resource}CreateResponse`, `{Resources}ListResponse`

#### Function Names
- Constructor: `New{Resource}Repository`, `New{Resource}Handler`
- Handler methods: `Create{Resource}`, `List{Resources}`, `Get{Resource}ByID`, `Update{Resource}ByID`, `Delete{Resource}ByID`
- Repository methods: `Create`, `FindByID`, `List`, `Update`, `Delete`
- Response converter: `New{Resource}Response`, `New{Resources}Response`

#### Field Names
- Go: `PascalCase` (e.g., `FirstName`)
- JSON: `snake_case` (e.g., `first_name`)
- Database: `snake_case` (e.g., `first_name`)
- URL params: `snake_case` (e.g., `:id`, `:user_id`)

#### Route Names
- Paths: `/{resources}` (plural, lowercase)
- Route names: `"Create {Resource}"`, `"List {Resources}"` (for Swagger)

---
