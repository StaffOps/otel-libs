using Microsoft.Extensions.DependencyInjection;
using OpenTelemetry.Trace;
using StackExchange.Redis;

namespace OtelHelper.Redis
{
    /// <summary>
    /// Extension methods to register Redis instrumentation as an opt-in subpackage.
    /// </summary>
    public static class OtelRedisExtensions
    {
        /// <summary>
        /// Adds StackExchange.Redis instrumentation, resolving IConnectionMultiplexer from DI.
        /// </summary>
        public static IServiceCollection AddOtelHelperRedis(this IServiceCollection services)
        {
            services.ConfigureOpenTelemetryTracerProvider((sp, builder) =>
            {
                var connection = sp.GetRequiredService<IConnectionMultiplexer>();
                builder.AddRedisInstrumentation(connection);
            });

            return services;
        }

        /// <summary>
        /// Adds StackExchange.Redis instrumentation with an explicit IConnectionMultiplexer instance.
        /// </summary>
        public static IServiceCollection AddOtelHelperRedis(
            this IServiceCollection services,
            IConnectionMultiplexer connection)
        {
            services.ConfigureOpenTelemetryTracerProvider(builder =>
                builder.AddRedisInstrumentation(connection));

            return services;
        }
    }
}
