using System;
using Microsoft.Extensions.DependencyInjection;
using OpenTelemetry;
using OpenTelemetry.Exporter;
using OpenTelemetry.Instrumentation.AspNetCore;
using OpenTelemetry.Instrumentation.GrpcNetClient;
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

        /// <summary>
        /// Deferred values of the instrumentation options (design D-NET8): the health filters of the
        /// server, HttpClient and gRPC client instrumentations (contract normalization, the code
        /// <see cref="TelemetryOptions.HealthPaths"/> or the default list, and the whole
        /// <c>grpc.health.v1.Health</c> service), and one CLIENT span per gRPC call. A filtered
        /// server request is still counted in <c>http.server.request.duration</c> (the framework
        /// meter does not see the filter), and its children are not sampled under the ParentBased
        /// default (the filter clears the <c>Recorded</c> flag).
        /// </summary>
        internal static void ConfigureInstrumentationOptions(IServiceCollection services)
        {
            services.AddOptions<AspNetCoreTraceInstrumentationOptions>()
                .Configure<IServiceProvider>((opts, sp) =>
                {
                    if (!ResolvedAccess.TryGet(sp, out var resolved))
                    {
                        return;
                    }

                    var healthPaths = HealthMatching.PathSet(resolved.HealthPaths);
                    opts.Filter = httpContext => !HealthMatching.IsServerHealth(httpContext, healthPaths);
                    opts.RecordException = true;
                });
            services.AddOptions<HttpClientTraceInstrumentationOptions>()
                .Configure<IServiceProvider>((opts, sp) =>
                {
                    if (!ResolvedAccess.TryGet(sp, out var resolved))
                    {
                        return;
                    }

                    // Also the gRPC health service (contract A5 rulings: gRPC client health calls are
                    // excluded), for a call that reaches HttpClient without the gRPC instrumentation.
                    var healthPaths = HealthMatching.PathSet(resolved.HealthPaths);
                    opts.FilterHttpRequestMessage = req =>
                    {
                        if (!HealthMatching.IsHealth(HealthMatching.ClientPath(req), healthPaths))
                        {
                            return true;
                        }

                        // No trace context on the wire either, as Go's NewHTTPTransport (C11 fix rounds 1
                        // and 2): a traceparent here would name a span that is never exported. The
                        // instrumentation injected with the OpenTelemetry propagator before calling the
                        // filter (only what it wrote is removed); the runtime's own injection is
                        // restricted by HelperRuntimePropagator (ClientPaths).
                        HealthMatching.RemoveInjectedTraceContext(req, System.Diagnostics.Activity.Current);
                        return false;
                    };
                    HealthMatching.ClientPaths = healthPaths;
                    opts.RecordException = true;
                });
            services.AddOptions<GrpcClientTraceInstrumentationOptions>()
                .Configure(opts =>
                {
                    // One CLIENT span per call (DN7): the gRPC span, not also HttpClient's POST.
                    opts.SuppressDownstreamInstrumentation = true;
                    opts.Filter = req => !HealthMatching.IsGrpcHealthMethod(HealthMatching.ClientPath(req));
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

            // Always set (R6, ADR-002). OTEL_TRACES_EXPORTER=none keeps the resolved sampler (Go and
            // Python keep an SDK provider there): an inbound sampled flag reaches what HttpClient sends
            // on. Traces disabled (DisabledSignals): roots are not sampled and a remote parent's flag is
            // followed; what is propagated is the no-op tracer's (PropagatorSetup, pass-through only).
            builder.SetSampler(resolved.IsSignalEnabled("traces") ? resolved.Sampler : new ParentBasedSampler(new AlwaysOffSampler()));

            // "Off" (disabled signal, OTEL_TRACES_EXPORTER=none, or OTEL_SDK_DISABLED): instrumentation
            // registered, no source and no exporter (D-NET3, contract row "Signal on/off").
            var exporters = resolved.ResolvedSignalExporters("traces");
            if (exporters.Count == 0)
            {
                return;
            }

            builder.AddSource(resolved.ServiceName);
            foreach (var source in resolved.AdditionalActivitySources)
            {
                builder.AddSource(source);
            }

            if (resolved.DebugLevel)
            {
                builder.AddProcessor(new DebugMarkerProcessor());
            }

            var diagnostics = sp.GetRequiredService<IOtelHelperDiagnostics>();
            if (exporters.Contains("otlp")
                && OtlpExporters.OptionsFor("traces", resolved, diagnostics, sp.GetRequiredService<ExporterClients>()) is { } otlp)
            {
                builder.AddProcessor(CreateOtlpProcessor(sp, resolved, otlp, diagnostics));
            }

            // OTEL_TRACES_EXPORTER=console (or the alias logging): the SDK console exporter, built
            // directly because AddConsoleExporter registers services, which this deferred step cannot.
            if (exporters.Contains("console"))
            {
                builder.AddProcessor(new SimpleActivityExportProcessor(new ConsoleActivityExporter(new ConsoleExporterOptions())));
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
    }
}
