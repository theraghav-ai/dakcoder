package repository

import (
	"context"
	"database/sql"
	"encoding/json"
	"errors"
	"fmt"
	"gotemplate/core/domain"
	"gotemplate/core/port"
	"strconv"
	"strings"
	"time"

	"github.com/Masterminds/squirrel"
	sq "github.com/Masterminds/squirrel"
	"github.com/gin-gonic/gin"
	"github.com/jackc/pgx/v5"
	"github.com/volatiletech/null/v9"
	config "gitlab.cept.gov.in/it-2.0-common/api-config"
	dblib "gitlab.cept.gov.in/it-2.0-common/api-db"
	log "gitlab.cept.gov.in/it-2.0-common/api-log"
)

/**
 * UserRepository implements port.UserRepository interface
 * and provides an access to the postgres database
 */
type PaogenRepository struct {
	Db  *dblib.DB
	Cfg *config.Config
}

// NewUserRepository creates a new user repository instance
func NewPaogenRepository(Db *dblib.DB, Cfg *config.Config) *PaogenRepository {
	return &PaogenRepository{
		Db,
		Cfg,
	}
}

func (ur *PaogenRepository) GetOfficenameRepo(gctx *gin.Context, req *domain.OfficeNameRequest) (*domain.OfficeDetails, bool, error) {

	ctx, cancel := context.WithTimeout(gctx.Request.Context(), ur.Cfg.GetDuration("db.QueryTimeoutLow"))
	defer cancel()
	log.Debug(gctx, "Came inside getOfficeNameRepo")
	var u1 domain.OfficeDetails
	columns := dblib.GenerateColumnsFromStruct(u1, "select")
	query := dblib.Psql.Select(columns...).
		From("pao.ddo_master").
		Where(sq.Eq{"ddo_office_id": req.Id}).
		Limit(1)
	return dblib.SelectOneOK(ctx, ur.Db, query, pgx.RowToAddrOfStructByNameLax[domain.OfficeDetails])

}

func (ur *PaogenRepository) GetDDOlistRepo120326(gctx *gin.Context, req *domain.DdoListRequest, reqMetadata port.MetaDataRequest) ([]domain.PfmsStatus, error) {
	ctx, cancel := context.WithTimeout(gctx.Request.Context(), ur.Cfg.GetDuration("db.QueryTimeoutMed"))
	defer cancel()
	log.Debug(gctx, "Came inside getOfficeNameRepo")
	sq1, _, err := sq.Select("pfms_ddo_id", "pao_code", "ddo_code", "ddo_name", "h_cash_book_receive_flag", "h_verification_flag", "h_pfms_generation_flag", "verified_date", "business_date", "opening_bal", "closing_bal", "verified_by").
		From("pao.pfms_main").
		Where("business_date = $1").ToSql()
	if err != nil {
		return nil, err
	}
	query := sq.Select("ddo.ddo_code as ddo_code", "ddo.ddo_name as ddo_name", "COALESCE(pfms.h_cash_book_receive_flag, 'false') as h_cash_book_receive_flag", "COALESCE(pfms.h_verification_flag, 'false') as h_verification_flag", "COALESCE(pfms.h_pfms_generation_flag, 'false') as h_pfms_generation_flag").
		From("pao.ddo_master as ddo").
		LeftJoin("("+sq1+") AS pfms ON ddo.ddo_code = pfms.ddo_code", req.Date).
		Where("ddo.pao_code = $2", req.PaoCode).
		OrderBy("ddo.ddo_code").Offset(uint64(reqMetadata.Skip * reqMetadata.Limit)).
		Limit(uint64(reqMetadata.Limit))

	results, err := dblib.SelectRows(ctx, ur.Db, query, pgx.RowToStructByNameLax[domain.PfmsStatus])
	if err != nil {
		return nil, err
	}
	for i := range results {
		results[i].Date = null.StringFrom(req.Date)
	}

	return results, nil
}

func (ur *PaogenRepository) GetDDOlistRepo(gctx *gin.Context, req *domain.DdoListRequest, reqMetadata port.MetaDataRequest) ([]domain.PfmsStatus, error) {
	ctx, cancel := context.WithTimeout(gctx.Request.Context(), ur.Cfg.GetDuration("db.QueryTimeoutMed"))
	defer cancel()
	log.Debug(gctx, "Came inside getOfficeNameRepo")
	sq1, _, err := sq.Select("pfms_ddo_id", "pao_code", "ddo_code", "ddo_name", "h_cash_book_receive_flag", "h_verification_flag", "h_pfms_generation_flag", "verified_date", "business_date", "opening_bal", "closing_bal", "verified_by").
		From("pao.pfms_main").
		Where("business_date >= $1::date AND business_date < $1::date + INTERVAL '1 day'").ToSql()
	if err != nil {
		return nil, err
	}
	query := sq.Select("ddo.ddo_code as ddo_code", "ddo.ddo_name as ddo_name", "COALESCE(pfms.h_cash_book_receive_flag, 'false') as h_cash_book_receive_flag", "COALESCE(pfms.h_verification_flag, 'false') as h_verification_flag", "COALESCE(pfms.h_pfms_generation_flag, 'false') as h_pfms_generation_flag").
		From("pao.ddo_master as ddo").
		LeftJoin("("+sq1+") AS pfms ON ddo.ddo_code = pfms.ddo_code", req.Date).
		Where("ddo.pao_code = $2 AND $3::date BETWEEN ddo.valid_from AND ddo.valid_to", req.PaoCode, req.Date).
		OrderBy("ddo.ddo_code").Offset(uint64(reqMetadata.Skip * reqMetadata.Limit)).
		Limit(uint64(reqMetadata.Limit))

	results, err := dblib.SelectRows(ctx, ur.Db, query, pgx.RowToStructByNameLax[domain.PfmsStatus])
	if err != nil {
		return nil, err
	}
	for i := range results {
		results[i].Date = null.StringFrom(req.Date)
	}

	return results, nil
}

func (ur *PaogenRepository) GetDDOlistupdateRepo(gctx *gin.Context, req *domain.DdoListRequestUpdate) error {
	ctx, cancel := context.WithTimeout(gctx.Request.Context(), ur.Cfg.GetDuration("db.QueryTimeoutLow"))
	defer cancel()

	log.Debug(gctx, "Inside GetDDOlistupdateRepo")
	fromDate, err := time.Parse("2006-01-02", req.FromDate)
	if err != nil {
		return fmt.Errorf("invalid FromDate: %v", err)
	}
	toDate, err := time.Parse("2006-01-02", req.ToDate)
	if err != nil {
		return fmt.Errorf("invalid ToDate: %v", err)
	}

	// Construct the subquery (sq1)
	sq1 := sq.Select("q10.pao_code", "q10.ddo_code", "q10.ddo_name", "q10.business_date").
		FromSelect(
			sq.Select("dm.ddo_code", "kc.business_date", "dm.pao_code", "dm.ddo_name").
				From("pao.kafka_cash_book kc").
				Join("pao.ddo_master dm ON kc.office_id = dm.ddo_office_id").
				Where("kc.office_id IN (SELECT ddo_office_id FROM pao.ddo_master WHERE pao_code = $1) AND kc.business_date BETWEEN $2 AND $3", req.PaoCode, fromDate, toDate),
			"q10",
		).
		LeftJoin("pao.pfms_main pm ON q10.ddo_code = pm.ddo_code AND q10.business_date = pm.business_date").
		Where("pm.ddo_code IS NULL")

	// Construct the main query (sq2)
	sq2 := sq.Select(
		"q10.ddo_code || TO_CHAR(q10.business_date, 'YYYYMMDD') AS pfms_ddo_id",
		"q10.pao_code",
		"q10.ddo_code",
		"q10.ddo_name",
		"true AS h_cash_book_receive_flag",
		"NULL AS h_verification_flag",
		"NULL AS h_pfms_generation_flag",
		"NULL AS verified_date",
		"q10.business_date",
		"NULL AS opening_bal",
		"NULL AS closing_bal",
		"NULL AS verified_by",
		"NULL AS pfms_unique_id",
	).FromSelect(sq1, "q10")

	// Construct the final insert query
	query := sq.Insert("pao.pfms_main").
		Columns(
			"pfms_ddo_id", "pao_code", "ddo_code", "ddo_name", "h_cash_book_receive_flag",
			"h_verification_flag", "h_pfms_generation_flag", "verified_date", "business_date",
			"opening_bal", "closing_bal", "verified_by", "pfms_unique_id",
		).
		Select(sq2)

	// Convert the final query to SQL and args
	sql, args, err := query.ToSql()
	if err != nil {
		return err
	}

	// Execute the query
	_, err = ur.Db.Exec(ctx, sql, args...)
	if err != nil {
		return err
	}

	return nil
}

func (ur *PaogenRepository) GetDDOsRepo03072026(gctx *gin.Context, req *domain.DdosRequest, reqMetadata port.MetaDataRequest) ([]domain.Ddo, error) {
	ctx, cancel := context.WithTimeout(gctx.Request.Context(), ur.Cfg.GetDuration("db.QueryTimeoutMed"))
	defer cancel()
	log.Debug(gctx, "Came inside getOfficeNameRepo")
	var u1 domain.Ddo

	if req.OfficeId != "" {
		columns := dblib.GenerateColumnsFromStruct(u1, "select")
		query := dblib.Psql.Select(columns...).
			From("pao.ddo_master").
			Where(sq.Eq{"pao_office_id": req.OfficeId}).
			OrderBy("ddo_code").Offset(uint64(reqMetadata.Skip * reqMetadata.Limit)).
			Limit(uint64(reqMetadata.Limit))

		return dblib.SelectRows(ctx, ur.Db, query, pgx.RowToStructByNameLax[domain.Ddo])
	}

	if req.PaoCode == "999999" {
		columns := dblib.GenerateColumnsFromStruct(u1, "select")
		query := dblib.Psql.Select(columns...).
			From("pao.ddo_master").
			OrderBy("ddo_code").Offset(uint64(reqMetadata.Skip * reqMetadata.Limit)).
			Limit(uint64(reqMetadata.Limit))

		return dblib.SelectRows(ctx, ur.Db, query, pgx.RowToStructByNameLax[domain.Ddo])
	} else {
		columns := dblib.GenerateColumnsFromStruct(u1, "select")
		query := dblib.Psql.Select(columns...).
			From("pao.ddo_master").
			Where(sq.Eq{"pao_code": req.PaoCode}).
			OrderBy("ddo_code").Offset(uint64(reqMetadata.Skip * reqMetadata.Limit)).
			Limit(uint64(reqMetadata.Limit))

		return dblib.SelectRows(ctx, ur.Db, query, pgx.RowToStructByNameLax[domain.Ddo])
	}
}

func (ur *PaogenRepository) GetDDOsRepo(gctx *gin.Context, req *domain.DdosRequest, reqMetadata port.MetaDataRequest) ([]domain.Ddo, error) {
	ctx, cancel := context.WithTimeout(gctx.Request.Context(), ur.Cfg.GetDuration("db.QueryTimeoutMed"))
	defer cancel()
	log.Debug(gctx, "Came inside getOfficeNameRepo")
	var u1 domain.Ddo

	validityFilter := sq.And{
		sq.Expr("valid_from <= CURRENT_DATE"),
		sq.Expr("valid_to >= CURRENT_DATE"),
	}

	if req.OfficeId != "" {
		columns := dblib.GenerateColumnsFromStruct(u1, "select")
		query := dblib.Psql.Select(columns...).
			From("pao.ddo_master").
			Where(sq.Eq{"pao_office_id": req.OfficeId}).
			Where(validityFilter).
			OrderBy("ddo_code").
			Offset(uint64(reqMetadata.Skip * reqMetadata.Limit)).
			Limit(uint64(reqMetadata.Limit))

		return dblib.SelectRows(ctx, ur.Db, query, pgx.RowToStructByNameLax[domain.Ddo])
	}

	if req.PaoCode == "999999" {
		columns := dblib.GenerateColumnsFromStruct(u1, "select")
		query := dblib.Psql.Select(columns...).
			From("pao.ddo_master").
			Where(validityFilter).
			OrderBy("ddo_code").
			Offset(uint64(reqMetadata.Skip * reqMetadata.Limit)).
			Limit(uint64(reqMetadata.Limit))

		return dblib.SelectRows(ctx, ur.Db, query, pgx.RowToStructByNameLax[domain.Ddo])
	} else {
		columns := dblib.GenerateColumnsFromStruct(u1, "select")
		query := dblib.Psql.Select(columns...).
			From("pao.ddo_master").
			Where(sq.Eq{"pao_code": req.PaoCode}).
			Where(validityFilter).
			OrderBy("ddo_code").
			Offset(uint64(reqMetadata.Skip * reqMetadata.Limit)).
			Limit(uint64(reqMetadata.Limit))

		return dblib.SelectRows(ctx, ur.Db, query, pgx.RowToStructByNameLax[domain.Ddo])
	}
}

func (ur *PaogenRepository) GetPAOsRepo(gctx *gin.Context) ([]domain.Pao, error) {
	ctx, cancel := context.WithTimeout(gctx.Request.Context(), ur.Cfg.GetDuration("db.QueryTimeoutMed"))
	defer cancel()
	log.Debug(gctx, "Came inside getOfficeNameRepo")
	var u1 domain.Pao
	columns := dblib.GenerateColumnsFromStruct(u1, "select")
	query := dblib.Psql.Select(columns...).
		Distinct().
		FromSelect(
			dblib.Psql.Select("pao_code", "pao_office_id", "pao_name").
				From("pao.ddo_master"), "t")

	return dblib.SelectRows(ctx, ur.Db, query, pgx.RowToStructByNameLax[domain.Pao])

}

func (ur *PaogenRepository) GetDDOdetailRepo21042026(gctx *gin.Context, req *domain.DdoDetailRequest, reqMetadata port.MetaDataRequest) ([]domain.DdoDetail, error) {
	ctx, cancel := context.WithTimeout(gctx.Request.Context(), ur.Cfg.GetDuration("db.QueryTimeoutMed"))
	defer cancel()
	log.Debug(gctx, "Came inside getOfficeNameRepo")
	Date, err := time.Parse("2006-01-02", req.Date)
	if err != nil {
		return nil, fmt.Errorf("invalid FromDate: %v", err)
	}

	query := dblib.Psql.Select("offi.ddo_code as ddo_code,offi.ddo_name as ddo_office_name,c.business_date as business_date, c.closing_bal as closing_bal, c.opening_bal as opening_bal,coalesce(m.hoa, '999999999999999') as hoa , coalesce(m.hoa_description, 'Hoa description not available') as hoa_description").
		Column(sq.Expr("SUM(CASE WHEN lat.each_item->>'credit_or_debit' = 'P' THEN (lat.each_item->>'total_amount')::numeric ELSE 0 END) AS payment")).
		Column(sq.Expr("SUM(CASE WHEN lat.each_item->>'credit_or_debit' = 'R' THEN (lat.each_item->>'total_amount')::numeric ELSE 0 END) AS receipt")).
		Column(sq.Expr("json_agg(json_build_object('account_code', lat.each_item->>'account_code','account_code_description', lat.each_item->>'account_code_description','payment', CASE WHEN lat.each_item->>'credit_or_debit' = 'P' THEN (lat.each_item->>'total_amount')::numeric ELSE 0 END,'receipt', CASE WHEN lat.each_item->>'credit_or_debit' = 'R' THEN (lat.each_item->>'total_amount')::numeric ELSE 0 END)) FILTER (WHERE (CASE WHEN lat.each_item->>'credit_or_debit' = 'P' THEN (lat.each_item->>'total_amount')::numeric ELSE 0 END) != 0 OR (CASE WHEN lat.each_item->>'credit_or_debit' = 'R' THEN (lat.each_item->>'total_amount')::numeric ELSE 0 END)!= 0) AS account_array")).
		From("pao.ddo_master offi").
		LeftJoin("pao.kafka_cash_book AS c ON offi.ddo_office_id = c.office_id AND c.business_date = $1", Date).
		CrossJoin("json_array_elements(c.details) AS lat(each_item)").
		LeftJoin("pao.kafka_account_codes_master AS m ON (lat.each_item->>'account_code') = m.account_code").
		// LeftJoin("pao.office_master AS mm ON offi.ddo_office_id = mm.office_id").
		Where("offi.ddo_code = $2", req.DdoCode).
		GroupBy("offi.ddo_code", "offi.ddo_name", "c.business_date", "c.closing_bal", "c.opening_bal", "m.hoa", "m.hoa_description").
		OrderBy("offi.ddo_code").
		Having(sq.Expr("ABS(SUM(CASE WHEN lat.each_item->>'credit_or_debit' = 'P' THEN (lat.each_item->>'total_amount')::numeric ELSE 0 END)) > 0 OR ABS(SUM(CASE WHEN lat.each_item->>'credit_or_debit' = 'R' THEN (lat.each_item->>'total_amount')::numeric ELSE 0 END)) > 0")).
		Offset(uint64(reqMetadata.Skip * reqMetadata.Limit)).
		Limit(uint64(reqMetadata.Limit))

	return dblib.SelectRows(ctx, ur.Db, query, pgx.RowToStructByNameLax[domain.DdoDetail])
}

func (ur *PaogenRepository) GetDDOdetailRepo(gctx *gin.Context, req *domain.DdoDetailRequest, reqMetadata port.MetaDataRequest) ([]domain.DdoDetail, error) {
	ctx, cancel := context.WithTimeout(gctx.Request.Context(), ur.Cfg.GetDuration("db.QueryTimeoutMed"))
	defer cancel()
	log.Debug(gctx, "Came inside getOfficeNameRepo")
	Date, err := time.Parse("2006-01-02", req.Date)
	if err != nil {
		return nil, fmt.Errorf("invalid FromDate: %v", err)
	}

	query := dblib.Psql.Select("offi.ddo_code as ddo_code, offi.ddo_name as ddo_office_name, c.business_date as business_date, c.closing_bal as closing_bal, c.opening_bal as opening_bal, coalesce(m.hoa, '999999999999999') as hoa, coalesce(m.hoa_description, 'Hoa description not available') as hoa_description, c.created_date as created_date").
		Column(sq.Expr("SUM(CASE WHEN lat.each_item->>'credit_or_debit' = 'P' THEN (lat.each_item->>'total_amount')::numeric ELSE 0 END) AS payment")).
		Column(sq.Expr("SUM(CASE WHEN lat.each_item->>'credit_or_debit' = 'R' THEN (lat.each_item->>'total_amount')::numeric ELSE 0 END) AS receipt")).
		Column(sq.Expr("json_agg(json_build_object('account_code', lat.each_item->>'account_code','account_code_description', lat.each_item->>'account_code_description','payment', CASE WHEN lat.each_item->>'credit_or_debit' = 'P' THEN (lat.each_item->>'total_amount')::numeric ELSE 0 END,'receipt', CASE WHEN lat.each_item->>'credit_or_debit' = 'R' THEN (lat.each_item->>'total_amount')::numeric ELSE 0 END)) FILTER (WHERE (CASE WHEN lat.each_item->>'credit_or_debit' = 'P' THEN (lat.each_item->>'total_amount')::numeric ELSE 0 END) != 0 OR (CASE WHEN lat.each_item->>'credit_or_debit' = 'R' THEN (lat.each_item->>'total_amount')::numeric ELSE 0 END)!= 0) AS account_array")).
		From("pao.ddo_master offi").
		LeftJoin("pao.kafka_cash_book AS c ON offi.ddo_office_id = c.office_id AND c.business_date = $1", Date).
		CrossJoin("json_array_elements(c.details) AS lat(each_item)").
		LeftJoin("pao.kafka_account_codes_master AS m ON (lat.each_item->>'account_code') = m.account_code").
		Where("offi.ddo_code = $2", req.DdoCode).
		GroupBy("offi.ddo_code", "offi.ddo_name", "c.business_date", "c.closing_bal", "c.opening_bal", "m.hoa", "m.hoa_description", "c.created_date").
		OrderBy("offi.ddo_code").
		Having(sq.Expr("ABS(SUM(CASE WHEN lat.each_item->>'credit_or_debit' = 'P' THEN (lat.each_item->>'total_amount')::numeric ELSE 0 END)) > 0 OR ABS(SUM(CASE WHEN lat.each_item->>'credit_or_debit' = 'R' THEN (lat.each_item->>'total_amount')::numeric ELSE 0 END)) > 0")).
		Offset(uint64(reqMetadata.Skip * reqMetadata.Limit)).
		Limit(uint64(reqMetadata.Limit))

	return dblib.SelectRows(ctx, ur.Db, query, pgx.RowToStructByNameLax[domain.DdoDetail])
}

func (ur *PaogenRepository) GetPfmsRepo(gctx *gin.Context, cbds []domain.CbData) (*domain.Xml, error) { // *domain.Order,

	ctx, cancel := context.WithTimeout(gctx.Request.Context(), ur.Cfg.GetDuration("db.QueryTimeoutMed"))
	defer cancel()
	var xml domain.Xml
	// Convert cbds slice to a string
	var cbdsStringSlice []string
	for _, cbd := range cbds {
		cbdJSON, err := json.Marshal(cbd)
		if err != nil {
			return nil, err
		}
		cbdsStringSlice = append(cbdsStringSlice, string(cbdJSON))
	}
	cbdsString := strings.Join(cbdsStringSlice, ",")

	query := dblib.Psql.Select("xml_output1", "uniqueid").
		From(fmt.Sprintf("pao.generate_pfms_xml('[%s]')", cbdsString)).
		Limit(1)

	err := pgx.BeginFunc(ctx, ur.Db, func(tx pgx.Tx) error {

		sql, args, err := query.ToSql()
		if err != nil {
			return err
		}
		println(sql, args)

		var pfmsOut string

		err = tx.QueryRow(ctx, sql, args...).Scan(
			&pfmsOut,
			&xml.UniqueIdentifier,
		)

		if err != nil {
			if err == pgx.ErrNoRows {
				return errors.New("data not found")
			}
			return err
		}
		cleanedXML := strings.ReplaceAll(pfmsOut, `=\"`, `="`)
		cleanedXML = strings.ReplaceAll(cleanedXML, `\\`, ``)
		xml.Pfms = null.StringFrom(cleanedXML)

		return nil
	})

	if err != nil {
		return nil, err
	}
	return &xml, err

}

func (ur *PaogenRepository) GetPfmspendingRepo03072026(gctx *gin.Context, req *domain.PfmsPendingRequest, reqMetadata port.MetaDataRequest) ([]domain.PfmsPending, error) {
	ctx, cancel := context.WithTimeout(gctx.Request.Context(), ur.Cfg.GetDuration("db.QueryTimeoutMed"))
	defer cancel()
	log.Debug(gctx, "Came inside getOfficeNameRepo")

	fromDate, err := time.Parse("2006-01-02", req.FromDate)
	if err != nil {
		return nil, fmt.Errorf("invalid FromDate: %v", err)
	}
	toDate, err := time.Parse("2006-01-02", req.ToDate)
	if err != nil {
		return nil, fmt.Errorf("invalid ToDate: %v", err)
	}

	query := sq.Select("pm.pfms_ddo_id as pfms_ddo_id,dm.ddo_office_id as ddo_office_id", "dm.ddo_name as ddo_name", "dm.ddo_code as ddo_code", "cb.business_date as business_date", "COALESCE(pm.h_cash_book_receive_flag, 'false') as h_cash_book_receive_flag", "COALESCE(pm.h_verification_flag, 'false') as h_verification_flag", "COALESCE(pm.h_pfms_generation_flag, 'false') as h_pfms_generation_flag").
		From("pao.ddo_master as dm").
		InnerJoin("pao.kafka_cash_book as cb on dm.ddo_office_id = cb.office_id").
		LeftJoin("pao.pfms_main as pm on dm.ddo_code = pm.ddo_code and cb.business_date = pm.business_date").
		Where(
			"dm.pao_code = $1 and cb.business_date BETWEEN $2 AND $3 and (pm.h_pfms_generation_flag is null or pm.h_pfms_generation_flag = 'false')",
			req.PaoCode, fromDate, toDate,
		).
		OrderBy("pm.pfms_ddo_id").
		Offset(uint64(reqMetadata.Skip * reqMetadata.Limit)).
		Limit(uint64(reqMetadata.Limit))

	return dblib.SelectRows(ctx, ur.Db, query, pgx.RowToStructByNameLax[domain.PfmsPending])
}

func (ur *PaogenRepository) GetPfmspendingRepo(gctx *gin.Context, req *domain.PfmsPendingRequest, reqMetadata port.MetaDataRequest) ([]domain.PfmsPending, error) {
	ctx, cancel := context.WithTimeout(gctx.Request.Context(), ur.Cfg.GetDuration("db.QueryTimeoutMed"))
	defer cancel()
	log.Debug(gctx, "Came inside getOfficeNameRepo")

	fromDate, err := time.Parse("2006-01-02", req.FromDate)
	if err != nil {
		return nil, fmt.Errorf("invalid FromDate: %v", err)
	}
	toDate, err := time.Parse("2006-01-02", req.ToDate)
	if err != nil {
		return nil, fmt.Errorf("invalid ToDate: %v", err)
	}

	query := sq.Select(
		"pm.pfms_ddo_id as pfms_ddo_id",
		"dm.ddo_office_id as ddo_office_id",
		"dm.ddo_name as ddo_name",
		"dm.ddo_code as ddo_code",
		"cb.business_date as business_date",
		"COALESCE(pm.h_cash_book_receive_flag, 'false') as h_cash_book_receive_flag",
		"COALESCE(pm.h_verification_flag, 'false') as h_verification_flag",
		"COALESCE(pm.h_pfms_generation_flag, 'false') as h_pfms_generation_flag",
	).
		From("pao.ddo_master as dm").
		InnerJoin("pao.kafka_cash_book as cb on dm.ddo_office_id = cb.office_id").
		LeftJoin("pao.pfms_main as pm on dm.ddo_code = pm.ddo_code and cb.business_date = pm.business_date").
		Where(
			"dm.pao_code = $1 AND cb.business_date BETWEEN $2 AND $3 AND (pm.h_pfms_generation_flag IS NULL OR pm.h_pfms_generation_flag = 'false') AND dm.valid_from <= CURRENT_DATE AND dm.valid_to >= CURRENT_DATE",
			req.PaoCode, fromDate, toDate,
		).
		OrderBy("pm.pfms_ddo_id").
		Offset(uint64(reqMetadata.Skip * reqMetadata.Limit)).
		Limit(uint64(reqMetadata.Limit))

	return dblib.SelectRows(ctx, ur.Db, query, pgx.RowToStructByNameLax[domain.PfmsPending])
}

func (ur *PaogenRepository) GetPfmsSubmissionStatusListRepo(gctx *gin.Context, req *domain.PfmsSubmissionStatusListRequest, reqMetadata port.MetaDataRequest) ([]domain.PfmsXmlPending, error) {
	ctx, cancel := context.WithTimeout(gctx.Request.Context(), ur.Cfg.GetDuration("db.QueryTimeoutMed"))
	defer cancel()
	log.Debug(gctx, "Came inside GetPfmsxmlRepo")
	var u1 domain.PfmsXmlPending
	columns := dblib.GenerateColumnsFromStruct(u1, "select")

	var baseSelect sq.SelectBuilder
	if req.Status == "All" {
		// Without pfms_submission_flag filter
		baseSelect = dblib.Psql.Select("a.pao_code", "a.ddo_code", "c.ddo_name", "business_date",
			"h_pfms_generation_flag", "a.pfms_unique_id", "a.pfms_submission_flag", "a.pfms_error_description", "a.te_number").
			From("pao.pfms_main a").
			LeftJoin("pao.ddo_master AS c ON a.ddo_code = c.ddo_code").
			Where("a.pao_code = $1 AND a.business_date >= $2 AND a.business_date <= $3 AND a.h_pfms_generation_flag = true",
				req.PaoCode, req.FromDate, req.ToDate)
	} else {
		// With pfms_submission_flag filter
		baseSelect = dblib.Psql.Select("a.pao_code", "a.ddo_code", "c.ddo_name", "business_date",
			"h_pfms_generation_flag", "a.pfms_unique_id", "a.pfms_submission_flag", "a.pfms_error_description", "a.te_number").
			From("pao.pfms_main a").
			LeftJoin("pao.ddo_master AS c ON a.ddo_code = c.ddo_code").
			Where("a.pao_code = $1 AND a.business_date >= $2 AND a.business_date <= $3 AND a.h_pfms_generation_flag = true AND a.pfms_submission_flag = $4",
				req.PaoCode, req.FromDate, req.ToDate, req.Status)
	}

	query := dblib.Psql.Select(columns...).
		FromSelect(baseSelect, "t").
		OrderBy("business_date", "ddo_code").
		Offset(uint64(reqMetadata.Skip * reqMetadata.Limit)).
		Limit(uint64(reqMetadata.Limit))

	return dblib.SelectRows(ctx, ur.Db, query, pgx.RowToStructByNameLax[domain.PfmsXmlPending])

}

func (ur *PaogenRepository) GetPfmsxmlteRepo(gctx *gin.Context, req *domain.PfmsSubmissionStatusListRequest, reqMetadata port.MetaDataRequest) ([]domain.PfmsXmlTePending, error) {
	ctx, cancel := context.WithTimeout(gctx.Request.Context(), ur.Cfg.GetDuration("db.QueryTimeoutMed"))
	defer cancel()
	log.Debug(gctx, "Came inside GetPfmsxmlteRepo")
	var u1 domain.PfmsXmlTePending

	columns := dblib.GenerateColumnsFromStruct(u1, "select")

	var baseSelect sq.SelectBuilder
	if req.Status == "All" {
		// Without pfms_submission_flag filter
		baseSelect = dblib.Psql.Select("DISTINCT a.transfer_entry_id", "a.pao_code", "a.created_date as business_date", "a.h_pfms_generation_flag", "a.pfms_unique_id", "a.pfms_submission_flag", "a.pfms_error_description", "a.te_number").
			From("pao.transfer_entry a").
			Where("a.pao_code = $1 and a.created_date >= $2 and a.created_date <= $3 AND a.h_pfms_generation_flag = true", req.PaoCode, req.FromDate, req.ToDate)
	} else {
		// With pfms_submission_flag filter
		baseSelect = dblib.Psql.Select("DISTINCT a.transfer_entry_id", "a.pao_code", "a.created_date as business_date", "a.h_pfms_generation_flag", "a.pfms_unique_id", "a.pfms_submission_flag", "a.pfms_error_description", "a.te_number").
			From("pao.transfer_entry a").
			Where("a.pao_code = $1 and a.created_date >= $2 and a.created_date <= $3 AND a.h_pfms_generation_flag = true AND a.pfms_submission_flag = $4", req.PaoCode, req.FromDate, req.ToDate, req.Status)
	}

	query := dblib.Psql.Select(columns...).
		FromSelect(baseSelect, "t").
		OrderBy("business_date").
		Offset(uint64(reqMetadata.Skip * reqMetadata.Limit)).
		Limit(uint64(reqMetadata.Limit))

	return dblib.SelectRows(ctx, ur.Db, query, pgx.RowToStructByNameLax[domain.PfmsXmlTePending])

}

func (ur *PaogenRepository) PostPfmsverifiedRepo(gctx *gin.Context, request []domain.PfmsVerified) error {
	ctx, cancel := context.WithTimeout(gctx.Request.Context(), ur.Cfg.GetDuration("db.QueryTimeoutMed"))
	defer cancel()
	log.Debug(gctx, "Came inside PostPfmsverifiedRepo")

	// Load Indian Standard Time (IST) location
	ist, err := time.LoadLocation("Asia/Kolkata")
	if err != nil {
		return err
	}

	// Get current time in IST
	verified_date := time.Now().In(ist)

	batch := &pgx.Batch{}

	updateBuilder := dblib.Psql.Update("pao.pfms_main").
		Set("closing_bal", request[0].ClosingBal).
		Set("opening_bal", request[0].OpeningBal).
		Set("verified_by", request[0].VerifiedBy).
		Set("h_verification_flag", request[0].VerificationStatus).
		Set("verified_date", verified_date).
		Where(sq.And{sq.Eq{"ddo_code": request[0].DdoCode}, sq.Eq{"business_date": request[0].BusinessDate}})

	err1 := dblib.QueueExecRow(batch, updateBuilder)
	if err1 != nil {
		return err1
	}

	for _, t := range request {
		insertBuilder := dblib.Psql.Insert("pao.pfms_detail").
			Columns("pfms_ddo_id", "hoa", "receipt", "payment", "account_code_detail").
			Values(t.DdoCode.String+t.BusinessDate.Format("20060102"), t.Hoa, t.Receipt, t.Payment, t.AccountCodeArray)
		err := dblib.QueueExecRow(batch, insertBuilder)
		if err != nil {
			return err
		}

	}

	results := ur.Db.SendBatch(ctx, batch)
	if results != nil {
		defer results.Close()

		for i := 0; i < batch.Len(); i++ {
			_, err := results.Exec()
			if err != nil {
				log.Debug(gctx, "Error executing batch command:", err)
				return err
			}
		}
	}

	return nil
}

func (ur *PaogenRepository) GetDDOdetail_monthlyRepo19062026(gctx *gin.Context, req *domain.DdoDetailMonthlyRequest, reqMetadata port.MetaDataRequest) ([]domain.DdoDetailMonthly, error) {
	ctx, cancel := context.WithTimeout(gctx.Request.Context(), ur.Cfg.GetDuration("db.QueryTimeoutMed"))
	defer cancel()
	log.Debug(gctx, "Came inside getOfficeNameRepo")

	query :=
		dblib.Psql.Select("coalesce(a.ddo_code, v.ddo_code) as ddo_code,coalesce(a.office_name, v.office_name) as office_name,"+
			"coalesce(a.opening_bal, 0) as opening_bal,coalesce(a.closing_bal, 0) as closing_bal,"+
			"coalesce(a.period, v.period) as period,coalesce(v.hoa, a.hoa) as hoa,coalesce(v.hoa_description, a.hoa_description) as hoa_description,"+
			"coalesce(a.payment, 0) as payment,"+
			"coalesce(a.receipt, 0) as receipt,coalesce(v.TE_Payment, 0) as te_payment,"+
			"coalesce(v.TE_receipt, 0) as te_receipt,account_array").
			FromSelect(
				dblib.Psql.Select("dm.ddo_code,dm.ddo_name as office_name,ca.opening_bal,ca.closing_bal, REPLACE(ca.period, '-', '') AS period,coalesce(m.hoa, '999999999999999') as hoa,m.hoa_description").
					Column(sq.Expr("SUM(CASE WHEN lat.each_item->>'credit_debit' = 'P' THEN (lat.each_item->>'Grand_total')::numeric ELSE 0 END) AS payment")).
					Column(sq.Expr("SUM(CASE WHEN lat.each_item->>'credit_debit' = 'R' THEN (lat.each_item->>'Grand_total')::numeric ELSE 0 END) AS receipt")).
					Column(sq.Expr("json_agg(json_build_object('account_code', lat.each_item->>'account_code','account_code_description', lat.each_item->>'account_code_description','receipt', CASE WHEN lat.each_item->>'credit_debit' = 'R' THEN (lat.each_item->>'Grand_total')::numeric ELSE 0 END,'payment', CASE WHEN lat.each_item->>'credit_debit' = 'P' THEN (lat.each_item->>'Grand_total')::numeric ELSE 0 END)) as account_array")).
					From("pao.ddo_master dm").
					LeftJoin("pao.kafka_cash_account AS ca ON dm.ddo_office_id = ca.office_id AND REPLACE(ca.period, '-', '') = $1", req.Period).
					CrossJoin("json_array_elements(ca.result_array) AS lat(each_item)").
					LeftJoin("pao.kafka_account_codes_master AS m ON (lat.each_item->>'account_code') = m.account_code").
					// LeftJoin("(select  CASE WHEN COUNT(*) = SUM(CASE WHEN h_pfms_generation_flag THEN 1 ELSE 0 END) THEN true ELSE false "+
					// 	"END AS h_pfms_generation_flag, ddo_code from pao.pfms_main where ddo_code = $2 AND to_char(business_date, 'MMYYYY') = $3 GROUP BY ddo_code)  n ON dm.ddo_code = n.ddo_code ", req.DdoCode, req.Period).
					// LeftJoin("pao.office_master AS mm ON dm.ddo_office_id = mm.office_id").
					Where("dm.ddo_code = $2", req.DdoCode).
					GroupBy("dm.ddo_code", "dm.ddo_name", "ca.period", "ca.closing_bal", "ca.opening_bal", "m.hoa", "m.hoa_description"), "a")

	// Create a subquery for the RIGHT JOIN part
	subquery, _, err := sq.Select("hoa", "hoa_description", "ddo_code", "office_name", "SUM(TE_Payment) AS TE_Payment", "SUM(TE_receipt) AS TE_receipt", "period").
		FromSelect(
			dblib.Psql.Select("a.hoa", "hoa_description", "a.ddo_code", "dm.ddo_name as office_name", "case when transfer_type = 'C' then transfer_amount else 0 end as TE_receipt").
				Column("case when transfer_type = 'D' then transfer_amount else 0 end as TE_Payment").
				Column("TO_CHAR(a.created_date, 'MMYYYY') as period").
				From("pao.transfer_entry as a").
				LeftJoin("(select distinct hoa,hoa_description from pao.kafka_account_codes_master) AS m ON m.hoa=a.hoa").
				LeftJoin("pao.ddo_master dm  on a.ddo_code = dm.ddo_code ").
				Where("TO_CHAR(created_date, 'MMYYYY') = $3 AND a.ddo_code = $4"), "q").
		GroupBy("hoa", "hoa_description", "ddo_code", "office_name", "period").ToSql()

	if err != nil {
		return nil, err
	}

	// Combine LEFT JOIN and RIGHT JOIN results
	query = query.JoinClause("FULL OUTER JOIN ("+subquery+") as v ON a.hoa = v.hoa", req.Period, req.DdoCode).
		OrderBy("ddo_code").Offset(uint64(reqMetadata.Skip * reqMetadata.Limit)).
		Where("coalesce(a.payment, 0) != 0 OR coalesce(a.receipt, 0) != 0 OR coalesce(v.TE_Payment, 0) != 0 OR coalesce(v.TE_receipt, 0) != 0").
		Limit(uint64(reqMetadata.Limit))

	return dblib.SelectRows(ctx, ur.Db, query, pgx.RowToStructByNameLax[domain.DdoDetailMonthly])
}

func (ur *PaogenRepository) GetDDOdetail_monthlyRepo(gctx *gin.Context, req *domain.DdoDetailMonthlyRequest, reqMetadata port.MetaDataRequest) ([]domain.DdoDetailMonthly, error) {
	ctx, cancel := context.WithTimeout(gctx.Request.Context(), ur.Cfg.GetDuration("db.QueryTimeoutMed"))
	defer cancel()
	log.Debug(gctx, "Came inside getOfficeNameRepo")

	query :=
		dblib.Psql.Select("coalesce(a.ddo_code, v.ddo_code) as ddo_code,coalesce(a.office_name, v.office_name) as office_name,"+
			"coalesce(a.opening_bal, 0) as opening_bal,coalesce(a.closing_bal, 0) as closing_bal,"+
			"coalesce(a.period, v.period) as period,coalesce(v.hoa, a.hoa) as hoa,coalesce(v.hoa_description, a.hoa_description) as hoa_description,"+
			"coalesce(a.payment, 0) as payment,"+
			"coalesce(a.receipt, 0) as receipt,coalesce(v.TE_Payment, 0) as te_payment,"+
			"coalesce(v.TE_receipt, 0) as te_receipt,account_array").
			FromSelect(
				dblib.Psql.Select("dm.ddo_code,dm.ddo_name as office_name,ca.opening_bal,ca.closing_bal, REPLACE(ca.period, '-', '') AS period,coalesce(m.hoa, '999999999999999') as hoa,m.hoa_description").
					Column(sq.Expr("SUM(CASE WHEN lat.each_item->>'credit_debit' = 'P' THEN (lat.each_item->>'Grand_total')::numeric ELSE 0 END) AS payment")).
					Column(sq.Expr("SUM(CASE WHEN lat.each_item->>'credit_debit' = 'R' THEN (lat.each_item->>'Grand_total')::numeric ELSE 0 END) AS receipt")).
					Column(sq.Expr("json_agg(json_build_object('account_code', lat.each_item->>'account_code','account_code_description', lat.each_item->>'account_code_description','receipt', CASE WHEN lat.each_item->>'credit_debit' = 'R' THEN (lat.each_item->>'Grand_total')::numeric ELSE 0 END,'payment', CASE WHEN lat.each_item->>'credit_debit' = 'P' THEN (lat.each_item->>'Grand_total')::numeric ELSE 0 END)) as account_array")).
					From("pao.ddo_master dm").
					LeftJoin("pao.kafka_cash_account AS ca ON dm.ddo_office_id = ca.office_id AND REPLACE(ca.period, '-', '') = $1", req.Period).
					CrossJoin("json_array_elements(ca.result_array) AS lat(each_item)").
					LeftJoin("pao.kafka_account_codes_master AS m ON (lat.each_item->>'account_code') = m.account_code").
					// LeftJoin("(select  CASE WHEN COUNT(*) = SUM(CASE WHEN h_pfms_generation_flag THEN 1 ELSE 0 END) THEN true ELSE false "+
					// 	"END AS h_pfms_generation_flag, ddo_code from pao.pfms_main where ddo_code = $2 AND to_char(business_date, 'MMYYYY') = $3 GROUP BY ddo_code)  n ON dm.ddo_code = n.ddo_code ", req.DdoCode, req.Period).
					// LeftJoin("pao.office_master AS mm ON dm.ddo_office_id = mm.office_id").
					Where("dm.ddo_code = $2", req.DdoCode).
					GroupBy("dm.ddo_code", "dm.ddo_name", "ca.period", "ca.closing_bal", "ca.opening_bal", "m.hoa", "m.hoa_description"), "a")

	// Create a subquery for the RIGHT JOIN part
	subquery, _, err := sq.Select("hoa", "hoa_description", "ddo_code", "office_name", "SUM(TE_Payment) AS TE_Payment", "SUM(TE_receipt) AS TE_receipt", "period").
		FromSelect(
			dblib.Psql.Select("a.hoa", "hoa_description", "a.ddo_code", "dm.ddo_name as office_name", "case when transfer_type = 'C' then transfer_amount else 0 end as TE_receipt").
				Column("case when transfer_type = 'D' then transfer_amount else 0 end as TE_Payment").
				Column("TO_CHAR(a.created_date, 'MMYYYY') as period").
				From("pao.transfer_entry as a").
				LeftJoin("(select distinct hoa,hoa_description from pao.kafka_account_codes_master) AS m ON m.hoa=a.hoa").
				LeftJoin("pao.ddo_master dm  on a.ddo_code = dm.ddo_code ").
				Where("TO_CHAR(created_date, 'MMYYYY') = $3 AND a.ddo_code = $4"), "q").
		GroupBy("hoa", "hoa_description", "ddo_code", "office_name", "period").ToSql()

	if err != nil {
		return nil, err
	}

	// Combine LEFT JOIN and RIGHT JOIN results
	query = query.JoinClause("FULL OUTER JOIN ("+subquery+") as v ON a.hoa = v.hoa", req.Period, req.DdoCode).
		Where("coalesce(a.payment, 0) != 0 OR coalesce(a.receipt, 0) != 0 OR coalesce(v.TE_Payment, 0) != 0 OR coalesce(v.TE_receipt, 0) != 0").
		OrderBy(`
        CASE
            WHEN coalesce(a.opening_bal,0)=0
             AND coalesce(a.closing_bal,0)=0
             AND (coalesce(v.TE_Payment,0) <> 0 OR coalesce(v.TE_receipt,0) <> 0)
            THEN 1
            ELSE 0
        END`,
			"ddo_code",
			"hoa",
		).
		Offset(uint64(reqMetadata.Skip * reqMetadata.Limit)).
		Limit(uint64(reqMetadata.Limit))

	return dblib.SelectRows(ctx, ur.Db, query, pgx.RowToStructByNameLax[domain.DdoDetailMonthly])
}

func (ur *PaogenRepository) PostPfmsMonthlyverifiedRepo(gctx *gin.Context, request []domain.PfmsVerifiedMonthly) error {
	ctx, cancel := context.WithTimeout(gctx.Request.Context(), ur.Cfg.GetDuration("db.QueryTimeoutMed"))
	defer cancel()
	log.Debug(gctx, "Came inside Post PfmsverifiedRepo")

	// Load Indian Standard Time (IST) location
	ist, err := time.LoadLocation("Asia/Kolkata")
	if err != nil {
		return err
	}

	// Get current time in IST
	verified_date := time.Now().In(ist)

	batch := &pgx.Batch{}

	pfmsmainupdateQuery := dblib.Psql.Update("pao.pfms_monthly_main").
		Set("pfms_ddo_id", request[0].DdoCode+request[0].Period).
		Set("closing_bal", request[0].ClosingBal).
		Set("opening_bal", request[0].OpeningBal).
		Set("verified_by", request[0].VerifiedBy).
		Set("h_verification_flag", request[0].VerificationStatus).
		Set("verified_date", verified_date).
		Where(sq.And{sq.Eq{"ddo_code": request[0].DdoCode}, sq.Eq{"period": request[0].Period}})

	err1 := dblib.QueueExecRow(batch, pfmsmainupdateQuery)
	if err1 != nil {
		return err1
	}

	for _, t := range request {
		insertBuilder := dblib.Psql.Insert("pao.pfms_monthly_detail").
			Columns("pfms_ddo_id", "hoa", "receipt", "payment", "account_code_detail", "te_payment", "te_receipt").
			Values(t.DdoCode+t.Period, t.Hoa, t.Receipt, t.Payment, t.AccountCodeArray, t.TePayment, t.TeReceipt)
		err := dblib.QueueExecRow(batch, insertBuilder)
		if err != nil {
			return err
		}

	}
	periodInt, err := strconv.Atoi(request[0].Period)
	if err != nil {
		// Handle the error if the string cannot be converted to an integer

		log.Debug(gctx, "Error converting period to integer:", err)
		return err
	}
	for _, broadsheet := range request {
		// Check if the first four characters of hoa match any of the specified numbers
		if len(broadsheet.Hoa) >= 4 && (broadsheet.Hoa[:4] == "8782" ||
			broadsheet.Hoa[:4] == "8661" ||
			broadsheet.Hoa[:4] == "8001" ||
			broadsheet.Hoa[:4] == "8002" ||
			broadsheet.Hoa[:4] == "8553" ||
			broadsheet.Hoa[:4] == "8446" ||
			broadsheet.Hoa[:4] == "8670" ||
			broadsheet.Hoa[:4] == "8677") {
			// If the condition is met, execute the query
			broadsheetQuery := dblib.Psql.Insert("pao.broad_sheet").
				Columns("broadsheet_month", "hoa", "ddo_code", "credit_amount", "debit_amount", "created_date", "created_by").
				Values(broadsheet.Period, broadsheet.Hoa, broadsheet.DdoCode, broadsheet.Receipt, broadsheet.Payment, verified_date, broadsheet.VerifiedBy).
				Suffix("RETURNING broadsheet_month")

			err := dblib.QueueExecRow(batch, broadsheetQuery)
			if err != nil {
				return err
			}

		}

		if broadsheet.Hoa[:4] == "8782" ||
			broadsheet.Hoa[:4] == "8553" {
			updateBuilder := dblib.Psql.Update("pao.broad_sheet as t1").
				Set("opening_balance", sq.Expr("t2.prev_closing_balance")).
				Set("closing_balance", sq.Expr("t2.prev_closing_balance - t1.credit_amount + t1.debit_amount")).
				FromSelect(
					dblib.Psql.Select("broadsheet_month").
						Column("COALESCE(LAG(closing_balance)  OVER (ORDER BY broadsheet_month ::integer) ,0) AS prev_closing_balance").
						Column("COALESCE(LAG(opening_balance) OVER (ORDER BY broadsheet_month ::integer) ,0) AS prev_opening_balance").
						From("pao.broad_sheet").
						Where(sq.Eq{"hoa": broadsheet.Hoa}).
						Where(sq.LtOrEq{"(broadsheet_month::integer)": periodInt}).
						Where(sq.Eq{"ddo_code": broadsheet.DdoCode}), "t2").
				Where(sq.Eq{"t1.broadsheet_month::integer": broadsheet.Period}).
				Where("t1.broadsheet_month = t2.broadsheet_month").
				Where(sq.Eq{"t1.hoa": broadsheet.Hoa}).
				Where(sq.Eq{"t1.ddo_code": broadsheet.DdoCode})

			err := dblib.QueueExecRow(batch, updateBuilder)
			if err != nil {
				return err
			}

		} else if broadsheet.Hoa[:4] == "8001" || broadsheet.Hoa[:4] == "8002" ||
			broadsheet.Hoa[:4] == "8446" {
			updateBuilder := dblib.Psql.Update("pao.broad_sheet as t1").
				Set("opening_balance", sq.Expr("t2.prev_closing_balance")).
				Set("closing_balance", sq.Expr("t2.prev_closing_balance + t1.credit_amount - t1.debit_amount")).
				FromSelect(
					dblib.Psql.Select("broadsheet_month").
						Column("COALESCE(LAG(closing_balance)  OVER (ORDER BY broadsheet_month ::integer) ,0) AS prev_closing_balance").
						Column("COALESCE(LAG(opening_balance) OVER (ORDER BY broadsheet_month ::integer) ,0) AS prev_opening_balance").From("pao.broad_sheet").
						Where(sq.Eq{"hoa": broadsheet.Hoa}).
						Where(sq.LtOrEq{"(broadsheet_month::integer)": periodInt}).
						Where(sq.Eq{"ddo_code": broadsheet.DdoCode}), "t2").
				Where(sq.Eq{"t1.broadsheet_month::integer": broadsheet.Period}).
				Where("t1.broadsheet_month = t2.broadsheet_month").
				Where(sq.Eq{"t1.hoa": broadsheet.Hoa}).
				Where(sq.Eq{"t1.ddo_code": broadsheet.DdoCode})

			err := dblib.QueueExecRow(batch, updateBuilder)
			if err != nil {
				return err
			}

		} else if broadsheet.Hoa[:4] == "8661" {
			updateBuilder := dblib.Psql.Update("pao.broad_sheet as t1").
				Set("opening_balance", sq.Expr("t2.prev_closing_balance")).
				Set("closing_balance", sq.Expr("t2.prev_closing_balance + t1.credit_amount + t1.debit_amount")).
				FromSelect(
					dblib.Psql.Select("broadsheet_month").
						Column("COALESCE(LAG(closing_balance)  OVER (ORDER BY broadsheet_month ::integer) ,0) AS prev_closing_balance").
						Column("COALESCE(LAG(opening_balance) OVER (ORDER BY broadsheet_month ::integer) ,0) AS prev_opening_balance").From("pao.broad_sheet").
						Where(sq.Eq{"hoa": broadsheet.Hoa}).
						Where(sq.LtOrEq{"(broadsheet_month::integer)": periodInt}).
						Where(sq.Eq{"ddo_code": broadsheet.DdoCode}), "t2").
				Where(sq.Eq{"t1.broadsheet_month::integer": broadsheet.Period}).
				Where("t1.broadsheet_month = t2.broadsheet_month").
				Where(sq.Eq{"t1.hoa": broadsheet.Hoa}).
				Where(sq.Eq{"t1.ddo_code": broadsheet.DdoCode})
			err := dblib.QueueExecRow(batch, updateBuilder)
			if err != nil {
				return err
			}

		} else if broadsheet.Hoa[:4] == "8670" {
			updateBuilder := dblib.Psql.Update("pao.broad_sheet as t1").
				Set("opening_balance", sq.Expr("t2.prev_closing_balance")).
				Set("closing_balance", sq.Expr("t2.prev_closing_balance + t1.credit_amount")).
				FromSelect(
					dblib.Psql.Select("broadsheet_month").
						Column("COALESCE(LAG(closing_balance)  OVER (ORDER BY broadsheet_month ::integer) ,0) AS prev_closing_balance").
						Column("COALESCE(LAG(opening_balance) OVER (ORDER BY broadsheet_month ::integer) ,0) AS prev_opening_balance").From("pao.broad_sheet").
						Where(sq.Eq{"hoa": broadsheet.Hoa}).
						Where(sq.LtOrEq{"(broadsheet_month::integer)": periodInt}).
						Where(sq.Eq{"ddo_code": broadsheet.DdoCode}), "t2").
				Where(sq.Eq{"t1.broadsheet_month::integer": broadsheet.Period}).
				Where("t1.broadsheet_month = t2.broadsheet_month").
				Where(sq.Eq{"t1.hoa": broadsheet.Hoa}).
				Where(sq.Eq{"t1.ddo_code": broadsheet.DdoCode})
			err := dblib.QueueExecRow(batch, updateBuilder)
			if err != nil {
				return err
			}

		} else if broadsheet.Hoa[:4] == "8677" {
			updateBuilder := dblib.Psql.Update("pao.broad_sheet as t1").
				Set("opening_balance", sq.Expr("t2.prev_closing_balance")).
				Set("closing_balance", sq.Expr("t2.prev_closing_balance + t1.debit_amount")).
				FromSelect(
					dblib.Psql.Select("broadsheet_month").
						Column("COALESCE(LAG(closing_balance)  OVER (ORDER BY broadsheet_month ::integer) ,0) AS prev_closing_balance").
						Column("COALESCE(LAG(opening_balance) OVER (ORDER BY broadsheet_month ::integer) ,0) AS prev_opening_balance").From("pao.broad_sheet").
						Where(sq.Eq{"hoa": broadsheet.Hoa}).
						Where(sq.LtOrEq{"(broadsheet_month::integer)": periodInt}).
						Where(sq.Eq{"ddo_code": broadsheet.DdoCode}), "t2").
				Where(sq.Eq{"t1.broadsheet_month::integer": broadsheet.Period}).
				Where("t1.broadsheet_month = t2.broadsheet_month").
				Where(sq.Eq{"t1.hoa": broadsheet.Hoa}).
				Where(sq.Eq{"t1.ddo_code": broadsheet.DdoCode})

			err := dblib.QueueExecRow(batch, updateBuilder)
			if err != nil {
				return err
			}

		}
	}
	results := ur.Db.SendBatch(ctx, batch)
	if results != nil {
		defer results.Close()

		for i := 0; i < batch.Len(); i++ {
			_, err := results.Exec()
			if err != nil {
				log.Debug(gctx, "Error executing batch command:", err)
				return err
			}
		}
	}

	return nil

}

func (ur *PaogenRepository) GetDDOlistMonthlyRepo1303(gctx *gin.Context, req *domain.DdoListRequestMonthly, reqMetadata port.MetaDataRequest) ([]domain.PfmsStatusMonthly, error) {
	ctx, cancel := context.WithTimeout(gctx.Request.Context(), ur.Cfg.GetDuration("db.QueryTimeoutMed"))
	defer cancel()
	log.Debug(gctx, "Came inside getOfficeNameRepo")

	sq1, _, err := sq.Select("pfms_ddo_id", "pao_code", "ddo_code", "ddo_name", "h_cash_account_receive_flag", "h_verification_flag", "verified_date", "period", "opening_bal", "closing_bal", "verified_by").
		From("pao.pfms_monthly_main").
		Where("period = $1").ToSql()
	if err != nil {
		return nil, err
	}
	query := sq.Select("ddo.ddo_code", "COALESCE(ddo.ddo_name, 'NA') as ddo_name", "COALESCE(pfms.h_cash_account_receive_flag, 'false') as h_cash_account_receive_flag", "COALESCE(pfms.h_verification_flag, 'false') as h_verification_flag").
		From("pao.ddo_master as ddo").
		LeftJoin("("+sq1+") AS pfms ON ddo.ddo_code = pfms.ddo_code", req.Period).
		Where("ddo.pao_code = $2", req.PaoCode).
		OrderBy("ddo.ddo_code").Offset(uint64(reqMetadata.Skip * reqMetadata.Limit)).
		Limit(uint64(reqMetadata.Limit))

	results, err := dblib.SelectRows(ctx, ur.Db, query, pgx.RowToStructByNameLax[domain.PfmsStatusMonthly])
	if err != nil {
		return nil, err
	}
	for i := range results {
		results[i].Period = null.StringFrom(req.Period)
	}

	return results, nil
}

func (ur *PaogenRepository) GetDDOlistMonthlyRepo(
	gctx *gin.Context,
	req *domain.DdoListRequestMonthly,
	reqMetadata port.MetaDataRequest,
) ([]domain.PfmsStatusMonthly, error) {
	ctx, cancel := context.WithTimeout(
		gctx.Request.Context(),
		ur.Cfg.GetDuration("db.QueryTimeoutMed"),
	)
	defer cancel()
	log.Debug(gctx, "Came inside GetDDOlistMonthlyRepo")

	const query = `
        SELECT
            ddo.ddo_code,
            COALESCE(ddo.ddo_name, 'NA')                                AS ddo_name,
            COALESCE(pfms.h_cash_account_receive_flag, false)           AS h_cash_account_receive_flag,
            COALESCE(pfms.h_verification_flag, false)                   AS h_verification_flag
        FROM pao.ddo_master AS ddo
        LEFT JOIN (
            SELECT
                ddo_code,
                h_cash_account_receive_flag,
                h_verification_flag
            FROM pao.pfms_monthly_main
            WHERE period = $1
        ) AS pfms ON ddo.ddo_code = pfms.ddo_code
        WHERE ddo.pao_code = $2
          AND ddo.valid_from <= (to_date($1, 'MMYYYY') + INTERVAL '1 month' - INTERVAL '1 day')
          AND ddo.valid_to   >=  to_date($1, 'MMYYYY')
        ORDER BY ddo.ddo_code
        LIMIT  $3
        OFFSET $4
    `
	// $1 = period (reused 3 times — pgx handles this fine)
	// $2 = paoCode
	// $3 = limit
	// $4 = offset
	offset := reqMetadata.Skip * reqMetadata.Limit

	rows, err := ur.Db.Query(ctx, query,
		req.Period,        // $1
		req.PaoCode,       // $2
		reqMetadata.Limit, // $3
		offset,            // $4
	)
	if err != nil {
		return nil, fmt.Errorf("GetDDOlistMonthlyRepo query: %w", err)
	}
	defer rows.Close()

	results, err := pgx.CollectRows(rows, pgx.RowToStructByNameLax[domain.PfmsStatusMonthly])
	if err != nil {
		return nil, fmt.Errorf("GetDDOlistMonthlyRepo scan: %w", err)
	}

	period := null.StringFrom(req.Period)
	for i := range results {
		results[i].Period = period
	}

	return results, nil
}

func (ur *PaogenRepository) GetDDOlistMonthlyOffRepo(
	gctx *gin.Context,
	req *domain.DdoListMonthlyQuery,
	reqMetadata port.MetaDataRequest,
) ([]domain.PfmsStatusMonthly, error) {
	ctx, cancel := context.WithTimeout(
		gctx.Request.Context(),
		ur.Cfg.GetDuration("db.QueryTimeoutMed"),
	)
	defer cancel()
	log.Debug(gctx, "Came inside GetDDOlistMonthlyRepo")

	const query = `
        SELECT
            ddo.ddo_code,
            COALESCE(ddo.ddo_name, 'NA')                                AS ddo_name,
            COALESCE(pfms.h_cash_account_receive_flag, false)           AS h_cash_account_receive_flag,
            COALESCE(pfms.h_verification_flag, false)                   AS h_verification_flag
        FROM pao.ddo_master AS ddo
        LEFT JOIN (
            SELECT
                ddo_code,
                h_cash_account_receive_flag,
                h_verification_flag
            FROM pao.pfms_monthly_main
            WHERE period = $1
        ) AS pfms ON ddo.ddo_code = pfms.ddo_code
        WHERE ddo.pao_office_id = $2
          AND ddo.valid_from <= (to_date($1, 'MMYYYY') + INTERVAL '1 month' - INTERVAL '1 day')
          AND ddo.valid_to   >=  to_date($1, 'MMYYYY')
        ORDER BY ddo.ddo_code
        LIMIT  $3
        OFFSET $4
    `
	// $1 = period (reused 3 times — pgx handles this fine)
	// $2 = officeId
	// $3 = limit
	// $4 = offset
	offset := reqMetadata.Skip * reqMetadata.Limit

	rows, err := ur.Db.Query(ctx, query,
		req.Period,        // $1
		req.OfficeId,      // $2
		reqMetadata.Limit, // $3
		offset,            // $4
	)
	if err != nil {
		return nil, fmt.Errorf("GetDDOlistMonthlyRepo query: %w", err)
	}
	defer rows.Close()

	results, err := pgx.CollectRows(rows, pgx.RowToStructByNameLax[domain.PfmsStatusMonthly])
	if err != nil {
		return nil, fmt.Errorf("GetDDOlistMonthlyRepo scan: %w", err)
	}

	period := null.StringFrom(req.Period)
	for i := range results {
		results[i].Period = period
	}

	return results, nil
}

func (ur *PaogenRepository) GetDDOlistMonthlyupdateRepo(gctx *gin.Context, req *domain.DdoListRequestMonthly) error {
	ctx, cancel := context.WithTimeout(gctx.Request.Context(), ur.Cfg.GetDuration("db.QueryTimeoutMed"))
	defer cancel()

	log.Debug(gctx, "Inside GetDDOlistupdateRepo")

	// Construct the subquery (sq1)
	sq1 := sq.Select("q10.pao_code", "q10.ddo_code", "q10.ddo_name", "q10.period").
		FromSelect(
			sq.Select("dm.ddo_code", "REPLACE(kc.period, '-', '') AS period", "dm.pao_code", "dm.ddo_name").
				From("pao.kafka_cash_account kc").
				Join("pao.ddo_master dm ON kc.office_id = dm.ddo_office_id").
				Where("kc.office_id IN (SELECT ddo_office_id FROM pao.ddo_master WHERE pao_code = $1) AND REPLACE(kc.period, '-', '') = $2", req.PaoCode, req.Period),
			"q10",
		).
		LeftJoin("pao.pfms_monthly_main pm ON q10.ddo_code = pm.ddo_code AND q10.period = pm.period").
		Where("pm.ddo_code IS NULL")

	// Construct the main query (sq2)
	sq2 := sq.Select(
		"q10.ddo_code || q10.period AS pfms_ddo_id",
		"q10.pao_code",
		"q10.ddo_code",
		"q10.ddo_name",
		"true AS h_cash_account_receive_flag",
		"NULL AS h_verification_flag",
		"NULL AS verified_date",
		"q10.period",
		"NULL AS opening_bal",
		"NULL AS closing_bal",
		"NULL AS verified_by",
	).FromSelect(sq1, "q10")

	// Construct the final insert query
	query := sq.Insert("pao.pfms_monthly_main").
		Columns(
			"pfms_ddo_id", "pao_code", "ddo_code", "ddo_name", "h_cash_account_receive_flag",
			"h_verification_flag", "verified_date", "period",
			"opening_bal", "closing_bal", "verified_by",
		).
		Select(sq2)

	// Convert the final query to SQL and args
	sql, args, err := query.ToSql()
	if err != nil {
		return err
	}

	// Execute the query
	_, err = ur.Db.Exec(ctx, sql, args...)
	if err != nil {
		return err
	}

	return nil
}

func (ur *PaogenRepository) GetPostPraoAccountRepo(gctx *gin.Context, req domain.PraoAccountSubmissionRequest) ([]domain.PaoPraoAccount, error) {
	ctx, cancel := context.WithTimeout(gctx.Request.Context(), ur.Cfg.GetDuration("db.QueryTimeoutMed"))
	defer cancel()
	log.Debug(gctx, "Came inside getOfficeNameRepo")
	query := sq.Select("a.pao_code as pao_code", "b.hoa as hoa", "a.period as period").
		Column(sq.Expr("SUM(COALESCE(b.receipt, 0) + COALESCE(b.te_receipt, 0)) AS total_receipt")).
		Column(sq.Expr("SUM(COALESCE(b.payment, 0) + COALESCE(b.te_payment, 0)) AS total_payment")).
		Column(sq.Expr("json_agg(json_build_object('ddo_code', a.ddo_code, 'receipt', b.receipt, 'payment', b.payment, 'te_receipt', b.te_receipt, 'te_payment', b.te_payment)) AS ddo_array")).
		From("pao.pfms_monthly_main AS a").
		LeftJoin("pao.pfms_monthly_detail AS b ON b.pfms_ddo_id = a.pfms_ddo_id").
		Where("a.pao_code = $1", req.PaoCode).
		Where("a.period = $2", req.Period).
		Where("NOT ((COALESCE(b.receipt, 0) + COALESCE(b.te_receipt, 0)) = 0 AND (COALESCE(b.payment, 0) + COALESCE(b.te_payment, 0)) = 0)").
		GroupBy("a.pao_code", "a.period", "b.hoa")

	return dblib.SelectRows(ctx, ur.Db, query, pgx.RowToStructByNameLax[domain.PaoPraoAccount])
}
func (ur *PaogenRepository) PostPraoAccountRepo(gctx *gin.Context, req []domain.PaoPraoAccount) error {
	ctx, cancel := context.WithTimeout(gctx.Request.Context(), ur.Cfg.GetDuration("db.QueryTimeoutMed"))
	defer cancel()
	log.Debug(gctx, "Came inside getOfficeNameRepo")

	copycount, err := ur.Db.CopyFrom(
		ctx,
		pgx.Identifier{"pao", "pao_prao_account_detail"},
		[]string{"pao_code", "hoa", "period", "total_payment", "total_receipt", "ddo_array"},
		pgx.CopyFromSlice(len(req), func(i int) ([]interface{}, error) {
			return []interface{}{
				req[i].PaoCode,
				req[i].Hoa,
				req[i].Period,
				req[i].TotalPayment,
				req[i].TotalReceipt,
				req[i].DdoArray,
			}, nil
		}))

	log.Debug(gctx, "Copy Count", copycount)

	if err != nil {
		log.Debug(gctx, "Error inserting hoas:", err)
		return err
	}

	query := dblib.Psql.Insert("pao.pao_prao_account_main").Columns("pao_code", "period", "account_submissionto_prao_status").
		Values(req[0].PaoCode, req[0].Period, "submitted").Suffix("returning pao_code,period,account_submissionto_prao_status")

	p, err := dblib.Insert(ctx, ur.Db, query)
	log.Debug(gctx, p)
	return err

}
func (ur *PaogenRepository) GetPraoAccountRepo(gctx *gin.Context, req domain.PraoAccountSubmissionRequest, reqMetadata port.MetaDataRequest) ([]domain.PaoPraoAccountReply, error) {
	ctx, cancel := context.WithTimeout(gctx.Request.Context(), ur.Cfg.GetDuration("db.QueryTimeoutMed"))
	defer cancel()
	log.Debug(gctx, "Came inside getOfficeNameRepo")
	var u1 domain.PaoPraoAccountReply

	q1, _, err1 := sq.Select("DISTINCT ON (pao_code) pao_code", "pao_name").
		From("pao.ddo_master").
		Where("pao_code = $1").
		OrderBy("pao_code").ToSql()
	if err1 != nil {
		log.Debug(gctx, "Error selecting distinct hoa:", err1)
		return nil, err1
	}

	q2, _, err2 := sq.Select("DISTINCT ON (hoa) hoa", "hoa_description").
		From("pao.kafka_account_codes_master").ToSql()
	if err2 != nil {
		log.Debug(gctx, "Error selecting distinct hoa:", err2)
		return nil, err2
	}

	columns := dblib.GenerateColumnsFromStruct(u1, "select")
	query := dblib.Psql.Select(columns...).
		FromSelect(
			dblib.Psql.Select("a.pao_code,a.hoa,a.period,a.total_payment,a.total_receipt,a.ddo_array,b.pao_name,c.hoa_description").
				From("pao.pao_prao_account_detail as a").
				LeftJoin("("+q1+") AS b ON a.pao_code = b.pao_code", req.PaoCode).
				LeftJoin("("+q2+") AS c ON a.hoa = c.hoa").
				Where("a.pao_code = $2", req.PaoCode).
				Where("period = $3", req.Period), "q").
		OrderBy("pao_code").Offset(uint64(reqMetadata.Skip * reqMetadata.Limit)).
		Limit(uint64(reqMetadata.Limit))

	return dblib.SelectRows(ctx, ur.Db, query, pgx.RowToStructByNameLax[domain.PaoPraoAccountReply])

}
func (ur *PaogenRepository) PraoAccountSubStatusRepo(gctx *gin.Context, req domain.PraoAccountSubmissionRequest, reqMetadata port.MetaDataRequest) (*domain.PraoAccountSubmissionStatus, error) {
	ctx, cancel := context.WithTimeout(gctx.Request.Context(), ur.Cfg.GetDuration("db.QueryTimeoutMed"))
	defer cancel()
	log.Debug(gctx, "Came inside getOfficeNameRepo")

	// Define the subquery that fetches data from the main table
	subQuery, _, err := sq.
		Select("pao_code", "period", "account_submissionto_prao_status").
		From("pao.pao_prao_account_main").
		Where("pao_code = $1 and period = $2").
		GroupBy("pao_code", "period", "account_submissionto_prao_status").ToSql()
	if err != nil {
		log.Debug(gctx, "Error selecting prao status:", err)
		return nil, err
	}

	// Define the main query with a LEFT JOIN on the subquery
	query := sq.
		Select(
			"COALESCE(e.pao_code, 'NA') AS pao_code",
			"COALESCE(e.period, 'NA') AS period",
			"COALESCE(e.account_submissionto_prao_status, 'Not Submitted to Pr AO') AS account_submissionto_prao_status",
		).
		From("(SELECT 1 AS dummy) d").
		LeftJoin("("+subQuery+") AS e ON 1 = 1", req.PaoCode, req.Period).
		OrderBy("pao_code").Offset(uint64(reqMetadata.Skip * reqMetadata.Limit)).
		Limit(uint64(reqMetadata.Limit))

	return dblib.SelectOne(ctx, ur.Db, query, pgx.RowToAddrOfStructByName[domain.PraoAccountSubmissionStatus])
}

func (ur *PaogenRepository) PraoAccountSubStatusListRepo(gctx *gin.Context, request *domain.AccountsubmissionStatusListRequest, reqMetadata port.MetaDataRequest) ([]domain.AccountSubmissionStatusList, error) {

	ctx, cancel := context.WithTimeout(gctx.Request.Context(), ur.Cfg.GetDuration("db.QueryTimeoutMed"))
	defer cancel()
	query := dblib.Psql.Select("DISTINCT a.pao_code", "a.pao_name", "c.period", "c.account_submissionto_prao_status").
		From("pao.ddo_master AS a").
		LeftJoin("pao.pao_prao_account_main AS c ON c.pao_code = a.pao_code AND c.period = $1", request.Period).
		OrderBy("a.pao_code").Offset(uint64(reqMetadata.Skip * reqMetadata.Limit)).
		Limit(uint64(reqMetadata.Limit))

	return dblib.SelectRows(ctx, ur.Db, query, pgx.RowToStructByNameLax[domain.AccountSubmissionStatusList])
}

func (ur *PaogenRepository) GetPfmsxmlSubmissionStatusRepo(gctx *gin.Context, req *domain.PfmsXmlSubmissionPendingRequest, reqMetadata port.MetaDataRequest) ([]domain.PfmsSubmissionPending, error) {
	ctx, cancel := context.WithTimeout(gctx.Request.Context(), ur.Cfg.GetDuration("db.QueryTimeoutMed"))
	defer cancel()

	query1 := dblib.Psql.Select("pfms_unique_id").
		From("pao.pfms_main").
		Where("h_pfms_generation_flag = TRUE").
		Where("pfms_submission_flag = 'Pending'").
		Where(fmt.Sprintf("business_date BETWEEN TO_DATE(CONCAT('01-04-', %s - 1), 'DD-MM-YYYY') AND TO_DATE(CONCAT('31-03-', %s), 'DD-MM-YYYY')", req.FinYear, req.FinYear))

	// Define the second query for pao.transfer_entry
	query2 := dblib.Psql.Select("pfms_unique_id").
		From("pao.transfer_entry").
		Where("h_pfms_generation_flag = TRUE").
		Where("pfms_submission_flag = 'Pending'").
		Where(fmt.Sprintf("created_date BETWEEN TO_DATE(CONCAT('01-04-', %s - 1), 'DD-MM-YYYY') AND TO_DATE(CONCAT('31-03-', %s), 'DD-MM-YYYY')", req.FinYear, req.FinYear))

	// Convert query2 to SQL
	sql2, _, err := query2.ToSql()
	if err != nil {
		log.Error(gctx, "Error building query2: ", err)
		return nil, err
	}

	// Combine queries with UNION ALL
	sqll := query1.Suffix("UNION ALL " + sql2)

	// Wrap in a select query
	query := dblib.Psql.Select("DISTINCT pfms_unique_id").FromSelect(sqll, "tt")

	// Execute the query using SelectRows
	return dblib.SelectRows(ctx, ur.Db, query, pgx.RowToStructByNameLax[domain.PfmsSubmissionPending])
}
func (ur *PaogenRepository) GetPfmsJsonRepo(gctx *gin.Context, cbds []domain.CbData) ([]domain.TransferEntryAccountingDetail, error) {
	// Set up context with timeout
	ctx, cancel := context.WithTimeout(gctx.Request.Context(), ur.Cfg.GetDuration("db.QueryTimeoutMed"))
	defer cancel()

	// Convert cbds slice to a JSON string array
	var cbdsStringSlice []string
	for _, cbd := range cbds {
		cbdJSON, err := json.Marshal(cbd)
		if err != nil {
			return nil, fmt.Errorf("failed to marshal cbds: %w", err)
		}
		cbdsStringSlice = append(cbdsStringSlice, string(cbdJSON))
	}
	cbdsString := strings.Join(cbdsStringSlice, ",")

	// Build query to select json_output from the database function
	query := dblib.Psql.Select("*").
		From(fmt.Sprintf("pao.generate_pfms_json('[%s]')", cbdsString))
	return dblib.SelectRows(ctx, ur.Db, query, pgx.RowToStructByNameLax[domain.TransferEntryAccountingDetail])
}
func (ur *PaogenRepository) GetPfmsUpdateStatusRepo(gctx *gin.Context, cbds []domain.CbData, Uniq string) error {
	ctx, cancel := context.WithTimeout(gctx.Request.Context(), ur.Cfg.GetDuration("db.QueryTimeoutMed"))
	defer cancel()
	batch := &pgx.Batch{}
	var pendingstatus = "Pending"

	for _, cb := range cbds {
		updateBuilder := dblib.Psql.Update("pao.pfms_main").
			Set("pfms_unique_id", sq.Expr("$1", Uniq)).
			Set("h_pfms_generation_flag", sq.Expr("$2", true)).
			Set("pfms_submission_flag", sq.Expr("$3", pendingstatus)).
			Where("ddo_code = $4", cb.OfficeId).
			Where("TO_CHAR(business_date, 'YYYY-MM-DD') = $5", cb.CbDate)

		err1 := dblib.QueueExecRow(batch, updateBuilder)
		if err1 != nil {
			return err1
		}
	}
	errors := ur.Db.SendBatch(ctx, batch).Close()
	if errors != nil {
		log.Debug(gctx, "Error results:", errors)
		return errors
	}

	return nil
}

func (ur *PaogenRepository) CreateDdomasterRepo(gctx *gin.Context, request domain.DdoMasterInput) error {

	ctx, cancel := context.WithTimeout(gctx.Request.Context(), ur.Cfg.GetDuration("db.QueryTimeoutLow"))
	defer cancel()

	query := dblib.Psql.Insert("pao.ddo_master").SetMap(dblib.GenerateMapFromStruct(request, "select"))
	_, err := dblib.Insert(ctx, ur.Db, query)

	return err
}
func (ur *PaogenRepository) UpdatePfmsSubmissionStatusRepo(gctx *gin.Context, request *domain.UpdatePfmsSubmissionStatusReq, tenumber string) error {
	ctx, cancel := context.WithTimeout(gctx.Request.Context(), ur.Cfg.GetDuration("db.QueryTimeoutMed"))
	defer cancel()

	tables := []string{"pao.pfms_main", "pao.transfer_entry"}

	for _, table := range tables {
		updateBuilder := dblib.Psql.Update(table).
			Set("pfms_submission_flag", sq.Expr("$1", request.Status)).
			Set("pfms_error_description", sq.Expr("$2", request.ErrorDescription)).
			Set("te_number", sq.Expr("$3", tenumber)).
			Where("pfms_unique_id = $4", request.UniqueIdentifier)

		sql, args, err := updateBuilder.ToSql()
		if err != nil {
			return err
		}

		_, err = ur.Db.Exec(ctx, sql, args...)
		if err != nil {
			return err
		}
	}
	updateBuilder := dblib.Psql.Update("pao.pfms_submission").
		Set("submission_status", sq.Expr("$1", request.Status)).
		Set("error_description", sq.Expr("$2", request.ErrorDescription)).
		Set("te_number", sq.Expr("$3", tenumber)).
		Where("pfms_unique_id = $4", request.UniqueIdentifier)

	sql1, args, err1 := updateBuilder.ToSql()
	if err1 != nil {
		return err1
	}

	_, err2 := ur.Db.Exec(ctx, sql1, args...)
	if err2 != nil {
		return err2
	}

	return nil
}

func (ur *PaogenRepository) GetInterPAOsRepo(gctx *gin.Context) ([]domain.InterPao, error) {
	ctx, cancel := context.WithTimeout(gctx.Request.Context(), ur.Cfg.GetDuration("db.QueryTimeoutMed"))
	defer cancel()
	log.Debug(gctx, "Came inside getOfficeNameRepo")
	var u1 domain.InterPao
	columns := dblib.GenerateColumnsFromStruct(u1, "select")
	query := dblib.Psql.Select(columns...).
		From("pao.ddo_master").
		Where("pao_office_id = ddo_office_id")

	return dblib.SelectRows(ctx, ur.Db, query, pgx.RowToStructByNameLax[domain.InterPao])

}

func (ur *PaogenRepository) GetSOOfficeDetailsRepo(gctx *gin.Context, req *domain.OfficeNameRequest) (*domain.SOOfficeDetails, bool, error) {
	ctx, cancel := context.WithTimeout(gctx.Request.Context(), ur.Cfg.GetDuration("db.QueryTimeoutLow"))
	defer cancel()

	log.Debug(gctx, "Came inside GetSOOfficeDetailsRepo")

	// Explicitly define columns to avoid ambiguity
	columns := []string{
		"k.office_id",
		"k.office_type_code",
		"k.reporting_office_id",
		"d.ddo_code",
		"d.ddo_name",
		"d.pao_office_id",
		"d.pao_code",
		"d.pao_name",
	}

	query := dblib.Psql.Select(columns...).
		From("pao.kafka_office_master k").
		LeftJoin("pao.ddo_master d ON k.reporting_office_id = d.ddo_office_id").
		Where(sq.Eq{"k.office_type_code": "SPO", "k.office_id": req.Id}).
		Limit(1)

	return dblib.SelectOneOK(ctx, ur.Db, query, pgx.RowToAddrOfStructByNameLax[domain.SOOfficeDetails])
}
func (ur *PaogenRepository) InsertPfmsSubmission(gctx *gin.Context, pfmsUniqueId string, submissionType string, cbRequest []domain.CbData, teRequest domain.TransferEntryDetail, businessDate string, submissionDate time.Time, submissionData domain.Payload, submissionStatus string, errorDescription string) error {

	ctx, cancel := context.WithTimeout(gctx.Request.Context(), ur.Cfg.GetDuration("db.QueryTimeoutMed"))
	defer cancel()
	// Parse businessDate to timestamp
	businessDateTime, err := time.Parse("2006-01-02", businessDate)
	if err != nil {
		return fmt.Errorf("failed to parse businessDate: %v", err)
	}

	// Convert cbRequest to JSONB
	cbRequestJSON, err := json.Marshal(cbRequest)
	if err != nil {
		return fmt.Errorf("failed to marshal cbRequest: %v", err)
	}

	// Convert submissionData to JSONB
	submissionDataJSON, err := json.Marshal(submissionData)
	if err != nil {
		return fmt.Errorf("failed to marshal submissionData: %v", err)
	}

	// Handle errorDescription: NULL if empty
	var errorDesc interface{}
	if errorDescription != "" {
		errorDesc = errorDescription
	} else {
		errorDesc = nil
	}

	query := dblib.Psql.Insert("pao.pfms_submission").Columns("pfms_unique_id", "pfms_submission_type", "cb_request", "business_date", "submission_date", "submission_data", "submission_status", "error_description").
		Values(pfmsUniqueId, submissionType, cbRequestJSON, businessDateTime, submissionDate, submissionDataJSON, submissionStatus, errorDesc)

	_, err3 := dblib.Insert(ctx, ur.Db, query)

	return err3
}
func (ur *PaogenRepository) GetCbRequestData(gctx *gin.Context, request *domain.UpdatePfmsSubmissionStatusReq) ([]domain.CbData, error) {
	// Set up context with timeout
	ctx, cancel := context.WithTimeout(gctx.Request.Context(), ur.Cfg.GetDuration("db.QueryTimeoutMed"))
	defer cancel()

	// Variable to hold the JSONB data
	var cbRequest []byte

	// Query the cb_request column where pfms_unique_id matches and pfms_submission_type is "cb"
	err := ur.Db.QueryRow(ctx, `
        SELECT cb_request 
        FROM pao.pfms_submission 
        WHERE pfms_unique_id = $1 AND pfms_submission_type = 'cb'
    `, request.UniqueIdentifier).Scan(&cbRequest)
	if err != nil {
		if err == sql.ErrNoRows {
			// No matching row found, return empty result
			return nil, nil
		}
		// Return other errors (e.g., database connection issues)
		return nil, err
	}

	// Unmarshal the JSONB data into an array of CbData structs
	var cbData []domain.CbData
	err = json.Unmarshal(cbRequest, &cbData)
	if err != nil {
		// Return error if JSON unmarshaling fails
		return nil, err
	}

	// Return the unmarshaled data
	return cbData, nil
}

func (ur *PaogenRepository) CheckEmptyCashbookRepo(gctx *gin.Context, req *domain.DdoDetailRequest, reqMetadata port.MetaDataRequest) ([]domain.DdoDetail, error) {
	ctx, cancel := context.WithTimeout(gctx.Request.Context(), ur.Cfg.GetDuration("db.QueryTimeoutMed"))
	defer cancel()
	log.Debug(gctx, "Came inside getOfficeNameRepo")

	query := dblib.Psql.Select("offi.ddo_code as ddo_code,offi.ddo_name as ddo_office_name,c.business_date as business_date, c.closing_bal as closing_bal, c.opening_bal as opening_bal,coalesce('000000000000000') as hoa , coalesce('No transactions') as hoa_description").
		From("pao.ddo_master offi").
		LeftJoin("pao.kafka_cash_book AS c ON offi.ddo_office_id = c.office_id AND c.business_date = $1", req.Date).
		Where("offi.ddo_code = $2 AND c.cash_book_seq IS NOT NULL", req.DdoCode).
		GroupBy("offi.ddo_code", "offi.ddo_name", "c.business_date", "c.closing_bal", "c.opening_bal").
		OrderBy("offi.ddo_code").
		Offset(uint64(reqMetadata.Skip * reqMetadata.Limit)).
		Limit(uint64(reqMetadata.Limit))

	return dblib.SelectRows(ctx, ur.Db, query, pgx.RowToStructByNameLax[domain.DdoDetail])
}

func (ur *PaogenRepository) PostEmptyPfmsverifiedRepo(gctx *gin.Context, request []domain.PfmsVerified) error {
	ctx, cancel := context.WithTimeout(gctx.Request.Context(), ur.Cfg.GetDuration("db.QueryTimeoutMed"))
	defer cancel()
	log.Debug(gctx, "Came inside PostPfmsverifiedRepo")

	// Load Indian Standard Time (IST) location
	ist, err := time.LoadLocation("Asia/Kolkata")
	if err != nil {
		return err
	}

	// Get current time in IST
	verified_date := time.Now().In(ist)

	batch := &pgx.Batch{}

	updateBuilder := dblib.Psql.Update("pao.pfms_main").
		Set("closing_bal", request[0].ClosingBal).
		Set("opening_bal", request[0].OpeningBal).
		Set("verified_by", request[0].VerifiedBy).
		Set("h_verification_flag", request[0].VerificationStatus).
		Set("verified_date", verified_date)

	// Check if both opening and closing balances are zero
	if request[0].OpeningBal.Float64 == 0 && request[0].ClosingBal.Float64 == 0 {
		updateBuilder = updateBuilder.Set("h_pfms_generation_flag", true)
	}

	updateBuilder = updateBuilder.Where(sq.And{sq.Eq{"ddo_code": request[0].DdoCode}, sq.Eq{"business_date": request[0].BusinessDate}})

	err1 := dblib.QueueExecRow(batch, updateBuilder)
	if err1 != nil {
		return err1
	}

	results := ur.Db.SendBatch(ctx, batch)
	if results != nil {
		defer results.Close()

		for i := 0; i < batch.Len(); i++ {
			_, err := results.Exec()
			if err != nil {
				log.Debug(gctx, "Error executing batch command:", err)
				return err
			}
		}
	}

	return nil
}

func (ur *PaogenRepository) GetClosingBalanceRepo(gctx *gin.Context, cbds []domain.CbData) ([]domain.ClosingBalance, error) {
	// Set up context with timeout
	ctx, cancel := context.WithTimeout(gctx.Request.Context(), ur.Cfg.GetDuration("db.QueryTimeoutMed"))
	defer cancel()

	// Validate inputs and prepare batch
	var results []domain.ClosingBalance

	for _, cbd := range cbds {

		// Build query for each CbData
		query := dblib.Psql.Select("d.ddo_code::text as office_id", "k.business_date::text as cb_date", "ROUND(k.closing_bal)::bigint AS closing_bal").
			From("pao.kafka_cash_book k").
			LeftJoin("pao.ddo_master d ON k.office_id = d.ddo_office_id").
			Where(sq.And{
				sq.Eq{"d.ddo_code::text": cbd.OfficeId},
				sq.Eq{"k.business_date::text": cbd.CbDate},
			})
		queryresults, err := dblib.SelectRows(ctx, ur.Db, query, pgx.RowToStructByNameLax[domain.ClosingBalance])
		if err != nil {
			return nil, err
		}
		if len(queryresults) > 0 {
			for _, result := range queryresults {
				// Append each result to the results slice
				results = append(results, result)
			}
		}

	}

	return results, nil
}

func (ur *PaogenRepository) CheckCashAccountVerificationRepo(gctx *gin.Context, req domain.PraoAccountSubmissionRequest) (bool, error) {
	ctx, cancel := context.WithTimeout(gctx.Request.Context(), ur.Cfg.GetDuration("db.QueryTimeoutMed"))
	defer cancel()

	log.Debug(gctx, "Checking cash account verification for pao_code: %s, period: %s", req.PaoCode, req.Period)

	query := sq.Select("COUNT(*) as total_count", "COUNT(CASE WHEN h_verification_flag = true THEN 1 END) as verified_count").
		From("pao.pfms_monthly_main").
		Where("pao_code = $1", req.PaoCode).
		Where("period = $2", req.Period)

	type verificationCounts struct {
		TotalCount    int64 `db:"total_count"`
		VerifiedCount int64 `db:"verified_count"`
	}

	rows, err := dblib.SelectRows(ctx, ur.Db, query, pgx.RowToStructByNameLax[verificationCounts])
	if err != nil {
		log.Error(gctx, "Failed to check cash account verification: %s", err.Error())
		return false, err
	}

	// Expecting exactly one row
	if len(rows) == 0 {
		log.Debug(gctx, "No records found for pao_code: %s, period: %s", req.PaoCode, req.Period)
		return false, nil
	}
	if len(rows) > 1 {
		log.Error(gctx, "Unexpected multiple rows returned for verification check")
		return false, errors.New("multiple rows returned for verification check")
	}

	counts := rows[0]
	// If no records or not all records are verified, return false
	if counts.TotalCount == 0 || counts.TotalCount != counts.VerifiedCount {
		log.Debug(gctx, "Verification incomplete: total_count=%d, verified_count=%d", counts.TotalCount, counts.VerifiedCount)
		return false, nil
	}

	return true, nil
}
func (hr *PaogenRepository) GetHoaonlyRepo(gctx *gin.Context, req *domain.HoaRequest1) ([]domain.AcccountHoaonlygetMapping, error) {

	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	log.Debug(gctx, "Came inside GetHoaRepo")
	var u1 domain.AcccountHoaonlygetMapping

	columns := dblib.GenerateColumnsFromStruct(u1, "select")
	query := dblib.Psql.Select(columns...).
		Distinct().
		From("pao.kafka_account_codes_master").
		// Where("status_flag is true").
		Where(
			squirrel.Or{
				squirrel.Expr("hoa ILIKE ?", "%"+req.Hoa+"%"),
				squirrel.Expr("hoa_description ILIKE ?", "%"+req.Hoa+"%"),
			},
		).
		Limit(10)
	return dblib.SelectRows(ctx, hr.Db, query, pgx.RowToStructByNameLax[domain.AcccountHoaonlygetMapping])

}

func (hr *PaogenRepository) GetAccountCodeRepo(gctx *gin.Context, req *domain.AccountCodeRequest) ([]domain.AcccountCodegetMapping, error) {

	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	log.Debug(gctx, "Came inside GetHoaRepo")
	var u1 domain.AcccountCodegetMapping

	columns := dblib.GenerateColumnsFromStruct(u1, "select")
	query := dblib.Psql.Select(columns...).
		Distinct().
		From("pao.kafka_account_codes_master").
		// Where("status_flag is true").
		Where(
			squirrel.Or{
				squirrel.Expr("account_code ILIKE ?", "%"+req.AccountCode+"%"),
				squirrel.Expr("account_code_description ILIKE ?", "%"+req.AccountCode+"%"),
			},
		).
		Limit(10)
	return dblib.SelectRows(ctx, hr.Db, query, pgx.RowToStructByNameLax[domain.AcccountCodegetMapping])

}

func (hr *PaogenRepository) GetPendingPfmsUniqueIdsRepo(gctx *gin.Context, paoCode string) ([]domain.PfmsSubmissionPending, error) {
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()

	log.Debug(gctx, "Came inside GetPendingPfmsUniqueIdsRepo")

	var u1 domain.PfmsSubmissionPending
	columns := dblib.GenerateColumnsFromStruct(u1, "select")
	query := dblib.Psql.Select(columns...).
		From("pao.pfms_main").
		// Where(squirrel.Eq{"pfms_submission_flag": "Pending"}).
		Where(squirrel.Eq{
			"pfms_submission_flag": "Pending",
			"pao_code":             paoCode, // ← filter by this PAO only
		}).
		OrderBy("verified_date ASC"). // oldest first
		Limit(5)

	return dblib.SelectRows(ctx, hr.Db, query, pgx.RowToStructByNameLax[domain.PfmsSubmissionPending])
}

func (hr *PaogenRepository) GetCashbookPfmsStatusRepo(gctx *gin.Context, req *domain.CashbookPfmsStatusRequest) (*domain.CashbookSubmissionStatus, error) {

	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	log.Debug(gctx, "Came inside GetCashbookPfmsStatusRepo")

	query := dblib.Psql.Select("COALESCE(pm.h_pfms_generation_flag, False) AS pfms_submission_flag").
		From("pao.pfms_main pm").
		Join("pao.ddo_master dm ON dm.ddo_code = pm.ddo_code").
		Where("dm.ddo_office_id = ?", req.OfficeId).
		Where("pm.business_date = ?", req.CashbookDate)

	return dblib.SelectOne(ctx, hr.Db, query, pgx.RowToAddrOfStructByName[domain.CashbookSubmissionStatus])
}

func (ur *PaogenRepository) CheckEmptyCashbookMonthlyRepo(gctx *gin.Context, req *domain.DdoDetailMonthlyRequest, reqMetadata port.MetaDataRequest) ([]domain.DdoDetailMonthlyEmpty, error) {
	ctx, cancel := context.WithTimeout(gctx.Request.Context(), ur.Cfg.GetDuration("db.QueryTimeoutMed"))
	defer cancel()
	log.Debug(gctx, "Came inside getOfficeNameRepo")

	query := dblib.Psql.Select("offi.ddo_code as ddo_code,offi.ddo_name as office_name,REPLACE(c.period, '-', '') AS period, c.closing_bal as closing_bal, c.opening_bal as opening_bal").
		From("pao.ddo_master offi").
		LeftJoin("pao.kafka_cash_account AS c ON offi.ddo_office_id = c.office_id AND REPLACE(c.period, '-', '') = $1", req.Period).
		Where("offi.ddo_code = $2 AND c.db_id IS NOT NULL", req.DdoCode).
		GroupBy("offi.ddo_code", "offi.ddo_name", "c.period", "c.closing_bal", "c.opening_bal").
		OrderBy("offi.ddo_code").
		Offset(uint64(reqMetadata.Skip * reqMetadata.Limit)).
		Limit(uint64(reqMetadata.Limit))

	return dblib.SelectRows(ctx, ur.Db, query, pgx.RowToStructByNameLax[domain.DdoDetailMonthlyEmpty])
}

func (ur *PaogenRepository) PostEmptyPfmsMonthlyverifiedRepo(gctx *gin.Context, request []domain.PfmsVerifiedMonthly) error {
	ctx, cancel := context.WithTimeout(gctx.Request.Context(), ur.Cfg.GetDuration("db.QueryTimeoutMed"))
	defer cancel()
	log.Debug(gctx, "Came inside Post PfmsverifiedRepo")

	// Load Indian Standard Time (IST) location
	ist, err := time.LoadLocation("Asia/Kolkata")
	if err != nil {
		return err
	}

	// Get current time in IST
	verified_date := time.Now().In(ist)

	batch := &pgx.Batch{}

	pfmsmainupdateQuery := dblib.Psql.Update("pao.pfms_monthly_main").
		Set("pfms_ddo_id", request[0].DdoCode+request[0].Period).
		Set("closing_bal", request[0].ClosingBal).
		Set("opening_bal", request[0].OpeningBal).
		Set("verified_by", request[0].VerifiedBy).
		Set("h_verification_flag", request[0].VerificationStatus).
		Set("verified_date", verified_date).
		Where(sq.And{sq.Eq{"ddo_code": request[0].DdoCode}, sq.Eq{"period": request[0].Period}})

	err1 := dblib.QueueExecRow(batch, pfmsmainupdateQuery)
	if err1 != nil {
		return err1
	}
	results := ur.Db.SendBatch(ctx, batch)
	if results != nil {
		defer results.Close()

		for i := 0; i < batch.Len(); i++ {
			_, err := results.Exec()
			if err != nil {
				log.Debug(gctx, "Error executing batch command:", err)
				return err
			}
		}
	}
	return nil

}
func (hr *PaogenRepository) CheckPreviousCashbook(
	gctx *gin.Context,
	ddoCode string,
	businessDate time.Time,
) (*domain.LastCBCheck, error) {
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	log.Debug(gctx, "Came inside CheckPreviousCashbook")

	var lastCB domain.LastCBCheck

	columns := dblib.GenerateColumnsFromStruct(lastCB, "select")

	// Build query
	query := dblib.Psql.
		Select(columns...).
		From("pao.pfms_main").
		Where(squirrel.Eq{"ddo_code": ddoCode}).
		Where(squirrel.Expr("business_date < ?", businessDate)).
		OrderBy("business_date DESC").
		Limit(1)

	// Run query
	result, err := dblib.SelectOne(ctx, hr.Db, query, pgx.RowToAddrOfStructByName[domain.LastCBCheck])
	if err != nil {
		// Gracefully handle "no rows found"
		if errors.Is(err, pgx.ErrNoRows) {
			return nil, nil
		}
		return nil, err
	}

	return result, nil
}

func (ur *PaogenRepository) RevertCashbookRepo(
	gctx *gin.Context,
	ddocode string,
	officeID string,
	businessDate string,
) (string, error) {
	ctx, cancel := context.WithTimeout(gctx.Request.Context(), ur.Cfg.GetDuration("db.QueryTimeoutMed"))
	defer cancel()

	// Query 1: delete from kafka_cash_book
	deleteBuilder1 := dblib.Psql.Delete("pao.kafka_cash_book").
		Where("office_id = ?", officeID).
		Where("business_date >= ?", businessDate)

	sql1, args1, err := deleteBuilder1.ToSql()
	if err != nil {
		return "", err
	}
	if _, err := ur.Db.Exec(ctx, sql1, args1...); err != nil {
		return "", err
	}

	// Query 2: delete from pfms_detail with subquery
	deleteBuilder2 := dblib.Psql.Delete("pao.pfms_detail").
		Where("pfms_ddo_id IN ("+
			"SELECT m.pfms_ddo_id FROM pao.pfms_main m "+
			"WHERE m.ddo_code = ? AND m.business_date::date >= ?)", ddocode, businessDate)

	sql2, args2, err := deleteBuilder2.ToSql()
	if err != nil {
		return "", err
	}
	if _, err := ur.Db.Exec(ctx, sql2, args2...); err != nil {
		return "", err
	}

	// Step before deleting pfms_main: fetch pfms_unique_id
	var pfmsUniqueID sql.NullString
	checkQuery := `
        SELECT pfms_unique_id
        FROM pao.pfms_main
        WHERE ddo_code = $1
          AND business_date::date = $2
        LIMIT 1
    `
	err = ur.Db.QueryRow(ctx, checkQuery, ddocode, businessDate).Scan(&pfmsUniqueID)
	if err != nil && err != pgx.ErrNoRows {
		return "", err
	}

	// Query 3: delete from pfms_main
	deleteBuilder3 := dblib.Psql.Delete("pao.pfms_main").
		Where("ddo_code = ?", ddocode).
		Where("business_date::date >= ?", businessDate)

	sql3, args3, err := deleteBuilder3.ToSql()
	if err != nil {
		return "", err
	}
	if _, err := ur.Db.Exec(ctx, sql3, args3...); err != nil {
		return "", err
	}

	// Return pfms_unique_id even though row is deleted
	if pfmsUniqueID.Valid {
		return pfmsUniqueID.String, nil
	}
	return "", nil
}
func (hr *PaogenRepository) GetCashbookReversionListRepo(gctx *gin.Context, req *domain.CashbookReversionListRequest, reqMetadata port.MetaDataRequest) ([]domain.PfmsStatus, error) {

	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	log.Debug(gctx, "Came inside GetCashbookPfmsStatusRepo")

	query := dblib.Psql.
		Select(`
			d.ddo_code,
			d.ddo_name,
			k.business_date AS date,
			COALESCE(p.h_cash_book_receive_flag, FALSE) AS h_cash_book_receive_flag,
			COALESCE(p.h_verification_flag, FALSE) AS h_verification_flag,
			COALESCE(p.h_pfms_generation_flag, FALSE) AS h_pfms_generation_flag
		`).
		From("pao.kafka_cash_book k").
		LeftJoin("pao.ddo_master d ON k.office_id = d.ddo_office_id").
		LeftJoin("pao.pfms_main p ON d.ddo_code = p.ddo_code AND k.business_date = DATE(p.business_date)").
		Where("d.ddo_code = ?", req.DdoCode).
		Where("k.business_date >= ?", req.FromDate).
		OrderBy("k.business_date").Offset(uint64(reqMetadata.Skip * reqMetadata.Limit)).
		Limit(uint64(reqMetadata.Limit))

	results, err := dblib.SelectRows(ctx, hr.Db, query, pgx.RowToStructByNameLax[domain.PfmsStatus])
	if err != nil {
		return nil, err
	}

	return results, nil
}

func (ur *PaogenRepository) GetDDOOfficeID(gctx *gin.Context, ddoCode string) (*domain.OfficeID, bool, error) {
	// Set timeout context
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	log.Debug(gctx, "Came inside GetCashbookPfmsStatusRepo")

	// Prepare query
	query := dblib.Psql.Select("ddo_office_id").
		From("pao.ddo_master").
		Where(sq.Eq{"ddo_code": ddoCode}).
		Limit(1)

	return dblib.SelectOneOK(ctx, ur.Db, query, pgx.RowToAddrOfStructByNameLax[domain.OfficeID])
}
func (ur *PaogenRepository) StoreReversalRequestRepo(gctx *gin.Context, requestOfficeID, requestEmployeeID, ddoCode, fromDate, remark string) error {
	ctx, cancel := context.WithTimeout(gctx.Request.Context(), ur.Cfg.GetDuration("db.QueryTimeoutLow"))
	defer cancel()

	log.Debug(gctx, "Storing reversal request for DDO Code: %s", ddoCode)

	// Parse fromDate (supports both MM/DD/YYYY and YYYY-MM-DD)
	parsedDate, err := time.Parse("2006-01-02", fromDate)
	if err != nil {
		parsedDate, err = time.Parse("01/02/2006", fromDate)
		if err != nil {
			log.Error(gctx, "Invalid date format in StoreReversalRequestRepo: %s", fromDate)
			return fmt.Errorf("invalid date format: %s", fromDate)
		}
	}

	// Convert IDs to int
	reqOfficeID, err := strconv.Atoi(requestOfficeID)
	if err != nil {
		return fmt.Errorf("invalid RequestOfficeID: %s", requestOfficeID)
	}
	reqEmpID, err := strconv.Atoi(requestEmployeeID)
	if err != nil {
		return fmt.Errorf("invalid RequestEmployeeID: %s", requestEmployeeID)
	}

	// Build insert query
	query := dblib.Psql.
		Insert("pao.reversion").
		Columns("request_office_id", "request_employee_id", "ddo_code", "from_date", "request_date", "remarks").
		Values(reqOfficeID, reqEmpID, ddoCode, parsedDate, time.Now(), remark)

	// Execute insert using dblib.Insert
	_, err = dblib.Insert(ctx, ur.Db, query)
	if err != nil {
		log.Error(gctx, "Failed to store reversal request: %s", err.Error())
		return err
	}

	log.Debug(gctx, "Reversal request successfully stored for DDO Code: %s", ddoCode)
	return nil
}
func (uh *PaogenRepository) TryStartSubmission(
	ctx context.Context,
	ddoCode string,
	bizDate time.Time, // ensure this is midnight date if column is DATE
) (acquired bool, remaining time.Duration, err error) {

	// Attempt atomic claim
	var newStart time.Time
	err = uh.Db.QueryRow(ctx, `
        UPDATE pfms_main
        SET submission_start = NOW()
        WHERE ddo_code = $1
          AND business_date = $2
          AND (submission_start IS NULL OR submission_start < NOW() - INTERVAL '50 seconds')
        RETURNING submission_start
    `, ddoCode, bizDate).Scan(&newStart)

	switch {
	case err == nil:
		return true, 0, nil
	case errors.Is(err, sql.ErrNoRows):
		return false, 0, nil
	default:
		return false, 0, err
	}

	// // Not acquired; fetch current start to compute remaining wait (optional)
	// var currentStart time.Time
	// qerr := uh.Db.QueryRow(ctx, `
	// 	SELECT submission_start FROM pfms_main
	// 	WHERE ddo_code = $1 AND business_date = $2
	// `, ddoCode, bizDate).Scan(&currentStart)
	// if qerr != nil {
	// 	if qerr == sql.ErrNoRows {
	// 		// no row? then nobody claimed AND your UPDATE didn't match; treat as not acquired
	// 		return false, 0, nil
	// 	}
	// 	return false, 0, qerr
	// }

	// // remaining time until 15s passes (clamped at 0)
	// wait := time.Until(currentStart.Add(15 * time.Second))
	// if wait < 0 {
	// 	wait = 0
	// }
	// return false, wait, nil
}

// func (ur *PaogenRepository) GetConsolidatedCashAccountRepoold(gctx *gin.Context, req *domain.DdoListRequestMonthly) (*domain.ConsolidatedCashAccount, error) {
// 	ctx, cancel := context.WithTimeout(gctx.Request.Context(), ur.Cfg.GetDuration("db.QueryTimeoutLow"))
// 	defer cancel()
// 	log.Debug(gctx, "Came inside GetConsolidatedCashAccountRepo")

// 	period := req.Period[:2] + "-" + req.Period[2:] // "072025" → "07-2025"

// 	// Step 1: Get PAO details
// 	var paoOfficeId int64
// 	var paoName string
// 	err := ur.Db.QueryRow(ctx,
// 		`SELECT pao_office_id, pao_name FROM pao.ddo_master WHERE pao_code = $1 LIMIT 1`,
// 		req.PaoCode,
// 	).Scan(&paoOfficeId, &paoName)
// 	if err != nil {
// 		return nil, fmt.Errorf("failed to fetch PAO details: %w", err)
// 	}

// 	// Step 2: Consolidate accounts, joining kafka_account_codes_master for HOA fields
// 	query := `
//         WITH office_accounts AS (
//             SELECT kca.result_array
//             FROM pao.kafka_cash_account kca
//             INNER JOIN pao.ddo_master dm ON dm.ddo_office_id = kca.office_id
//             WHERE dm.pao_code = $1
//               AND kca.period = $2
//               AND kca.result_array IS NOT NULL
//         ),
//         unnested AS (
//             SELECT
//                 elem->>'account_code'             AS account_code,
//                 elem->>'account_code_description' AS account_code_description,
//                 elem->>'part'                     AS part,
//                 elem->>'credit_debit'             AS credit_debit,
//                 COALESCE((elem->>'Grand_total')::numeric, 0) AS grand_total
//             FROM office_accounts,
//                 jsonb_array_elements(result_array::jsonb) AS elem
//         ),
//         aggregated AS (
//             SELECT
//                 account_code,
//                 account_code_description,
//                 part,
//                 credit_debit,
//                 SUM(grand_total) AS grand_total
//             FROM unnested
//             WHERE account_code IS NOT NULL
//             GROUP BY account_code, account_code_description, part, credit_debit
//         )
//         SELECT
//             a.account_code,
//             a.account_code_description,
//             a.part,
//             a.credit_debit,
//             COALESCE(m.hoa,             '') AS hoa,
//             COALESCE(m.hoa_description, '') AS hoa_description,
//             COALESCE(m.hoa_reflection,  '') AS hoa_reflection,
//             COALESCE(m.positive_side,   '') AS positive_side,
//             a.grand_total
//         FROM aggregated a
//         LEFT JOIN pao.kafka_account_codes_master m ON m.account_code = a.account_code
//         ORDER BY a.account_code
//     `

// 	rows, err := ur.Db.Query(ctx, query, req.PaoCode, period)
// 	if err != nil {
// 		return nil, fmt.Errorf("failed to fetch consolidated cash account: %w", err)
// 	}
// 	defer rows.Close()

// 	var headDetails []domain.ConsolidatedHeadDetail
// 	for rows.Next() {
// 		var h domain.ConsolidatedHeadDetail
// 		if err := rows.Scan(
// 			&h.AccountCode,
// 			&h.AccountCodeDescription,
// 			&h.Part,
// 			&h.CreditDebit,
// 			&h.Hoa,
// 			&h.HoaDescription,
// 			&h.HoaReflection,
// 			&h.PositiveSide,
// 			&h.GrandTotal,
// 		); err != nil {
// 			return nil, fmt.Errorf("failed to scan row: %w", err)
// 		}
// 		headDetails = append(headDetails, h)
// 	}
// 	if err := rows.Err(); err != nil {
// 		return nil, fmt.Errorf("rows iteration error: %w", err)
// 	}

// 	if headDetails == nil {
// 		headDetails = []domain.ConsolidatedHeadDetail{}
// 	}

// 	result := &domain.ConsolidatedCashAccount{
// 		PaoOfficeId:       paoOfficeId,
// 		PaoName:           paoName,
// 		CashAccountPeriod: period,
// 		HeadDetails:       headDetails,
// 	}

// 	return result, nil
// }

// func (ur *PaogenRepository) GetConsolidatedCashAccountRepo020326(gctx *gin.Context, req *domain.DdoListRequestMonthly) (*domain.ConsolidatedCashAccount, error) {
// 	ctx, cancel := context.WithTimeout(gctx.Request.Context(), ur.Cfg.GetDuration("db.QueryTimeoutLow"))
// 	defer cancel()
// 	log.Debug(gctx, "Came inside GetConsolidatedCashAccountRepo")

// 	period := req.Period[:2] + "-" + req.Period[2:]

// 	query := `
//         WITH pao AS (
//             SELECT pao_office_id, pao_name
//             FROM pao.ddo_master
//             WHERE pao_code = $1
//             LIMIT 1
//         ),
//         aggregated AS (
//             SELECT
//                 elem->>'account_code'             AS account_code,
//                 -- Take description/part/credit_debit from the first row seen;
//                 -- they're functionally the same per account_code.
//                 MIN(elem->>'account_code_description') AS account_code_description,
//                 MIN(elem->>'part')                AS part,
//                 MIN(elem->>'credit_debit')        AS credit_debit,
//                 SUM(COALESCE((elem->>'Grand_total')::numeric, 0)) AS grand_total
//             FROM pao.kafka_cash_account kca
//             INNER JOIN pao.ddo_master dm
//                     ON dm.ddo_office_id = kca.office_id
//                    AND dm.pao_code = $1          -- pushed into JOIN, not WHERE
//             CROSS JOIN LATERAL jsonb_array_elements(kca.result_array::jsonb) AS elem
//             WHERE kca.period = $2
//               AND kca.result_array IS NOT NULL
//               AND elem->>'account_code' IS NOT NULL   -- filter before grouping
//             GROUP BY elem->>'account_code'
//         )
//         SELECT
//             p.pao_office_id,
//             p.pao_name,
//             a.account_code,
//             a.account_code_description,
//             a.part,
//             a.credit_debit,
//             COALESCE(m.hoa,             '') AS hoa,
//             COALESCE(m.hoa_description, '') AS hoa_description,
//             COALESCE(m.hoa_reflection,  '') AS hoa_reflection,
//             COALESCE(m.positive_side,   '') AS positive_side,
//             a.grand_total
//         FROM aggregated a
//         CROSS JOIN pao p
//         LEFT JOIN pao.kafka_account_codes_master m ON m.account_code = a.account_code
//         ORDER BY a.account_code
//     `

// 	rows, err := ur.Db.Query(ctx, query, req.PaoCode, period)
// 	if err != nil {
// 		return nil, fmt.Errorf("failed to fetch consolidated cash account: %w", err)
// 	}
// 	defer rows.Close()

// 	var (
// 		paoOfficeId int64
// 		paoName     string
// 		headDetails []domain.ConsolidatedHeadDetail
// 		firstRow    = true
// 	)

// 	for rows.Next() {
// 		var h domain.ConsolidatedHeadDetail
// 		if err := rows.Scan(
// 			&paoOfficeId,
// 			&paoName,
// 			&h.AccountCode,
// 			&h.AccountCodeDescription,
// 			&h.Part,
// 			&h.CreditDebit,
// 			&h.Hoa,
// 			&h.HoaDescription,
// 			&h.HoaReflection,
// 			&h.PositiveSide,
// 			&h.GrandTotal,
// 		); err != nil {
// 			return nil, fmt.Errorf("failed to scan row: %w", err)
// 		}
// 		_ = firstRow
// 		firstRow = false
// 		headDetails = append(headDetails, h)
// 	}
// 	if err := rows.Err(); err != nil {
// 		return nil, fmt.Errorf("rows iteration error: %w", err)
// 	}

// 	if headDetails == nil {
// 		headDetails = []domain.ConsolidatedHeadDetail{}
// 	}

// 	return &domain.ConsolidatedCashAccount{
// 		PaoOfficeId:       paoOfficeId,
// 		PaoName:           paoName,
// 		CashAccountPeriod: period,
// 		HeadDetails:       headDetails,
// 	}, nil
// }

func (ur *PaogenRepository) GetConsolidatedCashAccountRepo(gctx *gin.Context, req *domain.DdoListRequestMonthly) (*domain.ConsolidatedCashAccount, error) {
	ctx, cancel := context.WithTimeout(gctx.Request.Context(), ur.Cfg.GetDuration("db.QueryTimeoutLow"))
	defer cancel()
	log.Debug(gctx, "Came inside GetConsolidatedCashAccountRepo")

	period := req.Period[:2] + "-" + req.Period[2:]

	query := `
        WITH pao AS (
            SELECT pao_office_id, pao_name
            FROM pao.ddo_master
            WHERE pao_code = $1
            LIMIT 1
        ),
        raw_aggregated AS (
            SELECT
                elem->>'account_code'                  AS account_code,
                MIN(elem->>'account_code_description') AS account_code_description,
                MIN(elem->>'part')                     AS part,
                m.hoa,
                MIN(m.hoa_description)                 AS hoa_description,
                MIN(m.hoa_reflection)                  AS hoa_reflection,
                MIN(m.positive_side)                   AS positive_side,
                SUM(CASE WHEN elem->>'credit_debit' = 'R'
                         THEN COALESCE((elem->>'Grand_total')::numeric, 0)
                         ELSE 0 END) AS receipt,
                SUM(CASE WHEN elem->>'credit_debit' = 'P'
                         THEN COALESCE((elem->>'Grand_total')::numeric, 0)
                         ELSE 0 END) AS payment
            FROM pao.kafka_cash_account kca
            INNER JOIN pao.ddo_master dm
                    ON dm.ddo_office_id = kca.office_id
                   AND dm.pao_code = $1
            CROSS JOIN LATERAL jsonb_array_elements(kca.result_array::jsonb) AS elem
            LEFT JOIN pao.kafka_account_codes_master m ON m.account_code = elem->>'account_code'
            WHERE kca.period = $2
              AND kca.result_array IS NOT NULL
              AND elem->>'account_code' IS NOT NULL
            GROUP BY
                elem->>'account_code',
                m.hoa
        ),
        aggregated AS (
            SELECT
                COALESCE(MIN(hoa),             '') AS hoa,
                COALESCE(MIN(hoa_description), '') AS hoa_description,
                COALESCE(MIN(hoa_reflection),  '') AS hoa_reflection,
                COALESCE(MIN(positive_side),   '') AS positive_side,
                MIN(part)                          AS part,
                SUM(receipt)                       AS receipt,
                SUM(payment)                       AS payment,
                jsonb_agg(
                    jsonb_build_object(
                        'account_code',             account_code,
                        'account_code_description', account_code_description,
                        'receipt',                  receipt,
                        'payment',                  payment
                    )
                    ORDER BY account_code
                ) AS account_array
            FROM raw_aggregated
            GROUP BY hoa
        )
        SELECT
            p.pao_office_id,
            p.pao_name,
            a.hoa,
            a.hoa_description,
            a.hoa_reflection,
            a.positive_side,
            a.part,
            a.receipt,
            a.payment,
            a.account_array
        FROM aggregated a
        CROSS JOIN pao p
        ORDER BY a.hoa
    `

	rows, err := ur.Db.Query(ctx, query, req.PaoCode, period)
	if err != nil {
		return nil, fmt.Errorf("failed to fetch consolidated cash account: %w", err)
	}
	defer rows.Close()

	var (
		paoOfficeId int64
		paoName     string
		hoaDetails  []domain.ConsolidatedHoaDetail
	)

	for rows.Next() {
		var (
			h            domain.ConsolidatedHoaDetail
			accountBytes []byte
		)
		if err := rows.Scan(
			&paoOfficeId,
			&paoName,
			&h.Hoa,
			&h.HoaDescription,
			&h.HoaReflection,
			&h.PositiveSide,
			&h.Part,
			&h.Receipt,
			&h.Payment,
			&accountBytes,
		); err != nil {
			return nil, fmt.Errorf("failed to scan row: %w", err)
		}

		if err := json.Unmarshal(accountBytes, &h.AccountArray); err != nil {
			return nil, fmt.Errorf("failed to parse account_array: %w", err)
		}

		hoaDetails = append(hoaDetails, h)
	}
	if err := rows.Err(); err != nil {
		return nil, fmt.Errorf("rows iteration error: %w", err)
	}

	if hoaDetails == nil {
		hoaDetails = []domain.ConsolidatedHoaDetail{}
	}

	return &domain.ConsolidatedCashAccount{
		PaoOfficeId:       paoOfficeId,
		PaoName:           paoName,
		CashAccountPeriod: period,
		HoaDetails:        hoaDetails,
	}, nil
}

// changes done on 23-03-2026 for the cashbook reversion and posting of -ve entry in PFMS.

func (ur *PaogenRepository) GetAllDatesForReversionRepo(
	gctx *gin.Context,
	officeID int, // ← changed from string ddoCode to int officeID
	ddoCode string, // ← added
	fromDate string,
) ([]time.Time, error) {
	ctx, cancel := context.WithTimeout(
		gctx.Request.Context(),
		ur.Cfg.GetDuration("db.QueryTimeoutMed"),
	)
	defer cancel()

	// UNION both tables to capture ALL dates that will be deleted
	// kafka_cash_book uses office_id
	// pfms_main uses ddo_code
	query := `
		SELECT DISTINCT business_date::date AS business_date
		FROM pao.kafka_cash_book
		WHERE office_id           = $1
		  AND business_date       IS NOT NULL
		  AND business_date::date >= $3::date

		UNION

		SELECT DISTINCT business_date::date
		FROM pao.pfms_main
		WHERE ddo_code            = $2
		  AND business_date       IS NOT NULL
		  AND business_date::date >= $3::date

		ORDER BY business_date ASC
	`
	rows, err := ur.Db.Query(ctx, query, officeID, ddoCode, fromDate)
	if err != nil {
		return nil, fmt.Errorf("GetAllDatesForReversionRepo failed: %w", err)
	}
	defer rows.Close()

	var dates []time.Time
	for rows.Next() {
		var d time.Time
		if err := rows.Scan(&d); err != nil {
			return nil, fmt.Errorf("GetAllDatesForReversionRepo scan failed: %w", err)
		}
		dates = append(dates, d)
	}
	if err := rows.Err(); err != nil {
		return nil, fmt.Errorf("GetAllDatesForReversionRepo rows error: %w", err)
	}
	return dates, nil
}

// Returns pfmsUID, submissionStatus, teNumber, cbRequests, found, error
func (ur *PaogenRepository) GetPfmsSubmissionByDateRepo(
	gctx *gin.Context,
	officeID string,
	businessDate time.Time,
) (string, string, string, []domain.CbData, bool, error) {

	ctx, cancel := context.WithTimeout(
		gctx.Request.Context(),
		ur.Cfg.GetDuration("db.QueryTimeoutMed"),
	)
	defer cancel()

	filterJSON, err := json.Marshal(
		[]map[string]string{{"office_id": officeID}},
	)
	if err != nil {
		return "", "", "", nil, false,
			fmt.Errorf("failed to build filter: %w", err)
	}

	query := `
        SELECT
            pfms_unique_id,
            COALESCE(submission_status, '') AS submission_status,
            COALESCE(te_number, '')         AS te_number,
            cb_request
        FROM pao.pfms_submission
        WHERE cb_request @> $1::jsonb
          AND business_date::date  = $2::date
          AND pfms_submission_type = 'cb'
        ORDER BY submission_date DESC
        LIMIT 1
    `
	var (
		pfmsUID          string
		submissionStatus string
		teNumber         string
		rawCbReq         []byte
	)
	err = ur.Db.QueryRow(ctx, query, string(filterJSON), businessDate).
		Scan(&pfmsUID, &submissionStatus, &teNumber, &rawCbReq)
	if err != nil {
		if errors.Is(err, pgx.ErrNoRows) {
			return "", "", "", nil, false, nil
		}
		return "", "", "", nil, false,
			fmt.Errorf("GetPfmsSubmissionByDateRepo failed: %w", err)
	}

	var cbRequests []domain.CbData
	if err := json.Unmarshal(rawCbReq, &cbRequests); err != nil {
		return "", "", "", nil, false,
			fmt.Errorf("failed to unmarshal cb_request: %w", err)
	}

	return pfmsUID, submissionStatus, teNumber, cbRequests, true, nil
}

func (ur *PaogenRepository) BulkInsertReversionRepo(
	gctx *gin.Context,
	rows []domain.ReversionRow,
) error {
	ctx, cancel := context.WithTimeout(
		gctx.Request.Context(),
		ur.Cfg.GetDuration("db.QueryTimeoutMed"),
	)
	defer cancel()

	if len(rows) == 0 {
		return nil
	}

	query := dblib.Psql.
		Insert("pao.reversion").
		Columns(
			"request_office_id",
			"request_employee_id",
			"ddo_code",
			"from_date",
			"request_date",
			"remarks",
			"business_date",
			"pfms_reversal_type",
			"original_pfms_uid",
			"original_submission_status",
			"original_te_number",
			"pfms_negative_posted",
			"db_deletion_status",
		)

	for _, row := range rows {
		// Nullable fields
		var origUID, origStatus, origTE, pfmsNegPosted interface{}

		if row.OriginalPfmsUID != "" {
			origUID = row.OriginalPfmsUID
		}
		if row.OriginalSubmissionStatus != "" {
			origStatus = row.OriginalSubmissionStatus
		}
		if row.OriginalTeNumber != "" {
			origTE = row.OriginalTeNumber
		}
		if row.PfmsNegativePosted != "" {
			pfmsNegPosted = row.PfmsNegativePosted
		}

		query = query.Values(
			row.RequestOfficeID,
			row.RequestEmployeeID,
			row.DdoCode,
			row.FromDate,
			time.Now(),
			row.Remark,
			row.BusinessDate,
			row.PfmsReversalType,
			origUID,
			origStatus,
			origTE,
			pfmsNegPosted,
			row.DbDeletionStatus,
		)
	}

	_, err := dblib.Insert(ctx, ur.Db, query)
	if err != nil {
		return fmt.Errorf("BulkInsertReversionRepo failed: %w", err)
	}
	return nil
}

func (ur *PaogenRepository) GetReversionRecordsRepo(
	gctx *gin.Context,
	ddoCode string,
	fromDate string,
) ([]domain.ReversionRecord, error) {
	ctx, cancel := context.WithTimeout(
		gctx.Request.Context(),
		ur.Cfg.GetDuration("db.QueryTimeoutMed"),
	)
	defer cancel()

	// Join pfms_submission live for WAIT rows to get current_status
	query := `
        SELECT
            r.reversion_id,
            r.ddo_code,
            r.from_date,
            r.business_date,
            COALESCE(r.pfms_reversal_type,         '') AS pfms_reversal_type,
            COALESCE(r.original_pfms_uid,           '') AS original_pfms_uid,
            COALESCE(r.original_submission_status,  '') AS original_submission_status,
            COALESCE(r.original_te_number,          '') AS original_te_number,
            COALESCE(ps.submission_status,          '') AS current_status,
            COALESCE(r.reversal_pfms_uid,           '') AS reversal_pfms_uid,
            COALESCE(r.pfms_negative_posted,        '') AS pfms_negative_posted,
            COALESCE(r.db_deletion_status,          '') AS db_deletion_status
        FROM pao.reversion r
        LEFT JOIN pao.pfms_submission ps
            ON ps.pfms_unique_id = r.original_pfms_uid
           AND ps.pfms_submission_type = 'cb'
        WHERE r.ddo_code   = $1
          AND r.from_date::date = $2::date
        ORDER BY r.business_date ASC
    `
	rows, err := ur.Db.Query(ctx, query, ddoCode, fromDate)
	if err != nil {
		return nil, fmt.Errorf("GetReversionRecordsRepo failed: %w", err)
	}
	defer rows.Close()

	var records []domain.ReversionRecord
	for rows.Next() {
		var rec domain.ReversionRecord
		if err := rows.Scan(
			&rec.ReversionID,
			&rec.DdoCode,
			&rec.FromDate,
			&rec.BusinessDate,
			&rec.PfmsReversalType,
			&rec.OriginalPfmsUID,
			&rec.OriginalSubmissionStatus,
			&rec.OriginalTeNumber,
			&rec.CurrentStatus,
			&rec.ReversalPfmsUID,
			&rec.PfmsNegativePosted,
			&rec.DbDeletionStatus,
		); err != nil {
			return nil, fmt.Errorf("GetReversionRecordsRepo scan failed: %w", err)
		}
		records = append(records, rec)
	}
	if err := rows.Err(); err != nil {
		return nil, fmt.Errorf("GetReversionRecordsRepo rows error: %w", err)
	}
	return records, nil
}

func (ur *PaogenRepository) GetReversionByPfmsUIDRepo(
	gctx *gin.Context,
	originalPfmsUID string,
) (*domain.ReversionRecord, bool, error) {
	ctx, cancel := context.WithTimeout(
		gctx.Request.Context(),
		ur.Cfg.GetDuration("db.QueryTimeoutLow"),
	)
	defer cancel()

	query := `
        SELECT
            r.reversion_id,
            r.ddo_code,
            r.from_date,
            r.business_date,
            COALESCE(r.pfms_reversal_type,         '') AS pfms_reversal_type,
            COALESCE(r.original_pfms_uid,           '') AS original_pfms_uid,
            COALESCE(r.original_submission_status,  '') AS original_submission_status,
            COALESCE(r.original_te_number,          '') AS original_te_number,
            COALESCE(ps.submission_status,          '') AS current_status,
            COALESCE(r.reversal_pfms_uid,           '') AS reversal_pfms_uid,
            COALESCE(r.pfms_negative_posted,        '') AS pfms_negative_posted,
            COALESCE(r.db_deletion_status,          '') AS db_deletion_status
        FROM pao.reversion r
        LEFT JOIN pao.pfms_submission ps
            ON ps.pfms_unique_id = r.original_pfms_uid
           AND ps.pfms_submission_type = 'cb'
        WHERE r.original_pfms_uid = $1
        LIMIT 1
    `
	var rec domain.ReversionRecord
	err := ur.Db.QueryRow(ctx, query, originalPfmsUID).Scan(
		&rec.ReversionID,
		&rec.DdoCode,
		&rec.FromDate,
		&rec.BusinessDate,
		&rec.PfmsReversalType,
		&rec.OriginalPfmsUID,
		&rec.OriginalSubmissionStatus,
		&rec.OriginalTeNumber,
		&rec.CurrentStatus,
		&rec.ReversalPfmsUID,
		&rec.PfmsNegativePosted,
		&rec.DbDeletionStatus,
	)
	if err != nil {
		if errors.Is(err, pgx.ErrNoRows) {
			return nil, false, nil
		}
		return nil, false, fmt.Errorf("GetReversionByPfmsUIDRepo failed: %w", err)
	}
	return &rec, true, nil
}

func (ur *PaogenRepository) UpdateReversionAfterPfmsRepo1904(
	gctx *gin.Context,
	originalPfmsUID string,
	reversalPfmsUID string,
) error {
	ctx, cancel := context.WithTimeout(
		gctx.Request.Context(),
		ur.Cfg.GetDuration("db.QueryTimeoutLow"),
	)
	defer cancel()

	query := `
        UPDATE pao.reversion
        SET reversal_pfms_uid   = $1,
            pfms_negative_posted = 'YES'
        WHERE original_pfms_uid = $2
    `
	_, err := ur.Db.Exec(ctx, query, reversalPfmsUID, originalPfmsUID)
	if err != nil {
		return fmt.Errorf("UpdateReversionAfterPfmsRepo failed: %w", err)
	}
	return nil
}

func (ur *PaogenRepository) UpdateReversionAfterPfmsRepo(
	ctx context.Context, // ← FIXED: was *gin.Context
	originalPfmsUID string,
	reversalPfmsUID string,
) error {
	dbCtx, cancel := context.WithTimeout(ctx, ur.Cfg.GetDuration("db.QueryTimeoutLow"))
	defer cancel()

	query := `
        UPDATE pao.reversion
        SET reversal_pfms_uid    = $1,
            pfms_negative_posted = 'YES'
        WHERE original_pfms_uid  = $2
    `
	_, err := ur.Db.Exec(dbCtx, query, reversalPfmsUID, originalPfmsUID)
	if err != nil {
		return fmt.Errorf("UpdateReversionAfterPfmsRepo failed: %w", err)
	}
	return nil
}

func (ur *PaogenRepository) InsertPfmsSubmissionNew1904(
	gctx *gin.Context,
	pfmsUniqueId string,
	submissionType string,
	cbRequest []domain.CbData,
	teRequest domain.TransferEntryDetail,
	businessDate string,
	submissionDate time.Time,
	submissionData domain.Payload,
	submissionStatus string,
	errorDescription string,
	originalPfmsUID string, // NEW: "" for cb rows → NULL
) error {
	ctx, cancel := context.WithTimeout(
		gctx.Request.Context(),
		ur.Cfg.GetDuration("db.QueryTimeoutMed"),
	)
	defer cancel()

	businessDateTime, err := time.Parse("2006-01-02", businessDate)
	if err != nil {
		return fmt.Errorf("failed to parse businessDate: %v", err)
	}

	cbRequestJSON, err := json.Marshal(cbRequest)
	if err != nil {
		return fmt.Errorf("failed to marshal cbRequest: %v", err)
	}

	submissionDataJSON, err := json.Marshal(submissionData)
	if err != nil {
		return fmt.Errorf("failed to marshal submissionData: %v", err)
	}

	var errorDesc interface{}
	if errorDescription != "" {
		errorDesc = errorDescription
	}

	var origUID interface{}
	if originalPfmsUID != "" {
		origUID = originalPfmsUID
	}

	query := dblib.Psql.
		Insert("pao.pfms_submission").
		Columns(
			"pfms_unique_id",
			"pfms_submission_type",
			"cb_request",
			"business_date",
			"submission_date",
			"submission_data",
			"submission_status",
			"error_description",
			"original_pfms_uid",
		).
		Values(
			pfmsUniqueId,
			submissionType,
			cbRequestJSON,
			businessDateTime,
			submissionDate,
			submissionDataJSON,
			submissionStatus,
			errorDesc,
			origUID,
		)

	_, err = dblib.Insert(ctx, ur.Db, query)
	return err
}

func (ur *PaogenRepository) InsertPfmsSubmissionNew(
	ctx context.Context, // ← FIXED: was *gin.Context
	pfmsUniqueId string,
	submissionType string,
	cbRequest []domain.CbData,
	teRequest domain.TransferEntryDetail,
	businessDate string,
	submissionDate time.Time,
	submissionData domain.Payload,
	submissionStatus string,
	errorDescription string,
	originalPfmsUID string,
) error {
	dbCtx, cancel := context.WithTimeout(ctx, ur.Cfg.GetDuration("db.QueryTimeoutMed"))
	defer cancel()

	businessDateTime, err := time.Parse("2006-01-02", businessDate)
	if err != nil {
		return fmt.Errorf("failed to parse businessDate: %v", err)
	}

	cbRequestJSON, err := json.Marshal(cbRequest)
	if err != nil {
		return fmt.Errorf("failed to marshal cbRequest: %v", err)
	}

	submissionDataJSON, err := json.Marshal(submissionData)
	if err != nil {
		return fmt.Errorf("failed to marshal submissionData: %v", err)
	}

	var errorDesc interface{}
	if errorDescription != "" {
		errorDesc = errorDescription
	}

	var origUID interface{}
	if originalPfmsUID != "" {
		origUID = originalPfmsUID
	}

	query := dblib.Psql.
		Insert("pao.pfms_submission").
		Columns(
			"pfms_unique_id",
			"pfms_submission_type",
			"cb_request",
			"business_date",
			"submission_date",
			"submission_data",
			"submission_status",
			"error_description",
			"original_pfms_uid",
		).
		Values(
			pfmsUniqueId,
			submissionType,
			cbRequestJSON,
			businessDateTime,
			submissionDate,
			submissionDataJSON,
			submissionStatus,
			errorDesc,
			origUID,
		)

	_, err = dblib.Insert(dbCtx, ur.Db, query)
	return err
}

// Fetches full submission_data payload by pfms_unique_id
// Used in PostNegativeEntryHandler to get the payload for building reversal
func (ur *PaogenRepository) GetPfmsSubmissionFullByUIDRepo(
	gctx *gin.Context,
	pfmsUID string,
) (domain.Payload, string, []domain.CbData, bool, error) {

	ctx, cancel := context.WithTimeout(
		gctx.Request.Context(),
		ur.Cfg.GetDuration("db.QueryTimeoutMed"),
	)
	defer cancel()

	query := `
        SELECT
            pfms_unique_id,
            submission_data,
            cb_request
        FROM pao.pfms_submission
        WHERE pfms_unique_id = $1
        LIMIT 1
    `
	var (
		uid      string
		rawData  []byte
		rawCbReq []byte
	)
	err := ur.Db.QueryRow(ctx, query, pfmsUID).
		Scan(&uid, &rawData, &rawCbReq)
	if err != nil {
		if errors.Is(err, pgx.ErrNoRows) {
			return domain.Payload{}, "", nil, false, nil
		}
		return domain.Payload{}, "", nil, false,
			fmt.Errorf("GetPfmsSubmissionFullByUIDRepo failed: %w", err)
	}

	var payload domain.Payload
	if err := json.Unmarshal(rawData, &payload); err != nil {
		return domain.Payload{}, "", nil, false,
			fmt.Errorf("failed to unmarshal submission_data: %w", err)
	}

	var cbRequests []domain.CbData
	if err := json.Unmarshal(rawCbReq, &cbRequests); err != nil {
		return domain.Payload{}, "", nil, false,
			fmt.Errorf("failed to unmarshal cb_request: %w", err)
	}

	return payload, uid, cbRequests, true, nil
}

func (ur *PaogenRepository) GetTeNumberByUIDRepo(
	gctx *gin.Context,
	pfmsUID string,
) (string, string, error) {
	// returns: teNumber, submissionStatus, error
	ctx, cancel := context.WithTimeout(
		gctx.Request.Context(),
		ur.Cfg.GetDuration("db.QueryTimeoutLow"), // low timeout — PK lookup
	)
	defer cancel()

	query := `
		SELECT
			COALESCE(te_number,        '') AS te_number,
			COALESCE(submission_status,'') AS submission_status
		FROM pao.pfms_submission
		WHERE pfms_unique_id = $1
		LIMIT 1
	`
	var teNumber, submissionStatus string
	err := ur.Db.QueryRow(ctx, query, pfmsUID).
		Scan(&teNumber, &submissionStatus)
	if err != nil {
		if errors.Is(err, pgx.ErrNoRows) {
			return "", "", nil
		}
		return "", "", fmt.Errorf("GetTeNumberByUIDRepo failed: %w", err)
	}
	return teNumber, submissionStatus, nil
}

func (ur *PaogenRepository) GetReversionPendingRepo(
	gctx *gin.Context,
	paoCode string,
) ([]domain.ReversionRecord, error) {
	ctx, cancel := context.WithTimeout(
		gctx.Request.Context(),
		ur.Cfg.GetDuration("db.QueryTimeoutMed"),
	)
	defer cancel()

	query := `
		SELECT
			r.reversion_id,
			r.ddo_code,
			COALESCE(dm.ddo_name,                  '') AS ddo_name,
			 dm.ddo_office_id                           AS ddo_office_id,
			r.from_date,
			r.request_date,
			r.business_date,
			COALESCE(r.pfms_reversal_type,         '') AS pfms_reversal_type,
			COALESCE(r.original_pfms_uid,           '') AS original_pfms_uid,
			COALESCE(r.original_submission_status,  '') AS original_submission_status,
			COALESCE(r.original_te_number,          '') AS original_te_number,
			COALESCE(ps.submission_status,          '') AS current_status,
			COALESCE(ps.te_number,                  '') AS current_te_number,
			COALESCE(r.reversal_pfms_uid,           '') AS reversal_pfms_uid,
			COALESCE(r.pfms_negative_posted,        '') AS pfms_negative_posted,
			COALESCE(r.db_deletion_status,          '') AS db_deletion_status
		FROM pao.reversion r
		INNER JOIN pao.ddo_master dm
			ON dm.ddo_code = r.ddo_code
		LEFT JOIN pao.pfms_submission ps
			ON ps.pfms_unique_id      = r.original_pfms_uid
		   AND ps.pfms_submission_type = 'cb'
		WHERE dm.pao_code              = $1
		  AND r.pfms_reversal_type     = 'with_pfms'
		  AND r.pfms_negative_posted   = 'NO'
		ORDER BY r.ddo_code ASC, r.business_date DESC
	`

	rows, err := ur.Db.Query(ctx, query, paoCode)
	if err != nil {
		return nil, fmt.Errorf("GetReversionPendingRepo failed: %w", err)
	}
	defer rows.Close()

	var records []domain.ReversionRecord
	for rows.Next() {
		var rec domain.ReversionRecord
		if err := rows.Scan(
			&rec.ReversionID,
			&rec.DdoCode,
			&rec.DdoName,     // ← new
			&rec.DdoOfficeID, // ← new
			&rec.FromDate,
			&rec.RequestDate,
			&rec.BusinessDate,
			&rec.PfmsReversalType,
			&rec.OriginalPfmsUID,
			&rec.OriginalSubmissionStatus,
			&rec.OriginalTeNumber,
			&rec.CurrentStatus,
			&rec.CurrentTeNumber,
			&rec.ReversalPfmsUID,
			&rec.PfmsNegativePosted,
			&rec.DbDeletionStatus,
		); err != nil {
			return nil, fmt.Errorf("GetReversionPendingRepo scan failed: %w", err)
		}
		records = append(records, rec)
	}
	if err := rows.Err(); err != nil {
		return nil, fmt.Errorf("GetReversionPendingRepo rows error: %w", err)
	}
	return records, nil
}

func (ur *PaogenRepository) GetReversionRecordsMainRepo10042026(
	gctx *gin.Context,
	paoCode string,
	ddoCode string,
	fromDate string,
	toDate string,
	skip int,
	limit int,
) ([]domain.ReversionRecord, error) {
	ctx, cancel := context.WithTimeout(
		gctx.Request.Context(),
		ur.Cfg.GetDuration("db.QueryTimeoutMed"),
	)
	defer cancel()

	query := `
		SELECT
			r.reversion_id,
			r.ddo_code,
			COALESCE(dm.ddo_name,                  '') AS ddo_name,
			COALESCE(dm.ddo_office_id,              0)  AS ddo_office_id,
			r.from_date,
			r.request_date,
			r.business_date,
			COALESCE(r.pfms_reversal_type,         '') AS pfms_reversal_type,
			COALESCE(r.original_pfms_uid,          '') AS original_pfms_uid,
			COALESCE(r.original_submission_status, '') AS original_submission_status,
			COALESCE(r.original_te_number,         '') AS original_te_number,
			COALESCE(r.reversal_pfms_uid,          '') AS reversal_pfms_uid,
			COALESCE(r.pfms_negative_posted,       '') AS pfms_negative_posted,
			COALESCE(r.db_deletion_status,         '') AS db_deletion_status,
			CASE
				WHEN r.pfms_reversal_type = 'with_pfms'
				THEN COALESCE(ps.submission_status, '')
				ELSE ''
			END AS current_status,
			CASE
				WHEN r.pfms_reversal_type = 'with_pfms'
				THEN COALESCE(ps.te_number, '')
				ELSE ''
			END AS current_te_number,
			r.request_employee_id,
			COALESCE(r.remarks, '') AS remarks
		FROM pao.reversion r
		INNER JOIN pao.ddo_master dm
			ON dm.ddo_code = r.ddo_code
		LEFT JOIN pao.pfms_submission ps
			ON  ps.pfms_unique_id       = r.original_pfms_uid
			AND ps.pfms_submission_type = 'cb'
		WHERE dm.pao_code           = $1
		  AND r.business_date::date >= $2::date
		  AND r.business_date::date <= $3::date
		  AND ($4 = '' OR r.ddo_code = $4)
		ORDER BY r.ddo_code ASC, r.business_date DESC
		LIMIT  $5
		OFFSET $6
	`

	rows, err := ur.Db.Query(ctx, query,
		paoCode,
		fromDate,
		toDate,
		ddoCode,
		limit,
		skip,
	)
	if err != nil {
		return nil, fmt.Errorf("GetReversionRecordsRepo failed: %w", err)
	}
	defer rows.Close()

	var records []domain.ReversionRecord
	for rows.Next() {
		var rec domain.ReversionRecord
		if err := rows.Scan(
			&rec.ReversionID,
			&rec.DdoCode,
			&rec.DdoName,
			&rec.DdoOfficeID,
			&rec.FromDate,
			&rec.RequestDate,
			&rec.BusinessDate,
			&rec.PfmsReversalType,
			&rec.OriginalPfmsUID,
			&rec.OriginalSubmissionStatus,
			&rec.OriginalTeNumber,
			&rec.ReversalPfmsUID,
			&rec.PfmsNegativePosted,
			&rec.DbDeletionStatus,
			&rec.CurrentStatus,
			&rec.CurrentTeNumber,
			&rec.RequestEmployeeID,
			&rec.Remarks,
		); err != nil {
			return nil, fmt.Errorf("GetReversionRecordsRepo scan failed: %w", err)
		}
		records = append(records, rec)
	}
	if err := rows.Err(); err != nil {
		return nil, fmt.Errorf("GetReversionRecordsRepo rows error: %w", err)
	}
	return records, nil
}

// changed to only with pfms type
func (ur *PaogenRepository) GetReversionRecordsMainRepo20042026(
	gctx *gin.Context,
	paoCode string,
	ddoCode string,
	fromDate string,
	toDate string,
	skip int,
	limit int,
) ([]domain.ReversionRecord, error) {
	ctx, cancel := context.WithTimeout(
		gctx.Request.Context(),
		ur.Cfg.GetDuration("db.QueryTimeoutMed"),
	)
	defer cancel()

	query := `
		SELECT
			r.reversion_id,
			r.ddo_code,
			COALESCE(dm.ddo_name,                  '') AS ddo_name,
			COALESCE(dm.ddo_office_id,              0)  AS ddo_office_id,
			r.from_date,
			r.request_date,
			r.business_date,
			COALESCE(r.pfms_reversal_type,         '') AS pfms_reversal_type,
			COALESCE(r.original_pfms_uid,          '') AS original_pfms_uid,
			COALESCE(r.original_submission_status, '') AS original_submission_status,
			COALESCE(r.original_te_number,         '') AS original_te_number,
			COALESCE(r.reversal_pfms_uid,          '') AS reversal_pfms_uid,
			COALESCE(r.pfms_negative_posted,       '') AS pfms_negative_posted,
			COALESCE(r.db_deletion_status,         '') AS db_deletion_status,
			COALESCE(ps.submission_status,         '') AS current_status,
			COALESCE(ps.te_number,                 '') AS current_te_number,
			r.request_employee_id,
			COALESCE(r.remarks, '') AS remarks
		FROM pao.reversion r
		INNER JOIN pao.ddo_master dm
			ON dm.ddo_code = r.ddo_code
		LEFT JOIN pao.pfms_submission ps
			ON  ps.pfms_unique_id       = r.original_pfms_uid
			AND ps.pfms_submission_type = 'cb'
		WHERE dm.pao_code               = $1
		  AND r.business_date::date    >= $2::date
		  AND r.business_date::date    <= $3::date
		  AND ($4 = '' OR r.ddo_code   = $4)
		  AND r.pfms_reversal_type      = 'with_pfms'
		ORDER BY r.ddo_code ASC, r.business_date DESC
		LIMIT  $5
		OFFSET $6
	`

	rows, err := ur.Db.Query(ctx, query,
		paoCode,
		fromDate,
		toDate,
		ddoCode,
		limit,
		skip,
	)
	if err != nil {
		return nil, fmt.Errorf("GetReversionRecordsRepo failed: %w", err)
	}
	defer rows.Close()

	var records []domain.ReversionRecord
	for rows.Next() {
		var rec domain.ReversionRecord
		if err := rows.Scan(
			&rec.ReversionID,
			&rec.DdoCode,
			&rec.DdoName,
			&rec.DdoOfficeID,
			&rec.FromDate,
			&rec.RequestDate,
			&rec.BusinessDate,
			&rec.PfmsReversalType,
			&rec.OriginalPfmsUID,
			&rec.OriginalSubmissionStatus,
			&rec.OriginalTeNumber,
			&rec.ReversalPfmsUID,
			&rec.PfmsNegativePosted,
			&rec.DbDeletionStatus,
			&rec.CurrentStatus,
			&rec.CurrentTeNumber,
			&rec.RequestEmployeeID,
			&rec.Remarks,
		); err != nil {
			return nil, fmt.Errorf("GetReversionRecordsRepo scan failed: %w", err)
		}
		records = append(records, rec)
	}
	if err := rows.Err(); err != nil {
		return nil, fmt.Errorf("GetReversionRecordsRepo rows error: %w", err)
	}
	return records, nil
}

func (ur *PaogenRepository) GetReversionRecordsMainRepo(
	gctx *gin.Context,
	paoCode string,
	ddoCode string,
	fromDate string,
	toDate string,
	skip int,
	limit int,
) ([]domain.ReversionRecord, error) {
	ctx, cancel := context.WithTimeout(
		gctx.Request.Context(),
		ur.Cfg.GetDuration("db.QueryTimeoutMed"),
	)
	defer cancel()

	query := `
		SELECT
			r.reversion_id,
			r.ddo_code,
			COALESCE(dm.ddo_name,                  '') AS ddo_name,
			COALESCE(dm.ddo_office_id,              0)  AS ddo_office_id,
			r.from_date,
			r.request_date,
			r.business_date,
			COALESCE(r.pfms_reversal_type,         '') AS pfms_reversal_type,
			COALESCE(r.original_pfms_uid,          '') AS original_pfms_uid,
			COALESCE(r.original_submission_status, '') AS original_submission_status,
			COALESCE(r.original_te_number,         '') AS original_te_number,
			COALESCE(r.reversal_pfms_uid,          '') AS reversal_pfms_uid,
			COALESCE(r.pfms_negative_posted,       '') AS pfms_negative_posted,
			COALESCE(r.db_deletion_status,         '') AS db_deletion_status,
			COALESCE(ps_orig.submission_status,    '') AS current_status,
			COALESCE(ps_orig.te_number,            '') AS current_te_number,
			COALESCE(ps_rev.submission_status,     '') AS reversal_submission_status,
			COALESCE(ps_rev.te_number,             '') AS reversal_te_number,
			r.request_employee_id,
			COALESCE(r.remarks, '') AS remarks
		FROM pao.reversion r
		INNER JOIN pao.ddo_master dm
			ON dm.ddo_code = r.ddo_code
		LEFT JOIN pao.pfms_submission ps_orig
			ON  ps_orig.pfms_unique_id       = r.original_pfms_uid
			AND ps_orig.pfms_submission_type = 'cb'
		LEFT JOIN pao.pfms_submission ps_rev
			ON  ps_rev.pfms_unique_id        = r.reversal_pfms_uid
			AND ps_rev.pfms_submission_type  = 'cb_reversal'
		WHERE dm.pao_code               = $1
		  AND r.business_date::date    >= $2::date
		  AND r.business_date::date    <= $3::date
		  AND ($4 = '' OR r.ddo_code   = $4)
		  AND r.pfms_reversal_type      = 'with_pfms'
		ORDER BY r.ddo_code ASC, r.business_date DESC
		LIMIT  $5
		OFFSET $6
	`

	rows, err := ur.Db.Query(ctx, query,
		paoCode,
		fromDate,
		toDate,
		ddoCode,
		limit,
		skip,
	)
	if err != nil {
		return nil, fmt.Errorf("GetReversionRecordsRepo failed: %w", err)
	}
	defer rows.Close()

	var records []domain.ReversionRecord
	for rows.Next() {
		var rec domain.ReversionRecord
		if err := rows.Scan(
			&rec.ReversionID,
			&rec.DdoCode,
			&rec.DdoName,
			&rec.DdoOfficeID,
			&rec.FromDate,
			&rec.RequestDate,
			&rec.BusinessDate,
			&rec.PfmsReversalType,
			&rec.OriginalPfmsUID,
			&rec.OriginalSubmissionStatus,
			&rec.OriginalTeNumber,
			&rec.ReversalPfmsUID,
			&rec.PfmsNegativePosted,
			&rec.DbDeletionStatus,
			&rec.CurrentStatus,
			&rec.CurrentTeNumber,
			&rec.ReversalSubmissionStatus, // new
			&rec.ReversalTeNumber,         // new
			&rec.RequestEmployeeID,
			&rec.Remarks,
		); err != nil {
			return nil, fmt.Errorf("GetReversionRecordsRepo scan failed: %w", err)
		}
		records = append(records, rec)
	}
	if err := rows.Err(); err != nil {
		return nil, fmt.Errorf("GetReversionRecordsRepo rows error: %w", err)
	}
	return records, nil
}

func (ur *PaogenRepository) RevertCashAccountRepo(
	gctx *gin.Context,
	hoCode string,
	caMonthYear string,
) (string, error) {
	ctx, cancel := context.WithTimeout(gctx.Request.Context(), ur.Cfg.GetDuration("db.QueryTimeoutMed"))
	defer cancel()

	// Query 1: delete from kafka_cash_account
	deleteBuilder1 := dblib.Psql.Delete("pao.kafka_cash_account").
		Where("ho_code = ?", hoCode).
		Where("camonthyear = ?", caMonthYear)

	sql1, args1, err := deleteBuilder1.ToSql()
	if err != nil {
		return "", err
	}
	if _, err := ur.Db.Exec(ctx, sql1, args1...); err != nil {
		return "", err
	}

	// Query 2: delete from pfms_monthly_detail with subquery
	deleteBuilder2 := dblib.Psql.Delete("pao.pfms_monthly_detail").
		Where("pfms_monthly_id IN ("+
			"SELECT m.pfms_monthly_id FROM pao.pfms_monthly_main m "+
			"WHERE m.ho_code = ? AND m.camonthyear = ?)", hoCode, caMonthYear)

	sql2, args2, err := deleteBuilder2.ToSql()
	if err != nil {
		return "", err
	}
	if _, err := ur.Db.Exec(ctx, sql2, args2...); err != nil {
		return "", err
	}

	// Step before deleting pfms_monthly_main: fetch pfms_unique_id
	var pfmsUniqueID sql.NullString
	checkQuery := `
        SELECT pfms_unique_id
        FROM pao.pfms_monthly_main
        WHERE ho_code = $1
          AND camonthyear = $2
        LIMIT 1
    `
	err = ur.Db.QueryRow(ctx, checkQuery, hoCode, caMonthYear).Scan(&pfmsUniqueID)
	if err != nil && err != pgx.ErrNoRows {
		return "", err
	}

	// Query 3: delete from pfms_monthly_main
	deleteBuilder3 := dblib.Psql.Delete("pao.pfms_monthly_main").
		Where("ho_code = ?", hoCode).
		Where("camonthyear = ?", caMonthYear)

	sql3, args3, err := deleteBuilder3.ToSql()
	if err != nil {
		return "", err
	}
	if _, err := ur.Db.Exec(ctx, sql3, args3...); err != nil {
		return "", err
	}

	if pfmsUniqueID.Valid {
		return pfmsUniqueID.String, nil
	}
	return "", nil
}

func (sr *PaogenRepository) DeletePAOCashAccountRepo03042026(
	gctx *gin.Context,
	hoCode int,
	ddoCode string,
	monthYear string, // MM-YYYY
) error {

	ctx, cancel := context.WithTimeout(gctx.Request.Context(), sr.Cfg.GetDuration("db.QueryTimeoutMed"))
	defer cancel()

	// Convert MM-YYYY → MMYYYY
	t, err := time.Parse("01-2006", monthYear)
	if err != nil {
		return fmt.Errorf("invalid month_year format")
	}
	period := t.Format("012006") // MMYYYY

	// Construct pfms_ddo_id
	pfmsDDOID := ddoCode + period

	tx, err := sr.Db.Begin(ctx)
	if err != nil {
		return err
	}
	defer tx.Rollback(ctx)

	//  1️⃣ DELETE KAFKA CASH ACCOUNT (SOURCE FIRST)
	deleteKafka := dblib.Psql.Delete("pao.kafka_cash_account").
		Where("office_id = ?", hoCode).
		Where("period = ?", period)

	sqlKafka, argsKafka, err := deleteKafka.ToSql()
	if err != nil {
		return err
	}

	res, err := tx.Exec(ctx, sqlKafka, argsKafka...)
	if err != nil {
		return fmt.Errorf("delete kafka_cash_account failed: %w", err)
	}

	rowsKafka := res.RowsAffected()

	if rowsKafka == 0 {
		log.Warn(ctx, "Kafka row not found (already deleted or not present)")
	}

	// 2️⃣ DELETE DETAIL (child, multiple rows)
	_, err = tx.Exec(ctx, `
		DELETE FROM pao.pfms_monthly_detail
		WHERE pfms_ddo_id = $1
	`, pfmsDDOID)
	if err != nil {
		return fmt.Errorf("delete pfms_monthly_detail failed: %w", err)
	}

	// 3️⃣ DELETE MAIN (single row)
	_, err = tx.Exec(ctx, `
		DELETE FROM pao.pfms_monthly_main
		WHERE pfms_ddo_id = $1
	`, pfmsDDOID)
	if err != nil {
		return fmt.Errorf("delete pfms_monthly_main failed: %w", err)
	}

	// 4️⃣ DELETE BROADSHEET (multiple rows)
	_, err = tx.Exec(ctx, `
		DELETE FROM pao.broad_sheet
		WHERE ddo_code = $1
		  AND broadsheet_month = $2
	`, ddoCode, period)
	if err != nil {
		return fmt.Errorf("delete broad_sheet failed: %w", err)
	}

	// ✅ COMMIT
	if err := tx.Commit(ctx); err != nil {
		return err
	}

	return nil
}

func (sr *PaogenRepository) DeletePAOCashAccountRepoWithCount(
	gctx *gin.Context,
	hoCode int,
	ddoCode string,
	monthYear string,
) (int64, error) {

	ctx, cancel := context.WithTimeout(gctx.Request.Context(), sr.Cfg.GetDuration("db.QueryTimeoutMed"))
	defer cancel()

	t, err := time.Parse("01-2006", monthYear)
	if err != nil {
		return 0, fmt.Errorf("INVALID_MONTH_FORMAT")
	}
	period := t.Format("012006")

	pfmsDDOID := ddoCode + period

	tx, err := sr.Db.Begin(ctx)
	if err != nil {
		return 0, fmt.Errorf("TX_BEGIN_FAILED: %w", err)
	}
	defer tx.Rollback(ctx)

	// 1️⃣ Kafka delete
	deleteKafka := dblib.Psql.Delete("pao.kafka_cash_account").
		Where("office_id = ?", hoCode).
		Where("period = ?", monthYear)

	sql, args, err := deleteKafka.ToSql()
	if err != nil {
		return 0, fmt.Errorf("KAFKA_SQL_BUILD_FAILED: %w", err)
	}

	res, err := tx.Exec(ctx, sql, args...)
	if err != nil {
		return 0, fmt.Errorf("PAO_KAFKA_DELETE_FAILED: %w", err)
	}

	kafkaRows := res.RowsAffected()

	if kafkaRows == 0 {
		log.Warn(gctx, "Kafka row not found (already deleted or not present)")
	}

	// 2️⃣ PFMS detail
	if _, err = tx.Exec(ctx,
		`DELETE FROM pao.pfms_monthly_detail WHERE pfms_ddo_id = $1`,
		pfmsDDOID,
	); err != nil {
		return 0, fmt.Errorf("PAO_PFMS_DETAIL_DELETE_FAILED: %w", err)
	}

	// 3️⃣ PFMS main
	if _, err = tx.Exec(ctx,
		`DELETE FROM pao.pfms_monthly_main WHERE pfms_ddo_id = $1`,
		pfmsDDOID,
	); err != nil {
		return 0, fmt.Errorf("PAO_PFMS_MAIN_DELETE_FAILED: %w", err)
	}

	// 4️⃣ Broadsheet
	if _, err = tx.Exec(ctx,
		`DELETE FROM pao.broad_sheet WHERE ddo_code = $1 AND broadsheet_month = $2`,
		ddoCode, period,
	); err != nil {
		return 0, fmt.Errorf("PAO_BROADSHEET_DELETE_FAILED: %w", err)
	}

	if err := tx.Commit(ctx); err != nil {
		return 0, fmt.Errorf("TX_COMMIT_FAILED: %w", err)
	}

	return kafkaRows, nil
}

func (sr *PaogenRepository) DeletePAOCashAccountRepoTracked0705(
	gctx *gin.Context,
	officeID int,
	ddoCode string,
	monthYear string,
	kafkaStatus *string,
	pfmsDetailStatus *string,
	pfmsMainStatus *string,
	broadStatus *string,
) error {

	ctx, cancel := context.WithTimeout(gctx.Request.Context(), sr.Cfg.GetDuration("db.QueryTimeoutMed"))
	defer cancel()

	t, err := time.Parse("01-2006", monthYear)
	if err != nil {
		return fmt.Errorf("INVALID_PERIOD_FORMAT")
	}
	period := t.Format("012006")
	pfmsDDOID := ddoCode + period

	tx, err := sr.Db.Begin(ctx)
	if err != nil {
		return fmt.Errorf("failed to begin transaction: %w", err)
	}
	defer tx.Rollback(ctx)

	// ---------------- Kafka ----------------
	if _, execErr := tx.Exec(ctx,
		`DELETE FROM pao.kafka_cash_account WHERE office_id=$1 AND period=$2`,
		officeID, monthYear,
	); execErr != nil {
		*kafkaStatus = "FAILED"
		return fmt.Errorf("kafka delete failed: %w", execErr)
	}
	*kafkaStatus = "SUCCESS"

	// ---------------- PFMS Detail ----------------
	if _, execErr := tx.Exec(ctx,
		`DELETE FROM pao.pfms_monthly_detail WHERE pfms_ddo_id=$1`,
		pfmsDDOID,
	); execErr != nil {
		*pfmsDetailStatus = "FAILED"
		return fmt.Errorf("pfms detail delete failed: %w", execErr)
	}
	*pfmsDetailStatus = "SUCCESS"

	// ---------------- PFMS Main ----------------
	if _, execErr := tx.Exec(ctx,
		`DELETE FROM pao.pfms_monthly_main WHERE pfms_ddo_id=$1`,
		pfmsDDOID,
	); execErr != nil {
		*pfmsMainStatus = "FAILED"
		return fmt.Errorf("pfms main delete failed: %w", execErr)
	}
	*pfmsMainStatus = "SUCCESS"

	// ---------------- Broadsheet ----------------
	if _, execErr := tx.Exec(ctx,
		`DELETE FROM pao.broad_sheet WHERE ddo_code=$1 AND broadsheet_month=$2`,
		ddoCode, period,
	); execErr != nil {
		*broadStatus = "FAILED"
		return fmt.Errorf("broadsheet delete failed: %w", execErr)
	}
	*broadStatus = "SUCCESS"

	// ---------------- Commit ----------------
	if err := tx.Commit(ctx); err != nil {
		return fmt.Errorf("transaction commit failed: %w", err)
	}

	return nil
}

func (sr *PaogenRepository) DeletePAOCashAccountRepoTracked(
	gctx *gin.Context,
	officeID int,
	ddoCode string,
	monthYear string,
	kafkaStatus *string,
	pfmsDetailStatus *string,
	pfmsMainStatus *string,
	broadStatus *string,
) error {

	txCtx, txCancel := context.WithTimeout(context.Background(), sr.Cfg.GetDuration("db.QueryTimeoutMed"))
	defer txCancel()

	t, err := time.Parse("01-2006", monthYear)
	if err != nil {
		return fmt.Errorf("INVALID_PERIOD_FORMAT")
	}
	period := t.Format("012006")
	pfmsDDOID := ddoCode + period

	tx, err := sr.Db.Begin(txCtx)
	if err != nil {
		return fmt.Errorf("failed to begin transaction: %w", err)
	}
	defer tx.Rollback(txCtx)

	// ---------------- Kafka ----------------
	res1, execErr := tx.Exec(txCtx,
		`DELETE FROM pao.kafka_cash_account WHERE office_id=$1 AND period=$2`,
		officeID, monthYear,
	)
	if execErr != nil {
		*kafkaStatus = "FAILED"
		return fmt.Errorf("kafka delete failed: %w", execErr)
	}
	if res1.RowsAffected() == 0 {
		*kafkaStatus = "NOT_FOUND" // soft — continue
	} else {
		*kafkaStatus = "SUCCESS"
	}

	// ---------------- PFMS Detail FIRST (child — must delete before main) ----------------
	res2, execErr := tx.Exec(txCtx,
		`DELETE FROM pao.pfms_monthly_detail WHERE pfms_ddo_id=$1`,
		pfmsDDOID,
	)
	if execErr != nil {
		*pfmsDetailStatus = "FAILED"
		return fmt.Errorf("pfms detail delete failed: %w", execErr)
	}
	if res2.RowsAffected() == 0 {
		*pfmsDetailStatus = "NOT_FOUND" // soft — continue
	} else {
		*pfmsDetailStatus = "SUCCESS"
	}

	// ---------------- PFMS Main SECOND (parent) ----------------
	res3, execErr := tx.Exec(txCtx,
		`DELETE FROM pao.pfms_monthly_main WHERE pfms_ddo_id=$1`,
		pfmsDDOID,
	)
	if execErr != nil {
		*pfmsMainStatus = "FAILED"
		return fmt.Errorf("pfms main delete failed: %w", execErr)
	}
	if res3.RowsAffected() == 0 {
		*pfmsMainStatus = "NOT_FOUND" // soft — continue
	} else {
		*pfmsMainStatus = "SUCCESS"
	}

	// ---------------- Broadsheet ----------------
	res4, execErr := tx.Exec(txCtx,
		`DELETE FROM pao.broad_sheet WHERE ddo_code=$1 AND broadsheet_month=$2`,
		ddoCode, period,
	)
	if execErr != nil {
		*broadStatus = "FAILED"
		return fmt.Errorf("broadsheet delete failed: %w", execErr)
	}
	if res4.RowsAffected() == 0 {
		*broadStatus = "NOT_FOUND" // soft — continue
	} else {
		*broadStatus = "SUCCESS"
	}

	// ---------------- Commit ----------------
	if err := tx.Commit(txCtx); err != nil {
		return fmt.Errorf("transaction commit failed: %w", err)
	}

	return nil
}

func (sr *PaogenRepository) InsertCashAccReversion(
	ctx context.Context,
	officeID int,
	employeeID int,
	ddoCode string,
	period string,
	remark string,
	subStatus string,
	kafkaStatus string,
	pfmsDetail string,
	pfmsMain string,
	broad string,
	final string,
) error {

	dbCtx, cancel := context.WithTimeout(ctx, sr.Cfg.GetDuration("db.QueryTimeoutMed"))
	defer cancel()

	query := `
	INSERT INTO pao.cashacc_reversion (
		office_id,
		request_employee_id,
		ddo_code,
		period,
		remark,
		subaccounts_status,
		kafka_status,
		pfms_detail_status,
		pfms_main_status,
		broadsheet_status,
		final_status
	)
	VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11)
	`

	res, err := sr.Db.Exec(dbCtx, query,
		officeID,
		employeeID,
		ddoCode,
		period,
		remark,
		subStatus,
		kafkaStatus,
		pfmsDetail,
		pfmsMain,
		broad,
		final,
	)

	if err != nil {
		return fmt.Errorf("AUDIT_INSERT_FAILED: %w", err)
	}

	rows := res.RowsAffected()
	log.Info(dbCtx, "Rows inserted:", rows)

	return nil
}

func (ur *PaogenRepository) GetDdoPfmsStatusRepo(
	gctx *gin.Context,
	ddoCode string,
	reqMetadata port.MetaDataRequest,
) ([]domain.DdoPfmsStatus, error) {
	ctx, cancel := context.WithTimeout(
		gctx.Request.Context(),
		ur.Cfg.GetDuration("db.QueryTimeoutMed"),
	)
	defer cancel()
	log.Debug(gctx, "Came inside GetDdoPfmsStatusRepo")

	const query = `
    SELECT
        m.ddo_code,
        COALESCE(d.ddo_name, 'NA')                           AS ddo_name,
        COALESCE(d.ddo_type, 'NA')                           AS ddo_type,
        d.ddo_office_id                                      AS office_id,
        m.period,
        COALESCE(m.h_cash_account_receive_flag, false)       AS h_cash_account_receive_flag,
        COALESCE(m.h_verification_flag, false)               AS h_verification_flag
    FROM pao.pfms_monthly_main m
    LEFT JOIN pao.ddo_master d ON m.ddo_code = d.ddo_code
    WHERE m.ddo_code = $1
    ORDER BY
        SUBSTRING(m.period, 3, 4) DESC,
        SUBSTRING(m.period, 1, 2) DESC
    LIMIT  $2
    OFFSET $3
`
	offset := reqMetadata.Skip * reqMetadata.Limit

	rows, err := ur.Db.Query(ctx, query,
		ddoCode,
		reqMetadata.Limit,
		offset,
	)
	if err != nil {
		return nil, fmt.Errorf("GetDdoPfmsStatusRepo query: %w", err)
	}
	defer rows.Close()

	results, err := pgx.CollectRows(rows, pgx.RowToStructByNameLax[domain.DdoPfmsStatus])
	if err != nil {
		return nil, fmt.Errorf("GetDdoPfmsStatusRepo scan: %w", err)
	}

	return results, nil
}

func (ur *PaogenRepository) GetPaoPraoStatusRepo(
	gctx *gin.Context,
	paoCode string,
	period string,
) (*domain.PaoPraoStatus, error) {
	ctx, cancel := context.WithTimeout(
		gctx.Request.Context(),
		ur.Cfg.GetDuration("db.QueryTimeoutMed"),
	)
	defer cancel()
	log.Debug(gctx, "Came inside GetPaoPraoStatusRepo")

	const query = `
        SELECT
            pao_code,
            period,
            CASE 
                WHEN account_submissionto_prao_status = 'submitted' THEN true
                ELSE false
            END AS submitted
        FROM pao.pao_prao_account_main
        WHERE pao_code = $1
        AND period = $2
        LIMIT 1
    `

	rows, err := ur.Db.Query(ctx, query, paoCode, period)
	if err != nil {
		return nil, fmt.Errorf("GetPaoPraoStatusRepo query: %w", err)
	}
	defer rows.Close()

	results, err := pgx.CollectRows(rows, pgx.RowToStructByNameLax[domain.PaoPraoStatus])
	if err != nil {
		return nil, fmt.Errorf("GetPaoPraoStatusRepo scan: %w", err)
	}

	if len(results) == 0 {
		// no record found means not submitted
		return &domain.PaoPraoStatus{
			PaoCode:   paoCode,
			Period:    period,
			Submitted: false,
		}, nil
	}

	return &results[0], nil
}

func (ur *PaogenRepository) GetAllReversionRecordsRepo20042026(
	gctx *gin.Context,
	paoCode string,
	ddoCode string,
	fromDate string,
	toDate string,
	skip int,
	limit int,
) ([]domain.ReversionRecord, error) {
	ctx, cancel := context.WithTimeout(
		gctx.Request.Context(),
		ur.Cfg.GetDuration("db.QueryTimeoutMed"),
	)
	defer cancel()

	query := `
    SELECT
        r.reversion_id,
        r.ddo_code,
        COALESCE(dm.ddo_name,                  '') AS ddo_name,
        COALESCE(dm.ddo_office_id,              0)  AS ddo_office_id,
        r.from_date,
        r.request_date,
        r.business_date,
        COALESCE(r.pfms_reversal_type,         '') AS pfms_reversal_type,
        COALESCE(r.original_pfms_uid,          '') AS original_pfms_uid,
        COALESCE(r.original_submission_status, '') AS original_submission_status,
        COALESCE(r.original_te_number,         '') AS original_te_number,
        COALESCE(r.reversal_pfms_uid,          '') AS reversal_pfms_uid,
        COALESCE(r.pfms_negative_posted,       '') AS pfms_negative_posted,
        COALESCE(r.db_deletion_status,         '') AS db_deletion_status,
        COALESCE(ps.submission_status,         '') AS current_status,
        COALESCE(ps.te_number,                 '') AS current_te_number,
        r.request_employee_id,
        COALESCE(r.remarks, '') AS remarks
    FROM pao.reversion r
    INNER JOIN pao.ddo_master dm
        ON dm.ddo_code = r.ddo_code
    LEFT JOIN pao.pfms_submission ps
        ON  ps.pfms_unique_id       = r.original_pfms_uid
        AND ps.pfms_submission_type = 'cb'
    WHERE dm.pao_code               = $1
      AND r.business_date::date    >= $2::date
      AND r.business_date::date    <= $3::date
      AND ($4 = '' OR r.ddo_code   = $4)
    ORDER BY r.ddo_code ASC, r.business_date DESC
    LIMIT  $5
    OFFSET $6
	`

	rows, err := ur.Db.Query(ctx, query,
		paoCode,
		fromDate,
		toDate,
		ddoCode,
		limit,
		skip,
	)
	if err != nil {
		return nil, fmt.Errorf("GetAllReversionRecordsRepo failed: %w", err)
	}
	defer rows.Close()

	var records []domain.ReversionRecord
	for rows.Next() {
		var rec domain.ReversionRecord
		if err := rows.Scan(
			&rec.ReversionID,
			&rec.DdoCode,
			&rec.DdoName,
			&rec.DdoOfficeID,
			&rec.FromDate,
			&rec.RequestDate,
			&rec.BusinessDate,
			&rec.PfmsReversalType,
			// &rec.ReversalTypeLabel,  // new field
			&rec.OriginalPfmsUID,
			&rec.OriginalSubmissionStatus,
			&rec.OriginalTeNumber,
			&rec.ReversalPfmsUID,
			&rec.PfmsNegativePosted,
			&rec.DbDeletionStatus,
			&rec.CurrentStatus,
			&rec.CurrentTeNumber,
			&rec.RequestEmployeeID,
			&rec.Remarks,
		); err != nil {
			return nil, fmt.Errorf("GetAllReversionRecordsRepo scan failed: %w", err)
		}
		records = append(records, rec)
	}
	if err := rows.Err(); err != nil {
		return nil, fmt.Errorf("GetAllReversionRecordsRepo rows error: %w", err)
	}
	return records, nil
}

func (ur *PaogenRepository) GetAllReversionRecordsRepo(
	gctx *gin.Context,
	paoCode string,
	ddoCode string,
	fromDate string,
	toDate string,
	skip int,
	limit int,
) ([]domain.ReversionRecord, error) {
	ctx, cancel := context.WithTimeout(
		gctx.Request.Context(),
		ur.Cfg.GetDuration("db.QueryTimeoutMed"),
	)
	defer cancel()

	query := `
    SELECT
        r.reversion_id,
        r.ddo_code,
        COALESCE(dm.ddo_name,                  '') AS ddo_name,
        COALESCE(dm.ddo_office_id,              0)  AS ddo_office_id,
        r.from_date,
        r.request_date,
        r.business_date,
        COALESCE(r.pfms_reversal_type,         '') AS pfms_reversal_type,
        COALESCE(r.original_pfms_uid,          '') AS original_pfms_uid,
        COALESCE(r.original_submission_status, '') AS original_submission_status,
        COALESCE(r.original_te_number,         '') AS original_te_number,
        COALESCE(r.reversal_pfms_uid,          '') AS reversal_pfms_uid,
        COALESCE(r.pfms_negative_posted,       '') AS pfms_negative_posted,
        COALESCE(r.db_deletion_status,         '') AS db_deletion_status,
        COALESCE(ps_orig.submission_status,    '') AS current_status,
        COALESCE(ps_orig.te_number,            '') AS current_te_number,
        COALESCE(ps_rev.submission_status,     '') AS reversal_submission_status,
        COALESCE(ps_rev.te_number,             '') AS reversal_te_number,
        r.request_employee_id,
        COALESCE(r.remarks, '') AS remarks
    FROM pao.reversion r
    INNER JOIN pao.ddo_master dm
        ON dm.ddo_code = r.ddo_code
    LEFT JOIN pao.pfms_submission ps_orig
        ON  ps_orig.pfms_unique_id       = r.original_pfms_uid
        AND ps_orig.pfms_submission_type = 'cb'
    LEFT JOIN pao.pfms_submission ps_rev
        ON  ps_rev.pfms_unique_id        = r.reversal_pfms_uid
        AND ps_rev.pfms_submission_type  = 'cb_reversal'
    WHERE dm.pao_code               = $1
      AND r.business_date::date    >= $2::date
      AND r.business_date::date    <= $3::date
      AND ($4 = '' OR r.ddo_code   = $4)
    ORDER BY r.ddo_code ASC, r.business_date DESC
    LIMIT  $5
    OFFSET $6
	`

	rows, err := ur.Db.Query(ctx, query,
		paoCode,
		fromDate,
		toDate,
		ddoCode,
		limit,
		skip,
	)
	if err != nil {
		return nil, fmt.Errorf("GetAllReversionRecordsRepo failed: %w", err)
	}
	defer rows.Close()

	var records []domain.ReversionRecord
	for rows.Next() {
		var rec domain.ReversionRecord
		if err := rows.Scan(
			&rec.ReversionID,
			&rec.DdoCode,
			&rec.DdoName,
			&rec.DdoOfficeID,
			&rec.FromDate,
			&rec.RequestDate,
			&rec.BusinessDate,
			&rec.PfmsReversalType,
			&rec.OriginalPfmsUID,
			&rec.OriginalSubmissionStatus,
			&rec.OriginalTeNumber,
			&rec.ReversalPfmsUID,
			&rec.PfmsNegativePosted,
			&rec.DbDeletionStatus,
			&rec.CurrentStatus,
			&rec.CurrentTeNumber,
			&rec.ReversalSubmissionStatus, // new
			&rec.ReversalTeNumber,         // new
			&rec.RequestEmployeeID,
			&rec.Remarks,
		); err != nil {
			return nil, fmt.Errorf("GetAllReversionRecordsRepo scan failed: %w", err)
		}
		records = append(records, rec)
	}
	if err := rows.Err(); err != nil {
		return nil, fmt.Errorf("GetAllReversionRecordsRepo rows error: %w", err)
	}
	return records, nil
}
