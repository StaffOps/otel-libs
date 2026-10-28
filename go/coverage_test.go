package otelhelper

import (
	"context"
	"log/slog"
	"testing"

	"go.opentelemetry.io/otel"
	"go.opentelemetry.io/otel/sdk/resource"
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
		{"WithResourceAttributes", WithResourceAttributes(attrs), func(o *Options) bool {
			return o.ResourceAttributes["team"] == "platform" && o.ResourceAttributes["version"] == "1.0"
		}},
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

// --- slog: NewSlogHandler, NewLogger (behavior: logging_test.go) ---

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

// --- gRPC stats handlers / options: assert non-nil ---

func TestGRPCServerHandler(t *testing.T) {
	if GRPCServerHandler() == nil {
		t.Fatal("GRPCServerHandler returned nil")
	}
}

func TestGRPCClientHandler(t *testing.T) {
	if GRPCClientHandler() == nil {
		t.Fatal("GRPCClientHandler returned nil")
	}
}

func TestGRPCServerOption(t *testing.T) {
	if GRPCServerOption() == nil {
		t.Fatal("GRPCServerOption returned nil")
	}
}

func TestGRPCDialOption(t *testing.T) {
	if GRPCDialOption() == nil {
		t.Fatal("GRPCDialOption returned nil")
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

// The helper's semconv version must match resource.Default()'s, or
// resource.Merge drops the schema URL from every signal.
func TestBuildResourceHasSchemaURL(t *testing.T) {
	res := buildResource(newOptions())
	if res.SchemaURL() == "" {
		t.Fatal("buildResource: empty schema URL (semconv version mismatch with resource.Default())")
	}
	if got, want := res.SchemaURL(), resource.Default().SchemaURL(); got != want {
		t.Errorf("schema URL = %q, want resource.Default()'s %q", got, want)
	}
}

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

// --- resolveCollectorHost edge cases ---

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
