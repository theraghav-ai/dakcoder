package repository

import (
	"context"
	"crypto/rand"
	"fmt"
	"gotemplate/core/domain"
	"math/big"
	"time"

	config "gitlab.cept.gov.in/it-2.0-common/api-config"

	"github.com/jackc/pgx/v5"
	dblib "gitlab.cept.gov.in/it-2.0-common/api-db"
)

type ObjectionGrpcRepository struct {
	Db  *dblib.DB
	cfg *config.Config
}

// NewUserRepository creates a new user repository instance
func NewObjectionGrpcRepository(Db *dblib.DB, cfg *config.Config) *ObjectionGrpcRepository {
	return &ObjectionGrpcRepository{
		Db,
		cfg,
	}
}

func generateObjectionIDGrpc(ddoCode string) string {
	currentTime := time.Now().Format("20060102150405") // Date and time in format YYYYMMDDHHMMSS
	max := big.NewInt(9999)
	randomInt, err := rand.Int(rand.Reader, max)
	if err != nil {
		panic(err) // Handle error appropriately in production code
	}
	randomPart := fmt.Sprintf("%04d", randomInt.Int64()+1000)
	return fmt.Sprintf("OBJ%s%s%s", ddoCode, currentTime, randomPart[:4])
}

func (ur *ObjectionGrpcRepository) ObjectionCreationGrpcRepo(gctx *context.Context, request *domain.ObjectionRequest) (domain.Objection, error) {

	transDate := time.Now().Format("2006-01-02")
	createdDate, err := time.Parse("2006-01-02", transDate)
	if err != nil {
		return domain.Objection{}, err
	}
	request.CreatedDate = createdDate
	request.ObjectionId = generateObjectionIDGrpc(request.DdoCode)

	query := dblib.Psql.Insert("pao.objection").SetMap(dblib.GenerateMapFromStruct(request, "insert")).Suffix("returning *")
	p, err := dblib.InsertReturning(*gctx, ur.Db, query, pgx.RowToStructByName[domain.Objection])

	return p, err
}
func (ur *ObjectionGrpcRepository) ObjectionCreationPraoGrpcRepo(gctx *context.Context, request *domain.ObjectionPraoRequest) (domain.ObjectionPrao, error) {

	transDate := time.Now().Format("2006-01-02")
	createdDate, err := time.Parse("2006-01-02", transDate)
	if err != nil {
		return domain.ObjectionPrao{}, err
	}
	request.CreatedDate = createdDate
	request.ObjectionId = generateObjectionIDGrpc(request.PaoCode)

	query := dblib.Psql.Insert("pao.objection_prao").SetMap(dblib.GenerateMapFromStruct(request, "insert")).Suffix("returning *")
	p, err := dblib.InsertReturning(*gctx, ur.Db, query, pgx.RowToStructByName[domain.ObjectionPrao])

	return p, err
}
