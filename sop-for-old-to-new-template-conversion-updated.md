# SOP Template for API Template Conversion

This SOP provides a step-by-step technical guide for migrating backend microservices from the legacy template architecture to the updated modular template.

## Major Changes Summary

- Generation of automated swagger docs (served natively at /docs/v3Doc.json)
- Generalized error handling and unified response formats
- Removing direct dependency on Gin framework in handlers and service layers
- Using DTOs for request binding and response objects
- Handler methods strictly accept request structs (raw slices/arrays in handler parameters are replaced with wrapper structs tagged with validate:"dive")
- Consolidating and moving all request DTO structs to a dedicated package at handler/request/request.go
- Removing ShouldBind and manual validation from handlers
- Commenting routes.go file and moving route registration inside individual handler files
- Enhanced stack trace readability with log levels configurable to DEBUG
- Standardized gRPC & Connect-RPC integration with Uber FX (bootstrapper.FxGrpc and grpcserver.HandlerRegistry)
- Handling read replica database dependencies via FxRepo alias (preventing duplicate metric registration panic)
- Configurable Swagger nullableTypeMap in config.yaml for custom nullable libraries (e.g. guregu/null)
- Support for parameterless endpoints using empty request structs (\_ struct{})
- Prohibition of pointer-to-slice (\*\[\]Struct) in response DTOs to ensure clean OpenAPI v3 schema registration
- Prohibition of json:"-" on URI and query request structs to prevent erroneous Swagger request body generation

## Step 1: Branch Creation & Git Setup

Objective: Create an isolated feature branch to work on the template conversion safely without affecting main development.

Run the following commands (Example assuming working from development branch):

```
# Switch to development, pull latest changes, and create template-conversion branch
git checkout development
git pull origin development
git checkout -b template-conversion
git push -u origin template-conversion
```

## Step 2: Dependencies (Updating go.mod)

Objective: Install new n-api-\* template libraries and clean up unused legacy dependencies from go.mod.

| **Legacy Package**                                | **New Modular Package**                                    | **Purpose / Notes**                              |
| ------------------------------------------------- | ---------------------------------------------------------- | ------------------------------------------------ |
| gitlab.cept.gov.in/it-2.0-common/api-bootstrapper | gitlab.cept.gov.in/it-2.0-common/n-api-bootstrapper@latest | Core app lifecycle & DI (provides \*n-api-db.DB) |
| gitlab.cept.gov.in/it-2.0-common/api-server       | gitlab.cept.gov.in/it-2.0-common/n-api-server@latest       | HTTP server routing & controllers                |
| gitlab.cept.gov.in/it-2.0-common/api-log          | gitlab.cept.gov.in/it-2.0-common/n-api-log@latest          | Structured zero-allocation logger                |
| gitlab.cept.gov.in/it-2.0-common/api-errors       | gitlab.cept.gov.in/it-2.0-common/n-api-errors@latest       | Standardized application errors                  |
| gitlab.cept.gov.in/it-2.0-common/api-db           | gitlab.cept.gov.in/it-2.0-common/n-api-db@latest           | Database utilities, pools & query builders       |
| gitlab.cept.gov.in/it-2.0-common/api-validation   | gitlab.cept.gov.in/it-2.0-common/n-api-validation@latest   | Compile-time validation engine (govalid)         |
| gitlab.cept.gov.in/it-2.0-common/api-config       | gitlab.cept.gov.in/it-2.0-common/api-config (NO CHANGE)    | Configuration manager (stays as api-config)      |

### Command to Execute

<div class="joplin-table-wrapper"><table><tbody><tr><th><pre><code># 1. Download updated template packages
go get gitlab.cept.gov.in/it-2.0-common/n-api-bootstrapper@latest
go get gitlab.cept.gov.in/it-2.0-common/n-api-server@latest
go get gitlab.cept.gov.in/it-2.0-common/n-api-log@latest
go get gitlab.cept.gov.in/it-2.0-common/n-api-validation@latest
go get github.com/bufbuild/protovalidate-go@v0.9.2
go get gitlab.cept.gov.in/it-2.0-common/grpc-server@latest
go get go.uber.org/fx@latest
go get github.com/jackc/pgx/v5@latest</code></pre><p></p><pre><code># 2. Clean up obsolete dependencies
go mod tidy</code></pre></th></tr></tbody></table></div>

**⚠️ CRITICAL DEPENDENCY RULE:** Always use n-api-bootstrapper (not the legacy api-bootstrapper). The legacy bootstrapper injects the old \*api-db.DB type into Uber FX, causing dependency mismatches with repositories expecting \*n-api-db.DB. Furthermore, ensure github.com/bufbuild/protovalidate-go is pinned to @v0.9.2 to maintain full interface compatibility with generated protobuf validators.

Protobuf & Protovalidate Dependency Clash Resolution (For Services with gRPC / Connect-RPC):

If your microservice uses gRPC or Connect-RPC generated protocol buffers and encounters compilation errors such as undefined: File_buf_validate_expression_proto or File_buf_validate_validate_proto, add the following replace directive at the bottom of go.mod:

```
// Add at the bottom of go.mod:
replace buf.build/gen/go/bufbuild/protovalidate/protocolbuffers/go => buf.build/gen/go/bufbuild/protovalidate/protocolbuffers/go v1.36.1-20241127180247-a33202765966.1
```

**💡 PROTOBUF REPLACE DIRECTIVE RULE:** Breaking changes in upstream generated protocol buffer repositories can cause missing validator symbols. Locking the protocol buffer package to commit v1.36.1-20241127180247-a33202765966.1 resolves this issue completely. Always run 'go mod tidy' after adding the replace directive.

## Step 3: Steps to follow (Code Conversion)

Note: The steps below must be applied to EVERY handler file in your microservice.

1\. Import the required server packages from n-api-server and request DTOs:

```
import (
    serverHandler "gitlab.cept.gov.in/it-2.0-common/n-api-server/handler"
    serverRoute "gitlab.cept.gov.in/it-2.0-common/n-api-server/route"
    request "<module-name>/handler/request"
    response "<module-name>/handler/response"
)
```

2\. Replace legacy logging and error imports with n-api-log and n-api-errors:

Replace api-log and api-errors imports with their n-api-\* equivalents in all handler, service, and repository files:

<div class="joplin-table-wrapper"><table><tbody><tr><th><pre><code>// Replace legacy api-log and api-errors imports:
// Old:  "gitlab.cept.gov.in/it-2.0-common/api-log"
// New:
import log "gitlab.cept.gov.in/it-2.0-common/n-api-log"</code></pre><p></p><pre><code>// Old:  "gitlab.cept.gov.in/it-2.0-common/api-errors"
// New:
import apierrors "gitlab.cept.gov.in/it-2.0-common/n-api-errors"</code></pre></th></tr></tbody></table></div>

3\. Add serverHandler.Base to the handler struct (or implement serverHandler.Handler):

```
// User"
type SanctionHandler struct {
 *serverHandler.Base
 svc port.SanctionService
}
```

4\. In the constructor function, initialize the Base field using SetPrefix:

```
func NewAwardsHandler(svc *repo.AwardRepository) *AwardHandler {
    return &AwardHandler{
        Base: serverHandler.NewBase().SetPrefix("/v1/awards"),
        svc:  svc,
    }
}
```

5\. Register the routes using Routes() method for the handler struct:

```
func (c *AwardHandler) Routes() []serverRoute.Route {
    return []serverRoute.Route{
        serverRoute.POST("", c.CreateAwardsBulk).Name("Create Awards Bulk"),
        serverRoute.GET("/:award-id", c.FetchAwardDetails).Name("Fetch Award Details"),
        serverRoute.PUT("/:award-id", c.UpdateAwardDetails).Name("Update Award Details"),
        serverRoute.DELETE("/:award-id", c.DeleteAwardDetails).Name("Delete Award Details"),
    }
}
```

**⚠️ ROUTE PREFIXING RULE:** In the legacy template, Gin route groups typically defined path prefixes (e.g. awards := r.Group("/awards")). In the new template, SetPrefix("/v1/awards") establishes the base path. Child routes in Routes() should only specify the sub-path (e.g. "" for base, "/:award-id" for parameter routes). Never duplicate the prefix inside Routes().

6\. Remove dependency on gin framework (\*gin.Context) from function signatures and use context & request struct:

```
func (ah *AwardHandler) CreateAwardsBulk(sctx *serverRoute.Context, req request.CreateAwardsRequest) (*response.CreateAwardsBulkAPIResponse, error) {
    // Access context via sctx.Ctx
    // Access request data directly from typed parameter req
}
```

**⚠️ CRITICAL HANDLER SIGNATURE RULE:** Handler functions/methods MUST accept AT MOST two parameters: the first parameter must be \*serverRoute.Context (or context.Context), and the optional second parameter must be a request STRUCT (e.g., req request.MyRequest). Handlers CANNOT accept raw primitives, raw slices/arrays (like \[\]request.Item), or more than two parameters. All parameters and body payloads must be encapsulated inside a single request struct.

**Handling Endpoints Without Request Parameters (\_ struct{}):**

When an API endpoint requires no request payload or URL query parameters (such as a parameterless GET endpoint or a simple trigger POST), n-api-server's generic route definitions still require a request type parameter. In such cases, declare the request parameter using an empty anonymous struct as \_ struct{} in the handler method signature:

<div class="joplin-table-wrapper"><table><tbody><tr><th><pre><code>// 1. In handler: Accept _ struct{} when no request parameters are needed:
func (ah *AwardHandler) FetchAwardsSummary(sctx *serverRoute.Context, _ struct{}) (*response.AwardSummaryAPIResponse, error) {
    // Access request context via sctx.Ctx
    // Execute service/repository call without input payload
    summary, err := ah.svc.GetSummary(sctx.Ctx)
    if err != nil {
        return nil, apierrors.NewAppError(apierrors.HTTPErrorInternalServerError, "Failed to fetch summary", err)
    }
    return summary, nil
}</code></pre><p></p><pre><code>// 2. In Routes(): Register route normally without special wrappers:
serverRoute.GET("/summary", ah.FetchAwardsSummary).Name("Fetch Awards Summary"),</code></pre></th></tr><tr><td><p><strong>⚠️ EMPTY REQUEST STRUCT RULE: </strong>In n-api-server, route helpers (serverRoute.GET, POST, PUT, DELETE) are generic functions expecting route.HandlerFunc[Req, Res]. Even if an endpoint takes no parameters, do NOT omit the second parameter or pass any/interface{}. Always use _ struct{} as the second argument. This ensures compile-time type safety, prevents accidental payload bindings, and ensures Swagger documentation does not generate an unnecessary request body definition.</p></td></tr></tbody></table></div>

7\. Handling Bulk / Array Request Payloads (Using Struct with validate:"dive"):

Define the wrapper struct in handler/request/request.go and update the handler method signature to accept this wrapper:

<div class="joplin-table-wrapper"><table><tbody><tr><th><pre><code>// 1. In handler/request/request.go:
type TransferEntryRequests struct {
    TransferEntries []TransferEntryRequest `json:"transfer_entries" validate:"required,dive"`
}</code></pre><p></p><pre><code>// 2. In handler:
func (h *TransferHandler) CreateTransferEntries(sctx *serverRoute.Context, req request.TransferEntryRequests) (*response.TransferEntryAPIResponse, error) {
    for _, entry := range req.TransferEntries {
        // Process each entry safely
    }
}</code></pre></th></tr></tbody></table></div>

Expected Client JSON Payload Format (sending an object with an array field):

```
// 3. Client JSON Payload:
{
  "transfer_entries": [
    { "amount": 100.50, "hoa": "123456789012345" },
    { "amount": 250.00, "hoa": "987654321098765" }
  ]
}
```

8\. Query Parameters & Pagination DTO Tagging Rule (MetaDataRequest):

For HTTP GET requests and pagination/sorting metadata (e.g. MetaDataRequest), NEVER use \`json:...\` tags. Use \`form:...\` tags so n-api-server can bind query parameters from URL strings into struct fields:

<div class="joplin-table-wrapper"><table><tbody><tr><th><pre><code>// handler/request/request.go
package request</code></pre><p></p><pre><code>// ❌ INCORRECT: Using json tags for query parameters causes empty/zero values
type FetchAllottedHOAListRequest struct {
    FinancialYear string `json:"financial_year"`
}</code></pre><p></p><pre><code>// ✅ CORRECT: Use form tags for all query parameters and search/filter fields
type FetchAllottedHOAListRequest struct {
    FinancialYear string `form:"financial_year" validate:"required,len=4,year"`
    port.MetaDataRequest
}</code></pre></th></tr></tbody></table></div>

**⚠️ URI & QUERY REQUEST TAGGING RULE:** Do NOT add json:"-" to path (uri:) or query (form:) parameter request structs. In n-api-server, the Swagger route generator checks \`if f.Tag.Get("json") != "" { hasBody = true }\` without verifying whether the tag value is "-". Any presence of a json tag causes Swagger to generate an HTTP request body definition referencing the struct (\$ref: "#/definitions/StructName"). However, n-api-server's schema definition builder strictly skips fields with json:"-". If all fields have json:"-", no schema definition is registered in the Swagger document, creating a broken dangling reference. This causes openapi2conv.ToV3 to crash during application startup with: \`Error converting to v3: failed to resolve "..." in fragment in URI: map key not found\`. Rule: For URI path and URL query parameter structs, omit the json tag entirely (only declare uri:"..." or form:"...").

9\. Unified Error Handling & Single Return Pattern:

Always use apierrors.NewAppError to return structured API errors with proper HTTP status codes. Avoid returning both response and error simultaneously:

```
// ✅ Correct Single Return Pattern:
if err != nil {
    log.Error(sctx, "Failed to process award: %s", err.Error())
    return nil, apierrors.NewAppError(
        apierrors.HTTPErrorBadRequest,
        "Invalid award payload",
        err,
    )
}
return &apiResponse, nil
```

### Handler with File Upload

1\. For file upload, define form fields and \*multipart.FileHeader in your request struct:

<div class="joplin-table-wrapper"><table><tbody><tr><th><pre><code>// handler/request/request.go
package request</code></pre><p></p><pre><code>type CreateAwardRequest struct {
    AwardName  string                `form:"award_name" validate:"required"`
    SingleFile *multipart.FileHeader `form:"single_file" validate:"required"`
}</code></pre></th></tr></tbody></table></div>

2\. Access the file directly using req.SingleFile.Open() inside the handler:

<div class="joplin-table-wrapper"><table><tbody><tr><th><pre><code>file, err := req.SingleFile.Open()
if err != nil {
    return nil, fmt.Errorf("file couldn't be opened")
}
defer file.Close()</code></pre><p></p><pre><code>fileSize := req.SingleFile.Size
fileName := req.SingleFile.Filename</code></pre></th></tr></tbody></table></div>

### File Response Handling

Use port.FileResponse to send byte arrays or streamed files:

<div class="joplin-table-wrapper"><table><tbody><tr><th><pre><code>// 1. File as byte array
res := port.FileResponse{
    ContentType:        "application/zip",
    ContentDisposition: "attachment; filename=\"pisdocuments.zip\"",
    Data:               buf.Bytes(),
}
return &amp;res, nil</code></pre><p></p><pre><code>// 2. File as stream (io.Reader)
resStream := port.FileResponse{
    ContentType:        "application/pdf",
    ContentDisposition: "inline; filename=\"sample.pdf\"",
    Reader:             fileReaderObject,
}
return &amp;resStream, nil</code></pre></th></tr></tbody></table></div>

### bootstrap/bootstrapper.go

Objective: Register all HTTP handlers, gRPC handlers, and Connect services in the Uber FX dependency injection container.

1\. Register Handlers in FxHandler:

HTTP REST handlers must be annotated with serverHandler.ServerControllersGroupTag. If your service implements gRPC / Connect-RPC, provide the gRPC handler constructors directly (unannotated) in FxHandler:

```
var FxHandler = fx.Module(
    "Handlermodule",
    fx.Provide(
        // HTTP REST Handlers (annotated with ServerControllersGroupTag)
        fx.Annotate(
            handler.NewTransferHandler,
            fx.As(new(serverHandler.Handler)),
            fx.ResultTags(serverHandler.ServerControllersGroupTag),
        ),
        fx.Annotate(
            handler.NewNocHandler,
            fx.As(new(serverHandler.Handler)),
            fx.ResultTags(serverHandler.ServerControllersGroupTag),
        ),
        // example gRPC Handlers (provided directly without annotations)
        handler.NewAllocationGrpcHandler,
        handler.NewBudgetGrpcHandler,
        handler.NewConsumptionGrpcHandler,
    ),
)
```

2\. Register Services with gRPC HandlerRegistry (AddHandlers):

For microservices implementing gRPC / Connect-RPC, define the AddHandlers function using \*grpcserver.HandlerRegistry. Each service handler constructor must be wrapped using grpcserver.Wrap:

```
import (
    v1 "gotemplate/gen/proto/v1/budgetallocationconnect"
    grpcserver "gitlab.cept.gov.in/it-2.0-common/grpc-server"
)
 //example
func AddHandlers(
    registry *grpcserver.HandlerRegistry,
    allocationHandler *handler.AllocationGrpcHandler,
    budgetHandler *handler.BudgetGrpcHandler,
    consumptionHandler *handler.ConsumptionGrpcHandler,
) {
    registry.AddHandlers([]grpcserver.HandlerDefinition{
        {
            Constructor: grpcserver.Wrap(v1.NewAllocationServiceHandler),
            Server:      allocationHandler,
        },
        {
            Constructor: grpcserver.Wrap(v1.NewBudgetServiceHandler),
            Server:      budgetHandler,
        },
        {
            Constructor: grpcserver.Wrap(v1.NewConsumptionServiceHandler),
            Server:      consumptionHandler,
        },
    })
}
```

**⚠️ CRITICAL gRPC ARCHITECTURE RULE:** DO NOT create a custom var FxGrpc inside bootstrap/bootstrapper.go (and do not create custom net.Listeners or custom servers). The gRPC server lifecycle is managed automatically by bootstrapper.FxGrpc from n-api-bootstrapper.

**💡 UNUSED gRPC SERVICE RULE:** If gRPC services are not implemented or are commented out in the microservice, make sure to comment out AddHandlers and bootstrapper.FxGrpc in main.go to avoid missing dependency errors (\*grpcserver.HandlerRegistry).

**3\. Repositories with read_db Dependency (Read DB Alias Pattern):**

Many microservice repositories inject both write_db and read_db (\*dblib.DB). In the updated modular template, do NOT initialize a separate read database pool via bootstrapper.FxReadDB. Instead, inside FxRepo in bootstrap/bootstrapper.go, provide read_db as an annotated alias pointing directly to write_db:

<div class="joplin-table-wrapper"><table><tbody><tr><th><pre><code>var FxRepo = fx.Module(
    "Repomodule",
    fx.Provide(
        repo.NewBankMasterRepository,
        repo.NewScrollRepository,
        // ... provide other repositories ...</code></pre><p></p><pre><code>        // Provide read_db as an annotated alias pointing to write_db:
        fx.Annotated{
            Name: "read_db",
            Target: func(p struct {
                fx.In
                WriteDB *dblib.DB `name:"write_db"`
            }) *dblib.DB {
                return p.WriteDB
            },
        },
    ),
)</code></pre></th></tr><tr><td><p><strong>⚠️ CRITICAL READ DB RULE: </strong>Do NOT include bootstrapper.FxReadDB in main.go. The default n-api-bootstrapper.FxReadDB attempts to register duplicate VictoriaMetrics metric counters ('pgxpool_acquire_count') into the shared metrics set, causing a fatal runtime panic on startup. Always provide read_db as an alias inside FxRepo as shown above.</p></td></tr></tbody></table></div>

### Validation Setup (govalid)

Objective: Generate automatic compile-time request validation code.

1\. Move and Consolidate Request Structs in handler/request/request.go:

Extract and move all request DTO structs (from individual handler files or legacy locations) into a single centralized file: handler/request/request.go under package request. Update all handler files to import this package (import request "&lt;module&gt;/handler/request") and use req request.&lt;StructName&gt;.

2\. Handling Validation Rules & Custom Validations:

n-api-validation (govalid) handles validation at compile-time by generating Go code from struct tags. The legacy validator.go file, runtime registration (RegisterCustomValidation / NewValidatorService), and the bootstrap.Fxvalidator module are all removed. Validation is now fully automated — no manual registration needed.

| **Legacy Custom Tag**             | **govalid Built-in Replacement**         | **Rule Description**                      |
| --------------------------------- | ---------------------------------------- | ----------------------------------------- |
| validatePaocode / validateDdocode | validate:"required,len=6,numeric"        | Must be exactly 6 numeric digits          |
| validatePeriod                    | validate:"required,len=6,numeric"        | Period code (MMYYYY) 6 numeric characters |
| validateDateTime                  | validate:"required,date_yyyy_mm_dd"      | Date format YYYY-MM-DD                    |
| allotamount                       | validate:"gte=0"                         | Positive amount (>= 0)                    |
| percent                           | validate:"gte=0,lte=100"                 | Percentage value between 0 and 100        |
| vendor_id_len                     | validate:"gte=1000000000,lte=9999999999" | Range for exactly 10 digits               |
| status                            | validate:"len=2,numeric"                 | 2-digit status code                       |
| employee_id / office_id           | validate:"required,employee_id"          | Built-in government ID format validator   |
| head_of_account / account_no      | validate:"required,head_of_account"      | Accounting Head of Account format         |

Built-in Markers (Standard Rules): Use concise tags for range, length, and options. For a complete list of supported markers, refer to the n-api-validation README (<https://gitlab.cept.gov.in/it-2.0-common/n-api-validation>).

\- Positive Amount (>= 0): validate:"gte=0"

\- Percentage (0 - 100): validate:"gte=0,lte=100"

\- Exact Length: validate:"len=2,numeric"

CEL Expressions (Custom Format Rules): Use validate:"cel=..." for custom regex or inline expressions.

\- HH:MM Format: validate:"cel=value.matches('^(\[01\]\\\\d|2\[0-3\]):(\[0-5\]\\\\d)\$')"

Complex Business Validation & Custom Validation Rules:

For simple validations, use built-in tags or CEL expressions. For custom domain logic that must be re-used across DTOs, define a Custom Validation Rule using the

// +govalid:rule marker.  
<br/>1\. Definition: Define the validation function in package request (in handler/request/request.go or a helper file inside handler/request/).  
2\. Signature: The validation function must be a package-level function (not a method), cannot use generics, accept exactly one parameter (matching the struct field type), and return exactly bool.  
3\. Usage: Document the function with // +govalid:rule=rule_name and tag the field in your DTO with validate:"rule_name".

<div class="joplin-table-wrapper"><table><tbody><tr><th><pre><code>// handler/request/request.go
package request</code></pre><p></p><pre><code>type AllTags struct {
    GrossAmount float64 `json:"gross_amount" validate:"omitempty,is_positive"`
}</code></pre><p></p><pre><code>// +govalid:rule=is_positive
// +govalid:message=field [@FIELD] must be a positive
func validatePositive(val float64) bool {
    return val &gt; 0
}</code></pre></th></tr></tbody></table></div>

Complex business validation belongs in the handler/service layer. Example: Validate vendor ID length in handler before processing:

```
// Complex business validation belongs in the handler/service layer
if len(req.VendorID) != 10 {
    return nil, apierrors.NewAppError(
        apierrors.HTTPErrorBadRequest,
        "Invalid vendor ID length: must be exactly 10 digits",
        nil,
    )
}
```

**⚠️ CRITICAL GOVALID RULE:** Do NOT define a Validate() method on request DTO structs. n-api-validation (govalid) generates compile-time validation code. Defining a manual Validate() method interferes with the generated code and causes compilation errors.

**⚠️ MANDATORY VALIDATION TAG RULE:** Every request DTO struct MUST have at least one field tagged with validate:... (or be registered with // +govalid:struct). If a struct contains no validation tags, govalid skips code generation for that struct, resulting in runtime binding failures or missing validator errors.

3\. Install govalid tool and generate validation code:

<div class="joplin-table-wrapper"><table><tbody><tr><th><pre><code>go install gitlab.cept.gov.in/it-2.0-common/n-api-validation/cmd/govalid@latest</code></pre><p></p><pre><code># Execute govalid directly inside handler/request directory:
cd handler/request
govalid ./request.go
cd ../..</code></pre></th></tr></tbody></table></div>

### main.go

Objective: Clean up the main function, configure application modules, and bind the gRPC server lifecycle.

1\. Remove \`fx.Invoke(routes.Routes)\` from \`main.go\` (HTTP routes are auto-registered by servercontrollers).

2\. Remove \`bootstrap.Fxvalidator\` from \`main.go\` (validation is now handled compile-time by govalid).

3\. Wire \`bootstrapper.FxGrpc\` and \`bootstrap.AddHandlers\` (when gRPC / Connect-RPC is implemented):

<div class="joplin-table-wrapper"><table><tbody><tr><th><pre><code>package main</code></pre><p></p><pre><code>import (
    "context"
    "gotemplate/bootstrap"
    bootstrapper "gitlab.cept.gov.in/it-2.0-common/n-api-bootstrapper"
    "go.uber.org/fx"
)</code></pre><p></p><pre><code>func main() {
    app := bootstrapper.New().Options(
        bootstrapper.Fxclient,
        bootstrap.FxHandler,
        bootstrap.FxRepo,
        // When gRPC is implemented:
        bootstrapper.FxGrpc,              // Core gRPC Server lifecycle
        fx.Invoke(bootstrap.AddHandlers), // Connects registered services to gRPC Server
        // Domain modules (Temporal, MinIO, etc.)
        bootstrap.Fxtemporal,
    )
    app.WithContext(context.Background()).Run()
}</code></pre></th></tr></tbody></table></div>

**💡 MAIN.GO gRPC WIRING NOTE:** If your microservice does not implement gRPC, omit bootstrapper.FxGrpc and fx.Invoke(bootstrap.AddHandlers) from main.go to avoid missing HandlerRegistry errors.

4\. Omit or comment out \`bootstrapper.FxReadDB\` from \`main.go\` (the read_db dependency is satisfied via FxRepo alias):

**💡 READ DB IN MAIN.GO RULE:** Never uncomment or add bootstrapper.FxReadDB in main.go. The read_db dependency is already provided by the alias inside FxRepo in bootstrap/bootstrapper.go.

## ../core/port/response.go

Copy the functions given below and paste them in response.go folder

<div class="joplin-table-wrapper"><table><tbody><tr><th><pre><code>func (s StatusCodeAndMessage) Status() int {
    return s.StatusCode
}</code></pre><p></p><pre><code>func (s StatusCodeAndMessage) ResponseType() string {
    return "standard"
}</code></pre><p></p><pre><code>func (s StatusCodeAndMessage) GetContentType() string {
    return "application/json"
}</code></pre><p></p><pre><code>func (s StatusCodeAndMessage) GetContentDisposition() string {
    return ""
}</code></pre><p></p><pre><code>func (s StatusCodeAndMessage) Object() []byte {
    return nil
}</code></pre></th></tr></tbody></table></div>

**⚠️ RESPONSE STRUCT SLICE CONVENTION:** Always define response slice fields as standard slices (Data \[\]SomeResponse), NEVER as pointers to slices (Data \*\[\]SomeResponse). In Go, a slice header already consists of a pointer to the underlying backing array, a length, and a capacity; wrapping a slice in a pointer (\*\[\]T) is redundant and non-idiomatic. Furthermore, n-api-server's Swagger reflection scanner inspects structs, pointers to structs (\*Struct), and slices of structs (\[\]Struct), but omits pointers to slices of structs (\*\[\]Struct). As a result, inner structs referenced via \*\[\]Struct are never registered in the Swagger definitions dictionary. This causes openapi2conv.ToV3 to fail on startup with: \`failed to resolve "..." in fragment in URI: "#/components/schemas/...": map key not found\`. Rule: Always use \[\]Struct in response DTOs.

## Step 4: Test Suite Setup & Updates (tests/testmain_test.go)

Objective: Modernize the test harness to support n-api-server, Uber FX, and testcontainers.

1\. Overwrite tests/testmain_test.go with the Generic Test Harness:

Replace your legacy tests/testmain_test.go with the universal N-API test harness:

<div class="joplin-table-wrapper"><table><tbody><tr><th><pre><code>package tests</code></pre><p></p><pre><code>import (
    "context"
    "fmt"
    "os"
    "path/filepath"
    "runtime"
    "testing"
    "time"</code></pre><p></p><pre><code>    "gotemplate/bootstrap"</code></pre><p></p><pre><code>    "github.com/gin-gonic/gin"
    _ "github.com/golang-migrate/migrate/v4/database/postgres"
    _ "github.com/golang-migrate/migrate/v4/source/file"
    "github.com/jackc/pgx/v5/pgxpool"
    "github.com/minio/minio-go/v7"
    "github.com/minio/minio-go/v7/pkg/credentials"
    "github.com/testcontainers/testcontainers-go"
    tcminio "github.com/testcontainers/testcontainers-go/modules/minio"
    "github.com/testcontainers/testcontainers-go/wait"
    config "gitlab.cept.gov.in/it-2.0-common/api-config"
    db "gitlab.cept.gov.in/it-2.0-common/n-api-db"
    log "gitlab.cept.gov.in/it-2.0-common/n-api-log"
    router "gitlab.cept.gov.in/it-2.0-common/n-api-server"
    serverHandler "gitlab.cept.gov.in/it-2.0-common/n-api-server/handler"
    tclient "go.temporal.io/sdk/client"
    "go.uber.org/fx"
    "go.uber.org/fx/fxtest"
)</code></pre><p></p><pre><code>var Router *gin.Engine</code></pre><p></p><pre><code>var Fxconfig = fx.Module(
    "configmodule",
    fx.Provide(
        config.NewDefaultConfigFactory,
        newFxConfig,
    ),
)</code></pre><p></p><pre><code>type FxConfigParam struct {
    fx.In
    Factory config.ConfigFactory
}</code></pre><p></p><pre><code>func newFxConfig(p FxConfigParam) (*config.Config, error) {
    return p.Factory.Create(
        config.WithFileName("config"),
        config.WithFilePaths(
            ".",
            "../configs",
        ),
    )
}</code></pre><p></p><pre><code>var FxDB = fx.Module(
    "DBModule",
    fx.Provide(
        SetUpDB,
    ),
)</code></pre><p></p><pre><code>var MinioContainer *tcminio.MinioContainer</code></pre><p></p><pre><code>// Dependency structure for Fx
type FxMinioParam struct {
    fx.In
    Factory log.LoggerFactory `optional:"true"`
    Config  *config.Config
}</code></pre><p></p><pre><code>// Function to initialize MinIO
func newTestFxMinio(p FxMinioParam, client *minio.Client, cfg *config.Config) error {
    ctx := context.Background()
    bucketName := cfg.GetString("minio.bucketName")
    if bucketName == "" {
        bucketName = "pao"
    }
    exists, err := client.BucketExists(ctx, bucketName)
    if err != nil {
        log.Info(ctx, "Error checking if bucket exists")
        return fmt.Errorf("failed to check bucket existence: %w", err)
    }</code></pre><p></p><pre><code>    if exists {
        log.GetBaseLoggerInstance().ToZerolog().Debug().Msg("Bucket found")
    } else {
        err := client.MakeBucket(ctx, bucketName, minio.MakeBucketOptions{})
        if err != nil {
            log.Info(ctx, "Error creating bucket")
            return fmt.Errorf("failed to create bucket: %w", err)
        }
        log.Info(ctx, "Bucket created successfully")
    }
    return nil
}</code></pre><p></p><pre><code>// Function to set up MinIO test container
func SetUpMinio(ctx context.Context, cfg *config.Config) (*minio.Client, *tcminio.MinioContainer, error) {
    if ctx == nil {
        ctx = context.Background()
    }
    // Run the MinIO test container
    var err error
    MinioContainer, err = tcminio.Run(ctx,
        "minio/minio:RELEASE.2024-01-16T16-07-38Z")
    if err != nil {
        return nil, nil, fmt.Errorf("failed to start MinIO container: %w", err)
    }
    // Retrieve connection details
    url, err := MinioContainer.ConnectionString(ctx)
    if err != nil {
        return nil, nil, fmt.Errorf("failed to get MinIO connection string: %w", err)
    }</code></pre><p></p><pre><code>    // Create MinIO client
    minioClient, err := minio.New(url, &amp;minio.Options{
        Creds:  credentials.NewStaticV4(MinioContainer.Username, MinioContainer.Password, ""),
        Secure: false, // Use false for testing; set true in production with HTTPS
    })
    if err != nil {
        return nil, nil, fmt.Errorf("failed to create MinIO client: %w", err)
    }
    bucketName := cfg.GetString("minio.bucketName")
    if bucketName == "" {
        bucketName = "test-bucket" // or cfg.GetString("app.name")
    }</code></pre><p></p><pre><code>    if err := minioClient.MakeBucket(ctx, bucketName, minio.MakeBucketOptions{}); err != nil {
        log.Info(ctx, "Failed to create bucket: %v", err)
    }
    log.Info(ctx, "Successfully connected to the minio")</code></pre><p></p><pre><code>    return minioClient, MinioContainer, nil
}</code></pre><p></p><pre><code>// Fx Module for MinIO
var FxMinIO = fx.Module(
    "MinIOModule",
    fx.Provide(func(ctx context.Context, cfg *config.Config) (*minio.Client, *tcminio.MinioContainer, error) {
        return SetUpMinio(ctx, cfg)
    }),
    fx.Invoke(newTestFxMinio),
)</code></pre><p></p><pre><code>// FxTemporal provides a Temporal client for the test graph. If host/port are not configured,
// it returns nil so tests can run without attempting to connect to a Temporal server.
var FxTemporal = fx.Module(
    "TemporalModule",
    fx.Provide(func(c *config.Config) (tclient.Client, error) {
        host := c.GetString("temporal.host")
        port := c.GetString("temporal.port")
        if host != "" &amp;&amp; port != "" {
            return tclient.NewLazyClient(tclient.Options{
                HostPort:  host + ":" + port,
                Namespace: "default",
            })
        }
        return nil, nil
    }),
)</code></pre><p></p><pre><code>func SetUpDB(c *config.Config) (*db.DB, testcontainers.Container) {
    ctx := context.Background()
    var db1 *pgxpool.Pool
    var err error
    db1, Container, err = setupdockerdb(ctx, c)
    if err != nil {
        log.Fatal(ctx, "failed to setup db---&gt;&gt;&gt; %s", err)
    }
    db := db.DB{Pool: db1}
    log.Info(ctx, "Successfully connected to the database %s", c.GetString("db.database"))
    return &amp;db, Container
}</code></pre><p></p><pre><code>func setupdockerdb(ctx context.Context, c *config.Config) (*pgxpool.Pool, testcontainers.Container, error) {
    var env = map[string]string{
        "POSTGRES_PASSWORD": c.GetString("db.password"),
        "POSTGRES_USER":     c.GetString("db.username"),
        "POSTGRES_DB":       c.GetString("db.database"),
    }
    var port = "5432/tcp"</code></pre><p></p><pre><code>    req := testcontainers.GenericContainerRequest{
        ContainerRequest: testcontainers.ContainerRequest{
            Image:        "postgres:14-alpine",
            ExposedPorts: []string{port},
            Env:          env,
            ShmSize:      134217728,
            WaitingFor:   wait.ForLog("database system is ready to accept connections"),
        },
        Started: true,
    }
    container, err := testcontainers.GenericContainer(ctx, req)
    if err != nil {
        return nil, container, fmt.Errorf("failed to start container: %v", err)
    }</code></pre><p></p><pre><code>    p, err := container.MappedPort(ctx, "5432")
    if err != nil {
        return nil, container, fmt.Errorf("failed to get container external port: %v", err)
    }</code></pre><p></p><pre><code>    host, err := container.Host(ctx)
    if err != nil || host == "" {
        host = "localhost"
    }</code></pre><p></p><pre><code>    //dbAddr := fmt.Sprintf("%s:%s", host, p.Port())</code></pre><p></p><pre><code>    dsn := fmt.Sprintf("user=%s password=%s host=%s port=%s dbname=%s search_path=%s sslmode=disable",
        c.GetString("db.username"),
        c.GetString("db.password"),
        host,
        p.Port(),
        c.GetString("db.database"),
        c.GetString("db.schema"))</code></pre><p></p><pre><code>    config, err := pgxpool.ParseConfig(dsn)
    if err != nil {
        return nil, container, err
    }
    config.MaxConns = int32(c.GetInt("db.maxconns"))
    config.MinConns = int32(c.GetInt("db.minconns"))
    config.MaxConnLifetime = time.Duration(c.GetInt("db.maxconnlifetime")) * time.Minute
    config.MaxConnIdleTime = time.Duration(c.GetInt("db.maxconnidletime")) * time.Minute
    retries := 0
    var db *pgxpool.Pool
    for retries &lt; 10 {
        db, _ = pgxpool.New(ctx, config.ConnString())</code></pre><p></p><pre><code>        err := db.Ping(ctx)
        if err == nil {
            log.Info(ctx, "Ping Successful....")
            break
        } else {
            db.Close()
        }</code></pre><p></p><pre><code>        retries++
        log.Info(ctx, "Ping attempt failed. Retrying... (Attempt %d/%d)\n", retries, 10)
        time.Sleep(1 * time.Second)
    }</code></pre><p></p><pre><code>    err = migrateDb(db, c)
    if err != nil {
        log.Fatal(ctx, "failed to perform db migration---&gt;&gt;&gt; %s", err)
    }</code></pre><p></p><pre><code>    return db, container, nil
}</code></pre><p></p><pre><code>func migrateDb(pool *pgxpool.Pool, c *config.Config) error {
    ctx := context.Background()
    _, path, _, ok := runtime.Caller(0)
    if !ok {
        return fmt.Errorf("failed to get path")
    }
    sqlPath := filepath.Join(filepath.Dir(path), "migration", "000001_init-setup.up.sql")
    sqlBytes, err := os.ReadFile(sqlPath)
    if err != nil {
        return fmt.Errorf("failed to read migration file: %w", err)
    }</code></pre><p></p><pre><code>    _, err = pool.Exec(ctx, string(sqlBytes))
    if err != nil {
        return fmt.Errorf("failed to execute migration sql: %w", err)
    }</code></pre><p></p><pre><code>    return nil
}</code></pre><p></p><pre><code>type FxTestControllerParam struct {
    fx.In
    Controllers []serverHandler.Handler `group:"servercontrollers"`
}</code></pre><p></p><pre><code>func ProvideTestEngine(cfg *config.Config, p FxTestControllerParam) *gin.Engine {
    gin.SetMode(gin.TestMode)
    engine := gin.Default()
    router.Setup(engine)
    registries := router.ParseControllers(p.Controllers...)
    r := router.NewRouter(engine, cfg, registries)
    r.RegisterRoutes()
    return engine
}</code></pre><p></p><pre><code>func teardownTestData() {
    if Container != nil {
        _ = Container.Terminate(context.Background())
    }
    if MinioContainer != nil {
        _ = MinioContainer.Terminate(context.Background())
    }
    if App != nil {
        App.RequireStop()
    }
}</code></pre><p></p><pre><code>var Container testcontainers.Container
var App *fxtest.App</code></pre><p></p><pre><code>func BootstrapTestApp(tb fxtest.TB, options ...fx.Option) *gin.Engine {
    opts := []fx.Option{
        fx.Provide(func() context.Context {
            return context.Background()
        }),
        fx.Provide(log.NewDefaultLoggerFactory),
        Fxconfig,
        FxDB,
        FxMinIO,
        FxTemporal,
        bootstrap.FxHandler,
        bootstrap.FxRepo,
        fx.Provide(ProvideTestEngine),
        fx.Populate(&amp;Router),
        fx.StartTimeout(60 * time.Second),
        fx.StopTimeout(60 * time.Second),
    }
    opts = append(opts, options...)</code></pre><p></p><pre><code>    App = fxtest.New(tb, opts...)
    App.RequireStart()</code></pre><p></p><pre><code>    return Router
}</code></pre><p></p><pre><code>// bootstrapTB adapts fxtest to TestMain, where no *testing.T exists yet.
// Using a zero-value &amp;testing.T{} here makes fxtest call FailNow, which runs
// runtime.Goexit() on the main goroutine and hangs the process forever with the
// real error buffered and never printed. Failing loudly via os.Exit avoids that.
type bootstrapTB struct{}</code></pre><p></p><pre><code>func (bootstrapTB) Logf(format string, args ...interface{}) {
    fmt.Fprintf(os.Stderr, format+"\n", args...)
}</code></pre><p></p><pre><code>func (bootstrapTB) Errorf(format string, args ...interface{}) {
    fmt.Fprintf(os.Stderr, format+"\n", args...)
}</code></pre><p></p><pre><code>func (bootstrapTB) FailNow() {
    fmt.Fprintln(os.Stderr, "failed to bootstrap test app")
    if Container != nil {
        _ = Container.Terminate(context.Background())
    }
    if MinioContainer != nil {
        _ = MinioContainer.Terminate(context.Background())
    }
    os.Exit(1)
}</code></pre><p></p><pre><code>func TestMain(m *testing.M) {
    Router = BootstrapTestApp(bootstrapTB{})
    code := m.Run()
    teardownTestData()
    os.Exit(code)
}</code></pre></th></tr></tbody></table></div>

2\. Update Test Assertions in Handler Tests:

After migrating to n-api-server, update your handler tests for the following assertion changes:

- Router Invocation Method: In legacy tests, Router was a wrapper struct; in n-api-server, Router is \*gin.Engine directly. Use Router.ServeHTTP(w, req) instead of Router.Engine.ServeHTTP(w, req).
- Validation Errors (422 Unprocessable Entity): In legacy Gin, validation failures returned 400 Bad Request. In n-api-server, compile-time validation failures return 422 Unprocessable Entity. Update your test assertions: assert.Equal(t, http.StatusUnprocessableEntity, w.Code).
- JSON Syntax Binding Errors (400 Bad Request): Malformed JSON payloads continue to return 400 Bad Request.
- URI Routes: Ensure test request URLs match the exact routes defined in the handler's Routes() method.

**💡 TEST EXECUTION NOTE:** Make sure Docker Desktop is running before running tests. Testcontainers requires a running Docker daemon to spin up the PostgreSQL container.

3\. Run the Test Suite:

```
go test -v -timeout 2m ./tests
```

## Running Application & Swagger Verification

1\. Add the following configuration in config.yaml in order to generate and host swagger docs:

```
swagger:
  generation:
    mode: "build"
  nullableTypeMap: | # optional where microservice uses nullable data types from libraries other than volatiletech/null
    {
      "null.String":  { "type": "string" },
      "null.Int":     { "type": "integer", "format": "int64" },
      "null.Int16":   { "type": "integer", "format": "int32" },
      "null.Int32":   { "type": "integer", "format": "int32" },
      "null.Int64":   { "type": "integer", "format": "int64" },
      "null.Float":   { "type": "number", "format": "double" },
      "null.Bool":    { "type": "boolean" },
      "null.Time":    { "type": "string", "format": "date-time" },
      "null.UUID":    { "type": "string", "format": "uuid" }
    }
```

**💡 NULLABLE TYPE MAP RULE:** The nullableTypeMap configuration block is required in config.yaml whenever your microservice uses nullable data types from libraries other than volatiletech/null (such as guregu/null). Without this mapping, n-api-server swagger generation may treat nullable fields as complex nested objects. The nullableTypeMap instructs the OpenAPI builder to cleanly map null.String, null.Int, null.Time, etc. directly to primitive OpenAPI types (string, integer, boolean, double, date-time). Note: The pipe character | (nullableTypeMap: |) is required so YAML treats the JSON block as a multi-line string scalar, as expected by n-api-server's configuration reader.

2\. Start your application server:

```
go run main.go
```

3\. OpenAPI / Swagger v3 specifications are generated automatically at:

<http://localhost:<PORT>/docs/v3Doc.json>

**4\. Common Swagger Generation Pitfalls & Troubleshooting:**

During application startup (or when running with swagger.generation.mode: "build"), n-api-server automatically inspects all registered handler routes and their generic request/response types. It first constructs OpenAPI v2 definitions and then converts them to OpenAPI v3 specifications using openapi2conv.ToV3. If any route references a schema type that failed to register in the definitions dictionary, the converter halts startup with a fatal error:

```
Error converting to v3: failed to resolve "..." in fragment in URI: "#/components/schemas/<StructName>": map key "<StructName>" not found
```

Below are the two most common root causes of this failure and how to resolve them:

**Pitfall 1: Using Pointer-to-Slice (\*\[\]Struct) in Response DTOs**

**• Symptom:** Error converting to v3: failed to resolve "..." in fragment in URI: "#/components/schemas/GetCarrierTrackingReportResponse": map key "GetCarrierTrackingReportResponse" not found

**• Root Cause:** In n-api-server/swagger/defs.go, schema reflection inspects direct structs (reflect.Struct), pointers to structs (\*Struct), and slices of structs (\[\]Struct). It omits pointers to slices of structs (\*\[\]Struct). Consequently, the inner struct type is never registered in the Swagger definitions dictionary, leaving a dangling reference during OpenAPI v3 conversion.

**• Resolution:** Replace \*\[\]Struct with a standard slice \[\]Struct. Slices in Go are already lightweight references containing a pointer to the backing array, length, and capacity.

<div class="joplin-table-wrapper"><table><tbody><tr><th><pre><code>// ❌ INCORRECT: Pointer to slice breaks Swagger schema registration
type AirlinesReportResponse struct {
    Data *[]GetCarrierTrackingReportResponse `json:"data"`
}</code></pre><p></p><pre><code>// ✅ CORRECT: Standard slice allows clean OpenAPI v3 schema registration
type AirlinesReportResponse struct {
    Data []GetCarrierTrackingReportResponse `json:"data"`
}</code></pre></th></tr></tbody></table></div>

**Pitfall 2: Declaring json:"-" on URI Path or Query Parameter Request Structs**

**• Symptom:** Error converting to v3: failed to resolve "..." in fragment in URI: "#/components/schemas/GetAirDispatchHandoverReportReq": map key "GetAirDispatchHandoverReportReq" not found

**• Root Cause:** In n-api-server/swagger/paths.go, the route scanner checks \`if f.Tag.Get("json") != "" { hasBody = true }\`. Because it checks only for non-emptiness rather than checking if the value is "-", any json tag flags the endpoint as having an HTTP request body referencing #/definitions/&lt;StructName&gt;. However, in n-api-server/swagger/defs.go, fields tagged with json:"-" are explicitly ignored. When all fields in the request struct have json:"-", zero schema properties are generated and the struct is never added to the definitions registry, resulting in a broken reference.

**• Resolution:** For request DTOs used exclusively for URI path parameters (uri:"...") or URL query parameters (form:"..."), omit the json tag entirely. Do NOT add json:"-".

<div class="joplin-table-wrapper"><table><tbody><tr><th><pre><code>// ❌ INCORRECT: json:"-" triggers Swagger body generation without schema definition
type AirDispatchHandoverReportRequest struct {
    ScheduleID   string `form:"schedule_id" json:"-"`
    ScheduleDate string `form:"schedule_date" json:"-"`
}</code></pre><p></p><pre><code>// ✅ CORRECT: Omit json tag entirely; use only form or uri tags
type AirDispatchHandoverReportRequest struct {
    ScheduleID   string `form:"schedule_id"`
    ScheduleDate string `form:"schedule_date"`
}</code></pre></th></tr></tbody></table></div>

**💡 SWAGGER TROUBLESHOOTING CHECKLIST:** When encountering \`map key "..." not found\` during Swagger generation:

1\. Check the referenced struct name in the fatal error message.

2\. If it is a Response DTO, verify whether any field uses a pointer to a slice (\*\[\]Struct) instead of a standard slice (\[\]Struct).

3\. If it is a Request DTO, verify whether any URI or query parameter fields contain json:"-". Remove the json tag completely.

4\. If nullable types from third-party libraries (e.g. guregu/null) are used, verify that swagger.nullableTypeMap is configured in config.yaml.

5\. For parameterless endpoints, ensure the handler method accepts \_ struct{} as the request type parameter.
