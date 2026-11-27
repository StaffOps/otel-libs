package demokit

import (
	"context"
	"crypto/rand"
	"errors"
	"fmt"

	"go.opentelemetry.io/otel/attribute"
	semconv "go.opentelemetry.io/otel/semconv/v1.43.0"
	"go.opentelemetry.io/otel/trace"
)

// Pinned error.type literals (DE-D8): unqualified and the same in every
// language, a deliberate deviation from the semconv fully qualified name so
// one query matches every language. The demo error types return them from an
// ErrorType() method, the per-error override the helper's otelhelper.WithSpan
// and otelhelper.RecordFailure read (contract design D15, A45).
const (
	TypeOrderRejected = "OrderRejectedError"
	TypeOrderExists   = "OrderExistsError"
	TypeDemoUnhandled = "DemoUnhandledError"
	outcomeOK         = "ok"
	outcomeError      = "error"
	attrScenario      = "demo.scenario"
	attrOutcome       = "demo.outcome"
	attrErrorType     = "error.type"
)

// OrderRejectedError is the backend's business error (business_error).
type OrderRejectedError struct{ Run string }

func (e *OrderRejectedError) Error() string { return "amount over limit rej-" + e.Run }

// ErrorType is the pinned error.type literal (the helper's override).
func (e *OrderRejectedError) ErrorType() string { return TypeOrderRejected }

// OrderExistsError wraps the driver error of the trigger-raised duplicate
// (db_error); the driver's message is "duplicate order dup-<run>".
type OrderExistsError struct {
	ID  string
	Err error
}

func (e *OrderExistsError) Error() string { return "order " + e.ID + " exists" }

func (e *OrderExistsError) Unwrap() error { return e.Err }

// ErrorType is the pinned error.type literal (the helper's override); it is
// found before the wrapped driver error.
func (e *OrderExistsError) ErrorType() string { return TypeOrderExists }

// DemoUnhandledError is thrown (panicked) by the api in demo.order.accept for
// the unhandled scenario.
type DemoUnhandledError struct{ Run string }

func (e *DemoUnhandledError) Error() string { return "boom unh-" + e.Run }

// ErrorType is the pinned error.type literal (the helper's override).
func (e *DemoUnhandledError) ErrorType() string { return TypeDemoUnhandled }

// ErrorType is the error.type value the helper sets on a span that err marks
// (contract design D15), so the metric attribute (DE-D9) equals the span
// attribute: the first non-empty ErrorType() method along err's Unwrap chain
// (the pinned literals of the demo errors), else semconv.ErrorType, which
// strips a single %w wrapper. "" for a nil error.
func ErrorType(err error) string {
	if err == nil {
		return ""
	}
	for e := err; e != nil; e = errors.Unwrap(e) {
		if o, ok := e.(interface{ ErrorType() string }); ok {
			if t := o.ErrorType(); t != "" {
				return t
			}
		}
	}
	return semconv.ErrorType(err).Value.AsString()
}

// TypeName is the Go type name of the root of err's Unwrap chain (%T): the
// error.type of the spans the demo marks by hand (the worker's demo.drive on a
// transport failure, the api's PRODUCER span, DE-D7 r3.5).
func TypeName(err error) string {
	for {
		next := errors.Unwrap(err)
		if next == nil {
			return fmt.Sprintf("%T", err)
		}
		err = next
	}
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

// Untraced returns ctx with a valid but unsampled local parent span context.
// Under the helper's parent-based sampler (default parentbased_always_on,
// ADR-002) nothing started from it is recorded: the worker's empty
// XREADGROUP polls and the api's readiness probes leave no span (DE-D11
// r3.6). A sampler without the parent-based rule (OTEL_TRACES_SAMPLER=
// always_on) would still record them. Never log with this context: the
// record would carry the throwaway trace id.
func Untraced(ctx context.Context) context.Context {
	var tid trace.TraceID
	var sid trace.SpanID
	_, _ = rand.Read(tid[:])
	_, _ = rand.Read(sid[:])
	return trace.ContextWithSpanContext(ctx, trace.NewSpanContext(trace.SpanContextConfig{TraceID: tid, SpanID: sid}))
}
