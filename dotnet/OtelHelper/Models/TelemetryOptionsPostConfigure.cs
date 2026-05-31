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
        private const string DefaultCollectorHost = "http://localhost";
        private const int DefaultOtlpPort = 4317;

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

            // Collector endpoint
            var collectorHost = ResolveCollectorHost();
            if (string.IsNullOrEmpty(options.OtelCollectorEndpoint))
                options.OtelCollectorEndpoint = $"{collectorHost}:{DefaultOtlpPort}";

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
        }

        private static string ResolveCollectorHost()
        {
            var env = Environment.GetEnvironmentVariable(TelemetryOptions.CollectorEndpointEnvVar);
            if (string.IsNullOrWhiteSpace(env))
                return DefaultCollectorHost;

            if (Uri.TryCreate(env.TrimEnd('/'), UriKind.Absolute, out var uri))
                return $"{uri.Scheme}://{uri.Host}";

            return env.TrimEnd('/');
        }

        private static bool ResolveEnvBool(string varName)
        {
            var env = Environment.GetEnvironmentVariable(varName);
            return string.Equals(env, "true", StringComparison.OrdinalIgnoreCase);
        }
    }
}
