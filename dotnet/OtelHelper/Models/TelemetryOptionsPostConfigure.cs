using System;
using Microsoft.Extensions.Options;

namespace OtelHelper
{
    /// <summary>
    /// Fills in options from environment variables when the consumer hasn't set them explicitly.
    /// Runs after Configure() — so consumer overrides take priority over env vars.
    /// </summary>
    internal sealed class TelemetryOptionsPostConfigure : IPostConfigureOptions<TelemetryOptions>
    {
        public void PostConfigure(string? name, TelemetryOptions options)
        {
            // ServiceName: env var only if still default
            if (options.ServiceName == "my-service")
            {
                options.ServiceName =
                    Environment.GetEnvironmentVariable(TelemetryOptions.ServiceNameEnvVar)
                    ?? Environment.GetEnvironmentVariable(TelemetryOptions.OtelServiceNameEnvVar)
                    ?? "my-service";
            }

            // Environment
            if (options.Environment == DeploymentEnvironment.LOCAL)
            {
                var env = Environment.GetEnvironmentVariable(TelemetryOptions.EnvironmentEnvVar);
                if (!string.IsNullOrWhiteSpace(env))
                {
                    var normalized = env.Replace("-", "_");
                    if (Enum.TryParse<DeploymentEnvironment>(normalized, ignoreCase: true, out var parsed))
                        options.Environment = parsed;
                }
            }

            // Collector endpoint: resolve from env var if not set by consumer.
            // If the env var is also unset, leave empty — triggers Prometheus fallback.
            if (string.IsNullOrEmpty(options.OtelCollectorEndpoint))
            {
                var endpoint = Environment.GetEnvironmentVariable(TelemetryOptions.CollectorEndpointEnvVar);
                if (!string.IsNullOrWhiteSpace(endpoint))
                {
                    options.OtelCollectorEndpoint = ResolveEndpoint(endpoint.TrimEnd('/'));
                }
            }

            // PrometheusMetricsPort from env var
            var metricsPort = Environment.GetEnvironmentVariable(TelemetryOptions.MetricsPortEnvVar);
            if (!string.IsNullOrWhiteSpace(metricsPort) && int.TryParse(metricsPort, out var port))
                options.PrometheusMetricsPort = port;

            // Debug level from env var (only if not already set by consumer)
            if (!options.DebugLevel)
                options.DebugLevel = ResolveEnvBool(TelemetryOptions.DebugLevelEnvVar);

            // Extra instrumentation from env var (only if still default)
            if (options.ExtraInstrumentation == "SQL")
            {
                var extra = Environment.GetEnvironmentVariable(TelemetryOptions.ExtraInstrumentationEnvVar);
                if (extra != null)
                    options.ExtraInstrumentation = extra;
            }

            // Metric exporters from the standard OTel env var (only if not set by consumer)
            if (string.IsNullOrWhiteSpace(options.MetricExporters))
            {
                var exporters = Environment.GetEnvironmentVariable(TelemetryOptions.MetricsExporterEnvVar);
                if (!string.IsNullOrWhiteSpace(exporters))
                    options.MetricExporters = exporters;
            }

            // Export interval: explicit > OTEL_METRIC_EXPORT_INTERVAL > 30000.
            // Resolved here (not hardcoded in MetricsSetup) so the standard var is honored.
            if (options.ExportIntervalMs is null)
            {
                var intervalStr = Environment.GetEnvironmentVariable(TelemetryOptions.MetricExportIntervalEnvVar);
                if (!string.IsNullOrWhiteSpace(intervalStr) && int.TryParse(intervalStr, out var interval) && interval > 0)
                    options.ExportIntervalMs = interval;
            }
            options.ExportIntervalMs ??= TelemetryOptions.DefaultExportIntervalMs;

            // Sample ratio from env var (only if sampler is still default AlwaysOn).
            // Standard OTel var wins over the proprietary one: when OTEL_TRACES_SAMPLER
            // is set, the SDK's own env handling configures the sampler and
            // OTEL_HELPER_SAMPLE_RATIO is ignored (see TracerSetup).
            if (options.Sampler is OpenTelemetry.Trace.AlwaysOnSampler
                && string.IsNullOrWhiteSpace(Environment.GetEnvironmentVariable(TelemetryOptions.TracesSamplerEnvVar)))
            {
                var ratioStr = Environment.GetEnvironmentVariable(TelemetryOptions.SampleRatioEnvVar);
                if (!string.IsNullOrWhiteSpace(ratioStr) && double.TryParse(ratioStr, System.Globalization.NumberStyles.Float, System.Globalization.CultureInfo.InvariantCulture, out var ratio))
                {
                    ratio = Math.Clamp(ratio, 0.0, 1.0);
                    if (ratio < 1.0)
                        options.Sampler = new OpenTelemetry.Trace.TraceIdRatioBasedSampler(ratio);
                }
            }

            // Disabled signals from env var
            if (string.IsNullOrEmpty(options.DisabledSignals))
            {
                var disabled = Environment.GetEnvironmentVariable(TelemetryOptions.DisabledSignalsEnvVar);
                if (!string.IsNullOrWhiteSpace(disabled))
                    options.DisabledSignals = disabled;
            }

            // Disabled metrics from env var
            if (string.IsNullOrEmpty(options.DisabledMetrics))
            {
                var disabledMetrics = Environment.GetEnvironmentVariable(TelemetryOptions.DisabledMetricsEnvVar);
                if (!string.IsNullOrWhiteSpace(disabledMetrics))
                    options.DisabledMetrics = disabledMetrics;
            }
        }

        private static bool ResolveEnvBool(string varName)
        {
            var env = Environment.GetEnvironmentVariable(varName);
            return string.Equals(env, "true", StringComparison.OrdinalIgnoreCase);
        }

        /// <summary>
        /// Resolves an endpoint string to a fully qualified URI with scheme and port.
        /// - If the endpoint already has a scheme (http:// or https://), preserves it.
        /// - If no scheme: uses OTEL_EXPORTER_OTLP_INSECURE to decide (true -> http, false/unset -> https).
        /// - If no port is specified, defaults to 4317.
        /// Secure by default: https when no explicit scheme and INSECURE is not set.
        /// </summary>
        internal static string ResolveEndpoint(string rawEndpoint)
        {
            // If the endpoint already has a scheme, parse it and normalize.
            if (Uri.TryCreate(rawEndpoint, UriKind.Absolute, out var uri)
                && (uri.Scheme == "http" || uri.Scheme == "https"))
            {
                var port = uri.IsDefaultPort ? 4317 : uri.Port;
                return $"{uri.Scheme}://{uri.Host}:{port}";
            }

            // No scheme present — decide scheme from OTEL_EXPORTER_OTLP_INSECURE env var.
            var insecure = ResolveEnvBool(TelemetryOptions.InsecureEnvVar);
            var scheme = insecure ? "http" : "https";

            // Parse host and port from the raw value (e.g. "host:4317" or "host").
            var hostPort = rawEndpoint;
            int resolvedPort = 4317;

            var lastColon = hostPort.LastIndexOf(':');
            if (lastColon > 0 && int.TryParse(hostPort.Substring(lastColon + 1), out var parsedPort))
            {
                // Has explicit port
                resolvedPort = parsedPort;
                hostPort = hostPort.Substring(0, lastColon);
            }

            return $"{scheme}://{hostPort}:{resolvedPort}";
        }
    }
}
