package otelhelper

import (
	"strings"

	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/propagation"
)

// Options holds configuration for the OTel helper. Fields are set through the
// With* options; anything left unset is resolved from the environment
// (code > standard OTEL_* env > legacy env > default — design D1).
type Options struct {
	// ServiceName sets service.name in code. Empty = OTEL_SERVICE_NAME >
	// OTEL_RESOURCE_ATTRIBUTES > SERVICE_NAME (deprecated) > SDK default
	// unknown_service:<exe>. After Setup it holds the effective name.
	ServiceName string
	// Environment sets deployment.environment.name in code. Empty =
	// OTEL_RESOURCE_ATTRIBUTES > ENVIRONMENT (deprecated alias) > absent.
	// After Setup it holds the effective value ("" when absent).
	Environment DeploymentEnvironment
	// OtelEndpoint is the generic OTLP endpoint set in code: a URL
	// ("http://collector:4318/otlp") or a schemeless host:port. For
	// http/protobuf "/v1/<signal>" is appended. Empty = SDK env resolution.
	OtelEndpoint string
	// TracesEndpoint, MetricsEndpoint and LogsEndpoint set a per-signal OTLP
	// endpoint in code, used verbatim (no "/v1/<signal>" appended).
	TracesEndpoint  string
	MetricsEndpoint string
	LogsEndpoint    string
	DebugLevel      bool
	// Insecure selects plaintext for schemeless endpoints (WithInsecure). With
	// a URL the scheme decides.
	Insecure bool
	// ExportTimeoutMs is the OTLP export timeout set in code. 0 = not set:
	// OTEL_EXPORTER_OTLP[_<SIGNAL>]_TIMEOUT, else the SDK default (10 s).
	ExportTimeoutMs    int
	SampleRatio        float64
	ResourceAttributes map[string]string
	DisabledSignals    []string
	DisabledMetrics    []string
	// MetricExporters selects the active metric exporters: "otlp",
	// "prometheus", "console", "none". Nil = resolve from
	// OTEL_METRICS_EXPORTER, falling back to legacy inference (otlp when a
	// metrics endpoint is configured, prometheus otherwise).
	MetricExporters []string
	// ExportIntervalMs is the metric export interval for periodic readers.
	// 0 = resolve from OTEL_METRIC_EXPORT_INTERVAL, else 30000.
	ExportIntervalMs int
	// PrometheusMetricsPort is the port for the standalone /metrics listener.
	// Default: 9464 (OTEL_HELPER_METRICS_PORT). 0 disables the listener while
	// keeping the Prometheus reader active for MetricsHandler().
	PrometheusMetricsPort int
	// OtlpProtocol selects the OTLP wire protocol: "grpc" or "http/protobuf".
	// Empty = OTEL_EXPORTER_OTLP_<SIGNAL>_PROTOCOL > OTEL_EXPORTER_OTLP_PROTOCOL
	// > port-4318 inference on that signal's endpoint > grpc.
	OtlpProtocol string
	// Compression set in code: "gzip" or "none". Empty =
	// OTEL_EXPORTER_OTLP[_<SIGNAL>]_COMPRESSION, else gzip (design D1.d).
	Compression string
	// HealthPaths are the HTTP paths whose server spans are dropped (exact,
	// case-insensitive, trailing slash and query ignored; design D5). Nil =
	// DefaultHealthPaths(); empty = no HTTP path is dropped. gRPC
	// grpc.health.v1.Health/* (Check, Watch, List) is always dropped.
	HealthPaths []string

	// Flags recording that a value came from code (sentinels, G10/R8).
	insecureExplicit   bool
	serviceNameSet     bool
	environmentSet     bool
	exportTimeoutSet   bool
	exportIntervalSet  bool
	sampleRatioSet     bool
	disabledSignalsSet bool

	// Resolution results (filled by resolveFromEnv / lazily).
	identityResolved       bool
	codeServiceName        bool
	codeEnvironment        bool
	resourceFallbacks      []attribute.KeyValue
	sdkDisabled            bool
	exports                map[string]*signalExport
	tracesExporters        []string
	logsExporters          []string
	codeMetricExporters    bool
	metricExportersInvalid []string
	// metricIntervalMs is the interval Setup gave the periodic readers
	// (parity seam: read through installedOptions, set only in Setup).
	metricIntervalMs int
	propagator       propagation.TextMapPropagator
	resolveErrs      []error
	warned           map[string]bool
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

// WithServiceName sets service.name, overriding every environment variable.
func WithServiceName(name string) Option {
	return func(o *Options) { o.ServiceName = name; o.serviceNameSet = true }
}

// WithEnvironment sets deployment.environment.name. Values other than
// LOCAL/DEV/HML/PRD are emitted verbatim.
func WithEnvironment(env DeploymentEnvironment) Option {
	return func(o *Options) { o.Environment = env; o.environmentSet = true }
}

// WithDebug enables debug mode (DEBUG log level, debug=true marker on entry
// spans — no parent or a remote parent, ADR-006), overriding
// OTEL_HELPER_DEBUG_LEVEL.
func WithDebug() Option { return func(o *Options) { o.DebugLevel = true } }

// WithEndpoint sets the generic OTLP endpoint (URL or schemeless host:port).
// For http/protobuf the helper appends "/v1/<signal>" to the URL path.
func WithEndpoint(endpoint string) Option { return func(o *Options) { o.OtelEndpoint = endpoint } }

// WithTracesEndpoint sets the traces endpoint URL. Its path is used verbatim (no
// "/v1/traces" appended); the schemeless and portless-gRPC shims still apply.
func WithTracesEndpoint(endpoint string) Option {
	return func(o *Options) { o.TracesEndpoint = endpoint }
}

// WithMetricsEndpoint sets the metrics endpoint URL. Its path is used verbatim (no
// "/v1/metrics" appended); the schemeless and portless-gRPC shims still apply.
func WithMetricsEndpoint(endpoint string) Option {
	return func(o *Options) { o.MetricsEndpoint = endpoint }
}

// WithLogsEndpoint sets the logs endpoint URL. Its path is used verbatim (no
// "/v1/logs" appended); the schemeless and portless-gRPC shims still apply.
func WithLogsEndpoint(endpoint string) Option {
	return func(o *Options) { o.LogsEndpoint = endpoint }
}

// WithOtlpProtocol selects the OTLP wire protocol ("grpc" or "http/protobuf")
// for every signal, overriding the environment and the port-based inference.
func WithOtlpProtocol(protocol string) Option {
	return func(o *Options) { o.OtlpProtocol = protocol }
}

// WithSampleRatio sets the head-sampling ratio in [0, 1] for root spans
// (deprecated alias OTEL_HELPER_SAMPLE_RATIO). Out-of-range or NaN fails Setup.
func WithSampleRatio(ratio float64) Option {
	return func(o *Options) { o.SampleRatio = ratio; o.sampleRatioSet = true }
}

// WithExportTimeout sets the OTLP export timeout (ms) for every signal.
func WithExportTimeout(ms int) Option {
	return func(o *Options) { o.ExportTimeoutMs = ms; o.exportTimeoutSet = true }
}

// WithCompression sets the OTLP compression (CompressionGzip or
// CompressionNone) for every signal, overriding the environment.
func WithCompression(compression string) Option {
	return func(o *Options) { o.Compression = compression }
}

// WithResourceAttributes adds resource attributes set in code; each key wins
// over the same key in OTEL_RESOURCE_ATTRIBUTES.
func WithResourceAttributes(attrs map[string]string) Option {
	return func(o *Options) { o.ResourceAttributes = attrs }
}

// WithDisabledSignals disables "traces", "metrics" and/or "logs"; other
// names fail Setup. Overrides OTEL_HELPER_DISABLED_SIGNALS (deprecated).
func WithDisabledSignals(signals []string) Option {
	return func(o *Options) { o.DisabledSignals = signals; o.disabledSignalsSet = true }
}

// WithDisabledMetrics drops instruments whose name matches any path.Match
// pattern, overriding OTEL_HELPER_DISABLED_METRICS.
func WithDisabledMetrics(patterns []string) Option {
	return func(o *Options) { o.DisabledMetrics = patterns }
}

// WithPrometheusMetricsPort sets the standalone /metrics listener port in
// [0, 65535] (0 = no listener), overriding OTEL_HELPER_METRICS_PORT.
func WithPrometheusMetricsPort(port int) Option {
	return func(o *Options) { o.PrometheusMetricsPort = port }
}

// WithMetricExporters selects the active metric exporters ("otlp",
// "prometheus", "console", "none"), overriding OTEL_METRICS_EXPORTER and the
// legacy endpoint inference.
func WithMetricExporters(exporters ...string) Option {
	return func(o *Options) {
		o.MetricExporters = exporters
		if o.MetricExporters == nil {
			o.MetricExporters = []string{}
		}
	}
}

// WithExportInterval sets the metric export interval in milliseconds,
// overriding OTEL_METRIC_EXPORT_INTERVAL.
func WithExportInterval(ms int) Option {
	return func(o *Options) { o.ExportIntervalMs = ms; o.exportIntervalSet = true }
}

// WithoutMetricsListener disables the standalone /metrics listener. The
// Prometheus reader stays active; mount MetricsHandler() on the app's own mux.
func WithoutMetricsListener() Option {
	return func(o *Options) { o.PrometheusMetricsPort = 0 }
}

// WithHealthPaths replaces DefaultHealthPaths: server spans of requests to
// these paths are not recorded, while the requests still count in the HTTP
// server metrics. Matching is exact and case-insensitive, ignoring a trailing
// slash and the query. WithHealthPaths() with no paths traces every path.
// Each path must be ASCII, start with "/" and carry no query, fragment or
// comma (parity with Python, where a comma would split the upstream list).
// The list also applies to NewHTTPTransport. gRPC health RPCs are dropped
// regardless.
func WithHealthPaths(paths ...string) Option {
	return func(o *Options) {
		o.HealthPaths = append([]string{}, paths...)
	}
}

// healthPaths returns (a copy of) the effective health path list.
func (o *Options) healthPaths() []string {
	if o.HealthPaths == nil {
		return DefaultHealthPaths()
	}
	return append([]string{}, o.HealthPaths...)
}

// WithInsecure selects plaintext (true) or TLS (false) for schemeless
// endpoints, overriding OTEL_EXPORTER_OTLP_INSECURE. It must agree with the
// scheme of an endpoint URL set in code.
func WithInsecure(insecure bool) Option {
	return func(o *Options) { o.Insecure = insecure; o.insecureExplicit = true }
}

// resolvedMetricExporters returns the active metric exporters after resolution.
// "none" (or an empty list) resolves to nil (metrics disabled).
func (o *Options) resolvedMetricExporters() []string {
	if o.MetricExporters == nil {
		if o.export(signalMetrics).configured {
			return []string{"otlp"}
		}
		return []string{"prometheus"}
	}
	var out []string
	for _, e := range o.MetricExporters {
		if e != "none" {
			out = append(out, e)
		}
	}
	if len(out) != len(o.MetricExporters) {
		return nil // a "none" entry disables metrics
	}
	return out
}

func (o *Options) hasMetricExporter(name string) bool {
	return containsStr(o.resolvedMetricExporters(), name)
}

// signalExporters returns the exporters for traces or logs: OTEL_<S>_EXPORTER,
// default "otlp".
func (o *Options) signalExporters(signal string) []string {
	list := o.tracesExporters
	if signal == signalLogs {
		list = o.logsExporters
	}
	if list == nil {
		return []string{"otlp"}
	}
	return list
}

func newOptions(opts ...Option) *Options {
	o := &Options{
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
