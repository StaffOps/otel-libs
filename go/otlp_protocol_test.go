package otelhelper

// OTLP wire protocol selection (grpc vs http/protobuf) — tracing, metrics, logging.
//
// Ported from bdcotelhelper's port-based auto-detection (4318 -> http/protobuf),
// extended to honor the standard OTEL_EXPORTER_OTLP_PROTOCOL env var first (see
// resolvedOtlpProtocol in config.go for the full precedence order).

import (
	"context"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"go.opentelemetry.io/otel/sdk/resource"
)

// --- resolvedOtlpProtocol precedence ---

func Test_OtlpProtocol_DefaultIsGrpc(t *testing.T) {
	o := resolvedOptions(t, WithEndpoint("collector:4317"))
	if got := o.resolvedOtlpProtocol(); got != ProtocolGRPC {
		t.Errorf("resolvedOtlpProtocol() = %q, want %q", got, ProtocolGRPC)
	}
}

func Test_OtlpProtocol_NoEndpointDefaultsToGrpc(t *testing.T) {
	t.Setenv(EnvCollectorEndpoint, "")
	o := resolvedOptions(t)
	if got := o.resolvedOtlpProtocol(); got != ProtocolGRPC {
		t.Errorf("resolvedOtlpProtocol() = %q, want %q", got, ProtocolGRPC)
	}
}

func Test_OtlpProtocol_Port4318InfersHttp(t *testing.T) {
	o := resolvedOptions(t, WithEndpoint("collector:4318"))
	if got := o.resolvedOtlpProtocol(); got != ProtocolHTTP {
		t.Errorf("resolvedOtlpProtocol() = %q, want %q", got, ProtocolHTTP)
	}
}

func Test_OtlpProtocol_Port4317InfersGrpc(t *testing.T) {
	o := resolvedOptions(t, WithEndpoint("collector:4317"))
	if got := o.resolvedOtlpProtocol(); got != ProtocolGRPC {
		t.Errorf("resolvedOtlpProtocol() = %q, want %q", got, ProtocolGRPC)
	}
}

func Test_OtlpProtocol_OtherPortInfersGrpc(t *testing.T) {
	o := resolvedOptions(t, WithEndpoint("collector:9999"))
	if got := o.resolvedOtlpProtocol(); got != ProtocolGRPC {
		t.Errorf("resolvedOtlpProtocol() = %q, want %q", got, ProtocolGRPC)
	}
}

func Test_OtlpProtocol_EnvVarBeatsPortInference(t *testing.T) {
	t.Setenv(EnvOtlpProtocol, "http/protobuf")
	o := resolvedOptions(t, WithEndpoint("collector:4317")) // would infer grpc
	if got := o.resolvedOtlpProtocol(); got != ProtocolHTTP {
		t.Errorf("resolvedOtlpProtocol() = %q, want %q", got, ProtocolHTTP)
	}
}

func Test_OtlpProtocol_EnvVarGrpcBeatsHttpPortInference(t *testing.T) {
	t.Setenv(EnvOtlpProtocol, "grpc")
	o := resolvedOptions(t, WithEndpoint("collector:4318")) // would infer http
	if got := o.resolvedOtlpProtocol(); got != ProtocolGRPC {
		t.Errorf("resolvedOtlpProtocol() = %q, want %q", got, ProtocolGRPC)
	}
}

func Test_OtlpProtocol_ExplicitOptionBeatsEnv(t *testing.T) {
	t.Setenv(EnvOtlpProtocol, "http/protobuf")
	o := resolvedOptions(t, WithEndpoint("collector:4317"), WithOtlpProtocol("grpc"))
	if got := o.resolvedOtlpProtocol(); got != ProtocolGRPC {
		t.Errorf("resolvedOtlpProtocol() = %q, want %q", got, ProtocolGRPC)
	}
}

func Test_OtlpProtocol_EnvVarCaseAndWhitespaceTolerant(t *testing.T) {
	t.Setenv(EnvOtlpProtocol, "  HTTP/PROTOBUF  ")
	o := resolvedOptions(t, WithEndpoint("collector:4317"))
	if got := o.resolvedOtlpProtocol(); got != ProtocolHTTP {
		t.Errorf("resolvedOtlpProtocol() = %q, want %q", got, ProtocolHTTP)
	}
}

// --- validation ---

func Test_OtlpProtocol_UnknownValueFailsValidation(t *testing.T) {
	o := resolvedOptions(t, WithServiceName("svc"), WithOtlpProtocol("http/json"))
	err := o.validate()
	if err == nil || !strings.Contains(err.Error(), "unknown OTLP protocol") {
		t.Errorf("validate() = %v, want unknown-protocol error", err)
	}
}

func Test_OtlpProtocol_HttpJsonRejectedEvenThoughSpecValid(t *testing.T) {
	// http/json is a valid spec value but no OTel Go exporter implements it
	// for traces/metrics/logs — must fail loud, not silently downgrade.
	t.Setenv(EnvOtlpProtocol, "http/json")
	o := resolvedOptions(t, WithServiceName("svc"))
	if err := o.validate(); err == nil {
		t.Error("validate() = nil, want error for http/json")
	}
}

func Test_OtlpProtocol_GrpcAndHttpProtobufPassValidation(t *testing.T) {
	for _, proto := range []string{"grpc", "http/protobuf"} {
		o := resolvedOptions(t, WithServiceName("svc"), WithOtlpProtocol(proto))
		if err := o.validate(); err != nil {
			t.Errorf("validate() with protocol %q = %v, want nil", proto, err)
		}
	}
}

// --- live integration: HTTP protocol actually delivers to /v1/{signal} ---

func Test_OtlpProtocol_Tracing_Http_DeliversToV1Traces(t *testing.T) {
	var gotPath string
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		gotPath = r.URL.Path
		w.WriteHeader(http.StatusOK)
	}))
	defer srv.Close()

	host := strings.TrimPrefix(srv.URL, "http://")
	opts := &Options{
		ServiceName:     "http-trace-test",
		Environment:     LOCAL,
		OtelEndpoint:    host,
		Insecure:        true,
		ExportTimeoutMs: 10_000,
		SampleRatio:     1.0,
		OtlpProtocol:    ProtocolHTTP,
	}
	tp, err := configureTracing(context.Background(), resource.Default(), opts)
	if err != nil {
		t.Fatalf("configureTracing error = %v", err)
	}
	defer func() { _ = tp.Shutdown(context.Background()) }()

	tracer := tp.Tracer("probe")
	_, span := tracer.Start(context.Background(), "test-span")
	span.End()

	ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
	defer cancel()
	if err := tp.ForceFlush(ctx); err != nil {
		t.Fatalf("ForceFlush error = %v", err)
	}

	if gotPath != "/v1/traces" {
		t.Errorf("received request path = %q, want /v1/traces", gotPath)
	}
}

// --- live integration: HTTP protocol works end-to-end for metrics and logs too ---

func Test_OtlpProtocol_Metrics_Http_DeliversToV1Metrics(t *testing.T) {
	var gotPath string
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		gotPath = r.URL.Path
		w.WriteHeader(http.StatusOK)
	}))
	defer srv.Close()

	host := strings.TrimPrefix(srv.URL, "http://")
	opts := &Options{
		ServiceName:      "http-metric-test",
		Environment:      LOCAL,
		OtelEndpoint:     host,
		Insecure:         true,
		ExportTimeoutMs:  10_000,
		SampleRatio:      1.0,
		MetricExporters:  []string{"otlp"},
		ExportIntervalMs: 30_000,
		OtlpProtocol:     ProtocolHTTP,
	}
	mp, _, err := configureMetrics(context.Background(), resource.Default(), opts)
	if err != nil {
		t.Fatalf("configureMetrics error = %v", err)
	}
	defer func() {
		ctx, cancel := context.WithTimeout(context.Background(), time.Second)
		defer cancel()
		_ = mp.Shutdown(ctx)
	}()

	counter, err := mp.Meter("probe").Int64Counter("http_protocol_test")
	if err != nil {
		t.Fatalf("counter error = %v", err)
	}
	counter.Add(context.Background(), 1)

	ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
	defer cancel()
	if err := mp.ForceFlush(ctx); err != nil {
		t.Fatalf("ForceFlush error = %v", err)
	}

	if gotPath != "/v1/metrics" {
		t.Errorf("received request path = %q, want /v1/metrics", gotPath)
	}
}
