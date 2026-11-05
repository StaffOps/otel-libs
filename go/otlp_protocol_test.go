package otelhelper

// OTLP wire protocol selection (grpc vs http/protobuf) — tracing, metrics, logging.
//
// Covers the port-based auto-detection (4318 -> http/protobuf),
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

// --- validation ---

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
	mp, _, _, err := configureMetrics(context.Background(), resource.Default(), opts)
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
