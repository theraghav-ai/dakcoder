---
slug: response-dto
handle: "@skill:response-dto"
fetch_when: "shaping a response, or choosing a status constant"
sources:
  - "skill.md §Response DTO Pattern"
---

# Response DTOs and envelopes

> **Generated.** Do not edit — run `make knowledge` and commit the result.
> Assembled from the reference template, so it cannot drift from what the linter enforces.

A response is a wire type plus a `New*Response` converter, wrapped in an operation envelope that embeds `port.StatusCodeAndMessage`.

`json:",inline"` on the embedded status is the subtle part: without it the embedded struct marshals as a nested object, so `status_code` and `message` end up under a key instead of at the top level, and every client breaks at once.

Status codes and messages come from the predefined `port.*Success` constants, not from literals, so every service in the estate answers identically.

Enforced by `response-dto` and `response-status`.

## Corrections to the source

The document below is reproduced as written. These parts of it are wrong:

- skill.md's list example builds `port.MetaDataResponse{TotalCount: …, Count: …}`. Those fields do not exist — the struct declares `Skip`, `Limit`, `OrderBy`, `SortType`, `TotalRecordsCount` and `ReturnedRecordsCount`. Use `port.NewMetaDataResponse(skip, limit, returned)`, as the shipped `ListUsers` does.
- skill.md prescribes `CreatedAt` and `UpdatedAt` on response types, formatted `"2006-01-02 15:04:05"`. The shipped `UserResponse` omits both. Follow the document: include them (plan.md §6).

## Response DTO Pattern

*From `skill.md` §Response DTO Pattern (lines 1099–1193).*

**Location**: `handler/response/{resource}.go`

**Purpose**: Defines response data transfer objects.

**Pattern**:
```go
package response

import (
    "pisapi/core/domain"
    "pisapi/core/port"
)

// {Resource}Response represents a {resource} in API responses
type {Resource}Response struct {
    ID        int64  `json:"id"`
    Field1    string `json:"field1"`
    Field2    string `json:"field2"`
    Field3    int    `json:"field3"`
    CreatedAt string `json:"created_at"`
    UpdatedAt string `json:"updated_at"`
}

// New{Resource}Response converts domain model to response DTO
func New{Resource}Response(d domain.{Resource}) {Resource}Response {
    return {Resource}Response{
        ID:        d.ID,
        Field1:    d.Field1,
        Field2:    d.Field2,
        Field3:    d.Field3,
        CreatedAt: d.CreatedAt.Format("2006-01-02 15:04:05"),
        UpdatedAt: d.UpdatedAt.Format("2006-01-02 15:04:05"),
    }
}

// New{Resources}Response converts slice of domain models to response DTOs
func New{Resources}Response(data []domain.{Resource}) []{Resource}Response {
    res := make([]{Resource}Response, 0, len(data))
    for _, d := range data {
        res = append(res, New{Resource}Response(d))
    }
    return res
}

// {Resource}CreateResponse represents the response for creating a {resource}
type {Resource}CreateResponse struct {
    port.StatusCodeAndMessage `json:",inline"`
    Data                      {Resource}Response `json:"data"`
}

// {Resource}FetchResponse represents the response for fetching a single {resource}
type {Resource}FetchResponse struct {
    port.StatusCodeAndMessage `json:",inline"`
    Data                      {Resource}Response `json:"data"`
}

// {Resources}ListResponse represents the response for listing {resources}
type {Resources}ListResponse struct {
    port.StatusCodeAndMessage `json:",inline"`
    port.MetaDataResponse     `json:",inline"`
    Data                      []{Resource}Response `json:"data"`
}

// {Resource}UpdateResponse represents the response for updating a {resource}
type {Resource}UpdateResponse struct {
    port.StatusCodeAndMessage `json:",inline"`
    Data                      {Resource}Response `json:"data"`
}

// {Resource}DeleteResponse represents the response for deleting a {resource}
type {Resource}DeleteResponse struct {
    port.StatusCodeAndMessage `json:",inline"`
}
```

**Rules**:
- Create separate response structs for each operation (Create, Fetch, List, Update, Delete)
- Embed `port.StatusCodeAndMessage` for status info
- Embed `port.MetaDataResponse` for list responses (pagination)
- Use `json:",inline"` for embedded structs
- Provide conversion functions: `New{Resource}Response()` and `New{Resources}Response()`
- Format timestamps as strings: `"2006-01-02 15:04:05"`
- Use `snake_case` for JSON field names

**Standard Response Structures**:
- Create: `{StatusCodeAndMessage, Data: {Resource}Response}`
- Fetch: `{StatusCodeAndMessage, Data: {Resource}Response}`
- List: `{StatusCodeAndMessage, MetaDataResponse, Data: []{Resource}Response}`
- Update: `{StatusCodeAndMessage, Data: {Resource}Response}`
- Delete: `{StatusCodeAndMessage}` (no data)

---
