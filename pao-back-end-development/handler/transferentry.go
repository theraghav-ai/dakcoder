package handler

import (
	//"database/sql"

	//"github.com/templatedop/githubrepo/dtime"

	"context"
	"crypto/tls"
	"encoding/json"
	"fmt"
	"gotemplate/core/domain"
	"gotemplate/core/port"
	"gotemplate/handler/response"
	"math"
	"reflect"
	"strconv"

	"github.com/google/uuid"
	config "gitlab.cept.gov.in/it-2.0-common/api-config"
	contracts "gitlab.cept.gov.in/it-2.0-common/temporal-contracts"
	"go.temporal.io/sdk/client"

	"net/http"
	"strings"

	//"github.com/guregu/null"

	//"github.com/jackc/pgx/v5/pgtype"
	//"github.com/aarondl/opt/null"

	//"github.com/volatiletech/null"

	//"gotemplate/core/port"
	repo "gotemplate/repo/postgres"

	"github.com/gin-gonic/gin"
	"github.com/go-resty/resty/v2"
	"github.com/volatiletech/null/v9"
	apierrors "gitlab.cept.gov.in/it-2.0-common/api-errors"
	log "gitlab.cept.gov.in/it-2.0-common/api-log"
	validation "gitlab.cept.gov.in/it-2.0-common/api-validation"

	//"gotemplate/dtime"
	"errors"
	"time"
)

type TransferEntryHandler struct {
	svc    *repo.TransferEntryRepository
	svs    *repo.ObjectionFileRepository
	svb    *repo.TemporalRepository
	cfg    *config.Config
	client client.Client
}

// NewUserHandler creates a new UserHandler instance
func NewTransferEntryHandler(svc *repo.TransferEntryRepository, svs *repo.ObjectionFileRepository, svb *repo.TemporalRepository, cfg *config.Config, client client.Client) *TransferEntryHandler {
	return &TransferEntryHandler{
		svc,
		svs,
		svb,
		cfg,
		client,
	}
}

type TransferEntryRequest struct {
	PaoCode            string  `json:"pao_code" validate:"required,validatePaocode"`
	DdoCode            string  `json:"ddo_code" select:"ddo_code" validate:"required,validateDdocode"`
	Hoa                string  `json:"hoa" validate:"required,head_of_account"`
	TransferAmount     float64 `json:"transfer_amount" validate:"required"`
	TransferType       string  `json:"transfer_type" validate:"required,max=20"`
	CreatedBy          uint64  `json:"created_by" validate:"required,employee_id"`
	CreatedDate        string  `json:"created_date" validate:"required,date_yyyy_mm_dd"`
	TeSourceOfficeType string  `json:"te_source_office_type" select:"te_source_office_type" validate:"required,max=50"`
	Remarks            string  `json:"remarks" validate:"required,max=255"`
	TransDate          string  `json:"trans_date" validate:"required,date_yyyy_mm_dd"` //to be uncommented on 10042026
}

type TransferEntryRequests struct {
	TransferEntries []TransferEntryRequest `json:"transfer_entries" validate:"dive"`
}

const ErrInternalServerError = "Internal Server Error"

// CreateTransferEntryHandler godoc
//
//	@Summary		Create Transfer Entry
//	@Description	Create Transfer Entry
//	@Tags			Transfer Entry
//	@Accept			json
//	@Produce		json
//	@Param			body	body		[]domain.TransferEntryRequest	true	"Transfer Entry creation request"
//	@Success		201		{object}	response.GetTransferentryCreationResponse			"resource created successfully"
//	@Failure		400					{object}	apierrors.APIErrorResponse						"Validation error"
//	@Failure		401					{object}	apierrors.APIErrorResponse						"Unauthorized error"
//	@Failure		403					{object}	apierrors.APIErrorResponse						"Forbidden error"
//	@Failure		404					{object}	apierrors.APIErrorResponse						"Data not found error"
//	@Failure		409					{object}	apierrors.APIErrorResponse						"Data conflict error"
//	@Failure		500					{object}	apierrors.APIErrorResponse						"Internal server error"
//	@Router			/v1/transfer-entry [post]
func (uh *TransferEntryHandler) CreateTransferEntryHandler(ctx *gin.Context) {

	var req []TransferEntryRequest
	if err := ctx.ShouldBindJSON(&req); err != nil {
		apierrors.HandleBindingError(ctx, err)
		log.Error(ctx, "Binding failed for TransferEntryRequests: %s", err.Error())
		return
	}
	for _, r := range req {
		if err := validation.ValidateStruct(r); err != nil {
			apierrors.HandleValidationError(ctx, err)
			log.Error(ctx, "Validation failed for TransferEntryRequests: %s", err.Error())
			return
		}
	}

	var request []domain.TransferEntryRequest

	for _, requ := range req {

		request = append(request, domain.TransferEntryRequest{

			PaoCode:            null.StringFrom(requ.PaoCode),
			DdoCode:            requ.DdoCode,
			Hoa:                requ.Hoa,
			TransferAmount:     requ.TransferAmount,
			TransferType:       requ.TransferType,
			CreatedBy:          requ.CreatedBy,
			CreatedDate:        requ.CreatedDate,
			TeSourceOfficeType: requ.TeSourceOfficeType,
			Remarks:            requ.Remarks,
			TransDate:          requ.TransDate,
		})
	}

	var Total_debit float64 = 0
	var Total_credit float64 = 0
	var Inserted_Ids []domain.InsertedIds
	var err error
	currentTime := time.Now()
	for _, r := range request {
		if r.TransferType == "D" {
			Total_debit = Total_debit + r.TransferAmount
		}
		if r.TransferType == "C" {
			Total_credit = Total_credit + r.TransferAmount
		}
		r.CreatedDate = currentTime.Format("2006-01-02 15:04:05")
	}
	if Total_credit == Total_debit {
		Inserted_Ids, err = uh.svc.TransferentryCreationRepo(ctx, request)
		if err != nil {
			apierrors.HandleDBError(ctx, err)
			log.Error(ctx, "Transfer Entry Creation Repo call failed: %s", err.Error())
			return
		}
	} else {
		err := errors.New("total debit not equal to total credit")
		appError := apierrors.NewAppError(
			"Debit Credit error", // User-friendly error message
			"500",                // Error code representing the error type
			err,                  // Original error for debugging purposes
		)
		apiErrorResponse := apierrors.NewAPIErrorResponse(
			http.StatusInternalServerError, // HTTP status code
			ErrInternalServerError,         // Message to return to the client
			appError,                       // Encapsulated application error
		)
		ctx.JSON(apiErrorResponse.StatusCode, apiErrorResponse)
		log.Error(ctx, "total debit not equal to total credit: %s", err.Error())
		return
	}
	rsp := response.NewGTransferentryCreationResponse(Inserted_Ids)

	metadata := port.NewMetaDataResponse(0, 0, len(rsp))

	apiRsp := response.GetTransferentryCreationResponse{
		StatusCodeAndMessage: port.CreateSuccess,
		MetaDataResponse:     metadata,
		Data:                 rsp,
	}
	log.Debug(ctx, "CreateTransferEntryHandler response", apiRsp)
	handleCreateSuccess(ctx, apiRsp)

}

type TransferEntryReportRequest struct {
	PaoCode             string `uri:"pao-code" validate:"omitempty,max=10"`
	FromDateCreated     string `form:"from-date-created" validate:"required"`
	ToDateCreated       string `form:"to-date-created" validate:"required"`
	FromDateVerified    string `form:"from-date-verified" validate:"omitempty"`
	ToDateVerified      string `form:"to-date-verified" validate:"omitempty"`
	PfmsSubmissionFlag  string `form:"pfms-submission-flag" validate:"omitempty,oneof=pending success failed"`
	HPfmsGenerationFlag *bool  `form:"h-pfms-generation-flag" validate:"omitempty"`
	VerificationStatus  string `form:"verification-status" validate:"omitempty,oneof=created verified deleted"`
	port.MetaDataRequest
}

// ListTransferEntryReportHandler godoc
//
//	@Summary		Get Transfer Entry Report
//	@Description	Retrieve transfer entry report based on provided filters with pagination
//	@Tags			Transfer Entry
//	@Accept			json
//	@Produce		json
//	@Param			pao-code				path		string			false	"PAO code"
//	@Param			from-date-created				query		string			true	"From created date (RFC3339)"
//	@Param			to-date-created					query		string			true	"To created date (RFC3339)"
//	@Param			from-date-verified				query		string			false	"From verified date (RFC3339)"
//	@Param			to-date-verified					query		string			false	"To verified date (RFC3339)"
//	@Param			pfms-submission-flag		query		string			false	"PFMS submission status (pending, success, failed)"
//	@Param			h-pfms-generation-flag	query		boolean			false	"PFMS generation flag"
//	@Param			verification-status		query		string			false	"Verification status (created, verified, deleted)"
//	@Param			skip					query		int				false	"Number of records to skip for pagination"
//	@Param			limit					query		int				false	"Number of records to limit for pagination"
//	@Success		200						{object}	response.TransferentryReportResponse	"List retrieved successfully"
//	@Failure		400						{object}	apierrors.APIErrorResponse			"Validation error"
//	@Failure		401						{object}	apierrors.APIErrorResponse			"Unauthorized error"
//	@Failure		403						{object}	apierrors.APIErrorResponse			"Forbidden error"
//	@Failure		404						{object}	apierrors.APIErrorResponse			"Data not found error"
//	@Failure		409						{object}	apierrors.APIErrorResponse			"Data conflict error"
//	@Failure		500						{object}	apierrors.APIErrorResponse			"Internal server error"
//	@Router			/v1/pao-gen/pao/{pao-code}/transfer-entry/reports [get]
func (uh *TransferEntryHandler) ListTransferEntryReportHandler(ctx *gin.Context) {

	var req TransferEntryReportRequest
	if err := ctx.ShouldBindUri(&req); err != nil {
		apierrors.HandleBindingError(ctx, err)
		log.Error(ctx, "Binding failed for transferEntryReportRequest: %s", err.Error())
		return
	}
	if err1 := ctx.ShouldBindQuery(&req); err1 != nil {
		apierrors.HandleBindingError(ctx, err1)
		log.Error(ctx, "Binding failed for transferEntryReportRequest: %s", err1.Error())
		return
	}
	if err := validation.ValidateStruct(req); err != nil {
		apierrors.HandleValidationError(ctx, err)
		log.Error(ctx, "Validation failed for transferEntryReportRequest: %s", err.Error())
		return
	}
	if req.Limit == 0 {
		req.Limit = math.MaxInt32
	}

	fromDateCreated, err := time.Parse("2006-01-02", req.FromDateCreated)
	if err != nil {
		apierrors.HandleValidationError(ctx, err)
		log.Error(ctx, "Validation failed for transferEntryReportRequest: %s", err.Error())
		return
	}

	// toDateCreated, err := time.Parse("2006-01-02", req.ToDateCreated)
	// if err != nil {
	// 	apierrors.HandleValidationError(ctx, err)
	// 	log.Error(ctx, "Validation failed for transferEntryReportRequest: %s", err.Error())
	// 	return
	// }
	toDateCreated, err := time.Parse("2006-01-02", req.ToDateCreated)
	if err != nil {
		apierrors.HandleValidationError(ctx, err)
		log.Error(ctx, "Validation failed for transferEntryReportRequest: %s", err.Error())
		return
	}
	toDateCreated = toDateCreated.AddDate(0, 0, 1) // ← only this line is added to the existing code

	var fromDateVerified, toDateVerified time.Time
	if req.FromDateVerified != "" {
		fromDateVerified, err = time.Parse("2006-01-02", req.FromDateVerified)
		if err != nil {
			apierrors.HandleValidationError(ctx, err)
			log.Error(ctx, "Validation failed for from_date_verified: %s", err.Error())
			return
		}
	}

	// Parse ToDateVerified only if non-empty
	if req.ToDateVerified != "" {
		toDateVerified, err = time.Parse("2006-01-02", req.ToDateVerified)
		if err != nil {
			apierrors.HandleValidationError(ctx, err)
			log.Error(ctx, "Validation failed for to_date_verified: %s", err.Error())
			return
		}
	}
	var request domain.TransferEntryReportRequest
	request.PaoCode = req.PaoCode
	request.FromDateCreated = fromDateCreated
	request.ToDateCreated = toDateCreated
	request.FromDateVerified = fromDateVerified
	request.ToDateVerified = toDateVerified
	request.PfmsSubmissionFlag = req.PfmsSubmissionFlag
	request.HPfmsGenerationFlag = req.HPfmsGenerationFlag
	request.VerificationStatus = req.VerificationStatus
	res, err := uh.svc.TransferentryReportRepo(ctx, request, req.MetaDataRequest)
	if err != nil {
		apierrors.HandleDBError(ctx, err)
		log.Error(ctx, "Transfer ENtry Report Repo call failed: %s", err.Error())
		return
	}

	rsp := response.NewTransferentryReportResponse(res)

	metadata := port.NewMetaDataResponse(req.Skip, req.Limit, len(rsp))

	apiRsp := response.TransferentryReportResponse{
		StatusCodeAndMessage: port.ListSuccess,
		MetaDataResponse:     metadata,
		Data:                 rsp,
	}
	log.Debug(ctx, "ListTransferEntryReportHandler response", apiRsp)
	handleSuccess(ctx, apiRsp)

}

type TransferEntryRejectRequest struct {
	TransferEntryId    string `uri:"transfer-entry-id" binding:"required" validate:"required,max=30"`
	VerifiedBy         uint64 `json:"verified_by" validate:"required,employee_id"`
	VerificationStatus string `json:"verification_status" validate:"required,oneof=created verified deleted"`
	ApproverRemarks    string `json:"approver_remarks" validate:"required,max=255"`
}

// UpdateTransferEntryRejectHandler godoc
//
//	@Summary		Update Transfer entry as Reject
//	@Description	Update Transfer entry as Reject
//	@Tags			Transfer Entry
//	@Accept			json
//	@Produce		json
//	@Param			transfer-entry-id				path		string									true	"transfer-entry-id"
//	@Param			body	body		TransferEntryRejectRequest true	"Reject Transfer Entry request"
//	@Success		200		{object}	response.TransferentryReportResponse			"resource updated successfully"
//	@Failure		400					{object}	apierrors.APIErrorResponse						"Validation error"
//	@Failure		401					{object}	apierrors.APIErrorResponse						"Unauthorized error"
//	@Failure		403					{object}	apierrors.APIErrorResponse						"Forbidden error"
//	@Failure		404					{object}	apierrors.APIErrorResponse						"Data not found error"
//	@Failure		409					{object}	apierrors.APIErrorResponse						"Data conflict error"
//	@Failure		500					{object}	apierrors.APIErrorResponse						"Internal server error"
//	@Router			/v1/transfer-entry/{transfer-entry-id}/rejection [put]
func (uh *TransferEntryHandler) UpdateTransferEntryRejectHandler(ctx *gin.Context) {

	var req TransferEntryRejectRequest

	if err := ctx.ShouldBindUri(&req); err != nil {
		apierrors.HandleBindingError(ctx, err)
		log.Error(ctx, "Binding failed for TransferEntryRejectRequest: %s", err.Error())
		return
	}
	if err1 := ctx.ShouldBindQuery(&req); err1 != nil {
		apierrors.HandleBindingError(ctx, err1)
		log.Error(ctx, "Binding failed for AccountSubmissionStatusListRequest: %s", err1.Error())
		return
	}
	if err := ctx.ShouldBindJSON(&req); err != nil {
		apierrors.HandleBindingError(ctx, err)
		log.Error(ctx, "Binding failed for TransferEntryRejectRequest: %s", err.Error())
		return
	}
	if err := validation.ValidateStruct(req); err != nil {
		apierrors.HandleValidationError(ctx, err)
		log.Error(ctx, "Validation failed for TransferEntryRejectRequest: %s", err.Error())
		return
	}

	request := domain.TransferEntryRejectRequest{
		TransferEntryId:    req.TransferEntryId,
		VerifiedBy:         req.VerifiedBy,
		VerificationStatus: req.VerificationStatus,
		ApproverRemarks:    req.ApproverRemarks,
	}

	err := uh.svc.TransferentryRejectRepo(ctx, &request)
	if err != nil {
		apierrors.HandleDBError(ctx, err)
		log.Error(ctx, "TransferEntryReject Repo call failed: %s", err.Error())
		return
	}

	apiRsp := response.TransferentryReportResponse{
		StatusCodeAndMessage: port.UpdateSuccess,
	}
	log.Debug(ctx, "UpdateTransferEntryRejectHandler response", apiRsp)
	handleSuccess(ctx, apiRsp)

}

type TransferEntryVerifyRequest struct {
	DdoCode            string    `json:"ddo_code" select:"ddo_code" validate:"required,validateDdocode"`
	Hoa                string    `json:"hoa" select:"hoa" validate:"required,head_of_account"`
	TransferAmount     float64   `json:"transfer_amount" select:"transfer_amount" validate:"required"`
	TransferType       string    `json:"transfer_type" select:"transfer_type" validate:"required,max=20"`
	CreatedDate        time.Time `json:"created_date" select:"created_date" validate:"required"`
	TransferEntryId    string    `json:"transfer_entry_id" select:"transfer_entry_id" validate:"required,max=30"`
	VerificationStatus string    `json:"verification_status" select:"verification_status" validate:"required,oneof=created verified deleted"`
	VerifiedBy         int64     `json:"verified_by" select:"verified_by" validate:"required,employee_id"`
	VerifiedDate       time.Time `json:"verified_date" select:"verified_date" validate:"required"`
	ApproverRemarks    string    `json:"approver_remarks" select:"approver_remarks" validate:"required,max=255"`
	TransDate          time.Time `json:"trans_date" select:"trans_date" validate:"required"`
}

// UpdateTransferEntryVerifyHandler godoc
//
//	@Summary		Update Transfer entry as Verified
//	@Description	Update Transfer entry as Verified
//	@Tags			Transfer Entry
//	@Accept			json
//	@Produce		json
//	@Param			body	body		[]TransferEntryVerifyRequest true	"Verify Transfer Entry request"
//	@Success		200		{object}	response.TransferentryReportResponse			"resource updated successfully"
//	@Failure		400					{object}	apierrors.APIErrorResponse						"Validation error"
//	@Failure		401					{object}	apierrors.APIErrorResponse						"Unauthorized error"
//	@Failure		403					{object}	apierrors.APIErrorResponse						"Forbidden error"
//	@Failure		404					{object}	apierrors.APIErrorResponse						"Data not found error"
//	@Failure		409					{object}	apierrors.APIErrorResponse						"Data conflict error"
//	@Failure		500					{object}	apierrors.APIErrorResponse						"Internal server error"
//	@Router			/v1/transfer-entry/bulk-verification [put]
func (uh *TransferEntryHandler) UpdateTransferEntryVerifyHandler(ctx *gin.Context) {

	var req []TransferEntryVerifyRequest
	if err := ctx.ShouldBindJSON(&req); err != nil {
		apierrors.HandleBindingError(ctx, err)
		log.Error(ctx, "Binding failed for TransferEntryVerifyRequests: %s", err.Error())
		return
	}
	for _, r := range req {
		if err := validation.ValidateStruct(r); err != nil {
			apierrors.HandleValidationError(ctx, err)
			log.Error(ctx, "Validation failed for TransferEntryVerifyRequests: %s", err.Error())
			return
		}
	}

	var request []domain.TransferEntryVerifyRequest

	for _, requ := range req {

		request = append(request, domain.TransferEntryVerifyRequest{

			DdoCode:            requ.DdoCode,
			Hoa:                requ.Hoa,
			TransferAmount:     requ.TransferAmount,
			TransferType:       requ.TransferType,
			CreatedDate:        requ.CreatedDate,
			TransferEntryId:    requ.TransferEntryId,
			VerificationStatus: requ.VerificationStatus,
			VerifiedBy:         requ.VerifiedBy,
			VerifiedDate:       requ.VerifiedDate,
			ApproverRemarks:    requ.ApproverRemarks,
			TransDate:          requ.TransDate,
		})
	}

	number_budget_hoa := 0
	var budget_request []domain.BudgetRequest

	for _, y := range request {

		// ✅ shouldPostToBudget — ALL matching HOAs must flow to budget
		if shouldPostToBudget(y.Hoa) {

			financialYear := getFinancialYear(y.TransDate)

			u, q, err1 := uh.svc.GetOfficeIdforpaoRepo(ctx, y.DdoCode)
			if err1 != nil {
				apierrors.HandleDBError(ctx, err1)
				log.Error(ctx, "GetOfficeIdRepo call failed: %s", err1.Error())
				return
			}
			if !q {
				err := errors.New("failed to get office_id")
				appError := apierrors.NewAppError(
					"failed to get office_id",
					"404",
					err,
				)
				apiErrorResponse := apierrors.NewAPIErrorResponse(
					http.StatusInternalServerError,
					ErrInternalServerError,
					appError,
				)
				ctx.JSON(apiErrorResponse.StatusCode, apiErrorResponse)
				log.Error(ctx, "failed to get office_id: %s", err.Error())
				return
			}
			y.OfficeId = u.DdoOfficeId

			// Build a fresh struct per row to avoid any risk of stale
			// field values carrying over between iterations.
			budget_req := domain.BudgetRequest{
				FinancialYear:     financialYear,
				OfficeId:          y.OfficeId,
				Hoa:               y.Hoa,
				Remarks:           y.TransferEntryId,
				UpdatedBy:         y.VerifiedBy,
				TransactionOffice: y.OfficeId,
				SourceModule:      "PAO",
			}
			if y.TransferType == "C" {
				budget_req.ConsumedAmount = -y.TransferAmount
			} else {
				budget_req.ConsumedAmount = y.TransferAmount
			}

			// ✅ Always append to budget_request regardless of check exemption
			budget_request = append(budget_request, budget_req)
			number_budget_hoa++

			// ✅ exempted object heads (01,04,05,07,70 under 3201) skip validation
			// but data is already posted to budget above
			if shouldCheckBudget(y.Hoa) {
				// future budget validation logic here
			}
		}
	}

	apiRsp := response.TransferentryReportResponse{
		StatusCodeAndMessage: port.UpdateSuccess,
	}
	if number_budget_hoa > 0 {

		header := map[string]string{
			"Content-Type": "application/json",
		}
		url_budget := uh.cfg.GetString("urls.budgetcall")
		method_budget := "POST"
		params_budget := map[string]interface{}{
			"data":          budget_request,
			"source_module": "PAO",
		}
		response, err := uh.CallAPI(url_budget, method_budget, header, params_budget)
		if err != nil {
			ctx.JSON(http.StatusInternalServerError, gin.H{"error": err.Error()})
			return
		}

		success, ok := response["success"].(bool)
		if !ok || !success {
			// Surface the budget API's real error message instead of a
			// generic one, and always log the full response so failures
			// are diagnosable without re-instrumenting the handler.
			budgetMsg := "Budget consumption error"
			if msg, ok := response["message"].(string); ok && msg != "" {
				budgetMsg = msg
			}
			log.Error(ctx, "Budget consumption failed, raw response: %+v", response)

			err1 := errors.New(budgetMsg)
			appError := apierrors.NewAppError(
				budgetMsg,
				"422",
				err1,
			)
			apiErrorResponse := apierrors.NewAPIErrorResponse(
				http.StatusUnprocessableEntity,
				budgetMsg,
				appError,
			)
			ctx.JSON(apiErrorResponse.StatusCode, apiErrorResponse)
		} else {
			error := uh.svc.TransferentryVerifyRepo(ctx, request)
			if error != nil {
				apierrors.HandleDBError(ctx, error)
				log.Error(ctx, "Transfer Entry Verify Repo call failed: %s", error.Error())
				return
			}
			log.Debug(ctx, "UpdateTransferEntryVerifyHandler response", apiRsp)
			handleSuccess(ctx, apiRsp)
			return
		}

	} else {

		error := uh.svc.TransferentryVerifyRepo(ctx, request)
		if error != nil {
			apierrors.HandleDBError(ctx, error)
			log.Error(ctx, "Transfer Entry Verify Repo call failed: %s", error.Error())
			return
		}
		log.Debug(ctx, "UpdateTransferEntryVerifyHandler response", apiRsp)
		handleSuccess(ctx, apiRsp)
		return
	}

}

func (uh *TransferEntryHandler) UpdateTransferEntryVerifyHandler1(ctx *gin.Context) {

	var req []TransferEntryVerifyRequest
	if err := ctx.ShouldBindJSON(&req); err != nil {
		apierrors.HandleBindingError(ctx, err)
		log.Error(ctx, "Binding failed for TransferEntryVerifyRequests: %s", err.Error())
		return
	}
	for _, r := range req {
		if err := validation.ValidateStruct(r); err != nil {
			apierrors.HandleValidationError(ctx, err)
			log.Error(ctx, "Validation failed for TransferEntryVerifyRequests: %s", err.Error())
			return
		}
	}

	var request []domain.TransferEntryVerifyRequest

	for _, requ := range req {

		request = append(request, domain.TransferEntryVerifyRequest{

			DdoCode:            requ.DdoCode,
			Hoa:                requ.Hoa,
			TransferAmount:     requ.TransferAmount,
			TransferType:       requ.TransferType,
			CreatedDate:        requ.CreatedDate,
			TransferEntryId:    requ.TransferEntryId,
			VerificationStatus: requ.VerificationStatus,
			VerifiedBy:         requ.VerifiedBy,
			VerifiedDate:       requ.VerifiedDate,
			ApproverRemarks:    requ.ApproverRemarks,
			TransDate:          requ.TransDate,
		})
	}

	number_budget_hoa := 0
	var budget_request []domain.BudgetRequest
	var budget_req domain.BudgetRequest

	for _, y := range request {

		// ✅ shouldPostToBudget — ALL matching HOAs must flow to budget
		if shouldPostToBudget(y.Hoa) {

			financialYear := getFinancialYear(y.TransDate)

			u, q, err1 := uh.svc.GetOfficeIdforpaoRepo(ctx, y.DdoCode)
			if err1 != nil {
				apierrors.HandleDBError(ctx, err1)
				log.Error(ctx, "GetOfficeIdRepo call failed: %s", err1.Error())
				return
			}
			if q {
				y.OfficeId = u.DdoOfficeId
			} else {
				err := errors.New("failed to get office_id")
				appError := apierrors.NewAppError(
					"failed to get office_id",
					"404",
					err,
				)
				apiErrorResponse := apierrors.NewAPIErrorResponse(
					http.StatusInternalServerError,
					ErrInternalServerError,
					appError,
				)
				ctx.JSON(apiErrorResponse.StatusCode, apiErrorResponse)
				log.Error(ctx, "failed to get office_id: %s", err.Error())
				return
			}

			budget_req.FinancialYear = financialYear
			budget_req.OfficeId = y.OfficeId
			budget_req.Hoa = y.Hoa
			if y.TransferType == "C" {
				budget_req.ConsumedAmount = -y.TransferAmount
			} else {
				budget_req.ConsumedAmount = y.TransferAmount
			}
			budget_req.Remarks = y.TransferEntryId
			budget_req.UpdatedBy = y.VerifiedBy
			budget_req.TransactionOffice = y.OfficeId

			// ✅ Always append to budget_request regardless of check exemption
			budget_request = append(budget_request, budget_req)
			number_budget_hoa++

			// ✅ exempted object heads (01,04,05,07,70 under 3201) skip validation
			// but data is already posted to budget above
			if shouldCheckBudget(y.Hoa) {
				// future budget validation logic here
			}
		}
	}

	apiRsp := response.TransferentryReportResponse{
		StatusCodeAndMessage: port.UpdateSuccess,
	}
	if number_budget_hoa > 0 {

		header := map[string]string{
			"Content-Type": "application/json",
		}
		url_budget := uh.cfg.GetString("urls.budgetcall")
		method_budget := "POST"
		params_budget := map[string]interface{}{
			"data": budget_request,
		}
		response, err := uh.CallAPI(url_budget, method_budget, header, params_budget)
		if err != nil {
			ctx.JSON(http.StatusInternalServerError, gin.H{"error": err.Error()})
			return
		}

		if !response["success"].(bool) {
			err1 := errors.New("Budget consumption error")
			appError := apierrors.NewAppError(
				"Budget consumption error",
				"422",
				err1,
			)
			apiErrorResponse := apierrors.NewAPIErrorResponse(
				http.StatusUnprocessableEntity,
				"Budget consumption failed",
				appError,
			)
			ctx.JSON(apiErrorResponse.StatusCode, apiErrorResponse)
		} else {
			error := uh.svc.TransferentryVerifyRepo(ctx, request)
			if error != nil {
				apierrors.HandleDBError(ctx, error)
				log.Error(ctx, "Transfer Entry Verify Repo call failed: %s", error.Error())
				return
			}
			log.Debug(ctx, "UpdateTransferEntryVerifyHandler response", apiRsp)
			handleSuccess(ctx, apiRsp)
			return
		}

	} else {

		error := uh.svc.TransferentryVerifyRepo(ctx, request)
		if error != nil {
			apierrors.HandleDBError(ctx, error)
			log.Error(ctx, "Transfer Entry Verify Repo call failed: %s", error.Error())
			return
		}
		log.Debug(ctx, "UpdateTransferEntryVerifyHandler response", apiRsp)
		handleSuccess(ctx, apiRsp)
		return
	}

}

// func (uh *TransferEntryHandler) UpdateTransferEntryVerifyHandler(ctx *gin.Context) {

// 	var req []TransferEntryVerifyRequest
// 	if err := ctx.ShouldBindJSON(&req); err != nil {
// 		apierrors.HandleBindingError(ctx, err)
// 		log.Error(ctx, "Binding failed for TransferEntryVerifyRequests: %s", err.Error())
// 		return
// 	}
// 	for _, r := range req {
// 		if err := validation.ValidateStruct(r); err != nil {
// 			apierrors.HandleValidationError(ctx, err)
// 			log.Error(ctx, "Validation failed for TransferEntryVerifyRequests: %s", err.Error())
// 			return
// 		}
// 	}

// 	var request []domain.TransferEntryVerifyRequest

// 	for _, requ := range req {

// 		request = append(request, domain.TransferEntryVerifyRequest{

// 			DdoCode:            requ.DdoCode,
// 			Hoa:                requ.Hoa,
// 			TransferAmount:     requ.TransferAmount,
// 			TransferType:       requ.TransferType,
// 			CreatedDate:        requ.CreatedDate,
// 			TransferEntryId:    requ.TransferEntryId,
// 			VerificationStatus: requ.VerificationStatus,
// 			VerifiedBy:         requ.VerifiedBy,
// 			VerifiedDate:       requ.VerifiedDate,
// 			ApproverRemarks:    requ.ApproverRemarks,
// 			TransDate:          requ.TransDate,
// 		})
// 	}

// 	number_budget_hoa := 0
// 	var budget_request []domain.BudgetRequest
// 	var budget_req domain.BudgetRequest

// 	for _, y := range request {

// 		// ✅ shouldPostToBudget — ALL matching HOAs must flow to budget
// 		if shouldPostToBudget(y.Hoa) {

// 			financialYear := getFinancialYear(y.TransDate)

// 			u, q, err1 := uh.svc.GetOfficeIdforpaoRepo(ctx, y.DdoCode)
// 			if err1 != nil {
// 				apierrors.HandleDBError(ctx, err1)
// 				log.Error(ctx, "GetOfficeIdRepo call failed: %s", err1.Error())
// 				return
// 			}
// 			if q {
// 				y.OfficeId = u.DdoOfficeId
// 			} else {
// 				err := errors.New("failed to get office_id")
// 				appError := apierrors.NewAppError(
// 					"failed to get office_id",
// 					"404",
// 					err,
// 				)
// 				apiErrorResponse := apierrors.NewAPIErrorResponse(
// 					http.StatusInternalServerError,
// 					ErrInternalServerError,
// 					appError,
// 				)
// 				ctx.JSON(apiErrorResponse.StatusCode, apiErrorResponse)
// 				log.Error(ctx, "failed to get office_id: %s", err.Error())
// 				return
// 			}

// 			budget_req.FinancialYear = financialYear
// 			budget_req.OfficeId = y.OfficeId
// 			budget_req.Hoa = y.Hoa
// 			if y.TransferType == "C" {
// 				budget_req.ConsumedAmount = -y.TransferAmount
// 			} else {
// 				budget_req.ConsumedAmount = y.TransferAmount
// 			}
// 			budget_req.Remarks = y.TransferEntryId
// 			budget_req.UpdatedBy = y.VerifiedBy
// 			budget_req.TransactionOffice = y.OfficeId

// 			// ✅ Always append to budget_request regardless of check exemption
// 			budget_request = append(budget_request, budget_req)
// 			number_budget_hoa++

// 			// ✅ exempted object heads (01,04,05,07,70 under 3201) skip validation
// 			// but data is already posted to budget above
// 			if shouldCheckBudget(y.Hoa) {
// 				// future budget validation logic here
// 			}
// 		}
// 	}

// 	apiRsp := response.TransferentryReportResponse{
// 		StatusCodeAndMessage: port.UpdateSuccess,
// 	}
// 	if number_budget_hoa > 0 {

// 		header := map[string]string{
// 			"Content-Type": "application/json",
// 		}
// 		url_budget := uh.cfg.GetString("urls.budgetcall")
// 		method_budget := "POST"
// 		params_budget := map[string]interface{}{
// 			"data": budget_request,
// 		}
// 		response, err := uh.CallAPI(url_budget, method_budget, header, params_budget)
// 		if err != nil {
// 			ctx.JSON(http.StatusInternalServerError, gin.H{"error": err.Error()})
// 			return
// 		}

// 		// 🔍 TEMP DEBUG — remove once root cause is confirmed.
// 		// Shows the budget API's real response instead of the generic
// 		// "Budget consumption error" message the code below falls back to.
// 		log.Error(ctx, "DEBUG BUDGET API RAW RESPONSE: %+v", response)

// 		success, ok := response["success"].(bool)
// 		if !ok || !success {
// 			err1 := errors.New("Budget consumption error")
// 			appError := apierrors.NewAppError(
// 				"Budget consumption error",
// 				"422",
// 				err1,
// 			)
// 			apiErrorResponse := apierrors.NewAPIErrorResponse(
// 				http.StatusUnprocessableEntity,
// 				"Budget consumption failed",
// 				appError,
// 			)
// 			ctx.JSON(apiErrorResponse.StatusCode, apiErrorResponse)
// 		} else {
// 			error := uh.svc.TransferentryVerifyRepo(ctx, request)
// 			if error != nil {
// 				apierrors.HandleDBError(ctx, error)
// 				log.Error(ctx, "Transfer Entry Verify Repo call failed: %s", error.Error())
// 				return
// 			}
// 			log.Debug(ctx, "UpdateTransferEntryVerifyHandler response", apiRsp)
// 			handleSuccess(ctx, apiRsp)
// 			return
// 		}

// 	} else {

// 		error := uh.svc.TransferentryVerifyRepo(ctx, request)
// 		if error != nil {
// 			apierrors.HandleDBError(ctx, error)
// 			log.Error(ctx, "Transfer Entry Verify Repo call failed: %s", error.Error())
// 			return
// 		}
// 		log.Debug(ctx, "UpdateTransferEntryVerifyHandler response", apiRsp)
// 		handleSuccess(ctx, apiRsp)
// 		return
// 	}

// }

func (uh *TransferEntryHandler) UpdateTransferEntryVerifyHandlertestresponseinterminal(ctx *gin.Context) {

	field, _ := reflect.TypeOf(TransferEntryVerifyRequest{}).FieldByName("TransDate")
	log.Error(ctx, "DEBUG TransDate raw tag: %q", field.Tag)

	var req []TransferEntryVerifyRequest
	if err := ctx.ShouldBindJSON(&req); err != nil {
		apierrors.HandleBindingError(ctx, err)
		log.Error(ctx, "Binding failed for TransferEntryVerifyRequests: %s", err.Error())
		return
	}
	for _, r := range req {
		if err := validation.ValidateStruct(r); err != nil {
			apierrors.HandleValidationError(ctx, err)
			log.Error(ctx, "Validation failed for TransferEntryVerifyRequests: %s", err.Error())
			return
		}
	}

	var request []domain.TransferEntryVerifyRequest

	for _, requ := range req {

		request = append(request, domain.TransferEntryVerifyRequest{

			DdoCode:            requ.DdoCode,
			Hoa:                requ.Hoa,
			TransferAmount:     requ.TransferAmount,
			TransferType:       requ.TransferType,
			CreatedDate:        requ.CreatedDate,
			TransferEntryId:    requ.TransferEntryId,
			VerificationStatus: requ.VerificationStatus,
			VerifiedBy:         requ.VerifiedBy,
			VerifiedDate:       requ.VerifiedDate,
			ApproverRemarks:    requ.ApproverRemarks,
			TransDate:          requ.TransDate,
		})
	}

	number_budget_hoa := 0
	var budget_request []domain.BudgetRequest
	var budget_req domain.BudgetRequest

	for _, y := range request {

		// 🔍 TEMP DEBUG — shows VerifiedBy as bound from the incoming request,
		// before it gets copied into budget_req.UpdatedBy below.
		log.Error(ctx, "DEBUG row: DdoCode=%s Hoa=%s TransferType=%s VerifiedBy=%d TransDate=%v",
			y.DdoCode, y.Hoa, y.TransferType, y.VerifiedBy, y.TransDate)

		// ✅ shouldPostToBudget — ALL matching HOAs must flow to budget
		if shouldPostToBudget(y.Hoa) {

			financialYear := getFinancialYear(y.TransDate)

			u, q, err1 := uh.svc.GetOfficeIdforpaoRepo(ctx, y.DdoCode)
			if err1 != nil {
				apierrors.HandleDBError(ctx, err1)
				log.Error(ctx, "GetOfficeIdRepo call failed: %s", err1.Error())
				return
			}
			if q {
				y.OfficeId = u.DdoOfficeId
			} else {
				err := errors.New("failed to get office_id")
				appError := apierrors.NewAppError(
					"failed to get office_id",
					"404",
					err,
				)
				apiErrorResponse := apierrors.NewAPIErrorResponse(
					http.StatusInternalServerError,
					ErrInternalServerError,
					appError,
				)
				ctx.JSON(apiErrorResponse.StatusCode, apiErrorResponse)
				log.Error(ctx, "failed to get office_id: %s", err.Error())
				return
			}

			// 🔍 TEMP DEBUG — confirms what office_id was actually resolved for this ddo_code
			log.Error(ctx, "DEBUG resolved OfficeId for DdoCode=%s -> OfficeId=%d FinancialYear=%s",
				y.DdoCode, y.OfficeId, financialYear)

			budget_req.FinancialYear = financialYear
			budget_req.OfficeId = y.OfficeId
			budget_req.Hoa = y.Hoa
			if y.TransferType == "C" {
				budget_req.ConsumedAmount = -y.TransferAmount
			} else {
				budget_req.ConsumedAmount = y.TransferAmount
			}
			budget_req.Remarks = y.TransferEntryId
			budget_req.UpdatedBy = y.VerifiedBy
			budget_req.TransactionOffice = y.OfficeId

			// ✅ Always append to budget_request regardless of check exemption
			budget_request = append(budget_request, budget_req)
			number_budget_hoa++

			// ✅ exempted object heads (01,04,05,07,70 under 3201) skip validation
			// but data is already posted to budget above
			if shouldCheckBudget(y.Hoa) {
				// future budget validation logic here
			}
		} else {
			// 🔍 TEMP DEBUG — confirms this HOA was skipped and never sent to budget API
			log.Error(ctx, "DEBUG SKIPPED (shouldPostToBudget=false): Hoa=%s", y.Hoa)
		}
	}

	apiRsp := response.TransferentryReportResponse{
		StatusCodeAndMessage: port.UpdateSuccess,
	}
	if number_budget_hoa > 0 {

		// 🔍 TEMP DEBUG — the exact body about to be sent to the budget API
		log.Error(ctx, "DEBUG BUDGET REQUEST BODY: %+v", budget_request)

		header := map[string]string{
			"Content-Type": "application/json",
		}
		url_budget := uh.cfg.GetString("urls.budgetcall")
		method_budget := "POST"
		params_budget := map[string]interface{}{
			"data": budget_request,
		}

		// 🔍 TEMP DEBUG — confirm the exact URL being called
		log.Error(ctx, "DEBUG BUDGET URL: %s", url_budget)

		response, err := uh.CallAPI(url_budget, method_budget, header, params_budget)
		if err != nil {
			log.Error(ctx, "DEBUG BUDGET API CALL ERROR: %s", err.Error())
			ctx.JSON(http.StatusInternalServerError, gin.H{"error": err.Error()})
			return
		}

		// 🔍 TEMP DEBUG — the raw response from the budget API, before it gets
		// flattened into the generic "Budget consumption error" message below.
		log.Error(ctx, "DEBUG BUDGET API RAW RESPONSE: %+v", response)
		if statusCode, ok := response["status_code"]; ok {
			log.Error(ctx, "DEBUG BUDGET API status_code: %v", statusCode)
		}
		if msg, ok := response["message"]; ok {
			log.Error(ctx, "DEBUG BUDGET API message: %v", msg)
		}
		if errBlock, ok := response["error"]; ok {
			log.Error(ctx, "DEBUG BUDGET API error block: %+v", errBlock)
		}

		if !response["success"].(bool) {
			err1 := errors.New("Budget consumption error")
			appError := apierrors.NewAppError(
				"Budget consumption error",
				"422",
				err1,
			)
			apiErrorResponse := apierrors.NewAPIErrorResponse(
				http.StatusUnprocessableEntity,
				"Budget consumption failed",
				appError,
			)
			ctx.JSON(apiErrorResponse.StatusCode, apiErrorResponse)
		} else {
			error := uh.svc.TransferentryVerifyRepo(ctx, request)
			if error != nil {
				apierrors.HandleDBError(ctx, error)
				log.Error(ctx, "Transfer Entry Verify Repo call failed: %s", error.Error())
				return
			}
			log.Debug(ctx, "UpdateTransferEntryVerifyHandler response", apiRsp)
			handleSuccess(ctx, apiRsp)
			return
		}

	} else {

		error := uh.svc.TransferentryVerifyRepo(ctx, request)
		if error != nil {
			apierrors.HandleDBError(ctx, error)
			log.Error(ctx, "Transfer Entry Verify Repo call failed: %s", error.Error())
			return
		}
		log.Debug(ctx, "UpdateTransferEntryVerifyHandler response", apiRsp)
		handleSuccess(ctx, apiRsp)
		return
	}

}

func (uh *TransferEntryHandler) UpdateTransferEntryVerifyHandler17062026(ctx *gin.Context) {

	var req []TransferEntryVerifyRequest
	if err := ctx.ShouldBindJSON(&req); err != nil {
		apierrors.HandleBindingError(ctx, err)
		log.Error(ctx, "Binding failed for TransferEntryVerifyRequests: %s", err.Error())
		return
	}
	for _, r := range req {
		if err := validation.ValidateStruct(r); err != nil {
			apierrors.HandleValidationError(ctx, err)
			log.Error(ctx, "Validation failed for TransferEntryVerifyRequests: %s", err.Error())
			return
		}
	}

	var request []domain.TransferEntryVerifyRequest

	for _, requ := range req {

		request = append(request, domain.TransferEntryVerifyRequest{

			DdoCode:            requ.DdoCode,
			Hoa:                requ.Hoa,
			TransferAmount:     requ.TransferAmount,
			TransferType:       requ.TransferType,
			CreatedDate:        requ.CreatedDate,
			TransferEntryId:    requ.TransferEntryId,
			VerificationStatus: requ.VerificationStatus,
			VerifiedBy:         requ.VerifiedBy,
			VerifiedDate:       requ.VerifiedDate,
			ApproverRemarks:    requ.ApproverRemarks,
			TransDate:          requ.TransDate,
		})
	}

	number_budget_hoa := 0
	var budget_request []domain.BudgetRequest
	var budget_req domain.BudgetRequest

	for _, y := range request {

		// if strings.HasPrefix(y.Hoa, "5201") ||
		// 	strings.HasPrefix(y.Hoa, "3201") ||
		// 	strings.HasPrefix(y.Hoa, "7610") ||
		// 	strings.HasPrefix(y.Hoa, "801606101010500") ||
		// 	strings.HasPrefix(y.Hoa, "801606101020500") ||
		// 	strings.HasPrefix(y.Hoa, "2552") ||
		// 	strings.HasPrefix(y.Hoa, "4552") {
		if shouldCheckBudget(y.Hoa) { // ← ONLY THIS LINE CHANGED

			financialYear := getFinancialYear(y.TransDate) //to be uncommented on 10042026
			// financialYear := "2025"

			u, q, err1 := uh.svc.GetOfficeIdRepo(ctx, y.DdoCode)
			if err1 != nil {
				apierrors.HandleDBError(ctx, err1)
				log.Error(ctx, "GetOfficeIdRepo call failed: %s", err1.Error())
				return
			}
			if q {
				y.OfficeId = u.DdoOfficeId
			} else {
				err := errors.New("failed to get office_id")
				appError := apierrors.NewAppError(
					"failed to get office_id", // User-friendly error message
					"404",                     // Error code representing the error type
					err,                       // Original error for debugging purposes
				)
				apiErrorResponse := apierrors.NewAPIErrorResponse(
					http.StatusInternalServerError, // HTTP status code
					ErrInternalServerError,         // Message to return to the client
					appError,                       // Encapsulated application error
				)
				ctx.JSON(apiErrorResponse.StatusCode, apiErrorResponse)
				log.Error(ctx, "failed to get office_id: %s", err.Error())

				return
			}

			budget_req.FinancialYear = financialYear
			budget_req.OfficeId = y.OfficeId
			budget_req.Hoa = y.Hoa
			if y.TransferType == "C" {
				budget_req.ConsumedAmount = -y.TransferAmount
			} else {
				budget_req.ConsumedAmount = y.TransferAmount
			}
			budget_req.Remarks = y.TransferEntryId
			budget_req.UpdatedBy = y.VerifiedBy
			budget_req.TransactionOffice = y.OfficeId

			budget_request = append(budget_request, budget_req)

			number_budget_hoa++
		}

	}
	apiRsp := response.TransferentryReportResponse{
		StatusCodeAndMessage: port.UpdateSuccess,
	}
	if number_budget_hoa > 0 {

		// server := uh.cfg.GetString("db.ApiUrl")
		header := map[string]string{
			"Content-Type": "application/json",
		}
		url_budget := uh.cfg.GetString("urls.budgetcall")
		method_budget := "POST"
		// Wrap the budget_request in a map
		params_budget := map[string]interface{}{
			"data": budget_request,
		}
		response, err := uh.CallAPI(url_budget, method_budget, header, params_budget)
		if err != nil {
			ctx.JSON(http.StatusInternalServerError, gin.H{"error": err.Error()})
			return
		}

		if !response["success"].(bool) {
			err1 := errors.New("Budget consumption error")
			// Create an AppError with a user-friendly message and code.
			appError := apierrors.NewAppError(
				"Budget consumption error", // User-friendly error message
				"422",                      // Error code representing the error type
				err1,                       // Original error for debugging purposes
			)
			apiErrorResponse := apierrors.NewAPIErrorResponse(
				http.StatusUnprocessableEntity, // HTTP status code
				"Budget consumption failed",    // Message to return to the client
				appError,                       // Encapsulated application error
			)
			ctx.JSON(apiErrorResponse.StatusCode, apiErrorResponse)
		} else {
			error := uh.svc.TransferentryVerifyRepo(ctx, request)
			if error != nil {
				apierrors.HandleDBError(ctx, err)
				log.Error(ctx, "Transfer Entry Verify Repo call failed: %s", error.Error())
				return
			}
			log.Debug(ctx, "UpdateTransferEntryVerifyHandler response", apiRsp)
			handleSuccess(ctx, apiRsp)
			return
		}

	} else {

		error := uh.svc.TransferentryVerifyRepo(ctx, request)
		if error != nil {
			apierrors.HandleDBError(ctx, error)
			log.Error(ctx, "Transfer Entry Verify Repo call failed: %s", error.Error())
			return
		}
		log.Debug(ctx, "UpdateTransferEntryVerifyHandler response", apiRsp)
		handleSuccess(ctx, apiRsp)
		return
	}

}

// helper function to check the HOA & object heads for verification of the transfer entries
// func shouldCheckBudget(hoa string) bool {
// 	if strings.HasPrefix(hoa, "3201") {
// 		if len(hoa) < 2 {
// 			return false
// 		}
// 		skipObjectHeads := map[string]bool{
// 			"01": true,
// 			"04": true,
// 			"05": true,
// 			"07": true,
// 			"70": true,
// 		}
// 		lastTwo := hoa[len(hoa)-2:]
// 		return !skipObjectHeads[lastTwo]
// 	}

// 	return strings.HasPrefix(hoa, "5201") ||
// 		strings.HasPrefix(hoa, "7610") ||
// 		strings.HasPrefix(hoa, "801606101010500") ||
// 		strings.HasPrefix(hoa, "801606101020500") ||
// 		strings.HasPrefix(hoa, "2552") ||
// 		strings.HasPrefix(hoa, "4552")
// }

func shouldPostToBudget(hoa string) bool {
	return strings.HasPrefix(hoa, "5201") ||
		strings.HasPrefix(hoa, "3201") ||
		strings.HasPrefix(hoa, "7610") ||
		strings.HasPrefix(hoa, "801606101010500") ||
		strings.HasPrefix(hoa, "801606101020500") ||
		strings.HasPrefix(hoa, "2552") ||
		strings.HasPrefix(hoa, "4552")
}

// func shouldCheckBudget(hoa string) bool {
// 	if strings.HasPrefix(hoa, "3201") {
// 		if len(hoa) < 2 {
// 			return false
// 		}
// 		skipObjectHeads := map[string]bool{
// 			"01": true, "04": true, "05": true,
// 			"07": true, "70": true,
// 		}
// 		lastTwo := hoa[len(hoa)-2:]
// 		return !skipObjectHeads[lastTwo] // false = skip check, but still post
// 	}

//		return strings.HasPrefix(hoa, "5201") ||
//			strings.HasPrefix(hoa, "7610") ||
//			strings.HasPrefix(hoa, "801606101010500") ||
//			strings.HasPrefix(hoa, "801606101020500") ||
//			strings.HasPrefix(hoa, "2552") ||
//			strings.HasPrefix(hoa, "4552")
//	}
func shouldCheckBudget(hoa string) bool {
	if strings.HasPrefix(hoa, "3201") {
		if len(hoa) < 2 {
			return false
		}

		// EXISTING: Skip check for object heads 01, 04, 05, 07, 70
		skipObjectHeads := map[string]bool{
			"01": true, "04": true, "05": true,
			"07": true, "70": true,
		}
		lastTwo := hoa[len(hoa)-2:]
		if skipObjectHeads[lastTwo] {
			return false
		}

		// NEW: Skip check for these specific HOAs from screenshot
		exemptedHoas := map[string]bool{
			"320101001010106": true, // POSTAL DIRECTORATE
			"320101001060106": true, // PARCEL DIRECTORATE
			"320101101010106": true, // CIRCLE OFFICES
			"320101101030106": true, // POSTAL DIVISION
			"320101101040106": true, // R.M.S. DIVISIONS
			"320101101050106": true, // FOREIGN POST DIVISIONS
			"320101101060106": true, // POSTAL STOCK DEPOT
			"320102003010106": true, // OPERATIONAL TRAINING
			"320102101010106": true, // EXISTING POST OFFICES
			"320102102010106": true, // MAIL SORTING
			"320102103060106": true, // OTHERS (Conveyance of Mails)
			"320102104010106": true, // REASEARCH & DEVELOPMENT
			"320103101010106": true, // SAVING BANK CONTROL ORGANIZATION
			"320103101020106": true, // SAVINGS BANK INTERNAL CHECK ORGANISATION
			"320103101030106": true, // SMALL SAVINGS WORK IN HEAD POST OFFICES
			"320103101070106": true, // POSTAL LIFE INSURANCE DIRECTORATE.
			"320103101080106": true, // POSTAL LIFE INSURANCE BRANCH CIRCLE OFFICE.
			"320103101100106": true, // DIRECTOR PLI CALCUTTA.
			"320104102010106": true, // DIRECTORATEP(P.A.WING)
			"320104102020106": true, // CIRCLE POSTAL ACCOUNTS OFFICES.
			"320105053030106": true, // BUILDING ESTABLISHMENT
			"320108102030106": true, // STORAGE & DISTRIBUTION OF FORMS
		}
		if exemptedHoas[hoa] {
			return false
		}

		return true
	}

	return strings.HasPrefix(hoa, "5201") ||
		strings.HasPrefix(hoa, "7610") ||
		strings.HasPrefix(hoa, "801606101010500") ||
		strings.HasPrefix(hoa, "801606101020500") ||
		strings.HasPrefix(hoa, "2552") ||
		strings.HasPrefix(hoa, "4552")
}

func (uh *TransferEntryHandler) UpdateTransferEntryVerifyHandler14052026(ctx *gin.Context) {

	var req []TransferEntryVerifyRequest
	if err := ctx.ShouldBindJSON(&req); err != nil {
		apierrors.HandleBindingError(ctx, err)
		log.Error(ctx, "Binding failed for TransferEntryVerifyRequests: %s", err.Error())
		return
	}
	for _, r := range req {
		if err := validation.ValidateStruct(r); err != nil {
			apierrors.HandleValidationError(ctx, err)
			log.Error(ctx, "Validation failed for TransferEntryVerifyRequests: %s", err.Error())
			return
		}
	}

	var request []domain.TransferEntryVerifyRequest

	for _, requ := range req {

		request = append(request, domain.TransferEntryVerifyRequest{

			DdoCode:            requ.DdoCode,
			Hoa:                requ.Hoa,
			TransferAmount:     requ.TransferAmount,
			TransferType:       requ.TransferType,
			CreatedDate:        requ.CreatedDate,
			TransferEntryId:    requ.TransferEntryId,
			VerificationStatus: requ.VerificationStatus,
			VerifiedBy:         requ.VerifiedBy,
			VerifiedDate:       requ.VerifiedDate,
			ApproverRemarks:    requ.ApproverRemarks,
			TransDate:          requ.TransDate,
		})
	}

	number_budget_hoa := 0
	var budget_request []domain.BudgetRequest
	var budget_req domain.BudgetRequest

	for _, y := range request {

		if strings.HasPrefix(y.Hoa, "5201") ||
			strings.HasPrefix(y.Hoa, "3201") ||
			strings.HasPrefix(y.Hoa, "7610") ||
			strings.HasPrefix(y.Hoa, "801606101010500") ||
			strings.HasPrefix(y.Hoa, "801606101020500") ||
			strings.HasPrefix(y.Hoa, "2552") ||
			strings.HasPrefix(y.Hoa, "4552") {

			financialYear := getFinancialYear(y.TransDate) //to be uncommented on 10042026
			// financialYear := "2025"

			u, q, err1 := uh.svc.GetOfficeIdRepo(ctx, y.DdoCode)
			if err1 != nil {
				apierrors.HandleDBError(ctx, err1)
				log.Error(ctx, "GetOfficeIdRepo call failed: %s", err1.Error())
				return
			}
			if q {
				y.OfficeId = u.DdoOfficeId
			} else {
				err := errors.New("failed to get office_id")
				appError := apierrors.NewAppError(
					"failed to get office_id", // User-friendly error message
					"404",                     // Error code representing the error type
					err,                       // Original error for debugging purposes
				)
				apiErrorResponse := apierrors.NewAPIErrorResponse(
					http.StatusInternalServerError, // HTTP status code
					ErrInternalServerError,         // Message to return to the client
					appError,                       // Encapsulated application error
				)
				ctx.JSON(apiErrorResponse.StatusCode, apiErrorResponse)
				log.Error(ctx, "failed to get office_id: %s", err.Error())

				return
			}

			budget_req.FinancialYear = financialYear
			budget_req.OfficeId = y.OfficeId
			budget_req.Hoa = y.Hoa
			if y.TransferType == "C" {
				budget_req.ConsumedAmount = -y.TransferAmount
			} else {
				budget_req.ConsumedAmount = y.TransferAmount
			}
			budget_req.Remarks = y.TransferEntryId
			budget_req.UpdatedBy = y.VerifiedBy
			budget_req.TransactionOffice = y.OfficeId

			budget_request = append(budget_request, budget_req)

			number_budget_hoa++
		}

	}
	apiRsp := response.TransferentryReportResponse{
		StatusCodeAndMessage: port.UpdateSuccess,
	}
	if number_budget_hoa > 0 {

		// server := uh.cfg.GetString("db.ApiUrl")
		header := map[string]string{
			"Content-Type": "application/json",
		}
		url_budget := uh.cfg.GetString("urls.budgetcall")
		method_budget := "POST"
		// Wrap the budget_request in a map
		params_budget := map[string]interface{}{
			"data": budget_request,
		}
		response, err := uh.CallAPI(url_budget, method_budget, header, params_budget)
		if err != nil {
			ctx.JSON(http.StatusInternalServerError, gin.H{"error": err.Error()})
			return
		}

		if !response["success"].(bool) {
			err1 := errors.New("Budget consumption error")
			// Create an AppError with a user-friendly message and code.
			appError := apierrors.NewAppError(
				"Budget consumption error", // User-friendly error message
				"422",                      // Error code representing the error type
				err1,                       // Original error for debugging purposes
			)
			apiErrorResponse := apierrors.NewAPIErrorResponse(
				http.StatusUnprocessableEntity, // HTTP status code
				"Budget consumption failed",    // Message to return to the client
				appError,                       // Encapsulated application error
			)
			ctx.JSON(apiErrorResponse.StatusCode, apiErrorResponse)
		} else {
			error := uh.svc.TransferentryVerifyRepo(ctx, request)
			if error != nil {
				apierrors.HandleDBError(ctx, err)
				log.Error(ctx, "Transfer Entry Verify Repo call failed: %s", error.Error())
				return
			}
			log.Debug(ctx, "UpdateTransferEntryVerifyHandler response", apiRsp)
			handleSuccess(ctx, apiRsp)
			return
		}

	} else {

		error := uh.svc.TransferentryVerifyRepo(ctx, request)
		if error != nil {
			apierrors.HandleDBError(ctx, error)
			log.Error(ctx, "Transfer Entry Verify Repo call failed: %s", error.Error())
			return
		}
		log.Debug(ctx, "UpdateTransferEntryVerifyHandler response", apiRsp)
		handleSuccess(ctx, apiRsp)
		return
	}

}

type DdoTeRequest struct {
	DdoCode  string `uri:"ddo-code" binding:"required" validate:"required,validateDdocode"`
	FromDate string `form:"from-date" validate:"required,date_yyyy_mm_dd"`
	ToDate   string `form:"to-date" validate:"required,date_yyyy_mm_dd"`
	Status   string `form:"status" validate:"required,min=1,max=10"`
	port.MetaDataRequest
}

// ListDdoTransferEntryReportHandler godoc
//
//	@Summary		Get Transfer entry report from DDO
//	@Description	Get Transfer entry report from DDO
//	@Tags			Transfer Entry
//	@Accept			json
//	@Produce		json
//	@Param			ddo-code	path		string			true	"Ddo_code"
//	@Param			from-date	query		string			true	"From_date"
//	@Param			to-date	query		string			true	"To_date"
//	@Param			status	query		string			true	"Status"
//
// @Param       skip    query       int     			false   		"Number of records to skip for pagination"
// @Param       limit   query       int     			false   		"Number of records to limit for pagination"
//
//	@Success		200		{object}	response.DdoTransferentryReportResponse			"data retrieved successfully"
//	@Failure		400					{object}	apierrors.APIErrorResponse						"Validation error"
//	@Failure		401					{object}	apierrors.APIErrorResponse						"Unauthorized error"
//	@Failure		403					{object}	apierrors.APIErrorResponse						"Forbidden error"
//	@Failure		404					{object}	apierrors.APIErrorResponse						"Data not found error"
//	@Failure		409					{object}	apierrors.APIErrorResponse						"Data conflict error"
//	@Failure		500					{object}	apierrors.APIErrorResponse						"Internal server error"
//	@Router			/v1/pao-gen/ddo/{ddo-code}/transfer-entry/sub-accounts/reports [get]
func (uh *TransferEntryHandler) ListDdoTransferEntryReportHandler(ctx *gin.Context) {

	var req DdoTeRequest
	if err := ctx.ShouldBindUri(&req); err != nil {
		apierrors.HandleBindingError(ctx, err)
		log.Error(ctx, "Binding failed for DdoTeRequest: %s", err.Error())
		return
	}
	if err1 := ctx.ShouldBindQuery(&req); err1 != nil {
		apierrors.HandleBindingError(ctx, err1)
		log.Error(ctx, "Binding failed for DdoTeRequest: %s", err1.Error())
		return
	}
	if err := validation.ValidateStruct(req); err != nil {
		apierrors.HandleValidationError(ctx, err)
		log.Error(ctx, "Validation failed for DdoTeRequest: %s", err.Error())
		return
	}
	if req.Limit == 0 {
		req.Limit = math.MaxInt32
	}
	var request domain.DdoTeRequest
	request.DdoCode = req.DdoCode
	request.FromDate = req.FromDate
	request.ToDate = req.ToDate
	request.Status = req.Status
	res, err := uh.svc.DdoTransferentryReportRepo(ctx, request, req.MetaDataRequest)
	if err != nil {
		apierrors.HandleDBError(ctx, err)
		log.Error(ctx, "Transfer Entry Report Repo call failed: %s", err.Error())
		return
	}

	rsp := response.NewDdoTransferentryReportResponse(res)

	metadata := port.NewMetaDataResponse(req.Skip, req.Limit, len(rsp))

	apiRsp := response.DdoTransferentryReportResponse{
		StatusCodeAndMessage: port.FetchSucess,
		MetaDataResponse:     metadata,
		Data:                 rsp,
	}
	log.Debug(ctx, "ListDdoTransferEntryReportHandler response", apiRsp)
	handleSuccess(ctx, apiRsp)

}

type PaoSubTeRequest struct {
	Type     string `form:"type" validate:"required,oneof=1 2"`
	PaoCode  string `uri:"pao-code" binding:"required" validate:"required,validatePaocode"`
	FromDate string `form:"from-date" validate:"omitempty,date_yyyy_mm_dd"`
	ToDate   string `form:"to-date" validate:"omitempty,date_yyyy_mm_dd"`
	Status   string `form:"status" validate:"required,min=1,max=10"`
	port.MetaDataRequest
}

// ListPaoSubTransferEntryReportHandler godoc
//
//	@Summary		Get Transfer entry report from DDOs under the PAO
//	@Description	Get Transfer entry report from DDOs under the PAO
//	@Tags			Transfer Entry
//	@Accept			json
//	@Produce		json
//	@Param			type	query		string			true	"Type"
//	@Param			pao-code	path		string			true	"Pao_code"
//	@Param			from-date	query		string			true	"From_date"
//	@Param			to-date	query		string			true	"To_date"
//	@Param			status	query		string			true	"Status"
//
// @Param       skip    query       int     			false   		"Number of records to skip for pagination"
// @Param       limit   query       int     			false   		"Number of records to limit for pagination"
//
//	@Success		200		{object}	response.PaoSubTransferentryReportResponse			"data retrieved successfully"
//	@Failure		400					{object}	apierrors.APIErrorResponse						"Validation error"
//	@Failure		401					{object}	apierrors.APIErrorResponse						"Unauthorized error"
//	@Failure		403					{object}	apierrors.APIErrorResponse						"Forbidden error"
//	@Failure		404					{object}	apierrors.APIErrorResponse						"Data not found error"
//	@Failure		409					{object}	apierrors.APIErrorResponse						"Data conflict error"
//	@Failure		500					{object}	apierrors.APIErrorResponse						"Internal server error"
//	@Router			/v1/pao-gen/pao/{pao-code}/transfer-entry/sub-accounts/pao-reports [get]
func (uh *TransferEntryHandler) ListPaoSubTransferEntryReportHandler(ctx *gin.Context) {

	var req PaoSubTeRequest
	if err := ctx.ShouldBindUri(&req); err != nil {
		apierrors.HandleBindingError(ctx, err)
		log.Error(ctx, "Binding failed for PaoSubTeRequest: %s", err.Error())
		return
	}
	if err1 := ctx.ShouldBindQuery(&req); err1 != nil {
		apierrors.HandleBindingError(ctx, err1)
		log.Error(ctx, "Binding failed for PaoSubTeRequest: %s", err1.Error())
		return
	}
	if err := validation.ValidateStruct(req); err != nil {
		apierrors.HandleValidationError(ctx, err)
		log.Error(ctx, "Validation failed for PaoSubTeRequest: %s", err.Error())
		return
	}
	if req.Limit == 0 {
		req.Limit = math.MaxInt32
	}
	var request domain.PaoSubTeRequest
	request.Type = req.Type
	request.PaoCode = req.PaoCode
	request.FromDate = req.FromDate
	request.ToDate = req.ToDate
	request.Status = req.Status
	res, err := uh.svc.PaoSubTransferentryReportRepo(ctx, request, req.MetaDataRequest)
	if err != nil {
		apierrors.HandleDBError(ctx, err)
		log.Error(ctx, "PaoSubTransferentryReport Repo call failed: %s", err.Error())
		return
	}

	rsp := response.NewPaoSubTransferentryReportResponse(res)

	metadata := port.NewMetaDataResponse(req.Skip, req.Limit, len(rsp))

	apiRsp := response.PaoSubTransferentryReportResponse{
		StatusCodeAndMessage: port.FetchSucess,
		MetaDataResponse:     metadata,
		Data:                 rsp,
	}
	log.Debug(ctx, "ListPaoSubTransferEntryReportHandler response", apiRsp)
	handleSuccess(ctx, apiRsp)

}

type PaoSubTeDetailRequest struct {
	TransId string `uri:"trans-id" validate:"required,min=1,max=25"`
}

func getFinancialYear(date time.Time) string {
	year := date.Year()
	month := date.Month()

	if month >= time.April {
		// If the date is from April to December, it's the current year to the next yearkjjj,
		return fmt.Sprintf("%d", year)
	} else {
		// If the date is from January to March, it's the previous year to the current year
		return fmt.Sprintf("%d", year-1)
	}
}

type SubTeVerified struct {
	PaoCode          string    `json:"pao_code" select:"pao_code" validate:"required,validatePaocode"`
	DdoCode          string    `json:"ddo_code" select:"ddo_code" validate:"required,validateDdocode"`
	TransId          string    `json:"trans_id" select:"trans_id" validate:"required,max=30"`
	Hoa              string    `json:"hoa" select:"hoa" validate:"required,head_of_account"`
	AccountCode      string    `json:"account_code" select:"account_code" validate:"required,account_no"`
	TransferAmount   float64   `json:"transfer_amount" select:"transfer_amount" validate:"required"`
	TransferType     string    `json:"transfer_type" select:"transfer_type" validate:"required,max=20"`
	CreatedBy        int64     `json:"created_by" select:"created_by" validate:"required,employee_id"`
	CreatedDate      time.Time `json:"created_date" select:"created_date" validate:"required"`
	Status           string    `json:"status" select:"status" validate:"required,max=20"`
	ApprovedBy       int64     `json:"approved_by" select:"approved_by" validate:"required,employee_id"`
	ApprovedDate     time.Time `json:"approved_date" select:"approved_date" validate:"required"`
	ApproverRemarks  string    `json:"approver_remarks" validate:"required,max=255"`
	RemarksByCreator string    `json:"remarks_by_creator" validate:"max=255"`
	WorkflowId       string    `json:"workflow_id" validate:"max=30"`
	TransDate        time.Time `json:"trans_date"`
}

type SubTeVerifiedBullk struct {
	SubTes []SubTeVerified `json:"sub_tes" validate:"dive"`
}

// func (uh *TransferEntryHandler) CreateSubaccountsTeVerifiedHandler(ctx *gin.Context) {

// 	var request SubTeVerifiedBullk
// 	if err := ctx.ShouldBindJSON(&request); err != nil {
// 		apierrors.HandleBindingError(ctx, err)
// 		log.Error(ctx, "Binding failed for SubTeVerifiedBullk: %s", err.Error())
// 		return
// 	}
// 	if err := validation.ValidateStruct(request); err != nil {
// 		apierrors.HandleValidationError(ctx, err)
// 		log.Error(ctx, "Validation failed for SubTeVerifiedBullk: %s", err.Error())
// 		return
// 	}

// 	req := domain.SubTeVerifiedBullk{
// 		SubTes: convertSubTeVerifiedToSubTeVerified(request.SubTes),
// 	}
// 	currentTime := time.Now()
// 	formattedTime := currentTime.Format("2006-01-02 15:04:05")

// 	var trans_id string
// 	var status string
// 	var approved_by int64
// 	var remarks string
// 	var ddocode string
// 	var officeid int64
// 	var budget_request []domain.BudgetRequest
// 	var budget_req domain.BudgetRequest
// 	var date time.Time
// 	number_budget_hoa := 0

// 	for _, t := range req.SubTes {
// 		trans_id = t.TransId
// 		status = t.Status
// 		approved_by = t.ApprovedBy
// 		remarks = t.ApproverRemarks
// 		ddocode = t.DdoCode
// 		date = t.CreatedDate
// 	}

// 	for _, y := range req.SubTes {

// 		if strings.HasPrefix(y.Hoa, "5201") ||
// 			strings.HasPrefix(y.Hoa, "3201") ||
// 			strings.HasPrefix(y.Hoa, "2552") ||
// 			strings.HasPrefix(y.Hoa, "4552") {

// 			financialYear := getFinancialYear(date)

// 			u, q, err1 := uh.svc.GetOfficeIdRepo(ctx, ddocode)
// 			if err1 != nil {
// 				apierrors.HandleDBError(ctx, err1)
// 				log.Error(ctx, "GetOfficeIdRepo call failed: %s", err1.Error())
// 				return
// 			}
// 			if q {
// 				officeid = u.DdoOfficeId
// 			} else {
// 				err := errors.New("failed to get office_id")
// 				appError := apierrors.NewAppError(
// 					"failed to get office_id", // User-friendly error message
// 					"404",                     // Error code representing the error type
// 					err,                       // Original error for debugging purposes
// 				)
// 				apiErrorResponse := apierrors.NewAPIErrorResponse(
// 					http.StatusInternalServerError, // HTTP status code
// 					ErrInternalServerError,         // Message to return to the client
// 					appError,                       // Encapsulated application error
// 				)
// 				ctx.JSON(apiErrorResponse.StatusCode, apiErrorResponse)
// 				log.Error(ctx, "failed to get office_id: %s", err.Error())

// 				return
// 			}

// 			budget_req.FinancialYear = financialYear
// 			budget_req.OfficeId = officeid
// 			budget_req.Hoa = y.Hoa
// 			if y.TransferType == "C" {
// 				budget_req.ConsumedAmount = -y.TransferAmount
// 			} else {
// 				budget_req.ConsumedAmount = y.TransferAmount
// 			}
// 			budget_req.Remarks = y.TransId
// 			budget_req.UpdatedBy = y.CreatedBy
// 			budget_req.TransactionOffice = officeid

// 			budget_request = append(budget_request, budget_req)

// 			number_budget_hoa++
// 		}

// 	}

// 	//call budget api with input as budget_request and check result. If "success" is true, then add budget unique id to req and follow following operations.

// 	server := uh.cfg.GetString("db.ApiUrl")
// 	url := fmt.Sprintf("https://%s/besubaccounts/v1/transfer-entries/%s", server, trans_id)
// 	method := "PUT"
// 	header := map[string]string{
// 		"Content-Type": "application/json",
// 	}
// 	params := map[string]interface{}{
// 		"status":        status,
// 		"approved_by":   approved_by,
// 		"approved_date": formattedTime,
// 		"remarks":       remarks,
// 	}
// 	apiRsp := response.PaoSubTransferentryReportResponse{
// 		StatusCodeAndMessage: port.CreateSuccess,
// 	}

// 	if number_budget_hoa > 0 {

// 		//making ready the budget api call
// 		url_budget := fmt.Sprintf("https://%s/bebudget/budget/v1/consumption/addconsumptionte", server)
// 		method_budget := "POST"
// 		// Wrap the budget_request in a map
// 		params_budget := map[string]interface{}{
// 			"sub_tes": budget_request,
// 		}

// 		//calls subaccounts api and make sure it is updated.
// 		response, err := uh.CallAPI(url, method, header, params)
// 		if err != nil {
// 			ctx.JSON(http.StatusInternalServerError, gin.H{"error": err.Error()})
// 			return
// 		}

// 		if !response["success"].(bool) {
// 			ctx.JSON(http.StatusBadRequest, gin.H{"error": response["message"]})
// 			return
// 		} else {

// 			response, err := uh.CallAPI(url_budget, method_budget, header, params_budget)
// 			if err != nil {
// 				ctx.JSON(http.StatusInternalServerError, gin.H{"error": err.Error()})
// 				return
// 			}

// 			if !response["success"].(bool) {
// 				ctx.JSON(http.StatusBadRequest, gin.H{"error": response["message"]})
// 				return
// 			} else {
// 				error := uh.svc.SubVerifiedTePostingRepo(ctx, req)
// 				if error != nil {
// 					log.Error(ctx, "SubVerifiedTePosting Repo call failed: %s", error.Error())
// 					if error.(*pgconn.PgError).Code == "23505" {
// 						err1 := errors.New("transfer entry verification failed")
// 						// Create an AppError with a user-friendly message and code.
// 						appError := apierrors.NewAppError(
// 							"transferentry already verified", // User-friendly error message
// 							"409",                            // Error code representing the error type
// 							err1,                             // Original error for debugging purposes
// 						)
// 						apiErrorResponse := apierrors.NewAPIErrorResponse(
// 							http.StatusConflict, // HTTP status code
// 							"Conflict",          // Message to return to the client
// 							appError,            // Encapsulated application error
// 						)
// 						ctx.JSON(apiErrorResponse.StatusCode, apiErrorResponse)
// 						return
// 					} else {
// 						apierrors.HandleDBError(ctx, err)
// 						return
// 					}
// 				}
// 				log.Debug(ctx, "CreateSubaccountsTeVerifiedHandler response", apiRsp)
// 				handleCreateSuccess(ctx, apiRsp)
// 				return
// 			}
// 		}
// 	} else {

// 		//calls subaccounts api
// 		response, err := uh.CallAPI(url, method, header, params)
// 		if err != nil {
// 			ctx.JSON(http.StatusInternalServerError, gin.H{"error": err.Error()})
// 			return
// 		}

// 		if !response["success"].(bool) {
// 			ctx.JSON(http.StatusBadRequest, gin.H{"error": response["message"]})
// 			return
// 		} else {

// 			err1 := uh.svc.SubVerifiedTePostingRepo(ctx, req)
// 			if err1 != nil {
// 				log.Error(ctx, "SubVerifiedTePosting Repo call failed: %s", err1.Error())
// 				if err1.(*pgconn.PgError).Code == "23505" {
// 					err1 := errors.New("transfer entry verification failed")
// 					// Create an AppError with a user-friendly message and code.
// 					appError := apierrors.NewAppError(
// 						"transferentry already verified", // User-friendly error message
// 						"409",                            // Error code representing the error type
// 						err1,                             // Original error for debugging purposes
// 					)
// 					apiErrorResponse := apierrors.NewAPIErrorResponse(
// 						http.StatusConflict, // HTTP status code
// 						"Conflict",          // Message to return to the client
// 						appError,            // Encapsulated application error
// 					)
// 					ctx.JSON(apiErrorResponse.StatusCode, apiErrorResponse)
// 					return
// 				} else {
// 					apierrors.HandleDBError(ctx, err1)
// 					return
// 				}
// 			}
// 			log.Debug(ctx, "CreateSubaccountsTeVerifiedHandler response", apiRsp)
// 			handleCreateSuccess(ctx, apiRsp)
// 			return
// 		}

// 	}

// }

// FetchPaoSubTransferentryDetailHandler godoc
//
//	@Summary		Get Transfer entry details
//	@Description	Get Transfer entry details
//	@Tags			Transfer Entry
//	@Accept			json
//	@Produce		json
//	@Param			trans-id	path		string			true	"Trans_id"
//
// @Param       skip    query       int     			false   		"Number of records to skip for pagination"
// @Param       limit   query       int     			false   		"Number of records to limit for pagination"
//
//	@Success		200		{object}	response.PaoSubTransferentryDetailResponse			"data retrieved successfully"
//	@Failure		400					{object}	apierrors.APIErrorResponse						"Validation error"
//	@Failure		401					{object}	apierrors.APIErrorResponse						"Unauthorized error"
//	@Failure		403					{object}	apierrors.APIErrorResponse						"Forbidden error"
//	@Failure		404					{object}	apierrors.APIErrorResponse						"Data not found error"
//	@Failure		409					{object}	apierrors.APIErrorResponse						"Data conflict error"
//	@Failure		500					{object}	apierrors.APIErrorResponse						"Internal server error"
//	@Router			/v1/transfer-entry/sub-accounts/details/{trans-id} [get]
func (uh *TransferEntryHandler) FetchPaoSubTransferentryDetailHandler(ctx *gin.Context) {

	var req PaoSubTeDetailRequest
	if err := ctx.ShouldBindUri(&req); err != nil {
		apierrors.HandleBindingError(ctx, err)
		log.Error(ctx, "Binding failed for PaoSubTeDetailRequest: %s", err.Error())
		return
	}
	if err := validation.ValidateStruct(req); err != nil {
		apierrors.HandleValidationError(ctx, err)
		log.Error(ctx, "Validation failed for PaoSubTeDetailRequest: %s", err.Error())
		return
	}
	var request domain.PaoSubTeDetailRequest
	request.TransId = req.TransId

	res, err := uh.svc.PaoSubTransferentryDetailRepo(ctx, request)
	if err != nil {
		apierrors.HandleDBError(ctx, err)
		log.Error(ctx, "PaoSubTransferentryDetail Repo call failed: %s", err.Error())
		return
	}

	rsp := response.NewPaoSubTransferentryDetailResponse(res)

	metadata := port.NewMetaDataResponse(0, 0, len(rsp))

	apiRsp := response.PaoSubTransferentryDetailResponse{
		StatusCodeAndMessage: port.FetchSucess,
		MetaDataResponse:     metadata,
		Data:                 rsp,
	}
	log.Debug(ctx, "FetchPaoSubTransferentryDetailHandler response", apiRsp)
	handleSuccess(ctx, apiRsp)

}

type TeData struct {
	TeId    string `json:"te_id" validate:"required,max=30"`
	TeDate  string `json:"te_date" validate:"required,max=30"`
	PaoCode string `json:"pao_code" validate:"required,validatePaocode"`
	FinYear string `json:"fin_year" validate:"required,year"`
}

func (uh *TransferEntryHandler) ConvertMapToStringMap(params map[string]interface{}) map[string]string {
	stringParams := make(map[string]string)
	for key, value := range params {
		stringParams[key] = fmt.Sprintf("%v", value)
	}
	return stringParams

}
func (uh *TransferEntryHandler) CallAPI(url string, method string, headers map[string]string, params interface{}) (map[string]interface{}, error) {
	tr := &http.Transport{
		TLSClientConfig: &tls.Config{
			MinVersion:         tls.VersionTLS12,
			InsecureSkipVerify: false,
			Renegotiation:      tls.RenegotiateOnceAsClient,
		},
		DisableKeepAlives: true,
	}

	client := resty.New().SetTimeout(30 * time.Second)
	client.SetTransport(tr)
	request := client.R()
	request.SetHeaders(headers)

	switch method {
	case "GET":
		if paramsMap, ok := params.(map[string]interface{}); ok {
			stringParams := uh.ConvertMapToStringMap(paramsMap)
			request.SetQueryParams(stringParams)
		} else if params != nil {
			return nil, fmt.Errorf("params for GET must be map[string]interface{}")
		}
	case "POST", "PUT", "DELETE", "PATCH":
		if params != nil {
			request.SetBody(params)
		}
	}

	response, err := request.Execute(method, url)
	if err != nil {
		return nil, err
	}

	var responseBody map[string]interface{}
	err = json.Unmarshal(response.Body(), &responseBody)
	if err != nil {
		return nil, err
	}
	return responseBody, nil
}

// CreatePfmsTeHandler godoc
//
//	@Summary		Generate PFMS for Trasfer Entry
//	@Description	Generate PFMS for Trasfer Entry
//	@Tags			Transfer Entry
//	@Accept			json
//	@Produce		json
//	@Param			body	body		[]TeData	true	"Generated TE PFMS request"
//	@Success		201		{object}	response.GetPfmsSubmissionPendingResponse			"list retrieved successfully"
//	@Failure		400					{object}	apierrors.APIErrorResponse						"Validation error"
//	@Failure		401					{object}	apierrors.APIErrorResponse						"Unauthorized error"
//	@Failure		403					{object}	apierrors.APIErrorResponse						"Forbidden error"
//	@Failure		404					{object}	apierrors.APIErrorResponse						"Data not found error"
//	@Failure		409					{object}	apierrors.APIErrorResponse						"Data conflict error"
//	@Failure		500					{object}	apierrors.APIErrorResponse						"Internal server error"
//	@Router			/v1/transfer-entry/pfms-submission [post]
func (uh *TransferEntryHandler) CreatePfmsTeHandler(ctx *gin.Context) {
	var cbds []TeData
	if err := ctx.ShouldBindJSON(&cbds); err != nil {
		apierrors.HandleBindingError(ctx, err)
		log.Error(ctx, "Binding failed for TeData: %s", err.Error())
		return
	}
	for _, r := range cbds {
		if err := validation.ValidateStruct(r); err != nil {
			apierrors.HandleValidationError(ctx, err)
			log.Error(ctx, "Validation failed for TeData: %s", err.Error())
			return
		}
	}
	var requests []domain.TeData
	var PfmsPayload domain.Payload
	var Paocode string
	var FinYear string
	var Tedate string

	for _, request := range cbds {
		Paocode = request.PaoCode
		Tedate = request.TeDate

		teDate, err := time.Parse("2006-01-02", request.TeDate)
		if err != nil {
			log.Error(ctx, "Invalid CbDate format for DdoCode: %s", err.Error())
			return
		}

		// Determine ending year of financial year

		year := teDate.Year()
		month := teDate.Month()
		if month >= time.April {
			FinYear = fmt.Sprintf("%d", year+1)
		} else {
			FinYear = fmt.Sprintf("%d", year)
		}

		requests = append(requests, domain.TeData{

			TeId:    request.TeId,
			TeDate:  request.TeDate,
			PaoCode: request.PaoCode,
			FinYear: request.FinYear,
		})
	}

	pfmsjson, err := uh.svc.GetPfmsteRepo(ctx, requests)
	if err != nil {
		apierrors.HandleDBError(ctx, err)
		log.Error(ctx, "Get PFMS Repo call failed: %s", err.Error())
		return
	}
	if len(pfmsjson) == 0 {
		err := fmt.Errorf("No effective Debit or Credit to any HOA: This happens when both account codes are mapped to same HOA and resulting in net zero transaction")
		apierrors.HandleDBError(ctx, err)
		log.Error(ctx, "Get PFMS Repo returned empty result: %s", err.Error())
		return
	}
	for i := range pfmsjson {
		if pfmsjson[i].Amount.Valid && pfmsjson[i].Amount.Float64 < 0 {
			// Flip the sign
			if pfmsjson[i].Sign.Valid {
				if pfmsjson[i].Sign.String == "+" {
					pfmsjson[i].Sign.String = "-"
				} else if pfmsjson[i].Sign.String == "-" {
					pfmsjson[i].Sign.String = "+"
				}
			} else {
				// If Sign is null, default to "-" when amount is negative
				pfmsjson[i].Sign = null.StringFrom("-")
			}

			// Make amount positive
			pfmsjson[i].Amount.Float64 = -pfmsjson[i].Amount.Float64
		}
	}
	finYearInt, err := strconv.Atoi(FinYear)
	if err != nil {
		log.Error(ctx, "Failed to convert FinYear to integer: %s", err.Error())
		return
	}
	transferEntry := domain.TransferEntryDetail{
		UniqueIdentifier: GenerateRandomNumber(Paocode, FinYear),
		RequestSource:    "POST",
		PaoCode:          Paocode,
		FinancialYear:    finYearInt,
		TransferEntryData: domain.TransferEntryData{
			InstrumentType:                 "Others",
			Remarks:                        "DoP Daily Account",
			TEDate:                         Tedate,
			TransferEntryAccountingDetails: ConvertToDetailsArray(pfmsjson),
		},
	}

	// Assign it as a slice with one element
	PfmsPayload.RequestPayload.TransferEntryDetails = []domain.TransferEntryDetail{transferEntry}

	username := uh.cfg.GetString("pfms.username")
	requestsource := uh.cfg.GetString("pfms.requestsource")
	password := uh.cfg.GetString("pfms.password")
	baseurl := uh.cfg.GetString("pfms.baseurl")
	// var username = "POSTwebsvc"
	// var requestsource = "POST"
	// var password = "jhI5nAdyb1qOEjmcB3JvWrHRYyr2pv8PRhzu6Flbp2U="
	var authcode string
	var accesstoken string
	url := baseurl + "/GetAuthCode"
	method := "POST"
	header := map[string]string{
		"Content-Type": "application/json",
	}
	params := map[string]interface{}{
		"UserName":      username,
		"RequestSource": requestsource,
	}

	response1, err := uh.CallAPI(url, method, header, params)
	if err != nil {
		apierrors.HandleError(ctx, err)
		return
	}
	success := response1["IsSuccess"].(string)
	if success == "0" {
		ctx.JSON(http.StatusBadRequest, gin.H{"error": response1["errorMessage"]})
		return
	}
	authcode = response1["AuthCode"].(string)

	// Second API call: Login
	url1 := baseurl + "/LogIn"
	method1 := "POST"
	header1 := map[string]string{
		"Content-Type": "application/json",
	}
	params1 := map[string]interface{}{
		"userName": username,
		"password": password + authcode,
	}

	response2, err1 := uh.CallAPI(url1, method1, header1, params1)
	if err1 != nil {
		apierrors.HandleError(ctx, err1)
		return
	}
	success = response2["isSuccess"].(string)
	if success == "0" {
		ctx.JSON(http.StatusBadRequest, gin.H{"error": response2["errorMessage"]})
		return
	}
	accesstoken = response2["accessToken"].(string)

	// Third API call: Send PfmsPayload
	url2 := baseurl + "/Budget/ReceiveTransferEntryData" // Corrected URL
	method2 := "POST"
	header2 := map[string]string{
		"Content-Type":  "application/json",
		"Authorization": "Bearer " + accesstoken,
	}
	params2 := PfmsPayload // Pass the struct directly

	response3, err := uh.CallAPI(url2, method2, header2, params2)
	if err != nil {
		apierrors.HandleError(ctx, err)
		return
	}
	success = response3["isSuccess"].(string)
	if success == "0" {
		ctx.JSON(http.StatusBadRequest, gin.H{"error": response3["errorMessage"]})
		return
	}
	err4 := uh.svc.GetTePfmsUpdateStatusRepo(ctx, requests, transferEntry.UniqueIdentifier)
	if err4 != nil {
		apierrors.HandleDBError(ctx, err4)
		log.Error(ctx, "Get PFMS Repo call failed: %s", err4.Error())
		return
	}

	errInsert := uh.svc.InsertTePfmsSubmission(
		ctx,
		transferEntry.UniqueIdentifier,
		"te",            // Since we are submitting cashbook
		domain.CbData{}, // Store API requests in cb_request
		requests,        // te_request is empty (null in JSONB)
		Tedate,          // Business date
		time.Now(),      // Submission date
		PfmsPayload,     // Payload sent to PFMS API
		"Pending",       // submissionStatus set to "Pending"
		"",              // errorDescription is null
	)
	if errInsert != nil {
		log.Error(ctx, "Failed to insert into pao.pfms_submission: %v", errInsert)
	}

	var output domain.PfmsSubmissionPending
	output.PfmsUniqueId = null.StringFrom(transferEntry.UniqueIdentifier)
	var outputs []domain.PfmsSubmissionPending
	outputs = append(outputs, output)

	rsp := response.NewFetchPfmsSubmissionPendingResponse(outputs)

	apiRsp := response.GetPfmsSubmissionPendingResponse{
		StatusCodeAndMessage: port.InsertSuccess,
		Data:                 rsp,
	}
	log.Debug(ctx, "FetchPfmsNewHandler response", apiRsp)
	handleSuccess(ctx, apiRsp)
}

type TransferEntryDirectRequest struct {
	PaoCode            string  `json:"pao_code" validate:"required,validatePaocode"`
	DdoCode            string  `json:"ddo_code" select:"ddo_code" validate:"required,validateDdocode"`
	Hoa                string  `json:"hoa" validate:"required,head_of_account"`
	TransferAmount     float64 `json:"transfer_amount" validate:"required"`
	TransferType       string  `json:"transfer_type" validate:"required,max=20"`
	CreatedBy          uint64  `json:"created_by" validate:"required,employee_id"`
	VerifiedBy         uint64  `json:"verified_by" select:"verified_by" validate:"required,employee_id"`
	TeSourceOfficeType string  `json:"te_source_office_type" select:"te_source_office_type" validate:"required,max=50"`
	Remarks            string  `json:"remarks" validate:"required,max=255"`
}
type TransferEntryDirectRequests struct {
	TransferEntries []TransferEntryDirectRequest `json:"transfer_entries" validate:"dive"`
}

// CreateTransferEntryDirectHandler godoc
//
//	@Summary		Create Transfer Entry Direct
//	@Description	Create Transfer Entry Direct
//	@Tags			Transfer Entry
//	@Accept			json
//	@Produce		json
//	@Param			body	body		[]domain.TransferEntryDirectRequest	true	"Transfer Entry Direct creation request"
//	@Success		201		{object}	response.GetTransferentryCreationResponse			"resource created successfully"
//	@Failure		400					{object}	apierrors.APIErrorResponse						"Validation error"
//	@Failure		401					{object}	apierrors.APIErrorResponse						"Unauthorized error"
//	@Failure		403					{object}	apierrors.APIErrorResponse						"Forbidden error"
//	@Failure		404					{object}	apierrors.APIErrorResponse						"Data not found error"
//	@Failure		409					{object}	apierrors.APIErrorResponse						"Data conflict error"
//	@Failure		500					{object}	apierrors.APIErrorResponse						"Internal server error"
//	@Router			/v1/transfer-entry/direct [post]
func (uh *TransferEntryHandler) CreateTransferEntryDirectHandler(ctx *gin.Context) {

	var req []TransferEntryDirectRequest
	if err := ctx.ShouldBindJSON(&req); err != nil {
		apierrors.HandleBindingError(ctx, err)
		log.Error(ctx, "Binding failed for TransferEntryRequests: %s", err.Error())
		return
	}
	for _, r := range req {
		if err := validation.ValidateStruct(r); err != nil {
			apierrors.HandleValidationError(ctx, err)
			log.Error(ctx, "Validation failed for TransferEntryRequests: %s", err.Error())
			return
		}
	}
	currentTime := time.Now()
	createdTimeString := currentTime.Format("20060102150405")

	// Derive trans_date from currentTime — date only, time zeroed out
	transDate := time.Date(currentTime.Year(), currentTime.Month(), currentTime.Day(), 0, 0, 0, 0, currentTime.Location())

	var request []domain.TransferEntryDirectRequest

	for _, requ := range req {

		request = append(request, domain.TransferEntryDirectRequest{

			PaoCode:             requ.PaoCode,
			DdoCode:             requ.DdoCode,
			Hoa:                 requ.Hoa,
			TransferAmount:      requ.TransferAmount,
			TransferType:        requ.TransferType,
			CreatedBy:           requ.CreatedBy,
			CreatedDate:         currentTime,
			TransDate:           transDate,
			TransferEntryId:     requ.PaoCode + createdTimeString,
			HPfmsGenerationFlag: false,
			TeSourceOfficeType:  "PAO",
			Remarks:             requ.Remarks,
			VerificationStatus:  "verified",
			VerifiedBy:          requ.VerifiedBy,
			VerifiedDate:        currentTime,
		})
	}

	var Total_debit float64 = 0
	var Total_credit float64 = 0
	var Inserted_Ids []domain.InsertedIds
	var err error
	for _, r := range request {
		if r.TransferType == "D" {
			Total_debit = Total_debit + r.TransferAmount
		}
		if r.TransferType == "C" {
			Total_credit = Total_credit + r.TransferAmount
		}
	}
	if Total_credit == Total_debit {
		Inserted_Ids, err = uh.svc.TransferentryDirectCreationRepo(ctx, request)
		if err != nil {
			apierrors.HandleDBError(ctx, err)
			log.Error(ctx, "Transfer Entry Creation Repo call failed: %s", err.Error())
			return
		}
	} else {
		err := errors.New("total debit not equal to total credit")
		appError := apierrors.NewAppError(
			"Debit Credit error", // User-friendly error message
			"422",                // Error code representing the error type
			err,                  // Original error for debugging purposes
		)
		apiErrorResponse := apierrors.NewAPIErrorResponse(
			http.StatusInternalServerError, // HTTP status code
			ErrInternalServerError,         // Message to return to the client
			appError,                       // Encapsulated application error
		)
		ctx.JSON(apiErrorResponse.StatusCode, apiErrorResponse)
		log.Error(ctx, "total debit not equal to total credit: %s", err.Error())
		return
	}
	rsp := response.NewGTransferentryCreationResponse(Inserted_Ids)

	metadata := port.NewMetaDataResponse(0, 0, len(rsp))

	apiRsp := response.GetTransferentryCreationResponse{
		StatusCodeAndMessage: port.CreateSuccess,
		MetaDataResponse:     metadata,
		Data:                 rsp,
	}
	log.Debug(ctx, "CreateTransferEntryHandler response", apiRsp)
	handleCreateSuccess(ctx, apiRsp)

}
func safeConvertToUint64(val int64) (uint64, error) {
	if val < 0 {
		return 0, fmt.Errorf("cannot convert negative int64 to uint64")
	}
	return uint64(val), nil
}
func NewBudgetREquesttoBudgetTransferEntryVerificationInput(requests []domain.BudgetRequest) ([]contracts.BudgetTransferEntryVerificationInput, error) {
	var response []contracts.BudgetTransferEntryVerificationInput
	for _, request := range requests {
		officeId, err := safeConvertToUint64(request.OfficeId)
		if err != nil {
			return nil, err
		}

		updatedBy, err := safeConvertToUint64(request.UpdatedBy)
		if err != nil {
			return nil, err
		}

		transactionOffice, err := safeConvertToUint64(request.TransactionOffice)
		if err != nil {
			return nil, err
		}
		requestResponse := contracts.BudgetTransferEntryVerificationInput{
			FinancialYear:     request.FinancialYear,
			OfficeId:          officeId,
			Hoa:               request.Hoa,
			ConsumedAmount:    request.ConsumedAmount,
			Remarks:           request.Remarks,
			UpdatedBy:         updatedBy,
			TransactionOffice: transactionOffice,
			SourceModule:      request.SourceModule, //added on 16-07-2026
		}
		response = append(response, requestResponse)
	}
	return response, nil
}

// CreateSubaccountsTeVerifiedTempoHandler godoc
//
//	@Summary		Post verified Transfer entry from Sub Account
//	@Description	Post verified Transfer entry from Sub Account
//	@Tags			Transfer Entry
//	@Accept			json
//	@Produce		json
//	@Param			body	body		SubTeVerifiedBullk	true	"Transfer entry post request"
//	@Success		201		{object}	response.PaoSubTransferentryReportResponse			"CreateSubaccountsTeVerifiedTempoHandler"
//	@Failure		400					{object}	apierrors.APIErrorResponse						"Validation error"
//	@Failure		401					{object}	apierrors.APIErrorResponse						"Unauthorized error"
//	@Failure		403					{object}	apierrors.APIErrorResponse						"Forbidden error"
//	@Failure		404					{object}	apierrors.APIErrorResponse						"Data not found error"
//	@Failure		409					{object}	apierrors.APIErrorResponse						"Data conflict error"
//	@Failure		500					{object}	apierrors.APIErrorResponse						"Internal server error"
//	@Router			/v1/transfer-entry/sub-accounts/verification [post]
func (uh *TransferEntryHandler) CreateSubaccountsTeVerifiedTempoHandler(ctx *gin.Context) {

	var request SubTeVerifiedBullk
	if err := ctx.ShouldBindJSON(&request); err != nil {
		apierrors.HandleBindingError(ctx, err)
		log.Error(ctx, "Binding failed for SubTeVerifiedBullk: %s", err.Error())
		return
	}
	if err := validation.ValidateStruct(request); err != nil {
		apierrors.HandleValidationError(ctx, err)
		log.Error(ctx, "Validation failed for SubTeVerifiedBullk: %s", err.Error())
		return
	}

	workflowid := uuid.NewString()
	req := domain.SubTeVerifiedBullk{
		SubTes: convertSubTeVerifiedToSubTeVerified(request.SubTes),
	}

	for i := range req.SubTes {
		req.SubTes[i].WorkflowId = workflowid
	}

	currentTime := time.Now()
	formattedTime := currentTime.Format("2006-01-02 15:04:05")

	var trans_id string
	var status string
	var approved_by int64
	var remarks string
	var ddocode string
	var officeid int64
	var budget_request []domain.BudgetRequest
	var budget_req domain.BudgetRequest
	var date time.Time
	number_budget_hoa := 0

	for _, t := range req.SubTes {
		trans_id = t.TransId
		status = t.Status
		approved_by = t.ApprovedBy
		remarks = t.ApproverRemarks
		ddocode = t.DdoCode
		date = t.CreatedDate // ← using CreatedDate since body sends created_date
	}

	// ✅ Save workflow_id immediately before starting workflow
	type UpdateWorkflowIdRequest struct {
		WorkflowId string `json:"workflow_id"`
	}
	workflowReq := UpdateWorkflowIdRequest{WorkflowId: workflowid}
	url_workflowid := fmt.Sprintf("%s/%s/workflow_id", uh.cfg.GetString("urls.subaccountscall5"), trans_id)
	_, err := uh.CallAPI(url_workflowid, "PATCH", map[string]string{"Content-Type": "application/json"}, workflowReq)
	if err != nil {
		log.Error(ctx, "Failed to save workflow_id to subaccounts: %s", err.Error())
		apierrors.HandleDBError(ctx, err)
		return
	}

	for _, y := range req.SubTes {

		// ✅ shouldPostToBudget — ALL matching HOAs must flow to budget
		if shouldPostToBudget(y.Hoa) {

			financialYear := getFinancialYear(date)

			u, q, err1 := uh.svc.GetOfficeIdRepo(ctx, ddocode)
			if err1 != nil {
				apierrors.HandleDBError(ctx, err1)
				log.Error(ctx, "GetOfficeIdRepo call failed: %s", err1.Error())
				return
			}
			if q {
				officeid = u.DdoOfficeId
			} else {
				err := errors.New("failed to get office_id")
				appError := apierrors.NewAppError(
					"failed to get office_id",
					"404",
					err,
				)
				apiErrorResponse := apierrors.NewAPIErrorResponse(
					http.StatusInternalServerError,
					ErrInternalServerError,
					appError,
				)
				ctx.JSON(apiErrorResponse.StatusCode, apiErrorResponse)
				log.Error(ctx, "failed to get office_id: %s", err.Error())
				return
			}

			budget_req.FinancialYear = financialYear
			budget_req.OfficeId = officeid
			budget_req.Hoa = y.Hoa
			if y.TransferType == "C" {
				budget_req.ConsumedAmount = -y.TransferAmount
			} else {
				budget_req.ConsumedAmount = y.TransferAmount
			}
			budget_req.Remarks = y.TransId
			budget_req.UpdatedBy = y.CreatedBy
			budget_req.TransactionOffice = officeid
			budget_req.SourceModule = "PAO" //added on 16-07-2026

			// ✅ Always append to budget_request regardless of check exemption
			budget_request = append(budget_request, budget_req)
			number_budget_hoa++

			// ✅ shouldCheckBudget — exempted object heads (01,04,05,07,70 under 3201)
			// skip validation but data is already posted to budget above
			if shouldCheckBudget(y.Hoa) {
				// budget validation logic goes here if needed in future
			}
		}
	}

	workflowOptions := client.StartWorkflowOptions{
		ID:        workflowid,
		TaskQueue: contracts.PAOTaskQueue,
	}
	Budgetinput, err3 := NewBudgetREquesttoBudgetTransferEntryVerificationInput(budget_request)
	if err3 != nil {
		apierrors.HandleDBError(ctx, err3)
		log.Error(ctx, "Budget Input Conversion Failed: %s", err3.Error())
		return
	}

	Subaccountinput := contracts.SubAccountTransferEntryVerificationInput{
		TransId:    trans_id,
		WorkflowId: workflowid,
		SubAccArrayInput: []contracts.SubAccountTransferEntryVerificationArrayInput{
			{
				Status:       status,
				ApprovedBy:   approved_by,
				ApprovedDate: formattedTime,
				Remarks:      remarks,
			},
		},
	}

	_, err = uh.client.ExecuteWorkflow(ctx, workflowOptions, uh.svc.TransferentryverificationWorkflow, Budgetinput, Subaccountinput, req)
	if err != nil {
		apierrors.HandleDBError(ctx, err)
		return
	}

	apiRsp := response.PaoSubTransferentryReportResponse{
		StatusCodeAndMessage: port.SubmissionSuccess,
	}
	handleCreateSuccess(ctx, apiRsp)
}

func (uh *TransferEntryHandler) CreateSubaccountsTeVerifiedTempoHandler03062026(ctx *gin.Context) {

	var request SubTeVerifiedBullk
	if err := ctx.ShouldBindJSON(&request); err != nil {
		apierrors.HandleBindingError(ctx, err)
		log.Error(ctx, "Binding failed for SubTeVerifiedBullk: %s", err.Error())
		return
	}
	if err := validation.ValidateStruct(request); err != nil {
		apierrors.HandleValidationError(ctx, err)
		log.Error(ctx, "Validation failed for SubTeVerifiedBullk: %s", err.Error())
		return
	}

	workflowid := uuid.NewString()
	req := domain.SubTeVerifiedBullk{
		SubTes: convertSubTeVerifiedToSubTeVerified(request.SubTes),
	}

	for i := range req.SubTes {
		req.SubTes[i].WorkflowId = workflowid
	}

	currentTime := time.Now()
	formattedTime := currentTime.Format("2006-01-02 15:04:05")

	var trans_id string
	var status string
	var approved_by int64
	var remarks string
	var ddocode string
	var officeid int64
	var budget_request []domain.BudgetRequest
	var budget_req domain.BudgetRequest
	var date time.Time //to be uncommented on 10042026
	number_budget_hoa := 0

	for _, t := range req.SubTes {
		trans_id = t.TransId
		status = t.Status
		approved_by = t.ApprovedBy
		remarks = t.ApproverRemarks
		ddocode = t.DdoCode
		date = t.TransDate //to be uncommented on 10042026
	}

	// ✅ Save workflow_id immediately before starting workflow
	type UpdateWorkflowIdRequest struct {
		WorkflowId string `json:"workflow_id"`
	}
	workflowReq := UpdateWorkflowIdRequest{WorkflowId: workflowid}
	url_workflowid := fmt.Sprintf("%s/%s/workflow_id", uh.cfg.GetString("urls.subaccountscall5"), trans_id)
	_, err := uh.CallAPI(url_workflowid, "PATCH", map[string]string{"Content-Type": "application/json"}, workflowReq)
	if err != nil {
		log.Error(ctx, "Failed to save workflow_id to subaccounts: %s", err.Error())
		apierrors.HandleDBError(ctx, err)
		return
	}

	for _, y := range req.SubTes {

		// if strings.HasPrefix(y.Hoa, "5201") ||
		// 	strings.HasPrefix(y.Hoa, "3201") ||
		// 	strings.HasPrefix(y.Hoa, "7610") ||
		// 	strings.HasPrefix(y.Hoa, "801606101010500") ||
		// 	strings.HasPrefix(y.Hoa, "801606101020500") ||
		// 	strings.HasPrefix(y.Hoa, "2552") ||
		// 	strings.HasPrefix(y.Hoa, "4552") {
		if shouldCheckBudget(y.Hoa) { // ← ONLY THIS LINE CHANGED

			financialYear := getFinancialYear(date) //to be uncommented on 10042026
			// financialYear := "2025"

			// financialYear := "2025"

			u, q, err1 := uh.svc.GetOfficeIdRepo(ctx, ddocode)
			if err1 != nil {
				apierrors.HandleDBError(ctx, err1)
				log.Error(ctx, "GetOfficeIdRepo call failed: %s", err1.Error())
				return
			}
			if q {
				officeid = u.DdoOfficeId
			} else {
				err := errors.New("failed to get office_id")
				appError := apierrors.NewAppError(
					"failed to get office_id",
					"404",
					err,
				)
				apiErrorResponse := apierrors.NewAPIErrorResponse(
					http.StatusInternalServerError,
					ErrInternalServerError,
					appError,
				)
				ctx.JSON(apiErrorResponse.StatusCode, apiErrorResponse)
				log.Error(ctx, "failed to get office_id: %s", err.Error())
				return
			}

			budget_req.FinancialYear = financialYear
			budget_req.OfficeId = officeid
			budget_req.Hoa = y.Hoa
			if y.TransferType == "C" {
				budget_req.ConsumedAmount = -y.TransferAmount
			} else {
				budget_req.ConsumedAmount = y.TransferAmount
			}
			budget_req.Remarks = y.TransId
			budget_req.UpdatedBy = y.CreatedBy
			budget_req.TransactionOffice = officeid

			budget_request = append(budget_request, budget_req)
			number_budget_hoa++
		}
	}

	workflowOptions := client.StartWorkflowOptions{
		ID:        workflowid,
		TaskQueue: contracts.PAOTaskQueue,
	}
	Budgetinput, err3 := NewBudgetREquesttoBudgetTransferEntryVerificationInput(budget_request)
	if err3 != nil {
		apierrors.HandleDBError(ctx, err3)
		log.Error(ctx, "Budget Input Conversion Failed: %s", err3.Error())
		return
	}

	Subaccountinput := contracts.SubAccountTransferEntryVerificationInput{
		TransId:    trans_id,
		WorkflowId: workflowid,
		SubAccArrayInput: []contracts.SubAccountTransferEntryVerificationArrayInput{
			{
				Status:       status,
				ApprovedBy:   approved_by,
				ApprovedDate: formattedTime,
				Remarks:      remarks,
			},
		},
	}

	_, err = uh.client.ExecuteWorkflow(ctx, workflowOptions, uh.svc.TransferentryverificationWorkflow, Budgetinput, Subaccountinput, req)
	if err != nil {
		apierrors.HandleDBError(ctx, err)
		return
	}

	apiRsp := response.PaoSubTransferentryReportResponse{
		StatusCodeAndMessage: port.SubmissionSuccess,
	}
	handleCreateSuccess(ctx, apiRsp)
}

func (uh *TransferEntryHandler) CreateSubaccountsTeVerifiedTempoHandlerold09042026(ctx *gin.Context) {

	var request SubTeVerifiedBullk
	if err := ctx.ShouldBindJSON(&request); err != nil {
		apierrors.HandleBindingError(ctx, err)
		log.Error(ctx, "Binding failed for SubTeVerifiedBullk: %s", err.Error())
		return
	}
	if err := validation.ValidateStruct(request); err != nil {
		apierrors.HandleValidationError(ctx, err)
		log.Error(ctx, "Validation failed for SubTeVerifiedBullk: %s", err.Error())
		return
	}

	workflowid := uuid.NewString()
	req := domain.SubTeVerifiedBullk{
		SubTes: convertSubTeVerifiedToSubTeVerified(request.SubTes),
	}

	for i := range req.SubTes {
		req.SubTes[i].WorkflowId = workflowid
	}

	currentTime := time.Now()
	formattedTime := currentTime.Format("2006-01-02 15:04:05")

	var trans_id string
	var status string
	var approved_by int64
	var remarks string
	var ddocode string
	var officeid int64
	var budget_request []domain.BudgetRequest
	var budget_req domain.BudgetRequest
	var date time.Time //to be uncommented on 10042026
	number_budget_hoa := 0

	for _, t := range req.SubTes {
		trans_id = t.TransId
		status = t.Status
		approved_by = t.ApprovedBy
		remarks = t.ApproverRemarks
		ddocode = t.DdoCode
		date = t.CreatedDate //to be uncommented on 10042026
	}

	for _, y := range req.SubTes {

		if strings.HasPrefix(y.Hoa, "5201") ||
			strings.HasPrefix(y.Hoa, "3201") ||
			strings.HasPrefix(y.Hoa, "7610") ||
			strings.HasPrefix(y.Hoa, "801606101010500") ||
			strings.HasPrefix(y.Hoa, "801606101020500") ||
			strings.HasPrefix(y.Hoa, "2552") ||
			strings.HasPrefix(y.Hoa, "4552") {

			financialYear := getFinancialYear(date) //to be uncommented on 10042026
			// financialYear := "2025"

			u, q, err1 := uh.svc.GetOfficeIdRepo(ctx, ddocode)
			if err1 != nil {
				apierrors.HandleDBError(ctx, err1)
				log.Error(ctx, "GetOfficeIdRepo call failed: %s", err1.Error())
				return
			}
			if q {
				officeid = u.DdoOfficeId
			} else {
				err := errors.New("failed to get office_id")
				appError := apierrors.NewAppError(
					"failed to get office_id", // User-friendly error message
					"404",                     // Error code representing the error type
					err,                       // Original error for debugging purposes
				)
				apiErrorResponse := apierrors.NewAPIErrorResponse(
					http.StatusInternalServerError, // HTTP status code
					ErrInternalServerError,         // Message to return to the client
					appError,                       // Encapsulated application error
				)
				ctx.JSON(apiErrorResponse.StatusCode, apiErrorResponse)
				log.Error(ctx, "failed to get office_id: %s", err.Error())

				return
			}

			budget_req.FinancialYear = financialYear
			budget_req.OfficeId = officeid
			budget_req.Hoa = y.Hoa
			if y.TransferType == "C" {
				budget_req.ConsumedAmount = -y.TransferAmount
			} else {
				budget_req.ConsumedAmount = y.TransferAmount
			}
			budget_req.Remarks = y.TransId
			budget_req.UpdatedBy = y.CreatedBy
			budget_req.TransactionOffice = officeid

			budget_request = append(budget_request, budget_req)

			number_budget_hoa++
		}

	}

	workflowOptions := client.StartWorkflowOptions{
		// ID:        "Pao_Transfer_Entry_Workflow_" + time.Now().Format("20060102150405"),
		// workflowid = uuid.NewString()
		ID:        workflowid,
		TaskQueue: contracts.PAOTaskQueue, // Replace with your actual task queue.
	}
	Budgetinput, err3 := NewBudgetREquesttoBudgetTransferEntryVerificationInput(budget_request)
	if err3 != nil {
		apierrors.HandleDBError(ctx, err3)
		log.Error(ctx, "Budget Input Conversion Failed: %s", err3.Error())
		return
	}

	Subaccountinput := contracts.SubAccountTransferEntryVerificationInput{
		TransId: trans_id,
		// WorkflowId: WorkflowId,
		WorkflowId: workflowid,
		SubAccArrayInput: []contracts.SubAccountTransferEntryVerificationArrayInput{
			{
				Status:       status,
				ApprovedBy:   approved_by,
				ApprovedDate: formattedTime,
				Remarks:      remarks,
			},
		},
	}

	_, err := uh.client.ExecuteWorkflow(ctx, workflowOptions, uh.svc.TransferentryverificationWorkflow, Budgetinput, Subaccountinput, req)
	if err != nil {
		apierrors.HandleDBError(ctx, err)
		return
	}
	apiRsp := response.PaoSubTransferentryReportResponse{
		StatusCodeAndMessage: port.SubmissionSuccess,
	}
	handleCreateSuccess(ctx, apiRsp)

	//call budget api with input as budget_request and check result. If "success" is true, then add budget unique id to req and follow following operations.

	// server := uh.cfg.GetString("db.ApiUrl")
	// url := fmt.Sprintf("https://%s/besubaccounts/v1/transfer-entries/%s", server, trans_id)
	// method := "PUT"
	// header := map[string]string{
	// 	"Content-Type": "application/json",
	// }
	// params := map[string]interface{}{
	// 	"status":        status,
	// 	"approved_by":   approved_by,
	// 	"approved_date": formattedTime,
	// 	"remarks":       remarks,
	// }
	// apiRsp := response.PaoSubTransferentryReportResponse{
	// 	StatusCodeAndMessage: port.CreateSuccess,
	// }

	// if number_budget_hoa > 0 {

	// 	//making ready the budget api call
	// 	url_budget := fmt.Sprintf("https://%s/bebudget/budget/v1/consumption/addconsumptionte", server)
	// 	method_budget := "POST"
	// 	// Wrap the budget_request in a map
	// 	params_budget := map[string]interface{}{
	// 		"sub_tes": budget_request,
	// 	}

	// 	//calls subaccounts api and make sure it is updated.
	// 	response, err := uh.CallAPI(url, method, header, params)
	// 	if err != nil {
	// 		ctx.JSON(http.StatusInternalServerError, gin.H{"error": err.Error()})
	// 		return
	// 	}

	// 	if !response["success"].(bool) {
	// 		ctx.JSON(http.StatusBadRequest, gin.H{"error": response["message"]})
	// 		return
	// 	} else {

	// 		response, err := uh.CallAPI(url_budget, method_budget, header, params_budget)
	// 		if err != nil {
	// 			ctx.JSON(http.StatusInternalServerError, gin.H{"error": err.Error()})
	// 			return
	// 		}

	// 		if !response["success"].(bool) {
	// 			ctx.JSON(http.StatusBadRequest, gin.H{"error": response["message"]})
	// 			return
	// 		} else {
	// 			error := uh.svc.SubVerifiedTePostingRepo(ctx, req)
	// 			if error != nil {
	// 				log.Error(ctx, "SubVerifiedTePosting Repo call failed: %s", error.Error())
	// 				if error.(*pgconn.PgError).Code == "23505" {
	// 					err1 := errors.New("transfer entry verification failed")
	// 					// Create an AppError with a user-friendly message and code.
	// 					appError := apierrors.NewAppError(
	// 						"transferentry already verified", // User-friendly error message
	// 						"409",                            // Error code representing the error type
	// 						err1,                             // Original error for debugging purposes
	// 					)
	// 					apiErrorResponse := apierrors.NewAPIErrorResponse(
	// 						http.StatusConflict, // HTTP status code
	// 						"Conflict",          // Message to return to the client
	// 						appError,            // Encapsulated application error
	// 					)
	// 					ctx.JSON(apiErrorResponse.StatusCode, apiErrorResponse)
	// 					return
	// 				} else {
	// 					apierrors.HandleDBError(ctx, err)
	// 					return
	// 				}
	// 			}
	// 			log.Debug(ctx, "CreateSubaccountsTeVerifiedHandler response", apiRsp)
	// 			handleCreateSuccess(ctx, apiRsp)
	// 			return
	// 		}
	// 	}
	// } else {

	// 	//calls subaccounts api
	// 	response, err := uh.CallAPI(url, method, header, params)
	// 	if err != nil {
	// 		ctx.JSON(http.StatusInternalServerError, gin.H{"error": err.Error()})
	// 		return
	// 	}

	// 	if !response["success"].(bool) {
	// 		ctx.JSON(http.StatusBadRequest, gin.H{"error": response["message"]})
	// 		return
	// 	} else {

	// 		err1 := uh.svc.SubVerifiedTePostingRepo(ctx, req)
	// 		if err1 != nil {
	// 			log.Error(ctx, "SubVerifiedTePosting Repo call failed: %s", err1.Error())
	// 			if err1.(*pgconn.PgError).Code == "23505" {
	// 				err1 := errors.New("transfer entry verification failed")
	// 				// Create an AppError with a user-friendly message and code.
	// 				appError := apierrors.NewAppError(
	// 					"transferentry already verified", // User-friendly error message
	// 					"409",                            // Error code representing the error type
	// 					err1,                             // Original error for debugging purposes
	// 				)
	// 				apiErrorResponse := apierrors.NewAPIErrorResponse(
	// 					http.StatusConflict, // HTTP status code
	// 					"Conflict",          // Message to return to the client
	// 					appError,            // Encapsulated application error
	// 				)
	// 				ctx.JSON(apiErrorResponse.StatusCode, apiErrorResponse)
	// 				return
	// 			} else {
	// 				apierrors.HandleDBError(ctx, err1)
	// 				return
	// 			}
	// 		}
	// 		log.Debug(ctx, "CreateSubaccountsTeVerifiedHandler response", apiRsp)
	// 		handleCreateSuccess(ctx, apiRsp)
	// 		return
	// 	}

	// }

}

type TransferEntryInterPaoRequest struct {
	MasterPaoCode      string  `json:"master_pao_code" validate:"required,validatePaocode"`
	PaoCode            string  `json:"pao_code" validate:"required,validatePaocode"`
	DdoCode            string  `json:"ddo_code" select:"ddo_code" validate:"required,validateDdocode"`
	Hoa                string  `json:"hoa" validate:"required,head_of_account"`
	TransferAmount     float64 `json:"transfer_amount" validate:"required"`
	TransferType       string  `json:"transfer_type" validate:"required,max=20"`
	CreatedBy          uint64  `json:"created_by" validate:"required,employee_id"`
	CreatedDate        string  `json:"created_date" validate:"required,date_yyyy_mm_dd"`
	TeSourceOfficeType string  `json:"te_source_office_type" select:"te_source_office_type" validate:"required,max=50"`
	Remarks            string  `json:"remarks" validate:"required,max=255"`
}

type TransferEntryInterPaoRequests struct {
	TransferEntries []TransferEntryInterPaoRequest `json:"transfer_entries" validate:"dive"`
}

// CreateTransferEntryHandler godoc
//
//	@Summary		Create Transfer Entry Inter PAO
//	@Description	Create Transfer Entry Inter PAO
//	@Tags			Transfer Entry
//	@Accept			json
//	@Produce		json
//	@Param			body	body		[]domain.TransferEntryInterPaoRequest	true	"Transfer Entry creation request"
//	@Success		201		{object}	response.GetTransferentryCreationResponse			"resource created successfully"
//	@Failure		400					{object}	apierrors.APIErrorResponse						"Validation error"
//	@Failure		401					{object}	apierrors.APIErrorResponse						"Unauthorized error"
//	@Failure		403					{object}	apierrors.APIErrorResponse						"Forbidden error"
//	@Failure		404					{object}	apierrors.APIErrorResponse						"Data not found error"
//	@Failure		409					{object}	apierrors.APIErrorResponse						"Data conflict error"
//	@Failure		500					{object}	apierrors.APIErrorResponse						"Internal server error"
//	@Router			/v1/transfer-entry/inter-pao [post]
func (uh *TransferEntryHandler) CreateTransferEntryInterPaoHandler(ctx *gin.Context) {

	var req []TransferEntryInterPaoRequest
	if err := ctx.ShouldBindJSON(&req); err != nil {
		apierrors.HandleBindingError(ctx, err)
		log.Error(ctx, "Binding failed for TransferEntryRequests: %s", err.Error())
		return
	}
	for _, r := range req {
		if err := validation.ValidateStruct(r); err != nil {
			apierrors.HandleValidationError(ctx, err)
			log.Error(ctx, "Validation failed for TransferEntryRequests: %s", err.Error())
			return
		}
	}

	var request []domain.TransferEntryInterPaoRequest

	for _, requ := range req {

		request = append(request, domain.TransferEntryInterPaoRequest{

			MasterPaoCode:      null.StringFrom(requ.MasterPaoCode),
			PaoCode:            null.StringFrom(requ.PaoCode),
			DdoCode:            requ.DdoCode,
			Hoa:                requ.Hoa,
			TransferAmount:     requ.TransferAmount,
			TransferType:       requ.TransferType,
			CreatedBy:          requ.CreatedBy,
			CreatedDate:        requ.CreatedDate,
			TeSourceOfficeType: requ.TeSourceOfficeType,
			Remarks:            requ.Remarks,
		})
	}

	var Total_debit float64 = 0
	var Total_credit float64 = 0
	var Inserted_Ids []domain.InsertedIds
	var err error
	currentTime := time.Now()
	for _, r := range request {
		if r.TransferType == "D" {
			Total_debit = Total_debit + r.TransferAmount
		}
		if r.TransferType == "C" {
			Total_credit = Total_credit + r.TransferAmount
		}
		r.CreatedDate = currentTime.Format("2006-01-02 15:04:05")
	}
	if Total_credit == Total_debit {
		Inserted_Ids, err = uh.svc.TransferentryInterPaoCreationRepo(ctx, request)
		if err != nil {
			apierrors.HandleDBError(ctx, err)
			log.Error(ctx, "Transfer Entry Creation Repo call failed: %s", err.Error())
			return
		}
	} else {
		err := errors.New("total debit not equal to total credit")
		appError := apierrors.NewAppError(
			"Debit Credit error", // User-friendly error message
			"500",                // Error code representing the error type
			err,                  // Original error for debugging purposes
		)
		apiErrorResponse := apierrors.NewAPIErrorResponse(
			http.StatusInternalServerError, // HTTP status code
			ErrInternalServerError,         // Message to return to the client
			appError,                       // Encapsulated application error
		)
		ctx.JSON(apiErrorResponse.StatusCode, apiErrorResponse)
		log.Error(ctx, "total debit not equal to total credit: %s", err.Error())
		return
	}
	rsp := response.NewGTransferentryCreationResponse(Inserted_Ids)

	metadata := port.NewMetaDataResponse(0, 0, len(rsp))

	apiRsp := response.GetTransferentryCreationResponse{
		StatusCodeAndMessage: port.CreateSuccess,
		MetaDataResponse:     metadata,
		Data:                 rsp,
	}
	log.Debug(ctx, "CreateTransferEntryHandler response", apiRsp)
	handleCreateSuccess(ctx, apiRsp)

}

type transferEntryInterPaoMasterRequest struct {
	PaoCode            string `form:"pao-code" binding:"required" validate:"required,validatePaocode"`
	FromDate           string `form:"from-date" validate:"required,date_yyyy_mm_dd"`
	ToDate             string `form:"to-date" validate:"required,date_yyyy_mm_dd"`
	VerificationStatus string `form:"verification-status" validate:"required,oneof=created verified deleted"`
	port.MetaDataRequest
}

// ListTransferEntryInterPaoMasterHandler godoc
//
//	@Summary		Get report of Inter Pao TE created in that PAO
//	@Description	Get report of Inter Pao TE created in that PAO
//	@Tags			Transfer Entry
//	@Accept			json
//	@Produce		json
//	@Param			pao-code	query		string			true	"Pao_code"
//	@Param			from-date	query		string			true	"From_date"
//	@Param			to-date	query		string			true	"To_date"
//	@Param			verification-status	query		string			true	"Verification_status"
//
// @Param       skip    query       int     			false   		"Number of records to skip for pagination"
// @Param       limit   query       int     			false   		"Number of records to limit for pagination"
//
//	@Success		200		{object}	response.TransferentryInterPaoReportResponse			"list retrieved successfully"
//	@Failure		400					{object}	apierrors.APIErrorResponse						"Validation error"
//	@Failure		401					{object}	apierrors.APIErrorResponse						"Unauthorized error"
//	@Failure		403					{object}	apierrors.APIErrorResponse						"Forbidden error"
//	@Failure		404					{object}	apierrors.APIErrorResponse						"Data not found error"
//	@Failure		409					{object}	apierrors.APIErrorResponse						"Data conflict error"
//	@Failure		500					{object}	apierrors.APIErrorResponse						"Internal server error"
//	@Router			/v1/pao-gen/transfer-entry/inter-pao/master [get]
func (uh *TransferEntryHandler) ListTransferEntryInterPaoMasterHandler(ctx *gin.Context) {

	var req transferEntryInterPaoMasterRequest
	if err1 := ctx.ShouldBindQuery(&req); err1 != nil {
		apierrors.HandleBindingError(ctx, err1)
		log.Error(ctx, "Binding failed for transferEntryReportRequest: %s", err1.Error())
		return
	}
	if err := validation.ValidateStruct(req); err != nil {
		apierrors.HandleValidationError(ctx, err)
		log.Error(ctx, "Validation failed for transferEntryReportRequest: %s", err.Error())
		return
	}
	if req.Limit == 0 {
		req.Limit = math.MaxInt32
	}
	var request domain.TransferEntryInterPaoMasterRequest
	request.PaoCode = req.PaoCode
	request.FromDate = req.FromDate
	request.ToDate = req.ToDate
	request.VerificationStatus = req.VerificationStatus
	res, err := uh.svc.ListTransferEntryInterPaoMasterRepo(ctx, request, req.MetaDataRequest)
	if err != nil {
		apierrors.HandleDBError(ctx, err)
		log.Error(ctx, "Transfer ENtry Report Repo call failed: %s", err.Error())
		return
	}

	rsp := response.NewTransferentryInterPaoReportResponse(res)

	metadata := port.NewMetaDataResponse(req.Skip, req.Limit, len(rsp))

	apiRsp := response.TransferentryInterPaoReportResponse{
		StatusCodeAndMessage: port.ListSuccess,
		MetaDataResponse:     metadata,
		Data:                 rsp,
	}
	log.Debug(ctx, "ListTransferEntryReportHandler response", apiRsp)
	handleSuccess(ctx, apiRsp)

}

// ListTransferEntryInterPaoHandler godoc
//
//	@Summary		Get report of Inter Pao TE created related to  that PAO
//	@Description	Get report of Inter Pao TE created related to  that PAO
//	@Tags			Transfer Entry
//	@Accept			json
//	@Produce		json
//	@Param			pao-code	query		string			true	"Pao_code"
//	@Param			from-date	query		string			true	"From_date"
//	@Param			to-date	query		string			true	"To_date"
//	@Param			verification-status	query		string			true	"Verification_status"
//
// @Param       skip    query       int     			false   		"Number of records to skip for pagination"
// @Param       limit   query       int     			false   		"Number of records to limit for pagination"
//
//	@Success		200		{object}	response.TransferentryInterPaoReportResponse			"list retrieved successfully"
//	@Failure		400					{object}	apierrors.APIErrorResponse						"Validation error"
//	@Failure		401					{object}	apierrors.APIErrorResponse						"Unauthorized error"
//	@Failure		403					{object}	apierrors.APIErrorResponse						"Forbidden error"
//	@Failure		404					{object}	apierrors.APIErrorResponse						"Data not found error"
//	@Failure		409					{object}	apierrors.APIErrorResponse						"Data conflict error"
//	@Failure		500					{object}	apierrors.APIErrorResponse						"Internal server error"
//	@Router			/v1/pao-gen/transfer-entry/inter-pao [get]
func (uh *TransferEntryHandler) ListTransferEntryInterPaoHandler(ctx *gin.Context) {

	var req transferEntryInterPaoMasterRequest
	if err1 := ctx.ShouldBindQuery(&req); err1 != nil {
		apierrors.HandleBindingError(ctx, err1)
		log.Error(ctx, "Binding failed for transferEntryReportRequest: %s", err1.Error())
		return
	}
	if err := validation.ValidateStruct(req); err != nil {
		apierrors.HandleValidationError(ctx, err)
		log.Error(ctx, "Validation failed for transferEntryReportRequest: %s", err.Error())
		return
	}
	if req.Limit == 0 {
		req.Limit = math.MaxInt32
	}
	var request domain.TransferEntryInterPaoMasterRequest
	request.PaoCode = req.PaoCode
	request.FromDate = req.FromDate
	request.ToDate = req.ToDate
	request.VerificationStatus = req.VerificationStatus
	res, err := uh.svc.ListTransferEntryInterPaoRepo(ctx, request, req.MetaDataRequest)
	if err != nil {
		apierrors.HandleDBError(ctx, err)
		log.Error(ctx, "Transfer ENtry Report Repo call failed: %s", err.Error())
		return
	}

	rsp := response.NewTransferentryInterPaoReportResponse(res)

	metadata := port.NewMetaDataResponse(req.Skip, req.Limit, len(rsp))

	apiRsp := response.TransferentryInterPaoReportResponse{
		StatusCodeAndMessage: port.ListSuccess,
		MetaDataResponse:     metadata,
		Data:                 rsp,
	}
	log.Debug(ctx, "ListTransferEntryReportHandler response", apiRsp)
	handleSuccess(ctx, apiRsp)

}

// FetchInterPaoTransferentryDetailHandler godoc
//
//	@Summary		Get InterPao Transfer entry details
//	@Description	Get InterPao Transfer entry details
//	@Tags			Transfer Entry
//	@Accept			json
//	@Produce		json
//	@Param			trans-id	path		string			true	"Trans_id"
//
// @Param       skip    query       int     			false   		"Number of records to skip for pagination"
// @Param       limit   query       int     			false   		"Number of records to limit for pagination"
//
//	@Success		200		{object}	response.InterPaoTransferentryDetailResponse			"data retrieved successfully"
//	@Failure		400					{object}	apierrors.APIErrorResponse						"Validation error"
//	@Failure		401					{object}	apierrors.APIErrorResponse						"Unauthorized error"
//	@Failure		403					{object}	apierrors.APIErrorResponse						"Forbidden error"
//	@Failure		404					{object}	apierrors.APIErrorResponse						"Data not found error"
//	@Failure		409					{object}	apierrors.APIErrorResponse						"Data conflict error"
//	@Failure		500					{object}	apierrors.APIErrorResponse						"Internal server error"
//	@Router			/v1/transfer-entry/inter-pao/details/{trans-id} [get]
func (uh *TransferEntryHandler) FetchInterPaoTransferentryDetailHandler(ctx *gin.Context) {

	var req PaoSubTeDetailRequest
	if err := ctx.ShouldBindUri(&req); err != nil {
		apierrors.HandleBindingError(ctx, err)
		log.Error(ctx, "Binding failed for PaoSubTeDetailRequest: %s", err.Error())
		return
	}
	if err := validation.ValidateStruct(req); err != nil {
		apierrors.HandleValidationError(ctx, err)
		log.Error(ctx, "Validation failed for PaoSubTeDetailRequest: %s", err.Error())
		return
	}
	var request domain.PaoSubTeDetailRequest
	request.TransId = req.TransId

	res, err := uh.svc.InterPaoTransferentryDetailRepo(ctx, request)
	if err != nil {
		apierrors.HandleDBError(ctx, err)
		log.Error(ctx, "PaoSubTransferentryDetail Repo call failed: %s", err.Error())
		return
	}

	rsp := response.NewInterPaoTransferentryDetailResponse(res)

	metadata := port.NewMetaDataResponse(0, 0, len(rsp))

	apiRsp := response.InterPaoTransferentryDetailResponse{
		StatusCodeAndMessage: port.FetchSucess,
		MetaDataResponse:     metadata,
		Data:                 rsp,
	}
	log.Debug(ctx, "FetchPaoSubTransferentryDetailHandler response", apiRsp)
	handleSuccess(ctx, apiRsp)

}

type TransferEntryInterPaoMasterUpdateRequest struct {
	TransferEntryId    string `json:"transfer_entry_id" select:"transfer_entry_id" validate:"required,max=30"`
	VerificationStatus string `json:"verification_status" select:"verification_status" validate:"required,oneof=verified deleted"`
	VerifiedBy         int64  `json:"verified_by" select:"verified_by" validate:"required,employee_id"`
	ApproverRemarks    string `json:"approver_remarks" select:"approver_remarks" validate:"required,max=255"`
}

// UpdateInterPaoTransferEntryVerifyMasterHandler godoc
//
//	@Summary		Update Transfer entry as Verified by Master PAO
//	@Description	Update Transfer entry as Verified by Master PAO
//	@Tags			Transfer Entry
//	@Accept			json
//	@Produce		json
//	@Param			body	body		TransferEntryInterPaoMasterUpdateRequest true	"Verify Transfer Entry request"
//	@Success		200		{object}	response.TransferentryReportResponse			"resource updated successfully"
//	@Failure		400					{object}	apierrors.APIErrorResponse						"Validation error"
//	@Failure		401					{object}	apierrors.APIErrorResponse						"Unauthorized error"
//	@Failure		403					{object}	apierrors.APIErrorResponse						"Forbidden error"
//	@Failure		404					{object}	apierrors.APIErrorResponse						"Data not found error"
//	@Failure		409					{object}	apierrors.APIErrorResponse						"Data conflict error"
//	@Failure		500					{object}	apierrors.APIErrorResponse						"Internal server error"
//	@Router			/v1/transfer-entry/inter-pao/master [put]
func (uh *TransferEntryHandler) UpdateInterPaoTransferEntryVerifyMasterHandler(ctx *gin.Context) {

	var req TransferEntryInterPaoMasterUpdateRequest
	if err := ctx.ShouldBindJSON(&req); err != nil {
		apierrors.HandleBindingError(ctx, err)
		log.Error(ctx, "Binding failed for TransferEntryVerifyRequests: %s", err.Error())
		return
	}

	if err := validation.ValidateStruct(req); err != nil {
		apierrors.HandleValidationError(ctx, err)
		log.Error(ctx, "Validation failed for TransferEntryVerifyRequests: %s", err.Error())
		return
	}

	request := domain.TransferEntryInterPao{

		TransferEntryId:    null.StringFrom(req.TransferEntryId),
		VerificationStatus: null.StringFrom(req.VerificationStatus),
		VerifiedBy:         null.Int64From(req.VerifiedBy),
		ApproverRemarks:    null.StringFrom(req.ApproverRemarks),
	}

	var err error

	if req.VerificationStatus == "verified" {
		err = uh.svc.TransferentryInterPaoMasterVerifyRepo(ctx, request)
	} else if req.VerificationStatus == "deleted" {
		err = uh.svc.TransferentryInterPaoRejectRepo(ctx, request)
	}

	if err != nil {
		apierrors.HandleDBError(ctx, err)
		log.Error(ctx, "Transfer Entry updation failed: %s", err.Error())
		return
	}

	apiRsp := response.TransferentryReportResponse{
		StatusCodeAndMessage: port.UpdateSuccess,
	}
	log.Debug(ctx, "UpdateInterPaoTransferEntryUpdateHandler response", apiRsp)
	handleSuccess(ctx, apiRsp)
}

type TransferEntryInterPaoUpdateRequest struct {
	TransferEntryId    string `json:"transfer_entry_id" select:"transfer_entry_id" validate:"required,max=30"`
	PaoCode            string `json:"pao_code" select:"pao_code" validate:"required,validatePaocode"`
	VerificationStatus string `json:"verification_status" select:"verification_status" validate:"required,oneof=verified deleted"`
	VerifiedBy         int64  `json:"verified_by" select:"verified_by" validate:"required,employee_id"`
	ApproverRemarks    string `json:"approver_remarks" select:"approver_remarks" validate:"required,max=255"`
}

// UpdateInterPaoTransferEntryVerifyHandler godoc
//
//	@Summary		Update Transfer entry as Verified by Master PAO
//	@Description	Update Transfer entry as Verified by Master PAO
//	@Tags			Transfer Entry
//	@Accept			json
//	@Produce		json
//	@Param			body	body		TransferEntryInterPaoUpdateRequest true	"Verify Transfer Entry request"
//	@Success		200		{object}	response.TransferentryReportResponse			"resource updated successfully"
//	@Failure		400					{object}	apierrors.APIErrorResponse						"Validation error"
//	@Failure		401					{object}	apierrors.APIErrorResponse						"Unauthorized error"
//	@Failure		403					{object}	apierrors.APIErrorResponse						"Forbidden error"
//	@Failure		404					{object}	apierrors.APIErrorResponse						"Data not found error"
//	@Failure		409					{object}	apierrors.APIErrorResponse						"Data conflict error"
//	@Failure		500					{object}	apierrors.APIErrorResponse						"Internal server error"
//	@Router			/v1/transfer-entry/inter-pao [put]
func (uh *TransferEntryHandler) UpdateInterPaoTransferEntryVerifyHandler(ctx *gin.Context) {

	var req TransferEntryInterPaoUpdateRequest
	if err := ctx.ShouldBindJSON(&req); err != nil {
		apierrors.HandleBindingError(ctx, err)
		log.Error(ctx, "Binding failed for TransferEntryVerifyRequests: %s", err.Error())
		return
	}

	if err := validation.ValidateStruct(req); err != nil {
		apierrors.HandleValidationError(ctx, err)
		log.Error(ctx, "Validation failed for TransferEntryVerifyRequests: %s", err.Error())
		return
	}

	request := domain.TransferEntryInterPao{

		TransferEntryId:    null.StringFrom(req.TransferEntryId),
		PaoCode:            null.StringFrom(req.PaoCode),
		VerificationStatus: null.StringFrom(req.VerificationStatus),
		VerifiedBy:         null.Int64From(req.VerifiedBy),
		ApproverRemarks:    null.StringFrom(req.ApproverRemarks),
	}

	var err error

	if req.VerificationStatus == "verified" {
		err = uh.svc.TransferentryInterPaoVerifyRepo(ctx, request)
	} else if req.VerificationStatus == "deleted" {
		err = uh.svc.TransferentryInterPaoRejectRepo(ctx, request)
	}

	if err != nil {
		apierrors.HandleDBError(ctx, err)
		log.Error(ctx, "Transfer Entry updation failed: %s", err.Error())
		return
	}

	apiRsp := response.TransferentryReportResponse{
		StatusCodeAndMessage: port.UpdateSuccess,
	}
	log.Debug(ctx, "UpdateInterPaoTransferEntryUpdateHandler response", apiRsp)
	handleSuccess(ctx, apiRsp)
}

type TransferEntryDirectBRSRequest struct {
	// PaoCode            string  `json:"pao_code" validate:"required,validatePaocode"`
	// DdoCode            string  `json:"ddo_code" select:"ddo_code" validate:"required,validateDdocode"`
	OfficeId           uint64  `json:"office_id" validate:"required"`
	Hoa                string  `json:"hoa" validate:"required,head_of_account"`
	TransferAmount     float64 `json:"transfer_amount" validate:"required"`
	TransferType       string  `json:"transfer_type" validate:"required,max=20"`
	CreatedBy          uint64  `json:"created_by" validate:"required,employee_id"`
	VerifiedBy         uint64  `json:"verified_by" select:"verified_by" validate:"employee_id"`
	TeSourceOfficeType string  `json:"te_source_office_type" select:"te_source_office_type" validate:"required,max=50"`
	Remarks            string  `json:"remarks" validate:"required,max=255"`
}

// CreateTransferEntryDirectBRSHandler godoc
//
//	@Summary		Create Transfer Entry Direct for BRS
//	@Description	Create Transfer Entry Direct for BRS
//	@Tags			Transfer Entry
//	@Accept			json
//	@Produce		json
//	@Param			body	body		[]TransferEntryDirectBRSRequest	true	"Transfer Entry Direct creation request"
//	@Success		201		{object}	response.GetTransferentryCreationResponse			"resource created successfully"
//	@Failure		400					{object}	apierrors.APIErrorResponse						"Validation error"
//	@Failure		401					{object}	apierrors.APIErrorResponse						"Unauthorized error"
//	@Failure		403					{object}	apierrors.APIErrorResponse						"Forbidden error"
//	@Failure		404					{object}	apierrors.APIErrorResponse						"Data not found error"
//	@Failure		409					{object}	apierrors.APIErrorResponse						"Data conflict error"
//	@Failure		500					{object}	apierrors.APIErrorResponse						"Internal server error"
//	@Router			/v1/transfer-entry/direct-brs [post]
func (uh *TransferEntryHandler) CreateTransferEntryDirectBRSHandler(ctx *gin.Context) {

	var req []TransferEntryDirectBRSRequest
	if err := ctx.ShouldBindJSON(&req); err != nil {
		apierrors.HandleBindingError(ctx, err)
		log.Error(ctx, "Binding failed for TransferEntryRequests: %s", err.Error())
		return
	}
	for _, r := range req {
		if err := validation.ValidateStruct(r); err != nil {
			apierrors.HandleValidationError(ctx, err)
			log.Error(ctx, "Validation failed for TransferEntryRequests: %s", err.Error())
			return
		}
	}
	currentTime := time.Now()
	createdTimeString := currentTime.Format("20060102150405")

	// Derive trans_date from currentTime — date only, time zeroed out
	transDate := time.Date(currentTime.Year(), currentTime.Month(), currentTime.Day(), 0, 0, 0, 0, currentTime.Location())

	var request []domain.TransferEntryDirectRequest

	for _, requ := range req {

		u, v, err := uh.svc.GetPaoCodenDdoCodeByOfficeIDRepo(ctx, requ.OfficeId)

		if err != nil {
			apierrors.HandleDBError(ctx, err)
			log.Error(ctx, "GetOfficeIdRepo call failed: %s", err.Error())
			return
		}
		if v {
			request = append(request, domain.TransferEntryDirectRequest{

				PaoCode:             u.PaoCode,
				DdoCode:             u.DdoCode,
				Hoa:                 requ.Hoa,
				TransferAmount:      requ.TransferAmount,
				TransferType:        requ.TransferType,
				CreatedBy:           requ.CreatedBy,
				CreatedDate:         currentTime,
				TransDate:           transDate, // ← added
				TransferEntryId:     u.PaoCode + createdTimeString,
				HPfmsGenerationFlag: false,
				TeSourceOfficeType:  "PAO",
				Remarks:             requ.Remarks,
				VerificationStatus:  "created",
			})
		} else {
			err := errors.New("invalid or missing DDO/PAO code for provided Office ID")
			appError := apierrors.NewAppError(
				"Invalid Office ID — no DDO/PAO code found",
				"404",
				err,
			)
			apiErrorResponse := apierrors.NewAPIErrorResponse(
				http.StatusBadRequest, // or http.StatusNotFound
				"Invalid Office ID — please verify and try again",
				appError,
			)
			ctx.JSON(apiErrorResponse.StatusCode, apiErrorResponse)
			log.Error(ctx, "No DDO/PAO code found for Office ID %d: %s", requ.OfficeId, err.Error())
			return
		}

	}

	var Total_debit float64 = 0
	var Total_credit float64 = 0
	var Inserted_Ids []domain.InsertedIds
	var err error
	for _, r := range request {
		if r.TransferType == "D" {
			Total_debit = Total_debit + r.TransferAmount
		}
		if r.TransferType == "C" {
			Total_credit = Total_credit + r.TransferAmount
		}
	}
	if Total_credit == Total_debit {
		Inserted_Ids, err = uh.svc.TransferentryDirectCreationRepo(ctx, request)
		if err != nil {
			apierrors.HandleDBError(ctx, err)
			log.Error(ctx, "Transfer Entry Creation Repo call failed: %s", err.Error())
			return
		}
	} else {
		err := errors.New("total debit not equal to total credit")
		appError := apierrors.NewAppError(
			"Debit Credit error", // User-friendly error message
			"422",                // Error code representing the error type
			err,                  // Original error for debugging purposes
		)
		apiErrorResponse := apierrors.NewAPIErrorResponse(
			http.StatusInternalServerError, // HTTP status code
			ErrInternalServerError,         // Message to return to the client
			appError,                       // Encapsulated application error
		)
		ctx.JSON(apiErrorResponse.StatusCode, apiErrorResponse)
		log.Error(ctx, "total debit not equal to total credit: %s", err.Error())
		return
	}
	rsp := response.NewGTransferentryCreationResponse(Inserted_Ids)

	metadata := port.NewMetaDataResponse(0, 0, len(rsp))

	apiRsp := response.GetTransferentryCreationResponse{
		StatusCodeAndMessage: port.CreateSuccess,
		MetaDataResponse:     metadata,
		Data:                 rsp,
	}
	log.Debug(ctx, "CreateTransferEntryHandler response", apiRsp)
	handleCreateSuccess(ctx, apiRsp)

}

type TransferEntryPFMSResetRequest struct {
	PfmsUniqueId string `json:"pfms_unique_id" uri:"pfms-unique-id" validate:"required"`
}

// ResetPFMSFlagHandler godoc
//
//	@Summary		Reset PFMS flags for a Transfer Entry
//	@Description	Resets pfms_submission_flag to false, clears pfms_error_description, h_pfms_generation_flag to false, and clears pfms_unique_id for the given PFMS Unique ID
//	@Tags			Transfer Entry
//	@Accept			json
//	@Produce		json
//	@Param			pfms-unique-id	path		string								true	"PFMS Unique ID (e.g. TE-07810220267630628721)"
//	@Success		200					{object}	response.PFMSResetResponse			"PFMS flags reset successfully"
//	@Failure		400					{object}	apierrors.APIErrorResponse			"Validation error"
//	@Failure		401					{object}	apierrors.APIErrorResponse			"Unauthorized error"
//	@Failure		403					{object}	apierrors.APIErrorResponse			"Forbidden error"
//	@Failure		404					{object}	apierrors.APIErrorResponse			"Transfer Entry not found"
//	@Failure		409					{object}	apierrors.APIErrorResponse			"Data conflict error"
//	@Failure		500					{object}	apierrors.APIErrorResponse			"Internal server error"
//	@Router			/v1/transfer-entry/pfms-reset/{pfms-unique-id} [put]
func (uh *TransferEntryHandler) ResetPFMSFlagHandler(ctx *gin.Context) {

	var req TransferEntryPFMSResetRequest

	if err := ctx.ShouldBindUri(&req); err != nil {
		apierrors.HandleBindingError(ctx, err)
		log.Error(ctx, "Binding failed for TransferEntryPFMSResetRequest: %s", err.Error())
		return
	}

	if err := validation.ValidateStruct(req); err != nil {
		apierrors.HandleValidationError(ctx, err)
		log.Error(ctx, "Validation failed for TransferEntryPFMSResetRequest: %s", err.Error())
		return
	}

	request := domain.TransferEntryPFMSResetRequest{
		PfmsUniqueId: req.PfmsUniqueId,
	}

	rowsAffected, err := uh.svc.ResetPFMSFlagByPfmsUniqueIdRepo(ctx, &request)
	if err != nil {
		apierrors.HandleDBError(ctx, err)
		log.Error(ctx, "ResetPFMSFlagByPfmsUniqueId service call failed: %s", err.Error())
		return
	}

	if rowsAffected == 0 {
		err := fmt.Errorf("no transfer entry found with pfms_unique_id: %s", req.PfmsUniqueId)
		appError := apierrors.NewAppError(
			"Transfer entry not found for the provided PFMS Unique ID", // User-friendly error message
			"404",
			err,
		)
		apiErrorResponse := apierrors.NewAPIErrorResponse(
			http.StatusNotFound,
			"Transfer entry not found for the provided PFMS Unique ID",
			appError,
		)
		ctx.JSON(apiErrorResponse.StatusCode, apiErrorResponse)
		log.Warn(ctx, "ResetPFMSFlagByPfmsUniqueId: no rows affected for pfms_unique_id: %s", req.PfmsUniqueId)
		return
	}

	apiRsp := response.PFMSResetResponse{
		StatusCodeAndMessage: port.UpdateSuccess,
		Message:              fmt.Sprintf("PFMS flags reset successfully for transfer entry: %s", req.PfmsUniqueId),
	}
	log.Debug(ctx, "ResetPFMSFlagHandler response", apiRsp)
	handleSuccess(ctx, apiRsp)
}

// GetReversiblePfmsTeHandler godoc
//
// @Summary      Get reversible TE PFMS submissions
// @Description  Get all successful TE submissions eligible for reversal
// @Tags         Transfer Entry
// @Produce      json
// @Success      200  {object}  []domain.PfmsTeReversible
// @Router       /v1/transfer-entry/pfms-te-reversible [get]
func (uh *TransferEntryHandler) GetReversiblePfmsTeHandler(ctx *gin.Context) {

	records, err := uh.svc.GetReversiblePfmsTe(ctx)
	if err != nil {
		apierrors.HandleDBError(ctx, err)
		log.Error(ctx, "GetReversiblePfmsTe failed: %s", err.Error())
		return
	}

	if len(records) == 0 {
		ctx.JSON(http.StatusNotFound, gin.H{
			"error": "No reversible TE submissions found",
		})
		return
	}

	ctx.JSON(http.StatusOK, gin.H{
		"data": records,
	})
}

// CreateNegativePfmsTeHandler godoc
//
// @Summary      Post negative TE to PFMS
// @Description  Reverses a previously submitted TE by sending negative amounts to PFMS
// @Tags         Transfer Entry
// @Accept       json
// @Produce      json
// @Param        body  body  domain.TeReversalRequest  true  "Reversal request"
// @Router       /v1/transfer-entry/pfms-negative-submission [post]
func (uh *TransferEntryHandler) CreateNegativePfmsTeHandler(ctx *gin.Context) {

	var req domain.TeReversalRequest
	if err := ctx.ShouldBindJSON(&req); err != nil {
		apierrors.HandleBindingError(ctx, err)
		return
	}

	// ---------------- FETCH ORIGINAL SUBMISSION ----------------
	original, err := uh.svc.GetPfmsSubmissionByUniqueID(ctx, req.PfmsUniqueId)
	if err != nil {
		apierrors.HandleDBError(ctx, err)
		return
	}
	if original == nil {
		ctx.JSON(http.StatusNotFound, gin.H{
			"error": "No submission found for given pfms_unique_id",
		})
		return
	}

	// Guard — only reverse Success + te type
	if original.SubmissionStatus != "Success" {
		ctx.JSON(http.StatusBadRequest, gin.H{
			"error": "Only successful submissions can be reversed",
		})
		return
	}
	if original.PfmsSubmissionType != "te" {
		ctx.JSON(http.StatusBadRequest, gin.H{
			"error": "Only TE type submissions can be reversed",
		})
		return
	}

	// Guard — already reversed
	alreadyReversed, err := uh.svc.CheckIfAlreadyReversed(ctx, req.PfmsUniqueId)
	if err != nil {
		apierrors.HandleDBError(ctx, err)
		return
	}
	if alreadyReversed {
		ctx.JSON(http.StatusBadRequest, gin.H{
			"error": "This submission has already been reversed",
		})
		return
	}

	// ---------------- FLIP PAYLOAD ----------------
	negativePayload := FlipPayloadAmounts(original.SubmissionData)
	newUniqueID := GenerateRandomNumber(
		original.SubmissionData.RequestPayload.TransferEntryDetails[0].PaoCode,
		fmt.Sprintf("%d", original.SubmissionData.RequestPayload.TransferEntryDetails[0].FinancialYear),
	)
	negativePayload.RequestPayload.TransferEntryDetails[0].UniqueIdentifier = newUniqueID

	// ---------------- PFMS AUTH ----------------
	username := uh.cfg.GetString("pfms.username")
	requestsource := uh.cfg.GetString("pfms.requestsource")
	password := uh.cfg.GetString("pfms.password")
	baseurl := uh.cfg.GetString("pfms.baseurl")
	header := map[string]string{"Content-Type": "application/json"}

	// Step 1 — GetAuthCode
	resp1, err := uh.CallAPI(baseurl+"/GetAuthCode", "POST", header, map[string]interface{}{
		"UserName":      username,
		"RequestSource": requestsource,
	})
	if err != nil {
		apierrors.HandleError(ctx, err)
		return
	}
	if resp1["IsSuccess"].(string) == "0" {
		ctx.JSON(http.StatusBadRequest, gin.H{"error": resp1["errorMessage"]})
		return
	}
	authcode := resp1["AuthCode"].(string)

	// Step 2 — Login
	resp2, err := uh.CallAPI(baseurl+"/LogIn", "POST", header, map[string]interface{}{
		"userName": username,
		"password": password + authcode,
	})
	if err != nil {
		apierrors.HandleError(ctx, err)
		return
	}
	if resp2["isSuccess"].(string) == "0" {
		ctx.JSON(http.StatusBadRequest, gin.H{"error": resp2["errorMessage"]})
		return
	}
	accesstoken := resp2["accessToken"].(string)

	// Step 3 — Send negative payload to PFMS
	resp3, err := uh.CallAPI(
		baseurl+"/Budget/ReceiveTransferEntryData",
		"POST",
		map[string]string{
			"Content-Type":  "application/json",
			"Authorization": "Bearer " + accesstoken,
		},
		negativePayload,
	)
	if err != nil {
		apierrors.HandleError(ctx, err)
		return
	}

	// Capture PFMS response
	pfmsStatus := "Success"
	pfmsError := ""
	if resp3["isSuccess"].(string) == "0" {
		pfmsStatus = "Failed"
		pfmsError = fmt.Sprintf("%v", resp3["errorMessage"])
	}

	// ---------------- ALWAYS INSERT AUDIT — regardless of PFMS response ----------------
	auditCtx, auditCancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer auditCancel()

	// Insert new row in pfms_submission (negative entry)
	errInsert := uh.svc.InsertReversalPfmsSubmission(
		auditCtx,
		newUniqueID,           // new unique id
		"terev",               // type
		original.TeRequest,    // same te_request as original
		original.BusinessDate, // same business date
		time.Now(),            // submission date = now
		negativePayload,       // flipped payload
		pfmsStatus,            // Success or Failed
		pfmsError,             // PFMS error if any
		req.PfmsUniqueId,      // original_pfms_uid — links back to original
	)
	if errInsert != nil {
		log.Error(ctx, "Failed to insert reversal pfms_submission: %v", errInsert)
	}

	// Insert audit row in pfms_te_reversal
	errAudit := uh.svc.InsertTeReversal(
		auditCtx,
		req.PfmsUniqueId,      // original unique id
		newUniqueID,           // reversal unique id
		original.TeNumber,     // original te_number
		original.BusinessDate, // original business date
		req.RequestEmployeeID,
		req.Remark,
		pfmsStatus,
		pfmsError,
	)
	if errAudit != nil {
		log.Error(ctx, "Failed to insert pfms_te_reversal audit: %v", errAudit)
	}

	// ---------------- RESPONSE ----------------
	if pfmsStatus == "Failed" {
		ctx.JSON(http.StatusBadRequest, gin.H{
			"error":              pfmsError,
			"original_unique_id": req.PfmsUniqueId,
			"reversal_unique_id": newUniqueID,
		})
		return
	}

	ctx.JSON(http.StatusOK, gin.H{
		"original_unique_id": req.PfmsUniqueId,
		"reversal_unique_id": newUniqueID,
		"status":             "Reversed successfully",
	})
}

// FlipPayloadAmounts flips all amounts and signs in the payload
func FlipPayloadAmounts(payload domain.Payload) domain.Payload {
	for i := range payload.RequestPayload.TransferEntryDetails {
		details := payload.RequestPayload.TransferEntryDetails[i].TransferEntryData.TransferEntryAccountingDetails
		for j := range details {
			detail := &details[j]

			// Flip amount by negating the float64 value.
			detail.Amount = -detail.Amount

			// Flip sign
			if detail.Sign == "+" {
				detail.Sign = "-"
			} else if detail.Sign == "-" {
				detail.Sign = "+"
			}
		}
	}
	return payload
}

// GetWorkflowStatus godoc
//
//	@Summary		Get workflow details and status of a Transfer Entry
//	@Description	Get workflow details and status of a Transfer Entry
//	@Tags			Transfer Entry
//	@Accept			json
//	@Produce		json
//	@Param			workflow_id	path		string	true	"Workflow ID"
//	@Success		200			{object}	repository.StandardizedOutput				"Workflow details retrieved successfully"
//	@Failure		400			{object}	apierrors.APIErrorResponse		"Validation error"
//	@Failure		401			{object}	apierrors.APIErrorResponse		"Unauthorized error"
//	@Failure		403			{object}	apierrors.APIErrorResponse		"Forbidden error"
//	@Failure		404			{object}	apierrors.APIErrorResponse		"Workflow not found"
//	@Failure		500			{object}	apierrors.APIErrorResponse		"Internal server error"
//	@Router			/v1/transfer-entry/sub-accounts/workflow/status/{workflow_id} [get]
func (h *TransferEntryHandler) GetWorkflowStatus(gctx *gin.Context) {
	ctx := gctx.Request.Context()

	workflowID := gctx.Param("workflow_id")
	if workflowID == "" {
		log.Error(ctx, "workflow_id is required")
		gctx.JSON(http.StatusBadRequest, gin.H{"error": "workflow_id is required"})
		return
	}

	output, err := h.svb.GetWorkflowDetails(gctx, h.client, workflowID)
	if err != nil {
		if strings.Contains(err.Error(), "no workflow found") {
			log.Error(ctx, "workflow not found: %v", err)
			gctx.JSON(http.StatusNotFound, gin.H{"error": err.Error()})
			return
		}
		log.Error(ctx, "failed to get workflow details: %v", err)
		gctx.JSON(http.StatusInternalServerError, gin.H{"error": err.Error()})
		return
	}

	gctx.JSON(http.StatusOK, output)
}
