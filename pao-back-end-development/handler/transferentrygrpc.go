package handler

import (
	"context"
	"errors"
	bud "gotemplate/gen/proto/v1"
	"time"

	"gotemplate/core/domain"
	repo "gotemplate/repo/postgres"

	"gotemplate/core/port"
	v1 "gotemplate/gen/proto/v1"
	"gotemplate/handler/response"

	"connectrpc.com/connect"
	"github.com/volatiletech/null/v9"
	log "gitlab.cept.gov.in/it-2.0-common/api-log"
)

type TransferEntryGrpcHandler struct {
	svc *repo.TransferEntryGrpcRepository
}

// NewUserHandler creates a new UserHandler instance
func NewTransferEntryGrpcHandler(svc *repo.TransferEntryGrpcRepository) *TransferEntryGrpcHandler {
	return &TransferEntryGrpcHandler{
		svc,
	}
}

func (uh *TransferEntryGrpcHandler) CreateTransferEntryGrpcHandler(
	ctx context.Context,
	req *connect.Request[bud.CreateTransferEntryGrpcHandlerRequest],
) (*connect.Response[bud.CreateTransferEntryGrpcHandlerResponse], error) {
	var remus TransferEntryRequests
	for _, r := range req.Msg.TransferEntries { // Correct way to access the field
		remus.TransferEntries = append(remus.TransferEntries, TransferEntryRequest{
			PaoCode:            r.PaoCode,
			DdoCode:            r.DdoCode,
			Hoa:                r.Hoa,
			TransferAmount:     r.TransferAmount,
			TransferType:       r.TransferType,
			CreatedBy:          r.CreatedBy,
			CreatedDate:        r.CreatedDate,
			TeSourceOfficeType: r.TeSourceOfficeType,
			Remarks:            r.Remarks,
		})
	}
	var request []domain.TransferEntryRequest

	for _, requ := range remus.TransferEntries {

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
		Inserted_Ids, err = uh.svc.TransferentryCreationGrpcRepo(&ctx, request)
		if err != nil {
			log.Error(ctx, "Transfer Entry Creation Repo call failed: %s", err.Error())
			return nil, connect.NewError(connect.CodeInternal, err)
		}
	} else {
		err := errors.New("total debit not equal to total credit")
		return nil, connect.NewError(connect.CodeInternal, err)

	}
	rsp := response.NewTransferEntryCreationResponsegrpc(Inserted_Ids)
	res := connect.NewResponse(&v1.CreateTransferEntryGrpcHandlerResponse{
		StatusCode: int32(port.GetPredefinedStatusDetails("create_success").StatusCode),
		Success:    port.GetPredefinedStatusDetails("create_success").Success,
		Message:    port.GetPredefinedStatusDetails("create_success").Message,
		Data:       rsp,
	})

	return res, nil
}

func (uh *TransferEntryGrpcHandler) CreateTransferEntryDirectGrpcHandler(
	ctx context.Context,
	req *connect.Request[bud.CreateTransferEntryDirectGrpcHandlerRequest],
) (*connect.Response[bud.CreateTransferEntryDirectGrpcHandlerResponse], error) {
	var remus TransferEntryDirectRequests
	for _, r := range req.Msg.TransferEntries { // Correct way to access the field
		remus.TransferEntries = append(remus.TransferEntries, TransferEntryDirectRequest{
			PaoCode:            r.PaoCode,
			DdoCode:            r.DdoCode,
			Hoa:                r.Hoa,
			TransferAmount:     r.TransferAmount,
			TransferType:       r.TransferType,
			CreatedBy:          r.CreatedBy,
			VerifiedBy:         r.VerifiedBy,
			TeSourceOfficeType: r.TeSourceOfficeType,
			Remarks:            r.Remarks,
		})
	}
	var request []domain.TransferEntryDirectRequest
	currentTime := time.Now()
	createdTimeString := currentTime.Format("20060102150405")

	for _, requ := range remus.TransferEntries {

		request = append(request, domain.TransferEntryDirectRequest{

			PaoCode:             requ.PaoCode,
			DdoCode:             requ.DdoCode,
			Hoa:                 requ.Hoa,
			TransferAmount:      requ.TransferAmount,
			TransferType:        requ.TransferType,
			CreatedBy:           requ.CreatedBy,
			CreatedDate:         currentTime,
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
		Inserted_Ids, err = uh.svc.TransferentryDirectGrpcCreationRepo(&ctx, request)
		if err != nil {
			log.Error(ctx, "Transfer Entry Creation Repo call failed: %s", err.Error())
			return nil, connect.NewError(connect.CodeInternal, err)
		}
	} else {
		err := errors.New("total debit not equal to total credit")
		return nil, connect.NewError(connect.CodeInternal, err)
	}
	rsp := response.NewTransferEntryGrpcCreationResponsegrpc(Inserted_Ids)
	res := connect.NewResponse(&v1.CreateTransferEntryDirectGrpcHandlerResponse{
		StatusCode: int32(port.GetPredefinedStatusDetails("create_success").StatusCode),
		Success:    port.GetPredefinedStatusDetails("create_success").Success,
		Message:    port.GetPredefinedStatusDetails("create_success").Message,
		Data:       rsp,
	})

	return res, nil
}
