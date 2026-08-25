package repository

import ( //"reflect"
	//"strings"
	//"gotemplate/core/port"

	// "gotemplate/config"
	"mime/multipart"

	config "gitlab.cept.gov.in/it-2.0-common/api-config"

	"github.com/gin-gonic/gin"
	"github.com/minio/minio-go/v7"
	dblib "gitlab.cept.gov.in/it-2.0-common/api-db"
	log "gitlab.cept.gov.in/it-2.0-common/api-log"
)

type ObjectionFileRepository struct {
	Db          *dblib.DB
	MinioClient *minio.Client
	Cfg         *config.Config
}

// NewUserRepository creates a new user repository instance
func NewObjectionFileRepository(Db *dblib.DB, MinioClient *minio.Client, Cfg *config.Config) *ObjectionFileRepository {
	return &ObjectionFileRepository{
		Db,
		MinioClient,
		Cfg,
	}
}

// var MinioClient *minio.Client

func (fr *ObjectionFileRepository) UploadFile(gctx *gin.Context, file multipart.File, objectName, contentType string, size int64) error {
	// bucketName := config.GetBucketName()
	// bucketName := fr.Cfg.Get("minio.bucketName")
	bucketName := fr.Cfg.Get("minio.bucketName").(string)

	ctx := gctx.Request.Context() // Use the request context for proper cancellation handling

	defer file.Close() // Ensure the file is closed after use

	// Upload the file to the bucket
	_, err := fr.MinioClient.PutObject(
		ctx,
		bucketName,
		objectName,
		file,
		size,
		minio.PutObjectOptions{ContentType: contentType},
	)
	if err != nil {
		// Log the error for traceability
		log.Debug(gctx, "Failed to upload file:")

		return err
	}

	return nil
}
