package repository

import (
	"context"
	"time"

	"gotemplate/core/domain"

	config "gitlab.cept.gov.in/it-2.0-common/api-config"

	"github.com/jackc/pgx/v5"
	dblib "gitlab.cept.gov.in/it-2.0-common/api-db"
	log "gitlab.cept.gov.in/it-2.0-common/api-log"
)

type TransferEntryGrpcRepository struct {
	Db  *dblib.DB
	Cfg *config.Config
}

// NewUserRepository creates a new user repository instance
func NewTransferEntryGrpcRepository(Db *dblib.DB, Cfg *config.Config) *TransferEntryGrpcRepository {
	return &TransferEntryGrpcRepository{
		Db,
		Cfg,
	}
}
func (ur *TransferEntryGrpcRepository) TransferentryCreationGrpcRepo(gctx *context.Context, request []domain.TransferEntryRequest) ([]domain.InsertedIds, error) {

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
		*gctx,
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

	log.Debug(*gctx, "Copy Count", copycount)

	if err != nil {
		log.Debug(*gctx, "Error inserting hoas:", err)
		return nil, err
	}
	return Inserted_Ids, nil // Return nil if everything executed successfully

}
func (ur *TransferEntryGrpcRepository) TransferentryDirectGrpcCreationRepo(gctx *context.Context, request []domain.TransferEntryDirectRequest) ([]domain.InsertedIds, error) {
	var Inserted_Ids []domain.InsertedIds

	copycount, err := ur.Db.CopyFrom(
		*gctx,
		pgx.Identifier{"pao", "transfer_entry"},
		[]string{"pao_code", "ddo_code", "hoa", "transfer_amount", "transfer_type", "created_by", "created_date", "te_source_office_type", "transfer_entry_id", "remarks", "verification_status", "xml_generation_status", "verified_by", "verified_date", "approver_remarks"},
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
				request[i].VerifiedBy,
				request[i].VerifiedDate,
				request[i].ApproverRemarks,
			}
			// Append the row to the slice
			Inserted_Ids = append(Inserted_Ids, domain.InsertedIds{

				TransferEntryId: request[i].TransferEntryId,
			})
			return row, nil
		}),
	)
	log.Debug(*gctx, "Copy Count", copycount)

	if err != nil {
		log.Debug(*gctx, "Error inserting hoas:", err)
		return nil, err
	}
	return Inserted_Ids, nil // Return nil if everything executed successfully

}
