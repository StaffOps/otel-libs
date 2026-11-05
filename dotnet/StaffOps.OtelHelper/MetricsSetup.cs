using System;
using System.Linq;
using System.Text.RegularExpressions;
using Microsoft.Extensions.DependencyInjection;
using OpenTelemetry.Exporter;
using OpenTelemetry.Metrics;

namespace StaffOps.OtelHelper.Metrics
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
                var port = options.PrometheusMetricsPort;
                builder.AddPrometheusHttpListener(opts => ConfigurePrometheusListener(opts, port));
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

        /// <summary>
        /// Prefix the Prometheus listener binds: every IPv4 address (the managed
        /// <c>HttpListener</c> maps <c>+</c> to <c>IPAddress.Any</c>; no IPv6), path
        /// <c>/metrics/</c> (design D-NET11). The upstream default host is
        /// <c>localhost</c>, which other containers and pods cannot reach, and a
        /// <c>*</c>, <c>+</c> or <c>0.0.0.0</c> <c>Host</c> aborts the provider build.
        /// </summary>
        internal static string PrometheusListenerPrefix(int port) => $"http://+:{port}/metrics/";

        /// <summary>
        /// Keeps the 0.2.x listener behavior on the 1.20.x exporter: bind all
        /// interfaces on the helper's port and serve a fresh collection on every
        /// scrape. This delegate runs after the options factory has read
        /// <c>OTEL_EXPORTER_PROMETHEUS_HOST</c>/<c>_PORT</c>, so the values set
        /// here win and those variables have no effect, as in 0.2.x.
        /// </summary>
        internal static void ConfigurePrometheusListener(PrometheusHttpListenerOptions opts, int port)
        {
            // A valid host for the exporter's own prefix, which the delegate below
            // replaces; an env-provided "*" would otherwise abort the build.
            opts.Host = "localhost";
            opts.Port = port;
            // 0.2.x (1.9.0-beta.2) had no response cache; upstream now defaults to 300 ms.
            opts.ScrapeResponseCacheDurationMilliseconds = 0;
            opts.ConfigureHttpListener = static (o, listener) =>
            {
                // Clear first: keeping the localhost prefix next to "+" fails on
                // Linux with "Address already in use" (exporter README).
                listener.Prefixes.Clear();
                listener.Prefixes.Add(PrometheusListenerPrefix(o.Port));
            };
        }
    }
}
