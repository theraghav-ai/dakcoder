package handler

import (
	//"database/sql"

	//"github.com/templatedop/githubrepo/dtime"
	"errors"
	"gotemplate/core/domain"
	"math"
	"net/http"

	"time"

	//"github.com/guregu/null"

	//"github.com/jackc/pgx/v5/pgtype"
	//"github.com/aarondl/opt/null"

	//"github.com/volatiletech/null"

	"gotemplate/core/port"
	"gotemplate/handler/response"
	repo "gotemplate/repo/postgres"

	log "gitlab.cept.gov.in/it-2.0-common/api-log"

	apierrors "gitlab.cept.gov.in/it-2.0-common/api-errors"
	validation "gitlab.cept.gov.in/it-2.0-common/api-validation"

	"github.com/gin-gonic/gin"
	"github.com/jackc/pgx/v5/pgconn"
	"github.com/volatiletech/null/v9"
	//"gotemplate/dtime"
)

type PublicAcctHandler struct {
	svc *repo.PublicAcctRepository
}

// NewUserHandler creates a new UserHandler instance
func NewPublicAcctHandler(svc *repo.PublicAcctRepository) *PublicAcctHandler {
	return &PublicAcctHandler{
		svc,
	}
}

type apprAcctRequest struct {
	Year string `form:"year" validate:"required"`
	port.MetaDataRequest
}

type broadsheetRequestv2 struct {
	Type      int64  `form:"type" validate:"required,min=1,max=5"`
	MonthYear string `form:"month-year" validate:"required,max=10"`
	MajorHead string `form:"major-head" validate:"omitempty,max=15"`
	port.MetaDataRequest
}
type broadsheetRequestUriv2 struct {
	DdoCode string `uri:"ddo-code" validate:"omitempty,validateDdocode"`
}

// ListBroadsheetHandler godoc
//
//	@Summary		Get Broadsheet details
//	@Description	Get Broadsheet details
//	@Tags			Public Account
//	@Accept			json
//	@Produce		json
//	@Param			ddo-code	path		string			true	"ddo-code"
//	@Param			type	query		string			true	"type"
//	@Param			month-year	query		string			true	"month-year"
//	@Param			major-head	query		string			true	"major-head"
//
// @Param       skip    query       int     			false   		"Number of records to skip for pagination"
// @Param       limit   query       int     			false   		"Number of records to limit for pagination"
//
//	@Success		200					{object}	response.GetBroadsheetResponse	"list retrieved successfully"
//	@Failure		400					{object}	apierrors.APIErrorResponse						"Validation error"
//	@Failure		401					{object}	apierrors.APIErrorResponse						"Unauthorized error"
//	@Failure		403					{object}	apierrors.APIErrorResponse						"Forbidden error"
//	@Failure		404					{object}	apierrors.APIErrorResponse						"Data not found error"
//	@Failure		409					{object}	apierrors.APIErrorResponse						"Data conflict error"
//	@Failure		500					{object}	apierrors.APIErrorResponse						"Internal server error"
//	@Router			/v1/pao-gen/ddo/{ddo-code}/public-acct/broad-sheet [get]
func (ph *PublicAcctHandler) ListBroadsheetHandler(ctx *gin.Context) {

	var req broadsheetRequestv2
	if err := ctx.ShouldBindQuery(&req); err != nil {
		apierrors.HandleBindingError(ctx, err)
		log.Error(ctx, "Binding failed for broadsheetRequestv2: %s", err.Error())
		return
	}
	if err := validation.ValidateStruct(req); err != nil {
		apierrors.HandleValidationError(ctx, err)
		log.Error(ctx, "Validation failed for broadsheetRequestv2: %s", err.Error())
		return
	}
	var reqURI broadsheetRequestUriv2
	if err := ctx.ShouldBindUri(&reqURI); err != nil {
		apierrors.HandleBindingError(ctx, err)
		log.Error(ctx, "Binding failed for broadsheetRequestUriv2: %s", err.Error())
		return
	}
	if err := validation.ValidateStruct(reqURI); err != nil {
		apierrors.HandleValidationError(ctx, err)
		log.Error(ctx, "Validation failed for broadsheetRequestUriv2: %s", err.Error())
		return
	}
	if req.Limit == 0 {
		req.Limit = math.MaxInt32
	}
	var request domain.BroadsheetRequest
	request.Type = req.Type
	request.MonthYear = req.MonthYear
	request.MajorHead = req.MajorHead
	request.DdoCode = reqURI.DdoCode

	res, err := ph.svc.GetbroadsheetRepo(ctx, request, req.MetaDataRequest)
	if err != nil {
		apierrors.HandleDBError(ctx, err)
		log.Error(ctx, "Get Broadsheet Repo call failed: %s", err.Error())
		return
	}

	rsp := response.NewGetBroadsheetResponse(res)

	metadata := port.NewMetaDataResponse(req.Skip, req.Limit, len(rsp))

	apiRsp := response.GetBroadsheetResponse{
		StatusCodeAndMessage: port.ListSuccess,
		MetaDataResponse:     metadata,
		Data:                 rsp,
	}
	log.Debug(ctx, "ListBroadsheetHandler response", apiRsp)
	handleSuccess(ctx, apiRsp)

}

const ErrBindingApprAcctRequest = "Binding failed for apprAcctRequest: %s"
const ErrValidationApprAcctRequest = "Validation failed for apprAcctRequest: %s"

// ListApprAcctsHandler godoc
//
//	@Summary		Get Account details
//	@Description	Get Account details
//	@Tags			Public Account
//	@Accept			json
//	@Produce		json
//	@Param			year	query		string			true	"Year"
//
// @Param       skip    query       int     			false   		"Number of records to skip for pagination"
// @Param       limit   query       int     			false   		"Number of records to limit for pagination"
//
// @Success		200		{object}	response.GetAppracctsResponse			"list retrieved successfully"
// @Failure		400					{object}	apierrors.APIErrorResponse						"Validation error"
// @Failure		401					{object}	apierrors.APIErrorResponse						"Unauthorized error"
// @Failure		403					{object}	apierrors.APIErrorResponse						"Forbidden error"
// @Failure		404					{object}	apierrors.APIErrorResponse						"Data not found error"
// @Failure		409					{object}	apierrors.APIErrorResponse						"Data conflict error"
// @Failure		500					{object}	apierrors.APIErrorResponse						"Internal server error"
// @Router			/v1/public-acct/appr-acct-one [get]
func (ph *PublicAcctHandler) ListApprAcctsHandler(ctx *gin.Context) {

	var req apprAcctRequest
	if err := ctx.ShouldBindQuery(&req); err != nil {
		apierrors.HandleBindingError(ctx, err)
		log.Error(ctx, ErrBindingApprAcctRequest, err.Error())
		return
	}
	if err := validation.ValidateStruct(req); err != nil {
		apierrors.HandleValidationError(ctx, err)
		log.Error(ctx, ErrValidationApprAcctRequest, err.Error())
		return
	}
	if req.Limit == 0 {
		req.Limit = math.MaxInt32
	}

	var request domain.ApprAcctsRequest

	request.Year = req.Year

	res, err := ph.svc.GetappracctRepo(ctx, request, req.MetaDataRequest)
	if err != nil {
		apierrors.HandleDBError(ctx, err)
		log.Error(ctx, "Get ApproAcct Repo call failed: %s", err.Error())
		return
	}
	rsp := response.NewGetAppracctsResponse(res)

	metadata := port.NewMetaDataResponse(req.Skip, req.Limit, len(rsp))

	apiRsp := response.GetAppracctsResponse{
		StatusCodeAndMessage: port.ListSuccess,
		MetaDataResponse:     metadata,
		Data:                 rsp,
	}
	log.Debug(ctx, "ListApprAcctsHandler response", apiRsp)
	handleSuccess(ctx, apiRsp)

}

// ListApprAcctsTwoHandler godoc
//
//	@Summary		Get Account 2 details
//	@Description	Get Account 2 details
//	@Tags			Public Account
//	@Accept			json
//	@Produce		json
//	@Param			year	query		string			true	"Year"
//
// @Param       skip    query       int     			false   		"Number of records to skip for pagination"
// @Param       limit   query       int     			false   		"Number of records to limit for pagination"
//
//	@Success		200		{object}	response.GetAppraccts2Response			"list retrieved successfully"
//	@Failure		400					{object}	apierrors.APIErrorResponse						"Validation error"
//	@Failure		401					{object}	apierrors.APIErrorResponse						"Unauthorized error"
//	@Failure		403					{object}	apierrors.APIErrorResponse						"Forbidden error"
//	@Failure		404					{object}	apierrors.APIErrorResponse						"Data not found error"
//	@Failure		409					{object}	apierrors.APIErrorResponse						"Data conflict error"
//	@Failure		500					{object}	apierrors.APIErrorResponse						"Internal server error"
//	@Router			/v1/public-acct/appr-acct-two [get]
func (ph *PublicAcctHandler) ListApprAcctsTwoHandler(ctx *gin.Context) {

	var req apprAcctRequest
	if err := ctx.ShouldBindQuery(&req); err != nil {
		apierrors.HandleBindingError(ctx, err)
		log.Error(ctx, ErrBindingApprAcctRequest, err.Error())
		return
	}
	if err := validation.ValidateStruct(req); err != nil {
		apierrors.HandleValidationError(ctx, err)
		log.Error(ctx, ErrValidationApprAcctRequest, err.Error())
		return
	}
	if req.Limit == 0 {
		req.Limit = math.MaxInt32
	}
	var request domain.ApprAcctsRequest

	request.Year = req.Year

	res, err := ph.svc.GetappracctRepo2(ctx, request, req.MetaDataRequest)
	if err != nil {
		log.Error(ctx, err)
		apierrors.HandleDBError(ctx, err)
		log.Error(ctx, "Get ApprAcct2 Repo call failed: %s", err.Error())
		return
	}

	rsp := response.NewGetAppraccts2Response(res)

	metadata := port.NewMetaDataResponse(req.Skip, req.Limit, len(rsp))

	apiRsp := response.GetAppraccts2Response{
		StatusCodeAndMessage: port.ListSuccess,
		MetaDataResponse:     metadata,
		Data:                 rsp,
	}
	log.Debug(ctx, "ListApprAcctsTwoHandler response", apiRsp)
	handleSuccess(ctx, apiRsp)

}

type RemunerationRequest struct {
	FinancialYear       string    `db:"financial_year" json:"financial_year" validate:"required,year"`
	RemunerationItem    string    `db:"remuneration_item" json:"remuneration_item" validate:"required,max=255"`
	RemunerationType    string    `db:"remuneration_type" json:"remuneration_type" validate:"required,max=50"`
	RemunerationRate    float32   `db:"remuneration_rate" json:"remuneration_rate" validate:"required"`
	UpdatedBy           uint64    `db:"updated_by" json:"updated_by" validate:"required,employee_id"`
	UpdatedDate         time.Time `db:"updated_date" json:"updated_date" validate:"omitempty"`
	Status              bool      `db:"status" json:"status" validate:"required,eq=true"`
	AuthorisationStatus string    `db:"authorisation_status" json:"authorisation_status" validate:"required,max=20"`
}

// CreateRemunerationRateHandler godoc
//
//	@Summary		Create Renumeration
//	@Description	Create Renumeration
//	@Tags			Public Account
//	@Accept			json
//	@Produce		json
//	@Param			body	body		[]RemunerationRequest	true	"Create Remuneration Request"
//	@Success		201		{object}	response.GetAppraccts2Response			"resource created successfully"
//	@Failure		400					{object}	apierrors.APIErrorResponse						"Validation error"
//	@Failure		401					{object}	apierrors.APIErrorResponse						"Unauthorized error"
//	@Failure		403					{object}	apierrors.APIErrorResponse						"Forbidden error"
//	@Failure		404					{object}	apierrors.APIErrorResponse						"Data not found error"
//	@Failure		409					{object}	apierrors.APIErrorResponse						"Data conflict error"
//	@Failure		500					{object}	apierrors.APIErrorResponse						"Internal server error"
//	@Router			/v1/public-acct/remuneration [post]
func (ph *PublicAcctHandler) CreateRemunerationRateHandler(ctx *gin.Context) {
	var hoas []RemunerationRequest
	if err := ctx.ShouldBindJSON(&hoas); err != nil {
		apierrors.HandleBindingError(ctx, err)
		log.Error(ctx, "Binding failed for RemunerationRequest: %s", err.Error())
		return
	}
	for _, req := range hoas {
		if err := validation.ValidateStruct(req); err != nil {
			apierrors.HandleValidationError(ctx, err)
			log.Error(ctx, "Validation failed for RemunerationRequest: %s", err.Error())
			return
		}
	}

	var requests []domain.RemunerationRequest

	for _, request := range hoas {

		requests = append(requests, domain.RemunerationRequest{

			FinancialYear:       request.FinancialYear,
			RemunerationItem:    request.RemunerationItem,
			RemunerationType:    request.RemunerationType,
			RemunerationRate:    request.RemunerationRate,
			UpdatedBy:           request.UpdatedBy,
			UpdatedDate:         request.UpdatedDate,
			Status:              request.Status,
			AuthorisationStatus: request.AuthorisationStatus,
		})
	}
	err := ph.svc.RemCreatewithpgx(ctx, requests)
	if err != nil {
		log.Error(ctx, "Create Remuneration Repo call failed: %s", err.Error())
		if err.(*pgconn.PgError).Code == "23505" {
			err1 := errors.New("remuneration creation failed")
			// Create an AppError with a user-friendly message and code.
			appError := apierrors.NewAppError(
				"Duplidate remuneration rate creation not allowed", // User-friendly error message
				"409", // Error code representing the error type
				err1,  // Original error for debugging purposes
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
	apiRsp := response.GetAppraccts2Response{
		StatusCodeAndMessage: port.CreateSuccess,
	}
	log.Debug(ctx, "CreateRemunerationRateHandler response", apiRsp)
	handleCreateSuccess(ctx, apiRsp)
}

type UpdateRemRequest struct {
	FinancialYear       string    `json:"financial_year" db:"financial_year" insert:"financial_year" validate:"required,year"`
	RemunerationItem    string    `json:"remuneration_item" db:"remuneration_item" insert:"remuneration_item" validate:"required,max=255"`
	RemunerationType    string    `json:"remuneration_type" db:"remuneration_type" insert:"remuneration_type" validate:"required,max=50"`
	RemunerationRate    float32   `json:"remuneration_rate" db:"remuneration_rate" insert:"remuneration_rate" validate:"required"`
	UpdatedBy           uint64    `json:"updated_by" db:"updated_by" insert:"updated_by" validate:"required,employee_id"`
	UpdatedDate         time.Time `json:"updated_date" db:"updated_date" insert:"updated_date" validate:"omitempty"`
	AuthorisationStatus string    `json:"authorisation_status" db:"authorisation_status" insert:"authorisation_status" validate:"required,max=20"`
	Status              *bool     `json:"status" db:"status" insert:"status" validate:"required"`
	ApprovedDate        time.Time `json:"approved_date" db:"approved_date" insert:"approved_date" validate:"omitempty"`
	ApprovedBy          uint64    `json:"approved_by" db:"approved_by" insert:"approved_by" validate:"omitempty,employee_id"`
}

// UpdateRemunerationRateHandler godoc
//
//	@Summary		Update Remuneration
//	@Description	Update Remuneration
//	@Tags			Public Account
//	@Accept			json
//	@Produce		json
//	@Param			body	body		[]UpdateRemRequest	true	"Update Remuneration Request"
//	@Success		200		{object}	response.GetAppraccts2Response			"resource updated successfully"
//	@Failure		400					{object}	apierrors.APIErrorResponse						"Validation error"
//	@Failure		401					{object}	apierrors.APIErrorResponse						"Unauthorized error"
//	@Failure		403					{object}	apierrors.APIErrorResponse						"Forbidden error"
//	@Failure		404					{object}	apierrors.APIErrorResponse						"Data not found error"
//	@Failure		409					{object}	apierrors.APIErrorResponse						"Data conflict error"
//	@Failure		500					{object}	apierrors.APIErrorResponse						"Internal server error"
//	@Router			/v1/public-acct/bulk-remuneration [put]
func (hh *PublicAcctHandler) UpdateRemunerationRateHandler(ctx *gin.Context) {
	var req []UpdateRemRequest
	if err := ctx.ShouldBindJSON(&req); err != nil {
		apierrors.HandleBindingError(ctx, err)
		log.Error(ctx, "Binding failed for UpdateRemRequest: %s", err.Error())
		return
	}
	currentDateTime := time.Now()
	var hoareq []domain.UpdateRemRequest

	for _, reqs := range req {
		if err := validation.ValidateStruct(reqs); err != nil {
			apierrors.HandleValidationError(ctx, err)
			log.Error(ctx, "Validation failed for UpdateRemRequest: %s", err.Error())
			return
		}
	}
	var t time.Time

	for _, request := range req {
		if request.ApprovedBy == 0 {
			hoareq = append(hoareq, domain.UpdateRemRequest{
				RemunerationItem:    request.RemunerationItem,
				FinancialYear:       request.FinancialYear,
				RemunerationType:    request.RemunerationType,
				RemunerationRate:    request.RemunerationRate,
				UpdatedBy:           request.UpdatedBy,
				UpdatedDate:         currentDateTime,
				AuthorisationStatus: request.AuthorisationStatus,
				Status:              *request.Status,
				ApprovedBy:          null.Uint64From(0),
				ApprovedDate:        null.TimeFrom(t),
			})
		} else {

			hoareq = append(hoareq, domain.UpdateRemRequest{
				RemunerationItem: request.RemunerationItem,
				FinancialYear:    request.FinancialYear,
				RemunerationType: request.RemunerationType,
				RemunerationRate: request.RemunerationRate,

				AuthorisationStatus: request.AuthorisationStatus,
				Status:              *request.Status,
				ApprovedBy:          null.Uint64From(request.ApprovedBy),
				ApprovedDate:        null.TimeFrom(currentDateTime),
			})
		}
	}

	err := hh.svc.Updateremexe(ctx, hoareq)
	if err != nil {
		apierrors.HandleDBError(ctx, err)
		log.Error(ctx, "Update Remuneration Repo call failed: %s", err.Error())
		return
	}
	apiRsp := response.GetAppraccts2Response{
		StatusCodeAndMessage: port.UpdateSuccess,
	}
	log.Debug(ctx, "UpdateRemunerationRateHandler response", apiRsp)
	handleSuccess(ctx, apiRsp)
}

type GetRemRequest struct {
	Type int64  `form:"type" validate:"required,min=1,max=3"`
	Id   string `form:"id" validate:"required,max=20"`
	port.MetaDataRequest
}

// Getremuneration godoc
//
//	@Summary		Get Remuneration details
//	@Description	Get Remuneration details
//	@Tags			Public Account
//	@Accept			json
//	@Produce		json
//	@Param			type	query		int64			true	"Type"
//	@Param			id	query		string			true	"Id"
//
// @Param       skip    query       int     			false   		"Number of records to skip for pagination"
// @Param       limit   query       int     			false   		"Number of records to limit for pagination"
//
//	@Success		200					{object}	response.GetremunerationdetResponse	"list retrieved successfully"
//	@Failure		400					{object}	apierrors.APIErrorResponse						"Validation error"
//	@Failure		401					{object}	apierrors.APIErrorResponse						"Unauthorized error"
//	@Failure		403					{object}	apierrors.APIErrorResponse						"Forbidden error"
//	@Failure		404					{object}	apierrors.APIErrorResponse						"Data not found error"
//	@Failure		409					{object}	apierrors.APIErrorResponse						"Data conflict error"
//	@Failure		500					{object}	apierrors.APIErrorResponse						"Internal server error"
//	@Router			/v1/public-acct/remuneration [get]
func (hh *PublicAcctHandler) ListRemunerationRateDetailHandler(ctx *gin.Context) {

	var req GetRemRequest
	if err := ctx.ShouldBindQuery(&req); err != nil {
		apierrors.HandleBindingError(ctx, err)
		log.Error(ctx, "Binding failed for GetRemRequest: %s", err.Error())
		return
	}
	if err := validation.ValidateStruct(req); err != nil {
		apierrors.HandleValidationError(ctx, err)
		log.Error(ctx, "Validation failed for GetRemRequest: %s", err.Error())
		return
	}
	if req.Limit == 0 {
		req.Limit = math.MaxInt32
	}
	request := domain.GetRemRequest{
		Type: req.Type,
		Id:   req.Id,
	}
	u, err := hh.svc.GetRemRepo(ctx, &request, req.MetaDataRequest)
	if err != nil {
		apierrors.HandleDBError(ctx, err)
		log.Error(ctx, "Get Remuneration Repo call failed: %s", err.Error())
		return
	}

	rsp := response.NewGetremunerationdetResponse(u)

	metadata := port.NewMetaDataResponse(req.Skip, req.Limit, len(rsp))

	apiRsp := response.GetremunerationdetResponse{
		StatusCodeAndMessage: port.ListSuccess,
		MetaDataResponse:     metadata,
		Data:                 rsp,
	}
	log.Debug(ctx, "ListRemunerationRateDetailHandler response", apiRsp)
	handleSuccess(ctx, apiRsp)

}

// RemunerationCalculation godoc
//
//	@Summary		Calculate Renumeration
//	@Description	Calculate Renumeration
//	@Tags			Public Account
//	@Accept			json
//	@Produce		json
//	@Param			body	body		domain.RemunerationCreationRequestBulk	true	"Calculate Remuneration Request"
//	@Success		200		{object}	response.GetRemunerationCreationResponse			"remuneration calculated successfully"
//	@Failure		400					{object}	apierrors.APIErrorResponse						"Validation error"
//	@Failure		401					{object}	apierrors.APIErrorResponse						"Unauthorized error"
//	@Failure		403					{object}	apierrors.APIErrorResponse						"Forbidden error"
//	@Failure		404					{object}	apierrors.APIErrorResponse						"Data not found error"
//	@Failure		409					{object}	apierrors.APIErrorResponse						"Data conflict error"
//	@Failure		500					{object}	apierrors.APIErrorResponse						"Internal server error"
//	@Router			/v1/public-acct/remuneration-calculation [post]
func (hh *PublicAcctHandler) RemunerationCalculation(ctx *gin.Context) {
	var remus domain.RemunerationCreationRequestBulk
	if err := ctx.ShouldBindJSON(&remus); err != nil {
		apierrors.HandleBindingError(ctx, err)
		return
	}

	if err := validation.ValidateStruct(remus); err != nil {
		apierrors.HandleValidationError(ctx, err)
		log.Error(ctx, "Validation failed for GetRemRequest: %s", err.Error())
		return
	}

	rem, err := hh.svc.RemunerationCalculationRepo(ctx, remus)
	if err != nil {
		apierrors.HandleDBError(ctx, err)
		log.Error(ctx, "Remuneration Calculation Repo call failed: %s", err.Error())
		return
	}

	if len(rem) > 0 {
		err2 := hh.svc.RemunerationCalculationPostRepo(ctx, rem)
		if err2 != nil {
			apierrors.HandleDBError(ctx, err2)
			log.Error(ctx, "Remuneration Calculation Post Repo call failed: %s", err2.Error())
			return
		}
	}
	rsp := response.NewRemunerationCreationResponse(rem)

	apiRsp := response.GetRemunerationCreationResponse{
		StatusCodeAndMessage: port.ListSuccess,
		Data:                 rsp,
	}
	log.Debug(ctx, "ListRemunerationRateDetailHandler response", apiRsp)
	handleSuccess(ctx, apiRsp)
}

// ListApprAcctsThreeHandler godoc
//
//	@Summary		Get Account 3 details
//	@Description	Get Account 3 details
//	@Tags			Public Account
//	@Accept			json
//	@Produce		json
//	@Param			year	query		string			true	"Year"
//
// @Param       skip    query       int     			false   		"Number of records to skip for pagination"
// @Param       limit   query       int     			false   		"Number of records to limit for pagination"
//
//	@Success		200		{object}	response.GetAppraccts3Response			"list retrieved successfully"
//	@Failure		400					{object}	apierrors.APIErrorResponse						"Validation error"
//	@Failure		401					{object}	apierrors.APIErrorResponse						"Unauthorized error"
//	@Failure		403					{object}	apierrors.APIErrorResponse						"Forbidden error"
//	@Failure		404					{object}	apierrors.APIErrorResponse						"Data not found error"
//	@Failure		409					{object}	apierrors.APIErrorResponse						"Data conflict error"
//	@Failure		500					{object}	apierrors.APIErrorResponse						"Internal server error"
//	@Router			/v1/public-acct/appr-acct-three [get]
func (ph *PublicAcctHandler) ListApprAcctsThreeHandler(ctx *gin.Context) {

	var req apprAcctRequest
	if err := ctx.ShouldBindQuery(&req); err != nil {
		apierrors.HandleBindingError(ctx, err)
		log.Error(ctx, ErrBindingApprAcctRequest, err.Error())
		return
	}
	if err := validation.ValidateStruct(req); err != nil {
		apierrors.HandleValidationError(ctx, err)
		log.Error(ctx, ErrValidationApprAcctRequest, err.Error())
		return
	}
	if req.Limit == 0 {
		req.Limit = math.MaxInt32
	}
	var request domain.ApprAcctsRequest

	request.Year = req.Year

	res, err := ph.svc.GetappracctRepo3(ctx, request, req.MetaDataRequest)
	if err != nil {
		log.Error(ctx, err)
		apierrors.HandleDBError(ctx, err)
		log.Error(ctx, "Get ApprAcct2 Repo call failed: %s", err.Error())
		return
	}

	rsp := response.NewGetAppraccts3Response(res)

	metadata := port.NewMetaDataResponse(req.Skip, req.Limit, len(rsp))

	apiRsp := response.GetAppraccts3Response{
		StatusCodeAndMessage: port.ListSuccess,
		MetaDataResponse:     metadata,
		Data:                 rsp,
	}
	log.Debug(ctx, "ListApprAcctsTwoHandler response", apiRsp)
	handleSuccess(ctx, apiRsp)

}

type getRemYearRequest struct {
	Financial_year string `uri:"financial-year" validate:"required,min=1,max=4"`
	port.MetaDataRequest
}

// Getremuneration Calculated By Year godoc
//
//	@Summary		Get Remuneration calculated details by year
//	@Description	Get Remuneration calculated details by year
//	@Tags			Public Account
//	@Accept			json
//	@Produce		json
//	@Param			financial-year	path		string			true	"financial-year"
//
// @Param       skip    query       int     			false   		"Number of records to skip for pagination"
// @Param       limit   query       int     			false   		"Number of records to limit for pagination"
//
//	@Success		200					{object}	response.GetRemunerationCreationResponse	"list retrieved successfully"
//	@Failure		400					{object}	apierrors.APIErrorResponse						"Validation error"
//	@Failure		401					{object}	apierrors.APIErrorResponse						"Unauthorized error"
//	@Failure		403					{object}	apierrors.APIErrorResponse						"Forbidden error"
//	@Failure		404					{object}	apierrors.APIErrorResponse						"Data not found error"
//	@Failure		409					{object}	apierrors.APIErrorResponse						"Data conflict error"
//	@Failure		500					{object}	apierrors.APIErrorResponse						"Internal server error"
//	@Router			/v1/public-acct/remuneration-calculated-year/{financial-year} [get]
func (hh *PublicAcctHandler) ListRemunerationCalculatedYearDetailHandler(ctx *gin.Context) {

	var req getRemYearRequest
	if err := ctx.ShouldBindUri(&req); err != nil {
		apierrors.HandleBindingError(ctx, err)
		log.Error(ctx, "Binding failed for broadsheetRequestUriv2: %s", err.Error())
		return
	}
	if err1 := ctx.ShouldBindQuery(&req); err1 != nil {
		apierrors.HandleBindingError(ctx, err1)
		log.Error(ctx, ErrBindingDdoListRequest, err1.Error())
		return
	}
	if err := validation.ValidateStruct(req); err != nil {
		apierrors.HandleValidationError(ctx, err)
		log.Error(ctx, "Validation failed for GetRemRequest: %s", err.Error())
		return
	}
	if req.Limit == 0 {
		req.Limit = math.MaxInt32
	}
	request := domain.GetRemYearRequest{
		Financial_year: req.Financial_year,
	}
	u, err := hh.svc.RemunerationCalculatedYearRepo(ctx, &request, req.MetaDataRequest)
	if err != nil {
		apierrors.HandleDBError(ctx, err)
		log.Error(ctx, "Get Remuneration Repo call failed: %s", err.Error())
		return
	}

	rsp := response.NewRemunerationCreationResponse(u)
	metadata := port.NewMetaDataResponse(req.Skip, req.Limit, len(rsp))

	apiRsp := response.GetRemunerationCreationResponse{
		StatusCodeAndMessage: port.ListSuccess,
		MetaDataResponse:     metadata,
		Data:                 rsp,
	}
	log.Debug(ctx, "ListRemunerationRateDetailHandler response", apiRsp)
	handleSuccess(ctx, apiRsp)

}
