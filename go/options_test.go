package otelhelper

import "testing"

func TestOptionsExplicitOverridesEnv(t *testing.T) {
	t.Setenv(EnvServiceName, "from-env")
	o := newOptions(WithServiceName("explicit"))
	if o.ServiceName != "explicit" {
		t.Errorf("Explicit option should override env, got %q", o.ServiceName)
	}
}

func TestOptionsEnvSampleRatio(t *testing.T) {
	t.Setenv(EnvSampleRatio, "0.5")
	o := newOptions()
	if o.SampleRatio != 0.5 {
		t.Errorf("SampleRatio = %f, want 0.5", o.SampleRatio)
	}
}

func TestOptionsEnvSampleRatioClamped(t *testing.T) {
	t.Setenv(EnvSampleRatio, "2.0")
	o := newOptions()
	if o.SampleRatio != 1.0 {
		t.Errorf("SampleRatio should be clamped to 1.0, got %f", o.SampleRatio)
	}
}

func TestOptionsEnvDebugLevel(t *testing.T) {
	t.Setenv(EnvDebugLevel, "true")
	o := newOptions()
	if !o.DebugLevel {
		t.Error("DebugLevel should be true")
	}
}

func TestHasInstrumentation(t *testing.T) {
	o := &Options{ExtraInstrumentation: "SQL,REDIS"}
	if !o.HasInstrumentation("sql") {
		t.Error("Should match SQL case-insensitive")
	}
	if !o.HasInstrumentation("REDIS") {
		t.Error("Should match REDIS")
	}
	if o.HasInstrumentation("AWS") {
		t.Error("Should not match AWS")
	}
}

func TestHasInstrumentationDebugEnablesAll(t *testing.T) {
	o := &Options{ExtraInstrumentation: "SQL", DebugLevel: true}
	if !o.HasInstrumentation("AWS") {
		t.Error("Debug mode should enable all instrumentations")
	}
}

func TestIsSignalEnabledTrueWhenEmpty(t *testing.T) {
	opts := &Options{DisabledSignals: nil}
	if !opts.IsSignalEnabled("traces") {
		t.Error("expected traces enabled")
	}
	if !opts.IsSignalEnabled("metrics") {
		t.Error("expected metrics enabled")
	}
	if !opts.IsSignalEnabled("logs") {
		t.Error("expected logs enabled")
	}
}

func TestIsSignalEnabledFalseWhenInList(t *testing.T) {
	opts := &Options{DisabledSignals: []string{"metrics"}}
	if opts.IsSignalEnabled("metrics") {
		t.Error("expected metrics disabled")
	}
	if !opts.IsSignalEnabled("traces") {
		t.Error("expected traces enabled")
	}
}

func TestIsSignalEnabledCaseInsensitive(t *testing.T) {
	opts := &Options{DisabledSignals: []string{"Metrics", "TRACES"}}
	if opts.IsSignalEnabled("metrics") {
		t.Error("expected metrics disabled")
	}
	if opts.IsSignalEnabled("traces") {
		t.Error("expected traces disabled")
	}
	if !opts.IsSignalEnabled("logs") {
		t.Error("expected logs enabled")
	}
}

func TestEnvResolvesDisabledSignals(t *testing.T) {
	t.Setenv("OTEL_HELPER_DISABLED_SIGNALS", "metrics,logs")
	opts := newOptions()
	if opts.IsSignalEnabled("metrics") {
		t.Error("expected metrics disabled")
	}
	if opts.IsSignalEnabled("logs") {
		t.Error("expected logs disabled")
	}
	if !opts.IsSignalEnabled("traces") {
		t.Error("expected traces enabled")
	}
}

func TestEnvResolvesDisabledMetrics(t *testing.T) {
	t.Setenv("OTEL_HELPER_DISABLED_METRICS", "http.server.*,rpc.client.*")
	opts := newOptions()
	if len(opts.DisabledMetrics) != 2 {
		t.Fatalf("expected 2 patterns, got %d", len(opts.DisabledMetrics))
	}
	if opts.DisabledMetrics[0] != "http.server.*" {
		t.Errorf("expected http.server.*, got %s", opts.DisabledMetrics[0])
	}
}

func TestDisabledMetricsEmptyWhenEnvNotSet(t *testing.T) {
	opts := newOptions()
	if len(opts.DisabledMetrics) != 0 {
		t.Errorf("expected empty, got %v", opts.DisabledMetrics)
	}
}
