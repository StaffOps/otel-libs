package otelhelper

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"log/slog"
	"net/http"
	"os"
	"runtime"
	"strings"
	"sync"
	"testing"
	"time"

	"go.opentelemetry.io/otel"
	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/codes"
	sdklog "go.opentelemetry.io/otel/sdk/log"
	sdktrace "go.opentelemetry.io/otel/sdk/trace"
	"go.opentelemetry.io/otel/sdk/trace/tracetest"
	"go.opentelemetry.io/otel/trace"
	grpccodes "google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
)

// R11, ADR-016, design D15 "Tests per language" (Go).

// failureEnv isolates a test: no Setup (records go to the stdout fallback),
// stdout captured, and an in-memory tracer.
type failureEnv struct {
	tracer trace.Tracer
	spans  *tracetest.SpanRecorder
	stop   func() string
}

func newFailureEnv(t *testing.T) *failureEnv {
	t.Helper()
	logsTest(t)
	stop := captureStdout(t)
	rec := tracetest.NewSpanRecorder()
	tp := sdktrace.NewTracerProvider(sdktrace.WithSpanProcessor(rec), sdktrace.WithSampler(sdktrace.ParentBased(sdktrace.AlwaysSample())))
	t.Cleanup(func() { _ = tp.Shutdown(context.Background()) })
	return &failureEnv{tracer: tp.Tracer("failure-test"), spans: rec, stop: stop}
}

// records returns the ERROR stdout lines written so far (stops the capture).
func (e *failureEnv) records(t *testing.T) []map[string]any {
	t.Helper()
	var out []map[string]any
	for _, line := range strings.Split(strings.TrimSpace(e.stop()), "\n") {
		if line == "" {
			continue
		}
		var m map[string]any
		if err := json.Unmarshal([]byte(line), &m); err != nil {
			t.Fatalf("stdout line not JSON: %v (%q)", err, line)
		}
		if m["level"] == "ERROR" {
			out = append(out, m)
		}
	}
	return out
}

func (e *failureEnv) span(t *testing.T, name string) sdktrace.ReadOnlySpan {
	t.Helper()
	for _, s := range e.spans.Ended() {
		if s.Name() == name {
			return s
		}
	}
	t.Fatalf("span %q not ended (have %d)", name, len(e.spans.Ended()))
	return nil
}

func failSpanAttr(s sdktrace.ReadOnlySpan, key string) (string, bool) {
	for _, kv := range s.Attributes() {
		if string(kv.Key) == key {
			return kv.Value.Emit(), true
		}
	}
	return "", false
}

// assertFailed checks status ERROR with an empty description, error.type and
// no event.
func assertFailed(t *testing.T, s sdktrace.ReadOnlySpan, wantType string) {
	t.Helper()
	if s.Status().Code != codes.Error || s.Status().Description != "" {
		t.Errorf("span %s status = %v %q, want Error with empty description", s.Name(), s.Status().Code, s.Status().Description)
	}
	if got, _ := failSpanAttr(s, "error.type"); got != wantType {
		t.Errorf("span %s error.type = %q, want %q", s.Name(), got, wantType)
	}
	if n := len(s.Events()); n != 0 {
		t.Errorf("span %s has %d events, want none", s.Name(), n)
	}
}

func assertUnset(t *testing.T, s sdktrace.ReadOnlySpan) {
	t.Helper()
	if s.Status().Code != codes.Unset {
		t.Errorf("span %s status = %v, want Unset", s.Name(), s.Status().Code)
	}
	if _, ok := failSpanAttr(s, "error.type"); ok {
		t.Errorf("span %s has error.type", s.Name())
	}
}

func assertRecordOf(t *testing.T, rec map[string]any, s sdktrace.ReadOnlySpan, wantType, wantMsg string) {
	t.Helper()
	if rec["msg"] != wantMsg || rec["exception.message"] != wantMsg || rec["exception.type"] != wantType {
		t.Errorf("record = %v, want msg/exception.message %q and exception.type %q", rec, wantMsg, wantType)
	}
	if st, _ := rec["exception.stacktrace"].(string); !strings.Contains(st, "goroutine") {
		t.Errorf("exception.stacktrace is not a Go stack: %q", st)
	}
	if rec["trace_id"] != s.SpanContext().TraceID().String() || rec["span_id"] != s.SpanContext().SpanID().String() {
		t.Errorf("record ids %v/%v, want the span's %s/%s", rec["trace_id"], rec["span_id"], s.SpanContext().TraceID(), s.SpanContext().SpanID())
	}
}

type typedErr struct{ msg string }

func (e *typedErr) Error() string     { return e.msg }
func (e *typedErr) ErrorType() string { return "OrderRejectedError" }

type plainErr struct{ msg string }

func (e *plainErr) Error() string { return e.msg }

// Failure: ERROR, empty description, error.type, no event, one record with the
// span's ids and the three attributes on stdout, error returned by identity.
func TestWithSpan_FailureMarksSpanAndRecordsOnce(t *testing.T) {
	env := newFailureEnv(t)
	want := &plainErr{"card declined 4111"}
	got := WithSpan(context.Background(), env.tracer, "op", func(context.Context) error { return want })
	if got != error(want) {
		t.Fatalf("returned %v, want the same error value", got)
	}
	s := env.span(t, "op")
	assertFailed(t, s, "*otelhelper.plainErr")
	recs := env.records(t)
	if len(recs) != 1 {
		t.Fatalf("want 1 record, got %d: %v", len(recs), recs)
	}
	assertRecordOf(t, recs[0], s, "*otelhelper.plainErr", "card declined 4111")
	if recs[0]["logger"] != "otel-helper" {
		t.Errorf("stdout failure record logger = %v, want otel-helper", recs[0]["logger"])
	}
	if !strings.Contains(recs[0]["exception.stacktrace"].(string), "WithSpan") {
		t.Error("the stack is taken at the marking point and shows the WithSpan frames")
	}
}

// Success: Unset, no record, never Ok.
func TestWithSpan_SuccessStaysUnset(t *testing.T) {
	env := newFailureEnv(t)
	if err := WithSpan(context.Background(), env.tracer, "ok", func(context.Context) error { return nil }); err != nil {
		t.Fatal(err)
	}
	assertUnset(t, env.span(t, "ok"))
	if recs := env.records(t); len(recs) != 0 {
		t.Errorf("success emitted records: %v", recs)
	}
}

// fn receives the span's context, and opts reach tracer.Start (a root).
func TestWithSpan_ContextAndOptions(t *testing.T) {
	env := newFailureEnv(t)
	ctx, parent := env.tracer.Start(context.Background(), "parent")
	var inner trace.SpanContext
	_ = WithSpan(ctx, env.tracer, "root", func(c context.Context) error {
		inner = trace.SpanContextFromContext(c)
		return nil
	}, trace.WithNewRoot(), trace.WithSpanKind(trace.SpanKindConsumer))
	parent.End()
	s := env.span(t, "root")
	if !inner.Equal(s.SpanContext()) {
		t.Error("fn did not receive the span's context")
	}
	if s.Parent().IsValid() || s.SpanKind() != trace.SpanKindConsumer {
		t.Errorf("options not applied: parent %v kind %v", s.Parent(), s.SpanKind())
	}
	env.records(t)
}

// The ErrorType() override wins over the type name, also through %w; a method
// with another signature is ignored.
type intTypeErr struct{}

func (intTypeErr) Error() string  { return "int" }
func (intTypeErr) ErrorType() int { return 7 }

func TestWithSpan_OverrideWins(t *testing.T) {
	env := newFailureEnv(t)
	_ = WithSpan(context.Background(), env.tracer, "direct", func(context.Context) error { return &typedErr{"rejected"} })
	_ = WithSpan(context.Background(), env.tracer, "wrapped", func(context.Context) error {
		return fmt.Errorf("step: %w", &typedErr{"rejected"})
	})
	_ = WithSpan(context.Background(), env.tracer, "other-signature", func(context.Context) error { return intTypeErr{} })
	_ = WithSpan(context.Background(), env.tracer, "single-wrap", func(context.Context) error {
		return fmt.Errorf("x: %w", &plainErr{"p"})
	})
	assertFailed(t, env.span(t, "direct"), "OrderRejectedError")
	assertFailed(t, env.span(t, "wrapped"), "OrderRejectedError")
	assertFailed(t, env.span(t, "other-signature"), "github.com/staffops/otel-libs/go.intTypeErr")
	assertFailed(t, env.span(t, "single-wrap"), "*otelhelper.plainErr")
	recs := env.records(t)
	if len(recs) != 4 || recs[0]["exception.type"] != "OrderRejectedError" {
		t.Errorf("records: %v", recs)
	}
}

// Cancellation: Unset, no record, error returned; a deadline is a failure.
func TestWithSpan_CancellationAndTimeout(t *testing.T) {
	env := newFailureEnv(t)
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	cases := map[string]error{
		"canceled":         ctx.Err(),
		"wrapped-canceled": fmt.Errorf("query: %w", ctx.Err()),
		"grpc-canceled":    status.Error(grpccodes.Canceled, "client went away"),
		"joined-canceled":  errors.Join(context.Canceled, status.Error(grpccodes.Canceled, "x")),
	}
	for name, e := range cases {
		if got := WithSpan(context.Background(), env.tracer, name, func(context.Context) error { return e }); got != e {
			t.Errorf("%s: returned %v, want the same value", name, got)
		}
	}
	dctx, dcancel := context.WithTimeout(context.Background(), time.Nanosecond)
	defer dcancel()
	<-dctx.Done()
	_ = WithSpan(context.Background(), env.tracer, "deadline", func(context.Context) error { return dctx.Err() })
	_ = WithSpan(context.Background(), env.tracer, "joined-own", func(context.Context) error {
		return errors.Join(errors.New("own failure"), context.Canceled)
	})
	_ = WithSpan(context.Background(), env.tracer, "grpc-deadline", func(context.Context) error {
		return status.Error(grpccodes.DeadlineExceeded, "slow")
	})
	for name := range cases {
		assertUnset(t, env.span(t, name))
	}
	assertFailed(t, env.span(t, "deadline"), "context.deadlineExceededError")
	assertFailed(t, env.span(t, "joined-own"), "*errors.joinError")
	assertFailed(t, env.span(t, "grpc-deadline"), "*status.Error")
	if recs := env.records(t); len(recs) != 3 {
		t.Errorf("want 3 records (deadline, joined-own, grpc-deadline), got %d: %v", len(recs), recs)
	}
}

// Nested through 3 helper spans: every span ERROR, one record per span, each
// in its own span context (design D15 "Nesting", owner ruling 2026-11-17).
func TestWithSpan_NestedOneRecordPerSpan(t *testing.T) {
	env := newFailureEnv(t)
	want := errors.New("db down")
	got := WithSpan(context.Background(), env.tracer, "a", func(ctx context.Context) error {
		return WithSpan(ctx, env.tracer, "b", func(ctx context.Context) error {
			return WithSpan(ctx, env.tracer, "c", func(context.Context) error { return want })
		})
	})
	if got != want {
		t.Fatalf("returned %v", got)
	}
	recs := env.records(t)
	if len(recs) != 3 {
		t.Fatalf("want 3 records, got %d", len(recs))
	}
	// Innermost first: c ends (and records) before b, b before a.
	for i, name := range []string{"c", "b", "a"} {
		s := env.span(t, name)
		assertFailed(t, s, "*errors.errorString")
		assertRecordOf(t, recs[i], s, "*errors.errorString", "db down")
	}
}

// Wrapped with %w through 3 layers: 3 records, each with its layer's text
// and span ids; error.type strips the single-%w wrappers.
func TestWithSpan_WrappedThreeLayers(t *testing.T) {
	env := newFailureEnv(t)
	base := &plainErr{"timeout talking to db"}
	_ = WithSpan(context.Background(), env.tracer, "l1", func(ctx context.Context) error {
		err := WithSpan(ctx, env.tracer, "l2", func(ctx context.Context) error {
			err := WithSpan(ctx, env.tracer, "l3", func(context.Context) error { return base })
			return fmt.Errorf("repo: %w", err)
		})
		return fmt.Errorf("service: %w", err)
	})
	recs := env.records(t)
	if len(recs) != 3 {
		t.Fatalf("want 3 records, got %d: %v", len(recs), recs)
	}
	msgs := []string{"timeout talking to db", "repo: timeout talking to db", "service: repo: timeout talking to db"}
	for i, name := range []string{"l3", "l2", "l1"} {
		s := env.span(t, name)
		assertFailed(t, s, "*otelhelper.plainErr")
		assertRecordOf(t, recs[i], s, "*otelhelper.plainErr", msgs[i])
	}
}

// A nested root span (new trace): one record per span, each in its trace.
func TestWithSpan_NestedRootOneRecordPerTrace(t *testing.T) {
	env := newFailureEnv(t)
	want := errors.New("batch item failed")
	_ = WithSpan(context.Background(), env.tracer, "outer", func(ctx context.Context) error {
		return WithSpan(ctx, env.tracer, "root", func(context.Context) error { return want }, trace.WithNewRoot())
	})
	outer, root := env.span(t, "outer"), env.span(t, "root")
	if outer.SpanContext().TraceID() == root.SpanContext().TraceID() {
		t.Fatal("WithNewRoot did not start a new trace")
	}
	recs := env.records(t)
	if len(recs) != 2 {
		t.Fatalf("want 2 records, got %d", len(recs))
	}
	assertRecordOf(t, recs[0], root, "*errors.errorString", "batch item failed")
	assertRecordOf(t, recs[1], outer, "*errors.errorString", "batch item failed")
}

// RecordFailure: marks the ctx span and always records in its context (also a
// span that is not the current one of the caller); returning the same error
// from the enclosing WithSpan records it again for that span.
func TestRecordFailure_HandledFailure(t *testing.T) {
	env := newFailureEnv(t)
	handled := &typedErr{"amount over limit"}
	var other trace.Span
	err := WithSpan(context.Background(), env.tracer, "outer", func(ctx context.Context) error {
		_, other = env.tracer.Start(context.Background(), "other")
		RecordFailure(trace.ContextWithSpan(ctx, other), &plainErr{"other span"})
		other.End()
		RecordFailure(ctx, handled)
		return handled
	})
	if err != error(handled) {
		t.Fatal("not propagated")
	}
	RecordFailure(context.Background(), nil) // no-op
	outer, o := env.span(t, "outer"), env.span(t, "other")
	assertFailed(t, outer, "OrderRejectedError")
	assertFailed(t, o, "*otelhelper.plainErr")
	recs := env.records(t)
	if len(recs) != 3 {
		t.Fatalf("want 3 records, got %d: %v", len(recs), recs)
	}
	assertRecordOf(t, recs[0], o, "*otelhelper.plainErr", "other span")
	assertRecordOf(t, recs[1], outer, "OrderRejectedError", "amount over limit")
	assertRecordOf(t, recs[2], outer, "OrderRejectedError", "amount over limit")
}

// RecordFailure marks a canceled error too (it marks whatever it is given),
// and works outside any WithSpan.
func TestRecordFailure_NoCancellationRuleOutsideWithSpan(t *testing.T) {
	env := newFailureEnv(t)
	ctx, span := env.tracer.Start(context.Background(), "plain")
	RecordFailure(ctx, context.Canceled)
	span.End()
	assertFailed(t, env.span(t, "plain"), "*errors.errorString")
	if recs := env.records(t); len(recs) != 1 {
		t.Errorf("want 1 record, got %d", len(recs))
	}
}

// An unsampled parent: the span is not recording, the record still carries
// its ids.
func TestWithSpan_UnsampledParentStillRecords(t *testing.T) {
	env := newFailureEnv(t)
	tid, _ := trace.TraceIDFromHex("4bf92f3577b34da6a3ce929d0e0e4736")
	sid, _ := trace.SpanIDFromHex("00f067aa0ba902b7")
	parent := trace.ContextWithRemoteSpanContext(context.Background(), trace.NewSpanContext(trace.SpanContextConfig{TraceID: tid, SpanID: sid, Remote: true}))
	var sc trace.SpanContext
	_ = WithSpan(parent, env.tracer, "unsampled", func(ctx context.Context) error {
		sc = trace.SpanContextFromContext(ctx)
		if trace.SpanFromContext(ctx).IsRecording() {
			t.Error("span under an unsampled parent should not record")
		}
		return errors.New("dropped span, kept record")
	})
	recs := env.records(t)
	if len(recs) != 1 || recs[0]["trace_id"] != tid.String() || recs[0]["span_id"] != sc.SpanID().String() {
		t.Errorf("want 1 record with the unsampled span's ids, got %v", recs)
	}
}

// Span marking does not depend on the log pipeline: with logs exported via
// the bridge the span is marked and the record carries the span context.
type failureLogRecorder struct {
	mu   sync.Mutex
	recs []sdklog.Record
}

func (r *failureLogRecorder) Export(_ context.Context, records []sdklog.Record) error {
	r.mu.Lock()
	defer r.mu.Unlock()
	for _, rec := range records {
		r.recs = append(r.recs, rec.Clone())
	}
	return nil
}
func (r *failureLogRecorder) Shutdown(context.Context) error   { return nil }
func (r *failureLogRecorder) ForceFlush(context.Context) error { return nil }

func TestWithSpan_RecordThroughBridge(t *testing.T) {
	env := newFailureEnv(t)
	rec := &failureLogRecorder{}
	otlpLogExporter = func(context.Context, *signalExport) (sdklog.Exporter, error) { return rec, nil }
	shutdown := setupLogs(t, WithEndpoint("http://127.0.0.1:1"))
	_ = WithSpan(context.Background(), env.tracer, "bridged", func(context.Context) error { return &typedErr{"rejected r-1"} })
	if err := shutdown(context.Background()); err != nil {
		t.Fatal(err)
	}
	s := env.span(t, "bridged")
	assertFailed(t, s, "OrderRejectedError")
	if recs := env.records(t); len(recs) != 0 {
		t.Errorf("logs exporter active: no stdout record expected, got %v", recs)
	}
	rec.mu.Lock()
	defer rec.mu.Unlock()
	if len(rec.recs) != 1 {
		t.Fatalf("want 1 exported record, got %d", len(rec.recs))
	}
	r := rec.recs[0]
	if r.TraceID() != s.SpanContext().TraceID() || r.SpanID() != s.SpanContext().SpanID() {
		t.Errorf("exported record ids %s/%s, want the span's", r.TraceID(), r.SpanID())
	}
	r.WalkAttributes(func(kv attribute.KeyValue) bool {
		if kv.Key == "logger" {
			t.Error("the stdout logger field must not be exported as an attribute")
		}
		return true
	})
	if r.SeverityText() != "ERROR" || r.Body().AsString() != "rejected r-1" {
		t.Errorf("severity %q body %q", r.SeverityText(), r.Body().AsString())
	}
	got := map[string]string{}
	r.WalkAttributes(func(kv attribute.KeyValue) bool { got[string(kv.Key)] = kv.Value.AsString(); return true })
	if got["exception.type"] != "OrderRejectedError" || got["exception.message"] != "rejected r-1" || !strings.Contains(got["exception.stacktrace"], "goroutine") {
		t.Errorf("exported attributes %v", got)
	}
}

// With OTEL_LOGS_EXPORTER=none (no log pipeline) the span is still marked and
// the record goes to the stdout fallback, never to a provider.
func TestWithSpan_MarksWithoutLogPipeline(t *testing.T) {
	env := newFailureEnv(t)
	t.Setenv(EnvLogsExporter, "none")
	shutdown := setupLogs(t)
	defer shutdown(context.Background())
	_ = WithSpan(context.Background(), env.tracer, "no-logs", func(context.Context) error { return errors.New("x") })
	assertFailed(t, env.span(t, "no-logs"), "*errors.errorString")
	if logsBridged() {
		t.Fatal("a log bridge is installed with OTEL_LOGS_EXPORTER=none")
	}
	recs := env.records(t)
	if len(recs) != 1 {
		t.Fatalf("want the record on stdout, got %d lines", len(recs))
	}
	assertRecordOf(t, recs[0], env.span(t, "no-logs"), "*errors.errorString", "x")
}

// A user-set Ok is final: the API keeps Ok, error.type and the record are
// still added (documented behavior).
func TestWithSpan_OkIsFinal(t *testing.T) {
	env := newFailureEnv(t)
	_ = WithSpan(context.Background(), env.tracer, "ok-first", func(ctx context.Context) error {
		trace.SpanFromContext(ctx).SetStatus(codes.Ok, "")
		return errors.New("after ok")
	})
	s := env.span(t, "ok-first")
	if s.Status().Code != codes.Ok {
		t.Errorf("status = %v, want Ok kept", s.Status().Code)
	}
	if got, _ := failSpanAttr(s, "error.type"); got != "*errors.errorString" {
		t.Errorf("error.type = %q", got)
	}
	if recs := env.records(t); len(recs) != 1 {
		t.Errorf("want 1 record, got %d", len(recs))
	}
}

// Panics: recovered, marked, re-panicked with the same value.
func recoverValue(f func()) (r any) {
	defer func() { r = recover() }()
	f()
	return nil
}

func TestWithSpan_PanicSliceReRaisedByIdentity(t *testing.T) {
	env := newFailureEnv(t)
	value := []int{1, 2, 3}
	r := recoverValue(func() {
		_ = WithSpan(context.Background(), env.tracer, "panics", func(context.Context) error { panic(value) })
	})
	got, ok := r.([]int)
	if !ok || len(got) != 3 || &got[0] != &value[0] {
		t.Fatalf("re-panicked %v, want the same slice", r)
	}
	s := env.span(t, "panics")
	assertFailed(t, s, "[]int")
	recs := env.records(t)
	if len(recs) != 1 {
		t.Fatalf("want 1 record, got %d", len(recs))
	}
	assertRecordOf(t, recs[0], s, "[]int", "[1 2 3]")
	if !strings.Contains(recs[0]["exception.stacktrace"].(string), "TestWithSpan_PanicSliceReRaisedByIdentity") {
		t.Error("the stack is taken at the recover and includes the panic site")
	}
}

// A panic crossing two spans: one record per span, re-panicked unchanged.
func TestWithSpan_NestedPanics(t *testing.T) {
	env := newFailureEnv(t)
	nested := func(value any) {
		_ = recoverValue(func() {
			_ = WithSpan(context.Background(), env.tracer, "outer", func(ctx context.Context) error {
				return WithSpan(ctx, env.tracer, "inner", func(context.Context) error { panic(value) })
			})
		})
	}
	nested("boom")
	nested([]int{1})
	if recs := env.records(t); len(recs) != 4 {
		t.Errorf("want 4 records (2 per panic), got %d", len(recs))
	}
}

// A panic with an error value uses the error rules: the override, and the
// cancellation rule.
func TestWithSpan_PanicWithErrorValue(t *testing.T) {
	env := newFailureEnv(t)
	value := &typedErr{"boom unh-1"}
	if r := recoverValue(func() {
		_ = WithSpan(context.Background(), env.tracer, "err-panic", func(context.Context) error { panic(value) })
	}); r != any(value) {
		t.Fatalf("re-panicked %v", r)
	}
	if r := recoverValue(func() {
		_ = WithSpan(context.Background(), env.tracer, "cancel-panic", func(context.Context) error { panic(context.Canceled) })
	}); r != any(context.Canceled) {
		t.Fatalf("re-panicked %v", r)
	}
	if r := recoverValue(func() {
		_ = WithSpan(context.Background(), env.tracer, "nil-panic", func(context.Context) error { panic(nil) })
	}); r == nil {
		t.Fatal("panic(nil) value lost")
	}
	assertFailed(t, env.span(t, "err-panic"), "OrderRejectedError")
	assertUnset(t, env.span(t, "cancel-panic"))
	assertFailed(t, env.span(t, "nil-panic"), "*runtime.PanicNilError")
	if recs := env.records(t); len(recs) != 2 || recs[0]["msg"] != "boom unh-1" {
		t.Errorf("records: %v", recs)
	}
}

// A struct whose interface field holds a slice: == panics; nested 2 deep no
// crash; one record per span.
type sliceHolderErr struct{ detail any }

func (sliceHolderErr) Error() string { return "slice holder" }

func TestWithSpan_UncomparableErrorNoCrash(t *testing.T) {
	env := newFailureEnv(t)
	e := sliceHolderErr{detail: []string{"a"}}
	got := WithSpan(context.Background(), env.tracer, "outer", func(ctx context.Context) error {
		return WithSpan(ctx, env.tracer, "inner", func(context.Context) error { return e })
	})
	if _, ok := got.(sliceHolderErr); !ok {
		t.Fatalf("returned %v", got)
	}
	assertFailed(t, env.span(t, "outer"), "github.com/staffops/otel-libs/go.sliceHolderErr")
	if recs := env.records(t); len(recs) != 2 {
		t.Errorf("want 2 records, got %d", len(recs))
	}
}

// A typed-nil receiver whose ErrorType() panics falls back to %T; its text
// is "<nil>".
type nilTypeErr struct{ kind string }

func (e *nilTypeErr) Error() string     { return e.kind }
func (e *nilTypeErr) ErrorType() string { return e.kind }

func TestWithSpan_TypedNilErrorType(t *testing.T) {
	env := newFailureEnv(t)
	var typedNil *nilTypeErr
	_ = WithSpan(context.Background(), env.tracer, "typed-nil", func(context.Context) error { return typedNil })
	assertFailed(t, env.span(t, "typed-nil"), "*otelhelper.nilTypeErr")
	recs := env.records(t)
	if len(recs) != 1 || recs[0]["msg"] != "<nil>" {
		t.Errorf("records: %v", recs)
	}
}

// runtime.Goexit: the span ends Unset, nothing is recorded.
func TestWithSpan_GoexitLeavesUnset(t *testing.T) {
	env := newFailureEnv(t)
	done := make(chan struct{})
	go func() {
		defer close(done)
		_ = WithSpan(context.Background(), env.tracer, "goexit", func(context.Context) error {
			runtime.Goexit()
			return nil
		})
	}()
	<-done
	assertUnset(t, env.span(t, "goexit"))
	if recs := env.records(t); len(recs) != 0 {
		t.Errorf("records: %v", recs)
	}
}

// Fan-out inside one WithSpan (errgroup style): safe under -race; every span
// that marks a failure records it.
func TestWithSpan_ConcurrentFanOut(t *testing.T) {
	env := newFailureEnv(t)
	first := errors.New("worker 0 failed")
	err := WithSpan(context.Background(), env.tracer, "fanout", func(ctx context.Context) error {
		var wg sync.WaitGroup
		errs := make([]error, 8)
		for i := range errs {
			wg.Add(1)
			go func() {
				defer wg.Done()
				errs[i] = WithSpan(ctx, env.tracer, fmt.Sprintf("worker-%d", i), func(c context.Context) error {
					if i == 0 {
						return first
					}
					RecordFailure(c, fmt.Errorf("handled %d", i))
					return nil
				})
			}()
		}
		wg.Wait()
		return errs[0]
	})
	if err != first {
		t.Fatal(err)
	}
	assertFailed(t, env.span(t, "fanout"), "*errors.errorString")
	if recs := env.records(t); len(recs) != 9 {
		t.Errorf("want 9 records (worker 0, 7 handled, fanout), got %d", len(recs))
	}
}

// Chain walks are bounded: a cyclic Unwrap and a panicking Unwrap neither hang
// nor crash; error.type falls back to the override or %T.
type cyclicErr struct{ next error }

func (e *cyclicErr) Error() string { return "cyclic" }
func (e *cyclicErr) Unwrap() error { return e.next }

type panicUnwrapErr struct{}

func (panicUnwrapErr) Error() string     { return "panic unwrap" }
func (panicUnwrapErr) Unwrap() error     { panic("bad unwrap") }
func (panicUnwrapErr) ErrorType() string { return "PanicUnwrap" }

type panicManyErr struct{}

func (panicManyErr) Error() string   { return "panic many" }
func (panicManyErr) Unwrap() []error { panic("bad unwrap") }

type deepErr struct{ n int }

func (e deepErr) Error() string { return "deep" }
func (e deepErr) Unwrap() error {
	return deepErr{n: e.n + 1} // a new value each level: no cycle is seen
}

func TestFailureChainWalksAreBounded(t *testing.T) {
	env := newFailureEnv(t)
	a := &cyclicErr{}
	a.next = &cyclicErr{next: a}
	for name, e := range map[string]error{"cycle": a, "panic-unwrap": panicUnwrapErr{}, "panic-many": panicManyErr{}, "deep": deepErr{}} {
		_ = WithSpan(context.Background(), env.tracer, name, func(context.Context) error { return e })
	}
	assertFailed(t, env.span(t, "cycle"), "*otelhelper.cyclicErr")
	assertFailed(t, env.span(t, "panic-unwrap"), "PanicUnwrap")
	assertFailed(t, env.span(t, "panic-many"), "otelhelper.panicManyErr")
	assertFailed(t, env.span(t, "deep"), "otelhelper.deepErr")
	if n := len(chainLinks(deepErr{})); n != maxFailureChain {
		t.Errorf("chain walk length %d, want the %d bound", n, maxFailureChain)
	}
	if recs := env.records(t); len(recs) != 4 {
		t.Errorf("want 4 records, got %d", len(recs))
	}
}

// Leaf rule edge cases: an empty Unwrap() []error is a leaf, a nil member is
// skipped, a typed-nil gRPC status error is not a cancellation.
type emptyMulti struct{}

func (emptyMulti) Error() string   { return "empty" }
func (emptyMulti) Unwrap() []error { return []error{nil} }

func TestIsCancellationLeaves(t *testing.T) {
	var nilStatus *statusErrNil
	cases := []struct {
		err  error
		want bool
	}{
		{context.Canceled, true},
		{fmt.Errorf("a: %w", fmt.Errorf("b: %w", context.Canceled)), true},
		{errors.Join(context.Canceled, nil), true},
		{emptyMulti{}, false},
		{nilStatus, false},
		{context.DeadlineExceeded, false},
		{errors.New("context canceled"), false},
	}
	for i, c := range cases {
		if got := isCancellation(c.err); got != c.want {
			t.Errorf("case %d (%v): isCancellation = %v, want %v", i, c.err, got, c.want)
		}
	}
}

// statusErrNil panics in GRPCStatus on a nil receiver.
type statusErrNil struct{ s *status.Status }

func (e *statusErrNil) Error() string { return "status" }
func (e *statusErrNil) GRPCStatus() *status.Status {
	return e.s
}

// A nil tracer is GetTracer(): scope otel-helper on the global provider.
func TestWithSpan_NilTracerUsesGlobal(t *testing.T) {
	env := newFailureEnv(t)
	rec := tracetest.NewSpanRecorder()
	tp := sdktrace.NewTracerProvider(sdktrace.WithSpanProcessor(rec))
	defer tp.Shutdown(context.Background())
	prev := otel.GetTracerProvider()
	otel.SetTracerProvider(tp)
	defer otel.SetTracerProvider(prev)
	_ = WithSpan(context.Background(), nil, "global", func(context.Context) error { return errors.New("x") })
	ended := rec.Ended()
	if len(ended) != 1 || ended[0].InstrumentationScope().Name != "otel-helper" {
		t.Fatalf("want one span from scope otel-helper, got %v", ended)
	}
	assertFailed(t, ended[0], "*errors.errorString")
	env.records(t)
}

// An empty ErrorType() at the top does not hide a deeper one.
type emptyTypeWrapper struct{ err error }

func (e emptyTypeWrapper) Error() string     { return "wrapper: " + e.err.Error() }
func (e emptyTypeWrapper) ErrorType() string { return "" }
func (e emptyTypeWrapper) Unwrap() error     { return e.err }

func TestWithSpan_EmptyOverrideDoesNotHideDeeper(t *testing.T) {
	env := newFailureEnv(t)
	_ = WithSpan(context.Background(), env.tracer, "deeper", func(context.Context) error {
		return emptyTypeWrapper{fmt.Errorf("x: %w", &typedErr{"r"})}
	})
	assertFailed(t, env.span(t, "deeper"), "OrderRejectedError")
	env.records(t)
}

// A panic with http.ErrAbortHandler is a cancellation: Unset, no record,
// re-panicked unchanged.
func TestWithSpan_ErrAbortHandlerPanicIsCancellation(t *testing.T) {
	env := newFailureEnv(t)
	r := recoverValue(func() {
		_ = WithSpan(context.Background(), env.tracer, "abort", func(context.Context) error { panic(http.ErrAbortHandler) })
	})
	if r != any(http.ErrAbortHandler) {
		t.Fatalf("re-panicked %v", r)
	}
	assertUnset(t, env.span(t, "abort"))
	if recs := env.records(t); len(recs) != 0 {
		t.Errorf("records: %v", recs)
	}
}

// Above ERROR the record is disabled: no record (and no stack taken); the
// span is still marked.
func TestWithSpan_RecordDisabledByLevel(t *testing.T) {
	env := newFailureEnv(t)
	prev := installedLogLevel.Level()
	installedLogLevel.Set(slog.LevelError + 4)
	defer installedLogLevel.Set(prev)
	_ = WithSpan(context.Background(), env.tracer, "quiet", func(context.Context) error { return errors.New("x") })
	assertFailed(t, env.span(t, "quiet"), "*errors.errorString")
	if recs := env.records(t); len(recs) != 0 {
		t.Errorf("records: %v", recs)
	}
}

// A stdout write error is reported once through the stderr diag logger.
func TestWithSpan_StdoutWriteErrorReportedOnce(t *testing.T) {
	env := newFailureEnv(t)
	diag := captureDiag(t)
	r, w, err := os.Pipe()
	if err != nil {
		t.Fatal(err)
	}
	_ = r.Close()
	_ = w.Close()
	orig := os.Stdout
	os.Stdout = w // closed: every write fails
	for i := 0; i < 2; i++ {
		_ = WithSpan(context.Background(), env.tracer, "broken-stdout", func(context.Context) error { return errors.New("secret-ish text") })
	}
	os.Stdout = orig
	out := diag.String()
	if n := strings.Count(out, "a failure log record could not be written"); n != 1 {
		t.Errorf("want the write error once on the diag channel, got %d: %q", n, out)
	}
	if strings.Contains(out, "secret-ish text") {
		t.Errorf("the diag line must not carry the record text: %q", out)
	}
	env.records(t)
}
