package handler

import (
	"gotemplate/core/port"
	"net/http"

	"github.com/gin-gonic/gin"
	//"github.com/guregu/null"
	//"github.com/jackc/pgx/v5/pgtype"
)

// meta represents metadata for a paginated response
// type meta struct {
// 	Total uint64 `json:"total" example:"100"`
// 	Limit uint64 `json:"limit" example:"10"`
// 	Skip  uint64 `json:"skip" example:"0"`
// }

func handleSuccess(ctx *gin.Context, data any) {

	ctx.JSON(http.StatusOK, data)
}

func handleCreateSuccess(ctx *gin.Context, data any) {

	ctx.JSON(http.StatusCreated, data)
}

type Xml struct {
	UniqueIdentifier *string `json:"uniqueIdentifier"`
	Pfms             *string `json:"pfms"`
}

func handleSuccessDoc(ctx *gin.Context, data any) {
	rsp := port.NewResponse(true, "Success", data)
	ctx.JSON(http.StatusOK, rsp)
}
