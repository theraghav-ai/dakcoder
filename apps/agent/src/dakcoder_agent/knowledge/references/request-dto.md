---
slug: request-dto
handle: "@skill:request-dto"
fetch_when: "adding or changing a request payload, a path parameter, or a query filter"
sources:
  - "skill.md §Request DTO Pattern"
  - "SOP.md §Validation"
---

# Request DTOs and validation

> **Generated.** Do not edit — run `make knowledge` and commit the result.
> Assembled from the reference template, so it cannot drift from what the linter enforces.

Every request struct lives in `handler/request.go`, and validation comes from `validate` tags plus generated code.

The location is not bureaucracy. `govalid` is run as `govalid ./request.go` from the handler directory, so a request struct in any other file never gets a generated validator — and the failure is silent: input reaches the handler unvalidated.

Running `govalid` is not optional either. The framework returns **422** for any non-GET route whose request DTO has no generated `Validate()` method, so a fresh resource fails before it reaches your code until the validators exist.

A tag must also *bound* the field, not merely require it. `validate:"required"` on a free-text string means "not empty" and nothing else, so a 10MB string is valid input. Bounds were the most systemic finding in the review of 41 services: numeric ranges asked for in 39 of them, string constraints in 34.

Enforced by `request-dto`, `request-validate-depth` and `validator-generated`; listed field by field by `gotools validation-audit`.

## Corrections to the source

The document below is reproduced as written. These parts of it are wrong:

- skill.md's worked example gives request DTOs a `ToDomain()` converter. No such method exists anywhere in the template; handlers pass fields positionally to the repository (plan.md §6). Do not add one.
- skill.md teaches only `required` and `omitempty`, which is why so much production code carries nothing else. The vocabulary you actually need:

| Field | Tag | Why |
|---|---|---|
| free text | `required,max=255` | an unbounded string is an unbounded row |
| code or ref | `required,len=13` | fixed-width identifiers |
| enum | `required,oneof=pending approved rejected` | rejects unknown states at the edge |
| email | `required,email` | |
| number | `required,min=1,max=9999` | bounds reach the database as intent |
| optional number | `omitempty,min=0` | |
| date string | `required,datetime=2006-01-02` | |
| slice | `required,max=100,dive` | caps the request body; `dive` validates elements |

A field that is genuinely unconstrained is fine — `omitempty` alone is an acceptable floor, and says the absence was deliberate.

## Request DTO Pattern

*From `skill.md` §Request DTO Pattern (lines 1032–1098).*

**Location**: `handler/request.go`

**Purpose**: Defines request data transfer objects with validation.

**Pattern**:
```go
package handler

import "pisapi/core/domain"

// Create{Resource}Request represents the request body for creating a {resource}
type Create{Resource}Request struct {
    Field1 string `json:"field1" validate:"required"`
    Field2 string `json:"field2" validate:"required"`
    Field3 int    `json:"field3" validate:"required"`
}

func (r Create{Resource}Request) ToDomain() domain.{Resource} {
    return domain.{Resource}{
        Field1: r.Field1,
        Field2: r.Field2,
        Field3: r.Field3,
    }
}

// Update{Resource}Request represents the request body for updating a {resource}
type Update{Resource}Request struct {
    ID     int64  `uri:"id" validate:"required"`
    Field1 string `json:"field1" validate:"omitempty"`
    Field2 string `json:"field2" validate:"omitempty"`
    Field3 int    `json:"field3" validate:"omitempty"`
}

// {Resource}IDUri represents the URI parameter for {resource} ID
type {Resource}IDUri struct {
    ID int64 `uri:"id" validate:"required"`
}

// List{Resources}Params represents query parameters for listing {resources}
type List{Resources}Params struct {
    port.MetadataRequest
}
```

**Rules**:
- Add all request structs to `handler/request.go`
- Use `validate:"required"` for mandatory fields
- Use `validate:"omitempty"` for optional fields (updates)
- Use `uri:` tag for URL parameters
- Use `json:` tag for JSON body fields
- Use `form:` tag for form data
- Embed `port.MetadataRequest` for list endpoints (provides Skip, Limit, OrderBy, SortType)
- Include `ToDomain()` method for create requests
- Use `snake_case` for JSON field names

**Validation Tags**:
- `required` - Field must not be empty
- `omitempty` - Field is optional
- `email` - Must be valid email format
- `min=N` - Minimum value/length
- `max=N` - Maximum value/length
- `oneof=val1 val2` - Must be one of specified values

---

## Validation

*From `SOP.md` §Validation (lines 223–232).*

1. For request validation use the `validate` tags in the request struct fields.
2. Install govalid latest version `go install gitlab.cept.gov.in/it-2.0-common/n-api-validation/cmd/govalid@latest`.
3. Place all the request structs in a separate file named `request.go` in the handler package.
4. Run the command `govalid ./request.go` to generate the validation code.
5. This will generate a file in the same package.
2. The validation will be automatically handled before calling the handler function.
3. If the validation fails, a 400 Bad Request error will be returned with the validation error message.
