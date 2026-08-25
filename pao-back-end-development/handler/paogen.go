package handler

import (
	//"database/sql"

	//"github.com/templatedop/githubrepo/dtime"
	"bytes"
	"context"
	"crypto/tls"
	"encoding/csv"
	"encoding/json"
	"errors"
	"fmt"
	"gotemplate/core/domain"
	"io"
	"math"
	"math/rand"
	"net/http"
	"strconv"
	"strings"

	"time"

	//"time"

	//"github.com/guregu/null"

	//"github.com/jackc/pgx/v5/pgtype"
	//"github.com/aarondl/opt/null"

	//"github.com/volatiletech/null"

	"gotemplate/core/port"
	"gotemplate/handler/response"
	repo "gotemplate/repo/postgres"

	log "gitlab.cept.gov.in/it-2.0-common/api-log"

	"github.com/gin-gonic/gin"
	"github.com/go-resty/resty/v2"

	// "github.com/google/uuid"
	"github.com/jackc/pgx/v5/pgconn"
	"github.com/volatiletech/null/v9"
	config "gitlab.cept.gov.in/it-2.0-common/api-config"
	apierrors "gitlab.cept.gov.in/it-2.0-common/api-errors"
	validation "gitlab.cept.gov.in/it-2.0-common/api-validation"
	//"gotemplate/dtime"
)

// UserHandler represents the HTTP handler for user-related requests
type PaogenHandler struct {
	svc *repo.PaogenRepository
	svs *repo.ObjectionFileRepository
	cfg *config.Config
}

// NewUserHandler creates a new UserHandler instance
func NewPaogenHandler(svc *repo.PaogenRepository, svs *repo.ObjectionFileRepository, cfg *config.Config) *PaogenHandler {
	return &PaogenHandler{
		svc,
		svs,
		cfg,
	}
}

type OfficeNameRequest struct {
	Id int64 `uri:"id" validate:"required,max=99999999"`
}

// Get Officename godoc
//
//	@Summary		Get the office name
//	@Description	Get the office name using officeid
//	@Tags			PAO GENERAL
//	@Accept			json
//	@Produce		json
//	@Param			id	path		int				true	"Office ID"
//	@Success		200	{object}	response.GetOfficenameResponse	"data retrieved successfully"
//	@Failure		400					{object}	apierrors.APIErrorResponse						"Validation error"
//	@Failure		401					{object}	apierrors.APIErrorResponse						"data retrieved successfully"
//	@Failure		403					{object}	apierrors.APIErrorResponse						"Forbidden error"
//	@Failure		404					{object}	apierrors.APIErrorResponse						"Data not found error"
//	@Failure		409					{object}	apierrors.APIErrorResponse						"Data conflict error"
//	@Failure		500					{object}	apierrors.APIErrorResponse						"Internal server error"
//	@Router			/v1/pao-gen/office-names/{id} [get]
func (uh *PaogenHandler) FetchOfficenameHandler(ctx *gin.Context) {

	var req OfficeNameRequest
	if err := ctx.ShouldBindUri(&req); err != nil {
		apierrors.HandleBindingError(ctx, err)
		log.Error(ctx, "Binding failed for OfficeNameRequest: %s", err.Error())
		return
	}
	if err := validation.ValidateStruct(req); err != nil {
		apierrors.HandleValidationError(ctx, err)
		log.Error(ctx, "Validation failed for OfficeNameRequest: %s", err.Error())
		return
	}
	request := domain.OfficeNameRequest{
		Id: req.Id,
	}
	u, b, err := uh.svc.GetOfficenameRepo(ctx, &request)
	if err != nil {
		apierrors.HandleDBError(ctx, err)
		log.Error(ctx, "Get Office details Repo call failed: %s", err.Error())
		return
	}
	if b {
		rsp := response.NewGetOfficenameResponse(*u)

		metadata := port.NewMetaDataResponse(0, 0, 1)

		apiRsp := response.GetOfficenameResponse{
			StatusCodeAndMessage: port.FetchSucess,
			MetaDataResponse:     metadata,
			Data:                 rsp,
		}
		log.Debug(ctx, "FetchOfficenameHandler response", apiRsp)
		handleSuccess(ctx, apiRsp)
	} else {
		err1 := errors.New("Invalid office_id")
		// Create an AppError with a user-friendly message and code.
		appError := apierrors.NewAppError(
			"No office corresponding to this office_id", // User-friendly error message
			"404", // Error code representing the error type
			err1,  // Original error for debugging purposes
		)
		apiErrorResponse := apierrors.NewAPIErrorResponse(
			http.StatusNotFound, // HTTP status code
			"Not Found",         // Message to return to the client
			appError,            // Encapsulated application error
		)
		ctx.JSON(apiErrorResponse.StatusCode, apiErrorResponse)
	}
}

// Get PAOs List godoc
//
//	@Summary		GET the list of PAOs
//	@Description	GET the list of PAOs
//	@Tags			PAO GENERAL
//	@Accept			json
//	@Produce		json
//	@Success		200		{object}	response.GetPAOsResponse			"list retrieved successfully"
//	@Failure		400					{object}	apierrors.APIErrorResponse						"Validation error"
//	@Failure		401					{object}	apierrors.APIErrorResponse						"data retrieved successfully"
//	@Failure		403					{object}	apierrors.APIErrorResponse						"Forbidden error"
//	@Failure		404					{object}	apierrors.APIErrorResponse						"Data not found error"
//	@Failure		409					{object}	apierrors.APIErrorResponse						"Data conflict error"
//	@Failure		500					{object}	apierrors.APIErrorResponse						"Internal server error"
//	@Router			/v1/pao-gen/pao [get]
func (uh *PaogenHandler) ListPAOHandler(ctx *gin.Context) {

	u, err := uh.svc.GetPAOsRepo(ctx)
	if err != nil {
		apierrors.HandleDBError(ctx, err)
		log.Error(ctx, "Get PAO List Repo call failed: %s", err.Error())
		return
	}

	rsp := response.NewGetPAOsResponse(u)

	metadata := port.NewMetaDataResponse(0, 0, len(rsp))

	apiRsp := response.GetPAOsResponse{
		StatusCodeAndMessage: port.ListSuccess,
		MetaDataResponse:     metadata,
		Data:                 rsp,
	}
	log.Debug(ctx, "ListPAOHandler response", apiRsp)
	handleSuccess(ctx, apiRsp)

}

type DdosRequest struct {
	PaoCode  string `uri:"pao-code" binding:"required,len=6" validate:"required,validatePaocode"`
	OfficeId string `form:"office-id" binding:"omitempty,len=8" validate:"omitempty,max=99999999"`
	port.MetaDataRequest
}

const ErrBindingDdoListRequest = "Binding failed for DdoListRequest: %s"

// Get DDOs godoc
//
//	@Summary		Get the list of DDOs
//	@Description	Get the list of DDOs under a PAO
//	@Tags			PAO GENERAL
//	@Accept			json
//	@Produce		json
//	@Param			pao-code	path		string			true	"pao-code"
//
// @Param       office-id    query       string     			false   		"Office-Id"
//
// @Param       skip    query       int     			false   		"Number of records to skip for pagination"
// @Param       limit   query       int     			false   		"Number of records to limit for pagination"
//
//	@Success		200		{object}	response.GetDDOsResponse			"list retrieved successfully"
//	@Failure		400					{object}	apierrors.APIErrorResponse						"Validation error"
//	@Failure		401					{object}	apierrors.APIErrorResponse						"data retrieved successfully"
//	@Failure		403					{object}	apierrors.APIErrorResponse						"Forbidden error"
//	@Failure		404					{object}	apierrors.APIErrorResponse						"Data not found error"
//	@Failure		409					{object}	apierrors.APIErrorResponse						"Data conflict error"
//	@Failure		500					{object}	apierrors.APIErrorResponse						"Internal server error"
//	@Router			/v1/pao-gen/pao/{pao-code}/ddos [get]
func (uh *PaogenHandler) ListDDOHandler(ctx *gin.Context) {

	var req DdosRequest
	if err := ctx.ShouldBindUri(&req); err != nil {
		apierrors.HandleBindingError(ctx, err)
		log.Error(ctx, ErrBindingDdoListRequest, err.Error())
		return
	}
	if err1 := ctx.ShouldBindQuery(&req); err1 != nil {
		apierrors.HandleBindingError(ctx, err1)
		log.Error(ctx, ErrBindingDdoListRequest, err1.Error())
		return
	}
	if err := validation.ValidateStruct(req); err != nil {
		apierrors.HandleValidationError(ctx, err)
		log.Error(ctx, "Validation failed for DdosRequest: %s", err.Error())
		return
	}
	if req.Limit == 0 {
		req.Limit = math.MaxInt32
	}
	request := domain.DdosRequest{
		PaoCode:  req.PaoCode,
		OfficeId: req.OfficeId,
	}
	u, err := uh.svc.GetDDOsRepo(ctx, &request, req.MetaDataRequest)
	if err != nil {
		apierrors.HandleDBError(ctx, err)
		log.Error(ctx, "List DDOs Repo call failed: %s", err.Error())
		return
	}

	rsp := response.NewGetDDOsResponse(u)

	metadata := port.NewMetaDataResponse(req.Skip, req.Limit, len(rsp))

	apiRsp := response.GetDDOsResponse{
		StatusCodeAndMessage: port.ListSuccess,
		MetaDataResponse:     metadata,
		Data:                 rsp,
	}
	log.Debug(ctx, "ListDDOHandler response", apiRsp)
	handleSuccess(ctx, apiRsp)

}

type DdoListRequest struct {
	PaoCode string `uri:"pao-code" binding:"required,len=6" validate:"required,validatePaocode"`
	Date    string `form:"date" binding:"omitempty,len=10" validate:"required,date_yyyy_mm_dd"`
	port.MetaDataRequest
}

// Get DDO List godoc
//
//	@Summary		Get the list of DDOs
//	@Description	Get the list of DDOs under a PAO
//	@Tags			PAO GENERAL
//	@Accept			json
//	@Produce		json
//	@Param			pao-code	path		string			true	"Pao-code"
//	@Param			date	query		string			true	"Date"
//
// @Param       skip    query       int     			false   		"Number of records to skip for pagination"
// @Param       limit   query       int     			false   		"Number of records to limit for pagination"
//
//	@Success		200		{object}	response.GetDDOlistResponse			"list retrieved successfully"
//	@Failure		400					{object}	apierrors.APIErrorResponse						"Validation error"
//	@Failure		401					{object}	apierrors.APIErrorResponse						"data retrieved successfully"
//	@Failure		403					{object}	apierrors.APIErrorResponse						"Forbidden error"
//	@Failure		404					{object}	apierrors.APIErrorResponse						"Data not found error"
//	@Failure		409					{object}	apierrors.APIErrorResponse						"Data conflict error"
//	@Failure		500					{object}	apierrors.APIErrorResponse						"Internal server error"
//	@Router			/v1/pao-gen/pao/{pao-code}/cashbook/ddo-lists [get]
func (uh *PaogenHandler) ListDDOPFMSHandler(ctx *gin.Context) {

	var req DdoListRequest
	if err := ctx.ShouldBindUri(&req); err != nil {
		apierrors.HandleBindingError(ctx, err)
		log.Error(ctx, ErrBindingDdoListRequest, err.Error())
		return
	}
	if err1 := ctx.ShouldBindQuery(&req); err1 != nil {
		apierrors.HandleBindingError(ctx, err1)
		log.Error(ctx, ErrBindingDdoListRequest, err1.Error())
		return
	}
	if err := validation.ValidateStruct(req); err != nil {
		apierrors.HandleValidationError(ctx, err)
		log.Error(ctx, "Validation failed for DdoListRequest: %s", err.Error())
		return
	}
	if req.Limit == 0 {
		req.Limit = math.MaxInt32
	}
	request := domain.DdoListRequest{
		PaoCode: req.PaoCode,
		Date:    req.Date,
	}
	u, err := uh.svc.GetDDOlistRepo(ctx, &request, req.MetaDataRequest)
	if err != nil {
		apierrors.HandleDBError(ctx, err)
		log.Error(ctx, "ListDDOPFMS Repo call failed: %s", err.Error())
		return
	}

	rsp := response.NewGetDDOlistResponse(u)

	metadata := port.NewMetaDataResponse(req.Skip, req.Limit, len(rsp))

	apiRsp := response.GetDDOlistResponse{
		StatusCodeAndMessage: port.ListSuccess,
		MetaDataResponse:     metadata,
		Data:                 rsp,
	}
	log.Debug(ctx, "ListDDOPFMSHandler response", apiRsp)
	handleSuccess(ctx, apiRsp)

}

type DdoListRequestUpdate struct {
	PaoCode  string `uri:"pao-code" binding:"required,len=6" validate:"required,validatePaocode"`
	FromDate string `form:"from-date" binding:"omitempty,len=10" validate:"required,date_yyyy_mm_dd"`
	ToDate   string `form:"to-date" validate:"required,date_yyyy_mm_dd"`
}

// Get DDO List Update godoc
//
//	@Summary		Update the list of DDOs
//	@Description	Update the list of DDOs under a PAO
//	@Tags			PAO GENERAL
//	@Accept			json
//	@Produce		json
//	@Param			pao-code	path		string			true	"Pao-code"
//	@Param			from-date	query		string			true	"Fromdate"
//	@Param			to-date	query		string			true	"Todate"
//	@Success		200		{object}	response.GetDDOlistResponse			"resource updated successfully"
//	@Failure		400					{object}	apierrors.APIErrorResponse						"Validation error"
//	@Failure		401					{object}	apierrors.APIErrorResponse						"data retrieved successfully"
//	@Failure		403					{object}	apierrors.APIErrorResponse						"Forbidden error"
//	@Failure		404					{object}	apierrors.APIErrorResponse						"Data not found error"
//	@Failure		409					{object}	apierrors.APIErrorResponse						"Data conflict error"
//	@Failure		500					{object}	apierrors.APIErrorResponse						"Internal server error"
//	@Router			/v1/pao-gen/pao/{pao-code}/cashbook/ddo-lists [put]
func (uh *PaogenHandler) UpdateDDOCashbookListHandler(ctx *gin.Context) {

	var req DdoListRequestUpdate
	if err := ctx.ShouldBindUri(&req); err != nil {
		apierrors.HandleBindingError(ctx, err)
		log.Error(ctx, "Binding failed for DdoListRequestUpdate: %s", err.Error())
		return
	}
	if err1 := ctx.ShouldBindQuery(&req); err1 != nil {
		apierrors.HandleBindingError(ctx, err1)
		log.Error(ctx, "Binding failed for DdoListRequestUpdate: %s", err1.Error())
		return
	}
	if err := validation.ValidateStruct(req); err != nil {
		apierrors.HandleValidationError(ctx, err)
		log.Error(ctx, "Validation failed for DdoListRequestUpdate: %s", err.Error())
		return
	}
	if req.FromDate == req.ToDate {
		// Convert ToDate to time.Time, subtract two days, and assign it to FromDate
		toDate, err := time.Parse("2006-01-02", req.ToDate)
		if err != nil {
			apierrors.HandleValidationError(ctx, err)
			log.Error(ctx, "Failed to parse ToDate: %s", err.Error())
			return
		}
		req.FromDate = toDate.AddDate(0, 0, -2).Format("2006-01-02")
	}
	request := domain.DdoListRequestUpdate{
		PaoCode:  req.PaoCode,
		FromDate: req.FromDate,
		ToDate:   req.ToDate,
	}
	err := uh.svc.GetDDOlistupdateRepo(ctx, &request)
	if err != nil {
		apierrors.HandleDBError(ctx, err)
		log.Error(ctx, "ListDDOlistupdate Repo call failed: %s", err.Error())
		return
	}

	apiRsp := response.GetDDOlistResponse{
		StatusCodeAndMessage: port.UpdateSuccess,
	}
	log.Debug(ctx, "UpdateDDOCashbookListHandler response", apiRsp)
	handleSuccess(ctx, apiRsp)

}

type DdoDetailRequest struct {
	DdoCode string `uri:"ddo-code" binding:"required,len=6" validate:"required,validateDdocode"`
	Date    string `form:"date"  binding:"omitempty,len=10" validate:"required,date_yyyy_mm_dd"`
	port.MetaDataRequest
}

// Get DDO List godoc
//
//	@Summary		Get the list of DDOs
//	@Description	Get the list of DDOs under a PAO
//	@Tags			PAO GENERAL
//	@Accept			json
//	@Produce		json
//	@Param			ddo-code	path		string			true	"Ddo-code"
//	@Param			date	query		string			true	"Date"
//
// @Param       skip    query       int     			false   		"Number of records to skip for pagination"
// @Param       limit   query       int     			false   		"Number of records to limit for pagination"
//
//	@Success		200		{object}	response.GetDDOdetailResponse			"data retrieved successfully"
//	@Failure		400					{object}	apierrors.APIErrorResponse						"Validation error"
//	@Failure		401					{object}	apierrors.APIErrorResponse						"data retrieved successfully"
//	@Failure		403					{object}	apierrors.APIErrorResponse						"Forbidden error"
//	@Failure		404					{object}	apierrors.APIErrorResponse						"Data not found error"
//	@Failure		409					{object}	apierrors.APIErrorResponse						"Data conflict error"
//	@Failure		500					{object}	apierrors.APIErrorResponse						"Internal server error"
//	@Router			/v1/pao-gen/ddo/{ddo-code}/cashbook/ddo-details [get]
func (uh *PaogenHandler) FetchDDOCashbookHandler(ctx *gin.Context) {

	var req DdoDetailRequest
	if err := ctx.ShouldBindUri(&req); err != nil {
		apierrors.HandleBindingError(ctx, err)
		log.Error(ctx, "Binding failed for DdoDetailRequest: %s", err.Error())
		return
	}
	if err1 := ctx.ShouldBindQuery(&req); err1 != nil {
		apierrors.HandleBindingError(ctx, err1)
		log.Error(ctx, "Binding failed for DdoDetailRequest: %s", err1.Error())
		return
	}
	if err := validation.ValidateStruct(req); err != nil {
		apierrors.HandleValidationError(ctx, err)
		log.Error(ctx, "Validation failed for DdoDetailRequest: %s", err.Error())
		return
	}
	if req.Limit == 0 {
		req.Limit = math.MaxInt32
	}

	request := domain.DdoDetailRequest{
		DdoCode: req.DdoCode,
		Date:    req.Date,
	}
	u, err := uh.svc.GetDDOdetailRepo(ctx, &request, req.MetaDataRequest)
	if err != nil {
		apierrors.HandleDBError(ctx, err)
		log.Error(ctx, "Get DDO Details Repo call failed: %s", err.Error())
		return
	}
	if len(u) == 0 {
		v, err := uh.svc.CheckEmptyCashbookRepo(ctx, &request, req.MetaDataRequest)
		if err != nil {
			apierrors.HandleDBError(ctx, err)
			log.Error(ctx, "Get DDO Details Repo call failed: %s", err.Error())
			return
		}
		if len(v) != 0 {
			rsp := response.NewGetDDOdetailResponse(v)

			metadata := port.NewMetaDataResponse(req.Skip, req.Limit, len(rsp))

			apiRsp := response.GetDDOdetailResponse{
				StatusCodeAndMessage: port.FetchSucess,
				MetaDataResponse:     metadata,
				Data:                 rsp,
			}
			log.Debug(ctx, "FetchDDOCashbookHandler response", apiRsp)
			handleSuccess(ctx, apiRsp)
			return
		}
	}

	rsp := response.NewGetDDOdetailResponse(u)

	metadata := port.NewMetaDataResponse(req.Skip, req.Limit, len(rsp))

	apiRsp := response.GetDDOdetailResponse{
		StatusCodeAndMessage: port.FetchSucess,
		MetaDataResponse:     metadata,
		Data:                 rsp,
	}
	log.Debug(ctx, "FetchDDOCashbookHandler response", apiRsp)
	handleSuccess(ctx, apiRsp)

}

type PfmsVerified struct {
	DdoCode            string      `json:"ddo_code" select:"ddo_code" validate:"required,validateDdocode"`
	BusinessDate       time.Time   `json:"business_date" select:"business_date" validate:"required"`
	ClosingBal         float64     `json:"closing_bal" select:"closing_bal"`
	OpeningBal         float64     `json:"opening_bal" select:"opening_bal"`
	VerifiedBy         uint64      `json:"verified_by" select:"verified_by" validate:"required,employee_id"`
	VerificationStatus string      `json:"h_verification" select:"h_verification_flag" validate:"required,max=20"`
	Hoa                string      `json:"hoa" select:"hoa" validate:"required,head_of_account"`
	Payment            float64     `json:"payment" select:"payment"`
	Receipt            float64     `json:"receipt" select:"receipt"`
	AccountCodeArray   []CodeArray `json:"account_array" select:"account_array" validate:"dive"`
}
type CodeArray struct {
	AccountCode            string  `json:"account_code" validate:"required,account_no"`
	AccountCodeDescription string  `json:"account_code_description" validate:"max=255"`
	Receipt                float64 `json:"receipt"`
	Payment                float64 `json:"payment"`
}

const ErrCashbookVerificationFailed = "cashbook Verification Failed"

// CreatePFMSVerificationHandler godoc
//
//	@Summary		Post Pfms verified data into database
//	@Description	Post Pfms verified data into database
//	@Tags			PAO GENERAL
//	@Accept			json
//	@Produce		json
//	@Param			body	body		[]PfmsVerified	true	"Post pfms verified request"
//	@Success		201		{object}	response.GetDDOlistResponse			"resource created successfully"
//	@Failure		400					{object}	apierrors.APIErrorResponse						"Validation error"
//	@Failure		401					{object}	apierrors.APIErrorResponse						"data retrieved successfully"
//	@Failure		403					{object}	apierrors.APIErrorResponse						"Forbidden error"
//	@Failure		404					{object}	apierrors.APIErrorResponse						"Data not found error"
//	@Failure		409					{object}	apierrors.APIErrorResponse						"Data conflict error"
//	@Failure		500					{object}	apierrors.APIErrorResponse						"Internal server error"
//	@Router			/v1/pao-gen/cashbook/verifications [post]
func (uh *PaogenHandler) CreatePFMSVerificationHandler(ctx *gin.Context) {

	var requestsin []PfmsVerified
	if err := ctx.ShouldBindJSON(&requestsin); err != nil {
		apierrors.HandleBindingError(ctx, err)
		log.Error(ctx, "Binding failed for PfmsVerifieds: %s", err.Error())
		return
	}
	for _, r := range requestsin {
		if err := validation.ValidateStruct(r); err != nil {
			apierrors.HandleValidationError(ctx, err)
			log.Error(ctx, "Validation failed for PfmsVerifieds: %s", err.Error())
			return
		}
		// 1️⃣ Check for unmapped account code
		if r.Hoa == "999999999999999" {
			appError := apierrors.NewAppError(
				"unmapped account code found in cashbook",
				"409",
				errors.New("invalid hoa"),
			)
			apiErrorResponse := apierrors.NewAPIErrorResponse(
				http.StatusBadRequest,
				"Bad Request",
				appError,
			)
			ctx.JSON(apiErrorResponse.StatusCode, apiErrorResponse)
			return
		}

	}
	// spetemberFirst2025, _ := time.Parse("2006-01-02", "2025-09-01")
	// if requestsin[0].BusinessDate.After(spetemberFirst2025) {
	if len(requestsin) > 0 {
		ddoCode := requestsin[0].DdoCode
		businessDate := requestsin[0].BusinessDate
		openingBalance := requestsin[0].OpeningBal

		val, err := uh.svc.CheckPreviousCashbook(ctx, ddoCode, businessDate)

		if err != nil {
			log.Error(ctx, "CheckPreviousCashbook failed: %s", err.Error())
			apierrors.HandleDBError(ctx, err)
			return
		}
		if val != nil {
			// 1️⃣ Check verification flag
			if !val.H_verification_flag.Valid || !val.H_verification_flag.Bool {
				appError := apierrors.NewAppError(
					"Previous cashbooks pending for verification",
					"409",
					errors.New("previous cashbook not verified"),
				)
				apiErrorResponse := apierrors.NewAPIErrorResponse(http.StatusConflict, "Conflict", appError)
				ctx.JSON(apiErrorResponse.StatusCode, apiErrorResponse)
				return
			}

			// 2️⃣ Check PFMS generation flag
			if !val.H_pfms_generation_flag.Valid || !val.H_pfms_generation_flag.Bool {
				appError := apierrors.NewAppError(
					"Previous cashbooks pending for PFMS submission",
					"409",
					errors.New("previous cashbook not submitted to PFMS"),
				)
				apiErrorResponse := apierrors.NewAPIErrorResponse(http.StatusConflict, "Conflict", appError)
				ctx.JSON(apiErrorResponse.StatusCode, apiErrorResponse)
				return
			}

			// 3️⃣ Check PFMS submission status
			if val.Pfms_submission_flag.Valid {
				flag := strings.ToLower(val.Pfms_submission_flag.String)
				if flag == "Pending" || flag == "Failed" {
					appError := apierrors.NewAppError(
						"Previous cashbook Failed or Pending",
						"409",
						errors.New("previous cashbook submission not completed"),
					)
					apiErrorResponse := apierrors.NewAPIErrorResponse(http.StatusConflict, "Conflict", appError)
					ctx.JSON(apiErrorResponse.StatusCode, apiErrorResponse)
					return
				}
			}

			if val.ClosingBal.Valid {
				if openingBalance != val.ClosingBal.Float64 {
					appError := apierrors.NewAppError(
						"Opening balance mismatch with previous cashbook",
						"409",
						errors.New("invalid opening balance"),
					)
					apiErrorResponse := apierrors.NewAPIErrorResponse(http.StatusConflict, "Conflict", appError)
					ctx.JSON(apiErrorResponse.StatusCode, apiErrorResponse)
					return
				}
			}
		}
	}
	//}

	var requests []domain.PfmsVerified

	for _, request := range requestsin {

		requests = append(requests, domain.PfmsVerified{

			DdoCode:            null.StringFrom(request.DdoCode),
			BusinessDate:       request.BusinessDate,
			ClosingBal:         null.Float64From(request.ClosingBal),
			OpeningBal:         null.Float64From(request.OpeningBal),
			VerifiedBy:         null.Uint64From(request.VerifiedBy),
			VerificationStatus: null.StringFrom(request.VerificationStatus),
			Hoa:                null.StringFrom(request.Hoa),
			Payment:            null.Float64From(request.Payment),
			Receipt:            null.Float64From(request.Receipt),
			AccountCodeArray:   convertOCodearrayToDomainCodearrayRequest(request.AccountCodeArray),
		})
	}
	err := uh.svc.PostPfmsverifiedRepo(ctx, requests)
	if err != nil {
		log.Error(ctx, "PFMS Verified Repo call failed: %s", err.Error())
		if err.(*pgconn.PgError).Code == "23503" {
			err1 := errors.New(ErrCashbookVerificationFailed)
			// Create an AppError with a user-friendly message and code.
			appError := apierrors.NewAppError(
				"cashbook not received", // User-friendly error message
				"409",                   // Error code representing the error type
				err1,                    // Original error for debugging purposes
			)
			apiErrorResponse := apierrors.NewAPIErrorResponse(
				http.StatusConflict, // HTTP status code
				"Conflict",          // Message to return to the client
				appError,            // Encapsulated application error
			)
			ctx.JSON(apiErrorResponse.StatusCode, apiErrorResponse)
			return
		} else if err.(*pgconn.PgError).Code == "23505" {
			err1 := errors.New(ErrCashbookVerificationFailed)
			// Create an AppError with a user-friendly message and code.
			appError := apierrors.NewAppError(
				"cashbook already verified", // User-friendly error message
				"409",                       // Error code representing the error type
				err1,                        // Original error for debugging purposes
			)
			apiErrorResponse := apierrors.NewAPIErrorResponse(
				http.StatusConflict, // HTTP status code
				"Conflict",          // Message to return to the client
				appError,            // Encapsulated application error
			)
			ctx.JSON(apiErrorResponse.StatusCode, apiErrorResponse)
			return

		} else {
			apierrors.HandleDBError(ctx, err)
			return
		}
	}

	apiRsp := response.GetDDOlistResponse{
		StatusCodeAndMessage: port.CreateSuccess,
	}
	log.Debug(ctx, "CreatePFMSVerificationHandler response", apiRsp)
	handleCreateSuccess(ctx, apiRsp)

}

type PfmsPendingRequest struct {
	PaoCode  string `uri:"pao-code" binding:"required,len=6" validate:"required,validatePaocode"`
	FromDate string `form:"from-date" binding:"omitempty,len=10" validate:"required,date_yyyy_mm_dd"`
	ToDate   string `form:"to-date" validate:"required,date_yyyy_mm_dd"`
	port.MetaDataRequest
}

// Get PFMS pending godoc
//
//	@Summary		Get the list of pfms pending
//	@Description	Get the list of pfms pending
//	@Tags			PAO GENERAL
//	@Accept			json
//	@Produce		json
//	@Param			pao-code	path		string			true	"Pao-code"
//	@Param			from-date	query		string			true	"Fromdate"
//	@Param			to-date	query		string			true	"Todate"
//
// @Param       skip    query       int     			false   		"Number of records to skip for pagination"
// @Param       limit   query       int     			false   		"Number of records to limit for pagination"
//
//	@Success		200		{object}	response.GetPfmspendingResponse			"list retrieved successfully"
//	@Failure		400					{object}	apierrors.APIErrorResponse						"Validation error"
//	@Failure		401					{object}	apierrors.APIErrorResponse						"Unauthorized error"
//	@Failure		403					{object}	apierrors.APIErrorResponse						"Forbidden error"
//	@Failure		404					{object}	apierrors.APIErrorResponse						"Data not found error"
//	@Failure		409					{object}	apierrors.APIErrorResponse						"Data conflict error"
//	@Failure		500					{object}	apierrors.APIErrorResponse						"Internal server error"
//	@Router			/v1/pao-gen/pao/{pao-code}/cashbook/verification-pending [get]
func (uh *PaogenHandler) ListPfmsPendingHandler(ctx *gin.Context) {

	var req PfmsPendingRequest
	if err := ctx.ShouldBindUri(&req); err != nil {
		apierrors.HandleBindingError(ctx, err)
		log.Error(ctx, "Binding failed for PfmsPendingRequest: %s", err.Error())
		return
	}
	if err1 := ctx.ShouldBindQuery(&req); err1 != nil {
		apierrors.HandleBindingError(ctx, err1)
		log.Error(ctx, "Binding failed for PfmsPendingRequest: %s", err1.Error())
		return
	}
	if err := validation.ValidateStruct(req); err != nil {
		apierrors.HandleValidationError(ctx, err)
		log.Error(ctx, "Validation failed for PfmsPendingRequest: %s", err.Error())
		return
	}
	if req.Limit == 0 {
		req.Limit = math.MaxInt32
	}

	request := domain.PfmsPendingRequest{
		PaoCode:  req.PaoCode,
		FromDate: req.FromDate,
		ToDate:   req.ToDate,
	}
	u, err := uh.svc.GetPfmspendingRepo(ctx, &request, req.MetaDataRequest)
	if err != nil {
		apierrors.HandleDBError(ctx, err)
		log.Error(ctx, "Get PFMS Pending Repo call failed: %s", err.Error())
		return
	}

	rsp := response.NewGetPfmspendingResponse(u)

	metadata := port.NewMetaDataResponse(req.Skip, req.Limit, len(rsp))

	apiRsp := response.GetPfmspendingResponse{
		StatusCodeAndMessage: port.FetchSucess,
		MetaDataResponse:     metadata,
		Data:                 rsp,
	}
	log.Debug(ctx, "ListPfmsPendingHandler response", apiRsp)
	handleSuccess(ctx, apiRsp)

}

type DdoListRequestMonthly struct {
	PaoCode string `uri:"pao-code" binding:"required,len=6" validate:"required,validatePaocode"`
	Period  string `form:"period" binding:"omitempty,len=6" validate:"required,validatePeriod"`
	port.MetaDataRequest
}

const ErrBindingDdoListRequestMonthly = "Binding failed for DdoListRequestMonthly: %s"

// ListDdoPfmsMonthlyHandler godoc
//
//	@Summary		Get the list of DDO list monthly detail
//	@Description	Get the list of DDO list monthly detail
//	@Tags			PAO GENERAL
//	@Accept			json
//	@Produce		json
//	@Param			pao-code	path		string			true	"Pao-code"
//	@Param			period	query		string			true	"Period"
//
// @Param       skip    query       int     			false   		"Number of records to skip for pagination"
// @Param       limit   query       int     			false   		"Number of records to limit for pagination"
//
//	@Success		200		{object}	response.GetDDOlistMonthlyResponse			"data retrieved successfully"
//	@Failure		400					{object}	apierrors.APIErrorResponse						"Validation error"
//	@Failure		401					{object}	apierrors.APIErrorResponse						"Unauthorized error"
//	@Failure		403					{object}	apierrors.APIErrorResponse						"Forbidden error"
//	@Failure		404					{object}	apierrors.APIErrorResponse						"Data not found error"
//	@Failure		409					{object}	apierrors.APIErrorResponse						"Data conflict error"
//	@Failure		500					{object}	apierrors.APIErrorResponse						"Internal server error"
//	@Router			/v1/pao-gen/pao/{pao-code}/cashaccount/ddo-lists [get]
func (uh *PaogenHandler) ListDdoPfmsMonthlyHandler(ctx *gin.Context) {

	var req DdoListRequestMonthly
	if err := ctx.ShouldBindUri(&req); err != nil {
		apierrors.HandleBindingError(ctx, err)
		log.Error(ctx, ErrBindingDdoListRequestMonthly, err.Error())
		return
	}
	if err1 := ctx.ShouldBindQuery(&req); err1 != nil {
		apierrors.HandleBindingError(ctx, err1)
		log.Error(ctx, ErrBindingDdoListRequestMonthly, err1.Error())
		return
	}
	if err := validation.ValidateStruct(req); err != nil {
		apierrors.HandleValidationError(ctx, err)
		log.Error(ctx, "Validation failed for DdoListRequestMonthly: %s", err.Error())
		return
	}
	if req.Limit == 0 {
		req.Limit = math.MaxInt32
	}

	request := domain.DdoListRequestMonthly{
		PaoCode: req.PaoCode,
		Period:  req.Period,
	}
	u, err := uh.svc.GetDDOlistMonthlyRepo(ctx, &request, req.MetaDataRequest)
	if err != nil {
		apierrors.HandleDBError(ctx, err)
		log.Error(ctx, "Get DDOlist monthly Repo call failed: %s", err.Error())
		return
	}

	rsp := response.NewGetDDOlistMonthlyResponse(u)

	metadata := port.NewMetaDataResponse(req.Skip, req.Limit, len(rsp))

	apiRsp := response.GetDDOlistMonthlyResponse{
		StatusCodeAndMessage: port.FetchSucess,
		MetaDataResponse:     metadata,
		Data:                 rsp,
	}
	log.Debug(ctx, "ListDdoPfmsMonthlyHandler response", apiRsp)
	handleSuccess(ctx, apiRsp)

}

type DdoListMonthlyQueryRequest struct {
	OfficeId string `uri:"office_id" validate:"required"`
	Period   string `form:"period" validate:"required"`
	port.MetaDataRequest
}

// ListDdoPfmsMonthlyHandler godoc
//
//	@Summary		Get the list of DDO list monthly detail based on office_id
//	@Description	Get the list of DDO list monthly detail based on office_id
//	@Tags			PAO GENERAL
//	@Accept			json
//	@Produce		json
//	@Param			office_id	path		string			true	"Office_id"
//	@Param			period	query		string			true	"Period"
//
// @Param       skip    query       int     			false   		"Number of records to skip for pagination"
// @Param       limit   query       int     			false   		"Number of records to limit for pagination"
//
//	@Success		200		{object}	response.GetDDOlistMonthlyResponse			"data retrieved successfully"
//	@Failure		400					{object}	apierrors.APIErrorResponse						"Validation error"
//	@Failure		401					{object}	apierrors.APIErrorResponse						"Unauthorized error"
//	@Failure		403					{object}	apierrors.APIErrorResponse						"Forbidden error"
//	@Failure		404					{object}	apierrors.APIErrorResponse						"Data not found error"
//	@Failure		409					{object}	apierrors.APIErrorResponse						"Data conflict error"
//	@Failure		500					{object}	apierrors.APIErrorResponse						"Internal server error"
//	@Router			/v1/pao-gen/pao/office/{office_id}/cashaccount/ddo-lists [get]
func (uh *PaogenHandler) ListDdoPfmsMonthlyOffHandler(ctx *gin.Context) {

	var req DdoListMonthlyQueryRequest
	if err := ctx.ShouldBindUri(&req); err != nil {
		apierrors.HandleBindingError(ctx, err)
		log.Error(ctx, "Error binding DDO list monthly URI: %s", err.Error())
		return
	}
	if err1 := ctx.ShouldBindQuery(&req); err1 != nil {
		apierrors.HandleBindingError(ctx, err1)
		log.Error(ctx, "Error binding DDO list monthly query: %s", err1.Error())
		return
	}
	if err := validation.ValidateStruct(req); err != nil {
		apierrors.HandleValidationError(ctx, err)
		log.Error(ctx, "Validation failed for DdoListMonthlyQueryRequest: %s", err.Error())
		return
	}
	if req.Limit == 0 {
		req.Limit = math.MaxInt32
	}

	request := domain.DdoListMonthlyQuery{
		OfficeId: req.OfficeId,
		Period:   req.Period,
	}
	u, err := uh.svc.GetDDOlistMonthlyOffRepo(ctx, &request, req.MetaDataRequest)
	if err != nil {
		apierrors.HandleDBError(ctx, err)
		log.Error(ctx, "Get DDOlist monthly Repo call failed: %s", err.Error())
		return
	}

	rsp := response.NewGetDDOlistMonthlyResponse(u)

	metadata := port.NewMetaDataResponse(req.Skip, req.Limit, len(rsp))

	apiRsp := response.GetDDOlistMonthlyResponse{
		StatusCodeAndMessage: port.FetchSucess,
		MetaDataResponse:     metadata,
		Data:                 rsp,
	}
	log.Debug(ctx, "ListDdoPfmsMonthlyHandler response", apiRsp)
	handleSuccess(ctx, apiRsp)

}

// UpdateDdoMonthlyHandler godoc
//
//	@Summary		Update the DDO list for monthly cash account status
//	@Description	Update the DDO list for monthly cash account status
//	@Tags			PAO GENERAL
//	@Accept			json
//	@Produce		json
//	@Param			pao-code	path		string			true	"Pao-code"
//	@Param			period	query		string			true	"Period"
//	@Success		200		{object}	response.GetDDOlistResponse			"resource updated successfully"
//	@Failure		400					{object}	apierrors.APIErrorResponse						"Validation error"
//	@Failure		401					{object}	apierrors.APIErrorResponse						"Unauthorized error"
//	@Failure		403					{object}	apierrors.APIErrorResponse						"Forbidden error"
//	@Failure		404					{object}	apierrors.APIErrorResponse						"Data not found error"
//	@Failure		409					{object}	apierrors.APIErrorResponse						"Data conflict error"
//	@Failure		500					{object}	apierrors.APIErrorResponse						"Internal server error"
//	@Router			/v1/pao-gen/pao/{pao-code}/cashaccount/ddo-lists [put]
func (uh *PaogenHandler) UpdateDdoMonthlyHandler(ctx *gin.Context) {

	var req DdoListRequestMonthly
	if err := ctx.ShouldBindUri(&req); err != nil {
		apierrors.HandleBindingError(ctx, err)
		log.Error(ctx, ErrBindingDdoListRequestMonthly, err.Error())
		return
	}
	if err1 := ctx.ShouldBindQuery(&req); err1 != nil {
		apierrors.HandleBindingError(ctx, err1)
		log.Error(ctx, ErrBindingDdoListRequestMonthly, err1.Error())
		return
	}
	if err := validation.ValidateStruct(req); err != nil {
		apierrors.HandleValidationError(ctx, err)
		log.Error(ctx, "Validation failed for DdoListRequestMonthly: %s", err.Error())
		return
	}

	request := domain.DdoListRequestMonthly{
		PaoCode: req.PaoCode,
		Period:  req.Period,
	}
	err := uh.svc.GetDDOlistMonthlyupdateRepo(ctx, &request)
	if err != nil {
		apierrors.HandleDBError(ctx, err)
		log.Error(ctx, "Update DDO list monthly Repo call failed: %s", err.Error())
		return
	}

	apiRsp := response.GetDDOlistResponse{
		StatusCodeAndMessage: port.UpdateSuccess,
	}
	log.Debug(ctx, "UpdateDdoMonthlyHandler response", apiRsp)
	handleSuccess(ctx, apiRsp)

}

type DdoDetailMonthlyRequest struct {
	DdoCode string `uri:"ddo-code" binding:"required,len=6" validate:"required,validateDdocode"`
	Period  string `form:"period" binding:"omitempty,len=6" validate:"required,validatePeriod"`
	port.MetaDataRequest
}

// FetchDdoMonthlyDetailHandler godoc
//
//	@Summary		Get the list of DDO monthly detail
//	@Description	Get the list of DDO monthly detail
//	@Tags			PAO GENERAL
//	@Accept			json
//	@Produce		json
//	@Param			ddo-code	path		string			true	"Ddo-code"
//	@Param			period	query		string			true	"Period"
//
// @Param       skip    query       int     			false   		"Number of records to skip for pagination"
// @Param       limit   query       int     			false   		"Number of records to limit for pagination"
//
//	@Success		200		{object}	response.GetDDOdetail_monthlyResponse			"data retrieved successfully"
//	@Failure		400					{object}	apierrors.APIErrorResponse						"Validation error"
//	@Failure		401					{object}	apierrors.APIErrorResponse						"Unauthorized error"
//	@Failure		403					{object}	apierrors.APIErrorResponse						"Forbidden error"
//	@Failure		404					{object}	apierrors.APIErrorResponse						"Data not found error"
//	@Failure		409					{object}	apierrors.APIErrorResponse						"Data conflict error"
//	@Failure		500					{object}	apierrors.APIErrorResponse						"Internal server error"
//	@Router			/v1/pao-gen/ddo/{ddo-code}/cashaccount/ddo-details [get]
func (uh *PaogenHandler) FetchDdoMonthlyDetailHandler(ctx *gin.Context) {

	var req DdoDetailMonthlyRequest
	if err := ctx.ShouldBindUri(&req); err != nil {
		apierrors.HandleBindingError(ctx, err)
		log.Error(ctx, "Binding failed for DdoDetailMonthlyRequest: %s", err.Error())
		return
	}
	if err1 := ctx.ShouldBindQuery(&req); err1 != nil {
		apierrors.HandleBindingError(ctx, err1)
		log.Error(ctx, "Binding failed for DdoDetailMonthlyRequest: %s", err1.Error())
		return
	}
	if err := validation.ValidateStruct(req); err != nil {
		apierrors.HandleValidationError(ctx, err)
		log.Error(ctx, "Validation failed for DdoDetailMonthlyRequest: %s", err.Error())
		return
	}
	if req.Limit == 0 {
		req.Limit = math.MaxInt32
	}

	request := domain.DdoDetailMonthlyRequest{
		DdoCode: req.DdoCode,
		Period:  req.Period,
	}
	u, err := uh.svc.GetDDOdetail_monthlyRepo(ctx, &request, req.MetaDataRequest)
	if err != nil {
		apierrors.HandleDBError(ctx, err)
		log.Error(ctx, "Get DDO details monthly Repo call failed: %s", err.Error())
		return
	}
	if len(u) == 0 {
		v, err := uh.svc.CheckEmptyCashbookMonthlyRepo(ctx, &request, req.MetaDataRequest)
		if err != nil {
			apierrors.HandleDBError(ctx, err)
			log.Error(ctx, "Get DDO details monthly Repo call failed: %s", err.Error())
			return
		}
		if len(v) > 0 {
			for _, emptyDetail := range v {
				u = append(u, domain.DdoDetailMonthly{
					DdoCode:        emptyDetail.DdoCode,
					OfficeName:     emptyDetail.OfficeName,
					OpeningBal:     emptyDetail.OpeningBal,
					ClosingBal:     emptyDetail.ClosingBal,
					Period:         emptyDetail.Period,
					Hoa:            null.StringFrom("000000000000000"),
					HoaDescription: null.StringFrom("NIL Transaction"),
					Payment:        null.Float64From(0.0),
					Receipt:        null.Float64From(0.0),
					TePayment:      null.Float64From(0.0),
					TeReceipt:      null.Float64From(0.0),
					AccountArray: []domain.CodeArray{
						{
							AccountCode:            null.StringFrom("1111111111"),
							AccountCodeDescription: null.StringFrom("NIL Transaction"),
							Receipt:                null.Float64From(0.0),
							Payment:                null.Float64From(0.0),
						},
					},
				})
			}
		}
	}

	rsp := response.NewGetDDOdetail_monthlyResponse(u)

	metadata := port.NewMetaDataResponse(req.Skip, req.Limit, len(rsp))

	apiRsp := response.GetDDOdetail_monthlyResponse{
		StatusCodeAndMessage: port.FetchSucess,
		MetaDataResponse:     metadata,
		Data:                 rsp,
	}
	log.Debug(ctx, "FetchDdoMonthlyDetailHandler response", apiRsp)
	handleSuccess(ctx, apiRsp)
}

type PfmsVerifiedMonthly struct {
	DdoCode            string      `json:"ddo_code" select:"ddo_code" validate:"required,validateDdocode"`
	Period             string      `json:"period" select:"period" validate:"required,validatePeriod"`
	ClosingBal         float64     `json:"closing_bal" select:"closing_bal" update:"closing_bal"`
	OpeningBal         float64     `json:"opening_bal" select:"opening_bal" update:"opening_bal"`
	VerifiedBy         uint64      `json:"verified_by" select:"verified_by" validate:"required,employee_id"`
	VerificationStatus string      `json:"h_verification" select:"h_verification_flag" validate:"required,max=20"`
	Hoa                string      `json:"hoa" select:"hoa" validate:"required,head_of_account"`
	Payment            float64     `json:"payment" select:"payment"`
	Receipt            float64     `json:"receipt" select:"receipt"`
	TePayment          float64     `json:"te_payment" select:"te_payment"`
	TeReceipt          float64     `json:"te_receipt" select:"te_receipt"`
	AccountCodeArray   []CodeArray `json:"account_array" select:"account_array" validate:"dive"`
}

const ErrCashAccountVerificationFailed = "cashaccount Verification Failed"

// CreatePfmsMonthlyVerifiedHandler godoc
//
//	@Summary		Post Pfms monthly verified data into database
//	@Description	Post Pfms monthly verified data into database
//	@Tags			PAO GENERAL
//	@Accept			json
//	@Produce		json
//	@Param			body	body		[]PfmsVerifiedMonthly	true	"Post pfms monthly verified request"
//	@Success		201		{object}	response.GetDDOlistResponse			"resource created successfully"
//	@Failure		400					{object}	apierrors.APIErrorResponse						"Validation error"
//	@Failure		401					{object}	apierrors.APIErrorResponse						"Unauthorized error"
//	@Failure		403					{object}	apierrors.APIErrorResponse						"Forbidden error"
//	@Failure		404					{object}	apierrors.APIErrorResponse						"Data not found error"
//	@Failure		409					{object}	apierrors.APIErrorResponse						"Data conflict error"
//	@Failure		500					{object}	apierrors.APIErrorResponse						"Internal server error"
//	@Router			/v1/pao-gen/cashaccount/verifications [post]
func (uh *PaogenHandler) CreatePfmsMonthlyVerifiedHandler(ctx *gin.Context) {

	var requestsin []PfmsVerifiedMonthly
	if err := ctx.ShouldBindJSON(&requestsin); err != nil {
		apierrors.HandleBindingError(ctx, err)
		log.Error(ctx, "Binding failed for PfmsVerifiedMonthlys: %s", err.Error())
		return
	}
	for _, r := range requestsin {
		if err := validation.ValidateStruct(r); err != nil {
			apierrors.HandleValidationError(ctx, err)
			log.Error(ctx, "Validation failed for PfmsVerifiedMonthlys: %s", err.Error())
			return
		}
	}

	var requests []domain.PfmsVerifiedMonthly

	for _, request := range requestsin {

		requests = append(requests, domain.PfmsVerifiedMonthly{

			DdoCode:            request.DdoCode,
			Period:             request.Period,
			ClosingBal:         null.Float64From(request.ClosingBal),
			OpeningBal:         null.Float64From(request.OpeningBal),
			VerifiedBy:         request.VerifiedBy,
			VerificationStatus: request.VerificationStatus,
			Hoa:                request.Hoa,
			Payment:            null.Float64From(request.Payment),
			Receipt:            null.Float64From(request.Receipt),
			TePayment:          null.Float64From(request.TePayment),
			TeReceipt:          null.Float64From(request.TeReceipt),
			AccountCodeArray:   convertOCodearrayToDomainCodearrayRequest(request.AccountCodeArray),
		})
	}
	if len(requests) == 1 {
		if requests[0].Hoa == "000000000000000" {
			err := uh.svc.PostEmptyPfmsMonthlyverifiedRepo(ctx, requests)
			if err != nil {
				log.Error(ctx, "PFMS Verified Repo call failed: %s", err.Error())
				if err.(*pgconn.PgError).Code == "23503" {
					err1 := errors.New(ErrCashbookVerificationFailed)
					// Create an AppError with a user-friendly message and code.
					appError := apierrors.NewAppError(
						"cashaccount not received", // User-friendly error message
						"409",                      // Error code representing the error type
						err1,                       // Original error for debugging purposes
					)
					apiErrorResponse := apierrors.NewAPIErrorResponse(
						http.StatusConflict, // HTTP status code
						"Conflict",          // Message to return to the client
						appError,            // Encapsulated application error
					)
					ctx.JSON(apiErrorResponse.StatusCode, apiErrorResponse)
					return
				} else if err.(*pgconn.PgError).Code == "23505" {
					err1 := errors.New(ErrCashbookVerificationFailed)
					// Create an AppError with a user-friendly message and code.
					appError := apierrors.NewAppError(
						"cashaccount already verified", // User-friendly error message
						"409",                          // Error code representing the error type
						err1,                           // Original error for debugging purposes
					)
					apiErrorResponse := apierrors.NewAPIErrorResponse(
						http.StatusConflict, // HTTP status code
						"Conflict",          // Message to return to the client
						appError,            // Encapsulated application error
					)
					ctx.JSON(apiErrorResponse.StatusCode, apiErrorResponse)
					return

				} else {
					apierrors.HandleDBError(ctx, err)
					return
				}
			}

			apiRsp := response.GetDDOlistResponse{
				StatusCodeAndMessage: port.CreateSuccess,
			}
			log.Debug(ctx, "CreatePfmsMonthlyVerifiedHandler response", apiRsp)
			handleCreateSuccess(ctx, apiRsp)
			return
		}
	}
	err := uh.svc.PostPfmsMonthlyverifiedRepo(ctx, requests)
	if err != nil {
		log.Error(ctx, "PFMS monthly verified Repo call failed: %s", err.Error())
		if err.(*pgconn.PgError).Code == "23503" {
			err1 := errors.New(ErrCashAccountVerificationFailed)
			// Create an AppError with a user-friendly message and code.
			appError := apierrors.NewAppError(
				"cashaccount not received", // User-friendly error message
				"409",                      // Error code representing the error type
				err1,                       // Original error for debugging purposes
			)
			apiErrorResponse := apierrors.NewAPIErrorResponse(
				http.StatusConflict, // HTTP status code
				"Conflict",          // Message to return to the client
				appError,            // Encapsulated application error
			)
			ctx.JSON(apiErrorResponse.StatusCode, apiErrorResponse)
			return
		} else if err.(*pgconn.PgError).Code == "23505" {
			err1 := errors.New(ErrCashAccountVerificationFailed)
			// Create an AppError with a user-friendly message and code.
			appError := apierrors.NewAppError(
				"cashaccount already verified", // User-friendly error message
				"409",                          // Error code representing the error type
				err1,                           // Original error for debugging purposes
			)
			apiErrorResponse := apierrors.NewAPIErrorResponse(
				http.StatusConflict, // HTTP status code
				"Conflict",          // Message to return to the client
				appError,            // Encapsulated application error
			)
			ctx.JSON(apiErrorResponse.StatusCode, apiErrorResponse)
			return

		} else {
			apierrors.HandleDBError(ctx, err)
			return
		}
	}

	apiRsp := response.GetDDOlistResponse{
		StatusCodeAndMessage: port.CreateSuccess,
	}
	log.Debug(ctx, "CreatePfmsMonthlyVerifiedHandler response", apiRsp)
	handleCreateSuccess(ctx, apiRsp)

}

func (uh *PaogenHandler) CreatePfmsMonthlyVerifiedHandlerfortest21042026(ctx *gin.Context) {

	// ✅ STEP 1: Read and log raw request
	bodyBytes, err := io.ReadAll(ctx.Request.Body)
	if err != nil {
		log.Error(ctx, "Failed to read request body: %s", err.Error())
		apierrors.HandleBindingError(ctx, err)
		return
	}

	log.Debug(ctx, "RAW REQUEST: %s", string(bodyBytes))

	// ✅ Restore body so Gin can bind again
	ctx.Request.Body = io.NopCloser(bytes.NewBuffer(bodyBytes))

	// ✅ STEP 2: Bind request
	var requestsin []PfmsVerifiedMonthly
	if err := ctx.ShouldBindJSON(&requestsin); err != nil {
		apierrors.HandleBindingError(ctx, err)
		log.Error(ctx, "Binding failed for PfmsVerifiedMonthlys: %s", err.Error())
		return
	}

	// ✅ STEP 3: Pretty print parsed request
	prettyJSON, _ := json.MarshalIndent(requestsin, "", "  ")
	log.Debug(ctx, "PARSED REQUEST: %s", string(prettyJSON))

	// ✅ STEP 4: Validate
	for _, r := range requestsin {
		if err := validation.ValidateStruct(r); err != nil {
			apierrors.HandleValidationError(ctx, err)
			log.Error(ctx, "Validation failed: %s", err.Error())
			return
		}
	}

	// ✅ STEP 5: Detect duplicate HOA (VERY IMPORTANT)
	seen := make(map[string]bool)
	for _, r := range requestsin {
		key := r.DdoCode + r.Period + r.Hoa

		if seen[key] {
			log.Error(ctx, "🚨 DUPLICATE HOA FOUND IN REQUEST: %s", key)
		}
		seen[key] = true
	}

	// ✅ STEP 6: Convert to domain
	var requests []domain.PfmsVerifiedMonthly

	for _, request := range requestsin {
		requests = append(requests, domain.PfmsVerifiedMonthly{
			DdoCode:            request.DdoCode,
			Period:             request.Period,
			ClosingBal:         null.Float64From(request.ClosingBal),
			OpeningBal:         null.Float64From(request.OpeningBal),
			VerifiedBy:         request.VerifiedBy,
			VerificationStatus: request.VerificationStatus,
			Hoa:                request.Hoa,
			Payment:            null.Float64From(request.Payment),
			Receipt:            null.Float64From(request.Receipt),
			TePayment:          null.Float64From(request.TePayment),
			TeReceipt:          null.Float64From(request.TeReceipt),
			AccountCodeArray:   convertOCodearrayToDomainCodearrayRequest(request.AccountCodeArray),
		})
	}

	// ✅ STEP 7: Existing logic (unchanged)
	if len(requests) == 1 {
		if requests[0].Hoa == "000000000000000" {
			err := uh.svc.PostEmptyPfmsMonthlyverifiedRepo(ctx, requests)
			if err != nil {
				log.Error(ctx, "PFMS Verified Repo call failed: %s", err.Error())

				if err.(*pgconn.PgError).Code == "23503" {
					appError := apierrors.NewAppError(
						"cashaccount not received",
						"409",
						err,
					)
					apiErrorResponse := apierrors.NewAPIErrorResponse(
						http.StatusConflict,
						"Conflict",
						appError,
					)
					ctx.JSON(apiErrorResponse.StatusCode, apiErrorResponse)
					return

				} else if err.(*pgconn.PgError).Code == "23505" {
					appError := apierrors.NewAppError(
						"Duplicate PFMS detail entry (not verification issue)",
						"409",
						err,
					)
					apiErrorResponse := apierrors.NewAPIErrorResponse(
						http.StatusConflict,
						"Conflict",
						appError,
					)
					ctx.JSON(apiErrorResponse.StatusCode, apiErrorResponse)
					return

				} else {
					apierrors.HandleDBError(ctx, err)
					return
				}
			}

			handleCreateSuccess(ctx, response.GetDDOlistResponse{
				StatusCodeAndMessage: port.CreateSuccess,
			})
			return
		}
	}

	err = uh.svc.PostPfmsMonthlyverifiedRepo(ctx, requests)
	if err != nil {
		log.Error(ctx, "PFMS monthly verified Repo call failed: %s", err.Error())

		if err.(*pgconn.PgError).Code == "23503" {
			appError := apierrors.NewAppError(
				"cashaccount not received",
				"409",
				err,
			)
			apiErrorResponse := apierrors.NewAPIErrorResponse(
				http.StatusConflict,
				"Conflict",
				appError,
			)
			ctx.JSON(apiErrorResponse.StatusCode, apiErrorResponse)
			return

		} else if err.(*pgconn.PgError).Code == "23505" {
			appError := apierrors.NewAppError(
				"Duplicate PFMS detail entry (check HOA duplication in request)",
				"409",
				err,
			)
			apiErrorResponse := apierrors.NewAPIErrorResponse(
				http.StatusConflict,
				"Conflict",
				appError,
			)
			ctx.JSON(apiErrorResponse.StatusCode, apiErrorResponse)
			return

		} else {
			apierrors.HandleDBError(ctx, err)
			return
		}
	}

	handleCreateSuccess(ctx, response.GetDDOlistResponse{
		StatusCodeAndMessage: port.CreateSuccess,
	})
}

type PfmsSubmissionStatusListRequest struct {
	PaoCode  string `uri:"pao-code" binding:"required,len=6" validate:"required,validatePaocode"`
	FromDate string `form:"from-date" validate:"omitempty,date_yyyy_mm_dd"`
	ToDate   string `form:"to-date" validate:"omitempty,date_yyyy_mm_dd"`
	Status   string `form:"status" validate:"required,oneof=Pending Success Failed All"`
	port.MetaDataRequest
}

const ErrBindingPfmsXmlRequest = "Binding failed for PfmsXmlRequest: %s"

// ListPfmsSubmissionStatusHandler godoc
//
//	@Summary		Get the submission status list for pfms pending
//	@Description	Get the submission status list for pfms pending
//	@Tags			PAO GENERAL
//	@Accept			json
//	@Produce		json
//	@Param			pao-code	path		string			true	"Pao-code"
//	@Param			from-date	query		string			true	"Fromdate"
//	@Param			to-date	query		string			true	"Todate"
//	@Param			status	query		string			true	"Status"
//
// @Param       skip    query       int     			false   		"Number of records to skip for pagination"
// @Param       limit   query       int     			false   		"Number of records to limit for pagination"
//
//	@Success		200		{object}	response.GetGetPfmsxmlResponse			"data retrieved successfully"
//	@Failure		400					{object}	apierrors.APIErrorResponse						"Validation error"
//	@Failure		401					{object}	apierrors.APIErrorResponse						"Unauthorized error"
//	@Failure		403					{object}	apierrors.APIErrorResponse						"Forbidden error"
//	@Failure		404					{object}	apierrors.APIErrorResponse						"Data not found error"
//	@Failure		409					{object}	apierrors.APIErrorResponse						"Data conflict error"
//	@Failure		500					{object}	apierrors.APIErrorResponse						"Internal server error"
//	@Router			/v1/pao-gen/pao/{pao-code}/pfms-submission-status [get]
func (uh *PaogenHandler) ListPfmsSubmissionStatusHandler(ctx *gin.Context) {

	var req PfmsSubmissionStatusListRequest
	if err := ctx.ShouldBindUri(&req); err != nil {
		apierrors.HandleBindingError(ctx, err)
		log.Error(ctx, ErrBindingPfmsXmlRequest, err.Error())
		return
	}
	if err1 := ctx.ShouldBindQuery(&req); err1 != nil {
		apierrors.HandleBindingError(ctx, err1)
		log.Error(ctx, ErrBindingPfmsXmlRequest, err1.Error())
		return
	}
	if err := validation.ValidateStruct(req); err != nil {
		apierrors.HandleValidationError(ctx, err)
		log.Error(ctx, "Validation failed for PfmsXmlRequest: %s", err.Error())
		return
	}
	if req.Limit == 0 {
		req.Limit = math.MaxInt32
	}
	request := domain.PfmsSubmissionStatusListRequest{
		PaoCode:  req.PaoCode,
		FromDate: req.FromDate,
		ToDate:   req.ToDate,
		Status:   req.Status,
	}
	u, err := uh.svc.GetPfmsSubmissionStatusListRepo(ctx, &request, req.MetaDataRequest)
	if err != nil {
		apierrors.HandleDBError(ctx, err)
		log.Error(ctx, "Get PFMS XML Repo call failed: %s", err.Error())
		return
	}

	rsp := response.NewGetPfmsxmlResponse(u)

	metadata := port.NewMetaDataResponse(req.Skip, req.Limit, len(rsp))

	apiRsp := response.GetGetPfmsxmlResponse{
		StatusCodeAndMessage: port.FetchSucess,
		MetaDataResponse:     metadata,
		Data:                 rsp,
	}
	log.Debug(ctx, "ListPfmsXmlHandler response", apiRsp)
	handleSuccess(ctx, apiRsp)

}

// ListPfmsTESubmissionStatusHandler godoc
//
//	@Summary		Get the xml status for pfms te pending
//	@Description	Get the xml status for pfms te pending
//	@Tags			PAO GENERAL
//	@Accept			json
//	@Produce		json
//	@Param			pao-code	path		string			true	"Pao-code"
//	@Param			from-date	query		string			true	"Fromdate"
//	@Param			to-date	query		string			true	"Todate"
//	@Param			status	query		string			true	"Status"
//
// @Param       skip    query       int     			false   		"Number of records to skip for pagination"
// @Param       limit   query       int     			false   		"Number of records to limit for pagination"
//
//	@Success		200		{object}	response.GetGetPfmsxmlteResponse			"data retrieved successfully"
//	@Failure		400					{object}	apierrors.APIErrorResponse						"Validation error"
//	@Failure		401					{object}	apierrors.APIErrorResponse						"Unauthorized error"
//	@Failure		403					{object}	apierrors.APIErrorResponse						"Forbidden error"
//	@Failure		404					{object}	apierrors.APIErrorResponse						"Data not found error"
//	@Failure		409					{object}	apierrors.APIErrorResponse						"Data conflict error"
//	@Failure		500					{object}	apierrors.APIErrorResponse						"Internal server error"
//	@Router			/v1/pao-gen/pao/{pao-code}/te-pfms-submission-status [get]
func (uh *PaogenHandler) ListPfmsTESubmissionStatusHandler(ctx *gin.Context) {

	var req PfmsSubmissionStatusListRequest
	if err := ctx.ShouldBindUri(&req); err != nil {
		apierrors.HandleBindingError(ctx, err)
		log.Error(ctx, ErrBindingPfmsXmlRequest, err.Error())
		return
	}
	if err1 := ctx.ShouldBindQuery(&req); err1 != nil {
		apierrors.HandleBindingError(ctx, err1)
		log.Error(ctx, ErrBindingPfmsXmlRequest, err1.Error())
		return
	}
	if err := validation.ValidateStruct(req); err != nil {
		apierrors.HandleValidationError(ctx, err)
		log.Error(ctx, "Validation failed for PfmsXmlRequest: %s", err.Error())
		return
	}
	if req.Limit == 0 {
		req.Limit = math.MaxInt32
	}
	request := domain.PfmsSubmissionStatusListRequest{
		PaoCode:  req.PaoCode,
		FromDate: req.FromDate,
		ToDate:   req.ToDate,
		Status:   req.Status,
	}
	u, err := uh.svc.GetPfmsxmlteRepo(ctx, &request, req.MetaDataRequest)
	if err != nil {
		apierrors.HandleDBError(ctx, err)
		log.Error(ctx, "Get PFMS XML TE Repo call failed: %s", err.Error())
		return
	}

	rsp := response.NewGetPfmsxmlteResponse(u)

	metadata := port.NewMetaDataResponse(req.Skip, req.Limit, len(rsp))

	apiRsp := response.GetGetPfmsxmlteResponse{
		StatusCodeAndMessage: port.FetchSucess,
		MetaDataResponse:     metadata,
		Data:                 rsp,
	}
	log.Debug(ctx, "ListPfmsXmlTeHandler response", apiRsp)
	handleSuccess(ctx, apiRsp)

}

type TotalsByDDO struct {
	DdoOfficeID  string
	TotalReceipt float64
	TotalPayment float64
}

// CalculateTotalsByDDO computes Total Receipt and Total Payment grouped by DdoOfficeID
func CalculateTotalsByDDO(ctx *gin.Context, entries []domain.TransferEntryAccountingDetail) ([]TotalsByDDO, error) {
	// Map to store totals by DdoOfficeID
	totalsMap := make(map[string]TotalsByDDO)

	for _, entry := range entries {
		if !entry.DdoOfficeID.Valid || !entry.Amount.Valid || !entry.Sign.Valid || !entry.ReceiptPayment.Valid {
			continue // Skip invalid entries
		}

		ddoID := entry.DdoOfficeID.String
		amount := entry.Amount.Float64
		if entry.Sign.String == "-" {
			amount = -amount
		}

		// Get or initialize totals for this DdoOfficeID
		totals, exists := totalsMap[ddoID]
		if !exists {
			totals = TotalsByDDO{DdoOfficeID: ddoID}
		}

		// Accumulate based on ReceiptPayment type
		if entry.ReceiptPayment.String == "T" {
			totals.TotalReceipt += amount
		} else if entry.ReceiptPayment.String == "F" {
			totals.TotalPayment += amount
		}

		totalsMap[ddoID] = totals
	}

	// Convert map to slice for return
	result := make([]TotalsByDDO, 0, len(totalsMap))
	for _, totals := range totalsMap {
		result = append(result, totals)
	}

	return result, nil
}

func ProcessAmountAndSign(ctx *gin.Context, entries []domain.TransferEntryAccountingDetail) []domain.TransferEntryAccountingDetail {
	for i, entry := range entries {
		// Check if amount is valid and negative
		if entry.Amount.Valid && entry.Amount.Float64 < 0 {
			// Convert negative amount to positive
			entries[i].Amount.Float64 = -entry.Amount.Float64

			// Reverse sign if it's valid
			if entry.Sign.Valid {
				if entry.Sign.String == "+" {
					entries[i].Sign.String = "-"
				} else if entry.Sign.String == "-" {
					entries[i].Sign.String = "+"
				}
			}
		}
	}
	return entries
}

type CbData struct {
	DdoCode string `db:"ddo_code" json:"ddo_code" binding:"required,len=6" validate:"required,validateDdocode"`
	CbDate  string `db:"cb_date" json:"cb_date" validate:"required,date_yyyy_mm_dd"`
	PaoCode string `db:"pao_code" json:"pao_code" validate:"required,validatePaocode"`
	FinYear string `db:"fin_year" json:"fin_year" validate:"required,year"`
}

// Pfms-Submission godoc
//
//	@Summary		Post Pfms verified data into pfms server
//	@Description	Post Pfms verified data into pfms server
//	@Tags			PAO GENERAL
//	@Accept			json
//	@Produce		json
//	@Param			body	body		[]CbData	true	"Get Pfms request"
//	@Success		201		{object}	response.GetPfmsSubmissionPendingResponse			"record inserted successfully"
//	@Failure		400					{object}	apierrors.APIErrorResponse						"Validation error"
//	@Failure		401					{object}	apierrors.APIErrorResponse						"Unauthorized error"
//	@Failure		403					{object}	apierrors.APIErrorResponse						"Forbidden error"
//	@Failure		404					{object}	apierrors.APIErrorResponse						"Data not found error"
//	@Failure		409					{object}	apierrors.APIErrorResponse						"Data conflict error"
//	@Failure		500					{object}	apierrors.APIErrorResponse						"Internal server error"
//	@Router			/v1/pao-gen/pfms-submission [post]
func (uh *PaogenHandler) FetchPfmsHandler(ctx *gin.Context) {
	var cbds []CbData
	if err := ctx.ShouldBindJSON(&cbds); err != nil {
		apierrors.HandleBindingError(ctx, err)
		log.Error(ctx, "Binding failed for CbData: %s", err.Error())
		return
	}
	for _, r := range cbds {
		if err := validation.ValidateStruct(r); err != nil {
			apierrors.HandleValidationError(ctx, err)
			log.Error(ctx, "Validation failed for CbData: %s", err.Error())
			return
		}
	}

	var requests []domain.CbData
	var PfmsPayload domain.Payload
	var Paocode string
	var Tedate string
	var finYear string

	for _, request := range cbds {
		Paocode = request.PaoCode
		Tedate = request.CbDate

		cbDate, err := time.Parse("2006-01-02", request.CbDate)
		if err != nil {
			log.Error(ctx, "Invalid CbDate format for DdoCode: %s", err.Error())
			return
		}
		acquired, _, err := uh.svc.TryStartSubmission(ctx, request.DdoCode, cbDate /* date only */)
		if err != nil {
			apierrors.HandleDBError(ctx, err)
			return
		}
		if !acquired {
			appError := apierrors.NewAppError(
				"PFMS submission in progress for this DDO",
				"422",
				errors.New("PFMS submission under progress"),
			)
			apiErrorResponse := apierrors.NewAPIErrorResponse(
				http.StatusUnprocessableEntity,
				"PFMS submission in progress for this DDO",
				appError,
			)
			ctx.JSON(apiErrorResponse.StatusCode, apiErrorResponse)
			return
		}

		// Determine ending year of financial year

		year := cbDate.Year()
		month := cbDate.Month()
		if month >= time.April {
			finYear = fmt.Sprintf("%d", year+1)
		} else {
			finYear = fmt.Sprintf("%d", year)
		}

		requests = append(requests, domain.CbData{

			OfficeId: request.DdoCode,
			CbDate:   request.CbDate,
			PaoCode:  request.PaoCode,
			FinYear:  finYear,
		})
	}

	closingBalances, err := uh.svc.GetClosingBalanceRepo(ctx, requests)
	if err != nil {
		apierrors.HandleDBError(ctx, err)
		log.Error(ctx, "GetClosingBalanceRepo failed: %s", err.Error())
		return
	}

	// Create a map of closing balances for quick lookup
	closingBalMap := make(map[string]float64)
	for _, cb := range closingBalances {
		if cb.ClosingBal.Valid {
			closingBalMap[cb.OfficeId] = float64(cb.ClosingBal.Int64)
		}
	}

	pfmsjs, err := uh.svc.GetPfmsJsonRepo(ctx, requests)
	if err != nil {
		apierrors.HandleDBError(ctx, err)
		log.Error(ctx, "Get PFMS Repo call failed: %s", err.Error())
		return
	}
	if len(pfmsjs) == 0 {
		err := fmt.Errorf("No effective Debit or Credit to any HOA: This happens when both OB and CB are zero for the DDO or no transactions exist for the DDO in the given period")
		apierrors.HandleDBError(ctx, err)
		log.Error(ctx, "Get PFMS Repo returned empty result: %s", err.Error())
		return
	}
	pfmsjson := ProcessAmountAndSign(ctx, pfmsjs)

	totals, err := CalculateTotalsByDDO(ctx, pfmsjson)
	if err != nil {
		apierrors.HandleError(ctx, err)
		log.Error(ctx, "CalculateTotalsByDDO failed: %s", err.Error())
		return
	}

	// Process each DDO total and append new entries as needed
	for _, total := range totals {
		closingBal, exists := closingBalMap[total.DdoOfficeID]
		if !exists {
			log.Error(ctx, "No closing balance found for DdoOfficeID: %s", total.DdoOfficeID)
			continue
		}

		// Calculate equation: TotalReceipt + TotalPayment - 2 * ClosingBal
		equationResult := total.TotalReceipt - total.TotalPayment - (2 * closingBal)

		if equationResult >= 100 {
			err := fmt.Errorf("Total Receipt and Total Payment difference exceeds allowable limit")
			apierrors.HandleDBError(ctx, err)
			log.Error(ctx, "Roundoff adjustment too large: %s", err.Error())
			return
		}

		var newEntry domain.TransferEntryAccountingDetail

		if equationResult < 0 {
			// Equation > 0: Add Receipt entry
			newEntry = domain.TransferEntryAccountingDetail{
				DdoOfficeID:    null.String{String: total.DdoOfficeID, Valid: true},
				FunctionalHead: null.String{String: "8671001020100", Valid: true},
				ObjectHead:     null.String{String: "00", Valid: true},
				GrantNo:        null.String{String: "800", Valid: true},
				Category:       null.String{String: "6", Valid: true},
				Remarks:        null.String{String: "Cash received", Valid: true},
				ReceiptPayment: null.String{String: "T", Valid: true},
				Sign:           null.String{String: "+", Valid: true},
				Amount:         null.Float64{Float64: -equationResult, Valid: true},
			}
		} else if equationResult > 0 {
			// Equation < 0: Add Payment entry
			newEntry = domain.TransferEntryAccountingDetail{
				DdoOfficeID:    null.String{String: total.DdoOfficeID, Valid: true},
				FunctionalHead: null.String{String: "8671001020100", Valid: true},
				ObjectHead:     null.String{String: "00", Valid: true},
				GrantNo:        null.String{String: "800", Valid: true},
				Category:       null.String{String: "7", Valid: true},
				Remarks:        null.String{String: "Cash sent", Valid: true},
				ReceiptPayment: null.String{String: "F", Valid: true},
				Sign:           null.String{String: "+", Valid: true},
				Amount:         null.Float64{Float64: equationResult, Valid: true}, // Use absolute value
			}
		} else {
			continue // Skip if equationResult == 0
		}
		pfmsjson = append(pfmsjson, newEntry)
	}
	finYearInt, err := strconv.Atoi(finYear)
	if err != nil {
		log.Error(ctx, "Failed to convert FinYear to integer: %s", err.Error())
		return
	}
	transferEntry := domain.TransferEntryDetail{
		UniqueIdentifier: GenerateRandomNumber(Paocode, finYear),
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

	// ctx.JSON(http.StatusOK, PfmsPayload)
	// return

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

	startTime := time.Now()
	response1, err := uh.CallAPI(url, method, header, params)
	latency := time.Since(startTime).Milliseconds()
	log.Debug(ctx, "GetAuthCode API Call Latency: %d ms", latency)
	if err != nil {
		log.Error(ctx, "PFMS AuthCode API Call Failed: %v | Username: %s | RequestSource: %s | Password: %s | URL: %s | Params: %v | Raw Response: %s | Latency: %d ms",
			err, username, requestsource, password, url, params, response1, latency)
		apierrors.HandleError(ctx, err)
		return
	}

	log.Debug(ctx, "Raw API1 Response: %v", response1)

	success := response1["IsSuccess"].(string)
	if success == "0" {
		ctx.JSON(http.StatusBadRequest, gin.H{"error": response1["ErrorMessage"]})
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

	startTime = time.Now()
	response2, err1 := uh.CallAPI(url1, method1, header1, params1)
	latency = time.Since(startTime).Milliseconds()
	log.Debug(ctx, "LogIn API Call Latency: %d ms", latency)
	if err1 != nil {
		log.Error(ctx, "PFMS AccessCode API Call Failed: %v | Username: %s | RequestSource: %s | Password: %s | URL: %s | Params: %v | Raw Response: %s | Latency: %d ms",
			err1, username, requestsource, password, url1, params1, response2, latency)
		apierrors.HandleError(ctx, err1)
		return
	}

	log.Debug(ctx, "Raw API2 Response: %v", response2)
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

	startTime = time.Now()
	response3, err2 := uh.CallAPI(url2, method2, header2, params2)
	latency = time.Since(startTime).Milliseconds()
	log.Debug(ctx, "ReceiveTransferEntryData API Call Latency: %d ms", latency)

	if err2 != nil {
		log.Error(ctx, "PFMS Submit PFMS DATA API Call Failed: %v | Username: %s | RequestSource: %s | Password: %s | URL: %s | Params: %v | Raw Response: %s | Latency: %d ms",
			err2, username, requestsource, password, url2, params2, response3, latency)
		apierrors.HandleError(ctx, err2)
		return
	}
	log.Debug(ctx, "Raw API2 Response: %v", response3)

	success, ok := response3["isSuccess"].(string)
	if !ok || success == "0" {
		errorMessage := "Unknown error"
		if em, exists := response3["errorMessage"]; exists {
			errorMessage = fmt.Sprintf("%v", em)
		}

		log.Error(ctx, "PFMS Submit PFMS DATA API responded with failure. Request: %+v, Response: %+v", params2, response3)
		ctx.JSON(http.StatusBadRequest, gin.H{"error": errorMessage})
		return
	}
	err4 := uh.svc.GetPfmsUpdateStatusRepo(ctx, requests, transferEntry.UniqueIdentifier)
	if err4 != nil {
		apierrors.HandleDBError(ctx, err4)
		log.Error(ctx, "Get PFMS Repo call failed: %s", err4.Error())
		return
	}
	errInsert := uh.svc.InsertPfmsSubmission(
		ctx,
		transferEntry.UniqueIdentifier,
		"cb",                         // Since we are submitting cashbook
		requests,                     // Store API requests in cb_request
		domain.TransferEntryDetail{}, // te_request is empty (null in JSONB)
		Tedate,                       // Business date
		time.Now(),                   // Submission date
		PfmsPayload,                  // Payload sent to PFMS API
		"Pending",                    // submissionStatus set to "Pending"
		"",                           // errorDescription is null
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

func (uh *PaogenHandler) FetchPfmsHandlerdebugging(ctx *gin.Context) {

	// ═══════════════════════════════════════════
	// AUDIT TRAIL — visible in Postman response
	// ═══════════════════════════════════════════
	type StepLog struct {
		Step        string      `json:"step"`
		URL         string      `json:"url"`
		RequestSent interface{} `json:"request_sent"`
		ResponseGot interface{} `json:"response_got"`
		LatencyMs   int64       `json:"latency_ms"`
		Error       string      `json:"error,omitempty"`
	}
	var auditTrail []StepLog

	var cbds []CbData
	if err := ctx.ShouldBindJSON(&cbds); err != nil {
		apierrors.HandleBindingError(ctx, err)
		log.Error(ctx, "Binding failed for CbData: %s", err.Error())
		return
	}
	for _, r := range cbds {
		if err := validation.ValidateStruct(r); err != nil {
			apierrors.HandleValidationError(ctx, err)
			log.Error(ctx, "Validation failed for CbData: %s", err.Error())
			return
		}
	}

	var requests []domain.CbData
	var PfmsPayload domain.Payload
	var Paocode string
	var Tedate string
	var finYear string

	for _, request := range cbds {
		Paocode = request.PaoCode
		Tedate = request.CbDate

		cbDate, err := time.Parse("2006-01-02", request.CbDate)
		if err != nil {
			log.Error(ctx, "Invalid CbDate format for DdoCode: %s", err.Error())
			return
		}
		acquired, _, err := uh.svc.TryStartSubmission(ctx, request.DdoCode, cbDate)
		if err != nil {
			apierrors.HandleDBError(ctx, err)
			return
		}
		if !acquired {
			appError := apierrors.NewAppError(
				"PFMS submission in progress for this DDO",
				"422",
				errors.New("PFMS submission under progress"),
			)
			apiErrorResponse := apierrors.NewAPIErrorResponse(
				http.StatusUnprocessableEntity,
				"PFMS submission in progress for this DDO",
				appError,
			)
			ctx.JSON(apiErrorResponse.StatusCode, apiErrorResponse)
			return
		}

		year := cbDate.Year()
		month := cbDate.Month()
		if month >= time.April {
			finYear = fmt.Sprintf("%d", year+1)
		} else {
			finYear = fmt.Sprintf("%d", year)
		}

		requests = append(requests, domain.CbData{
			OfficeId: request.DdoCode,
			CbDate:   request.CbDate,
			PaoCode:  request.PaoCode,
			FinYear:  finYear,
		})
	}

	closingBalances, err := uh.svc.GetClosingBalanceRepo(ctx, requests)
	if err != nil {
		apierrors.HandleDBError(ctx, err)
		log.Error(ctx, "GetClosingBalanceRepo failed: %s", err.Error())
		return
	}

	closingBalMap := make(map[string]float64)
	for _, cb := range closingBalances {
		if cb.ClosingBal.Valid {
			closingBalMap[cb.OfficeId] = float64(cb.ClosingBal.Int64)
		}
	}

	pfmsjs, err := uh.svc.GetPfmsJsonRepo(ctx, requests)
	if err != nil {
		apierrors.HandleDBError(ctx, err)
		log.Error(ctx, "Get PFMS Repo call failed: %s", err.Error())
		return
	}
	if len(pfmsjs) == 0 {
		err := fmt.Errorf("No effective Debit or Credit to any HOA: This happens when both OB and CB are zero for the DDO or no transactions exist for the DDO in the given period")
		apierrors.HandleDBError(ctx, err)
		log.Error(ctx, "Get PFMS Repo returned empty result: %s", err.Error())
		return
	}
	pfmsjson := ProcessAmountAndSign(ctx, pfmsjs)

	totals, err := CalculateTotalsByDDO(ctx, pfmsjson)
	if err != nil {
		apierrors.HandleError(ctx, err)
		log.Error(ctx, "CalculateTotalsByDDO failed: %s", err.Error())
		return
	}

	for _, total := range totals {
		closingBal, exists := closingBalMap[total.DdoOfficeID]
		if !exists {
			log.Error(ctx, "No closing balance found for DdoOfficeID: %s", total.DdoOfficeID)
			continue
		}

		equationResult := total.TotalReceipt - total.TotalPayment - (2 * closingBal)

		if equationResult >= 100 {
			err := fmt.Errorf("Total Receipt and Total Payment difference exceeds allowable limit")
			apierrors.HandleDBError(ctx, err)
			log.Error(ctx, "Roundoff adjustment too large: %s", err.Error())
			return
		}

		var newEntry domain.TransferEntryAccountingDetail
		if equationResult < 0 {
			newEntry = domain.TransferEntryAccountingDetail{
				DdoOfficeID:    null.String{String: total.DdoOfficeID, Valid: true},
				FunctionalHead: null.String{String: "8671001020100", Valid: true},
				ObjectHead:     null.String{String: "00", Valid: true},
				GrantNo:        null.String{String: "800", Valid: true},
				Category:       null.String{String: "6", Valid: true},
				Remarks:        null.String{String: "Cash received", Valid: true},
				ReceiptPayment: null.String{String: "T", Valid: true},
				Sign:           null.String{String: "+", Valid: true},
				Amount:         null.Float64{Float64: -equationResult, Valid: true},
			}
		} else if equationResult > 0 {
			newEntry = domain.TransferEntryAccountingDetail{
				DdoOfficeID:    null.String{String: total.DdoOfficeID, Valid: true},
				FunctionalHead: null.String{String: "8671001020100", Valid: true},
				ObjectHead:     null.String{String: "00", Valid: true},
				GrantNo:        null.String{String: "800", Valid: true},
				Category:       null.String{String: "7", Valid: true},
				Remarks:        null.String{String: "Cash sent", Valid: true},
				ReceiptPayment: null.String{String: "F", Valid: true},
				Sign:           null.String{String: "+", Valid: true},
				Amount:         null.Float64{Float64: equationResult, Valid: true},
			}
		} else {
			continue
		}
		pfmsjson = append(pfmsjson, newEntry)
	}

	finYearInt, err := strconv.Atoi(finYear)
	if err != nil {
		log.Error(ctx, "Failed to convert FinYear to integer: %s", err.Error())
		return
	}

	transferEntry := domain.TransferEntryDetail{
		UniqueIdentifier: GenerateRandomNumber(Paocode, finYear),
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
	PfmsPayload.RequestPayload.TransferEntryDetails = []domain.TransferEntryDetail{transferEntry}

	username := uh.cfg.GetString("pfms.username")
	requestsource := uh.cfg.GetString("pfms.requestsource")
	password := uh.cfg.GetString("pfms.password")
	baseurl := uh.cfg.GetString("pfms.baseurl")

	var authcode string
	var accesstoken string

	// ═══════════════════════════════════════════
	// STEP 1 — GetAuthCode
	// ═══════════════════════════════════════════
	url := baseurl + "/GetAuthCode"
	method := "POST"
	header := map[string]string{
		"Content-Type": "application/json",
	}
	params := map[string]interface{}{
		"UserName":      username,
		"RequestSource": requestsource,
	}

	log.Debug(ctx, "PFMS STEP1 REQUEST → URL: %s | Body: %+v", url, params)
	startTime := time.Now()
	response1, err := uh.CallAPI(url, method, header, params)
	latency := time.Since(startTime).Milliseconds()
	log.Debug(ctx, "PFMS STEP1 RESPONSE ← Latency: %d ms | Body: %+v", latency, response1)

	step1Log := StepLog{
		Step:        "Step1 - GetAuthCode",
		URL:         url,
		RequestSent: params,
		ResponseGot: response1,
		LatencyMs:   latency,
	}
	if err != nil {
		step1Log.Error = err.Error()
		auditTrail = append(auditTrail, step1Log)
		log.Error(ctx, "PFMS STEP1 FAILED: %v | Latency: %d ms", err, latency)
		ctx.JSON(http.StatusInternalServerError, gin.H{
			"failed_at":   "Step1 - GetAuthCode",
			"error":       err.Error(),
			"audit_trail": auditTrail,
		})
		return
	}
	auditTrail = append(auditTrail, step1Log)

	// Safe nil check
	isSuccess1, ok1 := response1["IsSuccess"].(string)
	if !ok1 {
		ctx.JSON(http.StatusInternalServerError, gin.H{
			"failed_at":   "Step1 - GetAuthCode",
			"error":       "IsSuccess field missing or null in GetAuthCode response",
			"audit_trail": auditTrail,
		})
		return
	}
	if isSuccess1 == "0" {
		ctx.JSON(http.StatusBadRequest, gin.H{
			"failed_at":   "Step1 - GetAuthCode",
			"error":       response1["ErrorMessage"],
			"audit_trail": auditTrail,
		})
		return
	}

	authcode, ok2 := response1["AuthCode"].(string)
	if !ok2 || authcode == "" {
		ctx.JSON(http.StatusInternalServerError, gin.H{
			"failed_at":   "Step1 - GetAuthCode",
			"error":       "AuthCode missing or null in GetAuthCode response",
			"audit_trail": auditTrail,
		})
		return
	}

	// ═══════════════════════════════════════════
	// STEP 2 — LogIn
	// ═══════════════════════════════════════════
	url1 := baseurl + "/LogIn"
	method1 := "POST"
	header1 := map[string]string{
		"Content-Type": "application/json",
	}
	params1 := map[string]interface{}{
		"userName": username,
		"password": password + authcode,
	}

	log.Debug(ctx, "PFMS STEP2 REQUEST → URL: %s | Body: %+v", url1, params1)
	startTime = time.Now()
	response2, err1 := uh.CallAPI(url1, method1, header1, params1)
	latency = time.Since(startTime).Milliseconds()
	log.Debug(ctx, "PFMS STEP2 RESPONSE ← Latency: %d ms | Body: %+v", latency, response2)

	step2Log := StepLog{
		Step:        "Step2 - LogIn",
		URL:         url1,
		RequestSent: params1,
		ResponseGot: response2,
		LatencyMs:   latency,
	}
	if err1 != nil {
		step2Log.Error = err1.Error()
		auditTrail = append(auditTrail, step2Log)
		log.Error(ctx, "PFMS STEP2 FAILED: %v | Latency: %d ms", err1, latency)
		ctx.JSON(http.StatusInternalServerError, gin.H{
			"failed_at":   "Step2 - LogIn",
			"error":       err1.Error(),
			"audit_trail": auditTrail,
		})
		return
	}
	auditTrail = append(auditTrail, step2Log)

	// Safe nil check
	isSuccess2, ok3 := response2["isSuccess"].(string)
	if !ok3 {
		ctx.JSON(http.StatusInternalServerError, gin.H{
			"failed_at":   "Step2 - LogIn",
			"error":       "isSuccess field missing or null in LogIn response",
			"audit_trail": auditTrail,
		})
		return
	}
	if isSuccess2 == "0" {
		ctx.JSON(http.StatusBadRequest, gin.H{
			"failed_at":   "Step2 - LogIn",
			"error":       response2["errorMessage"],
			"audit_trail": auditTrail,
		})
		return
	}

	accesstoken, ok4 := response2["accessToken"].(string)
	if !ok4 || accesstoken == "" {
		ctx.JSON(http.StatusInternalServerError, gin.H{
			"failed_at":   "Step2 - LogIn",
			"error":       "accessToken missing or null in LogIn response",
			"audit_trail": auditTrail,
		})
		return
	}

	// ═══════════════════════════════════════════
	// STEP 3 — ReceiveTransferEntryData
	// ═══════════════════════════════════════════
	url2 := baseurl + "/Budget/ReceiveTransferEntryData"
	method2 := "POST"
	header2 := map[string]string{
		"Content-Type":  "application/json",
		"Authorization": "Bearer " + accesstoken,
	}
	params2 := PfmsPayload

	payloadBytes, _ := json.Marshal(params2)
	log.Debug(ctx, "PFMS STEP3 REQUEST → URL: %s | Body: %s", url2, string(payloadBytes))
	startTime = time.Now()
	response3, err2 := uh.CallAPI(url2, method2, header2, params2)
	latency = time.Since(startTime).Milliseconds()
	log.Debug(ctx, "PFMS STEP3 RESPONSE ← Latency: %d ms | Body: %+v", latency, response3)

	step3Log := StepLog{
		Step:        "Step3 - ReceiveTransferEntryData",
		URL:         url2,
		RequestSent: params2,
		ResponseGot: response3,
		LatencyMs:   latency,
	}
	if err2 != nil {
		step3Log.Error = err2.Error()
		auditTrail = append(auditTrail, step3Log)
		log.Error(ctx, "PFMS STEP3 FAILED: %v | Latency: %d ms", err2, latency)
		ctx.JSON(http.StatusInternalServerError, gin.H{
			"failed_at":   "Step3 - ReceiveTransferEntryData",
			"error":       err2.Error(),
			"audit_trail": auditTrail,
		})
		return
	}
	auditTrail = append(auditTrail, step3Log)

	// Safe nil check
	success, ok := response3["isSuccess"].(string)
	if !ok || success == "0" {
		errorMessage := "Unknown error"
		if em, exists := response3["errorMessage"]; exists {
			errorMessage = fmt.Sprintf("%v", em)
		}
		log.Error(ctx, "PFMS STEP3 RESPONSE FAILURE | Request: %+v | Response: %+v", params2, response3)
		ctx.JSON(http.StatusBadRequest, gin.H{
			"failed_at":   "Step3 - ReceiveTransferEntryData",
			"error":       errorMessage,
			"audit_trail": auditTrail,
		})
		return
	}

	// ═══════════════════════════════════════════
	// SUCCESS — DB updates
	// ═══════════════════════════════════════════
	err4 := uh.svc.GetPfmsUpdateStatusRepo(ctx, requests, transferEntry.UniqueIdentifier)
	if err4 != nil {
		apierrors.HandleDBError(ctx, err4)
		log.Error(ctx, "Get PFMS Repo call failed: %s", err4.Error())
		return
	}

	errInsert := uh.svc.InsertPfmsSubmission(
		ctx,
		transferEntry.UniqueIdentifier,
		"cb",
		requests,
		domain.TransferEntryDetail{},
		Tedate,
		time.Now(),
		PfmsPayload,
		"Pending",
		"",
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

	// ═══════════════════════════════════════════
	// FINAL RESPONSE — always includes audit trail
	// ═══════════════════════════════════════════
	ctx.JSON(http.StatusOK, gin.H{
		"status_code": port.InsertSuccess,
		"success":     true,
		"data":        rsp,
		"audit_trail": auditTrail,
	})
}

type PraoAccountSubmissionRequest struct {
	PaoCode string `json:"pao_code" binding:"required,len=6" validate:"required,validatePaocode"`
	Period  string `json:"period" validate:"required,validatePeriod"`
}

// CreatePraoAccountHandler godoc
//
//	@Summary		Post monthly account to Prao
//	@Description	Post monthly account to Prao
//	@Tags			PAO GENERAL
//	@Accept			json
//	@Produce		json
//	@Param			body	body		PraoAccountSubmissionRequest true	"Post account to Prao request"
//	@Success		201		{object}	response.PostPraoAccountResponse			"resource created successfully"
//	@Failure		400					{object}	apierrors.APIErrorResponse						"Validation error"
//	@Failure		401					{object}	apierrors.APIErrorResponse						"Unauthorized error"
//	@Failure		403					{object}	apierrors.APIErrorResponse						"Forbidden error"
//	@Failure		404					{object}	apierrors.APIErrorResponse						"Data not found error"
//	@Failure		409					{object}	apierrors.APIErrorResponse						"Data conflict error"
//	@Failure		500					{object}	apierrors.APIErrorResponse						"Internal server error"
//	@Router			/v1/pao-gen/prao/account-submission [post]
func (uh *PaogenHandler) CreatePraoAccountHandler(ctx *gin.Context) {

	var req PraoAccountSubmissionRequest
	if err := ctx.ShouldBindJSON(&req); err != nil {
		apierrors.HandleBindingError(ctx, err)
		log.Error(ctx, "Binding failed for PraoAccountSubmissionRequest: %s", err.Error())
		return
	}
	if err := validation.ValidateStruct(req); err != nil {
		apierrors.HandleValidationError(ctx, err)
		log.Error(ctx, "Validation failed for PraoAccountSubmissionRequest: %s", err.Error())
		return
	}
	request := domain.PraoAccountSubmissionRequest{
		PaoCode: req.PaoCode,
		Period:  req.Period,
	}

	isVerified, err := uh.svc.CheckCashAccountVerificationRepo(ctx, request)
	if err != nil {
		apierrors.HandleDBError(ctx, err)
		log.Error(ctx, "Check cash account verification failed: %s", err.Error())
		return
	}
	if !isVerified {
		appError := apierrors.NewAppError(
			"not all cash accounts are verified for the given PAO code and period",
			"422",
			errors.New("cash account verification incomplete"),
		)
		apiErrorResponse := apierrors.NewAPIErrorResponse(
			http.StatusUnprocessableEntity,
			"Unprocessable Entity",
			appError,
		)
		ctx.JSON(apiErrorResponse.StatusCode, apiErrorResponse)
		log.Debug(ctx, "Cash account verification incomplete for pao_code: %s, period: %s", req.PaoCode, req.Period)
		return
	}

	u, err := uh.svc.GetPostPraoAccountRepo(ctx, request)
	if err != nil {
		apierrors.HandleDBError(ctx, err)
		log.Error(ctx, "Get PRAO Account Repo call failed: %s", err.Error())
		return
	}
	if len(u) > 0 {
		err2 := uh.svc.PostPraoAccountRepo(ctx, u)
		if err2 != nil {
			log.Error(ctx, "Post PRAO Account Repo call failed: %s", err2.Error())
			if err2.(*pgconn.PgError).Code == "23505" {
				err1 := errors.New("prao account submission failed")
				// Create an AppError with a user-friendly message and code.
				appError := apierrors.NewAppError(
					"prao account already submitted", // User-friendly error message
					"409",                            // Error code representing the error type
					err1,                             // Original error for debugging purposes
				)
				apiErrorResponse := apierrors.NewAPIErrorResponse(
					http.StatusConflict, // HTTP status code
					"Conflict",          // Message to return to the client
					appError,            // Encapsulated application error
				)
				ctx.JSON(apiErrorResponse.StatusCode, apiErrorResponse)
				return
			} else {
				apierrors.HandleDBError(ctx, err2)
				return
			}
		}

		rsp := response.NewPostPraoAccountResponse(u)

		metadata := port.NewMetaDataResponse(0, 0, len(rsp))

		apiRsp := response.PostPraoAccountResponse{
			StatusCodeAndMessage: port.CreateSuccess,
			MetaDataResponse:     metadata,
			Data:                 rsp,
		}
		log.Debug(ctx, "CreatePraoAccountHandler response", apiRsp)
		handleCreateSuccess(ctx, apiRsp)
	} else {
		apiRsp := response.PostPraoAccountResponse{
			StatusCodeAndMessage: port.CreateSuccess,
		}
		log.Debug(ctx, "CreatePraoAccountHandler response", apiRsp)
		handleCreateSuccess(ctx, apiRsp)
	}

}

// similar struct exists above, but we need uri for pao-code, hence a new struct created.
type PraoAccountSubmissionRequest2 struct {
	PaoCode string `uri:"pao-code" binding:"required,len=6" validate:"required,validatePaocode"`
	Period  string `form:"period" binding:"omitempty,len=6" validate:"required,validatePeriod"`
	port.MetaDataRequest
}

const ErrBindingPraoAccountSubmissionRequest2 = "Binding failed for PraoAccountSubmissionRequest2: %s"

// FetchPraoAccountHandler godoc
//
//	@Summary		Get Prao Account submission
//	@Description	Get Prao Account submission
//	@Tags			PAO GENERAL
//	@Accept			json
//	@Produce		json
//	@Param			pao-code	path		string			true	"Pao_code"
//	@Param			period	query		string			true	"Period"
//	@Success		200		{object}	response.GetPraoAccountResponse			"data retrieved successfully"
//	@Failure		400					{object}	apierrors.APIErrorResponse						"Validation error"
//	@Failure		401					{object}	apierrors.APIErrorResponse						"Unauthorized error"
//	@Failure		403					{object}	apierrors.APIErrorResponse						"Forbidden error"
//	@Failure		404					{object}	apierrors.APIErrorResponse						"Data not found error"
//	@Failure		409					{object}	apierrors.APIErrorResponse						"Data conflict error"
//	@Failure		500					{object}	apierrors.APIErrorResponse						"Internal server error"
//	@Router			/v1/pao-gen/pao/{pao-code}/prao/accounts [get]
func (uh *PaogenHandler) FetchPraoAccountHandler(ctx *gin.Context) {

	var req PraoAccountSubmissionRequest2
	if err := ctx.ShouldBindUri(&req); err != nil {
		apierrors.HandleBindingError(ctx, err)
		log.Error(ctx, ErrBindingPraoAccountSubmissionRequest2, err.Error())
		return
	}
	if err1 := ctx.ShouldBindQuery(&req); err1 != nil {
		apierrors.HandleBindingError(ctx, err1)
		log.Error(ctx, ErrBindingPraoAccountSubmissionRequest2, err1.Error())
		return
	}
	if err := validation.ValidateStruct(req); err != nil {
		apierrors.HandleValidationError(ctx, err)
		log.Error(ctx, "Validation failed for PraoAccountSubmissionRequest2: %s", err.Error())
		return
	}
	if req.Limit == 0 {
		req.Limit = math.MaxInt32
	}

	request := domain.PraoAccountSubmissionRequest{
		PaoCode: req.PaoCode,
		Period:  req.Period,
	}

	u, err := uh.svc.GetPraoAccountRepo(ctx, request, req.MetaDataRequest)
	if err != nil {
		apierrors.HandleDBError(ctx, err)
		log.Error(ctx, "Get Prao Account Repo call failed: %s", err.Error())
		return
	}

	rsp := response.NewGetPraoAccountResponse(u)

	metadata := port.NewMetaDataResponse(req.Skip, req.Limit, len(rsp))

	apiRsp := response.GetPraoAccountResponse{
		StatusCodeAndMessage: port.FetchSucess,
		MetaDataResponse:     metadata,
		Data:                 rsp,
	}
	log.Debug(ctx, "FetchPraoAccountHandler response", apiRsp)
	handleSuccess(ctx, apiRsp)

}

// FetchPraoAccountSubStatusHandler godoc
//
//	@Summary		Get Prao Account submission status
//	@Description	Get Prao Account submission status
//	@Tags			PAO GENERAL
//	@Accept			json
//	@Produce		json
//	@Param			pao-code	path		string			true	"Pao-code"
//	@Param			period	query		string			true	"Period"
//	@Success		200		{object}	response.PraoAccountSubStatusResponse			"data retrieved successfully"
//	@Failure		400					{object}	apierrors.APIErrorResponse						"Validation error"
//	@Failure		401					{object}	apierrors.APIErrorResponse						"Unauthorized error"
//	@Failure		403					{object}	apierrors.APIErrorResponse						"Forbidden error"
//	@Failure		404					{object}	apierrors.APIErrorResponse						"Data not found error"
//	@Failure		409					{object}	apierrors.APIErrorResponse						"Data conflict error"
//	@Failure		500					{object}	apierrors.APIErrorResponse						"Internal server error"
//	@Router			/v1/pao-gen/pao/{pao-code}/prao/account-submission-status [get]
func (uh *PaogenHandler) FetchPraoAccountSubStatusHandler(ctx *gin.Context) {

	var req PraoAccountSubmissionRequest2
	if err := ctx.ShouldBindUri(&req); err != nil {
		apierrors.HandleBindingError(ctx, err)
		log.Error(ctx, ErrBindingPraoAccountSubmissionRequest2, err.Error())
		return
	}
	if err1 := ctx.ShouldBindQuery(&req); err1 != nil {
		apierrors.HandleBindingError(ctx, err1)
		log.Error(ctx, ErrBindingPraoAccountSubmissionRequest2, err1.Error())
		return
	}
	if err := validation.ValidateStruct(req); err != nil {
		apierrors.HandleValidationError(ctx, err)
		log.Error(ctx, "Validation failed for PraoAccountSubmissionRequest2: %s", err.Error())
		return
	}
	if req.Limit == 0 {
		req.Limit = math.MaxInt32
	}

	request := domain.PraoAccountSubmissionRequest{
		PaoCode: req.PaoCode,
		Period:  req.Period,
	}

	u, err := uh.svc.PraoAccountSubStatusRepo(ctx, request, req.MetaDataRequest)
	if err != nil {
		apierrors.HandleDBError(ctx, err)
		log.Error(ctx, "Prao Account Submisstion Status Repo call failed: %s", err.Error())
		return
	}

	rsp := response.NewPraoAccountSubStatusResponse(*u)

	metadata := port.NewMetaDataResponse(req.Skip, req.Limit, 1)

	apiRsp := response.PraoAccountSubStatusResponse{
		StatusCodeAndMessage: port.FetchSucess,
		MetaDataResponse:     metadata,
		Data:                 rsp,
	}
	log.Debug(ctx, "FetchPraoAccountSubStatusHandler response", apiRsp)
	handleSuccess(ctx, apiRsp)

}

type AccountSubmissionStatusListRequest struct {
	PraoOfficeId string `uri:"prao-office-id" binding:"required,len=8" validate:"required"`
	Period       string `form:"period" binding:"omitempty,len=6" validate:"required,validatePeriod"`
	port.MetaDataRequest
}

// ListPraoAccountSubStatusHandler godoc
//
//	@Summary		Get Prao Account submission status list
//	@Description	Get Prao Account submission status list
//	@Tags			PAO GENERAL
//	@Accept			json
//	@Produce		json
//	@Param			prao-office-id	path		string			true	"Prao-office-id"
//	@Param			period	query		string			true	"Period"
//
// @Param       skip    query       int     			false   		"Number of records to skip for pagination"
// @Param       limit   query       int     			false   		"Number of records to limit for pagination"
//
//	@Success		200		{object}	response.PraoAccountSubStatusListResponse			"list retrieved successfully"
//	@Failure		400					{object}	apierrors.APIErrorResponse						"Validation error"
//	@Failure		401					{object}	apierrors.APIErrorResponse						"Unauthorized error"
//	@Failure		403					{object}	apierrors.APIErrorResponse						"Forbidden error"
//	@Failure		404					{object}	apierrors.APIErrorResponse						"Data not found error"
//	@Failure		409					{object}	apierrors.APIErrorResponse						"Data conflict error"
//	@Failure		500					{object}	apierrors.APIErrorResponse						"Internal server error"
//	@Router			/v1/pao-gen/prao/{prao-office-id}/account-submission-list [get]
func (uh *PaogenHandler) ListPraoAccountSubStatusHandler(ctx *gin.Context) {

	var req AccountSubmissionStatusListRequest
	if err := ctx.ShouldBindUri(&req); err != nil {
		apierrors.HandleBindingError(ctx, err)
		log.Error(ctx, "Binding failed for AccountSubmissionStatusListRequest: %s", err.Error())
		return
	}
	if err1 := ctx.ShouldBindQuery(&req); err1 != nil {
		apierrors.HandleBindingError(ctx, err1)
		log.Error(ctx, "Binding failed for AccountSubmissionStatusListRequest: %s", err1.Error())
		return
	}
	if err := validation.ValidateStruct(req); err != nil {
		apierrors.HandleValidationError(ctx, err)
		log.Error(ctx, "Validation failed for AccountSubmissionStatusListRequest: %s", err.Error())
		return
	}
	if req.Limit == 0 {
		req.Limit = math.MaxInt32
	}

	request := domain.AccountsubmissionStatusListRequest{
		PraoOfficeId: req.PraoOfficeId,
		Period:       req.Period,
	}

	u, err := uh.svc.PraoAccountSubStatusListRepo(ctx, &request, req.MetaDataRequest)
	if err != nil {
		log.Error(ctx, err)
		apierrors.HandleDBError(ctx, err)
		log.Error(ctx, "PraoAcccountSubmissionStatusList Repo call failed: %s", err.Error())
		return
	}

	rsp := response.NewPraoAccountSubStatusListResponse(u)

	metadata := port.NewMetaDataResponse(req.Skip, req.Limit, len(rsp))

	apiRsp := response.PraoAccountSubStatusListResponse{
		StatusCodeAndMessage: port.ListSuccess,
		MetaDataResponse:     metadata,
		Data:                 rsp,
	}
	log.Debug(ctx, "ListPraoAccountSubStatusHandler response", apiRsp)
	handleSuccess(ctx, apiRsp)

}

type PfmsXmlSubmissionPendingRequest struct {
	FinYear string `uri:"fin-year" binding:"required,len=4" validate:"required"`
	port.MetaDataRequest
}

// ListPfmsXmlSubmissionPendingHandler godoc
//
//	@Summary		Get the list of xml files to be uploaded to Pfms
//	@Description	Get the list of xml files to be uploaded to Pfms in the given financial year
//	@Tags			PAO GENERAL
//	@Accept			json
//	@Produce		json
//	@Param			fin-year	path		string			true	"Fin Year"
//
// @Param       skip    query       int     			false   		"Number of records to skip for pagination"
// @Param       limit   query       int     			false   		"Number of records to limit for pagination"
//
//	@Success		200		{object}	response.GetPfmsSubmissionPendingResponse			"data retrieved successfully"
//	@Failure		400					{object}	apierrors.APIErrorResponse						"Validation error"
//	@Failure		401					{object}	apierrors.APIErrorResponse						"Unauthorized error"
//	@Failure		403					{object}	apierrors.APIErrorResponse						"Forbidden error"
//	@Failure		404					{object}	apierrors.APIErrorResponse						"Data not found error"
//	@Failure		409					{object}	apierrors.APIErrorResponse						"Data conflict error"
//	@Failure		500					{object}	apierrors.APIErrorResponse						"Internal server error"
//	@Router			/v1/pao-gen/pfms-submission-pending-list/:fin-year [get]
func (uh *PaogenHandler) ListPfmsXmlSubmissionPendingHandler(ctx *gin.Context) {

	var req PfmsXmlSubmissionPendingRequest
	if err := ctx.ShouldBindUri(&req); err != nil {
		apierrors.HandleBindingError(ctx, err)
		log.Error(ctx, ErrBindingPfmsXmlRequest, err.Error())
		return
	}
	if err := validation.ValidateStruct(req); err != nil {
		apierrors.HandleValidationError(ctx, err)
		log.Error(ctx, "Validation failed for PfmsXmlRequest: %s", err.Error())
		return
	}
	if req.Limit == 0 {
		req.Limit = math.MaxInt32
	}
	request := domain.PfmsXmlSubmissionPendingRequest{
		FinYear: req.FinYear,
	}
	u, err := uh.svc.GetPfmsxmlSubmissionStatusRepo(ctx, &request, req.MetaDataRequest)
	if err != nil {
		apierrors.HandleDBError(ctx, err)
		log.Error(ctx, "Get PFMS XML Repo call failed: %s", err.Error())
		return
	}

	rsp := response.NewFetchPfmsSubmissionPendingResponse(u)

	metadata := port.NewMetaDataResponse(req.Skip, req.Limit, len(rsp))

	apiRsp := response.GetPfmsSubmissionPendingResponse{
		StatusCodeAndMessage: port.FetchSucess,
		MetaDataResponse:     metadata,
		Data:                 rsp,
	}
	log.Debug(ctx, "ListPfmsXmlHandler response", apiRsp)
	handleSuccess(ctx, apiRsp)

}

func (uh *PaogenHandler) ConvertMapToStringMap(params map[string]interface{}) map[string]string {
	stringParams := make(map[string]string)
	for key, value := range params {
		stringParams[key] = fmt.Sprintf("%v", value)
	}
	return stringParams

}
func (uh *PaogenHandler) CallAPI(url string, method string, headers map[string]string, params interface{}) (map[string]interface{}, error) {
	tr := &http.Transport{
		TLSClientConfig: &tls.Config{
			MinVersion:         tls.VersionTLS12,
			InsecureSkipVerify: false,
			Renegotiation:      tls.RenegotiateOnceAsClient,
		},
		DisableKeepAlives: true,
	}

	client := resty.New().SetTimeout(15 * time.Second)
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
	case "POST", "PUT", "DELETE":
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
func GenerateRandomNumber(paoCode string, financialYear string) string {
	// Ensure PAO code is 6 digits
	if len(paoCode) != 6 {
		panic("PAO Code must be exactly 6 digits")
	}
	// Ensure financial year is 4 digits
	if len(financialYear) != 4 {
		panic("Financial Year must be exactly 4 digits")
	}

	// Seed random generator
	rand.Seed(time.Now().UnixNano())

	// Generate 10 random digits
	randomNumber := rand.Int63n(1_000_000_0000) // Ensures a 10-digit number

	// Format the final output
	return fmt.Sprintf("TE-%s%s%010d", paoCode, financialYear, randomNumber)
}

type DdoMasterInput struct {
	PaoCode     string `json:"pao-code" validate:"required,validatePaocode"`
	DdoCode     string `json:"ddo-code" validate:"required,validateDdocode"`
	PaoOfficeId int64  `json:"pao-office-id" validate:"required,max=99999999"`
	DdoOfficeId int64  `json:"ddo-office-id" validate:"required,max=99999999"`
	DdoName     string `json:"ddo-name" validate:"required,max=100"`
	PaoName     string `json:"pao-name" validate:"required,max=100"`
	DdoType     string `json:"ddo-type" validate:"required,max=20"`
	GstNumber   string `json:"gst-number" validate:"required,max=20"`
}

// CreateDdomasterHandler godoc
//
//	@Summary		Post ddomaster entries
//	@Description	Post ddomaster entries
//	@Tags			PAO GENERAL
//	@Accept			json
//	@Produce		json
//	@Param			body	body		DdoMasterInput	true	"Ddo master request"
//	@Success		201		{object}	response.GetDDOlistResponse			"resource created successfully"
//	@Failure		400					{object}	apierrors.APIErrorResponse						"Validation error"
//	@Failure		401					{object}	apierrors.APIErrorResponse						"data retrieved successfully"
//	@Failure		403					{object}	apierrors.APIErrorResponse						"Forbidden error"
//	@Failure		404					{object}	apierrors.APIErrorResponse						"Data not found error"
//	@Failure		409					{object}	apierrors.APIErrorResponse						"Data conflict error"
//	@Failure		500					{object}	apierrors.APIErrorResponse						"Internal server error"
//	@Router			/v1/pao-gen/ddo-master [post]
func (uh *PaogenHandler) CreateDdomasterHandler(ctx *gin.Context) {

	var requestsin DdoMasterInput
	if err := ctx.ShouldBindJSON(&requestsin); err != nil {
		apierrors.HandleBindingError(ctx, err)
		log.Error(ctx, "Binding failed for PfmsVerifieds: %s", err.Error())
		return
	}

	if err := validation.ValidateStruct(requestsin); err != nil {
		apierrors.HandleValidationError(ctx, err)
		log.Error(ctx, "Validation failed for PfmsVerifieds: %s", err.Error())
		return
	}

	var requests domain.DdoMasterInput

	requests = domain.DdoMasterInput{

		PaoCode:     requestsin.PaoCode,
		DdoCode:     requestsin.DdoCode,
		PaoOfficeId: requestsin.PaoOfficeId,
		DdoOfficeId: requestsin.DdoOfficeId,
		DdoName:     requestsin.DdoName,
		PaoName:     requestsin.PaoName,
		DdoType:     requestsin.DdoType,
		GstNumber:   requestsin.GstNumber,
	}

	err := uh.svc.CreateDdomasterRepo(ctx, requests)
	if err != nil {
		log.Error(ctx, "Ddo master Repo call failed: %s", err.Error())
		if err.(*pgconn.PgError).Code == "23503" {
			err1 := errors.New(ErrCashbookVerificationFailed)
			// Create an AppError with a user-friendly message and code.
			appError := apierrors.NewAppError(
				"ddo_master entry already exists", // User-friendly error message
				"409",                             // Error code representing the error type
				err1,                              // Original error for debugging purposes
			)
			apiErrorResponse := apierrors.NewAPIErrorResponse(
				http.StatusConflict, // HTTP status code
				"Conflict",          // Message to return to the client
				appError,            // Encapsulated application error
			)
			ctx.JSON(apiErrorResponse.StatusCode, apiErrorResponse)
			return
		} else if err.(*pgconn.PgError).Code == "23505" {
			err1 := errors.New(ErrCashbookVerificationFailed)
			// Create an AppError with a user-friendly message and code.
			appError := apierrors.NewAppError(
				"ddo_master entry already exists", // User-friendly error message
				"409",                             // Error code representing the error type
				err1,                              // Original error for debugging purposes
			)
			apiErrorResponse := apierrors.NewAPIErrorResponse(
				http.StatusConflict, // HTTP status code
				"Conflict",          // Message to return to the client
				appError,            // Encapsulated application error
			)
			ctx.JSON(apiErrorResponse.StatusCode, apiErrorResponse)
			return

		} else {
			apierrors.HandleDBError(ctx, err)
			return
		}
	}

	apiRsp := response.GetDDOlistResponse{
		StatusCodeAndMessage: port.CreateSuccess,
	}
	log.Debug(ctx, "CreatePFMSVerificationHandler response", apiRsp)
	handleCreateSuccess(ctx, apiRsp)

}

type UpdatePfmsSubmissionStatusReq struct {
	UniqueIdentifier string `uri:"unique-identifier" validate:"required,max=50"`
}
type RequestStatus struct {
	UniqueIdentifier string `json:"UniqueIdentifier"`
	RequestSource    string `json:"RequestSource"`
	Status           string `json:"Status"`
	TENumber         string `json:"TENumber,omitempty"`
	Errors           []struct {
		ErrorCode    string `json:"ErrorCode"`
		ErrorMessage string `json:"ErrorMessage"`
	} `json:"Errors,omitempty"`
}

// ResponseData represents the structure of the responseData JSON string
type ResponseData struct {
	RequestStatus []RequestStatus `json:"RequestStatus"`
}

// UpdatePfmsSubmissionStatusHandler godoc
//
//	@Summary		Update the Pfms Submission status
//	@Description	Update the Pfms Submission status
//	@Tags			PAO GENERAL
//	@Accept			json
//	@Produce		json
//	@Param			unique-identifier	path		string			true	"unique-identifier"
//	@Success		200		{object}	response.GetDDOlistResponse			"resource updated successfully"
//	@Failure		400					{object}	apierrors.APIErrorResponse						"Validation error"
//	@Failure		401					{object}	apierrors.APIErrorResponse						"Unauthorized error"
//	@Failure		403					{object}	apierrors.APIErrorResponse						"Forbidden error"
//	@Failure		404					{object}	apierrors.APIErrorResponse						"Data not found error"
//	@Failure		409					{object}	apierrors.APIErrorResponse						"Data conflict error"
//	@Failure		500					{object}	apierrors.APIErrorResponse						"Internal server error"
//	@Router			/v1/pao-gen/pfms-submission/{unique-identifier} [put]
func (uh *PaogenHandler) UpdatePfmsSubmissionStatusHandler(ctx *gin.Context) {

	var req UpdatePfmsSubmissionStatusReq
	if err := ctx.ShouldBindUri(&req); err != nil {
		apierrors.HandleBindingError(ctx, err)
		log.Error(ctx, ErrBindingDdoListRequestMonthly, err.Error())
		return
	}
	if err := validation.ValidateStruct(req); err != nil {
		apierrors.HandleValidationError(ctx, err)
		log.Error(ctx, "Validation failed for DdoListRequestMonthly: %s", err.Error())
		return
	}

	request := domain.UpdatePfmsSubmissionStatusReq{
		UniqueIdentifier: req.UniqueIdentifier,
	}

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

	startTime := time.Now()
	response1, err := uh.CallAPI(url, method, header, params)
	latency := time.Since(startTime).Milliseconds()
	log.Debug(ctx, "GetAuthCode API Call Latency: %d ms", latency)
	if err != nil {
		log.Error(ctx, "PFMS AuthCode API Call Failed: %v | Username: %s | RequestSource: %s | Password: %s | URL: %s | Params: %v | Raw Response: %s | Latency: %d ms",
			err, username, requestsource, password, url, params, response1, latency)
		apierrors.HandleError(ctx, err)
		return
	}

	log.Debug(ctx, "Raw API1 Response: %v", response1)
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

	startTime = time.Now()
	response2, err1 := uh.CallAPI(url1, method1, header1, params1)
	latency = time.Since(startTime).Milliseconds()
	log.Debug(ctx, "LogIn API Call Latency: %d ms", latency)
	if err1 != nil {
		log.Error(ctx, "PFMS AccessCode API Call Failed: %v | Username: %s | RequestSource: %s | Password: %s | URL: %s | Params: %v | Raw Response: %s | Latency: %d ms",
			err1, username, requestsource, password, url1, params1, response2, latency)
		apierrors.HandleError(ctx, err1)
		return
	}

	log.Debug(ctx, "Raw API2 Response: %v", response2)
	success = response2["isSuccess"].(string)
	if success == "0" {
		ctx.JSON(http.StatusBadRequest, gin.H{"error": response2["errorMessage"]})
		return
	}
	accesstoken = response2["accessToken"].(string)

	url2 := baseurl + "/Budget/GetTransferEntryRequestStatus" // Corrected URL
	method2 := "POST"
	header2 := map[string]string{
		"Content-Type":  "application/json",
		"Authorization": "Bearer " + accesstoken,
	}
	params2 := map[string]interface{}{
		"requestPayload": map[string]interface{}{
			"GetTransferEntryRequestStatus": []map[string]interface{}{
				{
					"UniqueIdentifier": req.UniqueIdentifier, // Use the UniqueIdentifier from the request
					"RequestSource":    requestsource,        // Hardcoded as per your requirement
				},
			},
		},
	}

	startTime = time.Now()
	response3, err2 := uh.CallAPI(url2, method2, header2, params2)
	latency = time.Since(startTime).Milliseconds()
	log.Debug(ctx, "ReceiveTransferEntryData API Call Latency: %d ms", latency)

	if err2 != nil {
		log.Error(ctx, "PFMS Submit PFMS DATA API Call Failed: %v | Username: %s | RequestSource: %s | Password: %s | URL: %s | Params: %v | Raw Response: %s | Latency: %d ms",
			err2, username, requestsource, password, url2, params2, response3, latency)
		apierrors.HandleError(ctx, err2)
		return
	}
	log.Debug(ctx, "Raw API2 Response: %v", response3)

	// Check if the API call was successful
	success = response3["isSuccess"].(string)
	if success == "0" {
		ctx.JSON(http.StatusBadRequest, gin.H{"error": response3["errorMessage"]})
		return
	}

	// Parse responseData JSON string
	var responseData ResponseData
	if err := json.Unmarshal([]byte(response3["responseData"].(string)), &responseData); err != nil {
		apierrors.HandleError(ctx, fmt.Errorf("failed to parse responseData: %w", err))
		log.Error(ctx, "Failed to parse responseData: %s", err.Error())
		return
	}

	// Extract Status and ErrorDescription from RequestStatus
	if len(responseData.RequestStatus) == 0 {
		apierrors.HandleError(ctx, fmt.Errorf("no RequestStatus found in responseData"))
		log.Error(ctx, "No RequestStatus found in responseData")
		return
	}

	// Assuming the first item in RequestStatus corresponds to the requested UniqueIdentifier
	rs := responseData.RequestStatus[0]
	request.Status = rs.Status
	var TENumber string
	if request.Status == "Success" {
		TENumber = rs.TENumber
	}
	if len(rs.Errors) > 0 {
		// Combine error messages as sentences
		var errorMessages []string
		for _, e := range rs.Errors {
			// Trim leading/trailing spaces and ensure the message is not empty
			message := strings.TrimSpace(e.ErrorMessage)
			if message != "" {
				errorMessages = append(errorMessages, message)
			}
		}
		if len(errorMessages) > 0 {
			// Capitalize the first letter of the first error message
			errorMessages[0] = strings.ToUpper(errorMessages[0][:1]) + errorMessages[0][1:]
			// Join with periods and ensure the final string ends with a period
			request.ErrorDescription = strings.Join(errorMessages, ". ") + "."
		}
	}

	err3 := uh.svc.UpdatePfmsSubmissionStatusRepo(ctx, &request, TENumber)
	if err3 != nil {
		apierrors.HandleDBError(ctx, err3)
		log.Error(ctx, "Update DDO list monthly Repo call failed: %s", err3.Error())
		return
	}

	apiRsp := response.GetDDOlistResponse{
		StatusCodeAndMessage: port.UpdateSuccess,
	}
	log.Debug(ctx, "UpdateDdoMonthlyHandler response", apiRsp)
	handleSuccess(ctx, apiRsp)

}

// Get PAOs List with DDO codes godoc
//
//	@Summary		GET the list of PAOs with ddo codes
//	@Description	GET the list of PAOs with ddo codes
//	@Tags			PAO GENERAL
//	@Accept			json
//	@Produce		json
//	@Success		200		{object}	response.GetInterPAOsResponse			"list retrieved successfully"
//	@Failure		400					{object}	apierrors.APIErrorResponse						"Validation error"
//	@Failure		401					{object}	apierrors.APIErrorResponse						"data retrieved successfully"
//	@Failure		403					{object}	apierrors.APIErrorResponse						"Forbidden error"
//	@Failure		404					{object}	apierrors.APIErrorResponse						"Data not found error"
//	@Failure		409					{object}	apierrors.APIErrorResponse						"Data conflict error"
//	@Failure		500					{object}	apierrors.APIErrorResponse						"Internal server error"
//	@Router			/v1/pao-gen/pao/inter-pao [get]
func (uh *PaogenHandler) ListInterPAOTEHandler(ctx *gin.Context) {

	u, err := uh.svc.GetInterPAOsRepo(ctx)
	if err != nil {
		apierrors.HandleDBError(ctx, err)
		log.Error(ctx, "Get PAO List Repo call failed: %s", err.Error())
		return
	}

	rsp := response.NewGetInterPAOsResponse(u)

	metadata := port.NewMetaDataResponse(0, 0, len(rsp))

	apiRsp := response.GetInterPAOsResponse{
		StatusCodeAndMessage: port.ListSuccess,
		MetaDataResponse:     metadata,
		Data:                 rsp,
	}
	log.Debug(ctx, "ListPAOHandler response", apiRsp)
	handleSuccess(ctx, apiRsp)

}

// Get SO Office Details godoc
//
//	@Summary		Get the so office details
//	@Description	Get the so office details using officeid
//	@Tags			PAO GENERAL
//	@Accept			json
//	@Produce		json
//	@Param			id	path		int				true	"Office ID"
//	@Success		200	{object}	response.SOOfficeDetailsResponse	"data retrieved successfully"
//	@Failure		400					{object}	apierrors.APIErrorResponse						"Validation error"
//	@Failure		401					{object}	apierrors.APIErrorResponse						"data retrieved successfully"
//	@Failure		403					{object}	apierrors.APIErrorResponse						"Forbidden error"
//	@Failure		404					{object}	apierrors.APIErrorResponse						"Data not found error"
//	@Failure		409					{object}	apierrors.APIErrorResponse						"Data conflict error"
//	@Failure		500					{object}	apierrors.APIErrorResponse						"Internal server error"
//	@Router			/v1/pao-gen/so-office-details/{id} [get]
func (uh *PaogenHandler) FetchSOOfficeDetailsHandler(ctx *gin.Context) {

	var req OfficeNameRequest
	if err := ctx.ShouldBindUri(&req); err != nil {
		apierrors.HandleBindingError(ctx, err)
		log.Error(ctx, "Binding failed for OfficeNameRequest: %s", err.Error())
		return
	}
	if err := validation.ValidateStruct(req); err != nil {
		apierrors.HandleValidationError(ctx, err)
		log.Error(ctx, "Validation failed for OfficeNameRequest: %s", err.Error())
		return
	}
	request := domain.OfficeNameRequest{
		Id: req.Id,
	}
	u, b, err := uh.svc.GetSOOfficeDetailsRepo(ctx, &request)
	if err != nil {
		apierrors.HandleDBError(ctx, err)
		log.Error(ctx, "Get Office details Repo call failed: %s", err.Error())
		return
	}
	if b {
		rsp := response.NewSOOfficeDetailsResponse(*u)

		metadata := port.NewMetaDataResponse(0, 0, 1)

		apiRsp := response.SOOfficeDetailsResponse{
			StatusCodeAndMessage: port.FetchSucess,
			MetaDataResponse:     metadata,
			Data:                 rsp,
		}
		log.Debug(ctx, "FetchOfficenameHandler response", apiRsp)
		handleSuccess(ctx, apiRsp)
	} else {
		err1 := errors.New("Invalid office_id")
		// Create an AppError with a user-friendly message and code.
		appError := apierrors.NewAppError(
			"No office corresponding to this office_id", // User-friendly error message
			"404", // Error code representing the error type
			err1,  // Original error for debugging purposes
		)
		apiErrorResponse := apierrors.NewAPIErrorResponse(
			http.StatusNotFound, // HTTP status code
			"Not Found",         // Message to return to the client
			appError,            // Encapsulated application error
		)
		ctx.JSON(apiErrorResponse.StatusCode, apiErrorResponse)
	}
}

// Pfms-ReSubmission godoc
//
//	@Summary		Resubmit Pfms verified data into pfms server
//	@Description	Resubmit Pfms verified data into pfms server
//	@Tags			PAO GENERAL
//	@Accept			json
//	@Produce		json
//	@Param			unique-identifier	path		string			true	"unique-identifier"
//	@Success		201		{object}	response.GetPfmsSubmissionPendingResponse			"record inserted successfully"
//	@Failure		400					{object}	apierrors.APIErrorResponse						"Validation error"
//	@Failure		401					{object}	apierrors.APIErrorResponse						"Unauthorized error"
//	@Failure		403					{object}	apierrors.APIErrorResponse						"Forbidden error"
//	@Failure		404					{object}	apierrors.APIErrorResponse						"Data not found error"
//	@Failure		409					{object}	apierrors.APIErrorResponse						"Data conflict error"
//	@Failure		500					{object}	apierrors.APIErrorResponse						"Internal server error"
//	@Router			/v1/pao-gen/pfms-resubmission/{unique-identifier} [post]
func (uh *PaogenHandler) FetchPfmsReSubmissionHandler(ctx *gin.Context) {

	var req UpdatePfmsSubmissionStatusReq
	if err := ctx.ShouldBindUri(&req); err != nil {
		apierrors.HandleBindingError(ctx, err)
		log.Error(ctx, ErrBindingDdoListRequestMonthly, err.Error())
		return
	}
	if err := validation.ValidateStruct(req); err != nil {
		apierrors.HandleValidationError(ctx, err)
		log.Error(ctx, "Validation failed for DdoListRequestMonthly: %s", err.Error())
		return
	}

	request := domain.UpdatePfmsSubmissionStatusReq{
		UniqueIdentifier: req.UniqueIdentifier,
	}
	var requests []domain.CbData

	requests, err := uh.svc.GetCbRequestData(ctx, &request)
	if err != nil {
		apierrors.HandleDBError(ctx, err)
		log.Error(ctx, "Get PFMS Resubmission call failed: %s", err.Error())
		return
	}

	var PfmsPayload domain.Payload
	var Paocode string
	var FinYear string
	var Tedate string
	for _, request := range requests {
		Paocode = request.PaoCode
		FinYear = request.FinYear
		Tedate = request.CbDate
	}

	closingBalances, err := uh.svc.GetClosingBalanceRepo(ctx, requests)
	if err != nil {
		apierrors.HandleDBError(ctx, err)
		log.Error(ctx, "GetClosingBalanceRepo failed: %s", err.Error())
		return
	}

	// Create a map of closing balances for quick lookup
	closingBalMap := make(map[string]float64)
	for _, cb := range closingBalances {
		if cb.ClosingBal.Valid {
			closingBalMap[cb.OfficeId] = float64(cb.ClosingBal.Int64)
		}
	}

	pfmsjs, err := uh.svc.GetPfmsJsonRepo(ctx, requests)
	if err != nil {
		apierrors.HandleDBError(ctx, err)
		log.Error(ctx, "Get PFMS Repo call failed: %s", err.Error())
		return
	}
	if len(pfmsjs) == 0 {
		err := fmt.Errorf("No effective Debit or Credit to any HOA: This happens when both OB and CB are zero for the DDO or no transactions exist for the DDO in the given period")
		apierrors.HandleDBError(ctx, err)
		log.Error(ctx, "Get PFMS Repo returned empty result: %s", err.Error())
		return
	}
	pfmsjson := ProcessAmountAndSign(ctx, pfmsjs)

	totals, err := CalculateTotalsByDDO(ctx, pfmsjson)
	if err != nil {
		apierrors.HandleError(ctx, err)
		log.Error(ctx, "CalculateTotalsByDDO failed: %s", err.Error())
		return
	}

	// Process each DDO total and append new entries as needed
	for _, total := range totals {
		closingBal, exists := closingBalMap[total.DdoOfficeID]
		if !exists {
			log.Error(ctx, "No closing balance found for DdoOfficeID: %s", total.DdoOfficeID)
			continue
		}

		// Calculate equation: TotalReceipt + TotalPayment - 2 * ClosingBal
		equationResult := total.TotalReceipt - total.TotalPayment - (2 * closingBal)
		var newEntry domain.TransferEntryAccountingDetail

		if equationResult < 0 {
			// Equation > 0: Add Receipt entry
			newEntry = domain.TransferEntryAccountingDetail{
				DdoOfficeID:    null.String{String: total.DdoOfficeID, Valid: true},
				FunctionalHead: null.String{String: "8671001020100", Valid: true},
				ObjectHead:     null.String{String: "00", Valid: true},
				GrantNo:        null.String{String: "800", Valid: true},
				Category:       null.String{String: "6", Valid: true},
				Remarks:        null.String{String: "Cash received", Valid: true},
				ReceiptPayment: null.String{String: "T", Valid: true},
				Sign:           null.String{String: "+", Valid: true},
				Amount:         null.Float64{Float64: -equationResult, Valid: true},
			}
		} else if equationResult > 0 {
			// Equation < 0: Add Payment entry
			newEntry = domain.TransferEntryAccountingDetail{
				DdoOfficeID:    null.String{String: total.DdoOfficeID, Valid: true},
				FunctionalHead: null.String{String: "8671001020100", Valid: true},
				ObjectHead:     null.String{String: "00", Valid: true},
				GrantNo:        null.String{String: "800", Valid: true},
				Category:       null.String{String: "7", Valid: true},
				Remarks:        null.String{String: "Cash sent", Valid: true},
				ReceiptPayment: null.String{String: "F", Valid: true},
				Sign:           null.String{String: "+", Valid: true},
				Amount:         null.Float64{Float64: equationResult, Valid: true}, // Use absolute value
			}
		} else {
			continue // Skip if equationResult == 0
		}
		pfmsjson = append(pfmsjson, newEntry)
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
	startTime := time.Now()
	response1, err := uh.CallAPI(url, method, header, params)
	latency := time.Since(startTime).Milliseconds()
	log.Debug(ctx, "GetAuthCode API Call Latency: %d ms", latency)
	if err != nil {
		log.Error(ctx, "PFMS AuthCode API Call Failed: %v | Username: %s | RequestSource: %s | Password: %s | URL: %s | Params: %v | Raw Response: %s | Latency: %d ms",
			err, username, requestsource, password, url, params, response1, latency)
		apierrors.HandleError(ctx, err)
		return
	}

	log.Debug(ctx, "Raw API1 Response: %v", response1)

	success := response1["IsSuccess"].(string)
	if success == "0" {
		ctx.JSON(http.StatusBadRequest, gin.H{"error": response1["ErrorMessage"]})
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

	startTime = time.Now()
	response2, err1 := uh.CallAPI(url1, method1, header1, params1)
	latency = time.Since(startTime).Milliseconds()
	log.Debug(ctx, "LogIn API Call Latency: %d ms", latency)
	if err1 != nil {
		log.Error(ctx, "PFMS AccessCode API Call Failed: %v | Username: %s | RequestSource: %s | Password: %s | URL: %s | Params: %v | Raw Response: %s | Latency: %d ms",
			err1, username, requestsource, password, url1, params1, response2, latency)
		apierrors.HandleError(ctx, err1)
		return
	}

	log.Debug(ctx, "Raw API2 Response: %v", response2)
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

	startTime = time.Now()
	response3, err2 := uh.CallAPI(url2, method2, header2, params2)
	latency = time.Since(startTime).Milliseconds()
	log.Debug(ctx, "ReceiveTransferEntryData API Call Latency: %d ms", latency)

	if err2 != nil {
		log.Error(ctx, "PFMS Submit PFMS DATA API Call Failed: %v | Username: %s | RequestSource: %s | Password: %s | URL: %s | Params: %v | Raw Response: %s | Latency: %d ms",
			err2, username, requestsource, password, url2, params2, response3, latency)
		apierrors.HandleError(ctx, err2)
		return
	}
	log.Debug(ctx, "Raw API2 Response: %v", response3)

	success, ok := response3["isSuccess"].(string)
	if !ok || success == "0" {
		errorMessage := "Unknown error"
		if em, exists := response3["errorMessage"]; exists {
			errorMessage = fmt.Sprintf("%v", em)
		}

		log.Error(ctx, "PFMS Submit PFMS DATA API responded with failure. Request: %+v, Response: %+v", params2, response3)
		ctx.JSON(http.StatusBadRequest, gin.H{"error": errorMessage})
		return
	}
	err4 := uh.svc.GetPfmsUpdateStatusRepo(ctx, requests, transferEntry.UniqueIdentifier)
	if err4 != nil {
		apierrors.HandleDBError(ctx, err4)
		log.Error(ctx, "Get PFMS Repo call failed: %s", err4.Error())
		return
	}
	errInsert := uh.svc.InsertPfmsSubmission(
		ctx,
		transferEntry.UniqueIdentifier,
		"cb",                         // Since we are submitting cashbook
		requests,                     // Store API requests in cb_request
		domain.TransferEntryDetail{}, // te_request is empty (null in JSONB)
		Tedate,                       // Business date
		time.Now(),                   // Submission date
		PfmsPayload,                  // Payload sent to PFMS API
		"Pending",                    // submissionStatus set to "Pending"
		"",                           // errorDescription is null
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

type PfmsEmptyVerified struct {
	DdoCode            string      `json:"ddo_code" select:"ddo_code" validate:"required,validateDdocode"`
	BusinessDate       time.Time   `json:"business_date" select:"business_date" validate:"required"`
	ClosingBal         float64     `json:"closing_bal" select:"closing_bal"`
	OpeningBal         float64     `json:"opening_bal" select:"opening_bal"`
	VerifiedBy         uint64      `json:"verified_by" select:"verified_by" validate:"required,employee_id"`
	VerificationStatus string      `json:"h_verification" select:"h_verification_flag" validate:"required,max=20"`
	Hoa                string      `json:"hoa" select:"hoa"`
	Payment            float64     `json:"payment" select:"payment"`
	Receipt            float64     `json:"receipt" select:"receipt"`
	AccountCodeArray   []CodeArray `json:"account_array" select:"account_array"`
}

// CreateEmptyPFMSVerificationHandler godoc
//
//	@Summary		Post Empty Pfms verified data into database
//	@Description	Post Empty Pfms verified data into database
//	@Tags			PAO GENERAL
//	@Accept			json
//	@Produce		json
//	@Param			body	body		[]PfmsEmptyVerified	true	"Post pfms verified request"
//	@Success		201		{object}	response.GetDDOlistResponse			"resource created successfully"
//	@Failure		400					{object}	apierrors.APIErrorResponse						"Validation error"
//	@Failure		401					{object}	apierrors.APIErrorResponse						"data retrieved successfully"
//	@Failure		403					{object}	apierrors.APIErrorResponse						"Forbidden error"
//	@Failure		404					{object}	apierrors.APIErrorResponse						"Data not found error"
//	@Failure		409					{object}	apierrors.APIErrorResponse						"Data conflict error"
//	@Failure		500					{object}	apierrors.APIErrorResponse						"Internal server error"
//	@Router			/v1/pao-gen/cashbook/verifications-empty [post]
func (uh *PaogenHandler) CreateEmptyPFMSVerificationHandler(ctx *gin.Context) {

	var requestsin []PfmsEmptyVerified
	if err := ctx.ShouldBindJSON(&requestsin); err != nil {
		apierrors.HandleBindingError(ctx, err)
		log.Error(ctx, "Binding failed for PfmsVerifieds: %s", err.Error())
		return
	}
	for _, r := range requestsin {
		if err := validation.ValidateStruct(r); err != nil {
			apierrors.HandleValidationError(ctx, err)
			log.Error(ctx, "Validation failed for PfmsVerifieds: %s", err.Error())
			return
		}

	}

	if len(requestsin) > 0 {
		ddoCode := requestsin[0].DdoCode
		businessDate := requestsin[0].BusinessDate
		openingBalance := requestsin[0].OpeningBal

		val, err := uh.svc.CheckPreviousCashbook(ctx, ddoCode, businessDate)

		if err != nil {
			log.Error(ctx, "CheckPreviousCashbook failed: %s", err.Error())
			apierrors.HandleDBError(ctx, err)
			return
		}
		if val != nil {
			// 1️⃣ Check verification flag
			if !val.H_verification_flag.Valid || !val.H_verification_flag.Bool {
				appError := apierrors.NewAppError(
					"Previous cashbooks pending for verification",
					"409",
					errors.New("previous cashbook not verified"),
				)
				apiErrorResponse := apierrors.NewAPIErrorResponse(http.StatusConflict, "Conflict", appError)
				ctx.JSON(apiErrorResponse.StatusCode, apiErrorResponse)
				return
			}

			// 2️⃣ Check PFMS generation flag
			if !val.H_pfms_generation_flag.Valid || !val.H_pfms_generation_flag.Bool {
				appError := apierrors.NewAppError(
					"Previous cashbooks pending for PFMS submission",
					"409",
					errors.New("previous cashbook not submitted to PFMS"),
				)
				apiErrorResponse := apierrors.NewAPIErrorResponse(http.StatusConflict, "Conflict", appError)
				ctx.JSON(apiErrorResponse.StatusCode, apiErrorResponse)
				return
			}

			// 3️⃣ Check PFMS submission status
			if val.Pfms_submission_flag.Valid {
				flag := strings.ToLower(val.Pfms_submission_flag.String)
				if flag == "Pending" || flag == "Failed" {
					appError := apierrors.NewAppError(
						"Previous cashbook Failed or Pending",
						"409",
						errors.New("previous cashbook submission not completed"),
					)
					apiErrorResponse := apierrors.NewAPIErrorResponse(http.StatusConflict, "Conflict", appError)
					ctx.JSON(apiErrorResponse.StatusCode, apiErrorResponse)
					return
				}
			}

			if val.ClosingBal.Valid {
				if openingBalance != val.ClosingBal.Float64 {
					appError := apierrors.NewAppError(
						"Opening balance mismatch with previous cashbook",
						"409",
						errors.New("invalid opening balance"),
					)
					apiErrorResponse := apierrors.NewAPIErrorResponse(http.StatusConflict, "Conflict", appError)
					ctx.JSON(apiErrorResponse.StatusCode, apiErrorResponse)
					return
				}
			}
		}
	}

	var requests []domain.PfmsVerified

	for _, request := range requestsin {

		requests = append(requests, domain.PfmsVerified{

			DdoCode:            null.StringFrom(request.DdoCode),
			BusinessDate:       request.BusinessDate,
			ClosingBal:         null.Float64From(request.ClosingBal),
			OpeningBal:         null.Float64From(request.OpeningBal),
			VerifiedBy:         null.Uint64From(request.VerifiedBy),
			VerificationStatus: null.StringFrom(request.VerificationStatus),
		})
	}
	err := uh.svc.PostEmptyPfmsverifiedRepo(ctx, requests)
	if err != nil {
		log.Error(ctx, "PFMS Verified Repo call failed: %s", err.Error())
		if err.(*pgconn.PgError).Code == "23503" {
			err1 := errors.New(ErrCashbookVerificationFailed)
			// Create an AppError with a user-friendly message and code.
			appError := apierrors.NewAppError(
				"cashbook not received", // User-friendly error message
				"409",                   // Error code representing the error type
				err1,                    // Original error for debugging purposes
			)
			apiErrorResponse := apierrors.NewAPIErrorResponse(
				http.StatusConflict, // HTTP status code
				"Conflict",          // Message to return to the client
				appError,            // Encapsulated application error
			)
			ctx.JSON(apiErrorResponse.StatusCode, apiErrorResponse)
			return
		} else if err.(*pgconn.PgError).Code == "23505" {
			err1 := errors.New(ErrCashbookVerificationFailed)
			// Create an AppError with a user-friendly message and code.
			appError := apierrors.NewAppError(
				"cashbook already verified", // User-friendly error message
				"409",                       // Error code representing the error type
				err1,                        // Original error for debugging purposes
			)
			apiErrorResponse := apierrors.NewAPIErrorResponse(
				http.StatusConflict, // HTTP status code
				"Conflict",          // Message to return to the client
				appError,            // Encapsulated application error
			)
			ctx.JSON(apiErrorResponse.StatusCode, apiErrorResponse)
			return

		} else {
			apierrors.HandleDBError(ctx, err)
			return
		}
	}

	apiRsp := response.GetDDOlistResponse{
		StatusCodeAndMessage: port.CreateSuccess,
	}
	log.Debug(ctx, "CreatePFMSVerificationHandler response", apiRsp)
	handleCreateSuccess(ctx, apiRsp)

}

type HoaRequest1 struct {
	Hoa string `form:"hoa" validate:"required,max=20"`
}

// ListHoaHandler-Only godoc
//
//	@Summary		Get HOA list
//	@Description	Get HOA list based on character input
//	@Tags			PAO GENERAL
//	@Accept			json
//	@Produce		json
//	@Param			hoa			query		string									true	"hoa"
//
// @Param       skip    query       int     			false   		"Number of records to skip for pagination"
// @Param       limit   query       int     			false   		"Number of records to limit for pagination"
//
//	@Success		200					{object}	response.GetHoaResponse	"list retrieved successfully"
//	@Failure		400					{object}	apierrors.APIErrorResponse						"Validation error"
//	@Failure		401					{object}	apierrors.APIErrorResponse						"Unauthorized error"
//	@Failure		403					{object}	apierrors.APIErrorResponse						"Forbidden error"
//	@Failure		404					{object}	apierrors.APIErrorResponse						"Data not found error"
//	@Failure		409					{object}	apierrors.APIErrorResponse						"Data conflict error"
//	@Failure		500					{object}	apierrors.APIErrorResponse						"Internal server error"
//	@Router			/v1/pao-gen/hoa-only [get]
func (uh *PaogenHandler) ListHoaHandler(ctx *gin.Context) {

	var req HoaRequest1
	if err := ctx.ShouldBindQuery(&req); err != nil {
		apierrors.HandleBindingError(ctx, err)
		log.Error(ctx, "Binding failed for HoaRequest1: %s", err.Error())
		return
	}
	if err := validation.ValidateStruct(req); err != nil {
		apierrors.HandleValidationError(ctx, err)
		log.Error(ctx, "Validation failed for HoaRequest1: %s", err.Error())
		return
	}

	request := domain.HoaRequest1{
		Hoa: req.Hoa,
	}
	u, err := uh.svc.GetHoaonlyRepo(ctx, &request)
	if err != nil {
		apierrors.HandleDBError(ctx, err)
		log.Error(ctx, "Get Hoa Only Repo call failed: %s", err.Error())
		return
	}

	rsp := response.NewGetHoaResponse(u)

	metadata := port.NewMetaDataResponse(0, 10, len(rsp))

	apiRsp := response.GetHoaResponse{
		StatusCodeAndMessage: port.ListSuccess,
		MetaDataResponse:     metadata,
		Data:                 rsp,
	}
	log.Debug(ctx, "ListHoaHandler response", apiRsp)
	handleSuccess(ctx, apiRsp)
}

type AccountCodeRequest struct {
	AccountCode string `form:"account-code" validate:"required,max=20"`
}

// ListHoaHandler-Only godoc
//
//	@Summary		Get HOA list
//	@Description	Get HOA list based on character input
//	@Tags			PAO GENERAL
//	@Accept			json
//	@Produce		json
//	@Param			account-code			query		string									true	"account-code"
//
// @Param       skip    query       int     			false   		"Number of records to skip for pagination"
// @Param       limit   query       int     			false   		"Number of records to limit for pagination"
//
//	@Success		200					{object}	response.AccountCodeResponse	"list retrieved successfully"
//	@Failure		400					{object}	apierrors.APIErrorResponse						"Validation error"
//	@Failure		401					{object}	apierrors.APIErrorResponse						"Unauthorized error"
//	@Failure		403					{object}	apierrors.APIErrorResponse						"Forbidden error"
//	@Failure		404					{object}	apierrors.APIErrorResponse						"Data not found error"
//	@Failure		409					{object}	apierrors.APIErrorResponse						"Data conflict error"
//	@Failure		500					{object}	apierrors.APIErrorResponse						"Internal server error"
//	@Router			/v1/pao-gen/search-account-codes [get]
func (uh *PaogenHandler) ListAccountCodeHandler(ctx *gin.Context) {

	var req AccountCodeRequest
	if err := ctx.ShouldBindQuery(&req); err != nil {
		apierrors.HandleBindingError(ctx, err)
		log.Error(ctx, "Binding failed for HoaRequest1: %s", err.Error())
		return
	}
	if err := validation.ValidateStruct(req); err != nil {
		apierrors.HandleValidationError(ctx, err)
		log.Error(ctx, "Validation failed for HoaRequest1: %s", err.Error())
		return
	}

	request := domain.AccountCodeRequest{
		AccountCode: req.AccountCode,
	}
	u, err := uh.svc.GetAccountCodeRepo(ctx, &request)
	if err != nil {
		apierrors.HandleDBError(ctx, err)
		log.Error(ctx, "Get Account Code Only Repo call failed: %s", err.Error())
		return
	}

	rsp := response.NewAccountCodeResponse(u)

	metadata := port.NewMetaDataResponse(0, 10, len(rsp))

	apiRsp := response.AccountCodeResponse{
		StatusCodeAndMessage: port.ListSuccess,
		MetaDataResponse:     metadata,
		Data:                 rsp,
	}
	log.Debug(ctx, "ListHoaHandler response", apiRsp)
	handleSuccess(ctx, apiRsp)
}

// UpdatePfmsHandler godoc
//
//	@Summary		Update all Pfms Submission status
//	@Description	Update all Pfms Submission status
//	@Tags			PAO GENERAL
//	@Accept			json
//	@Produce		json
//	@Param			request	body		object{pao_code=string}	true	"PAO Code"
//	@Success		200		{object}	response.GetDDOlistResponse			"resource updated successfully"
//	@Failure		400					{object}	apierrors.APIErrorResponse						"Validation error"
//	@Failure		401					{object}	apierrors.APIErrorResponse						"Unauthorized error"
//	@Failure		403					{object}	apierrors.APIErrorResponse						"Forbidden error"
//	@Failure		404					{object}	apierrors.APIErrorResponse						"Data not found error"
//	@Failure		409					{object}	apierrors.APIErrorResponse						"Data conflict error"
//	@Failure		500					{object}	apierrors.APIErrorResponse						"Internal server error"
//	@Router			/v1/pao-gen/pfms-updation [put]
func (uh *PaogenHandler) UpdatePfmsHandler(ctx *gin.Context) {

	// ← ADD THIS
	// var req struct {
	// 	PaoCode string `json:"pao_code" binding:"required"`
	// }
	// if err := ctx.BindJSON(&req); err != nil {
	// 	ctx.JSON(http.StatusBadRequest, gin.H{"error": "pao_code is required"})
	// 	return
	// }

	// pendingUninqueids, err := uh.svc.GetPendingPfmsUniqueIdsRepo(ctx, req.PaoCode)
	var req struct {
		PaoCode string `json:"pao_code"`
	}
	if err := json.NewDecoder(ctx.Request.Body).Decode(&req); err != nil || req.PaoCode == "" {
		ctx.JSON(http.StatusBadRequest, gin.H{"error": "pao_code is required"})
		return
	}

	pendingUninqueids, err := uh.svc.GetPendingPfmsUniqueIdsRepo(ctx, req.PaoCode)
	if err != nil {
		apierrors.HandleError(ctx, err)
		return
	}

	var requests []domain.UpdatePfmsSubmissionStatusReq
	for _, request := range pendingUninqueids {
		requestResponse := domain.UpdatePfmsSubmissionStatusReq{
			UniqueIdentifier: request.PfmsUniqueId.String,
		}
		requests = append(requests, requestResponse)
	}

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
	// success := response1["IsSuccess"].(string)
	// if success == "0" {
	// 	ctx.JSON(http.StatusBadRequest, gin.H{"error": response1["errorMessage"]})
	// 	return
	// }
	// authcode = response1["AuthCode"].(string)
	isSuccess, ok := response1["IsSuccess"].(string)
	if !ok {
		ctx.JSON(http.StatusBadGateway, gin.H{"error": "PFMS GetAuthCode unreachable"})
		return
	}
	if isSuccess == "0" {
		ctx.JSON(http.StatusBadRequest, gin.H{"error": response1["errorMessage"]})
		return
	}
	authcode, _ = response1["AuthCode"].(string)

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
	// success = response2["isSuccess"].(string)
	// if success == "0" {
	// 	ctx.JSON(http.StatusBadRequest, gin.H{"error": response2["errorMessage"]})
	// 	return
	// }
	// accesstoken = response2["accessToken"].(string)
	isSuccess2, ok := response2["isSuccess"].(string)
	if !ok {
		ctx.JSON(http.StatusBadGateway, gin.H{"error": "PFMS LogIn unreachable"})
		return
	}
	if isSuccess2 == "0" {
		ctx.JSON(http.StatusBadRequest, gin.H{"error": response2["errorMessage"]})
		return
	}
	accesstoken, _ = response2["accessToken"].(string)

	url2 := baseurl + "/Budget/GetTransferEntryRequestStatus" // Corrected URL
	method2 := "POST"
	header2 := map[string]string{
		"Content-Type":  "application/json",
		"Authorization": "Bearer " + accesstoken,
	}

	for _, request := range requests {

		params2 := map[string]interface{}{
			"requestPayload": map[string]interface{}{
				"GetTransferEntryRequestStatus": []map[string]interface{}{
					{
						"UniqueIdentifier": request.UniqueIdentifier, // Use the UniqueIdentifier from the request
						"RequestSource":    requestsource,            // Hardcoded as per your requirement
					},
				},
			},
		}

		response3, err := uh.CallAPI(url2, method2, header2, params2)
		if err != nil {
			apierrors.HandleError(ctx, err)
			return
		}

		// Check if the API call was successful
		// success = response3["isSuccess"].(string)
		// if success == "0" {
		// 	ctx.JSON(http.StatusBadRequest, gin.H{"error": response3["errorMessage"]})
		// 	return
		// }
		isSuccess3, ok := response3["isSuccess"].(string)
		if !ok {
			ctx.JSON(http.StatusBadGateway, gin.H{"error": "PFMS GetTransferEntryRequestStatus unreachable"})
			return
		}
		if isSuccess3 == "0" {
			ctx.JSON(http.StatusBadRequest, gin.H{"error": response3["errorMessage"]})
			return
		}

		// Parse responseData JSON string
		var responseData ResponseData
		if err := json.Unmarshal([]byte(response3["responseData"].(string)), &responseData); err != nil {
			apierrors.HandleError(ctx, fmt.Errorf("failed to parse responseData: %w", err))
			log.Error(ctx, "Failed to parse responseData: %s", err.Error())
			return
		}

		// Extract Status and ErrorDescription from RequestStatus
		if len(responseData.RequestStatus) == 0 {
			apierrors.HandleError(ctx, fmt.Errorf("no RequestStatus found in responseData"))
			log.Error(ctx, "No RequestStatus found in responseData")
			return
		}

		// Assuming the first item in RequestStatus corresponds to the requested UniqueIdentifier
		rs := responseData.RequestStatus[0]
		request.Status = rs.Status
		var TENumber string
		if request.Status == "Success" {
			TENumber = rs.TENumber
		}
		if len(rs.Errors) > 0 {
			// Combine error messages as sentences
			var errorMessages []string
			for _, e := range rs.Errors {
				// Trim leading/trailing spaces and ensure the message is not empty
				message := strings.TrimSpace(e.ErrorMessage)
				if message != "" {
					errorMessages = append(errorMessages, message)
				}
			}
			if len(errorMessages) > 0 {
				// Capitalize the first letter of the first error message
				errorMessages[0] = strings.ToUpper(errorMessages[0][:1]) + errorMessages[0][1:]
				// Join with periods and ensure the final string ends with a period
				request.ErrorDescription = strings.Join(errorMessages, ". ") + "."
			}
		}

		err2 := uh.svc.UpdatePfmsSubmissionStatusRepo(ctx, &request, TENumber)
		if err2 != nil {
			apierrors.HandleDBError(ctx, err2)
			log.Error(ctx, "Update PFMS Updation Repo call failed: %s", err2.Error())
			return
		}
	}

	apiRsp := response.GetDDOlistResponse{
		StatusCodeAndMessage: port.UpdateSuccess,
	}
	log.Debug(ctx, "UpdateDdoMonthlyHandler response", apiRsp)
	handleSuccess(ctx, apiRsp)

}

type CashbookPfmsStatusRequest struct {
	OfficeId     int64  `form:"office-id" validate:"required,max=99999999"`
	CashbookDate string `form:"cashbook-date" validate:"required"`
}

// GetCashbookPfmsStatus godoc
//
//	@Summary		Get Cashbook Pfms Status
//	@Description	Get Cashbook Pfms Status based on office id and cashbook date input
//	@Tags			PAO GENERAL
//	@Accept			json
//	@Produce		json
//	@Param			office-id			query		int64									true	"office-id"
//	@Param			cashbook-date			query	string									true	"cashbook-date"
//
//	@Success		200					{object}	response.GetCashbookPfmsStatusResponse	"Cashbook Pfms Status retrieved successfully"
//	@Failure		400					{object}	apierrors.APIErrorResponse						"Validation error"
//	@Failure		401					{object}	apierrors.APIErrorResponse						"Unauthorized error"
//	@Failure		403					{object}	apierrors.APIErrorResponse						"Forbidden error"
//	@Failure		404					{object}	apierrors.APIErrorResponse						"Data not found error"
//	@Failure		409					{object}	apierrors.APIErrorResponse						"Data conflict error"
//	@Failure		500					{object}	apierrors.APIErrorResponse						"Internal server error"
//	@Router			/v1/pao-gen/cashbook/pfms-status [get]
func (uh *PaogenHandler) GetCashbookPfmsStatusHandler(ctx *gin.Context) {

	var req CashbookPfmsStatusRequest
	if err := ctx.ShouldBindQuery(&req); err != nil {
		apierrors.HandleBindingError(ctx, err)
		log.Error(ctx, "Binding failed for HoaRequest1: %s", err.Error())
		return
	}
	if err := validation.ValidateStruct(req); err != nil {
		apierrors.HandleValidationError(ctx, err)
		log.Error(ctx, "Validation failed for HoaRequest1: %s", err.Error())
		return
	}
	TransDate, err := time.Parse("2006-01-02", req.CashbookDate)
	request := domain.CashbookPfmsStatusRequest{
		OfficeId:     req.OfficeId,
		CashbookDate: TransDate,
	}
	u, err := uh.svc.GetCashbookPfmsStatusRepo(ctx, &request)
	if err != nil {
		if err.Error() == "no rows in result set" {
			apiRsp := response.GetCashbookPfmsStatusResponse{
				StatusCodeAndMessage: port.FetchSucess,
				Data:                 false,
			}
			log.Debug(ctx, "Check GetCashbookPfmsStatusResponse response: %s", apiRsp)
			handleSuccess(ctx, apiRsp)
			return
		}
		apierrors.HandleDBError(ctx, err)
		log.Error(ctx, "Get Hoa Only Repo call failed: %s", err.Error())
		return
	}

	apiRsp := response.GetCashbookPfmsStatusResponse{
		StatusCodeAndMessage: port.FetchSucess,
		Data:                 u.PfmsSubmissionFlag.Bool,
	}
	log.Debug(ctx, "Check GetCashbookPfmsStatusResponse response: %s", apiRsp)
	handleSuccess(ctx, apiRsp)

}
func (uh *PaogenHandler) CallAPI2(url string, method string, headers map[string]string, params interface{}) (map[string]interface{}, error) {
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
	case "POST", "PUT", "DELETE":
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

// Struct to hold each row
type RowData struct {
	Ddocode                                string
	OfficeID                               string
	Date                                   string
	SubaccountsCashbookReversion           bool
	SubaccountsCashbookReversionMessage    string
	PaoCashbookReversion                   bool
	PFMSUniqueID                           string
	SubaccountsCashbookResubmission        bool
	SubaccountsCashbookResubmissionMessage string
}
type Subaccounts_request1 struct {
	OfficeID     int    `json:"office_id"`
	BusinessDate string `json:"business_date"`
}
type Subaccounts_request2 struct {
	OfficeID     int    `json:"office_id"`
	BusinessDate string `json:"business_date"`
	ApprovedBy   string `json:"approved_by"`
}

// RevertResubmitCashbookHandler godoc
//
//	@Summary		Revert and Resubmit Cashbook entries
//	@Description	Revert and Resubmit Cashbook entries from uploaded Excel file
//	@Tags			PAO GENERAL
//	@Accept			json
//	@Produce		json
//	@Param			body	body		DdoMasterInput	true	"Ddo master request"
//	@Success		201		{object}	response.GetDDOlistResponse			"resource created successfully"
//	@Failure		400					{object}	apierrors.APIErrorResponse						"Validation error"
//	@Failure		401					{object}	apierrors.APIErrorResponse						"data retrieved successfully"
//	@Failure		403					{object}	apierrors.APIErrorResponse						"Forbidden error"
//	@Failure		404					{object}	apierrors.APIErrorResponse						"Data not found error"
//	@Failure		409					{object}	apierrors.APIErrorResponse						"Data conflict error"
//	@Failure		500					{object}	apierrors.APIErrorResponse						"Internal server error"
//	@Router			/v1/pao-gen/ddo-master [post]
func (uh *PaogenHandler) RevertResubmitCashbookHandler(ctx *gin.Context) {
	// Parse uploaded file
	file, _, err := ctx.Request.FormFile("file")
	if err != nil {
		apierrors.HandleBindingError(ctx, err)
		log.Error(ctx, "Failed to retrieve file: %s", err.Error())
		return
	}
	defer file.Close()

	// Read CSV directly from uploaded file
	reader := csv.NewReader(file)
	rows, err := reader.ReadAll()
	if err != nil {
		apierrors.HandleBindingError(ctx, err)
		log.Error(ctx, "Invalid CSV file: %s", err.Error())
		return
	}

	var results []RowData
	for i, row := range rows {
		if i == 0 { // skip header
			continue
		}
		if len(row) < 3 {
			continue
		}
		results = append(results, RowData{
			Ddocode:  row[0],
			OfficeID: row[1],
			Date:     row[2],
		})
	}
	var outputs []RowData

	// Process each row
	for _, r := range results {
		// defaults
		r.SubaccountsCashbookReversion = false
		r.SubaccountsCashbookReversionMessage = ""
		r.SubaccountsCashbookResubmission = false
		r.SubaccountsCashbookResubmissionMessage = ""
		r.PaoCashbookReversion = false
		r.PFMSUniqueID = ""

		// Convert date
		parsedDate, err := time.Parse("01/02/2006", r.Date)
		if err != nil {
			r.SubaccountsCashbookReversionMessage = "Invalid date format: " + r.Date
			outputs = append(outputs, r)
			continue
		}
		formattedDate := parsedDate.Format("2006-01-02")

		officeIDInt, err := strconv.Atoi(r.OfficeID)
		if err != nil {
			r.SubaccountsCashbookReversionMessage = "Invalid OfficeID: " + r.OfficeID
			outputs = append(outputs, r)
			continue
		}

		// API1: revert
		req1 := Subaccounts_request1{OfficeID: officeIDInt, BusinessDate: formattedDate}
		url_subaccounts1 := uh.cfg.GetString("urls.subaccountscall1")
		resp1, err := uh.CallAPI2(url_subaccounts1, "DELETE", map[string]string{"Content-Type": "application/json"}, req1)
		if err != nil {
			r.SubaccountsCashbookReversionMessage = "API1 failed: " + err.Error()
			outputs = append(outputs, r)
			continue
		}
		var apiResp struct {
			Message string `json:"message"`
			Status  string `json:"status"`
		}
		if b, err := json.Marshal(resp1); err == nil {
			_ = json.Unmarshal(b, &apiResp)
		}
		if apiResp.Status == "200" {
			r.SubaccountsCashbookReversion = true
		}
		r.SubaccountsCashbookReversionMessage = apiResp.Message

		// DB step
		r.PFMSUniqueID, err = uh.svc.RevertCashbookRepo(ctx, r.Ddocode, r.OfficeID, formattedDate)
		if err != nil {
			log.Warn(ctx, "RevertCashbook failed for %+v: %s", r, err.Error())
			r.PaoCashbookReversion = false
			if r.SubaccountsCashbookReversionMessage != "" {
				r.SubaccountsCashbookReversionMessage += " | "
			}
			r.SubaccountsCashbookReversionMessage += "DB error: " + err.Error()
			outputs = append(outputs, r)
			continue
		}
		r.PaoCashbookReversion = true

		// Validation API
		url_validate := uh.cfg.GetString("urls.subaccountscall3")
		validateURL := fmt.Sprintf("%s?cdate=%s&hocode=%d", url_validate, formattedDate, officeIDInt)
		respValidate, err := uh.CallAPI2(validateURL, "GET", nil, nil)
		if err != nil {
			r.SubaccountsCashbookResubmissionMessage = "Validation API failed: " + err.Error()
			outputs = append(outputs, r)
			continue
		}
		var vResp struct {
			Message      string   `json:"message"`
			MissingDates []string `json:"missing_dates"`
			OfficeID     int      `json:"office_id"`
			Valid        bool     `json:"valid"`
		}
		if b, err := json.Marshal(respValidate); err == nil {
			_ = json.Unmarshal(b, &vResp)
		}
		if !vResp.Valid {
			r.SubaccountsCashbookResubmissionMessage = vResp.Message
			outputs = append(outputs, r)
			continue
		}

		// API2: resubmit
		req2 := Subaccounts_request2{OfficeID: officeIDInt, BusinessDate: formattedDate, ApprovedBy: "99999999"}
		url_subaccounts2 := uh.cfg.GetString("urls.subaccountscall2")
		resp2, err := uh.CallAPI2(url_subaccounts2, "POST", map[string]string{"Content-Type": "application/json"}, req2)
		if err != nil {
			r.SubaccountsCashbookResubmissionMessage = "API2 failed: " + err.Error()
			outputs = append(outputs, r)
			continue
		}
		var aResp struct {
			StatusCode int         `json:"status_code"`
			Success    bool        `json:"success"`
			Message    string      `json:"message"`
			Data       interface{} `json:"data"`
			Error      *struct {
				Code    string `json:"code"`
				Message string `json:"message"`
			} `json:"error,omitempty"`
		}
		if b, err := json.Marshal(resp2); err == nil {
			_ = json.Unmarshal(b, &aResp)
		}
		if aResp.StatusCode == 201 {
			r.SubaccountsCashbookResubmission = true
			r.SubaccountsCashbookResubmissionMessage = aResp.Message
		} else if aResp.StatusCode == 500 && aResp.Error != nil {
			r.SubaccountsCashbookResubmissionMessage = aResp.Error.Message
		} else {
			r.SubaccountsCashbookResubmissionMessage = aResp.Message
		}

		outputs = append(outputs, r)
	}

	// === CSV OUTPUT with timestamped filename ===
	timestamp := time.Now().Format("20060102_150405")
	filename := fmt.Sprintf("output_%s.csv", timestamp)

	ctx.Header("Content-Type", "text/csv")
	ctx.Header("Content-Disposition", fmt.Sprintf("attachment; filename=%s", filename))

	writer := csv.NewWriter(ctx.Writer)
	defer writer.Flush()

	// Write header
	header := []string{
		"Ddocode",
		"OfficeID",
		"Date",
		"SubaccountsCashbookReversion",
		"SubaccountsCashbookReversionMessage",
		"PaoCashbookReversion",
		"PFMSUniqueID",
		"SubaccountsCashbookResubmission",
		"SubaccountsCashbookResubmissionMessage",
	}
	if err := writer.Write(header); err != nil {
		ctx.JSON(http.StatusInternalServerError, gin.H{"error": "Failed to write CSV header"})
		return
	}

	// Write rows
	for _, r := range outputs {
		row := []string{
			r.Ddocode,
			r.OfficeID,
			r.Date,
			strconv.FormatBool(r.SubaccountsCashbookReversion),
			r.SubaccountsCashbookReversionMessage,
			strconv.FormatBool(r.PaoCashbookReversion),
			r.PFMSUniqueID,
			strconv.FormatBool(r.SubaccountsCashbookResubmission),
			r.SubaccountsCashbookResubmissionMessage,
		}
		if err := writer.Write(row); err != nil {
			ctx.JSON(http.StatusInternalServerError, gin.H{"error": "Failed to write CSV row"})
			return
		}
	}
}

func (uh *PaogenHandler) RevertCashbookHandler(ctx *gin.Context) {
	// Parse uploaded file
	file, _, err := ctx.Request.FormFile("file")
	if err != nil {
		apierrors.HandleBindingError(ctx, err)
		log.Error(ctx, "Failed to retrieve file: %s", err.Error())
		return
	}
	defer file.Close()

	// Read CSV directly from uploaded file
	reader := csv.NewReader(file)
	rows, err := reader.ReadAll()
	if err != nil {
		apierrors.HandleBindingError(ctx, err)
		log.Error(ctx, "Invalid CSV file: %s", err.Error())
		return
	}

	var results []RowData
	for i, row := range rows {
		if i == 0 { // skip header
			continue
		}
		if len(row) < 3 {
			continue
		}
		results = append(results, RowData{
			Ddocode:  row[0],
			OfficeID: row[1],
			Date:     row[2],
		})
	}
	var outputs []RowData

	//Process each row
	for _, r := range results {
		//Calling first API to revert cashbook from subaccounts
		header := map[string]string{
			"Content-Type": "application/json",
		}
		url_subaccounts1 := uh.cfg.GetString("urls.subaccountscall1")
		method_subaccounts1 := "DELETE"

		// Convert MM/DD/YYYY -> YYYY-MM-DD
		parsedDate, err := time.Parse("01/02/2006", r.Date)
		if err != nil {
			log.Warn(ctx, "Invalid date format for row: %s", r.Date)
			continue // skip this row if date is invalid
		}
		formattedDate := parsedDate.Format("2006-01-02")

		officeIDInt, err := strconv.Atoi(r.OfficeID)
		if err != nil {
			log.Warn(ctx, "Invalid OfficeID format for row: %s", r.OfficeID)
			continue // skip this row if OfficeID is invalid
		}
		req1 := Subaccounts_request1{
			OfficeID:     officeIDInt,
			BusinessDate: formattedDate,
		}

		response, err := uh.CallAPI2(url_subaccounts1, method_subaccounts1, header, req1)
		if err != nil {
			ctx.JSON(http.StatusInternalServerError, gin.H{"error": err.Error()})
			continue
		}
		var apiResp struct {
			Message string `json:"message"`
			Status  string `json:"status"`
		}

		responseBytes, err := json.Marshal(response)
		if err != nil {
			ctx.JSON(http.StatusInternalServerError, gin.H{"error": "Failed to marshal API response"})
			continue
		}
		if err := json.Unmarshal(responseBytes, &apiResp); err != nil {
			ctx.JSON(http.StatusInternalServerError, gin.H{"error": "Invalid API response"})
			continue
		}
		if apiResp.Status == "200" {
			r.SubaccountsCashbookReversion = true
			r.SubaccountsCashbookReversionMessage = apiResp.Message
		} else {
			r.SubaccountsCashbookReversion = false
			r.SubaccountsCashbookReversionMessage = apiResp.Message
		}

		r.PFMSUniqueID, err = uh.svc.RevertCashbookRepo(ctx, r.Ddocode, r.OfficeID, formattedDate)
		r.PaoCashbookReversion = true
		if err != nil {
			log.Warn(ctx, "RevertCashbook failed for %+v: %s", r, err.Error())
			r.PaoCashbookReversion = false
			continue
		}
		outputs = append(outputs, r)
	}

	// // Generate Excel and return as download
	// fOut := excelize.NewFile()
	// sheet := "Results"
	// index, err := fOut.NewSheet(sheet)
	// if err != nil {
	// 	ctx.JSON(http.StatusInternalServerError, gin.H{"error": "Failed to create new sheet"})
	// 	return
	// }

	// // headers + rows loop (see Step 1 above)

	// fOut.SetActiveSheet(index)

	// // send as response
	// ctx.Header("Content-Type", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
	// ctx.Header("Content-Disposition", "attachment; filename=output.xlsx")
	// ctx.Header("File-Name", "output.xlsx")

	// if err := fOut.Write(ctx.Writer); err != nil {
	// 	ctx.JSON(http.StatusInternalServerError, gin.H{"error": "Failed to write Excel file"})
	// 	return
	// }

	// apiRsp := response.GetDDOlistResponse{
	// 	StatusCodeAndMessage: port.CreateSuccess,
	// }
	// log.Debug(ctx, "CreatePFMSVerificationHandler response", apiRsp)
	// handleCreateSuccess(ctx, outputs)

	// === CSV OUTPUT ===
	ctx.Header("Content-Type", "text/csv")
	ctx.Header("Content-Disposition", "attachment; filename=output.csv")

	writer := csv.NewWriter(ctx.Writer)
	defer writer.Flush()

	// Write header row
	header := []string{
		"Ddocode",
		"OfficeID",
		"Date",
		"SubaccountsCashbookReversion",
		"SubaccountsCashbookReversionMessage",
		"PaoCashbookReversion",
		"PFMSUniqueID",
		"SubaccountsCashbookResubmission",
		"SubaccountsCashbookResubmissionMessage",
	}
	if err := writer.Write(header); err != nil {
		ctx.JSON(http.StatusInternalServerError, gin.H{"error": "Failed to write CSV header"})
		return
	}

	// Write data rows
	for _, r := range outputs {
		row := []string{
			r.Ddocode,
			r.OfficeID,
			r.Date,
			strconv.FormatBool(r.SubaccountsCashbookReversion),
			r.SubaccountsCashbookReversionMessage,
			strconv.FormatBool(r.PaoCashbookReversion),
			r.PFMSUniqueID,
			strconv.FormatBool(r.SubaccountsCashbookResubmission),
			r.SubaccountsCashbookResubmissionMessage,
		}
		if err := writer.Write(row); err != nil {
			ctx.JSON(http.StatusInternalServerError, gin.H{"error": "Failed to write CSV row"})
			return
		}
	}
}

type CashbookReversionListRequest struct {
	DdoCode  string `form:"ddo-code" validate:"required,validateDdocode"`
	FromDate string `form:"from-date" validate:"required"`
	port.MetaDataRequest
}

// GetCashbookReversionList godoc
//
//	@Summary		Get Cashbook Reversion List
//	@Description	Get Cashbook Reversion List based on ddo code and cashbook date input
//	@Tags			PAO GENERAL
//	@Accept			json
//	@Produce		json
//	@Param			ddo-code			query		string									true	"ddo-code"
//	@Param			from-date			query	string									true	"from-date"
//
// @Param       skip    query       int     			false   		"Number of records to skip for pagination"
// @Param       limit   query       int     			false   		"Number of records to limit for pagination"
//
//	@Success		200					{object}	response.GetDDOlistResponse	"Cashbook Reversion List retrieved successfully"
//	@Failure		400					{object}	apierrors.APIErrorResponse						"Validation error"
//	@Failure		401					{object}	apierrors.APIErrorResponse						"Unauthorized error"
//	@Failure		403					{object}	apierrors.APIErrorResponse						"Forbidden error"
//	@Failure		404					{object}	apierrors.APIErrorResponse						"Data not found error"
//	@Failure		409					{object}	apierrors.APIErrorResponse						"Data conflict error"
//	@Failure		500					{object}	apierrors.APIErrorResponse						"Internal server error"
//	@Router			/v1/pao-gen/cashbook/reversion-list [get]
func (uh *PaogenHandler) GetCashbookReversionListHandler(ctx *gin.Context) {

	var req CashbookReversionListRequest
	if err := ctx.ShouldBindQuery(&req); err != nil {
		apierrors.HandleBindingError(ctx, err)
		log.Error(ctx, "Binding failed for HoaRequest1: %s", err.Error())
		return
	}
	if err := validation.ValidateStruct(req); err != nil {
		apierrors.HandleValidationError(ctx, err)
		log.Error(ctx, "Validation failed for HoaRequest1: %s", err.Error())
		return
	}
	TransDate, err := time.Parse("2006-01-02", req.FromDate)
	request := domain.CashbookReversionListRequest{
		DdoCode:  req.DdoCode,
		FromDate: TransDate,
	}
	if req.Limit == 0 {
		req.Limit = math.MaxInt32
	}
	u, err := uh.svc.GetCashbookReversionListRepo(ctx, &request, req.MetaDataRequest)
	if err != nil {
		apierrors.HandleDBError(ctx, err)
		log.Error(ctx, "GetCashbookReversionList Repo call failed: %s", err.Error())
		return
	}
	layoutIn := time.RFC3339
	layoutOut := "2006-01-02"

	for i := range u {
		if !u[i].Date.Valid {
			continue // skip null dates
		}

		t, err := time.Parse(layoutIn, u[i].Date.String)
		if err != nil {
			log.Error(ctx, "invalid date format for record %d: %v", i, err)
			continue
		}

		u[i].Date = null.StringFrom(t.Format(layoutOut))
	}

	rsp := response.NewGetDDOlistResponse(u)

	metadata := port.NewMetaDataResponse(0, 0, len(rsp))

	apiRsp := response.GetDDOlistResponse{
		StatusCodeAndMessage: port.ListSuccess,
		MetaDataResponse:     metadata,
		Data:                 rsp,
	}
	log.Debug(ctx, "GetCashbookReversionListHandler response", apiRsp)
	handleSuccess(ctx, apiRsp)

}

type CashbookRevertRequest struct {
	RequestOfficeID string `json:"request_office_id" binding:"required" validate:"required,max=99999999"`
	EmployeeID      string `json:"employee_id" binding:"required" validate:"required,employee_id"`
	Ddocode         string `json:"ddocode" binding:"required" validate:"required,validateDdocode"`
	FromDate        string `json:"from_date" binding:"required" validate:"required,date_yyyy_mm_dd"`
	Remark          string `json:"remark" binding:"required" validate:"required,max=500"`
}

// RevertCashbookPraoHandler godoc
//
//	@Summary		Revert Cashbook entries from PRAO
//	@Description	Revert Cashbook entries from PRAO
//	@Tags			PAO GENERAL
//	@Accept			json
//	@Produce		json
//	@Param			body	body		CashbookRevertRequest	true	"Cashbook reversion request"
//	@Success		200		{object}	response.GetDDOlistResponse			"resource updated successfully"
//	@Failure		400					{object}	apierrors.APIErrorResponse						"Validation error"
//	@Failure		401					{object}	apierrors.APIErrorResponse						"data retrieved successfully"
//	@Failure		403					{object}	apierrors.APIErrorResponse						"Forbidden error"
//	@Failure		404					{object}	apierrors.APIErrorResponse						"Data not found error"
//	@Failure		409					{object}	apierrors.APIErrorResponse						"Data conflict error"
//	@Failure		500					{object}	apierrors.APIErrorResponse						"Internal server error"
//	@Router			/v1/pao-gen/cashbook/reversion-prao [post]
func (uh *PaogenHandler) RevertCashbookPostHandler(ctx *gin.Context) {
	var req CashbookRevertRequest
	if err := ctx.ShouldBindJSON(&req); err != nil {
		apierrors.HandleBindingError(ctx, err)
		log.Error(ctx, "Invalid JSON input: %s", err.Error())
		return
	}

	// Step 1: Get DDO office ID
	ddoOfficeID, found, err := uh.svc.GetDDOOfficeID(ctx, req.Ddocode)
	if err != nil {
		log.Error(ctx, "Failed to get DDO office ID: %s", err.Error())
		ctx.JSON(http.StatusInternalServerError, gin.H{"error": "Failed to get DDO office ID"})
		return
	}
	if !found {
		ctx.JSON(http.StatusBadRequest, gin.H{"error": "DDO code not found"})
		return
	}
	officeIDInt := int(ddoOfficeID.Ddo_office_id.Int64)
	officeIDStr := strconv.Itoa(officeIDInt)
	log.Debug(ctx, "DDO office ID resolved — ddoCode: %s | officeIDInt: %d | officeIDStr: %s",
		req.Ddocode, officeIDInt, officeIDStr)

	// Step 2: Parse date
	parsedDate, err := time.Parse("2006-01-02", req.FromDate)
	if err != nil {
		ctx.JSON(http.StatusBadRequest, gin.H{"error": "Invalid date format. Use YYYY-MM-DD"})
		return
	}
	formattedDate := parsedDate.Format("2006-01-02")
	log.Debug(ctx, "Parsed date: %s", formattedDate)

	// Step 3: Fetch all dates BEFORE deletion
	allDates, err := uh.svc.GetAllDatesForReversionRepo(
		ctx, officeIDInt, req.Ddocode, formattedDate,
	)
	if err != nil {
		log.Error(ctx, "Failed to fetch reversion dates: %s", err.Error())
		ctx.JSON(http.StatusInternalServerError, gin.H{"error": "Failed to fetch reversion dates"})
		return
	}
	log.Debug(ctx, "Total dates fetched from pfms_main: %d", len(allDates))
	for _, d := range allDates {
		log.Debug(ctx, "  Date found: %s", d.Format("2006-01-02"))
	}

	if len(allDates) == 0 {
		ctx.JSON(http.StatusBadRequest, gin.H{"error": "No cashbook data found from selected date"})
		return
	}

	// Step 4: Check pfms_submission for each date
	reqEmpID, _ := strconv.Atoi(req.EmployeeID)
	reqOfficeID, _ := strconv.Atoi(req.RequestOfficeID)

	var reversionRows []domain.ReversionRow
	for _, date := range allDates {
		dateStr := date.Format("2006-01-02")
		pfmsUID, submissionStatus, teNumber, _, wasSubmitted, err :=
			uh.svc.GetPfmsSubmissionByDateRepo(ctx, req.Ddocode, date)
		if err != nil {
			log.Error(ctx, "Failed to check pfms_submission for date %s: %s", dateStr, err.Error())
			ctx.JSON(http.StatusInternalServerError, gin.H{
				"error": fmt.Sprintf("Failed to check PFMS submission for date %s", dateStr),
			})
			return
		}
		log.Debug(ctx, "Date: %s | wasSubmitted: %v | pfmsUID: %s | status: %s | teNumber: %s",
			dateStr, wasSubmitted, pfmsUID, submissionStatus, teNumber)

		row := domain.ReversionRow{
			RequestOfficeID:   reqOfficeID,
			RequestEmployeeID: reqEmpID,
			DdoCode:           req.Ddocode,
			FromDate:          parsedDate,
			Remark:            req.Remark,
			BusinessDate:      date,
		}

		if wasSubmitted {
			row.OriginalPfmsUID = pfmsUID
			row.OriginalSubmissionStatus = submissionStatus
			row.OriginalTeNumber = teNumber

			if submissionStatus == "Success" || submissionStatus == "Pending" {
				row.PfmsReversalType = "with_pfms"
				if submissionStatus == "Success" && teNumber != "" {
					row.PfmsNegativePosted = "NO"
				} else {
					row.PfmsNegativePosted = "WAIT"
				}
			} else {
				row.PfmsReversalType = "local_only"
				row.PfmsNegativePosted = ""
			}
		} else {
			row.PfmsReversalType = "local_only"
			row.PfmsNegativePosted = ""
		}

		reversionRows = append(reversionRows, row)
		log.Debug(ctx, "Row built — date: %s | type: %s | posted: %s",
			dateStr, row.PfmsReversalType, row.PfmsNegativePosted)
	}

	log.Debug(ctx, "Total reversion rows to insert: %d", len(reversionRows))

	// Step 5: Call subaccounts reversion API
	response1, err := uh.CallAPI2(
		uh.cfg.GetString("urls.subaccountscall1"),
		"DELETE",
		map[string]string{"Content-Type": "application/json"},
		Subaccounts_request1{
			OfficeID:     officeIDInt,
			BusinessDate: formattedDate,
		},
	)
	if err != nil {
		log.Error(ctx, "Subaccounts reversion API failed: %s", err.Error())
		ctx.JSON(http.StatusInternalServerError, gin.H{"error": "Failed to call subaccounts API"})
		return
	}
	var subResp struct {
		Message string `json:"message"`
		Status  string `json:"status"`
	}
	respBytes, _ := json.Marshal(response1)
	json.Unmarshal(respBytes, &subResp)
	log.Debug(ctx, "Subaccounts response — status: %s | message: %s",
		subResp.Status, subResp.Message)
	if subResp.Status != "200" {
		appError := apierrors.NewAppError(
			"Failed to revert cashbook in subaccounts", "409",
			errors.New("failed to revert cashbook in subaccounts"),
		)
		ctx.JSON(http.StatusConflict,
			apierrors.NewAPIErrorResponse(http.StatusConflict, "Conflict", appError))
		return
	}

	// Step 6: Delete local tables
	dbDelStatus := "SUCCESS"
	if _, err := uh.svc.RevertCashbookRepo(
		ctx, req.Ddocode, officeIDStr, formattedDate,
	); err != nil {
		log.Error(ctx, "Local cashbook reversion failed: %s", err.Error())
		dbDelStatus = "FAILED"
	}
	log.Debug(ctx, "DB deletion status: %s", dbDelStatus)

	// Apply db_deletion_status to all rows
	for i := range reversionRows {
		reversionRows[i].DbDeletionStatus = dbDelStatus
	}

	// Step 7: Bulk insert into pao.reversion
	log.Debug(ctx, "Attempting bulk insert of %d rows into pao.reversion", len(reversionRows))
	if err := uh.svc.BulkInsertReversionRepo(ctx, reversionRows); err != nil {
		log.Error(ctx, "BulkInsertReversionRepo failed: %s", err.Error())
		ctx.JSON(http.StatusInternalServerError, gin.H{"error": "Failed to store reversal records"})
		return
	}
	log.Debug(ctx, "BulkInsertReversionRepo completed successfully")

	apiRsp := response.GetDDOlistResponse{
		StatusCodeAndMessage: port.UpdateSuccess,
	}
	log.Debug(ctx, "RevertCashbookPostHandler completed successfully")
	handleSuccess(ctx, apiRsp)
}

func (uh *PaogenHandler) RevertCashbookPostHandlerold(ctx *gin.Context) {
	var req CashbookRevertRequest

	// Parse JSON input
	if err := ctx.ShouldBindJSON(&req); err != nil {
		apierrors.HandleBindingError(ctx, err)
		log.Error(ctx, "Invalid JSON input: %s", err.Error())
		return
	}

	// Step 1: Get DDO Office ID using DDO code
	ddoOfficeID, b, err := uh.svc.GetDDOOfficeID(ctx, req.Ddocode)
	if err != nil {
		log.Error(ctx, "Failed to get DDO office ID for %s: %s", req.Ddocode, err.Error())
		ctx.JSON(http.StatusInternalServerError, gin.H{"error": "Failed to get DDO office ID"})
		return
	}
	var officeIDInt int
	if !b {
		ctx.JSON(http.StatusBadRequest, gin.H{"error": "DDO code not found"})
		return
	}
	if b {
		officeIDInt = int(ddoOfficeID.Ddo_office_id.Int64)
	}

	// Step 2: Parse and format date
	parsedDate, err := time.Parse("2006-01-02", req.FromDate)
	if err != nil {
		log.Warn(ctx, "Invalid date format: %s", req.FromDate)
		ctx.JSON(http.StatusBadRequest, gin.H{"error": "Invalid date format. Use YYYY-MM-DD"})
		return
	}
	formattedDate := parsedDate.Format("2006-01-02")

	// Step 3: Call subaccounts reversion API
	header := map[string]string{
		"Content-Type": "application/json",
	}
	urlSubaccount := uh.cfg.GetString("urls.subaccountscall1")
	method := "DELETE"

	req1 := Subaccounts_request1{
		OfficeID:     officeIDInt,
		BusinessDate: formattedDate,
	}

	response1, err := uh.CallAPI2(urlSubaccount, method, header, req1)
	if err != nil {
		log.Error(ctx, "Subaccounts reversion API failed: %s", err.Error())
		ctx.JSON(http.StatusInternalServerError, gin.H{"error": "Failed to call subaccounts API"})
		return
	}
	var apiResp struct {
		Message string `json:"message"`
		Status  string `json:"status"`
	}

	responseBytes, err := json.Marshal(response1)
	if err != nil {
		log.Error(ctx, "Failed to marshal API response: %s", err.Error())
		ctx.JSON(http.StatusInternalServerError, gin.H{"error": "Failed to marshal API response"})
		return
	}
	if err := json.Unmarshal(responseBytes, &apiResp); err != nil {
		log.Error(ctx, "Failed to marshal API response: %s", err.Error())
		ctx.JSON(http.StatusInternalServerError, gin.H{"error": "Invalid API response"})
		return
	}
	if apiResp.Status != "200" {
		appError := apierrors.NewAppError(
			"Failed to revert cashbook in subaccounts",
			"409",
			errors.New("Failed to revert cashbook in subaccounts"),
		)
		apiErrorResponse := apierrors.NewAPIErrorResponse(http.StatusConflict, "Conflict", appError)
		ctx.JSON(apiErrorResponse.StatusCode, apiErrorResponse)
		return
	}
	// Step 4: Perform PAO cashbook reversion
	_, err2 := uh.svc.RevertCashbookRepo(ctx, req.Ddocode, strconv.Itoa(officeIDInt), formattedDate)
	if err2 != nil {
		log.Warn(ctx, "PAO cashbook reversion failed: %s", err2.Error())
	}

	// Step 5: Store reversal request in DB
	err = uh.svc.StoreReversalRequestRepo(ctx, req.RequestOfficeID, req.EmployeeID, req.Ddocode, req.FromDate, req.Remark)
	if err != nil {
		log.Error(ctx, "Failed to store reversal request: %s", err.Error())
		ctx.JSON(http.StatusInternalServerError, gin.H{"error": "Failed to store reversal request"})
		return
	}

	apiRspqe := response.GetDDOlistResponse{
		StatusCodeAndMessage: port.UpdateSuccess,
	}
	log.Debug(ctx, "UpdateDdoMonthlyHandler response", apiRspqe)
	handleSuccess(ctx, apiRspqe)
}

// GetConsolidatedCashAccountHandler godoc
//
//	@Summary		Get consolidated cash account for all DDOs under a PAO
//	@Description	Aggregates all account codes from kafka_cash_account across all offices under the given PAO for the specified period
//	@Tags			PAO GENERAL
//	@Accept			json
//	@Produce		json
//	@Param			pao-code	path		string	true	"PAO Code"
//	@Param			period		query		string	true	"Period (e.g. 082025)"
//	@Success		200		{object}	response.GetConsolidatedCashAccountResponse	"data retrieved successfully"
//	@Failure		400		{object}	apierrors.APIErrorResponse					"Validation error"
//	@Failure		401		{object}	apierrors.APIErrorResponse					"Unauthorized error"
//	@Failure		403		{object}	apierrors.APIErrorResponse					"Forbidden error"
//	@Failure		404		{object}	apierrors.APIErrorResponse					"Data not found error"
//	@Failure		500		{object}	apierrors.APIErrorResponse					"Internal server error"
//	@Router			/v1/pao-gen/pao/{pao-code}/cashaccount/consacc [get]
func (uh *PaogenHandler) GetConsolidatedCashAccountHandler(ctx *gin.Context) {

	var req DdoListRequestMonthly
	if err := ctx.ShouldBindUri(&req); err != nil {
		apierrors.HandleBindingError(ctx, err)
		log.Error(ctx, "Binding failed for ConsolidatedCashAccount: %s", err.Error())
		return
	}
	if err := ctx.ShouldBindQuery(&req); err != nil {
		apierrors.HandleBindingError(ctx, err)
		log.Error(ctx, "Binding failed for ConsolidatedCashAccount: %s", err.Error())
		return
	}
	if err := validation.ValidateStruct(req); err != nil {
		apierrors.HandleValidationError(ctx, err)
		log.Error(ctx, "Validation failed for ConsolidatedCashAccount: %s", err.Error())
		return
	}

	request := domain.DdoListRequestMonthly{
		PaoCode: req.PaoCode,
		Period:  req.Period,
	}

	result, err := uh.svc.GetConsolidatedCashAccountRepo(ctx, &request)
	if err != nil {
		apierrors.HandleDBError(ctx, err)
		log.Error(ctx, "GetConsolidatedCashAccountRepo call failed: %s", err.Error())
		return
	}

	// Empty result — return empty hoa_details gracefully
	if len(result.HoaDetails) == 0 {
		handleSuccess(ctx, response.GetConsolidatedCashAccountResponse{
			StatusCodeAndMessage: port.FetchSucess,
			Data: response.ConsolidatedCashAccountData{
				PaoOfficeId:       result.PaoOfficeId,
				PaoName:           result.PaoName,
				CashAccountPeriod: result.CashAccountPeriod,
				HoaDetails:        []response.ConsolidatedHoaDetailResponse{},
			},
		})
		return
	}

	apiRsp := response.NewGetConsolidatedCashAccountResponse(result)
	log.Debug(ctx, "GetConsolidatedCashAccountHandler response", apiRsp)
	handleSuccess(ctx, apiRsp)
}

// func (uh *PaogenHandler) GetConsolidatedCashAccountHandler(ctx *gin.Context) {

// 	var req DdoListRequestMonthly
// 	if err := ctx.ShouldBindUri(&req); err != nil {
// 		apierrors.HandleBindingError(ctx, err)
// 		log.Error(ctx, "Binding failed for ConsolidatedCashAccount: %s", err.Error())
// 		return
// 	}
// 	if err := ctx.ShouldBindQuery(&req); err != nil {
// 		apierrors.HandleBindingError(ctx, err)
// 		log.Error(ctx, "Binding failed for ConsolidatedCashAccount: %s", err.Error())
// 		return
// 	}
// 	if err := validation.ValidateStruct(req); err != nil {
// 		apierrors.HandleValidationError(ctx, err)
// 		log.Error(ctx, "Validation failed for ConsolidatedCashAccount: %s", err.Error())
// 		return
// 	}

// 	request := domain.DdoListRequestMonthly{
// 		PaoCode: req.PaoCode,
// 		Period:  req.Period,
// 	}

// 	result, err := uh.svc.GetConsolidatedCashAccountRepo(ctx, &request)
// 	if err != nil {
// 		apierrors.HandleDBError(ctx, err)
// 		log.Error(ctx, "GetConsolidatedCashAccountRepo call failed: %s", err.Error())
// 		return
// 	}

// 	apiRsp := response.NewGetConsolidatedCashAccountResponse(result)
// 	log.Debug(ctx, "GetConsolidatedCashAccountHandler response", apiRsp)
// 	handleSuccess(ctx, apiRsp)
// }

// changes done on 23-03-2026 for revert cashbook and post -ve entry to pfms

type ReversionPendingRequest struct {
	PaoCode string `form:"pao_code" binding:"required" validate:"required"`
}

func (uh *PaogenHandler) SubmitPayloadToPfms(
	ctx *gin.Context,
	payload domain.Payload,
) (map[string]interface{}, error) {

	username := uh.cfg.GetString("pfms.username")
	requestsource := uh.cfg.GetString("pfms.requestsource")
	password := uh.cfg.GetString("pfms.password")
	baseurl := uh.cfg.GetString("pfms.baseurl")

	header := map[string]string{"Content-Type": "application/json"}

	// Step 1: GetAuthCode
	startTime := time.Now()
	resp1, err := uh.CallAPI(
		baseurl+"/GetAuthCode", "POST", header,
		map[string]interface{}{
			"UserName":      username,
			"RequestSource": requestsource,
		},
	)
	log.Debug(ctx, "GetAuthCode latency: %d ms", time.Since(startTime).Milliseconds())
	if err != nil {
		return nil, fmt.Errorf("GetAuthCode failed: %w", err)
	}
	if resp1["IsSuccess"].(string) == "0" {
		return nil, fmt.Errorf("GetAuthCode rejected: %v", resp1["ErrorMessage"])
	}
	authCode := resp1["AuthCode"].(string)

	// Step 2: Login
	startTime = time.Now()
	resp2, err := uh.CallAPI(
		baseurl+"/LogIn", "POST", header,
		map[string]interface{}{
			"userName": username,
			"password": password + authCode,
		},
	)
	log.Debug(ctx, "Login latency: %d ms", time.Since(startTime).Milliseconds())
	if err != nil {
		return nil, fmt.Errorf("Login failed: %w", err)
	}
	if resp2["isSuccess"].(string) == "0" {
		return nil, fmt.Errorf("Login rejected: %v", resp2["errorMessage"])
	}
	accessToken := resp2["accessToken"].(string)

	// Step 3: Submit payload
	startTime = time.Now()
	resp3, err := uh.CallAPI2(
		baseurl+"/Budget/ReceiveTransferEntryData",
		"POST",
		map[string]string{
			"Content-Type":  "application/json",
			"Authorization": "Bearer " + accessToken,
		},
		payload,
	)
	log.Debug(ctx, "ReceiveTransferEntryData latency: %d ms", time.Since(startTime).Milliseconds())
	if err != nil {
		return nil, fmt.Errorf("ReceiveTransferEntryData failed: %w", err)
	}

	return resp3, nil
}

func (uh *PaogenHandler) GetReversionRecordsHandler1(ctx *gin.Context) {
	ddoCode := ctx.Query("ddo_code")
	fromDate := ctx.Query("from_date")

	if ddoCode == "" || fromDate == "" {
		ctx.JSON(http.StatusBadRequest, gin.H{
			"error": "ddo_code and from_date are required",
		})
		return
	}

	// Validate date format
	if _, err := time.Parse("2006-01-02", fromDate); err != nil {
		ctx.JSON(http.StatusBadRequest, gin.H{
			"error": "Invalid from_date format. Use YYYY-MM-DD",
		})
		return
	}

	records, err := uh.svc.GetReversionRecordsRepo(ctx, ddoCode, fromDate)
	if err != nil {
		log.Error(ctx, "GetReversionRecordsRepo failed: %s", err.Error())
		ctx.JSON(http.StatusInternalServerError, gin.H{
			"error": "Failed to fetch reversion records",
		})
		return
	}
	if len(records) == 0 {
		ctx.JSON(http.StatusNotFound, gin.H{
			"error": "No reversion records found for given DDO and date",
		})
		return
	}

	ctx.JSON(http.StatusOK, gin.H{
		"data": records,
	})
}

// PostNegativeEntryRequest — used for POST API
type PostNegativeEntryRequest struct {
	OriginalPfmsUID string `json:"original_pfms_uid" binding:"required"`
}

// PostNegativeEntryHandler godoc
//
//	@Summary		Post Negative Entry to PFMS
//	@Description	Post a negative TE entry to PFMS for a reverted cashbook submission
//	@Tags			PAO GENERAL
//	@Accept			json
//	@Produce		json
//	@Param			request	body		handler.PostNegativeEntryRequest					true	"Post Negative Entry Request"
//	@Success		200		{object}	response.GetPostNegativeEntryResponse				"Negative entry posted successfully"
//	@Failure		400		{object}	apierrors.APIErrorResponse							"Validation error"
//	@Failure		401		{object}	apierrors.APIErrorResponse							"Unauthorized error"
//	@Failure		403		{object}	apierrors.APIErrorResponse							"Forbidden error"
//	@Failure		404		{object}	apierrors.APIErrorResponse							"Data not found error"
//	@Failure		409		{object}	apierrors.APIErrorResponse							"Already posted error"
//	@Failure		422		{object}	apierrors.APIErrorResponse							"TE number not yet received"
//	@Failure		500		{object}	apierrors.APIErrorResponse							"Internal server error"
//	@Failure		502		{object}	apierrors.APIErrorResponse							"PFMS API error"
//	@Router			/v1/pao-gen/cashbook/post-negative-entry [post]
func (uh *PaogenHandler) PostNegativeEntryHandler(ctx *gin.Context) {
	var req PostNegativeEntryRequest
	if err := ctx.ShouldBindJSON(&req); err != nil {
		apierrors.HandleBindingError(ctx, err)
		log.Error(ctx, "Binding failed for PostNegativeEntryRequest: %s", err.Error())
		return
	}
	if err := validation.ValidateStruct(req); err != nil {
		apierrors.HandleValidationError(ctx, err)
		log.Error(ctx, "Validation failed for PostNegativeEntryRequest: %s", err.Error())
		return
	}

	// Guard 1: Fetch reversion row
	revRow, found, err := uh.svc.GetReversionByPfmsUIDRepo(ctx, req.OriginalPfmsUID)
	if err != nil {
		log.Error(ctx, "GetReversionByPfmsUIDRepo failed: %s", err.Error())
		ctx.JSON(http.StatusInternalServerError, gin.H{"error": "Failed to fetch reversion record"})
		return
	}
	if !found {
		ctx.JSON(http.StatusNotFound, gin.H{
			"error": "No reversion record found for this PFMS UID",
		})
		return
	}

	// Guard 2: Must be with_pfms
	if revRow.PfmsReversalType != "with_pfms" {
		ctx.JSON(http.StatusBadRequest, gin.H{
			"error": "This entry has no PFMS submission. Negative entry not applicable.",
		})
		return
	}

	// Guard 3: Must not already be posted
	if revRow.PfmsNegativePosted == "YES" {
		ctx.JSON(http.StatusConflict, gin.H{
			"error":        "Negative entry already posted for this date",
			"reversal_uid": revRow.ReversalPfmsUID,
		})
		return
	}

	// Guard 4: Fetch full payload from pfms_submission
	pfmsPayload, _, originalCbReqs, liveFound, err :=
		uh.svc.GetPfmsSubmissionFullByUIDRepo(ctx, req.OriginalPfmsUID)
	if err != nil {
		log.Error(ctx, "GetPfmsSubmissionFullByUIDRepo failed: %s", err.Error())
		ctx.JSON(http.StatusInternalServerError, gin.H{"error": "Failed to fetch submission data"})
		return
	}
	if !liveFound {
		ctx.JSON(http.StatusNotFound, gin.H{"error": "Original PFMS submission not found"})
		return
	}

	// Guard 5: Validate te_number live
	liveTeNumber, liveStatus, err := uh.svc.GetTeNumberByUIDRepo(ctx, req.OriginalPfmsUID)
	if err != nil {
		log.Error(ctx, "GetTeNumberByUIDRepo failed: %s", err.Error())
		ctx.JSON(http.StatusInternalServerError, gin.H{"error": "Failed to validate te_number"})
		return
	}
	if liveStatus != "Success" || liveTeNumber == "" {
		ctx.JSON(http.StatusUnprocessableEntity, gin.H{
			"error": "PFMS te_number not yet received. Please wait.",
		})
		return
	}

	// Derive financial year
	businessDate := revRow.BusinessDate
	finYear := strconv.Itoa(businessDate.Year() + 1)
	if businessDate.Month() < time.April {
		finYear = strconv.Itoa(businessDate.Year())
	}

	// Get pao_code from cb_request
	paoCode := ""
	if len(originalCbReqs) > 0 {
		paoCode = originalCbReqs[0].PaoCode
	}
	if paoCode == "" {
		log.Error(ctx, "PAO code missing in submission data for UID: %s", req.OriginalPfmsUID)
		ctx.JSON(http.StatusInternalServerError, gin.H{
			"error": "PAO code missing in submission data",
		})
		return
	}

	// Build negating payload
	reversalPayload, reversalUID := BuildReversalPayload(
		pfmsPayload,
		paoCode,
		finYear,
		businessDate.Format("2006-01-02"),
	)

	// Submit to PFMS
	pfmsResp, err := uh.SubmitPayloadToPfms(ctx, reversalPayload)
	if err != nil {
		log.Error(ctx, "PFMS submission failed: %s", err.Error())
		ctx.JSON(http.StatusBadGateway, gin.H{
			"error": "Failed to post negative entry to PFMS",
		})
		return
	}
	if success, _ := pfmsResp["isSuccess"].(string); success == "0" {
		errMsg := fmt.Sprintf("%v", pfmsResp["errorMessage"])
		log.Error(ctx, "PFMS rejected negative entry: %s", errMsg)
		ctx.JSON(http.StatusBadGateway, gin.H{
			"error": "PFMS rejected negative entry: " + errMsg,
		})
		return
	}
	log.Debug(ctx, "PFMS accepted negative entry — UID: %s", reversalUID)

	// ← FIXED: use context.Background() for all DB ops after PFMS call
	auditCtx, auditCancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer auditCancel()

	// Update reversion row
	if err := uh.svc.UpdateReversionAfterPfmsRepo(
		auditCtx, // ← FIXED
		req.OriginalPfmsUID,
		reversalUID,
	); err != nil {
		log.Error(ctx, "Failed to update reversion row: %s", err.Error())
	}

	// Insert cb_reversal into pfms_submission
	if err := uh.svc.InsertPfmsSubmissionNew(
		auditCtx, // ← FIXED
		reversalUID,
		"cb_reversal",
		originalCbReqs,
		domain.TransferEntryDetail{},
		businessDate.Format("2006-01-02"),
		time.Now(),
		reversalPayload,
		"Pending",
		"",
		req.OriginalPfmsUID,
	); err != nil {
		log.Error(ctx, "Failed to insert cb_reversal into pfms_submission: %v", err)
	}

	rsp := response.NewPostNegativeEntryResponse(
		reversalUID,
		businessDate.Format("2006-01-02"),
	)

	apiRsp := response.GetPostNegativeEntryResponse{
		StatusCodeAndMessage: port.InsertSuccess,
		Data:                 rsp,
	}
	log.Debug(ctx, "PostNegativeEntryHandler response", apiRsp)
	handleSuccess(ctx, apiRsp)
}

func (uh *PaogenHandler) PostNegativeEntryHandler1904(ctx *gin.Context) {
	var req PostNegativeEntryRequest
	if err := ctx.ShouldBindJSON(&req); err != nil {
		apierrors.HandleBindingError(ctx, err)
		log.Error(ctx, "Binding failed for PostNegativeEntryRequest: %s", err.Error())
		return
	}
	if err := validation.ValidateStruct(req); err != nil {
		apierrors.HandleValidationError(ctx, err)
		log.Error(ctx, "Validation failed for PostNegativeEntryRequest: %s", err.Error())
		return
	}

	// Guard 1: Fetch reversion row
	revRow, found, err := uh.svc.GetReversionByPfmsUIDRepo(ctx, req.OriginalPfmsUID)
	if err != nil {
		log.Error(ctx, "GetReversionByPfmsUIDRepo failed: %s", err.Error())
		ctx.JSON(http.StatusInternalServerError, gin.H{"error": "Failed to fetch reversion record"})
		return
	}
	if !found {
		ctx.JSON(http.StatusNotFound, gin.H{
			"error": "No reversion record found for this PFMS UID",
		})
		return
	}

	// Guard 2: Must be with_pfms
	if revRow.PfmsReversalType != "with_pfms" {
		ctx.JSON(http.StatusBadRequest, gin.H{
			"error": "This entry has no PFMS submission. Negative entry not applicable.",
		})
		return
	}

	// Guard 3: Must not already be posted
	if revRow.PfmsNegativePosted == "YES" {
		ctx.JSON(http.StatusConflict, gin.H{
			"error":        "Negative entry already posted for this date",
			"reversal_uid": revRow.ReversalPfmsUID,
		})
		return
	}

	// Guard 4: Fetch full payload from pfms_submission
	pfmsPayload, _, originalCbReqs, liveFound, err :=
		uh.svc.GetPfmsSubmissionFullByUIDRepo(ctx, req.OriginalPfmsUID)
	if err != nil {
		log.Error(ctx, "GetPfmsSubmissionFullByUIDRepo failed: %s", err.Error())
		ctx.JSON(http.StatusInternalServerError, gin.H{"error": "Failed to fetch submission data"})
		return
	}
	if !liveFound {
		ctx.JSON(http.StatusNotFound, gin.H{"error": "Original PFMS submission not found"})
		return
	}

	// Guard 5: Validate te_number live — PK lookup, negligible latency
	liveTeNumber, liveStatus, err := uh.svc.GetTeNumberByUIDRepo(ctx, req.OriginalPfmsUID)
	if err != nil {
		log.Error(ctx, "GetTeNumberByUIDRepo failed: %s", err.Error())
		ctx.JSON(http.StatusInternalServerError, gin.H{"error": "Failed to validate te_number"})
		return
	}
	if liveStatus != "Success" || liveTeNumber == "" {
		ctx.JSON(http.StatusUnprocessableEntity, gin.H{
			"error": "PFMS te_number not yet received. Please wait.",
		})
		return
	}

	// Derive financial year
	businessDate := revRow.BusinessDate
	finYear := strconv.Itoa(businessDate.Year() + 1)
	if businessDate.Month() < time.April {
		finYear = strconv.Itoa(businessDate.Year())
	}

	// Get pao_code from cb_request
	paoCode := ""
	if len(originalCbReqs) > 0 {
		paoCode = originalCbReqs[0].PaoCode
	}
	if paoCode == "" {
		log.Error(ctx, "PAO code missing in submission data for UID: %s", req.OriginalPfmsUID)
		ctx.JSON(http.StatusInternalServerError, gin.H{
			"error": "PAO code missing in submission data",
		})
		return
	}

	// Build negating payload
	reversalPayload, reversalUID := BuildReversalPayload(
		pfmsPayload,
		paoCode,
		finYear,
		businessDate.Format("2006-01-02"),
	)

	// Submit to PFMS
	pfmsResp, err := uh.SubmitPayloadToPfms(ctx, reversalPayload)
	if err != nil {
		log.Error(ctx, "PFMS submission failed: %s", err.Error())
		ctx.JSON(http.StatusBadGateway, gin.H{
			"error": "Failed to post negative entry to PFMS",
		})
		return
	}
	if success, _ := pfmsResp["isSuccess"].(string); success == "0" {
		errMsg := fmt.Sprintf("%v", pfmsResp["errorMessage"])
		log.Error(ctx, "PFMS rejected negative entry: %s", errMsg)
		ctx.JSON(http.StatusBadGateway, gin.H{
			"error": "PFMS rejected negative entry: " + errMsg,
		})
		return
	}
	log.Debug(ctx, "PFMS accepted negative entry — UID: %s", reversalUID)

	// Update reversion row → pfms_negative_posted = 'YES'
	if err := uh.svc.UpdateReversionAfterPfmsRepo(
		ctx, req.OriginalPfmsUID, reversalUID,
	); err != nil {
		// Non-blocking — PFMS already accepted
		log.Error(ctx, "Failed to update reversion row: %s", err.Error())
	}

	// Insert cb_reversal into pfms_submission
	if err := uh.svc.InsertPfmsSubmissionNew(
		ctx,
		reversalUID,
		"cb_reversal",
		originalCbReqs,
		domain.TransferEntryDetail{},
		businessDate.Format("2006-01-02"),
		time.Now(),
		reversalPayload,
		"Pending",
		"",
		req.OriginalPfmsUID,
	); err != nil {
		// Non-blocking — PFMS already accepted
		log.Error(ctx, "Failed to insert cb_reversal into pfms_submission: %v", err)
	}

	rsp := response.NewPostNegativeEntryResponse(
		reversalUID,
		businessDate.Format("2006-01-02"),
	)

	apiRsp := response.GetPostNegativeEntryResponse{
		StatusCodeAndMessage: port.InsertSuccess,
		Data:                 rsp,
	}
	log.Debug(ctx, "PostNegativeEntryHandler response", apiRsp)
	handleSuccess(ctx, apiRsp)
}

// GetReversionPendingHandler godoc
//
//	@Summary		Get Reversion Pending List
//	@Description	Get all pending PFMS negative entry records for a PAO code
//	@Tags			PAO GENERAL
//	@Accept			json
//	@Produce		json
//	@Param			pao_code	query		string								true	"PAO Code"
//	@Success		200			{object}	response.GetReversionPendingResponse	"Reversion pending list retrieved successfully"
//	@Failure		400			{object}	apierrors.APIErrorResponse			"Validation error"
//	@Failure		401			{object}	apierrors.APIErrorResponse			"Unauthorized error"
//	@Failure		403			{object}	apierrors.APIErrorResponse			"Forbidden error"
//	@Failure		404			{object}	apierrors.APIErrorResponse			"Data not found error"
//	@Failure		500			{object}	apierrors.APIErrorResponse			"Internal server error"
//	@Router			/v1/pao-gen/cashbook/reversion-pending [get]
func (uh *PaogenHandler) GetReversionPendingHandler(ctx *gin.Context) {
	var req ReversionPendingRequest
	if err := ctx.ShouldBindQuery(&req); err != nil {
		apierrors.HandleBindingError(ctx, err)
		log.Error(ctx, "Binding failed for ReversionPendingRequest: %s", err.Error())
		return
	}
	if err := validation.ValidateStruct(req); err != nil {
		apierrors.HandleValidationError(ctx, err)
		log.Error(ctx, "Validation failed for ReversionPendingRequest: %s", err.Error())
		return
	}

	records, err := uh.svc.GetReversionPendingRepo(ctx, req.PaoCode)
	if err != nil {
		apierrors.HandleDBError(ctx, err)
		log.Error(ctx, "GetReversionPendingRepo failed: %s", err.Error())
		return
	}

	rsp := response.NewGetReversionPendingResponse(records)

	metadata := port.NewMetaDataResponse(0, 0, len(rsp))

	apiRsp := response.GetReversionPendingResponse{
		StatusCodeAndMessage: port.ListSuccess,
		MetaDataResponse:     metadata,
		Data:                 rsp,
	}
	log.Debug(ctx, "GetReversionPendingHandler response", apiRsp)
	handleSuccess(ctx, apiRsp)
}

// TestPostNegativeEntryHandler godoc
//
//	@Summary		Test Post Negative Entry to PFMS
//	@Description	Test endpoint to post negative TE directly using pfms_unique_id. No reversion table involved.
//	@Tags			PAO GENERAL
//	@Accept			json
//	@Produce		json
//	@Param			request	body		handler.PostNegativeEntryRequest		true	"Post Negative Entry Request"
//	@Success		200		{object}	response.GetPostNegativeEntryResponse	"Negative entry posted successfully"
//	@Failure		400		{object}	apierrors.APIErrorResponse				"Validation error"
//	@Failure		404		{object}	apierrors.APIErrorResponse				"Not found"
//	@Failure		422		{object}	apierrors.APIErrorResponse				"TE number not ready"
//	@Failure		500		{object}	apierrors.APIErrorResponse				"Internal server error"
//	@Failure		502		{object}	apierrors.APIErrorResponse				"PFMS API error"
//	@Router			/v1/pao-gen/cashbook/test-post-negative-entry [post]
func (uh *PaogenHandler) TestPostNegativeEntryHandler(ctx *gin.Context) {
	var req PostNegativeEntryRequest
	if err := ctx.ShouldBindJSON(&req); err != nil {
		apierrors.HandleBindingError(ctx, err)
		log.Error(ctx, "Binding failed: %s", err.Error())
		return
	}

	// Step 1: Fetch payload directly from pfms_submission
	// No reversion table involved
	pfmsPayload, _, originalCbReqs, liveFound, err :=
		uh.svc.GetPfmsSubmissionFullByUIDRepo(ctx, req.OriginalPfmsUID)
	if err != nil {
		log.Error(ctx, "GetPfmsSubmissionFullByUIDRepo failed: %s", err.Error())
		ctx.JSON(http.StatusInternalServerError, gin.H{
			"error": "Failed to fetch submission data",
		})
		return
	}
	if !liveFound {
		ctx.JSON(http.StatusNotFound, gin.H{
			"error": "PFMS submission not found for this UID",
		})
		return
	}

	// Step 2: Validate te_number — must be Success with te_number present
	liveTeNumber, liveStatus, err := uh.svc.GetTeNumberByUIDRepo(ctx, req.OriginalPfmsUID)
	if err != nil {
		log.Error(ctx, "GetTeNumberByUIDRepo failed: %s", err.Error())
		ctx.JSON(http.StatusInternalServerError, gin.H{
			"error": "Failed to validate te_number",
		})
		return
	}
	log.Debug(ctx, "Live status: %s | te_number: %s", liveStatus, liveTeNumber)
	if liveStatus != "Success" || liveTeNumber == "" {
		ctx.JSON(http.StatusUnprocessableEntity, gin.H{
			"error":     "PFMS te_number not yet received. Cannot post negative entry.",
			"status":    liveStatus,
			"te_number": liveTeNumber,
		})
		return
	}

	// Step 3: Get pao_code from cb_request
	paoCode := ""
	if len(originalCbReqs) > 0 {
		paoCode = originalCbReqs[0].PaoCode
	}
	if paoCode == "" {
		log.Error(ctx, "PAO code missing in submission data for UID: %s", req.OriginalPfmsUID)
		ctx.JSON(http.StatusInternalServerError, gin.H{
			"error": "PAO code missing in submission data",
		})
		return
	}

	// Step 4: Derive financial year from cb_date
	cbDate, err := time.Parse("2006-01-02", originalCbReqs[0].CbDate)
	if err != nil {
		log.Error(ctx, "Invalid cb_date in submission data: %s", err.Error())
		ctx.JSON(http.StatusInternalServerError, gin.H{
			"error": "Invalid cb_date in submission data",
		})
		return
	}
	finYear := strconv.Itoa(cbDate.Year() + 1)
	if cbDate.Month() < time.April {
		finYear = strconv.Itoa(cbDate.Year())
	}
	log.Debug(ctx, "PAO code: %s | Financial year: %s | CB date: %s",
		paoCode, finYear, cbDate.Format("2006-01-02"))

	// Step 5: Build negating payload — flip all signs
	reversalPayload, reversalUID := BuildReversalPayload(
		pfmsPayload,
		paoCode,
		finYear,
		cbDate.Format("2006-01-02"),
	)
	log.Debug(ctx, "Reversal payload built — UID: %s", reversalUID)

	// Step 6: Authenticate and submit to PFMS
	pfmsResp, err := uh.SubmitPayloadToPfms(ctx, reversalPayload)
	if err != nil {
		log.Error(ctx, "PFMS submission failed: %s", err.Error())
		ctx.JSON(http.StatusBadGateway, gin.H{
			"error": "Failed to post negative entry to PFMS",
		})
		return
	}

	// Step 7: Check PFMS response
	if success, _ := pfmsResp["isSuccess"].(string); success == "0" {
		errMsg := fmt.Sprintf("%v", pfmsResp["errorMessage"])
		log.Error(ctx, "PFMS rejected negative entry: %s", errMsg)
		ctx.JSON(http.StatusBadGateway, gin.H{
			"error":         "PFMS rejected negative entry: " + errMsg,
			"pfms_response": pfmsResp, // ← full response for debugging
		})
		return
	}

	log.Debug(ctx, "PFMS accepted negative entry — UID: %s", reversalUID)

	// Return full PFMS response for test visibility
	// No DB updates — pure test
	ctx.JSON(http.StatusOK, gin.H{
		"message":       "Negative entry posted successfully",
		"reversal_uid":  reversalUID,
		"business_date": cbDate.Format("2006-01-02"),
		"pfms_response": pfmsResp, // ← full PFMS response visible for testing
	})
}

type ReversionRecordsRequest struct {
	PaoCode  string `form:"pao_code"  binding:"required" validate:"required"`
	DdoCode  string `form:"ddo_code"`
	FromDate string `form:"from_date" binding:"required" validate:"required,date_yyyy_mm_dd"`
	ToDate   string `form:"to_date"   binding:"required" validate:"required,date_yyyy_mm_dd"`
	port.MetaDataRequest
}

// GetReversionRecordsHandler godoc
//
//	@Summary		Get Reversion Records Report
//	@Description	Get complete reversion history report for a PAO with optional DDO and date range filters
//	@Tags			PAO GENERAL
//	@Accept			json
//	@Produce		json
//	@Param			pao_code	query		string									true	"PAO Code"
//	@Param			ddo_code	query		string									false	"DDO Code (optional)"
//	@Param			from_date	query		string									true	"From Date (YYYY-MM-DD) based on business_date"
//	@Param			to_date		query		string									true	"To Date (YYYY-MM-DD) based on business_date"
//	@Param			skip		query		int										false	"Number of records to skip"
//	@Param			limit		query		int										false	"Number of records to limit"
//	@Success		200			{object}	response.GetReversionRecordsResponse	"Reversion records retrieved successfully"
//	@Failure		400			{object}	apierrors.APIErrorResponse				"Validation error"
//	@Failure		401			{object}	apierrors.APIErrorResponse				"Unauthorized error"
//	@Failure		403			{object}	apierrors.APIErrorResponse				"Forbidden error"
//	@Failure		404			{object}	apierrors.APIErrorResponse				"Data not found"
//	@Failure		500			{object}	apierrors.APIErrorResponse				"Internal server error"
//	@Router			/v1/pao-gen/cashbook/reversion-records [get]
func (uh *PaogenHandler) GetReversionRecordsHandler(ctx *gin.Context) {
	var req ReversionRecordsRequest
	if err := ctx.ShouldBindQuery(&req); err != nil {
		apierrors.HandleBindingError(ctx, err)
		log.Error(ctx, "Binding failed for ReversionRecordsRequest: %s", err.Error())
		return
	}
	if err := validation.ValidateStruct(req); err != nil {
		apierrors.HandleValidationError(ctx, err)
		log.Error(ctx, "Validation failed for ReversionRecordsRequest: %s", err.Error())
		return
	}

	// Validate date range
	fromDate, err := time.Parse("2006-01-02", req.FromDate)
	if err != nil {
		ctx.JSON(http.StatusBadRequest, gin.H{
			"error": "Invalid from_date format. Use YYYY-MM-DD",
		})
		return
	}
	toDate, err := time.Parse("2006-01-02", req.ToDate)
	if err != nil {
		ctx.JSON(http.StatusBadRequest, gin.H{
			"error": "Invalid to_date format. Use YYYY-MM-DD",
		})
		return
	}
	if toDate.Before(fromDate) {
		ctx.JSON(http.StatusBadRequest, gin.H{
			"error": "to_date must be after from_date",
		})
		return
	}

	if req.Limit == 0 {
		req.Limit = math.MaxInt32
	}

	records, err := uh.svc.GetReversionRecordsMainRepo(
		ctx,
		req.PaoCode,
		req.DdoCode,
		req.FromDate,
		req.ToDate,
		int(req.Skip),
		int(req.Limit),
	)
	if err != nil {
		apierrors.HandleDBError(ctx, err)
		log.Error(ctx, "GetReversionRecordsRepo failed: %s", err.Error())
		return
	}

	rsp := response.NewGetReversionRecordsResponse(records)

	metadata := port.NewMetaDataResponse(req.Skip, req.Limit, len(rsp))

	apiRsp := response.GetReversionRecordsResponse{
		StatusCodeAndMessage: port.ListSuccess,
		MetaDataResponse:     metadata,
		Data:                 rsp,
	}
	log.Debug(ctx, "GetReversionRecordsHandler response", apiRsp)
	handleSuccess(ctx, apiRsp)
}

type CashAccountRevertRequest struct {
	HOCode      int    `json:"hocode" binding:"required"`
	CAMonthYear string `json:"camonthyear" binding:"required"`
}

type CashAccount_request struct {
	HOCode      int    `json:"hocode"`
	CAMonthYear string `json:"camonthyear"`
}

func (uh *PaogenHandler) RevertCashAccountPostHandler(ctx *gin.Context) {
	var req CashAccountRevertRequest

	if err := ctx.ShouldBindJSON(&req); err != nil {
		apierrors.HandleBindingError(ctx, err)
		log.Error(ctx, "Invalid JSON input: %s", err.Error())
		return
	}

	// Step 1: Call subaccounts reversion API
	header := map[string]string{
		"Content-Type": "application/json",
	}
	urlSubaccount := uh.cfg.GetString("urls.subaccountscashaccountcall")
	method := "DELETE"

	req1 := CashAccount_request{
		HOCode:      req.HOCode,
		CAMonthYear: req.CAMonthYear,
	}

	response1, err := uh.CallAPI2(urlSubaccount, method, header, req1)
	if err != nil {
		log.Error(ctx, "Subaccounts cash account reversion API failed: %s", err.Error())
		ctx.JSON(http.StatusInternalServerError, gin.H{"error": "Failed to call subaccounts API"})
		return
	}

	var apiResp struct {
		Message string `json:"message"`
		Status  string `json:"status"`
	}

	responseBytes, err := json.Marshal(response1)
	if err != nil {
		log.Error(ctx, "Failed to marshal API response: %s", err.Error())
		ctx.JSON(http.StatusInternalServerError, gin.H{"error": "Failed to marshal API response"})
		return
	}
	if err := json.Unmarshal(responseBytes, &apiResp); err != nil {
		log.Error(ctx, "Failed to unmarshal API response: %s", err.Error())
		ctx.JSON(http.StatusInternalServerError, gin.H{"error": "Invalid API response"})
		return
	}
	if apiResp.Status != "200" {
		appError := apierrors.NewAppError(
			"Failed to revert cash account in subaccounts",
			"409",
			errors.New("failed to revert cash account in subaccounts"),
		)
		apiErrorResponse := apierrors.NewAPIErrorResponse(http.StatusConflict, "Conflict", appError)
		ctx.JSON(apiErrorResponse.StatusCode, apiErrorResponse)
		return
	}

	// Step 2: Perform PAO cash account reversion
	_, err2 := uh.svc.RevertCashAccountRepo(ctx, strconv.Itoa(req.HOCode), req.CAMonthYear)
	if err2 != nil {
		log.Warn(ctx, "PAO cash account reversion failed: %s", err2.Error())
		ctx.JSON(http.StatusInternalServerError, gin.H{"error": "Failed to revert PAO cash account"})
		return
	}

	apiRsp := response.GetDDOlistResponse{
		StatusCodeAndMessage: port.UpdateSuccess,
	}
	log.Debug(ctx, "RevertCashAccountPostHandler response", apiRsp)
	handleSuccess(ctx, apiRsp)
}

func (uh *PaogenHandler) DeleteCashAccountHandler04042026(ctx *gin.Context) {

	var req struct {
		HOCode      int    `json:"ho_code" validate:"required"`
		DDOCode     string `json:"ddo_code" validate:"required"`
		CAMonthYear string `json:"camonth_year" validate:"required"` // MM-YYYY
	}

	// Step 1: Bind
	if err := ctx.ShouldBindJSON(&req); err != nil {
		apierrors.HandleBindingError(ctx, err)
		return
	}

	// Step 2: Validate camonth_year format
	if _, err := time.Parse("01-2006", req.CAMonthYear); err != nil {
		ctx.JSON(http.StatusBadRequest, gin.H{
			"error": "Invalid camonth_year format. Use MM-YYYY",
		})
		return
	}

	log.Info(ctx, "DeleteCashAccount Request:",
		req.HOCode, req.DDOCode, req.CAMonthYear)

	// Step 3: Call Subaccounts
	header := map[string]string{"Content-Type": "application/json"}
	urlSubaccount := uh.cfg.GetString("urls.subaccountscall4")

	subReq := map[string]interface{}{
		"hocode":       req.HOCode,
		"camonth_year": req.CAMonthYear,
	}

	resp, err := uh.CallAPI2(urlSubaccount, "DELETE", header, subReq)
	if err != nil {
		ctx.JSON(http.StatusInternalServerError, gin.H{
			"error": "SUBACCOUNTS_DELETE_FAILED",
		})
		return
	}

	var apiResp struct {
		Status  string `json:"status"`
		Message string `json:"message"`
	}

	respBytes, err := json.Marshal(resp)
	if err != nil {
		ctx.JSON(http.StatusInternalServerError, gin.H{
			"error": "SUBACCOUNTS_RESPONSE_PARSE_FAILED",
		})
		return
	}

	if err := json.Unmarshal(respBytes, &apiResp); err != nil {
		ctx.JSON(http.StatusInternalServerError, gin.H{
			"error": "SUBACCOUNTS_RESPONSE_INVALID",
		})
		return
	}

	if apiResp.Status == "" {
		ctx.JSON(http.StatusInternalServerError, gin.H{
			"error": "SUBACCOUNTS_EMPTY_RESPONSE",
		})
		return
	}

	log.Info(ctx, "Subaccounts response:", apiResp)

	// Step 4: PAO Delete
	kafkaRows, err := uh.svc.DeletePAOCashAccountRepoWithCount(
		ctx,
		req.HOCode,
		req.DDOCode,
		req.CAMonthYear,
	)

	if err != nil {
		ctx.JSON(http.StatusInternalServerError, gin.H{
			"error":   "PAO_DELETE_FAILED",
			"details": err.Error(),
		})
		return
	}

	// Step 5: Final validation (no data anywhere)
	if apiResp.Status == "404" && kafkaRows == 0 {
		ctx.JSON(http.StatusNotFound, gin.H{
			"error": "No cash account found for given office and period",
		})
		return
	}

	// Step 6: Success
	apiRspqe := response.GetDDOlistResponse{
		StatusCodeAndMessage: port.UpdateSuccess,
	}

	log.Debug(ctx, "DeleteCashAccountHandler response", apiRspqe)
	handleSuccess(ctx, apiRspqe)
}

// func (uh *PaogenHandler) CashAccountReversionHandler(ctx *gin.Context) {

// 	var req struct {
// 		OfficeID          int    `json:"office_id" validate:"required"`
// 		RequestEmployeeID int    `json:"request_employee_id" validate:"required"`
// 		DDOCode           string `json:"ddo_code" validate:"required"`
// 		Period            string `json:"period" validate:"required"` // MM-YYYY
// 		Remark            string `json:"remark"`
// 	}

// 	// Bind
// 	if err := ctx.ShouldBindJSON(&req); err != nil {
// 		apierrors.HandleBindingError(ctx, err)
// 		return
// 	}

// 	// Validate period
// 	if _, err := time.Parse("01-2006", req.Period); err != nil {
// 		ctx.JSON(http.StatusBadRequest, gin.H{
// 			"error": "Invalid period format. Use MM-YYYY",
// 		})
// 		return
// 	}

// 	// ---------------- SUBACCOUNTS ----------------
// 	subStatus := "FAILED"

// 	header := map[string]string{"Content-Type": "application/json"}
// 	url := uh.cfg.GetString("urls.subaccountscall4")

// 	subReq := map[string]interface{}{
// 		"hocode":       req.OfficeID,
// 		"camonth_year": req.Period,
// 	}

// 	resp, err := uh.CallAPI2(url, "DELETE", header, subReq)
// 	if err == nil {
// 		var apiResp struct {
// 			Status string `json:"status"`
// 		}

// 		b, _ := json.Marshal(resp)
// 		if err := json.Unmarshal(b, &apiResp); err == nil {
// 			if apiResp.Status == "200" {
// 				subStatus = "SUCCESS"
// 			} else if apiResp.Status == "404" {
// 				subStatus = "NOT_FOUND"
// 			}
// 		}
// 	}

// 	// ---------------- PAO DELETE ----------------
// 	kafkaStatus := "FAILED"
// 	pfmsDetailStatus := "FAILED"
// 	pfmsMainStatus := "FAILED"
// 	broadStatus := "FAILED"

// 	err = uh.svc.DeletePAOCashAccountRepoTracked(
// 		ctx,
// 		req.OfficeID,
// 		req.DDOCode,
// 		req.Period,
// 		&kafkaStatus,
// 		&pfmsDetailStatus,
// 		&pfmsMainStatus,
// 		&broadStatus,
// 	)

// 	finalStatus := "SUCCESS"
// 	if err != nil {
// 		finalStatus = "FAILED"
// 	}

// 	// ---------------- STORE AUDIT ----------------
// 	errInsert := uh.svc.InsertCashAccReversion(
// 		ctx,
// 		req.OfficeID,
// 		req.RequestEmployeeID,
// 		req.DDOCode,
// 		req.Period,
// 		req.Remark,
// 		subStatus,
// 		kafkaStatus,
// 		pfmsDetailStatus,
// 		pfmsMainStatus,
// 		broadStatus,
// 		finalStatus,
// 	)

// 	if errInsert != nil {
// 		log.Error(ctx, "❌ Audit insert failed:", errInsert.Error())
// 	} else {
// 		log.Info(ctx, "✅ Audit insert success")
// 	}

// 	// ---------------- RESPONSE ----------------
// 	apiRspqe := response.GetDDOlistResponse{
// 		StatusCodeAndMessage: port.UpdateSuccess,
// 	}

// 	handleSuccess(ctx, apiRspqe)
// }

// ============================================================
// HANDLER
// ============================================================

// CashAccountReversionHandler godoc
//
//	@Summary		Revert cash account data
//	@Description	Deletes cash account data from subaccounts and PAO tables and stores audit record
//	@Tags			PAO GENERAL
//	@Accept			json
//	@Produce		json
//	@Param			body	body		map[string]interface{}	true	"Cash Account Reversion Request"
//	@Success		200		{object}	response.GetDDOlistResponse		"resource updated successfully"
//	@Failure		400		{object}	apierrors.APIErrorResponse		"Validation error"
//	@Failure		401		{object}	apierrors.APIErrorResponse		"Unauthorized error"
//	@Failure		403		{object}	apierrors.APIErrorResponse		"Forbidden error"
//	@Failure		404		{object}	apierrors.APIErrorResponse		"Data not found error"
//	@Failure		500		{object}	apierrors.APIErrorResponse		"Internal server error"
//	@Router			/v1/pao-gen/cashaccount/cashacc-reversion [post]
func (uh *PaogenHandler) CashAccountReversionHandler(ctx *gin.Context) {

	var req struct {
		OfficeID          int    `json:"office_id" validate:"required"`
		RequestEmployeeID int    `json:"request_employee_id" validate:"required"`
		DDOCode           string `json:"ddo_code" validate:"required"`
		Period            string `json:"period" validate:"required"` // MM-YYYY
		Remark            string `json:"remark"`
	}

	if err := ctx.ShouldBindJSON(&req); err != nil {
		apierrors.HandleBindingError(ctx, err)
		return
	}

	if err := validation.ValidateStruct(req); err != nil {
		apierrors.HandleValidationError(ctx, err)
		return
	}

	if _, err := time.Parse("01-2006", req.Period); err != nil {
		ctx.JSON(http.StatusBadRequest, gin.H{
			"error": "Invalid period format. Use MM-YYYY",
		})
		return
	}

	// ---------------- SUBACCOUNTS ----------------
	subStatus := "FAILED"

	header := map[string]string{"Content-Type": "application/json"}
	url := uh.cfg.GetString("urls.subaccountscall4")

	subReq := map[string]interface{}{
		"hocode":       req.OfficeID,
		"camonth_year": req.Period,
	}

	resp, err := uh.CallAPI2(url, "DELETE", header, subReq)
	if err == nil {
		var apiResp struct {
			Status string `json:"status"`
		}
		b, _ := json.Marshal(resp)
		if err := json.Unmarshal(b, &apiResp); err == nil {
			switch apiResp.Status {
			case "200":
				subStatus = "SUCCESS"
			case "404":
				subStatus = "NOT_FOUND"
			}
		}
	}

	// ---------------- PAO DELETE ----------------
	kafkaStatus := "FAILED"
	pfmsDetailStatus := "FAILED"
	pfmsMainStatus := "FAILED"
	broadStatus := "FAILED"

	deleteErr := uh.svc.DeletePAOCashAccountRepoTracked(
		ctx,
		req.OfficeID,
		req.DDOCode,
		req.Period,
		&kafkaStatus,
		&pfmsDetailStatus,
		&pfmsMainStatus,
		&broadStatus,
	)

	finalStatus := "SUCCESS"
	if deleteErr != nil {
		finalStatus = "FAILED"
		log.Error(ctx, "PAO delete failed:", deleteErr.Error())
	}

	// ---------------- STORE AUDIT ----------------
	// Use background context so audit insert is NOT tied to the HTTP request lifecycle
	// auditCtx, auditCancel := context.WithTimeout(context.Background(), 10*time.Second)
	// defer auditCancel()
	auditCtx, auditCancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer auditCancel()

	errInsert := uh.svc.InsertCashAccReversion(
		auditCtx,
		// ctx.Request.Context(), // ← change this
		req.OfficeID,
		req.RequestEmployeeID,
		req.DDOCode,
		req.Period,
		req.Remark,
		subStatus,
		kafkaStatus,
		pfmsDetailStatus,
		pfmsMainStatus,
		broadStatus,
		finalStatus,
	)

	if errInsert != nil {
		log.Error(ctx, "Audit insert failed:", errInsert.Error())
	} else {
		log.Info(ctx, "Audit insert success")
	}

	apiRspqe := response.GetDDOlistResponse{
		StatusCodeAndMessage: port.UpdateSuccess,
	}

	handleSuccess(ctx, apiRspqe)
}

func (uh *PaogenHandler) CashAccountReversionHandlertesting(ctx *gin.Context) {

	var req struct {
		OfficeID          int    `json:"office_id" validate:"required"`
		RequestEmployeeID int    `json:"request_employee_id" validate:"required"`
		DDOCode           string `json:"ddo_code" validate:"required"`
		Period            string `json:"period" validate:"required"` // MM-YYYY
		Remark            string `json:"remark"`
	}

	if err := ctx.ShouldBindJSON(&req); err != nil {
		apierrors.HandleBindingError(ctx, err)
		return
	}

	if _, err := time.Parse("01-2006", req.Period); err != nil {
		ctx.JSON(http.StatusBadRequest, gin.H{
			"error": "Invalid period format. Use MM-YYYY",
		})
		return
	}

	// ---------------- SUBACCOUNTS (MANDATORY) ----------------
	subStatus := "FAILED"

	header := map[string]string{"Content-Type": "application/json"}
	url := uh.cfg.GetString("urls.subaccountscall4")

	subReq := map[string]interface{}{
		"hocode":       req.OfficeID,
		"camonth_year": req.Period,
	}

	resp, err := uh.CallAPI2(url, "DELETE", header, subReq)
	if err != nil {
		ctx.JSON(http.StatusOK, gin.H{
			"debug_checkpoint": "subaccounts_api_call_failed",
			"error":            err.Error(),
		})
		return
	}

	var apiResp struct {
		Status string `json:"status"`
	}
	b, _ := json.Marshal(resp)
	if err := json.Unmarshal(b, &apiResp); err != nil {
		ctx.JSON(http.StatusOK, gin.H{
			"debug_checkpoint": "subaccounts_response_parse_failed",
			"error":            err.Error(),
		})
		return
	}

	switch apiResp.Status {
	case "200":
		subStatus = "SUCCESS"
	case "404":
		ctx.JSON(http.StatusOK, gin.H{
			"debug_checkpoint": "subaccounts_not_found",
			"sub_status":       "NOT_FOUND",
			"error":            "No cash account record found in subaccounts",
		})
		return
	default:
		ctx.JSON(http.StatusOK, gin.H{
			"debug_checkpoint":            "subaccounts_unexpected_status",
			"subaccounts_status_returned": apiResp.Status,
		})
		return
	}

	// ---------------- PAO DELETE ----------------
	kafkaStatus := "FAILED"
	pfmsDetailStatus := "FAILED"
	pfmsMainStatus := "FAILED"
	broadStatus := "FAILED"

	deleteErr := uh.svc.DeletePAOCashAccountRepoTracked(
		ctx,
		req.OfficeID,
		req.DDOCode,
		req.Period,
		&kafkaStatus,
		&pfmsDetailStatus,
		&pfmsMainStatus,
		&broadStatus,
	)

	finalStatus := "SUCCESS"
	if deleteErr != nil {
		finalStatus = "FAILED"
	}

	// ---------------- STORE AUDIT (ALWAYS RUN) ----------------
	auditCtx, auditCancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer auditCancel()

	errInsert := uh.svc.InsertCashAccReversion(
		auditCtx,
		req.OfficeID,
		req.RequestEmployeeID,
		req.DDOCode,
		req.Period,
		req.Remark,
		subStatus,
		kafkaStatus,
		pfmsDetailStatus,
		pfmsMainStatus,
		broadStatus,
		finalStatus,
	)

	// ---------------- DEBUG RESPONSE (TEMP) ----------------
	deleteErrStr := ""
	if deleteErr != nil {
		deleteErrStr = deleteErr.Error()
	}

	insertErrStr := ""
	if errInsert != nil {
		insertErrStr = errInsert.Error()
	}

	ctx.JSON(http.StatusOK, gin.H{
		"debug_checkpoint":   "completed",
		"sub_status":         subStatus,
		"kafka_status":       kafkaStatus,
		"pfms_detail_status": pfmsDetailStatus,
		"pfms_main_status":   pfmsMainStatus,
		"broad_status":       broadStatus,
		"final_status":       finalStatus,
		"delete_err":         deleteErrStr,
		"insert_err":         insertErrStr,
	})
}

type DdoPfmsStatusRequest struct {
	DdoCode string `uri:"ddo-code" validate:"required"`
	port.MetaDataRequest
}

// ListDdoPfmsStatusHandler godoc
//
//	@Summary		Get DDO cash account and verification status for all periods
//	@Description	Get DDO PRAO cash account receive and verification status for all periods
//	@Tags			PAO GENERAL
//	@Accept			json
//	@Produce		json
//	@Param			ddo-code	path		string	true	"DDO Code"
//	@Param			skip		query		int		false	"Number of records to skip for pagination"
//	@Param			limit		query		int		false	"Number of records to limit for pagination"
//	@Success		200		{object}	response.GetDdoPfmsStatusResponse	"data retrieved successfully"
//	@Failure		400		{object}	apierrors.APIErrorResponse			"Validation error"
//	@Failure		401		{object}	apierrors.APIErrorResponse			"Unauthorized error"
//	@Failure		403		{object}	apierrors.APIErrorResponse			"Forbidden error"
//	@Failure		404		{object}	apierrors.APIErrorResponse			"Data not found error"
//	@Failure		500		{object}	apierrors.APIErrorResponse			"Internal server error"
//	@Router			/v1/pao-gen/cashaccount/ddo/cashacc-status/{ddo-code} [get]
func (uh *PaogenHandler) ListDdoPfmsStatusHandler(ctx *gin.Context) {

	var req DdoPfmsStatusRequest
	if err := ctx.ShouldBindUri(&req); err != nil {
		apierrors.HandleBindingError(ctx, err)
		log.Error(ctx, "Binding failed for DdoPfmsStatusRequest: %s", err.Error())
		return
	}
	if err := ctx.ShouldBindQuery(&req); err != nil {
		apierrors.HandleBindingError(ctx, err)
		log.Error(ctx, "Binding failed for DdoPfmsStatusRequest query: %s", err.Error())
		return
	}
	if err := validation.ValidateStruct(req); err != nil {
		apierrors.HandleValidationError(ctx, err)
		log.Error(ctx, "Validation failed for DdoPfmsStatusRequest: %s", err.Error())
		return
	}
	if req.Limit == 0 {
		req.Limit = math.MaxInt32
	}

	u, err := uh.svc.GetDdoPfmsStatusRepo(ctx, req.DdoCode, req.MetaDataRequest)
	if err != nil {
		apierrors.HandleDBError(ctx, err)
		log.Error(ctx, "GetDdoPfmsStatusRepo call failed: %s", err.Error())
		return
	}

	metadata := port.NewMetaDataResponse(req.Skip, req.Limit, len(u))

	apiRsp := response.GetDdoPfmsStatusResponse{
		StatusCodeAndMessage: port.FetchSucess,
		MetaDataResponse:     metadata,
		Data:                 u,
	}
	log.Debug(ctx, "ListDdoPfmsStatusHandler response", apiRsp)
	handleSuccess(ctx, apiRsp)
}

type PaoPraoStatusRequest struct {
	PaoCode string `form:"pao_code" validate:"required"`
	Period  string `form:"period" validate:"required"`
}

// GetPaoPraoStatusHandler godoc
//
//	@Summary		Get PAO PRAO submission status
//	@Description	Check if PAO has submitted to PRAO for given period
//	@Tags			PAO GENERAL
//	@Accept			json
//	@Produce		json
//	@Param			pao_code	query		string	true	"PAO Code"
//	@Param			period		query		string	true	"Period in MMYYYY format"
//	@Success		200		{object}	response.GetPaoPraoStatusResponse	"data retrieved successfully"
//	@Failure		400		{object}	apierrors.APIErrorResponse			"Validation error"
//	@Failure		401		{object}	apierrors.APIErrorResponse			"Unauthorized error"
//	@Failure		403		{object}	apierrors.APIErrorResponse			"Forbidden error"
//	@Failure		404		{object}	apierrors.APIErrorResponse			"Data not found error"
//	@Failure		500		{object}	apierrors.APIErrorResponse			"Internal server error"
//	@Router			/v1/pao-gen/cashaccount/prao-status [get]
func (uh *PaogenHandler) GetPaoPraoStatusHandler(ctx *gin.Context) {

	var req PaoPraoStatusRequest
	if err := ctx.ShouldBindQuery(&req); err != nil {
		apierrors.HandleBindingError(ctx, err)
		log.Error(ctx, "Binding failed for PaoPraoStatusRequest: %s", err.Error())
		return
	}
	if err := validation.ValidateStruct(req); err != nil {
		apierrors.HandleValidationError(ctx, err)
		log.Error(ctx, "Validation failed for PaoPraoStatusRequest: %s", err.Error())
		return
	}

	u, err := uh.svc.GetPaoPraoStatusRepo(ctx, req.PaoCode, req.Period)
	if err != nil {
		apierrors.HandleDBError(ctx, err)
		log.Error(ctx, "GetPaoPraoStatusRepo call failed: %s", err.Error())
		return
	}

	apiRsp := response.GetPaoPraoStatusResponse{
		StatusCodeAndMessage: port.FetchSucess,
		Data:                 u,
	}
	log.Debug(ctx, "GetPaoPraoStatusHandler response", apiRsp)
	handleSuccess(ctx, apiRsp)
}

// GetAllReversionRecordsHandler godoc
//
//	@Summary		Get All Reversion Records
//	@Description	Get all reversion records for a PAO irrespective of pfms_reversal_type, grouped by type
//	@Tags			PAO GENERAL
//	@Accept			json
//	@Produce		json
//	@Param			pao_code	query		string										true	"PAO Code"
//	@Param			ddo_code	query		string										false	"DDO Code (optional)"
//	@Param			from_date	query		string										true	"From Date (YYYY-MM-DD) based on business_date"
//	@Param			to_date		query		string										true	"To Date (YYYY-MM-DD) based on business_date"
//	@Param			skip		query		int											false	"Number of records to skip"
//	@Param			limit		query		int											false	"Number of records to limit"
//	@Success		200			{object}	response.GetReversionRecordsResponse		"All reversion records retrieved successfully"
//	@Failure		400			{object}	apierrors.APIErrorResponse					"Validation error"
//	@Failure		401			{object}	apierrors.APIErrorResponse					"Unauthorized error"
//	@Failure		403			{object}	apierrors.APIErrorResponse					"Forbidden error"
//	@Failure		404			{object}	apierrors.APIErrorResponse					"Data not found"
//	@Failure		500			{object}	apierrors.APIErrorResponse					"Internal server error"
//	@Router			/v1/pao-gen/cashbook/all-reversion-records [get]
func (uh *PaogenHandler) GetAllReversionRecordsHandler(ctx *gin.Context) {
	var req ReversionRecordsRequest
	if err := ctx.ShouldBindQuery(&req); err != nil {
		apierrors.HandleBindingError(ctx, err)
		log.Error(ctx, "Binding failed for ReversionRecordsRequest: %s", err.Error())
		return
	}
	if err := validation.ValidateStruct(req); err != nil {
		apierrors.HandleValidationError(ctx, err)
		log.Error(ctx, "Validation failed for ReversionRecordsRequest: %s", err.Error())
		return
	}

	fromDate, err := time.Parse("2006-01-02", req.FromDate)
	if err != nil {
		ctx.JSON(http.StatusBadRequest, gin.H{"error": "Invalid from_date format. Use YYYY-MM-DD"})
		return
	}
	toDate, err := time.Parse("2006-01-02", req.ToDate)
	if err != nil {
		ctx.JSON(http.StatusBadRequest, gin.H{"error": "Invalid to_date format. Use YYYY-MM-DD"})
		return
	}
	if toDate.Before(fromDate) {
		ctx.JSON(http.StatusBadRequest, gin.H{"error": "to_date must be after from_date"})
		return
	}

	if req.Limit == 0 {
		req.Limit = math.MaxInt32
	}

	records, err := uh.svc.GetAllReversionRecordsRepo(
		ctx,
		req.PaoCode,
		req.DdoCode,
		req.FromDate,
		req.ToDate,
		int(req.Skip),
		int(req.Limit),
	)
	if err != nil {
		apierrors.HandleDBError(ctx, err)
		log.Error(ctx, "GetAllReversionRecordsRepo failed: %s", err.Error())
		return
	}

	rsp := response.NewGetReversionRecordsResponse(records)

	metadata := port.NewMetaDataResponse(req.Skip, req.Limit, len(rsp))

	apiRsp := response.GetReversionRecordsResponse{
		StatusCodeAndMessage: port.ListSuccess,
		MetaDataResponse:     metadata,
		Data:                 rsp,
	}
	log.Debug(ctx, "GetReversionRecordsHandler response", apiRsp)
	handleSuccess(ctx, apiRsp)
}
