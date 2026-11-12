using Microsoft.Extensions.DependencyInjection;
using Microsoft.Extensions.DependencyInjection.Extensions;
using OpenTelemetry.Trace;

namespace StaffOps.OtelHelper.Profiling
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
        /// <remarks>
        /// <para>
        /// Nothing starts at registration: a hosted service checks the prerequisites
        /// (<see cref="ProfilingPrerequisites"/>) and starts the profiler when the host starts.
        /// When one is not met, the helper writes one <c>otelhelper: WARN</c> line on standard error,
        /// does not start the profiler and does not add the span processor (root spans get no
        /// <c>pyroscope.profile.id</c>). Calling this method twice registers the profiler once.
        /// </para>
        /// </remarks>
        public static IServiceCollection AddOtelHelperProfiling(this IServiceCollection services)
        {
            ArgumentNullException.ThrowIfNull(services);
            if (services.Any(d => d.ServiceType == typeof(ProfilingRegistered)))
            {
                return services;
            }

            services.AddSingleton<ProfilingRegistered>();
            services.TryAddSingleton<ProfilingState>();
            services.TryAddSingleton<IProfilingProvider, PyroscopeProfilingProvider>();
            services.AddHostedService<ProfilingStartup>();
            services.ConfigureOpenTelemetryTracerProvider((sp, builder) =>
            {
                if (sp.GetRequiredService<ProfilingState>().Prerequisites.IsSatisfied
                    && sp.GetRequiredService<IProfilingProvider>().GetSpanProcessor() is { } spanProcessor)
                {
                    builder.AddProcessor(spanProcessor);
                }
            });

            return services;
        }

        /// <summary>Marks a collection where <see cref="AddOtelHelperProfiling"/> already ran.</summary>
        private sealed class ProfilingRegistered
        {
        }
    }
}
