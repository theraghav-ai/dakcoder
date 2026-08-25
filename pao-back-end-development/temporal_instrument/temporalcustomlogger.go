package instrument

import (
	"context"
	"fmt"

	apilog "gitlab.cept.gov.in/it-2.0-common/api-log"
	"go.temporal.io/sdk/log"
)

type CustomTemporalLogger struct {
	ctx context.Context
}

func TemporalLoggerAdapter(ctx context.Context) log.Logger {
	return &CustomTemporalLogger{ctx: ctx}
}

func (l *CustomTemporalLogger) Debug(msg string, keyvals ...interface{}) {
	apilog.Debug(l.ctx, formatMessage(msg, keyvals...))
}

func (l *CustomTemporalLogger) Info(msg string, keyvals ...interface{}) {
	apilog.Info(l.ctx, formatMessage(msg, keyvals...))
}

func (l *CustomTemporalLogger) Warn(msg string, keyvals ...interface{}) {
	apilog.Warn(l.ctx, formatMessage(msg, keyvals...))
}

func (l *CustomTemporalLogger) Error(msg string, keyvals ...interface{}) {
	apilog.Error(l.ctx, formatMessage(msg, keyvals...))
}

func formatMessage(msg string, keyvals ...interface{}) string {
	if len(keyvals)%2 != 0 {

		keyvals = append(keyvals, "(missing)")
	}

	formatted := msg
	for i := 0; i < len(keyvals); i += 2 {
		key, ok := keyvals[i].(string)
		if !ok {
			key = "invalid_key"
		}
		formatted += fmt.Sprintf(" | %s=%v", key, keyvals[i+1])
	}
	return formatted
}
