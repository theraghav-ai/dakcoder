package tests

import (
	"bytes"
	"io"
	"mime/multipart"
	"net/http"
	"net/http/httptest"
	"os"
	"testing"

	"github.com/stretchr/testify/assert"
)

//UploadFile---------------------------------------------------------------------------------------------------------------------------

func TestUploadFileHandler_Success(t *testing.T) {
	// Create a new request with a file and folderPath
	filePath := "../handler/dummy-pdf.pdf" // Update with the actual test file path
	file, err := os.Open(filePath)
	assert.NoError(t, err)
	defer file.Close()

	body := &bytes.Buffer{}
	writer := multipart.NewWriter(body)

	// Add folderPath to form data
	_ = writer.WriteField("folderPath", "pao-back-end/handler")

	// Add the file to form data
	part, err := writer.CreateFormFile("file", filePath)
	assert.NoError(t, err)
	_, err = io.Copy(part, file)
	assert.NoError(t, err)

	_ = writer.Close()

	// Create the request
	req := httptest.NewRequest("POST", "/v1/objection-file/upload", body)
	req.Header.Set("Content-Type", writer.FormDataContentType())
	rec := httptest.NewRecorder()

	// Perform the request
	Router.Engine.ServeHTTP(rec, req)

	// Assert success response
	assert.Equal(t, http.StatusOK, rec.Code)
}

func TestUploadFileHandler_MissingFile(t *testing.T) {
	// Create a new request without a file
	body := &bytes.Buffer{}
	writer := multipart.NewWriter(body)
	_ = writer.WriteField("folderPath", "pao-back-end/handler")
	_ = writer.Close()

	req := httptest.NewRequest("POST", "/v1/objection-file/upload", body)
	req.Header.Set("Content-Type", writer.FormDataContentType())
	rec := httptest.NewRecorder()

	Router.Engine.ServeHTTP(rec, req)

	// Assert error response for missing file
	assert.Equal(t, http.StatusBadRequest, rec.Code)
}

//FetchFileHandler--------------------------------------------------------------------------------------------------------

func TestDownloadFileHandler_Success(t *testing.T) {

	req := httptest.NewRequest("GET", "/v1/objection-file/download?key=pao-back-end/handler/dummy_pdf&type=pdf", nil)
	req.Header.Set("Content-Type", "application/json")
	rec := httptest.NewRecorder()
	Router.Engine.ServeHTTP(rec, req)

	assert.Equal(t, http.StatusOK, rec.Code)
}
func TestDownloadFileHandler_MissingKey(t *testing.T) {
	req := httptest.NewRequest("GET", "/v1/objection-file/download", nil) // No query parameter
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("X-User-ID", "test-user")

	rec := httptest.NewRecorder()

	Router.Engine.ServeHTTP(rec, req)

	// Expect 400 Bad Request
	assert.Equal(t, http.StatusInternalServerError, rec.Code)
}
func TestDownloadFileHandler_FilePathNotFOund(t *testing.T) {

	req := httptest.NewRequest("GET", "/v1/objection-file/download?key=&type=pdf", nil)
	req.Header.Set("Content-Type", "application/json")
	rec := httptest.NewRecorder()
	Router.Engine.ServeHTTP(rec, req)

	assert.Equal(t, http.StatusInternalServerError, rec.Code)
}
