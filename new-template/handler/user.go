package handler

import (
	"pisapi/core/port"
	resp "pisapi/handler/response"
	repo "pisapi/repo/postgres"

	log "gitlab.cept.gov.in/it-2.0-common/n-api-log"
	serverHandler "gitlab.cept.gov.in/it-2.0-common/n-api-server/handler"
	serverRoute "gitlab.cept.gov.in/it-2.0-common/n-api-server/route"
)

type UserHandler struct {
	*serverHandler.Base
	svc *repo.UserRepository
}

func NewUserHandler(svc *repo.UserRepository) *UserHandler {
	base := serverHandler.New("Users").SetPrefix("/v1").AddPrefix("")
	return &UserHandler{Base: base, svc: svc}
}

func (h *UserHandler) Routes() []serverRoute.Route {
	return []serverRoute.Route{
		serverRoute.POST("/users", h.CreateUser).Name("Create User"),
		serverRoute.GET("/users", h.ListUsers).Name("List Users"),
		serverRoute.GET("/users/:id", h.GetUserByID).Name("Get User By ID"),
		serverRoute.PUT("/users/:id", h.UpdateUserByID).Name("Update User By ID"),
		serverRoute.DELETE("/users/:id", h.DeleteUserByID).Name("Delete User By ID"),
	}
}

func (h *UserHandler) CreateUser(sctx *serverRoute.Context, req CreateUserRequest) (*resp.UserCreateResponse, error) {
	u, err := h.svc.CreateUser(sctx.Ctx, req.FirstName, req.LastName, req.Age, req.City, req.Email)
	if err != nil {
		log.Error(sctx.Ctx, "Error creating user: %v", err)
		return nil, err
	}
	log.Info(sctx.Ctx, "User created with ID: %d", u.ID)
	r := &resp.UserCreateResponse{
		StatusCodeAndMessage: port.CreateSuccess,
		Data:                 resp.NewUserResponse(u),
	}
	return r, nil
}

func (h *UserHandler) ListUsers(sctx *serverRoute.Context, _ struct{}) (*resp.UsersListResponse, error) {

	users, err := h.svc.GetAllUsers(sctx.Ctx)
	if err != nil {
		log.Error(sctx.Ctx, "Error fetching users: %v", err)
		return nil, err
	}
	data := resp.NewUsersResponse(users)
	md := port.NewMetaDataResponse(0, 0, uint64(len(data)))
	r := &resp.UsersListResponse{
		StatusCodeAndMessage: port.ListSuccess,
		MetaDataResponse:     md,
		Data:                 data,
	}
	return r, nil
}

func (h *UserHandler) GetUserByID(sctx *serverRoute.Context, req UserIDUri) (*resp.UserFetchResponse, error) {
	u, err := h.svc.GetUserByID(sctx.Ctx, req.ID)
	if err != nil {
		log.Error(sctx.Ctx, "Error fetching user by ID: %v", err)
		return nil, err
	}
	r := &resp.UserFetchResponse{
		StatusCodeAndMessage: port.FetchSuccess,
		Data:                 resp.NewUserResponse(u),
	}
	return r, nil
}

func (h *UserHandler) UpdateUserByID(sctx *serverRoute.Context, req UpdateUserRequest) (*resp.UserFetchResponse, error) {
	var firstNamePtr, lastNamePtr, cityPtr, emailPtr *string
	var agePtr *int

	if req.FirstName != "" {
		firstNamePtr = &req.FirstName
	}
	if req.LastName != "" {
		lastNamePtr = &req.LastName
	}
	if req.Age > 0 {
		agePtr = &req.Age
	}
	if req.City != "" {
		cityPtr = &req.City
	}
	if req.Email != "" {
		emailPtr = &req.Email
	}

	u, err := h.svc.UpdateUserByID(sctx.Ctx, req.ID, firstNamePtr, lastNamePtr, agePtr, cityPtr, emailPtr)
	if err != nil {
		log.Error(sctx.Ctx, "Error updating user by ID: %v", err)
		return nil, err
	}
	r := &resp.UserFetchResponse{
		StatusCodeAndMessage: port.UpdateSuccess,
		Data:                 resp.NewUserResponse(u),
	}
	return r, nil
}

func (h *UserHandler) DeleteUserByID(sctx *serverRoute.Context, req UserIDUri) (*resp.UserDeleteResponse, error) {
	err := h.svc.DeleteUserByID(sctx.Ctx, req.ID)
	if err != nil {
		log.Error(sctx.Ctx, "Error deleting user by ID: %v", err)
		return nil, err
	}
	r := &resp.UserDeleteResponse{
		StatusCodeAndMessage: port.DeleteSuccess,
	}

	return r, nil
}
