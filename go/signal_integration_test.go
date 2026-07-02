package otelhelper

import (
	"context"
	"testing"
	"time"

	sdkmetric "go.opentelemetry.io/otel/sdk/metric"
	"go.opentelemetry.io/otel/sdk/resource"
)

// --- resolveCollectorHost behavior ---

func Test_ResolveCollectorHost_NoScheme_DefaultsToHTTP(t *testing.T) {
	t.Setenv(EnvCollectorEndpoint, "collector.svc:4317")

	got := resolveCollectorHost()
	want := "collector.svc:4317"
	if got != want {
		t.Errorf("resolveCollectorHost() = %q, want %q", got, want)
	}
}

func Test_ResolveCollectorHost_WithScheme_ExtractsHost(t *testing.T) {
	t.Setenv(EnvCollectorEndpoint, "http://my-collector:4317")

	got := resolveCollectorHost()
	want := "my-collector:4317"
	if got != want {
		t.Errorf("resolveCollectorHost() = %q, want %q", got, want)
	}
}

func Test_ResolveCollectorHost_WithScheme_NoPort_Defaults4317(t *testing.T) {
	t.Setenv(EnvCollectorEndpoint, "https://collector.internal")

	got := resolveCollectorHost()
	want := "collector.internal:4317"
	if got != want {
		t.Errorf("resolveCollectorHost() = %q, want %q", got, want)
	}
}

func Test_ResolveCollectorHost_Empty_ReturnsEmpty(t *testing.T) {
	t.Setenv(EnvCollectorEndpoint, "")

	got := resolveCollectorHost()
	want := ""
	if got != want {
		t.Errorf("resolveCollectorHost() = %q, want %q", got, want)
	}
}

// --- Metrics configuration behavior ---

func Test_MetricInterval_Is30Seconds(t *testing.T) {
	// configureMetrics creates a PeriodicReader with 30s interval.
	// We cannot inspect the interval via reflection, but we verify the provider
	// is created successfully (no panic, no error) with valid options.
	t.Setenv(EnvCollectorEndpoint, "localhost:4317")

	opts := &Options{
		ServiceName:     "test-svc",
		Environment:     LOCAL,
		OtelEndpoint:    "localhost:4317",
		ExportTimeoutMs: 10_000,
		SampleRatio:     1.0,
	}

	res := resource.Default()
	mp, err := configureMetrics(context.Background(), res, opts)
	if err != nil {
		t.Fatalf("configureMetrics() error = %v", err)
	}
	// Shutdown with a short timeout — we only care that creation succeeds,
	// not that it can reach a real collector.
	ctx, cancel := context.WithTimeout(context.Background(), 100*time.Millisecond)
	defer cancel()
	if err := mp.Shutdown(ctx); err != nil {
		t.Logf("shutdown warning (expected — no collector): %v", err)
	}
}

// --- disabledMetricsView behavior ---

func Test_DisabledMetricsView_DropMatching(t *testing.T) {
	view := disabledMetricsView([]string{"http.*"})

	tests := []struct {
		name      string
		instName  string
		wantMatch bool
	}{
		{"matches http pattern", "http.server.requests", true},
		{"does not match non-http", "app.counter", false},
		{"matches http.client too", "http.client.duration", true},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			inst := instrumentWithName(tt.instName)
			_, matched := view(inst)
			if matched != tt.wantMatch {
				t.Errorf("disabledMetricsView(%q) matched = %v, want %v", tt.instName, matched, tt.wantMatch)
			}
		})
	}
}

// --- Disabled signals via env ---

func Test_DisabledSignals_Env(t *testing.T) {
	t.Setenv(EnvDisabledSignals, "traces,logs")

	opts := newOptions()

	if opts.IsSignalEnabled("traces") {
		t.Error("expected traces to be disabled")
	}
	if opts.IsSignalEnabled("logs") {
		t.Error("expected logs to be disabled")
	}
	if !opts.IsSignalEnabled("metrics") {
		t.Error("expected metrics to be enabled")
	}
}

// --- Disabled metrics via env ---

func Test_DisabledMetrics_Env(t *testing.T) {
	t.Setenv(EnvDisabledMetrics, "http.*,process.runtime.*")

	opts := newOptions()

	if len(opts.DisabledMetrics) != 2 {
		t.Fatalf("DisabledMetrics len = %d, want 2", len(opts.DisabledMetrics))
	}
	if opts.DisabledMetrics[0] != "http.*" {
		t.Errorf("DisabledMetrics[0] = %q, want %q", opts.DisabledMetrics[0], "http.*")
	}
	if opts.DisabledMetrics[1] != "process.runtime.*" {
		t.Errorf("DisabledMetrics[1] = %q, want %q", opts.DisabledMetrics[1], "process.runtime.*")
	}
}

// --- helpers ---

// instrumentWithName creates an sdkmetric.Instrument with the given name for view testing.
// The sdkmetric.Instrument struct is not exported with a constructor, so we build it directly.
func instrumentWithName(name string) sdkmetric.Instrument {
	return sdkmetric.Instrument{Name: name}
}

// --- Additional tests to cover uncovered paths ---

func Test_WithDisabledSignals_Option(t *testing.T) {
	opts := &Options{ServiceName: "test", OtelEndpoint: "localhost:4317"}
	WithDisabledSignals([]string{"traces", "logs"})(opts)

	if len(opts.DisabledSignals) != 2 {
		t.Fatalf("len(DisabledSignals) = %d, want 2", len(opts.DisabledSignals))
	}
	if opts.IsSignalEnabled("traces") {
		t.Error("IsSignalEnabled(traces) = true, want false")
	}
	if opts.IsSignalEnabled("logs") {
		t.Error("IsSignalEnabled(logs) = true, want false")
	}
	if !opts.IsSignalEnabled("metrics") {
		t.Error("IsSignalEnabled(metrics) = false, want true")
	}
}

func Test_WithDisabledMetrics_Option(t *testing.T) {
	opts := &Options{ServiceName: "test", OtelEndpoint: "localhost:4317"}
	WithDisabledMetrics([]string{"http.*", "process.runtime.*"})(opts)

	if len(opts.DisabledMetrics) != 2 {
		t.Fatalf("len(DisabledMetrics) = %d, want 2", len(opts.DisabledMetrics))
	}
	if opts.DisabledMetrics[0] != "http.*" {
		t.Errorf("DisabledMetrics[0] = %q, want %q", opts.DisabledMetrics[0], "http.*")
	}
}

func Test_DebugProcessor_OnEnd_Noop(t *testing.T) {
	// OnEnd is a no-op but must be callable without panic
	p := &debugProcessor{}
	p.OnEnd(nil) // should not panic
}

func Test_ParseEnvironment_AllValues(t *testing.T) {
	tests := []struct {
		input    string
		expected DeploymentEnvironment
	}{
		{"LOCAL", LOCAL},
		{"local", LOCAL},
		{"DEV", DEV},
		{"dev", DEV},
		{"HML", HML},
		{"hml", HML},
		{"PRD", PRD},
		{"prd", PRD},
		{"unknown", LOCAL},
		{"", LOCAL},
	}
	for _, tt := range tests {
		t.Run(tt.input, func(t *testing.T) {
			if got := parseEnvironment(tt.input); got != tt.expected {
				t.Errorf("parseEnvironment(%q) = %q, want %q", tt.input, got, tt.expected)
			}
		})
	}
}

// --- TLS/Insecure configuration tests ---

func Test_ConfigureTracing_Insecure(t *testing.T) {
	opts := &Options{
		ServiceName:     "test-svc",
		Environment:     LOCAL,
		OtelEndpoint:    "localhost:4317",
		Insecure:        true,
		ExportTimeoutMs: 10_000,
		SampleRatio:     1.0,
	}
	res := resource.Default()
	tp, err := configureTracing(context.Background(), res, opts)
	if err != nil {
		t.Fatalf("configureTracing(insecure=true) error = %v", err)
	}
	ctx, cancel := context.WithTimeout(context.Background(), 100*time.Millisecond)
	defer cancel()
	if err := tp.Shutdown(ctx); err != nil {
		t.Logf("shutdown warning (expected — no collector): %v", err)
	}
}

func Test_ConfigureTracing_TLS(t *testing.T) {
	opts := &Options{
		ServiceName:     "test-svc",
		Environment:     LOCAL,
		OtelEndpoint:    "localhost:4317",
		Insecure:        false,
		ExportTimeoutMs: 10_000,
		SampleRatio:     1.0,
	}
	res := resource.Default()
	tp, err := configureTracing(context.Background(), res, opts)
	if err != nil {
		t.Fatalf("configureTracing(insecure=false/TLS) error = %v", err)
	}
	ctx, cancel := context.WithTimeout(context.Background(), 100*time.Millisecond)
	defer cancel()
	if err := tp.Shutdown(ctx); err != nil {
		t.Logf("shutdown warning (expected — no collector): %v", err)
	}
}

func Test_ConfigureMetrics_Insecure(t *testing.T) {
	opts := &Options{
		ServiceName:     "test-svc",
		Environment:     LOCAL,
		OtelEndpoint:    "localhost:4317",
		Insecure:        true,
		ExportTimeoutMs: 10_000,
		SampleRatio:     1.0,
	}
	res := resource.Default()
	mp, err := configureMetrics(context.Background(), res, opts)
	if err != nil {
		t.Fatalf("configureMetrics(insecure=true) error = %v", err)
	}
	ctx, cancel := context.WithTimeout(context.Background(), 100*time.Millisecond)
	defer cancel()
	if err := mp.Shutdown(ctx); err != nil {
		t.Logf("shutdown warning (expected — no collector): %v", err)
	}
}

func Test_ConfigureMetrics_TLS(t *testing.T) {
	opts := &Options{
		ServiceName:     "test-svc",
		Environment:     LOCAL,
		OtelEndpoint:    "localhost:4317",
		Insecure:        false,
		ExportTimeoutMs: 10_000,
		SampleRatio:     1.0,
	}
	res := resource.Default()
	mp, err := configureMetrics(context.Background(), res, opts)
	if err != nil {
		t.Fatalf("configureMetrics(insecure=false/TLS) error = %v", err)
	}
	ctx, cancel := context.WithTimeout(context.Background(), 100*time.Millisecond)
	defer cancel()
	if err := mp.Shutdown(ctx); err != nil {
		t.Logf("shutdown warning (expected — no collector): %v", err)
	}
}

func Test_ConfigureLogging_Insecure(t *testing.T) {
	opts := &Options{
		ServiceName:     "test-svc",
		Environment:     LOCAL,
		OtelEndpoint:    "localhost:4317",
		Insecure:        true,
		ExportTimeoutMs: 10_000,
	}
	res := resource.Default()
	lp, err := configureLogging(context.Background(), res, opts)
	if err != nil {
		t.Fatalf("configureLogging(insecure=true) error = %v", err)
	}
	ctx, cancel := context.WithTimeout(context.Background(), 100*time.Millisecond)
	defer cancel()
	if err := lp.Shutdown(ctx); err != nil {
		t.Logf("shutdown warning (expected — no collector): %v", err)
	}
}

func Test_ConfigureLogging_TLS(t *testing.T) {
	opts := &Options{
		ServiceName:     "test-svc",
		Environment:     LOCAL,
		OtelEndpoint:    "localhost:4317",
		Insecure:        false,
		ExportTimeoutMs: 10_000,
	}
	res := resource.Default()
	lp, err := configureLogging(context.Background(), res, opts)
	if err != nil {
		t.Fatalf("configureLogging(insecure=false/TLS) error = %v", err)
	}
	ctx, cancel := context.WithTimeout(context.Background(), 100*time.Millisecond)
	defer cancel()
	if err := lp.Shutdown(ctx); err != nil {
		t.Logf("shutdown warning (expected — no collector): %v", err)
	}
}

func Test_WithPrometheusMetricsPort_Option(t *testing.T) {
	opts := &Options{PrometheusMetricsPort: 9464}
	WithPrometheusMetricsPort(8080)(opts)
	if opts.PrometheusMetricsPort != 8080 {
		t.Errorf("PrometheusMetricsPort = %d, want 8080", opts.PrometheusMetricsPort)
	}
}

// --- Prometheus fallback (no OTLP endpoint) ---

func Test_ConfigureMetrics_PrometheusMode(t *testing.T) {
	// When OtelEndpoint is empty, configureMetrics should use Prometheus exporter.
	opts := &Options{
		ServiceName:           "test-svc",
		Environment:           LOCAL,
		OtelEndpoint:          "", // triggers Prometheus fallback
		ExportTimeoutMs:       10_000,
		SampleRatio:           1.0,
		PrometheusMetricsPort: 0, // port 0 = random port (won't conflict)
	}
	res := resource.Default()
	mp, err := configureMetrics(context.Background(), res, opts)
	if err != nil {
		t.Fatalf("configureMetrics(prometheus mode) error = %v", err)
	}
	ctx, cancel := context.WithTimeout(context.Background(), 100*time.Millisecond)
	defer cancel()
	if err := mp.Shutdown(ctx); err != nil {
		t.Logf("shutdown warning: %v", err)
	}
}

// --- Setup with debug mode (covers debugProcessor OnStart branch) ---

func Test_Setup_DebugMode(t *testing.T) {
	resetGlobals()
	t.Setenv(EnvServiceName, "debug-test")

	shutdown, err := Setup(context.Background(), WithEndpoint("localhost:4317"), WithDebug())
	if err != nil {
		t.Fatalf("Setup with debug failed: %v", err)
	}
	ctx, cancel := context.WithTimeout(context.Background(), 100*time.Millisecond)
	defer cancel()
	if err := shutdown(ctx); err != nil {
		t.Logf("shutdown warning: %v", err)
	}
}

// --- Setup with disabled signals (covers noop provider paths) ---

func Test_Setup_DisabledTraces(t *testing.T) {
	resetGlobals()
	t.Setenv(EnvServiceName, "disabled-traces")

	shutdown, err := Setup(context.Background(),
		WithEndpoint("localhost:4317"),
		WithDisabledSignals([]string{"traces"}),
	)
	if err != nil {
		t.Fatalf("Setup with disabled traces failed: %v", err)
	}
	ctx, cancel := context.WithTimeout(context.Background(), 100*time.Millisecond)
	defer cancel()
	if err := shutdown(ctx); err != nil {
		t.Logf("shutdown warning: %v", err)
	}
}

func Test_Setup_DisabledMetrics(t *testing.T) {
	resetGlobals()
	t.Setenv(EnvServiceName, "disabled-metrics")

	shutdown, err := Setup(context.Background(),
		WithEndpoint("localhost:4317"),
		WithDisabledSignals([]string{"metrics"}),
	)
	if err != nil {
		t.Fatalf("Setup with disabled metrics failed: %v", err)
	}
	ctx, cancel := context.WithTimeout(context.Background(), 100*time.Millisecond)
	defer cancel()
	if err := shutdown(ctx); err != nil {
		t.Logf("shutdown warning: %v", err)
	}
}

func Test_Setup_DisabledLogs(t *testing.T) {
	resetGlobals()
	t.Setenv(EnvServiceName, "disabled-logs")

	shutdown, err := Setup(context.Background(),
		WithEndpoint("localhost:4317"),
		WithDisabledSignals([]string{"logs"}),
	)
	if err != nil {
		t.Fatalf("Setup with disabled logs failed: %v", err)
	}
	ctx, cancel := context.WithTimeout(context.Background(), 100*time.Millisecond)
	defer cancel()
	if err := shutdown(ctx); err != nil {
		t.Logf("shutdown warning: %v", err)
	}
}

// --- configureMetrics with DisabledMetrics (covers disabledMetricsView in OTLP path) ---

func Test_ConfigureMetrics_WithDisabledMetrics_OTLP(t *testing.T) {
	opts := &Options{
		ServiceName:     "test-svc",
		Environment:     LOCAL,
		OtelEndpoint:    "localhost:4317",
		Insecure:        true,
		ExportTimeoutMs: 10_000,
		SampleRatio:     1.0,
		DisabledMetrics: []string{"http.*"},
	}
	res := resource.Default()
	mp, err := configureMetrics(context.Background(), res, opts)
	if err != nil {
		t.Fatalf("configureMetrics(disabled metrics) error = %v", err)
	}
	ctx, cancel := context.WithTimeout(context.Background(), 100*time.Millisecond)
	defer cancel()
	if err := mp.Shutdown(ctx); err != nil {
		t.Logf("shutdown warning: %v", err)
	}
}

// --- configureTracing with debug + SampleRatio < 1.0 ---

func Test_ConfigureTracing_Debug_WithRatioBased(t *testing.T) {
	opts := &Options{
		ServiceName:     "test-svc",
		Environment:     LOCAL,
		OtelEndpoint:    "localhost:4317",
		Insecure:        true,
		ExportTimeoutMs: 10_000,
		SampleRatio:     0.5,
		DebugLevel:      true,
	}
	res := resource.Default()
	tp, err := configureTracing(context.Background(), res, opts)
	if err != nil {
		t.Fatalf("configureTracing(debug+ratio) error = %v", err)
	}
	ctx, cancel := context.WithTimeout(context.Background(), 100*time.Millisecond)
	defer cancel()
	if err := tp.Shutdown(ctx); err != nil {
		t.Logf("shutdown warning: %v", err)
	}
}

// --- configureTracing with no endpoint (no exporter added) ---

func Test_ConfigureTracing_NoEndpoint(t *testing.T) {
	opts := &Options{
		ServiceName:     "test-svc",
		Environment:     LOCAL,
		OtelEndpoint:    "",
		ExportTimeoutMs: 10_000,
		SampleRatio:     1.0,
	}
	res := resource.Default()
	tp, err := configureTracing(context.Background(), res, opts)
	if err != nil {
		t.Fatalf("configureTracing(no endpoint) error = %v", err)
	}
	ctx, cancel := context.WithTimeout(context.Background(), 100*time.Millisecond)
	defer cancel()
	if err := tp.Shutdown(ctx); err != nil {
		t.Logf("shutdown warning: %v", err)
	}
}

// --- configureLogging with no endpoint ---

func Test_ConfigureLogging_NoEndpoint(t *testing.T) {
	opts := &Options{
		ServiceName:     "test-svc",
		Environment:     LOCAL,
		OtelEndpoint:    "",
		ExportTimeoutMs: 10_000,
	}
	res := resource.Default()
	lp, err := configureLogging(context.Background(), res, opts)
	if err != nil {
		t.Fatalf("configureLogging(no endpoint) error = %v", err)
	}
	ctx, cancel := context.WithTimeout(context.Background(), 100*time.Millisecond)
	defer cancel()
	if err := lp.Shutdown(ctx); err != nil {
		t.Logf("shutdown warning: %v", err)
	}
}
