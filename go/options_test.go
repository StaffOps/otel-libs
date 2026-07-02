package otelhelper

import "testing"

func TestOptionsDefaults(t *testing.T) {
	o := newOptions()
	if o.ServiceName != "my-service" {
		t.Errorf("ServiceName = %q, want %q", o.ServiceName, "my-service")
	}
	if o.Environment != LOCAL {
		t.Errorf("Environment = %q, want %q", o.Environment, LOCAL)
	}
	if o.SampleRatio != 1.0 {
		t.Errorf("SampleRatio = %f, want 1.0", o.SampleRatio)
	}
	if o.ExportTimeoutMs != 10_000 {
		t.Errorf("ExportTimeoutMs = %d, want 10000", o.ExportTimeoutMs)
	}
	if o.ExtraInstrumentation != "SQL" {
		t.Errorf("ExtraInstrumentation = %q, want %q", o.ExtraInstrumentation, "SQL")
	}
}

func TestOptionsEnvServiceName(t *testing.T) {
	t.Setenv(EnvServiceName, "from-env")
	o := newOptions()
	if o.ServiceName != "from-env" {
		t.Errorf("ServiceName = %q, want %q", o.ServiceName, "from-env")
	}
}

func TestOptionsEnvOtelServiceName(t *testing.T) {
	t.Setenv(EnvOtelServiceName, "otel-name")
	o := newOptions()
	if o.ServiceName != "otel-name" {
		t.Errorf("ServiceName = %q, want %q", o.ServiceName, "otel-name")
	}
}

func TestOptionsServiceNamePriority(t *testing.T) {
	t.Setenv(EnvServiceName, "primary")
	t.Setenv(EnvOtelServiceName, "secondary")
	o := newOptions()
	if o.ServiceName != "primary" {
		t.Errorf("SERVICE_NAME should take priority, got %q", o.ServiceName)
	}
}

func TestOptionsExplicitOverridesEnv(t *testing.T) {
	t.Setenv(EnvServiceName, "from-env")
	o := newOptions(WithServiceName("explicit"))
	if o.ServiceName != "explicit" {
		t.Errorf("Explicit option should override env, got %q", o.ServiceName)
	}
}

func TestOptionsEnvEnvironment(t *testing.T) {
	tests := []struct {
		env  string
		want DeploymentEnvironment
	}{
		{"PRD", PRD},
		{"prd", PRD},
		{"DEV", DEV},
		{"HML", HML},
		{"unknown", LOCAL},
	}
	for _, tt := range tests {
		t.Run(tt.env, func(t *testing.T) {
			t.Setenv(EnvEnvironment, tt.env)
			o := newOptions()
			if o.Environment != tt.want {
				t.Errorf("Environment = %q, want %q", o.Environment, tt.want)
			}
		})
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

func TestOptionsValidateEmptyServiceName(t *testing.T) {
	o := &Options{ServiceName: "", OtelEndpoint: "localhost:4317", ExportTimeoutMs: 10000}
	if err := o.validate(); err == nil {
		t.Error("Expected error for empty ServiceName")
	}
}

func TestOptionsValidateEmptyEndpoint(t *testing.T) {
	o := &Options{ServiceName: "svc", OtelEndpoint: "", ExportTimeoutMs: 10000}
	if err := o.validate(); err != nil {
		t.Errorf("Empty OtelEndpoint should be valid (Prometheus fallback), got: %v", err)
	}
}

func TestOptionsValidateZeroTimeout(t *testing.T) {
	o := &Options{ServiceName: "svc", OtelEndpoint: "localhost:4317", ExportTimeoutMs: 0}
	if err := o.validate(); err == nil {
		t.Error("Expected error for zero ExportTimeoutMs")
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

func TestOptionsEnvEndpoint(t *testing.T) {
	t.Setenv(EnvCollectorEndpoint, "http://collector.monitoring:4317")
	o := newOptions()
	if o.OtelEndpoint != "collector.monitoring:4317" {
		t.Errorf("OtelEndpoint = %q, want %q", o.OtelEndpoint, "collector.monitoring:4317")
	}
}

func TestOptionsEnvEndpointNoPort(t *testing.T) {
	t.Setenv(EnvCollectorEndpoint, "http://collector.monitoring")
	o := newOptions()
	if o.OtelEndpoint != "collector.monitoring:4317" {
		t.Errorf("OtelEndpoint = %q, want %q", o.OtelEndpoint, "collector.monitoring:4317")
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
