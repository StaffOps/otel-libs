using System.Data.Common;
using System.Diagnostics;

namespace StaffOps.OtelHelper.Tracing
{
    /// <summary>
    /// The database rules of the sanitizing processor (task C14a; contract A25 and R10, design
    /// D-NET14). They apply to the spans of the database client sources by name, whoever
    /// subscribed the source (the Sql package or the application):
    /// <list type="number">
    /// <item><b>Status</b>: a failed span keeps <c>ERROR</c> and loses its description, the
    /// database's error text (A25). SqlClient 1.20.x already exports none; Npgsql 10 writes the
    /// SQLSTATE or, for other errors, the exception message; MySqlConnector 2.6 the error code or
    /// the exception message.</item>
    /// <item><b>Query text</b>: the literals of <c>db.query.text</c> and the legacy
    /// <c>db.statement</c> become <c>?</c> on Npgsql and MySqlConnector spans, which record the
    /// command text as written (<see cref="DbQuerySanitizer"/>). SqlClient sanitizes its own
    /// (upstream, kept).</item>
    /// <item><b>Connection string</b>: <c>db.connection_string</c> (MySqlConnector's legacy
    /// conventions, Npgsql 8 and 9) is removed — it is the connection string without
    /// <c>Password</c>, but with every other key, <c>Certificate Password</c> included.</item>
    /// <item><b>Data source</b>: <c>db.npgsql.data_source</c> (the connection string without
    /// <c>Password</c> unless the data source has a name) keeps its keys with the value of every
    /// password key (<c>SSL Password</c>, …) written as <c>REDACTED</c>; a value that does not parse
    /// is removed.</item>
    /// <item><b>Bound values</b>: every <c>db.query.parameter.*</c> attribute is removed (SqlClient
    /// writes them with <c>OTEL_DOTNET_EXPERIMENTAL_SQLCLIENT_ENABLE_TRACE_DB_QUERY_PARAMETERS</c>).</item>
    /// <item><b>Semantic-convention keys</b> (R10, as Go): <c>db.system.name</c> and the legacy
    /// <c>db.system</c> both by default, the new keys only when <c>OTEL_SEMCONV_STABILITY_OPT_IN</c>
    /// holds <c>database</c> without <c>database/dup</c>; a MySqlConnector or Redis <c>db.statement</c> also
    /// gives <c>db.query.text</c>. Spans only: the drivers' metrics keep the keys each driver writes.</item>
    /// </list>
    /// The <c>exception</c> event keeps only <c>exception.type</c>; the detail goes to a correlated log
    /// record (ADR-014, <see cref="DbExceptionEvents"/>).
    /// <para>
    /// Redis (task C14b, contract A26): the StackExchange.Redis spans
    /// (<see cref="StackExchangeRedisSource"/>) are database client spans too. Their
    /// <c>db.statement</c> / <c>db.query.text</c> is the command only by default; with
    /// <c>SetVerboseDatabaseStatements</c> upstream appends the key or the Lua script (measured on
    /// <c>redis:7.4.12-alpine</c>; e.g. <c>SET orders:42</c>, <c>EVAL local x = '...' return x</c>), so a
    /// statement with more than the command becomes the command and one <c>?</c>
    /// (<see cref="RedisStatement"/>; Python writes one <c>?</c> per argument, Go no statement). The
    /// status, exception-event, connection-string and parameter rules apply as for the SQL drivers
    /// (upstream sets no status and adds no exception event; an application <c>Enrich</c> may). The
    /// semantic-convention keys follow the same R10 rule (owner decision O8 (b), reversed from (c)
    /// on 2026-11-11): <c>db.system.name=redis</c>, <c>db.operation.name</c> (the command) and
    /// <c>db.query.text</c> (from the sanitized <c>db.statement</c>) are added by default; the stable
    /// mode removes <c>db.system</c>, <c>db.statement</c> and <c>db.redis.database_index</c>. A key the instrumentation wrote is kept;
    /// <c>db.namespace</c> is not derived (Go and Python write none).
    /// </para>
    /// </summary>
    internal static class DbSpanRules
    {
        internal const string SqlClientSource = "OpenTelemetry.Instrumentation.SqlClient";
        internal const string NpgsqlSource = "Npgsql";
        internal const string MySqlConnectorSource = "MySqlConnector";

        /// <summary>The source of <c>OpenTelemetry.Instrumentation.StackExchangeRedis</c> (its assembly name; verified at 1.20.1-beta.1).</summary>
        internal const string StackExchangeRedisSource = "OpenTelemetry.Instrumentation.StackExchangeRedis";
        internal const string Redacted = "REDACTED";

        /// <summary>Whether <paramref name="sourceName"/> is a database client source these rules cover.</summary>
        internal static bool IsDbSource(string sourceName)
            => sourceName is SqlClientSource or NpgsqlSource or MySqlConnectorSource or StackExchangeRedisSource;

        /// <summary>
        /// Whether the legacy keys (<c>db.system</c>, <c>db.statement</c>) are emitted: yes unless
        /// <c>OTEL_SEMCONV_STABILITY_OPT_IN</c> holds <c>database</c> without <c>database/dup</c>
        /// (contract R10, A25; tokens trimmed with the contract set and ASCII-folded, as Go
        /// <c>ext/otelsql</c> <c>legacyKeysEnabled</c>).
        /// </summary>
        internal static bool LegacyKeysEnabled(string? optIn)
        {
            bool stable = false, dup = false;
            foreach (var token in (optIn ?? string.Empty).Split(','))
            {
                switch (ContractText.AsciiLower(ContractText.Trim(token)))
                {
                    case "database":
                        stable = true;
                        break;
                    case "database/dup":
                        dup = true;
                        break;
                }
            }

            return dup || !stable;
        }

        /// <summary>
        /// Applies the rules to a span of a database client source; other spans are left alone.
        /// A tag is written only when its value changes, so a second pass over an unchanged span
        /// (the helper's exporters) only reads it.
        /// </summary>
        internal static void Apply(Activity activity, bool legacyKeys)
        {
            var source = activity.Source.Name;
            if (!IsDbSource(source))
            {
                return;
            }

            if (activity.Status == ActivityStatusCode.Error && !string.IsNullOrEmpty(activity.StatusDescription))
            {
                activity.SetStatus(ActivityStatusCode.Error);
            }

            Remove(activity, "otel.status_description");
            Remove(activity, "db.connection_string");
            // The database user (semconv removed db.user; Go never writes it).
            Remove(activity, "db.user");

            // Bound values (R10): SqlClient records them as db.query.parameter.<name> when the deployment
            // sets OTEL_DOTNET_EXPERIMENTAL_SQLCLIENT_ENABLE_TRACE_DB_QUERY_PARAMETERS.
            List<string>? parameters = null;
            foreach (var tag in activity.TagObjects)
            {
                if (tag.Key.StartsWith("db.query.parameter.", StringComparison.Ordinal))
                {
                    (parameters ??= new()).Add(tag.Key);
                }
            }

            foreach (var key in parameters ?? Enumerable.Empty<string>())
            {
                activity.SetTag(key, null);
            }

            var dialect = source switch
            {
                NpgsqlSource => SqlDialect.PostgreSql,
                MySqlConnectorSource => SqlDialect.MySql,
                _ => (SqlDialect?)null,
            };
            if (dialect is { } d)
            {
                SanitizeQuery(activity, "db.query.text", d);
                SanitizeQuery(activity, "db.statement", d);
            }

            if (source == NpgsqlSource && activity.GetTagItem("db.npgsql.data_source") is string dataSource)
            {
                Set(activity, "db.npgsql.data_source", RedactConnectionString(dataSource));
            }

            if (source == StackExchangeRedisSource)
            {
                foreach (var key in RedisStatementKeys)
                {
                    if (activity.GetTagItem(key) is string statement)
                    {
                        Set(activity, key, RedisStatement(statement));
                    }
                }
            }

            ApplySemconvKeys(activity, source, legacyKeys);
        }

        /// <summary>
        /// Both generations by default, only the new keys in the stable mode (R10, as Go's
        /// <c>systemAttributes</c>): <c>db.system.name</c> and the legacy <c>db.system</c> (v1.26
        /// values: SqlClient <c>microsoft.sql_server</c> / <c>mssql</c>, Npgsql and MySqlConnector
        /// <c>postgresql</c>, <c>mysql</c>), on every span of the source that carries one of the two;
        /// a MySqlConnector or Redis <c>db.statement</c> also gives <c>db.query.text</c> (both sanitized). In the
        /// stable mode the legacy <c>db.system</c> and <c>db.statement</c> are removed.
        /// </summary>
        internal static void ApplySemconvKeys(Activity activity, string source, bool legacyKeys)
        {
            var (name, legacy) = source switch
            {
                SqlClientSource => ("microsoft.sql_server", "mssql"),
                NpgsqlSource => ("postgresql", "postgresql"),
                MySqlConnectorSource => ("mysql", "mysql"),
                StackExchangeRedisSource => ("redis", "redis"),
                _ => ((string?)null, (string?)null),
            };
            if (name is null || legacy is null)
            {
                return;
            }

            var hasName = activity.GetTagItem("db.system.name") is not null;
            var hasLegacy = activity.GetTagItem("db.system") is not null;
            if (hasName || hasLegacy)
            {
                if (!hasName)
                {
                    activity.SetTag("db.system.name", name);
                }

                if (legacyKeys && !hasLegacy)
                {
                    activity.SetTag("db.system", legacy);
                }
                else if (!legacyKeys && hasLegacy)
                {
                    activity.SetTag("db.system", null);
                }
            }

            if (source == MySqlConnectorSource)
            {
                // MySqlConnector's other legacy keys (round 3), with the same rule.
                foreach (var (legacyKey, newKey) in MySqlLegacyKeys)
                {
                    if (activity.GetTagItem(legacyKey) is { } value)
                    {
                        if (activity.GetTagItem(newKey) is null)
                        {
                            activity.SetTag(newKey, value);
                        }

                        if (!legacyKeys)
                        {
                            activity.SetTag(legacyKey, null);
                        }
                    }
                }
            }

            // The legacy Redis index has no current key (db.namespace is not derived): stable mode drops it.
            if (source == StackExchangeRedisSource && !legacyKeys && activity.GetTagItem("db.redis.database_index") is not null)
            {
                activity.SetTag("db.redis.database_index", null);
            }

            if (source == StackExchangeRedisSource && activity.GetTagItem("db.operation.name") is null
                && RedisOperation(activity) is { } operation)
            {
                activity.SetTag("db.operation.name", operation);
            }

            if (source is MySqlConnectorSource or StackExchangeRedisSource && activity.GetTagItem("db.statement") is string statement)
            {
                if (activity.GetTagItem("db.query.text") is null)
                {
                    activity.SetTag("db.query.text", statement);
                }

                if (!legacyKeys)
                {
                    activity.SetTag("db.statement", null);
                }
            }
        }

        /// <summary>The span name the Redis instrumentation gives a command without a name (never an operation).</summary>
        internal const string RedisUnnamedSpan = StackExchangeRedisSource + ".Execute";

        /// <summary>
        /// The command of a Redis span as the client provides it: the first token of
        /// <c>db.statement</c> (or <c>db.query.text</c>), else the span name unless it is the
        /// instrumentation's unnamed fallback; null when none is known.
        /// </summary>
        internal static string? RedisOperation(Activity activity)
        {
            foreach (var key in RedisStatementKeys)
            {
                if (activity.GetTagItem(key) is string text)
                {
                    var command = RedisStatement(text);
                    var space = command.IndexOf(' ');
                    var token = space < 0 ? command : command[..space];
                    if (token.Length > 0 && token != "?")
                    {
                        return token;
                    }
                }
            }

            var name = activity.DisplayName;
            return string.IsNullOrWhiteSpace(name) || name == RedisUnnamedSpan ? null : name;
        }

        /// <summary>The Redis statement keys, legacy and current.</summary>
        private static readonly string[] RedisStatementKeys = { "db.statement", "db.query.text" };

        /// <summary>MySqlConnector 2.6 legacy keys and their current names.</summary>
        private static readonly (string Legacy, string New)[] MySqlLegacyKeys =
        {
            ("db.name", "db.namespace"), ("net.peer.name", "server.address"), ("net.peer.port", "server.port"),
        };

        private static void Remove(Activity activity, string key)
        {
            if (activity.GetTagItem(key) is not null)
            {
                activity.SetTag(key, null);
            }
        }

        private static void Set(Activity activity, string key, string? value)
        {
            if (!Equals(activity.GetTagItem(key), value))
            {
                activity.SetTag(key, value);
            }
        }

        /// <summary>
        /// Fail-closed path when a rule threw: status description, query text, connection string,
        /// data source, the AWS payload and exception-text attributes and the URL attributes removed.
        /// </summary>
        internal static void Strip(Activity activity)
        {
            if (activity.Status == ActivityStatusCode.Error && activity.StatusDescription is not null)
            {
                activity.SetStatus(ActivityStatusCode.Error);
            }

            foreach (var key in StrippedKeys)
            {
                Remove(activity, key);
            }
        }

        private static readonly string[] StrippedKeys = new[]
        {
            "otel.status_description", "db.connection_string", "db.query.text", "db.statement", "db.npgsql.data_source",
            "url.full", "http.url", "url.query", "http.target",
        }.Concat(AwsSpanRules.DroppedKeys).ToArray();

        private static void SanitizeQuery(Activity activity, string key, SqlDialect dialect)
        {
            if (activity.GetTagItem(key) is string text)
            {
                Set(activity, key, DbQuerySanitizer.Sanitize(text, dialect));
            }
        }

        /// <summary>
        /// A StackExchange.Redis statement without its arguments: the command (the text up to the first
        /// whitespace) and one <c>?</c> when anything follows it (<c>SET orders:42</c> → <c>SET ?</c>,
        /// <c>EVAL return redis.call('GET', KEYS[1])</c> → <c>EVAL ?</c>); a bare command unchanged,
        /// trailing whitespace dropped, a text starting with whitespace <c>?</c>.
        /// </summary>
        internal static string RedisStatement(string text)
        {
            var end = 0;
            while (end < text.Length && !char.IsWhiteSpace(text[end]))
            {
                end++;
            }

            if (end == text.Length)
            {
                return text;
            }

            if (end == 0)
            {
                return "?";
            }

            for (var i = end; i < text.Length; i++)
            {
                if (!char.IsWhiteSpace(text[i]))
                {
                    return string.Concat(text.AsSpan(0, end), " ?");
                }
            }

            return text[..end];
        }

        /// <summary>
        /// <paramref name="value"/> with the value of every password key (a key containing
        /// <c>password</c> in any case, or <c>pwd</c>) written as <c>REDACTED</c>; a value
        /// without <c>=</c> (a data source name) unchanged; null when it does not parse.
        /// </summary>
        internal static string? RedactConnectionString(string value)
        {
            if (!value.Contains('='))
            {
                return value;
            }

            DbConnectionStringBuilder builder;
            try
            {
                builder = new DbConnectionStringBuilder { ConnectionString = value };
            }
            catch (ArgumentException)
            {
                return null;
            }

            var secretKeys = builder.Keys.Cast<string>().Where(IsPasswordKey).ToList();
            foreach (var key in secretKeys)
            {
                builder[key] = Redacted;
            }

            return secretKeys.Count == 0 ? value : builder.ConnectionString;
        }

        private static bool IsPasswordKey(string key)
            => key.Contains("password", StringComparison.OrdinalIgnoreCase) || key.Equals("pwd", StringComparison.OrdinalIgnoreCase);
    }
}
