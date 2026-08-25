package tests

import (
	"bytes"
	"net/http"
	"net/http/httptest"
	"testing"

	"github.com/magiconair/properties/assert"
)

//ListBroadsheetHandler-----------------------------------------------------------------------------------------

func TestListBroadsheetHandlertype1_Success(t *testing.T) {
	// Router = BootstrapTestApp(t)

	req := httptest.NewRequest("GET", "/v1/pao-gen/ddo/102595/public-acct/broad-sheet?type=1&month-year=082024&major-head=8001&skip=0&limit=0", nil)
	req.Header.Set("Content-Type", "application/json")
	rec := httptest.NewRecorder()
	Router.Engine.ServeHTTP(rec, req)

	assert.Equal(t, http.StatusOK, rec.Code)
}
func TestListBroadsheetHandlertype2_Success(t *testing.T) {
	// Router = BootstrapTestApp(t)

	req := httptest.NewRequest("GET", "/v1/pao-gen/ddo/102595/public-acct/broad-sheet?type=2&month-year=082024&skip=0&limit=0", nil)
	req.Header.Set("Content-Type", "application/json")
	rec := httptest.NewRecorder()
	Router.Engine.ServeHTTP(rec, req)

	assert.Equal(t, http.StatusOK, rec.Code)
}
func TestListBroadsheetHandlertype3_Success(t *testing.T) {
	// Router = BootstrapTestApp(t)

	req := httptest.NewRequest("GET", "/v1/pao-gen/ddo//public-acct/broad-sheet?type=3&month-year=082024&skip=0&limit=0", nil)
	req.Header.Set("Content-Type", "application/json")
	rec := httptest.NewRecorder()
	Router.Engine.ServeHTTP(rec, req)

	assert.Equal(t, http.StatusOK, rec.Code)
}
func TestListBroadsheetHandlertype4_Success(t *testing.T) {
	// Router = BootstrapTestApp(t)

	req := httptest.NewRequest("GET", "/v1/pao-gen/ddo/102595/public-acct/broad-sheet?type=4&month-year=082024&major-head=8001&skip=0&limit=0", nil)
	req.Header.Set("Content-Type", "application/json")
	rec := httptest.NewRecorder()
	Router.Engine.ServeHTTP(rec, req)

	assert.Equal(t, http.StatusOK, rec.Code)
}
func TestListBroadsheetHandlertype5_Success(t *testing.T) {
	// Router = BootstrapTestApp(t)

	req := httptest.NewRequest("GET", "/v1/pao-gen/ddo/102595/public-acct/broad-sheet?type=5&month-year=082024&major-head=8001&skip=0&limit=0", nil)
	req.Header.Set("Content-Type", "application/json")
	rec := httptest.NewRecorder()
	Router.Engine.ServeHTTP(rec, req)

	assert.Equal(t, http.StatusOK, rec.Code)
}

func TestListBroadsheetHandler_QueryBindingError(t *testing.T) {

	req := httptest.NewRequest("GET", "/v1/pao-gen/ddo/102595/public-acct/broad-sheet?type=1&month-year=082024&major-head=8001&skip=a&limit=0", nil)
	req.Header.Set("Content-Type", "application/json")
	rec := httptest.NewRecorder()
	Router.Engine.ServeHTTP(rec, req)

	assert.Equal(t, http.StatusBadRequest, rec.Code)
}

func TestListBroadsheetHandler_ValidationError(t *testing.T) {

	req := httptest.NewRequest("GET", "/v1/pao-gen/ddo/102595/public-acct/broad-sheet?type=9&month-year=082024&major-head=8001", nil)
	req.Header.Set("Content-Type", "application/json")
	rec := httptest.NewRecorder()
	Router.Engine.ServeHTTP(rec, req)

	assert.Equal(t, http.StatusUnprocessableEntity, rec.Code)
}
func TestListBroadsheetHandler2_ValidationError(t *testing.T) {

	req := httptest.NewRequest("GET", "/v1/pao-gen/ddo/5r3ei4j8/public-acct/broad-sheet?type=1&month-year=082024&major-head=8001&skip=0&limit=0", nil)
	req.Header.Set("Content-Type", "application/json")
	rec := httptest.NewRecorder()
	Router.Engine.ServeHTTP(rec, req)

	assert.Equal(t, http.StatusUnprocessableEntity, rec.Code)
}

//CreateRemunerationRateHandler--------------------------------------------------------------------------------------

func TestCreateRemunerationRateHandler_Success(t *testing.T) {
	input := `[
    {
        "financial_year": "2024",
        "remuneration_item": "Savings Deposits",
        "remuneration_type": "TIN",
        "remuneration_rate": 219.26,
        "updated_by": 10257696,
        "updated_date": "2024-06-28T00:00:00Z",
        "approved_by": 10257698,
        "approved_date": "2024-06-28T00:00:00Z",
        "status": true,
        "authorisation_status": "2"
    }
]`
	// body, _ := json.Marshal(input)
	req := httptest.NewRequest("POST", "/v1/public-acct/remuneration", bytes.NewBuffer([]byte(input)))
	req.Header.Set("Content-Type", "application/json")
	rec := httptest.NewRecorder()
	Router.Engine.ServeHTTP(rec, req)
	assert.Equal(t, http.StatusCreated, rec.Code)
}

func TestCreateRemunerationRateHandler_JSONBindingError(t *testing.T) {
	input := `[
    {
        "financial_year": "2024",
        "remuneration_item": "Savings Deposits",
        "remuneration_type": "TIN",
        "remuneration_rate": 219.26,
        "updated_by": "10257696",
        "updated_date": "2024-06-28T00:00:00Z",
        "approved_by": 10257698,
        "approved_date": "2024-06-28T00:00:00Z",
        "status": true,
        "authorisation_status": "2"
    }
]`
	// body, _ := json.Marshal(input)
	req := httptest.NewRequest("POST", "/v1/public-acct/remuneration", bytes.NewBuffer([]byte(input)))
	req.Header.Set("Content-Type", "application/json")
	rec := httptest.NewRecorder()
	Router.Engine.ServeHTTP(rec, req)

	assert.Equal(t, http.StatusBadRequest, rec.Code)
}

func TestCreateRemunerationRateHandler_ValidationError(t *testing.T) {
	input := `[
    {
        "remuneration_item": "Savings Deposits",
        "remuneration_type": "TIN",
        "remuneration_rate": 219.26,
        "updated_by": 10257696,
        "updated_date": "2024-06-28T00:00:00Z",
        "approved_by": 10257698,
        "approved_date": "2024-06-28T00:00:00Z",
        "status": true,
        "authorisation_status": "2"
    }
]`
	// body, _ := json.Marshal(input)
	req := httptest.NewRequest("POST", "/v1/public-acct/remuneration", bytes.NewBuffer([]byte(input)))
	req.Header.Set("Content-Type", "application/json")
	rec := httptest.NewRecorder()
	Router.Engine.ServeHTTP(rec, req)

	assert.Equal(t, http.StatusUnprocessableEntity, rec.Code)
}
func TestCreateRemunerationRateHandler_AlreadyVerified(t *testing.T) {
	input := `[
    {
        "financial_year": "2024",
        "remuneration_item": "Savings Deposits",
        "remuneration_type": "NAN",
        "remuneration_rate": 219.26,
        "updated_by": 10257696,
        "updated_date": "2024-06-28T00:00:00Z",
        "approved_by": 10257698,
        "approved_date": "2024-06-28T00:00:00Z",
        "status": true,
        "authorisation_status": "2"
    }
]`
	// body, _ := json.Marshal(input)
	req := httptest.NewRequest("POST", "/v1/public-acct/remuneration", bytes.NewBuffer([]byte(input)))
	req.Header.Set("Content-Type", "application/json")
	rec := httptest.NewRecorder()
	Router.Engine.ServeHTTP(rec, req)
	assert.Equal(t, http.StatusConflict, rec.Code)
}

//UpdateRemunerationRateHandler-----------------------------------------------------------------------------------------

func TestUpdateRemunerationRateHandler_Success(t *testing.T) {
	input := `[
    {
        "financial_year": "2024",
        "remuneration_item": "Savings Deposits",
        "remuneration_type": "RT",
        "remuneration_rate": 219.23,
        "updated_by": 10257696,
        "updated_date": "2024-06-28T00:00:00Z",
        "status": true,
        "authorisation_status": "Approved"
    }
]`
	req := httptest.NewRequest("PUT", "/v1/public-acct/bulk-remuneration", bytes.NewBuffer([]byte(input)))
	req.Header.Set("Content-Type", "application/json")
	rec := httptest.NewRecorder()
	Router.Engine.ServeHTTP(rec, req)

	assert.Equal(t, http.StatusOK, rec.Code)
}
func TestUpdateRemunerationRateHandler2_Success(t *testing.T) {
	input := `[
    {
        "financial_year": "2024",
        "remuneration_item": "Savings Deposits",
        "remuneration_type": "RT",
        "remuneration_rate": 219.23,
        "updated_by": 10257696,
        "updated_date": "2024-06-28T00:00:00Z",
        "status": true,
        "authorisation_status": "Approved",
        "approved_by": 12401056
    }
]`
	req := httptest.NewRequest("PUT", "/v1/public-acct/bulk-remuneration", bytes.NewBuffer([]byte(input)))
	req.Header.Set("Content-Type", "application/json")
	rec := httptest.NewRecorder()
	Router.Engine.ServeHTTP(rec, req)

	assert.Equal(t, http.StatusOK, rec.Code)
}
func TestUpdateRemunerationRateHandler_JSONBindingError(t *testing.T) {

	input := `[
    {
        "financial_year": "2024",
        "remuneration_item": "Savings Deposits",
        "remuneration_type": "RT",
        "remuneration_rate": 219.23,
        "updated_by": "10257696",
        "updated_date": "2024-06-28T00:00:00Z",
        "status": true,
        "authorisation_status": "Approved"
    }
]`
	req := httptest.NewRequest("PUT", "/v1/public-acct/bulk-remuneration", bytes.NewBuffer([]byte(input)))
	req.Header.Set("Content-Type", "application/json")
	rec := httptest.NewRecorder()
	Router.Engine.ServeHTTP(rec, req)

	assert.Equal(t, http.StatusBadRequest, rec.Code)
}

func TestUpdateRemunerationRateHandler_ValidationError(t *testing.T) {
	input := `[
    {
        "remuneration_item": "Savings Deposits",
        "remuneration_type": "RT",
        "remuneration_rate": 219.23,
        "updated_by": 10257696,
        "updated_date": "2024-06-28T00:00:00Z",
        "status": true,
        "authorisation_status": "Approved"
    }
]`
	req := httptest.NewRequest("PUT", "/v1/public-acct/bulk-remuneration", bytes.NewBuffer([]byte(input)))
	req.Header.Set("Content-Type", "application/json")
	rec := httptest.NewRecorder()
	Router.Engine.ServeHTTP(rec, req)
	assert.Equal(t, http.StatusUnprocessableEntity, rec.Code)
}

//ListRemunerationRateDetailHandler--------------------------------------------------------------------------------------

func TestListRemunerationRateDetailHandler_Success(t *testing.T) {
	// Router = BootstrapTestApp(t)

	req := httptest.NewRequest("GET", "/v1/public-acct/remuneration?type=1&id=2024&skip=0&limit=0", nil)
	req.Header.Set("Content-Type", "application/json")
	rec := httptest.NewRecorder()
	Router.Engine.ServeHTTP(rec, req)

	assert.Equal(t, http.StatusOK, rec.Code)
}
func TestListRemunerationRateDetailHandler2_Success(t *testing.T) {
	// Router = BootstrapTestApp(t)

	req := httptest.NewRequest("GET", "/v1/public-acct/remuneration?type=2&id=1&skip=0&limit=0", nil)
	req.Header.Set("Content-Type", "application/json")
	rec := httptest.NewRecorder()
	Router.Engine.ServeHTTP(rec, req)

	assert.Equal(t, http.StatusOK, rec.Code)
}
func TestListRemunerationRateDetailHandler3_Success(t *testing.T) {
	// Router = BootstrapTestApp(t)

	req := httptest.NewRequest("GET", "/v1/public-acct/remuneration?type=3&id=true&skip=0&limit=0", nil)
	req.Header.Set("Content-Type", "application/json")
	rec := httptest.NewRecorder()
	Router.Engine.ServeHTTP(rec, req)

	assert.Equal(t, http.StatusOK, rec.Code)
}
func TestListRemunerationRateDetailHandler_QueryBindingError(t *testing.T) {

	req := httptest.NewRequest("GET", "/v1/public-acct/remuneration?type=1&id=2024&skip=a&limit=0", nil)
	req.Header.Set("Content-Type", "application/json")
	rec := httptest.NewRecorder()
	Router.Engine.ServeHTTP(rec, req)

	assert.Equal(t, http.StatusBadRequest, rec.Code)
}

func TestListRemunerationRateDetailHandler_ValidationError(t *testing.T) {

	req := httptest.NewRequest("GET", "/v1/public-acct/remuneration?type=9&id=2024&skip=0&limit=0", nil)
	req.Header.Set("Content-Type", "application/json")
	rec := httptest.NewRecorder()
	Router.Engine.ServeHTTP(rec, req)

	assert.Equal(t, http.StatusUnprocessableEntity, rec.Code)
}

//ListApprAcctsHandler----------------------------------------------------------------------------------------------

func TestListApprAcctsHandler_Success(t *testing.T) {
	// Router = BootstrapTestApp(t)

	req := httptest.NewRequest("GET", "/v1/public-acct/appr-acct-one?year=2024&skip=0&limit=0", nil)
	req.Header.Set("Content-Type", "application/json")
	rec := httptest.NewRecorder()
	Router.Engine.ServeHTTP(rec, req)

	assert.Equal(t, http.StatusOK, rec.Code)
}
func TestListApprAcctsHandler_QueryBindingError(t *testing.T) {

	req := httptest.NewRequest("GET", "/v1/public-acct/appr-acct-one?year=2022&skip=a&limit=0", nil)
	req.Header.Set("Content-Type", "application/json")
	rec := httptest.NewRecorder()
	Router.Engine.ServeHTTP(rec, req)

	assert.Equal(t, http.StatusBadRequest, rec.Code)
}

func TestListApprAcctsHandler_ValidationError(t *testing.T) {

	req := httptest.NewRequest("GET", "/v1/public-acct/appr-acct-one?year=202245", nil)
	req.Header.Set("Content-Type", "application/json")
	rec := httptest.NewRecorder()
	Router.Engine.ServeHTTP(rec, req)

	assert.Equal(t, http.StatusUnprocessableEntity, rec.Code)
}

//ListApprAcctsTwoHandler---------------------------------------------------------------------------------------------

func TestListApprAcctsTwoHandler_Success(t *testing.T) {
	// Router = BootstrapTestApp(t)

	req := httptest.NewRequest("GET", "/v1/public-acct/appr-acct-two?year=2024&skip=0&limit=0", nil)
	req.Header.Set("Content-Type", "application/json")
	rec := httptest.NewRecorder()
	Router.Engine.ServeHTTP(rec, req)

	assert.Equal(t, http.StatusOK, rec.Code)
}
func TestListApprAcctsTwoHandler_QueryBindingError(t *testing.T) {

	req := httptest.NewRequest("GET", "/v1/public-acct/appr-acct-two?year=2024&skip=a&limit=0", nil)
	req.Header.Set("Content-Type", "application/json")
	rec := httptest.NewRecorder()
	Router.Engine.ServeHTTP(rec, req)

	assert.Equal(t, http.StatusBadRequest, rec.Code)
}

func TestListApprAcctsTwoHandler_ValidationError(t *testing.T) {

	req := httptest.NewRequest("GET", "/v1/public-acct/appr-acct-two?year=202478", nil)
	req.Header.Set("Content-Type", "application/json")
	rec := httptest.NewRecorder()
	Router.Engine.ServeHTTP(rec, req)

	assert.Equal(t, http.StatusUnprocessableEntity, rec.Code)
}
