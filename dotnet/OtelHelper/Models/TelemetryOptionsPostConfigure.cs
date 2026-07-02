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
                    if (Uri.TryCreate(endpoint.TrimEnd('/'), UriKind.Absolute, out var uri))
                        options.OtelCollectorEndpoint = $"{uri.Scheme}://{uri.Host}:{uri.Port}";
                    else
                        options.OtelCollectorEndpoint = $"{endpoint.TrimEnd('/')}:4317";
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

            // Sample ratio from env var (only if sampler is still default AlwaysOn)
            if (options.Sampler is OpenTelemetry.Trace.AlwaysOnSampler)
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
    }
}
