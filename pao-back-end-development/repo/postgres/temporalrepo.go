package repository

import (
	"context"
	"fmt"
	"strconv"

	"github.com/gin-gonic/gin"
	log "gitlab.cept.gov.in/it-2.0-common/api-log"

	commonpb "go.temporal.io/api/common/v1"
	"go.temporal.io/api/enums/v1"
	historypb "go.temporal.io/api/history/v1"
	workflowpb "go.temporal.io/api/workflow/v1"
	workflowservice "go.temporal.io/api/workflowservice/v1"
	"go.temporal.io/sdk/client"
	"go.temporal.io/sdk/converter"

	config "gitlab.cept.gov.in/it-2.0-common/api-config"

	dblib "gitlab.cept.gov.in/it-2.0-common/api-db"
	// log "gitlab.cept.gov.in/it-2.0-common/api-log"
)

type TemporalRepository struct {
	Db  *dblib.DB
	Cfg *config.Config
}

// func (t *TemporalRepository) GetWorkflowDetails(gctx *gin.Context, param any, workflowID string) (StandardizedOutput, error) {
// 	panic("unimplemented")
// }

// func (t *TemporalRepository) GetWorkflowDetails(gctx *gin.Context, param any, workflowID string) (any, any) {
// 	panic("unimplemented")
// }

// NewUserRepository creates a new user repository instance
func NewTemporalRepository(Db *dblib.DB, Cfg *config.Config) *TemporalRepository {
	return &TemporalRepository{
		Db,
		Cfg,
	}
}

type WorkflowStatus string

const (
	StatusRunning        WorkflowStatus = "Running"
	StatusCompleted      WorkflowStatus = "Completed"
	StatusFailed         WorkflowStatus = "Failed"
	StatusTimedOut       WorkflowStatus = "TimedOut"
	StatusCanceled       WorkflowStatus = "Canceled"
	StatusTerminated     WorkflowStatus = "Terminated"
	StatusContinuedAsNew WorkflowStatus = "ContinuedAsNew"
	StatusUnknown        WorkflowStatus = "Unknown"
)

type StandardizedOutput struct {
	WorkflowID string         `json:"workflow_id"`
	RunID      string         `json:"run_id"`
	Status     WorkflowStatus `json:"status"`
	Details    interface{}    `json:"details,omitempty"`
}

func (t *TemporalRepository) GetWorkflowDetails(gctx *gin.Context, c client.Client, workflowID string) (StandardizedOutput, error) {

	var output StandardizedOutput

	ctx := gctx.Request.Context()
	execution, err := getWorkflowExecution(ctx, c, workflowID)
	if err != nil {
		return output, err
	}

	status := getWorkflowStatus(execution)

	if status == StatusRunning {
		output := StandardizedOutput{
			WorkflowID: workflowID,
			RunID:      execution.GetExecution().GetRunId(),
			Status:     StatusRunning,
		}

		return output, nil
	}

	finalEvent, err := getFinalHistoryEvent(ctx, c, workflowID)
	if err != nil {
		return output, err
	}

	output = buildStandardizedOutput(ctx, workflowID, execution.GetExecution().GetRunId(), finalEvent)

	return output, nil
}

func getWorkflowExecution(ctx context.Context, c client.Client, workflowID string) (*workflowpb.WorkflowExecutionInfo, error) {
	search := &workflowservice.ListWorkflowExecutionsRequest{

		Query: fmt.Sprintf("WorkflowId = '%s'", workflowID),
	}

	workflows, err := c.ListWorkflow(ctx, search)
	if err != nil {
		return nil, fmt.Errorf("failed to list workflows: %w", err)
	}

	if len(workflows.Executions) == 0 {
		return nil, fmt.Errorf("no workflow found with ID: %s", workflowID)
	}

	return workflows.Executions[0], nil
}

func getWorkflowStatus(exec *workflowpb.WorkflowExecutionInfo) WorkflowStatus {
	switch exec.GetStatus() {
	case enums.WORKFLOW_EXECUTION_STATUS_RUNNING:
		return StatusRunning
	case enums.WORKFLOW_EXECUTION_STATUS_COMPLETED:
		return StatusCompleted
	case enums.WORKFLOW_EXECUTION_STATUS_FAILED:
		return StatusFailed
	case enums.WORKFLOW_EXECUTION_STATUS_TIMED_OUT:
		return StatusTimedOut
	case enums.WORKFLOW_EXECUTION_STATUS_CANCELED:
		return StatusCanceled
	case enums.WORKFLOW_EXECUTION_STATUS_TERMINATED:
		return StatusTerminated
	case enums.WORKFLOW_EXECUTION_STATUS_CONTINUED_AS_NEW:
		return StatusContinuedAsNew
	default:
		return StatusUnknown
	}
}

func getFinalHistoryEvent(ctx context.Context, c client.Client, workflowID string) (*historypb.HistoryEvent, error) {
	iter := c.GetWorkflowHistory(ctx, workflowID, "", true, enums.HISTORY_EVENT_FILTER_TYPE_ALL_EVENT)

	for iter.HasNext() {
		event, err := iter.Next()
		if err != nil {
			return nil, fmt.Errorf("failed to get next history event: %w", err)
		}

		switch event.GetEventType() {
		case enums.EVENT_TYPE_WORKFLOW_EXECUTION_COMPLETED,
			enums.EVENT_TYPE_WORKFLOW_EXECUTION_FAILED,
			enums.EVENT_TYPE_WORKFLOW_EXECUTION_TIMED_OUT,
			enums.EVENT_TYPE_WORKFLOW_EXECUTION_TERMINATED,
			enums.EVENT_TYPE_WORKFLOW_EXECUTION_CANCELED,
			enums.EVENT_TYPE_WORKFLOW_EXECUTION_CONTINUED_AS_NEW:
			return event, nil
		}
	}

	return nil, fmt.Errorf("no final event found for workflow")
}

func buildStandardizedOutput(ctx context.Context, workflowID, runID string, finalEvent *historypb.HistoryEvent) StandardizedOutput {
	var output StandardizedOutput
	output.WorkflowID = workflowID
	output.RunID = runID

	switch finalEvent.GetEventType() {
	case enums.EVENT_TYPE_WORKFLOW_EXECUTION_COMPLETED:
		result := finalEvent.GetWorkflowExecutionCompletedEventAttributes().GetResult()
		decodedResult := decodePayload(ctx, result)
		output.Status = StatusCompleted
		output.Details = decodedResult

	case enums.EVENT_TYPE_WORKFLOW_EXECUTION_FAILED:
		failure := finalEvent.GetWorkflowExecutionFailedEventAttributes().GetFailure()
		output.Status = StatusFailed
		output.Details = failure

	case enums.EVENT_TYPE_WORKFLOW_EXECUTION_TIMED_OUT:
		timeoutType := finalEvent.GetWorkflowExecutionTimedOutEventAttributes().GetRetryState()
		output.Status = StatusTimedOut
		output.Details = timeoutType

	case enums.EVENT_TYPE_WORKFLOW_EXECUTION_TERMINATED:
		reason := finalEvent.GetWorkflowExecutionTerminatedEventAttributes().GetReason()
		output.Status = StatusTerminated
		output.Details = reason

	case enums.EVENT_TYPE_WORKFLOW_EXECUTION_CANCELED:
		details := finalEvent.GetWorkflowExecutionCanceledEventAttributes().GetDetails()
		decodedDetails := decodePayload(ctx, details)
		output.Status = StatusCanceled
		output.Details = decodedDetails

	case enums.EVENT_TYPE_WORKFLOW_EXECUTION_CONTINUED_AS_NEW:
		newRunID := finalEvent.GetWorkflowExecutionContinuedAsNewEventAttributes().GetNewExecutionRunId()
		output.Status = StatusContinuedAsNew
		output.Details = newRunID

	default:
		output.Status = StatusUnknown
	}

	return output
}
func decodePayload(ctx context.Context, payloads *commonpb.Payloads) interface{} {
	if payloads == nil || len(payloads.Payloads) == 0 {
		return nil
	}

	dc := converter.GetDefaultDataConverter()

	var raw interface{}
	err := dc.FromPayload(payloads.Payloads[0], &raw)
	if err != nil {
		log.Debug(ctx, "Failed to decode payload to raw: %v", err)
		return string(payloads.Payloads[0].GetData()) // fallback to raw data
	}

	// If raw is a string wrapped with quotes, unquote it
	if str, ok := raw.(string); ok {
		unquoted, err := strconv.Unquote(str)
		if err == nil {
			return unquoted
		}
		// If unquoting fails, return as-is
		return str
	}

	return raw
}
