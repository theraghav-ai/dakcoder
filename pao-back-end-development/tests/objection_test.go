package tests

import (
	"bytes"
	"net/http"
	"net/http/httptest"
	"testing"

	"github.com/magiconair/properties/assert"
)

//CreateObjectionHandler----------------------------------------------------------------------------------------------

func TestCreateObjectionHandler_Success(t *testing.T) {
	input := `{
  "pao_code": "078109",
  "ddo_code": "102558",
  "description": "Sample objection description",
  "created_by": 10257696,
  "remarks": [
  ],
  "status_flag": "created"
}`
	// body, _ := json.Marshal(input)
	req := httptest.NewRequest("POST", "/v1/objection", bytes.NewBuffer([]byte(input)))
	req.Header.Set("Content-Type", "application/json")
	rec := httptest.NewRecorder()
	Router.Engine.ServeHTTP(rec, req)
	assert.Equal(t, http.StatusCreated, rec.Code)
}

func TestCreateObjectionHandler_JSONBindingError(t *testing.T) {
	input := `{
  "pao_code": "078109",
  "ddo_code": "102558",
  "description": "Sample objection description",
  "created_by": "10257696",
  "remarks": [
  ],
  "status_flag": "created"
}`
	// body, _ := json.Marshal(input)
	req := httptest.NewRequest("POST", "/v1/objection", bytes.NewBuffer([]byte(input)))
	req.Header.Set("Content-Type", "application/json")
	rec := httptest.NewRecorder()
	Router.Engine.ServeHTTP(rec, req)

	assert.Equal(t, http.StatusBadRequest, rec.Code)
}

func TestCreateObjectionHandler_ValidationError(t *testing.T) {
	input := `{
  "pao_code": "078ij9",
  "ddo_code": "102558",
  "description": "Sample objection description",
  "created_by": 10257696,
  "remarks": [
  ],
  "status_flag": "created"
}`
	// body, _ := json.Marshal(input)
	req := httptest.NewRequest("POST", "/v1/objection", bytes.NewBuffer([]byte(input)))
	req.Header.Set("Content-Type", "application/json")
	rec := httptest.NewRecorder()
	Router.Engine.ServeHTTP(rec, req)

	assert.Equal(t, http.StatusUnprocessableEntity, rec.Code)
}

//FetchObjectionByIdHandler----------------------------------------------------------------------------------

func TestFetchObjectionByIdHandler_Success(t *testing.T) {
	// Router = BootstrapTestApp(t)

	req := httptest.NewRequest("GET", "/v1/objection/OBJ102558202411271720451021/details", nil)
	req.Header.Set("Content-Type", "application/json")
	rec := httptest.NewRecorder()
	Router.Engine.ServeHTTP(rec, req)

	assert.Equal(t, http.StatusOK, rec.Code)
}
func TestFetchObjectionByIdHandler_UriBindingError(t *testing.T) {

	req := httptest.NewRequest("GET", "/v1/objection//details", nil)
	req.Header.Set("Content-Type", "application/json")
	rec := httptest.NewRecorder()
	Router.Engine.ServeHTTP(rec, req)

	assert.Equal(t, http.StatusBadRequest, rec.Code)
}

func TestFetchObjectionByIdHandler_ValidationError(t *testing.T) {

	req := httptest.NewRequest("GET", "/v1/objection/OBJ102560202409051424103452438adsfaSD612/details", nil)
	req.Header.Set("Content-Type", "application/json")
	rec := httptest.NewRecorder()
	Router.Engine.ServeHTTP(rec, req)

	assert.Equal(t, http.StatusUnprocessableEntity, rec.Code)
}

//UpdateObjectionHandler-------------------------------------------------------------------------------------

func TestUpdateObjectionHandler_Success(t *testing.T) {
	input := `{
  "objection_id": "OBJ102558202411281711423285",
  "remarks":
    {
      "data": "sdfgstg",
      "commented_by": 10257696,
      "commented_date": "2024-04-16T12:30:00Z",
      "commented_office_id": 12345674,
"filepath": "test"
    },
    "status_flag": "paoupdated",
"updated_by":   10257696,
    "updated_date": "2024-06-19T00:00:00Z"


}`
	// body, _ := json.Marshal(input)
	req := httptest.NewRequest("PUT", "/v1/objection/OBJ102558202411281711423285/remarks", bytes.NewBuffer([]byte(input)))
	req.Header.Set("Content-Type", "application/json")
	rec := httptest.NewRecorder()
	Router.Engine.ServeHTTP(rec, req)

	assert.Equal(t, http.StatusOK, rec.Code)
}

func TestUpdateObjectionHandler_JSONBindingError(t *testing.T) {
	input := `{
  "objection_id": "OBJ102558202411281711423285",
  "remarks":
    {
      "data": "sdfgstg",
      "commented_by": 10257696,
      "commented_date": "2024-04-16T12:30:00Z",
      "commented_office_id": 12345674,
"filepath": "test"
    },
    "status_flag": "paoupdated",
"updated_by":   "10257696",
    "updated_date": "2024-06-19T00:00:00Z"


}`
	req := httptest.NewRequest("PUT", "/v1/objection/OBJ102558202411281711423285/remarks", bytes.NewBuffer([]byte(input)))
	req.Header.Set("Content-Type", "application/json")
	rec := httptest.NewRecorder()
	Router.Engine.ServeHTTP(rec, req)

	assert.Equal(t, http.StatusBadRequest, rec.Code)
}

func TestUpdateObjectionHandler_UriBindingError(t *testing.T) {
	input := `{
  "objection_id": "OBJ102558202411281711423285",
  "remarks":
    {
      "data": "sdfgstg",
      "commented_by": 10257696,
      "commented_date": "2024-04-16T12:30:00Z",
      "commented_office_id": 12345674,
"filepath": "test"
    },
    "status_flag": "paoupdated",
"updated_by":   "10257696",
    "updated_date": "2024-06-19T00:00:00Z"


}`
	req := httptest.NewRequest("PUT", "/v1/objection//remarks", bytes.NewBuffer([]byte(input)))
	req.Header.Set("Content-Type", "application/json")
	rec := httptest.NewRecorder()
	Router.Engine.ServeHTTP(rec, req)

	assert.Equal(t, http.StatusBadRequest, rec.Code)
}

func TestUpdateObjectionHandler_ValidationError(t *testing.T) {
	input := `{
  "objection_id": "OBJ102558202411281711423285",
  "remarks":
    {
      "data": "sdfgstg",
      "commented_by": 10257696,
      "commented_date": "2024-04-16T12:30:00Z",
      "commented_office_id": 12345674,
"filepath": "test"
    },
    "status_flag": "paoupdated",
"updated_by":   1025769689,
    "updated_date": "2024-06-19T00:00:00Z"


}`
	req := httptest.NewRequest("PUT", "/v1/objection/OBJ102558202411281711423285/remarks", bytes.NewBuffer([]byte(input)))
	req.Header.Set("Content-Type", "application/json")
	rec := httptest.NewRecorder()
	Router.Engine.ServeHTTP(rec, req)
	assert.Equal(t, http.StatusUnprocessableEntity, rec.Code)
}

//UpdateObjectionClosureHandler------------------------------------------------------------------------------------------

func TestUpdateObjectionClosureHandler_Success(t *testing.T) {
	input := `{
  "objection_id": "OBJ102558202411281711423285",
  "remarks":
    {
      "data": "sdfgstg",
      "commented_by": 10257696,
      "commented_date": "2024-04-16T12:30:00Z",
      "commented_office_id": 12345674,
"filepath": "test"
    },
    "status_flag": "paoupdated",
"updated_by":   10257696,
    "updated_date": "2024-06-19T00:00:00Z"


}`
	// body, _ := json.Marshal(input)
	req := httptest.NewRequest("PUT", "/v1/objection/OBJ102558202411281711423285/closure", bytes.NewBuffer([]byte(input)))
	req.Header.Set("Content-Type", "application/json")
	rec := httptest.NewRecorder()
	Router.Engine.ServeHTTP(rec, req)

	assert.Equal(t, http.StatusOK, rec.Code)
}

func TestUpdateObjectionClosureHandler_JSONBindingError(t *testing.T) {
	input := `{
    "objection_id": "OBJ102558202412021056336650",
    "status_flag": "closed",
    "remarks": {
        "data": "Closure 5",
        "commented_by": 10257698,
        "commented_date": "2024-04-16T12:30:00Z",
        "commented_office_id": 12345678
    },
    "closed_by": "10257967",
    "closed_date": "2024-06-19T00:00:00Z"
}`
	req := httptest.NewRequest("PUT", "/v1/objection/OBJ102558202412021056336650/closure", bytes.NewBuffer([]byte(input)))
	req.Header.Set("Content-Type", "application/json")
	rec := httptest.NewRecorder()
	Router.Engine.ServeHTTP(rec, req)

	assert.Equal(t, http.StatusBadRequest, rec.Code)
}
func TestUpdateObjectionClosureHandler_UriBindingError(t *testing.T) {
	input := `{
    "objection_id": "OBJ102558202412021056336650",
    "status_flag": "closed",
    "remarks": {
        "data": "Closure 5",
        "commented_by": 10257698,
        "commented_date": "2024-04-16T12:30:00Z",
        "commented_office_id": 12345678
    },
    "closed_by": 10257967,
    "closed_date": "2024-06-19T00:00:00Z"
}`
	req := httptest.NewRequest("PUT", "/v1/objection//closure", bytes.NewBuffer([]byte(input)))
	req.Header.Set("Content-Type", "application/json")
	rec := httptest.NewRecorder()
	Router.Engine.ServeHTTP(rec, req)

	assert.Equal(t, http.StatusBadRequest, rec.Code)
}

func TestUpdateObjectionClosureHandler_ValidationError(t *testing.T) {
	input := `{
    "objection_id": "OBJ102558202412021056336650",
    "status_flag": "closed",
    "remarks": {
        "data": "Closure 5",
        "commented_by": 10257698,
        "commented_date": "2024-04-16T12:30:00Z",
        "commented_office_id": 12345678
    },
    "closed_by": 1035257967,
    "closed_date": "2024-06-19T00:00:00Z"
}`
	req := httptest.NewRequest("PUT", "/v1/objection/OBJ102558202412021056sdff33sadfqwerdfcvzdfasdff6650/closure", bytes.NewBuffer([]byte(input)))
	req.Header.Set("Content-Type", "application/json")
	rec := httptest.NewRecorder()
	Router.Engine.ServeHTTP(rec, req)
	assert.Equal(t, http.StatusUnprocessableEntity, rec.Code)
}

//CreateObjectionPraoHandler------------------------------------------------------------------------------------------------

func TestCreateObjectionPraoHandler_Success(t *testing.T) {
	input := `{
  "prao_code": "PRAO12",
  "pao_code": "078109",
  "description": "Sample objection description",
  "objection_id": "",
  "created_by": 10257696,
  "created_date": "2024-04-16T10:00:00Z",
  "remarks": [
    {
      "data": "Sample remark data 1",
      "commented_by": 10257696,
      "commented_date": "2024-04-16T12:30:00Z",
      "commented_office_id": 12345679
    },
    {
      "data": "Sample remark data 2",
      "commented_by": 10257696,
      "commented_date": "2024-04-17T08:45:00Z",
      "commented_office_id": 12345679
    }
  ],
  "status_flag": "created"
}`
	// body, _ := json.Marshal(input)
	req := httptest.NewRequest("POST", "/v1/objection/prao", bytes.NewBuffer([]byte(input)))
	req.Header.Set("Content-Type", "application/json")
	rec := httptest.NewRecorder()
	Router.Engine.ServeHTTP(rec, req)
	assert.Equal(t, http.StatusCreated, rec.Code)
}

func TestCreateObjectionPraoHandler_JSONBindingError(t *testing.T) {
	input := `{
  "prao_code": "PRAO12",
  "pao_code": "078109",
  "description": "Sample objection description",
  "objection_id": "",
  "created_by": "10257696",
  "created_date": "2024-04-16T10:00:00Z",
  "remarks": [
    {
      "data": "Sample remark data 1",
      "commented_by": 10257696,
      "commented_date": "2024-04-16T12:30:00Z",
      "commented_office_id": 12345679
    },
    {
      "data": "Sample remark data 2",
      "commented_by": 10257696,
      "commented_date": "2024-04-17T08:45:00Z",
      "commented_office_id": 12345679
    }
  ],
  "status_flag": "created"
}`
	// body, _ := json.Marshal(input)
	req := httptest.NewRequest("POST", "/v1/objection/prao", bytes.NewBuffer([]byte(input)))
	req.Header.Set("Content-Type", "application/json")
	rec := httptest.NewRecorder()
	Router.Engine.ServeHTTP(rec, req)

	assert.Equal(t, http.StatusBadRequest, rec.Code)
}

func TestCreateObjectionPraoHandler_ValidationError(t *testing.T) {
	input := `{
  "prao_code": "PRAO12",
  "pao_code": "078109",
  "description": "Sample objection description",
  "objection_id": "",
  "created_by": 10257456696,
  "created_date": "2024-04-16T10:00:00Z",
  "remarks": [
    {
      "data": "Sample remark data 1",
      "commented_by": 10257696,
      "commented_date": "2024-04-16T12:30:00Z",
      "commented_office_id": 12345679
    },
    {
      "data": "Sample remark data 2",
      "commented_by": 10257696,
      "commented_date": "2024-04-17T08:45:00Z",
      "commented_office_id": 12345679
    }
  ],
  "status_flag": "created"
}`
	// body, _ := json.Marshal(input)
	req := httptest.NewRequest("POST", "/v1/objection/prao", bytes.NewBuffer([]byte(input)))
	req.Header.Set("Content-Type", "application/json")
	rec := httptest.NewRecorder()
	Router.Engine.ServeHTTP(rec, req)

	assert.Equal(t, http.StatusUnprocessableEntity, rec.Code)
}

//FetchObjectionPraoByIdHandler-----------------------------------------------------------------------------------

func TestFetchObjectionPraoByIdHandler_Success(t *testing.T) {
	// Router = BootstrapTestApp(t)

	req := httptest.NewRequest("GET", "/v1/objection/OBJ078109202411271728415502/prao/details", nil)
	req.Header.Set("Content-Type", "application/json")
	rec := httptest.NewRecorder()
	Router.Engine.ServeHTTP(rec, req)

	assert.Equal(t, http.StatusOK, rec.Code)
}
func TestFetchObjectionPraoByIdHandler_UriBindingError(t *testing.T) {

	req := httptest.NewRequest("GET", "/v1/objection//prao/details", nil)
	req.Header.Set("Content-Type", "application/json")
	rec := httptest.NewRecorder()
	Router.Engine.ServeHTTP(rec, req)

	assert.Equal(t, http.StatusBadRequest, rec.Code)
}

func TestFetchObjectionPraoByIdHandler_ValidationError(t *testing.T) {

	req := httptest.NewRequest("GET", "/v1/objection/OBJ07810920240822345456serdgfdsghserd1454198019/prao/details", nil)
	req.Header.Set("Content-Type", "application/json")
	rec := httptest.NewRecorder()
	Router.Engine.ServeHTTP(rec, req)

	assert.Equal(t, http.StatusUnprocessableEntity, rec.Code)
}

//UpdateObjectionPraoHandler--------------------------------------------------------------------------------------------

func TestUpdateObjectionPraoHandler_Success(t *testing.T) {
	input := `{
  "objection_id": "OBJ078109202411271726198491",
  "remarks":
    {
      "data": "Sample remark data 10",
      "commented_by": 10257696,
      "commented_date": "2024-04-16T12:30:00Z",
      "commented_office_id": 12345670
    },
    "updated_by":   10257696,
        "updated_date": "2024-06-19T00:00:00Z",
        "status_flag":  "paoupdated"
}`
	// body, _ := json.Marshal(input)
	req := httptest.NewRequest("PUT", "/v1/objection/OBJ078109202411271726198491/prao/remarks", bytes.NewBuffer([]byte(input)))
	req.Header.Set("Content-Type", "application/json")
	rec := httptest.NewRecorder()
	Router.Engine.ServeHTTP(rec, req)

	assert.Equal(t, http.StatusOK, rec.Code)
}

func TestUpdateObjectionPraoHandler_JSONBindingError(t *testing.T) {
	input := `{
  "objection_id": "OBJ078109202411271726198491",
  "remarks":
    {
      "data": "Sample remark data 10",
      "commented_by": 10257696,
      "commented_date": "2024-04-16T12:30:00Z",
      "commented_office_id": 12345670
    },
    "updated_by":   "10257696",
        "updated_date": "2024-06-19T00:00:00Z",
        "status_flag":  "paoupdated"
}`
	req := httptest.NewRequest("PUT", "/v1/objection/OBJ078109202411271726198491/prao/remarks", bytes.NewBuffer([]byte(input)))
	req.Header.Set("Content-Type", "application/json")
	rec := httptest.NewRecorder()
	Router.Engine.ServeHTTP(rec, req)

	assert.Equal(t, http.StatusBadRequest, rec.Code)
}
func TestUpdateObjectionPraoHandler_UriBindingError(t *testing.T) {
	input := `{
  "objection_id": "OBJ078109202411271726198491",
  "remarks":
    {
      "data": "Sample remark data 10",
      "commented_by": 10257696,
      "commented_date": "2024-04-16T12:30:00Z",
      "commented_office_id": 12345670
    },
    "updated_by":   10257696,
        "updated_date": "2024-06-19T00:00:00Z",
        "status_flag":  "paoupdated"
}`
	req := httptest.NewRequest("PUT", "/v1/objection//prao/remarks", bytes.NewBuffer([]byte(input)))
	req.Header.Set("Content-Type", "application/json")
	rec := httptest.NewRecorder()
	Router.Engine.ServeHTTP(rec, req)

	assert.Equal(t, http.StatusBadRequest, rec.Code)
}

func TestUpdateObjectionPraoHandler_ValidationError(t *testing.T) {
	input := `{
  "objection_id": "OBJ078109202411271726198491",
  "remarks":
    {
      "data": "Sample remark data 10",
      "commented_by": 10257696,
      "commented_date": "2024-04-16T12:30:00Z",
      "commented_office_id": 12345670
    },
    "updated_by":   1025657696,
        "updated_date": "2024-06-19T00:00:00Z",
        "status_flag":  "paoupdated"
}`
	req := httptest.NewRequest("PUT", "/v1/objection/OBJ078109202411271726198491/prao/remarks", bytes.NewBuffer([]byte(input)))
	req.Header.Set("Content-Type", "application/json")
	rec := httptest.NewRecorder()
	Router.Engine.ServeHTTP(rec, req)
	assert.Equal(t, http.StatusUnprocessableEntity, rec.Code)
}

//UpdateObjectionClosurePraoHandler-------------------------------------------------------------------------------------

func TestUpdateObjectionClosurePraoHandler_Success(t *testing.T) {
	input := `{
  "objection_id": "OBJ078109202411291805084605",
  "status_flag": "closed",
  "remarks":
    {
      "data": "Closure 5",
      "commented_by": 10257696,
      "commented_date": "2024-03-01T12:30:00Z",
      "commented_office_id": 12345679
    },
    "closed_by": 10257696
}`
	// body, _ := json.Marshal(input)
	req := httptest.NewRequest("PUT", "/v1/objection/OBJ078109202411291805084605/prao/closure", bytes.NewBuffer([]byte(input)))
	req.Header.Set("Content-Type", "application/json")
	rec := httptest.NewRecorder()
	Router.Engine.ServeHTTP(rec, req)

	assert.Equal(t, http.StatusOK, rec.Code)
}

func TestUpdateObjectionClosurePraoHandler_JSONBindingError(t *testing.T) {
	input := `{
  "objection_id": "OBJ078109202411291805084605",
  "status_flag": "closed",
  "remarks":
    {
      "data": "Closure 5",
      "commented_by": 10257696,
      "commented_date": "2024-03-01T12:30:00Z",
      "commented_office_id": 12345679
    },
    "closed_by": "10257696"
}`
	req := httptest.NewRequest("PUT", "/v1/objection/OBJ078109202411291805084605/prao/closure", bytes.NewBuffer([]byte(input)))
	req.Header.Set("Content-Type", "application/json")
	rec := httptest.NewRecorder()
	Router.Engine.ServeHTTP(rec, req)

	assert.Equal(t, http.StatusBadRequest, rec.Code)
}
func TestUpdateObjectionClosurePraoHandler_UriBindingError(t *testing.T) {
	input := `{
  "objection_id": "OBJ078109202411291805084605",
  "status_flag": "closed",
  "remarks":
    {
      "data": "Closure 5",
      "commented_by": 10257696,
      "commented_date": "2024-03-01T12:30:00Z",
      "commented_office_id": 12345679
    },
    "closed_by": 10257696
}`
	req := httptest.NewRequest("PUT", "/v1/objection//prao/closure", bytes.NewBuffer([]byte(input)))
	req.Header.Set("Content-Type", "application/json")
	rec := httptest.NewRecorder()
	Router.Engine.ServeHTTP(rec, req)

	assert.Equal(t, http.StatusBadRequest, rec.Code)
}

func TestUpdateObjectionClosurePraoHandler_ValidationError(t *testing.T) {
	input := `{
  "objection_id": "OBJ078109202411291805084605",
  "status_flag": "closed",
  "remarks":
    {
      "data": "Closure 5",
      "commented_by": 10257696,
      "commented_date": "2024-03-01T12:30:00Z",
      "commented_office_id": 12345679
    },
    "closed_by": 1025766896
}`
	req := httptest.NewRequest("PUT", "/v1/objection/OBJ078109202411291805084605/prao/closure", bytes.NewBuffer([]byte(input)))
	req.Header.Set("Content-Type", "application/json")
	rec := httptest.NewRecorder()
	Router.Engine.ServeHTTP(rec, req)
	assert.Equal(t, http.StatusUnprocessableEntity, rec.Code)
}

//ListObjectionCodeHandler-------------------------------------------------------------------------------------------------

func TestListObjectionCodeHandlerType2_Success(t *testing.T) {
	// Router = BootstrapTestApp(t)

	req := httptest.NewRequest("GET", "/v1/objection/pao/code?code=102557&type=2&status=notclosed&from-date=2024-01-01&to-date=2024-10-01&skip=0&limit=0", nil)
	req.Header.Set("Content-Type", "application/json")
	rec := httptest.NewRecorder()
	Router.Engine.ServeHTTP(rec, req)

	assert.Equal(t, http.StatusOK, rec.Code)
}
func TestListObjectionCodeHandlerType21_Success(t *testing.T) {
	// Router = BootstrapTestApp(t)

	req := httptest.NewRequest("GET", "/v1/objection/pao/code?code=102557&type=2&status=paoupdated&from-date=2024-01-01&to-date=2024-10-01&skip=0&limit=0", nil)
	req.Header.Set("Content-Type", "application/json")
	rec := httptest.NewRecorder()
	Router.Engine.ServeHTTP(rec, req)

	assert.Equal(t, http.StatusOK, rec.Code)
}
func TestListObjectionCodeHandlerType1_Success(t *testing.T) {
	// Router = BootstrapTestApp(t)

	req := httptest.NewRequest("GET", "/v1/objection/pao/code?code=078118&type=1&status=notclosed&from-date=2024-01-01&to-date=2024-10-01&skip=0&limit=0", nil)
	req.Header.Set("Content-Type", "application/json")
	rec := httptest.NewRecorder()
	Router.Engine.ServeHTTP(rec, req)

	assert.Equal(t, http.StatusOK, rec.Code)
}
func TestListObjectionCodeHandlerType12_Success(t *testing.T) {
	// Router = BootstrapTestApp(t)

	req := httptest.NewRequest("GET", "/v1/objection/pao/code?code=078118&type=1&status=paoupdated&from-date=2024-01-01&to-date=2024-10-01&skip=0&limit=0", nil)
	req.Header.Set("Content-Type", "application/json")
	rec := httptest.NewRecorder()
	Router.Engine.ServeHTTP(rec, req)

	assert.Equal(t, http.StatusOK, rec.Code)
}
func TestListObjectionCodeHandler_QueryBindingError(t *testing.T) {

	req := httptest.NewRequest("GET", "/v1/objection/pao/code?code=078118&type=d&status=notclosed&from-date=2024-01-01&to-date=2024-10-01", nil)
	req.Header.Set("Content-Type", "application/json")
	rec := httptest.NewRecorder()
	Router.Engine.ServeHTTP(rec, req)

	assert.Equal(t, http.StatusBadRequest, rec.Code)
}

func TestListObjectionCodeHandler_ValidationError(t *testing.T) {

	req := httptest.NewRequest("GET", "/v1/objection/pao/code?code=078618&type=1&status=losed&from-date=2024-01-01&to-date=2024-10-01", nil)
	req.Header.Set("Content-Type", "application/json")
	rec := httptest.NewRecorder()
	Router.Engine.ServeHTTP(rec, req)

	assert.Equal(t, http.StatusUnprocessableEntity, rec.Code)
}

//ListObjectionPraoCodeHandler--------------------------------------------------------------------------------------------------------

func TestListObjectionPraoCodeHandlerType2_Success(t *testing.T) {
	// Router = BootstrapTestApp(t)

	req := httptest.NewRequest("GET", "/v1/objection/prao/code?code=078118&type=2&status=notclosed&from-date=2024-01-01&to-date=2024-10-01&skip=0&limit=0", nil)
	req.Header.Set("Content-Type", "application/json")
	rec := httptest.NewRecorder()
	Router.Engine.ServeHTTP(rec, req)

	assert.Equal(t, http.StatusOK, rec.Code)
}
func TestListObjectionPraoCodeHandlerType21_Success(t *testing.T) {
	// Router = BootstrapTestApp(t)

	req := httptest.NewRequest("GET", "/v1/objection/prao/code?code=078118&type=2&status=paoupdated&from-date=2024-01-01&to-date=2024-10-01&skip=0&limit=0", nil)
	req.Header.Set("Content-Type", "application/json")
	rec := httptest.NewRecorder()
	Router.Engine.ServeHTTP(rec, req)

	assert.Equal(t, http.StatusOK, rec.Code)
}
func TestListObjectionPraoCodeHandlerType1_Success(t *testing.T) {
	// Router = BootstrapTestApp(t)

	req := httptest.NewRequest("GET", "/v1/objection/prao/code?code=078118&type=1&status=notclosed&from-date=2024-01-01&to-date=2024-10-01", nil)
	req.Header.Set("Content-Type", "application/json")
	rec := httptest.NewRecorder()
	Router.Engine.ServeHTTP(rec, req)

	assert.Equal(t, http.StatusOK, rec.Code)
}
func TestListObjectionPraoCodeHandlerType12_Success(t *testing.T) {
	// Router = BootstrapTestApp(t)

	req := httptest.NewRequest("GET", "/v1/objection/prao/code?code=078118&type=1&status=paoupdated&from-date=2024-01-01&to-date=2024-10-01", nil)
	req.Header.Set("Content-Type", "application/json")
	rec := httptest.NewRecorder()
	Router.Engine.ServeHTTP(rec, req)

	assert.Equal(t, http.StatusOK, rec.Code)
}
func TestListObjectionPraoCodeHandler_QueryBindingError(t *testing.T) {

	req := httptest.NewRequest("GET", "/v1/objection/prao/code?code=078118&type=d&status=notclosed&from-date=2024-01-01&to-date=2024-10-01&skip=0&limit=0", nil)
	req.Header.Set("Content-Type", "application/json")
	rec := httptest.NewRecorder()
	Router.Engine.ServeHTTP(rec, req)

	assert.Equal(t, http.StatusBadRequest, rec.Code)
}

func TestListObjectionPraoCodeHandler_ValidationError(t *testing.T) {

	req := httptest.NewRequest("GET", "/v1/objection/prao/code?code=123478&type=1&status=ed&from-date=2024-01-01&to-date=2024-10-01", nil)
	req.Header.Set("Content-Type", "application/json")
	rec := httptest.NewRecorder()
	Router.Engine.ServeHTTP(rec, req)

	assert.Equal(t, http.StatusUnprocessableEntity, rec.Code)
}

//ListObjectionPaoReportHandler------------------------------------------------------------------------------------------------

func TestListObjectionPaoReportHandlerType1_Success(t *testing.T) {
	// Router = BootstrapTestApp(t)

	req := httptest.NewRequest("GET", "/v1/objection/pao/report?code=078109&from-date=2023-01-01&to-date=2024-12-25&status=created&type=1&skip=0&limit=0", nil)
	req.Header.Set("Content-Type", "application/json")
	rec := httptest.NewRecorder()
	Router.Engine.ServeHTTP(rec, req)

	assert.Equal(t, http.StatusOK, rec.Code)
}
func TestListObjectionPaoReportHandlerType12_Success(t *testing.T) {
	// Router = BootstrapTestApp(t)

	req := httptest.NewRequest("GET", "/v1/objection/pao/report?code=078109&from-date=2023-01-01&to-date=2024-12-25&status=notclosed&type=1&skip=0&limit=0", nil)
	req.Header.Set("Content-Type", "application/json")
	rec := httptest.NewRecorder()
	Router.Engine.ServeHTTP(rec, req)

	assert.Equal(t, http.StatusOK, rec.Code)
}
func TestListObjectionPaoReportHandlerType2_Success(t *testing.T) {
	// Router = BootstrapTestApp(t)

	req := httptest.NewRequest("GET", "/v1/objection/pao/report?code=102604&from-date=2023-01-01&to-date=2024-12-25&status=created&type=2", nil)
	req.Header.Set("Content-Type", "application/json")
	rec := httptest.NewRecorder()
	Router.Engine.ServeHTTP(rec, req)

	assert.Equal(t, http.StatusOK, rec.Code)
}
func TestListObjectionPaoReportHandlerType21_Success(t *testing.T) {
	// Router = BootstrapTestApp(t)

	req := httptest.NewRequest("GET", "/v1/objection/pao/report?code=102604&from-date=2023-01-01&to-date=2024-12-25&status=notclosed&type=2", nil)
	req.Header.Set("Content-Type", "application/json")
	rec := httptest.NewRecorder()
	Router.Engine.ServeHTTP(rec, req)

	assert.Equal(t, http.StatusOK, rec.Code)
}
func TestListObjectionPaoReportHandler_QueryBindingError(t *testing.T) {

	req := httptest.NewRequest("GET", "/v1/objection/pao/report?code=078118&type=1&status=notclosed&from-date=2024-01-01&to-date=2024-10-01&skip=d&limit=0", nil)
	req.Header.Set("Content-Type", "application/json")
	rec := httptest.NewRecorder()
	Router.Engine.ServeHTTP(rec, req)

	assert.Equal(t, http.StatusBadRequest, rec.Code)
}

func TestListObjectionPaoReportHandler_ValidationError(t *testing.T) {

	req := httptest.NewRequest("GET", "/v1/objection/pao/report?code=078618&type=1&status=losed&from-date=2024-01-01&to-date=2024-10-01", nil)
	req.Header.Set("Content-Type", "application/json")
	rec := httptest.NewRecorder()
	Router.Engine.ServeHTTP(rec, req)

	assert.Equal(t, http.StatusUnprocessableEntity, rec.Code)
}

//ListObjectionPraoReportHandler------------------------------------------------------------------------------------

func TestListObjectionPraoReportHandlerType2_Success(t *testing.T) {
	// Router = BootstrapTestApp(t)

	req := httptest.NewRequest("GET", "/v1/objection/prao/report?code=078109&type=2&status=notclosed&from-date=2024-01-01&to-date=2024-12-28&skip=0&limit=0", nil)
	req.Header.Set("Content-Type", "application/json")
	rec := httptest.NewRecorder()
	Router.Engine.ServeHTTP(rec, req)

	assert.Equal(t, http.StatusOK, rec.Code)
}
func TestListObjectionPraoReportHandlerType21_Success(t *testing.T) {
	// Router = BootstrapTestApp(t)

	req := httptest.NewRequest("GET", "/v1/objection/prao/report?code=078109&type=2&status=paoupdated&from-date=2024-01-01&to-date=2024-12-28&skip=0&limit=0", nil)
	req.Header.Set("Content-Type", "application/json")
	rec := httptest.NewRecorder()
	Router.Engine.ServeHTTP(rec, req)

	assert.Equal(t, http.StatusOK, rec.Code)
}
func TestListObjectionPraoReportHandlerType1_Success(t *testing.T) {
	// Router = BootstrapTestApp(t)

	req := httptest.NewRequest("GET", "/v1/objection/prao/report?code=078118&type=1&status=notclosed&from-date=2024-01-01&to-date=2024-10-01", nil)
	req.Header.Set("Content-Type", "application/json")
	rec := httptest.NewRecorder()
	Router.Engine.ServeHTTP(rec, req)

	assert.Equal(t, http.StatusOK, rec.Code)
}
func TestListObjectionPraoReportHandlerType12_Success(t *testing.T) {
	// Router = BootstrapTestApp(t)

	req := httptest.NewRequest("GET", "/v1/objection/prao/report?code=078118&type=1&status=paoupdated&from-date=2024-01-01&to-date=2024-10-01", nil)
	req.Header.Set("Content-Type", "application/json")
	rec := httptest.NewRecorder()
	Router.Engine.ServeHTTP(rec, req)

	assert.Equal(t, http.StatusOK, rec.Code)
}
func TestListObjectionPraoReportHandler_QueryBindingError(t *testing.T) {

	req := httptest.NewRequest("GET", "/v1/objection/prao/report?code=078118&type=d&status=notclosed&from-date=2024-01-01&to-date=2024-10-01", nil)
	req.Header.Set("Content-Type", "application/json")
	rec := httptest.NewRecorder()
	Router.Engine.ServeHTTP(rec, req)

	assert.Equal(t, http.StatusBadRequest, rec.Code)
}

func TestListObjectionPraoReportHandler_ValidationError(t *testing.T) {

	req := httptest.NewRequest("GET", "/v1/objection/prao/report?code=078618&type=1&status=ed&from-date=2024-01-01&to-date=2024-10-01", nil)
	req.Header.Set("Content-Type", "application/json")
	rec := httptest.NewRecorder()
	Router.Engine.ServeHTTP(rec, req)

	assert.Equal(t, http.StatusUnprocessableEntity, rec.Code)
}
