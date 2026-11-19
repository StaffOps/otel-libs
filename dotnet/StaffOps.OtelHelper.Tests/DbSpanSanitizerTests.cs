using System.Collections.Concurrent;
using System.Diagnostics;
using System.Diagnostics.Metrics;
using Microsoft.Extensions.DependencyInjection;
using Microsoft.Extensions.Options;
using OpenTelemetry;
using OpenTelemetry.Logs;
using OpenTelemetry.Metrics;
using OpenTelemetry.Trace;
using StaffOps.OtelHelper.Diagnostics;
using StaffOps.OtelHelper.Sql;
using StaffOps.OtelHelper.Tracing;
using Xunit;

namespace StaffOps.OtelHelper.Tests;

/// <summary>
/// Task C14a: the sanitizing processor and its database rules (design D-NET14, DN9; contract
/// A25, R10), and what <c>AddOtelHelperSql</c> subscribes. Spans come from test sources named
/// after the drivers, so the rules are checked without a database; the real drivers run in
/// <see cref="SqlDriverIntegrationTests"/>. In the <c>EnvVarTests</c> collection: no other
/// test listens to these source names while one runs, and the console test swaps standard output.
/// </summary>
[Collection("EnvVarTests")]
[ResetGlobalPropagation]
public sealed class DbSpanSanitizerTests : IDisposable
{
    private static readonly string[] Variables = { "OTEL_TRACES_EXPORTER", "OTEL_METRICS_EXPORTER", "OTEL_SDK_DISABLED", "OTEL_SEMCONV_STABILITY_OPT_IN" };

    public DbSpanSanitizerTests() => Clear();

    public void Dispose() => Clear();

    private static void Clear()
    {
        foreach (var variable in Variables)
        {
            Environment.SetEnvironmentVariable(variable, null);
        }
    }

    /// <summary>Spans after the helper's processors (registered after <c>AddOtelHelper</c>).</summary>
    private sealed class Capture : BaseProcessor<Activity>
    {
        public ConcurrentQueue<Activity> Ended { get; } = new();

        public override void OnEnd(Activity data) => Ended.Enqueue(data);
    }

    private static ServiceProvider Build(Capture capture, bool sql = true, TestDiagnostics? diagnostics = null, Action<IServiceCollection>? after = null)
    {
        var services = new ServiceCollection();
        services.AddSingleton<IOtelHelperDiagnostics>((diagnostics ?? new TestDiagnostics()).Sink);
        services.AddOtelHelper(o =>
        {
            TestDiagnostics.Identified(o);
            o.ServiceName = "c14a-sql";
            o.OtelCollectorEndpoint = "http://localhost:4317";
            o.PrometheusMetricsPort = 0;
        });
        if (sql)
        {
            services.AddOtelHelperSql();
        }

        after?.Invoke(services);
        services.ConfigureOpenTelemetryTracerProvider(b => b.AddProcessor(capture));
        var provider = services.BuildServiceProvider();
        provider.GetRequiredService<TracerProvider>();
        return provider;
    }

    /// <summary>One span of <paramref name="source"/>, failed with a description, and the tags given.</summary>
    private static Activity Emit(Capture capture, string source, string name, params (string Key, object? Value)[] tags)
    {
        using (var activitySource = new ActivitySource(source))
        using (var activity = activitySource.StartActivity(name, ActivityKind.Client))
        {
            Assert.NotNull(activity);
            foreach (var (key, value) in tags)
            {
                activity!.SetTag(key, value);
            }

            activity!.SetStatus(ActivityStatusCode.Error, "Duplicate entry 'secret@example.com' for key 'users.email'");
        }

        return Assert.Single(capture.Ended, a => a.DisplayName == name);
    }

    // ── status (A25) ───────────────────────────────────────────────────────

    [Theory]
    [InlineData(DbSpanRules.NpgsqlSource)]
    [InlineData(DbSpanRules.MySqlConnectorSource)]
    [InlineData(DbSpanRules.SqlClientSource)]
    public void Failed_Db_Span_Keeps_Error_Without_The_Description(string source)
    {
        var capture = new Capture();
        using var provider = Build(capture);

        var span = Emit(capture, source, "status-" + source, ("otel.status_description", "secret"));

        Assert.Equal(ActivityStatusCode.Error, span.Status);
        Assert.Null(span.StatusDescription);
        Assert.Null(span.GetTagItem("otel.status_description"));
    }

    [Fact]
    public void Other_Sources_Keep_Their_Status_And_Attributes()
    {
        var capture = new Capture();
        using var provider = Build(capture, after: s => s.ConfigureOpenTelemetryTracerProvider(b => b.AddSource("c14a-other")));

        var span = Emit(capture, "c14a-other", "other-span",
            ("db.query.text", "SELECT 'x'"), ("db.connection_string", "Server=h"), ("db.npgsql.data_source", "Password=p;Host=h"));

        Assert.Equal("Duplicate entry 'secret@example.com' for key 'users.email'", span.StatusDescription);
        Assert.Equal("SELECT 'x'", span.GetTagItem("db.query.text"));
        Assert.Equal("Server=h", span.GetTagItem("db.connection_string"));
        Assert.Equal("Password=p;Host=h", span.GetTagItem("db.npgsql.data_source"));
    }

    [Fact]
    public void Unset_Or_Ok_Status_Is_Left_Alone()
    {
        var capture = new Capture();
        using var provider = Build(capture);
        using (var source = new ActivitySource(DbSpanRules.NpgsqlSource))
        {
            using (var ok = source.StartActivity("ok-span"))
            {
                ok!.SetStatus(ActivityStatusCode.Ok, "fine");
            }

            using (source.StartActivity("unset-span"))
            {
            }
        }

        Assert.Equal(ActivityStatusCode.Ok, Assert.Single(capture.Ended, a => a.DisplayName == "ok-span").Status);
        Assert.Equal(ActivityStatusCode.Unset, Assert.Single(capture.Ended, a => a.DisplayName == "unset-span").Status);
    }

    // ── query text ─────────────────────────────────────────────────────────

    [Fact]
    public void Npgsql_Query_Text_Uses_The_PostgreSql_Rules()
    {
        var capture = new Capture();
        using var provider = Build(capture);

        var span = Emit(capture, DbSpanRules.NpgsqlSource, "pg-query",
            ("db.query.text", @"SELECT ""Email"" FROM t WHERE a = 'x' AND b = 'secret' AND c = $1"),
            ("db.statement", "SELECT $$secret$$, 42"));

        Assert.Equal(@"SELECT ""Email"" FROM t WHERE a = ? AND b = ? AND c = $1", span.GetTagItem("db.query.text"));
        Assert.Equal("SELECT ?, ?", span.GetTagItem("db.statement"));
    }

    [Fact]
    public void MySqlConnector_Query_Text_Uses_The_MySql_Rules()
    {
        var capture = new Capture();
        using var provider = Build(capture);

        var span = Emit(capture, DbSpanRules.MySqlConnectorSource, "my-query",
            ("db.statement", @"SELECT `email` FROM t WHERE a = 'x\' secret' AND b = ""secret"" # c"),
            ("db.query.text", "SELECT 7"));

        Assert.Equal("SELECT `email` FROM t WHERE a = ? AND b = ? ", span.GetTagItem("db.statement"));
        Assert.Equal("SELECT ?", span.GetTagItem("db.query.text"));
    }

    [Fact]
    public void SqlClient_Query_Text_Is_Left_To_Upstream()
    {
        var capture = new Capture();
        using var provider = Build(capture);

        var span = Emit(capture, DbSpanRules.SqlClientSource, "mssql-query", ("db.query.text", "SELECT * FROM t WHERE a = ?"), ("db.statement", "SELECT 1"));

        Assert.Equal("SELECT * FROM t WHERE a = ?", span.GetTagItem("db.query.text"));
        Assert.Equal("SELECT 1", span.GetTagItem("db.statement"));
    }

    [Fact]
    public void Non_String_Query_Text_Is_Left_Alone()
    {
        var capture = new Capture();
        using var provider = Build(capture);

        var span = Emit(capture, DbSpanRules.NpgsqlSource, "pg-number", ("db.query.text", 42));

        Assert.Equal(42, span.GetTagItem("db.query.text"));
    }

    // ── connection strings ─────────────────────────────────────────────────

    [Theory]
    [InlineData(DbSpanRules.NpgsqlSource)]
    [InlineData(DbSpanRules.MySqlConnectorSource)]
    [InlineData(DbSpanRules.SqlClientSource)]
    public void Connection_String_Attribute_Is_Removed(string source)
    {
        var capture = new Capture();
        using var provider = Build(capture);

        var span = Emit(capture, source, "cs-" + source, ("db.connection_string", "Server=db;Certificate Password=secret"), ("db.user", "otel"));

        Assert.Null(span.GetTagItem("db.connection_string"));
        Assert.Null(span.GetTagItem("db.user"));
    }

    [Theory]
    [InlineData("Host=db;Username=otel;SSL Password=secret;Database=d")]
    [InlineData("host=db;sslpassword=secret")]
    [InlineData("Host=db;PWD=secret")]
    [InlineData("Host=db;Key_Store_Password=secret")]
    public void Npgsql_Data_Source_Password_Keys_Are_Redacted(string value)
    {
        var capture = new Capture();
        using var provider = Build(capture);

        var span = Emit(capture, DbSpanRules.NpgsqlSource, "ds-" + value, ("db.npgsql.data_source", value));

        var text = Assert.IsType<string>(span.GetTagItem("db.npgsql.data_source"));
        Assert.DoesNotContain("secret", text, StringComparison.Ordinal);
        Assert.Contains(DbSpanRules.Redacted, text, StringComparison.Ordinal);
        Assert.Contains("db", text, StringComparison.Ordinal);
    }

    [Theory]
    [InlineData("orders-primary")]
    [InlineData("Host=db;Username=otel;Database=d")]
    public void Npgsql_Data_Source_Without_Password_Is_Unchanged(string value)
    {
        var capture = new Capture();
        using var provider = Build(capture);

        var span = Emit(capture, DbSpanRules.NpgsqlSource, "ds-keep-" + value, ("db.npgsql.data_source", value));

        Assert.Equal(value, span.GetTagItem("db.npgsql.data_source"));
    }

    [Fact]
    public void Npgsql_Data_Source_That_Does_Not_Parse_Is_Removed()
    {
        var capture = new Capture();
        using var provider = Build(capture);

        var span = Emit(capture, DbSpanRules.NpgsqlSource, "ds-bad", ("db.npgsql.data_source", "Host='db;Password=secret"));

        Assert.Null(span.GetTagItem("db.npgsql.data_source"));
    }

    [Fact]
    public void Data_Source_Rule_Is_Npgsql_Only()
    {
        var capture = new Capture();
        using var provider = Build(capture);

        var span = Emit(capture, DbSpanRules.MySqlConnectorSource, "ds-mysql", ("db.npgsql.data_source", "Password=p"));

        Assert.Equal("Password=p", span.GetTagItem("db.npgsql.data_source"));
    }

    // ── the processor ──────────────────────────────────────────────────────

    [Fact]
    public void Spans_Without_Recorded_Data_Are_Not_Touched()
    {
        using var source = new ActivitySource(DbSpanRules.NpgsqlSource);
        using var listener = new ActivityListener
        {
            ShouldListenTo = s => ReferenceEquals(s, source),
            Sample = (ref ActivityCreationOptions<ActivityContext> _) => ActivitySamplingResult.PropagationData,
        };
        ActivitySource.AddActivityListener(listener);
        using var activity = source.StartActivity("propagation-only")!;
        Assert.False(activity.IsAllDataRequested);
        activity.SetStatus(ActivityStatusCode.Error, "text");
        activity.SetTag("db.query.text", "SELECT 'x'");

        new SanitizingProcessor(new TestDiagnostics().Sink).OnEnd(activity);

        Assert.Equal("text", activity.StatusDescription);
    }

    [Fact]
    public void A_Failing_Rule_Strips_The_Sensitive_Attributes_And_Warns_Once()
    {
        var diagnostics = new TestDiagnostics();
        var processor = new SanitizingProcessor(diagnostics.Sink) { Rules = _ => throw new InvalidOperationException("boom 'secret'") };
        using var source = new ActivitySource("c14a-failing");
        using var listener = new ActivityListener
        {
            ShouldListenTo = s => ReferenceEquals(s, source),
            Sample = (ref ActivityCreationOptions<ActivityContext> _) => ActivitySamplingResult.AllDataAndRecorded,
        };
        ActivitySource.AddActivityListener(listener);

        for (var i = 0; i < 2; i++)
        {
            using var activity = source.StartActivity("failing")!;
            activity.SetStatus(ActivityStatusCode.Error, "secret text");
            foreach (var key in new[] { "otel.status_description", "db.connection_string", "db.query.text", "db.statement", "db.npgsql.data_source" })
            {
                activity.SetTag(key, "secret");
            }

            activity.SetTag("db.system.name", "postgresql");
            processor.OnEnd(activity);

            Assert.Equal(ActivityStatusCode.Error, activity.Status);
            Assert.Null(activity.StatusDescription);
            Assert.DoesNotContain(activity.TagObjects, t => Equals(t.Value, "secret"));
            Assert.Equal("postgresql", activity.GetTagItem("db.system.name"));
        }

        var line = Assert.Single(diagnostics.Lines);
        Assert.StartsWith("otelhelper: WARN span sanitizer failed", line);
        Assert.Contains("source=c14a-failing", line);
        Assert.Contains("error=InvalidOperationException", line);
        Assert.DoesNotContain("secret", line);
    }

    [Fact]
    public void A_Failing_Rule_Leaves_An_Unfailed_Status_Alone()
    {
        var processor = new SanitizingProcessor(new TestDiagnostics().Sink) { Rules = _ => throw new InvalidOperationException() };
        using var source = new ActivitySource("c14a-failing-ok");
        using var listener = new ActivityListener
        {
            ShouldListenTo = s => ReferenceEquals(s, source),
            Sample = (ref ActivityCreationOptions<ActivityContext> _) => ActivitySamplingResult.AllDataAndRecorded,
        };
        ActivitySource.AddActivityListener(listener);
        using var activity = source.StartActivity("ok")!;
        activity.SetStatus(ActivityStatusCode.Ok, "fine");

        processor.OnEnd(activity);

        Assert.Equal(ActivityStatusCode.Ok, activity.Status);
    }

    [Fact]
    public void The_Helper_Console_Exporter_Sees_The_Sanitized_Span()
    {
        // SimpleActivityExportProcessor exports inside OnEnd: only a sanitizer added before it
        // changes what is written (a batch exporter reads the span later and would hide the order).
        Environment.SetEnvironmentVariable("OTEL_TRACES_EXPORTER", "console");
        var original = Console.Out;
        var captured = new StringWriter();
        Console.SetOut(TextWriter.Synchronized(captured));
        try
        {
            var services = new ServiceCollection();
            services.AddSingleton<IOtelHelperDiagnostics>(new TestDiagnostics().Sink);
            services.AddOtelHelper(o => { TestDiagnostics.Identified(o); o.ServiceName = "c14a-console"; o.PrometheusMetricsPort = 0; });
            services.AddOtelHelperSql();
            using (var provider = services.BuildServiceProvider())
            {
                provider.GetRequiredService<TracerProvider>();
                using var source = new ActivitySource(DbSpanRules.NpgsqlSource);
                using (var activity = source.StartActivity("c14a-console-span"))
                {
                    activity!.SetTag("db.query.text", "SELECT 'console-secret'");
                    activity.SetStatus(ActivityStatusCode.Error, "console-description");
                }
            }
        }
        finally
        {
            Console.SetOut(original);
        }

        var text = captured.ToString();
        Assert.Contains("c14a-console-span", text);
        Assert.Contains("SELECT ?", text);
        Assert.DoesNotContain("console-secret", text);
        Assert.DoesNotContain("console-description", text);
    }

    /// <summary>Stopped spans of <paramref name="source"/> changed after they ended, as MySqlConnector does for a failed command.</summary>
    private static Activity[] LateChangedSpans(ActivitySource source, int count)
    {
        var spans = new Activity[count];
        for (var i = 0; i < count; i++)
        {
            var activity = source.StartActivity("late-" + i)!;
            activity.Stop();
            activity.SetStatus(ActivityStatusCode.Error, "late-description");
            activity.SetTag("db.statement", "SELECT 'late-secret'");
            spans[i] = activity;
        }

        return spans;
    }

    private static ActivityListener RecordAll(ActivitySource source)
    {
        var listener = new ActivityListener
        {
            ShouldListenTo = s => ReferenceEquals(s, source),
            Sample = (ref ActivityCreationOptions<ActivityContext> _) => ActivitySamplingResult.AllDataAndRecorded,
        };
        ActivitySource.AddActivityListener(listener);
        return listener;
    }

    [Fact]
    public void Sanitize_Reads_The_Batch_Once_And_Returns_Every_Span_Sanitized()
    {
        using var source = new ActivitySource(DbSpanRules.MySqlConnectorSource);
        using var listener = RecordAll(source);
        var spans = LateChangedSpans(source, 3);

        var batch = new SanitizingProcessor(new TestDiagnostics().Sink).Sanitize(new Batch<Activity>(spans, 3));

        var exported = new List<Activity>();
        foreach (var activity in batch)
        {
            exported.Add(activity);
        }

        Assert.Equal(spans, exported);
        Assert.All(spans, s =>
        {
            Assert.Null(s.StatusDescription);
            Assert.Equal("SELECT ?", s.GetTagItem("db.statement"));
        });
    }

    [Fact]
    public void Sanitized_Exporter_Behind_A_Batch_Processor_Exports_Every_Span()
    {
        // A batch processor's batch drains its queue when read: Sanitize must hand on what it read.
        var original = Console.Out;
        var captured = new StringWriter();
        Console.SetOut(TextWriter.Synchronized(captured));
        try
        {
            using var source = new ActivitySource("c14a-batch");
            using (var provider = Sdk.CreateTracerProviderBuilder()
                .AddSource("c14a-batch")
                .AddProcessor(new BatchActivityExportProcessor(new SanitizingConsoleActivityExporter(
                    new OpenTelemetry.Exporter.ConsoleExporterOptions(), new SanitizingProcessor(new TestDiagnostics().Sink))))
                .Build())
            {
                using (source.StartActivity("c14a-batch-span-1"))
                {
                }

                using (source.StartActivity("c14a-batch-span-2"))
                {
                }

                provider!.ForceFlush();
            }
        }
        finally
        {
            Console.SetOut(original);
        }

        var text = captured.ToString();
        Assert.Contains("c14a-batch-span-1", text);
        Assert.Contains("c14a-batch-span-2", text);
    }

    [Fact]
    public void Helper_Otlp_Exporter_Sanitizes_A_Span_Changed_After_It_Ended()
    {
        using var source = new ActivitySource(DbSpanRules.MySqlConnectorSource);
        using var listener = RecordAll(source);
        var spans = LateChangedSpans(source, 1);
        var options = new OpenTelemetry.Exporter.OtlpExporterOptions
        {
            Endpoint = new Uri($"http://127.0.0.1:{TestPorts.FreeTcpPort()}"),
            Protocol = OpenTelemetry.Exporter.OtlpExportProtocol.HttpProtobuf,
            TimeoutMilliseconds = 1000,
        };
        using var exporter = new SanitizingOtlpTraceExporter(options, new SanitizingProcessor(new TestDiagnostics().Sink));

        exporter.Export(new Batch<Activity>(spans, 1));

        Assert.Null(spans[0].StatusDescription);
        Assert.Equal("SELECT ?", spans[0].GetTagItem("db.statement"));
    }

    [Fact]
    public void Helper_Console_Exporter_Sanitizes_A_Span_Changed_After_It_Ended()
    {
        using var source = new ActivitySource(DbSpanRules.MySqlConnectorSource);
        using var listener = RecordAll(source);
        var spans = LateChangedSpans(source, 1);
        var original = Console.Out;
        var captured = new StringWriter();
        Console.SetOut(TextWriter.Synchronized(captured));
        try
        {
            using var exporter = new SanitizingConsoleActivityExporter(new OpenTelemetry.Exporter.ConsoleExporterOptions(), new SanitizingProcessor(new TestDiagnostics().Sink));
            exporter.Export(new Batch<Activity>(spans, 1));
        }
        finally
        {
            Console.SetOut(original);
        }

        var text = captured.ToString();
        Assert.Contains("late-0", text);
        Assert.DoesNotContain("late-description", text);
        Assert.DoesNotContain("late-secret", text);
    }

    [Fact]
    public void Sanitizer_Runs_When_The_Helper_Exports_No_Traces()
    {
        Environment.SetEnvironmentVariable("OTEL_TRACES_EXPORTER", "none");
        var capture = new Capture();
        using var provider = Build(capture, after: s => s.ConfigureOpenTelemetryTracerProvider(b => b.AddSource(DbSpanRules.NpgsqlSource)));

        var span = Emit(capture, DbSpanRules.NpgsqlSource, "off-span", ("db.query.text", "SELECT 'x'"));

        Assert.Equal("SELECT ?", span.GetTagItem("db.query.text"));
        Assert.Null(span.StatusDescription);
    }

    [Fact]
    public void Rules_Apply_To_A_Driver_Source_The_Application_Subscribed_Without_The_Sql_Package()
    {
        var capture = new Capture();
        using var provider = Build(capture, sql: false, after: s => s.ConfigureOpenTelemetryTracerProvider(b => b.AddSource(DbSpanRules.MySqlConnectorSource)));

        var span = Emit(capture, DbSpanRules.MySqlConnectorSource, "app-subscribed", ("db.statement", "SELECT 'x'"));

        Assert.Equal("SELECT ?", span.GetTagItem("db.statement"));
    }

    // ── what AddOtelHelperSql subscribes ───────────────────────────────────

    [Fact]
    public void Sql_Package_Subscribes_The_Driver_Sources()
    {
        var capture = new Capture();
        using (var provider = Build(capture, sql: false))
        using (var source = new ActivitySource(DbSpanRules.NpgsqlSource))
        {
            Assert.Null(source.StartActivity("not-subscribed"));
        }

        using (var provider = Build(capture))
        {
            foreach (var name in new[] { DbSpanRules.NpgsqlSource, DbSpanRules.MySqlConnectorSource })
            {
                using var source = new ActivitySource(name);
                using var activity = source.StartActivity("subscribed");
                Assert.NotNull(activity);
            }

            var resolved = provider.GetRequiredService<ResolvedTelemetry>();
            Assert.Equal(new[] { "Npgsql", "MySqlConnector" }, resolved.AdditionalActivitySources);
            Assert.Equal(new[] { "Npgsql", "MySqlConnector" }, resolved.AdditionalMeters);
        }
    }

    [Fact]
    public void Sql_Package_Keeps_Its_Names_Over_A_Later_Configure_And_Adds_No_Duplicate()
    {
        var capture = new Capture();
        using var provider = Build(capture, after: s => s.Configure<TelemetryOptions>(o =>
        {
            o.AdditionalActivitySources = new List<string> { "app.source", "Npgsql" };
            o.AdditionalMeters = null!;
        }));

        var resolved = provider.GetRequiredService<ResolvedTelemetry>();
        Assert.Equal(new[] { "app.source", "Npgsql", "MySqlConnector" }, resolved.AdditionalActivitySources);
        Assert.Equal(new[] { "Npgsql", "MySqlConnector" }, resolved.AdditionalMeters);
    }

    [Fact]
    public void Driver_Sources_Are_Not_Subscribed_While_The_Helper_Exports_No_Traces()
    {
        Environment.SetEnvironmentVariable("OTEL_TRACES_EXPORTER", "none");
        var capture = new Capture();
        using var provider = Build(capture);
        using var source = new ActivitySource(DbSpanRules.NpgsqlSource);

        Assert.Null(source.StartActivity("off"));
    }

    [Fact]
    public void Sql_Package_Exports_The_Driver_Meters_And_The_SqlClient_Meter()
    {
        var exported = new List<Metric>();
        var services = new ServiceCollection();
        services.AddSingleton<IOtelHelperDiagnostics>(new TestDiagnostics().Sink);
        services.AddOtelHelper(o => { TestDiagnostics.Identified(o); o.ServiceName = "c14a-meters"; o.PrometheusMetricsPort = 0; });
        services.AddOtelHelperSql();
        services.ConfigureOpenTelemetryMeterProvider(b => b.AddInMemoryExporter(exported));
        using var provider = services.BuildServiceProvider();
        var meterProvider = provider.GetRequiredService<MeterProvider>();
        using var npgsql = new Meter(DbSpanRules.NpgsqlSource);
        using var mysql = new Meter(DbSpanRules.MySqlConnectorSource);
        using var sqlClient = new Meter(DbSpanRules.SqlClientSource);
        npgsql.CreateCounter<long>("c14a.npgsql.count").Add(1);
        mysql.CreateCounter<long>("c14a.mysql.count").Add(1);
        sqlClient.CreateCounter<long>("c14a.sqlclient.count").Add(1);

        meterProvider.ForceFlush();

        Assert.Contains(exported, m => m.Name == "c14a.npgsql.count");
        Assert.Contains(exported, m => m.Name == "c14a.mysql.count");
        Assert.Contains(exported, m => m.Name == "c14a.sqlclient.count");
    }

    [Fact]
    public void Driver_Meters_Are_Dropped_While_The_Helper_Exports_No_Metrics()
    {
        Environment.SetEnvironmentVariable("OTEL_METRICS_EXPORTER", "none");
        var exported = new List<Metric>();
        var services = new ServiceCollection();
        services.AddSingleton<IOtelHelperDiagnostics>(new TestDiagnostics().Sink);
        services.AddOtelHelper(o => { TestDiagnostics.Identified(o); o.ServiceName = "c14a-meters-off"; o.PrometheusMetricsPort = 0; });
        services.AddOtelHelperSql();
        services.ConfigureOpenTelemetryMeterProvider(b => b.AddMeter(DbSpanRules.NpgsqlSource).AddInMemoryExporter(exported));
        using var provider = services.BuildServiceProvider();
        var meterProvider = provider.GetRequiredService<MeterProvider>();
        using var npgsql = new Meter(DbSpanRules.NpgsqlSource);
        npgsql.CreateCounter<long>("c14a.npgsql.off").Add(1);

        meterProvider.ForceFlush();

        Assert.DoesNotContain(exported, m => m.Name == "c14a.npgsql.off");
    }
    // ── semantic-convention keys (R10, fix round 1) ────────────────────────

    [Theory]
    [InlineData(null, true)]
    [InlineData("", true)]
    [InlineData("http", true)]
    [InlineData("database", false)]
    [InlineData(" DATABASE ", false)]
    [InlineData("http,\tDataBase\n", false)]
    [InlineData("database/dup", true)]
    [InlineData("database,database/dup", true)]
    [InlineData("Database/DUP", true)]
    [InlineData(" database", true)]
    [InlineData("databases", true)]
    public void Legacy_Keys_Follow_The_Opt_In_As_Go(string? optIn, bool legacy)
        => Assert.Equal(legacy, DbSpanRules.LegacyKeysEnabled(optIn));

    /// <summary>One span of <paramref name="source"/> with <paramref name="tags"/>, through a processor in <paramref name="legacy"/> mode.</summary>
    private static Activity Process(string source, bool legacy, params (string Key, object? Value)[] tags)
    {
        using var activitySource = new ActivitySource(source);
        using var listener = RecordAll(activitySource);
        var activity = activitySource.StartActivity("semconv")!;
        foreach (var (key, value) in tags)
        {
            activity.SetTag(key, value);
        }

        activity.Stop();
        new SanitizingProcessor(new TestDiagnostics().Sink, legacy).OnEnd(activity);
        return activity;
    }

    [Theory]
    [InlineData(DbSpanRules.SqlClientSource, "microsoft.sql_server", "mssql")]
    [InlineData(DbSpanRules.NpgsqlSource, "postgresql", "postgresql")]
    public void Stable_Only_Drivers_Get_The_Legacy_System_By_Default(string source, string name, string legacy)
    {
        var span = Process(source, legacy: true, ("db.system.name", name));
        Assert.Equal(name, span.GetTagItem("db.system.name"));
        Assert.Equal(legacy, span.GetTagItem("db.system"));

        var stable = Process(source, legacy: false, ("db.system.name", name));
        Assert.Equal(name, stable.GetTagItem("db.system.name"));
        Assert.Null(stable.GetTagItem("db.system"));
    }

    [Fact]
    public void MySqlConnector_Legacy_Span_Gets_The_New_Keys_And_Keeps_The_Legacy_By_Default()
    {
        var span = Process(DbSpanRules.MySqlConnectorSource, legacy: true, ("db.system", "mysql"), ("db.statement", "SELECT 'x' FROM t"));

        Assert.Equal("mysql", span.GetTagItem("db.system"));
        Assert.Equal("mysql", span.GetTagItem("db.system.name"));
        Assert.Equal("SELECT ? FROM t", span.GetTagItem("db.statement"));
        Assert.Equal("SELECT ? FROM t", span.GetTagItem("db.query.text"));
    }

    [Fact]
    public void MySqlConnector_Legacy_Span_Keeps_Only_The_New_Keys_In_The_Stable_Mode()
    {
        var span = Process(DbSpanRules.MySqlConnectorSource, legacy: false, ("db.system", "mysql"), ("db.statement", "SELECT 'x' FROM t"));

        Assert.Null(span.GetTagItem("db.system"));
        Assert.Null(span.GetTagItem("db.statement"));
        Assert.Equal("mysql", span.GetTagItem("db.system.name"));
        Assert.Equal("SELECT ? FROM t", span.GetTagItem("db.query.text"));
    }

    [Fact]
    public void Existing_Query_Text_Is_Not_Replaced_By_The_Statement()
    {
        var span = Process(DbSpanRules.MySqlConnectorSource, legacy: true, ("db.statement", "SELECT 1"), ("db.query.text", "SELECT 2"));

        Assert.Equal("SELECT ?", span.GetTagItem("db.query.text"));
        Assert.Equal("SELECT ?", span.GetTagItem("db.statement"));
    }

    [Fact]
    public void Spans_Without_A_System_Key_Get_None()
    {
        var span = Process(DbSpanRules.NpgsqlSource, legacy: true, ("db.query.text", "SELECT 1"));

        Assert.Null(span.GetTagItem("db.system"));
        Assert.Null(span.GetTagItem("db.system.name"));
    }

    [Fact]
    public void Processor_Reads_The_Opt_In_When_The_Provider_Is_Built()
    {
        Environment.SetEnvironmentVariable("OTEL_SEMCONV_STABILITY_OPT_IN", "database");
        var capture = new Capture();
        using var provider = Build(capture);

        var span = Emit(capture, DbSpanRules.NpgsqlSource, "opt-in-span", ("db.system.name", "postgresql"));

        Assert.Null(span.GetTagItem("db.system"));
        Assert.False(new SanitizingProcessor(new TestDiagnostics().Sink).LegacyDbKeys);
    }

    [Fact]
    public void Default_Pipeline_Adds_The_Legacy_System()
    {
        var capture = new Capture();
        using var provider = Build(capture);

        var span = Emit(capture, DbSpanRules.SqlClientSource, "default-span", ("db.system.name", "microsoft.sql_server"));

        Assert.Equal("mssql", span.GetTagItem("db.system"));
    }

    [Theory]
    [InlineData(DbSpanRules.SqlClientSource)]
    [InlineData(DbSpanRules.NpgsqlSource)]
    public void Query_Parameters_Are_Removed(string source)
    {
        var span = Process(source, legacy: true, ("db.query.parameter.email", "secret@example.com"), ("db.query.parameter.0", 42), ("db.query.summary", "SELECT t"));

        Assert.DoesNotContain(span.TagObjects, t => t.Key.StartsWith("db.query.parameter.", StringComparison.Ordinal));
        Assert.Equal("SELECT t", span.GetTagItem("db.query.summary"));
    }

    [Fact]
    public void SqlClient_Exception_Events_Can_Be_Turned_Off_After_AddOtelHelperSql()
    {
        var services = new ServiceCollection();
        services.AddSingleton<IOtelHelperDiagnostics>(new TestDiagnostics().Sink);
        services.AddOtelHelper(o => { TestDiagnostics.Identified(o); o.PrometheusMetricsPort = 0; });
        services.AddOtelHelperSql();
        services.Configure<OpenTelemetry.Instrumentation.SqlClient.SqlClientTraceInstrumentationOptions>(o => o.RecordException = false);
        using var provider = services.BuildServiceProvider();

        var options = provider.GetRequiredService<IOptionsMonitor<OpenTelemetry.Instrumentation.SqlClient.SqlClientTraceInstrumentationOptions>>().Get(Options.DefaultName);

        Assert.False(options.RecordException);
    }

    // ── driver meters: the pool name is dropped (fix round 1) ──────────────

    private static List<MetricSnapshot> ExportDriverMetrics(Action<TelemetryOptions>? configure = null, bool sql = true, Action<IServiceCollection>? after = null)
    {
        var exported = new List<MetricSnapshot>();
        var services = new ServiceCollection();
        services.AddSingleton<IOtelHelperDiagnostics>(new TestDiagnostics().Sink);
        services.AddOtelHelper(o => { TestDiagnostics.Identified(o); o.ServiceName = "c14a-pool"; o.PrometheusMetricsPort = 0; configure?.Invoke(o); });
        if (sql)
        {
            services.AddOtelHelperSql();
        }

        after?.Invoke(services);
        services.ConfigureOpenTelemetryMeterProvider(b => b.AddInMemoryExporter(exported));
        using var provider = services.BuildServiceProvider();
        var meterProvider = provider.GetRequiredService<MeterProvider>();
        using (var npgsql = new Meter(DbSpanRules.NpgsqlSource))
        using (var mysql = new Meter(DbSpanRules.MySqlConnectorSource))
        using (var other = new Meter("c14a-pool"))
        {
            npgsql.CreateCounter<long>("c14a.pool.npgsql").Add(1,
                new("db.client.connection.pool.name", "Host=db;SSL Password=pool-secret"), new("db.client.connection.state", "idle"), new("server.port", 5432));
            npgsql.CreateHistogram<double>("c14a.pool.npgsql.duration").Record(0.5,
                new("db.client.connection.pool.name", "Host=db;SSL Password=pool-secret"), new("db.system.name", "postgresql"));
            mysql.CreateCounter<long>("c14a.pool.mysql").Add(1, new("pool.name", "Server=db;Certificate Password=pool-secret"), new("state", "used"));
            other.CreateCounter<long>("c14a.pool.other").Add(1, new KeyValuePair<string, object?>("pool.name", "kept"));
            meterProvider.ForceFlush();
        }

        return exported;
    }

    private static List<KeyValuePair<string, object?>> Tags(MetricSnapshot metric)
    {
        var tags = new List<KeyValuePair<string, object?>>();
        foreach (var point in metric.MetricPoints)
        {
            foreach (var tag in point.Tags)
            {
                tags.Add(tag);
            }
        }

        return tags;
    }

    [Fact]
    public void Driver_Meters_Lose_The_Pool_Name_And_Keep_The_Other_Keys()
    {
        var exported = ExportDriverMetrics();

        var npgsql = Tags(exported.First(m => m.Name == "c14a.pool.npgsql"));
        Assert.DoesNotContain(npgsql, t => t.Key == "db.client.connection.pool.name");
        Assert.Contains(npgsql, t => t.Key == "db.client.connection.state" && Equals(t.Value, "idle"));
        Assert.Contains(npgsql, t => t.Key == "server.port");
        var histogram = Tags(exported.First(m => m.Name == "c14a.pool.npgsql.duration"));
        Assert.Equal(new[] { "db.system.name" }, histogram.Select(t => t.Key));
        var mysql = Tags(exported.First(m => m.Name == "c14a.pool.mysql"));
        Assert.Equal(new[] { "state" }, mysql.Select(t => t.Key));
        Assert.Contains(Tags(exported.First(m => m.Name == "c14a.pool.other")), t => t.Key == "pool.name");
        Assert.DoesNotContain(exported.SelectMany(Tags), t => t.Value is string s && s.Contains("pool-secret", StringComparison.Ordinal));
    }

    [Fact]
    public void Driver_Meters_Subscribed_By_The_Application_Lose_The_Pool_Name_Too()
    {
        var exported = ExportDriverMetrics(sql: false, after: s => s.ConfigureOpenTelemetryMeterProvider(b => b.AddMeter(DbSpanRules.NpgsqlSource)));

        var npgsql = Tags(exported.First(m => m.Name == "c14a.pool.npgsql"));
        Assert.DoesNotContain(npgsql, t => t.Key == "db.client.connection.pool.name");
    }

    [Fact]
    public void Disabled_Metric_Patterns_Still_Drop_A_Driver_Instrument()
    {
        var exported = ExportDriverMetrics(o => o.DisabledMetrics = "c14a.pool.npgsql");

        Assert.DoesNotContain(exported, m => m.Name == "c14a.pool.npgsql");
        Assert.Contains(exported, m => m.Name == "c14a.pool.npgsql.duration");
    }
    // ── ADR-014: exception detail to a correlated log (round 2) ────────────

    /// <summary>Records what the processor logs.</summary>
    private sealed class ListLogger : Microsoft.Extensions.Logging.ILogger
    {
        public List<(Microsoft.Extensions.Logging.LogLevel Level, IReadOnlyList<KeyValuePair<string, object?>> State, string Message)> Records { get; } = new();

        public bool Enabled { get; set; } = true;

        public IDisposable? BeginScope<TState>(TState state) where TState : notnull => null;

        public bool IsEnabled(Microsoft.Extensions.Logging.LogLevel logLevel) => Enabled;

        public void Log<TState>(Microsoft.Extensions.Logging.LogLevel logLevel, Microsoft.Extensions.Logging.EventId eventId, TState state, Exception? exception, Func<TState, Exception?, string> formatter)
        {
            lock (Records)
            {
                Records.Add((logLevel, (IReadOnlyList<KeyValuePair<string, object?>>)state!, formatter(state, exception)));
            }
        }
    }

    private static object? Value(IEnumerable<KeyValuePair<string, object?>> pairs, string key) => pairs.FirstOrDefault(p => p.Key == key).Value;

    private static Activity FailedWithException(ActivitySource source, string message)
    {
        var activity = source.StartActivity("failed-db")!;
        activity.AddEvent(new ActivityEvent("before"));
        try
        {
            throw new InvalidOperationException(message);
        }
        catch (InvalidOperationException e)
        {
            activity.AddException(e, new TagList { { "exception.escaped", true } });
        }

        activity.SetStatus(ActivityStatusCode.Error, message);
        return activity;
    }

    [Fact]
    public void Activity_Still_Has_The_Private_Event_List_The_Rewrite_Uses()
        => Assert.NotNull(DbExceptionEvents.ActivityEventsField);

    [Theory]
    [InlineData(DbSpanRules.NpgsqlSource)]
    [InlineData(DbSpanRules.SqlClientSource)]
    [InlineData(DbSpanRules.MySqlConnectorSource)]
    public void Exception_Event_Keeps_Only_The_Type_And_The_Detail_Goes_To_One_Correlated_Log(string sourceName)
    {
        using var source = new ActivitySource(sourceName);
        using var listener = RecordAll(source);
        var logger = new ListLogger();
        var processor = new SanitizingProcessor(new TestDiagnostics().Sink) { ExceptionLogger = () => logger };
        var activity = FailedWithException(source, "Duplicate entry 'mydupsecret' for key 'PRIMARY'");
        activity.Stop();

        processor.OnEnd(activity);
        processor.Sanitize(new Batch<Activity>(new[] { activity }, 1));

        var events = activity.Events.ToList();
        Assert.Equal(new[] { "before", "exception" }, events.Select(e => e.Name));
        var exception = events[1];
        Assert.Equal(typeof(InvalidOperationException).FullName, Value(exception.Tags, "exception.type"));
        Assert.Equal(true, Value(exception.Tags, "exception.escaped"));
        Assert.DoesNotContain(exception.Tags, t => t.Key is "exception.message" or "exception.stacktrace");
        Assert.DoesNotContain(activity.Events.SelectMany(e => e.Tags), t => t.Value is string s && s.Contains("mydupsecret", StringComparison.Ordinal));

        var record = Assert.Single(logger.Records);
        Assert.Equal(Microsoft.Extensions.Logging.LogLevel.Error, record.Level);
        Assert.Equal("Duplicate entry 'mydupsecret' for key 'PRIMARY'", record.Message);
        Assert.Equal(typeof(InvalidOperationException).FullName, Value(record.State, "exception.type"));
        Assert.Equal("Duplicate entry 'mydupsecret' for key 'PRIMARY'", Value(record.State, "exception.message"));
        Assert.Contains("FailedWithException", (string)Value(record.State, "exception.stacktrace")!, StringComparison.Ordinal);
        Assert.Equal(activity.TraceId.ToHexString(), Value(record.State, "trace_id"));
        Assert.Equal(activity.SpanId.ToHexString(), Value(record.State, "span_id"));
    }

    [Fact]
    public void An_Exception_Event_Added_After_The_Span_Ended_Is_Handled_By_The_Helper_Exporter()
    {
        using var source = new ActivitySource(DbSpanRules.MySqlConnectorSource);
        using var listener = RecordAll(source);
        var logger = new ListLogger();
        var processor = new SanitizingProcessor(new TestDiagnostics().Sink) { ExceptionLogger = () => logger };
        var activity = source.StartActivity("late")!;
        activity.Stop();
        processor.OnEnd(activity);
        activity.AddEvent(new ActivityEvent("exception", tags: new ActivityTagsCollection
        {
            { "exception.type", "MySqlConnector.MySqlException" },
            { "exception.message", "Duplicate entry 'mydupsecret'" },
            { "exception.stacktrace", "MySqlConnector.MySqlException: Duplicate entry 'mydupsecret' at ..." },
        }));
        Assert.Empty(logger.Records);
        var options = new OpenTelemetry.Exporter.OtlpExporterOptions
        {
            Endpoint = new Uri($"http://127.0.0.1:{TestPorts.FreeTcpPort()}"),
            Protocol = OpenTelemetry.Exporter.OtlpExportProtocol.HttpProtobuf,
            TimeoutMilliseconds = 1000,
        };
        using var exporter = new SanitizingOtlpTraceExporter(options, processor);

        exporter.Export(new Batch<Activity>(new[] { activity }, 1));

        Assert.DoesNotContain(activity.Events.Single().Tags, t => t.Key is "exception.message" or "exception.stacktrace");
        Assert.Equal("Duplicate entry 'mydupsecret'", Assert.Single(logger.Records).Message);
        Assert.Equal(activity.SpanId.ToHexString(), Value(logger.Records[0].State, "span_id"));
    }

    [Fact]
    public void Other_Sources_Keep_Their_Exception_Events()
    {
        using var source = new ActivitySource("c14a-http");
        using var listener = RecordAll(source);
        var logger = new ListLogger();
        var activity = FailedWithException(source, "kept");
        activity.Stop();

        new SanitizingProcessor(new TestDiagnostics().Sink) { ExceptionLogger = () => logger }.OnEnd(activity);

        Assert.Equal("kept", Value(activity.Events.Last().Tags, "exception.message"));
        Assert.Empty(logger.Records);
    }

    [Fact]
    public void Without_The_Private_Event_List_Nothing_Changes_And_One_Warning_Is_Written()
    {
        using var source = new ActivitySource(DbSpanRules.NpgsqlSource);
        using var listener = RecordAll(source);
        var diagnostics = new TestDiagnostics();
        var logger = new ListLogger();
        var processor = new SanitizingProcessor(diagnostics.Sink) { ExceptionLogger = () => logger, EventsField = null };
        for (var i = 0; i < 2; i++)
        {
            var activity = FailedWithException(source, "pgcastsecret");
            activity.Stop();
            processor.OnEnd(activity);
            Assert.Equal("pgcastsecret", Value(activity.Events.Last().Tags, "exception.message"));
        }

        Assert.Empty(logger.Records);
        var line = Assert.Single(diagnostics.Lines);
        Assert.StartsWith("otelhelper: WARN cannot rewrite span events", line);
        Assert.DoesNotContain("pgcastsecret", line);
    }

    [Fact]
    public void A_Failing_Rule_Still_Removes_The_Exception_Detail()
    {
        using var source = new ActivitySource(DbSpanRules.NpgsqlSource);
        using var listener = RecordAll(source);
        var processor = new SanitizingProcessor(new TestDiagnostics().Sink) { Rules = _ => throw new InvalidOperationException() };
        var activity = FailedWithException(source, "pgcastsecret");
        activity.Stop();

        processor.OnEnd(activity);

        Assert.DoesNotContain(activity.Events.SelectMany(e => e.Tags), t => t.Key is "exception.message" or "exception.stacktrace");
    }

    [Fact]
    public void No_Record_When_The_Logger_Is_Off_Or_Absent_And_The_Detail_Still_Leaves_The_Span()
    {
        using var source = new ActivitySource(DbSpanRules.NpgsqlSource);
        using var listener = RecordAll(source);
        var off = new ListLogger { Enabled = false };
        foreach (var processor in new[]
        {
            new SanitizingProcessor(new TestDiagnostics().Sink) { ExceptionLogger = () => off },
            new SanitizingProcessor(new TestDiagnostics().Sink),
        })
        {
            var activity = FailedWithException(source, "pgcastsecret");
            activity.Stop();
            processor.OnEnd(activity);
            Assert.DoesNotContain(activity.Events.SelectMany(e => e.Tags), t => t.Key == "exception.message");
        }

        Assert.Empty(off.Records);
    }

    /// <summary>Log records after the helper's processors (registered after <c>AddOtelHelper</c>).</summary>
    private sealed class LogCapture : BaseProcessor<OpenTelemetry.Logs.LogRecord>
    {
        public List<(string? Category, ActivityTraceId TraceId, ActivitySpanId SpanId, string? Body, List<KeyValuePair<string, object?>> Attributes)> Records { get; } = new();

        public override void OnEnd(OpenTelemetry.Logs.LogRecord data)
        {
            lock (Records)
            {
                Records.Add((data.CategoryName, data.TraceId, data.SpanId, data.FormattedMessage ?? data.Body, data.Attributes?.ToList() ?? new()));
            }
        }
    }

    [Fact]
    public void Otlp_Log_Record_Carries_The_Span_Context_From_Any_Thread()
    {
        var capture = new Capture();
        var logs = new LogCapture();
        using var provider = Build(capture, after: s => s.ConfigureOpenTelemetryLoggerProvider(b => b.AddProcessor(logs)));
        provider.GetRequiredService<Microsoft.Extensions.Logging.ILoggerFactory>();
        using var source = new ActivitySource(DbSpanRules.NpgsqlSource);
        var activity = FailedWithException(source, "pgcastsecret");
        activity.Stop();

        var record = Assert.Single(logs.Records, r => r.Category == Logging.DbExceptionLog.Category);
        Assert.Equal(activity.TraceId, record.TraceId);
        Assert.Equal(activity.SpanId, record.SpanId);
        Assert.Equal("pgcastsecret", record.Body);
        Assert.Equal("pgcastsecret", Value(record.Attributes, "exception.message"));
        Assert.NotNull(Value(record.Attributes, "exception.stacktrace"));
        Assert.DoesNotContain(record.Attributes, a => a.Key is "trace_id" or "span_id");
    }

    [Fact]
    public void Correlation_Processor_Leaves_Other_Records_Alone()
    {
        var logs = new LogCapture();
        var services = new ServiceCollection();
        services.AddSingleton<IOtelHelperDiagnostics>(new TestDiagnostics().Sink);
        services.AddOtelHelper(o => { TestDiagnostics.Identified(o); o.OtelCollectorEndpoint = "http://localhost:4317"; o.PrometheusMetricsPort = 0; });
        services.ConfigureOpenTelemetryLoggerProvider(b => b.AddProcessor(logs));
        using var provider = services.BuildServiceProvider();
        var factory = provider.GetRequiredService<Microsoft.Extensions.Logging.ILoggerFactory>();
        Microsoft.Extensions.Logging.LoggerExtensions.Log(factory.CreateLogger("app.orders"), Microsoft.Extensions.Logging.LogLevel.Error, "x {trace_id} {span_id}", "0123456789abcdef0123456789abcdef", "0123456789abcdef");
        factory.CreateLogger(Logging.DbExceptionLog.Category).Log(Microsoft.Extensions.Logging.LogLevel.Error, default, new List<KeyValuePair<string, object?>> { new("trace_id", "short"), new("span_id", "0123456789abcdef") }, null, (_, _) => "bad ids");

        var app = Assert.Single(logs.Records, r => r.Category == "app.orders");
        Assert.Equal(default, app.TraceId);
        Assert.Contains(app.Attributes, a => a.Key == "trace_id");
        var bad = Assert.Single(logs.Records, r => r.Body == "bad ids");
        Assert.Equal(default, bad.TraceId);
        Assert.Contains(bad.Attributes, a => a.Key == "trace_id");
    }

    [Fact]
    public void Stdout_Fallback_Line_Has_The_Detail_And_The_Ids()
    {
        Environment.SetEnvironmentVariable("OTEL_TRACES_EXPORTER", "console");
        var original = Console.Out;
        var captured = new StringWriter();
        Console.SetOut(TextWriter.Synchronized(captured));
        Activity activity;
        try
        {
            var services = new ServiceCollection();
            services.AddSingleton<IOtelHelperDiagnostics>(new TestDiagnostics().Sink);
            services.AddOtelHelper(o => { TestDiagnostics.Identified(o); o.ServiceName = "c14a-stdout"; o.PrometheusMetricsPort = 0; });
            services.AddOtelHelperSql();
            using (var provider = services.BuildServiceProvider())
            {
                provider.GetRequiredService<Microsoft.Extensions.Logging.ILoggerFactory>();
                provider.GetRequiredService<TracerProvider>();
                using var source = new ActivitySource(DbSpanRules.NpgsqlSource);
                activity = FailedWithException(source, "pgcastsecret");
                activity.Stop();
            }
        }
        finally
        {
            Console.SetOut(original);
        }

        var line = Assert.Single(captured.ToString().Split('\n'), l => l.Contains(Logging.DbExceptionLog.Category, StringComparison.Ordinal));
        Assert.Contains("\"level\":\"ERROR\"", line);
        Assert.Contains("\"msg\":\"pgcastsecret\"", line);
        Assert.Contains($"\"trace_id\":\"{activity.TraceId.ToHexString()}\"", line);
        Assert.Contains($"\"span_id\":\"{activity.SpanId.ToHexString()}\"", line);
        Assert.Contains("\"exception.stacktrace\":", line);
    }
    [Fact]
    public async Task On_The_Wire_The_Span_Has_No_Detail_And_The_Log_Has_The_Span_Ids()
    {
        var port = TestPorts.FreeTcpPort();
        var bodies = new ConcurrentDictionary<string, List<byte[]>>();
        using var listener = new System.Net.HttpListener();
        listener.Prefixes.Add($"http://localhost:{port}/");
        listener.Start();
        var serve = Task.Run(async () =>
        {
            while (listener.IsListening)
            {
                System.Net.HttpListenerContext ctx;
                try
                {
                    ctx = await listener.GetContextAsync();
                }
                catch (Exception ex) when (ex is System.Net.HttpListenerException or ObjectDisposedException or InvalidOperationException)
                {
                    return;
                }

                using var body = new MemoryStream();
                var input = ctx.Request.Headers["Content-Encoding"] == "gzip"
                    ? new System.IO.Compression.GZipStream(ctx.Request.InputStream, System.IO.Compression.CompressionMode.Decompress)
                    : ctx.Request.InputStream;
                await input.CopyToAsync(body);
                var list = bodies.GetOrAdd(ctx.Request.Url!.AbsolutePath, _ => new List<byte[]>());
                lock (list)
                {
                    list.Add(body.ToArray());
                }

                ctx.Response.StatusCode = 200;
                ctx.Response.Close();
            }
        });

        Activity activity;
        var services = new ServiceCollection();
        services.AddSingleton<IOtelHelperDiagnostics>(new TestDiagnostics().Sink);
        services.AddOtelHelper(o =>
        {
            TestDiagnostics.Identified(o);
            o.ServiceName = "c14a-wire";
            o.OtelCollectorEndpoint = $"http://localhost:{port}";
            o.OtlpProtocol = TelemetryOptions.ProtocolHttpProtobuf;
            o.PrometheusMetricsPort = 0;
        });
        services.AddOtelHelperSql();
        using (var provider = services.BuildServiceProvider())
        {
            provider.GetRequiredService<Microsoft.Extensions.Logging.ILoggerFactory>();
            var tracer = provider.GetRequiredService<TracerProvider>();
            using (var source = new ActivitySource(DbSpanRules.NpgsqlSource))
            {
                activity = FailedWithException(source, "pgcastsecret");
                activity.Stop();
            }

            tracer.ForceFlush();
            provider.GetRequiredService<LoggerProvider>().ForceFlush();
        }

        listener.Stop();
        await Task.WhenAny(serve, Task.Delay(TimeSpan.FromSeconds(5)));

        static bool Has(byte[] haystack, byte[] needle) => haystack.AsSpan().IndexOf(needle) >= 0;
        var traceBytes = Convert.FromHexString(activity.TraceId.ToHexString());
        var spanBytes = Convert.FromHexString(activity.SpanId.ToHexString());
        var secret = System.Text.Encoding.UTF8.GetBytes("pgcastsecret");
        var traces = Assert.Single(bodies["/v1/traces"], b => Has(b, spanBytes));
        Assert.True(Has(traces, System.Text.Encoding.UTF8.GetBytes("exception.type")));
        Assert.False(Has(traces, secret));
        Assert.False(Has(traces, System.Text.Encoding.UTF8.GetBytes("exception.stacktrace")));
        var log = Assert.Single(bodies["/v1/logs"], b => Has(b, secret));
        Assert.True(Has(log, traceBytes));
        Assert.True(Has(log, spanBytes));
        Assert.True(Has(log, System.Text.Encoding.UTF8.GetBytes("exception.stacktrace")));
    }
    // ── round 3 ────────────────────────────────────────────────────────────

    private static Activity LateEventSpan(ActivitySource source, DateTimeOffset? at = null)
    {
        var activity = source.StartActivity("late-race")!;
        activity.AddEvent(new ActivityEvent("before"));
        activity.Stop();
        activity.AddEvent(new ActivityEvent("exception", at ?? default, new ActivityTagsCollection
        {
            { "exception.type", "MySqlConnector.MySqlException" },
            { "exception.message", "Duplicate entry 'mydupsecret'" },
            { "exception.stacktrace", "MySqlConnector.MySqlException: Duplicate entry 'mydupsecret'" },
        }));
        return activity;
    }

    /// <summary>A logger whose first record blocks until <see cref="Release"/> is set.</summary>
    private sealed class BlockingLogger : Microsoft.Extensions.Logging.ILogger
    {
        private int _calls;

        public ManualResetEventSlim Entered { get; } = new();

        public ManualResetEventSlim Release { get; } = new();

        public List<string> Messages { get; } = new();

        public IDisposable? BeginScope<TState>(TState state) where TState : notnull => null;

        public bool IsEnabled(Microsoft.Extensions.Logging.LogLevel logLevel) => true;

        public void Log<TState>(Microsoft.Extensions.Logging.LogLevel logLevel, Microsoft.Extensions.Logging.EventId eventId, TState state, Exception? exception, Func<TState, Exception?, string> formatter)
        {
            lock (Messages)
            {
                Messages.Add(formatter(state, exception));
            }

            if (Interlocked.Increment(ref _calls) == 1)
            {
                Entered.Set();
                Release.Wait(TimeSpan.FromSeconds(30));
            }
        }
    }

    [Fact]
    public void A_Blocked_Log_Sink_Stalls_No_Other_Span_And_A_Concurrent_Pass_Logs_Nothing()
    {
        using var source = new ActivitySource(DbSpanRules.MySqlConnectorSource);
        using var listener = RecordAll(source);
        var logger = new BlockingLogger();
        var processor = new SanitizingProcessor(new TestDiagnostics().Sink) { ExceptionLogger = () => logger };
        var late = LateEventSpan(source);

        // First pass: blocks inside Handle, in the logger.
        var first = new Thread(() => processor.Sanitize(new Batch<Activity>(new[] { late }, 1)));
        first.Start();
        Assert.True(logger.Entered.Wait(TimeSpan.FromSeconds(10)));
        try
        {
            // While it is blocked: a second pass over the same span and the end of another failed DB
            // span both complete (the old code held the lock across the logger call).
            var second = new Thread(() => processor.Sanitize(new Batch<Activity>(new[] { late }, 1))) { IsBackground = true };
            var other = new Thread(() =>
            {
                var activity = FailedWithException(source, "other-failure");
                activity.Stop();
                processor.OnEnd(activity);
            }) { IsBackground = true };
            second.Start();
            other.Start();
            Assert.True(second.Join(TimeSpan.FromSeconds(10)) && other.Join(TimeSpan.FromSeconds(10)), "a span end waited for a blocked log sink");
        }
        finally
        {
            logger.Release.Set();
            first.Join();
        }

        Assert.Equal(1, logger.Messages.Count(m => m == "Duplicate entry 'mydupsecret'"));
        Assert.Contains("other-failure", logger.Messages);
        Assert.Equal(new[] { "before", "exception" }, late.Events.Select(e => e.Name));
        Assert.DoesNotContain(late.Events.SelectMany(e => e.Tags), t => t.Key == "exception.message");
    }


    [Theory]
    [InlineData("0123456789abcdef0123456789abcdef", 32, true)]
    [InlineData("0123456789ABCDEF0123456789ABCDEF", 32, false)]
    [InlineData("zz23456789abcdef0123456789abcdef", 32, false)]
    [InlineData("00000000000000000000000000000000", 32, false)]
    [InlineData("0123456789abcdef", 32, false)]
    [InlineData("0123456789abcdef", 16, true)]
    [InlineData(null, 16, false)]
    public void Ids_Are_Lower_Case_Non_Zero_Hex(string? value, int length, bool valid)
        => Assert.Equal(valid, Logging.DbExceptionLog.IsHexId(value, length));

    [Fact]
    public void Invalid_Ids_Leave_The_Record_Unchanged_And_Do_Not_Throw()
    {
        var logs = new LogCapture();
        var services = new ServiceCollection();
        services.AddSingleton<IOtelHelperDiagnostics>(new TestDiagnostics().Sink);
        services.AddOtelHelper(o => { TestDiagnostics.Identified(o); o.OtelCollectorEndpoint = "http://localhost:4317"; o.PrometheusMetricsPort = 0; });
        services.ConfigureOpenTelemetryLoggerProvider(b => b.AddProcessor(logs));
        using var provider = services.BuildServiceProvider();
        var logger = provider.GetRequiredService<Microsoft.Extensions.Logging.ILoggerFactory>().CreateLogger(Logging.DbExceptionLog.Category);
        foreach (var trace in new[] { "zz23456789abcdef0123456789abcdef", "00000000000000000000000000000000" })
        {
            logger.Log(Microsoft.Extensions.Logging.LogLevel.Error, default,
                new List<KeyValuePair<string, object?>> { new("trace_id", trace), new("span_id", "0123456789abcdef") }, null, (_, _) => "invalid " + trace);
        }

        Assert.Equal(2, logs.Records.Count(r => r.Body!.StartsWith("invalid ", StringComparison.Ordinal)));
        Assert.All(logs.Records.Where(r => r.Body!.StartsWith("invalid ", StringComparison.Ordinal)), r =>
        {
            Assert.Equal(default, r.TraceId);
            Assert.Contains(r.Attributes, a => a.Key == "trace_id");
        });
    }

    [Fact]
    public void Otlp_Record_Timestamp_Is_The_Exception_Event_Time()
    {
        var capture = new Capture();
        var timestamps = new List<DateTime>();
        using var provider = Build(capture, after: s => s.ConfigureOpenTelemetryLoggerProvider(b => b.AddProcessor(new TimestampCapture(timestamps))));
        using var source = new ActivitySource(DbSpanRules.NpgsqlSource);
        var at = new DateTimeOffset(2020, 1, 2, 3, 4, 5, 678, TimeSpan.Zero);
        var activity = source.StartActivity("timed")!;
        activity.AddException(new InvalidOperationException("pgcastsecret"), default, at);
        activity.Stop();

        Assert.Equal(at.UtcDateTime, Assert.Single(timestamps));
    }

    private sealed class TimestampCapture : BaseProcessor<OpenTelemetry.Logs.LogRecord>
    {
        private readonly List<DateTime> _into;

        public TimestampCapture(List<DateTime> into) => _into = into;

        public override void OnEnd(OpenTelemetry.Logs.LogRecord data)
        {
            if (data.CategoryName == Logging.DbExceptionLog.Category)
            {
                _into.Add(data.Timestamp);
            }
        }
    }

    [Fact]
    public void Late_Event_Record_Reaches_The_Log_Pipeline_Through_The_Helper_Exporter()
    {
        // Pins "not under SuppressInstrumentation": the OpenTelemetry logger drops records written
        // inside a suppression scope, so a record written inside OtlpTraceExporter.Export would be lost.
        var capture = new Capture();
        var logs = new LogCapture();
        using var provider = Build(capture, after: s => s.ConfigureOpenTelemetryLoggerProvider(b => b.AddProcessor(logs)));
        provider.GetRequiredService<Microsoft.Extensions.Logging.ILoggerFactory>();
        using var source = new ActivitySource(DbSpanRules.MySqlConnectorSource);
        var activity = LateEventSpan(source);

        provider.GetRequiredService<TracerProvider>().ForceFlush();

        var record = Assert.Single(logs.Records, r => r.Category == Logging.DbExceptionLog.Category);
        Assert.Equal(activity.SpanId, record.SpanId);
        Assert.DoesNotContain(activity.Events.SelectMany(e => e.Tags), t => t.Key == "exception.message");
    }

    [Fact]
    public void Stdout_Line_Has_The_Current_Span_Ids_After_Logger_And_No_Duplicate()
    {
        using var span = new Activity("stdout-span").Start();
        var time = DateTimeOffset.UnixEpoch;
        var state = new List<KeyValuePair<string, object?>> { new("trace_id", "app-value"), new("order", "o1") };

        var context = Logging.JsonLine.SpanContext("app.orders", state, ref time);
        var line = Logging.JsonLine.Format(time, Microsoft.Extensions.Logging.LogLevel.Warning, "app.orders", state, null, (_, _) => "m", true, context);

        Assert.Contains($"\"logger\":\"app.orders\",\"trace_id\":\"{span.TraceId.ToHexString()}\",\"span_id\":\"{span.SpanId.ToHexString()}\",\"order\":\"o1\"}}", line);
        Assert.DoesNotContain("app-value", line);
    }

    [Fact]
    public void Stdout_Line_Without_A_Span_Has_No_Ids_And_Keeps_An_App_Trace_Id()
    {
        Activity.Current = null;
        var time = DateTimeOffset.UnixEpoch;
        var state = new List<KeyValuePair<string, object?>> { new("trace_id", "app-value") };

        var context = Logging.JsonLine.SpanContext("app.orders", state, ref time);
        var line = Logging.JsonLine.Format(time, Microsoft.Extensions.Logging.LogLevel.Warning, "app.orders", state, null, (_, _) => "m", true, context);

        Assert.Null(context);
        Assert.Contains("\"logger\":\"app.orders\",\"trace_id\":\"app-value\"}", line);
    }

    [Fact]
    public void Stdout_Database_Record_Uses_Its_Span_And_Event_Time_Not_The_Current_Span()
    {
        using var current = new Activity("other-current").Start();
        var time = DateTimeOffset.UnixEpoch.AddDays(1);
        var state = new List<KeyValuePair<string, object?>>
        {
            new("exception.message", "m"),
            new("trace_id", "0123456789abcdef0123456789abcdef"),
            new("span_id", "0123456789abcdef"),
            new(Logging.DbExceptionLog.EventTimeKey, "2020-01-02T03:04:05.6780000Z"),
        };

        var context = Logging.JsonLine.SpanContext(Logging.DbExceptionLog.Category, state, ref time);
        var line = Logging.JsonLine.Format(time, Microsoft.Extensions.Logging.LogLevel.Error, Logging.DbExceptionLog.Category, state, null, (_, _) => "m", true, context);

        Assert.StartsWith("{\"time\":\"2020-01-02T03:04:05.678Z\"", line);
        Assert.Contains($"\"logger\":\"{Logging.DbExceptionLog.Category}\",\"trace_id\":\"0123456789abcdef0123456789abcdef\",\"span_id\":\"0123456789abcdef\",\"exception.message\":\"m\"}}", line);
        Assert.DoesNotContain(current.SpanId.ToHexString(), line);
        Assert.DoesNotContain(Logging.DbExceptionLog.EventTimeKey, line);
    }

    [Fact]
    public void MySqlConnector_Legacy_Name_And_Peer_Keys_Get_The_Current_Names()
    {
        var tags = new (string, object?)[] { ("db.name", "otel_test"), ("net.peer.name", "db.example.com"), ("net.peer.port", 3306), ("db.user", "otel") };

        var legacy = Process(DbSpanRules.MySqlConnectorSource, legacy: true, tags);
        Assert.Equal("otel_test", legacy.GetTagItem("db.namespace"));
        Assert.Equal("db.example.com", legacy.GetTagItem("server.address"));
        Assert.Equal(3306, legacy.GetTagItem("server.port"));
        Assert.Equal("otel_test", legacy.GetTagItem("db.name"));
        Assert.Null(legacy.GetTagItem("db.user"));

        var stable = Process(DbSpanRules.MySqlConnectorSource, legacy: false, tags);
        Assert.Equal("otel_test", stable.GetTagItem("db.namespace"));
        Assert.Null(stable.GetTagItem("db.name"));
        Assert.Null(stable.GetTagItem("net.peer.name"));
        Assert.Null(stable.GetTagItem("net.peer.port"));

        var present = Process(DbSpanRules.MySqlConnectorSource, legacy: true, ("db.name", "old"), ("db.namespace", "new"));
        Assert.Equal("new", present.GetTagItem("db.namespace"));
    }

    [Fact]
    public void Exception_Logger_Is_Null_When_The_Container_Is_Disposed()
    {
        var services = new ServiceCollection();
        services.AddLogging();
        var provider = services.BuildServiceProvider();
        Assert.NotNull(TracerSetup.ExceptionLogger(provider));
        provider.Dispose();

        Assert.Null(TracerSetup.ExceptionLogger(provider));
    }
}
