using System;
using System.Diagnostics;
using System.Diagnostics.Metrics;
using System.Linq;
using Microsoft.Extensions.Configuration;
using Microsoft.Extensions.DependencyInjection;
using Microsoft.Extensions.DependencyInjection.Extensions;
using Microsoft.Extensions.Options;
using OpenTelemetry;
using OpenTelemetry.Logs;
using OpenTelemetry.Metrics;
using OpenTelemetry.Trace;
using StaffOps.OtelHelper.Diagnostics;
using StaffOps.OtelHelper.Logging;
using StaffOps.OtelHelper.Metrics;
using StaffOps.OtelHelper.Tracing;

namespace StaffOps.OtelHelper
{
    /// <summary>
    /// Entry point of the helper: <c>AddOtelHelper</c> registers tracing, metrics and logging.
    /// </summary>
    public static class TelemetryExtensions
    {
        /// <summary>
        /// Registers all StaffOps.OtelHelper telemetry using environment variables only.
        /// </summary>
        public static IServiceCollection AddOtelHelper(this IServiceCollection services)
            => AddOtelHelper(services, _ => { });

        /// <summary>
        /// Registers all StaffOps.OtelHelper telemetry. Values set in <paramref name="configure"/>
        /// (or in any <c>Configure&lt;TelemetryOptions&gt;</c>, also one registered later or bound
        /// from <c>IConfiguration</c>) win over environment variables; an invalid code value
        /// fails host start naming the option. A second call changes nothing and warns once.
        /// </summary>
        public static IServiceCollection AddOtelHelper(
            this IServiceCollection services,
            Action<TelemetryOptions> configure)
        {
            if (services.Any(s => s.ServiceType == typeof(AddOtelHelperMarker)))
            {
                if (!services.Any(s => s.ServiceType == typeof(DoubleCallWarnedMarker)))
                {
                    services.AddSingleton<DoubleCallWarnedMarker>();
                    RegistrationSink(services).Report(DiagnosticLevel.Warn,
                        "AddOtelHelper was called more than once; the later calls change nothing (configure options with Configure<TelemetryOptions> instead)");
                }

                return services;
            }

            services.AddSingleton<AddOtelHelperMarker>();

            // Code options: Configure (application) → Validate (code values only, D-NET6).
            services.AddOptions<TelemetryOptions>()
                .Configure(configure)
                .ValidateOnStart();
            services.AddSingleton<IValidateOptions<TelemetryOptions>, TelemetryOptionsValidator>();

            // Diagnostics sink (D-NET5): one per container unless the application or a test registered one.
            services.TryAddSingleton<IOtelHelperDiagnostics>(_ => new OtelHelperDiagnostics());
            services.TryAddSingleton(sp =>
            {
                var code = sp.GetRequiredService<IOptions<TelemetryOptions>>().Value;
                var codeSecrets = new[] { code.OtelCollectorEndpoint, code.TracesEndpoint, code.MetricsEndpoint, code.LogsEndpoint }
                    .SelectMany(SdkTextSanitizer.EndpointSecrets);
                return SdkDiagnosticsForwarder.Acquire(sp.GetRequiredService<IOtelHelperDiagnostics>(), codeSecrets);
            });

            // The helper's exporter clients, disposed with the container after the providers.
            services.TryAddSingleton<StaffOps.OtelHelper.Exporters.ExporterClients>();

            // Resolved once per container, from the final IOptions value (late Configure included).
            services.TryAddSingleton(sp =>
            {
                var resolved = TelemetryResolver.Resolve(
                    sp.GetRequiredService<IOptions<TelemetryOptions>>().Value,
                    sp.GetRequiredService<IOtelHelperDiagnostics>(),
                    sp.GetService<IConfiguration>());
                MetricsSetup.WarnHistogramAggregation(sp, resolved, sp.GetService<IConfiguration>(), sp.GetRequiredService<IOtelHelperDiagnostics>());
                // Every provider takes a lease, also one that does not forward: its secrets are masked
                // in what another provider forwards. Created before the providers, so it is disposed
                // after them and sees their shutdown. Secrets: the code endpoints (factory) plus the
                // header values captured now (A29: the union of setup-time and current values).
                var lease = sp.GetRequiredService<SdkDiagnosticsForwarder.Lease>();
                lease.Secrets = lease.Secrets.Concat(SdkTextSanitizer.HeaderSecrets(resolved.Headers)).ToArray();
                if (!resolved.IsSignalEnabled("metrics") || resolved.ResolvedMetricExporters().Count == 0)
                {
                    lease.QuietMeters = new[] { resolved.ServiceName }.Concat(resolved.AdditionalMeters).ToArray();
                }

                lease.Forwards = resolved.ForwardSdkDiagnostics;

                return resolved;
            });

            // Resolves (and so reports) at host start even when no provider reads the values,
            // e.g. under OTEL_SDK_DISABLED=true. Registered before the SDK's hosted service.
            services.AddHostedService<ResolveAtStartService>();

            // The service's ActivitySource and Meter, keyed (DN5, N1): an unkeyed ActivitySource
            // would replace the one ASP.NET Core resolves for its server spans.
            services.AddKeyedSingleton(OtelHelperKeys.Instrumentation,
                (sp, _) => new ActivitySource(sp.GetRequiredService<ResolvedTelemetry>().ServiceName));
            services.AddKeyedSingleton(OtelHelperKeys.Instrumentation,
                (sp, _) => new Meter(sp.GetRequiredService<ResolvedTelemetry>().ServiceName));

            // Eager: every call that registers services (D-NET3). Deferred: values only.
            services.AddOpenTelemetry()
                .WithTracing(builder => builder.AddHelperInstrumentation())
                .WithMetrics(builder =>
                {
                    builder.AddHelperInstrumentation();
                    if (MetricsSetup.HistogramAggregationRequested(services))
                    {
                        if (MetricsSetup.UseOtlpExporterRegistered(services))
                        {
                            // UseOtlpExporter's own reader applies the variable; the SDK refuses both paths.
                            RegistrationSink(services).ReportOnce("use-otlp-exporter-path", DiagnosticLevel.Warn,
                                $"UseOtlpExporter() is registered: its own reader applies {TelemetryResolver.HistogramAggregationVariable}, so AddOtelHelper registers no second reader for it",
                                ("variable", TelemetryResolver.HistogramAggregationVariable));
                        }
                        else
                        {
                            MetricsSetup.AddHistogramAggregationPath(builder, services);
                        }
                    }
                })
                .WithLogging(_ => { }, LoggingSetup.ConfigureLoggerOptions);

            TracerSetup.ConfigureInstrumentationOptions(services);
            MetricsSetup.ConfigureListenerOptions(services);
            MetricsSetup.ConfigureReaderOptions(services);
            LoggingSetup.ConfigureFilters(services);
            services.ConfigureOpenTelemetryTracerProvider(TracerSetup.ApplyResolved);
            services.ConfigureOpenTelemetryMeterProvider(MetricsSetup.ApplyResolved);
            services.ConfigureOpenTelemetryLoggerProvider(LoggingSetup.ApplyResolved);

            return services;
        }

        /// <summary>The sink an application or test registered as an instance, else the static fallback.</summary>
        private static IOtelHelperDiagnostics RegistrationSink(IServiceCollection services)
            => services.LastOrDefault(s => s.ServiceType == typeof(IOtelHelperDiagnostics) && !s.IsKeyedService)?.ImplementationInstance as IOtelHelperDiagnostics
                ?? OtelHelperDiagnostics.Fallback;

        /// <summary>Resolves <see cref="ResolvedTelemetry"/> when the host starts.</summary>
        private sealed class ResolveAtStartService : Microsoft.Extensions.Hosting.IHostedService
        {
            private readonly IServiceProvider _services;

            public ResolveAtStartService(IServiceProvider services) => _services = services;

            public System.Threading.Tasks.Task StartAsync(System.Threading.CancellationToken cancellationToken)
            {
                _services.GetRequiredService<ResolvedTelemetry>();

                // The Prometheus listener's cancel is shutdown noise only once the host stops
                // (attached here, not in the lease factory: the lifetime needs a logger, which may
                // be what is resolving the lease).
                if (_services.GetService<Microsoft.Extensions.Hosting.IHostApplicationLifetime>() is { } lifetime)
                {
                    var lease = _services.GetRequiredService<SdkDiagnosticsForwarder.Lease>();
                    lease.HasLifetime = true;
                    lifetime.ApplicationStopping.Register(() => lease.Stopping = true);
                }

                return System.Threading.Tasks.Task.CompletedTask;
            }

            public System.Threading.Tasks.Task StopAsync(System.Threading.CancellationToken cancellationToken) => System.Threading.Tasks.Task.CompletedTask;
        }

        /// <summary>Marks a collection AddOtelHelper already configured.</summary>
        private sealed class AddOtelHelperMarker
        {
        }

        /// <summary>Marks that the double-call warning was written for a collection.</summary>
        private sealed class DoubleCallWarnedMarker
        {
        }
    }
}
