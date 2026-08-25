package response

import (
	"pisapi/core/domain"
	"pisapi/core/port"
)

type UserResponse struct {
	ID        int64  `json:"id"`
	FirstName string `json:"first_name"`
	LastName  string `json:"last_name"`
	Age       int    `json:"age"`
	City      string `json:"city"`
	Email     string `json:"email"`
}

func NewUserResponse(u domain.User) UserResponse {
	return UserResponse{
		ID:        u.ID,
		FirstName: u.FirstName,
		LastName:  u.LastName,
		Age:       u.Age,
		City:      u.City,
		Email:     u.Email,
	}
}

func NewUsersResponse(users []domain.User) []UserResponse {
	res := make([]UserResponse, 0, len(users))
	for _, u := range users {
		res = append(res, NewUserResponse(u))
	}
	return res
}

type UserCreateResponse struct {
	port.StatusCodeAndMessage `json:",inline"`
	Data                      UserResponse `json:"data"`
}

type UserFetchResponse struct {
	port.StatusCodeAndMessage `json:",inline"`
	Data                      UserResponse `json:"data"`
}

type UserDeleteResponse struct {
	port.StatusCodeAndMessage `json:",inline"`
}

type UsersListResponse struct {
	port.StatusCodeAndMessage `json:",inline"`
	port.MetaDataResponse     `json:",inline"`
	Data                      []UserResponse `json:"data"`
}
