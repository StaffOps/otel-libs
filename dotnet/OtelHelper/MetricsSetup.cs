using System;
using System.Linq;
using System.Text.RegularExpressions;
using Microsoft.Extensions.DependencyInjection;
using OpenTelemetry.Exporter;
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

            // One reader per resolved exporter: OTLP push and/or Prometheus scrape
            // can be active on the same provider (OTEL_METRICS_EXPORTER contract).
            var exporters = options.ResolvedMetricExporters();

            if (exporters.Contains("otlp"))
            {
                var protocol = options.ResolvedOtlpProtocol();
                builder.AddOtlpExporter((exporterOptions, metricReaderOptions) =>
                {
                    if (protocol == TelemetryOptions.ProtocolHttpProtobuf)
                    {
                        exporterOptions.Protocol = OtlpExportProtocol.HttpProtobuf;
                        exporterOptions.Endpoint = new Uri(options.OtelCollectorEndpoint.TrimEnd('/') + "/v1/metrics");
                    }
                    else
                    {
                        exporterOptions.Protocol = OtlpExportProtocol.Grpc;
                        exporterOptions.Endpoint = new Uri(options.OtelCollectorEndpoint);
                    }
                    exporterOptions.TimeoutMilliseconds = options.ExportTimeoutMs;
                    metricReaderOptions.PeriodicExportingMetricReaderOptions.ExportIntervalMilliseconds =
                        options.ExportIntervalMs ?? TelemetryOptions.DefaultExportIntervalMs;
                });
            }

            // Port 0 = listener disabled. ASP.NET Core apps should instead add
            // OpenTelemetry.Exporter.Prometheus.AspNetCore and map
            // MapPrometheusScrapingEndpoint() on their own pipeline.
            if (exporters.Contains("prometheus") && options.PrometheusMetricsPort > 0)
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
