using System.Collections.Concurrent;
using System.Diagnostics;
using Microsoft.Extensions.DependencyInjection;
using MySqlConnector;
using Npgsql;
using OpenTelemetry;
using OpenTelemetry.Logs;
using OpenTelemetry.Metrics;
using OpenTelemetry.Trace;
using StaffOps.OtelHelper.Diagnostics;
using StaffOps.OtelHelper.Sql;
using Xunit;

namespace StaffOps.OtelHelper.Tests;

/// <summary>
/// Skipped unless the database URL in <see cref="Variable"/> is set (compose <c>dotnet-tests</c>
/// sets it for <c>sql-postgres</c> / <c>sql-mysql</c>); with <c>SQL_TEST_REQUIRE=1</c> a missing URL
/// fails the test instead, as in the Node.js suite.
/// </summary>
public sealed class DatabaseFactAttribute : FactAttribute
{
    public DatabaseFactAttribute(string variable)
    {
        Variable = variable;
        if (string.IsNullOrEmpty(Environment.GetEnvironmentVariable(variable)) && Environment.GetEnvironmentVariable("SQL_TEST_REQUIRE") != "1")
        {
            Skip = $"{variable} is not set (compose dotnet-tests sets it)";
        }
    }

    public string Variable { get; }
}

/// <summary>
/// Task C14a against real servers (compose <c>sql-postgres</c>, <c>sql-mysql</c>; throwaway
/// credentials): Npgsql 10.0.3 and MySqlConnector 2.6.2 spans through <c>AddOtelHelper</c> +
/// <c>AddOtelHelperSql</c>, after the sanitizer. Each test asserts what the driver emits upstream
/// (attribute keys at the pinned version) and that no literal, password or error text of the
/// statement reaches a span attribute or the status.
/// </summary>
[Collection("EnvVarTests")]
[ResetGlobalPropagation]
public sealed class SqlDriverIntegrationTests
{
    private const string PasswordInUrl = "otel-test-only";

    private sealed class Capture : BaseProcessor<Activity>
    {
        public ConcurrentQueue<Activity> Ended { get; } = new();

        public override void OnEnd(Activity data) => Ended.Enqueue(data);
    }

    /// <summary>Log records after the helper's processors.</summary>
    private sealed class LogCapture : BaseProcessor<OpenTelemetry.Logs.LogRecord>
    {
        public ConcurrentQueue<(string? Category, ActivityTraceId TraceId, ActivitySpanId SpanId, string? Body, List<KeyValuePair<string, object?>> Attributes)> Records { get; } = new();

        public override void OnEnd(OpenTelemetry.Logs.LogRecord data)
            => Records.Enqueue((data.CategoryName, data.TraceId, data.SpanId, data.FormattedMessage ?? data.Body, data.Attributes?.ToList() ?? new()));
    }

    /// <summary>ADR-014: one correlated record per failed span, with the detail; returns it.</summary>
    private static (string? Category, ActivityTraceId TraceId, ActivitySpanId SpanId, string? Body, List<KeyValuePair<string, object?>> Attributes) AssertCorrelatedLog(LogCapture logs, Activity span, string secret)
    {
        var record = Assert.Single(logs.Records, r => r.Category == "StaffOps.OtelHelper.Database" && r.SpanId == span.SpanId);
        Assert.Equal(span.TraceId, record.TraceId);
        Assert.Contains(secret, record.Body, StringComparison.Ordinal);
        Assert.Contains(secret, (string?)record.Attributes.FirstOrDefault(a => a.Key == "exception.message").Value, StringComparison.Ordinal);
        Assert.False(string.IsNullOrEmpty((string?)record.Attributes.FirstOrDefault(a => a.Key == "exception.stacktrace").Value));
        Assert.NotNull(record.Attributes.FirstOrDefault(a => a.Key == "exception.type").Value);
        var exception = Assert.Single(span.Events, e => e.Name == "exception");
        Assert.DoesNotContain(exception.Tags, t => t.Key is "exception.message" or "exception.stacktrace");
        Assert.Contains(exception.Tags, t => t.Key == "exception.type");
        return record;
    }

    private static ServiceProvider Build(Capture capture, List<MetricSnapshot> metrics, LogCapture? logs = null)
    {
        var services = new ServiceCollection();
        services.AddSingleton<IOtelHelperDiagnostics>(new TestDiagnostics().Sink);
        services.AddOtelHelper(o =>
        {
            TestDiagnostics.Identified(o);
            o.ServiceName = "c14a-drivers";
            o.OtelCollectorEndpoint = "http://localhost:4317";
            o.PrometheusMetricsPort = 0;
        });
        services.AddOtelHelperSql();
        services.ConfigureOpenTelemetryTracerProvider(b => b.AddProcessor(capture));
        services.ConfigureOpenTelemetryMeterProvider(b => b.AddInMemoryExporter(metrics));
        if (logs is not null)
        {
            services.ConfigureOpenTelemetryLoggerProvider(b => b.AddProcessor(logs));
        }

        var provider = services.BuildServiceProvider();
        // As a host does: the logger factory exists before the tracer provider, so it outlives the
        // tracer's final export (the MySqlConnector records are written then).
        provider.GetRequiredService<Microsoft.Extensions.Logging.ILoggerFactory>();
        provider.GetRequiredService<TracerProvider>();
        provider.GetRequiredService<MeterProvider>();
        return provider;
    }

    /// <summary>The ADO.NET parts of a <c>scheme://user:password@host:port/database</c> URL.</summary>
    private static (string Host, int Port, string User, string Password, string Database) Url(string variable)
    {
        var value = Environment.GetEnvironmentVariable(variable);
        Assert.False(string.IsNullOrEmpty(value), $"{variable} is required (SQL_TEST_REQUIRE=1)");
        var uri = new Uri(value!);
        var userInfo = uri.UserInfo.Split(':', 2);
        return (uri.Host, uri.Port, Uri.UnescapeDataString(userInfo[0]), Uri.UnescapeDataString(userInfo[1]), uri.AbsolutePath.TrimStart('/'));
    }

    /// <summary>No metric point of <paramref name="meter"/> carries <paramref name="poolKey"/> or <paramref name="secret"/> (fix round 1: the pool name is a connection string).</summary>
    private static void AssertNoPoolName(List<MetricSnapshot> metrics, string meter, string poolKey, string secret)
    {
        var points = metrics.Where(m => m.MeterName == meter).SelectMany(m => m.MetricPoints).ToList();
        Assert.NotEmpty(points);
        foreach (var point in points)
        {
            foreach (var tag in point.Tags)
            {
                Assert.NotEqual(poolKey, tag.Key);
                Assert.DoesNotContain(secret, tag.Value?.ToString() ?? string.Empty, StringComparison.Ordinal);
                Assert.DoesNotContain(PasswordInUrl, tag.Value?.ToString() ?? string.Empty, StringComparison.Ordinal);
            }
        }
    }

    /// <summary>Every attribute value and the status description of <paramref name="spans"/>, as text.</summary>
    private static string Texts(IEnumerable<Activity> spans)
        => string.Join("\n", spans.SelectMany(s => s.TagObjects.Concat(s.Events.SelectMany(e => e.Tags)).Select(t => $"{t.Key}={t.Value}").Append($"status={s.StatusDescription}")));

    [DatabaseFact("SQL_TEST_POSTGRES")]
    public async Task Npgsql_Spans_Are_Sanitized_And_Metrics_Exported()
    {
        var (host, port, user, password, database) = Url("SQL_TEST_POSTGRES");
        var capture = new Capture();
        var metrics = new List<MetricSnapshot>();
        var logs = new LogCapture();
        using (var provider = Build(capture, metrics, logs))
        {
            // SSL Password is not used without a client certificate; Npgsql keeps it in the data source
            // name (the connection string without Password), which the sanitizer redacts.
            var cs = new NpgsqlConnectionStringBuilder
            {
                Host = host, Port = port, Username = user, Password = password, Database = database,
                SslPassword = "c14a-ssl-secret", Timeout = 10,
            };
            await using var dataSource = NpgsqlDataSource.Create(cs);
            await using (var connection = await dataSource.OpenConnectionAsync())
            {
                await using (var ok = new NpgsqlCommand("SELECT 'pg-literal-secret' AS v, 42 /* pg-comment-secret */", connection))
                {
                    Assert.Equal("pg-literal-secret", await ok.ExecuteScalarAsync());
                }

                await using var failing = new NpgsqlCommand("SELECT CAST('pgcastsecret' AS integer)", connection);
                var error = await Assert.ThrowsAsync<PostgresException>(() => failing.ExecuteScalarAsync());
                Assert.Equal("22P02", error.SqlState);
            }

            provider.GetRequiredService<MeterProvider>().ForceFlush();
        }

        var spans = capture.Ended.Where(a => a.Source.Name == "Npgsql").ToList();
        var query = Assert.Single(spans, s => (s.GetTagItem("db.query.text") as string)?.StartsWith("SELECT ? AS v", StringComparison.Ordinal) == true);
        Assert.Equal("SELECT ? AS v, ? ", query.GetTagItem("db.query.text"));
        Assert.Equal("postgresql", query.GetTagItem("db.system.name"));
        Assert.Equal("postgresql", query.GetTagItem("db.system")); // added by the helper: Npgsql 10 writes the stable keys only
        Assert.Equal(database, query.GetTagItem("db.namespace"));
        var dataSourceName = Assert.IsType<string>(query.GetTagItem("db.npgsql.data_source"));
        Assert.Contains("REDACTED", dataSourceName, StringComparison.Ordinal);

        var failed = Assert.Single(spans, s => Equals(s.GetTagItem("db.query.text"), "SELECT CAST(? AS integer)"));
        Assert.Equal(ActivityStatusCode.Error, failed.Status);
        Assert.Null(failed.StatusDescription);
        Assert.Equal("22P02", failed.GetTagItem("error.type"));
        Assert.Equal("22P02", failed.GetTagItem("db.response.status_code"));
        // ADR-014: the event keeps only the type; the server text (it quotes the literal) is in a
        // correlated log record.
        AssertCorrelatedLog(logs, failed, "pgcastsecret");

        var texts = Texts(spans);
        foreach (var secret in new[] { "pg-literal-secret", "pg-comment-secret", "pgcastsecret", "c14a-ssl-secret", PasswordInUrl })
        {
            Assert.DoesNotContain(secret, texts, StringComparison.Ordinal);
        }

        Assert.Contains(metrics, m => m.MeterName == "Npgsql" && m.Name == "db.client.operation.duration");
        AssertNoPoolName(metrics, "Npgsql", "db.client.connection.pool.name", "c14a-ssl-secret");
    }

    [DatabaseFact("SQL_TEST_MYSQL")]
    public async Task MySqlConnector_Spans_Are_Sanitized_And_Metrics_Exported()
    {
        var (host, port, user, password, database) = Url("SQL_TEST_MYSQL");
        var capture = new Capture();
        var metrics = new List<MetricSnapshot>();
        var logs = new LogCapture();
        using (var provider = Build(capture, metrics, logs))
        {
            var cs = new MySqlConnectionStringBuilder
            {
                Server = host, Port = (uint)port, UserID = user, Password = password, Database = database,
                CertificatePassword = "c14a-cert-secret", SslMode = MySqlSslMode.None, ConnectionTimeout = 10,
            };
            await using (var connection = new MySqlConnection(cs.ConnectionString))
            {
                await connection.OpenAsync();
                await using (var ok = new MySqlCommand("SELECT 'my-literal-secret', \"my-double-secret\", 42 # my-comment-secret", connection))
                {
                    Assert.Equal("my-literal-secret", await ok.ExecuteScalarAsync());
                }

                await using var failing = new MySqlCommand("SELECT * FROM missing_c14a WHERE a = 'my-where-secret'", connection);
                var error = await Assert.ThrowsAsync<MySqlException>(() => failing.ExecuteScalarAsync());
                Assert.Equal(MySqlErrorCode.NoSuchTable, error.ErrorCode);

                await using (var create = new MySqlCommand("CREATE TABLE IF NOT EXISTS c14a_dup (id VARCHAR(32) PRIMARY KEY); DELETE FROM c14a_dup", connection))
                {
                    await create.ExecuteNonQueryAsync();
                }

                for (var i = 0; i < 2; i++)
                {
                    await using var insert = new MySqlCommand("INSERT INTO c14a_dup VALUES ('mydupsecret')", connection);
                    if (i == 0)
                    {
                        await insert.ExecuteNonQueryAsync();
                    }
                    else
                    {
                        var duplicate = await Assert.ThrowsAsync<MySqlException>(() => insert.ExecuteNonQueryAsync());
                        Assert.Equal(MySqlErrorCode.DuplicateKeyEntry, duplicate.ErrorCode);
                        Assert.Contains("mydupsecret", duplicate.Message, StringComparison.Ordinal);
                    }
                }
            }

            // MySqlConnector 2.6 stops a failed command's activity before it sets the status (reader
            // Dispose, then CommandExecutor's catch): a processor after the helper's sees the
            // description (residual for application processors). The helper's OTLP exporter sanitizes
            // each batch again; it has not exported yet (batch delay 5 s), and does at shutdown below.
            var late = Assert.Single(capture.Ended, s => Equals(s.GetTagItem("db.statement"), "SELECT * FROM missing_c14a WHERE a = ?"));
            Assert.Equal("NoSuchTable", late.StatusDescription);
            Assert.Contains(late.Events, e => e.Name == "exception" && e.Tags.Any(t => t.Key == "exception.message"));

            provider.GetRequiredService<TracerProvider>().ForceFlush();
            provider.GetRequiredService<OpenTelemetry.Logs.LoggerProvider>().ForceFlush();
            provider.GetRequiredService<MeterProvider>().ForceFlush();
        }

        var spans = capture.Ended.Where(a => a.Source.Name == "MySqlConnector").ToList();
        // MySqlConnector's default without OTEL_SEMCONV_STABILITY_OPT_IN: the legacy conventions only
        // (db.system, db.statement; no db.system.name): the helper adds the new keys.
        var query = Assert.Single(spans, s => Equals(s.GetTagItem("db.statement"), "SELECT ?, ?, ? "));
        Assert.Equal("mysql", query.GetTagItem("db.system"));
        Assert.Equal("mysql", query.GetTagItem("db.system.name")); // added by the helper, with db.query.text
        Assert.Equal("SELECT ?, ?, ? ", query.GetTagItem("db.query.text"));
        Assert.Equal(database, query.GetTagItem("db.namespace")); // from db.name (round 3)
        Assert.Equal(host, query.GetTagItem("server.address")); // from net.peer.name
        Assert.Null(query.GetTagItem("db.user"));
        Assert.Null(query.GetTagItem("db.connection_string"));
        Assert.Contains(spans, s => s.DisplayName == "Open");

        var failed = Assert.Single(spans, s => Equals(s.GetTagItem("db.statement"), "SELECT * FROM missing_c14a WHERE a = ?"));
        AssertCorrelatedLog(logs, failed, "missing_c14a");
        var dup = Assert.Single(spans, s => Equals(s.GetTagItem("db.statement"), "INSERT INTO c14a_dup VALUES (?)") && s.Status == ActivityStatusCode.Error);
        Assert.Null(dup.StatusDescription);
        AssertCorrelatedLog(logs, dup, "mydupsecret");
        Assert.Equal(ActivityStatusCode.Error, failed.Status);
        Assert.Null(failed.StatusDescription);
        Assert.Equal(typeof(MySqlException).FullName, failed.GetTagItem("error.type"));

        var texts = Texts(spans);
        foreach (var secret in new[] { "my-literal-secret", "my-double-secret", "my-comment-secret", "my-where-secret", "mydupsecret", "c14a-cert-secret", PasswordInUrl })
        {
            Assert.DoesNotContain(secret, texts, StringComparison.Ordinal);
        }

        Assert.Contains(metrics, m => m.MeterName == "MySqlConnector");
        AssertNoPoolName(metrics, "MySqlConnector", "pool.name", "c14a-cert-secret");
    }
}
