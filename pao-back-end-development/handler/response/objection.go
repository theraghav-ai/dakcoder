package response

import (
	"gotemplate/core/domain"
	"gotemplate/core/port"
	"time"
)

type FetchObjectionCreationResponse struct {
	PaoCode         string            `json:"pao_code"`
	DdoCode         string            `json:"ddo_code"`
	Description     string            `json:"description"`
	ObjectionId     string            `json:"objection_id"`
	CreatedBy       uint64            `json:"created_by"`
	CreatedDate     time.Time         `json:"created_date"`
	Remarks         []ObjectionRemark `json:"remarks"`
	StatusFlag      string            `json:"status_flag"`
	LastUpdatedBy   uint64            `json:"last_updated_by"`
	LastUpdatedDate time.Time         `json:"last_updated_date"`
}

type ObjectionRemark struct {
	Data              string    `json:"data"`
	CommentedBy       uint64    `json:"commented_by"`
	CommentedDate     time.Time `json:"commented_date"`
	CommentedOfficeId uint64    `json:"commented_office_id"`
	Filepath          string    `json:"filepath"`
	Sender            string    `json:"sender"`
}

func convertDomainObjection_remarkToObjection_remark(domainResult []domain.ObjectionRemark) []ObjectionRemark {
	results := make([]ObjectionRemark, len(domainResult))
	for i, r := range domainResult {
		results[i] = ObjectionRemark{
			Data:              r.Data.String,
			CommentedBy:       r.CommentedBy.Uint64,
			CommentedDate:     r.CommentedDate.Time,
			CommentedOfficeId: r.CommentedOfficeId.Uint64,
			Filepath:          r.Filepath.String,
			Sender:            r.Sender.String,
		}
	}
	return results
}

func NewObjectionCreationResponse(request domain.Objection) FetchObjectionCreationResponse {

	requestResponse := FetchObjectionCreationResponse{
		PaoCode:         request.PaoCode.String,
		DdoCode:         request.DdoCode.String,
		Description:     request.Description.String,
		ObjectionId:     request.ObjectionId.String,
		CreatedBy:       request.CreatedBy.Uint64,
		CreatedDate:     request.CreatedDate.Time,
		Remarks:         convertDomainObjection_remarkToObjection_remark(request.Remarks),
		StatusFlag:      request.StatusFlag.String,
		LastUpdatedBy:   request.LastUpdatedBy.Uint64,
		LastUpdatedDate: request.LastUpdatedDate.Time,
	}
	return requestResponse
}

type ObjectionCreationResponse struct {
	port.StatusCodeAndMessage `json:",inline"`
	port.MetaDataResponse     `json:",inline"`
	Data                      FetchObjectionCreationResponse `json:"data"`
}

type FetchObjectionPaoByIdResponse struct {
	PaoCode         string            `json:"pao_code"`
	DdoCode         string            `json:"ddo_code"`
	DdoName         string            `json:"ddo_name"`
	Description     string            `json:"description"`
	ObjectionId     string            `json:"objection_id"`
	CreatedBy       uint64            `json:"created_by"`
	CreatedDate     time.Time         `json:"created_date"`
	Remarks         []ObjectionRemark `json:"remarks"`
	StatusFlag      string            `json:"status_flag"`
	LastUpdatedBy   uint64            `json:"last_updated_by"`
	LastUpdatedDate time.Time         `json:"last_updated_date"`
}

func NewObjectionPaoByIdResponse(requests []domain.ObjectionReply) []FetchObjectionPaoByIdResponse {
	var response []FetchObjectionPaoByIdResponse
	for _, request := range requests {
		requestResponse := FetchObjectionPaoByIdResponse{
			PaoCode:         request.PaoCode.String,
			DdoCode:         request.DdoCode.String,
			DdoName:         request.DdoName.String,
			Description:     request.Description.String,
			ObjectionId:     request.ObjectionId.String,
			CreatedBy:       request.CreatedBy.Uint64,
			CreatedDate:     request.CreatedDate.Time,
			Remarks:         convertDomainObjection_remarkToObjection_remark(request.Remarks),
			StatusFlag:      request.StatusFlag.String,
			LastUpdatedBy:   request.LastUpdatedBy.Uint64,
			LastUpdatedDate: request.LastUpdatedDate.Time,
		}
		response = append(response, requestResponse)
	}
	return response
}

type ObjectionPaoByIdResponse struct {
	port.StatusCodeAndMessage `json:",inline"`
	port.MetaDataResponse     `json:",inline"`
	Data                      []FetchObjectionPaoByIdResponse `json:"data"`
}

type FetchObjectionCreationPraoResponse struct {
	PraoCode        string            `json:"prao_code"`
	PaoCode         string            `json:"pao_code"`
	Description     string            `json:"description"`
	ObjectionId     string            `json:"objection_id"`
	CreatedBy       uint64            `json:"created_by"`
	CreatedDate     time.Time         `json:"created_date"`
	Remarks         []ObjectionRemark `json:"remarks"`
	StatusFlag      string            `json:"status_flag"`
	LastUpdatedBy   uint64            `json:"last_updated_by"`
	LastUpdatedDate time.Time         `json:"last_updated_date"`
}

func NewObjectionCreationPraoResponse(request domain.ObjectionPrao) FetchObjectionCreationPraoResponse {

	requestResponse := FetchObjectionCreationPraoResponse{
		PraoCode:        request.PraoCode.String,
		PaoCode:         request.PaoCode.String,
		Description:     request.Description.String,
		ObjectionId:     request.ObjectionId.String,
		CreatedBy:       request.CreatedBy.Uint64,
		CreatedDate:     request.CreatedDate.Time,
		Remarks:         convertDomainObjection_remarkToObjection_remark(request.Remarks),
		StatusFlag:      request.StatusFlag.String,
		LastUpdatedBy:   request.LastUpdatedBy.Uint64,
		LastUpdatedDate: request.LastUpdatedDate.Time,
	}
	return requestResponse
}

type ObjectionCreationPraoResponse struct {
	port.StatusCodeAndMessage `json:",inline"`
	port.MetaDataResponse     `json:",inline"`
	Data                      FetchObjectionCreationPraoResponse `json:"data"`
}

type FetchObjectionPraoByIdResponse struct {
	PraoCode        string            `json:"prao_code"`
	PaoCode         string            `json:"pao_code"`
	PaoName         string            `json:"pao_name"`
	Description     string            `json:"description"`
	ObjectionId     string            `json:"objection_id"`
	CreatedBy       uint64            `json:"created_by"`
	CreatedDate     time.Time         `json:"created_date"`
	Remarks         []ObjectionRemark `json:"remarks"`
	StatusFlag      string            `json:"status_flag"`
	LastUpdatedBy   uint64            `json:"last_updated_by"`
	LastUpdatedDate time.Time         `json:"last_updated_date"`
}

func NewObjectionPraoByIdResponse(requests []domain.ObjectionPraoReply) []FetchObjectionPraoByIdResponse {
	var response []FetchObjectionPraoByIdResponse
	for _, request := range requests {
		requestResponse := FetchObjectionPraoByIdResponse{
			PraoCode:        request.PraoCode.String,
			PaoCode:         request.PaoCode.String,
			PaoName:         request.PaoName.String,
			Description:     request.Description.String,
			ObjectionId:     request.ObjectionId.String,
			CreatedBy:       request.CreatedBy.Uint64,
			CreatedDate:     request.CreatedDate.Time,
			Remarks:         convertDomainObjection_remarkToObjection_remark(request.Remarks),
			StatusFlag:      request.StatusFlag.String,
			LastUpdatedBy:   request.LastUpdatedBy.Uint64,
			LastUpdatedDate: request.LastUpdatedDate.Time,
		}
		response = append(response, requestResponse)
	}
	return response
}

type ObjectionPraoByIdResponse struct {
	port.StatusCodeAndMessage `json:",inline"`
	port.MetaDataResponse     `json:",inline"`
	Data                      []FetchObjectionPraoByIdResponse `json:"data"`
}

type FetchObjectionCodeResponse struct {
	PaoCode         string    `json:"pao_code"`
	DdoCode         string    `json:"ddo_code"`
	DdoName         string    `json:"ddo_name"`
	Description     string    `json:"description"`
	ObjectionId     string    `json:"objection_id"`
	CreatedBy       uint64    `json:"created_by"`
	CreatedDate     time.Time `json:"created_date"`
	StatusFlag      string    `json:"status_flag"`
	LastUpdatedBy   uint64    `json:"last_updated_by"`
	LastUpdatedDate time.Time `json:"last_updated_date"`
	LatestRemark    string    `json:"latest_remark"`
}

func NewObjectionCodeResponse(requests []domain.ObjectionReplyWithLatestRemark) []FetchObjectionCodeResponse {
	var response []FetchObjectionCodeResponse
	for _, request := range requests {
		requestResponse := FetchObjectionCodeResponse{
			PaoCode:         request.PaoCode.String,
			DdoCode:         request.DdoCode.String,
			DdoName:         request.DdoName.String,
			Description:     request.Description.String,
			ObjectionId:     request.ObjectionId.String,
			CreatedBy:       request.CreatedBy.Uint64,
			CreatedDate:     request.CreatedDate.Time,
			StatusFlag:      request.StatusFlag.String,
			LastUpdatedBy:   request.LastUpdatedBy.Uint64,
			LastUpdatedDate: request.LastUpdatedDate.Time,
			LatestRemark:    request.LatestRemark.String,
		}
		response = append(response, requestResponse)
	}
	return response
}

type ObjectionCodeResponse struct {
	port.StatusCodeAndMessage `json:",inline"`
	port.MetaDataResponse     `json:",inline"`
	Data                      []FetchObjectionCodeResponse `json:"data"`
}

type FetchObjectionPraoCodeResponse struct {
	PraoCode        string    `json:"prao_code"`
	PaoCode         string    `json:"pao_code"`
	PaoName         string    `json:"pao_name"`
	Description     string    `json:"description"`
	ObjectionId     string    `json:"objection_id"`
	CreatedBy       uint64    `json:"created_by"`
	CreatedDate     time.Time `json:"created_date"`
	StatusFlag      string    `json:"status_flag"`
	LastUpdatedBy   uint64    `json:"last_updated_by"`
	LastUpdatedDate time.Time `json:"last_updated_date"`
	LatestRemark    string    `json:"latest_remark"`
}

func NewObjectionPraoCodeResponse(requests []domain.ObjectionPraoReplyWithLatestRemarks) []FetchObjectionPraoCodeResponse {
	var response []FetchObjectionPraoCodeResponse
	for _, request := range requests {
		requestResponse := FetchObjectionPraoCodeResponse{
			PraoCode:        request.PraoCode.String,
			PaoCode:         request.PaoCode.String,
			PaoName:         request.PaoName.String,
			Description:     request.Description.String,
			ObjectionId:     request.ObjectionId.String,
			CreatedBy:       request.CreatedBy.Uint64,
			CreatedDate:     request.CreatedDate.Time,
			StatusFlag:      request.StatusFlag.String,
			LastUpdatedBy:   request.LastUpdatedBy.Uint64,
			LastUpdatedDate: request.LastUpdatedDate.Time,
			LatestRemark:    request.LatestRemark.String,
		}
		response = append(response, requestResponse)
	}
	return response
}

type ObjectionPraoCodeResponse struct {
	port.StatusCodeAndMessage `json:",inline"`
	port.MetaDataResponse     `json:",inline"`
	Data                      []FetchObjectionPraoCodeResponse `json:"data"`
}
