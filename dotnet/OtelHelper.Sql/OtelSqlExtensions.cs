using Microsoft.Extensions.DependencyInjection;
using OpenTelemetry.Trace;

namespace OtelHelper.Sql
{
    /// <summary>
    /// Extension methods to register SQL Client instrumentation as an opt-in subpackage.
    /// </summary>
    public static class OtelSqlExtensions
    {
        /// <summary>
        /// Adds SQL Client instrumentation with exception recording enabled.
        /// </summary>
        public static IServiceCollection AddOtelHelperSql(this IServiceCollection services)
        {
            services.ConfigureOpenTelemetryTracerProvider(builder =>
                builder.AddSqlClientInstrumentation(opts => opts.RecordException = true));

            return services;
        }
    }
}
