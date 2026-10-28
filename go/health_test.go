package otelhelper

import (
	"context"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"go.opentelemetry.io/otel/attribute"
	sdktrace "go.opentelemetry.io/otel/sdk/trace"
	"go.opentelemetry.io/otel/sdk/trace/tracetest"
	"go.opentelemetry.io/otel/trace"
	"google.golang.org/grpc"
	healthpb "google.golang.org/grpc/health/grpc_health_v1"
	"google.golang.org/protobuf/types/known/emptypb"
)

// R7 / design D5: health-check server spans are dropped by the sampler Setup
// installs, while the requests still count in the RED metrics.

// healthTest runs Setup (no endpoint: Prometheus reader for MetricsHandler,
// no listener) and records the installed provider's spans.
func healthTest(t *testing.T, opts ...Option) *tracetest.InMemoryExporter {
	t.Helper()
	lifecycleTest(t)
	shutdown, err := Setup(context.Background(), append([]Option{WithServiceName("health-test"), WithoutMetricsListener()}, opts...)...)
	if err != nil {
		t.Fatalf("Setup: %v", err)
	}
	t.Cleanup(func() { _ = shutdown(context.Background()) })
	return recordSpans(t)
}

// serve sends GET requests through NewHTTPHandler; the handler optionally
// starts a child span. traceparent is set when non-empty.
func serve(handlerChild bool, traceparent string, paths ...string) {
	h := NewHTTPHandler(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if handlerChild {
			_, s := GetTracer("health-test").Start(r.Context(), "handler-child")
			s.End()
		}
		w.WriteHeader(http.StatusOK)
	}), "health-test")
	for _, p := range paths {
		req := httptest.NewRequest(http.MethodGet, p, nil)
		if traceparent != "" {
			req.Header.Set("traceparent", traceparent)
		}
		h.ServeHTTP(httptest.NewRecorder(), req)
	}
}

func TestHealthSpansDroppedButCounted(t *testing.T) {
	exp := healthTest(t)
	paths := []string{
		"/health", "/healthz", "/ready", "/readyz", "/live", "/livez", "/ping", "/metrics", // defaults
		"/Health", "/READY", // case-insensitive
		"/health/", "/healthz?probe=1", "/livez//", // trailing slash, query
	}
	serve(true, "", paths...)
	if n := len(exp.GetSpans()); n != 0 {
		t.Errorf("health requests recorded %d spans (incl. handler children), want 0: %v", n, spanNames(exp))
	}
	if got := scrapeCount(httpServerCountRe); got != float64(len(paths)) {
		t.Errorf("http server duration count = %v, want %d (health stays in RED metrics)", got, len(paths))
	}
}

func TestHealthSpansDroppedWithRemoteParent(t *testing.T) {
	exp := healthTest(t)
	serve(true, "00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-01", "/health")
	if n := len(exp.GetSpans()); n != 0 {
		t.Errorf("health request with a sampled traceparent recorded %d spans, want 0", n)
	}
}

func TestNonHealthPathsTraced(t *testing.T) {
	exp := healthTest(t)
	paths := []string{"/orders", "/users/ping", "/healthcare", "/health/live", "/metrics/x"}
	serve(false, "", paths...)
	if n := len(exp.GetSpans()); n != len(paths) {
		t.Errorf("recorded %d spans, want %d (exact match only): %v", n, len(paths), spanNames(exp))
	}
}

func TestWithHealthPathsOverride(t *testing.T) {
	exp := healthTest(t, WithHealthPaths("/Status/"))
	serve(false, "", "/status", "/STATUS/?x=1", "/health")
	names := spanNames(exp)
	if len(names) != 1 {
		t.Errorf("spans %v, want only the /health one (list replaced)", names)
	}
}

func TestWithHealthPathsEmptyTracesEverything(t *testing.T) {
	exp := healthTest(t, WithHealthPaths())
	serve(false, "", "/health", "/ready")
	if n := len(exp.GetSpans()); n != 2 {
		t.Errorf("WithHealthPaths(): %d spans, want 2", n)
	}
}

func TestWithHealthPathsInvalid(t *testing.T) {
	lifecycleTest(t)
	for _, p := range []string{"health", "/health?x=1", "", "/saúde", "/a,b"} {
		if _, err := Setup(context.Background(), WithHealthPaths(p)); err == nil || !strings.Contains(err.Error(), "WithHealthPaths") {
			t.Errorf("WithHealthPaths(%q): err = %v, want a WithHealthPaths error", p, err)
		}
	}
}

func TestGRPCHealthSpansDroppedButCounted(t *testing.T) {
	exp := healthTest(t)
	conn := grpcPair(t, []grpc.ServerOption{GRPCServerOption()})
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	client := healthpb.NewHealthClient(conn)
	if _, err := client.Check(ctx, &healthpb.HealthCheckRequest{}); err != nil {
		t.Fatalf("Check: %v", err)
	}
	wctx, wcancel := context.WithCancel(ctx)
	stream, err := client.Watch(wctx, &healthpb.HealthCheckRequest{})
	if err != nil {
		t.Fatalf("Watch: %v", err)
	}
	if _, err := stream.Recv(); err != nil {
		t.Fatalf("Watch recv: %v", err)
	}
	wcancel()
	if err := conn.Invoke(ctx, "/test.v1.Echo/Ping", &emptypb.Empty{}, &emptypb.Empty{}); err != nil {
		t.Fatalf("invoke: %v", err)
	}
	waitFor(t, func() bool { return scrapeCount(rpcServerCountRe) == 3 })
	for _, name := range spanNames(exp) {
		if strings.HasPrefix(name, "grpc.health.v1.Health/") {
			t.Errorf("health RPC traced: %s", name)
		}
	}
	waitFor(t, func() bool { return len(exp.GetSpans()) == 1 })
}

func spanNames(exp *tracetest.InMemoryExporter) []string {
	var out []string
	for _, s := range exp.GetSpans() {
		out = append(out, s.Name)
	}
	return out
}

// --- the sampler on its own ---

var (
	healthTraceID = trace.TraceID{0x0a, 0xf7, 0x65, 0x19, 0x16, 0xcd, 0x43, 0xdd, 0x84, 0x48, 0xeb, 0x21, 0x1c, 0x80, 0x31, 0x9c}
	healthSpanID  = trace.SpanID{0xb7, 0xad, 0x6b, 0x71, 0x69, 0x20, 0x33, 0x31}
)

func parentCtx(t *testing.T, remote bool) context.Context {
	t.Helper()
	ts, err := trace.ParseTraceState("vendor=abc")
	if err != nil {
		t.Fatal(err)
	}
	sc := trace.NewSpanContext(trace.SpanContextConfig{
		TraceID: healthTraceID, SpanID: healthSpanID, TraceFlags: trace.FlagsSampled, TraceState: ts, Remote: remote,
	})
	return trace.ContextWithSpanContext(context.Background(), sc)
}

func TestHealthSamplerDecisions(t *testing.T) {
	s := newHealthSampler(sdktrace.ParentBased(sdktrace.AlwaysSample()), DefaultHealthPaths())
	path := func(p string) []attribute.KeyValue { return []attribute.KeyValue{attribute.String("url.path", p)} }
	tests := []struct {
		name   string
		ctx    context.Context
		kind   trace.SpanKind
		span   string
		attrs  []attribute.KeyValue
		sample bool
	}{
		{"server root health", context.Background(), trace.SpanKindServer, "GET", path("/health"), false},
		{"server remote parent health", parentCtx(t, true), trace.SpanKindServer, "GET", path("/ready"), false},
		{"server local parent health (not an entry span)", parentCtx(t, false), trace.SpanKindServer, "GET", path("/health"), true},
		{"client span to health path", context.Background(), trace.SpanKindClient, "GET", path("/health"), true},
		{"internal span", context.Background(), trace.SpanKindInternal, "GET", path("/health"), true},
		{"server non-health", context.Background(), trace.SpanKindServer, "GET", path("/orders"), true},
		{"http.target fallback with query", context.Background(), trace.SpanKindServer, "GET",
			[]attribute.KeyValue{attribute.String("http.target", "/Healthz/?a=1")}, false},
		{"grpc span name", context.Background(), trace.SpanKindServer, "grpc.health.v1.Health/Check", nil, false},
		{"grpc span name with slash", context.Background(), trace.SpanKindServer, "/grpc.health.v1.Health/Watch", nil, false},
		{"grpc rpc.method", context.Background(), trace.SpanKindServer, "custom-name",
			[]attribute.KeyValue{attribute.String("rpc.method", "grpc.health.v1.Health/Check")}, false},
		{"grpc List (whole service)", context.Background(), trace.SpanKindServer, "grpc.health.v1.Health/List", nil, false},
		{"grpc other service with Health prefix", context.Background(), trace.SpanKindServer, "grpc.health.v1.HealthX/Check", nil, true},
		{"grpc short rpc.method is not enough", context.Background(), trace.SpanKindServer, "custom-name",
			[]attribute.KeyValue{attribute.String("rpc.method", "Check")}, true},
		{"grpc application method", context.Background(), trace.SpanKindServer, "test.v1.Echo/Ping", nil, true},
		{"no attributes", context.Background(), trace.SpanKindServer, "GET", nil, true},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			res := s.ShouldSample(sdktrace.SamplingParameters{
				ParentContext: tt.ctx, TraceID: healthTraceID, Name: tt.span, Kind: tt.kind, Attributes: tt.attrs,
			})
			if got := res.Decision == sdktrace.RecordAndSample; got != tt.sample {
				t.Errorf("decision = %v, want sampled=%v", res.Decision, tt.sample)
			}
		})
	}
}

func TestHealthSamplerKeepsParentTraceState(t *testing.T) {
	s := newHealthSampler(sdktrace.AlwaysSample(), DefaultHealthPaths())
	res := s.ShouldSample(sdktrace.SamplingParameters{
		ParentContext: parentCtx(t, true), TraceID: healthTraceID, Name: "GET", Kind: trace.SpanKindServer,
		Attributes: []attribute.KeyValue{attribute.String("url.path", "/health")},
	})
	if res.Decision != sdktrace.Drop || res.Tracestate.String() != "vendor=abc" {
		t.Errorf("got %v / %q, want Drop keeping tracestate vendor=abc", res.Decision, res.Tracestate.String())
	}
}

func TestHealthSamplerDescription(t *testing.T) {
	d := newHealthSampler(sdktrace.ParentBased(sdktrace.AlwaysSample()), nil).Description()
	if !strings.HasPrefix(d, "HealthCheckExcluding{ParentBased{") {
		t.Errorf("Description = %q", d)
	}
}

func TestNormalizeHealthPath(t *testing.T) {
	for in, want := range map[string]string{"/": "/", "/A/": "/a", "/a//?q": "/a", "/a#f": "/a", "": ""} {
		if got := normalizeHealthPath(in); got != want {
			t.Errorf("normalizeHealthPath(%q) = %q, want %q", in, got, want)
		}
	}
}

func TestHealthPathListsAreCopies(t *testing.T) {
	d := DefaultHealthPaths()
	d[0] = "/mutated"
	if DefaultHealthPaths()[0] != "/health" {
		t.Error("DefaultHealthPaths returned the shared slice")
	}
	o := &Options{HealthPaths: []string{"/a"}}
	got := o.healthPaths()
	got[0] = "/b"
	if o.HealthPaths[0] != "/a" {
		t.Error("Options.healthPaths returned the caller's slice")
	}
}

// Client side (D6 A5 rulings): health requests get no client span (nor
// metric), same list and normalization as the server, WithHealthPaths too.
func TestHTTPTransportHealthNormalization(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {}))
	defer srv.Close()
	get := func(exp *tracetest.InMemoryExporter, path string) int {
		exp.Reset()
		c := &http.Client{Transport: NewHTTPTransport(nil)}
		resp, err := c.Get(srv.URL + path)
		if err != nil {
			t.Fatal(err)
		}
		_ = resp.Body.Close()
		return len(exp.GetSpans())
	}
	exp := healthTest(t)
	for _, p := range []string{"/Health/", "/livez?x=1", "/metrics"} {
		if n := get(exp, p); n != 0 {
			t.Errorf("client request to %s: %d spans, want 0", p, n)
		}
	}
	if n := get(exp, "/users/ping"); n != 1 {
		t.Errorf("/users/ping: %d spans, want 1", n)
	}
	exp = healthTest(t, WithHealthPaths("/status"))
	if get(exp, "/STATUS") != 0 || get(exp, "/health") != 1 {
		t.Error("WithHealthPaths not applied to NewHTTPTransport")
	}
}

func TestGRPCClientHealthNotTraced(t *testing.T) {
	exp := healthTest(t)
	conn := grpcPair(t, nil, GRPCDialOption())
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	if _, err := healthpb.NewHealthClient(conn).Check(ctx, &healthpb.HealthCheckRequest{}); err != nil {
		t.Fatal(err)
	}
	if err := conn.Invoke(ctx, "/test.v1.Echo/Ping", &emptypb.Empty{}, &emptypb.Empty{}); err != nil {
		t.Fatal(err)
	}
	names := spanNames(exp)
	if len(names) != 1 || names[0] != "test.v1.Echo/Ping" {
		t.Errorf("client spans %v, want only test.v1.Echo/Ping", names)
	}
	// List (whole-service match): the client call is filtered whatever the
	// server answers.
	_ = conn.Invoke(ctx, "/grpc.health.v1.Health/List", &emptypb.Empty{}, &emptypb.Empty{})
	if names := spanNames(exp); len(names) != 1 {
		t.Errorf("client spans after List %v, want only the Ping one", names)
	}
	if !isGRPCHealthMethod("/grpc.health.v1.Health/Watch") || !isGRPCHealthMethod("grpc.health.v1.Health/List") ||
		isGRPCHealthMethod("/grpc.health.v1.HealthX/Check") {
		t.Error("isGRPCHealthMethod")
	}
}
