using Microsoft.Extensions.DependencyInjection;
using OpenTelemetry.Trace;

namespace OtelHelper.AWS
{
    /// <summary>
    /// Extension methods to register AWS instrumentation as an opt-in subpackage.
    /// </summary>
    public static class OtelAwsExtensions
    {
        /// <summary>
        /// Adds AWS SDK instrumentation to the OtelHelper tracing pipeline.
        /// </summary>
        public static IServiceCollection AddOtelHelperAws(this IServiceCollection services)
        {
            services.ConfigureOpenTelemetryTracerProvider(builder =>
                builder.AddAWSInstrumentation());

            return services;
        }
    }
}
