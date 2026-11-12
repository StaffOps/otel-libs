using Microsoft.Extensions.DependencyInjection;
using OpenTelemetry.Trace;
using StackExchange.Redis;

namespace StaffOps.OtelHelper.Redis
{
    /// <summary>
    /// Extension methods to register Redis instrumentation as an opt-in subpackage.
    /// </summary>
    /// <remarks>
    /// The spans come from <c>OpenTelemetry.Instrumentation.StackExchangeRedis</c>, built from
    /// StackExchange.Redis's profiler after each command completed: they are named after the
    /// command (<c>SET</c>; a transaction is one <c>EXEC</c> span), carry no status, no
    /// <c>error.type</c> and no <c>exception</c> event even when the command failed (upstream; a
    /// documented gap). The instrumentation writes the legacy keys (<c>db.system</c>,
    /// <c>db.statement</c>, <c>db.redis.database_index</c>), and the new ones only when the process
    /// environment sets <c>OTEL_SEMCONV_STABILITY_OPT_IN</c> to <c>database/dup</c> or
    /// <c>database</c>. The helper's sanitizing processor (core) treats them as database client
    /// spans (owner decision O8 (b)): it adds <c>db.system.name=redis</c>, <c>db.operation.name</c>
    /// and <c>db.query.text</c> by default, removes <c>db.system</c>, <c>db.statement</c> and
    /// <c>db.redis.database_index</c> in the stable mode, and writes a statement with more than the
    /// command — <c>SetVerboseDatabaseStatements</c> adds the key or the Lua script — as the
    /// command and one <c>?</c> (task C14b).
    /// </remarks>
    public static class OtelRedisExtensions
    {
        /// <summary>
        /// Adds StackExchange.Redis instrumentation for the <see cref="IConnectionMultiplexer"/>
        /// registered in the service collection (the last registration, as <c>GetService</c>
        /// resolves it), resolved when the tracer provider is built. Building the tracer provider
        /// throws <see cref="InvalidOperationException"/> when no unkeyed
        /// <see cref="IConnectionMultiplexer"/> is registered (an application error, so it fails at
        /// start instead of tracing nothing; also with traces off); the check is skipped by a
        /// container without <see cref="IServiceProviderIsService"/>. For keyed or several
        /// multiplexers call <see cref="AddOtelHelperRedis(IServiceCollection, IConnectionMultiplexer)"/>
        /// once per multiplexer. Call once per multiplexer: a second registration of the same one
        /// starts a second drain thread and takes over its profiler (one span per command).
        /// </summary>
        public static IServiceCollection AddOtelHelperRedis(this IServiceCollection services)
        {
            ArgumentNullException.ThrowIfNull(services);

            // Upstream's DI-resolving overload: it registers its services now and resolves the
            // multiplexer in its instrumentation factory. Calling AddRedisInstrumentation inside a
            // deferred (sp, builder) callback, as 0.2.x did, registers services after the container
            // was built and throws NotSupportedException (task C14b, C1 dev B1).
            services.ConfigureOpenTelemetryTracerProvider(builder => builder.AddRedisInstrumentation());

            // A check only (it registers nothing): upstream instruments nothing, silently, when the
            // multiplexer is missing. IServiceProviderIsService answers without creating it.
            services.ConfigureOpenTelemetryTracerProvider((sp, _) =>
            {
                if (sp.GetService<IServiceProviderIsService>() is { } registry
                    && !registry.IsService(typeof(IConnectionMultiplexer)))
                {
                    throw new InvalidOperationException(
                        "AddOtelHelperRedis(): no IConnectionMultiplexer is registered in the service collection "
                        + "(a keyed-only registration is not found; pass it explicitly); register one or call "
                        + "AddOtelHelperRedis(connection). The check runs whenever the tracer provider is built, "
                        + "also with traces off");
                }
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
            ArgumentNullException.ThrowIfNull(services);
            ArgumentNullException.ThrowIfNull(connection);

            services.ConfigureOpenTelemetryTracerProvider(builder =>
                builder.AddRedisInstrumentation(connection));

            return services;
        }
    }
}
