using System;
using Microsoft.Extensions.Options;

namespace OtelHelper
{
    public class TelemetryOptionsValidator : IValidateOptions<TelemetryOptions>
    {
        public ValidateOptionsResult Validate(string? name, TelemetryOptions options)
        {
            if (string.IsNullOrWhiteSpace(options.ServiceName))
                return ValidateOptionsResult.Fail(
                    $"ServiceName is required. Set the {TelemetryOptions.ServiceNameEnvVar} environment variable.");

            // OtelCollectorEndpoint is optional — empty means Prometheus fallback mode.
            // But if set, it must be a valid URI.
            if (!string.IsNullOrWhiteSpace(options.OtelCollectorEndpoint)
                && !Uri.TryCreate(options.OtelCollectorEndpoint, UriKind.Absolute, out _))
                return ValidateOptionsResult.Fail($"OtelCollectorEndpoint '{options.OtelCollectorEndpoint}' is not a valid URI.");

            if (options.ExportTimeoutMs <= 0)
                return ValidateOptionsResult.Fail("ExportTimeoutMs must be greater than 0.");

            if (options.PrometheusMetricsPort <= 0 || options.PrometheusMetricsPort > 65535)
                return ValidateOptionsResult.Fail("PrometheusMetricsPort must be between 1 and 65535.");

            return ValidateOptionsResult.Success;
        }
    }
}
