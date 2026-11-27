package otelhelper

import (
	"context"
	"errors"
	"io"
	"log/slog"
	"testing"
	"time"
)

// benchStdoutHandler is the stdout fallback path of the helper's handler,
// writing to io.Discard (A23 fix round: cost of the reserved-key filter).
func benchStdoutHandler() *switchHandler {
	return newSwitchHandler(slog.LevelInfo, slog.NewJSONHandler(io.Discard, &slog.HandlerOptions{ReplaceAttr: stdoutTime}))
}

func benchRecord(attrs ...slog.Attr) slog.Record {
	r := slog.NewRecord(time.Now(), slog.LevelWarn, "m", 0)
	r.AddAttrs(attrs...)
	return r
}

func BenchmarkStdoutPlain(b *testing.B) {
	h := benchStdoutHandler()
	r := benchRecord(slog.String("order", "o-1"), slog.Int("n", 2), slog.Bool("ok", true))
	ctx := context.Background()
	b.ReportAllocs()
	for b.Loop() {
		_ = h.Handle(ctx, r)
	}
}

func BenchmarkStdoutError(b *testing.B) {
	h := benchStdoutHandler()
	r := benchRecord(slog.String("order", "o-1"), slog.Any("err", errors.New("boom")))
	ctx := context.Background()
	b.ReportAllocs()
	for b.Loop() {
		_ = h.Handle(ctx, r)
	}
}

func BenchmarkStdoutReserved(b *testing.B) {
	h := benchStdoutHandler()
	r := benchRecord(slog.String("msg", "dup"), slog.String("order", "o-1"), slog.String("logger", "lg"))
	ctx := context.Background()
	b.ReportAllocs()
	for b.Loop() {
		_ = h.Handle(ctx, r)
	}
}
