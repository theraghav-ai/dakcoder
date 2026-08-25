package handler

import (
	"context"
	"gotemplate/core/domain"
	"gotemplate/core/port"
	bud "gotemplate/gen/proto/v1"

	v1 "gotemplate/gen/proto/v1"
	"gotemplate/handler/response"
	repo "gotemplate/repo/postgres"

	"connectrpc.com/connect"
	log "gitlab.cept.gov.in/it-2.0-common/api-log"
	"google.golang.org/protobuf/types/known/timestamppb"
)

type ObjectionGrpcHandler struct {
	svc *repo.ObjectionGrpcRepository
}

// NewUserHandler creates a new UserHandler instance
func NewObjectionGrpcHandler(svc *repo.ObjectionGrpcRepository) *ObjectionGrpcHandler {
	return &ObjectionGrpcHandler{
		svc,
	}
}

func (bh *ObjectionGrpcHandler) CreateObjectionGrpcHandler(
	ctx context.Context,
	req *connect.Request[bud.CreateObjectionGrpcHandlerRequest],
) (*connect.Response[bud.CreateObjectionGrpcHandlerResponse], error) {

	Request := domain.ObjectionRequest{
		PaoCode:     req.Msg.PaoCode,
		DdoCode:     req.Msg.DdoCode,
		Description: req.Msg.Description,
		ObjectionId: req.Msg.ObjectionId,
		CreatedBy:   req.Msg.CreatedBy,
		Remarks:     ProtoRemarkstoRemarks(req.Msg.Remarks),
		CreatedDate: req.Msg.CreatedDate.AsTime(),
		StatusFlag:  req.Msg.StatusFlag,
	}

	p, err := bh.svc.ObjectionCreationGrpcRepo(&ctx, &Request)

	if err != nil {
		log.Error(ctx, "ReAllocate Repo call failed %s", err.Error())
		return nil, connect.NewError(connect.CodeInternal, err)
	}

	rsp := connect.NewResponse(&bud.CreateObjectionGrpcResponse{

		PaoCode:     p.PaoCode.String,
		DdoCode:     p.DdoCode.String,
		Description: p.Description.String,
		CreatedBy:   p.CreatedBy.Uint64,
		ObjectionId: p.ObjectionId.String,
		Remarks:     response.RemarkstoProtoRemarks(p.Remarks),
		CreatedDate: timestamppb.New(p.CreatedDate.Time),
		StatusFlag:  p.StatusFlag.String,
	})
	res := connect.NewResponse(&v1.CreateObjectionGrpcHandlerResponse{
		StatusCode: int32(port.GetPredefinedStatusDetails("create_success").StatusCode),
		Success:    port.GetPredefinedStatusDetails("create_success").Success,
		Message:    port.GetPredefinedStatusDetails("create_success").Message,
		Data:       []*bud.CreateObjectionGrpcResponse{rsp.Msg},
	})

	return res, nil

}
func (bh *ObjectionGrpcHandler) CreateObjectionPraoGrpcHandler(
	ctx context.Context,
	req *connect.Request[bud.CreateObjectionPraoGrpcHandlerRequest],
) (*connect.Response[bud.CreateObjectionPraoGrpcHandlerResponse], error) {

	Request := domain.ObjectionPraoRequest{
		PraoCode:    req.Msg.PraoCode,
		PaoCode:     req.Msg.PaoCode,
		Description: req.Msg.Description,
		ObjectionId: req.Msg.ObjectionId,
		CreatedBy:   req.Msg.CreatedBy,
		Remarks:     ProtoRemarkstoRemarks(req.Msg.Remarks),
		CreatedDate: req.Msg.CreatedDate.AsTime(),
		StatusFlag:  req.Msg.StatusFlag,
	}

	p, err := bh.svc.ObjectionCreationPraoGrpcRepo(&ctx, &Request)

	if err != nil {
		log.Error(ctx, "ReAllocate Repo call failed %s", err.Error())
		return nil, connect.NewError(connect.CodeInternal, err)
	}

	rsp := connect.NewResponse(&bud.CreateObjectionPraoGrpcResponse{

		PraoCode:    p.PraoCode.String,
		PaoCode:     p.PaoCode.String,
		Description: p.Description.String,
		CreatedBy:   p.CreatedBy.Uint64,
		ObjectionId: p.ObjectionId.String,
		Remarks:     response.RemarkstoProtoRemarks(p.Remarks),
		CreatedDate: timestamppb.New(p.CreatedDate.Time),
		StatusFlag:  p.StatusFlag.String,
	})
	res := connect.NewResponse(&v1.CreateObjectionPraoGrpcHandlerResponse{
		StatusCode: int32(port.GetPredefinedStatusDetails("create_success").StatusCode),
		Success:    port.GetPredefinedStatusDetails("create_success").Success,
		Message:    port.GetPredefinedStatusDetails("create_success").Message,
		Data:       []*bud.CreateObjectionPraoGrpcResponse{rsp.Msg},
	})

	return res, nil

}
