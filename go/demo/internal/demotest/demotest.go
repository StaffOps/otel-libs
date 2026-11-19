// Package demotest holds test helpers shared by the demo apps' tests: an
// in-memory tracer, a slog handler that keeps each record with the span that
// was current when it was logged, and a capture of os.Stdout for the helper's
// failure records.
package demotest

import (
	"context"
	"encoding/json"
	"fmt"
	"log/slog"
	"os"
	"strings"
	"sync"
	"testing"

	sdktrace "go.opentelemetry.io/otel/sdk/trace"
	"go.opentelemetry.io/otel/sdk/trace/tracetest"
	"go.opentelemetry.io/otel/trace"
)

// Tracer returns a tracer whose ended spans land in the returned recorder.
func Tracer() (trace.Tracer, *tracetest.SpanRecorder) {
	rec := tracetest.NewSpanRecorder()
	tp := sdktrace.NewTracerProvider(sdktrace.WithSpanProcessor(rec))
	return tp.Tracer("otel-libs-demo"), rec
}

// Record is one captured log record.
type Record struct {
	Level   slog.Level
	Message string
	Attrs   map[string]string
	// SpanContext is the span current in the record's context.
	SpanContext trace.SpanContext
}

// Logs captures records; safe for concurrent use.
type Logs struct {
	mu      sync.Mutex
	records []Record
}

// Logger returns a logger that writes into l.
func (l *Logs) Logger() *slog.Logger { return slog.New(&handler{logs: l}) }

// Records returns a copy of the captured records.
func (l *Logs) Records() []Record {
	l.mu.Lock()
	defer l.mu.Unlock()
	return append([]Record(nil), l.records...)
}

type handler struct {
	logs  *Logs
	attrs []slog.Attr
}

func (h *handler) Enabled(context.Context, slog.Level) bool { return true }

func (h *handler) Handle(ctx context.Context, r slog.Record) error {
	rec := Record{Level: r.Level, Message: r.Message, Attrs: map[string]string{}, SpanContext: trace.SpanContextFromContext(ctx)}
	for _, a := range h.attrs {
		rec.Attrs[a.Key] = a.Value.String()
	}
	r.Attrs(func(a slog.Attr) bool {
		rec.Attrs[a.Key] = a.Value.String()
		return true
	})
	h.logs.mu.Lock()
	h.logs.records = append(h.logs.records, rec)
	h.logs.mu.Unlock()
	return nil
}

func (h *handler) WithAttrs(attrs []slog.Attr) slog.Handler {
	return &handler{logs: h.logs, attrs: append(append([]slog.Attr(nil), h.attrs...), attrs...)}
}

func (h *handler) WithGroup(string) slog.Handler { return h }

// Stdout captures what the process writes to os.Stdout during a test: the
// helper's stdout JSON fallback, where its failure records land when no logs
// pipeline is installed (otelhelper.WithSpan / RecordFailure, contract design
// D15 "Channel"). Not safe with parallel tests: os.Stdout is a process global.
type Stdout struct {
	t    *testing.T
	file *os.File
}

// CaptureStdout points os.Stdout at a temporary file until the test ends.
func CaptureStdout(t *testing.T) *Stdout {
	t.Helper()
	f, err := os.CreateTemp(t.TempDir(), "stdout")
	if err != nil {
		t.Fatal(err)
	}
	prev := os.Stdout
	os.Stdout = f
	t.Cleanup(func() {
		os.Stdout = prev
		_ = f.Close()
	})
	return &Stdout{t: t, file: f}
}

// Records returns every JSON line written so far, decoded.
func (s *Stdout) Records() []map[string]any {
	s.t.Helper()
	raw, err := os.ReadFile(s.file.Name())
	if err != nil {
		s.t.Fatal(err)
	}
	var out []map[string]any
	for _, line := range strings.Split(strings.TrimSpace(string(raw)), "\n") {
		if line == "" {
			continue
		}
		var m map[string]any
		if err := json.Unmarshal([]byte(line), &m); err != nil {
			s.t.Fatalf("stdout line %q is not JSON: %v", line, err)
		}
		out = append(out, m)
	}
	return out
}

// String returns the value of key in a decoded record as text ("" when absent).
func String(rec map[string]any, key string) string {
	v, ok := rec[key]
	if !ok || v == nil {
		return ""
	}
	if s, ok := v.(string); ok {
		return s
	}
	return fmt.Sprint(v)
}
