using Microsoft.Extensions.DependencyInjection;
using Microsoft.Extensions.Logging;
using OpenTelemetry.Trace;

namespace OtelHelper.Profiling
{
    /// <summary>
    /// Extension methods to add Pyroscope continuous profiling.
    /// </summary>
    public static class OtelProfilingExtensions
    {
        /// <summary>
        /// Adds Pyroscope continuous profiling and registers a span processor for
        /// trace↔profile correlation.
        /// </summary>
        public static IServiceCollection AddOtelHelperProfiling(this IServiceCollection services)
        {
            var prereqs = ProfilingPrerequisites.FromEnvironment();

            if (!prereqs.IsSatisfied)
            {
                services.AddHostedService(sp =>
                    new ProfilingPrerequisiteWarning(
                        sp.GetRequiredService<ILoggerFactory>(),
                        prereqs.DescribeMissing()!));
            }

            var provider = new PyroscopeProfilingProvider();
            provider.Start();
            services.AddSingleton<IProfilingProvider>(provider);

            var spanProcessor = provider.GetSpanProcessor();
            if (spanProcessor is not null)
            {
                services.ConfigureOpenTelemetryTracerProvider(builder =>
                {
                    builder.AddProcessor(spanProcessor);
                });
            }

            return services;
        }
    }
}
