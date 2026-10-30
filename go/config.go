package otelhelper

import (
	"crypto/rand"
	"errors"
	"fmt"
	"math"
	"os"
	"strconv"
	"strings"

	"go.opentelemetry.io/otel/propagation"
	semconv "go.opentelemetry.io/otel/semconv/v1.43.0"
)

// DeploymentEnvironment is the value of deployment.environment.name. The four
// constants below switch helper behavior (log level); any other value is
// emitted verbatim and behaves like a non-LOCAL environment (ADR-003).
type DeploymentEnvironment string

const (
	LOCAL DeploymentEnvironment = "LOCAL"
	DEV   DeploymentEnvironment = "DEV"
	HML   DeploymentEnvironment = "HML"
	PRD   DeploymentEnvironment = "PRD"
)

// Environment variable names.
const (
	EnvServiceName          = "SERVICE_NAME"
	EnvOtelServiceName      = "OTEL_SERVICE_NAME"
	EnvEnvironment          = "ENVIRONMENT"
	EnvResourceAttributes   = "OTEL_RESOURCE_ATTRIBUTES"
	EnvCollectorEndpoint    = "OTEL_EXPORTER_OTLP_ENDPOINT"
	EnvInsecure             = "OTEL_EXPORTER_OTLP_INSECURE"
	EnvDebugLevel           = "OTEL_HELPER_DEBUG_LEVEL"
	EnvExtraInstrumentation = "OTEL_HELPER_EXTRA_INSTRUMENTATION"
	EnvSampleRatio          = "OTEL_HELPER_SAMPLE_RATIO"
	EnvDisabledSignals      = "OTEL_HELPER_DISABLED_SIGNALS"
	EnvDisabledMetrics      = "OTEL_HELPER_DISABLED_METRICS"
	EnvMetricsPort          = "OTEL_HELPER_METRICS_PORT"
	EnvMetricsExporter      = "OTEL_METRICS_EXPORTER"
	EnvTracesExporter       = "OTEL_TRACES_EXPORTER"
	EnvLogsExporter         = "OTEL_LOGS_EXPORTER"
	EnvMetricExportInterval = "OTEL_METRIC_EXPORT_INTERVAL"
	EnvTracesSampler        = "OTEL_TRACES_SAMPLER"
	EnvTracesSamplerArg     = "OTEL_TRACES_SAMPLER_ARG"
	EnvOtlpProtocol         = "OTEL_EXPORTER_OTLP_PROTOCOL"
	EnvSDKDisabled          = "OTEL_SDK_DISABLED"
	EnvPropagators          = "OTEL_PROPAGATORS"
)

const (
	defaultExportIntervalMs = 30_000
	defaultOtlpGRPCPort     = "4317"
	defaultOtlpHTTPPort     = "4318"
)

// OTLP wire protocol values. "http/json" is a valid spec value but not
// implemented by the Go SDK's OTLP exporters.
const (
	ProtocolGRPC = "grpc"
	ProtocolHTTP = "http/protobuf"
)

// OTLP compression values (WithCompression, OTEL_EXPORTER_OTLP_COMPRESSION).
const (
	CompressionGzip = "gzip"
	CompressionNone = "none"
)

const (
	signalTraces  = "traces"
	signalMetrics = "metrics"
	signalLogs    = "logs"
)

var allSignals = []string{signalTraces, signalMetrics, signalLogs}

// otlpEnv returns OTEL_EXPORTER_OTLP_<suffix> (signal "") or
// OTEL_EXPORTER_OTLP_<SIGNAL>_<suffix>.
func otlpEnv(signal, suffix string) string {
	if signal == "" {
		return "OTEL_EXPORTER_OTLP_" + suffix
	}
	return "OTEL_EXPORTER_OTLP_" + strings.ToUpper(signal) + "_" + suffix
}

// wsCutset is the exact set the helper trims, identical in every language
// (design D1 "Trimmed character set"): not Unicode whitespace.
const wsCutset = " \t\n\r\v\f"

// trimWS trims wsCutset from both ends.
func trimWS(s string) string { return strings.Trim(s, wsCutset) }

// parseRatio accepts a plain decimal in [0, 1] ("0", "0.25", "1.0"): the
// timeout grammar (decimalMs), so no hex, sign, exponent, underscore,
// Inf/NaN spelling or non-ASCII digits (design D3).
func parseRatio(raw string) (float64, bool) {
	if !decimalMs.MatchString(raw) {
		return 0, false
	}
	f, err := strconv.ParseFloat(raw, 64)
	if err != nil || f > 1 {
		return 0, false
	}
	return f, true
}

// knownEnvironment reports whether s (case-insensitive) is one of the four
// behavior-switching environments.
func knownEnvironment(s string) (DeploymentEnvironment, bool) {
	switch e := DeploymentEnvironment(strings.ToUpper(trimWS(s))); e {
	case LOCAL, DEV, HML, PRD:
		return e, true
	}
	return "", false
}

// envBool parses a boolean env var: "true"/"false" (case-insensitive). Any
// other non-empty value warns and reads as false — the SDKs' own reading of
// such values — with set=true.
func envBool(key string) (value, set bool) {
	raw := trimWS(os.Getenv(key))
	if raw == "" {
		return false, false
	}
	switch strings.ToLower(raw) {
	case "true":
		return true, true
	case "false":
		return false, true
	}
	warnEnv("unparseable boolean", key, raw, "false")
	return false, true
}

// newInstanceID returns a random RFC 4122 version 4 UUID.
func newInstanceID() string {
	var b [16]byte
	_, _ = rand.Read(b[:]) // crypto/rand.Read never fails (Go ≥ 1.24)
	b[6] = (b[6] & 0x0f) | 0x40
	b[8] = (b[8] & 0x3f) | 0x80
	return fmt.Sprintf("%x-%x-%x-%x-%x", b[0:4], b[4:6], b[6:8], b[8:10], b[10:])
}

// resolveFromEnv fills everything code left unset from the environment. Env
// values that can't be used are warned and replaced by the default; invalid
// code values are left for validate() to reject (R8).
func (o *Options) resolveFromEnv() {
	if v, set := envBool(EnvSDKDisabled); set {
		o.sdkDisabled = v
	}

	o.resolveIdentity()

	if !o.DebugLevel {
		o.DebugLevel, _ = envBool(EnvDebugLevel)
	}

	o.warnDeprecatedEnv()

	// Standard OTel var wins over the deprecated helper one: while
	// OTEL_TRACES_SAMPLER is set (non-blank), buildSampler parses it and
	// OTEL_HELPER_SAMPLE_RATIO is ignored. Same plain-decimal grammar as
	// OTEL_TRACES_SAMPLER_ARG (design D3).
	if !o.sampleRatioSet && trimWS(os.Getenv(EnvTracesSampler)) == "" {
		if v := trimWS(os.Getenv(EnvSampleRatio)); v != "" {
			if f, ok := parseRatio(v); ok {
				o.SampleRatio = f
				o.sampleRatioFromEnv = true
			} else {
				warnEnv("sample ratio is not a number in [0, 1]", EnvSampleRatio, v, "1.0")
			}
		}
	}

	for _, s := range allSignals {
		o.export(s)
	}
	if o.tracesExporters == nil {
		o.tracesExporters = parseExporterEnv(EnvTracesExporter, "otlp", "console")
	}
	if o.logsExporters == nil {
		o.logsExporters = parseExporterEnv(EnvLogsExporter, "otlp", "console")
	}
	o.resolveMetricExporters()

	if o.ExportIntervalMs == 0 && !o.exportIntervalSet {
		if v := trimWS(os.Getenv(EnvMetricExportInterval)); v != "" {
			if ms, err := strconv.Atoi(v); err == nil && ms > 0 {
				o.ExportIntervalMs = ms
			} else {
				warnEnv("invalid metric export interval (positive integer milliseconds)", EnvMetricExportInterval, v, strconv.Itoa(defaultExportIntervalMs))
			}
		}
	}
	if o.ExportIntervalMs == 0 && !o.exportIntervalSet {
		o.ExportIntervalMs = defaultExportIntervalMs
	}

	if len(o.DisabledSignals) == 0 && !o.disabledSignalsSet {
		for _, s := range splitList(os.Getenv(EnvDisabledSignals), true) {
			if containsStr(allSignals, s) {
				o.DisabledSignals = append(o.DisabledSignals, s)
			} else {
				warnEnv("unknown signal, ignored", EnvDisabledSignals, s, "traces, metrics, logs")
			}
		}
	}
	if !o.disabledMetricsSet {
		o.DisabledMetrics = splitList(os.Getenv(EnvDisabledMetrics), false)
	}

	if !o.metricsPortSet {
		if v := trimWS(os.Getenv(EnvMetricsPort)); v != "" {
			// 0 is valid: disables the standalone listener (mounted-handler mode).
			if p, err := strconv.Atoi(v); err == nil && p >= 0 && p <= 65535 {
				o.PrometheusMetricsPort = p
			} else {
				warnEnv("invalid port", EnvMetricsPort, v, "9464")
			}
		}
	}

	o.propagator = parsePropagators(os.Getenv(EnvPropagators))
	o.warnIgnoredTimeouts()
}

// warnDeprecatedEnv warns, once per process, about every deprecated or dead
// variable that is set (design D8, G9). It warns even when a standard variable
// wins, so the legacy one can be removed before 1.0.
func (o *Options) warnDeprecatedEnv() {
	for _, d := range []struct{ variable, replacement string }{
		{EnvServiceName, EnvOtelServiceName},
		{EnvEnvironment, EnvResourceAttributes + " (deployment.environment.name=<value>)"},
		{EnvSampleRatio, EnvTracesSampler + "=parentbased_traceidratio with " + EnvTracesSamplerArg},
		{EnvDisabledSignals, EnvTracesExporter + "/" + EnvMetricsExporter + "/" + EnvLogsExporter + "=none"},
	} {
		if trimWS(os.Getenv(d.variable)) != "" {
			warnDeprecated(d.variable, d.replacement)
		}
	}
	if trimWS(os.Getenv(EnvExtraInstrumentation)) != "" {
		warnUnused(EnvExtraInstrumentation, "import the ext/otelsql, ext/otelredis or ext/otelaws module instead")
	}
}

func splitList(v string, lower bool) []string {
	var out []string
	for _, s := range strings.Split(v, ",") {
		t := trimWS(s)
		if lower {
			t = strings.ToLower(t)
		}
		if t != "" {
			out = append(out, t)
		}
	}
	return out
}

// resolveIdentity decides service.name, deployment.environment.name and
// service.instance.id (design D1 table and 2026-10-29 rows, D4, ADR-003). The
// helper parses OTEL_RESOURCE_ATTRIBUTES itself (envResource); the SDK's
// resource.Default() is not used. A blank value (empty or contract-trim-set
// only) counts as not provided at every level, so the next one applies. The
// dedicated code option beats the same key in code ResourceAttributes.
func (o *Options) resolveIdentity() {
	o.identityResolved = true
	parsed := envResourceAttributes()
	o.envResource, o.envResourceEcho = parsed.attrs, parsed.sdkWouldEcho()
	code := o.ResourceAttributes

	// service.name: code option > code attribute > OTEL_SERVICE_NAME >
	// OTEL_RESOURCE_ATTRIBUTES > SERVICE_NAME (deprecated) > SDK default
	// unknown_service:<exe> + warning.
	nameKey := string(semconv.ServiceNameKey)
	switch name := firstNonBlank(o.ServiceName, code[nameKey], os.Getenv(EnvOtelServiceName),
		o.envResource[nameKey], os.Getenv(EnvServiceName)); {
	case o.serviceNameSet && trimWS(o.ServiceName) == "":
		// Invalid code value: kept so validate() rejects it (R8).
	case name != "":
		o.ServiceName = name
	default:
		o.ServiceName = defaultServiceName()
		warnOncePerProcess("identity:service.name", "otelhelper: no service name configured; set "+EnvOtelServiceName,
			"using", o.ServiceName)
	}

	// deployment.environment.name: code option > code attribute >
	// OTEL_RESOURCE_ATTRIBUTES > ENVIRONMENT (deprecated alias, known values
	// canonicalized) > absent + warning. Emitted verbatim.
	envKey := string(semconv.DeploymentEnvironmentNameKey)
	alias := trimWS(os.Getenv(EnvEnvironment))
	switch v := firstNonBlank(string(o.Environment), code[envKey], o.envResource[envKey]); {
	case o.environmentSet && trimWS(string(o.Environment)) == "":
		// Invalid code value: kept so validate() rejects it (R8).
	case v != "":
		o.Environment = DeploymentEnvironment(v)
	case alias != "":
		if known, ok := knownEnvironment(alias); ok {
			alias = string(known)
		}
		o.Environment = DeploymentEnvironment(alias)
	default:
		o.Environment = ""
		warnOncePerProcess("identity:environment", "otelhelper: deployment.environment.name is not set; set it in "+
			EnvResourceAttributes+" (e.g. deployment.environment.name=PRD); log level defaults to INFO", "using", "absent")
	}

	// service.instance.id: code > OTEL_RESOURCE_ATTRIBUTES > per-process UUID (D4).
	idKey := string(semconv.ServiceInstanceIDKey)
	o.serviceInstanceID = firstNonBlank(code[idKey], o.envResource[idKey])
	if o.serviceInstanceID == "" {
		o.serviceInstanceID = newInstanceID()
	}

	// telemetry.sdk.* set in code are ignored (the SDK sets them): warned once
	// per process, naming the key only. Env values are ignored silently.
	for k := range code {
		if isTelemetrySDKKey(k) {
			warnOncePerProcess("code-telemetry-sdk:"+k, "otelhelper: "+k+" in WithResourceAttributes is ignored; the OpenTelemetry SDK sets it",
				"key", k)
		}
	}
}

// firstNonBlank returns the first value that is not blank, trimmed with the
// contract set, or "" when every value is blank.
func firstNonBlank(values ...string) string {
	for _, v := range values {
		if t := trimWS(v); t != "" {
			return t
		}
	}
	return ""
}

// parseExporterEnv parses OTEL_{TRACES,METRICS,LOGS}_EXPORTER (R8). nil means
// unset (use the helper default); an empty non-nil slice means "none".
// "logging" is the deprecated spelling of "console"; "none" together with
// other values warns and wins; unknown values warn and are dropped.
func parseExporterEnv(variable string, allowed ...string) []string {
	raw := trimWS(os.Getenv(variable))
	if raw == "" {
		return nil
	}
	list, _ := normalizeExporters(raw, variable, allowed, false)
	return list
}

// normalizeExporters parses an exporter list (nil = only unknown values, an
// empty slice = none). strict (code values) reports problems instead of
// warning, so validate() can fail fast.
func normalizeExporters(raw, variable string, allowed []string, strict bool) (list, problems []string) {
	out := []string{}
	hasNone := false
	seen := map[string]bool{}
	for _, part := range strings.Split(raw, ",") {
		v := strings.ToLower(trimWS(part))
		switch {
		case v == "":
			continue
		case v == "none":
			hasNone = true
			continue
		case v == "logging":
			warnEnv(`deprecated exporter value "logging"; use "console"`, variable, v, "console")
			v = "console"
		case !containsStr(allowed, v):
			if strict {
				problems = append(problems, fmt.Sprintf("unknown metric exporter %q in %s; valid values: %s, none", v, variable, strings.Join(allowed, ", ")))
			} else {
				warnEnv("unknown exporter, ignored", variable, v, strings.Join(allowed, ", "))
			}
			continue
		}
		if !seen[v] {
			seen[v] = true
			out = append(out, v)
		}
	}
	if hasNone {
		if len(out) > 0 {
			if strict {
				problems = append(problems, fmt.Sprintf(`"none" cannot be combined with other metric exporters in %s`, variable))
			} else {
				warnEnv(`"none" combined with other exporters; treating the signal as disabled`, variable, raw, "none")
			}
		}
		return []string{}, problems
	}
	if len(out) == 0 {
		return nil, problems
	}
	return out, problems
}

func containsStr(list []string, v string) bool {
	for _, s := range list {
		if s == v {
			return true
		}
	}
	return false
}

// resolveMetricExporters resolves code / OTEL_METRICS_EXPORTER (design D2 of
// metrics-exporter-contract + R2/R8). Code values are only normalized here;
// validate() rejects the invalid ones.
func (o *Options) resolveMetricExporters() {
	if o.MetricExporters != nil {
		o.codeMetricExporters = true
		list, problems := normalizeExporters(strings.Join(o.MetricExporters, ","), "WithMetricExporters",
			[]string{"otlp", "prometheus", "console"}, true)
		o.metricExportersInvalid = problems
		if list == nil {
			list = []string{}
		}
		o.MetricExporters = list
		return
	}
	list := parseExporterEnv(EnvMetricsExporter, "otlp", "prometheus", "console")
	if containsStr(list, "otlp") && !o.export(signalMetrics).configured {
		warnEnv(`"otlp" requires an OTLP endpoint (OTEL_EXPORTER_OTLP_ENDPOINT or OTEL_EXPORTER_OTLP_METRICS_ENDPOINT); ignored`,
			EnvMetricsExporter, "otlp", "endpoint-based inference")
		kept := []string{}
		for _, e := range list {
			if e != "otlp" {
				kept = append(kept, e)
			}
		}
		list = kept
		if len(list) == 0 {
			list = nil
		}
	}
	o.MetricExporters = list
}

// parsePropagators honors OTEL_PROPAGATORS (R8). The helper links only the
// W3C propagators: tracecontext and baggage; "none" disables propagation.
// Other spec values (b3, jaeger, xray, ottrace) are not linked into the core
// module and are warned and skipped.
func parsePropagators(raw string) propagation.TextMapPropagator {
	def := propagation.NewCompositeTextMapPropagator(propagation.TraceContext{}, propagation.Baggage{})
	raw = trimWS(raw)
	if raw == "" {
		return def
	}
	var props []propagation.TextMapPropagator
	none := false
	seen := map[string]bool{}
	for _, part := range strings.Split(raw, ",") {
		v := strings.ToLower(trimWS(part))
		if v == "" || seen[v] {
			continue
		}
		seen[v] = true
		switch v {
		case "tracecontext":
			props = append(props, propagation.TraceContext{})
		case "baggage":
			props = append(props, propagation.Baggage{})
		case "none":
			none = true
		case "b3", "b3multi", "jaeger", "xray", "ottrace":
			warnEnv("propagator not supported by the Go helper, ignored", EnvPropagators, v, "tracecontext, baggage")
		default:
			warnEnv("unknown propagator, ignored", EnvPropagators, v, "tracecontext, baggage")
		}
	}
	if none {
		if len(props) > 0 {
			warnEnv(`"none" combined with other propagators; propagation disabled`, EnvPropagators, raw, "none")
		}
		return propagation.NewCompositeTextMapPropagator()
	}
	if len(props) == 0 {
		return def
	}
	return propagation.NewCompositeTextMapPropagator(props...)
}

// validate rejects invalid values set in code (R8: fail fast, naming the
// option). Invalid env values were already warned and replaced.
func (o *Options) validate() error {
	var errs []error
	seen := map[string]bool{}
	for _, err := range o.resolveErrs { // a generic code endpoint is resolved once per signal
		if !seen[err.Error()] {
			seen[err.Error()] = true
			errs = append(errs, err)
		}
	}
	if o.serviceNameSet && trimWS(o.ServiceName) == "" {
		errs = append(errs, errors.New("WithServiceName: service name must not be empty"))
	}
	if o.environmentSet && trimWS(string(o.Environment)) == "" {
		errs = append(errs, errors.New("WithEnvironment: environment must not be empty"))
	}
	for k := range o.ResourceAttributes {
		if trimWS(k) == "" {
			errs = append(errs, errors.New("WithResourceAttributes: resource attributes have an empty key"))
			break
		}
	}
	if (o.exportTimeoutSet && o.ExportTimeoutMs <= 0) || o.ExportTimeoutMs < 0 || o.ExportTimeoutMs > maxExportTimeoutMs {
		// [1 ms, 24 h], the env clamp range (D1 ruling "Code export timeout bound", A18).
		errs = append(errs, fmt.Errorf("WithExportTimeout: timeout must be in [1, %d] ms, got %d", maxExportTimeoutMs, o.ExportTimeoutMs))
	}
	if (o.exportIntervalSet && o.ExportIntervalMs <= 0) || o.ExportIntervalMs < 0 {
		errs = append(errs, fmt.Errorf("WithExportInterval: interval must be > 0 ms, got %d", o.ExportIntervalMs))
	}
	if o.sampleRatioSet && (math.IsNaN(o.SampleRatio) || o.SampleRatio < 0 || o.SampleRatio > 1) {
		errs = append(errs, fmt.Errorf("WithSampleRatio: sample_ratio must be in [0, 1], got %v", o.SampleRatio))
	}
	if o.PrometheusMetricsPort < 0 || o.PrometheusMetricsPort > 65535 {
		errs = append(errs, fmt.Errorf("WithPrometheusMetricsPort: port must be in [0, 65535], got %d", o.PrometheusMetricsPort))
	}
	if o.disabledSignalsSet {
		for _, s := range o.DisabledSignals {
			if !containsStr(allSignals, strings.ToLower(trimWS(s))) {
				errs = append(errs, fmt.Errorf("WithDisabledSignals: unknown signal %q; valid values: traces, metrics, logs", s))
			}
		}
	}
	switch strings.ToLower(trimWS(o.Compression)) {
	case "", CompressionGzip, CompressionNone:
	default:
		errs = append(errs, fmt.Errorf("WithCompression: unknown compression %q; valid values: gzip, none", o.Compression))
	}
	if p := strings.ToLower(trimWS(o.OtlpProtocol)); p != "" && p != ProtocolGRPC && p != ProtocolHTTP {
		errs = append(errs, fmt.Errorf("WithOtlpProtocol: unknown OTLP protocol %q; valid values: %s, %s", o.OtlpProtocol, ProtocolGRPC, ProtocolHTTP))
	}
	for _, p := range o.HealthPaths {
		if !strings.HasPrefix(p, "/") || strings.ContainsAny(p, "?#,") || !isASCII(p) {
			errs = append(errs, fmt.Errorf(`WithHealthPaths: path %q must be ASCII, start with "/" and have no query, fragment or comma`, p))
		}
	}
	if o.codeMetricExporters {
		for _, p := range o.metricExportersInvalid {
			errs = append(errs, errors.New(p))
		}
		if containsStr(o.MetricExporters, "otlp") && !o.export(signalMetrics).configured {
			errs = append(errs, fmt.Errorf(`WithMetricExporters: metric exporter "otlp" requires an endpoint; set %s or remove "otlp"`, EnvCollectorEndpoint))
		}
	}
	return errors.Join(errs...)
}

// defaultPortFor returns the IANA OTLP port of a protocol.
func defaultPortFor(protocol string) string {
	if protocol == ProtocolHTTP {
		return defaultOtlpHTTPPort
	}
	return defaultOtlpGRPCPort
}

// isASCII reports whether s has only ASCII bytes (health paths are matched
// case-insensitively; non-ASCII case folding differs between languages).
func isASCII(s string) bool {
	for i := 0; i < len(s); i++ {
		if s[i] >= 0x80 {
			return false
		}
	}
	return true
}
