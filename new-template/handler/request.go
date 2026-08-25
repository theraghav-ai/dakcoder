package handler

import (
	"pisapi/core/port"
)

// CreateUserRequest represents the payload to create a user
type CreateUserRequest struct {
	FirstName string `json:"first_name" validate:"required"`
	LastName  string `json:"last_name" validate:"required"`
	Age       int    `json:"age" validate:"required"`
	City      string `json:"city" validate:"required"`
	Email     string `json:"email" validate:"required"`
}

// UpdateUserRequest represents the payload to update a user (all fields optional)
type UpdateUserRequest struct {
	ID        int64  `uri:"id" validate:"required"`
	FirstName string `json:"first_name" validate:"omitempty"`
	LastName  string `json:"last_name" validate:"omitempty"`
	Age       int    `json:"age" validate:"omitempty"`
	City      string `json:"city" validate:"omitempty"`
	Email     string `json:"email" validate:"omitempty"`
}

// Uri struct for id
type UserIDUri struct {
	ID int64 `uri:"id" validate:"required"`
}

type ListUsersParams struct {
	port.MetadataRequest
}

func (p *ListUsersParams) Validate() error {
	return nil
}
