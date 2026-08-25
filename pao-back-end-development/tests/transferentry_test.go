package tests

import (
	"bytes"
	"net/http"
	"net/http/httptest"
	"testing"

	"github.com/magiconair/properties/assert"
)

//CreateTransferEntryHandler--------------------------------------------------------------------------------------------------------

func TestCreateTransferEntryHandler_Success(t *testing.T) {
	input := `[
    {
        "pao_code": "099999",
        "ddo_code":        "999991",
        "hoa": "320108101030228",
        "transfer_amount": 110,
        "transfer_type": "D",
        "created_by": 10257696,
        "created_date": "2024-04-06",
        "te_source_office_type": "PAO",
        "remarks":               "created"
    },
    {
        "pao_code": "099999",
        "ddo_code":        "999991",
        "hoa": "320107911010070",
        "transfer_amount": 110,
        "transfer_type": "C",
        "created_by": 10257696,
        "created_date": "2024-04-06",
        "te_source_office_type": "PAO",
        "remarks":               "created"
    }
]`
	// body, _ := json.Marshal(input)
	req := httptest.NewRequest("POST", "/v1/transfer-entry", bytes.NewBuffer([]byte(input)))
	req.Header.Set("Content-Type", "application/json")
	rec := httptest.NewRecorder()
	Router.Engine.ServeHTTP(rec, req)
	assert.Equal(t, http.StatusCreated, rec.Code)
}

func TestCreateTransferEntryHandler_JSONBindingError(t *testing.T) {
	input := `[
    {
        "pao_code": "099999",
        "ddo_code":        "999991",
        "hoa": "320108101030228",
        "transfer_amount": 110,
        "transfer_type": "D",
        "created_by": "10257696",
        "created_date": "2024-04-06",
        "te_source_office_type": "PAO",
        "remarks":               "created"
    },
    {
        "pao_code": "099999",
        "ddo_code":        "999991",
        "hoa": "320107911010070",
        "transfer_amount": 110,
        "transfer_type": "C",
        "created_by": 10257696,
        "created_date": "2024-04-06",
        "te_source_office_type": "PAO",
        "remarks":               "created"
    }
]`
	// body, _ := json.Marshal(input)
	req := httptest.NewRequest("POST", "/v1/transfer-entry", bytes.NewBuffer([]byte(input)))
	req.Header.Set("Content-Type", "application/json")
	rec := httptest.NewRecorder()
	Router.Engine.ServeHTTP(rec, req)

	assert.Equal(t, http.StatusBadRequest, rec.Code)
}

func TestCreateTransferEntryHandler_ValidationError(t *testing.T) {
	input := `[
    {
        "pao_code": "099999",
        "ddo_code":        "999991",
        "hoa": "320108101030228",
        "transfer_amount": 110,
        "transfer_type": "D",
        "created_by": 1025765696,
        "created_date": "2024-04-06",
        "te_source_office_type": "PAO",
        "remarks":               "created"
    },
    {
        "pao_code": "099999",
        "ddo_code":        "999991",
        "hoa": "320107911010070",
        "transfer_amount": 110,
        "transfer_type": "C",
        "created_by": 10257696,
        "created_date": "2024-04-06",
        "te_source_office_type": "PAO",
        "remarks":               "created"
    }
]`
	// body, _ := json.Marshal(input)
	req := httptest.NewRequest("POST", "/v1/transfer-entry", bytes.NewBuffer([]byte(input)))
	req.Header.Set("Content-Type", "application/json")
	rec := httptest.NewRecorder()
	Router.Engine.ServeHTTP(rec, req)

	assert.Equal(t, http.StatusUnprocessableEntity, rec.Code)
}
func TestCreateTransferEntryHandler_CreditDebitError(t *testing.T) {
	input := `[
    {
        "pao_code": "099999",
        "ddo_code":        "999991",
        "hoa": "320108101030228",
        "transfer_amount": 110,
        "transfer_type": "D",
        "created_by": 10257696,
        "created_date": "2024-04-06",
        "te_source_office_type": "PAO",
        "remarks":               "created"
    },
    {
        "pao_code": "099999",
        "ddo_code":        "999991",
        "hoa": "320107911010070",
        "transfer_amount": 120,
        "transfer_type": "C",
        "created_by": 10257696,
        "created_date": "2024-04-06",
        "te_source_office_type": "PAO",
        "remarks":               "created"
    }
]`
	// body, _ := json.Marshal(input)
	req := httptest.NewRequest("POST", "/v1/transfer-entry", bytes.NewBuffer([]byte(input)))
	req.Header.Set("Content-Type", "application/json")
	rec := httptest.NewRecorder()
	Router.Engine.ServeHTTP(rec, req)
	assert.Equal(t, http.StatusInternalServerError, rec.Code)
}

//ListTransferEntryReportHandler------------------------------------------------------------------------------------------

func TestListTransferEntryReportHandler_Success(t *testing.T) {
	// Router = BootstrapTestApp(t)

	req := httptest.NewRequest("GET", "/v1/pao-gen/pao/078109/transfer-entry/reports?from-date=2024-04-01&to-date=2024-12-25&xml-generation-status=pending&verification-status=created&skip=0&limit=0", nil)
	req.Header.Set("Content-Type", "application/json")
	rec := httptest.NewRecorder()
	Router.Engine.ServeHTTP(rec, req)

	assert.Equal(t, http.StatusOK, rec.Code)
}
func TestListTransferEntryReportHandlerAll_Success(t *testing.T) {
	// Router = BootstrapTestApp(t)

	req := httptest.NewRequest("GET", "/v1/pao-gen/pao/078109/transfer-entry/reports?from-date=2024-04-01&to-date=2024-12-25&xml-generation-status=all&verification-status=created&skip=0&limit=0", nil)
	req.Header.Set("Content-Type", "application/json")
	rec := httptest.NewRecorder()
	Router.Engine.ServeHTTP(rec, req)

	assert.Equal(t, http.StatusOK, rec.Code)
}
func TestListTransferEntryReportHandler_UriBindingError(t *testing.T) {

	req := httptest.NewRequest("GET", "/v1/pao-gen/pao//transfer-entry/reports?from-date=2024-04-01&to-date=2024-12-25&xml-generation-status=pending&verification-status=created", nil)
	req.Header.Set("Content-Type", "application/json")
	rec := httptest.NewRecorder()
	Router.Engine.ServeHTTP(rec, req)

	assert.Equal(t, http.StatusBadRequest, rec.Code)
}
func TestListTransferEntryReportHandler_QueryBindingError(t *testing.T) {

	req := httptest.NewRequest("GET", "/v1/pao-gen/pao//transfer-entry/reports?from-date=2024-04-01&to-date=2024-12-25&xml-generation-status=pending&verification-status=created&skip=a&limit=0", nil)
	req.Header.Set("Content-Type", "application/json")
	rec := httptest.NewRecorder()
	Router.Engine.ServeHTTP(rec, req)

	assert.Equal(t, http.StatusBadRequest, rec.Code)
}

func TestListTransferEntryReportHandler_ValidationError(t *testing.T) {

	req := httptest.NewRequest("GET", "/v1/pao-gen/pao/078at9/transfer-entry/reports?from-date=2024-04-01&to-date=2024-12-25&xml-generation-status=pending&verification-status=created&skip=0&limit=0", nil)
	req.Header.Set("Content-Type", "application/json")
	rec := httptest.NewRecorder()
	Router.Engine.ServeHTTP(rec, req)

	assert.Equal(t, http.StatusUnprocessableEntity, rec.Code)
}

//UpdateTransferEntryRejectHandler--------------------------------------------------------------------------------------

func TestUpdateTransferEntryRejectHandler_Success(t *testing.T) {
	input := `{
    "verified_by": 10257696,
    "verification_status": "deleted",
    "approver_remarks": "rejected"
}`
	req := httptest.NewRequest("PUT", "/v1/transfer-entry/09999920240531143137/rejection", bytes.NewBuffer([]byte(input)))
	req.Header.Set("Content-Type", "application/json")
	rec := httptest.NewRecorder()
	Router.Engine.ServeHTTP(rec, req)

	assert.Equal(t, http.StatusOK, rec.Code)
}

func TestUpdateTransferEntryRejectHandler_UriBindingError(t *testing.T) {
	input := `{
    "verified_by": 10257696,
    "verification_status": "deleted",
    "approver_remarks": "rejected"
}`
	req := httptest.NewRequest("PUT", "/v1/transfer-entry//rejection", bytes.NewBuffer([]byte(input)))
	req.Header.Set("Content-Type", "application/json")
	rec := httptest.NewRecorder()
	Router.Engine.ServeHTTP(rec, req)

	assert.Equal(t, http.StatusBadRequest, rec.Code)
}
func TestUpdateTransferEntryRejectHandler_JSONBindingError(t *testing.T) {
	input := `{
    "verified_by": "10257696",
    "verification_status": "deleted",
    "approver_remarks": "rejected"
}`
	req := httptest.NewRequest("PUT", "/v1/transfer-entry/09999920240531143137/rejection", bytes.NewBuffer([]byte(input)))
	req.Header.Set("Content-Type", "application/json")
	rec := httptest.NewRecorder()
	Router.Engine.ServeHTTP(rec, req)

	assert.Equal(t, http.StatusBadRequest, rec.Code)
}

func TestUpdateTransferEntryRejectHandler_ValidationError(t *testing.T) {
	input := `{
    "verified_by": 10257696,
    "verification_status": "ted",
    "approver_remarks": "rejected"
}`
	req := httptest.NewRequest("PUT", "/v1/transfer-entry/09999920240531143137/rejection", bytes.NewBuffer([]byte(input)))
	req.Header.Set("Content-Type", "application/json")
	rec := httptest.NewRecorder()
	Router.Engine.ServeHTTP(rec, req)
	assert.Equal(t, http.StatusUnprocessableEntity, rec.Code)
}

//UpdateTransferEntryVerifyHandler--------------------------------------------------------------------------------------------

func TestUpdateTransferEntryVerifyHandler_Success(t *testing.T) {
	input := `[
        {
            "ddo_code": "102595",
            "hoa": "021001103000000",
            "transfer_amount": 1000,
            "transfer_type": "D",
            "created_date": "2024-08-10T05:58:27Z",
            "transfer_entry_id": "68000120240418163719",
            "verification_status": "verified",
            "verified_by": 10257696,
            "verified_date": "2024-08-10T05:58:27Z",
            "approver_remarks": "verified by Sreejith",
            "office_id": 23748583
        }
]`
	req := httptest.NewRequest("PUT", "/v1/transfer-entry/bulk-verification", bytes.NewBuffer([]byte(input)))
	req.Header.Set("Content-Type", "application/json")
	rec := httptest.NewRecorder()
	Router.Engine.ServeHTTP(rec, req)

	assert.Equal(t, http.StatusOK, rec.Code)
}

// func TestUpdateTransferEntryVerifyHandlerBudget_Failure(t *testing.T) {
// 	input := `[
//         {
//             "ddo_code": "102595",
//             "hoa": "320101103000000",
//             "transfer_amount": 1000,
//             "transfer_type": "D",
//             "created_date": "2024-08-10T05:58:27Z",
//             "transfer_entry_id": "68000120240418163719",
//             "verification_status": "verified",
//             "verified_by": 10257696,
//             "verified_date": "2024-08-10T05:58:27Z",
//             "approver_remarks": "verified by Sreejith",
//             "office_id": 23748583
//         }
// ]`
// 	req := httptest.NewRequest("PUT", "/v1/transfer-entry/bulk-verification", bytes.NewBuffer([]byte(input)))
// 	req.Header.Set("Content-Type", "application/json")
// 	rec := httptest.NewRecorder()
// 	Router.Engine.ServeHTTP(rec, req)

// 	assert.Equal(t, http.StatusOK, rec.Code)
// }

func TestUpdateTransferEntryVerifyHandler_JSONBindingError(t *testing.T) {
	input := `[
        {
            "ddo_code": "102595",
            "hoa": "021001103000000",
            "transfer_amount": 1000,
            "transfer_type": "D",
            "created_date": "2024-08-10T05:58:27Z",
            "transfer_entry_id": "68000120240418163719",
            "verification_status": "verified",
            "verified_by": "10257696",
            "verified_date": "2024-08-10T05:58:27Z",
            "approver_remarks": "verified by Sreejith",
            "office_id": 23748583
        }
]`
	req := httptest.NewRequest("PUT", "/v1/transfer-entry/bulk-verification", bytes.NewBuffer([]byte(input)))
	req.Header.Set("Content-Type", "application/json")
	rec := httptest.NewRecorder()
	Router.Engine.ServeHTTP(rec, req)

	assert.Equal(t, http.StatusBadRequest, rec.Code)
}

func TestUpdateTransferEntryVerifyHandler_ValidationError(t *testing.T) {
	input := `[
        {
            "ddo_code": "102595",
            "hoa": "021001103000000",
            "transfer_amount": 1000,
            "transfer_type": "D",
            "created_date": "2024-08-10T05:58:27Z",
            "transfer_entry_id": "68000120240418163719",
            "verification_status": "vied",
            "verified_by": 10257696,
            "verified_date": "2024-08-10T05:58:27Z",
            "approver_remarks": "verified by Sreejith",
            "office_id": 23748583
        }
]`
	req := httptest.NewRequest("PUT", "/v1/transfer-entry/bulk-verification", bytes.NewBuffer([]byte(input)))
	req.Header.Set("Content-Type", "application/json")
	rec := httptest.NewRecorder()
	Router.Engine.ServeHTTP(rec, req)
	assert.Equal(t, http.StatusUnprocessableEntity, rec.Code)
}

//ListDdoTransferEntryReportHandler----------------------------------------------------------------------------------------

func TestListDdoTransferEntryReportHandler_Success(t *testing.T) {
	// Router = BootstrapTestApp(t)

	req := httptest.NewRequest("GET", "/v1/pao-gen/ddo/102595/transfer-entry/sub-accounts/reports?from-date=2023-01-01&to-date=2024-12-28&status=approved&skip=0&limit=0", nil)
	req.Header.Set("Content-Type", "application/json")
	rec := httptest.NewRecorder()
	Router.Engine.ServeHTTP(rec, req)

	assert.Equal(t, http.StatusOK, rec.Code)
}
func TestListDdoTransferEntryReportHandler_UriBindingError(t *testing.T) {

	req := httptest.NewRequest("GET", "/v1/pao-gen/ddo//transfer-entry/sub-accounts/reports?from-date=2023-01-01&to-date=2024-07-25&status=approved", nil)
	req.Header.Set("Content-Type", "application/json")
	rec := httptest.NewRecorder()
	Router.Engine.ServeHTTP(rec, req)

	assert.Equal(t, http.StatusBadRequest, rec.Code)
}
func TestListDdoTransferEntryReportHandler_QueryBindingError(t *testing.T) {

	req := httptest.NewRequest("GET", "/v1/pao-gen/ddo/102557/transfer-entry/sub-accounts/reports?from-date=2023-01-01&to-date=2024-07-25&status=approved&skip=a&limit=0", nil)
	req.Header.Set("Content-Type", "application/json")
	rec := httptest.NewRecorder()
	Router.Engine.ServeHTTP(rec, req)

	assert.Equal(t, http.StatusBadRequest, rec.Code)
}

func TestListDdoTransferEntryReportHandler_ValidationError(t *testing.T) {

	req := httptest.NewRequest("GET", "/v1/pao-gen/ddo/1025y7557/transfer-entry/sub-accounts/reports?from-date=2023-01-01&to-date=2024-07-25&status=ved", nil)
	req.Header.Set("Content-Type", "application/json")
	rec := httptest.NewRecorder()
	Router.Engine.ServeHTTP(rec, req)

	assert.Equal(t, http.StatusUnprocessableEntity, rec.Code)
}

//ListPaoSubTransferEntryReportHandler---------------------------------------------------------------------------------

func TestListPaoSubTransferEntryReportHandler_Success(t *testing.T) {
	// Router = BootstrapTestApp(t)

	req := httptest.NewRequest("GET", "/v1/pao-gen/pao/078119/transfer-entry/sub-accounts/pao-reports?from-date=2023-01-01&to-date=2024-12-29&status=approved&type=1", nil)
	req.Header.Set("Content-Type", "application/json")
	rec := httptest.NewRecorder()
	Router.Engine.ServeHTTP(rec, req)

	assert.Equal(t, http.StatusOK, rec.Code)
}
func TestListPaoSubTransferEntryReportHandler2_Success(t *testing.T) {
	// Router = BootstrapTestApp(t)

	req := httptest.NewRequest("GET", "/v1/pao-gen/pao/078109/transfer-entry/sub-accounts/pao-reports?from-date=2023-01-01&to-date=2024-07-25&status=approved&type=2", nil)
	req.Header.Set("Content-Type", "application/json")
	rec := httptest.NewRecorder()
	Router.Engine.ServeHTTP(rec, req)

	assert.Equal(t, http.StatusOK, rec.Code)
}
func TestListPaoSubTransferEntryReportHandler_UriBindingError(t *testing.T) {

	req := httptest.NewRequest("GET", "/v1/pao-gen/pao//transfer-entry/sub-accounts/pao-reports?from-date=2023-01-01&to-date=2024-07-25&status=approved&type=1", nil)
	req.Header.Set("Content-Type", "application/json")
	rec := httptest.NewRecorder()
	Router.Engine.ServeHTTP(rec, req)

	assert.Equal(t, http.StatusBadRequest, rec.Code)
}
func TestListPaoSubTransferEntryReportHandler_QueryBindingError(t *testing.T) {

	req := httptest.NewRequest("GET", "/v1/pao-gen/pao/078109/transfer-entry/sub-accounts/pao-reports?from-date=2023-01-01&to-date=2024-07-25&status=approved&type=1&skip=a&limit=0", nil)
	req.Header.Set("Content-Type", "application/json")
	rec := httptest.NewRecorder()
	Router.Engine.ServeHTTP(rec, req)

	assert.Equal(t, http.StatusBadRequest, rec.Code)
}

func TestListPaoSubTransferEntryReportHandler_ValidationError(t *testing.T) {

	req := httptest.NewRequest("GET", "/v1/pao-gen/pao/078hu09/transfer-entry/sub-accounts/pao-reports?from-date=2023-01-01&to-date=2024-07-25&status=oved&type=1", nil)
	req.Header.Set("Content-Type", "application/json")
	rec := httptest.NewRecorder()
	Router.Engine.ServeHTTP(rec, req)

	assert.Equal(t, http.StatusUnprocessableEntity, rec.Code)
}

//CreateSubaccountsTeVerifiedHandler------------------------------------------------------------------------

//	func TestCreateSubaccountsTeVerifiedHandler_Success(t *testing.T) {
//		input := `{
//	    "sub_tes":[
//	   {
//	            "pao_code": "078109",
//	            "ddo_code": "102595",
//	            "trans_id": "10259520241204174407",
//	            "hoa": "021001103000000",
//	            "account_code": "9210000100",
//	            "transfer_amount": 1000,
//	            "transfer_type": "D",
//	            "created_by": 11100011,
//	            "created_date": "2024-08-10T05:58:27Z",
//	            "status": "approved",
//	            "approved_by":10257696,
//	            "approved_date": "2024-08-10T05:58:27Z",
//	            "approver_remarks": "approver remark"
//	    }
//	    ]
//	}`
//
//		// body, _ := json.Marshal(input)
//		req := httptest.NewRequest("POST", "/v1/transfer-entry/sub-accounts/verification", bytes.NewBuffer([]byte(input)))
//		req.Header.Set("Content-Type", "application/json")
//		rec := httptest.NewRecorder()
//		Router.Engine.ServeHTTP(rec, req)
//		assert.Equal(t, http.StatusCreated, rec.Code)
//	}
func TestCreateSubaccountsTeVerifiedHandlerBudget_Failure(t *testing.T) {
	input := `{
    "sub_tes":[
   {
            "pao_code": "078109",
            "ddo_code": "102598",
            "trans_id": "10259520241204174407",
            "hoa": "320101103000000",
            "account_code": "9210000100",
            "transfer_amount": 1000,
            "transfer_type": "D",
            "created_by": 11100011,
            "created_date": "2024-08-10T05:58:27Z",
            "status": "approved",
            "approved_by":10257696,
            "approved_date": "2024-08-10T05:58:27Z",
            "approver_remarks": "approver remark"
    }
    ]
}`
	// body, _ := json.Marshal(input)
	req := httptest.NewRequest("POST", "/v1/transfer-entry/sub-accounts/verification", bytes.NewBuffer([]byte(input)))
	req.Header.Set("Content-Type", "application/json")
	rec := httptest.NewRecorder()
	Router.Engine.ServeHTTP(rec, req)
	assert.Equal(t, http.StatusInternalServerError, rec.Code)
}

func TestCreateSubaccountsTeVerifiedHandler_JSONBindingError(t *testing.T) {
	input := `{
    "sub_tes":[
   {
            "pao_code": "078109",
            "ddo_code": "102595",
            "trans_id": "10259520241204174407",
            "hoa": "021001103000000",
            "account_code": "9210000100",
            "transfer_amount": 1000,
            "transfer_type": "D",
            "created_by": 11100011,
            "created_date": "2024-08-10T05:58:27Z",
            "status": "approved",
            "approved_by":1"0257696",
            "approved_date": "2024-08-10T05:58:27Z",
            "approver_remarks": "approver remark"
    }
    ]
}`
	// body, _ := json.Marshal(input)
	req := httptest.NewRequest("POST", "/v1/transfer-entry/sub-accounts/verification", bytes.NewBuffer([]byte(input)))
	req.Header.Set("Content-Type", "application/json")
	rec := httptest.NewRecorder()
	Router.Engine.ServeHTTP(rec, req)

	assert.Equal(t, http.StatusBadRequest, rec.Code)
}

func TestCreateSubaccountsTeVerifiedHandler_ValidationError(t *testing.T) {
	input := `{
    "sub_tes":[
   {
            "pao_code": "078109",
            "ddo_code": "102595",
            "trans_id": "10259520241204174407",
            "hoa": "021001103000000",
            "account_code": "9210000100",
            "transfer_amount": 1000,
            "transfer_type": "D",
            "created_by": 11100011,
            "created_date": "2024-08-10T05:58:27Z",
            "status": "oved",
            "approved_by":10223457696,
            "approved_date": "2024-08-10T05:58:27Z",
            "approver_remarks": "approver remark"
    }
    ]
}`
	// body, _ := json.Marshal(input)
	req := httptest.NewRequest("POST", "/v1/transfer-entry/sub-accounts/verification", bytes.NewBuffer([]byte(input)))
	req.Header.Set("Content-Type", "application/json")
	rec := httptest.NewRecorder()
	Router.Engine.ServeHTTP(rec, req)

	assert.Equal(t, http.StatusUnprocessableEntity, rec.Code)
}

//FetchPaoSubTransferentryDetailHandler---------------------------------------------------------------------------

func TestFetchPaoSubTransferentryDetailHandler_Success(t *testing.T) {
	// Router = BootstrapTestApp(t)

	req := httptest.NewRequest("GET", "/v1/transfer-entry/sub-accounts/details/68000120240228102841", nil)
	req.Header.Set("Content-Type", "application/json")
	rec := httptest.NewRecorder()
	Router.Engine.ServeHTTP(rec, req)

	assert.Equal(t, http.StatusOK, rec.Code)
}

func TestFetchPaoSubTransferentryDetailHandler_ValidationError(t *testing.T) {

	req := httptest.NewRequest("GET", "/v1/transfer-entry/sub-accounts/details/6800012dsfwerfddfgsaerdfsaddfg0240226113437", nil)
	req.Header.Set("Content-Type", "application/json")
	rec := httptest.NewRecorder()
	Router.Engine.ServeHTTP(rec, req)

	assert.Equal(t, http.StatusUnprocessableEntity, rec.Code)
}

//CreatePfmsTeHandler-------------------------------------------------------------------------------------------------------

// func TestCreatePfmsTeHandler_Success(t *testing.T) {
// 	input := `[
//   {"te_id": "09999920240507142633","te_date": "2024-03-08","pao_code":"099999","fin_year":"2024"},
//     { "te_id": "09999920240508065734","te_date": "2024-03-08","pao_code":"099999","fin_year":"2024"}
// ]`
// 	// body, _ := json.Marshal(input)
// 	req := httptest.NewRequest("POST", "/v1/transfer-entry/xml-generation", bytes.NewBuffer([]byte(input)))
// 	req.Header.Set("Content-Type", "application/json")
// 	rec := httptest.NewRecorder()
// 	Router.Engine.ServeHTTP(rec, req)
// 	assert.Equal(t, http.StatusCreated, rec.Code)
// }

func TestCreatePfmsTeHandler_JSONBindingError(t *testing.T) {
	input := `[
  {"te_id": "09999920240507142633","te_date": "2024-03-08","pao_code":099999,"fin_year":"2024"},
    { "te_id": "09999920240508065734","te_date": "2024-03-08","pao_code":"099999","fin_year":"2024"}
]`
	// body, _ := json.Marshal(input)
	req := httptest.NewRequest("POST", "/v1/transfer-entry/xml-generation", bytes.NewBuffer([]byte(input)))
	rec := httptest.NewRecorder()
	Router.Engine.ServeHTTP(rec, req)

	assert.Equal(t, http.StatusBadRequest, rec.Code)
}

func TestCreatePfmsTeHandler_ValidationError(t *testing.T) {
	input := `[
  {"te_id": "09999920240507142633","te_date": "2024-03-08","pao_code":"099999","fin_year":"24"},
    { "te_id": "09999920240508065734","te_date": "2024-03-08","pao_code":"099999","fin_year":"2024"}
]`
	// body, _ := json.Marshal(input)
	req := httptest.NewRequest("POST", "/v1/transfer-entry/xml-generation", bytes.NewBuffer([]byte(input)))
	req.Header.Set("Content-Type", "application/json")
	rec := httptest.NewRecorder()
	Router.Engine.ServeHTTP(rec, req)

	assert.Equal(t, http.StatusUnprocessableEntity, rec.Code)
}

//CreateTransferEntryDirectHandler------------------------------------------------------------------------------------

func TestCreateTransferEntryDirectHandler_Success(t *testing.T) {
	input := `[
    {
        "pao_code": "099999",
        "ddo_code":        "999991",
        "hoa": "878900128000000",
        "transfer_amount": 110,
        "transfer_type": "D",
        "created_by": 10257696,
        "verified_by": 10257695,
        "te_source_office_type": "PAO",
        "remarks":               "BRS Entry"
    },
    {
        "pao_code": "099999",
        "ddo_code":        "999991",
        "hoa": "878700139000000",
        "transfer_amount": 110,
        "transfer_type": "C",
        "created_by": 10257696,
        "verified_by": 10257695,
        "te_source_office_type": "PAO",
        "remarks":               "BRS Entry"
    }
]`
	// body, _ := json.Marshal(input)
	req := httptest.NewRequest("POST", "/v1/transfer-entry/direct", bytes.NewBuffer([]byte(input)))
	req.Header.Set("Content-Type", "application/json")
	rec := httptest.NewRecorder()
	Router.Engine.ServeHTTP(rec, req)
	assert.Equal(t, http.StatusCreated, rec.Code)
}

func TestCreateTransferEntryDirectHandler_JSONBindingError(t *testing.T) {
	input := `[
    {
        "pao_code": "099999",
        "ddo_code":        "999991",
        "hoa": "878900128000000",
        "transfer_amount": 110,
        "transfer_type": "D",
        "created_by": "10257696",
        "verified_by": 10257695,
        "te_source_office_type": "PAO",
        "remarks":               "BRS Entry"
    },
    {
        "pao_code": "099999",
        "ddo_code":        "999991",
        "hoa": "878700139000000",
        "transfer_amount": 110,
        "transfer_type": "C",
        "created_by": 10257696,
        "verified_by": 10257695,
        "te_source_office_type": "PAO",
        "remarks":               "BRS Entry"
    }
]`
	// body, _ := json.Marshal(input)
	req := httptest.NewRequest("POST", "/v1/transfer-entry/direct", bytes.NewBuffer([]byte(input)))
	req.Header.Set("Content-Type", "application/json")
	rec := httptest.NewRecorder()
	Router.Engine.ServeHTTP(rec, req)

	assert.Equal(t, http.StatusBadRequest, rec.Code)
}

func TestCreateTransferEntryDirectHandler_ValidationError(t *testing.T) {
	input := `[
    {
        "pao_code": "099999",
        "ddo_code":        "999991",
        "hoa": "878900128000000",
        "transfer_amount": 110,
        "transfer_type": "D",
        "created_by": 102596,
        "verified_by": 10257695,
        "te_source_office_type": "PAO",
        "remarks":               "BRS Entry"
    },
    {
        "pao_code": "099999",
        "ddo_code":        "999991",
        "hoa": "878700139000000",
        "transfer_amount": 110,
        "transfer_type": "C",
        "created_by": 10257696,
        "verified_by": 10257695,
        "te_source_office_type": "PAO",
        "remarks":               "BRS Entry"
    }
]`
	// body, _ := json.Marshal(input)
	req := httptest.NewRequest("POST", "/v1/transfer-entry/direct", bytes.NewBuffer([]byte(input)))
	req.Header.Set("Content-Type", "application/json")
	rec := httptest.NewRecorder()
	Router.Engine.ServeHTTP(rec, req)

	assert.Equal(t, http.StatusUnprocessableEntity, rec.Code)
}
func TestCreateTransferEntryDirectHandler_CreditDebitError(t *testing.T) {
	input := `[
    {
        "pao_code": "099999",
        "ddo_code":        "999991",
        "hoa": "878900128000000",
        "transfer_amount": 110,
        "transfer_type": "D",
        "created_by": 10257696,
        "verified_by": 10257695,
        "te_source_office_type": "PAO",
        "remarks":               "BRS Entry"
    },
    {
        "pao_code": "099999",
        "ddo_code":        "999991",
        "hoa": "878700139000000",
        "transfer_amount": 120,
        "transfer_type": "C",
        "created_by": 10257696,
        "verified_by": 10257695,
        "te_source_office_type": "PAO",
        "remarks":               "BRS Entry"
    }
]`
	// body, _ := json.Marshal(input)
	req := httptest.NewRequest("POST", "/v1/transfer-entry/direct", bytes.NewBuffer([]byte(input)))
	req.Header.Set("Content-Type", "application/json")
	rec := httptest.NewRecorder()
	Router.Engine.ServeHTTP(rec, req)
	assert.Equal(t, http.StatusInternalServerError, rec.Code)
}

func TestHealthCheckHandler_Success(t *testing.T) {
	req := httptest.NewRequest("GET", "/healthz", nil)
	req.Header.Set("Content-Type", "application/json")
	rec := httptest.NewRecorder()
	Router.Engine.ServeHTTP(rec, req)
	assert.Equal(t, http.StatusOK, rec.Code)
}
