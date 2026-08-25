package port

type MetaDataRequest struct {
	Skip                 uint64 `form:"skip,default=0" validate:"omitempty"`
	Limit                uint64 `form:"limit,default=10" validate:"omitempty"`
	OrderBy              string `json:"order_by,omitempty" example:"id, name"`
	SortType             string `json:"sort_type,omitempty"`
	TotalRecordsRequired bool   `json:"total_records_required,omitempty"`
}
