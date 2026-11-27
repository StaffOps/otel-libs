package otelhelper

// A48 (design D1 row "Metrics exporters / interval / exemplars", amendment
// 2026-11-19): the helper parses OTEL_METRICS_EXEMPLAR_FILTER itself and
// always passes the filter. A49 (D1 row "Propagation with traces off",
// amendment 2026-11-19): a root carries no inbound context with traces
// disabled; a child passes the inbound context on unchanged.

import (
	"context"
	"os"
	"strings"
	"testing"
	"time"

	"go.opentelemetry.io/otel"
	"go.opentelemetry.io/otel/baggage"
	"go.opentelemetry.io/otel/propagation"
	"go.opentelemetry.io/otel/sdk/metric/exemplar"
	"go.opentelemetry.io/otel/trace"
)

func setupForA48(t *testing.T, opts ...Option) {
	t.Helper()
	s, err := Setup(context.Background(), append([]Option{WithServiceName("a48"), WithMetricExporters("prometheus"), WithoutMetricsListener()}, opts...)...)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() {
		ctx, cancel := context.WithTimeout(context.Background(), 500*time.Millisecond)
		defer cancel()
		_ = s(ctx)
	})
}

func TestExemplarFilterEnv(t *testing.T) {
	long := strings.Repeat("x", 100)
	for _, c := range []struct {
		name, env, want string
		warn            bool
	}{
		{"unset", "", "trace_based", false},
		{"always_off", "always_off", "always_off", false},
		{"always_on", "always_on", "always_on", false},
		{"trace_based", "trace_based", "trace_based", false},
		{"ascii trim and case", " \tALWAYS_On\n", "always_on", false},
		{"blank is unset", " \t ", "trace_based", false},
		{"unknown", "sometimes", "trace_based", true},
		// The Go SDK's own parse (strings.TrimSpace) would accept this one.
		{"nbsp is not trimmed", " always_on", "trace_based", true},
		// strings.EqualFold folds U+017F to "s"; asciiLower does not.
		{"non-ascii case fold rejected", "alway\u017f_on", "trace_based", true},
		{"long value", long, "trace_based", true},
	} {
		t.Run(c.name, func(t *testing.T) {
			lifecycleTest(t)
			diag := captureDiag(t)
			if c.env != "" {
				t.Setenv(EnvMetricsExemplarFilter, c.env)
			} else {
				t.Setenv(EnvMetricsExemplarFilter, "") // restored after the test
				os.Unsetenv(EnvMetricsExemplarFilter)
			}
			setupForA48(t)
			if got := installedExemplarFilter(); got != c.want {
				t.Errorf("installed exemplar filter = %q, want %q", got, c.want)
			}
			out := diag.String()
			if n := strings.Count(out, EnvMetricsExemplarFilter); (n == 1) != c.warn || n > 1 {
				t.Errorf("%d warnings naming %s, want warn=%v (once):\n%s", n, EnvMetricsExemplarFilter, c.warn, out)
			}
			if c.warn && !strings.Contains(out, "using=trace_based") {
				t.Errorf("warning does not name the default:\n%s", out)
			}
			if c.env == long && (strings.Contains(out, long) || !strings.Contains(out, strings.Repeat("x", maxEchoedValue)+"…")) {
				t.Errorf("long value not cut to %d characters:\n%s", maxEchoedValue, out)
			}
		})
	}
}

// Once per Setup (not per process): a second Setup warns again, and the
// variable is parsed with metrics disabled too.
func TestExemplarFilterWarnedEachSetupMetricsOff(t *testing.T) {
	lifecycleTest(t)
	diag := captureDiag(t)
	t.Setenv(EnvMetricsExemplarFilter, "sometimes")
	t.Setenv(EnvDisabledSignals, "metrics")
	for i := 0; i < 2; i++ {
		s, err := Setup(context.Background(), WithServiceName("a48"))
		if err != nil {
			t.Fatal(err)
		}
		_ = s(context.Background())
	}
	if n := strings.Count(diag.String(), EnvMetricsExemplarFilter); n != 2 {
		t.Errorf("%d warnings over two setups, want 2:\n%s", n, diag.String())
	}
}

func TestExemplarFilterFor(t *testing.T) {
	for name, want := range map[string]exemplar.Filter{
		"always_on": exemplar.AlwaysOnFilter, "always_off": exemplar.AlwaysOffFilter,
		"trace_based": exemplar.TraceBasedFilter, "": exemplar.TraceBasedFilter,
	} {
		if got := exemplarFilterFor(name); got == nil || !sameFilter(got, want) {
			t.Errorf("exemplarFilterFor(%q) is not the expected filter", name)
		}
	}
}

func sameFilter(a, b exemplar.Filter) bool {
	// Distinguish the three SDK filters by behavior on a context without a
	// sampled span: AlwaysOn true, AlwaysOff and TraceBased false; and with
	// one: TraceBased true.
	ctx := context.Background()
	sampled := trace.ContextWithSpanContext(ctx, trace.NewSpanContext(trace.SpanContextConfig{
		TraceID: trace.TraceID{1}, SpanID: trace.SpanID{1}, TraceFlags: trace.FlagsSampled,
	}))
	return a(ctx) == b(ctx) && a(sampled) == b(sampled)
}

const (
	a49Traceparent = "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-00"
	a49Tracestate  = "vendor1=a,vendor2=b"
)

// a49Inbound sets up the helper with traces disabled and returns a context
// carrying the extracted inbound trace context and a baggage member.
func a49Inbound(t *testing.T) context.Context {
	t.Helper()
	lifecycleTest(t)
	t.Setenv(EnvDisabledSignals, "traces")
	s, err := Setup(context.Background(), WithServiceName("a49"), WithMetricExporters("prometheus"), WithoutMetricsListener())
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = s(context.Background()) })
	ctx := otel.GetTextMapPropagator().Extract(context.Background(), propagation.MapCarrier{
		"traceparent": a49Traceparent, "tracestate": a49Tracestate,
	})
	if !trace.SpanContextFromContext(ctx).IsRemote() {
		t.Fatal("inbound context was not extracted")
	}
	m, _ := baggage.NewMember("parity", "1")
	bag, _ := baggage.New(m)
	return baggage.ContextWithBaggage(ctx, bag)
}

func injected(ctx context.Context) propagation.MapCarrier {
	c := propagation.MapCarrier{}
	otel.GetTextMapPropagator().Inject(ctx, c)
	return c
}

func TestStartRootSpanTracesDisabledDropsInbound(t *testing.T) {
	ctx := a49Inbound(t)
	rootCtx, root := StartRootSpan(ctx, GetTracer("a49"), "root")
	defer root.End()
	if root.SpanContext().IsValid() {
		t.Errorf("root span context = %v, want invalid", root.SpanContext())
	}
	c := injected(rootCtx)
	if _, ok := c["traceparent"]; ok {
		t.Errorf("root injected traceparent %q, want none", c["traceparent"])
	}
	if _, ok := c["tracestate"]; ok {
		t.Errorf("root injected tracestate %q, want none", c["tracestate"])
	}
	if c["baggage"] != "parity=1" {
		t.Errorf("baggage = %q, want parity=1 (kept)", c["baggage"])
	}
}

func TestChildSpanTracesDisabledPassesInboundUnchanged(t *testing.T) {
	ctx := a49Inbound(t)
	childCtx, child := GetTracer("a49").Start(ctx, "child")
	defer child.End()
	c := injected(childCtx)
	if c["traceparent"] != a49Traceparent || c["tracestate"] != a49Tracestate {
		t.Errorf("injected traceparent %q tracestate %q, want %q %q", c["traceparent"], c["tracestate"], a49Traceparent, a49Tracestate)
	}
}

func TestWithSpanNewRootTracesDisabledDropsInbound(t *testing.T) {
	ctx := a49Inbound(t)
	var root, child propagation.MapCarrier
	if err := WithSpan(ctx, nil, "root", func(ctx context.Context) error {
		root = injected(ctx)
		return nil
	}, trace.WithNewRoot()); err != nil {
		t.Fatal(err)
	}
	if err := WithSpan(ctx, nil, "child", func(ctx context.Context) error {
		child = injected(ctx)
		return nil
	}); err != nil {
		t.Fatal(err)
	}
	if _, ok := root["traceparent"]; ok {
		t.Errorf("WithSpan(WithNewRoot) injected traceparent %q, want none", root["traceparent"])
	}
	if root["baggage"] != "parity=1" {
		t.Errorf("WithSpan(WithNewRoot) baggage = %q, want parity=1", root["baggage"])
	}
	if child["traceparent"] != a49Traceparent || child["tracestate"] != a49Tracestate {
		t.Errorf("WithSpan child injected %q %q, want the inbound context unchanged", child["traceparent"], child["tracestate"])
	}
}

// With an SDK provider, StartRootSpan still starts a new trace from a remote
// parent and keeps the baggage of ctx.
func TestStartRootSpanSDKRemoteParent(t *testing.T) {
	exp, tracer := newTestProvider(t)
	ctx := propagation.TraceContext{}.Extract(context.Background(), propagation.MapCarrier{"traceparent": a49Traceparent})
	m, _ := baggage.NewMember("k", "v")
	bag, _ := baggage.New(m)
	ctx = baggage.ContextWithBaggage(ctx, bag)
	rootCtx, root := StartRootSpan(ctx, tracer, "root")
	root.End()
	spans := exp.GetSpans()
	if len(spans) != 1 || spans[0].Parent.IsValid() || spans[0].SpanContext.TraceID().String() == "4bf92f3577b34da6a3ce929d0e0e4736" {
		t.Fatalf("root span = %+v, want a new trace without a parent", spans)
	}
	if baggage.FromContext(rootCtx).Member("k").Value() != "v" {
		t.Error("StartRootSpan dropped the baggage of ctx")
	}
}

// With an SDK provider, WithSpan with trace.WithNewRoot() starts a new trace
// from a remote parent and keeps the baggage of ctx.
func TestWithSpanNewRootSDKRemoteParent(t *testing.T) {
	exp, tracer := newTestProvider(t)
	ctx := propagation.TraceContext{}.Extract(context.Background(), propagation.MapCarrier{"traceparent": a49Traceparent})
	m, _ := baggage.NewMember("k", "v")
	bag, _ := baggage.New(m)
	ctx = baggage.ContextWithBaggage(ctx, bag)
	var inner context.Context
	if err := WithSpan(ctx, tracer, "root", func(ctx context.Context) error {
		inner = ctx
		return nil
	}, trace.WithNewRoot()); err != nil {
		t.Fatal(err)
	}
	spans := exp.GetSpans()
	if len(spans) != 1 || spans[0].Parent.IsValid() || spans[0].SpanContext.TraceID().String() == "4bf92f3577b34da6a3ce929d0e0e4736" {
		t.Fatalf("root span = %+v, want a new trace without a parent", spans)
	}
	if baggage.FromContext(inner).Member("k").Value() != "v" {
		t.Error("WithSpan(WithNewRoot) dropped the baggage of ctx")
	}
}
