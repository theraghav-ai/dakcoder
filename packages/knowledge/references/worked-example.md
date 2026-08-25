---
slug: worked-example
handle: "@skill:worked-example"
fetch_when: "the full ten-step recipe, when you want to see every file at once"
sources:
  - "skill.md §Complete Example Workflow"
---

# Worked example: adding a resource end to end

> **Generated.** Do not edit — run `make knowledge` and commit the result.
> Assembled from the reference template, so it cannot drift from what the linter enforces.

The complete sequence for adding a resource. Read the corrections first — three parts of this example do not compile against the current libraries.

In practice, prefer `resource_scaffold`: it emits the same seven files deterministically, already corrected, and wires the FX registration.

## Corrections to the source

The document below is reproduced as written. These parts of it are wrong:

- The repository uses `sq.Insert(...).PlaceholderFormat(sq.Dollar)`. Use `dblib.Psql` — see @skill:repository-pattern.
- The request DTOs have a `ToDomain()` method. No such method exists in the template — see @skill:request-dto.
- The list handler builds `port.MetaDataResponse` with fields that do not exist, and the handler imports `pgx` — see @skill:response-dto and @skill:handler-pattern.

## Complete Example Workflow

*From `skill.md` §Complete Example Workflow (lines 1382–1872).*

When creating a new resource called `Product`, follow these steps:

#### Step 1: Create Domain Model
**File**: `core/domain/product.go`
```go
package domain

import "time"

type Product struct {
    ID          int64     `json:"id" db:"id"`
    Name        string    `json:"name" db:"name"`
    Description string    `json:"description" db:"description"`
    Price       float64   `json:"price" db:"price"`
    Stock       int       `json:"stock" db:"stock"`
    CreatedAt   time.Time `json:"created_at" db:"created_at"`
    UpdatedAt   time.Time `json:"updated_at" db:"updated_at"`
}
```

#### Step 2: Create Database Schema
**File**: `db/products.sql`
```sql
CREATE TABLE IF NOT EXISTS products (
    id SERIAL PRIMARY KEY,
    name VARCHAR(255) NOT NULL,
    description TEXT,
    price DECIMAL(10, 2) NOT NULL,
    stock INTEGER NOT NULL DEFAULT 0,
    created_at TIMESTAMP DEFAULT NOW(),
    updated_at TIMESTAMP DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_products_name ON products(name);
```

#### Step 3: Create Repository
**File**: `repo/postgres/product.go`
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

type ProductRepository struct {
    db  *dblib.DB
    cfg *config.Config
}

func NewProductRepository(db *dblib.DB, cfg *config.Config) *ProductRepository {
    return &ProductRepository{
        db:  db,
        cfg: cfg,
    }
}

const productTable = "products"

func (r *ProductRepository) Create(ctx context.Context, data domain.Product) (domain.Product, error) {
    ctx, cancel := context.WithTimeout(ctx, r.cfg.GetDuration("db.QueryTimeoutLow"))
    defer cancel()

    query := sq.Insert(productTable).
        Columns("name", "description", "price", "stock").
        Values(data.Name, data.Description, data.Price, data.Stock).
        Suffix("RETURNING id, name, description, price, stock, created_at, updated_at").
        PlaceholderFormat(sq.Dollar)

    var result domain.Product
    err := dblib.Insert(ctx, r.db, query, &result)
    return result, err
}

func (r *ProductRepository) FindByID(ctx context.Context, id int64) (domain.Product, error) {
    ctx, cancel := context.WithTimeout(ctx, r.cfg.GetDuration("db.QueryTimeoutLow"))
    defer cancel()

    query := sq.Select("id", "name", "description", "price", "stock", "created_at", "updated_at").
        From(productTable).
        Where(sq.Eq{"id": id}).
        PlaceholderFormat(sq.Dollar)

    var result domain.Product
    err := dblib.SelectOne(ctx, r.db, query, &result)
    if err != nil {
        return result, err
    }
    return result, nil
}

func (r *ProductRepository) List(ctx context.Context, skip, limit int64, orderBy, sortType string) ([]domain.Product, int64, error) {
    ctx, cancel := context.WithTimeout(ctx, r.cfg.GetDuration("db.QueryTimeoutMed"))
    defer cancel()

    countQuery := sq.Select("COUNT(*)").
        From(productTable).
        PlaceholderFormat(sq.Dollar)

    var totalCount int64
    err := dblib.SelectOne(ctx, r.db, countQuery, &totalCount)
    if err != nil {
        return nil, 0, err
    }

    query := sq.Select("id", "name", "description", "price", "stock", "created_at", "updated_at").
        From(productTable).
        OrderBy(orderBy + " " + sortType).
        Limit(uint64(limit)).
        Offset(uint64(skip)).
        PlaceholderFormat(sq.Dollar)

    var results []domain.Product
    err = dblib.SelectRows(ctx, r.db, query, &results)
    if err != nil {
        return nil, 0, err
    }

    return results, totalCount, nil
}

func (r *ProductRepository) Update(ctx context.Context, id int64, name, description *string, price *float64, stock *int) (domain.Product, error) {
    ctx, cancel := context.WithTimeout(ctx, r.cfg.GetDuration("db.QueryTimeoutLow"))
    defer cancel()

    query := sq.Update(productTable).
        Set("updated_at", time.Now()).
        Where(sq.Eq{"id": id}).
        PlaceholderFormat(sq.Dollar)

    if name != nil {
        query = query.Set("name", *name)
    }
    if description != nil {
        query = query.Set("description", *description)
    }
    if price != nil {
        query = query.Set("price", *price)
    }
    if stock != nil {
        query = query.Set("stock", *stock)
    }

    query = query.Suffix("RETURNING id, name, description, price, stock, created_at, updated_at")

    var result domain.Product
    err := dblib.Update(ctx, r.db, query, &result)
    return result, err
}

func (r *ProductRepository) Delete(ctx context.Context, id int64) error {
    ctx, cancel := context.WithTimeout(ctx, r.cfg.GetDuration("db.QueryTimeoutLow"))
    defer cancel()

    query := sq.Delete(productTable).
        Where(sq.Eq{"id": id}).
        PlaceholderFormat(sq.Dollar)

    return dblib.Delete(ctx, r.db, query)
}
```

#### Step 4: Create Request DTOs
**File**: `handler/request.go` (add to existing file)
```go
import "pisapi/core/port"

type CreateProductRequest struct {
    Name        string  `json:"name" validate:"required"`
    Description string  `json:"description" validate:"required"`
    Price       float64 `json:"price" validate:"required"`
    Stock       int     `json:"stock" validate:"required"`
}

func (r CreateProductRequest) ToDomain() domain.Product {
    return domain.Product{
        Name:        r.Name,
        Description: r.Description,
        Price:       r.Price,
        Stock:       r.Stock,
    }
}

type UpdateProductRequest struct {
    ID          int64   `uri:"id" validate:"required"`
    Name        string  `json:"name" validate:"omitempty"`
    Description string  `json:"description" validate:"omitempty"`
    Price       float64 `json:"price" validate:"omitempty"`
    Stock       int     `json:"stock" validate:"omitempty"`
}

type ProductIDUri struct {
    ID int64 `uri:"id" validate:"required"`
}

type ListProductsParams struct {
    port.MetadataRequest
}
```

#### Step 5: Create Response DTOs
**File**: `handler/response/product.go`
```go
package response

import (
    "pisapi/core/domain"
    "pisapi/core/port"
)

type ProductResponse struct {
    ID          int64   `json:"id"`
    Name        string  `json:"name"`
    Description string  `json:"description"`
    Price       float64 `json:"price"`
    Stock       int     `json:"stock"`
    CreatedAt   string  `json:"created_at"`
    UpdatedAt   string  `json:"updated_at"`
}

func NewProductResponse(d domain.Product) ProductResponse {
    return ProductResponse{
        ID:          d.ID,
        Name:        d.Name,
        Description: d.Description,
        Price:       d.Price,
        Stock:       d.Stock,
        CreatedAt:   d.CreatedAt.Format("2006-01-02 15:04:05"),
        UpdatedAt:   d.UpdatedAt.Format("2006-01-02 15:04:05"),
    }
}

func NewProductsResponse(data []domain.Product) []ProductResponse {
    res := make([]ProductResponse, 0, len(data))
    for _, d := range data {
        res = append(res, NewProductResponse(d))
    }
    return res
}

type ProductCreateResponse struct {
    port.StatusCodeAndMessage `json:",inline"`
    Data                      ProductResponse `json:"data"`
}

type ProductFetchResponse struct {
    port.StatusCodeAndMessage `json:",inline"`
    Data                      ProductResponse `json:"data"`
}

type ProductsListResponse struct {
    port.StatusCodeAndMessage `json:",inline"`
    port.MetaDataResponse     `json:",inline"`
    Data                      []ProductResponse `json:"data"`
}

type ProductUpdateResponse struct {
    port.StatusCodeAndMessage `json:",inline"`
    Data                      ProductResponse `json:"data"`
}

type ProductDeleteResponse struct {
    port.StatusCodeAndMessage `json:",inline"`
}
```

#### Step 6: Create Handler
**File**: `handler/product.go`
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

type ProductHandler struct {
    *serverHandler.Base
    svc *repo.ProductRepository
}

func NewProductHandler(svc *repo.ProductRepository) *ProductHandler {
    base := serverHandler.New("Products").
        SetPrefix("/v1").
        AddPrefix("")
    return &ProductHandler{
        Base: base,
        svc:  svc,
    }
}

func (h *ProductHandler) Routes() []serverRoute.Route {
    return []serverRoute.Route{
        serverRoute.POST("/products", h.CreateProduct).Name("Create Product"),
        serverRoute.GET("/products", h.ListProducts).Name("List Products"),
        serverRoute.GET("/products/:id", h.GetProductByID).Name("Get Product By ID"),
        serverRoute.PUT("/products/:id", h.UpdateProductByID).Name("Update Product By ID"),
        serverRoute.DELETE("/products/:id", h.DeleteProductByID).Name("Delete Product By ID"),
    }
}

func (h *ProductHandler) CreateProduct(sctx *serverRoute.Context, req CreateProductRequest) (*resp.ProductCreateResponse, error) {
    data := req.ToDomain()

    result, err := h.svc.Create(sctx.Ctx, data)
    if err != nil {
        log.Error(sctx.Ctx, "Error creating product: %v", err)
        return nil, err
    }

    log.Info(sctx.Ctx, "Product created with ID: %d", result.ID)
    r := &resp.ProductCreateResponse{
        StatusCodeAndMessage: port.CreateSuccess,
        Data:                 resp.NewProductResponse(result),
    }
    return r, nil
}

func (h *ProductHandler) ListProducts(sctx *serverRoute.Context, req ListProductsParams) (*resp.ProductsListResponse, error) {
    results, totalCount, err := h.svc.List(sctx.Ctx, req.Skip, req.Limit, req.OrderBy, req.SortType)
    if err != nil {
        log.Error(sctx.Ctx, "Error fetching products: %v", err)
        return nil, err
    }

    r := &resp.ProductsListResponse{
        StatusCodeAndMessage: port.ListSuccess,
        MetaDataResponse: port.MetaDataResponse{
            TotalCount: totalCount,
            Count:      int64(len(results)),
            Skip:       req.Skip,
            Limit:      req.Limit,
        },
        Data: resp.NewProductsResponse(results),
    }
    return r, nil
}

func (h *ProductHandler) GetProductByID(sctx *serverRoute.Context, req ProductIDUri) (*resp.ProductFetchResponse, error) {
    result, err := h.svc.FindByID(sctx.Ctx, req.ID)
    if err != nil {
        if err == pgx.ErrNoRows {
            log.Error(sctx.Ctx, "Product not found with ID: %d", req.ID)
            return nil, err
        }
        log.Error(sctx.Ctx, "Error fetching product by ID: %v", err)
        return nil, err
    }

    r := &resp.ProductFetchResponse{
        StatusCodeAndMessage: port.FetchSuccess,
        Data:                 resp.NewProductResponse(result),
    }
    return r, nil
}

func (h *ProductHandler) UpdateProductByID(sctx *serverRoute.Context, req UpdateProductRequest) (*resp.ProductUpdateResponse, error) {
    var name, description *string
    var price *float64
    var stock *int

    if req.Name != "" {
        name = &req.Name
    }
    if req.Description != "" {
        description = &req.Description
    }
    if req.Price != 0 {
        price = &req.Price
    }
    if req.Stock != 0 {
        stock = &req.Stock
    }

    result, err := h.svc.Update(sctx.Ctx, req.ID, name, description, price, stock)
    if err != nil {
        if err == pgx.ErrNoRows {
            log.Error(sctx.Ctx, "Product not found with ID: %d", req.ID)
            return nil, err
        }
        log.Error(sctx.Ctx, "Error updating product by ID: %v", err)
        return nil, err
    }

    r := &resp.ProductUpdateResponse{
        StatusCodeAndMessage: port.UpdateSuccess,
        Data:                 resp.NewProductResponse(result),
    }
    return r, nil
}

func (h *ProductHandler) DeleteProductByID(sctx *serverRoute.Context, req ProductIDUri) (*resp.ProductDeleteResponse, error) {
    err := h.svc.Delete(sctx.Ctx, req.ID)
    if err != nil {
        if err == pgx.ErrNoRows {
            log.Error(sctx.Ctx, "Product not found with ID: %d", req.ID)
            return nil, err
        }
        log.Error(sctx.Ctx, "Error deleting product by ID: %v", err)
        return nil, err
    }

    r := &resp.ProductDeleteResponse{
        StatusCodeAndMessage: port.DeleteSuccess,
    }
    return r, nil
}
```

#### Step 7: Register Dependencies
**File**: `bootstrap/bootstrapper.go`
```go
var FxRepo = fx.Module(
    "Repomodule",
    fx.Provide(
        repo.NewUserRepository,
        repo.NewProductRepository, // Add this line
    ),
)

var FxHandler = fx.Module(
    "Handlermodule",
    fx.Provide(
        fx.Annotate(
            handler.NewUserHandler,
            fx.As(new(serverHandler.Handler)),
            fx.ResultTags(serverHandler.ServerControllersGroupTag),
        ),
        fx.Annotate(
            handler.NewProductHandler, // Add this block
            fx.As(new(serverHandler.Handler)),
            fx.ResultTags(serverHandler.ServerControllersGroupTag),
        ),
    ),
)
```

#### Step 8: Generate Validators
```bash
cd handler
govalid
```

#### Step 9: Run Migrations
```bash
# Apply database schema
psql -U username -d database -f db/products.sql
```

#### Step 10: Test Endpoints
```bash
# Start the server
go run main.go

# Test endpoints
# Create
curl -X POST http://localhost:8080/v1/products \
  -H "Content-Type: application/json" \
  -d '{"name":"Product 1","description":"Description","price":99.99,"stock":100}'

# List
curl http://localhost:8080/v1/products

# Get by ID
curl http://localhost:8080/v1/products/1

# Update
curl -X PUT http://localhost:8080/v1/products/1 \
  -H "Content-Type: application/json" \
  -d '{"name":"Updated Product"}'

# Delete
curl -X DELETE http://localhost:8080/v1/products/1
```

---
