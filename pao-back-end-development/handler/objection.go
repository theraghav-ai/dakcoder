package handler

import (
	"gotemplate/core/domain"
	"gotemplate/core/port"
	"gotemplate/handler/response"
	repo "gotemplate/repo/postgres"
	"math"
	"time"

	"github.com/gin-gonic/gin"
	"github.com/volatiletech/null/v9"
	apierrors "gitlab.cept.gov.in/it-2.0-common/api-errors"
	log "gitlab.cept.gov.in/it-2.0-common/api-log"
	validation "gitlab.cept.gov.in/it-2.0-common/api-validation"
)

type ObjectionHandler struct {
	svc *repo.ObjectionRepository
}

// NewUserHandler creates a new UserHandler instance
func NewObjectionHandler(svc *repo.ObjectionRepository) *ObjectionHandler {
	return &ObjectionHandler{
		svc,
	}
}

type ObjectionRequest struct {
	PaoCode     string                  `json:"pao_code" select:"pao_code" insert:"pao_code"  validate:"required,validatePaocode"`
	DdoCode     string                  `json:"ddo_code" select:"ddo_code" insert:"ddo_code"  validate:"required,validateDdocode"`
	Description string                  `json:"description" select:"description" insert:"description" validate:"required,max=255"`
	ObjectionId string                  `json:"objection_id" select:"objection_id" insert:"objection_id,max=25"`
	CreatedBy   uint64                  `json:"created_by" select:"created_by" insert:"created_by" validate:"required,employee_id"`
	CreatedDate time.Time               `json:"created_date" select:"created_date" insert:"created_date" validate:"omitempty,validateDateTime"`
	Remarks     []ObjectionRemarkcreate `json:"remarks" select:"remarks" insert:"remarks" validate:"dive"`
	StatusFlag  string                  `json:"status_flag" select:"status_flag" insert:"status_flag" validate:"required,oneof=created paoupdated rejected ddoupdated closed"`
}
type ObjectionRemarkcreate struct {
	Data              string    `json:"data" validate:"omitempty,max=255"`
	CommentedBy       uint64    `json:"commented_by" validate:"omitempty,employee_id"`
	CommentedDate     time.Time `json:"commented_date" validate:"omitempty"`
	CommentedOfficeId uint64    `json:"commented_office_id" validate:"omitempty,office_id"`
	Filepath          string    `json:"filepath" validate:"omitempty,max=255"`
	Sender            string    `json:"sender" validate:"omitempty,max=255"`
	// EcmsTransactionId string    `json:"ecms_transaction_id,omitempty" validate:"omitempty,max=255"`
	// EcmsServiceName   string    `json:"ecms_service_name,omitempty" validate:"omitempty,max=255"`
}

//While creating Objection, remark is not required, but data and commentedby is required for update valiation, hence created separete ObjectionRemark for creation and updation.

type ObjectionRemark struct {
	Data              string    `json:"data" validate:"required,max=255"`
	CommentedBy       uint64    `json:"commented_by" validate:"required,employee_id"`
	CommentedDate     time.Time `json:"commented_date" validate:"omitempty"`
	CommentedOfficeId uint64    `json:"commented_office_id" validate:"omitempty,office_id"`
	Filepath          string    `json:"filepath" validate:"omitempty,max=255"`
	Sender            string    `json:"sender" validate:"omitempty,max=255"`
		// EcmsTransactionId string    `json:"ecms_transaction_id,omitempty" validate:"omitempty,max=255"`
		// EcmsServiceName   string    `json:"ecms_service_name,omitempty" validate:"omitempty,max=255"`
}

// CreateObjectionHandler godoc
//
//	@Summary		Create Objection
//	@Description	Create Objection
//	@Tags			OBJECTION
//	@Accept			json
//	@Produce		json
//	@Param			body	body		ObjectionRequest	true	"Objection Creation Request"
//	@Success		201		{object}	response.ObjectionCreationResponse			"resource created successfully"
//	@Failure		400					{object}	apierrors.APIErrorResponse						"Validation error"
//	@Failure		401					{object}	apierrors.APIErrorResponse						"Unauthorized error"
//	@Failure		403					{object}	apierrors.APIErrorResponse						"Forbidden error"
//	@Failure		404					{object}	apierrors.APIErrorResponse						"Data not found error"
//	@Failure		409					{object}	apierrors.APIErrorResponse						"Data conflict error"
//	@Failure		500					{object}	apierrors.APIErrorResponse						"Internal server error"
//	@Router			/v1/objection [post]
func (uh *ObjectionHandler) CreateObjectionHandler(ctx *gin.Context) {

	var request ObjectionRequest
	if err := ctx.ShouldBindJSON(&request); err != nil {
		apierrors.HandleBindingError(ctx, err)
		log.Error(ctx, "Binding failed for ObjectionRequest: %s", err.Error())
		return
	}
	if err := validation.ValidateStruct(request); err != nil {
		apierrors.HandleValidationError(ctx, err)
		log.Error(ctx, "Validation failed for ObjectionRequest: %s", err.Error())
		return
	}

	req := domain.ObjectionRequest{
		PaoCode:     request.PaoCode,
		DdoCode:     request.DdoCode,
		Description: request.Description,
		ObjectionId: request.ObjectionId,
		CreatedBy:   request.CreatedBy,
		CreatedDate: request.CreatedDate,
		Remarks:     convertObjectionRemarkToDomainObjectionRemarkRequestCreate(request.Remarks),
		StatusFlag:  request.StatusFlag,
	}
	p, err := uh.svc.ObjectionCreationRepo(ctx, &req)
	if err != nil {
		apierrors.HandleDBError(ctx, err)
		log.Error(ctx, "Objection Creation Repo call failed: %s", err.Error())
		return
	}
	rsp := response.NewObjectionCreationResponse(p)

	apiRsp := response.ObjectionCreationResponse{
		StatusCodeAndMessage: port.CreateSuccess,
		Data:                 rsp,
	}
	log.Debug(ctx, "CreateObjectionHandler response", apiRsp)
	handleCreateSuccess(ctx, apiRsp)

}

type ObjectionByPaocode struct {
	PaoCode string `form:"pao_code" validate:"required,validatePaocode"`
	Status  string `form:"status" validate:"required,oneof=created paoupdated rejected ddoupdated closed"`
}

type ObjectionByPaoCodeRpt struct {
	PaoCode  string `form:"pao_code" validate:"required,validatePaocode"`
	FromDate string `form:"from_date" validate:"required,validateDateTime"`
	ToDate   string `form:"to_date" validate:"required,validateDateTime"`
	Status   string `form:"status" validate:"required,oneof=created paoupdated rejected ddoupdated closed"`
}

type ObjectionByDdoCode struct {
	DdoCode string `form:"ddo_code" validate:"required,validateDdocode"`
	Status  string `form:"status" validate:"required,oneof=created paoupdated rejected ddoupdated closed"`
}

type ObjectionByDdoCodeRpt struct {
	DdoCode  string `form:"ddo_code" validate:"required,validateDdocode"`
	FromDate string `form:"from_date" validate:"required,validateDateTime"`
	ToDate   string `form:"to_date" validate:"required,validateDateTime"`
	Status   string `form:"status" validate:"required,oneof=created paoupdated rejected ddoupdated closed"`
}

type ObjectionId struct {
	ObjectionId string `uri:"objection-id" binding:"required" validate:"required,min=1,max=30"`
}

const ErrBindingObjectionID = "Binding failed for ObjectionId: %s"
const ErrValidationObjectionID = "Validation failed for ObjectionId: %s"

// FetchObjectionByIdHandler godoc
//
//	@Summary		Get Objection details
//	@Description	Get Objection details based on ID
//	@Tags			OBJECTION
//	@Accept			json
//	@Produce		json
//	@Param			objection-id			path		string									true	"objection-id"
//
// @Param       skip    query       int     			false   		"Number of records to skip for pagination"
// @Param       limit   query       int     			false   		"Number of records to limit for pagination"
//
//	@Success		200					{object}	response.ObjectionPaoByIdResponse	"data retrieved successfully"
//	@Failure		400					{object}	apierrors.APIErrorResponse						"Validation error"
//	@Failure		401					{object}	apierrors.APIErrorResponse						"Unauthorized error"
//	@Failure		403					{object}	apierrors.APIErrorResponse						"Forbidden error"
//	@Failure		404					{object}	apierrors.APIErrorResponse						"Data not found error"
//	@Failure		409					{object}	apierrors.APIErrorResponse						"Data conflict error"
//	@Failure		500					{object}	apierrors.APIErrorResponse						"Internal server error"
//	@Router			/v1/objection/{objection-id}/details [get]
func (uh *ObjectionHandler) FetchObjectionByIdHandler(ctx *gin.Context) {

	var req ObjectionId
	if err := ctx.ShouldBindUri(&req); err != nil {
		apierrors.HandleBindingError(ctx, err)
		log.Error(ctx, ErrBindingObjectionID, err.Error())
		return
	}
	if err := validation.ValidateStruct(req); err != nil {
		apierrors.HandleValidationError(ctx, err)
		log.Error(ctx, ErrValidationObjectionID, err.Error())
		return
	}
	request := domain.Objection{
		ObjectionId: null.StringFrom(req.ObjectionId),
	}
	u, err := uh.svc.ObjectionPaoByIdRepo(ctx, &request)
	if err != nil {
		apierrors.HandleDBError(ctx, err)
		log.Error(ctx, "Get Objection by Id Repo call failed: %s", err.Error())
		return
	}

	rsp := response.NewObjectionPaoByIdResponse(u)

	metadata := port.NewMetaDataResponse(0, 0, len(rsp))

	apiRsp := response.ObjectionPaoByIdResponse{
		StatusCodeAndMessage: port.FetchSucess,
		MetaDataResponse:     metadata,
		Data:                 rsp,
	}
	log.Debug(ctx, "FetchObjectionByIdHandler response", apiRsp)
	handleSuccess(ctx, apiRsp)

}

type Objectioncomment struct {
	ObjectionId string          `json:"objection_id" select:"objection_id" insert:"objection_id" validate:"required,max=30"`
	Remark      ObjectionRemark `json:"remarks" select:"remarks" insert:"remarks" validate:"required"`
	UpdatedBy   uint64          `json:"updated_by" select:"last_updated_by" insert:"last_updated_by" validate:"required,employee_id"`
	UpdatedDate time.Time       `json:"updated_date" select:"last_updated_date" insert:"last_updated_date" validate:"omitempty"`
	StatusFlag  string          `json:"status_flag" select:"status_flag" insert:"status_flag" validate:"required,oneof=created paoupdated rejected ddoupdated closed"`
}

// UpdateObjectionHandler godoc
//
//	@Summary		Update Objections
//	@Description	Update Objections
//	@Tags			OBJECTION
//	@Accept			json
//	@Produce		json
//	@Param			objection-id			path		string									true	"Objection_id"
//
// @Param			body	body		Objectioncomment	true	"Objection Update Request"
// @Success		200					{object}	response.ObjectionCreationResponse	"resource updated successfully"
// @Failure		400					{object}	apierrors.APIErrorResponse						"Validation error"
// @Failure		401					{object}	apierrors.APIErrorResponse						"Unauthorized error"
// @Failure		403					{object}	apierrors.APIErrorResponse						"Forbidden error"
// @Failure		404					{object}	apierrors.APIErrorResponse						"Data not found error"
// @Failure		409					{object}	apierrors.APIErrorResponse						"Data conflict error"
// @Failure		500					{object}	apierrors.APIErrorResponse						"Internal server error"
// @Router			/v1/objection/{objection-id}/remarks [put]
func (uh *ObjectionHandler) UpdateObjectionHandler(ctx *gin.Context) {

	var req ObjectionId
	if err := ctx.ShouldBindUri(&req); err != nil {
		apierrors.HandleBindingError(ctx, err)
		log.Error(ctx, ErrBindingObjectionID, err.Error())
		return
	}

	var request Objectioncomment
	request.ObjectionId = req.ObjectionId
	if err := ctx.ShouldBindJSON(&request); err != nil {
		apierrors.HandleBindingError(ctx, err)
		log.Error(ctx, ErrValidationObjectionID, err.Error())
		return
	}
	if err := validation.ValidateStruct(request); err != nil {
		apierrors.HandleValidationError(ctx, err)
		return
	}
	currentTime := time.Now()

	requ := domain.Objectioncomment{
		ObjectionId: request.ObjectionId,
		Remark:      convertObjectionRemarkToDomainObjectionRemark(request.Remark),
		UpdatedBy:   request.UpdatedBy,
		UpdatedDate: currentTime,
		StatusFlag:  request.StatusFlag,
	}
	p, err := uh.svc.ObjectionUpdateRepo(ctx, &requ)
	if err != nil {
		apierrors.HandleDBError(ctx, err)
		log.Error(ctx, "Objection Update Repo call failed: %s", err.Error())
		return
	}
	rsp := response.NewObjectionCreationResponse(p)

	apiRsp := response.ObjectionCreationResponse{
		StatusCodeAndMessage: port.UpdateSuccess,
		Data:                 rsp,
	}
	log.Debug(ctx, "UpdateObjectionHandler response", apiRsp)
	handleSuccess(ctx, apiRsp)

}

type ObjectionClosure struct {
	ObjectionId   string          `json:"objection_id" select:"objection_id" insert:"objection_id" validate:"required,max=30"`
	StatusFlag    string          `json:"status_flag" select:"status_flag" insert:"status_flag" validate:"required,oneof=created paoupdated rejected ddoupdated closed"`
	ClosureRemark ObjectionRemark `json:"remarks" select:"remarks" insert:"remarks" validate:"required"`
	ClosedBy      uint64          `json:"closed_by" select:"last_updated_by" insert:"last_updated_by" validate:"required,employee_id"`
	ClosedDate    time.Time       `json:"closed_date" select:"last_updated_date" insert:"last_updated_date" validate:"omitempty,validateDateTime"`
}

// UpdateObjectionClosureHandler godoc
//
//	@Summary		Close Objections
//	@Description	Close Objections
//	@Tags			OBJECTION
//	@Accept			json
//	@Produce		json
//	@Param			objection-id			path		string									true	"Objection_id"
//	@Param			body	body		ObjectionClosure	true	"Objection Closure Request"
//	@Success		200					{object}	response.ObjectionCreationResponse	"resource deleted successfully"
//	@Failure		400					{object}	apierrors.APIErrorResponse						"Validation error"
//	@Failure		401					{object}	apierrors.APIErrorResponse						"Unauthorized error"
//	@Failure		403					{object}	apierrors.APIErrorResponse						"Forbidden error"
//	@Failure		404					{object}	apierrors.APIErrorResponse						"Data not found error"
//	@Failure		409					{object}	apierrors.APIErrorResponse						"Data conflict error"
//	@Failure		500					{object}	apierrors.APIErrorResponse						"Internal server error"
//	@Router			/v1/objection/{objection-id}/closure [put]
func (uh *ObjectionHandler) UpdateObjectionClosureHandler(ctx *gin.Context) {

	var req ObjectionId
	if err := ctx.ShouldBindUri(&req); err != nil {
		apierrors.HandleBindingError(ctx, err)
		log.Error(ctx, ErrBindingObjectionID, err.Error())
		return
	}

	var request ObjectionClosure
	request.ObjectionId = req.ObjectionId
	if err := ctx.ShouldBindJSON(&request); err != nil {
		apierrors.HandleBindingError(ctx, err)
		log.Error(ctx, "Binding failed for ObjectionClosure: %s", err.Error())
		return
	}
	if err := validation.ValidateStruct(req); err != nil {
		apierrors.HandleValidationError(ctx, err)
		log.Error(ctx, "Validation failed for ObjectionClosure: %s", err.Error())
		return
	}
	currentTime := time.Now()

	requ := domain.ObjectionClosure{
		ObjectionId:   request.ObjectionId,
		ClosureRemark: convertObjectionRemarkToDomainObjectionRemark(request.ClosureRemark),
		ClosedBy:      request.ClosedBy,
		ClosedDate:    currentTime,
		StatusFlag:    request.StatusFlag,
	}
	p, err := uh.svc.ObjectionClosureRepo(ctx, &requ)
	if err != nil {
		apierrors.HandleDBError(ctx, err)
		log.Error(ctx, "Objection Closure Repo call failed: %s", err.Error())
		return
	}
	rsp := response.NewObjectionCreationResponse(p)

	apiRsp := response.ObjectionCreationResponse{
		StatusCodeAndMessage: port.DeleteSuccess,
		Data:                 rsp,
	}
	log.Debug(ctx, "UpdateObjectionClosureHandler response", apiRsp)
	handleSuccess(ctx, apiRsp)

}

type ObjectionPraoRequest struct {
	PraoCode    string                  `json:"prao_code" select:"prao_code" insert:"prao_code" validate:"required,max=10"`
	PaoCode     string                  `json:"pao_code" select:"pao_code" insert:"pao_code" validate:"required,validatePaocode"`
	Description string                  `json:"description" select:"description" insert:"description" validate:"required,max=255"`
	ObjectionId string                  `json:"objection_id" select:"objection_id" insert:"objection_id" validate:"omitempty,max=30"`
	CreatedBy   uint64                  `json:"created_by" select:"created_by" insert:"created_by" validate:"required,employee_id"`
	CreatedDate time.Time               `json:"created_date" select:"created_date" insert:"created_date" validate:"required"`
	Remarks     []ObjectionRemarkcreate `json:"remarks" select:"remarks" insert:"remarks" validate:"dive"`
	StatusFlag  string                  `json:"status_flag" select:"status_flag" insert:"status_flag" validate:"required,oneof=created paoupdated rejected ddoupdated closed"`
}

// ObjectionPraoCreation godoc
//
//	@Summary		Create Objection at PRAO
//	@Description	Create Objection at PRAO
//	@Tags			OBJECTION
//	@Accept			json
//	@Produce		json
//	@Param			body	body		ObjectionPraoRequest	true	"Objection Prao Creation Request"
//	@Success		201		{object}	response.ObjectionCreationPraoResponse			"resource created successfully"
//	@Failure		400					{object}	apierrors.APIErrorResponse						"Validation error"
//	@Failure		401					{object}	apierrors.APIErrorResponse						"Unauthorized error"
//	@Failure		403					{object}	apierrors.APIErrorResponse						"Forbidden error"
//	@Failure		404					{object}	apierrors.APIErrorResponse						"Data not found error"
//	@Failure		409					{object}	apierrors.APIErrorResponse						"Data conflict error"
//	@Failure		500					{object}	apierrors.APIErrorResponse						"Internal server error"
//	@Router			/v1/objection/prao [post]
func (uh *ObjectionHandler) CreateObjectionPraoHandler(ctx *gin.Context) {

	var request ObjectionPraoRequest
	if err := ctx.ShouldBindJSON(&request); err != nil {
		apierrors.HandleBindingError(ctx, err)
		log.Error(ctx, "Binding failed for ObjectionPraoRequest: %s", err.Error())
		return
	}
	if err := validation.ValidateStruct(request); err != nil {
		apierrors.HandleValidationError(ctx, err)
		log.Error(ctx, "Validation failed for ObjectionPraoRequest: %s", err.Error())
		return
	}

	req := domain.ObjectionPraoRequest{

		PraoCode:    request.PraoCode,
		PaoCode:     request.PaoCode,
		Description: request.Description,
		ObjectionId: request.ObjectionId,
		CreatedBy:   request.CreatedBy,
		CreatedDate: request.CreatedDate,
		Remarks:     convertObjectionRemarkToDomainObjectionRemarkRequestCreate(request.Remarks),
		StatusFlag:  request.StatusFlag,
	}

	p, err := uh.svc.ObjectionCreationPraoRepo(ctx, &req)
	if err != nil {
		apierrors.HandleDBError(ctx, err)
		log.Error(ctx, "Objection Prao Creation Repo call failed: %s", err.Error())
		return
	}
	rsp := response.NewObjectionCreationPraoResponse(p)

	apiRsp := response.ObjectionCreationPraoResponse{
		StatusCodeAndMessage: port.CreateSuccess,
		Data:                 rsp,
	}
	log.Debug(ctx, "CreateObjectionPraoHandler response", apiRsp)
	handleCreateSuccess(ctx, apiRsp)

}

type ObjectionByPraoCode struct {
	PraoCode string `form:"prao_code" validate:"required,min=0,max=10" example:"0"`
	Status   string `form:"status" validate:"required,oneof=created paoupdated rejected ddoupdated closed"`
}

type ObjectionByPraoCodeReport struct {
	PraoCode string `form:"prao_code" validate:"required,min=0,max=10" example:"0"`
	FromDate string `form:"from_date" validate:"required,validateDateTime"`
	ToDate   string `form:"to_date" validate:"required,validateDateTime"`
	Status   string `form:"status" validate:"required,oneof=created paoupdated rejected ddoupdated closed"`
}

// FetchObjectionPraoByIdHandler godoc
//
//	@Summary		Get Objections PRAO
//	@Description	Get Objections PRAO details by its ID
//	@Tags			OBJECTION
//	@Accept			json
//	@Produce		json
//	@Param			objection-id			path		string									true	"Objection_id"
//
// @Param       skip    query       int     			false   		"Number of records to skip for pagination"
// @Param       limit   query       int     			false   		"Number of records to limit for pagination"
//
//	@Success		200					{object}	response.ObjectionPraoByIdResponse	"data retrieved successfully"
//	@Failure		400					{object}	apierrors.APIErrorResponse						"Validation error"
//	@Failure		401					{object}	apierrors.APIErrorResponse						"Unauthorized error"
//	@Failure		403					{object}	apierrors.APIErrorResponse						"Forbidden error"
//	@Failure		404					{object}	apierrors.APIErrorResponse						"Data not found error"
//	@Failure		409					{object}	apierrors.APIErrorResponse						"Data conflict error"
//	@Failure		500					{object}	apierrors.APIErrorResponse						"Internal server error"
//	@Router			/v1/objection/{objection-id}/prao/details [get]
func (uh *ObjectionHandler) FetchObjectionPraoByIdHandler(ctx *gin.Context) {

	var req ObjectionId
	if err := ctx.ShouldBindUri(&req); err != nil {
		apierrors.HandleBindingError(ctx, err)
		log.Error(ctx, ErrBindingObjectionID, err.Error())
		return
	}
	if err := validation.ValidateStruct(req); err != nil {
		apierrors.HandleValidationError(ctx, err)
		log.Error(ctx, ErrValidationObjectionID, err.Error())
		return
	}
	request := domain.ObjectionPrao{
		ObjectionId: null.StringFrom(req.ObjectionId),
	}
	u, err := uh.svc.ObjectionPraoByIdRepo(ctx, &request)
	if err != nil {
		apierrors.HandleDBError(ctx, err)
		log.Error(ctx, "Get Objection Prao by Id Repo call failed: %s", err.Error())
		return
	}

	rsp := response.NewObjectionPraoByIdResponse(u)

	metadata := port.NewMetaDataResponse(0, 0, len(rsp))

	apiRsp := response.ObjectionPraoByIdResponse{
		StatusCodeAndMessage: port.FetchSucess,
		MetaDataResponse:     metadata,
		Data:                 rsp,
	}
	log.Debug(ctx, "FetchObjectionPraoByIdHandler response", apiRsp)
	handleSuccess(ctx, apiRsp)

}

type ObjectionComment struct {
	ObjectionId string `uri:"objection-id" binding:"required" validate:"required,max=30"`
}
type ObjectionPraocomment struct {
	ObjectionId string          `json:"objection_id" select:"objection_id" insert:"objection_id" validate:"required,max=30"`
	Remark      ObjectionRemark `json:"remarks" select:"remarks" insert:"remarks" validate:"required"`
	UpdatedBy   uint64          `json:"updated_by" select:"last_updated_by" insert:"last_updated_by" validate:"required,employee_id"`
	UpdatedDate time.Time       `json:"updated_date" select:"last_updated_date" insert:"last_updated_date" validate:"omitempty"`
	StatusFlag  string          `json:"status_flag" select:"status_flag" insert:"status_flag" validate:"required,oneof=created praoupdated rejected paoupdated ddoupdated closed"`
}

const ErrBindingObjectionComment = "Binding failed for ObjectionComment: %s"

// UpdateObjectionPraoHandler godoc
//
//	@Summary		Update Objections PRAO
//	@Description	Update Objections PRAO
//	@Tags			OBJECTION
//	@Accept			json
//	@Produce		json
//	@Param			objection-id			path		string									true	"Objection_id"
//	@Param			body	body		Objectioncomment	true	"Objection Update Request"
//	@Success		200					{object}	response.ObjectionCreationPraoResponse	"resource updated successfully"
//	@Failure		400					{object}	apierrors.APIErrorResponse						"Validation error"
//	@Failure		401					{object}	apierrors.APIErrorResponse						"Unauthorized error"
//	@Failure		403					{object}	apierrors.APIErrorResponse						"Forbidden error"
//	@Failure		404					{object}	apierrors.APIErrorResponse						"Data not found error"
//	@Failure		409					{object}	apierrors.APIErrorResponse						"Data conflict error"
//	@Failure		500					{object}	apierrors.APIErrorResponse						"Internal server error"
//	@Router			/v1/objection/{objection-id}/prao/remarks [put]
func (uh *ObjectionHandler) UpdateObjectionPraoHandler(ctx *gin.Context) {

	var req ObjectionComment
	if err := ctx.ShouldBindUri(&req); err != nil {
		apierrors.HandleBindingError(ctx, err)
		log.Error(ctx, ErrBindingObjectionComment, err.Error())
		return
	}

	var request ObjectionPraocomment
	request.ObjectionId = req.ObjectionId
	if err := ctx.ShouldBindJSON(&request); err != nil {
		apierrors.HandleBindingError(ctx, err)
		log.Error(ctx, ErrBindingObjectionComment, err.Error())
		return
	}
	if err := validation.ValidateStruct(request); err != nil {
		apierrors.HandleValidationError(ctx, err)
		log.Error(ctx, "Validation failed for ObjectionComment: %s", err.Error())
		return
	}
	currentTime := time.Now()
	requ := domain.Objectioncomment{
		ObjectionId: request.ObjectionId,
		Remark:      convertObjectionRemarkToDomainObjectionRemark(request.Remark),
		UpdatedBy:   request.UpdatedBy,
		UpdatedDate: currentTime,
		StatusFlag:  request.StatusFlag,
	}
	p, err := uh.svc.ObjectionUpdatePraoRepo(ctx, &requ)
	if err != nil {
		apierrors.HandleDBError(ctx, err)
		log.Error(ctx, "Objection Prao Update Repo call failed: %s", err.Error())
		return
	}
	rsp := response.NewObjectionCreationPraoResponse(p)

	apiRsp := response.ObjectionCreationPraoResponse{
		StatusCodeAndMessage: port.UpdateSuccess,
		Data:                 rsp,
	}
	log.Debug(ctx, "UpdateObjectionPraoHandler response", apiRsp)
	handleSuccess(ctx, apiRsp)

}

// UpdateObjectionClosurePraoHandler godoc
//
//	@Summary		Close Objections PRAO
//	@Description	Close Objections PRAO
//	@Tags			OBJECTION
//	@Accept			json
//	@Produce		json
//	@Param			objection-id			path		string									true	"Objection_id"
//	@Param			body	body		ObjectionClosure	true	"Objection Update Request"
//	@Success		200					{object}	response.ObjectionCreationPraoResponse	"resource deleted successfully"
//	@Failure		400					{object}	apierrors.APIErrorResponse						"Validation error"
//	@Failure		401					{object}	apierrors.APIErrorResponse						"Unauthorized error"
//	@Failure		403					{object}	apierrors.APIErrorResponse						"Forbidden error"
//	@Failure		404					{object}	apierrors.APIErrorResponse						"Data not found error"
//	@Failure		409					{object}	apierrors.APIErrorResponse						"Data conflict error"
//	@Failure		500					{object}	apierrors.APIErrorResponse						"Internal server error"
//	@Router			/v1/objection/{objection-id}/prao/closure [put]
func (uh *ObjectionHandler) UpdateObjectionClosurePraoHandler(ctx *gin.Context) {

	var req ObjectionComment
	if err := ctx.ShouldBindUri(&req); err != nil {
		apierrors.HandleBindingError(ctx, err)
		log.Error(ctx, "Binding failed for ObjectionComment: %s", err.Error())
		return
	}

	var request ObjectionClosure
	request.ObjectionId = req.ObjectionId
	if err := ctx.ShouldBindJSON(&request); err != nil {
		apierrors.HandleBindingError(ctx, err)
		log.Error(ctx, "Binding failed for ObjectionClosure: %s", err.Error())
		return
	}
	if err := validation.ValidateStruct(request); err != nil {
		apierrors.HandleValidationError(ctx, err)
		log.Error(ctx, "Validation failed for ObjectionClosure: %s", err.Error())
		return
	}
	currentTime := time.Now()

	requ := domain.ObjectionClosure{
		ObjectionId:   request.ObjectionId,
		ClosureRemark: convertObjectionRemarkToDomainObjectionRemark(request.ClosureRemark),
		ClosedBy:      request.ClosedBy,
		ClosedDate:    currentTime,
		StatusFlag:    request.StatusFlag,
	}
	p, err := uh.svc.ObjectionClosurePraoRepo(ctx, &requ)
	if err != nil {
		apierrors.HandleDBError(ctx, err)
		log.Error(ctx, "Objection Prao Closure Repo call failed: %s", err.Error())
		return
	}
	rsp := response.NewObjectionCreationPraoResponse(p)

	apiRsp := response.ObjectionCreationPraoResponse{
		StatusCodeAndMessage: port.DeleteSuccess,
		Data:                 rsp,
	}
	log.Debug(ctx, "UpdateObjectionClosurePraoHandler response", apiRsp)
	handleSuccess(ctx, apiRsp)

}

type ObjectionByCode struct {
	Code   string `form:"code" validate:"required,max=30"`
	Type   int64  `form:"type" validate:"required,min=1,max=2"`
	Status string `form:"status" validate:"required,oneof=created paoupdated rejected ddoupdated closed notclosed"`
	port.MetaDataRequest
}

// ListObjectionCodeHandler godoc
//
//	@Summary		Get Objection List
//	@Description	Get Objection List
//	@Tags			HOA
//	@Accept			json
//	@Produce		json
//	@Param			type				query		string									true	"type = 1 for objections under PAO, type =2 for objections under DDO "
//	@Param			code			query		string									true	"Give PAO_code if type =1 else give DDO_code if type = 2"
//	@Param			status				query		string									true	"status of the objections"
//
// @Param       skip    query       int     			false   		"Number of records to skip for pagination"
// @Param       limit   query       int     			false   		"Number of records to limit for pagination"
//
//	@Success		200					{object}	response.ObjectionCodeResponse	"list retrieved successfully"
//	@Failure		400					{object}	apierrors.APIErrorResponse						"Validation error"
//	@Failure		401					{object}	apierrors.APIErrorResponse						"Unauthorized error"
//	@Failure		403					{object}	apierrors.APIErrorResponse						"Forbidden error"
//	@Failure		404					{object}	apierrors.APIErrorResponse						"Data not found error"
//	@Failure		409					{object}	apierrors.APIErrorResponse						"Data conflict error"
//	@Failure		500					{object}	apierrors.APIErrorResponse						"Internal server error"
//	@Router			/v1/objection/pao/code [get]
func (uh *ObjectionHandler) ListObjectionCodeHandler(ctx *gin.Context) {

	var req ObjectionByCode
	if err := ctx.ShouldBindQuery(&req); err != nil {
		apierrors.HandleBindingError(ctx, err)
		log.Error(ctx, "Binding failed for ObjectionByCode: %s", err.Error())
		return
	}
	if err := validation.ValidateStruct(req); err != nil {
		apierrors.HandleValidationError(ctx, err)
		log.Error(ctx, "Validation failed for ObjectionByCode: %s", err.Error())
		return
	}

	if req.Type == 1 {
		request := domain.Objection{
			PaoCode:    null.StringFrom(req.Code),
			StatusFlag: null.StringFrom(req.Status),
		}
		if req.Limit == 0 {
			req.Limit = math.MaxInt32
		}
		u, err := uh.svc.ObjectionPaocodeRepo(ctx, &request, req.MetaDataRequest)
		if err != nil {
			apierrors.HandleDBError(ctx, err)
			log.Error(ctx, "Get Objections Repo call failed: %s", err.Error())
		}

		rsp := response.NewObjectionCodeResponse(u)

		metadata := port.NewMetaDataResponse(req.Skip, req.Limit, len(rsp))

		apiRsp := response.ObjectionCodeResponse{
			StatusCodeAndMessage: port.FetchSucess,
			MetaDataResponse:     metadata,
			Data:                 rsp,
		}
		log.Debug(ctx, "ListObjectionCodeHandler response", apiRsp)
		handleSuccess(ctx, apiRsp)
	}
	if req.Type == 2 {
		request := domain.Objection{
			DdoCode:    null.StringFrom(req.Code),
			StatusFlag: null.StringFrom(req.Status),
		}
		if req.Limit == 0 {
			req.Limit = math.MaxInt32
		}
		u, err := uh.svc.ObjectionDdocodeRepo(ctx, &request, req.MetaDataRequest)
		if err != nil {
			apierrors.HandleDBError(ctx, err)
			log.Error(ctx, "Get Objections Repo call failed: %s", err.Error())
			return
		}

		rsp := response.NewObjectionCodeResponse(u)

		metadata := port.NewMetaDataResponse(req.Skip, req.Limit, len(rsp))

		apiRsp := response.ObjectionCodeResponse{
			StatusCodeAndMessage: port.FetchSucess,
			MetaDataResponse:     metadata,
			Data:                 rsp,
		}
		log.Debug(ctx, "ListObjectionCodeHandler response", apiRsp)
		handleSuccess(ctx, apiRsp)
	}

}

type ObjectionByPraoCodeRequest struct {
	Code   string `form:"code" validate:"required,min=0,max=10" example:"0"`
	Status string `form:"status" validate:"required,oneof=created paoupdated rejected praoupdated closed notclosed"`
	Type   int64  `form:"type" validate:"required,min=1,max=2"`
	port.MetaDataRequest
}

// ListObjectionPraoCodeHandler godoc
//
//	@Summary		Get Objection Prao List
//	@Description	Get Objection Prao List
//	@Tags			HOA
//	@Accept			json
//	@Produce		json
//	@Param			type				query		string									true	"type = 1 for PRAO objections under PRAO, type =2 for PRAO objections under PAO "
//	@Param			code			query		string									true	"Give PRAO_code if type =1 else give PAO_code if type = 2"
//	@Param			status				query		string									true	"status of the objections"
//
// @Param       skip    query       int     			false   		"Number of records to skip for pagination"
// @Param       limit   query       int     			false   		"Number of records to limit for pagination"
//
//	@Success		200					{object}	response.ObjectionPraoCodeResponse	"list retrieved successfully"
//	@Failure		400					{object}	apierrors.APIErrorResponse						"Validation error"
//	@Failure		401					{object}	apierrors.APIErrorResponse						"Unauthorized error"
//	@Failure		403					{object}	apierrors.APIErrorResponse						"Forbidden error"
//	@Failure		404					{object}	apierrors.APIErrorResponse						"Data not found error"
//	@Failure		409					{object}	apierrors.APIErrorResponse						"Data conflict error"
//	@Failure		500					{object}	apierrors.APIErrorResponse						"Internal server error"
//	@Router			/v1/objection/prao/code [get]
func (uh *ObjectionHandler) ListObjectionPraoCodeHandler(ctx *gin.Context) {

	var req ObjectionByPraoCodeRequest
	if err := ctx.ShouldBindQuery(&req); err != nil {
		apierrors.HandleBindingError(ctx, err)
		log.Error(ctx, "Binding failed for ObjectionByPraoCodev2: %s", err.Error())
		return
	}
	if err := validation.ValidateStruct(req); err != nil {
		apierrors.HandleValidationError(ctx, err)
		log.Error(ctx, "Validation failed for ObjectionByPraoCodev2: %s", err.Error())
		return
	}
	if req.Limit == 0 {
		req.Limit = math.MaxInt32
	}

	if req.Type == 1 {
		request := domain.ObjectionPrao{
			PraoCode:   null.StringFrom(req.Code),
			StatusFlag: null.StringFrom(req.Status),
		}
		u, err := uh.svc.ObjectionPraocodePraoRepo(ctx, &request, req.MetaDataRequest)
		if err != nil {
			apierrors.HandleDBError(ctx, err)
			log.Error(ctx, "Get Objection Prao Repo call failed: %s", err.Error())
			return
		}

		rsp := response.NewObjectionPraoCodeResponse(u)

		metadata := port.NewMetaDataResponse(req.Skip, req.Limit, len(rsp))

		apiRsp := response.ObjectionPraoCodeResponse{
			StatusCodeAndMessage: port.FetchSucess,
			MetaDataResponse:     metadata,
			Data:                 rsp,
		}
		log.Debug(ctx, "ListObjectionPraoCodeHandler response", apiRsp)
		handleSuccess(ctx, apiRsp)
	}
	if req.Type == 2 {
		request := domain.ObjectionPrao{
			PaoCode:    null.StringFrom(req.Code),
			StatusFlag: null.StringFrom(req.Status),
		}
		u, err := uh.svc.ObjectionPaocodePraoRepo(ctx, &request, req.MetaDataRequest)
		if err != nil {
			apierrors.HandleDBError(ctx, err)
			log.Error(ctx, "Get Objection Prao Repo call failed: %s", err.Error())
			return
		}

		rsp := response.NewObjectionPraoCodeResponse(u)

		metadata := port.NewMetaDataResponse(req.Skip, req.Limit, len(rsp))

		apiRsp := response.ObjectionPraoCodeResponse{
			StatusCodeAndMessage: port.FetchSucess,
			MetaDataResponse:     metadata,
			Data:                 rsp,
		}
		log.Debug(ctx, "ListObjectionPraoCodeHandler response", apiRsp)
		handleSuccess(ctx, apiRsp)
	}

}

type ObjectionRpt struct {
	Code     string `form:"code" validate:"required,max=20"`
	FromDate string `form:"from-date" validate:"required,date_yyyy_mm_dd"`
	ToDate   string `form:"to-date" validate:"required,date_yyyy_mm_dd"`
	Status   string `form:"status" validate:"required,oneof=created paoupdated rejected ddoupdated closed notclosed"`
	Type     int64  `form:"type" validate:"required,min=1,max=2"`
	port.MetaDataRequest
}

// ListObjectionPaoReportHandler godoc
//
//	@Summary		Get Objection Report
//	@Description	Get Objection Report
//	@Tags			HOA
//	@Accept			json
//	@Produce		json
//	@Param			type				query		string									true	"type = 1 for objections under PAO, type =2 for objections under DDO "
//	@Param			code			query		string									true	"Give PAO_code if type =1 else give DDO_code if type = 2"
//	@Param			status				query		string									true	"status of the objections"
//	@Param			from-date				query		string									true	"from-date"
//	@Param			to-date				query		string									true	"to-date"
//
// @Param       skip    query       int     			false   		"Number of records to skip for pagination"
// @Param       limit   query       int     			false   		"Number of records to limit for pagination"
//
//	@Success		200					{object}	response.ObjectionCodeResponse	"list retrieved successfully"
//	@Failure		400					{object}	apierrors.APIErrorResponse						"Validation error"
//	@Failure		401					{object}	apierrors.APIErrorResponse						"Unauthorized error"
//	@Failure		403					{object}	apierrors.APIErrorResponse						"Forbidden error"
//	@Failure		404					{object}	apierrors.APIErrorResponse						"Data not found error"
//	@Failure		409					{object}	apierrors.APIErrorResponse						"Data conflict error"
//	@Failure		500					{object}	apierrors.APIErrorResponse						"Internal server error"
//	@Router			/v1/objection/pao/report [get]
func (uh *ObjectionHandler) ListObjectionPaoReportHandler(ctx *gin.Context) {

	var req ObjectionRpt
	if err := ctx.ShouldBindQuery(&req); err != nil {
		apierrors.HandleBindingError(ctx, err)
		log.Error(ctx, "Binding failed for ObjectionRpt: %s", err.Error())
		return
	}
	if err := validation.ValidateStruct(req); err != nil {
		apierrors.HandleValidationError(ctx, err)
		log.Error(ctx, "Validation failed for ObjectionRpt: %s", err.Error())
		return
	}
	if req.Limit == 0 {
		req.Limit = math.MaxInt32
	}

	if req.Type == 1 {
		var request domain.ObjectionbyPaocodeReport
		request.PaoCode = req.Code
		request.FromDate = req.FromDate
		request.ToDate = req.ToDate
		request.Status = req.Status

		u, err := uh.svc.ObjectionPaocodeRepoRpt(ctx, request, req.MetaDataRequest)
		if err != nil {
			apierrors.HandleDBError(ctx, err)
			log.Error(ctx, "Objection Report Repo call failed: %s", err.Error())
			return
		}

		rsp := response.NewObjectionCodeResponse(u)

		metadata := port.NewMetaDataResponse(req.Skip, req.Limit, len(rsp))

		apiRsp := response.ObjectionCodeResponse{
			StatusCodeAndMessage: port.FetchSucess,
			MetaDataResponse:     metadata,
			Data:                 rsp,
		}
		log.Debug(ctx, "ListObjectionPaoReportHandler response", apiRsp)
		handleSuccess(ctx, apiRsp)
	}
	if req.Type == 2 {
		var request domain.ObjectionbyDdocodeRpt
		request.DdoCode = req.Code
		request.FromDate = req.FromDate
		request.ToDate = req.ToDate
		request.Status = req.Status

		u, err := uh.svc.ObjectionDdocodeRptRepo(ctx, request, req.MetaDataRequest)
		if err != nil {
			apierrors.HandleDBError(ctx, err)
			log.Error(ctx, "Objection Report Repo call failed: %s", err.Error())
			return
		}

		rsp := response.NewObjectionCodeResponse(u)

		metadata := port.NewMetaDataResponse(req.Skip, req.Limit, len(rsp))

		apiRsp := response.ObjectionCodeResponse{
			StatusCodeAndMessage: port.FetchSucess,
			MetaDataResponse:     metadata,
			Data:                 rsp,
		}
		log.Debug(ctx, "ListObjectionPaoReportHandler response", apiRsp)
		handleSuccess(ctx, apiRsp)
	}

}

type ObjectionPraoRpt struct {
	Code     string `form:"code" validate:"required,max=20"`
	FromDate string `form:"from-date" validate:"required,date_yyyy_mm_dd"`
	ToDate   string `form:"to-date" validate:"required,date_yyyy_mm_dd"`
	Status   string `form:"status" validate:"required,oneof=created paoupdated rejected praoupdated closed notclosed"`
	Type     int64  `form:"type" validate:"required,min=1,max=2"`
	port.MetaDataRequest
}

// ListObjectionPraoReportHandler godoc
//
//	@Summary		Get Objection Prao Report
//	@Description	Get Objection Prao Report
//	@Tags			HOA
//	@Accept			json
//	@Produce		json
//	@Param			type				query		string									true	"type = 1 for PRAO objections under PRAO, type =2 for PRAO objections under PAO "
//	@Param			code			query		string									true	"Give PRAO_code if type =1 else give PAO_code if type = 2"
//	@Param			status				query		string									true	"status of the objections"
//	@Param			from-date				query		string									true	"from-date"
//	@Param			to-date				query		string									true	"to-date"
//
// @Param       skip    query       int     			false   		"Number of records to skip for pagination"
// @Param       limit   query       int     			false   		"Number of records to limit for pagination"
//
//	@Success		200					{object}	response.ObjectionPraoCodeResponse	"list retrieved successfully"
//	@Failure		400					{object}	apierrors.APIErrorResponse						"Validation error"
//	@Failure		401					{object}	apierrors.APIErrorResponse						"Unauthorized error"
//	@Failure		403					{object}	apierrors.APIErrorResponse						"Forbidden error"
//	@Failure		404					{object}	apierrors.APIErrorResponse						"Data not found error"
//	@Failure		409					{object}	apierrors.APIErrorResponse						"Data conflict error"
//	@Failure		500					{object}	apierrors.APIErrorResponse						"Internal server error"
//	@Router			/v1/objection/prao/report [get]
func (uh *ObjectionHandler) ListObjectionPraoReportHandler(ctx *gin.Context) {

	var req ObjectionPraoRpt
	if err := ctx.ShouldBindQuery(&req); err != nil {
		apierrors.HandleBindingError(ctx, err)
		log.Error(ctx, "Binding failed for ObjectionRpt: %s", err.Error())
		return
	}
	if err := validation.ValidateStruct(req); err != nil {
		apierrors.HandleValidationError(ctx, err)
		log.Error(ctx, "Validation failed for ObjectionRpt: %s", err.Error())
		return
	}
	if req.Limit == 0 {
		req.Limit = math.MaxInt32
	}

	if req.Type == 1 {
		var request domain.ObjectionbyPraocodeReport

		// Prao_code: zero.NewString(req.Prao_code, true),
		request.PraoCode = req.Code
		request.FromDate = req.FromDate
		request.ToDate = req.ToDate
		request.Status = req.Status

		u, err := uh.svc.ObjectionPraocodePraoRptRepo(ctx, request, req.MetaDataRequest)
		if err != nil {
			apierrors.HandleDBError(ctx, err)
			log.Error(ctx, "Objection Prao Report Repo call failed: %s", err.Error())
			return
		}

		rsp := response.NewObjectionPraoCodeResponse(u)

		metadata := port.NewMetaDataResponse(req.Skip, req.Limit, len(rsp))

		apiRsp := response.ObjectionPraoCodeResponse{
			StatusCodeAndMessage: port.FetchSucess,
			MetaDataResponse:     metadata,
			Data:                 rsp,
		}
		log.Debug(ctx, "ListObjectionPraoReportHandler response", apiRsp)
		handleSuccess(ctx, apiRsp)
	}
	if req.Type == 2 {
		var request domain.ObjectionbyPaocodeReport
		request.PaoCode = req.Code
		request.FromDate = req.FromDate
		request.ToDate = req.ToDate
		request.Status = req.Status

		u, err := uh.svc.ObjectionPaocodePraoRptRepo(ctx, request, req.MetaDataRequest)
		if err != nil {
			apierrors.HandleDBError(ctx, err)
			log.Error(ctx, "Objection Prao Report Repo call failed: %s", err.Error())
			return
		}

		rsp := response.NewObjectionPraoCodeResponse(u)

		metadata := port.NewMetaDataResponse(req.Skip, req.Limit, len(rsp))

		apiRsp := response.ObjectionPraoCodeResponse{
			StatusCodeAndMessage: port.FetchSucess,
			MetaDataResponse:     metadata,
			Data:                 rsp,
		}
		log.Debug(ctx, "ListObjectionPraoReportHandler response", apiRsp)
		handleSuccess(ctx, apiRsp)
	}

}
