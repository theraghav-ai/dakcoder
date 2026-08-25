package response

import (
	"gotemplate/core/domain"
	"gotemplate/core/port"
	pao "gotemplate/gen/proto/v1"
	"time"
)

type FetchTransferentryCreationResponse struct {
	TransferEntryId string `json:"transfer_entry_id"`
}

func NewGTransferentryCreationResponse(requests []domain.InsertedIds) []FetchTransferentryCreationResponse {
	var response []FetchTransferentryCreationResponse
	for _, request := range requests {
		requestResponse := FetchTransferentryCreationResponse{
			TransferEntryId: request.TransferEntryId,
		}
		response = append(response, requestResponse)
	}
	return response
}

type GetTransferentryCreationResponse struct {
	port.StatusCodeAndMessage `json:",inline"`
	port.MetaDataResponse     `json:",inline"`
	Data                      []FetchTransferentryCreationResponse `json:"data"`
}

type FetchTransferentryReportResponse struct {
	PaoCode              string    `json:"pao_code"`
	Hoa                  string    `json:"hoa"`
	HoaDescription       string    `json:"hoa_description"`
	TransferAmount       float64   `json:"transfer_amount"`
	TransferType         string    `json:"transfer_type"`
	CreatedBy            uint64    `json:"created_by"`
	CreatedDate          time.Time `json:"created_date"`
	TransDate            time.Time `json:"trans_date"`
	DdoCode              string    `json:"ddo_code"`
	DdoName              string    `json:"ddo_name"`
	TransferEntryID      string    `json:"transfer_entry_id"`
	TeSourceOfficeType   string    `json:"te_source_office_type"`
	Remarks              string    `json:"remarks"`
	VerifiedBy           uint64    `json:"verified_by"`
	VerifiedDate         time.Time `json:"verified_date"`
	VerificationStatus   string    `json:"verification_status"`
	PfmsUniqueID         string    `json:"pfms_unique_id"`
	ApproverRemarks      string    `json:"approver_remarks"`
	BudgetID             string    `json:"budget_id"`
	PfmsSubmissionFlag   string    `json:"pfms_submission_flag"`
	PfmsErrorDescription string    `json:"pfms_error_description"`
	HPfmsGenerationFlag  bool      `json:"h_pfms_generation_flag"`
	TENumber             string    `json:"te_number"`
	AccountCode          string    `json:"account_code"`
	AccountCodeDescription string    `json:"account_code_description"`
}

func NewTransferentryReportResponse(requests []domain.TransferEntryReport) []FetchTransferentryReportResponse {
	var response []FetchTransferentryReportResponse
	for _, request := range requests {
		requestResponse := FetchTransferentryReportResponse{
			PaoCode:              request.PaoCode.String,
			DdoCode:              request.DdoCode.String,
			DdoName:              request.DdoName.String,
			Hoa:                  request.Hoa.String,
			HoaDescription:       request.HoaDescription.String,
			TransferAmount:       request.TransferAmount.Float64,
			TransferType:         request.TransferType.String,
			CreatedBy:            request.CreatedBy.Uint64,
			CreatedDate:          request.CreatedDate.Time,
			TransDate:            request.TransDate.Time,
			TransferEntryID:      request.TransferEntryID.String,
			TeSourceOfficeType:   request.TeSourceOfficeType.String,
			Remarks:              request.Remarks.String,
			VerificationStatus:   request.VerificationStatus.String,
			VerifiedBy:           request.VerifiedBy.Uint64,
			VerifiedDate:         request.VerifiedDate.Time,
			PfmsUniqueID:         request.PfmsUniqueID.String,
			BudgetID:             request.BudgetID.String,
			ApproverRemarks:      request.ApproverRemarks.String,
			PfmsSubmissionFlag:   request.PfmsSubmissionFlag.String,
			PfmsErrorDescription: request.PfmsErrorDescription.String,
			HPfmsGenerationFlag:  request.HPfmsGenerationFlag.Bool,
			TENumber:             request.TENumber.String,
			AccountCode:          request.AccountCode.String,
			AccountCodeDescription: request.AccountCodeDescription.String,

		}
		response = append(response, requestResponse)
	}
	return response
}

type TransferentryReportResponse struct {
	port.StatusCodeAndMessage `json:",inline"`
	port.MetaDataResponse     `json:",inline"`
	Data                      []FetchTransferentryReportResponse `json:"data"`
}

type FetchDdoTransferentryReportResponse struct {
	DdoCode              string    `json:"ddo_code"`
	DdoName              string    `json:"ddo_name"`
	TransId              string    `json:"trans_id"`
	Hoa                  string    `json:"hoa"`
	HoaDescription       string    `json:"hoa_description"`
	AccountCode          string    `json:"account_code"`
	TransferAmount       float64   `json:"transfer_amount"`
	TransferType         string    `json:"transfer_type"`
	CreatedBy            uint64    `json:"created_by"`
	CreatedDate          time.Time `json:"created_date"`
	Status               string    `json:"status"`
	ApproverRemarks      string    `json:"approver_remarks"`
	PfmsUniqueID         string    `json:"pfms_unique_id"`
	PfmsSubmissionFlag   string    `json:"pfms_submission_flag"`
	PfmsErrorDescription string    `json:"pfms_error_description"`
	TENumber             string    `json:"te_number"`
	RemarksByCreator     string    `json:"remarks_by_creator"`
}

func NewDdoTransferentryReportResponse(requests []domain.DdoTeRequestReply) []FetchDdoTransferentryReportResponse {
	var response []FetchDdoTransferentryReportResponse
	for _, request := range requests {
		requestResponse := FetchDdoTransferentryReportResponse{
			DdoCode:              request.DdoCode.String,
			DdoName:              request.DdoName.String,
			TransId:              request.TransId.String,
			Hoa:                  request.Hoa.String,
			HoaDescription:       request.HoaDescription.String,
			AccountCode:          request.AccountCode.String,
			TransferAmount:       request.TransferAmount.Float64,
			TransferType:         request.TransferType.String,
			CreatedBy:            request.CreatedBy.Uint64,
			CreatedDate:          request.CreatedDate.Time,
			Status:               request.Status.String,
			ApproverRemarks:      request.ApproverRemarks.String,
			PfmsUniqueID:         request.PfmsUniqueID.String,
			PfmsSubmissionFlag:   request.PfmsSubmissionFlag.String,
			PfmsErrorDescription: request.PfmsErrorDescription.String,
			TENumber:             request.TENumber.String,
			RemarksByCreator:     request.RemarksByCreator.String,
		}
		response = append(response, requestResponse)
	}
	return response
}

type DdoTransferentryReportResponse struct {
	port.StatusCodeAndMessage `json:",inline"`
	port.MetaDataResponse     `json:",inline"`
	Data                      []FetchDdoTransferentryReportResponse `json:"data"`
}

type FetchPaoSubTransferentryReportResponse struct {
	PaoCode              string    `json:"pao_code"`
	DdoCode              string    `json:"ddo_code"`
	DdoName              string    `json:"ddo_name"`
	TransId              string    `json:"trans_id"`
	CreatedBy            uint64    `json:"created_by"`
	CreatedDate          time.Time `json:"created_date"`
	ApprovedBy           uint64    `json:"approved_by"`
	ApprovedDate         time.Time `json:"approved_date"`
	TransDate            time.Time `json:"trans_date"`
	Remarks              string    `json:"remarks"`
	Status               string    `json:"status"`
	ApproverRemarks      string    `json:"approver_remarks"`
	PfmsUniqueID         string    `json:"pfms_unique_id"`
	PfmsSubmissionFlag   string    `json:"pfms_submission_flag"`
	PfmsErrorDescription string    `json:"pfms_error_description"`
	TENumber             string    `json:"te_number"`
	RemarksByCreator     string    `json:"remarks_by_creator"`
	WorkflowId          string    `json:"workflow_id"`
	
}

func NewPaoSubTransferentryReportResponse(requests []domain.PaoSubTeRequestReply) []FetchPaoSubTransferentryReportResponse {
	var response []FetchPaoSubTransferentryReportResponse
	for _, request := range requests {
		requestResponse := FetchPaoSubTransferentryReportResponse{
			PaoCode:              request.PaoCode.String,
			DdoCode:              request.DdoCode.String,
			DdoName:              request.DdoName.String,
			TransId:              request.TransId.String,
			CreatedBy:            request.CreatedBy.Uint64,
			CreatedDate:          request.CreatedDate.Time,
			ApprovedBy:           request.ApprovedBy.Uint64,
			ApprovedDate:         request.ApprovedDate.Time,
			TransDate:            request.TransDate.Time,
			Remarks:              request.Remarks.String,
			Status:               request.Status.String,
			ApproverRemarks:      request.ApproverRemarks.String,
			PfmsUniqueID:         request.PfmsUniqueID.String,
			PfmsSubmissionFlag:   request.PfmsSubmissionFlag.String,
			PfmsErrorDescription: request.PfmsErrorDescription.String,
			TENumber:             request.TENumber.String,
			RemarksByCreator:     request.RemarksByCreator.String,
			WorkflowId:          request.WorkflowId.String,
		}
		response = append(response, requestResponse)
	}
	return response
}

type PaoSubTransferentryReportResponse struct {
	port.StatusCodeAndMessage `json:",inline"`
	port.MetaDataResponse     `json:",inline"`
	Data                      []FetchPaoSubTransferentryReportResponse `json:"data"`
}

type FetchPaoSubTransferentryDetailResponse struct {
	PaoCode          string    `json:"pao_code"`
	DdoCode          string    `json:"ddo_code"`
	DdoName          string    `json:"ddo_name"`
	TransId          string    `json:"trans_id"`
	Hoa              string    `json:"hoa"`
	HoaDescription   string    `json:"hoa_description"`
	AccountCode      string    `json:"account_code"`
	TransferAmount   float64   `json:"transfer_amount"`
	TransferType     string    `json:"transfer_type"`
	CreatedBy        uint64    `json:"created_by"`
	CreatedDate      time.Time `json:"created_date"`
	Status           string    `json:"status"`
	RemarksByCreator string    `json:"remarks_by_creator"`
	Trans_date       time.Time `json:"trans_date"`
}

func NewPaoSubTransferentryDetailResponse(requests []domain.PaoSubTeRequestDetailReply) []FetchPaoSubTransferentryDetailResponse {
	var response []FetchPaoSubTransferentryDetailResponse
	for _, request := range requests {
		requestResponse := FetchPaoSubTransferentryDetailResponse{
			PaoCode:          request.PaoCode.String,
			DdoCode:          request.DdoCode.String,
			DdoName:          request.DdoName.String,
			TransId:          request.TransId.String,
			Hoa:              request.Hoa.String,
			HoaDescription:   request.HoaDescription.String,
			AccountCode:      request.AccountCode.String,
			TransferAmount:   request.TransferAmount.Float64,
			TransferType:     request.TransferType.String,
			CreatedBy:        request.CreatedBy.Uint64,
			CreatedDate:      request.CreatedDate.Time,
			Status:           request.Status.String,
			RemarksByCreator: request.RemarksByCreator.String,
			Trans_date:       request.Trans_date.Time,
		}
		response = append(response, requestResponse)
	}
	return response
}

type PaoSubTransferentryDetailResponse struct {
	port.StatusCodeAndMessage `json:",inline"`
	port.MetaDataResponse     `json:",inline"`
	Data                      []FetchPaoSubTransferentryDetailResponse `json:"data"`
}

func NewTransferEntryCreationResponsegrpc(remus []domain.InsertedIds) []*pao.FetchTransferEntryCreationResponse {
	var responses []*pao.FetchTransferEntryCreationResponse
	for _, rem := range remus {
		response := &pao.FetchTransferEntryCreationResponse{
			TransferEntryId: rem.TransferEntryId,
		}
		responses = append(responses, response)
	}
	return responses
}
func NewTransferEntryGrpcCreationResponsegrpc(remus []domain.InsertedIds) []*pao.FetchTransferEntryDirectCreationResponse {
	var responses []*pao.FetchTransferEntryDirectCreationResponse
	for _, rem := range remus {
		response := &pao.FetchTransferEntryDirectCreationResponse{
			TransferEntryId: rem.TransferEntryId,
		}
		responses = append(responses, response)
	}
	return responses
}

type FetchTransferentryInterPaoReportResponse struct {
	TransferEntryId string    `json:"transfer_entry_id"`
	MasterPaoCode   string    `json:"master_pao_code"`
	CreatedBy       uint64    `json:"created_by"`
	CreatedDate     time.Time `json:"created_date"`
	Remarks         string    `json:"remarks"`
}

func NewTransferentryInterPaoReportResponse(requests []domain.TransferEntryInterPaoReport) []FetchTransferentryInterPaoReportResponse {
	var response []FetchTransferentryInterPaoReportResponse
	for _, request := range requests {
		requestResponse := FetchTransferentryInterPaoReportResponse{
			TransferEntryId: request.TransferEntryId.String,
			MasterPaoCode:   request.MasterPaoCode.String,
			CreatedBy:       request.CreatedBy.Uint64,
			CreatedDate:     request.CreatedDate.Time,
			Remarks:         request.Remarks.String,
		}
		response = append(response, requestResponse)
	}
	return response
}

type TransferentryInterPaoReportResponse struct {
	port.StatusCodeAndMessage `json:",inline"`
	port.MetaDataResponse     `json:",inline"`
	Data                      []FetchTransferentryInterPaoReportResponse `json:"data"`
}
type FetchInterPaoTransferentryDetailResponse struct {
	MasterPaoCode        string    `json:"master_pao_code"`
	PaoCode              string    `json:"pao_code"`
	Hoa                  string    `json:"hoa"`
	HoaDescription       string    `json:"hoa_description"`
	DdoName              string    `json:"ddo_name"`
	TransferAmount       float64   `json:"transfer_amount"`
	TransferType         string    `json:"transfer_type"`
	CreatedBy            int64     `json:"created_by"`
	CreatedDate          time.Time `json:"created_date"`
	DdoCode              string    `json:"ddo_code"`
	TransferEntryId      string    `json:"transfer_entry_id"`
	TeSourceOfficeType   string    `json:"te_source_office_type"`
	Remarks              string    `json:"remarks"`
	VerifiedBy           int64     `json:"verified_by"`
	VerifiedDate         time.Time `json:"verified_date"`
	VerificationStatus   string    `json:"verification_status"`
	ApproverRemarks      string    `json:"approver_remarks"`
	BudgetId             string    `json:"budget_id"`
	HPfmsGenerationFlag  bool      `json:"h_pfms_generation_flag"`
	PfmsUniqueId         string    `json:"pfms_unique_id"`
	PfmsSubmissionFlag   string    `json:"pfms_submission_flag"`
	PfmsErrorDescription string    `json:"pfms_error_description"`
}

type InterPaoTransferentryDetailResponse struct {
	port.StatusCodeAndMessage `json:",inline"`
	port.MetaDataResponse     `json:",inline"`
	Data                      []FetchInterPaoTransferentryDetailResponse `json:"data"`
}

func NewInterPaoTransferentryDetailResponse(requests []domain.TransferEntryInterPao) []FetchInterPaoTransferentryDetailResponse {
	var response []FetchInterPaoTransferentryDetailResponse
	for _, request := range requests {
		requestResponse := FetchInterPaoTransferentryDetailResponse{
			MasterPaoCode:        request.MasterPaoCode.String,
			PaoCode:              request.PaoCode.String,
			Hoa:                  request.Hoa.String,
			HoaDescription:       request.HoaDescription.String,
			DdoName:              request.DdoName.String,
			TransferAmount:       request.TransferAmount.Float64,
			TransferType:         request.TransferType.String,
			CreatedBy:            request.CreatedBy.Int64,
			CreatedDate:          request.CreatedDate.Time,
			DdoCode:              request.DdoCode.String,
			TransferEntryId:      request.TransferEntryId.String,
			TeSourceOfficeType:   request.TeSourceOfficeType.String,
			Remarks:              request.Remarks.String,
			VerifiedBy:           request.VerifiedBy.Int64,
			VerifiedDate:         request.VerifiedDate.Time,
			VerificationStatus:   request.VerificationStatus.String,
			ApproverRemarks:      request.ApproverRemarks.String,
			BudgetId:             request.BudgetId.String,
			HPfmsGenerationFlag:  request.HPfmsGenerationFlag.Bool,
			PfmsUniqueId:         request.PfmsUniqueId.String,
			PfmsSubmissionFlag:   request.PfmsSubmissionFlag.String,
			PfmsErrorDescription: request.PfmsErrorDescription.String,
		}
		response = append(response, requestResponse)
	}
	return response
}

type PFMSResetResponse struct {
	port.StatusCodeAndMessage
	Message string `json:"message,omitempty"`
}
