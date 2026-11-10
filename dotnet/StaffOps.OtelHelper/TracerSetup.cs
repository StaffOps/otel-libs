using System;
using Microsoft.Extensions.DependencyInjection;
using OpenTelemetry;
using OpenTelemetry.Exporter;
using OpenTelemetry.Instrumentation.AspNetCore;
using OpenTelemetry.Instrumentation.Http;
using OpenTelemetry.Trace;
using StaffOps.OtelHelper.Diagnostics;
using StaffOps.OtelHelper.Exporters;

namespace StaffOps.OtelHelper.Tracing
{
    /// <summary>
    /// Tracing in two phases (design D-NET3): the instrumentations are registered when
    /// <c>AddOtelHelper</c> runs; every value — sources, sampler, resource, filters, the
    /// exporter — is applied when the provider is built, from <see cref="ResolvedTelemetry"/>.
    /// </summary>
    internal static class TracerSetup
    {
        /// <summary>Eager part: service-registering calls only.</summary>
        internal static TracerProviderBuilder AddHelperInstrumentation(this TracerProviderBuilder builder)
            => builder
                .AddAspNetCoreInstrumentation()
                .AddHttpClientInstrumentation()
                .AddGrpcClientInstrumentation();

        /// <summary>Deferred values of the instrumentation options (health filters).</summary>
        internal static void ConfigureInstrumentationOptions(IServiceCollection services)
        {
            services.AddOptions<AspNetCoreTraceInstrumentationOptions>()
                .Configure<IServiceProvider>((opts, sp) =>
                {
                    if (!ResolvedAccess.TryGet(sp, out var resolved))
                    {
                        return;
                    }

                    var healthPaths = HealthPathSet(resolved);
                    opts.Filter = httpContext => !healthPaths.Contains(httpContext.Request.Path);
                    opts.RecordException = true;
                });
            services.AddOptions<HttpClientTraceInstrumentationOptions>()
                .Configure<IServiceProvider>((opts, sp) =>
                {
                    if (!ResolvedAccess.TryGet(sp, out var resolved))
                    {
                        return;
                    }

                    var healthPaths = HealthPathSet(resolved);
                    opts.FilterHttpRequestMessage = req => !healthPaths.Contains(req.RequestUri?.AbsolutePath ?? "");
                    opts.RecordException = true;
                });
        }

        /// <summary>Deferred part, at provider build.</summary>
        internal static void ApplyResolved(IServiceProvider sp, TracerProviderBuilder builder)
        {
            if (!ResolvedAccess.TryGet(sp, out var resolved))
            {
                return;
            }

            builder.ConfigureResource(r => ResourceSetup.Apply(r, resolved, sp, "traces"));

            // "Off" (disabled signal): instrumentation registered, no source and no exporter (D-NET3).
            if (!resolved.IsSignalEnabled("traces"))
            {
                return;
            }

            builder.AddSource(resolved.ServiceName);
            foreach (var source in resolved.AdditionalActivitySources)
            {
                builder.AddSource(source);
            }

            if (resolved.Sampler is { } sampler)
            {
                builder.SetSampler(sampler);
            }

            if (resolved.DebugLevel)
            {
                builder.AddProcessor(new DebugTraceStateProcessor());
            }

            var diagnostics = sp.GetRequiredService<IOtelHelperDiagnostics>();
            if (OtlpExporters.OptionsFor("traces", resolved, diagnostics, sp.GetRequiredService<ExporterClients>()) is { } otlp)
            {
                builder.AddProcessor(CreateOtlpProcessor(sp, resolved, otlp, diagnostics));
            }
        }

        /// <summary>
        /// The OTLP batch processor: queue size and delay from the SDK's options, the export deadline
        /// of traces (A18: <c>OTEL_BSP_EXPORT_TIMEOUT</c> is not honored) and the batch size clamped to
        /// the queue size (A18: the SDK constructor would throw).
        /// </summary>
        internal static BatchActivityExportProcessor CreateOtlpProcessor(IServiceProvider sp, ResolvedTelemetry resolved, OtlpExporterOptions otlp, IOtelHelperDiagnostics diagnostics)
        {
            var batch = sp.GetRequiredService<Microsoft.Extensions.Options.IOptionsMonitor<BatchExportActivityProcessorOptions>>()
                .Get(Microsoft.Extensions.Options.Options.DefaultName);
            ExportSettingsResolver.WarnIgnoredOptionTimeout(
                nameof(BatchExportActivityProcessorOptions) + "." + nameof(BatchExportActivityProcessorOptions.ExporterTimeoutMilliseconds), "OTEL_BSP_EXPORT_TIMEOUT",
                () => batch.ExporterTimeoutMilliseconds != new BatchExportActivityProcessorOptions().ExporterTimeoutMilliseconds, diagnostics);
            return new BatchActivityExportProcessor(
                new OtlpTraceExporter(otlp),
                batch.MaxQueueSize,
                batch.ScheduledDelayMilliseconds,
                OtlpExporters.TimeoutFor("traces", resolved),
                ExportSettingsResolver.ClampBatchSize("OTEL_BSP", nameof(BatchExportActivityProcessorOptions), batch.MaxQueueSize, batch.MaxExportBatchSize, diagnostics));
        }

        private static HashSet<string> HealthPathSet(ResolvedTelemetry resolved)
            => new(resolved.HealthPaths, StringComparer.OrdinalIgnoreCase);
    }
}
