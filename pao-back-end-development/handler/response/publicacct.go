package response

import (
	"gotemplate/core/domain"
	"gotemplate/core/port"
	pao "gotemplate/gen/proto/v1"
	"time"

	"google.golang.org/protobuf/types/known/timestamppb"
)

type FetchGetBroadsheetResponse struct {
	BroadsheetMonth string  `json:"broadsheet_month"`
	Hoa             string  `json:"hoa"`
	DdoCode         string  `json:"ddo_code"`
	DdoName         string  `json:"ddo_name"`
	OpeningBalance  float64 `json:"opening_balance"`
	CreditAmount    float64 `json:"credit_amount"`
	DebitAmount     float64 `json:"debit_amount"`
	ClosingBalance  float64 `json:"closing_balance"`
}

func NewGetBroadsheetResponse(requests []domain.BroadSheet) []FetchGetBroadsheetResponse {
	var response []FetchGetBroadsheetResponse
	for _, request := range requests {
		requestResponse := FetchGetBroadsheetResponse{
			BroadsheetMonth: request.BroadsheetMonth.String,
			Hoa:             request.Hoa.String,
			DdoCode:         request.DdoCode.String,
			DdoName:         request.DdoName.String,
			OpeningBalance:  request.OpeningBalance.Float64,
			CreditAmount:    request.CreditAmount.Float64,
			DebitAmount:     request.DebitAmount.Float64,
			ClosingBalance:  request.ClosingBalance.Float64,
		}
		response = append(response, requestResponse)
	}
	return response
}

type GetBroadsheetResponse struct {
	port.StatusCodeAndMessage `json:",inline"`
	port.MetaDataResponse     `json:",inline"`
	Data                      []FetchGetBroadsheetResponse `json:"data"`
}

type FetchGetAppracctsResponse struct {
	Be             float64 `json:"be"`
	Re             float64 `json:"re"`
	Fg             float64 `json:"fg"`
	Hoa            string  `json:"hoa"`
	HoaDescription string  `json:"hoa_description" select:"hoa_description"`
}

func NewGetAppracctsResponse(requests []domain.ApprAccts) []FetchGetAppracctsResponse {
	var response []FetchGetAppracctsResponse
	for _, request := range requests {
		requestResponse := FetchGetAppracctsResponse{
			Be:             request.Be.Float64,
			Re:             request.Re.Float64,
			Fg:             request.Fg.Float64,
			Hoa:            request.Hoa.String,
			HoaDescription: request.HoaDescription.String,
		}
		response = append(response, requestResponse)
	}
	return response
}

type GetAppracctsResponse struct {
	port.StatusCodeAndMessage `json:",inline"`
	port.MetaDataResponse     `json:",inline"`
	Data                      []FetchGetAppracctsResponse `json:"data"`
}

type FetchGetAppraccts2Response struct {
	Hoa            string  `json:"hoa" select:"hoa"`
	HoaDescription string  `json:"hoa_description" select:"hoa_description"`
	Fg             float64 `json:"fg" select:"fg"`
	TotalExp       float64 `json:"total_exp" select:"total_exp"`
}

func NewGetAppraccts2Response(requests []domain.ApprAccts2) []FetchGetAppraccts2Response {
	var response []FetchGetAppraccts2Response
	for _, request := range requests {
		requestResponse := FetchGetAppraccts2Response{
			Hoa:            request.Hoa.String,
			HoaDescription: request.HoaDescription.String,
			Fg:             request.Fg.Float64,
			TotalExp:       request.TotalExp.Float64,
		}
		response = append(response, requestResponse)
	}
	return response
}

type GetAppraccts2Response struct {
	port.StatusCodeAndMessage `json:",inline"`
	port.MetaDataResponse     `json:",inline"`
	Data                      []FetchGetAppraccts2Response `json:"data"`
}

type FetchGetAppraccts3Response struct {
	Hoapart               string  `json:"hoapart" select:"hoapart"`
	Mh                    string  `json:"mh" select:"mh"`
	Mh_description        string  `json:"mh_description" select:"mh_description"`
	Smh                   string  `json:"smh" select:"smh"`
	Smh_description       string  `json:"smh_description" select:"smh_description"`
	Minorhead             string  `json:"minorhead" select:"minorhead"`
	Minorhead_description string  `json:"minorhead_description" select:"minorhead_description"`
	Subhoa                string  `json:"subhoa" select:"subhoa"`
	Subhoa_description    string  `json:"subhoa_description" select:"subhoa_description"`
	O                     float64 `json:"o" select:"o"`
	S                     float64 `json:"s" select:"s"`
	R                     float64 `json:"r" select:"r"`
	TotalExp              float64 `json:"total_exp" select:"total_exp"`
}

func NewGetAppraccts3Response(requests []domain.ApprAccts3) []FetchGetAppraccts3Response {
	var response []FetchGetAppraccts3Response
	for _, request := range requests {
		requestResponse := FetchGetAppraccts3Response{
			Hoapart:               request.Hoapart.String,
			Mh:                    request.Mh.String,
			Mh_description:        request.Mh_description.String,
			Smh:                   request.Smh.String,
			Smh_description:       request.Smh_description.String,
			Minorhead:             request.Minorhead.String,
			Minorhead_description: request.Minorhead_description.String,
			Subhoa:                request.Subhoa.String,
			Subhoa_description:    request.Subhoa_description.String,
			O:                     request.O.Float64,
			S:                     request.S.Float64,
			R:                     request.R.Float64,
			TotalExp:              request.TotalExp.Float64,
		}
		response = append(response, requestResponse)
	}
	return response
}

type GetAppraccts3Response struct {
	port.StatusCodeAndMessage `json:",inline"`
	port.MetaDataResponse     `json:",inline"`
	Data                      []FetchGetAppraccts3Response `json:"data"`
}
type FetchGetRemunerationDetResponse struct {
	FinancialYear    string    `json:"financial_year"`
	RemunerationItem string    `json:"remuneration_item"`
	RemunerationType string    `json:"remuneration_type"`
	RemunerationRate float64   `json:"remuneration_rate"`
	UpdatedBy        uint64    `json:"updated_by"`
	UpdatedDate      time.Time `json:"updated_date"`
	ApprovedBy       uint64    `json:"approved_by"`
	ApprovedDate     time.Time `json:"approved_date"`
}

func NewGetremunerationdetResponse(requests []domain.GetRemuneration) []FetchGetRemunerationDetResponse {
	var response []FetchGetRemunerationDetResponse
	for _, request := range requests {
		requestResponse := FetchGetRemunerationDetResponse{
			FinancialYear:    request.FinancialYear.String,
			RemunerationItem: request.RemunerationItem.String,
			RemunerationType: request.RemunerationType.String,
			RemunerationRate: request.RemunerationRate.Float64,
			UpdatedBy:        request.UpdatedBy.Uint64,
			UpdatedDate:      request.UpdatedDate.Time,
			ApprovedBy:       request.ApprovedBy.Uint64,
			ApprovedDate:     request.ApprovedDate.Time,
		}
		response = append(response, requestResponse)
	}
	return response
}

type GetremunerationdetResponse struct {
	port.StatusCodeAndMessage `json:",inline"`
	port.MetaDataResponse     `json:",inline"`
	Data                      []FetchGetRemunerationDetResponse `json:"data"`
}
type RemunerationCreationResponse struct {
	FinancialYear         string  `json:"financial_year"`
	RemunerationItem      string  `json:"remuneration_item"`
	RemunerationType      string  `json:"remuneration_type"`
	RemunerationRate      float32 `json:"remuneration_rate"`
	RemunerationItemCount float32 `json:"remuneration_item_count"`
	ItemRemuneration      float32 `json:"item_remuneration"`
}

func NewRemunerationCreationResponse(requests []domain.RemunerationCreation) []RemunerationCreationResponse {
	var response []RemunerationCreationResponse
	for _, request := range requests {
		requestResponse := RemunerationCreationResponse{
			FinancialYear:         request.FinancialYear,
			RemunerationItem:      request.RemunerationItem,
			RemunerationType:      request.RemunerationType,
			RemunerationRate:      request.RemunerationRate,
			RemunerationItemCount: request.RemunerationItemCount,
			ItemRemuneration:      request.ItemRemuneration,
		}
		response = append(response, requestResponse)
	}
	return response
}

type GetRemunerationCreationResponse struct {
	port.StatusCodeAndMessage `json:",inline"`
	port.MetaDataResponse     `json:",inline"`
	Data                      []RemunerationCreationResponse `json:"data"`
}

func NewRemunerationCreationResponsegrpc(remus []domain.RemunerationCreation) []*pao.RemunerationCreationResponse {
	var responses []*pao.RemunerationCreationResponse
	for _, rem := range remus {
		response := &pao.RemunerationCreationResponse{
			FinancialYear:         rem.FinancialYear,
			RemunerationItem:      rem.RemunerationItem,
			RemunerationType:      rem.RemunerationType,
			RemunerationRate:      rem.RemunerationRate, // handle null values
			RemunerationItemCount: rem.RemunerationItemCount,
			ItemRemuneration:      rem.ItemRemuneration,
		}
		responses = append(responses, response)
	}
	return responses
}
func RemarkstoProtoRemarks(rems []domain.ObjectionRemark) []*pao.ObjectionRemark {
	results := make([]*pao.ObjectionRemark, len(rems))
	for i, r := range rems {
		results[i] = &pao.ObjectionRemark{
			Data:              r.Data.String,
			CommentedBy:       r.CommentedBy.Uint64,
			CommentedDate:     timestamppb.New(r.CommentedDate.Time),
			CommentedOfficeId: r.CommentedOfficeId.Uint64,
			Filepath:          r.Filepath.String,
			Sender:            r.Sender.String,
		}
	}
	return results
}
