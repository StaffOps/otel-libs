package otelhelper

import (
	"bytes"
	"context"
	"log/slog"
	"testing"

	"go.opentelemetry.io/otel"
)

// --- Functional options (With*) table-driven ---

func TestWithOptions(t *testing.T) {
	attrs := map[string]string{"team": "platform", "version": "1.0"}
	tests := []struct {
		name   string
		opt    Option
		assert func(*Options) bool
	}{
		{"WithEnvironment", WithEnvironment(PRD), func(o *Options) bool { return o.Environment == PRD }},
		{"WithDebug", WithDebug(), func(o *Options) bool { return o.DebugLevel }},
		{"WithSampleRatio", WithSampleRatio(0.3), func(o *Options) bool { return o.SampleRatio == 0.3 }},
		{"WithExportTimeout", WithExportTimeout(5000), func(o *Options) bool { return o.ExportTimeoutMs == 5000 }},
		{"WithExtraInstrumentation", WithExtraInstrumentation("SQL,AWS"), func(o *Options) bool { return o.ExtraInstrumentation == "SQL,AWS" }},
		{"WithResourceAttributes", WithResourceAttributes(attrs), func(o *Options) bool { return o.ResourceAttributes["team"] == "platform" && o.ResourceAttributes["version"] == "1.0" }},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			o := &Options{ResourceAttributes: make(map[string]string)}
			tt.opt(o)
			if !tt.assert(o) {
				t.Errorf("%s did not set expected value", tt.name)
			}
		})
	}
}

// --- GetTracer / GetMeter ---

func TestGetTracer(t *testing.T) {
	tr := GetTracer()
	if tr == nil {
		t.Fatal("GetTracer() returned nil")
	}
	tr2 := GetTracer("custom-tracer")
	if tr2 == nil {
		t.Fatal("GetTracer(custom) returned nil")
	}
}

func TestGetMeter(t *testing.T) {
	m := GetMeter()
	if m == nil {
		t.Fatal("GetMeter() returned nil")
	}
	m2 := GetMeter("custom-meter")
	if m2 == nil {
		t.Fatal("GetMeter(custom) returned nil")
	}
}

// --- GetTracer/GetMeter with empty string (should use default) ---

func TestGetTracerEmptyString(t *testing.T) {
	tr := GetTracer("")
	if tr == nil {
		t.Fatal("GetTracer('') returned nil")
	}
}

func TestGetMeterEmptyString(t *testing.T) {
	m := GetMeter("")
	if m == nil {
		t.Fatal("GetMeter('') returned nil")
	}
}

// --- slog: NewSlogHandler, NewLogger, levelFilterHandler ---

func TestNewSlogHandler(t *testing.T) {
	h := NewSlogHandler()
	if h == nil {
		t.Fatal("NewSlogHandler returned nil")
	}
}

func TestNewLogger(t *testing.T) {
	l := NewLogger(DEV, false)
	if l == nil {
		t.Fatal("NewLogger returned nil")
	}
}

func TestDefaultLogLevelUnknownEnv(t *testing.T) {
	got := DefaultLogLevel(DeploymentEnvironment("STAGING"), false)
	if got != slog.LevelInfo {
		t.Errorf("DefaultLogLevel(unknown) = %v, want Info", got)
	}
}

func TestLevelFilterHandlerEnabled(t *testing.T) {
	var buf bytes.Buffer
	inner := slog.NewTextHandler(&buf, &slog.HandlerOptions{Level: slog.LevelDebug})
	h := levelFilterHandler{level: slog.LevelWarn, inner: inner}

	if h.Enabled(context.Background(), slog.LevelInfo) {
		t.Error("Info should not be enabled when level is Warn")
	}
	if !h.Enabled(context.Background(), slog.LevelWarn) {
		t.Error("Warn should be enabled when level is Warn")
	}
	if !h.Enabled(context.Background(), slog.LevelError) {
		t.Error("Error should be enabled when level is Warn")
	}
}

func TestLevelFilterHandlerHandle(t *testing.T) {
	var buf bytes.Buffer
	inner := slog.NewTextHandler(&buf, &slog.HandlerOptions{Level: slog.LevelDebug})
	h := levelFilterHandler{level: slog.LevelDebug, inner: inner}

	logger := slog.New(h)
	logger.Info("hello")

	if buf.Len() == 0 {
		t.Error("Handle should write through to inner handler")
	}
}

func TestLevelFilterHandlerWithAttrs(t *testing.T) {
	var buf bytes.Buffer
	inner := slog.NewTextHandler(&buf, &slog.HandlerOptions{Level: slog.LevelDebug})
	h := levelFilterHandler{level: slog.LevelInfo, inner: inner}

	h2 := h.WithAttrs([]slog.Attr{slog.String("key", "val")})
	if h2 == nil {
		t.Fatal("WithAttrs returned nil")
	}
	lf, ok := h2.(levelFilterHandler)
	if !ok {
		t.Fatal("WithAttrs should return levelFilterHandler")
	}
	if lf.level != slog.LevelInfo {
		t.Error("WithAttrs should preserve level")
	}
}

func TestLevelFilterHandlerWithGroup(t *testing.T) {
	var buf bytes.Buffer
	inner := slog.NewTextHandler(&buf, &slog.HandlerOptions{Level: slog.LevelDebug})
	h := levelFilterHandler{level: slog.LevelWarn, inner: inner}

	h2 := h.WithGroup("grp")
	if h2 == nil {
		t.Fatal("WithGroup returned nil")
	}
	lf, ok := h2.(levelFilterHandler)
	if !ok {
		t.Fatal("WithGroup should return levelFilterHandler")
	}
	if lf.level != slog.LevelWarn {
		t.Error("WithGroup should preserve level")
	}
}

// --- gRPC interceptors: assert non-nil ---

func TestUnaryServerInterceptor(t *testing.T) {
	if UnaryServerInterceptor() == nil {
		t.Fatal("UnaryServerInterceptor returned nil")
	}
}

func TestStreamServerInterceptor(t *testing.T) {
	if StreamServerInterceptor() == nil {
		t.Fatal("StreamServerInterceptor returned nil")
	}
}

func TestUnaryClientInterceptor(t *testing.T) {
	if UnaryClientInterceptor() == nil {
		t.Fatal("UnaryClientInterceptor returned nil")
	}
}

func TestStreamClientInterceptor(t *testing.T) {
	if StreamClientInterceptor() == nil {
		t.Fatal("StreamClientInterceptor returned nil")
	}
}

// --- InstrumentSQL tests removed ---
// SQL instrumentation moved to ext/otelsql sub-module.
// See ext/otelsql/ for the actual implementation and its own tests.

// --- processors: OnEnd / ForceFlush ---

func TestDebugProcessorOnEnd(t *testing.T) {
	p := &debugProcessor{}
	// OnEnd is a no-op, just assert no panic
	p.OnEnd(nil)
}

func TestDebugProcessorForceFlush(t *testing.T) {
	p := &debugProcessor{}
	if err := p.ForceFlush(context.Background()); err != nil {
		t.Errorf("ForceFlush should return nil, got %v", err)
	}
}

func TestDebugProcessorShutdown(t *testing.T) {
	p := &debugProcessor{}
	if err := p.Shutdown(context.Background()); err != nil {
		t.Errorf("Shutdown should return nil, got %v", err)
	}
}

// --- buildResource ---

func TestBuildResource(t *testing.T) {
	opts := &Options{
		ServiceName:        "test-svc",
		Environment:        PRD,
		ResourceAttributes: map[string]string{"custom.key": "custom.val"},
	}
	res := buildResource(opts)
	if res == nil {
		t.Fatal("buildResource returned nil")
	}

	// Check attributes
	attrs := res.Attributes()
	found := map[string]string{}
	for _, a := range attrs {
		found[string(a.Key)] = a.Value.AsString()
	}
	if found["service.name"] != "test-svc" {
		t.Errorf("service.name = %q, want %q", found["service.name"], "test-svc")
	}
	if found["deployment.environment.name"] != "PRD" {
		t.Errorf("deployment.environment.name = %q, want %q", found["deployment.environment.name"], "PRD")
	}
	if found["custom.key"] != "custom.val" {
		t.Errorf("custom.key = %q, want %q", found["custom.key"], "custom.val")
	}
}

// --- parseEnvironment default case ---

func TestParseEnvironmentDefault(t *testing.T) {
	tests := []struct {
		input string
		want  DeploymentEnvironment
	}{
		{"STAGING", LOCAL},
		{"production", LOCAL},
		{"", LOCAL},
		{"PRD", PRD},
		{"dev", DEV},
		{"hml", HML},
	}
	for _, tt := range tests {
		t.Run(tt.input, func(t *testing.T) {
			got := parseEnvironment(tt.input)
			if got != tt.want {
				t.Errorf("parseEnvironment(%q) = %q, want %q", tt.input, got, tt.want)
			}
		})
	}
}

// --- resolveCollectorHost edge cases ---

func TestResolveCollectorHostEmpty(t *testing.T) {
	t.Setenv(EnvCollectorEndpoint, "")
	got := resolveCollectorHost()
	if got != "" {
		t.Errorf("resolveCollectorHost() = %q, want %q", got, "")
	}
}

func TestResolveCollectorHostBareHost(t *testing.T) {
	t.Setenv(EnvCollectorEndpoint, "myhost:4317")
	got := resolveCollectorHost()
	// No scheme means url.Parse won't extract Host, falls through to return raw
	if got != "myhost:4317" {
		t.Errorf("resolveCollectorHost() = %q, want %q", got, "myhost:4317")
	}
}

// --- Setup with global tracer provider set ---

func TestSetupSetsGlobalTracerProvider(t *testing.T) {
	resetGlobals()
	t.Setenv(EnvServiceName, "global-tp-test")

	shutdown, err := Setup(context.Background(), WithEndpoint("localhost:4317"))
	if err != nil {
		t.Fatalf("Setup failed: %v", err)
	}
	defer shutdown(context.Background())

	// After Setup, global tracer provider should produce non-noop tracers
	tp := otel.GetTracerProvider()
	if tp == nil {
		t.Fatal("Global TracerProvider is nil after Setup")
	}
}
