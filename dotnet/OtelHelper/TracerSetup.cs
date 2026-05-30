using System;
using System.Collections.Generic;
using Microsoft.Extensions.DependencyInjection;
using OpenTelemetry.Trace;

namespace OtelHelper.Tracing
{
    internal static class TracerSetup
    {
        internal static TracerProviderBuilder ConfigureTracing(
            this TracerProviderBuilder builder,
            TelemetryOptions options)
        {
            var healthPaths = new HashSet<string>(StringComparer.OrdinalIgnoreCase)
            {
                "/ping", "/health", "/healthz", "/ready"
            };

            builder
                .AddAspNetCoreInstrumentation(opts =>
                {
                    opts.Filter = httpContext => !healthPaths.Contains(httpContext.Request.Path);
                    opts.RecordException = true;
                })
                .AddHttpClientInstrumentation(opts =>
                {
                    opts.FilterHttpRequestMessage = req =>
                        !healthPaths.Contains(req.RequestUri?.AbsolutePath ?? "");
                    opts.RecordException = true;
                })
                .AddGrpcClientInstrumentation()
                .AddSource(options.ServiceName)
                .SetSampler(options.Sampler);

            foreach (var source in options.AdditionalActivitySources)
                builder.AddSource(source);

            if (options.HasInstrumentation("SQL"))
                builder.AddSqlClientInstrumentation(opts => opts.RecordException = true);

            if (options.HasInstrumentation("AWS"))
                builder.AddAWSInstrumentation();

            builder.AddOtlpExporter(otlp =>
            {
                otlp.Endpoint = new Uri(options.OtelCollectorEndpoint);
                otlp.TimeoutMilliseconds = options.ExportTimeoutMs;
            });

            if (options.DebugLevel)
                builder.AddProcessor(new DebugTraceStateProcessor());

            return builder;
        }
    }
}
