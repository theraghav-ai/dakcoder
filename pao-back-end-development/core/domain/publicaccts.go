package domain

import (
	"gotemplate/core/port"
	"time"

	"github.com/volatiletech/null/v9"
)

type BroadSheet struct {
	BroadsheetMonth null.String  `json:"broadsheet_month" select:"broadsheet_month"`
	Hoa             null.String  `json:"hoa" select:"hoa"`
	DdoCode         null.String  `json:"ddo_code" select:"ddo_code"`
	DdoName         null.String  `json:"ddo_name" select:"ddo_name"`
	OpeningBalance  null.Float64 `json:"opening_balance" select:"opening_balance"`
	CreditAmount    null.Float64 `json:"credit_amount" select:"credit_amount"`
	DebitAmount     null.Float64 `json:"debit_amount" select:"debit_amount"`
	ClosingBalance  null.Float64 `json:"closing_balance" select:"closing_balance"`
}

type ApprAccts struct {
	Be             null.Float64 `json:"be" select:"be"`
	Re             null.Float64 `json:"re" select:"re"`
	Fg             null.Float64 `json:"fg" select:"fg"`
	Hoa            null.String  `json:"hoa" select:"hoa"`
	HoaDescription null.String  `json:"hoa_description" select:"hoa_description"`
}

type ApprAccts2 struct {
	Hoa            null.String  `json:"hoa" select:"hoa"`
	HoaDescription null.String  `json:"hoa_description" select:"hoa_description"`
	Fg             null.Float64 `json:"fg" select:"fg"`
	TotalExp       null.Float64 `json:"total_exp" select:"total_exp"`
}

type ApprAccts3 struct {
	Hoapart               null.String  `json:"hoapart" select:"hoapart"`
	Mh                    null.String  `json:"mh" select:"mh"`
	Mh_description        null.String  `json:"mh_description" select:"mh_description"`
	Smh                   null.String  `json:"smh" select:"smh"`
	Smh_description       null.String  `json:"smh_description" select:"smh_description"`
	Minorhead             null.String  `json:"minorhead" select:"minorhead"`
	Minorhead_description null.String  `json:"minorhead_description" select:"minorhead_description"`
	Subhoa                null.String  `json:"subhoa" select:"subhoa"`
	Subhoa_description    null.String  `json:"subhoa_description" select:"subhoa_description"`
	O                     null.Float64 `json:"o" select:"o"`
	S                     null.Float64 `json:"s" select:"s"`
	R                     null.Float64 `json:"r" select:"r"`
	TotalExp              null.Float64 `json:"total_exp" select:"total_exp"`
}

type BroadsheetRequest struct {
	Type      int64  `form:"type" validate:"required,ValidateHoaType"`
	MonthYear string `form:"month_year" validate:"required"`
	MajorHead string `form:"major_head" validate:"required"`
	DdoCode   string `form:"ddo_code" validate:"required"`
}

type ApprAcctsRequest struct {
	Year string `form:"year" validate:"required"`
}

type RemunerationRequest struct {
	FinancialYear       string    `db:"financial_year" json:"financial-year" validate:"required"`
	RemunerationItem    string    `db:"remuneration_item" json:"remuneration-item" validate:"required"`
	RemunerationType    string    `db:"remuneration_type" json:"remuneration-type" validate:"required"`
	RemunerationRate    float32   `db:"remuneration_rate" json:"remuneration-rate" validate:"required"`
	UpdatedBy           uint64    `db:"updated_by" json:"updated-by" validate:"required"`
	UpdatedDate         time.Time `db:"updated_date" json:"updated-date"`
	Status              bool      `db:"status" json:"status" validate:"required,eq=true"`
	AuthorisationStatus string    `db:"authorisation_status" json:"authorisation-status" validate:"required"`
}
type RemunerationCreationRequest struct {
	FinancialYear         string  `db:"financial_year" json:"financial_year" validate:"required"`
	RemunerationItem      string  `db:"remuneration_item" json:"remuneration_item" validate:"required"`
	RemunerationType      string  `db:"remuneration_type" json:"remuneration_type" validate:"required"`
	RemunerationItemCount float32 `db:"remuneration_item_count" json:"remuneration_item_count" validate:"required"`
}
type RemunerationCreationRequestBulk struct {
	RemunerationCreation []RemunerationCreationRequest `json:"remuneration_creation" validate:"dive"`
}
type RemunerationCreation struct {
	FinancialYear         string  `db:"financial_year" json:"financial_year" validate:"required"`
	RemunerationItem      string  `db:"remuneration_item" json:"remuneration_item" validate:"required"`
	RemunerationType      string  `db:"remuneration_type" json:"remuneration_type" validate:"required"`
	RemunerationRate      float32 `db:"remuneration_rate" json:"remuneration_rate" validate:"required"`
	RemunerationItemCount float32 `db:"remuneration_item_count" json:"remuneration_item_count" validate:"required"`
	ItemRemuneration      float32 `db:"item_remuneration" json:"item_remuneration" validate:"required"`
}
type GetRemYearRequest struct {
	Financial_year string `uri:"financial-year" validate:"required,min=1,max=4"`
	port.MetaDataRequest
}

type UpdateRemRequest struct {
	FinancialYear       string      `json:"financial_year" db:"financial_year" insert:"financial_year" validate:"required"`
	RemunerationItem    string      `json:"remuneration_item" db:"remuneration_item" insert:"remuneration_item" validate:"required"`
	RemunerationType    string      `json:"remuneration_type" db:"remuneration_type" insert:"remuneration_type" validate:"required"`
	RemunerationRate    float32     `json:"remuneration_rate" db:"remuneration_rate" insert:"remuneration_rate"`
	UpdatedBy           uint64      `json:"updated_by" db:"updated_by" insert:"updated_by"`
	UpdatedDate         time.Time   `json:"updated_date" db:"updated_date" insert:"updated_date"`
	AuthorisationStatus string      `json:"authorisation_status" db:"authorisation_status" insert:"authorisation_status"`
	Status              bool        `json:"status" db:"status" insert:"status"`
	ApprovedDate        null.Time   `json:"approved_date" db:"approved_date" insert:"approved_date"`
	ApprovedBy          null.Uint64 `json:"approved_by" db:"approved_by" insert:"approved_by"`
}

type GetRemRequest struct {
	Type int64  `form:"type" validate:"required,validateHoaexeType"`
	Id   string `form:"id" validate:"required"`
}

type GetRemuneration struct {
	FinancialYear    null.String  `db:"financial_year" json:"financial_year" select:"financial_year"`
	RemunerationItem null.String  `db:"remuneration_item" json:"remuneration_item" select:"remuneration_item"`
	RemunerationType null.String  `db:"remuneration_type" json:"remuneration_type" select:"remuneration_type"`
	RemunerationRate null.Float64 `db:"remuneration_rate" json:"remuneration_rate" select:"remuneration_rate"`
	UpdatedBy        null.Uint64  `db:"updated_by" json:"updated_by" select:"updated_by"`
	UpdatedDate      null.Time    `db:"updated_date" json:"updated_date" select:"updated_date"`
	ApprovedBy       null.Uint64  `db:"approved_by" json:"approved_by" select:"approved_by"`
	ApprovedDate     null.Time    `db:"approved_date" json:"approved_date" select:"approved_date"`
}
