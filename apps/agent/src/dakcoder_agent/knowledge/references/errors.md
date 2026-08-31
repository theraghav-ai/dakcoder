---
slug: errors
handle: "@skill:errors"
fetch_when: "returning an error, or deciding a status code"
sources:
  - "skill.md §Error Handling"
  - "SOP.md §[handler].go"
---

# Error handling

> **Generated.** Do not edit — run `make knowledge` and commit the result.
> Assembled from the reference template, so it cannot drift from what the linter enforces.

Log with context, then return. An error returned without a log line is invisible in production: the developer sees a 500 in Grafana with no trace of where it came from.

`pgx.ErrNoRows` is the one to be careful with — it has to reach the framework intact to become a 404, so do not wrap it with `%v` and do not swallow it in the repository.

Enforced by `error-handling`, and by `go-idiom` for `%w` versus `%v`.

## Error Handling

*From `skill.md` §Error Handling (lines 1345–1381).*

**In Handlers**:
```go
result, err := h.svc.SomeMethod(sctx.Ctx, params)
if err != nil {
    if err == pgx.ErrNoRows {
        sctx.Log.Error("resource not found", "id", id)
        return nil, err
    }
    sctx.Log.Error("failed to perform operation", "error", err)
    return nil, err
}
```

**In Repositories**:
```go
err := dblib.SelectOne(ctx, r.db, query, &result)
if err != nil {
    if err == pgx.ErrNoRows {
        return result, err // Let handler decide how to handle
    }
    return result, err
}
return result, nil
```

**Rules**:
- Always log errors before returning
- Use descriptive log messages
- Include relevant context in logs (IDs, parameters)
- Return errors directly (framework handles HTTP status codes)
- Check for `pgx.ErrNoRows` for 404 scenarios
- Don't wrap errors unnecessarily

---

## [handler].go

*From `SOP.md` §[handler].go (lines 19–162).*

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

##### Define the Routes

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

##### Handler with file upload
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
