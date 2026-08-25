package port

var (
	ListSuccess       StatusCodeAndMessage = StatusCodeAndMessage{StatusCode: 200, Message: "list retrieved successfully", Success: true}
	FetchSucess       StatusCodeAndMessage = StatusCodeAndMessage{StatusCode: 200, Message: "data retrieved successfully", Success: true}
	CreateSuccess     StatusCodeAndMessage = StatusCodeAndMessage{StatusCode: 201, Message: "resource created successfully", Success: true}
	UpdateSuccess     StatusCodeAndMessage = StatusCodeAndMessage{StatusCode: 200, Message: "resource updated successfully", Success: true}
	DeleteSuccess     StatusCodeAndMessage = StatusCodeAndMessage{StatusCode: 200, Message: "resource deleted successfully", Success: true}
	SubmissionSuccess StatusCodeAndMessage = StatusCodeAndMessage{StatusCode: 201, Message: "approval submitted successfully", Success: true}
	InsertSuccess     StatusCodeAndMessage = StatusCodeAndMessage{StatusCode: 201, Message: "record inserted successfully", Success: true}
)

// response represents a response body format
type Response struct {
	Success bool   `json:"success" example:"true"`
	Message string `json:"message" example:"Success"`
	Data    any    `json:"data,omitempty"`
}

// NewResponse is a helper function to create a response body
func NewResponse(success bool, message string, data any) Response {
	return Response{
		Success: success,
		Message: message,
		Data:    data,
	}
}

type StatusCodeAndMessage struct {
	StatusCode int32  `json:"status_code"`
	Success    bool   `json:"success" example:"true"`
	Message    string `json:"message"`
}

type MetaDataResponse struct {
	Skip                 uint64 `json:"skip,default=0"`
	Limit                uint64 `json:"limit,default=10"`
	OrderBy              string `json:"order_by,omitempty"`
	SortType             string `json:"sort_type,omitempty"`
	TotalRecordsCount    uint64 `json:"total_records_count,omitempty"`
	ReturnedRecordsCount int    `json:"returned_records_count"`
}

func NewMetaDataResponse(skip uint64, limit uint64, total int) MetaDataResponse {
	return MetaDataResponse{
		Skip:                 skip,
		Limit:                limit,
		ReturnedRecordsCount: total,
	}
}
func GetPredefinedStatusDetails(status string) StatusCodeAndMessage {

	PredefinedStatusDetailsMap := map[string]StatusCodeAndMessage{
		"list_success":   ListSuccess,
		"fetch_success":  FetchSucess,
		"create_success": CreateSuccess,
		"update_success": UpdateSuccess,
		"delete_success": DeleteSuccess,
	}

	if details, exists := PredefinedStatusDetailsMap[status]; exists {
		return details
	}
	return StatusCodeAndMessage{
		StatusCode: 500,
		Success:    false,
		Message:    "Unknown status",
	}

}
