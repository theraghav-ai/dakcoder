package domain

import (
	"time"

	"github.com/volatiletech/null/v9"
)

type CbData struct {
	OfficeId string `db:"office_id" json:"office_id" validate:"required,len=6,numeric"`   // e.g., "102604"
	CbDate   string `db:"cb_date" json:"cb_date" validate:"required,datetime=2006-01-02"` // e.g., "2025-03-10"
	PaoCode  string `db:"pao_code" json:"pao_code" validate:"required,len=6,numeric"`     // e.g., "078109"
	FinYear  string `db:"fin_year" json:"fin_year" validate:"required,len=4,numeric"`     // e.g., "2025"
}

type CbDatas struct {
	Cbds []CbData `json:"CbData,omitempty"`
}

type OfficeDetails struct {
	Ddo_office_id null.Int64  `json:"ddo_office_id" select:"ddo_office_id"`
	Ddo_name      null.String `json:"ddo_name" select:"ddo_name"`
	PaoCode       null.String `json:"pao_code" select:"pao_code"`
	DdoCode       null.String `json:"ddo_code" select:"ddo_code"`
}
type OfficeID struct {
	Ddo_office_id null.Int64 `json:"ddo_office_id" select:"ddo_office_id"`
}

type Ddo struct {
	DdoCode     null.String `json:"ddo_code" select:"ddo_code"`
	DdoOfficeId null.Int64  `json:"ddo_office_id" select:"ddo_office_id"`
	DdoName     null.String `json:"ddo_name" select:"ddo_name"`
	DdoType     null.String `json:"ddo_type" select:"ddo_type"`
}

type Pao struct {
	PaoCode     null.String `json:"pao_code" select:"pao_code"`
	PaoOfficeId null.Int64  `json:"pao_office_id" select:"pao_office_id"`
	PaoName     null.String `json:"pao_name" select:"pao_name"`
}

type InterPao struct {
	PaoCode     null.String `json:"pao_code" select:"pao_code"`
	PaoOfficeId null.Int64  `json:"pao_office_id" select:"pao_office_id"`
	PaoName     null.String `json:"pao_name" select:"pao_name"`
	DdoCode     null.String `json:"ddo_code" select:"ddo_code"`
	DdoOfficeId null.Int64  `json:"ddo_office_id" select:"ddo_office_id"`
	DdoName     null.String `json:"ddo_name" select:"ddo_name"`
}

type OfficeNameRequest struct {
	Id int64 `json:"id" validate:"required,customIdValidator"`
}
type DdoListRequest struct {
	PaoCode string `json:"pao_code" select:"pao_code"`
	Date    string `json:"date" select:"date"`
}
type DdoListRequestUpdate struct {
	PaoCode  string `json:"pao_code" select:"pao_code"`
	FromDate string `json:"fromdate" select:"fromdate"`
	ToDate   string `json:"todate" select:"todate"`
}
type DdosRequest struct {
	PaoCode  string `json:"pao_code" select:"pao_code"`
	OfficeId string `json:"office_id" select:"office_id"`
}
type PraosRequest struct {
	PraoCode string `json:"prao_code" select:"prao_code"`
}
type PfmsStatus struct {
	DdoCode                  null.String `json:"ddo_code" select:"ddo_code"`
	DdoName                  null.String `json:"ddo_name" select:"ddo_name"`
	Date                     null.String `json:"date" select:"date"`
	H_cash_book_receive_flag null.Bool   `json:"h_cash_book_receive_flag" select:"h_cash_book_receive_flag"`
	H_verification_flag      null.Bool   `json:"h_verification_flag" select:"h_verification_flag"`
	H_pfms_generation_flag   null.Bool   `json:"h_pfms_generation_flag" select:"h_pfms_generation_flag"`
}
type PfmsStatusMonthly struct {
	DdoCode                     null.String `json:"ddo_code" select:"ddo_code"`
	DdoName                     null.String `json:"ddo_name" select:"ddo_name"`
	Period                      null.String
	H_cash_account_receive_flag null.Bool `json:"cashaccount_receive_status" select:"h_cash_account_receive_flag"`
	H_verification_flag         null.Bool `json:"verification_status" select:"h_verification_flag"`
}
type DdoDetailRequest struct {
	DdoCode string `json:"ddo_code" select:"ddo_code"`
	Date    string `json:"date" select:"date"`
}

type DdoDetailMonthlyRequest struct {
	DdoCode string `json:"ddo_code" select:"ddo_code"`
	Period  string `json:"period" select:"period"`
}
type DdoDetail struct {
	DdoCode        null.String  `json:"ddo_code" select:"ddo_code"`
	DdoOfficeName  null.String  `json:"ddo_office_name" select:"ddo_office_name"`
	BusinessDate   null.Time    `json:"business_date" select:"business_date"`
	ClosingBal     null.Float64 `json:"closing_bal" select:"closing_bal"`
	OpeningBal     null.Float64 `json:"opening_bal" select:"opening_bal"`
	Hoa            null.String  `json:"hoa" select:"hoa"`
	HoaDescription null.String  `json:"hoa_description" select:"hoa_description"`
	Payment        null.Float64 `json:"payment" select:"payment"`
	Receipt        null.Float64 `json:"receipt" select:"receipt"`
	AccountArray   []CodeArray  `json:"account_array" select:"account_array"`
	CreatedDate    time.Time    `json:"created_date" select:"created_date"`
}
type DdoDetailMonthly struct {
	DdoCode        null.String  `json:"ddo_code" select:"ddo_code"`
	OfficeName     null.String  `json:"office_name" select:"office_name"`
	OpeningBal     null.Float64 `json:"opening_bal" select:"opening_bal"`
	ClosingBal     null.Float64 `json:"closing_bal" select:"closing_bal"`
	Period         null.String  `json:"period" select:"period"`
	Hoa            null.String  `json:"hoa" select:"hoa"`
	HoaDescription null.String  `json:"hoa_description" select:"hoa_description"`
	Payment        null.Float64 `json:"payment" select:"payment"`
	Receipt        null.Float64 `json:"receipt" select:"receipt"`
	TePayment      null.Float64 `json:"te_payment" select:"te_payment"`
	TeReceipt      null.Float64 `json:"te_receipt" select:"te_receipt"`
	AccountArray   []CodeArray  `json:"account_array" select:"account_array"`
}
type DdoDetailMonthlyEmpty struct {
	DdoCode    null.String  `json:"ddo_code" select:"ddo_code"`
	OfficeName null.String  `json:"office_name" select:"office_name"`
	OpeningBal null.Float64 `json:"opening_bal" select:"opening_bal"`
	ClosingBal null.Float64 `json:"closing_bal" select:"closing_bal"`
	Period     null.String  `json:"period" select:"period"`
}
type CodeArray struct {
	AccountCode            null.String  `json:"account_code"`
	AccountCodeDescription null.String  `json:"account_code_description"`
	Receipt                null.Float64 `json:"receipt"`
	Payment                null.Float64 `json:"payment"`
}

type Xml struct {
	UniqueIdentifier null.String `json:"uniqueIdentifier"`
	Pfms             null.String `json:"pfms"`
}
type PfmsPendingRequest struct {
	PaoCode  string `json:"pao_code" select:"pao_code"`
	FromDate string `json:"from_date" select:"from_date"`
	ToDate   string `json:"to_date" select:"to_date"`
}

type PfmsSubmissionStatusListRequest struct {
	PaoCode  string `json:"pao_code" select:"pao_code"`
	FromDate string `json:"from_date" select:"from_date"`
	ToDate   string `json:"to_date" select:"to_date"`
	Status   string `json:"status" select:"status"`
}
type PfmsXmlSubmissionPendingRequest struct {
	FinYear string `json:"fin-year"`
}

type PfmsPending struct {
	PfmsDdoId                null.String `json:"pfms_ddo_id" select:"pfms_ddo_id"`
	DdoOfficeId              null.String `json:"ddo_office_id" select:"ddo_office_id"`
	DdoName                  null.String `json:"ddo_name" select:"ddo_name"`
	DdoCode                  null.String `json:"ddo_code" select:"ddo_code"`
	BusinessDate             null.Time   `json:"business_date" select:"business_date"`
	H_cash_book_receive_flag null.Bool   `json:"cashbook_receive_status" select:"h_cash_book_receive_flag"`
	H_verification_flag      null.Bool   `json:"verification_status" select:"h_verification_flag"`
	H_pfms_generation_flag   null.Bool   `json:"pfms_generation_status" select:"h_pfms_generation_flag"`
}
type LastCBCheck struct {
	DdoName                  null.String  `json:"ddo_name" select:"ddo_name"`
	DdoCode                  null.String  `json:"ddo_code" select:"ddo_code"`
	BusinessDate             null.Time    `json:"business_date" select:"business_date"`
	ClosingBal               null.Float64 `json:"closing_bal" select:"closing_bal"`
	H_cash_book_receive_flag null.Bool    `json:"cashbook_receive_status" select:"h_cash_book_receive_flag"`
	H_verification_flag      null.Bool    `json:"verification_status" select:"h_verification_flag"`
	H_pfms_generation_flag   null.Bool    `json:"pfms_generation_status" select:"h_pfms_generation_flag"`
	Pfms_submission_flag     null.String  `json:"pfms_submission_flag" select:"pfms_submission_flag"`
}
type PfmsXmlPending struct {
	PaoCode              null.String `json:"pao_code" select:"pao_code"`
	DdoCode              null.String `json:"ddo_code" select:"ddo_code"`
	DdoName              null.String `json:"ddo_name" select:"ddo_name"`
	BusinessDate         null.Time   `json:"business_date" select:"business_date"`
	HPfmsGenerationFlag  null.Bool   `json:"h_pfms_generation_flag" select:"h_pfms_generation_flag"`
	PfmsUniqueId         null.String `json:"pfms_unique_id" select:"pfms_unique_id"`
	PfmsSubmissionFlag   null.String `json:"pfms_submission_flag" select:"pfms_submission_flag"`
	PfmsErrorDescription null.String `json:"Pfms_error_description" select:"pfms_error_description"`
	TENumber             null.String `json:"te_number" select:"te_number"`
}
type PfmsSubmissionPending struct {
	PfmsUniqueId null.String `json:"pfms_unique_id" select:"pfms_unique_id"`
}

type PfmsXmlTePending struct {
	PaoCode              null.String `json:"pao_code" select:"pao_code"`
	TransferEntryId      null.String `json:"transfer_entry_id" select:"transfer_entry_id"`
	BusinessDate         null.Time   `json:"business_date" select:"business_date"`
	HPfmsGenerationFlag  null.Bool   `json:"h_pfms_generation_flag" select:"h_pfms_generation_flag"`
	PfmsUniqueId         null.String `json:"pfms_unique_id" select:"pfms_unique_id"`
	PfmsSubmissionFlag   null.String `json:"pfms_submission_flag" select:"pfms_submission_flag"`
	PfmsErrorDescription null.String `json:"Pfms_error_description" select:"pfms_error_description"`
	TENumber             null.String `json:"te_number" select:"te_number"`
}

type PfmsVerified struct {
	DdoCode            null.String  `json:"ddo_code" select:"ddo_code" validate:"required"`
	BusinessDate       time.Time    `json:"business_date" select:"business_date" validate:"required"`
	ClosingBal         null.Float64 `json:"closing_bal" select:"closing_bal" validate:"required"`
	OpeningBal         null.Float64 `json:"opening_bal" select:"opening_bal" validate:"required"`
	VerifiedBy         null.Uint64  `json:"verified_by" select:"verified_by" validate:"required"`
	VerificationStatus null.String  `json:"h_verification" select:"h_verification_flag" validate:"required"`
	Hoa                null.String  `json:"hoa" select:"hoa" validate:"required"`
	Payment            null.Float64 `json:"payment" select:"payment" validate:"required"`
	Receipt            null.Float64 `json:"receipt" select:"receipt" validate:"required"`
	AccountCodeArray   []CodeArray  `json:"account_array" select:"account_array" validate:"dive"`
}
type PfmsVerifiedMonthly struct {
	DdoCode            string       `json:"ddo_code" select:"ddo_code" validate:"required"`
	Period             string       `json:"period" select:"period" validate:"required"`
	ClosingBal         null.Float64 `json:"closing_bal" select:"closing_bal" update:"closing_bal" validate:"required"`
	OpeningBal         null.Float64 `json:"opening_bal" select:"opening_bal" update:"opening_bal" validate:"required"`
	VerifiedBy         uint64       `json:"verified_by" select:"verified_by" validate:"required"`
	VerificationStatus string       `json:"h_verification" select:"h_verification_flag" validate:"required"`
	Hoa                string       `json:"hoa" select:"hoa" validate:"required"`
	Payment            null.Float64 `json:"payment" select:"payment" validate:"required"`
	Receipt            null.Float64 `json:"receipt" select:"receipt" validate:"required"`
	TePayment          null.Float64 `json:"te_payment" select:"te_payment" validate:"required"`
	TeReceipt          null.Float64 `json:"te_receipt" select:"te_receipt" validate:"required"`
	AccountCodeArray   []CodeArray  `json:"account_array" select:"account_array" validate:"dive"`
}
type DdoListRequestMonthly struct {
	PaoCode string `json:"pao_code" select:"pao_code"`
	Period  string `json:"period" select:"period"`
}

type DdoListMonthlyQuery struct {
	OfficeId string `json:"office_id" select:"office_id"`
	Period   string `json:"period" select:"period"`	
}

type UpdatePfmsSubmissionStatusReq struct {
	UniqueIdentifier string `json:"unique-identifier"`
	Status           string `json:"status"`
	ErrorDescription string `json:"error_description"`
}
type PraoAccountSubmissionRequest struct {
	PaoCode string `form:"pao_code" validate:"required,min=0" example:"0"`
	Period  string `form:"period" validate:"required"`
}
type PaoPraoAccount struct {
	PaoCode      null.String  `json:"pao_code" select:"pao_code" insert:"pao_code"`
	PaoName      null.String  `json:"pao_name" select:"pao_name" insert:"pao_name"`
	Hoa          null.String  `json:"hoa" select:"hoa" insert:"hoa"`
	Period       null.String  `json:"period" select:"period" insert:"period"`
	TotalPayment null.Float64 `json:"total_payment" select:"total_payment" insert:"total_payment"`
	TotalReceipt null.Float64 `json:"total_receipt" select:"total_receipt" insert:"total_receipt"`
	DdoArray     []DdoDetails `json:"ddo_array" select:"ddo_array" insert:"ddo_array"`
}
type PaoPraoAccountReply struct {
	PaoCode        null.String  `json:"pao_code" select:"pao_code" insert:"pao_code"`
	PaoName        null.String  `json:"pao_name" select:"pao_name" insert:"pao_name"`
	Hoa            null.String  `json:"hoa" select:"hoa" insert:"hoa"`
	HoaDescription null.String  `json:"hoa_description" select:"hoa_description"`
	Period         null.String  `json:"period" select:"period" insert:"period"`
	TotalPayment   null.Float64 `json:"total_payment" select:"total_payment" insert:"total_payment"`
	TotalReceipt   null.Float64 `json:"total_receipt" select:"total_receipt" insert:"total_receipt"`
	DdoArray       []DdoDetails `json:"ddo_array" select:"ddo_array" insert:"ddo_array"`
}

type DdoDetails struct {
	DdoCode   null.String  `json:"ddo_code" select:"ddo_code"`
	Receipt   null.Float64 `json:"receipt" select:"receipt"`
	Payment   null.Float64 `json:"payment" select:"payment"`
	TeReceipt null.Float64 `json:"te_receipt" select:"te_receipt"`
	TePayment null.Float64 `json:"te_payment" select:"te_payment"`
}
type PraoAccountSubmissionStatus struct {
	PaoCode                       null.String `json:"pao_code" select:"pao_code"`
	Period                        null.String `json:"period" select:"period"`
	AccountSubmissionToPraoStatus null.String `json:"account_submissionto_prao_status" select:"account_submissionto_prao_status"`
}

type AccountSubmissionStatusList struct {
	PaoCode                       null.String `json:"pao_code" select:"pao_code"`
	PaoName                       null.String `json:"pao_name" select:"pao_name"`
	Period                        null.String `json:"period" select:"period"`
	AccountSubmissionToPraoStatus null.String `json:"account_submissionto_prao_status" select:"account_submissionto_prao_status"`
}

// domain/consolidated_cash_account.go

type ConsolidatedAccountCodeDetail struct {
	AccountCode            string  `json:"account_code"`
	AccountCodeDescription string  `json:"account_code_description"`
	Receipt                float64 `json:"receipt"`
	Payment                float64 `json:"payment"`
}

type ConsolidatedHoaDetail struct {
	Hoa            string                          `json:"hoa"`
	HoaDescription string                          `json:"hoa_description"`
	HoaReflection  string                          `json:"hoa_reflection,omitempty"`
	PositiveSide   string                          `json:"positive_side,omitempty"`
	Part           string                          `json:"part,omitempty"`
	Receipt        float64                         `json:"receipt"`
	Payment        float64                         `json:"payment"`
	AccountArray   []ConsolidatedAccountCodeDetail `json:"account_array"`
}

type ConsolidatedCashAccount struct {
	PaoOfficeId       int64                   `json:"pao_office_id"`
	PaoName           string                  `json:"pao_name"`
	CashAccountPeriod string                  `json:"cash_account_period"`
	HoaDetails        []ConsolidatedHoaDetail `json:"hoa_details"`
}

// type TransferEntryAccountDetailsList struct {
// 	DDOCode        null.String  `json:"ddo_code" select:"ddo_code"`
// 	GrantNo        null.String  `json:"grant_no" select:"grant_no"`
// 	FunctionalHead null.String  `json:"functional_head" select:"functional_head"`
// 	ObjectHead     null.String  `json:"object_head" select:"object_head"`
// 	Category       null.String  `json:"category" select:"category"`
// 	Sign           null.String  `json:"sign" select:"sign"`
// 	Amount         null.Float64 `json:"amount" select:"amount"`
// 	Remarks        null.String  `json:"remarks" select:"remarks"`
// 	Transfer       null.String  `json:"transfer" select:"transfer"`
// }
// type InstrumentDetailsList struct {
// 	InstrumentDDOCode       null.String `json:"instrument_ddocode" select:"instrument_ddocode"`
// 	InstrumentNumer         null.String `json:"instrument_number" select:"instrument_number"`
// 	InstrumentFinancialYear int64       `json:"instrument_financial_year" select:"instrument_financial_year"`
// 	InstrumentDate          null.String `json:"instrument_date" select:"instrument_date"`
// }
// type TransferEntryDataList struct {
// 	InstrumentType                 null.String                       `json:"instrument_type" select:"instrument_type"`
// 	Remarks                        null.String                       `json:"remarks" select:"remarks"`
// 	TEDate                         null.String                       `json:"te_date" select:"te_date"`
// 	InstrumentDetails              InstrumentDetailsList             `json:"instrument_details" select:"instrument_details"`
// 	TransferEntryAccountingDetails []TransferEntryAccountDetailsList `json:"transfer_entry_accounting_details" select:"transfer_entry_accounting_details"`
// }

//	type TransferEntryDetailsList struct {
//		UniqueIdentifier     null.String           `json:"unique_identifier" select:"unique_identifier"`
//		RequestSource        string                `json:"request_source" select:"request_source"`
//		PaoCode              null.String           `json:"pao_code" select:"pao_code"`
//		FinancialYEar        int64                 `json:"financial_year" select:"financial_year"`
//		TransferEntryData    TransferEntryDataList `json:"transfer_entry_data" select:"transfer_entry_data"`
//	}
//
// Payload represents the top-level JSON object
type Payload struct {
	RequestPayload RequestPayload `json:"requestPayload"`
}

// RequestPayload holds the array of transfer entry details
type RequestPayload struct {
	TransferEntryDetails []TransferEntryDetail `json:"TransferEntryDetails"`
}

// TransferEntryDetail represents an individual transfer entry
type TransferEntryDetail struct {
	UniqueIdentifier  string            `json:"UniqueIdentifier"`
	RequestSource     string            `json:"RequestSource"`
	PaoCode           string            `json:"PaoCode"`
	FinancialYear     int               `json:"FinancialYear"`
	TransferEntryData TransferEntryData `json:"TransferEntryData"`
}

// TransferEntryData contains details about the transfer entry
type TransferEntryData struct {
	InstrumentType                 string                           `json:"InstrumentType"`
	Remarks                        string                           `json:"Remarks"`
	TEDate                         string                           `json:"TEDate"`
	TransferEntryAccountingDetails []TransferEntryAccountingDetails `json:"TransferEntryAccountingDetails"`
}
type TransferEntryAccountingDetails struct {
	DDOCode        string  `json:"DDOCode"`
	GrantNo        string  `json:"GrantNo"`
	FunctionalHead string  `json:"FunctionalHead"`
	ObjectHead     string  `json:"ObjectHead"`
	Category       string  `json:"Category"`
	Sign           string  `json:"Sign"`
	Amount         float64 `json:"Amount"`
	Remarks        string  `json:"Remarks"`
	Transfer       string  `json:"Transfer"`
}

// TransferEntryAccountingDetail represents accounting details for a transfer entry
type TransferEntryAccountingDetail struct {
	DdoOfficeID    null.String  `json:"ddo_office_id"`
	FunctionalHead null.String  `json:"functional_head"`
	ObjectHead     null.String  `json:"object_head"`
	GrantNo        null.String  `json:"grant_no"`
	Category       null.String  `json:"category"`
	Remarks        null.String  `json:"remarks"`
	ReceiptPayment null.String  `json:"receipt_payment"`
	Sign           null.String  `json:"sign"`
	Amount         null.Float64 `json:"amount"`
}

type DdoMasterInput struct {
	PaoCode     string `json:"pao_code" select:"pao_code"`
	DdoCode     string `json:"ddo_code" select:"ddo_code"`
	PaoOfficeId int64  `json:"pao_office_id" select:"pao_office_id"`
	DdoOfficeId int64  `json:"ddo_office_id" select:"ddo_office_id"`
	DdoName     string `json:"ddo_name" select:"ddo_name"`
	PaoName     string `json:"pao_name" select:"pao_name"`
	DdoType     string `json:"ddo_type" select:"ddo_type"`
	GstNumber   string `json:"gst_number" select:"gst_number"`
}
type SOOfficeDetails struct {
	OfficeId          null.Int64  `json:"office_id" select:"office_id"`
	OfficeTypeCode    null.String `json:"office_type_code" select:"office_type_code"`
	ReportingOfficeId null.Int64  `json:"reporting_office_id" select:"reporting_office_id"`
	DdoCode           null.String `json:"ddo_code" select:"ddo_code"`
	DdoName           null.String `json:"ddo_name" select:"ddo_name"`
	PaoOfficeId       null.Int64  `json:"pao_office_id" select:"pao_office_id"`
	PaoCode           null.String `json:"pao_code" select:"pao_code"`
	PaoName           null.String `json:"pao_name" select:"pao_name"`
}
type ClosingBalance struct {
	OfficeId   string     `json:"office_id"`
	CbDate     string     `json:"cb_date"`
	ClosingBal null.Int64 `json:"closing_bal"` // Rounded to nearest integer
}
type HoaRequest1 struct {
	Hoa string `form:"hoa" validate:"required"`
}
type AcccountHoaonlygetMapping struct {
	Hoa            null.String `db:"hoa" json:"hoa" select:"hoa" insert:"hoa"`
	HoaDescription null.String `db:"hoa_description" json:"hoa_description" select:"hoa_description" insert:"hoa_description"`
}
type AccountCodeRequest struct {
	AccountCode string `form:"account-code" validate:"required,max=20"`
}
type AcccountCodegetMapping struct {
	AccountCode            null.String `db:"account_code" json:"account_code" select:"account_code" insert:"account_code"`
	AccountCodeDescription null.String `db:"account_code_description" json:"account_code_description" select:"account_code_description" insert:"account_code_description"`
}
type CashbookPfmsStatusRequest struct {
	OfficeId     int64
	CashbookDate time.Time
}
type CashbookReversionListRequest struct {
	DdoCode  string
	FromDate time.Time
}
type CashbookSubmissionStatus struct {
	PfmsSubmissionFlag null.Bool `json:"pfms_submission_flag" select:"pfms_submission_flag"`
}

// ConsolidatedHeadDetail represents a single account code row after aggregation
type ConsolidatedHeadDetail struct {
	AccountCode            string  `json:"account_code" db:"account_code"`
	AccountCodeDescription string  `json:"account_code_description" db:"account_code_description"`
	Part                   string  `json:"part" db:"part"`
	CreditDebit            string  `json:"credit_debit" db:"credit_debit"`
	Hoa                    string  `json:"hoa" db:"hoa"`
	HoaDescription         string  `json:"hoa_description" db:"hoa_description"`
	HoaReflection          string  `json:"hoa_reflection" db:"hoa_reflection"`
	PositiveSide           string  `json:"positive_side" db:"positive_side"`
	GrandTotal             float64 `json:"Grand_total" db:"grand_total"`
}

// ConsolidatedCashAccount is the full response payload
type ConsolidatedCashAccount1 struct {
	PaoOfficeId       int64                    `json:"pao_office_id"`
	PaoName           string                   `json:"pao_name"`
	CashAccountPeriod string                   `json:"cash_account_period"`
	HeadDetails       []ConsolidatedHeadDetail `json:"head_details"`
}

// changes done on 23-03-2026 for cashbook reversion and -ve entry postage in PAO sir

// ReversionRow — used for bulk insert
type ReversionRow struct {
	RequestOfficeID          int
	RequestEmployeeID        int
	DdoCode                  string
	FromDate                 time.Time
	Remark                   string
	BusinessDate             time.Time
	PfmsReversalType         string
	OriginalPfmsUID          string
	OriginalSubmissionStatus string
	OriginalTeNumber         string
	PfmsNegativePosted       string
	DbDeletionStatus         string
}

// ReversionRecord — used for GET API response

type ReversionRecord struct {
	ReversionID              int       `json:"reversion_id"`
	DdoCode                  string    `json:"ddo_code"`
	DdoName                  string    `json:"ddo_name"`
	DdoOfficeID              int64     `json:"ddo_office_id"`
	FromDate                 time.Time `json:"from_date"`
	RequestDate              time.Time `json:"request_date"`
	BusinessDate             time.Time `json:"business_date"`
	PfmsReversalType         string    `json:"pfms_reversal_type"`
	OriginalPfmsUID          string    `json:"original_pfms_uid"`
	OriginalSubmissionStatus string    `json:"original_submission_status"`
	OriginalTeNumber         string    `json:"original_te_number"`
	ReversalPfmsUID          string    `json:"reversal_pfms_uid"`
	PfmsNegativePosted       string    `json:"pfms_negative_posted"`
	DbDeletionStatus         string    `json:"db_deletion_status"`
	CurrentStatus            string    `json:"current_status"`
	CurrentTeNumber          string    `json:"current_te_number"`
	ReversalSubmissionStatus string    `json:"reversal_submission_status"` // new
	ReversalTeNumber         string    `json:"reversal_te_number"`         // new
	RequestEmployeeID        int       `json:"request_employee_id"`
	Remarks                  string    `json:"remarks"`
}

// PfmsSubmissionRow — maps to pfms_submission table row
type PfmsSubmissionRow struct {
	PfmsUniqueId       string    `db:"pfms_unique_id"`
	PfmsSubmissionType string    `db:"pfms_submission_type"`
	TeRequest          []TeData  `db:"te_request"`
	BusinessDate       time.Time `db:"business_date"`
	SubmissionDate     time.Time `db:"submission_date"`
	SubmissionData     Payload   `db:"submission_data"`
	SubmissionStatus   string    `db:"submission_status"`
	ErrorDescription   string    `db:"error_description"`
	TeNumber           string    `db:"te_number"`
}

// Add this new struct for reversal request
type TeReversalRequest struct {
	PfmsUniqueId      string `json:"pfms_unique_id" validate:"required"`
	RequestEmployeeID int    `json:"request_employee_id" validate:"required"`
	Remark            string `json:"remark"`
}

// Add this for GET response
type PfmsTeReversible struct {
	PfmsUniqueId     string    `json:"pfms_unique_id"`
	TeNumber         string    `json:"te_number"`
	BusinessDate     time.Time `json:"business_date"`
	SubmissionDate   time.Time `json:"submission_date"`
	SubmissionStatus string    `json:"submission_status"`
}

type DdoPfmsStatus struct {
	DdoCode                 string      `json:"ddo_code" db:"ddo_code"`
	DdoName                 null.String `json:"ddo_name" db:"ddo_name"`
	DdoType                 null.String `json:"ddo_type" db:"ddo_type"`
	OfficeId                null.Int64  `json:"office_id" db:"office_id"`
	Period                  null.String `json:"period" db:"period"`
	HCashAccountReceiveFlag bool        `json:"h_cash_account_receive_flag" db:"h_cash_account_receive_flag"`
	HVerificationFlag       bool        `json:"h_verification_flag" db:"h_verification_flag"`
}

type PaoPraoStatus struct {
	PaoCode   string `json:"pao_code" db:"pao_code"`
	Period    string `json:"period" db:"period"`
	Submitted bool   `json:"submitted" db:"submitted"`
}
