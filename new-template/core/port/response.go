package port

import "io"

var (
	ListSuccess   StatusCodeAndMessage = StatusCodeAndMessage{StatusCode: 200, Message: "list retrieved successfully", Success: true}
	FetchSuccess  StatusCodeAndMessage = StatusCodeAndMessage{StatusCode: 200, Message: "data retrieved successfully", Success: true}
	CreateSuccess StatusCodeAndMessage = StatusCodeAndMessage{StatusCode: 201, Message: "resource created successfully", Success: true}
	UpdateSuccess StatusCodeAndMessage = StatusCodeAndMessage{StatusCode: 200, Message: "resource updated successfully", Success: true}
	DeleteSuccess StatusCodeAndMessage = StatusCodeAndMessage{StatusCode: 200, Message: "resource deleted successfully", Success: true}
	CustomEnv     StatusCodeAndMessage = StatusCodeAndMessage{StatusCode: 200, Message: "This is environment specific", Success: true}
)

// OTP-related status constants
var (
	OTPSuccess     StatusCodeAndMessage = StatusCodeAndMessage{StatusCode: 200, Message: "OTP generated successfully", Success: true}
	OTPAuthSuccess StatusCodeAndMessage = StatusCodeAndMessage{StatusCode: 200, Message: "OTP authenticated successfully", Success: true}
)

type StatusCodeAndMessage struct {
	StatusCode int    `json:"status_code"`
	Success    bool   `json:"success"`
	Message    string `json:"message"`
}

type FileResponse struct {
	ContentDisposition string
	ContentType        string
	Data               []byte        // existing memory-based payload
	Reader             io.ReadCloser // optional streaming source
}

// Status returns the HTTP status code to be used in responses.
// This enables any response struct embedding StatusCodeAndMessage
// to satisfy interfaces that require a Status() int method.
func (s StatusCodeAndMessage) Status() int {
	return s.StatusCode
}

func (s StatusCodeAndMessage) ResponseType() string {
	return "standard"
}

func (s StatusCodeAndMessage) GetContentType() string {
	return "application/json"
}

func (s StatusCodeAndMessage) GetContentDisposition() string {
	return ""
}

func (s StatusCodeAndMessage) Object() []byte {
	return nil
}
func (s FileResponse) GetContentType() string {
	return s.ContentType
}

func (s FileResponse) GetContentDisposition() string {
	return s.ContentDisposition
}

func (s FileResponse) ResponseType() string {
	return "file"
}

func (s FileResponse) Status() int {
	return 200
}

func (s FileResponse) Object() []byte {
	return s.Data
}

// Stream copies Reader to w if available; else writes Data.
func (s FileResponse) Stream(w io.Writer) error {
	if s.Reader == nil {
		if len(s.Data) > 0 {
			_, err := w.Write(s.Data)
			return err
		}
		return nil
	}
	defer s.Reader.Close()
	_, err := io.Copy(w, s.Reader)
	return err
}

type MetaDataResponse struct {
	Skip                 uint64 `json:"skip,default=0"`
	Limit                uint64 `json:"limit,default=10"`
	OrderBy              string `json:"order_by,omitempty"`
	SortType             string `json:"sort_type,omitempty"`
	TotalRecordsCount    int    `json:"total_records_count,omitempty"`
	ReturnedRecordsCount uint64 `json:"returned_records_count"`
}

func NewMetaDataResponse(skip, limit, total uint64) MetaDataResponse {
	return MetaDataResponse{
		Skip:                 skip,
		Limit:                limit,
		ReturnedRecordsCount: total,
	}
}

func GetPredefinedStatusDetails(status string) StatusCodeAndMessage {

	PredefinedStatusDetailsMap := map[string]StatusCodeAndMessage{
		"list_success":   ListSuccess,
		"fetch_success":  FetchSuccess,
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
