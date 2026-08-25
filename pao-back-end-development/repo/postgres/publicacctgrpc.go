package repository

import (
	"context"
	"gotemplate/core/domain"
	"time"

	config "gitlab.cept.gov.in/it-2.0-common/api-config"

	"github.com/jackc/pgx/v5"
	dblib "gitlab.cept.gov.in/it-2.0-common/api-db"
	log "gitlab.cept.gov.in/it-2.0-common/api-log"
)

type PublicAcctGrpcRepository struct {
	Db  *dblib.DB
	Cfg *config.Config
}

// NewUserRepository creates a new user repository instance
func NewPublicAcctGrpcRepository(Db *dblib.DB, Cfg *config.Config) *PublicAcctGrpcRepository {
	return &PublicAcctGrpcRepository{
		Db,
		Cfg,
	}
}

func (hr *PublicAcctRepository) RemunerationCalculationGrpcRepo(gctx *context.Context, req domain.RemunerationCreationRequestBulk) ([]domain.RemunerationCreation, error) {

	var arp []domain.RemunerationCreation

	batch := &pgx.Batch{}

	var i int = 0

	for _, r := range req.RemunerationCreation {
		queryselectSubpiece := dblib.Psql.Select("r.financial_year,r.remuneration_item,r.remuneration_type,r.remuneration_rate").
			Column("(r.remuneration_rate * $1) AS item_remuneration", r.RemunerationItemCount).
			Column("$2::integer AS remuneration_item_count", r.RemunerationItemCount).
			From("pao.remuneration_rate_master r").
			Where("r.financial_year = $3", r.FinancialYear).
			Where("r.remuneration_item = $4 ", r.RemunerationItem).
			Where("r.remuneration_type = $5", r.RemunerationType).
			Where("r.authorisation_status = '2'")

		err := dblib.QueueReturnBulk(batch, queryselectSubpiece, pgx.RowToStructByName[domain.RemunerationCreation], &arp)
		if err != nil {
			return nil, err
		}

		i++
	}

	results := hr.Db.SendBatch(*gctx, batch).Close()

	if results != nil {
		log.Debug(*gctx, "Error results:", results)
		return nil, results
	}
	return arp, nil

}

func (hr *PublicAcctRepository) RemunerationCalculationGrpcPostRepo(gctx *context.Context, request []domain.RemunerationCreation) error {

	createdTime := time.Now()

	copycount, err := hr.Db.CopyFrom(
		*gctx,
		pgx.Identifier{"pao", "remuneration"},
		[]string{"financial_year", "remuneration_item", "remuneration_type", "remuneration_rate", "remuneration_item_count", "item_remuneration", "created_date", "last_modified_date"},
		pgx.CopyFromSlice(len(request), func(i int) ([]interface{}, error) {
			row := []interface{}{
				request[i].FinancialYear,
				request[i].RemunerationItem,
				request[i].RemunerationType,
				request[i].RemunerationRate,
				request[i].RemunerationItemCount,
				request[i].ItemRemuneration,
				createdTime,
				createdTime,
			}
			return row, nil
		}),
	)

	log.Debug(*gctx, "Copy Count", copycount)

	if err != nil {
		log.Debug(*gctx, "Error inserting hoas:", err)
		return err
	}

	return nil

}
