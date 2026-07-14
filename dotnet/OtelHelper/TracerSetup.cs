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
                .AddSource(options.ServiceName);

            // Standard SDK env config wins when the consumer kept the default
            // sampler: without SetSampler, the SDK reads OTEL_TRACES_SAMPLER /
            // OTEL_TRACES_SAMPLER_ARG itself. An explicit sampler set in code
            // (or OTEL_HELPER_SAMPLE_RATIO when the standard var is absent —
            // see TelemetryOptionsPostConfigure) still takes precedence.
            var standardSamplerSet = !string.IsNullOrWhiteSpace(
                Environment.GetEnvironmentVariable(TelemetryOptions.TracesSamplerEnvVar));
            if (!(standardSamplerSet && options.Sampler is AlwaysOnSampler))
                builder.SetSampler(options.Sampler);

            foreach (var source in options.AdditionalActivitySources)
                builder.AddSource(source);

            // OTLP exporter only when collector endpoint is configured.
            // Without endpoint, traces are still created for in-process context propagation but not exported.
            if (!string.IsNullOrWhiteSpace(options.OtelCollectorEndpoint))
            {
                builder.AddOtlpExporter(otlp =>
                {
                    otlp.Endpoint = new Uri(options.OtelCollectorEndpoint);
                    otlp.TimeoutMilliseconds = options.ExportTimeoutMs;
                });
            }

            if (options.DebugLevel)
                builder.AddProcessor(new DebugTraceStateProcessor());

            return builder;
        }
    }
}
