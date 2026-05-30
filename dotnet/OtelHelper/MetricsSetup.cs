using System;
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
                .SetExemplarFilter(ExemplarFilterType.TraceBased)
                .AddOtlpExporter(otlp =>
                {
                    otlp.Endpoint = new Uri(options.OtelCollectorEndpoint);
                    otlp.TimeoutMilliseconds = options.ExportTimeoutMs;
                });

            return builder;
        }
    }
}
