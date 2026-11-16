using Npgsql;
using StaffOps.OtelLibs.Demo.Backend;

namespace StaffOps.OtelLibs.Demo.Tests;

public class SchemaTests
{
    private static Func<string, string?> Env(string? schemaFile) => name => name == "DEMO_SCHEMA_FILE" ? schemaFile : null;

    [Fact]
    public void EmbeddedSchema_IsTheSharedFile()
    {
        var schema = SchemaInitializer.EmbeddedSchema();
        Assert.StartsWith("-- Schema of the otel-libs demo", schema);
        Assert.Contains("SELECT pg_advisory_xact_lock(4242);", schema);
        Assert.Contains("CREATE OR REPLACE TRIGGER orders_dup", schema);
        Assert.Contains("$$;", schema);
    }

    [Fact]
    public void Schema_ReadsTheConfiguredFile()
    {
        var path = Path.GetTempFileName();
        try
        {
            File.WriteAllText(path, "SELECT 1;");
            Assert.Equal("SELECT 1;", SchemaInitializer.Schema(Env(path)));
        }
        finally
        {
            File.Delete(path);
        }
    }

    [Fact]
    public void Schema_MissingConfiguredFileFails()
    {
        var ex = Assert.Throws<InvalidOperationException>(() => SchemaInitializer.Schema(Env("/nonexistent/schema.sql")));
        Assert.Equal("DEMO_SCHEMA_FILE: the file does not exist", ex.Message);
    }

    [Fact]
    public void Schema_WithoutVariableUsesTheImageFileOrTheEmbeddedCopy()
    {
        var expected = File.Exists(SchemaInitializer.DefaultSchemaFile)
            ? File.ReadAllText(SchemaInitializer.DefaultSchemaFile)
            : SchemaInitializer.EmbeddedSchema();
        Assert.Equal(expected, SchemaInitializer.Schema(Env(null)));
    }

    [Fact]
    public void IsRetryable_OnlyConnectionErrorsAndCannotConnectNow()
    {
        Assert.True(SchemaInitializer.IsRetryable(new NpgsqlException("connect failed")));
        Assert.True(SchemaInitializer.IsRetryable(new TimeoutException()));
        Assert.True(SchemaInitializer.IsRetryable(new System.Net.Sockets.SocketException()));
        Assert.True(SchemaInitializer.IsRetryable(new PostgresException("starting", "FATAL", "FATAL", PostgresErrorCodes.CannotConnectNow)));
        Assert.False(SchemaInitializer.IsRetryable(new PostgresException("syntax", "ERROR", "ERROR", PostgresErrorCodes.SyntaxError)));
        Assert.False(SchemaInitializer.IsRetryable(new PostgresException("denied", "ERROR", "ERROR", PostgresErrorCodes.InsufficientPrivilege)));
        Assert.False(SchemaInitializer.IsRetryable(new InvalidOperationException()));
    }
}
