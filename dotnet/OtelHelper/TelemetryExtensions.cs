using System;
using System.Diagnostics;
using System.Diagnostics.Metrics;
using System.Linq;
using Microsoft.Extensions.DependencyInjection;
using Microsoft.Extensions.Options;
using OpenTelemetry.Resources;
using OtelHelper.Logging;
using OtelHelper.Metrics;
using OtelHelper.Tracing;

namespace OtelHelper
{
    public static class TelemetryExtensions
    {
        /// <summary>
        /// Registers all OtelHelper telemetry using environment variables only.
        /// </summary>
        public static IServiceCollection AddOtelHelper(this IServiceCollection services)
            => AddOtelHelper(services, _ => { });

        /// <summary>
        /// Registers all OtelHelper telemetry with optional overrides on top of env var defaults.
        /// Environment variables are applied via IPostConfigureOptions — consumer overrides take priority.
        /// </summary>
        public static IServiceCollection AddOtelHelper(
            this IServiceCollection services,
            Action<TelemetryOptions> configure)
        {
            // Guard against double registration
            if (services.Any(s => s.ServiceType == typeof(IValidateOptions<TelemetryOptions>)
                && s.ImplementationType == typeof(TelemetryOptionsValidator)))
                return services;

            // Register options pipeline: Configure (consumer) → PostConfigure (env vars) → Validate
            services.AddOptions<TelemetryOptions>()
                .Configure(configure)
                .ValidateOnStart();

            services.AddSingleton<IPostConfigureOptions<TelemetryOptions>, TelemetryOptionsPostConfigure>();
            services.AddSingleton<IValidateOptions<TelemetryOptions>, TelemetryOptionsValidator>();

            // Register ActivitySource/Meter lazily, resolved from the real IOptions<TelemetryOptions>
            // pipeline on first use — not a hand-rolled snapshot. Any Configure<TelemetryOptions>
            // a consumer registers (before this factory first runs) is picked up correctly, since
            // DI's IOptionsFactory re-runs Configure -> PostConfigure -> Validate on first .Value access.
            services.AddSingleton(sp =>
                new ActivitySource(sp.GetRequiredService<IOptions<TelemetryOptions>>().Value.ServiceName));
            services.AddSingleton(sp =>
                new Meter(sp.GetRequiredService<IOptions<TelemetryOptions>>().Value.ServiceName));

            // The OTel SDK builder callbacks below (ConfigureResource/WithTracing/WithMetrics) have
            // no IServiceProvider-aware overload in OpenTelemetry.Extensions.Hosting — there is no
            // way to defer this configuration to first-resolution time the way the ActivitySource/
            // Meter factories above do. To avoid maintaining a second, hand-rolled re-implementation
            // of Configure+PostConfigure (the original P9 bug — two independent copies of the same
            // resolution logic that could silently drift apart), resolve options through the real
            // DI options pipeline itself, via a short-lived bootstrap provider scoped to the
            // registrations made so far. This still runs Configure, PostConfigure, and validation
            // exactly once, through one code path — not a duplicate.
            //
            // Residual limitation: because this resolution happens now (synchronously, during
            // AddOtelHelper), a services.Configure<TelemetryOptions>(...) registered by the consumer
            // AFTER this call still won't be reflected in the tracing/metrics/resource pipeline built
            // below (though it WILL be reflected by IOptions<TelemetryOptions> resolved elsewhere,
            // including ValidateOnStart — so validation and pipeline setup can still disagree in that
            // specific scenario). Register any TelemetryOptions overrides before calling
            // AddOtelHelper(), or via the `configure` parameter here, to avoid it.
            TelemetryOptions opts;
            using (var bootstrapProvider = services.BuildServiceProvider())
            {
                opts = bootstrapProvider.GetRequiredService<IOptions<TelemetryOptions>>().Value;
            }

            services.AddOpenTelemetry()
                .ConfigureResource(r =>
                {
                    r.AddService(serviceName: opts.ServiceName);
                    // "deployment.environment.name" is the semconv >= v1.27 key
                    // (not the legacy "deployment.environment") — kept in sync with Go/Python.
                    r.AddAttributes(new[]
                    {
                        new KeyValuePair<string, object>("deployment.environment.name", opts.Environment.ToString())
                    });
                    if (opts.ResourceAttributes.Count > 0)
                        r.AddAttributes(opts.ResourceAttributes.Select(kv => new KeyValuePair<string, object>(kv.Key, kv.Value)));
                })
                .WithTracing(builder => { if (opts.IsSignalEnabled("traces")) builder.ConfigureTracing(opts); })
                .WithMetrics(builder =>
                {
                    // "none" (empty resolved list) disables metrics, same as DisabledSignals.
                    if (opts.IsSignalEnabled("metrics") && opts.ResolvedMetricExporters().Length > 0)
                        builder.ConfigureMetrics(opts);
                });

            if (opts.IsSignalEnabled("logs"))
                services.ConfigureLogging(opts);

            return services;
        }
    }
}
