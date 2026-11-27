package otelhelper

import (
	"context"
	"net"
	"net/http"
	"net/http/httptest"
	"testing"
	"time"

	"go.opentelemetry.io/otel"
	"go.opentelemetry.io/otel/attribute"
	sdkmetric "go.opentelemetry.io/otel/sdk/metric"
	"go.opentelemetry.io/otel/sdk/metric/metricdata"
	sdktrace "go.opentelemetry.io/otel/sdk/trace"
	"go.opentelemetry.io/otel/sdk/trace/tracetest"
	"go.opentelemetry.io/otel/trace"
	"google.golang.org/grpc"
	"google.golang.org/grpc/credentials/insecure"
	"google.golang.org/grpc/health"
	healthpb "google.golang.org/grpc/health/grpc_health_v1"
	"google.golang.org/grpc/test/bufconn"
	"google.golang.org/protobuf/types/known/emptypb"
)

// inMemoryTracing installs a synchronous in-memory TracerProvider for one test.
func inMemoryTracing(t *testing.T) *tracetest.InMemoryExporter {
	t.Helper()
	exp := tracetest.NewInMemoryExporter()
	tp := sdktrace.NewTracerProvider(sdktrace.WithSpanProcessor(sdktrace.NewSimpleSpanProcessor(exp)))
	prev := otel.GetTracerProvider()
	otel.SetTracerProvider(tp)
	t.Cleanup(func() {
		otel.SetTracerProvider(prev)
		_ = tp.Shutdown(context.Background())
	})
	return exp
}

func spanAttr(s tracetest.SpanStub, key attribute.Key) (string, bool) {
	for _, kv := range s.Attributes {
		if kv.Key == key {
			return kv.Value.Emit(), true
		}
	}
	return "", false
}

// NewHTTPHandler itself filters nothing (design D5): with a plain provider a
// health request is traced and counted. Dropping its span is the job of the
// sampler Setup installs (health_test.go), so the metric survives.
func TestHTTPHandlerDoesNotFilterHealthPaths(t *testing.T) {
	exp := inMemoryTracing(t)
	reader := inMemoryMetrics(t)
	handler := NewHTTPHandler(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(200)
	}), "test-server")
	paths := []string{"/health", "/healthz", "/ready", "/ping"}
	for _, path := range paths {
		handler.ServeHTTP(httptest.NewRecorder(), httptest.NewRequest("GET", path, nil))
	}
	if n := len(exp.GetSpans()); n != len(paths) {
		t.Errorf("spans = %d, want %d (no WithFilter on the handler)", n, len(paths))
	}
	var rm metricdata.ResourceMetrics
	if err := reader.Collect(context.Background(), &rm); err != nil {
		t.Fatal(err)
	}
	var count uint64
	for _, sm := range rm.ScopeMetrics {
		for _, m := range sm.Metrics {
			if h, ok := m.Data.(metricdata.Histogram[float64]); ok && m.Name == "http.server.request.duration" {
				for _, dp := range h.DataPoints {
					count += dp.Count
				}
			}
		}
	}
	if count != uint64(len(paths)) {
		t.Errorf("http.server.request.duration count = %d, want %d", count, len(paths))
	}
}

func TestHTTPHandlerCreatesSpanForNonHealth(t *testing.T) {
	exp := tracetest.NewInMemoryExporter()
	tp := sdktrace.NewTracerProvider(sdktrace.WithSpanProcessor(sdktrace.NewSimpleSpanProcessor(exp)))
	defer tp.Shutdown(context.Background())
	otel.SetTracerProvider(tp)

	handler := NewHTTPHandler(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(200)
	}), "test-server")

	req := httptest.NewRequest("GET", "/api/data", nil)
	rec := httptest.NewRecorder()
	handler.ServeHTTP(rec, req)

	if len(exp.GetSpans()) == 0 {
		t.Error("Non-health path should create a span")
	}
}

// With otelhttp v0.72 and ServeMux patterns (Go >= 1.22; the module floor is
// 1.26) the server span is
// named after the matched route, whether NewHTTPHandler wraps the route's
// handler (pattern known at span start) or the whole mux (span renamed after
// the mux sets r.Pattern).
func TestHTTPHandlerSpanNameIncludesRoute(t *testing.T) {
	ok := http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) { w.WriteHeader(http.StatusOK) })
	tests := []struct {
		name      string
		handler   func() http.Handler
		method    string
		target    string
		wantName  string
		wantRoute bool
	}{
		{
			name: "handler inside mux",
			handler: func() http.Handler {
				mux := http.NewServeMux()
				mux.Handle("GET /status/{id}", NewHTTPHandler(ok, "op"))
				return mux
			},
			method:    http.MethodGet,
			target:    "/status/42",
			wantName:  "GET /status/{id}",
			wantRoute: true,
		},
		{
			name: "mux inside handler",
			handler: func() http.Handler {
				mux := http.NewServeMux()
				mux.Handle("POST /detect", ok)
				return NewHTTPHandler(mux, "op")
			},
			method:   http.MethodPost,
			target:   "/detect",
			wantName: "POST /detect",
		},
		{
			name:     "no mux pattern",
			handler:  func() http.Handler { return NewHTTPHandler(ok, "op") },
			method:   http.MethodGet,
			target:   "/anything",
			wantName: "GET",
		},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			exp := inMemoryTracing(t)
			tt.handler().ServeHTTP(httptest.NewRecorder(), httptest.NewRequest(tt.method, tt.target, nil))
			spans := exp.GetSpans()
			if len(spans) != 1 {
				t.Fatalf("got %d spans, want 1", len(spans))
			}
			if spans[0].Name != tt.wantName {
				t.Errorf("span name = %q, want %q", spans[0].Name, tt.wantName)
			}
			if spans[0].SpanKind != trace.SpanKindServer {
				t.Errorf("span kind = %v, want server", spans[0].SpanKind)
			}
			if tt.wantRoute {
				if got, _ := spanAttr(spans[0], "http.route"); got != "/status/{id}" {
					t.Errorf("http.route = %q, want /status/{id}", got)
				}
			}
		})
	}
}

// grpcPair starts a bufconn server (health service + an echo for any other
// method) and returns a client connection built with clientOpts.
func grpcPair(t *testing.T, serverOpts []grpc.ServerOption, clientOpts ...grpc.DialOption) *grpc.ClientConn {
	t.Helper()
	lis := bufconn.Listen(1 << 20)
	serverOpts = append(serverOpts, grpc.UnknownServiceHandler(func(_ any, s grpc.ServerStream) error {
		var m emptypb.Empty
		if err := s.RecvMsg(&m); err != nil {
			return err
		}
		return s.SendMsg(&emptypb.Empty{})
	}))
	srv := grpc.NewServer(serverOpts...)
	healthpb.RegisterHealthServer(srv, health.NewServer())
	go func() { _ = srv.Serve(lis) }()
	clientOpts = append(clientOpts,
		grpc.WithContextDialer(func(ctx context.Context, _ string) (net.Conn, error) { return lis.DialContext(ctx) }),
		grpc.WithTransportCredentials(insecure.NewCredentials()))
	conn, err := grpc.NewClient("passthrough:///test", clientOpts...)
	if err != nil {
		t.Fatalf("grpc.NewClient: %v", err)
	}
	t.Cleanup(func() {
		_ = conn.Close()
		srv.Stop()
	})
	return conn
}

func countKind(spans tracetest.SpanStubs, kind trace.SpanKind) int {
	n := 0
	for _, s := range spans {
		if s.SpanKind == kind {
			n++
		}
	}
	return n
}

// The server stats handler traces and counts every RPC — health Check (unary)
// and Watch (stream) included — in rpc.server.call.duration, so failing health
// probes stay visible in metrics (dropping server health spans is the Setup
// sampler's job). The client handler filters health calls: no client span,
// no rpc.client.call.duration (design D6 "A5 validation rulings").
func TestGRPCStatsHandlersTraceAndCount(t *testing.T) {
	tests := []struct {
		name   string
		method string
		health bool
		call   func(ctx context.Context, conn *grpc.ClientConn) error
	}{
		{"application unary", "test.v1.Echo/Ping", false, func(ctx context.Context, conn *grpc.ClientConn) error {
			return conn.Invoke(ctx, "/test.v1.Echo/Ping", &emptypb.Empty{}, &emptypb.Empty{})
		}},
		{"health check", "grpc.health.v1.Health/Check", true, func(ctx context.Context, conn *grpc.ClientConn) error {
			_, err := healthpb.NewHealthClient(conn).Check(ctx, &healthpb.HealthCheckRequest{})
			return err
		}},
		{"health watch", "grpc.health.v1.Health/Watch", true, func(ctx context.Context, conn *grpc.ClientConn) error {
			wctx, cancel := context.WithCancel(ctx)
			defer cancel()
			stream, err := healthpb.NewHealthClient(conn).Watch(wctx, &healthpb.HealthCheckRequest{})
			if err != nil {
				return err
			}
			_, err = stream.Recv()
			return err
		}},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			exp := inMemoryTracing(t)
			reader := inMemoryMetrics(t)
			conn := grpcPair(t, []grpc.ServerOption{GRPCServerOption()}, GRPCDialOption())
			ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
			defer cancel()
			if err := tt.call(ctx, conn); err != nil {
				t.Fatalf("call: %v", err)
			}
			_ = conn.Close() // ends the client span of a Watch stream
			client := 1
			if tt.health {
				client = 0
			}
			waitFor(t, func() bool {
				return callCount(t, reader, "rpc.server.call.duration", tt.method) == 1 &&
					countKind(exp.GetSpans(), trace.SpanKindServer) == 1
			})
			if n := callCount(t, reader, "rpc.client.call.duration", tt.method); n != uint64(client) {
				t.Errorf("rpc.client.call.duration count = %d, want %d", n, client)
			}
			if n := countKind(exp.GetSpans(), trace.SpanKindClient); n != client {
				t.Errorf("client spans = %d, want %d", n, client)
			}
		})
	}
}

// inMemoryMetrics installs a MeterProvider with a ManualReader for one test.
// Handlers must be built after it (otelgrpc reads the global at construction).
func inMemoryMetrics(t *testing.T) *sdkmetric.ManualReader {
	t.Helper()
	reader := sdkmetric.NewManualReader()
	mp := sdkmetric.NewMeterProvider(sdkmetric.WithReader(reader))
	prev := otel.GetMeterProvider()
	otel.SetMeterProvider(mp)
	t.Cleanup(func() {
		otel.SetMeterProvider(prev)
		_ = mp.Shutdown(context.Background())
	})
	return reader
}

// callCount returns the count of an RPC duration histogram for one method.
func callCount(t *testing.T, reader *sdkmetric.ManualReader, name, method string) uint64 {
	t.Helper()
	var rm metricdata.ResourceMetrics
	if err := reader.Collect(context.Background(), &rm); err != nil {
		t.Fatalf("collect: %v", err)
	}
	var n uint64
	for _, sm := range rm.ScopeMetrics {
		for _, m := range sm.Metrics {
			if m.Name != name {
				continue
			}
			h, ok := m.Data.(metricdata.Histogram[float64])
			if !ok {
				t.Fatalf("%s: unexpected data type %T", name, m.Data)
			}
			for _, dp := range h.DataPoints {
				if v, ok := dp.Attributes.Value("rpc.method"); ok && v.AsString() == method {
					n += dp.Count
				}
			}
		}
	}
	return n
}

// waitFor polls cond for up to 2s (server-side telemetry ends asynchronously).
func waitFor(t *testing.T, cond func() bool) {
	t.Helper()
	deadline := time.Now().Add(2 * time.Second)
	for !cond() {
		if time.Now().After(deadline) {
			t.Fatal("condition not met within 2s")
		}
		time.Sleep(10 * time.Millisecond)
	}
}

// GRPCServerHandler / GRPCClientHandler are usable directly with
// grpc.StatsHandler / grpc.WithStatsHandler.
func TestGRPCHandlersDirect(t *testing.T) {
	exp := inMemoryTracing(t)
	conn := grpcPair(t, []grpc.ServerOption{grpc.StatsHandler(GRPCServerHandler())}, grpc.WithStatsHandler(GRPCClientHandler()))
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	if err := conn.Invoke(ctx, "/test.v1.Echo/Ping", &emptypb.Empty{}, &emptypb.Empty{}); err != nil {
		t.Fatalf("invoke: %v", err)
	}
	waitFor(t, func() bool { return len(exp.GetSpans()) >= 2 })
	spans := exp.GetSpans()
	if countKind(spans, trace.SpanKindServer) != 1 || countKind(spans, trace.SpanKindClient) != 1 {
		t.Fatalf("want 1 server + 1 client span, got %d spans", len(spans))
	}
	for _, s := range spans {
		if s.Name != "test.v1.Echo/Ping" {
			t.Errorf("span name = %q, want test.v1.Echo/Ping", s.Name)
		}
	}
}
