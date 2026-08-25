package repository

import (
	"context"
	"errors"
	"gotemplate/core/domain"
	"gotemplate/core/port"
	"strconv"
	"time"

	config "gitlab.cept.gov.in/it-2.0-common/api-config"

	sq "github.com/Masterminds/squirrel"
	"github.com/gin-gonic/gin"
	"github.com/jackc/pgx/v5"
	dblib "gitlab.cept.gov.in/it-2.0-common/api-db"
	log "gitlab.cept.gov.in/it-2.0-common/api-log"
)

type PublicAcctRepository struct {
	Db  *dblib.DB
	Cfg *config.Config
}

// NewUserRepository creates a new user repository instance
func NewPublicAcctRepository(Db *dblib.DB, Cfg *config.Config) *PublicAcctRepository {
	return &PublicAcctRepository{
		Db,
		Cfg,
	}
}

func (pr *PublicAcctRepository) GetbroadsheetRepo(gctx *gin.Context, request domain.BroadsheetRequest, reqMetadata port.MetaDataRequest) ([]domain.BroadSheet, error) {
	ctx, cancel := context.WithTimeout(gctx.Request.Context(), pr.Cfg.GetDuration("db.QueryTimeoutMed"))
	defer cancel()
	log.Debug(gctx, "Came inside GetbroadsheetRepo")

	if request.Type == 1 {

		query := dblib.Psql.Select("a.broadsheet_month, a.hoa,a.ddo_code,b.ddo_name,a.opening_balance,a.credit_amount,a.debit_amount,a.closing_balance").
			From("pao.broad_sheet a").
			LeftJoin("pao.ddo_master AS b ON a.ddo_code = b.ddo_code").
			Where(sq.And{sq.Eq{"a.broadsheet_month": request.MonthYear}, sq.Eq{"LEFT(a.hoa, 4)": request.MajorHead}}).
			OrderBy("a.ddo_code").Offset(uint64(reqMetadata.Skip * reqMetadata.Limit)).
			Limit(uint64(reqMetadata.Limit))

		return dblib.SelectRows(ctx, pr.Db, query, pgx.RowToStructByNameLax[domain.BroadSheet])
	} else if request.Type == 2 {

		query := dblib.Psql.Select("a.broadsheet_month, a.hoa,a.ddo_code,b.ddo_name,a.opening_balance,a.credit_amount,a.debit_amount,a.closing_balance").
			From("pao.broad_sheet a").
			LeftJoin("pao.ddo_master AS b ON a.ddo_code = b.ddo_code").
			Where(sq.And{sq.Eq{"a.broadsheet_month": request.MonthYear}, sq.Eq{"a.ddo_code": request.DdoCode}}).
			OrderBy("a.ddo_code").Offset(uint64(reqMetadata.Skip * reqMetadata.Limit)).
			Limit(uint64(reqMetadata.Limit))

		return dblib.SelectRows(ctx, pr.Db, query, pgx.RowToStructByNameLax[domain.BroadSheet])
	} else if request.Type == 3 {

		query := dblib.Psql.Select("a.broadsheet_month, a.hoa,a.ddo_code,b.ddo_name,a.opening_balance,a.credit_amount,a.debit_amount,a.closing_balance").
			From("pao.broad_sheet a").
			LeftJoin("pao.ddo_master AS b ON a.ddo_code = b.ddo_code").
			Where(sq.Eq{"a.broadsheet_month": request.MonthYear}).
			OrderBy("a.ddo_code").Offset(uint64(reqMetadata.Skip * reqMetadata.Limit)).
			Limit(uint64(reqMetadata.Limit))

		return dblib.SelectRows(ctx, pr.Db, query, pgx.RowToStructByNameLax[domain.BroadSheet])
	} else if request.Type == 4 {

		query := dblib.Psql.Select("a.broadsheet_month, a.hoa,a.ddo_code,b.ddo_name,a.opening_balance,a.credit_amount,a.debit_amount,a.closing_balance").
			From("pao.broad_sheet a").
			LeftJoin("pao.ddo_master AS b ON a.ddo_code = b.ddo_code").
			Where(sq.And{sq.Eq{"a.broadsheet_month": request.MonthYear}, sq.Eq{"LEFT(a.hoa, 4)": request.MajorHead}, sq.Eq{"a.ddo_code": request.DdoCode}}).
			OrderBy("a.ddo_code").Offset(uint64(reqMetadata.Skip * reqMetadata.Limit)).
			Limit(uint64(reqMetadata.Limit))

		return dblib.SelectRows(ctx, pr.Db, query, pgx.RowToStructByNameLax[domain.BroadSheet])
	} else if request.Type == 5 {

		query := dblib.Psql.Select("a.broadsheet_month, a.hoa,a.ddo_code,b.ddo_name,a.opening_balance,a.credit_amount,a.debit_amount,a.closing_balance").
			From("pao.broad_sheet a").
			LeftJoin("pao.ddo_master b on b.ddo_code = a.ddo_code").
			Where(sq.And{sq.Eq{"a.broadsheet_month": request.MonthYear}, sq.Eq{"b.pao_code": request.DdoCode}}).
			OrderBy("a.ddo_code").Offset(uint64(reqMetadata.Skip * reqMetadata.Limit)).
			Limit(uint64(reqMetadata.Limit))

		return dblib.SelectRows(ctx, pr.Db, query, pgx.RowToStructByNameLax[domain.BroadSheet])
	} else {
		return nil, errors.New("invalid request type")
	}
}

func (pr *PublicAcctRepository) GetappracctRepo(gctx *gin.Context, request domain.ApprAcctsRequest, reqMetadata port.MetaDataRequest) ([]domain.ApprAccts, error) {
	ctx, cancel := context.WithTimeout(gctx.Request.Context(), pr.Cfg.GetDuration("db.QueryTimeoutMed"))
	defer cancel()
	log.Debug(gctx, "Came inside Get appr acccts Repo")

	var appraccts domain.ApprAccts

	columns := dblib.GenerateColumnsFromStruct(appraccts, "select")
	query := dblib.Psql.Select(columns...).
		FromSelect(
			dblib.Psql.Select("a.hoa", "b.hoa_description").
				Column(sq.Expr("SUM(CASE WHEN allocation_type = 'BE' THEN amount ELSE 0 END) AS be")).
				Column(sq.Expr("SUM(CASE WHEN allocation_type = 'RE' THEN amount ELSE 0 END) AS re")).
				Column(sq.Expr("SUM(CASE WHEN allocation_type = 'FG' THEN amount ELSE 0 END) AS fg")).
				From("pao.kafka_budget a").
				LeftJoin("pao.kafka_account_codes_master b ON a.hoa = b.hoa").
				Where(sq.Eq{"from_office_id": 99999999}).
				Where(sq.Eq{"financial_year": request.Year}).
				GroupBy("a.hoa", "b.hoa_description"), "t").
		Offset(uint64(reqMetadata.Skip * reqMetadata.Limit)).
		Limit(uint64(reqMetadata.Limit))

	return dblib.SelectRows(ctx, pr.Db, query, pgx.RowToStructByNameLax[domain.ApprAccts])

}

func (pr *PublicAcctRepository) GetappracctRepo2(gctx *gin.Context, request domain.ApprAcctsRequest, reqMetadata port.MetaDataRequest) ([]domain.ApprAccts2, error) {
	ctx, cancel := context.WithTimeout(context.Background(), 100*time.Second)
	defer cancel()
	log.Debug(gctx, "Came inside Get appr acccts Repo")

	var appraccts2 domain.ApprAccts2
	year, err := strconv.Atoi(request.Year)
	if err != nil {
		log.Error(gctx, "Error in conversion")
	}

	columns := dblib.GenerateColumnsFromStruct(appraccts2, "select")

	subQuery, _, err := sq.Select("hoa", "total_payment").
		From("pao.pao_prao_account_detail").
		Where(sq.Or{
			sq.And{
				sq.Expr("CAST(SUBSTRING(period, 3, 4) AS INTEGER) = ?", year),
				sq.Expr("CAST(SUBSTRING(period, 1, 2) AS INTEGER) >= 04"),
			},
			sq.And{
				sq.Expr("CAST(SUBSTRING(period, 3, 4) AS INTEGER) = ?", year+1),
				sq.Expr("CAST(SUBSTRING(period, 1, 2) AS INTEGER) <= 03"),
			},
		}).ToSql()
	if err != nil {
		log.Error(gctx, "Error in sub squery")
	}

	query := dblib.Psql.Select(columns...).
		FromSelect(
			dblib.Psql.Select("a.hoa", "c.hoa_description").
				Column(sq.Expr("SUM(CASE WHEN allocation_type = 'FG' THEN amount ELSE 0 END) AS fg")).
				Column(sq.Expr("sum(b.total_payment) as total_exp")).
				From("pao.kafka_budget a").
				LeftJoin("("+subQuery+") AS b ON a.hoa = b.hoa", request.Year, request.Year).
				LeftJoin("pao.kafka_account_codes_master c ON a.hoa = c.hoa").
				Where(sq.Eq{"from_office_id": "99999999"}).
				Where(sq.Eq{"financial_year": request.Year}).
				GroupBy("a.hoa", "c.hoa_description"), "t").
		Offset(uint64(reqMetadata.Skip * reqMetadata.Limit)).
		Limit(uint64(reqMetadata.Limit))

	return dblib.SelectRows(ctx, pr.Db, query, pgx.RowToStructByNameLax[domain.ApprAccts2])

}

func (hr *PublicAcctRepository) GetRemRepo(gctx *gin.Context, req *domain.GetRemRequest, reqMetadata port.MetaDataRequest) ([]domain.GetRemuneration, error) {

	ctx, cancel := context.WithTimeout(gctx.Request.Context(), hr.Cfg.GetDuration("db.QueryTimeoutMed"))
	defer cancel()
	log.Debug(gctx, "Came inside GetRemunRepo")
	var u1 domain.GetRemuneration
	if req.Type == 1 {
		columns := dblib.GenerateColumnsFromStruct(u1, "select")
		query := dblib.Psql.Select(columns...).
			From("pao.remuneration_rate_master").
			Where(sq.And{sq.Eq{"financial_year": req.Id}, sq.Eq{"status": true}}).
			OrderBy("financial_year ASC", "remuneration_item ASC").Offset(uint64(reqMetadata.Skip * reqMetadata.Limit)).
			Limit(uint64(reqMetadata.Limit))

		return dblib.SelectRows(ctx, hr.Db, query, pgx.RowToStructByNameLax[domain.GetRemuneration])
	} else if req.Type == 2 {
		columns := dblib.GenerateColumnsFromStruct(u1, "select")
		query := dblib.Psql.Select(columns...).
			From("pao.remuneration_rate_master").
			Where(sq.And{sq.Eq{"authorisation_status": req.Id}, sq.Eq{"status": true}}).
			OrderBy("financial_year ASC", "remuneration_item ASC").Offset(uint64(reqMetadata.Skip * reqMetadata.Limit)).
			Limit(uint64(reqMetadata.Limit))

		return dblib.SelectRows(ctx, hr.Db, query, pgx.RowToStructByNameLax[domain.GetRemuneration])
	} else if req.Type == 3 {
		columns := dblib.GenerateColumnsFromStruct(u1, "select")
		query := dblib.Psql.Select(columns...).
			From("pao.remuneration_rate_master").
			Where(sq.Eq{"status": req.Id}).
			OrderBy("financial_year ASC", "remuneration_item ASC").Offset(uint64(reqMetadata.Skip * reqMetadata.Limit)).
			Limit(uint64(reqMetadata.Limit))

		return dblib.SelectRows(ctx, hr.Db, query, pgx.RowToStructByNameLax[domain.GetRemuneration])
	} else {
		return nil, errors.New("invalid request type")
	}
}

func (hr *PublicAcctRepository) RemCreatewithpgx(gctx *gin.Context, hoas []domain.RemunerationRequest) error {
	ctx, cancel := context.WithTimeout(gctx.Request.Context(), hr.Cfg.GetDuration("db.QueryTimeoutMed"))
	defer cancel()

	currentDateTime := time.Now()

	copycount, err := hr.Db.CopyFrom(
		ctx,
		pgx.Identifier{"pao", "remuneration_rate_master"},
		[]string{"financial_year", "remuneration_item", "remuneration_type", "remuneration_rate", "authorisation_status", "status", "updated_date", "updated_by"},
		pgx.CopyFromSlice(len(hoas), func(i int) ([]interface{}, error) {
			return []interface{}{
				hoas[i].FinancialYear,
				hoas[i].RemunerationItem,
				hoas[i].RemunerationType,
				hoas[i].RemunerationRate,
				hoas[i].AuthorisationStatus,
				hoas[i].Status,
				currentDateTime,
				hoas[i].UpdatedBy,
			}, nil
		}))

	log.Debug(gctx, "Copy Count", copycount)

	if err != nil {
		log.Debug(gctx, "Error inserting hoas:", err)
		return err
	}

	return nil

}

func (hr *PublicAcctRepository) Updateremexe(gctx *gin.Context, updatehoa []domain.UpdateRemRequest) error {
	ctx, cancel := context.WithTimeout(gctx.Request.Context(), hr.Cfg.GetDuration("db.QueryTimeoutMed"))
	defer cancel()
	batch := &pgx.Batch{}

	for _, sub := range updatehoa {
		intlSubPieceSetToMap := dblib.StructToSetMap(&sub)

		updateBuilder := dblib.Psql.Update("pao.remuneration_rate_master").
			SetMap(intlSubPieceSetToMap).
			Where(sq.And{sq.Eq{"remuneration_item": sub.RemunerationItem}, sq.Eq{"financial_year": sub.FinancialYear}, sq.Eq{"remuneration_type": sub.RemunerationType}})
		err := dblib.QueueExecRow(batch, updateBuilder)
		if err != nil {
			return err
		}

	}

	errors := hr.Db.SendBatch(ctx, batch).Close()
	if errors != nil {
		log.Debug(gctx, "Error results:", errors)
		return errors
	}

	return nil
}
func (hr *PublicAcctRepository) RemunerationCalculationRepo(gctx *gin.Context, req domain.RemunerationCreationRequestBulk) ([]domain.RemunerationCreation, error) {

	ctx, cancel := context.WithTimeout(gctx.Request.Context(), hr.Cfg.GetDuration("db.QueryTimeoutMed"))
	defer cancel()
	log.Debug(gctx, "Came inside GetRemunRepo")

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

	results := hr.Db.SendBatch(ctx, batch).Close()

	if results != nil {
		log.Debug(gctx, "Error results:", results)
		return nil, results
	}
	return arp, nil

}

func (hr *PublicAcctRepository) RemunerationCalculationPostRepo(gctx *gin.Context, request []domain.RemunerationCreation) error {

	ctx, cancel := context.WithTimeout(gctx.Request.Context(), hr.Cfg.GetDuration("db.QueryTimeoutMed"))
	defer cancel()
	log.Debug(gctx, "Came inside GetRemunRepo")

	createdTime := time.Now()

	copycount, err := hr.Db.CopyFrom(
		ctx,
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

	log.Debug(gctx, "Copy Count", copycount)

	if err != nil {
		log.Debug(gctx, "Error inserting hoas:", err)
		return err
	}

	return nil

}
func (pr *PublicAcctRepository) GetappracctRepo3(gctx *gin.Context, request domain.ApprAcctsRequest, reqMetadata port.MetaDataRequest) ([]domain.ApprAccts3, error) {
	ctx, cancel := context.WithTimeout(context.Background(), 100*time.Second)
	defer cancel()
	log.Debug(gctx, "Came inside Get appr acccts Repo")

	year, err := strconv.Atoi(request.Year)
	if err != nil {
		log.Error(gctx, "Error in year conversion")
		return nil, err
	}

	// Subquery for account details (b)
	subQueryB, _, err := sq.Select("hoa", "total_payment").
		From("pao.pao_prao_account_detail").
		Where(sq.Or{
			sq.And{
				sq.Expr("CAST(SUBSTRING(period, 3, 4) AS INTEGER) = ?", year),
				sq.Expr("CAST(SUBSTRING(period, 1, 2) AS INTEGER) >= 4"),
			},
			sq.And{
				sq.Expr("CAST(SUBSTRING(period, 3, 4) AS INTEGER) = ?", year+1),
				sq.Expr("CAST(SUBSTRING(period, 1, 2) AS INTEGER) <= 3"),
			},
		}).ToSql()
	if err != nil {
		log.Error(gctx, "Error in sub squery")
	}

	// Re-appropriation subquery (rp)
	subQueryRP, _, err := sq.Select("combined.hoa", "SUM(combined.amount) AS amount").
		FromSelect(
			sq.Select(
				"CASE d.direction WHEN 'from' THEN kra.from_hoa ELSE kra.to_hoa END AS hoa",
				"CASE d.direction WHEN 'from' THEN -kra.amount ELSE kra.amount END AS amount",
			).
				From("pao.kafka_re_appropriation kra").
				CrossJoin("(VALUES ('from'), ('to')) AS d(direction)").
				Where(sq.Eq{"kra.financial_year": request.Year}),
			"combined",
		).
		GroupBy("combined.hoa").ToSql()
	if err != nil {
		log.Error(gctx, "Error in sub squery")
	}
	query := dblib.Psql.Select("des.hoapart", "mh", "mh_description", "smh", "smh_description", "minorhead", "minorhead_description", "subhoa", "subhoa_description", "O", "S", "R", "total_exp").
		FromSelect(
			dblib.Psql.Select("LEFT(t.hoa, 11) AS hoapart", "LEFT(t.hoa, 4) AS mh", "SUBSTRING(t.hoa FROM 5 FOR 2) AS smh", "SUBSTRING(t.hoa FROM 7 FOR 3) AS minorhead", "SUBSTRING(t.hoa FROM 10 FOR 2) AS subhoa", "SUM(t.O) AS O", "SUM(t.RS + t.FS) AS S", "SUM(COALESCE(rp.amount, 0)) AS R", "SUM(COALESCE(t.total_exp, 0)) AS total_exp").
				FromSelect(
					dblib.Psql.Select("a.hoa").
						Column(sq.Expr("SUM(CASE WHEN allocation_type = 'BE' THEN amount ELSE 0 END) AS O")).
						Column(sq.Expr("SUM(CASE WHEN allocation_type = 'RE' THEN amount ELSE 0 END) AS RE")).
						Column(sq.Expr("SUM(CASE WHEN allocation_type = 'FG' THEN amount ELSE 0 END) AS FG")).
						Column(sq.Expr(`CASE 
                WHEN SUM(CASE WHEN allocation_type = 'RE' THEN amount ELSE 0 END) = 0 
                THEN 0 
                ELSE SUM(CASE WHEN allocation_type = 'RE' THEN amount ELSE 0 END) - 
                    SUM(CASE WHEN allocation_type = 'BE' THEN amount ELSE 0 END)
            END AS RS`)).
						Column(sq.Expr(`CASE 
                WHEN SUM(CASE WHEN allocation_type = 'FG' THEN amount ELSE 0 END) = 0 
                THEN 0 
                WHEN SUM(CASE WHEN allocation_type = 'RE' THEN amount ELSE 0 END) != 0 
                THEN SUM(CASE WHEN allocation_type = 'FG' THEN amount ELSE 0 END) - 
                    SUM(CASE WHEN allocation_type = 'RE' THEN amount ELSE 0 END)
                ELSE SUM(CASE WHEN allocation_type = 'FG' THEN amount ELSE 0 END) - 
                    SUM(CASE WHEN allocation_type = 'BE' THEN amount ELSE 0 END)
            END AS FS`)).
						Column(sq.Expr("SUM(b.total_payment) AS total_exp")).
						From("pao.kafka_budget a").
						LeftJoin("("+subQueryB+") AS b ON a.hoa = b.hoa", request.Year, request.Year).
						Where(sq.Eq{
							"from_office_id": "99999999",
							"financial_year": request.Year,
						}).
						GroupBy("a.hoa"), "t").
				LeftJoin("("+subQueryRP+") AS rp ON t.hoa = rp.hoa", request.Year).
				GroupBy("HOAPART", "mh", "smh", "minorhead", "subhoa"), "ty").
		RightJoin("pao.head_description des ON ty.hoapart = des.hoapart").
		GroupBy("des.hoapart", "mh", "mh_description", "smh", "smh_description", "minorhead", "minorhead_description", "subhoa", "subhoa_description", "O", "S", "R", "total_exp").
		Offset(uint64(reqMetadata.Skip * reqMetadata.Limit)).
		Limit(uint64(reqMetadata.Limit))

	return dblib.SelectRows(ctx, pr.Db, query, pgx.RowToStructByNameLax[domain.ApprAccts3])
}

func (hr *PublicAcctRepository) RemunerationCalculatedYearRepo(gctx *gin.Context, req *domain.GetRemYearRequest, reqMetadata port.MetaDataRequest) ([]domain.RemunerationCreation, error) {

	ctx, cancel := context.WithTimeout(gctx.Request.Context(), hr.Cfg.GetDuration("db.QueryTimeoutMed"))
	defer cancel()
	log.Debug(gctx, "Came inside GetRemunRepo")
	var u1 domain.RemunerationCreation
	columns := dblib.GenerateColumnsFromStruct(u1, "db")

	query := dblib.Psql.Select(columns...).
		From("pao.remuneration").
		Where("financial_year = $1", req.Financial_year).
		OrderBy("financial_year").Offset(uint64(reqMetadata.Skip * reqMetadata.Limit)).
		Limit(uint64(reqMetadata.Limit))

	return dblib.SelectRows(ctx, hr.Db, query, pgx.RowToStructByNameLax[domain.RemunerationCreation])

}
