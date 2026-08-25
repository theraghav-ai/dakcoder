package domain

import (
	"time"

	"github.com/volatiletech/null/v9"
)

type TransferEntryRequest struct {
	PaoCode            null.String `json:"pao_code" validate:"required"`
	DdoCode            string      `json:"ddo_code" select:"ddo_code"`
	Hoa                string      `json:"hoa" validate:"required"`
	TransferAmount     float64     `json:"transfer_amount" validate:"required"`
	TransferType       string      `json:"transfer_type" validate:"required"`
	CreatedBy          uint64      `json:"created_by" validate:"required"`
	CreatedDate        string      `json:"created_date" validate:"required"`
	TeSourceOfficeType string      `json:"te_source_office_type" select:"te_source_office_type" validate:"required"`
	Remarks            string      `json:"remarks" validate:"required"`
	TransDate          string      `json	:"trans_date" validate:"required"`
}
type TransferEntryInterPaoRequest struct {
	MasterPaoCode      null.String `json:"master_pao_code" validate:"required"`
	PaoCode            null.String `json:"pao_code" validate:"required"`
	DdoCode            string      `json:"ddo_code" select:"ddo_code"`
	Hoa                string      `json:"hoa" validate:"required"`
	TransferAmount     float64     `json:"transfer_amount" validate:"required"`
	TransferType       string      `json:"transfer_type" validate:"required"`
	CreatedBy          uint64      `json:"created_by" validate:"required"`
	CreatedDate        string      `json:"created_date" validate:"required"`
	TeSourceOfficeType string      `json:"te_source_office_type" select:"te_source_office_type" validate:"required"`
	Remarks            string      `json:"remarks" validate:"required"`
}
type TransferEntryDirectRequest struct {
	PaoCode             string    `json:"pao_code" select:"pao_code"`
	DdoCode             string    `json:"ddo_code" select:"ddo_code"`
	Hoa                 string    `json:"hoa" select:"hoa"`
	TransferAmount      float64   `json:"transfer_amount" select:"transfer_amount"`
	TransferType        string    `json:"transfer_type" select:"transfer_type"`
	TransDate           time.Time `json:"trans_date" select:"trans_date"`
	CreatedBy           uint64    `json:"created_by" select:"created_by"`
	CreatedDate         time.Time `json:"created_date" select:"created_date"`
	TransferEntryId     string    `json:"transfer_entry_id" select:"transfer_entry_id"`
	HPfmsGenerationFlag bool      `json:"h_pfms_generation_flag" select:"h_pfms_generation_flag"`
	TeSourceOfficeType  string    `json:"te_source_office_type" select:"te_source_office_type"`
	Remarks             string    `json:"remarks" select:"remarks"`
	VerificationStatus  string    `json:"verification_status" select:"verification_status"`
	VerifiedBy          uint64    `json:"verified_by" select:"verified_by"`
	VerifiedDate        time.Time `json:"verified_date" select:"verified_date"`
	ApproverRemarks     string    `json:"approver_remarks" select:"approver_remarks"`
}
type TransferEntryReport struct {
	PaoCode                null.String  `json:"pao_code"`
	Hoa                    null.String  `json:"hoa"`
	HoaDescription         null.String  `json:"hoa_description"`
	TransferAmount         null.Float64 `json:"transfer_amount"`
	TransferType           null.String  `json:"transfer_type"`
	CreatedBy              null.Uint64  `json:"created_by"`
	CreatedDate            null.Time    `json:"created_date"`
	TransDate              null.Time    `json:"trans_date"`
	DdoCode                null.String  `json:"ddo_code"`
	DdoName                null.String  `json:"ddo_name"`
	TransferEntryID        null.String  `json:"transfer_entry_id"`
	TeSourceOfficeType     null.String  `json:"te_source_office_type"`
	Remarks                null.String  `json:"remarks"`
	VerifiedBy             null.Uint64  `json:"verified_by"`
	VerifiedDate           null.Time    `json:"verified_date"`
	VerificationStatus     null.String  `json:"verification_status"`
	PfmsUniqueID           null.String  `json:"pfms_unique_id"`
	ApproverRemarks        null.String  `json:"approver_remarks"`
	BudgetID               null.String  `json:"budget_id"`
	PfmsSubmissionFlag     null.String  `json:"pfms_submission_flag"`
	PfmsErrorDescription   null.String  `json:"pfms_error_description"`
	HPfmsGenerationFlag    null.Bool    `json:"h_pfms_generation_flag"`
	TENumber               null.String  `json:"te_number"`
	AccountCode            null.String  `db:"account_code"`
	AccountCodeDescription null.String  `db:"account_code_description"`
}

type TransferEntryVerifyRequest struct {
	DdoCode            string    `json:"ddo_code" select:"ddo_code" validate:"required"`
	Hoa                string    `json:"hoa" select:"hoa"`
	TransferAmount     float64   `json:"transfer_amount" select:"transfer_amount" validate:"required"`
	TransferType       string    `json:"transfer_type" select:"transfer_type" validate:"required"`
	CreatedDate        time.Time `json:"created_date" select:"created_date" validate:"required"`
	TransferEntryId    string    `json:"transfer_entry_id" select:"transfer_entry_id" validate:"required"`
	VerificationStatus string    `json:"verification_status" select:"verification_status" validate:"required"`
	VerifiedBy         int64     `json:"verified_by" select:"verified_by" validate:"required"`
	VerifiedDate       time.Time `json:"verified_date" select:"verified_date" validate:"required"`
	ApproverRemarks    string    `json:"approver_remarks" select:"approver_remarks" validate:"required"`
	OfficeId           int64     `json:"office_id" select:"office_id"`
	TransDate          time.Time `json:"trans_date" select:"trans_date" validate:"required"`
}
type InsertedIds struct {
	TransferEntryId string `json:"transfer_entry_id" select:"transfer_entry_id"`
}

type TransferEntryReportRequest struct {
	PaoCode             string    `json:"pao_code"`
	FromDateCreated     time.Time `json:"from_date_created"`
	ToDateCreated       time.Time `json:"to_date_created"`
	FromDateVerified    time.Time `json:"from_date_verified"`
	ToDateVerified      time.Time `json:"to_date_verified"`
	PfmsSubmissionFlag  string    `json:"pfms_submission_flag"`
	HPfmsGenerationFlag *bool     `json:"h_pfms_generation_flag"`
	VerificationStatus  string    `json:"verification_status"`
}

type TransferEntryInterPaoMasterRequest struct {
	PaoCode            string `json:"pao_code" select:"pao_code"`
	FromDate           string `json:"from_date" select:"from_date"`
	ToDate             string `json:"to_date" select:"to_date"`
	VerificationStatus string `json:"verification_status" select:"verification_status"`
}

type TransferEntryRejectRequest struct {
	TransferEntryId    string `json:"transfer_entry_id" validate:"required"`
	VerifiedBy         uint64 `json:"verified_by" validate:"required"`
	VerificationStatus string `json:"verification_status" validate:"required"`
	ApproverRemarks    string `json:"approver_remarks" validate:"required"`
}

type DdoTeRequest struct {
	DdoCode  string `json:"ddo_code" select:"ddo_code"`
	FromDate string `json:"from_date" select:"from_date"`
	ToDate   string `json:"to_date" select:"to_date"`
	Status   string `json:"status" select:"status"`
}
type DdoTeRequestReply struct {
	DdoCode              null.String  `json:"ddo_code" select:"ddo_code"`
	DdoName              null.String  `json:"ddo_name" select:"ddo_name"`
	TransId              null.String  `json:"trans_id" select:"trans_id"`
	Hoa                  null.String  `json:"hoa" select:"hoa"`
	HoaDescription       null.String  `json:"hoa_description" select:"hoa_description"`
	AccountCode          null.String  `json:"account_code" select:"account_code"`
	TransferAmount       null.Float64 `json:"transfer_amount" select:"transfer_amount"`
	TransferType         null.String  `json:"transfer_type" select:"transfer_type"`
	CreatedBy            null.Uint64  `json:"created_by" select:"created_by"`
	CreatedDate          null.Time    `json:"created_date" select:"created_date"`
	Status               null.String  `json:"status" select:"status"`
	ApproverRemarks      null.String  `json:"approver_remarks" select:"approver_remarks"`
	PfmsUniqueID         null.String  `json:"pfms_unique_id" select:"pfms_unique_id"`
	PfmsSubmissionFlag   null.String  `json:"pfms_submission_flag" select:"pfms_submission_flag"`
	PfmsErrorDescription null.String  `json:"pfms_error_description" select:"pfms_error_description"`
	TENumber             null.String  `json:"te_number" select:"te_number"`
	RemarksByCreator     null.String  `json:"remarks_by_creator" select:"remarks_by_creator"`
}
type PaoSubTeRequest struct {
	Type     string `json:"type"`
	PaoCode  string `json:"pao_code" select:"pao_code"`
	FromDate string `json:"from_date" select:"from_date"`
	ToDate   string `json:"to_date" select:"to_date"`
	Status   string `json:"status" select:"status"`
}

type PaoSubTeDetailRequest struct {
	TransId string `json:"trans_id"`
}

type PaoSubTeRequestReply struct {
	PaoCode              null.String `json:"pao_code" select:"pao_code"`
	DdoCode              null.String `json:"ddo_code" select:"ddo_code"`
	DdoName              null.String `json:"ddo_name" select:"ddo_name"`
	TransId              null.String `json:"trans_id" select:"trans_id"`
	CreatedBy            null.Uint64 `json:"created_by" select:"created_by"`
	CreatedDate          null.Time   `json:"created_date" select:"created_date"`
	ApprovedBy           null.Uint64 `json:"approved_by" select:"approved_by"`
	ApprovedDate         null.Time   `json:"approved_date" select:"approved_date"`
	TransDate            null.Time   `json:"trans_date" select:"trans_date"`
	Remarks              null.String `json:"remarks" select:"remarks"`
	Status               null.String `json:"status" select:"status"`
	ApproverRemarks      null.String `json:"approver_remarks" select:"approver_remarks"`
	PfmsUniqueID         null.String `json:"pfms_unique_id" select:"pfms_unique_id"`
	PfmsSubmissionFlag   null.String `json:"pfms_submission_flag" select:"pfms_submission_flag"`
	PfmsErrorDescription null.String `json:"pfms_error_description" select:"pfms_error_description"`
	TENumber             null.String `json:"te_number" select:"te_number"`
	RemarksByCreator     null.String `json:"remarks_by_creator" select:"remarks_by_creator"`
	WorkflowId           null.String `json:"workflow_id" select:"workflow_id"`
}

type PaoSubTeRequestDetailReply struct {
	PaoCode          null.String  `json:"pao_code" select:"pao_code"`
	DdoCode          null.String  `json:"ddo_code" select:"ddo_code"`
	DdoName          null.String  `json:"ddo_name" select:"ddo_name"`
	TransId          null.String  `json:"trans_id" select:"trans_id"`
	Hoa              null.String  `json:"hoa" select:"hoa"`
	HoaDescription   null.String  `json:"hoa_description" select:"hoa_description"`
	AccountCode      null.String  `json:"account_code" select:"account_code"`
	TransferAmount   null.Float64 `json:"transfer_amount" select:"transfer_amount"`
	TransferType     null.String  `json:"transfer_type" select:"transfer_type"`
	CreatedBy        null.Uint64  `json:"created_by" select:"created_by"`
	CreatedDate      null.Time    `json:"created_date" select:"created_date"`
	Status           null.String  `json:"status" select:"status"`
	RemarksByCreator null.String  `json:"remarks_by_creator" select:"remarks_by_creator"`
	Trans_date       null.Time    `json:"trans_date" select:"trans_date"`
}
type SubTeVerified struct {
	PaoCode          string    `json:"pao_code" select:"pao_code" validate:"required"`
	DdoCode          string    `json:"ddo_code" select:"ddo_code" validate:"required"`
	TransId          string    `json:"trans_id" select:"trans_id" validate:"required"`
	Hoa              string    `json:"hoa" select:"hoa" validate:"required"`
	AccountCode      string    `json:"account_code" select:"account_code" validate:"required"`
	TransferAmount   float64   `json:"transfer_amount" select:"transfer_amount" validate:"required"`
	TransferType     string    `json:"transfer_type" select:"transfer_type" validate:"required"`
	CreatedBy        int64     `json:"created_by" select:"created_by" validate:"required"`
	CreatedDate      time.Time `json:"created_date" select:"created_date" validate:"required"`
	Status           string    `json:"status" select:"status" validate:"required"`
	ApprovedBy       int64     `json:"approved_by" select:"approved_by" validate:"required"`
	ApprovedDate     time.Time `json:"approved_date" select:"approved_date" validate:"required"`
	ApproverRemarks  string    `json:"approver_remarks" validate:"required"`
	RemarksByCreator string    `json:"remarks_by_creator" validate:"required"`
	WorkflowId       string    `json:"workflow_id" validate:"required"`
	TransDate        time.Time `json:"trans_date" validate:"required"`
}

type SubTeVerifiedBullk struct {
	SubTes []SubTeVerified `json:"sub_tes" validate:"dive"`
}

type TeData struct {
	TeId    string `db:"te_id" json:"te_id" validate:"required"`
	TeDate  string `db:"te_date" json:"te_date" validate:"required"`
	PaoCode string `db:"pao_code" json:"pao_code" validate:"required"`
	FinYear string `db:"fin_year" json:"fin_year" validate:"required"`
}

type BudgetRequest struct {
	FinancialYear     string  `db:"financial_year" json:"financial_year"`
	OfficeId          int64   `db:"office_id" json:"office_id"`
	Hoa               string  `db:"hoa" json:"hoa"`
	ConsumedAmount    float64 `db:"consumed_amount" json:"consumed_amount"`
	Remarks           string  `db:"remarks" json:"remarks"`
	UpdatedBy         int64   `db:"updated_by" json:"updated_by"`
	TransactionOffice int64   `db:"transaction_office" json:"transaction_office"`
	SourceModule      string  `db:"source_module" json:"source_module"` //added on 16-07-2026
}

type OfficeIdReply struct {
	DdoOfficeId int64 `json:"ddo_office_id" select:"ddo_office_id"`
}

type OfficeIdBRSReply struct {
	PaoCode string `json:"pao_code" select:"pao_code"`
	DdoCode string `json:"ddo_code" select:"ddo_code"`
}
type TransferEntryInterPaoReport struct {
	TransferEntryId null.String `json:"transfer_entry_id" select:"transfer_entry_id"`
	MasterPaoCode   null.String `json:"master_pao_code" select:"master_pao_code"`
	CreatedBy       null.Uint64 `json:"created_by" select:"created_by"`
	CreatedDate     null.Time   `json:"created_date" select:"created_date"`
	Remarks         null.String `json:"remarks" select:"remarks"`
}

type TransferEntryInterPao struct {
	MasterPaoCode        null.String  `json:"master_pao_code" select:"master_pao_code"`
	PaoCode              null.String  `json:"pao_code" select:"pao_code"`
	Hoa                  null.String  `json:"hoa" select:"hoa"`
	HoaDescription       null.String  `json:"hoa_description" select:"hoa_description"`
	DdoName              null.String  `json:"ddo_name" select:"ddo_name"`
	TransferAmount       null.Float64 `json:"transfer_amount" select:"transfer_amount"`
	TransferType         null.String  `json:"transfer_type" select:"transfer_type"`
	CreatedBy            null.Int64   `json:"created_by" select:"created_by"`
	CreatedDate          null.Time    `json:"created_date" select:"created_date"`
	DdoCode              null.String  `json:"ddo_code" select:"ddo_code"`
	TransferEntryId      null.String  `json:"transfer_entry_id" select:"transfer_entry_id"`
	TeSourceOfficeType   null.String  `json:"te_source_office_type" select:"te_source_office_type"`
	Remarks              null.String  `json:"remarks" select:"remarks"`
	VerifiedBy           null.Int64   `json:"verified_by" select:"verified_by"`
	VerifiedDate         null.Time    `json:"verified_date" select:"verified_date"`
	VerificationStatus   null.String  `json:"verification_status" select:"verification_status"`
	ApproverRemarks      null.String  `json:"approver_remarks" select:"approver_remarks"`
	BudgetId             null.String  `json:"budget_id" select:"budget_id"`
	HPfmsGenerationFlag  null.Bool    `json:"h_pfms_generation_flag" select:"h_pfms_generation_flag"`
	PfmsUniqueId         null.String  `json:"pfms_unique_id" select:"pfms_unique_id"`
	PfmsSubmissionFlag   null.String  `json:"pfms_submission_flag" select:"pfms_submission_flag"`
	PfmsErrorDescription null.String  `json:"pfms_error_description" select:"pfms_error_description"`
}

type TransferEntryPFMSResetRequest struct {
	PfmsUniqueId string `json:"pfms_unique_id" uri:"pfms-unique-id" validate:"required"`
}
