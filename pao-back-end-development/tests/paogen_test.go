package tests

import (
	"bytes"
	"net/http"
	"net/http/httptest"
	"testing"

	"github.com/magiconair/properties/assert"
)

//FetchOfficenameHandler------------------------------------------------------------------------------------------------

func TestFetchOfficenameHandler_Success(t *testing.T) {
	// Router = BootstrapTestApp(t)

	req := httptest.NewRequest("GET", "/v1/pao-gen/office-names/21280551", nil)
	req.Header.Set("Content-Type", "application/json")
	rec := httptest.NewRecorder()
	Router.Engine.ServeHTTP(rec, req)

	assert.Equal(t, http.StatusOK, rec.Code)
}
func TestFetchOfficenameHandler_UriBindingError(t *testing.T) {

	req := httptest.NewRequest("GET", "/v1/pao-gen/office-names/'21280551'", nil)
	req.Header.Set("Content-Type", "application/json")
	rec := httptest.NewRecorder()
	Router.Engine.ServeHTTP(rec, req)

	assert.Equal(t, http.StatusBadRequest, rec.Code)
}

func TestFetchOfficenameHandler_ValidationError(t *testing.T) {

	req := httptest.NewRequest("GET", "/v1/pao-gen/office-names/3033474201", nil)
	req.Header.Set("Content-Type", "application/json")
	rec := httptest.NewRecorder()
	Router.Engine.ServeHTTP(rec, req)

	assert.Equal(t, http.StatusUnprocessableEntity, rec.Code)
}
func TestFetchOfficenameHandler_NoOfficeFound(t *testing.T) {

	req := httptest.NewRequest("GET", "/v1/pao-gen/office-names/30334745", nil)
	req.Header.Set("Content-Type", "application/json")
	rec := httptest.NewRecorder()
	Router.Engine.ServeHTTP(rec, req)

	assert.Equal(t, http.StatusNotFound, rec.Code)
}

//ListPAOHandler------------------------------------------------------------------------------------

func TestListPAOHandler_Success(t *testing.T) {
	// Router = BootstrapTestApp(t)

	req := httptest.NewRequest("GET", "/v1/pao-gen/pao", nil)
	req.Header.Set("Content-Type", "application/json")
	rec := httptest.NewRecorder()
	Router.Engine.ServeHTTP(rec, req)

	assert.Equal(t, http.StatusOK, rec.Code)
}

//ListDDOHandler------------------------------------------------------------------------------------------

func TestListDDOHandler_Success(t *testing.T) {
	// Router = BootstrapTestApp(t)

	req := httptest.NewRequest("GET", "/v1/pao-gen/pao/078118/ddos?skip=0&limit=0", nil)
	req.Header.Set("Content-Type", "application/json")
	rec := httptest.NewRecorder()
	Router.Engine.ServeHTTP(rec, req)

	assert.Equal(t, http.StatusOK, rec.Code)
}
func TestListDDOHandlerAll_Success(t *testing.T) {
	// Router = BootstrapTestApp(t)

	req := httptest.NewRequest("GET", "/v1/pao-gen/pao/999999/ddos", nil)
	req.Header.Set("Content-Type", "application/json")
	rec := httptest.NewRecorder()
	Router.Engine.ServeHTTP(rec, req)

	assert.Equal(t, http.StatusOK, rec.Code)
}
func TestListDDOHandler_UriBindingError(t *testing.T) {

	req := httptest.NewRequest("GET", "/v1/pao-gen/pao/078118'/ddos", nil)
	req.Header.Set("Content-Type", "application/json")
	rec := httptest.NewRecorder()
	Router.Engine.ServeHTTP(rec, req)

	assert.Equal(t, http.StatusBadRequest, rec.Code)
}
func TestListDDOHandler_QueryBindingError(t *testing.T) {

	req := httptest.NewRequest("GET", "/v1/pao-gen/pao/078118/ddos?skip=d&limit=0", nil)
	req.Header.Set("Content-Type", "application/json")
	rec := httptest.NewRecorder()
	Router.Engine.ServeHTTP(rec, req)

	assert.Equal(t, http.StatusBadRequest, rec.Code)
}

func TestListDDOHandler_ValidationError(t *testing.T) {

	req := httptest.NewRequest("GET", "/v1/pao-gen/pao/07811q/ddos", nil)
	req.Header.Set("Content-Type", "application/json")
	rec := httptest.NewRecorder()
	Router.Engine.ServeHTTP(rec, req)

	assert.Equal(t, http.StatusUnprocessableEntity, rec.Code)
}

//ListDDOPFMSHandler----------------------------------------------------------------------------------

func TestListDDOPFMSHandler_Success(t *testing.T) {
	// Router = BootstrapTestApp(t)

	req := httptest.NewRequest("GET", "/v1/pao-gen/pao/078103/cashbook/ddo-lists?date=2024-07-19&skip=0&limit=0", nil)
	req.Header.Set("Content-Type", "application/json")
	rec := httptest.NewRecorder()
	Router.Engine.ServeHTTP(rec, req)

	assert.Equal(t, http.StatusOK, rec.Code)
}
func TestListDDOPFMSHandler_UriBindingError(t *testing.T) {

	req := httptest.NewRequest("GET", "/v1/pao-gen/pao/078103'/cashbook/ddo-lists?date=2024-07-19", nil)
	req.Header.Set("Content-Type", "application/json")
	rec := httptest.NewRecorder()
	Router.Engine.ServeHTTP(rec, req)

	assert.Equal(t, http.StatusBadRequest, rec.Code)
}
func TestListDDOPFMSHandler_QueryBindingError(t *testing.T) {

	req := httptest.NewRequest("GET", "/v1/pao-gen/pao/078103/cashbook/ddo-lists?date=2024-07-19&skip=d&limit=0", nil)
	req.Header.Set("Content-Type", "application/json")
	rec := httptest.NewRecorder()
	Router.Engine.ServeHTTP(rec, req)

	assert.Equal(t, http.StatusBadRequest, rec.Code)
}

func TestListDDOPFMSHandler_ValidationError(t *testing.T) {

	req := httptest.NewRequest("GET", "/v1/pao-gen/pao/0781ad/cashbook/ddo-lists?date=2024-07-19", nil)
	req.Header.Set("Content-Type", "application/json")
	rec := httptest.NewRecorder()
	Router.Engine.ServeHTTP(rec, req)

	assert.Equal(t, http.StatusUnprocessableEntity, rec.Code)
}

//UpdateDDOCashbookListHandler-------------------------------------------------------------------------------

func TestUpdateDDOCashbookListHandler_Success(t *testing.T) {
	// body, _ := json.Marshal(input)
	req := httptest.NewRequest("PUT", "/v1/pao-gen/pao/078109/cashbook/ddo-lists?from-date=2024-04-12&to-date=2024-10-12", nil)
	req.Header.Set("Content-Type", "application/json")
	rec := httptest.NewRecorder()
	Router.Engine.ServeHTTP(rec, req)

	assert.Equal(t, http.StatusOK, rec.Code)
}

func TestUpdateDDOCashbookListHandler_UriBindingError(t *testing.T) {

	req := httptest.NewRequest("PUT", "/v1/pao-gen/pao/078109'/cashbook/ddo-lists?from-date=2024-04-12&to-date=2024-10-12", nil)
	req.Header.Set("Content-Type", "application/json")
	rec := httptest.NewRecorder()
	Router.Engine.ServeHTTP(rec, req)

	assert.Equal(t, http.StatusBadRequest, rec.Code)
}
func TestUpdateDDOCashbookListHandler_QueryBindingError(t *testing.T) {

	req := httptest.NewRequest("PUT", "/v1/pao-gen/pao/078109/cashbook/ddo-lists?from-date=2024-04-12-323423sdogimn&to-date=2024-10-12", nil)
	req.Header.Set("Content-Type", "application/json")
	rec := httptest.NewRecorder()
	Router.Engine.ServeHTTP(rec, req)

	assert.Equal(t, http.StatusBadRequest, rec.Code)
}

func TestUpdateDDOCashbookListHandler_ValidationError(t *testing.T) {
	req := httptest.NewRequest("PUT", "/v1/pao-gen/pao/07er09/cashbook/ddo-lists?from-date=2024-04-12&to-date=2024-10-12", nil)
	req.Header.Set("Content-Type", "application/json")
	rec := httptest.NewRecorder()
	Router.Engine.ServeHTTP(rec, req)
	assert.Equal(t, http.StatusUnprocessableEntity, rec.Code)
}

//FetchDDOCashbookHandler-------------------------------------------------------------------------------------------

func TestFetchDDOCashbookHandler_Success(t *testing.T) {
	// Router = BootstrapTestApp(t)

	req := httptest.NewRequest("GET", "/v1/pao-gen/ddo/102595/cashbook/ddo-details?date=2024-07-29&skip=0&limit=0", nil)
	req.Header.Set("Content-Type", "application/json")
	rec := httptest.NewRecorder()
	Router.Engine.ServeHTTP(rec, req)

	assert.Equal(t, http.StatusOK, rec.Code)
}
func TestFetchDDOCashbookHandler_UriBindingError(t *testing.T) {

	req := httptest.NewRequest("GET", "/v1/pao-gen/ddo/102604'/cashbook/ddo-details?date=2024-07-31", nil)
	req.Header.Set("Content-Type", "application/json")
	rec := httptest.NewRecorder()
	Router.Engine.ServeHTTP(rec, req)

	assert.Equal(t, http.StatusBadRequest, rec.Code)
}
func TestFetchDDOCashbookHandler_QueryBindingError(t *testing.T) {

	req := httptest.NewRequest("GET", "/v1/pao-gen/ddo/102604/cashbook/ddo-details?date=20-07-31", nil)
	req.Header.Set("Content-Type", "application/json")
	rec := httptest.NewRecorder()
	Router.Engine.ServeHTTP(rec, req)

	assert.Equal(t, http.StatusBadRequest, rec.Code)
}

func TestFetchDDOCashbookHandler_ValidationError(t *testing.T) {

	req := httptest.NewRequest("GET", "/v1/pao-gen/ddo/102edg/cashbook/ddo-details?date=2024-07-31", nil)
	req.Header.Set("Content-Type", "application/json")
	rec := httptest.NewRecorder()
	Router.Engine.ServeHTTP(rec, req)

	assert.Equal(t, http.StatusUnprocessableEntity, rec.Code)
}

//CreatePFMSVerificationHandler---------------------------------------------------------------------------------

func TestCreatePFMSVerificationHandler_Success(t *testing.T) {
	input := `[
        {
            "ddo_code": "102604",
            "business_date": "2024-08-21T00:00:00Z",
            "closing_bal": 21000,
            "opening_bal": 11000,
            "verified_by": 10257696,
            "h_verification": "true",
            "hoa": "120100101200400",
            "payment": 0,
            "receipt": 28000,
            "account_array": [
                {
                    "account_code": "1201005700",
                    "account_code_description": "DEDUCT- COMMISSION PAID TO OUTSOURCED AGENTS FOR S",
                    "receipt": 28000,
                    "payment": 0
                }
            ]
        }
]`
	// body, _ := json.Marshal(input)
	req := httptest.NewRequest("POST", "/v1/pao-gen/cashbook/verifications", bytes.NewBuffer([]byte(input)))
	req.Header.Set("Content-Type", "application/json")
	rec := httptest.NewRecorder()
	Router.Engine.ServeHTTP(rec, req)
	assert.Equal(t, http.StatusCreated, rec.Code)
}

func TestCreatePFMSVerificationHandler_JSONBindingError(t *testing.T) {
	input := `[
        {
            "ddo_code": "102604",
            "business_date": "2024-08-21T00:00:00Z",
            "closing_bal": "21000",
            "opening_bal": 11000,
            "verified_by": 10257696,
            "h_verification": "true",
            "hoa": "120100101200400",
            "payment": 0,
            "receipt": 28000,
            "account_array": [
                {
                    "account_code": "1201005700",
                    "account_code_description": "DEDUCT- COMMISSION PAID TO OUTSOURCED AGENTS FOR S",
                    "receipt": 28000,
                    "payment": 0
                }
            ]
        }
]`
	// body, _ := json.Marshal(input)
	req := httptest.NewRequest("POST", "/v1/pao-gen/cashbook/verifications", bytes.NewBuffer([]byte(input)))
	req.Header.Set("Content-Type", "application/json")
	rec := httptest.NewRecorder()
	Router.Engine.ServeHTTP(rec, req)

	assert.Equal(t, http.StatusBadRequest, rec.Code)
}

func TestCreatePFMSVerificationHandler_ValidationError(t *testing.T) {
	input := `[
        {
            "ddo_code": "abc604",
            "business_date": "2024-08-21T00:00:00Z",
            "closing_bal": 21000,
            "opening_bal": 11000,
            "verified_by": 10257696,
            "h_verification": "true",
            "hoa": "120100101200400",
            "payment": 0,
            "receipt": 28000,
            "account_array": [
                {
                    "account_code": "1201005700",
                    "account_code_description": "DEDUCT- COMMISSION PAID TO OUTSOURCED AGENTS FOR S",
                    "receipt": 28000,
                    "payment": 0
                }
            ]
        }
]`
	// body, _ := json.Marshal(input)
	req := httptest.NewRequest("POST", "/v1/pao-gen/cashbook/verifications", bytes.NewBuffer([]byte(input)))
	req.Header.Set("Content-Type", "application/json")
	rec := httptest.NewRecorder()
	Router.Engine.ServeHTTP(rec, req)

	assert.Equal(t, http.StatusUnprocessableEntity, rec.Code)
}
func TestCreatePFMSVerificationHandler_AlreadyVerified(t *testing.T) {
	input := `[
        {
            "ddo_code": "102595",
            "business_date": "2024-07-26T00:00:00Z",
            "closing_bal": 21000,
            "opening_bal": 11000,
            "verified_by": 10257696,
            "h_verification": "true",
            "hoa": "120100101100100",
            "payment": 0,
            "receipt": 28000,
            "account_array": [
                {
                    "account_code": "1201005700",
                    "account_code_description": "DEDUCT- COMMISSION PAID TO OUTSOURCED AGENTS FOR S",
                    "receipt": 28000,
                    "payment": 0
                }
            ]
        }
]`
	// body, _ := json.Marshal(input)
	req := httptest.NewRequest("POST", "/v1/pao-gen/cashbook/verifications", bytes.NewBuffer([]byte(input)))
	req.Header.Set("Content-Type", "application/json")
	rec := httptest.NewRecorder()
	Router.Engine.ServeHTTP(rec, req)
	assert.Equal(t, http.StatusConflict, rec.Code)
}
func TestCreatePFMSVerificationHandler_CashbookNotReceived(t *testing.T) {
	input := `[
        {
            "ddo_code": "103333",
            "business_date": "2024-07-26T00:00:00Z",
            "closing_bal": 21000,
            "opening_bal": 11000,
            "verified_by": 10257696,
            "h_verification": "true",
            "hoa": "120100101100100",
            "payment": 0,
            "receipt": 28000,
            "account_array": [
                {
                    "account_code": "1201005700",
                    "account_code_description": "DEDUCT- COMMISSION PAID TO OUTSOURCED AGENTS FOR S",
                    "receipt": 28000,
                    "payment": 0
                }
            ]
        }
]`
	// body, _ := json.Marshal(input)
	req := httptest.NewRequest("POST", "/v1/pao-gen/cashbook/verifications", bytes.NewBuffer([]byte(input)))
	req.Header.Set("Content-Type", "application/json")
	rec := httptest.NewRecorder()
	Router.Engine.ServeHTTP(rec, req)
	assert.Equal(t, http.StatusConflict, rec.Code)
}

//ListPfmsPendingHandler-------------------------------------------------------------------------------------

func TestListPfmsPendingHandler_Success(t *testing.T) {
	// Router = BootstrapTestApp(t)

	req := httptest.NewRequest("GET", "/v1/pao-gen/pao/078109/cashbook/verification-pending?from-date=2023-01-01&to-date=2024-12-01&skip=0&limit=0", nil)
	req.Header.Set("Content-Type", "application/json")
	rec := httptest.NewRecorder()
	Router.Engine.ServeHTTP(rec, req)

	assert.Equal(t, http.StatusOK, rec.Code)
}
func TestListPfmsPendingHandler_UriBindingError(t *testing.T) {

	req := httptest.NewRequest("GET", "/v1/pao-gen/pao/078109'/cashbook/verification-pending?from-date=2024-01-01&to-date=2024-12-01", nil)
	req.Header.Set("Content-Type", "application/json")
	rec := httptest.NewRecorder()
	Router.Engine.ServeHTTP(rec, req)

	assert.Equal(t, http.StatusBadRequest, rec.Code)
}
func TestListPfmsPendingHandler_QueryBindingError(t *testing.T) {

	req := httptest.NewRequest("GET", "/v1/pao-gen/pao/078109/cashbook/verification-pending?from-date=20-01-01&to-date=2024-12-01", nil)
	req.Header.Set("Content-Type", "application/json")
	rec := httptest.NewRecorder()
	Router.Engine.ServeHTTP(rec, req)

	assert.Equal(t, http.StatusBadRequest, rec.Code)
}

func TestListPfmsPendingHandler_ValidationError(t *testing.T) {

	req := httptest.NewRequest("GET", "/v1/pao-gen/pao/078109/cashbook/verification-pending?from-date=2024-01-01&to-date=20-12-01", nil)
	req.Header.Set("Content-Type", "application/json")
	rec := httptest.NewRecorder()
	Router.Engine.ServeHTTP(rec, req)

	assert.Equal(t, http.StatusUnprocessableEntity, rec.Code)
}

//ListDdoPfmsMonthlyHandler-----------------------------------------------------------------------------------------

func TestListDdoPfmsMonthlyHandler_Success(t *testing.T) {
	// Router = BootstrapTestApp(t)

	req := httptest.NewRequest("GET", "/v1/pao-gen/pao/078109/cashaccount/ddo-lists?period=072024&skip=0&limit=0", nil)
	req.Header.Set("Content-Type", "application/json")
	rec := httptest.NewRecorder()
	Router.Engine.ServeHTTP(rec, req)

	assert.Equal(t, http.StatusOK, rec.Code)
}
func TestListDdoPfmsMonthlyHandler_UriBindingError(t *testing.T) {

	req := httptest.NewRequest("GET", "/v1/pao-gen/pao/078109'/cashaccount/ddo-lists?period=072024", nil)
	req.Header.Set("Content-Type", "application/json")
	rec := httptest.NewRecorder()
	Router.Engine.ServeHTTP(rec, req)

	assert.Equal(t, http.StatusBadRequest, rec.Code)
}
func TestListDdoPfmsMonthlyHandler_QueryBindingError(t *testing.T) {

	req := httptest.NewRequest("GET", "/v1/pao-gen/pao/078109/cashaccount/ddo-lists?period=07202", nil)
	req.Header.Set("Content-Type", "application/json")
	rec := httptest.NewRecorder()
	Router.Engine.ServeHTTP(rec, req)

	assert.Equal(t, http.StatusBadRequest, rec.Code)
}

func TestListDdoPfmsMonthlyHandler_ValidationError(t *testing.T) {

	req := httptest.NewRequest("GET", "/v1/pao-gen/pao/078ad9/cashaccount/ddo-lists?period=072024", nil)
	req.Header.Set("Content-Type", "application/json")
	rec := httptest.NewRecorder()
	Router.Engine.ServeHTTP(rec, req)

	assert.Equal(t, http.StatusUnprocessableEntity, rec.Code)
}

//UpdateDdoMonthlyHandler----------------------------------------------------------------------------------------------

func TestUpdateDdoMonthlyHandler_Success(t *testing.T) {
	// body, _ := json.Marshal(input)
	req := httptest.NewRequest("PUT", "/v1/pao-gen/pao/078109/cashaccount/ddo-lists?period=072024", nil)
	req.Header.Set("Content-Type", "application/json")
	rec := httptest.NewRecorder()
	Router.Engine.ServeHTTP(rec, req)

	assert.Equal(t, http.StatusOK, rec.Code)
}

func TestUpdateDdoMonthlyHandler_UriBindingError(t *testing.T) {

	req := httptest.NewRequest("PUT", "/v1/pao-gen/pao/078109'/cashaccount/ddo-lists?period=072024", nil)
	req.Header.Set("Content-Type", "application/json")
	rec := httptest.NewRecorder()
	Router.Engine.ServeHTTP(rec, req)

	assert.Equal(t, http.StatusBadRequest, rec.Code)
}
func TestUpdateDdoMonthlyHandler_QueryBindingError(t *testing.T) {

	req := httptest.NewRequest("PUT", "/v1/pao-gen/pao/078109/cashaccount/ddo-lists?period=07202", nil)
	req.Header.Set("Content-Type", "application/json")
	rec := httptest.NewRecorder()
	Router.Engine.ServeHTTP(rec, req)

	assert.Equal(t, http.StatusBadRequest, rec.Code)
}

func TestUpdateDdoMonthlyHandler_ValidationError(t *testing.T) {
	req := httptest.NewRequest("PUT", "/v1/pao-gen/pao/0ad109/cashaccount/ddo-lists?period=072024", nil)
	req.Header.Set("Content-Type", "application/json")
	rec := httptest.NewRecorder()
	Router.Engine.ServeHTTP(rec, req)
	assert.Equal(t, http.StatusUnprocessableEntity, rec.Code)
}

//FetchDdoMonthlyDetailHandler-----------------------------------------------------------------------------------

func TestFetchDdoMonthlyDetailHandler_Success(t *testing.T) {
	// Router = BootstrapTestApp(t)

	req := httptest.NewRequest("GET", "/v1/pao-gen/ddo/102595/cashaccount/ddo-details?period=082024&skip=0&limit=0", nil)
	req.Header.Set("Content-Type", "application/json")
	rec := httptest.NewRecorder()
	Router.Engine.ServeHTTP(rec, req)

	assert.Equal(t, http.StatusOK, rec.Code)
}
func TestFetchDdoMonthlyDetailHandler_UriBindingError(t *testing.T) {

	req := httptest.NewRequest("GET", "/v1/pao-gen/ddo/102595'/cashaccount/ddo-details?period=072024", nil)
	req.Header.Set("Content-Type", "application/json")
	rec := httptest.NewRecorder()
	Router.Engine.ServeHTTP(rec, req)

	assert.Equal(t, http.StatusBadRequest, rec.Code)
}
func TestFetchDdoMonthlyDetailHandler_QueryBindingError(t *testing.T) {

	req := httptest.NewRequest("GET", "/v1/pao-gen/ddo/102595/cashaccount/ddo-details?period=07202", nil)
	req.Header.Set("Content-Type", "application/json")
	rec := httptest.NewRecorder()
	Router.Engine.ServeHTTP(rec, req)

	assert.Equal(t, http.StatusBadRequest, rec.Code)
}

func TestFetchDdoMonthlyDetailHandler_ValidationError(t *testing.T) {

	req := httptest.NewRequest("GET", "/v1/pao-gen/ddo/10qr95/cashaccount/ddo-details?period=072028", nil)
	req.Header.Set("Content-Type", "application/json")
	rec := httptest.NewRecorder()
	Router.Engine.ServeHTTP(rec, req)

	assert.Equal(t, http.StatusUnprocessableEntity, rec.Code)
}

//CreatePfmsMonthlyVerifiedHandler-----------------------------------------------------------------------------------

func TestCreatePfmsMonthlyVerifiedHandler_Success(t *testing.T) {
	input := `[
        {
            "ddo_code": "102606",
            "period": "092024",
            "closing_bal": 35600000,
            "opening_bal": 25600000,
            "verified_by": 10257696,
            "h_verification": "true",
            "hoa": "120100800130100",
            "payment": 118000,
            "receipt": 2000000,
            "te_payment": 0,
            "te_receipt": 0,
            "account_array": [
                {
                    "account_code": "1201018400",
                    "account_code_description": "Logistic Post - Transp. charges-FTL (Surafce)",
                    "receipt": 0,
                    "payment": 118000
                },
                {
                    "account_code": "1201019900",
                    "account_code_description": "Advance for Logistic Mails Service",
                    "receipt": 2000000,
                    "payment": 0
                }
            ]
        }
]`
	// body, _ := json.Marshal(input)
	req := httptest.NewRequest("POST", "/v1/pao-gen/cashaccount/verifications", bytes.NewBuffer([]byte(input)))
	req.Header.Set("Content-Type", "application/json")
	rec := httptest.NewRecorder()
	Router.Engine.ServeHTTP(rec, req)
	assert.Equal(t, http.StatusCreated, rec.Code)
}
func TestCreatePfmsMonthlyVerifiedHandlerBroadsheet_Success(t *testing.T) {
	input := `[
        {
            "ddo_code": "102606",
            "period": "092024",
            "closing_bal": 35600000,
            "opening_bal": 25600000,
            "verified_by": 10257696,
            "h_verification": "true",
            "hoa": "866100800130100",
            "payment": 118000,
            "receipt": 2000000,
            "te_payment": 0,
            "te_receipt": 0,
            "account_array": [
                {
                    "account_code": "1201018400",
                    "account_code_description": "Logistic Post - Transp. charges-FTL (Surafce)",
                    "receipt": 0,
                    "payment": 118000
                },
                {
                    "account_code": "1201019900",
                    "account_code_description": "Advance for Logistic Mails Service",
                    "receipt": 2000000,
                    "payment": 0
                }
            ]
        },
		{
            "ddo_code": "102606",
            "period": "092024",
            "closing_bal": 35600000,
            "opening_bal": 25600000,
            "verified_by": 10257696,
            "h_verification": "true",
            "hoa": "800100800130100",
            "payment": 118000,
            "receipt": 2000000,
            "te_payment": 0,
            "te_receipt": 0,
            "account_array": [
                {
                    "account_code": "1201018400",
                    "account_code_description": "Logistic Post - Transp. charges-FTL (Surafce)",
                    "receipt": 0,
                    "payment": 118000
                },
                {
                    "account_code": "1201019900",
                    "account_code_description": "Advance for Logistic Mails Service",
                    "receipt": 2000000,
                    "payment": 0
                }
            ]
        },
		{
            "ddo_code": "102606",
            "period": "092024",
            "closing_bal": 35600000,
            "opening_bal": 25600000,
            "verified_by": 10257696,
            "h_verification": "true",
            "hoa": "800200800130100",
            "payment": 118000,
            "receipt": 2000000,
            "te_payment": 0,
            "te_receipt": 0,
            "account_array": [
                {
                    "account_code": "1201018400",
                    "account_code_description": "Logistic Post - Transp. charges-FTL (Surafce)",
                    "receipt": 0,
                    "payment": 118000
                },
                {
                    "account_code": "1201019900",
                    "account_code_description": "Advance for Logistic Mails Service",
                    "receipt": 2000000,
                    "payment": 0
                }
            ]
        },
		{
            "ddo_code": "102606",
            "period": "092024",
            "closing_bal": 35600000,
            "opening_bal": 25600000,
            "verified_by": 10257696,
            "h_verification": "true",
            "hoa": "855300800130100",
            "payment": 118000,
            "receipt": 2000000,
            "te_payment": 0,
            "te_receipt": 0,
            "account_array": [
                {
                    "account_code": "1201018400",
                    "account_code_description": "Logistic Post - Transp. charges-FTL (Surafce)",
                    "receipt": 0,
                    "payment": 118000
                },
                {
                    "account_code": "1201019900",
                    "account_code_description": "Advance for Logistic Mails Service",
                    "receipt": 2000000,
                    "payment": 0
                }
            ]
        },
		{
            "ddo_code": "102606",
            "period": "092024",
            "closing_bal": 35600000,
            "opening_bal": 25600000,
            "verified_by": 10257696,
            "h_verification": "true",
            "hoa": "867700800130100",
            "payment": 118000,
            "receipt": 2000000,
            "te_payment": 0,
            "te_receipt": 0,
            "account_array": [
                {
                    "account_code": "1201018400",
                    "account_code_description": "Logistic Post - Transp. charges-FTL (Surafce)",
                    "receipt": 0,
                    "payment": 118000
                },
                {
                    "account_code": "1201019900",
                    "account_code_description": "Advance for Logistic Mails Service",
                    "receipt": 2000000,
                    "payment": 0
                }
            ]
        },
		{
            "ddo_code": "102606",
            "period": "092024",
            "closing_bal": 35600000,
            "opening_bal": 25600000,
            "verified_by": 10257696,
            "h_verification": "true",
            "hoa": "867000800130100",
            "payment": 118000,
            "receipt": 2000000,
            "te_payment": 0,
            "te_receipt": 0,
            "account_array": [
                {
                    "account_code": "1201018400",
                    "account_code_description": "Logistic Post - Transp. charges-FTL (Surafce)",
                    "receipt": 0,
                    "payment": 118000
                },
                {
                    "account_code": "1201019900",
                    "account_code_description": "Advance for Logistic Mails Service",
                    "receipt": 2000000,
                    "payment": 0
                }
            ]
        }
]`
	// body, _ := json.Marshal(input)
	req := httptest.NewRequest("POST", "/v1/pao-gen/cashaccount/verifications", bytes.NewBuffer([]byte(input)))
	req.Header.Set("Content-Type", "application/json")
	rec := httptest.NewRecorder()
	Router.Engine.ServeHTTP(rec, req)
	assert.Equal(t, http.StatusCreated, rec.Code)
}
func TestCreatePfmsMonthlyVerifiedHandler_JSONBindingError(t *testing.T) {
	input := `[
        {
            "ddo_code": "102606",
            "period": "082024",
            "closing_bal": 35600000,
            "opening_bal": "25600000",
            "verified_by": 10257696,
            "h_verification": "true",
            "hoa": "866100800130100",
            "payment": 118000,
            "receipt": 2000000,
            "te_payment": 0,
            "te_receipt": 0,
            "account_array": [
                {
                    "account_code": "1201018400",
                    "account_code_description": "Logistic Post - Transp. charges-FTL (Surafce)",
                    "receipt": 0,
                    "payment": 118000
                },
                {
                    "account_code": "1201019900",
                    "account_code_description": "Advance for Logistic Mails Service",
                    "receipt": 2000000,
                    "payment": 0
                }
            ]
        }
]`
	// body, _ := json.Marshal(input)
	req := httptest.NewRequest("POST", "/v1/pao-gen/cashaccount/verifications", bytes.NewBuffer([]byte(input)))
	req.Header.Set("Content-Type", "application/json")
	rec := httptest.NewRecorder()
	Router.Engine.ServeHTTP(rec, req)

	assert.Equal(t, http.StatusBadRequest, rec.Code)
}

func TestCreatePfmsMonthlyVerifiedHandler_ValidationError(t *testing.T) {
	input := `[
        {
            "ddo_code": "10ar06",
            "period": "082024",
            "closing_bal": 35600000,
            "opening_bal": 25600000,
            "verified_by": 10257696,
            "h_verification": "true",
            "hoa": "120100800130100",
            "payment": 118000,
            "receipt": 2000000,
            "te_payment": 0,
            "te_receipt": 0,
            "account_array": [
                {
                    "account_code": "1201018400",
                    "account_code_description": "Logistic Post - Transp. charges-FTL (Surafce)",
                    "receipt": 0,
                    "payment": 118000
                },
                {
                    "account_code": "1201019900",
                    "account_code_description": "Advance for Logistic Mails Service",
                    "receipt": 2000000,
                    "payment": 0
                }
            ]
        }
]`
	// body, _ := json.Marshal(input)
	req := httptest.NewRequest("POST", "/v1/pao-gen/cashaccount/verifications", bytes.NewBuffer([]byte(input)))
	req.Header.Set("Content-Type", "application/json")
	rec := httptest.NewRecorder()
	Router.Engine.ServeHTTP(rec, req)

	assert.Equal(t, http.StatusUnprocessableEntity, rec.Code)
}
func TestCreatePfmsMonthlyVerifiedHandler_AlreadyVerified(t *testing.T) {
	input := `[
        {
            "ddo_code": "102595",
            "period": "082024",
            "closing_bal": 35600000,
            "opening_bal": 25600000,
            "verified_by": 10257696,
            "h_verification": "true",
            "hoa": "120100800130100",
            "payment": 118000,
            "receipt": 2000000,
            "te_payment": 0,
            "te_receipt": 0,
            "account_array": [
                {
                    "account_code": "1201018400",
                    "account_code_description": "Logistic Post - Transp. charges-FTL (Surafce)",
                    "receipt": 0,
                    "payment": 118000
                },
                {
                    "account_code": "1201019900",
                    "account_code_description": "Advance for Logistic Mails Service",
                    "receipt": 2000000,
                    "payment": 0
                }
            ]
        }
]`
	// body, _ := json.Marshal(input)
	req := httptest.NewRequest("POST", "/v1/pao-gen/cashaccount/verifications", bytes.NewBuffer([]byte(input)))
	req.Header.Set("Content-Type", "application/json")
	rec := httptest.NewRecorder()
	Router.Engine.ServeHTTP(rec, req)
	assert.Equal(t, http.StatusConflict, rec.Code)
}
func TestCreatePfmsMonthlyVerifiedHandler_CashbookNotReceived(t *testing.T) {
	input := `[
        {
            "ddo_code": "102589",
            "period": "082024",
            "closing_bal": 35600000,
            "opening_bal": 25600000,
            "verified_by": 10257696,
            "h_verification": "true",
            "hoa": "120100800130100",
            "payment": 118000,
            "receipt": 2000000,
            "te_payment": 0,
            "te_receipt": 0,
            "account_array": [
                {
                    "account_code": "1201018400",
                    "account_code_description": "Logistic Post - Transp. charges-FTL (Surafce)",
                    "receipt": 0,
                    "payment": 118000
                },
                {
                    "account_code": "1201019900",
                    "account_code_description": "Advance for Logistic Mails Service",
                    "receipt": 2000000,
                    "payment": 0
                }
            ]
        }
]`
	// body, _ := json.Marshal(input)
	req := httptest.NewRequest("POST", "/v1/pao-gen/cashaccount/verifications", bytes.NewBuffer([]byte(input)))
	req.Header.Set("Content-Type", "application/json")
	rec := httptest.NewRecorder()
	Router.Engine.ServeHTTP(rec, req)
	assert.Equal(t, http.StatusConflict, rec.Code)
}

//ListPfmsXmlHandler--------------------------------------------------------------------------------------------------

func TestListPfmsXmlHandler_Success(t *testing.T) {
	// Router = BootstrapTestApp(t)

	req := httptest.NewRequest("GET", "/v1/pao-gen/pao/078109/xml-generation-status?type=2&from-date=&to-date=&uniqueIdentifier=TE-07810920240606114555&skip=0&limit=0", nil)
	req.Header.Set("Content-Type", "application/json")
	rec := httptest.NewRecorder()
	Router.Engine.ServeHTTP(rec, req)

	assert.Equal(t, http.StatusOK, rec.Code)
}
func TestListPfmsXmlHandler2_Success(t *testing.T) {
	// Router = BootstrapTestApp(t)

	req := httptest.NewRequest("GET", "/v1/pao-gen/pao/078109/xml-generation-status?type=1&from-date=2024-01-01&to-date=2024-12-30&uniqueIdentifier=TE-07810920240606114555", nil)
	req.Header.Set("Content-Type", "application/json")
	rec := httptest.NewRecorder()
	Router.Engine.ServeHTTP(rec, req)

	assert.Equal(t, http.StatusOK, rec.Code)
}
func TestListPfmsXmlHandler_UriBindingError(t *testing.T) {

	req := httptest.NewRequest("GET", "/v1/pao-gen/pao/078109'/xml-generation-status?type=2&from-date=&to-date=&uniqueIdentifier=TE-07810920240606114555", nil)
	req.Header.Set("Content-Type", "application/json")
	rec := httptest.NewRecorder()
	Router.Engine.ServeHTTP(rec, req)

	assert.Equal(t, http.StatusBadRequest, rec.Code)
}
func TestListPfmsXmlHandler_QueryBindingError(t *testing.T) {

	req := httptest.NewRequest("GET", "/v1/pao-gen/pao/078109/xml-generation-status?type=b&from-date=&to-date=&uniqueIdentifier=TE-07810920240606114555", nil)
	req.Header.Set("Content-Type", "application/json")
	rec := httptest.NewRecorder()
	Router.Engine.ServeHTTP(rec, req)

	assert.Equal(t, http.StatusBadRequest, rec.Code)
}

func TestListPfmsXmlHandler_ValidationError(t *testing.T) {

	req := httptest.NewRequest("GET", "/v1/pao-gen/pao/078109/xml-generation-status?type=5&from-date=&to-date=&uniqueIdentifier=TE-07810920240606114555", nil)
	req.Header.Set("Content-Type", "application/json")
	rec := httptest.NewRecorder()
	Router.Engine.ServeHTTP(rec, req)

	assert.Equal(t, http.StatusUnprocessableEntity, rec.Code)
}

//ListPfmsXmlTeHandler----------------------------------------------------------------------------------------------------------------

func TestListPfmsXmlTeHandler_Success(t *testing.T) {
	// Router = BootstrapTestApp(t)

	req := httptest.NewRequest("GET", "/v1/pao-gen/pao/078109/te-xml-generation-status?type=2&from-date=&to-date=&uniqueIdentifier=TE-07810920240703121359&skip=0&limit=0", nil)
	req.Header.Set("Content-Type", "application/json")
	rec := httptest.NewRecorder()
	Router.Engine.ServeHTTP(rec, req)

	assert.Equal(t, http.StatusOK, rec.Code)
}
func TestListPfmsXmlTeHandler2_Success(t *testing.T) {
	// Router = BootstrapTestApp(t)

	req := httptest.NewRequest("GET", "/v1/pao-gen/pao/078109/te-xml-generation-status?type=1&from-date=2024-01-01&to-date=2024-12-30&uniqueIdentifier=TE-07810920240703121359", nil)
	req.Header.Set("Content-Type", "application/json")
	rec := httptest.NewRecorder()
	Router.Engine.ServeHTTP(rec, req)

	assert.Equal(t, http.StatusOK, rec.Code)
}
func TestListPfmsXmlTeHandler_UriBindingError(t *testing.T) {

	req := httptest.NewRequest("GET", "/v1/pao-gen/pao/078109'/te-xml-generation-status?type=2&from-date=&to-date=&uniqueIdentifier=TE-07810920240703121359", nil)
	req.Header.Set("Content-Type", "application/json")
	rec := httptest.NewRecorder()
	Router.Engine.ServeHTTP(rec, req)

	assert.Equal(t, http.StatusBadRequest, rec.Code)
}
func TestListPfmsXmlTeHandler_QueryBindingError(t *testing.T) {

	req := httptest.NewRequest("GET", "/v1/pao-gen/pao/078109/te-xml-generation-status?type=p&from-date=&to-date=&uniqueIdentifier=TE-07810920240703121359", nil)
	req.Header.Set("Content-Type", "application/json")
	rec := httptest.NewRecorder()
	Router.Engine.ServeHTTP(rec, req)

	assert.Equal(t, http.StatusBadRequest, rec.Code)
}

func TestListPfmsXmlTeHandler_ValidationError(t *testing.T) {

	req := httptest.NewRequest("GET", "/v1/pao-gen/pao/078109/te-xml-generation-status?type=9&from-date=&to-date=&uniqueIdentifier=TE-07810920240703121359", nil)
	req.Header.Set("Content-Type", "application/json")
	rec := httptest.NewRecorder()
	Router.Engine.ServeHTTP(rec, req)

	assert.Equal(t, http.StatusUnprocessableEntity, rec.Code)
}

//FetchPfmsHandler--------------------------------------------------------------------------------------------------

// func TestFetchPfmsHandler_Success(t *testing.T) {
// 	input := `[
//   {"ddo_code": "102595", "cb_date": "2024-11-21","pao_code":"078109","fin_year":"2024"}
// ]`
// 	// body, _ := json.Marshal(input)
// 	req := httptest.NewRequest("POST", "/v1/pao-gen/xml-generation", bytes.NewBuffer([]byte(input)))
// 	req.Header.Set("Content-Type", "application/json")
// 	rec := httptest.NewRecorder()
// 	Router.Engine.ServeHTTP(rec, req)
// 	assert.Equal(t, http.StatusOK, rec.Code)
// }

// func TestFetchPfmsHandler_JSONBindingError(t *testing.T) {
// 	input := `[
//   {"ddo_code": "102595'", "cb_date": "2024-11-21","pao_code":"078109","fin_year":"2024"}
// ]`
// 	// body, _ := json.Marshal(input)
// 	req := httptest.NewRequest("POST", "/v1/pao-gen/xml-generation", bytes.NewBuffer([]byte(input)))
// 	req.Header.Set("Content-Type", "application/json")
// 	rec := httptest.NewRecorder()
// 	Router.Engine.ServeHTTP(rec, req)

// 	assert.Equal(t, http.StatusBadRequest, rec.Code)
// }

// func TestFetchPfmsHandler_ValidationError(t *testing.T) {
// 	input := `[
//   {"ddo_code": "10aj95", "cb_date": "2024-11-21","pao_code":"078109","fin_year":"2024"}
// ]`
// 	// body, _ := json.Marshal(input)
// 	req := httptest.NewRequest("POST", "/v1/pao-gen/xml-generation", bytes.NewBuffer([]byte(input)))
// 	req.Header.Set("Content-Type", "application/json")
// 	rec := httptest.NewRecorder()
// 	Router.Engine.ServeHTTP(rec, req)

// 	assert.Equal(t, http.StatusUnprocessableEntity, rec.Code)
// }

// //CreatePraoAccountHandler-----------------------------------------------------------------------

// func TestCreatePraoAccountHandler_Success(t *testing.T) {
// 	input := `{
//     "pao_code": "078109",
//     "period": "082024"
// }`
// 	// body, _ := json.Marshal(input)
// 	req := httptest.NewRequest("POST", "/v1/pao-gen/prao/account-submission", bytes.NewBuffer([]byte(input)))
// 	req.Header.Set("Content-Type", "application/json")
// 	rec := httptest.NewRecorder()
// 	Router.Engine.ServeHTTP(rec, req)
// 	assert.Equal(t, http.StatusCreated, rec.Code)
// }

// func TestCreatePraoAccountHandler_JSONBindingError(t *testing.T) {
// 	input := `{
//     "pao_code": "078109'",
//     "period": "072024"
// }`
// 	// body, _ := json.Marshal(input)
// 	req := httptest.NewRequest("POST", "/v1/pao-gen/prao/account-submission", bytes.NewBuffer([]byte(input)))
// 	req.Header.Set("Content-Type", "application/json")
// 	rec := httptest.NewRecorder()
// 	Router.Engine.ServeHTTP(rec, req)

// 	assert.Equal(t, http.StatusBadRequest, rec.Code)
// }

// func TestCreatePraoAccountHandler_ValidationError(t *testing.T) {
// 	input := `{
//     "pao_code": "0781yi",
//     "period": "072024"
// }`
// 	// body, _ := json.Marshal(input)
// 	req := httptest.NewRequest("POST", "/v1/pao-gen/prao/account-submission", bytes.NewBuffer([]byte(input)))
// 	req.Header.Set("Content-Type", "application/json")
// 	rec := httptest.NewRecorder()
// 	Router.Engine.ServeHTTP(rec, req)

// 	assert.Equal(t, http.StatusUnprocessableEntity, rec.Code)
// }
// func TestCreatePraoAccountHandler_AlreadyVerified(t *testing.T) {
// 	input := `{
//     "pao_code": "078109",
//     "period": "072024"
// }`
// 	// body, _ := json.Marshal(input)
// 	req := httptest.NewRequest("POST", "/v1/pao-gen/prao/account-submission", bytes.NewBuffer([]byte(input)))
// 	req.Header.Set("Content-Type", "application/json")
// 	rec := httptest.NewRecorder()
// 	Router.Engine.ServeHTTP(rec, req)
// 	assert.Equal(t, http.StatusConflict, rec.Code)
// }

// //FetchPraoAccountHandler-------------------------------------------------------------------------------------

// func TestFetchPraoAccountHandler_Success(t *testing.T) {
// 	// Router = BootstrapTestApp(t)

// 	req := httptest.NewRequest("GET", "/v1/pao-gen/pao/078109/prao/accounts?period=072024&skip=0&limit=0", nil)
// 	req.Header.Set("Content-Type", "application/json")
// 	rec := httptest.NewRecorder()
// 	Router.Engine.ServeHTTP(rec, req)

// 	assert.Equal(t, http.StatusOK, rec.Code)
// }
// func TestFetchPraoAccountHandler_UriBindingError(t *testing.T) {

// 	req := httptest.NewRequest("GET", "/v1/pao-gen/pao/078109'/prao/accounts?period=032024", nil)
// 	req.Header.Set("Content-Type", "application/json")
// 	rec := httptest.NewRecorder()
// 	Router.Engine.ServeHTTP(rec, req)

// 	assert.Equal(t, http.StatusBadRequest, rec.Code)
// }
// func TestFetchPraoAccountHandler_QueryBindingError(t *testing.T) {

// 	req := httptest.NewRequest("GET", "/v1/pao-gen/pao/078109/prao/accounts?period=0320", nil)
// 	req.Header.Set("Content-Type", "application/json")
// 	rec := httptest.NewRecorder()
// 	Router.Engine.ServeHTTP(rec, req)

// 	assert.Equal(t, http.StatusBadRequest, rec.Code)
// }

// func TestFetchPraoAccountHandler_ValidationError(t *testing.T) {

// 	req := httptest.NewRequest("GET", "/v1/pao-gen/pao/07ty09/prao/accounts?period=032024", nil)
// 	req.Header.Set("Content-Type", "application/json")
// 	rec := httptest.NewRecorder()
// 	Router.Engine.ServeHTTP(rec, req)

// 	assert.Equal(t, http.StatusUnprocessableEntity, rec.Code)
// }

//FetchPraoAccountSubStatusHandler--------------------------------------------------------------------------------------

func TestFetchPraoAccountSubStatusHandler_Success(t *testing.T) {
	// Router = BootstrapTestApp(t)

	req := httptest.NewRequest("GET", "/v1/pao-gen/pao/078109/prao/account-submission-status?period=032024&skip=0&limit=0", nil)
	req.Header.Set("Content-Type", "application/json")
	rec := httptest.NewRecorder()
	Router.Engine.ServeHTTP(rec, req)

	assert.Equal(t, http.StatusOK, rec.Code)
}
func TestFetchPraoAccountSubStatusHandler_UriBindingError(t *testing.T) {

	req := httptest.NewRequest("GET", "/v1/pao-gen/pao/078109'/prao/account-submission-status?period=032024", nil)
	req.Header.Set("Content-Type", "application/json")
	rec := httptest.NewRecorder()
	Router.Engine.ServeHTTP(rec, req)

	assert.Equal(t, http.StatusBadRequest, rec.Code)
}
func TestFetchPraoAccountSubStatusHandler_QueryBindingError(t *testing.T) {

	req := httptest.NewRequest("GET", "/v1/pao-gen/pao/078109/prao/account-submission-status?period=03202", nil)
	req.Header.Set("Content-Type", "application/json")
	rec := httptest.NewRecorder()
	Router.Engine.ServeHTTP(rec, req)

	assert.Equal(t, http.StatusBadRequest, rec.Code)
}

func TestFetchPraoAccountSubStatusHandler_ValidationError(t *testing.T) {

	req := httptest.NewRequest("GET", "/v1/pao-gen/pao/07ok09/prao/account-submission-status?period=032024", nil)
	req.Header.Set("Content-Type", "application/json")
	rec := httptest.NewRecorder()
	Router.Engine.ServeHTTP(rec, req)

	assert.Equal(t, http.StatusUnprocessableEntity, rec.Code)
}

//ListPraoAccountSubStatusHandler----------------------------------------------------------------------------------

func TestListPraoAccountSubStatusHandler_Success(t *testing.T) {
	// Router = BootstrapTestApp(t)

	req := httptest.NewRequest("GET", "/v1/pao-gen/prao/823940734/account-submission-list?period=042024&skip=0&limit=0", nil)
	req.Header.Set("Content-Type", "application/json")
	rec := httptest.NewRecorder()
	Router.Engine.ServeHTTP(rec, req)

	assert.Equal(t, http.StatusOK, rec.Code)
}
func TestListPraoAccountSubStatusHandler_UriBindingError(t *testing.T) {

	req := httptest.NewRequest("GET", "/v1/pao-gen/prao/823940734'/account-submission-list?period=042024", nil)
	req.Header.Set("Content-Type", "application/json")
	rec := httptest.NewRecorder()
	Router.Engine.ServeHTTP(rec, req)

	assert.Equal(t, http.StatusBadRequest, rec.Code)
}
func TestListPraoAccountSubStatusHandler_QueryBindingError(t *testing.T) {

	req := httptest.NewRequest("GET", "/v1/pao-gen/prao/823940734/account-submission-list?period=0420245", nil)
	req.Header.Set("Content-Type", "application/json")
	rec := httptest.NewRecorder()
	Router.Engine.ServeHTTP(rec, req)

	assert.Equal(t, http.StatusBadRequest, rec.Code)
}

func TestListPraoAccountSubStatusHandler_ValidationError(t *testing.T) {

	req := httptest.NewRequest("GET", "/v1/pao-gen/prao/823940734/account-submission-list?period=04af24", nil)
	req.Header.Set("Content-Type", "application/json")
	rec := httptest.NewRecorder()
	Router.Engine.ServeHTTP(rec, req)

	assert.Equal(t, http.StatusUnprocessableEntity, rec.Code)
}

//ListTaxDetailHandler-----------------------------------------------------------------------------------

func TestListTaxDetailHandler_Success(t *testing.T) {
	// Router = BootstrapTestApp(t)

	req := httptest.NewRequest("GET", "/v1/pao-gen/ddo/102555/tax-details?period=032024&type=3&skip=0&limit=0", nil)
	req.Header.Set("Content-Type", "application/json")
	rec := httptest.NewRecorder()
	Router.Engine.ServeHTTP(rec, req)

	assert.Equal(t, http.StatusOK, rec.Code)
}
func TestListTaxDetailHandler_UriBindingError(t *testing.T) {

	req := httptest.NewRequest("GET", "/v1/pao-gen/ddo/102555'/tax-details?period=032024&type=3", nil)
	req.Header.Set("Content-Type", "application/json")
	rec := httptest.NewRecorder()
	Router.Engine.ServeHTTP(rec, req)

	assert.Equal(t, http.StatusBadRequest, rec.Code)
}
func TestListTaxDetailHandler_QueryBindingError(t *testing.T) {

	req := httptest.NewRequest("GET", "/v1/pao-gen/ddo/102555/tax-details?period=03202&type=3", nil)
	req.Header.Set("Content-Type", "application/json")
	rec := httptest.NewRecorder()
	Router.Engine.ServeHTTP(rec, req)

	assert.Equal(t, http.StatusBadRequest, rec.Code)
}

func TestListTaxDetailHandler_ValidationError(t *testing.T) {

	req := httptest.NewRequest("GET", "/v1/pao-gen/ddo/102555/tax-details?period=032024&type=9", nil)
	req.Header.Set("Content-Type", "application/json")
	rec := httptest.NewRecorder()
	Router.Engine.ServeHTTP(rec, req)

	assert.Equal(t, http.StatusUnprocessableEntity, rec.Code)
}
