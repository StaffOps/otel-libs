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

            // Build resolved options for pipeline setup
            var opts = new TelemetryOptions();
            configure(opts);
            new TelemetryOptionsPostConfigure().PostConfigure(null, opts);

            // Register ActivitySource for DI — avoids manual "new ActivitySource(serviceName)" in each class
            services.AddSingleton(new ActivitySource(opts.ServiceName));

            // Register Meter for DI — avoids manual "new Meter(serviceName)" in each class
            services.AddSingleton(new Meter(opts.ServiceName));

            services.AddOpenTelemetry()
                .ConfigureResource(r =>
                {
                    r.AddService(serviceName: opts.ServiceName);
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
