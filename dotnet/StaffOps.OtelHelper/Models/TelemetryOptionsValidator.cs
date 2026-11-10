using System.Globalization;
using Microsoft.Extensions.Options;
using StaffOps.OtelHelper.Diagnostics;

namespace StaffOps.OtelHelper
{
    /// <summary>
    /// Validates the values set in code (design D-NET6): an invalid code value fails host
    /// start (<c>ValidateOnStart</c>) with a message naming the option. Environment values
    /// are never checked here: the resolver warns about them and uses the default. Every
    /// echoed value is redacted, cut and escaped (requirement DN3).
    /// </summary>
    internal sealed class TelemetryOptionsValidator : IValidateOptions<TelemetryOptions>
    {
        private const string Owner = "TelemetryOptions.";

        public ValidateOptionsResult Validate(string? name, TelemetryOptions options)
        {
            var failures = new List<string>();

            CheckEndpoint(failures, nameof(TelemetryOptions.OtelCollectorEndpoint), options.OtelCollectorEndpoint, allowSchemeless: true);
            CheckEndpoint(failures, nameof(TelemetryOptions.TracesEndpoint), options.TracesEndpoint, allowSchemeless: false);
            CheckEndpoint(failures, nameof(TelemetryOptions.MetricsEndpoint), options.MetricsEndpoint, allowSchemeless: false);
            CheckEndpoint(failures, nameof(TelemetryOptions.LogsEndpoint), options.LogsEndpoint, allowSchemeless: false);

            if (options.ExportTimeoutMs is { } timeout && (timeout < 1 || timeout > TelemetryOptions.MaxExportTimeoutMs))
            {
                failures.Add($"{Owner}{nameof(TelemetryOptions.ExportTimeoutMs)} must be in [1, {TelemetryOptions.MaxExportTimeoutMs}] ms, got {timeout.ToString(CultureInfo.InvariantCulture)}.");
            }

            if (options.ExportIntervalMs is { } interval && (interval < 1 || interval > int.MaxValue))
            {
                failures.Add($"{Owner}{nameof(TelemetryOptions.ExportIntervalMs)} must be in [1, {int.MaxValue}] ms, got {interval.ToString(CultureInfo.InvariantCulture)}.");
            }

            if (options.PrometheusMetricsPort is { } port && (port < 0 || port > 65535))
            {
                failures.Add($"{Owner}{nameof(TelemetryOptions.PrometheusMetricsPort)} must be in [0, 65535] (0 = no listener), got {port.ToString(CultureInfo.InvariantCulture)}.");
            }

            if (options.SampleRatio is { } ratio && !(ratio >= 0.0 && ratio <= 1.0))
            {
                failures.Add($"{Owner}{nameof(TelemetryOptions.SampleRatio)} must be in [0, 1], got {ratio.ToString("R", CultureInfo.InvariantCulture)}.");
            }

            if (!string.IsNullOrWhiteSpace(options.OtlpProtocol)
                && !TelemetryOptions.ValidOtlpProtocols.Contains(options.OtlpProtocol.Trim().ToLowerInvariant()))
            {
                failures.Add($"Unknown OTLP protocol {DiagnosticText.Echo(options.OtlpProtocol)} in {Owner}{nameof(TelemetryOptions.OtlpProtocol)}. " +
                    $"Valid values: {string.Join(", ", TelemetryOptions.ValidOtlpProtocols)}.");
            }

            if (!string.IsNullOrWhiteSpace(options.Compression)
                && !TelemetryOptions.ValidCompressions.Contains(options.Compression.Trim().ToLowerInvariant()))
            {
                failures.Add($"Unknown compression {DiagnosticText.Echo(options.Compression)} in {Owner}{nameof(TelemetryOptions.Compression)}. " +
                    $"Valid values: {string.Join(", ", TelemetryOptions.ValidCompressions)}.");
            }

            CheckMetricExporters(failures, options);

            return failures.Count == 0 ? ValidateOptionsResult.Success : ValidateOptionsResult.Fail(failures);
        }

        private static void CheckEndpoint(List<string> failures, string option, string? value, bool allowSchemeless)
        {
            if (string.IsNullOrWhiteSpace(value) || TelemetryResolver.IsHttpUrl(value))
            {
                return;
            }

            if (allowSchemeless && IsSchemeless(value.Trim()))
            {
                return;
            }

            failures.Add($"{Owner}{option} {DiagnosticText.RenderUrl(value)} is not a valid OTLP endpoint: use an http:// or https:// URL" +
                (allowSchemeless ? " or host:port." : "."));
        }

        /// <summary><c>host</c>, <c>host:port</c> or <c>[ipv6]:port</c>, optionally with a path; no scheme, no userinfo.</summary>
        private static bool IsSchemeless(string value)
        {
            if (value.Contains("://", StringComparison.Ordinal) || value.Contains('@') || value.StartsWith('/'))
            {
                return false;
            }

            return Uri.TryCreate("https://" + value, UriKind.Absolute, out var uri) && uri.Host.Length > 0
                && DiagnosticText.RenderUrl(value) != DiagnosticText.UnparseableUrl
                && !DiagnosticText.RenderUrl(value).EndsWith(':');
        }

        private static void CheckMetricExporters(List<string> failures, TelemetryOptions options)
        {
            const string option = Owner + nameof(TelemetryOptions.MetricExporters);
            if (string.IsNullOrWhiteSpace(options.MetricExporters))
            {
                return;
            }

            var exporters = options.MetricExporters
                .Split(',', StringSplitOptions.RemoveEmptyEntries | StringSplitOptions.TrimEntries)
                .Select(e => e.ToLowerInvariant())
                .ToArray();

            var unknown = exporters.Except(TelemetryOptions.ValidMetricExporters).ToArray();
            if (unknown.Length > 0)
            {
                failures.Add($"Unknown metric exporter(s) {string.Join(", ", unknown.Select(DiagnosticText.Echo))} in {option}. " +
                    $"Valid values: {string.Join(", ", TelemetryOptions.ValidMetricExporters)}.");
                return;
            }

            if (exporters.Contains("none") && exporters.Distinct().Count() > 1)
            {
                failures.Add($"'none' cannot be combined with other metric exporters in {option}.");
                return;
            }

            var hasEndpoint = !string.IsNullOrWhiteSpace(options.OtelCollectorEndpoint)
                || !string.IsNullOrWhiteSpace(options.MetricsEndpoint)
                || !string.IsNullOrWhiteSpace(System.Environment.GetEnvironmentVariable(TelemetryOptions.CollectorEndpointEnvVar));
            if (exporters.Contains("otlp") && !hasEndpoint)
            {
                failures.Add($"Metric exporter 'otlp' in {option} requires an endpoint. Set {Owner}{nameof(TelemetryOptions.OtelCollectorEndpoint)}, " +
                    $"{Owner}{nameof(TelemetryOptions.MetricsEndpoint)} or {TelemetryOptions.CollectorEndpointEnvVar}.");
            }
        }
    }
}
