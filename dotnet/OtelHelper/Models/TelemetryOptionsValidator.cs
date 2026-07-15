using System;
using System.Linq;
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

            if (options.ExportIntervalMs is <= 0)
                return ValidateOptionsResult.Fail("ExportIntervalMs must be greater than 0.");

            // 0 = standalone listener disabled (mounted-endpoint mode).
            if (options.PrometheusMetricsPort < 0 || options.PrometheusMetricsPort > 65535)
                return ValidateOptionsResult.Fail("PrometheusMetricsPort must be between 0 and 65535.");

            if (!string.IsNullOrWhiteSpace(options.MetricExporters))
            {
                var exporters = options.MetricExporters
                    .Split(',', StringSplitOptions.RemoveEmptyEntries | StringSplitOptions.TrimEntries)
                    .Select(e => e.ToLowerInvariant())
                    .ToArray();

                var unknown = exporters.Except(TelemetryOptions.ValidMetricExporters).ToArray();
                if (unknown.Length > 0)
                    return ValidateOptionsResult.Fail(
                        $"Unknown metric exporter(s) '{string.Join(", ", unknown)}' in {TelemetryOptions.MetricsExporterEnvVar}. " +
                        $"Valid values: {string.Join(", ", TelemetryOptions.ValidMetricExporters)}.");

                if (exporters.Contains("none") && exporters.Length > 1)
                    return ValidateOptionsResult.Fail(
                        $"'none' cannot be combined with other metric exporters in {TelemetryOptions.MetricsExporterEnvVar}.");
            }

            if (options.ResolvedMetricExporters().Contains("otlp") && string.IsNullOrWhiteSpace(options.OtelCollectorEndpoint))
                return ValidateOptionsResult.Fail(
                    $"Metric exporter 'otlp' requires an endpoint. Set {TelemetryOptions.CollectorEndpointEnvVar} " +
                    $"or remove 'otlp' from {TelemetryOptions.MetricsExporterEnvVar}.");

            // http/json is a valid OTel spec value but no .NET OTLP exporter implements
            // it — must fail loud, not silently fall back to another protocol.
            if (!string.IsNullOrWhiteSpace(options.OtlpProtocol))
            {
                var protocol = options.OtlpProtocol.Trim().ToLowerInvariant();
                if (!TelemetryOptions.ValidOtlpProtocols.Contains(protocol))
                    return ValidateOptionsResult.Fail(
                        $"Unknown OTLP protocol '{options.OtlpProtocol}' in {TelemetryOptions.OtlpProtocolEnvVar}. " +
                        $"Valid values: {string.Join(", ", TelemetryOptions.ValidOtlpProtocols)}.");
            }

            return ValidateOptionsResult.Success;
        }
    }
}
