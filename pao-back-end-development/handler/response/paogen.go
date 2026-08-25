package response

import (
	"gotemplate/core/domain"
	"gotemplate/core/port"
	"time"
)

type FetchGetOfficeNameResponse struct {
	OfficeId   int64  `json:"office_id"`
	OfficeName string `json:"office_name"`
	PaoCode    string `json:"pao_code"`
	DdoCode    string `json:"ddo_code"`
}

func NewGetOfficenameResponse(request domain.OfficeDetails) FetchGetOfficeNameResponse {

	requestResponse := FetchGetOfficeNameResponse{
		OfficeId:   request.Ddo_office_id.Int64,
		OfficeName: request.Ddo_name.String,
		PaoCode:    request.PaoCode.String,
		DdoCode:    request.DdoCode.String,
	}
	return requestResponse
}

type GetOfficenameResponse struct {
	port.StatusCodeAndMessage `json:",inline"`
	port.MetaDataResponse     `json:",inline"`
	Data                      FetchGetOfficeNameResponse `json:"data"`
}

type FetchGetPAOsResponse struct {
	PaoCode     string `json:"pao_code"`
	PaoOfficeId int64  `json:"pao_office_id"`
	PaoName     string `json:"pao_name"`
}

func NewGetPAOsResponse(requests []domain.Pao) []FetchGetPAOsResponse {
	var response []FetchGetPAOsResponse
	for _, request := range requests {
		requestResponse := FetchGetPAOsResponse{
			PaoCode:     request.PaoCode.String,
			PaoOfficeId: request.PaoOfficeId.Int64,
			PaoName:     request.PaoName.String,
		}
		response = append(response, requestResponse)
	}
	return response
}

type GetPAOsResponse struct {
	port.StatusCodeAndMessage `json:",inline"`
	port.MetaDataResponse     `json:",inline"`
	Data                      []FetchGetPAOsResponse `json:"data"`
}

type FetchGetDDOsResponse struct {
	DdoCode     string `json:"ddo_code"`
	DdoOfficeId int64  `json:"ddo_office_id"`
	DdoName     string `json:"ddo_name"`
	DdoType     string `json:"ddo_type"`
}

func NewGetDDOsResponse(requests []domain.Ddo) []FetchGetDDOsResponse {
	var response []FetchGetDDOsResponse
	for _, request := range requests {
		requestResponse := FetchGetDDOsResponse{
			DdoCode:     request.DdoCode.String,
			DdoOfficeId: request.DdoOfficeId.Int64,
			DdoName:     request.DdoName.String,
			DdoType:     request.DdoType.String,
		}
		response = append(response, requestResponse)
	}
	return response
}

type GetDDOsResponse struct {
	port.StatusCodeAndMessage `json:",inline"`
	port.MetaDataResponse     `json:",inline"`
	Data                      []FetchGetDDOsResponse `json:"data"`
}

type FetchGetDDOlistResponse struct {
	DdoCode               string `json:"ddo_code"`
	DdoName               string `json:"ddo_name"`
	Date                  string `json:"date"`
	CashbookReceiveStatus bool   `json:"cashbook_receive_status"`
	VerificationStatus    bool   `json:"verification_status"`
	PfmsGenerationStatus  bool   `json:"pfms_generation_status"`
}

func NewGetDDOlistResponse(requests []domain.PfmsStatus) []FetchGetDDOlistResponse {
	var response []FetchGetDDOlistResponse
	for _, request := range requests {
		requestResponse := FetchGetDDOlistResponse{
			DdoCode:               request.DdoCode.String,
			DdoName:               request.DdoName.String,
			Date:                  request.Date.String,
			CashbookReceiveStatus: request.H_cash_book_receive_flag.Bool,
			VerificationStatus:    request.H_verification_flag.Bool,
			PfmsGenerationStatus:  request.H_pfms_generation_flag.Bool,
		}
		response = append(response, requestResponse)
	}
	return response
}

type GetDDOlistResponse struct {
	port.StatusCodeAndMessage `json:",inline"`
	port.MetaDataResponse     `json:",inline"`
	Data                      []FetchGetDDOlistResponse `json:"data"`
}

type FetchGetDDOdetailResponse struct {
	DdoCode          string      `json:"ddo_code"`
	DdoOfficeName    string      `json:"ddo_office_name"`
	BusinessDate     time.Time   `json:"business_date"`
	ClosingBal       float64     `json:"closing_bal"`
	OpeningBal       float64     `json:"opening_bal"`
	Hoa              string      `json:"hoa"`
	Payment          float64     `json:"payment"`
	Receipt          float64     `json:"receipt"`
	AccountCodeArray []CodeArray `json:"account_array"`
	HoaDescription   string      `json:"hoa_description"`
	CreatedDate      time.Time   `json:"created_date"`
}

type CodeArray struct {
	Accountcode            string  `json:"account_code"`
	AccountcodeDescription string  `json:"account_code_description"`
	Receipt                float64 `json:"receipt"`
	Payment                float64 `json:"payment"`
}

func convertDomainCodearrayToCodearray(domainResult []domain.CodeArray) []CodeArray {
	results := make([]CodeArray, len(domainResult))
	for i, r := range domainResult {
		results[i] = CodeArray{
			Accountcode:            r.AccountCode.String,
			AccountcodeDescription: r.AccountCodeDescription.String,
			Receipt:                r.Receipt.Float64,
			Payment:                r.Payment.Float64,
		}
	}
	return results
}

func NewGetDDOdetailResponse(requests []domain.DdoDetail) []FetchGetDDOdetailResponse {
	var response []FetchGetDDOdetailResponse
	for _, request := range requests {
		requestResponse := FetchGetDDOdetailResponse{
			DdoCode:          request.DdoCode.String,
			DdoOfficeName:    request.DdoOfficeName.String,
			BusinessDate:     request.BusinessDate.Time,
			ClosingBal:       request.ClosingBal.Float64,
			OpeningBal:       request.OpeningBal.Float64,
			Hoa:              request.Hoa.String,
			Payment:          request.Payment.Float64,
			Receipt:          request.Receipt.Float64,
			AccountCodeArray: convertDomainCodearrayToCodearray(request.AccountArray),
			HoaDescription:   request.HoaDescription.String,
			CreatedDate:      request.CreatedDate,
		}
		response = append(response, requestResponse)
	}
	return response
}

type GetDDOdetailResponse struct {
	port.StatusCodeAndMessage `json:",inline"`
	port.MetaDataResponse     `json:",inline"`
	Data                      []FetchGetDDOdetailResponse `json:"data"`
}

type FetchGetPfmsPendingResponse struct {
	PfmsDdoId             string    `json:"pfms_ddo_id"`
	DdoOfficeId           string    `json:"ddo_office_id"`
	DdoName               string    `json:"ddo_name"`
	DdoCode               string    `json:"ddo_code"`
	BusinessDate          time.Time `json:"business_date"`
	CashbookReceiveStatus bool      `json:"cashbook_receive_status"`
	VerificationStatus    bool      `json:"verification_status"`
	PfmsGenerationStatus  bool      `json:"pfms_generation_status"`
}

func NewGetPfmspendingResponse(requests []domain.PfmsPending) []FetchGetPfmsPendingResponse {
	var response []FetchGetPfmsPendingResponse
	for _, request := range requests {
		requestResponse := FetchGetPfmsPendingResponse{
			PfmsDdoId:             request.PfmsDdoId.String,
			DdoOfficeId:           request.DdoOfficeId.String,
			DdoName:               request.DdoName.String,
			DdoCode:               request.DdoCode.String,
			BusinessDate:          request.BusinessDate.Time,
			CashbookReceiveStatus: request.H_cash_book_receive_flag.Bool,
			VerificationStatus:    request.H_verification_flag.Bool,
			PfmsGenerationStatus:  request.H_pfms_generation_flag.Bool,
		}
		response = append(response, requestResponse)
	}
	return response
}

type GetPfmspendingResponse struct {
	port.StatusCodeAndMessage `json:",inline"`
	port.MetaDataResponse     `json:",inline"`
	Data                      []FetchGetPfmsPendingResponse `json:"data"`
}

type FetchGetDDOlistMonthlyResponse struct {
	DdoCode                  string `json:"ddo_code"`
	DdoName                  string `json:"ddo_name"`
	Period                   string `json:"period"`
	CashAccountReceiveStatus bool   `json:"cashaccount_receive_status"`
	VerificationStatus       bool   `json:"verification_status"`
}

func NewGetDDOlistMonthlyResponse(requests []domain.PfmsStatusMonthly) []FetchGetDDOlistMonthlyResponse {
	var response []FetchGetDDOlistMonthlyResponse
	for _, request := range requests {
		requestResponse := FetchGetDDOlistMonthlyResponse{
			DdoCode:                  request.DdoCode.String,
			DdoName:                  request.DdoName.String,
			Period:                   request.Period.String,
			CashAccountReceiveStatus: request.H_cash_account_receive_flag.Bool,
			VerificationStatus:       request.H_verification_flag.Bool,
		}
		response = append(response, requestResponse)
	}
	return response
}

type GetDDOlistMonthlyResponse struct {
	port.StatusCodeAndMessage `json:",inline"`
	port.MetaDataResponse     `json:",inline"`
	Data                      []FetchGetDDOlistMonthlyResponse `json:"data"`
}

type FetchGetDdoDetailMonthlyResponse struct {
	DdoCode          string      `json:"ddo_code"`
	DdoOfficeName    string      `json:"ddo_office_name"`
	Period           string      `json:"period"`
	ClosingBal       float64     `json:"closing_bal"`
	OpeningBal       float64     `json:"opening_bal"`
	Hoa              string      `json:"hoa"`
	Payment          float64     `json:"payment"`
	Receipt          float64     `json:"receipt"`
	TePayment        float64     `json:"te_payment"`
	TeReceipt        float64     `json:"te_receipt"`
	AccountCodeArray []CodeArray `json:"account_array"`
	HoaDescription   string      `json:"hoa_description"`
}

func NewGetDDOdetail_monthlyResponse(requests []domain.DdoDetailMonthly) []FetchGetDdoDetailMonthlyResponse {
	var response []FetchGetDdoDetailMonthlyResponse
	for _, request := range requests {
		requestResponse := FetchGetDdoDetailMonthlyResponse{
			DdoCode:          request.DdoCode.String,
			DdoOfficeName:    request.OfficeName.String,
			Period:           request.Period.String,
			ClosingBal:       request.ClosingBal.Float64,
			OpeningBal:       request.OpeningBal.Float64,
			Hoa:              request.Hoa.String,
			Payment:          request.Payment.Float64,
			Receipt:          request.Receipt.Float64,
			TePayment:        request.TePayment.Float64,
			TeReceipt:        request.TeReceipt.Float64,
			AccountCodeArray: convertDomainCodearrayToCodearray(request.AccountArray),
			HoaDescription:   request.HoaDescription.String,
		}
		response = append(response, requestResponse)
	}
	return response
}

type GetDDOdetail_monthlyResponse struct {
	port.StatusCodeAndMessage `json:",inline"`
	port.MetaDataResponse     `json:",inline"`
	Data                      []FetchGetDdoDetailMonthlyResponse `json:"data"`
}

type FetchGetPfmsxmlResponse struct {
	PaoCode              string    `json:"pao_code"`
	DdoCode              string    `json:"ddo_code"`
	DdoName              string    `json:"ddo_name"`
	BusinessDate         time.Time `json:"business_date"`
	PfmsGenerationFlag   bool      `json:"h_pfms_generation_flag"`
	PfmsUniqueId         string    `json:"pfms_unique_id"`
	PfmsStatusFlag       string    `json:"pfms_status_flag"`
	PfmsErrorDescription string    `json:"Pfms_error_description"`
	TENumber             string    `json:"te_number"`
}

func NewGetPfmsxmlResponse(requests []domain.PfmsXmlPending) []FetchGetPfmsxmlResponse {

	var response []FetchGetPfmsxmlResponse
	for _, request := range requests {
		requestResponse := FetchGetPfmsxmlResponse{
			PaoCode:              request.PaoCode.String,
			DdoCode:              request.DdoCode.String,
			DdoName:              request.DdoName.String,
			BusinessDate:         request.BusinessDate.Time,
			PfmsGenerationFlag:   request.HPfmsGenerationFlag.Bool,
			PfmsUniqueId:         request.PfmsUniqueId.String,
			PfmsStatusFlag:       request.PfmsSubmissionFlag.String,
			PfmsErrorDescription: request.PfmsErrorDescription.String,
			TENumber:             request.TENumber.String,
		}
		response = append(response, requestResponse)
	}
	return response
}

type GetGetPfmsxmlResponse struct {
	port.StatusCodeAndMessage `json:",inline"`
	port.MetaDataResponse     `json:",inline"`
	Data                      []FetchGetPfmsxmlResponse `json:"data"`
}

type FetchGetPfmsxmlTeResponse struct {
	PaoCode              string    `json:"pao_code"`
	TransferEntryId      string    `json:"transfer_entry_id"`
	BusinessDate         time.Time `json:"business_date"`
	PfmsGenerationFlag   bool      `json:"h_pfms_generation_flag"`
	PfmsUniqueId         string    `json:"pfms_unique_id"`
	PfmsStatusFlag       string    `json:"pfms_status_flag"`
	PfmsErrorDescription string    `json:"Pfms_error_description"`
	TENumber             string    `json:"te_number"`
}

func NewGetPfmsxmlteResponse(requests []domain.PfmsXmlTePending) []FetchGetPfmsxmlTeResponse {

	var response []FetchGetPfmsxmlTeResponse
	for _, request := range requests {
		requestResponse := FetchGetPfmsxmlTeResponse{
			PaoCode:              request.PaoCode.String,
			TransferEntryId:      request.TransferEntryId.String,
			BusinessDate:         request.BusinessDate.Time,
			PfmsGenerationFlag:   request.HPfmsGenerationFlag.Bool,
			PfmsUniqueId:         request.PfmsUniqueId.String,
			PfmsStatusFlag:       request.PfmsSubmissionFlag.String,
			PfmsErrorDescription: request.PfmsErrorDescription.String,
			TENumber:             request.TENumber.String,
		}
		response = append(response, requestResponse)
	}
	return response
}

type GetGetPfmsxmlteResponse struct {
	port.StatusCodeAndMessage `json:",inline"`
	port.MetaDataResponse     `json:",inline"`
	Data                      []FetchGetPfmsxmlTeResponse `json:"data"`
}

type FetchGetPfmsResponse struct {
	UniqueIdentifier string `json:"uniqueIdentifier"`
	Pfms             string `json:"pfms"`
}

func NewGetPfmsResponse(request domain.Xml) FetchGetPfmsResponse {

	requestResponse := FetchGetPfmsResponse{
		UniqueIdentifier: request.UniqueIdentifier.String,
		Pfms:             request.Pfms.String,
	}
	return requestResponse
}

type GetPfmsResponse struct {
	port.StatusCodeAndMessage `json:",inline"`
	port.MetaDataResponse     `json:",inline"`
	Data                      FetchGetPfmsResponse `json:"data"`
}

type FetchPostPraoAccountResponse struct {
	PaoCode      string       `json:"pao_code"`
	PaoName      string       `json:"pao_name"`
	Hoa          string       `json:"hoa"`
	Period       string       `json:"period"`
	TotalPayment float64      `json:"total_payment"`
	TotalReceipt float64      `json:"total_receipt"`
	DdoArray     []DdoDetails `json:"ddo_array"`
}

type DdoDetails struct {
	DdoCode   string  `json:"ddo_code"`
	Receipt   float64 `json:"receipt"`
	Payment   float64 `json:"payment"`
	TeReceipt float64 `json:"te_receipt"`
	TePayment float64 `json:"te_payment"`
}

func convertDomainDdo_detailsToDdo_details(domainResult []domain.DdoDetails) []DdoDetails {
	results := make([]DdoDetails, len(domainResult))
	for i, r := range domainResult {
		results[i] = DdoDetails{
			DdoCode:   r.DdoCode.String,
			Receipt:   r.Receipt.Float64,
			Payment:   r.Payment.Float64,
			TeReceipt: r.TeReceipt.Float64,
			TePayment: r.TePayment.Float64,
		}
	}
	return results
}

func NewPostPraoAccountResponse(requests []domain.PaoPraoAccount) []FetchPostPraoAccountResponse {
	var response []FetchPostPraoAccountResponse
	for _, request := range requests {
		requestResponse := FetchPostPraoAccountResponse{
			PaoCode:      request.PaoCode.String,
			PaoName:      request.PaoName.String,
			Hoa:          request.Hoa.String,
			Period:       request.Period.String,
			TotalPayment: request.TotalPayment.Float64,
			TotalReceipt: request.TotalReceipt.Float64,
			DdoArray:     convertDomainDdo_detailsToDdo_details(request.DdoArray),
		}
		response = append(response, requestResponse)
	}
	return response
}

type PostPraoAccountResponse struct {
	port.StatusCodeAndMessage `json:",inline"`
	port.MetaDataResponse     `json:",inline"`
	Data                      []FetchPostPraoAccountResponse `json:"data"`
}

type FetchGetPraoAccountResponse struct {
	PaoCode        string       `json:"pao_code"`
	PaoName        string       `json:"pao_name"`
	Hoa            string       `json:"hoa"`
	HoaDescription string       `json:"hoa_description"`
	Period         string       `json:"period"`
	TotalPayment   float64      `json:"total_payment"`
	TotalReceipt   float64      `json:"total_receipt"`
	DdoArray       []DdoDetails `json:"ddo_array"`
}

func NewGetPraoAccountResponse(requests []domain.PaoPraoAccountReply) []FetchGetPraoAccountResponse {
	var response []FetchGetPraoAccountResponse
	for _, request := range requests {
		requestResponse := FetchGetPraoAccountResponse{
			PaoCode:        request.PaoCode.String,
			PaoName:        request.PaoName.String,
			Hoa:            request.Hoa.String,
			HoaDescription: request.HoaDescription.String,
			Period:         request.Period.String,
			TotalPayment:   request.TotalPayment.Float64,
			TotalReceipt:   request.TotalReceipt.Float64,
			DdoArray:       convertDomainDdo_detailsToDdo_details(request.DdoArray),
		}
		response = append(response, requestResponse)
	}
	return response
}

type GetPraoAccountResponse struct {
	port.StatusCodeAndMessage `json:",inline"`
	port.MetaDataResponse     `json:",inline"`
	Data                      []FetchGetPraoAccountResponse `json:"data"`
}

type FetchPraoAccountSubStatusResponse struct {
	PaoCode                       string `json:"pao_code"`
	Period                        string `json:"period"`
	AccountSubmissiontoPraoStatus string `json:"account_submissionto_prao_status"`
}

func NewPraoAccountSubStatusResponse(request domain.PraoAccountSubmissionStatus) FetchPraoAccountSubStatusResponse {

	requestResponse := FetchPraoAccountSubStatusResponse{
		PaoCode:                       request.PaoCode.String,
		Period:                        request.Period.String,
		AccountSubmissiontoPraoStatus: request.AccountSubmissionToPraoStatus.String,
	}
	return requestResponse
}

type PraoAccountSubStatusResponse struct {
	port.StatusCodeAndMessage `json:",inline"`
	port.MetaDataResponse     `json:",inline"`
	Data                      FetchPraoAccountSubStatusResponse `json:"data"`
}
type FetchPraoAccountSubStatusListResponse struct {
	PaoCode                       string `json:"pao_code"`
	PaoName                       string `json:"pao_name"`
	Period                        string `json:"period"`
	AccountSubmissiontoPraoStatus string `json:"account_submissionto_prao_status"`
}

func NewPraoAccountSubStatusListResponse(requests []domain.AccountSubmissionStatusList) []FetchPraoAccountSubStatusListResponse {
	var response []FetchPraoAccountSubStatusListResponse
	for _, request := range requests {
		requestResponse := FetchPraoAccountSubStatusListResponse{
			PaoCode:                       request.PaoCode.String,
			PaoName:                       request.PaoName.String,
			Period:                        request.Period.String,
			AccountSubmissiontoPraoStatus: request.AccountSubmissionToPraoStatus.String,
		}
		response = append(response, requestResponse)
	}
	return response
}

type PraoAccountSubStatusListResponse struct {
	port.StatusCodeAndMessage `json:",inline"`
	port.MetaDataResponse     `json:",inline"`
	Data                      []FetchPraoAccountSubStatusListResponse `json:"data"`
}

type FetchPfmsSubmissionPendingResponse struct {
	PfmsUniqueId string `json:"pfms_unique_id"`
}

func NewFetchPfmsSubmissionPendingResponse(requests []domain.PfmsSubmissionPending) []FetchPfmsSubmissionPendingResponse {

	var response []FetchPfmsSubmissionPendingResponse
	for _, request := range requests {
		requestResponse := FetchPfmsSubmissionPendingResponse{

			PfmsUniqueId: request.PfmsUniqueId.String,
		}
		response = append(response, requestResponse)
	}
	return response
}

type GetPfmsSubmissionPendingResponse struct {
	port.StatusCodeAndMessage `json:",inline"`
	port.MetaDataResponse     `json:",inline"`
	Data                      []FetchPfmsSubmissionPendingResponse `json:"data"`
}
type FetchGetInterPAOsResponse struct {
	PaoCode     string `json:"pao_code"`
	PaoOfficeId int64  `json:"pao_office_id"`
	PaoName     string `json:"pao_name"`
	DdoCode     string `json:"ddo_code"`
	DdoOfficeId int64  `json:"ddo_office_id"`
	DdoName     string `json:"ddo_name"`
}

func NewGetInterPAOsResponse(requests []domain.InterPao) []FetchGetInterPAOsResponse {
	var response []FetchGetInterPAOsResponse
	for _, request := range requests {
		requestResponse := FetchGetInterPAOsResponse{
			PaoCode:     request.PaoCode.String,
			PaoOfficeId: request.PaoOfficeId.Int64,
			PaoName:     request.PaoName.String,
			DdoCode:     request.DdoCode.String,
			DdoOfficeId: request.DdoOfficeId.Int64,
			DdoName:     request.DdoName.String,
		}
		response = append(response, requestResponse)
	}
	return response
}

type GetInterPAOsResponse struct {
	port.StatusCodeAndMessage `json:",inline"`
	port.MetaDataResponse     `json:",inline"`
	Data                      []FetchGetInterPAOsResponse `json:"data"`
}

// FetchSOOfficeDetailsResponse defines the response structure for SO office details
type FetchSOOfficeDetailsResponse struct {
	OfficeId          int64  `json:"office_id"`
	OfficeTypeCode    string `json:"office_type_code"`
	ReportingOfficeId int64  `json:"reporting_office_id"`
	DdoCode           string `json:"ddo_code"`
	DdoName           string `json:"ddo_name"`
	PaoOfficeId       int64  `json:"pao_office_id"`
	PaoCode           string `json:"pao_code"`
	PaoName           string `json:"pao_name"`
}

// NewSOOfficeDetailsResponse creates a FetchSOOfficeDetailsResponse from SOOfficeDetails
func NewSOOfficeDetailsResponse(request domain.SOOfficeDetails) FetchSOOfficeDetailsResponse {
	requestResponse := FetchSOOfficeDetailsResponse{
		OfficeId:          request.OfficeId.Int64,
		OfficeTypeCode:    request.OfficeTypeCode.String,
		ReportingOfficeId: request.ReportingOfficeId.Int64,
		DdoCode:           request.DdoCode.String,
		DdoName:           request.DdoName.String,
		PaoOfficeId:       request.PaoOfficeId.Int64,
		PaoCode:           request.PaoCode.String,
		PaoName:           request.PaoName.String,
	}
	return requestResponse
}

// SOOfficeDetailsResponse defines the complete response structure
type SOOfficeDetailsResponse struct {
	port.StatusCodeAndMessage `json:",inline"`
	port.MetaDataResponse     `json:",inline"`
	Data                      FetchSOOfficeDetailsResponse `json:"data"`
}
type FetchGetHoaResponse struct {
	Hoa            string `json:"hoa"`
	HoaDescription string `json:"hoa_description"`
}

func NewGetHoaResponse(requests []domain.AcccountHoaonlygetMapping) []FetchGetHoaResponse {
	var response []FetchGetHoaResponse
	for _, request := range requests {
		requestResponse := FetchGetHoaResponse{
			Hoa:            request.Hoa.String,
			HoaDescription: request.HoaDescription.String,
		}
		response = append(response, requestResponse)
	}
	return response
}

type GetHoaResponse struct {
	port.StatusCodeAndMessage `json:",inline"`
	port.MetaDataResponse     `json:",inline"`
	Data                      []FetchGetHoaResponse `json:"data"`
}

type FetchAccountCodeResponse struct {
	AccountCode            string `json:"account_code"`
	AccountCodeDescription string `json:"account_code_description"`
}

func NewAccountCodeResponse(requests []domain.AcccountCodegetMapping) []FetchAccountCodeResponse {
	var response []FetchAccountCodeResponse
	for _, request := range requests {
		requestResponse := FetchAccountCodeResponse{
			AccountCode:            request.AccountCode.String,
			AccountCodeDescription: request.AccountCodeDescription.String,
		}
		response = append(response, requestResponse)
	}
	return response
}

type AccountCodeResponse struct {
	port.StatusCodeAndMessage `json:",inline"`
	port.MetaDataResponse     `json:",inline"`
	Data                      []FetchAccountCodeResponse `json:"data"`
}

type GetCashbookPfmsStatusResponse struct {
	port.StatusCodeAndMessage `json:",inline"`
	Data                      bool `json:"data"`
}

// GetConsolidatedCashAccountResponse is the API response wrapper
// type GetConsolidatedCashAccountResponse struct {
// 	port.StatusCodeAndMessage
// 	Data *domain.ConsolidatedCashAccount `json:"data"`
// }

// func NewGetConsolidatedCashAccountResponse(data *domain.ConsolidatedCashAccount) GetConsolidatedCashAccountResponse {
// 	return GetConsolidatedCashAccountResponse{
// 		StatusCodeAndMessage: port.FetchSucess,
// 		Data:                 data,
// 	}
// }

// response/consolidated_cash_account.go

type ConsolidatedAccountCodeResponse struct {
	AccountCode            string  `json:"account_code"`
	AccountCodeDescription string  `json:"account_code_description"`
	Receipt                float64 `json:"receipt"`
	Payment                float64 `json:"payment"`
}

type ConsolidatedHoaDetailResponse struct {
	Hoa            string                            `json:"hoa"`
	HoaDescription string                            `json:"hoa_description"`
	HoaReflection  string                            `json:"hoa_reflection,omitempty"`
	PositiveSide   string                            `json:"positive_side,omitempty"`
	Part           string                            `json:"part,omitempty"`
	Receipt        float64                           `json:"receipt"`
	Payment        float64                           `json:"payment"`
	AccountArray   []ConsolidatedAccountCodeResponse `json:"account_array"`
}

type ConsolidatedCashAccountData struct {
	PaoOfficeId       int64                           `json:"pao_office_id"`
	PaoName           string                          `json:"pao_name"`
	CashAccountPeriod string                          `json:"cash_account_period"`
	HoaDetails        []ConsolidatedHoaDetailResponse `json:"hoa_details"`
}

type GetConsolidatedCashAccountResponse struct {
	port.StatusCodeAndMessage
	Data ConsolidatedCashAccountData `json:"data"`
}

func NewGetConsolidatedCashAccountResponse(d *domain.ConsolidatedCashAccount) GetConsolidatedCashAccountResponse {
	hoaDetails := make([]ConsolidatedHoaDetailResponse, 0, len(d.HoaDetails))

	for _, h := range d.HoaDetails {
		accounts := make([]ConsolidatedAccountCodeResponse, 0, len(h.AccountArray))
		for _, a := range h.AccountArray {
			accounts = append(accounts, ConsolidatedAccountCodeResponse{
				AccountCode:            a.AccountCode,
				AccountCodeDescription: a.AccountCodeDescription,
				Receipt:                a.Receipt,
				Payment:                a.Payment,
			})
		}
		hoaDetails = append(hoaDetails, ConsolidatedHoaDetailResponse{
			Hoa:            h.Hoa,
			HoaDescription: h.HoaDescription,
			HoaReflection:  h.HoaReflection,
			PositiveSide:   h.PositiveSide,
			Part:           h.Part,
			Receipt:        h.Receipt,
			Payment:        h.Payment,
			AccountArray:   accounts,
		})
	}

	return GetConsolidatedCashAccountResponse{
		StatusCodeAndMessage: port.FetchSucess,
		Data: ConsolidatedCashAccountData{
			PaoOfficeId:       d.PaoOfficeId,
			PaoName:           d.PaoName,
			CashAccountPeriod: d.CashAccountPeriod,
			HoaDetails:        hoaDetails,
		},
	}
}

// changes done on 23-03-2026

// ReversionPendingResponse — wrapper
type GetReversionPendingResponse struct {
	port.StatusCodeAndMessage `json:",inline"`
	port.MetaDataResponse     `json:",inline"`
	Data                      []FetchReversionPendingResponse `json:"data"`
}

// FetchReversionPendingResponse — individual record
type FetchReversionPendingResponse struct {
	ReversionID              int    `json:"reversion_id"`
	DdoCode                  string `json:"ddo_code"`
	DdoName                  string `json:"ddo_name"`
	DdoOfficeID              int64  `json:"ddo_office_id"`
	FromDate                 string `json:"from_date"`
	RequestDate              string `json:"request_date"`
	BusinessDate             string `json:"business_date"`
	PfmsReversalType         string `json:"pfms_reversal_type"`
	OriginalPfmsUID          string `json:"original_pfms_uid"`
	OriginalSubmissionStatus string `json:"original_submission_status"`
	OriginalTeNumber         string `json:"original_te_number"`
	CurrentStatus            string `json:"current_status"`
	CurrentTeNumber          string `json:"current_te_number"`
	ReversalPfmsUID          string `json:"reversal_pfms_uid"`
	PfmsNegativePosted       string `json:"pfms_negative_posted"`
	DbDeletionStatus         string `json:"db_deletion_status"`
}

// PostNegativeEntryResponse — wrapper
type GetPostNegativeEntryResponse struct {
	port.StatusCodeAndMessage `json:",inline"`
	Data                      FetchPostNegativeEntryResponse `json:"data"`
}

// FetchPostNegativeEntryResponse — individual record
type FetchPostNegativeEntryResponse struct {
	Message      string `json:"message"`
	ReversalUID  string `json:"reversal_uid"`
	BusinessDate string `json:"business_date"`
}

// NewGetReversionPendingResponse — converter
func NewGetReversionPendingResponse(
	records []domain.ReversionRecord,
) []FetchReversionPendingResponse {
	var response []FetchReversionPendingResponse
	for _, r := range records {
		response = append(response, FetchReversionPendingResponse{
			ReversionID:              r.ReversionID,
			DdoCode:                  r.DdoCode,
			DdoName:                  r.DdoName,
			DdoOfficeID:              r.DdoOfficeID,
			FromDate:                 r.FromDate.Format("2006-01-02"),
			RequestDate:              r.RequestDate.Format("2006-01-02"),
			BusinessDate:             r.BusinessDate.Format("2006-01-02"),
			PfmsReversalType:         r.PfmsReversalType,
			OriginalPfmsUID:          r.OriginalPfmsUID,
			OriginalSubmissionStatus: r.OriginalSubmissionStatus,
			OriginalTeNumber:         r.OriginalTeNumber,
			CurrentStatus:            r.CurrentStatus,
			CurrentTeNumber:          r.CurrentTeNumber,
			ReversalPfmsUID:          r.ReversalPfmsUID,
			PfmsNegativePosted:       r.PfmsNegativePosted,
			DbDeletionStatus:         r.DbDeletionStatus,
		})
	}
	return response
}

// NewPostNegativeEntryResponse — converter
func NewPostNegativeEntryResponse(
	reversalUID string,
	businessDate string,
) FetchPostNegativeEntryResponse {
	return FetchPostNegativeEntryResponse{
		Message:      "Negative entry posted successfully",
		ReversalUID:  reversalUID,
		BusinessDate: businessDate,
	}
}

// Response wrapper
type GetReversionRecordsResponse struct {
	port.StatusCodeAndMessage `json:",inline"`
	port.MetaDataResponse     `json:",inline"`
	Data                      []FetchReversionRecordsResponse `json:"data"`
}

// Individual record
type FetchReversionRecordsResponse struct {
	ReversionID              int    `json:"reversion_id"`
	DdoCode                  string `json:"ddo_code"`
	DdoName                  string `json:"ddo_name"`
	DdoOfficeID              int64  `json:"ddo_office_id"`
	FromDate                 string `json:"from_date"`
	RequestDate              string `json:"request_date"`
	BusinessDate             string `json:"business_date"`
	PfmsReversalType         string `json:"pfms_reversal_type"`
	OriginalPfmsUID          string `json:"original_pfms_uid"`
	OriginalSubmissionStatus string `json:"original_submission_status"`
	OriginalTeNumber         string `json:"original_te_number"`
	ReversalPfmsUID          string `json:"reversal_pfms_uid"`
	PfmsNegativePosted       string `json:"pfms_negative_posted"`
	DbDeletionStatus         string `json:"db_deletion_status"`
	CurrentStatus            string `json:"current_status"`
	CurrentTeNumber          string `json:"current_te_number"`
	ReversalSubmissionStatus string `json:"reversal_submission_status"` // new
	ReversalTeNumber         string `json:"reversal_te_number"`         // new
	RequestEmployeeID        int    `json:"request_employee_id"`
	Remarks                  string `json:"remarks"`
}

// Converter
func NewGetReversionRecordsResponse(
	records []domain.ReversionRecord,
) []FetchReversionRecordsResponse {
	var response []FetchReversionRecordsResponse
	for _, r := range records {
		response = append(response, FetchReversionRecordsResponse{
			ReversionID:              r.ReversionID,
			DdoCode:                  r.DdoCode,
			DdoName:                  r.DdoName,
			DdoOfficeID:              r.DdoOfficeID,
			FromDate:                 r.FromDate.Format("2006-01-02"),
			RequestDate:              r.RequestDate.Format("2006-01-02"),
			BusinessDate:             r.BusinessDate.Format("2006-01-02"),
			PfmsReversalType:         r.PfmsReversalType,
			OriginalPfmsUID:          r.OriginalPfmsUID,
			OriginalSubmissionStatus: r.OriginalSubmissionStatus,
			OriginalTeNumber:         r.OriginalTeNumber,
			ReversalPfmsUID:          r.ReversalPfmsUID,
			PfmsNegativePosted:       r.PfmsNegativePosted,
			DbDeletionStatus:         r.DbDeletionStatus,
			CurrentStatus:            r.CurrentStatus,
			CurrentTeNumber:          r.CurrentTeNumber,
			ReversalSubmissionStatus: r.ReversalSubmissionStatus,
			ReversalTeNumber:         r.ReversalTeNumber,
			RequestEmployeeID:        r.RequestEmployeeID,
			Remarks:                  r.Remarks,
		})
	}
	return response
}

type GetDdoPfmsStatusResponse struct {
	port.StatusCodeAndMessage
	port.MetaDataResponse
	Data []domain.DdoPfmsStatus `json:"data"`
}

func NewGetDdoPfmsStatusResponse(data []domain.DdoPfmsStatus) []domain.DdoPfmsStatus {
	return data
}

type GetPaoPraoStatusResponse struct {
	port.StatusCodeAndMessage
	Data *domain.PaoPraoStatus `json:"data"`
}
