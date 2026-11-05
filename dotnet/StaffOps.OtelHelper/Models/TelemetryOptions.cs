using System.ComponentModel.DataAnnotations;
using Microsoft.Extensions.Logging;
using OpenTelemetry.Trace;

namespace StaffOps.OtelHelper
{
    /// <summary>
    /// Configuration options for StaffOps.OtelHelper telemetry.
    /// This is a POCO — environment variable resolution is handled by TelemetryOptionsPostConfigure.
    /// </summary>
    public class TelemetryOptions
    {
        /// <summary>Environment variable name: OTLP collector endpoint for every signal. Unset: metrics fall back to the Prometheus <c>/metrics</c> endpoint and logs to stdout.</summary>
        public const string CollectorEndpointEnvVar = "OTEL_EXPORTER_OTLP_ENDPOINT";
        /// <summary>Environment variable name: Legacy service name variable, read before <see cref="OtelServiceNameEnvVar"/>.</summary>
        public const string ServiceNameEnvVar = "SERVICE_NAME";
        /// <summary>Environment variable name: Standard OpenTelemetry service name variable, read when <see cref="ServiceNameEnvVar"/> is unset.</summary>
        public const string OtelServiceNameEnvVar = "OTEL_SERVICE_NAME";
        /// <summary>Environment variable name: Deployment environment (<c>LOCAL</c>, <c>DEV</c>, <c>HML</c>, <c>PRD</c>); see <see cref="DeploymentEnvironment"/>.</summary>
        public const string EnvironmentEnvVar = "ENVIRONMENT";
        /// <summary>Environment variable name: Debug mode switch (<c>true</c>/<c>false</c>); see <see cref="DebugLevel"/>.</summary>
        public const string DebugLevelEnvVar = "OTEL_HELPER_DEBUG_LEVEL";
        /// <summary>Environment variable name: Comma-separated extra instrumentations; see <see cref="ExtraInstrumentation"/>.</summary>
        public const string ExtraInstrumentationEnvVar = "OTEL_HELPER_EXTRA_INSTRUMENTATION";
        /// <summary>Environment variable name: Trace sample ratio in [0, 1], used only when no sampler is set in code and <see cref="TracesSamplerEnvVar"/> is unset.</summary>
        public const string SampleRatioEnvVar = "OTEL_HELPER_SAMPLE_RATIO";
        /// <summary>Environment variable name: Comma-separated signals to disable (<c>traces</c>, <c>metrics</c>, <c>logs</c>); see <see cref="DisabledSignals"/>.</summary>
        public const string DisabledSignalsEnvVar = "OTEL_HELPER_DISABLED_SIGNALS";
        /// <summary>Environment variable name: Comma-separated instrument names or wildcard patterns to drop; see <see cref="DisabledMetrics"/>.</summary>
        public const string DisabledMetricsEnvVar = "OTEL_HELPER_DISABLED_METRICS";
        /// <summary>Environment variable name: Standard OpenTelemetry variable; <c>true</c> makes a schemeless endpoint use plaintext instead of TLS.</summary>
        public const string InsecureEnvVar = "OTEL_EXPORTER_OTLP_INSECURE";
        /// <summary>Environment variable name: Port of the standalone Prometheus <c>/metrics</c> listener; see <see cref="PrometheusMetricsPort"/>.</summary>
        public const string MetricsPortEnvVar = "OTEL_HELPER_METRICS_PORT";
        /// <summary>Environment variable name: Standard OpenTelemetry metric exporter list (<c>otlp</c>, <c>prometheus</c>, <c>none</c>); see <see cref="MetricExporters"/>.</summary>
        public const string MetricsExporterEnvVar = "OTEL_METRICS_EXPORTER";
        /// <summary>Environment variable name: Standard OpenTelemetry metric export interval in milliseconds; see <see cref="ExportIntervalMs"/>.</summary>
        public const string MetricExportIntervalEnvVar = "OTEL_METRIC_EXPORT_INTERVAL";
        /// <summary>Environment variable name: Standard OpenTelemetry sampler variable, applied by the SDK when no sampler is set in code.</summary>
        public const string TracesSamplerEnvVar = "OTEL_TRACES_SAMPLER";
        /// <summary>Environment variable name: Standard OpenTelemetry OTLP protocol (<c>grpc</c> or <c>http/protobuf</c>); see <see cref="OtlpProtocol"/>.</summary>
        public const string OtlpProtocolEnvVar = "OTEL_EXPORTER_OTLP_PROTOCOL";

        internal const int DefaultExportIntervalMs = 30_000;
        internal const int DefaultOtlpHttpPort = 4318;
        internal const string ProtocolGrpc = "grpc";
        internal const string ProtocolHttpProtobuf = "http/protobuf";
        internal static readonly string[] ValidMetricExporters = { "otlp", "prometheus", "none" };
        internal static readonly string[] ValidOtlpProtocols = { ProtocolGrpc, ProtocolHttpProtobuf };

        /// <summary>
        /// Service name, emitted as <c>service.name</c>. Default <c>my-service</c>; while it
        /// keeps the default, <c>SERVICE_NAME</c> and then <c>OTEL_SERVICE_NAME</c> replace it.
        /// Required: an empty value fails options validation.
        /// </summary>
        [Required(AllowEmptyStrings = false)]
        public string ServiceName { get; set; } = "my-service";

        /// <summary>
        /// Deployment environment, emitted as <c>deployment.environment.name</c>. Default
        /// <see cref="DeploymentEnvironment.LOCAL"/>; while it keeps the default, the
        /// <c>ENVIRONMENT</c> variable replaces it.
        /// </summary>
        public DeploymentEnvironment Environment { get; set; } = DeploymentEnvironment.LOCAL;

        /// <summary>
        /// OTLP collector endpoint. Empty (the default) reads <c>OTEL_EXPORTER_OTLP_ENDPOINT</c>;
        /// when both are empty, metrics fall back to the Prometheus <c>/metrics</c> endpoint and
        /// logs to stdout. A non-empty value must be an absolute URI.
        /// </summary>
        public string OtelCollectorEndpoint { get; set; } = "";

        /// <summary>
        /// Debug mode. When true: log level lowered to Debug, all instrumentations enabled.
        /// </summary>
        public bool DebugLevel { get; set; }

        /// <summary>
        /// Comma-separated list of extra instrumentations to enable.
        /// Supported values: SQL, AWS. Case-insensitive.
        /// Default: "SQL". Debug mode enables all.
        /// </summary>
        public string ExtraInstrumentation { get; set; } = "SQL";

        /// <summary>
        /// OTLP export timeout in milliseconds. Default: 10000 (10s).
        /// </summary>
        public int ExportTimeoutMs { get; set; } = 10_000;

        /// <summary>
        /// Trace sampler. Default: AlwaysOnSampler.
        /// Override only if you have a specific reason — tail-based sampling in the Collector is preferred.
        /// </summary>
        public Sampler Sampler { get; set; } = new AlwaysOnSampler();

        /// <summary>
        /// Minimum log level. When null, resolved automatically based on Environment.
        /// Set explicitly to override.
        /// </summary>
        public LogLevel? MinimumLogLevel { get; set; }

        /// <summary>
        /// Additional resource attributes added to all signals (traces, metrics, logs).
        /// Note: k8s.*, cloud.*, deployment.environment are enriched by the Collector — avoid duplicating.
        /// </summary>
        public Dictionary<string, object> ResourceAttributes { get; set; } = new();

        /// <summary>
        /// Additional ActivitySource names to subscribe to, beyond ServiceName.
        /// Use when the app creates ActivitySources with different names.
        /// </summary>
        public List<string> AdditionalActivitySources { get; set; } = new();

        /// <summary>
        /// Comma-separated list of signals to disable. Values: traces, metrics, logs. Case-insensitive.
        /// </summary>
        public string DisabledSignals { get; set; } = "";

        /// <summary>
        /// Comma-separated list of metric name patterns to drop. Supports * wildcard. Case-insensitive.
        /// </summary>
        public string DisabledMetrics { get; set; } = "";

        /// <summary>
        /// Comma-separated metric exporters: "otlp", "prometheus", "none".
        /// Empty = resolve from OTEL_METRICS_EXPORTER, falling back to legacy
        /// inference (otlp when an endpoint is set, prometheus otherwise).
        /// </summary>
        public string MetricExporters { get; set; } = "";

        /// <summary>
        /// Metric export interval (ms) for the OTLP reader.
        /// Null = resolve from OTEL_METRIC_EXPORT_INTERVAL, else 30000.
        /// </summary>
        public int? ExportIntervalMs { get; set; }

        /// <summary>
        /// Port for the standalone Prometheus /metrics HTTP listener.
        /// Default: 9464 (OTEL_HELPER_METRICS_PORT). 0 disables the listener —
        /// for ASP.NET Core apps, prefer OpenTelemetry.Exporter.Prometheus.AspNetCore
        /// with MapPrometheusScrapingEndpoint() on the app's own pipeline.
        /// </summary>
        public int PrometheusMetricsPort { get; set; } = 9464;

        /// <summary>
        /// OTLP wire protocol: "grpc" or "http/protobuf".
        /// Null/empty = resolve from OTEL_EXPORTER_OTLP_PROTOCOL, falling back to
        /// port-based inference (4318 -> http/protobuf, else grpc).
        /// </summary>
        public string? OtlpProtocol { get; set; }

        /// <summary>
        /// Returns true if the given signal is NOT in the DisabledSignals list.
        /// </summary>
        public bool IsSignalEnabled(string signal)
            => !DisabledSignals.Split(',', StringSplitOptions.RemoveEmptyEntries | StringSplitOptions.TrimEntries)
                .Any(s => s.Equals(signal, StringComparison.OrdinalIgnoreCase));

        /// <summary>
        /// Returns the default log level for a given environment.
        /// Use this to align external logging config with the lib's defaults.
        /// </summary>
        public static LogLevel GetDefaultLogLevel(DeploymentEnvironment environment, bool debugLevel = false)
        {
            if (debugLevel) return LogLevel.Debug;

            return environment switch
            {
                DeploymentEnvironment.LOCAL => LogLevel.Debug,
                DeploymentEnvironment.DEV => LogLevel.Information,
                DeploymentEnvironment.HML => LogLevel.Information,
                DeploymentEnvironment.PRD => LogLevel.Warning,
                _ => LogLevel.Information,
            };
        }

        /// <summary>
        /// Active metric exporters after resolution. "none" resolves to empty
        /// (metrics disabled). Legacy inference when MetricExporters is unset:
        /// otlp when an endpoint is configured, prometheus otherwise.
        /// </summary>
        public string[] ResolvedMetricExporters()
        {
            if (string.IsNullOrWhiteSpace(MetricExporters))
                return string.IsNullOrWhiteSpace(OtelCollectorEndpoint)
                    ? new[] { "prometheus" }
                    : new[] { "otlp" };

            var exporters = MetricExporters
                .Split(',', StringSplitOptions.RemoveEmptyEntries | StringSplitOptions.TrimEntries)
                .Select(e => e.ToLowerInvariant())
                .ToArray();

            return exporters.Length == 1 && exporters[0] == "none"
                ? Array.Empty<string>()
                : exporters;
        }

        /// <summary>
        /// Resolved OTLP wire protocol. Precedence: explicit OtlpProtocol (set by
        /// consumer code or resolved from OTEL_EXPORTER_OTLP_PROTOCOL in
        /// TelemetryOptionsPostConfigure) > port-based inference (4318 ->
        /// http/protobuf) > grpc default.
        /// </summary>
        public string ResolvedOtlpProtocol()
        {
            if (!string.IsNullOrWhiteSpace(OtlpProtocol))
                return OtlpProtocol.Trim().ToLowerInvariant();

            if (!string.IsNullOrWhiteSpace(OtelCollectorEndpoint)
                && Uri.TryCreate(OtelCollectorEndpoint, UriKind.Absolute, out var uri)
                && uri.Port == DefaultOtlpHttpPort)
                return ProtocolHttpProtobuf;

            return ProtocolGrpc;
        }

        internal bool HasInstrumentation(string name)
        {
            if (DebugLevel) return true;
            return ExtraInstrumentation
                .Split(',', StringSplitOptions.RemoveEmptyEntries | StringSplitOptions.TrimEntries)
                .Any(x => x.Equals(name, StringComparison.OrdinalIgnoreCase));
        }
    }
}
