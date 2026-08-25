package handler

import (
	"context"
	"errors"

	config "gitlab.cept.gov.in/it-2.0-common/api-config"

	// "gotemplate/config"
	"mime"
	"mime/multipart"
	"net/http"
	"path"
	"path/filepath"
	"strings"

	repo "gotemplate/repo/postgres"

	apierrors "gitlab.cept.gov.in/it-2.0-common/api-errors"
	log "gitlab.cept.gov.in/it-2.0-common/api-log"
	validation "gitlab.cept.gov.in/it-2.0-common/api-validation"

	"github.com/gin-gonic/gin"

	"github.com/minio/minio-go/v7"
)

type FileHandler struct {
	svc         *repo.ObjectionFileRepository
	Config      *config.Config
	MinioClient *minio.Client
}

// NewUserHandler creates a new UserHandler instance
func NewObjectionFileHandler(svc *repo.ObjectionFileRepository, Config *config.Config, MinioClient *minio.Client) *FileHandler {
	return &FileHandler{
		svc,
		Config,
		MinioClient,
	}
}

// UploadFileHandler godoc
//
//	@Summary		Upload a file to the server
//	@Description	Uploads a file to the specified folder path on the server. The file must be of type JPG or PDF and should not exceed 1MB in size.
//	@Tags			FILE
//	@Accept			multipart/form-data
//	@Produce		json
//	@Param			folderPath	formData	string	true	"Destination folder path"
//	@Param			file		formData	file	true	"File to upload (PDF OR JPG OR JPEG ONLY)"
//	@Success		200		{object}	map[string]string	"File uploaded successfully"
//	@Failure		400					{object}	apierrors.APIErrorResponse						"Validation error"
//	@Failure		401					{object}	apierrors.APIErrorResponse						"Unauthorized error"
//	@Failure		403					{object}	apierrors.APIErrorResponse						"Forbidden error"
//	@Failure		404					{object}	apierrors.APIErrorResponse						"Data not found error"
//	@Failure		409					{object}	apierrors.APIErrorResponse						"Data conflict error"
//	@Failure		500					{object}	apierrors.APIErrorResponse						"Internal server error"
//	@Router			/v1/objection-file/upload [post]
func (fh *FileHandler) UploadFileHandler(c *gin.Context) {
	folderPath := c.PostForm("folderPath")
	file, header, err := c.Request.FormFile("file")
	if err != nil {
		c.String(http.StatusBadRequest, "Bad request")
		return
	}

	if header.Size <= 0 {
		c.JSON(http.StatusBadRequest, gin.H{"error": "Uploaded file is empty"})
		return
	}

	maxAllowedFileSize := int64(1 * 1024 * 1024) // 1MB
	if header.Size > maxAllowedFileSize {
		c.JSON(http.StatusBadRequest, gin.H{"error": "File size should be less than 1MB"})
		return
	}

	allowedExtensions := map[string]bool{"jpg": true, "pdf": true, "jpeg": true}
	ext := filepath.Ext(header.Filename)
	if !allowedExtensions[strings.ToLower(strings.TrimPrefix(ext, "."))] {
		c.JSON(http.StatusBadRequest, gin.H{"error": "Invalid file type. Only JPG and PDF are allowed"})
		return
	}

	defer func(file multipart.File) {
		err := file.Close()
		if err != nil {
			log.Error(c, "Error in closing file: %s", err.Error())
		}
	}(file)

	filename := strings.ToLower(header.Filename)
	filename = strings.ReplaceAll(filename, " ", "_")
	filename = strings.ReplaceAll(filename, "-", "_")

	objectName := path.Join(folderPath, filename)

	err = fh.svc.UploadFile(c, file, objectName, header.Header.Get("Content-Type"), header.Size)
	if err != nil {
		log.Error(c, err)
		apierrors.HandleDBError(c, err)
		log.Error(c, "Upload File Repo call failed: %s", err.Error())
		return
	}

	defer c.Request.MultipartForm.RemoveAll() // Clean up temporary files
	// c.JSON(http.StatusOK, gin.H{"key": objectName})
	handleSuccessDoc(c, objectName)

}

type FileType struct {
	Type string `form:"type" validate:"required,oneof=pdf jpg jpeg xml"`
}

// FetchFileHandler godoc
//
//	@Summary		Retrieve a file from the server
//	@Description	Fetches a file from the specified storage bucket using the file key. The file is returned with the appropriate content type and can be viewed inline or downloaded.
//	@Tags			FILE
//	@Accept			json
//	@Produce		application/octet-stream
//	@Param			key	query		string	true	"File key"
//	@Param			type	query		string			true	"Type"
//	@Success		200	{file}		file		"File retrieved successfully"
//	@Failure		400					{object}	apierrors.APIErrorResponse						"Validation error"
//	@Failure		401					{object}	apierrors.APIErrorResponse						"Unauthorized error"
//	@Failure		403					{object}	apierrors.APIErrorResponse						"Forbidden error"
//	@Failure		404					{object}	apierrors.APIErrorResponse						"Data not found error"
//	@Failure		409					{object}	apierrors.APIErrorResponse						"Data conflict error"
//	@Failure		500					{object}	apierrors.APIErrorResponse						"Internal server error"
//	@Router			/v1/objection-file/download [get]
func (fh *FileHandler) FetchFileHandler(ctx *gin.Context) {

	filePath := ctx.Query("key")
	if filePath == "" {
		err := errors.New("file key error")
		appError := apierrors.NewAppError(
			"file key is required", // User-friendly error message
			"500",                  // Error code representing the error type
			err,                    // Original error for debugging purposes
		)
		apiErrorResponse := apierrors.NewAPIErrorResponse(
			http.StatusInternalServerError, // HTTP status code
			"Internal Server Error",        // Message to return to the client
			appError,                       // Encapsulated application error
		)
		ctx.JSON(apiErrorResponse.StatusCode, apiErrorResponse)
		log.Error(ctx, "File key error: %s", err.Error())
		return
	}
	var req FileType
	if err1 := ctx.ShouldBindQuery(&req); err1 != nil {
		apierrors.HandleBindingError(ctx, err1)
		log.Error(ctx, "Binding failed for DdoListRequest: %s", err1.Error())
		return
	}
	if err := validation.ValidateStruct(req); err != nil {
		apierrors.HandleValidationError(ctx, err)
		log.Error(ctx, "Validation failed for DdoListRequest: %s", err.Error())
		return
	}
	if req.Type == "pdf" {
		filePath += ".pdf"
	}
	if req.Type == "jpg" {
		filePath += ".jpg"
	}
	if req.Type == "jpeg" {
		filePath += ".jpeg"
	}
	if req.Type == "xml" {
		filePath = "pfms/" + filePath + ".xml"
	}

	// var Config *config.Config
	bucketName := fh.Config.GetString("minio.bucketName")
	// bucketName := "pao"
	obj, err := fh.MinioClient.GetObject(context.Background(), bucketName, filePath, minio.GetObjectOptions{})
	if err != nil {
		apierrors.HandleError(ctx, err)
		return
	}

	defer obj.Close() // Ensure the object is closed after use

	_, err = obj.Stat()
	if err != nil {
		if minio.ToErrorResponse(err).Code == "NoSuchKey" {
			err := errors.New("file not found error")
			appError := apierrors.NewAppError(
				"file not found", // User-friendly error message
				"500",            // Error code representing the error type
				err,              // Original error for debugging purposes
			)
			apiErrorResponse := apierrors.NewAPIErrorResponse(
				http.StatusInternalServerError, // HTTP status code
				"Internal Server Error",        // Message to return to the client
				appError,                       // Encapsulated application error
			)
			ctx.JSON(apiErrorResponse.StatusCode, apiErrorResponse)
			log.Error(ctx, "File not found error: %s", err.Error())
			return
		}

		apierrors.HandleError(ctx, err)
		return
	}

	ext := path.Ext(filePath)
	contentType := mime.TypeByExtension(ext)
	if contentType == "" {
		contentType = "application/octet-stream"
	}

	ctx.Header("Content-Type", contentType)
	ctx.Header("Content-Disposition", "inline; filename=\""+path.Base(filePath)+"\"")
	ctx.DataFromReader(http.StatusOK, -1, contentType, obj, nil)

	// defer ctx.Request.MultipartForm.RemoveAll() // Clean up temporary files

}
