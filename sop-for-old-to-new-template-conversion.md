SOP Template for API Template Conversion
This SOP provides a step-by-step technical guide for migrating backend microservices from the legacy template architecture to the updated modular template.
Major Changes Summary
• Generation of automated swagger docs (served natively at /docs/v3Doc.json)
• Generalized error handling and unified response formats
• Removing direct dependency on Gin framework in handlers and service layers
• Using DTOs for request binding and response objects
• Handler methods strictly accept request structs (raw slices/arrays in handler parameters are replaced with wrapper structs tagged with validate:"dive")
• Consolidating and moving all request DTO structs to a dedicated package at handler/request/request.go
• Removing ShouldBind and manual validation from handlers
• Commenting routes.go file and moving route registration inside individual handler files
• Enhanced stack trace readability with log levels configurable to DEBUG

Step 1: Branch Creation & Git Setup
Objective: Create an isolated feature branch to work on the template conversion safely without affecting main development.
Run the following commands (Example assuming working from development branch):

# Switch to development, pull latest changes, and create template-conversion branch

git checkout development
git pull origin development
git checkout -b template-conversion
git push -u origin template-conversion

Step 2: Dependencies (Updating go.mod)
Objective: Install new n-api-* template libraries and clean up unused legacy dependencies from go.mod.
Legacy Package New Modular Package Purpose / Notes
gitlab.cept.gov.in/it-2.0-common/api-bootstrapper gitlab.cept.gov.in/it-2.0-common/n-api-bootstrapper@latest Core app lifecycle & DI (provides *n-api-db.DB)
gitlab.cept.gov.in/it-2.0-common/api-server gitlab.cept.gov.in/it-2.0-common/n-api-server@latest HTTP server routing & controllers
gitlab.cept.gov.in/it-2.0-common/api-log gitlab.cept.gov.in/it-2.0-common/n-api-log@latest Structured zero-allocation logger
gitlab.cept.gov.in/it-2.0-common/api-errors gitlab.cept.gov.in/it-2.0-common/n-api-errors@latest Standardized application errors
gitlab.cept.gov.in/it-2.0-common/api-db gitlab.cept.gov.in/it-2.0-common/n-api-db@latest Database utilities, pools & query builders
gitlab.cept.gov.in/it-2.0-common/api-validation gitlab.cept.gov.in/it-2.0-common/n-api-validation@latest Compile-time validation engine (govalid)
gitlab.cept.gov.in/it-2.0-common/api-config gitlab.cept.gov.in/it-2.0-common/api-config (NO CHANGE) Configuration manager (stays as api-config)

Command to Execute:

# 1. Download updated template packages

go get gitlab.cept.gov.in/it-2.0-common/n-api-bootstrapper@latest
go get gitlab.cept.gov.in/it-2.0-common/n-api-server@latest
go get gitlab.cept.gov.in/it-2.0-common/n-api-log@latest
go get gitlab.cept.gov.in/it-2.0-common/n-api-validation@latest
go get github.com/bufbuild/protovalidate-go@v0.10.1
go get go.uber.org/fx@latest
go get github.com/jackc/pgx/v5@latest

# 2. Clean up obsolete dependencies

go mod tidy

⚠️ CRITICAL DEPENDENCY RULE: Always use n-api-bootstrapper (not the legacy api-bootstrapper). The legacy bootstrapper injects the old *api-db.DB type into Uber FX, causing dependency mismatches with repositories expecting *n-api-db.DB. Furthermore, ensure github.com/bufbuild/protovalidate-go is pinned to @v0.10.1 to maintain full interface compatibility with generated protobuf validators.

Step 3: Steps to follow (Code Conversion)
Note: The steps below must be applied to EVERY handler file in your microservice package (e.g., award_handler.go, transfer_handler.go, noc_handler.go, paogen.go, etc.).

1. Import the required server packages from n-api-server and request DTOs:
   import (
   serverHandler "gitlab.cept.gov.in/it-2.0-common/n-api-server/handler"
   serverRoute "gitlab.cept.gov.in/it-2.0-common/n-api-server/route"
   request "gotemplate/handler/request"
   )

2. Replace legacy logging and error imports with n-api-log and n-api-errors:
   Replace api-log and api-errors imports with their n-api-\* equivalents across all handler files:
   // Replace legacy api-log and api-errors imports:
   // Old: "gitlab.cept.gov.in/it-2.0-common/api-log"
   // New: log "gitlab.cept.gov.in/it-2.0-common/n-api-log"

// Old: "gitlab.cept.gov.in/it-2.0-common/api-errors"
// New: apierrors "gitlab.cept.gov.in/it-2.0-common/n-api-errors"

import (
apierrors "gitlab.cept.gov.in/it-2.0-common/n-api-errors"
log "gitlab.cept.gov.in/it-2.0-common/n-api-log"
)

3. Add serverHandler.Base to the handler struct (or implement serverHandler.Handler interface methods):
   // User"
   type SanctionHandler struct {
   *serverHandler.Base //newly added
   svc *repo.SanctionRepository
   cfg \*config.Config
   client client.Client
   }

4. In the constructor function, initialize the Base field using SetPrefix and AddPrefix:
   func NewAwardsHandler(svc *repo.AwardRepository) *AwardHandler {
   base := serverHandler.New("Awards").SetPrefix("/v1").AddPrefix("/awards")
   return &AwardHandler{
   Base: base,
   svc: svc,
   }
   }

5. Register the routes using Routes() method for the handler struct:
   func (c \*AwardHandler) Routes() []serverRoute.Route {
   return []serverRoute.Route{
   serverRoute.POST("/award-makers", c.CreateAwardsBulk).Name("Create Awards Bulk"),
   serverRoute.PUT("/award-makers/:award-id", c.UpdateAwards).Name("Update Awards"),
   serverRoute.GET("/award-makers", c.GetMakerAwards).Name("Get Maker Awards"),
   serverRoute.POST("/award-makers/approve-bulk", c.ApproveAwards).Name("Approve Awards"),
   serverRoute.GET("/awards", c.GetAwards).Name("Get Awards"),
   }
   }

⚠️ ROUTE PREFIXING RULE: In the legacy template, Gin route groups (e.g. v1 := r.Group("/pao-gen")) automatically attached prefixes. In n-api-server, route paths are mounted exactly as written in Routes(). If not using SetPrefix(), you must explicitly include the full prefix path on every route (e.g. serverRoute.GET("/pao-gen/office-names/:id", ...), serverRoute.POST("/transfer-entry", ...)).

6. Remove dependency on gin framework (*gin.Context) from function signatures and use *serverRoute.Context with DTO parameters (from request package):
   func (ah *AwardHandler) CreateAwardsBulk(sctx *serverRoute.Context, req request.CreateAwardsReq) (\*response.AwardsBulkCreateResponse, error) {
   // Access context via sctx.Ctx
   // req is already binded and validated
   return &response.AwardsBulkCreateResponse{...}, nil
   }

7. Handling Bulk / Array Request Payloads (Using Struct with validate:"dive"):
   ⚠️ CRITICAL HANDLER SIGNATURE RULE: Handler functions/methods CANNOT take raw slices or arrays directly as input parameters (e.g. req []request.SomeStruct is strictly prohibited). n-api-server requires all request inputs to implement the route.Validator interface (Validate() error). In Go, methods cannot be defined on unnamed slice types ([]T), causing govalid to skip them and the server to throw a 'request must implement Validator.Validate()' runtime error. If an endpoint requires an array payload, developers MUST define a wrapper struct in handler/request/request.go with a slice field tagged with validate:"dive" and pass that struct into the handler.

Define the wrapper struct in handler/request/request.go and update the handler function accordingly:
// 1. In handler/request/request.go:
type TransferEntryRequests struct {
TransferEntries []TransferEntryRequest `json:"transfer_entries" validate:"dive"`
}

// 2. In handler function:
// ❌ BEFORE (Raw slice parameter - throws 'request must implement Validator.Validate()'):
func (uh *TransferEntryHandler) CreateTransferEntryHandler(sctx *serverRoute.Context, req []request.TransferEntryRequest) (\*response.GetTransferentryCreationResponse, error) {
var request []domain.TransferEntryRequest
for \_, requ := range req {
// ...
}
}

// ✅ AFTER (Wrapper struct with validate:"dive" - validated at compile-time):
func (uh *TransferEntryHandler) CreateTransferEntryHandler(sctx *serverRoute.Context, req request.TransferEntryRequests) (\*response.GetTransferentryCreationResponse, error) {
var request []domain.TransferEntryRequest
for \_, requ := range req.TransferEntries {
// ...
}
}

Expected Client JSON Payload Format (sending an object with an array field):
// 3. Client JSON Payload:
{
"transfer_entries": [
{
"pao_code": "099999",
"ddo_code": "999991",
"hoa": "320108101030228",
"transfer_amount": 110,
"transfer_type": "D",
"created_by": 10257696,
"created_date": "2024-04-06",
"te_source_office_type": "PAO",
"remarks": "created",
"trans_date": "2024-04-06"
}
]
}

8. Query Parameters & Pagination DTO Tagging Rule (MetaDataRequest):
   For HTTP GET requests and pagination/sorting metadata (e.g. MetaDataRequest), always tag struct fields with form: instead of json:. In n-api-server, query parameters are extracted via form binding. If tagged with json:, query values like order_by, sort_type, and total_records_required will be ignored:
   // handler/request/request.go
   package request

// ❌ INCORRECT (Query params will NOT bind in GET requests):
type MetaDataRequest struct {
Skip uint64 `form:"skip,default=0" validate:"omitempty"`
Limit uint64 `form:"limit,default=10" validate:"omitempty"`
OrderBy string `json:"order_by,omitempty" example:"id, name"`
SortType string `json:"sort_type,omitempty"`
TotalRecordsRequired bool `json:"total_records_required,omitempty"`
}

// ✅ CORRECT (All query fields bound via form tags):
type MetaDataRequest struct {
Skip uint64 `form:"skip,default=0" validate:"omitempty"`
Limit uint64 `form:"limit,default=10" validate:"omitempty"`
OrderBy string `form:"order_by,omitempty" example:"id, name"`
SortType string `form:"sort_type,omitempty"`
TotalRecordsRequired bool `form:"total_records_required,omitempty"`
}

9. Unified Error Handling & Single Return Pattern:
   Always use apierrors.NewAppError to return structured API errors with proper HTTP status codes. Ensure handlers have exactly one return statement per error branch to satisfy go vet checks:
   // ✅ Correct Single Return Pattern:
   if err != nil {
   log.Error(sctx, "Database query failed: %s", err.Error())
   appErr := apierrors.NewAppError("Failed to retrieve record", http.StatusInternalServerError, err)
   return nil, &appErr
   }

Handler with File Upload

1. For file upload, define form fields and \*multipart.FileHeader in your request DTO (in handler/request/request.go):
   // handler/request/request.go
   package request

type CreateAwardsReq struct {
EmployeeID string `form:"employee_id" validate:"required"`
Data string `form:"data" validate:"required"`
SingleFile *multipart.FileHeader `form:"single_file" validate:"required"`
Files []*multipart.FileHeader `form:"files" validate:"required"`
}

2. Access the file directly using req.SingleFile.Open() inside the handler:
   file, err := req.SingleFile.Open()
   if err != nil {
   return nil, fmt.Errorf("file couldn't be opened")
   }
   defer file.Close()

fileSize := req.SingleFile.Size
fileName := req.SingleFile.Filename

File Response Handling
Use port.FileResponse to send byte arrays or streamed files:
// 1. File as byte array
res := port.FileResponse{
ContentType: "application/zip",
ContentDisposition: "attachment; filename=\"pisdocuments.zip\"",
Data: buf.Bytes(),
}
return &res, nil

// 2. File as stream (io.Reader)
resStream := port.FileResponse{
ContentType: "application/pdf",
ContentDisposition: "inline; filename=\"sample.pdf\"",
Reader: fileReaderObject,
}
return &resStream, nil

bootstrap/bootstrapper.go
Objective: Register all handler modules in the Uber FX dependency injection container.
var FxHandler = fx.Module(
"Handlermodule",
fx.Provide(
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
),
)

💡 GRPC SERVICE RULE: If gRPC services are not implemented or are commented out in the microservice, make sure to comment out AddHandlers and bootstrapper.FxGrpc in main.go to avoid missing dependency errors (\*grpcserver.HandlerRegistry).

Validation Setup (govalid)
Objective: Generate automatic compile-time request validation code.

1. Move and Consolidate Request Structs in handler/request/request.go:
   Extract and move all request DTO structs (from individual handler files or legacy locations) into a single centralized file: handler/request/request.go under package request. Update all handler files to import this package (import request "<module>/handler/request") and use req request.<StructName>.
2. Handling Validation Rules & Custom Validations:
   n-api-validation (govalid) handles validation at compile-time by generating Go code from struct tags. The legacy validator.go file, runtime registration (RegisterCustomValidation / NewValidatorService), and the bootstrap.Fxvalidator module are all removed. Validation is now fully automated — no manual registration needed.
   Legacy Custom Tag govalid Built-in Replacement Rule Description
   validatePaocode / validateDdocode validate:"required,len=6,numeric" Must be exactly 6 numeric digits
   validatePeriod validate:"required,len=6,numeric" Period code (MMYYYY) 6 numeric characters
   validateDateTime validate:"required,date_yyyy_mm_dd" Date format YYYY-MM-DD
   allotamount validate:"gte=0" Positive amount (>= 0)
   percent validate:"gte=0,lte=100" Percentage value between 0 and 100
   vendor_id_len validate:"gte=1000000000,lte=9999999999" Range for exactly 10 digits
   status validate:"len=2,numeric" 2-digit status code
   employee_id / office_id validate:"required,employee_id" Built-in government ID format validator
   head_of_account / account_no validate:"required,head_of_account" Accounting Head of Account format

Built-in Markers (Standard Rules): Use concise tags for range, length, and options. For a complete list of supported markers, refer to the n-api-validation README (https://gitlab.cept.gov.in/it-2.0-common/n-api-validation).

- Positive Amount (>= 0): validate:"gte=0"
- Percentage (0 - 100): validate:"gte=0,lte=100"
- Exact Length: validate:"len=2,numeric"
  CEL Expressions (Custom Format Rules): Use validate:"cel=..." for custom regex or inline expressions.
- HH:MM Format: validate:"cel=value.matches('^([01]\\d|2[0-3]):([0-5]\\d)$')"
  Complex Business Validation & Custom Validation Rules:
  For simple validations, use built-in tags or CEL expressions. For custom domain logic that must be re-used across DTOs, define a Custom Validation Rule using the
  // +govalid:rule marker.

1. Definition: Define the validation function in package request (in handler/request/request.go or a helper file inside handler/request/).
2. Signature: The validation function must be a package-level function (not a method), cannot use generics, accept exactly one parameter (matching the struct field type), and return exactly bool.
3. Usage: Document the function with // +govalid:rule=rule_name and tag the field in your DTO with validate:"rule_name".
   // handler/request/request.go
   package request

type AllTags struct {
GrossAmount float64 `json:"gross_amount" validate:"omitempty,is_positive"`
}

// +govalid:rule=is_positive
// +govalid:message=value must be strictly positive
func IsPositive(f float64) bool {
return f > 0
}

Complex business validation belongs in the handler/service layer. Example: Validate vendor ID length in handler before processing:
// Complex business validation belongs in the handler/service layer.
// Example: Validate vendor ID length in handler before processing.
func (h *SanctionHandler) validateBusinessRules(req *request.SanctionTemp) error {
if req.VendorID != 0 {
if len(strconv.FormatUint(req.VendorID, 10)) != 10 {
return errors.New("Vendor ID must be exactly 10 digits")
}
}
return nil
}

⚠️ CRITICAL GOVALID RULE: Do NOT define a Validate() method on request DTO structs. govalid auto-generates Validate() methods and your custom one will conflict with it at compile time.

⚠️ MANDATORY VALIDATION TAG RULE: Every request DTO struct MUST contain at least one field tagged with validation (such as validate:"omitempty" or specific rules). If a struct has no validation tags on any field, govalid will skip generating its Validate() method, causing n-api-server to throw a 'validator not implemented' runtime error during request binding.

3. Install govalid tool and generate validation code:
   go install gitlab.cept.gov.in/it-2.0-common/n-api-validation/cmd/govalid@latest
   cd handler/request
   govalid ./request.go

main.go
Objective: Clean up main function since routes are auto-registered by the handler module.

1. Remove `fx.Invoke(routes.Routes)` from `main.go`.
2. Remove `bootstrap.Fxvalidator` from `main.go`.
   package main

import (
"context"
"gotemplate/bootstrap"
bootstrapper "gitlab.cept.gov.in/it-2.0-common/n-api-bootstrapper"
)

func main() {
app := bootstrapper.New().Options(
bootstrap.FxHandler,
bootstrap.FxRepo,
bootstrapper.FxMinIO,
bootstrap.Fxtemporal,
)
app.WithContext(context.Background()).Run()
}

../core/port/response.go
Copy the functions given below and paste them in response.go folder
func (s StatusCodeAndMessage) Status() int {
return s.StatusCode
}

func (s StatusCodeAndMessage) ResponseType() string {
return "standard"
}

func (s StatusCodeAndMessage) GetContentType() string {
return "application/json"
}

func (s StatusCodeAndMessage) GetContentDisposition() string {
return ""
}

func (s StatusCodeAndMessage) Object() []byte {
return nil
}

Step 4: Test Suite Setup & Updates (tests/testmain_test.go)
Objective: Modernize the test harness to support n-api-server, Uber FX controller auto-discovery, Docker testcontainers (PostgreSQL + MinIO), and non-blocking Temporal clients.

1. Overwrite tests/testmain_test.go with the Generic Test Harness:
   Replace your legacy tests/testmain_test.go with the universal N-API test harness. When copying into your microservice, simply update the bootstrap import path (e.g., "gotemplate/bootstrap" -> "<your-module>/bootstrap"):
   package tests

import (
"context"
"fmt"
"os"
"path/filepath"
"runtime"
"testing"
"time"

    "gotemplate/bootstrap" // Update with your module name

    "github.com/gin-gonic/gin"
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

)

var Router *gin.Engine
var Container testcontainers.Container
var MinioContainer *tcminio.MinioContainer
var App \*fxtest.App

var Fxconfig = fx.Module("configmodule", fx.Provide(config.NewDefaultConfigFactory, newFxConfig))

type FxConfigParam struct {
fx.In
Factory config.ConfigFactory
}

func newFxConfig(p FxConfigParam) (\*config.Config, error) {
return p.Factory.Create(config.WithFileName("config"), config.WithFilePaths(".", "../configs"))
}

var FxDB = fx.Module("DBModule", fx.Provide(SetUpDB))

var FxMinIO = fx.Module(
"MinIOModule",
fx.Provide(func(ctx context.Context, cfg *config.Config) (*minio.Client, \*tcminio.MinioContainer, error) {
return SetUpMinio(ctx, cfg)
}),
)

var FxTemporal = fx.Module(
"TemporalModule",
fx.Provide(func(c \*config.Config) (tclient.Client, error) {
host := c.GetString("temporal.host")
port := c.GetString("temporal.port")
if host != "" && port != "" {
return tclient.NewLazyClient(tclient.Options{HostPort: host + ":" + port, Namespace: "default"})
}
return nil, nil
}),
)

type FxTestControllerParam struct {
fx.In
Controllers []serverHandler.Handler `group:"servercontrollers"`
}

func ProvideTestEngine(cfg *config.Config, p FxTestControllerParam) *gin.Engine {
gin.SetMode(gin.TestMode)
engine := gin.Default()
router.Setup(engine)
registries := router.ParseControllers(p.Controllers...)
r := router.NewRouter(engine, cfg, registries)
r.RegisterRoutes()
return engine
}

func BootstrapTestApp(tb fxtest.TB, options ...fx.Option) _gin.Engine {
opts := []fx.Option{
fx.Provide(func() context.Context { return context.Background() }),
fx.Provide(log.NewDefaultLoggerFactory),
Fxconfig,
FxDB,
FxMinIO,
FxTemporal,
bootstrap.FxHandler,
bootstrap.FxRepo,
fx.Provide(ProvideTestEngine),
fx.Populate(&Router),
fx.StartTimeout(60 _ time.Second),
fx.StopTimeout(60 \* time.Second),
}
opts = append(opts, options...)
App = fxtest.New(tb, opts...)
App.RequireStart()
return Router
}

func TestMain(m \*testing.M) {
Router = BootstrapTestApp(bootstrapTB{})
code := m.Run()
teardownTestData()
os.Exit(code)
}

2. Update Test Assertions in Handler Tests:
   After migrating to n-api-server, update your handler tests for the following changes:
   - Router Invocation Method: In legacy tests, Router was a wrapper struct accessed via Router.Engine.ServeHTTP(rec, req). In the updated test harness, Router is directly a \*gin.Engine, so update all test calls from Router.Engine.ServeHTTP(rec, req) to Router.ServeHTTP(rec, req).
   - Validation Errors (422 Unprocessable Entity): In legacy Gin, validation errors returned 400 Bad Request. In n-api-server with govalid, failed field validations (e.g. invalid length, regex mismatch, missing required field) return 422 Unprocessable Entity. Update your validation test cases (e.g. Test\*Handler_ValidationError) to assert http.StatusUnprocessableEntity (422).
   - JSON Syntax Binding Errors (400 Bad Request): Malformed JSON payloads continue to return 400 Bad Request.
   - URI Routes: Ensure test request URLs match the exact routes defined in the handler's Routes() method.
     💡 TEST EXECUTION NOTE: Make sure Docker Desktop is running before executing tests. Testcontainers automatically starts isolated PostgreSQL and MinIO containers for the duration of the test run and cleans them up upon completion.

3. Run the Test Suite:
   go test -v -timeout 2m ./tests

Running Application & Swagger Verification

1. Add the following configuration in config.yaml in order to generate auto swagger:
   swagger:
   generation:
   mode: "build"

2. Start your application server:
   go run main.go

3. OpenAPI / Swagger v3 specifications are generated automatically at:
   http://localhost:<PORT>/docs/v3Doc.json
