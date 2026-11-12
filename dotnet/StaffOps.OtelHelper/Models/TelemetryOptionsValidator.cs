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

            // The resolver's own endpoint rules (contract D1, A17), so a code value that passes here is one it uses.
            failures.AddRange(TelemetryResolver.CodeEndpointErrors(options));

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

            CheckHealthPaths(failures, options);
            CheckMetricExporters(failures, options);
            CheckDisabledSignals(failures, options);
            CheckResourceAttributes(failures, options);

            // A blank identity option is an invalid code value (Go WithServiceName/WithEnvironment,
            // Python service_name/environment): null is "not set", "" or whitespace fails.
            if (options.ServiceName is { } serviceName && ContractText.Trim(serviceName).Length == 0)
            {
                failures.Add($"{Owner}{nameof(TelemetryOptions.ServiceName)} must not be empty (null means not set).");
            }

            if (options.Environment is { } environment && ContractText.Trim(environment).Length == 0)
            {
                failures.Add($"{Owner}{nameof(TelemetryOptions.Environment)} must not be empty (null means not set).");
            }

            return failures.Count == 0 ? ValidateOptionsResult.Success : ValidateOptionsResult.Fail(failures);
        }

        /// <summary>
        /// Code resource attributes (contract row "Invalid code resource_attributes", A16): an empty
        /// or contract-trim-set-only key fails; a value the SDK's <c>Resource</c> cannot hold (null,
        /// or not a string, bool, number or array of them) fails naming the key, here at start
        /// instead of as an <c>ArgumentException</c> while a provider is built; the identity keys
        /// take strings only (as Python's).
        /// </summary>
        private static void CheckResourceAttributes(List<string> failures, TelemetryOptions options)
        {
            const string option = Owner + nameof(TelemetryOptions.ResourceAttributes);
            if (options.ResourceAttributes is null)
            {
                failures.Add($"{option} must not be null.");
                return;
            }

            foreach (var (key, value) in options.ResourceAttributes)
            {
                if (ContractText.Trim(key).Length == 0)
                {
                    failures.Add($"{option} has an empty key.");
                    continue;
                }

                var identity = key is TelemetryResolver.ServiceNameKey or TelemetryResolver.EnvironmentKey or TelemetryResolver.ServiceInstanceIdKey;
                if (identity ? value is not string : !IsResourceValue(value))
                {
                    failures.Add($"{option}[{DiagnosticText.Echo(key)}] must be " +
                        (identity ? "a string" : "a string, bool, number or an array of them") +
                        $", got {(value is null ? "null" : value.GetType().Name)}.");
                }
            }
        }

        /// <summary>The value types <c>OpenTelemetry.Resources.Resource</c> accepts (<c>Resource.SanitizeValue</c>, SDK 1.20).</summary>
        private static bool IsResourceValue(object? value) => value is string or bool or double or long or int or short or float
            or string[] or bool[] or double[] or long[] or int[] or short[] or float[];

        /// <summary>
        /// Code disabled signals (contract row "Deprecated <c>OTEL_HELPER_DISABLED_SIGNALS</c>", A19):
        /// trimmed and lowercased like the resolver does; an entry that is not a signal fails, as in
        /// Go (<c>WithDisabledSignals</c>) and Python (<c>disabled_signals</c>).
        /// </summary>
        private static void CheckDisabledSignals(List<string> failures, TelemetryOptions options)
        {
            if (string.IsNullOrWhiteSpace(options.DisabledSignals))
            {
                return;
            }

            var unknown = TelemetryResolver.SplitList(options.DisabledSignals)
                .Where(s => !ResolvedTelemetry.Signals.Contains(s))
                .Distinct()
                .ToArray();
            if (unknown.Length > 0)
            {
                failures.Add($"Unknown signal(s) {string.Join(", ", unknown.Select(DiagnosticText.Echo))} in {Owner}{nameof(TelemetryOptions.DisabledSignals)}. " +
                    $"Valid values: {string.Join(", ", ResolvedTelemetry.Signals)}.");
            }
        }

        /// <summary>
        /// Code health paths (Go <c>WithHealthPaths</c>, Python <c>health_paths</c>, Node.js
        /// <c>healthPaths</c>): each must be ASCII, start with <c>/</c> and have no query, fragment or
        /// comma; a null item fails too.
        /// </summary>
        private static void CheckHealthPaths(List<string> failures, TelemetryOptions options)
        {
            if (options.HealthPaths is null)
            {
                return;
            }

            for (var i = 0; i < options.HealthPaths.Count; i++)
            {
                var path = options.HealthPaths[i];
                if (path is null || !path.StartsWith('/') || path.IndexOfAny(new[] { '?', '#', ',' }) >= 0 || !path.All(char.IsAscii))
                {
                    failures.Add($"{Owner}{nameof(TelemetryOptions.HealthPaths)} item #{(i + 1).ToString(CultureInfo.InvariantCulture)} " +
                        $"({(path is null ? "null" : DiagnosticText.Echo(path))}) must be ASCII, start with \"/\" and have no query, fragment or comma.");
                }
            }
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
                || !string.IsNullOrWhiteSpace(System.Environment.GetEnvironmentVariable(TelemetryOptions.CollectorEndpointEnvVar))
                || !string.IsNullOrWhiteSpace(System.Environment.GetEnvironmentVariable(TelemetryResolver.SignalVariable("metrics", "ENDPOINT")));
            if (exporters.Contains("otlp") && !hasEndpoint)
            {
                failures.Add($"Metric exporter 'otlp' in {option} requires an endpoint. Set {Owner}{nameof(TelemetryOptions.OtelCollectorEndpoint)}, " +
                    $"{Owner}{nameof(TelemetryOptions.MetricsEndpoint)} or {TelemetryOptions.CollectorEndpointEnvVar}.");
            }
        }
    }
}
