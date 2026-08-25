package handler

import (
	"gotemplate/core/domain"
	"strconv"

	pao "gotemplate/gen/proto/v1"

	"github.com/volatiletech/null/v9"
)

func convertObjectionRemarkToDomainObjectionRemarkRequestCreate(domainResult []ObjectionRemarkcreate) []domain.ObjectionRemarkRequest {
	results := make([]domain.ObjectionRemarkRequest, len(domainResult))
	for i, r := range domainResult {
		results[i] = domain.ObjectionRemarkRequest{
			Data:              r.Data,
			CommentedBy:       r.CommentedBy,
			CommentedDate:     r.CommentedDate,
			CommentedOfficeId: r.CommentedOfficeId,
			Filepath:          r.Filepath,
			Sender:            r.Sender,
			// EcmsTransactionId: r.EcmsTransactionId,
			// EcmsServiceName:   r.EcmsServiceName,
		}
	}
	return results
}

func convertObjectionRemarkToDomainObjectionRemark(domainResult ObjectionRemark) domain.ObjectionRemark {

	results := domain.ObjectionRemark{
		Data:              null.StringFrom(domainResult.Data),
		CommentedBy:       null.Uint64From(domainResult.CommentedBy),
		CommentedDate:     null.TimeFrom(domainResult.CommentedDate),
		CommentedOfficeId: null.Uint64From(domainResult.CommentedOfficeId),
		Filepath:          null.StringFrom(domainResult.Filepath),
		Sender:            null.StringFrom(domainResult.Sender),
		// EcmsTransactionId: null.StringFrom(domainResult.EcmsTransactionId),
		// EcmsServiceName:   null.StringFrom(domainResult.EcmsServiceName),
	}

	return results
}
func convertOCodearrayToDomainCodearrayRequest(domainResult []CodeArray) []domain.CodeArray {
	results := make([]domain.CodeArray, len(domainResult))
	for i, r := range domainResult {
		results[i] = domain.CodeArray{
			AccountCode:            null.StringFrom(r.AccountCode),
			AccountCodeDescription: null.StringFrom(r.AccountCodeDescription),
			Receipt:                null.Float64From(r.Receipt),
			Payment:                null.Float64From(r.Payment),
		}
	}
	return results
}
func convertSubTeVerifiedToSubTeVerified(domainResult []SubTeVerified) []domain.SubTeVerified {
	results := make([]domain.SubTeVerified, len(domainResult))
	for i, r := range domainResult {
		results[i] = domain.SubTeVerified{
			PaoCode:         r.PaoCode,
			DdoCode:         r.DdoCode,
			TransId:         r.TransId,
			Hoa:             r.Hoa,
			AccountCode:     r.AccountCode,
			TransferAmount:  r.TransferAmount,
			TransferType:    r.TransferType,
			CreatedBy:       r.CreatedBy,
			CreatedDate:     r.CreatedDate,
			Status:          r.Status,
			ApprovedBy:      r.ApprovedBy,
			ApprovedDate:    r.ApprovedDate,
			ApproverRemarks: r.ApproverRemarks,
			WorkflowId:       r.WorkflowId,
			TransDate: r.TransDate,
		}
	}
	return results
}
func ProtoRemarkstoRemarks(rems []*pao.ObjectionRemarkcreate) []domain.ObjectionRemarkRequest {
	results := make([]domain.ObjectionRemarkRequest, len(rems))
	for i, r := range rems {
		results[i] = domain.ObjectionRemarkRequest{
			Data:              r.Data,
			CommentedBy:       r.CommentedBy,
			CommentedDate:     r.CommentedDate.AsTime(),
			CommentedOfficeId: r.CommentedOfficeId,
			Filepath:          r.Filepath,
			Sender:            r.Sender,
		
		}
	}
	return results
}

// ConvertToDetailsArray converts a slice of TransferEntryAccountingDetail to a slice of TransferEntryAccountingDetails
func ConvertToDetailsArray(details []domain.TransferEntryAccountingDetail) []domain.TransferEntryAccountingDetails {
	var result []domain.TransferEntryAccountingDetails
	for _, detail := range details {
		if detail.DdoOfficeID.Valid && detail.DdoOfficeID.String != "" &&
			detail.GrantNo.Valid && detail.GrantNo.String != "" &&
			detail.FunctionalHead.Valid && detail.FunctionalHead.String != "" &&
			detail.ObjectHead.Valid && detail.ObjectHead.String != "" &&
			detail.Category.Valid && detail.Category.String != "" &&
			detail.Sign.Valid && detail.Sign.String != "" &&
			detail.Amount.Valid && detail.Amount.Float64 > 0 &&
			detail.Remarks.Valid && detail.Remarks.String != "" &&
			detail.ReceiptPayment.Valid && detail.ReceiptPayment.String != "" {
			result = append(result, domain.TransferEntryAccountingDetails{
				DDOCode:        detail.DdoOfficeID.String,
				GrantNo:        detail.GrantNo.String,
				FunctionalHead: detail.FunctionalHead.String,
				ObjectHead:     detail.ObjectHead.String,
				Category:       detail.Category.String,
				Sign:           detail.Sign.String,
				Amount:         detail.Amount.Float64,
				Remarks:        detail.Remarks.String,
				Transfer:       detail.ReceiptPayment.String,
			})
		}
	}
	return result
}



func BuildReversalPayload(
    original domain.Payload,
    paoCode string,
    finYear string,
    teDate string,
) (domain.Payload, string) {

    originalDetails := original.RequestPayload.
        TransferEntryDetails[0].
        TransferEntryData.
        TransferEntryAccountingDetails

    reversedDetails := make(
        []domain.TransferEntryAccountingDetails,
        len(originalDetails),
    )
    copy(reversedDetails, originalDetails)

    for i, line := range reversedDetails {
        if line.Sign == "+" {
            reversedDetails[i].Sign = "-"
        } else if line.Sign == "-" {
            reversedDetails[i].Sign = "+"
        }
    }

    reversalUID  := GenerateRandomNumber(paoCode, finYear)
    finYearInt, _ := strconv.Atoi(finYear)

    reversalTE := domain.TransferEntryDetail{
        UniqueIdentifier: reversalUID,
        RequestSource:    "POST",
        PaoCode:          paoCode,
        FinancialYear:    finYearInt,
        TransferEntryData: domain.TransferEntryData{
            InstrumentType:                 "Others",
            Remarks:                        "Reversal: DoP Daily Account",
            TEDate:                         teDate,
            TransferEntryAccountingDetails: reversedDetails,
        },
    }

    var reversalPayload domain.Payload
    reversalPayload.RequestPayload.TransferEntryDetails =
        []domain.TransferEntryDetail{reversalTE}

    return reversalPayload, reversalUID
}