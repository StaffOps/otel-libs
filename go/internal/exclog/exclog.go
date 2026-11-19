// Package exclog moves the detail of an instrumentation's exception events
// to a correlated log record (ADR-014, contract task A36). It is internal to
// the otel-libs Go modules (the helper's ext modules import it); it is not
// public API.
//
// Cross-module contract: ext modules are released as separate Go modules and
// may be built against a newer or older core of the same major version. The
// exported names Emit, TracerProvider, Option, WithRecordAttributes and
// WithErrorAttributes, and their signatures, are therefore frozen within a
// major version, and across the whole 0.x series too: the surface is
// additive-only (a new Option is fine; a changed or removed name or
// signature is not). contract_test.go asserts the signatures at compile time;
// the release skill checks this before every core tag.
//
// TracerProvider wraps the TracerProvider an instrumentation is given. The
// spans of its tracers turn RecordError(err) into:
//
//   - an "exception" span event with exception.type only (no message, no
//     stack trace), when the span records;
//   - one ERROR log record for err in the span's context (same trace_id and
//     span_id), whether or not the span is sampled, through the helper's log
//     channel (design D15 "Channel": the OTel bridge while a logs exporter is
//     installed, the stdout JSON fallback otherwise), with err's text as body,
//     exception.type, exception.message and exception.stacktrace, and the
//     record attributes of the options (db.system.name, the error's
//     db.response.status_code and error.type). A cancellation
//     (context.Canceled, a gRPC Canceled status) gets the event but no
//     record, as WithSpan; a second RecordError on the same span with the
//     same error (equal, or errors.Is in both directions) is skipped.
//
// Every other span method is the wrapped span's. The rule applies to the
// span value Start returns: code that reads the span back with
// trace.SpanFromContext, or that calls the wrapped provider's tracer
// directly, records through the SDK span and bypasses it (XSAM otelsql and
// redisotel record on the returned value). ext/otelsql uses it for XSAM
// otelsql; ext/otelredis could pass the same wrapper to redisotel.
package exclog

import (
	"context"
	"errors"
	"fmt"

	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/trace"
)

// Emit writes the record for err in ctx with the extra record attributes
// attrs and returns the exception.type it used, the same value the span
// event gets. It writes no record for a cancellation. The helper's core
// package (github.com/staffops/otel-libs/go) sets it in its init; while it
// is nil (the core is not linked) the event gets %T and no record is
// written.
var Emit func(ctx context.Context, err error, attrs []attribute.KeyValue) string

// Event and attribute names (semantic conventions, exceptions).
const (
	eventName        = "exception"
	attrExceptionKey = attribute.Key("exception.type")
)

// Option configures TracerProvider.
type Option func(*config)

type config struct {
	static   []attribute.KeyValue
	errAttrs func(error) []attribute.KeyValue
}

// WithRecordAttributes adds attrs to every record (ext/otelsql:
// db.system.name). The span's own attributes are not read: an unsampled
// span has none.
func WithRecordAttributes(attrs ...attribute.KeyValue) Option {
	return func(c *config) { c.static = append(c.static, attrs...) }
}

// WithErrorAttributes adds f(err) to the record of err (ext/otelsql: the
// D16 db.response.status_code and error.type). f must not panic on a typed
// nil; a panic in it is recovered and adds nothing.
func WithErrorAttributes(f func(error) []attribute.KeyValue) Option {
	return func(c *config) { c.errAttrs = f }
}

// TracerProvider returns tp with RecordError rewritten as described in the
// package documentation.
func TracerProvider(tp trace.TracerProvider, opts ...Option) trace.TracerProvider {
	cfg := &config{}
	for _, o := range opts {
		if o != nil {
			o(cfg)
		}
	}
	return tracerProvider{TracerProvider: tp, cfg: cfg}
}

type tracerProvider struct {
	trace.TracerProvider
	cfg *config
}

func (p tracerProvider) Tracer(name string, opts ...trace.TracerOption) trace.Tracer {
	return tracer{Tracer: p.TracerProvider.Tracer(name, opts...), cfg: p.cfg}
}

type tracer struct {
	trace.Tracer
	cfg *config
}

func (t tracer) Start(ctx context.Context, name string, opts ...trace.SpanStartOption) (context.Context, trace.Span) {
	ctx, s := t.Tracer.Start(ctx, name, opts...)
	// The returned context keeps the SDK span (so the SDK still counts its
	// children): the rule applies to the span value the instrumentation
	// records on, which is what XSAM and redisotel do.
	return ctx, &span{Span: s, ctx: ctx, cfg: t.cfg}
}

// span is a span whose RecordError follows ADR-014. ctx is the context Start
// returned: the record is emitted in it, so it carries the span's ids and
// whatever else the instrumentation's context holds. last is the error
// recorded last (pgx returns the same error from Rows.Err and Close). last
// has no lock: an instrumentation records on one span from one goroutine
// (XSAM records in the call that owns the span; database/sql serializes a
// Rows' Next and Close), as the SDK's RecordError contract assumes.
type span struct {
	trace.Span
	ctx  context.Context
	cfg  *config
	last error
}

// RecordError emits the record and adds the type-only exception event. A nil
// err, or the same error as the previous call, does nothing. The options'
// timestamp is kept; their attributes and a stack trace option are dropped
// (the detail goes to the record).
func (s *span) RecordError(err error, opts ...trace.EventOption) {
	if err == nil || (s.last != nil && sameError(s.last, err)) {
		return
	}
	s.last = err
	typ := ""
	if Emit != nil {
		typ = Emit(s.ctx, err, s.recordAttributes(err))
	}
	if typ == "" {
		typ = fmt.Sprintf("%T", err)
	}
	if !s.IsRecording() {
		return
	}
	// NewEventConfig sets the time to now when the options carry none.
	cfg := trace.NewEventConfig(opts...)
	s.AddEvent(eventName, trace.WithAttributes(attrExceptionKey.String(typ)), trace.WithTimestamp(cfg.Timestamp()))
}

func (s *span) recordAttributes(err error) (attrs []attribute.KeyValue) {
	attrs = append(attrs, s.cfg.static...)
	if s.cfg.errAttrs == nil {
		return attrs
	}
	defer func() {
		if recover() != nil {
			attrs = append([]attribute.KeyValue(nil), s.cfg.static...)
		}
	}()
	return append(attrs, s.cfg.errAttrs(err)...)
}

// sameError reports whether a and b are the same failure: equal values, or
// errors.Is in both directions. A panic (an uncomparable value, a broken Is
// method) counts as different.
func sameError(a, b error) (same bool) {
	defer func() {
		if recover() != nil {
			same = false
		}
	}()
	return a == b || (errors.Is(a, b) && errors.Is(b, a))
}
