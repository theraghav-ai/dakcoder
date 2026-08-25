package repository

import (
	"context"
	"crypto/rand"
	"encoding/json"
	"fmt"
	"gotemplate/core/domain"
	"gotemplate/core/port"
	"math/big"
	"time"

	config "gitlab.cept.gov.in/it-2.0-common/api-config"

	sq "github.com/Masterminds/squirrel"
	"github.com/gin-gonic/gin"
	"github.com/jackc/pgx/v5"
	"github.com/volatiletech/null/v9"
	dblib "gitlab.cept.gov.in/it-2.0-common/api-db"
)

type ObjectionRepository struct {
	Db  *dblib.DB
	Cfg *config.Config
}

// NewUserRepository creates a new user repository instance
func NewObjectionRepository(Db *dblib.DB, Cfg *config.Config) *ObjectionRepository {
	return &ObjectionRepository{
		Db,
		Cfg,
	}
}

func generateObjectionID(ddoCode string) string {
	currentTime := time.Now().Format("20060102150405") // Date and time in format YYYYMMDDHHMMSS
	max := big.NewInt(9999)
	randomInt, err := rand.Int(rand.Reader, max)
	if err != nil {
		panic(err) // Handle error appropriately in production code
	}
	randomPart := fmt.Sprintf("%04d", randomInt.Int64()+1000)
	return fmt.Sprintf("OBJ%s%s%s", ddoCode, currentTime, randomPart[:4])
}
func (ur *ObjectionRepository) ObjectionCreationRepo(gctx *gin.Context, request *domain.ObjectionRequest) (domain.Objection, error) {

	ctx, cancel := context.WithTimeout(gctx.Request.Context(), ur.Cfg.GetDuration("db.QueryTimeoutLow"))
	defer cancel()
	transDate := time.Now().Format("2006-01-02")
	createdDate, err := time.Parse("2006-01-02", transDate)
	if err != nil {
		return domain.Objection{}, err
	}
	request.CreatedDate = createdDate
	request.ObjectionId = generateObjectionID(request.DdoCode)

	query := dblib.Psql.Insert("pao.objection").SetMap(dblib.GenerateMapFromStruct(request, "insert")).Suffix("returning *")
	p, err := dblib.InsertReturning(ctx, ur.Db, query, pgx.RowToStructByName[domain.Objection])

	return p, err
}
func (ur *ObjectionRepository) ObjectionPaocodeRepo(gctx *gin.Context, request *domain.Objection, reqMetadata port.MetaDataRequest) ([]domain.ObjectionReplyWithLatestRemark, error) {

	ctx, cancel := context.WithTimeout(gctx.Request.Context(), ur.Cfg.GetDuration("db.QueryTimeoutMed"))
	defer cancel()
	var res domain.ObjectionReplyWithLatestRemark
	columns := dblib.GenerateColumnsFromStruct(res, "select")

	var query sq.SelectBuilder

	if request.StatusFlag.String == "notclosed" {
		query = dblib.Psql.Select(columns...).
			FromSelect(
				sq.Select("a.pao_code", "a.ddo_code", "b.ddo_name", "a.description", "a.objection_id", "a.created_by", "a.created_date", "a.status_flag", "a.last_updated_by", "a.last_updated_date").
					Column(sq.Expr("(CASE WHEN array_length(a.remarks, 1) > 0 THEN (a.remarks[array_length(a.remarks, 1)])->>'data'ELSE NULL END) AS latest_remark")).
					From("pao.objection a").
					LeftJoin("pao.ddo_master b on b.ddo_code = a.ddo_code").
					Where(sq.And{
						sq.Eq{"a.pao_code": request.PaoCode},
						sq.NotEq{"a.status_flag": "closed"},
						sq.NotEq{"a.status_flag": "created"},
						sq.NotEq{"a.status_flag": "rejected"},
					}), "t").
			OrderBy("pao_code").Offset(uint64(reqMetadata.Skip * reqMetadata.Limit)).
			Limit(uint64(reqMetadata.Limit))
	} else {
		query = dblib.Psql.Select(columns...).
			FromSelect(
				sq.Select("a.pao_code", "a.ddo_code", "b.ddo_name", "a.description", "a.objection_id", "a.created_by", "a.created_date", "a.status_flag", "a.last_updated_by", "a.last_updated_date").
					Column(sq.Expr("(CASE WHEN array_length(a.remarks, 1) > 0 THEN (a.remarks[array_length(a.remarks, 1)])->>'data'ELSE NULL END) AS latest_remark")).
					From("pao.objection a").
					LeftJoin("pao.ddo_master b on b.ddo_code = a.ddo_code").
					Where(sq.And{
						sq.Eq{"a.pao_code": request.PaoCode},
						sq.Eq{"a.status_flag": request.StatusFlag},
					}), "t").
			OrderBy("pao_code").Offset(uint64(reqMetadata.Skip * reqMetadata.Limit)).
			Limit(uint64(reqMetadata.Limit))
	}
	return dblib.SelectRows(ctx, ur.Db, query, pgx.RowToStructByNameLax[domain.ObjectionReplyWithLatestRemark])
}

func (ur *ObjectionRepository) ObjectionPaocodeRepoRpt(gctx *gin.Context, request domain.ObjectionbyPaocodeReport, reqMetadata port.MetaDataRequest) ([]domain.ObjectionReplyWithLatestRemark, error) {

	ctx, cancel := context.WithTimeout(gctx.Request.Context(), ur.Cfg.GetDuration("db.QueryTimeoutMed"))
	defer cancel()
	var res domain.ObjectionReplyWithLatestRemark
	columns := dblib.GenerateColumnsFromStruct(res, "select")

	var query sq.SelectBuilder

	if request.Status == "notclosed" {
		query = dblib.Psql.Select(columns...).
			FromSelect(
				sq.Select("a.pao_code", "a.ddo_code", "b.ddo_name", "a.description", "a.objection_id", "a.created_by", "a.created_date", "a.status_flag", "a.last_updated_by", "a.last_updated_date").
					Column(sq.Expr("(CASE WHEN array_length(a.remarks, 1) > 0 THEN (a.remarks[array_length(a.remarks, 1)])->>'data'ELSE NULL END) AS latest_remark")).
					From("pao.objection a").
					LeftJoin("pao.ddo_master b on b.ddo_code = a.ddo_code").
					Where(sq.And{
						sq.GtOrEq{"DATE(a.created_date)": request.FromDate},
						sq.LtOrEq{"DATE(a.created_date)": request.ToDate},
						sq.Eq{"a.pao_code": request.PaoCode},
						sq.NotEq{"a.status_flag": "closed"},
						sq.NotEq{"a.status_flag": "created"},
						sq.NotEq{"a.status_flag": "rejected"},
					}), "t").
			OrderBy("pao_code").Offset(uint64(reqMetadata.Skip * reqMetadata.Limit)).
			Limit(uint64(reqMetadata.Limit))
	} else {
		query = dblib.Psql.Select(columns...).
			FromSelect(
				sq.Select("a.pao_code", "a.ddo_code", "b.ddo_name", "a.description", "a.objection_id", "a.created_by", "a.created_date", "a.status_flag", "a.last_updated_by", "a.last_updated_date").
					Column(sq.Expr("(CASE WHEN array_length(a.remarks, 1) > 0 THEN (a.remarks[array_length(a.remarks, 1)])->>'data'ELSE NULL END) AS latest_remark")).
					From("pao.objection a").
					LeftJoin("pao.ddo_master b on b.ddo_code = a.ddo_code").
					Where(sq.And{
						sq.GtOrEq{"DATE(a.created_date)": request.FromDate},
						sq.LtOrEq{"DATE(a.created_date)": request.ToDate},
						sq.Eq{"a.pao_code": request.PaoCode},
						sq.Eq{"a.status_flag": request.Status},
					}), "t").
			OrderBy("pao_code").Offset(uint64(reqMetadata.Skip * reqMetadata.Limit)).
			Limit(uint64(reqMetadata.Limit))
	}

	return dblib.SelectRows(ctx, ur.Db, query, pgx.RowToStructByNameLax[domain.ObjectionReplyWithLatestRemark])
}

func (ur *ObjectionRepository) ObjectionDdocodeRepo(gctx *gin.Context, request *domain.Objection, reqMetadata port.MetaDataRequest) ([]domain.ObjectionReplyWithLatestRemark, error) {

	ctx, cancel := context.WithTimeout(gctx.Request.Context(), ur.Cfg.GetDuration("db.QueryTimeoutMed"))
	defer cancel()
	var res domain.ObjectionReplyWithLatestRemark
	columns := dblib.GenerateColumnsFromStruct(res, "select")

	var query sq.SelectBuilder

	if request.StatusFlag.String == "notclosed" {
		query = dblib.Psql.Select(columns...).
			FromSelect(
				sq.Select("a.pao_code", "a.ddo_code", "b.ddo_name", "a.description", "a.objection_id", "a.created_by", "a.created_date", "a.status_flag", "a.last_updated_by", "a.last_updated_date").
					Column(sq.Expr("(CASE WHEN array_length(a.remarks, 1) > 0 THEN (a.remarks[array_length(a.remarks, 1)])->>'data'ELSE NULL END) AS latest_remark")).
					From("pao.objection a").
					LeftJoin("pao.ddo_master b on b.ddo_code = a.ddo_code").
					Where(sq.And{
						sq.Eq{"a.ddo_code": request.DdoCode},
						sq.NotEq{"a.status_flag": "closed"},
						sq.NotEq{"a.status_flag": "created"},
						sq.NotEq{"a.status_flag": "rejected"},
					}), "t").
			OrderBy("pao_code").Offset(uint64(reqMetadata.Skip * reqMetadata.Limit)).
			Limit(uint64(reqMetadata.Limit))
	} else {
		query = dblib.Psql.Select(columns...).
			FromSelect(
				sq.Select("a.pao_code", "a.ddo_code", "b.ddo_name", "a.description", "a.objection_id", "a.created_by", "a.created_date", "a.status_flag", "a.last_updated_by", "a.last_updated_date").
					Column(sq.Expr("(CASE WHEN array_length(a.remarks, 1) > 0 THEN (a.remarks[array_length(a.remarks, 1)])->>'data'ELSE NULL END) AS latest_remark")).
					From("pao.objection a").
					LeftJoin("pao.ddo_master b on b.ddo_code = a.ddo_code").
					Where(sq.And{
						sq.Eq{"a.ddo_code": request.DdoCode},
						sq.Eq{"a.status_flag": request.StatusFlag},
					}), "t").
			OrderBy("pao_code").Offset(uint64(reqMetadata.Skip * reqMetadata.Limit)).
			Limit(uint64(reqMetadata.Limit))
	}

	return dblib.SelectRows(ctx, ur.Db, query, pgx.RowToStructByNameLax[domain.ObjectionReplyWithLatestRemark])
}

func (ur *ObjectionRepository) ObjectionDdocodeRptRepo(gctx *gin.Context, request domain.ObjectionbyDdocodeRpt, reqMetadata port.MetaDataRequest) ([]domain.ObjectionReplyWithLatestRemark, error) {

	ctx, cancel := context.WithTimeout(gctx.Request.Context(), ur.Cfg.GetDuration("db.QueryTimeoutMed"))
	defer cancel()
	var res domain.ObjectionReplyWithLatestRemark
	columns := dblib.GenerateColumnsFromStruct(res, "select")

	var query sq.SelectBuilder

	if request.Status == "notclosed" {
		query = dblib.Psql.Select(columns...).
			FromSelect(
				sq.Select("a.pao_code", "a.ddo_code", "b.ddo_name", "a.description", "a.objection_id", "a.created_by", "a.created_date", "a.status_flag", "a.last_updated_by", "a.last_updated_date").
					Column(sq.Expr("(CASE WHEN array_length(a.remarks, 1) > 0 THEN (a.remarks[array_length(a.remarks, 1)])->>'data'ELSE NULL END) AS latest_remark")).
					From("pao.objection a").
					LeftJoin("pao.ddo_master b on b.ddo_code = a.ddo_code").
					Where(sq.And{
						sq.GtOrEq{"DATE(a.created_date)": request.FromDate},
						sq.LtOrEq{"DATE(a.created_date)": request.ToDate},
						sq.Eq{"a.ddo_code": request.DdoCode},
						sq.NotEq{"a.status_flag": "closed"},
						sq.NotEq{"a.status_flag": "created"},
						sq.NotEq{"a.status_flag": "rejected"},
					}), "t").
			OrderBy("pao_code").Offset(uint64(reqMetadata.Skip * reqMetadata.Limit)).
			Limit(uint64(reqMetadata.Limit))
	} else {
		query = dblib.Psql.Select(columns...).
			FromSelect(
				sq.Select("a.pao_code", "a.ddo_code", "b.ddo_name", "a.description", "a.objection_id", "a.created_by", "a.created_date", "a.status_flag", "a.last_updated_by", "a.last_updated_date").
					Column(sq.Expr("(CASE WHEN array_length(a.remarks, 1) > 0 THEN (a.remarks[array_length(a.remarks, 1)])->>'data'ELSE NULL END) AS latest_remark")).
					From("pao.objection a").
					LeftJoin("pao.ddo_master b on b.ddo_code = a.ddo_code").
					Where(sq.And{
						sq.GtOrEq{"DATE(a.created_date)": request.FromDate},
						sq.LtOrEq{"DATE(a.created_date)": request.ToDate},
						sq.Eq{"a.ddo_code": request.DdoCode},
						sq.Eq{"a.status_flag": request.Status},
					}), "t").
			OrderBy("pao_code").Offset(uint64(reqMetadata.Skip * reqMetadata.Limit)).
			Limit(uint64(reqMetadata.Limit))
	}

	return dblib.SelectRows(ctx, ur.Db, query, pgx.RowToStructByNameLax[domain.ObjectionReplyWithLatestRemark])
}

func (ur *ObjectionRepository) ObjectionPaoByIdRepo(gctx *gin.Context, request *domain.Objection) ([]domain.ObjectionReply, error) {

	ctx, cancel := context.WithTimeout(gctx.Request.Context(), ur.Cfg.GetDuration("db.QueryTimeoutMed"))
	defer cancel()
	var res domain.ObjectionReply
	columns := dblib.GenerateColumnsFromStruct(res, "select")
	query := dblib.Psql.Select(columns...).
		FromSelect(
			sq.Select("a.pao_code", "a.ddo_code", "b.ddo_name", "a.description", "a.objection_id", "a.created_by", "a.created_date", "a.remarks", "a.status_flag", "a.last_updated_by", "a.last_updated_date").
				From("pao.objection a").
				LeftJoin("pao.ddo_master b on b.ddo_code = a.ddo_code").
				Where(sq.Eq{"a.objection_id": request.ObjectionId}), "t")
	return dblib.SelectRows(ctx, ur.Db, query, pgx.RowToStructByNameLax[domain.ObjectionReply])
}
func (ur *ObjectionRepository) ObjectionUpdateRepo(gctx *gin.Context, request *domain.Objectioncomment) (domain.Objection, error) {

	ctx, cancel := context.WithTimeout(gctx.Request.Context(), ur.Cfg.GetDuration("db.QueryTimeoutLow"))
	defer cancel()
	currentTime := time.Now()
	request.Remark.CommentedDate = null.TimeFrom(currentTime)

	jsonBytes, err := json.Marshal(request.Remark)
	if err != nil {
		return domain.Objection{}, err
	}
	jsonString := string(jsonBytes)

	query := sq.Update("pao.objection").
		Set("remarks", sq.Expr("remarks || $1 :: jsonb", jsonString)).
		Set("last_updated_by", sq.Expr("$2", request.UpdatedBy)).
		Set("last_updated_date", sq.Expr("$3", request.UpdatedDate)).
		Set("status_flag", sq.Expr("$4", request.StatusFlag)).
		Where("objection_id = $5", request.ObjectionId).
		Where("status_flag != $6", "closed").
		Where("status_flag != $7", "rejected").
		Suffix("Returning pao_code,ddo_code,description,created_by,created_date,remarks,status_flag,objection_id,last_updated_by,last_updated_date")

	return dblib.UpdateReturning(ctx, ur.Db, query, pgx.RowToStructByNameLax[domain.Objection])
}
func (ur *ObjectionRepository) ObjectionClosureRepo(gctx *gin.Context, request *domain.ObjectionClosure) (domain.Objection, error) {

	ctx, cancel := context.WithTimeout(gctx.Request.Context(), ur.Cfg.GetDuration("db.QueryTimeoutLow"))
	defer cancel()
	var p domain.Objection
	currentTime := time.Now()
	request.ClosureRemark.CommentedDate = null.TimeFrom(currentTime)

	jsonBytes, err := json.Marshal(request.ClosureRemark)
	if err != nil {
		return p, err
	}
	jsonString := string(jsonBytes)

	query := sq.Update("pao.objection").
		Set("remarks", sq.Expr("remarks || $1 :: jsonb", jsonString)).
		Set("status_flag", sq.Expr("$2", request.StatusFlag)).
		Set("last_updated_by", sq.Expr("$3", request.ClosedBy)).
		Set("last_updated_date", sq.Expr("$4", request.ClosedDate)).
		Where("objection_id = $5", request.ObjectionId).
		Where("status_flag != $6", "closed").
		Where("status_flag != $7", "rejected").
		Suffix("Returning pao_code,ddo_code,description,created_by,created_date,remarks,status_flag,objection_id,last_updated_by,last_updated_date")

	return dblib.UpdateReturning(ctx, ur.Db, query, pgx.RowToStructByNameLax[domain.Objection])
}
func (ur *ObjectionRepository) ObjectionCreationPraoRepo(gctx *gin.Context, request *domain.ObjectionPraoRequest) (domain.ObjectionPrao, error) {

	ctx, cancel := context.WithTimeout(context.Background(), ur.Cfg.GetDuration("db.QueryTimeoutMed"))
	defer cancel()
	transDate := time.Now().Format("2006-01-02")
	createdDate, err := time.Parse("2006-01-02", transDate)
	if err != nil {
		return domain.ObjectionPrao{}, err
	}
	request.CreatedDate = createdDate
	request.ObjectionId = generateObjectionID(request.PaoCode)

	query := dblib.Psql.Insert("pao.objection_prao").SetMap(dblib.GenerateMapFromStruct(request, "insert")).Suffix("returning *")
	p, err := dblib.InsertReturning(ctx, ur.Db, query, pgx.RowToStructByName[domain.ObjectionPrao])

	return p, err
}
func (ur *ObjectionRepository) ObjectionPraocodePraoRepo(gctx *gin.Context, request *domain.ObjectionPrao, reqMetadata port.MetaDataRequest) ([]domain.ObjectionPraoReplyWithLatestRemarks, error) {

	ctx, cancel := context.WithTimeout(gctx.Request.Context(), ur.Cfg.GetDuration("db.QueryTimeoutMed"))
	defer cancel()
	var res domain.ObjectionPraoReplyWithLatestRemarks
	columns := dblib.GenerateColumnsFromStruct(res, "select")

	var query sq.SelectBuilder

	if request.StatusFlag.String == "notclosed" {
		query = dblib.Psql.Select(columns...).
			FromSelect(
				sq.Select("a.prao_code", "a.pao_code", "MIN(b.pao_name) AS pao_name", "a.description", "a.objection_id", "a.created_by", "a.created_date", "a.status_flag", "a.last_updated_by", "a.last_updated_date").
					Column(sq.Expr("(CASE WHEN array_length(a.remarks, 1) > 0 THEN (a.remarks[array_length(a.remarks, 1)])->>'data'ELSE NULL END) AS latest_remark")).
					From("pao.objection_prao a").
					LeftJoin("pao.ddo_master b on b.pao_code = a.pao_code").
					Where(sq.And{
						sq.Eq{"a.prao_code": request.PraoCode},
						sq.NotEq{"a.status_flag": "closed"},
						sq.NotEq{"a.status_flag": "created"},
						sq.NotEq{"a.status_flag": "rejected"},
					}).
					GroupBy("a.prao_code", "a.pao_code", "b.pao_name", "a.description", "a.objection_id", "a.created_by", "a.created_date", "a.status_flag", "a.last_updated_by", "a.last_updated_date"),
				"derived_table").
			Offset(uint64(reqMetadata.Skip * reqMetadata.Limit)).
			Limit(uint64(reqMetadata.Limit))
	} else {
		query = dblib.Psql.Select(columns...).
			FromSelect(
				sq.Select("a.prao_code", "a.pao_code", "MIN(b.pao_name) AS pao_name", "a.description", "a.objection_id", "a.created_by", "a.created_date", "a.status_flag", "a.last_updated_by", "a.last_updated_date").
					Column(sq.Expr("(CASE WHEN array_length(a.remarks, 1) > 0 THEN (a.remarks[array_length(a.remarks, 1)])->>'data'ELSE NULL END) AS latest_remark")).
					From("pao.objection_prao a").
					LeftJoin("pao.ddo_master b on b.pao_code = a.pao_code").
					Where(sq.And{
						sq.Eq{"a.prao_code": request.PraoCode},
						sq.Eq{"a.status_flag": request.StatusFlag},
					}).
					GroupBy("a.prao_code", "a.pao_code", "b.pao_name", "a.description", "a.objection_id", "a.created_by", "a.created_date", "a.status_flag", "a.last_updated_by", "a.last_updated_date"),
				"derived_table").
			Offset(uint64(reqMetadata.Skip * reqMetadata.Limit)).
			Limit(uint64(reqMetadata.Limit))
	}

	return dblib.SelectRows(ctx, ur.Db, query, pgx.RowToStructByNameLax[domain.ObjectionPraoReplyWithLatestRemarks])
}

func (ur *ObjectionRepository) ObjectionPraocodePraoRptRepo(gctx *gin.Context, request domain.ObjectionbyPraocodeReport, reqMetadata port.MetaDataRequest) ([]domain.ObjectionPraoReplyWithLatestRemarks, error) {

	ctx, cancel := context.WithTimeout(gctx.Request.Context(), ur.Cfg.GetDuration("db.QueryTimeoutMed"))
	defer cancel()
	var res domain.ObjectionPraoReplyWithLatestRemarks
	columns := dblib.GenerateColumnsFromStruct(res, "select")

	var query sq.SelectBuilder

	if request.Status == "notclosed" {
		query = dblib.Psql.Select(columns...).
			FromSelect(
				sq.Select("a.prao_code", "a.pao_code", "MIN(b.pao_name) AS pao_name", "a.description", "a.objection_id", "a.created_by", "a.created_date", "a.status_flag", "a.last_updated_by", "a.last_updated_date").
					Column(sq.Expr("(CASE WHEN array_length(a.remarks, 1) > 0 THEN (a.remarks[array_length(a.remarks, 1)])->>'data'ELSE NULL END) AS latest_remark")).
					From("pao.objection_prao a").
					LeftJoin("pao.ddo_master b on b.pao_code = a.pao_code").
					Where(sq.And{
						sq.GtOrEq{"DATE(a.created_date)": request.FromDate},
						sq.LtOrEq{"DATE(a.created_date)": request.ToDate},
						sq.Eq{"a.prao_code": request.PraoCode},
						sq.NotEq{"a.status_flag": "closed"},
						sq.NotEq{"a.status_flag": "created"},
						sq.NotEq{"a.status_flag": "rejected"},
					}).
					GroupBy("a.prao_code", "a.pao_code", "b.pao_name", "a.description", "a.objection_id", "a.created_by", "a.created_date", "a.status_flag", "a.last_updated_by", "a.last_updated_date"),
				"derived_table").
			Offset(uint64(reqMetadata.Skip * reqMetadata.Limit)).
			Limit(uint64(reqMetadata.Limit))
	} else {
		query = dblib.Psql.Select(columns...).
			FromSelect(
				sq.Select("a.prao_code", "a.pao_code", "MIN(b.pao_name) AS pao_name", "a.description", "a.objection_id", "a.created_by", "a.created_date", "a.status_flag", "a.last_updated_by", "a.last_updated_date").
					Column(sq.Expr("(CASE WHEN array_length(a.remarks, 1) > 0 THEN (a.remarks[array_length(a.remarks, 1)])->>'data'ELSE NULL END) AS latest_remark")).
					From("pao.objection_prao a").
					LeftJoin("pao.ddo_master b on b.pao_code = a.pao_code").
					Where(sq.And{
						sq.GtOrEq{"DATE(a.created_date)": request.FromDate},
						sq.LtOrEq{"DATE(a.created_date)": request.ToDate},
						sq.Eq{"a.prao_code": request.PraoCode},
						sq.Eq{"a.status_flag": request.Status},
					}).
					GroupBy("a.prao_code", "a.pao_code", "b.pao_name", "a.description", "a.objection_id", "a.created_by", "a.created_date", "a.status_flag", "a.last_updated_by", "a.last_updated_date"),
				"derived_table").
			Offset(uint64(reqMetadata.Skip * reqMetadata.Limit)).
			Limit(uint64(reqMetadata.Limit))
	}

	return dblib.SelectRows(ctx, ur.Db, query, pgx.RowToStructByNameLax[domain.ObjectionPraoReplyWithLatestRemarks])
}

func (ur *ObjectionRepository) ObjectionPaocodePraoRepo(gctx *gin.Context, request *domain.ObjectionPrao, reqMetadata port.MetaDataRequest) ([]domain.ObjectionPraoReplyWithLatestRemarks, error) {

	ctx, cancel := context.WithTimeout(gctx.Request.Context(), ur.Cfg.GetDuration("db.QueryTimeoutMed"))
	defer cancel()
	var res domain.ObjectionPraoReplyWithLatestRemarks
	columns := dblib.GenerateColumnsFromStruct(res, "select")

	var query sq.SelectBuilder

	if request.StatusFlag.String == "notclosed" {
		query = dblib.Psql.Select(columns...).
			FromSelect(
				sq.Select("a.prao_code", "a.pao_code", "MIN(b.pao_name) AS pao_name", "a.description", "a.objection_id", "a.created_by", "a.created_date", "a.status_flag", "a.last_updated_by", "a.last_updated_date").
					Column(sq.Expr("(CASE WHEN array_length(a.remarks, 1) > 0 THEN (a.remarks[array_length(a.remarks, 1)])->>'data'ELSE NULL END) AS latest_remark")).
					From("pao.objection_prao a").
					LeftJoin("pao.ddo_master b on b.pao_code = a.pao_code").
					Where(sq.And{
						sq.Eq{"a.pao_code": request.PaoCode},
						sq.NotEq{"a.status_flag": "closed"},
						sq.NotEq{"a.status_flag": "created"},
						sq.NotEq{"a.status_flag": "rejected"},
					}).
					GroupBy("a.prao_code", "a.pao_code", "b.pao_name", "a.description", "a.objection_id", "a.created_by", "a.created_date", "a.status_flag", "a.last_updated_by", "a.last_updated_date"),
				"derived_table").
			Offset(uint64(reqMetadata.Skip * reqMetadata.Limit)).
			Limit(uint64(reqMetadata.Limit))
	} else {
		query = dblib.Psql.Select(columns...).
			FromSelect(
				sq.Select("a.prao_code", "a.pao_code", "MIN(b.pao_name) AS pao_name", "a.description", "a.objection_id", "a.created_by", "a.created_date", "a.status_flag", "a.last_updated_by", "a.last_updated_date").
					Column(sq.Expr("(CASE WHEN array_length(a.remarks, 1) > 0 THEN (a.remarks[array_length(a.remarks, 1)])->>'data'ELSE NULL END) AS latest_remark")).
					From("pao.objection_prao a").
					LeftJoin("pao.ddo_master b on b.pao_code = a.pao_code").
					Where(sq.And{
						sq.Eq{"a.pao_code": request.PaoCode},
						sq.Eq{"a.status_flag": request.StatusFlag},
					}).
					GroupBy("a.prao_code", "a.pao_code", "b.pao_name", "a.description", "a.objection_id", "a.created_by", "a.created_date", "a.status_flag", "a.last_updated_by", "a.last_updated_date"),
				"derived_table").
			Offset(uint64(reqMetadata.Skip * reqMetadata.Limit)).
			Limit(uint64(reqMetadata.Limit))
	}
	return dblib.SelectRows(ctx, ur.Db, query, pgx.RowToStructByNameLax[domain.ObjectionPraoReplyWithLatestRemarks])
}

func (ur *ObjectionRepository) ObjectionPaocodePraoRptRepo(gctx *gin.Context, request domain.ObjectionbyPaocodeReport, reqMetadata port.MetaDataRequest) ([]domain.ObjectionPraoReplyWithLatestRemarks, error) {

	ctx, cancel := context.WithTimeout(gctx.Request.Context(), ur.Cfg.GetDuration("db.QueryTimeoutMed"))
	defer cancel()
	var res domain.ObjectionPraoReplyWithLatestRemarks
	columns := dblib.GenerateColumnsFromStruct(res, "select")

	var query sq.SelectBuilder

	if request.Status == "notclosed" {
		query = dblib.Psql.Select(columns...).
			FromSelect(
				sq.Select("a.prao_code", "a.pao_code", "MIN(b.pao_name) AS pao_name", "a.description", "a.objection_id", "a.created_by", "a.created_date", "a.status_flag", "a.last_updated_by", "a.last_updated_date").
					Column(sq.Expr("(CASE WHEN array_length(a.remarks, 1) > 0 THEN (a.remarks[array_length(a.remarks, 1)])->>'data'ELSE NULL END) AS latest_remark")).
					From("pao.objection_prao a").
					LeftJoin("pao.ddo_master b on b.pao_code = a.pao_code").
					Where(sq.And{
						sq.GtOrEq{"DATE(a.created_date)": request.FromDate},
						sq.LtOrEq{"DATE(a.created_date)": request.ToDate},
						sq.Eq{"a.pao_code": request.PaoCode},
						sq.NotEq{"a.status_flag": "closed"},
						sq.NotEq{"a.status_flag": "created"},
						sq.NotEq{"a.status_flag": "rejected"},
					}).
					GroupBy("a.prao_code", "a.pao_code", "b.pao_name", "a.description", "a.objection_id", "a.created_by", "a.created_date", "a.status_flag", "a.last_updated_by", "a.last_updated_date"),
				"derived_table").
			Offset(uint64(reqMetadata.Skip * reqMetadata.Limit)).
			Limit(uint64(reqMetadata.Limit))
	} else {
		query = dblib.Psql.Select(columns...).
			FromSelect(
				sq.Select("a.prao_code", "a.pao_code", "MIN(b.pao_name) AS pao_name", "a.description", "a.objection_id", "a.created_by", "a.created_date", "a.status_flag", "a.last_updated_by", "a.last_updated_date").
					Column(sq.Expr("(CASE WHEN array_length(a.remarks, 1) > 0 THEN (a.remarks[array_length(a.remarks, 1)])->>'data'ELSE NULL END) AS latest_remark")).
					From("pao.objection_prao a").
					LeftJoin("pao.ddo_master b on b.pao_code = a.pao_code").
					Where(sq.And{
						sq.GtOrEq{"DATE(a.created_date)": request.FromDate},
						sq.LtOrEq{"DATE(a.created_date)": request.ToDate},
						sq.Eq{"a.pao_code": request.PaoCode},
						sq.Eq{"a.status_flag": request.Status},
					}).
					GroupBy("a.prao_code", "a.pao_code", "b.pao_name", "a.description", "a.objection_id", "a.created_by", "a.created_date", "a.status_flag", "a.last_updated_by", "a.last_updated_date"),
				"derived_table").
			Offset(uint64(reqMetadata.Skip * reqMetadata.Limit)).
			Limit(uint64(reqMetadata.Limit))
	}
	return dblib.SelectRows(ctx, ur.Db, query, pgx.RowToStructByNameLax[domain.ObjectionPraoReplyWithLatestRemarks])
}

func (ur *ObjectionRepository) ObjectionPraoByIdRepo(gctx *gin.Context, request *domain.ObjectionPrao) ([]domain.ObjectionPraoReply, error) {

	ctx, cancel := context.WithTimeout(gctx.Request.Context(), ur.Cfg.GetDuration("db.QueryTimeoutMed"))
	defer cancel()
	var res domain.ObjectionPraoReply
	columns := dblib.GenerateColumnsFromStruct(res, "select")
	query := dblib.Psql.Select(columns...).
		FromSelect(
			sq.Select("a.prao_code", "a.pao_code", "MIN(b.pao_name) AS pao_name", "a.description", "a.objection_id", "a.created_by", "a.created_date", "a.remarks", "a.status_flag", "a.last_updated_by", "a.last_updated_date").
				From("pao.objection_prao a").
				LeftJoin("pao.ddo_master b on b.pao_code = a.pao_code").
				Where(sq.Eq{"a.objection_id": request.ObjectionId}).
				GroupBy("a.prao_code", "a.pao_code", "b.pao_name", "a.description", "a.objection_id", "a.created_by", "a.created_date", "a.remarks", "a.status_flag", "a.last_updated_by", "a.last_updated_date"),
			"derived_table")
	return dblib.SelectRows(ctx, ur.Db, query, pgx.RowToStructByNameLax[domain.ObjectionPraoReply])
}
func (ur *ObjectionRepository) ObjectionUpdatePraoRepo(gctx *gin.Context, request *domain.Objectioncomment) (domain.ObjectionPrao, error) {

	ctx, cancel := context.WithTimeout(gctx.Request.Context(), ur.Cfg.GetDuration("db.QueryTimeoutMed"))
	defer cancel()
	currentTime := time.Now()
	request.Remark.CommentedDate = null.TimeFrom(currentTime)

	jsonBytes, err := json.Marshal(request.Remark)
	if err != nil {
		return domain.ObjectionPrao{}, err
	}
	jsonString := string(jsonBytes)

	query := sq.Update("pao.objection_prao").
		Set("remarks", sq.Expr("remarks || $1 :: jsonb", jsonString)).
		Set("last_updated_by", sq.Expr("$2", request.UpdatedBy)).
		Set("last_updated_date", sq.Expr("$3", request.UpdatedDate)).
		Set("status_flag", sq.Expr("$4", request.StatusFlag)).
		Where("objection_id = $5", request.ObjectionId).
		Where("status_flag != $6", "closed").
		Where("status_flag != $7", "rejected").
		Suffix("Returning prao_code,pao_code,description,created_by,created_date,remarks,status_flag,objection_id,last_updated_by,last_updated_date")

	return dblib.UpdateReturning(ctx, ur.Db, query, pgx.RowToStructByNameLax[domain.ObjectionPrao])
}
func (ur *ObjectionRepository) ObjectionClosurePraoRepo(gctx *gin.Context, request *domain.ObjectionClosure) (domain.ObjectionPrao, error) {

	ctx, cancel := context.WithTimeout(gctx.Request.Context(), ur.Cfg.GetDuration("db.QueryTimeoutMed"))
	defer cancel()
	var p domain.ObjectionPrao
	currentTime := time.Now()
	request.ClosureRemark.CommentedDate = null.TimeFrom(currentTime)

	jsonBytes, err := json.Marshal(request.ClosureRemark)
	if err != nil {
		return p, err
	}
	jsonString := string(jsonBytes)

	query := sq.Update("pao.objection_prao").
		Set("remarks", sq.Expr("remarks || $1 :: jsonb", jsonString)).
		Set("status_flag", sq.Expr("$2", request.StatusFlag)).
		Set("last_updated_by", sq.Expr("$3", request.ClosedBy)).
		Set("last_updated_date", sq.Expr("$4", request.ClosedDate)).
		Where("objection_id = $5", request.ObjectionId).
		Where("status_flag != $6", "closed").
		Where("status_flag != $7", "rejected").
		Suffix("Returning prao_code,pao_code,description,created_by,created_date,remarks,status_flag,objection_id,last_updated_by,last_updated_date")

	return dblib.UpdateReturning(ctx, ur.Db, query, pgx.RowToStructByNameLax[domain.ObjectionPrao])
}
