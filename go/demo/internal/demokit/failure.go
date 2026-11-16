package demokit

import (
	"context"
	"errors"
	"fmt"
	"log/slog"
	"runtime/debug"

	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/codes"
	"go.opentelemetry.io/otel/trace"
)

// Pinned error.type literals (DE-D8): unqualified and the same in every
// language, a deliberate deviation from the semconv fully qualified name so
// one query matches every language.
const (
	TypeOrderRejected  = "OrderRejectedError"
	TypeOrderExists    = "OrderExistsError"
	TypeDemoUnhandled  = "DemoUnhandledError"
	outcomeOK          = "ok"
	outcomeError       = "error"
	attrScenario       = "demo.scenario"
	attrOutcome        = "demo.outcome"
	attrErrorType      = "error.type"
	attrExceptionType  = "exception.type"
	attrExceptionMsg   = "exception.message"
	attrExceptionStack = "exception.stacktrace"
)

// OrderRejectedError is the backend's business error (business_error).
type OrderRejectedError struct{ Run string }

func (e *OrderRejectedError) Error() string { return "amount over limit rej-" + e.Run }

// OrderExistsError wraps the driver error of the trigger-raised duplicate
// (db_error); the driver's message is "duplicate order dup-<run>".
type OrderExistsError struct {
	ID  string
	Err error
}

func (e *OrderExistsError) Error() string { return "order " + e.ID + " exists" }

func (e *OrderExistsError) Unwrap() error { return e.Err }

// DemoUnhandledError is thrown (panicked) by the api in demo.order.accept for
// the unhandled scenario.
type DemoUnhandledError struct{ Run string }

func (e *DemoUnhandledError) Error() string { return "boom unh-" + e.Run }

// ErrorType maps an error to its pinned error.type literal; any other error
// gets TypeName (the exception type name, DE-D7 r3.5).
func ErrorType(err error) string {
	var (
		rejected  *OrderRejectedError
		exists    *OrderExistsError
		unhandled *DemoUnhandledError
	)
	switch {
	case err == nil:
		return ""
	case errors.As(err, &rejected):
		return TypeOrderRejected
	case errors.As(err, &exists):
		return TypeOrderExists
	case errors.As(err, &unhandled):
		return TypeDemoUnhandled
	}
	return TypeName(err)
}

// TypeName is the Go type name of the root of err's Unwrap chain (%T), the
// error.type of failures without a pinned literal (DE-D7 r3.5).
func TypeName(err error) string {
	for {
		next := errors.Unwrap(err)
		if next == nil {
			return fmt.Sprintf("%T", err)
		}
		err = next
	}
}

// RecordFailure applies the ADR-014 pattern to the owning span (DE-D8): status
// ERROR without description, error.type, one exception event carrying only
// exception.type, then one ERROR log record written while that span is
// current (ctx must be the context returned when span was started), with the
// message as body and exception.type, exception.message and
// exception.stacktrace. The stack trace is debug.Stack() at this call: the
// logging goroutine's stack (ruling DE-O7). It returns the error.type value.
func RecordFailure(ctx context.Context, span trace.Span, logger *slog.Logger, err error) string {
	t := ErrorType(err)
	span.SetStatus(codes.Error, "")
	span.SetAttributes(attribute.String(attrErrorType, t))
	span.AddEvent("exception", trace.WithAttributes(attribute.String(attrExceptionType, t)))
	msg := err.Error()
	logger.ErrorContext(ctx, msg,
		slog.String(attrExceptionType, t),
		slog.String(attrExceptionMsg, msg),
		slog.String(attrExceptionStack, string(debug.Stack())),
	)
	return t
}

// MetricAttributes are the attributes of demo.orders.processed and
// demo.order.duration (DE-D9): demo.scenario, demo.outcome and, on failure
// only, error.type.
func MetricAttributes(scenario string, err error) []attribute.KeyValue {
	if err == nil {
		return []attribute.KeyValue{
			attribute.String(attrScenario, scenario),
			attribute.String(attrOutcome, outcomeOK),
		}
	}
	return []attribute.KeyValue{
		attribute.String(attrScenario, scenario),
		attribute.String(attrOutcome, outcomeError),
		attribute.String(attrErrorType, ErrorType(err)),
	}
}

// Outcome is the demo.outcome value of a span attribute.
func Outcome(failed bool) attribute.KeyValue {
	if failed {
		return attribute.String(attrOutcome, outcomeError)
	}
	return attribute.String(attrOutcome, outcomeOK)
}

// Scenario is the demo.scenario span attribute.
func Scenario(s string) attribute.KeyValue { return attribute.String(attrScenario, s) }
