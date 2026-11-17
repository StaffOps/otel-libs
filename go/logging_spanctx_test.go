package otelhelper

import (
	"context"
	"encoding/json"
	"log/slog"
	"strings"
	"testing"

	"go.opentelemetry.io/otel/trace"
)

// Contract design D1 row "Stdout fallback fields", A23 amendment 2026-11-11
// (implemented in A45): a stdout line for a record in a valid span context
// carries trace_id and span_id right after logger (after msg without one); an
// attribute of either name is then dropped; without a span context neither
// field is written.

const (
	spanCtxTraceID = "4bf92f3577b34da6a3ce929d0e0e4736"
	spanCtxSpanID  = "00f067aa0ba902b7"
)

func spanCtx(sampled bool) context.Context {
	tid, _ := trace.TraceIDFromHex(spanCtxTraceID)
	sid, _ := trace.SpanIDFromHex(spanCtxSpanID)
	cfg := trace.SpanContextConfig{TraceID: tid, SpanID: sid}
	if sampled {
		cfg.TraceFlags = trace.FlagsSampled
	}
	return trace.ContextWithSpanContext(context.Background(), trace.NewSpanContext(cfg))
}

func TestStdoutSpanContextFields(t *testing.T) {
	logsTest(t)
	stop := captureStdout(t)
	l := NewLogger(DEV, false)
	ctx := spanCtx(false)                                                                         // a non-recording span context counts too
	l.WarnContext(ctx, "plain")                                                                   // 0
	l.WarnContext(ctx, "with-logger", "logger", "lg", "k", "v")                                   // 1
	l.WarnContext(ctx, "dup", "trace_id", "mine", "span_id", "mine", "k", "v")                    // 2
	l.With("trace_id", "scope", "a", 1).WarnContext(ctx, "scope-ids")                             // 3
	l.With("trace_id", "scope", "a", 1).Warn("scope-ids-no-span")                                 // 4
	l.Warn("no-span", "trace_id", "kept")                                                         // 5
	l.With("logger", "svc", "trace_id", "s").WithGroup("g").WarnContext(ctx, "grouped", "k", "v") // 6
	l.With("trace_id", "s").WithGroup("g").Warn("grouped-no-span", "k", "v")                      // 7
	l.WarnContext(ctx, "inline", slog.Group("", "span_id", "x", "k", "v"))                        // 8
	l.With(slog.Group("", "span_id", "x")).With("span_id", "y").Warn("merged")                    // 9
	l.WithGroup("g").With("k", 1).WithGroup("h").WarnContext(ctx, "nested", "z", 2)               // 10
	l.WarnContext(ctx, "err-under-trace-id", "trace_id", errTest("e"))                            // 11
	l.WarnContext(context.Background(), "background")                                             // 12
	out := stop()
	lines := strings.Split(strings.TrimSpace(out), "\n")
	if len(lines) != 13 {
		t.Fatalf("want 13 lines, got %d: %q", len(lines), out)
	}
	recs := make([]map[string]any, len(lines))
	for i, line := range lines {
		if err := json.Unmarshal([]byte(line), &recs[i]); err != nil {
			t.Fatalf("line %d not JSON: %v (%q)", i, err, line)
		}
		for _, k := range []string{"trace_id", "span_id"} {
			if n := strings.Count(line, `"`+k+`":`); n > 1 {
				t.Errorf("line %d: duplicate %s: %q", i, k, line)
			}
		}
	}
	ids := `"trace_id":"` + spanCtxTraceID + `","span_id":"` + spanCtxSpanID + `"`
	contains := map[int]string{
		0:  `"msg":"plain",` + ids + `}`,
		1:  `"msg":"with-logger","logger":"lg",` + ids + `,"k":"v"}`,
		2:  `"msg":"dup",` + ids + `,"k":"v"}`,
		3:  `"msg":"scope-ids","a":1,` + ids + `}`,
		4:  `"msg":"scope-ids-no-span","a":1,"trace_id":"scope"}`,
		5:  `"msg":"no-span","trace_id":"kept"}`,
		6:  `"msg":"grouped","logger":"svc",` + ids + `,"g":{"k":"v"}}`,
		7:  `"msg":"grouped-no-span","trace_id":"s","g":{"k":"v"}}`,
		8:  `"msg":"inline",` + ids + `,"k":"v"}`,
		9:  `"msg":"merged","span_id":"y"}`,
		10: `"msg":"nested",` + ids + `,"g":{"k":1,"h":{"z":2}}}`,
		11: `"msg":"err-under-trace-id",` + ids + `,"exception":"e"}`,
		12: `"msg":"background"}`,
	}
	for i, want := range contains {
		if !strings.Contains(lines[i], want) {
			t.Errorf("line %d = %q, want it to contain %q", i, lines[i], want)
		}
	}
}

type errTest string

func (e errTest) Error() string { return string(e) }

// The OTel bridge path is unchanged: the SDK takes the ids from the context.
func TestStdoutSpanContextNotOnBridge(t *testing.T) {
	exporter := logsTest(t)
	stop := captureStdout(t)
	shutdown := setupLogs(t, WithEndpoint("http://127.0.0.1:1"))
	NewLogger(DEV, false).WarnContext(spanCtx(true), "bridged-in-span")
	_ = shutdown(context.Background())
	if out := stop(); strings.Contains(out, "bridged-in-span") {
		t.Errorf("record written to stdout with an exporter: %q", out)
	}
	if exp := exporter(); exp == nil || !exp.has("bridged-in-span") || exp.hasAttr("trace_id") {
		t.Error("bridge record missing, or ids added as attributes")
	}
}
