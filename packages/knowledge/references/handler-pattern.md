---
slug: handler-pattern
handle: "@skill:handler-pattern"
fetch_when: "writing or changing a handler, a route, or a handler constructor"
sources:
  - "skill.md §Handler Pattern"
  - "skill.md §Routing Pattern"
---

# Handler pattern

> **Generated.** Do not edit — run `make knowledge` and commit the result.
> Assembled from the reference template, so it cannot drift from what the linter enforces.

A handler is a struct embedding `*serverHandler.Base`, a constructor that builds that base with the prefix chain, a `Routes()` method, and one method per route on the DTO signature.

Two things fail silently if you get them wrong. Omitting the `*serverHandler.Base` embed means the type does not satisfy `serverHandler.Handler`, which surfaces at start-up as an Uber-FX graph error naming a type rather than a file. Omitting `.Name(...)` on a route means the route serves correctly and is missing from the generated OpenAPI document, with no error at all.

Enforced by `handler-signature`, `handler-base` and `routes-in-handler`.

## Corrections to the source

The document below is reproduced as written. These parts of it are wrong:

- skill.md's worked example imports `github.com/jackc/pgx/v5` into the handler to special-case `pgx.ErrNoRows`. Do not: that crosses the layer boundary `layer-sql-boundary` enforces, and the shipped `user` handler does not do it. Return the error and let the framework map it.

## Handler Pattern

*From `skill.md` §Handler Pattern (lines 850–1031).*

**Location**: `handler/{resource}.go`

**Purpose**: Defines HTTP routes and handles HTTP requests.

**Pattern**:
```go
package handler

import (
    "github.com/jackc/pgx/v5"
    log "gitlab.cept.gov.in/it-2.0-common/n-api-log"
    serverHandler "gitlab.cept.gov.in/it-2.0-common/n-api-server/handler"
    serverRoute "gitlab.cept.gov.in/it-2.0-common/n-api-server/route"
    "pisapi/core/port"
    resp "pisapi/handler/response"
    repo "pisapi/repo/postgres"
)

type {Resource}Handler struct {
    *serverHandler.Base
    svc *repo.{Resource}Repository
}

func New{Resource}Handler(svc *repo.{Resource}Repository) *{Resource}Handler {
    base := serverHandler.New("{Resources}").
        SetPrefix("/v1").
        AddPrefix("")
    return &{Resource}Handler{
        Base: base,
        svc:  svc,
    }
}

// Routes defines all routes for this handler
func (h *{Resource}Handler) Routes() []serverRoute.Route {
    return []serverRoute.Route{
        serverRoute.POST("/{resources}", h.Create{Resource}).Name("Create {Resource}"),
        serverRoute.GET("/{resources}", h.List{Resources}).Name("List {Resources}"),
        serverRoute.GET("/{resources}/:id", h.Get{Resource}ByID).Name("Get {Resource} By ID"),
        serverRoute.PUT("/{resources}/:id", h.Update{Resource}ByID).Name("Update {Resource} By ID"),
        serverRoute.DELETE("/{resources}/:id", h.Delete{Resource}ByID).Name("Delete {Resource} By ID"),
    }
}

// Create{Resource} creates a new {resource}
func (h *{Resource}Handler) Create{Resource}(sctx *serverRoute.Context, req Create{Resource}Request) (*resp.{Resource}CreateResponse, error) {
    // Convert request to domain model
    data := req.ToDomain()

    // Call repository
    result, err := h.svc.Create(sctx.Ctx, data)
    if err != nil {
        log.Error(sctx.Ctx, "Error creating {resource}: %v", err)
        return nil, err
    }

    log.Info(sctx.Ctx, "{Resource} created with ID: %d", result.ID)
    // Convert to response
    r := &resp.{Resource}CreateResponse{
        StatusCodeAndMessage: port.CreateSuccess,
        Data:                 resp.New{Resource}Response(result),
    }
    return r, nil
}

// List{Resources} retrieves all {resources}
func (h *{Resource}Handler) List{Resources}(sctx *serverRoute.Context, req List{Resources}Params) (*resp.{Resources}ListResponse, error) {
    // Call repository
    results, totalCount, err := h.svc.List(sctx.Ctx, req.Skip, req.Limit, req.OrderBy, req.SortType)
    if err != nil {
        log.Error(sctx.Ctx, "Error fetching {resources}: %v", err)
        return nil, err
    }

    // Convert to response
    r := &resp.{Resources}ListResponse{
        StatusCodeAndMessage: port.ListSuccess,
        MetaDataResponse: port.MetaDataResponse{
            TotalCount: totalCount,
            Count:      int64(len(results)),
            Skip:       req.Skip,
            Limit:      req.Limit,
        },
        Data: resp.New{Resources}Response(results),
    }
    return r, nil
}

// Get{Resource}ByID retrieves a {resource} by ID
func (h *{Resource}Handler) Get{Resource}ByID(sctx *serverRoute.Context, req {Resource}IDUri) (*resp.{Resource}FetchResponse, error) {
    // Call repository
    result, err := h.svc.FindByID(sctx.Ctx, req.ID)
    if err != nil {
        if err == pgx.ErrNoRows {
            log.Error(sctx.Ctx, "{Resource} not found with ID: %d", req.ID)
            return nil, err
        }
        log.Error(sctx.Ctx, "Error fetching {resource} by ID: %v", err)
        return nil, err
    }

    // Convert to response
    r := &resp.{Resource}FetchResponse{
        StatusCodeAndMessage: port.FetchSuccess,
        Data:                 resp.New{Resource}Response(result),
    }
    return r, nil
}

// Update{Resource}ByID updates a {resource} by ID
func (h *{Resource}Handler) Update{Resource}ByID(sctx *serverRoute.Context, req Update{Resource}Request) (*resp.{Resource}UpdateResponse, error) {
    // Convert non-empty fields to pointers
    var field1, field2 *string
    var field3 *int

    if req.Field1 != "" {
        field1 = &req.Field1
    }
    if req.Field2 != "" {
        field2 = &req.Field2
    }
    if req.Field3 != 0 {
        field3 = &req.Field3
    }

    // Call repository
    result, err := h.svc.Update(sctx.Ctx, req.ID, field1, field2, field3)
    if err != nil {
        log.Error(sctx.Ctx, "Error updating {resource} by ID: %v", err)
        return nil, err
    }

    // Convert to response
    r := &resp.{Resource}UpdateResponse{
        StatusCodeAndMessage: port.UpdateSuccess,
        Data:                 resp.New{Resource}Response(result),
    }
    return r, nil
}

// Delete{Resource}ByID deletes a {resource} by ID
func (h *{Resource}Handler) Delete{Resource}ByID(sctx *serverRoute.Context, req {Resource}IDUri) (*resp.{Resource}DeleteResponse, error) {
    // Call repository
    err := h.svc.Delete(sctx.Ctx, req.ID)
    if err != nil {
        if err == pgx.ErrNoRows {
            log.Error(sctx.Ctx, "{Resource} not found with ID: %d", req.ID)
            return nil, err
        }
        log.Error(sctx.Ctx, "Error deleting {resource} by ID: %v", err)
        return nil, err
    }

    // Return success response
    r := &resp.{Resource}DeleteResponse{
        StatusCodeAndMessage: port.DeleteSuccess,
    }
    return r, nil
}
```

**Rules**:
- Embed `*serverHandler.Base`
- Inject repository as `svc` with correct type (e.g., `*repo.UserRepository`)
- Import repository package as `repo "pisapi/repo/postgres"`
- Import log package as `log "gitlab.cept.gov.in/it-2.0-common/n-api-log"`
- Use `serverHandler.New()` with resource name (plural, capitalized)
- Set prefix to `/v1` for API versioning
- Handler signature: `(sctx *serverRoute.Context, req RequestType) (*ResponseType, error)`
- Always log errors before returning using `log.Error(sctx.Ctx, "message: %v", err)`
- Use `log.Info(sctx.Ctx, "message: %v", value)` for info logging
- Logging format: `log.Error(sctx.Ctx, "Error description: %v", err)` with printf-style formatting
- Check for `pgx.ErrNoRows` for 404 errors in repository errors
- For updates: no need to check existence first, handle error from Update
- For deletes: no need to check existence first, handle error from Delete (returns pgx.ErrNoRows if not found)
- Use `sctx.Ctx` for context parameter
- Always create response in intermediate variable `r`, then return `r, nil` (not inline return)

---

## Routing Pattern

*From `skill.md` §Routing Pattern (lines 1194–1226).*

**Routes Definition**:
```go
func (h *{Resource}Handler) Routes() []serverRoute.Route {
    return []serverRoute.Route{
        serverRoute.POST("/{resources}", h.Create{Resource}).Name("Create {Resource}"),
        serverRoute.GET("/{resources}", h.List{Resources}).Name("List {Resources}"),
        serverRoute.GET("/{resources}/:id", h.Get{Resource}ByID).Name("Get {Resource} By ID"),
        serverRoute.PUT("/{resources}/:id", h.Update{Resource}ByID).Name("Update {Resource} By ID"),
        serverRoute.DELETE("/{resources}/:id", h.Delete{Resource}ByID).Name("Delete {Resource} By ID"),
    }
}
```

**RESTful Conventions**:
| Method | Path | Handler | Purpose |
|--------|------|---------|---------|
| POST | `/{resources}` | `Create{Resource}` | Create new resource |
| GET | `/{resources}` | `List{Resources}` | List all resources |
| GET | `/{resources}/:id` | `Get{Resource}ByID` | Get single resource |
| PUT | `/{resources}/:id` | `Update{Resource}ByID` | Update resource |
| DELETE | `/{resources}/:id` | `Delete{Resource}ByID` | Delete resource |

**Rules**:
- Use plural for collection endpoints (`/users`)
- Use `:id` for path parameters
- Use `.Name()` for Swagger documentation
- Prefix is set in handler constructor (`/v1`)
- Final URL: `/v1/{resources}` or `/v1/{resources}/:id`

---
