using System.ComponentModel.DataAnnotations;
using Microsoft.Extensions.Logging;
using OpenTelemetry.Trace;

namespace OtelHelper
{
    /// <summary>
    /// Configuration options for OtelHelper Telemetry.
    /// This is a POCO — environment variable resolution is handled by TelemetryOptionsPostConfigure.
    /// </summary>
    public class TelemetryOptions
    {
        public const string CollectorEndpointEnvVar = "OTEL_EXPORTER_OTLP_ENDPOINT";
        public const string ServiceNameEnvVar = "SERVICE_NAME";
        public const string OtelServiceNameEnvVar = "OTEL_SERVICE_NAME";
        public const string EnvironmentEnvVar = "ENVIRONMENT";
        public const string DebugLevelEnvVar = "OTEL_HELPER_DEBUG_LEVEL";
        public const string ExtraInstrumentationEnvVar = "OTEL_HELPER_EXTRA_INSTRUMENTATION";
        public const string SampleRatioEnvVar = "OTEL_HELPER_SAMPLE_RATIO";
        public const string DisabledSignalsEnvVar = "OTEL_HELPER_DISABLED_SIGNALS";
        public const string DisabledMetricsEnvVar = "OTEL_HELPER_DISABLED_METRICS";
        public const string InsecureEnvVar = "OTEL_EXPORTER_OTLP_INSECURE";
        public const string MetricsPortEnvVar = "OTEL_HELPER_METRICS_PORT";

        [Required(AllowEmptyStrings = false)]
        public string ServiceName { get; set; } = "my-service";

        public DeploymentEnvironment Environment { get; set; } = DeploymentEnvironment.LOCAL;

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
        /// Port for the Prometheus /metrics HTTP listener when no OTLP endpoint is configured.
        /// Default: 9464 (Prometheus convention). Set via OTEL_HELPER_METRICS_PORT env var.
        /// </summary>
        public int PrometheusMetricsPort { get; set; } = 9464;

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

        internal bool HasInstrumentation(string name)
        {
            if (DebugLevel) return true;
            return ExtraInstrumentation
                .Split(',', StringSplitOptions.RemoveEmptyEntries | StringSplitOptions.TrimEntries)
                .Any(x => x.Equals(name, StringComparison.OrdinalIgnoreCase));
        }
    }
}
