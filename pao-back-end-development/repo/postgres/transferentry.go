package repository

import (
	"context"
	"encoding/json"
	"fmt"
	"strings"
	"time"

	"gotemplate/core/domain"
	"gotemplate/core/port"

	config "gitlab.cept.gov.in/it-2.0-common/api-config"
	contracts "gitlab.cept.gov.in/it-2.0-common/temporal-contracts"
	"go.temporal.io/sdk/temporal"
	"go.temporal.io/sdk/workflow"

	sq "github.com/Masterminds/squirrel"
	"github.com/gin-gonic/gin"
	"github.com/jackc/pgx/v5"
	dblib "gitlab.cept.gov.in/it-2.0-common/api-db"
	log "gitlab.cept.gov.in/it-2.0-common/api-log"
)

type TransferEntryRepository struct {
	Db  *dblib.DB
	Cfg *config.Config
}

var TransferentryRepoInstance *TransferEntryRepository

// NewUserRepository creates a new user repository instance
func NewTransferEntryRepository(Db *dblib.DB, Cfg *config.Config) *TransferEntryRepository {
	TransferentryRepoInstance = &TransferEntryRepository{
		Db,
		Cfg,
	}
	return TransferentryRepoInstance
}
func (ur *TransferEntryRepository) TransferentryCreationRepo21042026(gctx *gin.Context, request []domain.TransferEntryRequest) ([]domain.InsertedIds, error) {
	ctx, cancel := context.WithTimeout(gctx.Request.Context(), ur.Cfg.GetDuration("db.QueryTimeoutMed"))
	defer cancel()
	log.Debug(gctx, "Came inside PostPfmsverifiedRepo")

	transDate := time.Now().Format("2006-01-02")
	createdDate, err := time.Parse("2006-01-02", transDate)
	if err != nil {
		return nil, err
	}
	createdTime := time.Now()
	createdTimeString := createdTime.Format("20060102150405")
	var verification_status = "created"

	var Inserted_Ids []domain.InsertedIds

	copycount, err := ur.Db.CopyFrom(
		ctx,
		pgx.Identifier{"pao", "transfer_entry"},
		[]string{"pao_code", "ddo_code", "hoa", "transfer_amount", "transfer_type", "created_by", "created_date", "te_source_office_type", "transfer_entry_id", "remarks", "verification_status"},
		pgx.CopyFromSlice(len(request), func(i int) ([]interface{}, error) {
			row := []interface{}{
				request[i].PaoCode,
				request[i].DdoCode,
				request[i].Hoa,
				request[i].TransferAmount,
				request[i].TransferType,
				request[i].CreatedBy,
				createdDate,
				request[i].TeSourceOfficeType,
				request[i].PaoCode.String + createdTimeString,
				request[i].Remarks,
				verification_status,
			}
			// Append the row to the slice
			Inserted_Ids = append(Inserted_Ids, domain.InsertedIds{

				TransferEntryId: request[i].PaoCode.String + createdTimeString,
			})
			return row, nil
		}),
	)

	log.Debug(gctx, "Copy Count", copycount)

	if err != nil {
		log.Debug(gctx, "Error inserting hoas:", err)
		return nil, err
	}
	return Inserted_Ids, nil // Return nil if everything executed successfully

}

func (ur *TransferEntryRepository) TransferentryCreationRepo210420261730(gctx *gin.Context, request []domain.TransferEntryRequest) ([]domain.InsertedIds, error) {
	ctx, cancel := context.WithTimeout(gctx.Request.Context(), ur.Cfg.GetDuration("db.QueryTimeoutMed"))
	defer cancel()
	log.Debug(gctx, "Came inside PostPfmsverifiedRepo")

	transDate := time.Now().Format("2006-01-02")
	createdDate, err := time.Parse("2006-01-02", transDate)
	if err != nil {
		return nil, err
	}
	createdTime := time.Now()
	createdTimeString := createdTime.Format("20060102150405")
	var verification_status = "created"

	var parsedTransDate time.Time
	if len(request) > 0 {
		parsedTransDate, err = time.Parse("2006-01-02", request[0].TransDate)
		if err != nil {
			return nil, err
		}
	}

	var Inserted_Ids []domain.InsertedIds

	copycount, err := ur.Db.CopyFrom(
		ctx,
		pgx.Identifier{"pao", "transfer_entry"},
		[]string{"pao_code", "ddo_code", "hoa", "transfer_amount", "transfer_type", "created_by", "created_date", "te_source_office_type", "transfer_entry_id", "remarks", "verification_status", "trans_date"},
		pgx.CopyFromSlice(len(request), func(i int) ([]interface{}, error) {
			row := []interface{}{
				request[i].PaoCode,
				request[i].DdoCode,
				request[i].Hoa,
				request[i].TransferAmount,
				request[i].TransferType,
				request[i].CreatedBy,
				createdDate,
				request[i].TeSourceOfficeType,
				request[i].PaoCode.String + createdTimeString,
				request[i].Remarks,
				verification_status,
				parsedTransDate,
			}
			Inserted_Ids = append(Inserted_Ids, domain.InsertedIds{
				TransferEntryId: request[i].PaoCode.String + createdTimeString,
			})
			return row, nil
		}),
	)

	log.Debug(gctx, "Copy Count", copycount)

	if err != nil {
		log.Debug(gctx, "Error inserting hoas:", err)
		return nil, err
	}
	return Inserted_Ids, nil
}

func (ur *TransferEntryRepository) TransferentryCreationRepo(gctx *gin.Context, request []domain.TransferEntryRequest) ([]domain.InsertedIds, error) {
	ctx, cancel := context.WithTimeout(gctx.Request.Context(), ur.Cfg.GetDuration("db.QueryTimeoutMed"))
	defer cancel()
	log.Debug(gctx, "Came inside PostPfmsverifiedRepo")

	createdDate := time.Now()
	createdTime := time.Now()
	createdTimeString := createdTime.Format("20060102150405")
	var verification_status = "created"

	var parsedTransDate time.Time
	if len(request) > 0 {
		var err error
		parsedTransDate, err = time.Parse("2006-01-02", request[0].TransDate)
		if err != nil {
			return nil, err
		}
	}

	var Inserted_Ids []domain.InsertedIds

	copycount, err := ur.Db.CopyFrom(
		ctx,
		pgx.Identifier{"pao", "transfer_entry"},
		[]string{"pao_code", "ddo_code", "hoa", "transfer_amount", "transfer_type", "created_by", "created_date", "te_source_office_type", "transfer_entry_id", "remarks", "verification_status", "trans_date"},
		pgx.CopyFromSlice(len(request), func(i int) ([]interface{}, error) {
			row := []interface{}{
				request[i].PaoCode,
				request[i].DdoCode,
				request[i].Hoa,
				request[i].TransferAmount,
				request[i].TransferType,
				request[i].CreatedBy,
				createdDate,
				request[i].TeSourceOfficeType,
				request[i].PaoCode.String + createdTimeString,
				request[i].Remarks,
				verification_status,
				parsedTransDate,
			}
			Inserted_Ids = append(Inserted_Ids, domain.InsertedIds{
				TransferEntryId: request[i].PaoCode.String + createdTimeString,
			})
			return row, nil
		}),
	)

	log.Debug(gctx, "Copy Count", copycount)

	if err != nil {
		log.Debug(gctx, "Error inserting hoas:", err)
		return nil, err
	}
	return Inserted_Ids, nil
}

func (ur *TransferEntryRepository) TransferentryReportRepo01042026(gctx *gin.Context, req domain.TransferEntryReportRequest, reqMetadata port.MetaDataRequest) ([]domain.TransferEntryReport, error) {
	ctx, cancel := context.WithTimeout(gctx.Request.Context(), ur.Cfg.GetDuration("db.QueryTimeoutMed"))
	defer cancel()
	log.Debug(gctx, "Came inside PostPfmsverifiedRepo")

	// Initialize SelectBuilder
	builder := sq.Select(
		"a.pao_code",
		"a.hoa",
		"b.hoa_description",
		"a.transfer_amount",
		"a.transfer_type",
		"a.created_by",
		"a.created_date",
		"a.ddo_code",
		"c.ddo_name",
		"a.transfer_entry_id",
		"a.te_source_office_type",
		"a.remarks",
		"a.verified_by",
		"a.verified_date",
		"a.verification_status",
		"a.pfms_unique_id",
		"a.approver_remarks",
		"a.budget_id",
		"a.pfms_submission_flag",
		"a.pfms_error_description",
		"a.h_pfms_generation_flag",
		"a.te_number",
	).
		From("pao.transfer_entry a").
		LeftJoin(`(
			SELECT DISTINCT ON (hoa) hoa, hoa_description
			FROM pao.kafka_account_codes_master
			ORDER BY hoa, created_date DESC
		) b ON b.hoa = a.hoa`).
		LeftJoin("pao.ddo_master c ON c.ddo_code = a.ddo_code").
		PlaceholderFormat(sq.Dollar)

	// Add conditions
	if req.PaoCode != "" {
		builder = builder.Where(sq.Eq{"a.pao_code": req.PaoCode})
	}
	if !req.FromDateCreated.IsZero() {
		builder = builder.Where(sq.GtOrEq{"a.created_date": req.FromDateCreated})
	}
	if !req.ToDateCreated.IsZero() {
		builder = builder.Where(sq.LtOrEq{"a.created_date": req.ToDateCreated})
	}
	if !req.FromDateVerified.IsZero() {
		builder = builder.Where(sq.GtOrEq{"a.verified_date": req.FromDateVerified})
	}
	if !req.ToDateVerified.IsZero() {
		builder = builder.Where(sq.LtOrEq{"a.verified_date": req.ToDateVerified})
	}
	if req.PfmsSubmissionFlag != "" {
		builder = builder.Where(sq.Eq{"a.pfms_submission_flag": req.PfmsSubmissionFlag})
	}
	if req.HPfmsGenerationFlag != nil {
		builder = builder.Where(sq.Eq{"a.h_pfms_generation_flag": *req.HPfmsGenerationFlag})
	}
	if req.VerificationStatus != "" {
		builder = builder.Where(sq.Eq{"a.verification_status": req.VerificationStatus})
	}

	// Add pagination
	builder = builder.OrderBy("a.verified_date DESC").
		Offset(uint64(reqMetadata.Skip)).
		Limit(uint64(reqMetadata.Limit))

	return dblib.SelectRows(ctx, ur.Db, builder, pgx.RowToStructByNameLax[domain.TransferEntryReport])
}

func (ur *TransferEntryRepository) TransferentryReportRepo07052026(gctx *gin.Context, req domain.TransferEntryReportRequest, reqMetadata port.MetaDataRequest) ([]domain.TransferEntryReport, error) {
	ctx, cancel := context.WithTimeout(gctx.Request.Context(), ur.Cfg.GetDuration("db.QueryTimeoutMed"))
	defer cancel()
	log.Debug(gctx, "Came inside PostPfmsverifiedRepo")

	builder := sq.Select(
		"a.pao_code",
		"a.hoa",
		"b.hoa_description",
		"a.transfer_amount",
		"a.transfer_type",
		"a.created_by",
		"a.created_date",
		"a.ddo_code",
		"c.ddo_name",
		"a.transfer_entry_id",
		"a.te_source_office_type",
		"a.remarks",
		"a.verified_by",
		"a.verified_date",
		"a.verification_status",
		"a.pfms_unique_id",
		"a.approver_remarks",
		"a.budget_id",
		"a.pfms_submission_flag",
		"a.pfms_error_description",
		"a.h_pfms_generation_flag",
		"a.te_number",
		// "k.trans_date", (changed on 21-04-2026)
		"COALESCE(a.trans_date, k.trans_date) AS trans_date", // ← only change
	).
		From("pao.transfer_entry a").
		LeftJoin(`(
			SELECT DISTINCT ON (hoa) hoa, hoa_description
			FROM pao.kafka_account_codes_master
			ORDER BY hoa, created_date DESC
		) b ON b.hoa = a.hoa`).
		LeftJoin("pao.ddo_master c ON c.ddo_code = a.ddo_code").
		LeftJoin(`(
			SELECT DISTINCT ON (trans_id) trans_id, trans_date
			FROM pao.kafka_transfer_entry
			ORDER BY trans_id
		) k ON k.trans_id = a.transfer_entry_id`).
		PlaceholderFormat(sq.Dollar)

	if req.PaoCode != "" {
		builder = builder.Where(sq.Eq{"a.pao_code": req.PaoCode})
	}
	if !req.FromDateCreated.IsZero() {
		builder = builder.Where(sq.GtOrEq{"a.created_date": req.FromDateCreated})
	}
	// if !req.ToDateCreated.IsZero() {
	// 	builder = builder.Where(sq.LtOrEq{"a.created_date": req.ToDateCreated})
	// }
	if !req.ToDateCreated.IsZero() {
		builder = builder.Where(sq.Lt{"a.created_date": req.ToDateCreated}) // LtOrEq → Lt
	}

	if !req.FromDateVerified.IsZero() {
		builder = builder.Where(sq.GtOrEq{"a.verified_date": req.FromDateVerified})
	}
	if !req.ToDateVerified.IsZero() {
		builder = builder.Where(sq.LtOrEq{"a.verified_date": req.ToDateVerified})
	}
	if req.PfmsSubmissionFlag != "" {
		builder = builder.Where(sq.Eq{"a.pfms_submission_flag": req.PfmsSubmissionFlag})
	}
	if req.HPfmsGenerationFlag != nil {
		builder = builder.Where(sq.Eq{"a.h_pfms_generation_flag": *req.HPfmsGenerationFlag})
	}
	if req.VerificationStatus != "" {
		builder = builder.Where(sq.Eq{"a.verification_status": req.VerificationStatus})
	}

	builder = builder.OrderBy("a.verified_date DESC").
		Offset(uint64(reqMetadata.Skip)).
		Limit(uint64(reqMetadata.Limit))

	return dblib.SelectRows(ctx, ur.Db, builder, pgx.RowToStructByNameLax[domain.TransferEntryReport])
}

func (ur *TransferEntryRepository) TransferentryReportRepo(gctx *gin.Context, req domain.TransferEntryReportRequest, reqMetadata port.MetaDataRequest) ([]domain.TransferEntryReport, error) {
	ctx, cancel := context.WithTimeout(gctx.Request.Context(), ur.Cfg.GetDuration("db.QueryTimeoutMed"))
	defer cancel()
	log.Debug(gctx, "Came inside PostPfmsverifiedRepo")

	builder := sq.Select(
		"a.pao_code",
		"a.hoa",
		"b.hoa_description",
		"a.transfer_amount",
		"a.transfer_type",
		"a.created_by",
		"a.created_date",
		"a.ddo_code",
		"c.ddo_name",
		"a.transfer_entry_id",
		"a.te_source_office_type",
		"a.remarks",
		"a.verified_by",
		"a.verified_date",
		"a.verification_status",
		"a.pfms_unique_id",
		"a.approver_remarks",
		"a.budget_id",
		"a.pfms_submission_flag",
		"a.pfms_error_description",
		"a.h_pfms_generation_flag",
		"a.te_number",
		// "k.trans_date", (changed on 21-04-2026)
		"COALESCE(a.trans_date, k.trans_date) AS trans_date", // ← only change
		"a.account_code",              // ← added
		"ac.account_code_description", // ← added
	).
		From("pao.transfer_entry a").
		LeftJoin(`(
			SELECT DISTINCT ON (hoa) hoa, hoa_description
			FROM pao.kafka_account_codes_master
			ORDER BY hoa, created_date DESC
		) b ON b.hoa = a.hoa`).
		LeftJoin("pao.ddo_master c ON c.ddo_code = a.ddo_code").
		LeftJoin(`(
			SELECT DISTINCT ON (trans_id) trans_id, trans_date
			FROM pao.kafka_transfer_entry
			ORDER BY trans_id
		) k ON k.trans_id = a.transfer_entry_id`).
		LeftJoin(`(
    SELECT DISTINCT ON (account_code) account_code, account_code_description
    FROM pao.kafka_account_codes_master
    ORDER BY account_code, created_date DESC
) ac ON ac.account_code = a.account_code`). // a = transfer_entry
		PlaceholderFormat(sq.Dollar)

	if req.PaoCode != "" {
		builder = builder.Where(sq.Eq{"a.pao_code": req.PaoCode})
	}
	if !req.FromDateCreated.IsZero() {
		builder = builder.Where(sq.GtOrEq{"a.created_date": req.FromDateCreated})
	}
	// if !req.ToDateCreated.IsZero() {
	// 	builder = builder.Where(sq.LtOrEq{"a.created_date": req.ToDateCreated})
	// }
	if !req.ToDateCreated.IsZero() {
		builder = builder.Where(sq.Lt{"a.created_date": req.ToDateCreated}) // LtOrEq → Lt
	}

	if !req.FromDateVerified.IsZero() {
		builder = builder.Where(sq.GtOrEq{"a.verified_date": req.FromDateVerified})
	}
	if !req.ToDateVerified.IsZero() {
		builder = builder.Where(sq.LtOrEq{"a.verified_date": req.ToDateVerified})
	}
	if req.PfmsSubmissionFlag != "" {
		builder = builder.Where(sq.Eq{"a.pfms_submission_flag": req.PfmsSubmissionFlag})
	}
	if req.HPfmsGenerationFlag != nil {
		builder = builder.Where(sq.Eq{"a.h_pfms_generation_flag": *req.HPfmsGenerationFlag})
	}
	if req.VerificationStatus != "" {
		builder = builder.Where(sq.Eq{"a.verification_status": req.VerificationStatus})
	}

	builder = builder.OrderBy("a.verified_date DESC").
		Offset(uint64(reqMetadata.Skip)).
		Limit(uint64(reqMetadata.Limit))

	return dblib.SelectRows(ctx, ur.Db, builder, pgx.RowToStructByNameLax[domain.TransferEntryReport])
}

func (ur *TransferEntryRepository) TransferentryRejectRepo(gctx *gin.Context, request *domain.TransferEntryRejectRequest) error {

	ctx, cancel := context.WithTimeout(gctx.Request.Context(), ur.Cfg.GetDuration("db.QueryTimeoutMed"))
	defer cancel()
	currentTime := time.Now()

	updateBuilder := dblib.Psql.Update("pao.transfer_entry").
		Set("verification_status", sq.Expr("$1", request.VerificationStatus)).
		Set("verified_by", sq.Expr("$2", request.VerifiedBy)).
		Set("verified_date", sq.Expr("$3", currentTime)).
		Set("approver_remarks", sq.Expr("$4", request.ApproverRemarks)).
		Where("transfer_entry_id = $5", request.TransferEntryId)

	sql, args, err := updateBuilder.ToSql()
	if err != nil {
		return err
	}
	_, err = ur.Db.Exec(ctx, sql, args...)
	if err != nil {
		return err
	}

	return nil

}
func (ur *TransferEntryRepository) TransferentryVerifyRepo(gctx *gin.Context, request []domain.TransferEntryVerifyRequest) error {

	ctx, cancel := context.WithTimeout(gctx.Request.Context(), ur.Cfg.GetDuration("db.QueryTimeoutMed"))
	defer cancel()
	currentTime := time.Now()
	formattedTime := currentTime.Format("2006-01-02 15:04:05")

	batch := &pgx.Batch{}

	for _, sub := range request {

		updateBuilder := dblib.Psql.Update("pao.transfer_entry").
			Set("verification_status", sq.Expr("$1", sub.VerificationStatus)).
			Set("verified_by", sq.Expr("$2", sub.VerifiedBy)).
			Set("verified_date", sq.Expr("$3", formattedTime)).
			Set("approver_remarks", sq.Expr("$4", sub.ApproverRemarks)).
			Where("transfer_entry_id = $5", sub.TransferEntryId)
		err := dblib.QueueExecRow(batch, updateBuilder)
		if err != nil {
			return err
		}

	}

	errors := ur.Db.SendBatch(ctx, batch).Close()
	if errors != nil {
		log.Debug(gctx, "Error results:", errors)
		return errors
	}
	return nil
}

func (ur *TransferEntryRepository) DdoTransferentryReportRepo(gctx *gin.Context, request domain.DdoTeRequest, reqMetadata port.MetaDataRequest) ([]domain.DdoTeRequestReply, error) {
	ctx, cancel := context.WithTimeout(gctx.Request.Context(), ur.Cfg.GetDuration("db.QueryTimeoutMed"))
	defer cancel()
	log.Debug(gctx, "Came inside PostPfmsverifiedRepo")
	fromDate, err := time.Parse("2006-01-02", request.FromDate)
	if err != nil {
		return nil, err
	}

	toDate, err := time.Parse("2006-01-02", request.ToDate)
	if err != nil {
		return nil, err
	}
	var transentry domain.DdoTeRequestReply
	columns := dblib.GenerateColumnsFromStruct(transentry, "select")
	query := dblib.Psql.Select(columns...).
		FromSelect(
			dblib.Psql.Select("a.ddo_code", "b.ddo_name", "a.trans_id", "a.account_code", "a.transfer_amount", "a.transfer_type", "a.created_by", "a.created_date", "a.status", "c.hoa", "c.hoa_description", "coalesce (d.approver_remarks, '') as approver_remarks", "coalesce (d.pfms_unique_id, '') as pfms_unique_id", "coalesce (d.pfms_submission_flag, '') as pfms_submission_flag", "coalesce (d.pfms_error_description, '') as pfms_error_description", "coalesce (d.te_number, '') as te_number", "coalesce (a.remarks_by_creator, '') as remarks_by_creator").
				From("pao.kafka_transfer_entry a").
				LeftJoin("pao.ddo_master b on b.ddo_code = a.ddo_code").
				LeftJoin("pao.kafka_account_codes_master c on a.account_code = c.account_code").
				LeftJoin("pao.transfer_entry d on a.trans_id = d.transfer_entry_id").
				Where(sq.And{
					sq.GtOrEq{"DATE(a.created_date)": fromDate},
					sq.LtOrEq{"DATE(a.created_date)": toDate},
					sq.Eq{"a.ddo_code": request.DdoCode},
					sq.Eq{"LOWER(a.status)": request.Status},
				}), "t").
		OrderBy("ddo_code").Offset(uint64(reqMetadata.Skip * reqMetadata.Limit)).
		Limit(uint64(reqMetadata.Limit))
	return dblib.SelectRows(ctx, ur.Db, query, pgx.RowToStructByNameLax[domain.DdoTeRequestReply])
}

func (ur *TransferEntryRepository) PaoSubTransferentryReportRepo01042026(gctx *gin.Context, request domain.PaoSubTeRequest, reqMetadata port.MetaDataRequest) ([]domain.PaoSubTeRequestReply, error) {
	ctx, cancel := context.WithTimeout(gctx.Request.Context(), ur.Cfg.GetDuration("db.QueryTimeoutMed"))
	defer cancel()
	log.Debug(gctx, "Came inside PostPfmsverifiedRepo")
	var transentry domain.PaoSubTeRequestReply

	if request.Type == "1" {
		fromDate, err := time.Parse("2006-01-02", request.FromDate)
		if err != nil {
			return nil, err
		}

		toDate, err := time.Parse("2006-01-02", request.ToDate)
		if err != nil {
			return nil, err
		}
		columns := dblib.GenerateColumnsFromStruct(transentry, "select")
		query := dblib.Psql.Select(columns...).
			Distinct().
			FromSelect(
				sq.Select("a.pao_code", "b.ddo_code", "a.ddo_name", "b.trans_id", "b.created_by", "DATE(b.created_date::timestamp) AS created_date", "b.approved_by", "DATE(b.approved_date::timestamp) AS approved_date", "b.remarks", "b.status", "coalesce (d.approver_remarks, '') as approver_remarks", "coalesce (d.pfms_unique_id, '') as pfms_unique_id", "coalesce (d.pfms_submission_flag, '') as pfms_submission_flag", "coalesce (d.pfms_error_description, '') as pfms_error_description", "coalesce (d.te_number, '') as te_number", "coalesce (b.remarks_by_creator, '') as remarks_by_creator").
					From("pao.ddo_master a").
					LeftJoin("pao.kafka_transfer_entry b on a.ddo_code = b.ddo_code").
					LeftJoin("pao.kafka_account_codes_master c on b.account_code = c.account_code").
					LeftJoin("pao.transfer_entry d on b.trans_id = d.transfer_entry_id").
					Where(sq.And{
						sq.Eq{"a.pao_code": request.PaoCode},
						sq.GtOrEq{"DATE(b.created_date)": fromDate},
						sq.LtOrEq{"DATE(b.created_date)": toDate},
						sq.Eq{"LOWER(b.status)": request.Status},
					}).
					GroupBy("a.pao_code", "b.ddo_code", "a.ddo_name", "b.trans_id", "b.created_by", "b.created_date", "b.approved_by", "b.approved_date", "b.remarks", "b.status", "d.approver_remarks", "d.pfms_unique_id", "d.pfms_submission_flag", "d.pfms_error_description", "d.te_number", "b.remarks_by_creator"),
				"derived_table").
			Offset(uint64(reqMetadata.Skip * reqMetadata.Limit)).
			Limit(uint64(reqMetadata.Limit))
		return dblib.SelectRows(ctx, ur.Db, query, pgx.RowToStructByNameLax[domain.PaoSubTeRequestReply])
	}
	if request.Type == "2" {
		columns := dblib.GenerateColumnsFromStruct(transentry, "select")
		query := dblib.Psql.Select(columns...).
			Distinct().
			FromSelect(
				sq.Select("a.pao_code", "b.ddo_code", "a.ddo_name", "b.trans_id", "b.created_by", "DATE(b.created_date::timestamp) AS created_date", "b.approved_by", "DATE(b.approved_date::timestamp) AS approved_date", "b.remarks", "b.status", "coalesce (d.approver_remarks, '') as approver_remarks", "coalesce (d.pfms_unique_id, '') as pfms_unique_id", "coalesce (d.pfms_submission_flag, '') as pfms_submission_flag", "coalesce (d.pfms_error_description, '') as pfms_error_description", "coalesce (d.te_number, '') as te_number", "coalesce (b.remarks_by_creator, '') as remarks_by_creator").
					From("pao.ddo_master a").
					LeftJoin("pao.kafka_transfer_entry b on a.ddo_code = b.ddo_code").
					LeftJoin("pao.transfer_entry d on b.trans_id = d.transfer_entry_id").
					// LeftJoin("pao.account_hoa_mapping c on b.account_code = c.account_code").
					Where(sq.And{
						sq.Eq{"a.pao_code": request.PaoCode},
						sq.Eq{"LOWER(b.status)": request.Status},
					}).
					GroupBy("a.pao_code", "b.ddo_code", "a.ddo_name", "b.trans_id", "b.created_by", "b.created_date", "b.approved_by", "b.approved_date", "b.remarks", "b.status", "d.approver_remarks", "d.pfms_unique_id", "d.pfms_submission_flag", "d.pfms_error_description", "d.te_number", "b.remarks_by_creator"),
				"derived_table")

		return dblib.SelectRows(ctx, ur.Db, query, pgx.RowToStructByNameLax[domain.PaoSubTeRequestReply])
	}
	var nullreturn []domain.PaoSubTeRequestReply
	return nullreturn, nil
}

func (ur *TransferEntryRepository) PaoSubTransferentryReportRepo(gctx *gin.Context, request domain.PaoSubTeRequest, reqMetadata port.MetaDataRequest) ([]domain.PaoSubTeRequestReply, error) {
	ctx, cancel := context.WithTimeout(gctx.Request.Context(), ur.Cfg.GetDuration("db.QueryTimeoutMed"))
	defer cancel()
	log.Debug(gctx, "Came inside PostPfmsverifiedRepo")
	var transentry domain.PaoSubTeRequestReply

	if request.Type == "1" {
		fromDate, err := time.Parse("2006-01-02", request.FromDate)
		if err != nil {
			return nil, err
		}

		toDate, err := time.Parse("2006-01-02", request.ToDate)
		if err != nil {
			return nil, err
		}
		columns := dblib.GenerateColumnsFromStruct(transentry, "select")
		query := dblib.Psql.Select(columns...).
			Distinct().
			FromSelect(
				sq.Select("a.pao_code", "b.ddo_code", "a.ddo_name", "b.trans_id", "b.created_by", "DATE(b.created_date::timestamp) AS created_date", "b.approved_by", "DATE(b.approved_date::timestamp) AS approved_date", "DATE(b.trans_date::timestamp) AS trans_date", "b.remarks", "b.status", "coalesce (d.approver_remarks, '') as approver_remarks", "coalesce (d.pfms_unique_id, '') as pfms_unique_id", "coalesce (d.pfms_submission_flag, '') as pfms_submission_flag", "coalesce (d.pfms_error_description, '') as pfms_error_description", "coalesce (d.te_number, '') as te_number", "coalesce (b.remarks_by_creator, '') as remarks_by_creator", "coalesce (b.workflow_id, '') as workflow_id").
					From("pao.ddo_master a").
					LeftJoin("pao.kafka_transfer_entry b on a.ddo_code = b.ddo_code").
					LeftJoin("pao.kafka_account_codes_master c on b.account_code = c.account_code").
					LeftJoin("pao.transfer_entry d on b.trans_id = d.transfer_entry_id").
					Where(sq.And{
						sq.Eq{"a.pao_code": request.PaoCode},
						sq.GtOrEq{"DATE(b.created_date)": fromDate},
						sq.LtOrEq{"DATE(b.created_date)": toDate},
						sq.Eq{"LOWER(b.status)": request.Status},
					}).
					GroupBy("a.pao_code", "b.ddo_code", "a.ddo_name", "b.trans_id", "b.created_by", "b.created_date", "b.approved_by", "b.approved_date", "b.trans_date", "b.remarks", "b.status", "d.approver_remarks", "d.pfms_unique_id", "d.pfms_submission_flag", "d.pfms_error_description", "d.te_number", "b.remarks_by_creator", "b.workflow_id"),
				"derived_table").
			Offset(uint64(reqMetadata.Skip * reqMetadata.Limit)).
			Limit(uint64(reqMetadata.Limit))
		return dblib.SelectRows(ctx, ur.Db, query, pgx.RowToStructByNameLax[domain.PaoSubTeRequestReply])
	}
	if request.Type == "2" {
		columns := dblib.GenerateColumnsFromStruct(transentry, "select")
		query := dblib.Psql.Select(columns...).
			Distinct().
			FromSelect(
				sq.Select("a.pao_code", "b.ddo_code", "a.ddo_name", "b.trans_id", "b.created_by", "DATE(b.created_date::timestamp) AS created_date", "b.approved_by", "DATE(b.approved_date::timestamp) AS approved_date", "DATE(b.trans_date::timestamp) AS trans_date", "b.remarks", "b.status", "coalesce (d.approver_remarks, '') as approver_remarks", "coalesce (d.pfms_unique_id, '') as pfms_unique_id", "coalesce (d.pfms_submission_flag, '') as pfms_submission_flag", "coalesce (d.pfms_error_description, '') as pfms_error_description", "coalesce (d.te_number, '') as te_number", "coalesce (b.remarks_by_creator, '') as remarks_by_creator", "coalesce (b.workflow_id, '') as workflow_id").
					From("pao.ddo_master a").
					LeftJoin("pao.kafka_transfer_entry b on a.ddo_code = b.ddo_code").
					LeftJoin("pao.transfer_entry d on b.trans_id = d.transfer_entry_id").
					// LeftJoin("pao.account_hoa_mapping c on b.account_code = c.account_code").
					Where(sq.And{
						sq.Eq{"a.pao_code": request.PaoCode},
						sq.Eq{"LOWER(b.status)": request.Status},
					}).
					GroupBy("a.pao_code", "b.ddo_code", "a.ddo_name", "b.trans_id", "b.created_by", "b.created_date", "b.approved_by", "b.approved_date", "b.trans_date", "b.remarks", "b.status", "d.approver_remarks", "d.pfms_unique_id", "d.pfms_submission_flag", "d.pfms_error_description", "d.te_number", "b.remarks_by_creator", "b.workflow_id"),
				"derived_table")

		return dblib.SelectRows(ctx, ur.Db, query, pgx.RowToStructByNameLax[domain.PaoSubTeRequestReply])
	}
	var nullreturn []domain.PaoSubTeRequestReply
	return nullreturn, nil
}

func (ur *TransferEntryRepository) PaoSubTransferentryDetailRepo(gctx *gin.Context, request domain.PaoSubTeDetailRequest) ([]domain.PaoSubTeRequestDetailReply, error) {
	ctx, cancel := context.WithTimeout(gctx.Request.Context(), ur.Cfg.GetDuration("db.QueryTimeoutMed"))
	defer cancel()
	log.Debug(gctx, "Came inside PostPfmsverifiedRepo")
	var transentry domain.PaoSubTeRequestDetailReply

	columns := dblib.GenerateColumnsFromStruct(transentry, "select")
	query := dblib.Psql.Select(columns...).
		Distinct().
		FromSelect(
			sq.Select("a.pao_code", "b.ddo_code", "a.ddo_name", "b.trans_id", "b.account_code", "b.transfer_amount", "b.transfer_type", "b.created_by", "DATE(b.created_date::timestamp) AS created_date", "b.status", "c.hoa", "hoa_description", "b.remarks_by_creator", "DATE(b.trans_date::timestamp) AS trans_date").
				From("pao.ddo_master a").
				LeftJoin("pao.kafka_transfer_entry b on a.ddo_code = b.ddo_code").
				LeftJoin("pao.kafka_account_codes_master c on b.account_code = c.account_code").
				Where(
					sq.Eq{"b.trans_id": request.TransId},
				).
				GroupBy("a.pao_code", "b.ddo_code", "a.ddo_name", "b.trans_id", "b.account_code", "b.transfer_amount", "b.transfer_type", "b.created_by", "b.created_date", "b.status,c.hoa", "hoa_description", "b.remarks_by_creator", "b.trans_date"),
			"derived_table")

	return dblib.SelectRows(ctx, ur.Db, query, pgx.RowToStructByNameLax[domain.PaoSubTeRequestDetailReply])
}

func (ur *TransferEntryRepository) SubVerifiedTePostingRepo(gctx *gin.Context, request domain.SubTeVerifiedBullk) error {
	ctx, cancel := context.WithTimeout(gctx.Request.Context(), ur.Cfg.GetDuration("db.QueryTimeoutMed"))
	defer cancel()
	log.Debug(gctx, "Came inside PostPfmsverifiedRepo")

	transDate := time.Now().Format("2006-01-02")
	approvedDate, err := time.Parse("2006-01-02", transDate)
	if err != nil {
		return err
	}
	var officetype = "DDO"

	batch := &pgx.Batch{}

	var verification_status = "verified"

	// Batch inserts for transfer_entry
	for _, t := range request.SubTes {
		insertBuilder := dblib.Psql.Insert("pao.transfer_entry").
			Columns("pao_code", "ddo_code", "transfer_entry_id", "hoa", "transfer_amount", "transfer_type", "created_by", "created_date", "te_source_office_type", "verification_status", "verified_by", "verified_date", "approver_remarks").
			Values(t.PaoCode, t.DdoCode, t.TransId, t.Hoa, t.TransferAmount, t.TransferType, t.CreatedBy, t.CreatedDate, officetype, verification_status, t.ApprovedBy, approvedDate, t.ApproverRemarks)
		err := dblib.QueueExecRow(batch, insertBuilder)
		if err != nil {
			return err
		}

	}

	// Execute the batch and check for errors
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

func (ur *TransferEntryRepository) GetPfmsteRepo(gctx *gin.Context, cbds []domain.TeData) ([]domain.TransferEntryAccountingDetail, error) { // *domain.Order,

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
		From(fmt.Sprintf("pao.generate_pfms_te_json('[%s]')", cbdsString))
	return dblib.SelectRows(ctx, ur.Db, query, pgx.RowToStructByNameLax[domain.TransferEntryAccountingDetail])

}
func (ur *TransferEntryRepository) GetOfficeIdRepo(gctx *gin.Context, request string) (*domain.OfficeIdReply, bool, error) {
	ctx, cancel := context.WithTimeout(gctx.Request.Context(), ur.Cfg.GetDuration("db.QueryTimeoutMed"))
	defer cancel()
	log.Debug(gctx, "Came inside GetOfficeIdRepo")
	query := dblib.Psql.Select("ddo_office_id").
		From("pao.ddo_master").
		Where(sq.Eq{"ddo_code": request}).
		Limit(1)
	return dblib.SelectOneOK(ctx, ur.Db, query, pgx.RowToAddrOfStructByNameLax[domain.OfficeIdReply])
}

// This repo was designed to fetch pao code as ddo code as such if ddo master table wont have pao code as ddo code. but later changes were implemented on 17-06-2026
func (ur *TransferEntryRepository) GetOfficeIdforpaoRepo(gctx *gin.Context, request string) (*domain.OfficeIdReply, bool, error) {
	ctx, cancel := context.WithTimeout(gctx.Request.Context(), ur.Cfg.GetDuration("db.QueryTimeoutMed"))
	defer cancel()
	log.Debug(gctx, "Came inside GetOfficeIdRepo")

	// Primary: try ddo_code → ddo_office_id
	query := dblib.Psql.Select("ddo_office_id").
		From("pao.ddo_master").
		Where(sq.Eq{"ddo_code": request}).
		Limit(1)

	result, found, err := dblib.SelectOneOK(ctx, ur.Db, query, pgx.RowToAddrOfStructByNameLax[domain.OfficeIdReply])
	if err != nil {
		return nil, false, err
	}
	if found {
		return result, true, nil
	}

	// Fallback: try pao_code → pao_office_id aliased as ddo_office_id
	// Alias ensures OfficeIdReply struct maps correctly without any struct change
	log.Debug(gctx, "ddo_code not found, falling back to pao_code for request: %s", request)
	fallbackQuery := dblib.Psql.Select("pao_office_id AS ddo_office_id").
		From("pao.ddo_master").
		Where(sq.Eq{"pao_code": request}).
		Limit(1)

	return dblib.SelectOneOK(ctx, ur.Db, fallbackQuery, pgx.RowToAddrOfStructByNameLax[domain.OfficeIdReply])
}

func (ur *TransferEntryRepository) TransferentryDirectCreationRepo22042026(gctx *gin.Context, request []domain.TransferEntryDirectRequest) ([]domain.InsertedIds, error) {
	ctx, cancel := context.WithTimeout(gctx.Request.Context(), ur.Cfg.GetDuration("db.QueryTimeoutMed"))
	defer cancel()
	log.Debug(gctx, "Came inside PostPfmsverifiedRepo")
	var Inserted_Ids []domain.InsertedIds

	copycount, err := ur.Db.CopyFrom(
		ctx,
		pgx.Identifier{"pao", "transfer_entry"},
		[]string{"pao_code", "ddo_code", "hoa", "transfer_amount", "transfer_type", "created_by", "created_date", "te_source_office_type", "transfer_entry_id", "remarks", "verification_status", "h_pfms_generation_flag"},
		pgx.CopyFromSlice(len(request), func(i int) ([]interface{}, error) {
			row := []interface{}{
				request[i].PaoCode,
				request[i].DdoCode,
				request[i].Hoa,
				request[i].TransferAmount,
				request[i].TransferType,
				request[i].CreatedBy,
				request[i].CreatedDate,
				request[i].TeSourceOfficeType,
				request[i].TransferEntryId,
				request[i].Remarks,
				request[i].VerificationStatus,
				request[i].HPfmsGenerationFlag,
			}
			// Append the row to the slice
			Inserted_Ids = append(Inserted_Ids, domain.InsertedIds{

				TransferEntryId: request[i].TransferEntryId,
			})
			return row, nil
		}),
	)

	log.Debug(gctx, "Copy Count", copycount)

	if err != nil {
		log.Debug(gctx, "Error inserting hoas:", err)
		return nil, err
	}
	return Inserted_Ids, nil // Return nil if everything executed successfully

}

func (ur *TransferEntryRepository) TransferentryDirectCreationRepo(gctx *gin.Context, request []domain.TransferEntryDirectRequest) ([]domain.InsertedIds, error) {
	ctx, cancel := context.WithTimeout(gctx.Request.Context(), ur.Cfg.GetDuration("db.QueryTimeoutMed"))
	defer cancel()
	log.Debug(gctx, "Came inside PostPfmsverifiedRepo")
	var Inserted_Ids []domain.InsertedIds

	copycount, err := ur.Db.CopyFrom(
		ctx,
		pgx.Identifier{"pao", "transfer_entry"},
		[]string{"pao_code", "ddo_code", "hoa", "transfer_amount", "transfer_type", "created_by", "created_date", "trans_date", "te_source_office_type", "transfer_entry_id", "remarks", "verification_status", "h_pfms_generation_flag"},
		pgx.CopyFromSlice(len(request), func(i int) ([]interface{}, error) {
			row := []interface{}{
				request[i].PaoCode,
				request[i].DdoCode,
				request[i].Hoa,
				request[i].TransferAmount,
				request[i].TransferType,
				request[i].CreatedBy,
				request[i].CreatedDate,
				request[i].TransDate, // ← added
				request[i].TeSourceOfficeType,
				request[i].TransferEntryId,
				request[i].Remarks,
				request[i].VerificationStatus,
				request[i].HPfmsGenerationFlag,
			}
			Inserted_Ids = append(Inserted_Ids, domain.InsertedIds{
				TransferEntryId: request[i].TransferEntryId,
			})
			return row, nil
		}),
	)

	log.Debug(gctx, "Copy Count", copycount)

	if err != nil {
		log.Debug(gctx, "Error inserting hoas:", err)
		return nil, err
	}
	return Inserted_Ids, nil
}

//Temporal code for work flow

type OperationDetails struct {
	EndpointName  string
	ServiceName   string
	OperationName string
	Options       workflow.NexusOperationOptions
}

func (ur *TransferEntryRepository) TransferentryverificationWorkflow(ctx workflow.Context, BudgetInput []contracts.BudgetTransferEntryVerificationInput, SubaccountsInput contracts.SubAccountTransferEntryVerificationInput, TEInput domain.SubTeVerifiedBullk) (string, error) {
	var exec workflow.NexusOperationExecution
	var ReversalInput []contracts.BudgetConsumptionReversalInput
	if len(BudgetInput) != 0 {
		opBudgetEndDetails := OperationDetails{
			EndpointName:  contracts.BudgetNexusEndpoint,
			ServiceName:   contracts.TransferEntryVerificationService,
			OperationName: contracts.BudgetTransferEntryVerificationOperation,
			Options: workflow.NexusOperationOptions{
				ScheduleToCloseTimeout: 60 * time.Second,
			},
		}
		// Create a Nexus client instance.
		// (Replace "endpointName" and "HelloServiceName" with your actual values.)
		nexusClient := workflow.NewNexusClient(opBudgetEndDetails.EndpointName, opBudgetEndDetails.ServiceName)

		// Execute the nexus operation asynchronously.
		fut := nexusClient.ExecuteOperation(ctx, opBudgetEndDetails.OperationName, BudgetInput, workflow.NexusOperationOptions{})

		// Wait until the nexus operation is confirmed as started.

		if err := fut.GetNexusOperationExecution().Get(ctx, &exec); err != nil {
			return "Budget Nexus Initiation failed", err
		}

		// Retrieve the result of the nexus operation.
		var BudgetOutput []contracts.BudgetConsumptionResponse
		if err := fut.Get(ctx, &BudgetOutput); err != nil {
			return "Budget Nexus call failed", err
		}
		var insufcount = 0
		for _, t := range BudgetOutput {
			if t.Remark == "Insufficient funds" {
				insufcount++
			}
		}
		if insufcount > 0 {
			return "Insufficient funds", nil
		}

		for _, request := range BudgetOutput {
			requestResponse := contracts.BudgetConsumptionReversalInput{
				BudgetID: request.BudgetID,
			}
			ReversalInput = append(ReversalInput, requestResponse)
		}
	}
	opSubaccountsDetails := OperationDetails{
		EndpointName:  contracts.SubaccountsNexusEndpoint,
		ServiceName:   contracts.TransferEntryVerificationService,
		OperationName: contracts.SubAccountTransferEntryVerificationOperation,
		Options: workflow.NexusOperationOptions{
			ScheduleToCloseTimeout: 60 * time.Second,
		},
	}
	// Create a Nexus client instance.
	// (Replace "endpointName" and "HelloServiceName" with your actual values.)
	nexusClient := workflow.NewNexusClient(opSubaccountsDetails.EndpointName, opSubaccountsDetails.ServiceName)

	// Execute the nexus operation asynchronously.
	fut2 := nexusClient.ExecuteOperation(ctx, opSubaccountsDetails.OperationName, SubaccountsInput, workflow.NexusOperationOptions{})

	// Wait until the nexus operation is confirmed as started.
	if err := fut2.GetNexusOperationExecution().Get(ctx, &exec); err != nil {
		return "Subaccounts Nexus Call Failed", err
	}

	// Retrieve the result of the nexus operation.
	var SubaccountsOutput string
	err := fut2.Get(ctx, &SubaccountsOutput)

	if err != nil {
		if len(ReversalInput) != 0 {
			opBudgetEndDetails := OperationDetails{
				EndpointName:  contracts.BudgetNexusEndpoint,
				ServiceName:   contracts.TransferEntryVerificationService,
				OperationName: contracts.BudgetTransferEntryVerificationReversalOperation,
				Options: workflow.NexusOperationOptions{
					ScheduleToCloseTimeout: 120 * time.Second,
				},
			}
			// Create a Nexus client instance.
			// (Replace "endpointName" and "HelloServiceName" with your actual values.)
			nexusClient := workflow.NewNexusClient(opBudgetEndDetails.EndpointName, opBudgetEndDetails.ServiceName)

			// Execute the nexus operation asynchronously.
			fut3 := nexusClient.ExecuteOperation(ctx, opBudgetEndDetails.OperationName, ReversalInput, workflow.NexusOperationOptions{})

			// Wait until the nexus operation is confirmed as started.
			var exec workflow.NexusOperationExecution
			if err := fut3.GetNexusOperationExecution().Get(ctx, &exec); err != nil {
				return "", err
			}

			// Retrieve the result of the nexus operation.
			var BudgetreversalOutput string
			if err := fut3.Get(ctx, &BudgetreversalOutput); err != nil {
				return "Budget reversal failed", err
			}
			return "Subaccounts updation failed", nil
		}
	}

	ao := workflow.ActivityOptions{
		RetryPolicy: &temporal.RetryPolicy{
			MaximumAttempts:    0, // Retry indefinitely unless it succeeds
			InitialInterval:    time.Second,
			BackoffCoefficient: 1.5,
		},
		StartToCloseTimeout: 30 * time.Second,
	}
	actx := workflow.WithActivityOptions(ctx, ao)

	fut4 := workflow.ExecuteActivity(actx, TransferentryRepoInstance.SubVerifiedTePostingTemporalRepoNew, TEInput)

	err = fut4.Get(actx, nil)
	if err != nil {
		// Handle the error
		return "Transfer Entry posting failed", err
	}

	return "Transfer Entry Successfully verified", nil
}

//Temporal repo replica

func (ur *TransferEntryRepository) SubVerifiedTePostingTemporalRepoNew(ctx context.Context, request domain.SubTeVerifiedBullk) error {
	ctx, cancel := context.WithTimeout(ctx, TransferentryRepoInstance.Cfg.GetDuration("db.QueryTimeoutMed"))
	defer cancel()
	log.Debug(ctx, "Came inside PostPfmsverifiedRepo")

	transDate := time.Now().Format("2006-01-02")
	approvedDate, err := time.Parse("2006-01-02", transDate)
	if err != nil {
		return err
	}
	var officetype = "DDO"

	batch := &pgx.Batch{}

	var verification_status = "verified"

	// Batch inserts for transfer_entry
	for _, t := range request.SubTes {
		insertBuilder := dblib.Psql.Insert("pao.transfer_entry").
			Columns("pao_code", "ddo_code", "transfer_entry_id", "hoa", "transfer_amount", "transfer_type", "created_by", "created_date", "te_source_office_type", "verification_status", "verified_by", "verified_date", "approver_remarks", "remarks", "account_code", "trans_date").
			Values(t.PaoCode, t.DdoCode, t.TransId, t.Hoa, t.TransferAmount, t.TransferType, t.CreatedBy, t.CreatedDate, officetype, verification_status, t.ApprovedBy, approvedDate, t.ApproverRemarks, t.RemarksByCreator, t.AccountCode, t.TransDate)

		// insertBuilder := dblib.Psql.Insert("pao.transfer_entry").
		// 	Columns(
		// 		"pao_code", "ddo_code", "transfer_entry_id", "hoa",
		// 		"transfer_amount", "transfer_type", "created_by", "created_date",
		// 		"te_source_office_type", "verification_status", "verified_by",
		// 		"verified_date", "approver_remarks", "remarks", "account_code",
		// 		"trans_date", // ✅ new column
		// 	).
		// 	Values(
		// 		t.PaoCode, t.DdoCode, t.TransId, t.Hoa,
		// 		t.TransferAmount, t.TransferType, t.CreatedBy, t.CreatedDate,
		// 		officetype, verification_status, t.ApprovedBy, approvedDate,
		// 		t.ApproverRemarks, t.RemarksByCreator, t.AccountCode,
		// 		approvedDate, // ✅ reuse - already 2025-08-28 00:00:00.000
		// 	)

		err := dblib.QueueExecRow(batch, insertBuilder)
		if err != nil {
			return err
		}

	}

	// Execute the batch and check for errors
	results := TransferentryRepoInstance.Db.SendBatch(ctx, batch)
	if results != nil {
		defer results.Close()

		for i := 0; i < batch.Len(); i++ {
			_, err := results.Exec()
			if err != nil {
				log.Debug(ctx, "Error executing batch command:", err)
				return err
			}
		}
	}

	return nil
}
func (ur *TransferEntryRepository) GetTePfmsUpdateStatusRepo(gctx *gin.Context, cbds []domain.TeData, Uniq string) error {
	ctx, cancel := context.WithTimeout(gctx.Request.Context(), ur.Cfg.GetDuration("db.QueryTimeoutMed"))
	defer cancel()
	batch := &pgx.Batch{}
	var pendingstatus = "Pending"

	for _, cb := range cbds {
		updateBuilder := dblib.Psql.Update("pao.transfer_entry").
			Set("pfms_unique_id", sq.Expr("$1", Uniq)).
			Set("h_pfms_generation_flag", sq.Expr("$2", true)).
			Set("pfms_submission_flag", sq.Expr("$3", pendingstatus)).
			Where("transfer_entry_id = $4", cb.TeId)
			// Where("TO_CHAR(created_date, 'YYYY-MM-DD') = $5", cb.TeDate)

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

func (ur *TransferEntryRepository) TransferentryInterPaoCreationRepo(gctx *gin.Context, request []domain.TransferEntryInterPaoRequest) ([]domain.InsertedIds, error) {
	ctx, cancel := context.WithTimeout(gctx.Request.Context(), ur.Cfg.GetDuration("db.QueryTimeoutMed"))
	defer cancel()
	log.Debug(gctx, "Came inside PostPfmsverifiedRepo")

	transDate := time.Now().Format("2006-01-02")
	createdDate, err := time.Parse("2006-01-02", transDate)
	if err != nil {
		return nil, err
	}
	createdTime := time.Now()
	createdTimeString := createdTime.Format("20060102150405")
	var verification_status = "initiated"

	var Inserted_Ids []domain.InsertedIds

	copycount, err := ur.Db.CopyFrom(
		ctx,
		pgx.Identifier{"pao", "transfer_entry_interpao"},
		[]string{"master_pao_code", "pao_code", "ddo_code", "hoa", "transfer_amount", "transfer_type", "created_by", "created_date", "te_source_office_type", "transfer_entry_id", "remarks", "verification_status"},
		pgx.CopyFromSlice(len(request), func(i int) ([]interface{}, error) {
			row := []interface{}{
				request[i].MasterPaoCode,
				request[i].PaoCode,
				request[i].DdoCode,
				request[i].Hoa,
				request[i].TransferAmount,
				request[i].TransferType,
				request[i].CreatedBy,
				createdDate,
				request[i].TeSourceOfficeType,
				request[i].MasterPaoCode.String + createdTimeString,
				request[i].Remarks,
				verification_status,
			}
			// Append the row to the slice
			Inserted_Ids = append(Inserted_Ids, domain.InsertedIds{

				TransferEntryId: request[i].MasterPaoCode.String + createdTimeString,
			})
			return row, nil
		}),
	)

	log.Debug(gctx, "Copy Count", copycount)

	if err != nil {
		log.Debug(gctx, "Error inserting hoas:", err)
		return nil, err
	}
	return Inserted_Ids, nil // Return nil if everything executed successfully

}

func (ur *TransferEntryRepository) ListTransferEntryInterPaoMasterRepo(gctx *gin.Context, request domain.TransferEntryInterPaoMasterRequest, reqMetadata port.MetaDataRequest) ([]domain.TransferEntryInterPaoReport, error) {
	ctx, cancel := context.WithTimeout(gctx.Request.Context(), ur.Cfg.GetDuration("db.QueryTimeoutMed"))
	defer cancel()
	log.Debug(gctx, "Came inside PostPfmsverifiedRepo")
	fromDate, err := time.Parse("2006-01-02", request.FromDate)
	if err != nil {
		return nil, err
	}

	toDate, err := time.Parse("2006-01-02", request.ToDate)
	if err != nil {
		return nil, err
	}
	var transentry domain.TransferEntryInterPaoReport
	columns := dblib.GenerateColumnsFromStruct(transentry, "select")
	query := dblib.Psql.Select(columns...).
		FromSelect(
			dblib.Psql.Select("DISTINCT transfer_entry_id", "master_pao_code", "created_date", "created_by", "remarks").
				From("pao.transfer_entry_interpao").
				Where(sq.And{
					sq.Eq{"master_pao_code": request.PaoCode},
					sq.GtOrEq{"DATE(created_date)": fromDate},
					sq.LtOrEq{"DATE(created_date)": toDate},
					sq.Eq{"verification_status": request.VerificationStatus},
				}), "t").
		OrderBy("master_pao_code").Offset(uint64(reqMetadata.Skip * reqMetadata.Limit)).
		Limit(uint64(reqMetadata.Limit))

	return dblib.SelectRows(ctx, ur.Db, query, pgx.RowToStructByNameLax[domain.TransferEntryInterPaoReport])

}

func (ur *TransferEntryRepository) ListTransferEntryInterPaoRepo(gctx *gin.Context, request domain.TransferEntryInterPaoMasterRequest, reqMetadata port.MetaDataRequest) ([]domain.TransferEntryInterPaoReport, error) {
	ctx, cancel := context.WithTimeout(gctx.Request.Context(), ur.Cfg.GetDuration("db.QueryTimeoutMed"))
	defer cancel()
	log.Debug(gctx, "Came inside PostPfmsverifiedRepo")
	fromDate, err := time.Parse("2006-01-02", request.FromDate)
	if err != nil {
		return nil, err
	}

	toDate, err := time.Parse("2006-01-02", request.ToDate)
	if err != nil {
		return nil, err
	}
	var transentry domain.TransferEntryInterPaoReport
	columns := dblib.GenerateColumnsFromStruct(transentry, "select")
	query := dblib.Psql.Select(columns...).
		FromSelect(
			dblib.Psql.Select("DISTINCT transfer_entry_id", "master_pao_code", "created_date", "created_by", "remarks").
				From("pao.transfer_entry_interpao").
				Where(sq.And{
					sq.Eq{"pao_code": request.PaoCode},
					sq.GtOrEq{"DATE(created_date)": fromDate},
					sq.LtOrEq{"DATE(created_date)": toDate},
					sq.Eq{"verification_status": request.VerificationStatus},
				}), "t").
		OrderBy("master_pao_code").Offset(uint64(reqMetadata.Skip * reqMetadata.Limit)).
		Limit(uint64(reqMetadata.Limit))

	return dblib.SelectRows(ctx, ur.Db, query, pgx.RowToStructByNameLax[domain.TransferEntryInterPaoReport])

}
func (ur *TransferEntryRepository) InterPaoTransferentryDetailRepo(gctx *gin.Context, request domain.PaoSubTeDetailRequest) ([]domain.TransferEntryInterPao, error) {
	ctx, cancel := context.WithTimeout(gctx.Request.Context(), ur.Cfg.GetDuration("db.QueryTimeoutMed"))
	defer cancel()
	log.Debug(gctx, "Came inside InterPaoTransferentryDetailRepo")

	columns := []string{
		"t.master_pao_code",
		"t.pao_code",
		"t.hoa",
		"k.hoa_description",
		"d.ddo_name",
		"t.transfer_amount",
		"t.transfer_type",
		"t.created_by",
		"t.created_date",
		"t.ddo_code",
		"t.transfer_entry_id",
		"t.te_source_office_type",
		"t.remarks",
		"t.verified_by",
		"t.verified_date",
		"t.verification_status",
		"t.approver_remarks",
		"t.budget_id",
		"t.h_pfms_generation_flag",
		"t.pfms_unique_id",
		"t.pfms_submission_flag",
		"t.pfms_error_description",
	}

	query := dblib.Psql.Select(columns...).
		Distinct().
		From("pao.transfer_entry_interpao t").
		LeftJoin("pao.kafka_account_codes_master k ON t.hoa = k.hoa").
		LeftJoin("pao.ddo_master d ON t.ddo_code = d.ddo_code").
		Where(sq.Eq{"t.transfer_entry_id": request.TransId})

	return dblib.SelectRows(ctx, ur.Db, query, pgx.RowToStructByNameLax[domain.TransferEntryInterPao])
}

func (ur *TransferEntryRepository) TransferentryInterPaoMasterVerifyRepo(gctx *gin.Context, req domain.TransferEntryInterPao) error {

	ctx, cancel := context.WithTimeout(gctx.Request.Context(), ur.Cfg.GetDuration("db.QueryTimeoutMed"))
	defer cancel()
	currentTime := time.Now()
	formattedTime := currentTime.Format("2006-01-02 15:04:05")

	batch := &pgx.Batch{}

	updateBuilder1 := sq.Update("pao.transfer_entry_interpao").
		Set("verified_by", sq.Expr("$1", req.VerifiedBy)).
		Set("verified_date", sq.Expr("$2", formattedTime)).
		Set("approver_remarks", sq.Expr("$3", req.ApproverRemarks)).
		Set("verification_status", sq.Expr("$4", "verified")).
		Where("transfer_entry_id = $5", req.TransferEntryId).
		Where("pao_code = master_pao_code")
	err := dblib.QueueExecRow(batch, updateBuilder1)
	if err != nil {
		return err
	}

	updateBuilder2 := sq.Update("pao.transfer_entry_interpao").
		Set("verification_status", sq.Expr("$1", "created")).
		Where("transfer_entry_id = $2", req.TransferEntryId).
		Where("pao_code != master_pao_code")

	err1 := dblib.QueueExecRow(batch, updateBuilder2)
	if err1 != nil {
		return err1
	}

	batchResults := ur.Db.SendBatch(ctx, batch)
	defer batchResults.Close()

	// Check results of batch execution
	_, err = batchResults.Exec()
	if err != nil {
		log.Debug(gctx, "Batch execution failed", "error", err)
		return err
	}

	return nil
}

func (ur *TransferEntryRepository) TransferentryInterPaoRejectRepo(gctx *gin.Context, req domain.TransferEntryInterPao) error {

	ctx, cancel := context.WithTimeout(gctx.Request.Context(), ur.Cfg.GetDuration("db.QueryTimeoutMed"))
	defer cancel()
	currentTime := time.Now()
	formattedTime := currentTime.Format("2006-01-02 15:04:05")

	batch := &pgx.Batch{}

	updateBuilder1 := sq.Update("pao.transfer_entry_interpao").
		Set("verified_by", sq.Expr("$1", req.VerifiedBy)).
		Set("verified_date", sq.Expr("$2", formattedTime)).
		Set("approver_remarks", sq.Expr("$3", req.ApproverRemarks)).
		Set("verification_status", sq.Expr("$4", "deleted")).
		Where("transfer_entry_id = $5", req.TransferEntryId)
	err := dblib.QueueExecRow(batch, updateBuilder1)
	if err != nil {
		return err
	}

	errors := ur.Db.SendBatch(ctx, batch).Close()
	if errors != nil {
		log.Debug(gctx, "Error results:", errors)
		return errors
	}
	return nil
}

func (ur *TransferEntryRepository) TransferentryInterPaoVerifyRepo(gctx *gin.Context, req domain.TransferEntryInterPao) error {

	ctx, cancel := context.WithTimeout(gctx.Request.Context(), ur.Cfg.GetDuration("db.QueryTimeoutMed"))
	defer cancel()
	currentTime := time.Now()
	formattedTime := currentTime.Format("2006-01-02 15:04:05")

	batch := &pgx.Batch{}

	updateBuilder1 := sq.Update("pao.transfer_entry_interpao").
		Set("verified_by", sq.Expr("$1", req.VerifiedBy)).
		Set("verified_date", sq.Expr("$2", formattedTime)).
		Set("approver_remarks", sq.Expr("$3", req.ApproverRemarks)).
		Set("verification_status", sq.Expr("$4", "verified")).
		Where("transfer_entry_id = $5", req.TransferEntryId).
		Where("pao_code = $6", req.PaoCode)
	err := dblib.QueueExecRow(batch, updateBuilder1)
	if err != nil {
		return err
	}

	batchResults := ur.Db.SendBatch(ctx, batch)
	defer batchResults.Close()

	// Check results of batch execution
	_, err = batchResults.Exec()
	if err != nil {
		log.Debug(gctx, "Batch execution failed", "error", err)
		return err
	}

	return nil
}

func (ur *TransferEntryRepository) InsertTePfmsSubmission(gctx *gin.Context, pfmsUniqueId string, submissionType string, cbRequest domain.CbData, teRequest []domain.TeData, businessDate string, submissionDate time.Time, submissionData domain.Payload, submissionStatus string, errorDescription string) error {

	ctx, cancel := context.WithTimeout(gctx.Request.Context(), ur.Cfg.GetDuration("db.QueryTimeoutMed"))
	defer cancel()
	// Parse businessDate to timestamp
	businessDateTime, err := time.Parse("2006-01-02", businessDate)
	if err != nil {
		return fmt.Errorf("failed to parse businessDate: %v", err)
	}

	// Convert cbRequest to JSONB
	teRequestJSON, err := json.Marshal(teRequest)
	if err != nil {
		return fmt.Errorf("failed to marshal teRequest: %v", err)
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

	query := dblib.Psql.Insert("pao.pfms_submission").Columns("pfms_unique_id", "pfms_submission_type", "te_request", "business_date", "submission_date", "submission_data", "submission_status", "error_description").
		Values(pfmsUniqueId, submissionType, teRequestJSON, businessDateTime, submissionDate, submissionDataJSON, submissionStatus, errorDesc)

	_, err3 := dblib.Insert(ctx, ur.Db, query)

	return err3
}

func (ur *TransferEntryRepository) GetPaoCodenDdoCodeByOfficeIDRepo(gctx *gin.Context, request uint64) (*domain.OfficeIdBRSReply, bool, error) {
	ctx, cancel := context.WithTimeout(gctx.Request.Context(), ur.Cfg.GetDuration("db.QueryTimeoutMed"))
	defer cancel()
	log.Debug(gctx, "Came inside GetPaoCodenDdoCodeByOfficeIDRepo")
	query := dblib.Psql.Select("pao_code, ddo_code").
		From("pao.ddo_master").
		Where(sq.Eq{"ddo_office_id": request}).
		Limit(1)
	return dblib.SelectOneOK(ctx, ur.Db, query, pgx.RowToAddrOfStructByNameLax[domain.OfficeIdBRSReply])
}

func (ur *TransferEntryRepository) ResetPFMSFlagByPfmsUniqueIdRepo(gctx *gin.Context, request *domain.TransferEntryPFMSResetRequest) (int64, error) {

	ctx, cancel := context.WithTimeout(gctx.Request.Context(), ur.Cfg.GetDuration("db.QueryTimeoutMed"))
	defer cancel()

	updateBuilder := dblib.Psql.Update("pao.transfer_entry").
		Set("pfms_submission_flag", "false"). // varchar(10)  → string
		Set("pfms_error_description", nil).   // varchar(1000) → NULL
		Set("h_pfms_generation_flag", false). // bool          → Go bool
		Set("pfms_unique_id", nil).           // varchar(30)  → NULL
		Where("pfms_unique_id = ?", request.PfmsUniqueId)

	sql, args, err := updateBuilder.ToSql()
	if err != nil {
		return 0, err
	}

	result, err := ur.Db.Exec(ctx, sql, args...)
	if err != nil {
		return 0, err
	}

	rowsAffected := result.RowsAffected()
	return rowsAffected, nil
}

// GetReversiblePfmsTe — fetch all successful TE submissions
func (ur *TransferEntryRepository) GetReversiblePfmsTe(gctx *gin.Context) ([]domain.PfmsTeReversible, error) {

	ctx, cancel := context.WithTimeout(gctx.Request.Context(), ur.Cfg.GetDuration("db.QueryTimeoutMed"))
	defer cancel()

	query := dblib.Psql.
		Select(
			"pfms_unique_id",
			"te_number",
			"business_date",
			"submission_date",
			"submission_status",
		).
		From("pao.pfms_submission").
		Where(sq.Eq{
			"pfms_submission_type": "te",
			"submission_status":    "Success",
		})

	return dblib.SelectRows(ctx, ur.Db, query, pgx.RowToStructByNameLax[domain.PfmsTeReversible])
}

// GetPfmsSubmissionByUniqueID — fetch single row by pfms_unique_id
func (ur *TransferEntryRepository) GetPfmsSubmissionByUniqueID(gctx *gin.Context, uniqueID string) (*domain.PfmsSubmissionRow, error) {

	ctx, cancel := context.WithTimeout(gctx.Request.Context(), ur.Cfg.GetDuration("db.QueryTimeoutMed"))
	defer cancel()

	query := dblib.Psql.
		Select(
			"pfms_unique_id",
			"pfms_submission_type",
			"te_request",
			"business_date",
			"submission_date",
			"submission_data",
			"submission_status",
			"error_description",
			"te_number",
		).
		From("pao.pfms_submission").
		Where(sq.Eq{"pfms_unique_id": uniqueID}).
		Limit(1)

	rows, err := dblib.SelectRows(ctx, ur.Db, query, pgx.RowToStructByNameLax[domain.PfmsSubmissionRow])
	if err != nil {
		return nil, err
	}
	if len(rows) == 0 {
		return nil, nil
	}
	return &rows[0], nil
}

// CheckIfAlreadyReversed — check if a reversal already exists for this original_pfms_uid
func (ur *TransferEntryRepository) CheckIfAlreadyReversed(gctx *gin.Context, originalUniqueID string) (bool, error) {

	ctx, cancel := context.WithTimeout(gctx.Request.Context(), ur.Cfg.GetDuration("db.QueryTimeoutMed"))
	defer cancel()

	query := dblib.Psql.
		Select("COUNT(*)").
		From("pao.pfms_submission").
		Where(sq.Eq{
			"original_pfms_uid":    originalUniqueID,
			"pfms_submission_type": "terev",
			"submission_status":    "Success",
		})

	sql, args, err := query.ToSql()
	if err != nil {
		return false, err
	}

	var count int
	err = ur.Db.QueryRow(ctx, sql, args...).Scan(&count)
	if err != nil {
		return false, err
	}
	return count > 0, nil
}

// InsertReversalPfmsSubmission — insert negative entry into pfms_submission
func (ur *TransferEntryRepository) InsertReversalPfmsSubmission(
	ctx context.Context,
	newUniqueID string,
	submissionType string,
	teRequest []domain.TeData,
	businessDate time.Time,
	submissionDate time.Time,
	payload domain.Payload,
	status string,
	errorDesc string,
	originalPfmsUID string,
) error {

	dbCtx, cancel := context.WithTimeout(ctx, ur.Cfg.GetDuration("db.QueryTimeoutMed"))
	defer cancel()

	teRequestJSON, err := json.Marshal(teRequest)
	if err != nil {
		return fmt.Errorf("failed to marshal teRequest: %w", err)
	}

	submissionDataJSON, err := json.Marshal(payload)
	if err != nil {
		return fmt.Errorf("failed to marshal payload: %w", err)
	}

	var errDesc interface{}
	if errorDesc != "" {
		errDesc = errorDesc
	} else {
		errDesc = nil
	}

	query := dblib.Psql.Insert("pao.pfms_submission").
		Columns(
			"pfms_unique_id",
			"pfms_submission_type",
			"te_request",
			"business_date",
			"submission_date",
			"submission_data",
			"submission_status",
			"error_description",
			"original_pfms_uid",
		).
		Values(
			newUniqueID,
			submissionType,
			teRequestJSON,
			businessDate,
			submissionDate,
			submissionDataJSON,
			status,
			errDesc,
			originalPfmsUID,
		)

	_, err = dblib.Insert(dbCtx, ur.Db, query)
	return err
}

// InsertTeReversal — insert audit row into pfms_te_reversal
func (ur *TransferEntryRepository) InsertTeReversal(
	ctx context.Context,
	originalUniqueID string,
	reversalUniqueID string,
	teNumber string,
	businessDate time.Time,
	employeeID int,
	remark string,
	status string,
	errorDesc string,
) error {

	dbCtx, cancel := context.WithTimeout(ctx, ur.Cfg.GetDuration("db.QueryTimeoutMed"))
	defer cancel()

	var errDesc interface{}
	if errorDesc != "" {
		errDesc = errorDesc
	} else {
		errDesc = nil
	}

	var teNum interface{}
	if teNumber != "" {
		teNum = teNumber
	} else {
		teNum = nil
	}

	query := dblib.Psql.Insert("pao.pfms_te_reversal").
		Columns(
			"original_pfms_unique_id",
			"reversal_pfms_unique_id",
			"te_number",
			"business_date",
			"request_employee_id",
			"remark",
			"status",
			"error_description",
		).
		Values(
			originalUniqueID,
			reversalUniqueID,
			teNum,
			businessDate,
			employeeID,
			remark,
			status,
			errDesc,
		)

	_, err := dblib.Insert(dbCtx, ur.Db, query)
	return err
}
