package otelhelper

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"strings"
	"testing"

	"go.opentelemetry.io/otel/attribute"
	sdklog "go.opentelemetry.io/otel/sdk/log"
	sdktrace "go.opentelemetry.io/otel/sdk/trace"
	"go.opentelemetry.io/otel/sdk/trace/tracetest"

	"github.com/staffops/otel-libs/go/internal/exclog"
)

// A36 (ADR-014): the core sets the internal/exclog hook; an instrumentation
// span's RecordError writes one ERROR record in the span's context through
// the helper channel, and the span keeps a type-only exception event.

type a36Err struct{}

func (a36Err) Error() string     { return "duplicate 'a36-secret'" }
func (a36Err) ErrorType() string { return "a36.Duplicate" }

func TestExclogHookIsInstalled(t *testing.T) {
	if exclog.Emit == nil {
		t.Fatal("the core's init did not set exclog.Emit")
	}
}

func TestInstrumentationExceptionRecord(t *testing.T) {
	logsTest(t)
	exp := tracetest.NewInMemoryExporter()
	tp := sdktrace.NewTracerProvider(sdktrace.WithSyncer(exp))
	t.Cleanup(func() { _ = tp.Shutdown(context.Background()) })
	tr := exclog.TracerProvider(tp).Tracer("instr")
	stop := captureStdout(t)
	_, span := tr.Start(context.Background(), "db")
	span.RecordError(a36Err{})
	span.RecordError(errors.New("second"))
	span.End()
	out := stop()

	lines := strings.Split(strings.TrimSpace(out), "\n")
	if len(lines) != 2 {
		t.Fatalf("want 2 records (one per RecordError), got %q", out)
	}
	var r map[string]any
	if err := json.Unmarshal([]byte(lines[0]), &r); err != nil {
		t.Fatal(err)
	}
	sc := span.SpanContext()
	if r["msg"] != "duplicate 'a36-secret'" || r["exception.message"] != r["msg"] || r["exception.type"] != "a36.Duplicate" ||
		r["level"] != "ERROR" || r["logger"] != "otel-helper" || r["trace_id"] != sc.TraceID().String() || r["span_id"] != sc.SpanID().String() {
		t.Errorf("record = %v", r)
	}
	stack, _ := r["exception.stacktrace"].(string)
	first := strings.Split(stack, "\n")
	if len(first) < 2 || !strings.Contains(first[1], "TestInstrumentationExceptionRecord") {
		t.Errorf("first frame = %q, want the caller of RecordError (helper and exclog frames trimmed)", first)
	}
	s := exp.GetSpans()[0]
	if len(s.Events) != 2 {
		t.Fatalf("events = %v", s.Events)
	}
	for i, want := range []string{"a36.Duplicate", "*errors.errorString"} {
		e := s.Events[i]
		if len(e.Attributes) != 1 || e.Attributes[0].Value.AsString() != want {
			t.Errorf("event %d = %v, want only exception.type=%s", i, e.Attributes, want)
		}
	}
}

func TestIsHelperFrameExclog(t *testing.T) {
	if !isHelperFrame(exclogFramePrefix + "(*span).RecordError(...)") {
		t.Error("an internal/exclog frame is not a helper frame")
	}
	if !isHelperFrame(helperFramePrefix + "emitInstrumentationException(...)") {
		t.Error("the hook is not a helper frame")
	}
	if isHelperFrame("github.com/XSAM/otelsql.recordSpanError(...)") {
		t.Error("an instrumentation frame is a helper frame")
	}
}

func TestInstrumentationExceptionRecordThroughBridge(t *testing.T) {
	logsTest(t)
	rec := &failureLogRecorder{}
	otlpLogExporter = func(context.Context, *signalExport) (sdklog.Exporter, error) { return rec, nil }
	shutdown := setupLogs(t, WithEndpoint("http://127.0.0.1:1"))
	exp := tracetest.NewInMemoryExporter()
	tp := sdktrace.NewTracerProvider(sdktrace.WithSyncer(exp))
	t.Cleanup(func() { _ = tp.Shutdown(context.Background()) })
	stop := captureStdout(t)
	_, span := exclog.TracerProvider(tp).Tracer("instr").Start(context.Background(), "db")
	span.RecordError(a36Err{})
	span.End()
	if err := shutdown(context.Background()); err != nil {
		t.Fatal(err)
	}
	if out := stop(); out != "" {
		t.Errorf("logs exporter active: nothing on stdout, got %q", out)
	}
	rec.mu.Lock()
	defer rec.mu.Unlock()
	if len(rec.recs) != 1 {
		t.Fatalf("want 1 exported record, got %d", len(rec.recs))
	}
	r := rec.recs[0]
	if r.TraceID() != span.SpanContext().TraceID() || r.SpanID() != span.SpanContext().SpanID() {
		t.Errorf("record ids %s/%s, want the span's", r.TraceID(), r.SpanID())
	}
	got := map[string]string{}
	r.WalkAttributes(func(kv attribute.KeyValue) bool { got[string(kv.Key)] = kv.Value.AsString(); return true })
	if r.SeverityText() != "ERROR" || r.Body().AsString() != "duplicate 'a36-secret'" || got["exception.type"] != "a36.Duplicate" ||
		got["exception.message"] != "duplicate 'a36-secret'" || !strings.Contains(got["exception.stacktrace"], "goroutine") {
		t.Errorf("severity %q body %q attributes %v", r.SeverityText(), r.Body().AsString(), got)
	}
}

func TestInstrumentationExceptionCancellationAndAttributes(t *testing.T) {
	logsTest(t)
	exp := tracetest.NewInMemoryExporter()
	tp := sdktrace.NewTracerProvider(sdktrace.WithSyncer(exp))
	t.Cleanup(func() { _ = tp.Shutdown(context.Background()) })
	tr := exclog.TracerProvider(tp,
		exclog.WithRecordAttributes(attribute.String("db.system.name", "postgresql")),
		exclog.WithErrorAttributes(func(error) []attribute.KeyValue {
			return []attribute.KeyValue{attribute.String("db.response.status_code", "23505"), attribute.String("error.type", "23505"), {}}
		})).Tracer("instr")
	stop := captureStdout(t)
	_, span := tr.Start(context.Background(), "db")
	span.RecordError(fmt.Errorf("query: %w", context.Canceled)) // a cancellation: event, no record
	span.RecordError(a36Err{})
	span.End()
	lines := strings.Split(strings.TrimSpace(stop()), "\n")
	if len(lines) != 1 {
		t.Fatalf("want one record (none for the cancellation), got %q", lines)
	}
	var r map[string]any
	if err := json.Unmarshal([]byte(lines[0]), &r); err != nil {
		t.Fatal(err)
	}
	if r["db.system.name"] != "postgresql" || r["db.response.status_code"] != "23505" || r["error.type"] != "23505" || r["exception.type"] != "a36.Duplicate" {
		t.Errorf("record = %v", r)
	}
	if ev := exp.GetSpans()[0].Events; len(ev) != 2 || len(ev[0].Attributes) != 1 || ev[0].Attributes[0].Key != "exception.type" {
		t.Errorf("events = %v, want the cancellation's type-only event kept", ev)
	}
}
