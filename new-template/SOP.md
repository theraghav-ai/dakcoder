# SOP Template 

This SOP helps in standardizing the API Template.
The major changes are:
- Generation of automated swagger docs
- Generalized error handling
- Removing dependency on gin framework in the handler and other service layers
- Using DTOs for request and response objects
- Removing shouldbind and validation from handler
- Removing routes.go file and moving all the routes to their individual handler files
- Better readability of Panic and error stack traces with log level set to debug

## Dependencies

- Update bootstrapper library to latest version

## Steps to follow

### [handler].go

1. Import the required packages.
```go
import (
    serverHandler "gitlab.cept.gov.in/it-2.0-common/api-server/handler"
	serverRoute "gitlab.cept.gov.in/it-2.0-common/api-server/route"
)
```

2. Add `serverHandler.Base` to the handler struct.
```go
type AwardHandler struct {
	*serverHandler.Base
	svc *repo.AwardRepository
}
```
3. In the constructor function, initialize the Base field.
    - In the below code, `SetPrefix` is used to set the version of the API and `AddPrefix` is used to set any additional prefix for the routes (AddPrefix is similar to using `Group` in gin).

    - All the routes defined in this handler will have the prefix `/v1/awards`.

    - AddPrefix can be empty string `""` if no additional prefix is required.
```go
func NewAwardsHandler(svc *repo.AwardRepository) *AwardHandler {
	base := serverHandler.New("Awards").SetPrefix("/v1").AddPrefix("/awards")
	return &AwardHandler{
		base,
		svc,
	}
}

```
4. Register the routes using `Routes()` method for the handler struct.

```go
func (c *AwardHandler) Routes() []serverRoute.Route {
	return []serverRoute.Route{
		
	}
}
```

#### Define the Routes

1. In the `Routes()` function of the handler, define the routes as shown below.
    - The first parameter is the route path.
    - The second parameter is the handler function.
    - The `Name` method is used to set the name of the route which will be used in the swagger docs.
```go
func (c *AwardHandler) Routes() []serverRoute.Route {
	return []serverRoute.Route{
		serverRoute.POST("/award-makers", c.CreateAwardsBulk).Name("Create Awards Bulk"),
		serverRoute.PUT("/award-makers/:award-id", c.UpdateAwards).Name("Update Awards"),
		serverRoute.GET("/award-makers", c.GetMakerAwards).Name("Get Maker Awards"),
		serverRoute.POST("/award-makers/approve-bulk", c.ApproveAwards).Name("Approve Awards"),
		serverRoute.GET("/awards", c.GetAwards).Name("Get Awards"),
	}
}
```

5. Remove dependency on gin framework `*gin.Context` from function signatures and add the request and response DTOs as parameters.
    - The `sctx *serverRoute.Context` parameter provides access to context.Context, and `req CreateAwardsReq` is the same request struct used earlier with `ShouldBind`.

    - The `sctx` parameter can be used to get the  context using `sctx.Context`.

    - The return values are the response struct pointer and error.

    - The response struct is the same struct used earlier to send the response using `handleSuccess()`.
    - The response struct is the same struct used earlier to send the response using `handleSuccess()`.
```go
func (ah *AwardHandler) CreateAwardsBulk(sctx *serverRoute.Context, req CreateAwardsReq) (*response.AwardsBulkCreateResponse, error) {
	// Implementation goes here
}

```

6. In the hndler function now you can directly use the request struct `req` to access the request data as it is already binded and validated.

7. Now you can return the erros directly using `return nil, err` from the handler function.

8. To send a successful response, return the response struct pointer and nil error like `return &response.AwardsBulkCreateResponse{...}, nil`.

7. For a successful response the message and status code will be picked from `StatusCodeAndMessage` set in the response struct.

8. For an error response the following order will be used to determine the status code and message:
   - If the error is of type `*pg.Error`, then the status code and message from the error will be used, so the error from Repo can be returned as is.
    - Example:
    ```go
    	_, err := ah.svc.ApproveAwardsQry(sctx.Ctx, req.AwardIDs, req.ApprovedBy, req.ApproveStatus, req.ApproverRemarks)
    if err != nil {
        log.Error(sctx.Ctx, "Error creating awards in bulk: %s", err)
        return nil, err
    }
    ```
   - If you want to set a custom status code and message, for your error, you can use `apierrors.HandleErrorWithStatusCodeAndMessage`
    - Example:
    ```go
    	_, err := ah.svc.ApproveAwardsQry(sctx.Ctx, req.AwardIDs, req.ApprovedBy, req.ApproveStatus, req.ApproverRemarks)
    if err != nil {
			errMsg := apierrors.HandleErrorWithStatusCodeAndMessage(apierrors.HTTPErrorNotFound, "No employee awards found for processing", err)
			return nil, errMsg
	
		log.Error(sctx.Ctx, "Error approving/rejecting awards: %s", err.Error())
		return nil, err
	}
    ```
    - If the error is a standard error and if it does not fall into the above categories, then the status code will be 500 and the message will be "Internal Server Error".

#### Handler with file upload
1. For file upload, the request struct will will have all the form fields and the file field as `*multipart.FileHeader`.
    - for a single file upload use `*multipart.FileHeader`
    - for multiple file upload use `[]*multipart.FileHeader`
    - you can use validation tags as required as well.
```go
type CreateAwardsReq struct {
	EmployeeID string                  `form:"employee_id" validate:"required"`
	Data       string                  `form:"data" validate:"required"`
	SingleFile *multipart.FileHeader   `form:"single_file" validate:"required"`
	Files      []*multipart.FileHeader `form:"files" validate:"required"`
}
```
2. If you are using json object in the form field then you can handle it by unmarshalling it in the handler function.
```go
	var subreq EmpNocCreateRequest
	// Unmarshal JSON data into req
	if err := json.Unmarshal([]byte(req.Data), &subreq); err != nil {
		log.Error(sctx.Ctx, "Unmarshall Error: ", err.Error())
		return nil, err
	}
```
3. In the handler function, you can access the file header from the request struct and use `Open()` method to get the file and other file metadata can also be accessed.
```go
	file, err := req.File.Open()
	if err != nil {
		return nil, fmt.Errorf("file couldn't be opened")
	}
	defer file.Close()
    // Get file size
    fileSize := req.File.Size
    // Get file name
    fileName := req.File.Filename
```

### File as a Response
1. If you want to send a file as a response, then you have two ways to do it.
    - If the file is small and can be sent as a byte array, then you can use the response struct to send the file as a byte array.
    - If the file is large then you can send the file as a stream.

#### File as byte array in response struct
1. Use the response struct as `port.FileResponse` to send the file as a byte array.
    -  assign a content type, content disposition and the file data as byte array to the struct fields.
```go
	res := port.FileResponse{
		ContentType:        "application/zip",
		ContentDisposition: "attachment; filename=\"pisdocuments.zip\"",
		Data:               buf.Bytes(), // type []byte
	}

    return &res, nil
```
#### File as a stream
1. Use the response struct as `port.FileResponse` to send the file as a stream.
    - assign a content type, content disposition and the file stream to the struct fields.
```go

    // Here object is of type io.Reader
    object, err := emh.dr.DownloadFile(document.DocumentFilePath)
	if err != nil {
		nil, err
	}

    res := port.FileResponse{
        ContentType:        "application/pdf",
        ContentDisposition: "inline; filename=\"sample.pdf\"",
        Reader:             object,
    }

    return &res, nil
```


### bootstrap/bootstrapper.go
1. add the handlers to the bootstrapper as shown.
```go


var FxHandler = fx.Module(
	"Handlermodule",
	fx.Provide(
		fx.Annotate(
			handler.NewTransferHandler,
			fx.As(new(serverHandler.Handler)),
			fx.ResultTags(serverHandler.serverControllersGroupTag),
		),
		fx.Annotate(
			handler.NewNocHandler,
			fx.As(new(serverHandler.Handler)),
			fx.ResultTags(serverHandler.serverControllersGroupTag),
		),
		
	),
)
```
### Validation
1. For request validation use the `validate` tags in the request struct fields.
2. Install govalid latest version `go install gitlab.cept.gov.in/it-2.0-common/n-api-validation/cmd/govalid@latest`.
3. Place all the request structs in a separate file named `request.go` in the handler package.
4. Run the command `govalid ./request.go` to generate the validation code.
5. This will generate a file in the same package.
2. The validation will be automatically handled before calling the handler function.
3. If the validation fails, a 400 Bad Request error will be returned with the validation error message.


### main.go

- From the main function remove `fx.Invoke(routes.Routes)`, as the routes are now registered in the handler itself.


## Running the application and Swagger Docs

- Run the application using `go run main.go`
- After the application is running, you can check for swagger docs generated at `/docs/v3Doc.json`
- Copy the file contents and paste it in [Swagger Editor](https://editor.swagger.io/) to view the docs.
- Use the same file to generate the Typescript client SDK.

