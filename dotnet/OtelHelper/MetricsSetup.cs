using System;
using System.Linq;
using System.Text.RegularExpressions;
using Microsoft.Extensions.DependencyInjection;
using OpenTelemetry.Metrics;

namespace OtelHelper.Metrics
{
    internal static class MetricsSetup
    {
        internal static MeterProviderBuilder ConfigureMetrics(
            this MeterProviderBuilder builder,
            TelemetryOptions options)
        {
            builder
                .AddRuntimeInstrumentation()
                .AddAspNetCoreInstrumentation()
                .AddHttpClientInstrumentation()
                .AddMeter(options.ServiceName)
                .SetExemplarFilter(ExemplarFilterType.TraceBased);

            // Export: OTLP push if endpoint configured, otherwise Prometheus HTTP listener
            if (!string.IsNullOrWhiteSpace(options.OtelCollectorEndpoint))
            {
                builder.AddOtlpExporter((exporterOptions, metricReaderOptions) =>
                {
                    exporterOptions.Endpoint = new Uri(options.OtelCollectorEndpoint);
                    exporterOptions.TimeoutMilliseconds = options.ExportTimeoutMs;
                    metricReaderOptions.PeriodicExportingMetricReaderOptions.ExportIntervalMilliseconds = 30_000;
                });
            }
            else
            {
                builder.AddPrometheusHttpListener(opts =>
                {
                    opts.UriPrefixes = new[] { $"http://*:{options.PrometheusMetricsPort}/" };
                });
            }

            // Drop metrics matching wildcard patterns
            if (!string.IsNullOrEmpty(options.DisabledMetrics))
            {
                var patterns = options.DisabledMetrics
                    .Split(',', StringSplitOptions.RemoveEmptyEntries | StringSplitOptions.TrimEntries)
                    .Select(p => "^" + Regex.Escape(p).Replace("\\*", ".*") + "$")
                    .Select(p => new Regex(p, RegexOptions.Compiled | RegexOptions.IgnoreCase))
                    .ToList();

                builder.AddView(instrument =>
                {
                    if (patterns.Any(p => p.IsMatch(instrument.Name)))
                        return MetricStreamConfiguration.Drop;
                    return null;
                });
            }

            return builder;
        }
    }
}
