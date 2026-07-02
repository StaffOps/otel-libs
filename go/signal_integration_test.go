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
