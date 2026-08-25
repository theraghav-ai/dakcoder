package domain

import (
	"time"

	"github.com/volatiletech/null/v9"
)

type ObjectionRequest struct {
	PaoCode     string                   `json:"pao_code" select:"pao_code" insert:"pao_code"  validate:"required,validatePaocode"`
	DdoCode     string                   `json:"ddo_code" select:"ddo_code" insert:"ddo_code"  validate:"required,validateDdocode"`
	Description string                   `json:"description" select:"description" insert:"description" validate:"required"`
	ObjectionId string                   `json:"objection_id" select:"objection_id" insert:"objection_id"`
	CreatedBy   uint64                   `json:"created_by" select:"created_by" insert:"created_by" validate:"required"`
	CreatedDate time.Time                `json:"created_date" select:"created_date" insert:"created_date"`
	Remarks     []ObjectionRemarkRequest `json:"remarks" select:"remarks" insert:"remarks"`
	StatusFlag  string                   `json:"status_flag" select:"status_flag" insert:"status_flag" validate:"required"`
}

type ObjectionRemarkRequest struct {
	Data              string    `json:"data"`
	CommentedBy       uint64    `json:"commented_by"`
	CommentedDate     time.Time `json:"commented_date"`
	CommentedOfficeId uint64    `json:"commented_office_id"`
	Filepath          string    `json:"filepath"`
	Sender            string    `json:"sender"`
	// EcmsTransactionId string    `json:"ecms_transaction_id,omitempty"`
	// EcmsServiceName   string    `json:"ecms_service_name,omitempty"`
}

type Objection struct {
	PaoCode         null.String       `json:"pao_code" select:"pao_code" insert:"pao_code"`
	DdoCode         null.String       `json:"ddo_code" select:"ddo_code" insert:"ddo_code"`
	Description     null.String       `json:"description" select:"description" insert:"description"`
	ObjectionId     null.String       `json:"objection_id" select:"objection_id"`
	CreatedBy       null.Uint64       `json:"created_by" select:"created_by" insert:"created_by"`
	CreatedDate     null.Time         `json:"created_date" select:"created_date" insert:"created_date"`
	Remarks         []ObjectionRemark `json:"remarks" select:"remarks" insert:"remarks"`
	StatusFlag      null.String       `json:"status_flag" select:"status_flag" insert:"status_flag"`
	LastUpdatedBy   null.Uint64       `json:"last_updated_by" select:"last_updated_by"`
	LastUpdatedDate null.Time         `json:"last_updated_date" select:"last_updated_date"`
}
type ObjectionReply struct {
	PaoCode         null.String       `json:"pao_code" select:"pao_code"`
	DdoCode         null.String       `json:"ddo_code" select:"ddo_code"`
	DdoName         null.String       `json:"ddo_name" select:"ddo_name"`
	Description     null.String       `json:"description" select:"description"`
	ObjectionId     null.String       `json:"objection_id" select:"objection_id"`
	CreatedBy       null.Uint64       `json:"created_by" select:"created_by"`
	CreatedDate     null.Time         `json:"created_date" select:"created_date"`
	Remarks         []ObjectionRemark `json:"remarks" select:"remarks"`
	StatusFlag      null.String       `json:"status_flag" select:"status_flag"`
	LastUpdatedBy   null.Uint64       `json:"last_updated_by" select:"last_updated_by"`
	LastUpdatedDate null.Time         `json:"last_updated_date" select:"last_updated_date"`
}
type ObjectionReplyWithLatestRemark struct {
	PaoCode         null.String `json:"pao_code" select:"pao_code"`
	DdoCode         null.String `json:"ddo_code" select:"ddo_code"`
	DdoName         null.String `json:"ddo_name" select:"ddo_name"`
	Description     null.String `json:"description" select:"description"`
	ObjectionId     null.String `json:"objection_id" select:"objection_id"`
	CreatedBy       null.Uint64 `json:"created_by" select:"created_by"`
	CreatedDate     null.Time   `json:"created_date" select:"created_date"`
	StatusFlag      null.String `json:"status_flag" select:"status_flag"`
	LastUpdatedBy   null.Uint64 `json:"last_updated_by" select:"last_updated_by"`
	LastUpdatedDate null.Time   `json:"last_updated_date" select:"last_updated_date"`
	LatestRemark    null.String `json:"latest_remark" select:"latest_remark"`
}

type ObjectionRemark struct {
	Data              null.String `json:"data"`
	CommentedBy       null.Uint64 `json:"commented_by"`
	CommentedDate     null.Time   `json:"commented_date"`
	CommentedOfficeId null.Uint64 `json:"commented_office_id"`
	Filepath          null.String `json:"filepath"`
	Sender            null.String `json:"sender"`
	// EcmsTransactionId null.String `json:"ecms_transaction_id,omitempty"`
	// EcmsServiceName   null.String `json:"ecms_service_name,omitempty"`
}

type Objectioncomment struct {
	ObjectionId string          `json:"objection_id" select:"objection_id" insert:"objection_id" validate:"required"`
	Remark      ObjectionRemark `json:"remarks" select:"remarks" insert:"remarks" validate:"dive"`
	UpdatedBy   uint64          `json:"updated_by" select:"last_updated_by" insert:"last_updated_by"`
	UpdatedDate time.Time       `json:"updated_date" select:"last_updated_date" insert:"last_updated_date"`
	StatusFlag  string          `json:"status_flag" select:"status_flag" insert:"status_flag"`
}

type ObjectionClosure struct {
	ObjectionId   string          `json:"objection_id" select:"objection_id" insert:"objection_id"`
	StatusFlag    string          `json:"status_flag" select:"status_flag" insert:"status_flag"`
	ClosureRemark ObjectionRemark `json:"remarks" select:"remarks" insert:"remarks"`
	ClosedBy      uint64          `json:"closed_by" select:"last_updated_by" insert:"last_updated_by"`
	ClosedDate    time.Time       `json:"closed_date" select:"last_updated_date" insert:"last_updated_date"`
}
type ObjectionPrao struct {
	PraoCode        null.String       `json:"prao_code" select:"prao_code" insert:"prao_code"`
	PaoCode         null.String       `json:"pao_code" select:"pao_code" insert:"pao_code"`
	Description     null.String       `json:"description" select:"description" insert:"description"`
	ObjectionId     null.String       `json:"objection_id" select:"objection_id" `
	CreatedBy       null.Uint64       `json:"created_by" select:"created_by" insert:"created_by"`
	CreatedDate     null.Time         `json:"created_date" select:"created_date" insert:"created_date"`
	Remarks         []ObjectionRemark `json:"remarks" select:"remarks" insert:"remarks"`
	StatusFlag      null.String       `json:"status_flag" select:"status_flag" insert:"status_flag"`
	LastUpdatedBy   null.Uint64       `json:"last_updated_by" select:"last_updated_by"`
	LastUpdatedDate null.Time         `json:"last_updated_date" select:"last_updated_date"`
}
type ObjectionPraoReply struct {
	PraoCode        null.String       `json:"prao_code" select:"prao_code" insert:"prao_code"`
	PaoCode         null.String       `json:"pao_code" select:"pao_code" insert:"pao_code"`
	PaoName         null.String       `json:"pao_name" select:"pao_name"`
	Description     null.String       `json:"description" select:"description" insert:"description"`
	ObjectionId     null.String       `json:"objection_id" select:"objection_id" `
	CreatedBy       null.Uint64       `json:"created_by" select:"created_by" insert:"created_by"`
	CreatedDate     null.Time         `json:"created_date" select:"created_date" insert:"created_date"`
	Remarks         []ObjectionRemark `json:"remarks" select:"remarks" insert:"remarks"`
	StatusFlag      null.String       `json:"status_flag" select:"status_flag" insert:"status_flag"`
	LastUpdatedBy   null.Uint64       `json:"last_updated_by" select:"last_updated_by"`
	LastUpdatedDate null.Time         `json:"last_updated_date" select:"last_updated_date"`
}
type ObjectionPraoReplyWithLatestRemarks struct {
	PraoCode        null.String `json:"prao_code" select:"prao_code" insert:"prao_code"`
	PaoCode         null.String `json:"pao_code" select:"pao_code" insert:"pao_code"`
	PaoName         null.String `json:"pao_name" select:"pao_name"`
	Description     null.String `json:"description" select:"description" insert:"description"`
	ObjectionId     null.String `json:"objection_id" select:"objection_id" `
	CreatedBy       null.Uint64 `json:"created_by" select:"created_by" insert:"created_by"`
	CreatedDate     null.Time   `json:"created_date" select:"created_date" insert:"created_date"`
	StatusFlag      null.String `json:"status_flag" select:"status_flag" insert:"status_flag"`
	LastUpdatedBy   null.Uint64 `json:"last_updated_by" select:"last_updated_by"`
	LastUpdatedDate null.Time   `json:"last_updated_date" select:"last_updated_date"`
	LatestRemark    null.String `json:"latest_remark" select:"latest_remark"`
}
type ObjectionPraoRequest struct {
	PraoCode    string                   `json:"prao_code" select:"prao_code" insert:"prao_code" validate:"required"`
	PaoCode     string                   `json:"pao_code" select:"pao_code" insert:"pao_code" validate:"required,validatePaocode"`
	Description string                   `json:"description" select:"description" insert:"description" validate:"required"`
	ObjectionId string                   `json:"objection_id" select:"objection_id" insert:"objection_id"`
	CreatedBy   uint64                   `json:"created_by" select:"created_by" insert:"created_by" validate:"required"`
	CreatedDate time.Time                `json:"created_date" select:"created_date" insert:"created_date" validate:"required"`
	Remarks     []ObjectionRemarkRequest `json:"remarks" select:"remarks" insert:"remarks" validate:"dive"`
	StatusFlag  string                   `json:"status_flag" select:"status_flag" insert:"status_flag" validate:"required"`
}
type AccountsubmissionStatusListRequest struct {
	PraoOfficeId string `form:"prao_office_id"`
	Period       string `form:"period" validate:"required,validatePeriod"`
}
type ObjectionbyPraocodeReport struct {
	PraoCode string `json:"prao_code" select:"prao_code"`
	FromDate string `json:"from_date" select:"from_date"`
	ToDate   string `json:"to_date" select:"to_date"`
	Status   string `json:"status" select:"status"`
}

type ObjectionbyPaocodeReport struct {
	PaoCode  string `json:"pao_code" select:"pao_code"`
	FromDate string `json:"from_date" select:"from_date"`
	ToDate   string `json:"to_date" select:"to_date"`
	Status   string `json:"status" select:"status"`
}

type ObjectionbyDdocodeRpt struct {
	DdoCode  string `json:"ddo_code" select:"ddo_code"`
	FromDate string `json:"from_date" select:"from_date"`
	ToDate   string `json:"to_date" select:"to_date"`
	Status   string `json:"status" select:"status"`
}
