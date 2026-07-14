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
	// MetricExporters selects the active metric exporters: "otlp", "prometheus",
	// "none". Nil = resolve from OTEL_METRICS_EXPORTER, falling back to legacy
	// inference (otlp when an endpoint is set, prometheus otherwise).
	MetricExporters []string
	// ExportIntervalMs is the metric export interval for the OTLP reader.
	// 0 = resolve from OTEL_METRIC_EXPORT_INTERVAL, else 30000.
	ExportIntervalMs int
	// PrometheusMetricsPort is the port for the standalone /metrics listener.
	// Default: 9464 (OTEL_HELPER_METRICS_PORT). 0 disables the listener while
	// keeping the Prometheus reader active for MetricsHandler().
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

// WithMetricExporters selects the active metric exporters ("otlp", "prometheus",
// "none"), overriding OTEL_METRICS_EXPORTER and the legacy endpoint inference.
func WithMetricExporters(exporters ...string) Option {
	return func(o *Options) { o.MetricExporters = exporters }
}

// WithExportInterval sets the OTLP metric export interval in milliseconds,
// overriding OTEL_METRIC_EXPORT_INTERVAL.
func WithExportInterval(ms int) Option {
	return func(o *Options) { o.ExportIntervalMs = ms }
}

// WithoutMetricsListener disables the standalone /metrics listener. The
// Prometheus reader stays active; mount MetricsHandler() on the app's own mux.
func WithoutMetricsListener() Option {
	return func(o *Options) { o.PrometheusMetricsPort = 0 }
}
func WithInsecure(insecure bool) Option {
	return func(o *Options) { o.Insecure = insecure; o.insecureExplicit = true }
}

// resolvedMetricExporters returns the active metric exporters after resolution.
// "none" resolves to an empty list (metrics disabled).
func (o *Options) resolvedMetricExporters() []string {
	if o.MetricExporters == nil {
		if o.OtelEndpoint != "" {
			return []string{"otlp"}
		}
		return []string{"prometheus"}
	}
	if len(o.MetricExporters) == 1 && o.MetricExporters[0] == "none" {
		return nil
	}
	return o.MetricExporters
}

func (o *Options) hasMetricExporter(name string) bool {
	for _, e := range o.resolvedMetricExporters() {
		if e == name {
			return true
		}
	}
	return false
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
