---
slug: file-upload
handle: "@skill:file-upload"
fetch_when: "a route that accepts an upload or returns a file"
sources:
  - "SOP.md §Handler with file upload"
  - "SOP.md §File as a Response"
---

# File upload and file responses

> **Generated.** Do not edit — run `make knowledge` and commit the result.
> Assembled from the reference template, so it cannot drift from what the linter enforces.

Uploads arrive as `*multipart.FileHeader` fields on the request DTO with `form:` tags — one for a single file, a slice for many. Responses use `port.FileResponse`, either with the bytes in `Data` or with an `io.ReadCloser` in `Reader` for anything large enough to be worth streaming.

## Handler with file upload

*From `SOP.md` §Handler with file upload (lines 128–162).*

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

## File as a Response

*From `SOP.md` §File as a Response (lines 163–200).*

1. If you want to send a file as a response, then you have two ways to do it.
    - If the file is small and can be sent as a byte array, then you can use the response struct to send the file as a byte array.
    - If the file is large then you can send the file as a stream.

##### File as byte array in response struct
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
##### File as a stream
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
