package port

type MetadataRequest struct {
	Skip     uint64 `form:"skip,default=0" validate:"omitempty"`
	Limit    uint64 `form:"limit,default=10" validate:"omitempty"`
	OrderBy  string `form:"orderBy" validate:"omitempty"`
	SortType string `form:"sortType" validate:"omitempty"`
}
