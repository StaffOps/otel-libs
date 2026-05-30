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

            if (string.IsNullOrWhiteSpace(options.OtelCollectorEndpoint))
                return ValidateOptionsResult.Fail(
                    $"OtelCollectorEndpoint is required. Set the {TelemetryOptions.CollectorEndpointEnvVar} environment variable.");

            if (!Uri.TryCreate(options.OtelCollectorEndpoint, UriKind.Absolute, out _))
                return ValidateOptionsResult.Fail($"OtelCollectorEndpoint '{options.OtelCollectorEndpoint}' is not a valid URI.");

            if (options.ExportTimeoutMs <= 0)
                return ValidateOptionsResult.Fail("ExportTimeoutMs must be greater than 0.");

            return ValidateOptionsResult.Success;
        }
    }
}
