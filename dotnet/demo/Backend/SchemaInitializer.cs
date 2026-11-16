using Npgsql;

namespace StaffOps.OtelLibs.Demo.Backend;

/// <summary>
/// Applies demo/sql/schema.sql (embedded at build) at start-up, before the server listens, as one
/// command without bind parameters (DE-D4 r3.3: one implicit transaction, so the advisory lock
/// serializes backends starting at once; Npgsql's statement split keeps the dollar-quoted body).
/// Retries while PostgreSQL is not reachable yet; fails the start after the last attempt.
/// </summary>
public sealed class SchemaInitializer(NpgsqlDataSource dataSource, ILogger<SchemaInitializer> logger) : IHostedService
{
    /// <summary>Attempts before the start fails.</summary>
    public const int Attempts = 60;

    /// <summary>The default of <c>DEMO_SCHEMA_FILE</c>: where the image holds demo/sql/schema.sql.</summary>
    public const string DefaultSchemaFile = "/etc/otel-libs-demo/schema.sql";

    /// <summary>
    /// The schema text (DE-D7 r3.4): the file named by <c>DEMO_SCHEMA_FILE</c> (default
    /// <see cref="DefaultSchemaFile"/>, baked into the image). When the variable is unset and the
    /// default file is absent (a local run), the copy embedded at build is used; a file named
    /// explicitly that does not exist fails the start.
    /// </summary>
    public static string Schema(Func<string, string?>? env = null)
    {
        var configured = DemoSettings.Get("DEMO_SCHEMA_FILE", "", env);
        var path = configured.Length > 0 ? configured : DefaultSchemaFile;
        if (File.Exists(path))
        {
            return File.ReadAllText(path);
        }

        if (configured.Length > 0)
        {
            throw new InvalidOperationException("DEMO_SCHEMA_FILE: the file does not exist");
        }

        return EmbeddedSchema();
    }

    /// <summary>The schema text embedded in the assembly at build.</summary>
    public static string EmbeddedSchema()
    {
        using var stream = typeof(SchemaInitializer).Assembly.GetManifestResourceStream("demo-schema.sql")
            ?? throw new InvalidOperationException("embedded resource demo-schema.sql missing");
        using var reader = new StreamReader(stream);
        return reader.ReadToEnd();
    }

    /// <inheritdoc />
    [System.Diagnostics.CodeAnalysis.ExcludeFromCodeCoverage(Justification = "Needs PostgreSQL: exercised by the local smoke and the cluster run (DE6).")]
    public async Task StartAsync(CancellationToken cancellationToken)
    {
        var schema = Schema();
        for (var attempt = 1; ; attempt++)
        {
            try
            {
                await using var command = dataSource.CreateCommand(schema);
                await command.ExecuteNonQueryAsync(cancellationToken);
                DemoLog.Write(logger, LogLevel.Information, "schema applied", null, ("demo.attempt", attempt));
                return;
            }
            catch (Exception ex) when (attempt < Attempts && IsRetryable(ex))
            {
                DemoLog.Write(logger, LogLevel.Warning, "schema apply failed, retrying", ex, ("demo.attempt", attempt));
                await Task.Delay(TimeSpan.FromSeconds(2), cancellationToken);
            }
        }
    }

    /// <summary>
    /// True for failures worth another attempt (r3.5): connection errors (an <see cref="NpgsqlException"/>
    /// that is no server error, a socket error, a timeout) and the server's <c>57P03</c>
    /// (cannot_connect_now, the database is starting). A server error such as a syntax or permission
    /// error is permanent and fails the start at once.
    /// </summary>
    public static bool IsRetryable(Exception exception) => exception switch
    {
        PostgresException pg => pg.SqlState == PostgresErrorCodes.CannotConnectNow,
        NpgsqlException or TimeoutException or System.Net.Sockets.SocketException => true,
        _ => false,
    };

    /// <inheritdoc />
    public Task StopAsync(CancellationToken cancellationToken) => Task.CompletedTask;
}
