package handler

import (
	"context"
	"gotemplate/core/domain"
	bud "gotemplate/gen/proto/v1"

	repo "gotemplate/repo/postgres"

	"gotemplate/core/port"
	v1 "gotemplate/gen/proto/v1"
	"gotemplate/handler/response"

	"connectrpc.com/connect"
	log "gitlab.cept.gov.in/it-2.0-common/api-log"
)

type PublicAcctGrpcHandler struct {
	svc *repo.PublicAcctRepository
}

// NewUserHandler creates a new UserHandler instancePublicAcctGrpcHandler
func NewPublicAcctGrpcHandler(svc *repo.PublicAcctRepository) *PublicAcctGrpcHandler {
	return &PublicAcctGrpcHandler{
		svc,
	}
}

func (bh *PublicAcctGrpcHandler) CreateRemunerationGrpcHandler(
	ctx context.Context,
	req *connect.Request[bud.CreateRemunerationGrpcHandlerRequest],
) (*connect.Response[bud.CreateRemunerationGrpcHandlerResponse], error) {

	var remus domain.RemunerationCreationRequestBulk

	for _, r := range req.Msg.RemunerationCreation { // Correct way to access the field
		remus.RemunerationCreation = append(remus.RemunerationCreation, domain.RemunerationCreationRequest{
			FinancialYear:         r.FinancialYear,
			RemunerationItem:      r.RemunerationItem,
			RemunerationType:      r.RemunerationType,
			RemunerationItemCount: r.RemunerationItemCount,
		})
	}

	rem, err := bh.svc.RemunerationCalculationGrpcRepo(&ctx, remus)

	if err != nil {
		log.Error(ctx, "RemunerationCreation Repo call failed %s", err.Error())
		return nil, connect.NewError(connect.CodeInternal, err)
	}
	if len(rem) > 0 {
		err2 := bh.svc.RemunerationCalculationGrpcPostRepo(&ctx, rem)
		if err2 != nil {
			log.Error(ctx, "Remuneration Calculation Post Repo call failed: %s", err2.Error())
			return nil, connect.NewError(connect.CodeInternal, err)
		}
	}

	rsp := response.NewRemunerationCreationResponsegrpc(rem)
	res := connect.NewResponse(&v1.CreateRemunerationGrpcHandlerResponse{
		StatusCode: int32(port.GetPredefinedStatusDetails("create_success").StatusCode),
		Success:    port.GetPredefinedStatusDetails("create_success").Success,
		Message:    port.GetPredefinedStatusDetails("create_success").Message,
		Data:       rsp,
	})

	return res, nil

}
