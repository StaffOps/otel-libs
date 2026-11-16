// Package demotest holds test helpers shared by the demo apps' tests: an
// in-memory tracer and a slog handler that keeps each record with the span
// that was current when it was logged.
package demotest

import (
	"context"
	"log/slog"
	"sync"

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
