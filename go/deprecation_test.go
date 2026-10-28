package otelhelper

import (
	"context"
	"strings"
	"testing"

	"go.opentelemetry.io/otel/baggage"
	sdktrace "go.opentelemetry.io/otel/sdk/trace"
	"go.opentelemetry.io/otel/trace"
)

// resetDeprecationsForTest forgets which once-per-process warnings were
// emitted, so a test can observe them regardless of test order.
func resetDeprecationsForTest(t *testing.T) {
	t.Helper()
	deprecationsWarned.Clear()
	t.Cleanup(func() { deprecationsWarned.Clear() })
}

// ── A9: deprecations (design D8, R8) ───────────────────────────────────────

func TestDeprecatedEnvWarnsNamingReplacement(t *testing.T) {
	tests := []struct {
		variable, value, replacement string
	}{
		{EnvServiceName, "svc-legacy", EnvOtelServiceName},
		{EnvEnvironment, "DEV", EnvResourceAttributes},
		{EnvSampleRatio, "0.5", EnvTracesSampler},
		{EnvDisabledSignals, "logs", "OTEL_LOGS_EXPORTER=none"},
	}
	for _, tt := range tests {
		t.Run(tt.variable, func(t *testing.T) {
			buf := captureDiag(t)
			t.Setenv(tt.variable, tt.value)
			newOptions()
			got := buf.String()
			want := tt.variable + " is deprecated"
			if !strings.Contains(got, want) || !strings.Contains(got, tt.replacement) {
				t.Fatalf("diagnostics missing %q / replacement %q:\n%s", want, tt.replacement, got)
			}
			if strings.Contains(got, tt.value) {
				t.Errorf("diagnostics must never contain the value %q:\n%s", tt.value, got)
			}
		})
	}
}

func TestDeprecatedEnvWarnsEvenWhenStandardVarWins(t *testing.T) {
	buf := captureDiag(t)
	t.Setenv(EnvOtelServiceName, "svc-new")
	t.Setenv(EnvServiceName, "svc-legacy")
	newOptions()
	if !strings.Contains(buf.String(), "SERVICE_NAME is deprecated") {
		t.Errorf("want deprecation warning while the legacy var is set:\n%s", buf)
	}
}

func TestDeprecationWarnedOncePerProcess(t *testing.T) {
	buf := captureDiag(t)
	t.Setenv(EnvServiceName, "svc-legacy")
	t.Setenv(EnvExtraInstrumentation, "SQL")
	for range 3 {
		newOptions()
	}
	for _, v := range []string{"SERVICE_NAME is deprecated", EnvExtraInstrumentation + " has no effect"} {
		if n := strings.Count(buf.String(), v); n != 1 {
			t.Errorf("%q warned %d times across 3 resolutions, want 1:\n%s", v, n, buf)
		}
	}
}

func TestDeprecationSilentWhenUnsetOrBlank(t *testing.T) {
	buf := captureDiag(t)
	t.Setenv(EnvOtelServiceName, "svc")
	t.Setenv(EnvSampleRatio, "  ")
	newOptions()
	if strings.Contains(buf.String(), "deprecated") || strings.Contains(buf.String(), "no effect") {
		t.Errorf("unexpected deprecation warning:\n%s", buf)
	}
}

// G9: the option is removed in Go; the variable is accepted but warned.
func TestExtraInstrumentationEnvWarnsNoEffect(t *testing.T) {
	buf := captureDiag(t)
	t.Setenv(EnvOtelServiceName, "svc")
	t.Setenv(EnvExtraInstrumentation, "SQL,REDIS")
	newOptions()
	got := buf.String()
	if !strings.Contains(got, EnvExtraInstrumentation+" has no effect in Go") || !strings.Contains(got, "ext/otelsql") {
		t.Errorf("want no-effect warning naming the ext modules:\n%s", got)
	}
}

func TestSchemelessHTTPDeprecationOncePerProcess(t *testing.T) {
	buf := captureDiag(t)
	t.Setenv(EnvOtelServiceName, "svc")
	t.Setenv(EnvCollectorEndpoint, "collector:4318")
	t.Setenv(EnvOtlpProtocol, ProtocolHTTP)
	for range 2 {
		o := newOptions()
		for _, s := range allSignals {
			o.export(s)
		}
	}
	if n := strings.Count(buf.String(), "schemeless endpoint is deprecated"); n != 1 {
		t.Errorf("schemeless deprecation warned %d times, want 1:\n%s", n, buf)
	}
}

// ── A9: debug marker on entry spans (R9, ADR-006) ──────────────────────────

func debugAttr(t *testing.T, ctx context.Context) (string, bool) {
	t.Helper()
	_, tracer := newTestProvider(t, &debugProcessor{})
	_, span := tracer.Start(ctx, "target")
	defer span.End()
	// Read the live span: an unsampled one is never exported.
	for _, kv := range span.(sdktrace.ReadOnlySpan).Attributes() {
		if kv.Key == "debug" {
			return kv.Value.Emit(), true
		}
	}
	return "", false
}

func debugRemoteCtx(sampled bool, ts string) context.Context {
	cfg := trace.SpanContextConfig{
		TraceID: trace.TraceID{1, 2, 3}, SpanID: trace.SpanID{4, 5, 6}, Remote: true,
	}
	if sampled {
		cfg.TraceFlags = trace.FlagsSampled
	}
	if ts != "" {
		cfg.TraceState, _ = trace.ParseTraceState(ts)
	}
	return trace.ContextWithRemoteSpanContext(context.Background(), trace.NewSpanContext(cfg))
}

// Failed before A9: the processor only marked spans without any parent.
// (An unsampled remote parent yields a non-recording span: nothing to mark.)
func TestDebugProcessorMarksRemoteParentEntrySpan(t *testing.T) {
	if v, ok := debugAttr(t, debugRemoteCtx(true, "")); !ok || v != "true" {
		t.Errorf("debug = %q (present=%v), want \"true\" on an entry span", v, ok)
	}
}

func TestDebugProcessorSkipsLocalChildOfRemote(t *testing.T) {
	_, tracer := newTestProvider(t, &debugProcessor{})
	ctx, entry := tracer.Start(debugRemoteCtx(true, ""), "entry")
	defer entry.End()
	_, child := tracer.Start(ctx, "child")
	defer child.End()
	for _, kv := range child.(sdktrace.ReadOnlySpan).Attributes() {
		if kv.Key == "debug" {
			t.Error("a span with a local parent must not carry debug")
		}
	}
}

func TestDebugProcessorDoesNotWriteTraceState(t *testing.T) {
	exp, tracer := newTestProvider(t, &debugProcessor{})
	_, root := tracer.Start(context.Background(), "root")
	root.End()
	_, entry := tracer.Start(debugRemoteCtx(true, "vendor=x"), "entry")
	entry.End()
	for _, s := range exp.GetSpans() {
		if strings.Contains(s.SpanContext.TraceState().String(), "debug") {
			t.Errorf("%s: tracestate %q, the marker must not touch tracestate", s.Name, s.SpanContext.TraceState())
		}
	}
}

// With debug mode off, inbound debug tracestate/baggage must not mark spans.
func TestDebugOffIgnoresInboundDebugSignals(t *testing.T) {
	t.Setenv(EnvOtelServiceName, "svc")
	shutdown, err := Setup(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = shutdown(context.Background()) })
	m, _ := baggage.NewMember("debug", "true")
	bag, _ := baggage.New(m)
	for name, ctx := range map[string]context.Context{
		"tracestate": debugRemoteCtx(true, "debug=true"),
		"baggage":    baggage.ContextWithBaggage(debugRemoteCtx(true, ""), bag),
	} {
		_, span := GetTracer("t").Start(ctx, "target")
		ro, ok := span.(sdktrace.ReadOnlySpan)
		if !ok {
			t.Fatalf("%s: span is %T, want an SDK span", name, span)
		}
		for _, kv := range ro.Attributes() {
			if kv.Key == "debug" {
				t.Errorf("%s: inbound debug signal set debug=%s with debug mode off", name, kv.Value.Emit())
			}
		}
		span.End()
	}
}
