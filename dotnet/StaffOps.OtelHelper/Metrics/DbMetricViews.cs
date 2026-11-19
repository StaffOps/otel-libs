using System.Diagnostics.Metrics;
using OpenTelemetry.Metrics;

namespace StaffOps.OtelHelper.Metrics
{
    /// <summary>
    /// The view on the database driver meters (task C14a fix round 1, design D-NET15): Npgsql names
    /// an unnamed pool after its connection string without <c>Password</c> (other secret keys such as
    /// <c>SSL Password</c> stay) and MySqlConnector after its connection string without the password
    /// keys (<c>Certificate Password</c> stays), in the attribute <c>db.client.connection.pool.name</c>
    /// / <c>pool.name</c> of every metric point. That is a secret and a high-cardinality value; Go
    /// never puts the DSN on pool metrics. The view keeps an allowlist of attribute keys (the
    /// semantic-convention keys the drivers write or may write, without the pool name), so the
    /// pools of one meter are merged. Scoped by meter name: every other meter is untouched. Added
    /// by core whoever subscribed the meter (the Sql package or the application).
    /// </summary>
    internal static class DbMetricViews
    {
        internal const string NpgsqlMeter = "Npgsql";
        internal const string MySqlConnectorMeter = "MySqlConnector";

        /// <summary>Npgsql 10.0.3 writes <c>db.system.name</c>, <c>server.address</c>, <c>server.port</c>, <c>db.client.connection.state</c> (and the pool name).</summary>
        internal static readonly string[] NpgsqlKeys =
        {
            "db.system.name", "db.namespace", "server.address", "server.port", "db.client.connection.state",
            "db.operation.name", "db.collection.name", "db.query.summary", "db.response.status_code", "error.type",
        };

        /// <summary>MySqlConnector 2.6.2 writes <c>state</c> (and <c>pool.name</c>).</summary>
        internal static readonly string[] MySqlConnectorKeys =
        {
            "state", "db.system.name", "db.namespace", "server.address", "server.port", "db.client.connection.state", "error.type",
        };

        /// <summary>
        /// The stream for <paramref name="instrument"/>: null for another meter; Drop when
        /// <paramref name="dropped"/> (the helper's own drop rules); else the allowlisted keys.
        /// </summary>
        internal static MetricStreamConfiguration? View(Instrument instrument, Func<Instrument, bool> dropped)
        {
            var keys = instrument.Meter.Name switch
            {
                NpgsqlMeter => NpgsqlKeys,
                MySqlConnectorMeter => MySqlConnectorKeys,
                _ => null,
            };
            if (keys is null)
            {
                return null;
            }

            return dropped(instrument) ? MetricStreamConfiguration.Drop : new MetricStreamConfiguration { TagKeys = keys };
        }
    }
}
