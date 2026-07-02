package otelhelper

import "strings"

// Options holds configuration for the OTel helper.
type Options struct {
	ServiceName          string
	Environment          DeploymentEnvironment
	OtelEndpoint         string
	DebugLevel           bool
	Insecure             bool
	ExtraInstrumentation string
	ExportTimeoutMs      int
	SampleRatio          float64
	ResourceAttributes   map[string]string
	DisabledSignals      []string
	DisabledMetrics      []string
	// PrometheusMetricsPort is the port for /metrics when no OTLP endpoint is configured.
	// Default: 9464. Configurable via OTEL_HELPER_METRICS_PORT env var.
	PrometheusMetricsPort int

	// insecureExplicit tracks whether WithInsecure was called explicitly,
	// preventing env/scheme auto-detection from overriding the consumer's choice.
	insecureExplicit bool
}

// IsSignalEnabled returns true if the signal is not in DisabledSignals (case-insensitive).
func (o *Options) IsSignalEnabled(signal string) bool {
	for _, s := range o.DisabledSignals {
		if strings.EqualFold(s, signal) {
			return false
		}
	}
	return true
}

// Option is a functional option for Setup.
type Option func(*Options)

func WithServiceName(name string) Option          { return func(o *Options) { o.ServiceName = name } }
func WithEnvironment(env DeploymentEnvironment) Option { return func(o *Options) { o.Environment = env } }
func WithDebug() Option                           { return func(o *Options) { o.DebugLevel = true } }
func WithEndpoint(endpoint string) Option         { return func(o *Options) { o.OtelEndpoint = endpoint } }
func WithSampleRatio(ratio float64) Option        { return func(o *Options) { o.SampleRatio = ratio } }
func WithExportTimeout(ms int) Option             { return func(o *Options) { o.ExportTimeoutMs = ms } }
func WithExtraInstrumentation(instr string) Option {
	return func(o *Options) { o.ExtraInstrumentation = instr }
}
func WithResourceAttributes(attrs map[string]string) Option {
	return func(o *Options) { o.ResourceAttributes = attrs }
}
func WithDisabledSignals(signals []string) Option {
	return func(o *Options) { o.DisabledSignals = signals }
}
func WithDisabledMetrics(patterns []string) Option {
	return func(o *Options) { o.DisabledMetrics = patterns }
}
func WithPrometheusMetricsPort(port int) Option {
	return func(o *Options) { o.PrometheusMetricsPort = port }
}
func WithInsecure(insecure bool) Option {
	return func(o *Options) { o.Insecure = insecure; o.insecureExplicit = true }
}

// HasInstrumentation checks if a named instrumentation is enabled.
func (o *Options) HasInstrumentation(name string) bool {
	if o.DebugLevel {
		return true
	}
	for _, part := range strings.Split(o.ExtraInstrumentation, ",") {
		if strings.EqualFold(strings.TrimSpace(part), name) {
			return true
		}
	}
	return false
}

func newOptions(opts ...Option) *Options {
	o := &Options{
		ServiceName:           "my-service",
		Environment:           LOCAL,
		ExtraInstrumentation:  "SQL",
		ExportTimeoutMs:       10_000,
		SampleRatio:           1.0,
		ResourceAttributes:    make(map[string]string),
		PrometheusMetricsPort: 9464,
	}
	for _, opt := range opts {
		opt(o)
	}
	o.resolveFromEnv()
	return o
}
