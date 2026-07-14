package otelhelper

import (
	"fmt"
	"net/url"
	"os"
	"strconv"
	"strings"
)

// DeploymentEnvironment represents the runtime environment.
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
	EnvCollectorEndpoint    = "OTEL_EXPORTER_OTLP_ENDPOINT"
	EnvInsecure             = "OTEL_EXPORTER_OTLP_INSECURE"
	EnvDebugLevel           = "OTEL_HELPER_DEBUG_LEVEL"
	EnvExtraInstrumentation = "OTEL_HELPER_EXTRA_INSTRUMENTATION"
	EnvSampleRatio          = "OTEL_HELPER_SAMPLE_RATIO"
	EnvDisabledSignals      = "OTEL_HELPER_DISABLED_SIGNALS"
	EnvDisabledMetrics      = "OTEL_HELPER_DISABLED_METRICS"
	EnvMetricsPort          = "OTEL_HELPER_METRICS_PORT"
	EnvMetricsExporter      = "OTEL_METRICS_EXPORTER"
	EnvMetricExportInterval = "OTEL_METRIC_EXPORT_INTERVAL"
	EnvTracesSampler        = "OTEL_TRACES_SAMPLER"
)

const defaultExportIntervalMs = 30_000

func parseEnvironment(s string) DeploymentEnvironment {
	switch strings.ToUpper(strings.ReplaceAll(s, "-", "_")) {
	case "LOCAL":
		return LOCAL
	case "DEV":
		return DEV
	case "HML":
		return HML
	case "PRD":
		return PRD
	default:
		return LOCAL
	}
}

func resolveCollectorHost() string {
	env := strings.TrimRight(strings.TrimSpace(os.Getenv(EnvCollectorEndpoint)), "/")
	if env == "" {
		return ""
	}

	// url.Parse treats "host:port" (no scheme) as scheme="host", yielding an
	// empty Host field. Default to http:// when no scheme is present so
	// endpoints like "collector.svc:4317" resolve correctly.
	if !strings.Contains(env, "://") {
		env = "http://" + env
	}

	u, err := url.Parse(env)
	if err != nil || u.Host == "" {
		return env
	}
	host := u.Hostname()
	port := u.Port()
	if port == "" {
		port = "4317"
	}
	return host + ":" + port
}

func envBool(key string) bool {
	return strings.ToLower(os.Getenv(key)) == "true"
}

func (o *Options) resolveFromEnv() {
	if o.ServiceName == "my-service" {
		if v := os.Getenv(EnvServiceName); v != "" {
			o.ServiceName = v
		} else if v := os.Getenv(EnvOtelServiceName); v != "" {
			o.ServiceName = v
		}
	}

	if o.Environment == LOCAL {
		if v := os.Getenv(EnvEnvironment); v != "" {
			o.Environment = parseEnvironment(v)
		}
	}

	if o.OtelEndpoint == "" {
		o.OtelEndpoint = resolveCollectorHost()
	}

	// Resolve Insecure flag: explicit WithInsecure() takes priority, then env var,
	// then scheme from the raw endpoint URL.
	if !o.insecureExplicit {
		if v := os.Getenv(EnvInsecure); v != "" {
			o.Insecure = strings.ToLower(v) == "true"
		} else {
			raw := strings.TrimSpace(os.Getenv(EnvCollectorEndpoint))
			if strings.HasPrefix(raw, "http://") {
				o.Insecure = true
			}
			// "https://" or no scheme → Insecure stays false (secure by default).
		}
	}

	if !o.DebugLevel {
		o.DebugLevel = envBool(EnvDebugLevel)
	}

	if o.ExtraInstrumentation == "SQL" {
		if v := os.Getenv(EnvExtraInstrumentation); v != "" {
			o.ExtraInstrumentation = v
		}
	}

	// Standard OTel var wins over the proprietary one: when OTEL_TRACES_SAMPLER
	// is set, the SDK's own env handling configures the sampler and
	// OTEL_HELPER_SAMPLE_RATIO is ignored.
	if o.SampleRatio == 1.0 && os.Getenv(EnvTracesSampler) == "" {
		if v := os.Getenv(EnvSampleRatio); v != "" {
			if f, err := strconv.ParseFloat(v, 64); err == nil {
				o.SampleRatio = max(0.0, min(1.0, f))
			}
		}
	}

	if o.MetricExporters == nil {
		if v := strings.TrimSpace(os.Getenv(EnvMetricsExporter)); v != "" {
			for _, e := range strings.Split(v, ",") {
				if t := strings.ToLower(strings.TrimSpace(e)); t != "" {
					o.MetricExporters = append(o.MetricExporters, t)
				}
			}
		}
	} else {
		normalized := make([]string, 0, len(o.MetricExporters))
		for _, e := range o.MetricExporters {
			if t := strings.ToLower(strings.TrimSpace(e)); t != "" {
				normalized = append(normalized, t)
			}
		}
		o.MetricExporters = normalized
	}

	if o.ExportIntervalMs == 0 {
		if v := os.Getenv(EnvMetricExportInterval); v != "" {
			if ms, err := strconv.Atoi(v); err == nil && ms > 0 {
				o.ExportIntervalMs = ms
			}
		}
	}
	if o.ExportIntervalMs == 0 {
		o.ExportIntervalMs = defaultExportIntervalMs
	}

	if len(o.DisabledSignals) == 0 {
		if v := os.Getenv(EnvDisabledSignals); v != "" {
			for _, s := range strings.Split(v, ",") {
				if t := strings.ToLower(strings.TrimSpace(s)); t != "" {
					o.DisabledSignals = append(o.DisabledSignals, t)
				}
			}
		}
	}

	if len(o.DisabledMetrics) == 0 {
		if v := os.Getenv(EnvDisabledMetrics); v != "" {
			for _, s := range strings.Split(v, ",") {
				if t := strings.TrimSpace(s); t != "" {
					o.DisabledMetrics = append(o.DisabledMetrics, t)
				}
			}
		}
	}

	if o.PrometheusMetricsPort == 9464 {
		if v := os.Getenv(EnvMetricsPort); v != "" {
			// 0 is valid: disables the standalone listener (mounted-handler mode).
			if p, err := strconv.Atoi(v); err == nil && p >= 0 {
				o.PrometheusMetricsPort = p
			}
		}
	}
}

func (o *Options) validate() error {
	if strings.TrimSpace(o.ServiceName) == "" {
		return fmt.Errorf("ServiceName is required; set %s env var", EnvServiceName)
	}
	if o.ExportTimeoutMs <= 0 {
		return fmt.Errorf("ExportTimeoutMs must be > 0")
	}
	if o.ExportIntervalMs < 0 {
		return fmt.Errorf("ExportIntervalMs must be > 0")
	}
	if o.MetricExporters != nil {
		for _, e := range o.MetricExporters {
			if e != "otlp" && e != "prometheus" && e != "none" {
				return fmt.Errorf("unknown metric exporter %q in %s; valid values: otlp, prometheus, none", e, EnvMetricsExporter)
			}
		}
		if len(o.MetricExporters) > 1 {
			for _, e := range o.MetricExporters {
				if e == "none" {
					return fmt.Errorf("%q cannot be combined with other metric exporters in %s", "none", EnvMetricsExporter)
				}
			}
		}
	}
	if o.hasMetricExporter("otlp") && o.OtelEndpoint == "" {
		return fmt.Errorf("metric exporter \"otlp\" requires an endpoint; set %s or remove \"otlp\" from %s", EnvCollectorEndpoint, EnvMetricsExporter)
	}
	return nil
}
