using Microsoft.Extensions.DependencyInjection;
using OpenTelemetry.Metrics;
using OpenTelemetry.Trace;

namespace StaffOps.OtelHelper.Sql
{
    /// <summary>
    /// Extension methods to register database client instrumentation as an opt-in subpackage:
    /// Microsoft.Data.SqlClient / System.Data.SqlClient (the OpenTelemetry SqlClient
    /// instrumentation), Npgsql and MySqlConnector (their own <c>ActivitySource</c> and
    /// <c>Meter</c>, both named after the driver).
    /// </summary>
    public static class OtelSqlExtensions
    {
        /// <summary>The <c>ActivitySource</c> and <c>Meter</c> name of Npgsql (verified at 10.0.3).</summary>
        private const string NpgsqlSourceName = "Npgsql";

        /// <summary>The <c>ActivitySource</c> and <c>Meter</c> name of MySqlConnector (verified at 2.6.2).</summary>
        private const string MySqlConnectorSourceName = "MySqlConnector";

        /// <summary>
        /// Adds SqlClient tracing (exception events recorded) and metrics, and subscribes the
        /// Npgsql and MySqlConnector spans and metrics through <c>AddOtelHelper</c>'s additional
        /// sources and meters, so they follow its signal switches (no source or meter while the
        /// helper exports nothing). The helper's sanitizing processor handles the spans of the three
        /// drivers: a failed span keeps <c>ERROR</c> without the database's error text, Npgsql and
        /// MySqlConnector query literals become <c>?</c>, and connection strings are removed or
        /// redacted. Call <c>AddOtelHelper</c> too; without it the Npgsql and MySqlConnector sources
        /// are not subscribed.
        /// </summary>
        public static IServiceCollection AddOtelHelperSql(this IServiceCollection services)
        {
            services.ConfigureOpenTelemetryTracerProvider(builder =>
                builder.AddSqlClientInstrumentation(opts => opts.RecordException = true));
            services.ConfigureOpenTelemetryMeterProvider(builder => builder.AddSqlClientInstrumentation());

            // PostConfigure: after every Configure, so an application that replaces the lists
            // in a later Configure<TelemetryOptions> does not drop the drivers.
            services.PostConfigure<TelemetryOptions>(options =>
            {
                options.AdditionalActivitySources ??= new();
                options.AdditionalMeters ??= new();
                foreach (var name in new[] { NpgsqlSourceName, MySqlConnectorSourceName })
                {
                    if (!options.AdditionalActivitySources.Contains(name))
                    {
                        options.AdditionalActivitySources.Add(name);
                    }

                    if (!options.AdditionalMeters.Contains(name))
                    {
                        options.AdditionalMeters.Add(name);
                    }
                }
            });

            return services;
        }
    }
}
