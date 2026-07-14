package otelhelper

// Contract tests for OTEL_METRICS_EXPORTER / OTEL_METRIC_EXPORT_INTERVAL (US-1, US-2, US-6).
// Cross-language parity: python/tests/test_metrics_contract.py and
// dotnet/OtelHelper.Tests/MetricsContractTests.cs assert the same table.

import (
	"context"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/http/httptest"
	"os"
	"reflect"
	"strings"
	"testing"
	"time"

	"go.opentelemetry.io/otel/sdk/resource"
)

func resolvedOptions(t *testing.T, opts ...Option) *Options {
	t.Helper()
	return newOptions(opts...)
}

// --- US-1: exporter resolution table ---

func Test_Exporters_UnsetWithEndpoint_IsOtlp(t *testing.T) {
	o := resolvedOptions(t, WithEndpoint("collector:4317"))
	if got := o.resolvedMetricExporters(); !reflect.DeepEqual(got, []string{"otlp"}) {
		t.Errorf("resolvedMetricExporters() = %v, want [otlp]", got)
	}
}

func Test_Exporters_UnsetWithoutEndpoint_IsPrometheus(t *testing.T) {
	t.Setenv(EnvCollectorEndpoint, "")
	o := resolvedOptions(t)
	if got := o.resolvedMetricExporters(); !reflect.DeepEqual(got, []string{"prometheus"}) {
		t.Errorf("resolvedMetricExporters() = %v, want [prometheus]", got)
	}
}

func Test_Exporters_PrometheusOnly_EvenWithEndpoint(t *testing.T) {
	t.Setenv(EnvMetricsExporter, "prometheus")
	o := resolvedOptions(t, WithEndpoint("collector:4317"))
	if got := o.resolvedMetricExporters(); !reflect.DeepEqual(got, []string{"prometheus"}) {
		t.Errorf("resolvedMetricExporters() = %v, want [prometheus]", got)
	}
}

func Test_Exporters_DualMode(t *testing.T) {
	t.Setenv(EnvMetricsExporter, "otlp,prometheus")
	o := resolvedOptions(t, WithEndpoint("collector:4317"))
	if got := o.resolvedMetricExporters(); !reflect.DeepEqual(got, []string{"otlp", "prometheus"}) {
		t.Errorf("resolvedMetricExporters() = %v, want [otlp prometheus]", got)
	}
}

func Test_Exporters_None_DisablesMetrics(t *testing.T) {
	t.Setenv(EnvMetricsExporter, "none")
	o := resolvedOptions(t, WithEndpoint("collector:4317"), WithServiceName("svc"))
	if err := o.validate(); err != nil {
		t.Fatalf("validate() error = %v", err)
	}
	if got := o.resolvedMetricExporters(); len(got) != 0 {
		t.Errorf("resolvedMetricExporters() = %v, want empty", got)
	}
}

func Test_Exporters_CaseAndWhitespaceTolerant(t *testing.T) {
	t.Setenv(EnvMetricsExporter, " OTLP , Prometheus ")
	o := resolvedOptions(t, WithEndpoint("collector:4317"))
	if got := o.resolvedMetricExporters(); !reflect.DeepEqual(got, []string{"otlp", "prometheus"}) {
		t.Errorf("resolvedMetricExporters() = %v, want [otlp prometheus]", got)
	}
}

func Test_Exporters_ExplicitOptionBeatsEnv(t *testing.T) {
	t.Setenv(EnvMetricsExporter, "otlp")
	o := resolvedOptions(t, WithEndpoint("collector:4317"), WithMetricExporters("prometheus"))
	if got := o.resolvedMetricExporters(); !reflect.DeepEqual(got, []string{"prometheus"}) {
		t.Errorf("resolvedMetricExporters() = %v, want [prometheus]", got)
	}
}

func Test_Exporters_UnknownValue_FailsValidation(t *testing.T) {
	t.Setenv(EnvMetricsExporter, "statsd")
	o := resolvedOptions(t, WithServiceName("svc"))
	err := o.validate()
	if err == nil || !strings.Contains(err.Error(), "statsd") || !strings.Contains(err.Error(), "valid values") {
		t.Errorf("validate() = %v, want unknown-exporter error listing valid values", err)
	}
}

func Test_Exporters_OtlpWithoutEndpoint_FailsValidation(t *testing.T) {
	t.Setenv(EnvCollectorEndpoint, "")
	t.Setenv(EnvMetricsExporter, "otlp")
	o := resolvedOptions(t, WithServiceName("svc"))
	err := o.validate()
	if err == nil || !strings.Contains(err.Error(), "requires an endpoint") {
		t.Errorf("validate() = %v, want otlp-without-endpoint error", err)
	}
}

func Test_Exporters_NoneCombined_FailsValidation(t *testing.T) {
	t.Setenv(EnvMetricsExporter, "none,prometheus")
	o := resolvedOptions(t, WithServiceName("svc"))
	err := o.validate()
	if err == nil || !strings.Contains(err.Error(), "cannot be combined") {
		t.Errorf("validate() = %v, want none-combined error", err)
	}
}

// --- US-2: OTEL_METRIC_EXPORT_INTERVAL precedence ---

func Test_Interval_DefaultIs30s(t *testing.T) {
	o := resolvedOptions(t)
	if o.ExportIntervalMs != 30_000 {
		t.Errorf("ExportIntervalMs = %d, want 30000", o.ExportIntervalMs)
	}
}

func Test_Interval_EnvHonored(t *testing.T) {
	t.Setenv(EnvMetricExportInterval, "5000")
	o := resolvedOptions(t)
	if o.ExportIntervalMs != 5000 {
		t.Errorf("ExportIntervalMs = %d, want 5000", o.ExportIntervalMs)
	}
}

func Test_Interval_ExplicitBeatsEnv(t *testing.T) {
	t.Setenv(EnvMetricExportInterval, "5000")
	o := resolvedOptions(t, WithExportInterval(1234))
	if o.ExportIntervalMs != 1234 {
		t.Errorf("ExportIntervalMs = %d, want 1234", o.ExportIntervalMs)
	}
}

func Test_Interval_InvalidEnvFallsBack(t *testing.T) {
	t.Setenv(EnvMetricExportInterval, "not-a-number")
	o := resolvedOptions(t)
	if o.ExportIntervalMs != 30_000 {
		t.Errorf("ExportIntervalMs = %d, want 30000", o.ExportIntervalMs)
	}
}

// --- US-1/US-6: dual-mode pipeline — same counter in /metrics scrape ---

func Test_DualMode_CounterVisibleOnMetricsHandler(t *testing.T) {
	opts := &Options{
		ServiceName:           "dual-svc",
		Environment:           LOCAL,
		OtelEndpoint:          "localhost:4317",
		Insecure:              true,
		ExportTimeoutMs:       10_000,
		SampleRatio:           1.0,
		MetricExporters:       []string{"otlp", "prometheus"},
		ExportIntervalMs:      30_000,
		PrometheusMetricsPort: 0, // mounted-handler mode
	}
	mp, srvShutdown, err := configureMetrics(context.Background(), resource.Default(), opts)
	if err != nil {
		t.Fatalf("configureMetrics(dual) error = %v", err)
	}
	if srvShutdown != nil {
		t.Error("srvShutdown should be nil when the standalone listener is disabled")
	}
	defer func() {
		ctx, cancel := context.WithTimeout(context.Background(), 100*time.Millisecond)
		defer cancel()
		_ = mp.Shutdown(ctx)
	}()

	counter, err := mp.Meter("contract-test").Int64Counter("dual_mode_hits")
	if err != nil {
		t.Fatalf("counter error = %v", err)
	}
	counter.Add(context.Background(), 7)

	rr := httptest.NewRecorder()
	MetricsHandler().ServeHTTP(rr, httptest.NewRequest(http.MethodGet, "/metrics", nil))
	if rr.Code != http.StatusOK {
		t.Fatalf("MetricsHandler status = %d, want 200", rr.Code)
	}
	body, _ := io.ReadAll(rr.Body)
	if !strings.Contains(string(body), "dual_mode_hits") {
		t.Errorf("/metrics scrape missing counter; body:\n%s", body)
	}
}

func Test_PrometheusOnly_WithEndpoint_NoOtlp(t *testing.T) {
	// prometheus-only with an endpoint set must not create the OTLP reader —
	// behaviorally: configureMetrics succeeds without contacting the endpoint
	// and /metrics serves.
	opts := &Options{
		ServiceName:           "prom-svc",
		Environment:           LOCAL,
		OtelEndpoint:          "localhost:4317",
		ExportTimeoutMs:       10_000,
		SampleRatio:           1.0,
		MetricExporters:       []string{"prometheus"},
		PrometheusMetricsPort: 0,
	}
	mp, _, err := configureMetrics(context.Background(), resource.Default(), opts)
	if err != nil {
		t.Fatalf("configureMetrics(prometheus-only) error = %v", err)
	}
	ctx, cancel := context.WithTimeout(context.Background(), 100*time.Millisecond)
	defer cancel()
	// Shutdown must be instant: no OTLP reader means no export attempt to a
	// dead endpoint (which would exhaust the timeout).
	if err := mp.Shutdown(ctx); err != nil {
		t.Errorf("shutdown error = %v (OTLP reader present?)", err)
	}
}

// --- US-4: listener robustness ---

func Test_Listener_BusyPort_FailsSetupFast(t *testing.T) {
	ln, err := net.Listen("tcp", ":0")
	if err != nil {
		t.Fatalf("net.Listen error = %v", err)
	}
	defer ln.Close()
	busyPort := ln.Addr().(*net.TCPAddr).Port

	opts := &Options{
		ServiceName:           "busy-svc",
		Environment:           LOCAL,
		ExportTimeoutMs:       10_000,
		SampleRatio:           1.0,
		MetricExporters:       []string{"prometheus"},
		PrometheusMetricsPort: busyPort,
	}
	_, _, err = configureMetrics(context.Background(), resource.Default(), opts)
	if err == nil || !strings.Contains(err.Error(), "MetricsHandler") {
		t.Errorf("configureMetrics(busy port) = %v, want actionable listener error", err)
	}
}

func Test_Listener_ServesAndJoinsShutdownChain(t *testing.T) {
	ln, err := net.Listen("tcp", ":0")
	if err != nil {
		t.Fatalf("net.Listen error = %v", err)
	}
	freePort := ln.Addr().(*net.TCPAddr).Port
	ln.Close()

	opts := &Options{
		ServiceName:           "listener-svc",
		Environment:           LOCAL,
		ExportTimeoutMs:       10_000,
		SampleRatio:           1.0,
		MetricExporters:       []string{"prometheus"},
		PrometheusMetricsPort: freePort,
	}
	mp, srvShutdown, err := configureMetrics(context.Background(), resource.Default(), opts)
	if err != nil {
		t.Fatalf("configureMetrics(listener) error = %v", err)
	}
	if srvShutdown == nil {
		t.Fatal("srvShutdown should be non-nil when the listener is active")
	}

	resp, err := http.Get(fmt.Sprintf("http://localhost:%d/metrics", freePort))
	if err != nil {
		t.Fatalf("GET /metrics error = %v", err)
	}
	resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		t.Errorf("GET /metrics status = %d, want 200", resp.StatusCode)
	}

	ctx, cancel := context.WithTimeout(context.Background(), time.Second)
	defer cancel()
	if err := srvShutdown(ctx); err != nil {
		t.Errorf("server shutdown error = %v", err)
	}
	if _, err := http.Get(fmt.Sprintf("http://localhost:%d/metrics", freePort)); err == nil {
		t.Error("listener still serving after shutdown")
	}
	_ = mp.Shutdown(ctx)
}

// --- US-2: sampler precedence (OTEL_TRACES_SAMPLER wins over helper var) ---

func Test_Sampler_StandardVarBeatsHelperVar(t *testing.T) {
	t.Setenv(EnvTracesSampler, "always_off")
	t.Setenv(EnvSampleRatio, "0.25")
	o := resolvedOptions(t)
	if o.SampleRatio != 1.0 {
		t.Errorf("SampleRatio = %v, want 1.0 (helper var ignored when %s is set)", o.SampleRatio, EnvTracesSampler)
	}

	tp, err := configureTracing(context.Background(), resource.Default(), o)
	if err != nil {
		t.Fatalf("configureTracing error = %v", err)
	}
	defer func() { _ = tp.Shutdown(context.Background()) }()
	// always_off from the SDK env handling: spans must not be sampled.
	_, span := tp.Tracer("t").Start(context.Background(), "probe")
	if span.SpanContext().IsSampled() {
		t.Error("span sampled; want always_off from OTEL_TRACES_SAMPLER")
	}
	span.End()
}

func Test_Sampler_HelperVarAppliesWhenStandardUnset(t *testing.T) {
	os.Unsetenv("OTEL_TRACES_SAMPLER")
	t.Setenv(EnvSampleRatio, "0.25")
	o := resolvedOptions(t)
	if o.SampleRatio != 0.25 {
		t.Errorf("SampleRatio = %v, want 0.25", o.SampleRatio)
	}
}

func Test_Sampler_ExplicitRatioBeatsStandardVar(t *testing.T) {
	// Env says always_on; explicit code says ratio 0. If explicit wins (as the
	// precedence rule demands), the span must NOT be sampled.
	t.Setenv(EnvTracesSampler, "always_on")
	o := resolvedOptions(t, WithSampleRatio(0.0))
	tp, err := configureTracing(context.Background(), resource.Default(), o)
	if err != nil {
		t.Fatalf("configureTracing error = %v", err)
	}
	defer func() { _ = tp.Shutdown(context.Background()) }()
	_, span := tp.Tracer("t").Start(context.Background(), "probe")
	if span.SpanContext().IsSampled() {
		t.Error("span sampled; explicit ratio 0.0 should beat OTEL_TRACES_SAMPLER=always_on")
	}
	span.End()
}

// --- US-3: MetricsHandler before setup answers 503 ---

func Test_MetricsHandler_BeforeSetup_Returns503(t *testing.T) {
	setPromRegistry(nil)
	rr := httptest.NewRecorder()
	MetricsHandler().ServeHTTP(rr, httptest.NewRequest(http.MethodGet, "/metrics", nil))
	if rr.Code != http.StatusServiceUnavailable {
		t.Errorf("status = %d, want 503", rr.Code)
	}
}
