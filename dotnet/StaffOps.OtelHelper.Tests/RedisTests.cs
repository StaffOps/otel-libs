using System.Diagnostics;
using Microsoft.Extensions.DependencyInjection;
using Moq;
using OpenTelemetry;
using OpenTelemetry.Trace;
using StackExchange.Redis;
using StackExchange.Redis.Profiling;
using StaffOps.OtelHelper.Diagnostics;
using StaffOps.OtelHelper.Redis;
using StaffOps.OtelHelper.Tracing;
using Xunit;

namespace StaffOps.OtelHelper.Tests;

/// <summary>
/// Task C14b: <c>AddOtelHelperRedis</c> registration (the DI overload, C1 dev B1) and the
/// sanitizer's Redis rules (contract A26, ADR-014, design O8 (c)), on spans of a test source named
/// after the StackExchange.Redis instrumentation. The real instrumentation against a Redis server
/// runs in <see cref="RedisIntegrationTests"/>. In the <c>EnvVarTests</c> collection: no other test
/// listens to the source name while one runs.
/// </summary>
[Collection("EnvVarTests")]
[ResetGlobalPropagation]
public sealed class RedisTests
{
    private static ServiceCollection Services()
    {
        var services = new ServiceCollection();
        services.AddSingleton<IOtelHelperDiagnostics>(new TestDiagnostics().Sink);
        services.AddOtelHelper(o =>
        {
            TestDiagnostics.Identified(o);
            o.ServiceName = "c14b-redis";
            o.OtelCollectorEndpoint = "http://localhost:4317";
            o.PrometheusMetricsPort = 0;
        });
        return services;
    }

    // ── registration (C1 dev B1) ───────────────────────────────────────────

    [Fact]
    public void FromDI_Builds_The_Tracer_Provider_And_Instruments_The_Registered_Multiplexer()
    {
        var services = Services();
        var connection = new Mock<IConnectionMultiplexer>();
        services.AddSingleton(connection.Object);

        services.AddOtelHelperRedis();

        // 0.2.x threw NotSupportedException here: AddRedisInstrumentation registered services from
        // inside a deferred callback, after the container was built.
        using var provider = services.BuildServiceProvider();
        Assert.NotNull(provider.GetRequiredService<TracerProvider>());
        connection.Verify(c => c.RegisterProfiler(It.IsAny<Func<ProfilingSession?>>()), Times.Once);
    }

    [Fact]
    public void FromDI_Multiplexer_Registered_After_The_Call_Is_Instrumented()
    {
        var services = Services();
        services.AddOtelHelperRedis();
        var connection = new Mock<IConnectionMultiplexer>();
        services.AddSingleton(connection.Object);

        using var provider = services.BuildServiceProvider();
        provider.GetRequiredService<TracerProvider>();
        connection.Verify(c => c.RegisterProfiler(It.IsAny<Func<ProfilingSession?>>()), Times.Once);
    }

    [Fact]
    public void FromDI_Without_A_Multiplexer_Fails_When_The_Provider_Is_Built()
    {
        var services = Services();
        services.AddOtelHelperRedis();

        using var provider = services.BuildServiceProvider();
        var error = Assert.Throws<InvalidOperationException>(() => provider.GetRequiredService<TracerProvider>());
        Assert.Contains("AddOtelHelperRedis()", error.Message, StringComparison.Ordinal);
        Assert.Contains("IConnectionMultiplexer", error.Message, StringComparison.Ordinal);
    }

    [Fact]
    public void Explicit_Connection_Is_Instrumented_Without_A_Registration()
    {
        var services = Services();
        var connection = new Mock<IConnectionMultiplexer>();

        Assert.Same(services, services.AddOtelHelperRedis(connection.Object));

        using var provider = services.BuildServiceProvider();
        provider.GetRequiredService<TracerProvider>();
        connection.Verify(c => c.RegisterProfiler(It.IsAny<Func<ProfilingSession?>>()), Times.Once);
    }

    [Fact]
    public void Registering_The_Same_Multiplexer_Twice_Instruments_It_Twice()
    {
        // Pins today's behavior: two upstream registrations, the profiler registered twice (the
        // client keeps the last one; RedisIntegrationTests shows one span per command). Call once.
        var services = Services();
        var connection = new Mock<IConnectionMultiplexer>();
        services.AddSingleton(connection.Object);
        services.AddOtelHelperRedis();
        services.AddOtelHelperRedis(connection.Object);

        using var provider = services.BuildServiceProvider();
        provider.GetRequiredService<TracerProvider>();
        connection.Verify(c => c.RegisterProfiler(It.IsAny<Func<ProfilingSession?>>()), Times.Exactly(2));
    }

    [Fact]
    public void FromDI_Instruments_Only_The_Last_Registered_Multiplexer()
    {
        var services = Services();
        var first = new Mock<IConnectionMultiplexer>();
        var last = new Mock<IConnectionMultiplexer>();
        services.AddSingleton(first.Object);
        services.AddSingleton(last.Object);
        services.AddOtelHelperRedis();

        using var provider = services.BuildServiceProvider();
        provider.GetRequiredService<TracerProvider>();
        first.Verify(c => c.RegisterProfiler(It.IsAny<Func<ProfilingSession?>>()), Times.Never);
        last.Verify(c => c.RegisterProfiler(It.IsAny<Func<ProfilingSession?>>()), Times.Once);
    }

    [Fact]
    public void FromDI_Keyed_Only_Multiplexer_Fails_Like_A_Missing_One()
    {
        var services = Services();
        services.AddKeyedSingleton("cache", new Mock<IConnectionMultiplexer>().Object);
        services.AddOtelHelperRedis();

        using var provider = services.BuildServiceProvider();
        var error = Assert.Throws<InvalidOperationException>(() => provider.GetRequiredService<TracerProvider>());
        Assert.Contains("keyed-only", error.Message, StringComparison.Ordinal);
    }

    [Fact]
    public void FromDI_Without_A_Multiplexer_Fails_Also_With_Traces_Off()
    {
        Environment.SetEnvironmentVariable("OTEL_TRACES_EXPORTER", "none");
        try
        {
            var services = Services();
            services.AddOtelHelperRedis();
            using var provider = services.BuildServiceProvider();
            Assert.Throws<InvalidOperationException>(() => provider.GetRequiredService<TracerProvider>());
        }
        finally
        {
            Environment.SetEnvironmentVariable("OTEL_TRACES_EXPORTER", null);
        }
    }

    [Fact]
    public void Null_Arguments_Are_Refused_At_The_Call()
    {
        Assert.Throws<ArgumentNullException>(() => Services().AddOtelHelperRedis(null!));
        Assert.Throws<ArgumentNullException>(() => ((IServiceCollection)null!).AddOtelHelperRedis());
        Assert.Throws<ArgumentNullException>(() => ((IServiceCollection)null!).AddOtelHelperRedis(new Mock<IConnectionMultiplexer>().Object));
    }

    // ── sanitizer rules (A26, ADR-014, O8 (c)) ─────────────────────────────

    [Theory]
    [InlineData("SET", "SET")]
    [InlineData("CONFIG GET maxmemory", "CONFIG ?")]
    [InlineData("SET c14b:key-secret", "SET ?")]
    [InlineData("HSET c14b:hash-secret", "HSET ?")]
    [InlineData("PUBLISH c14b:channel-secret", "PUBLISH ?")]
    [InlineData("EVAL local x = 'lua-literal-secret' return x + 1", "EVAL ?")]
    [InlineData("EVAL\nreturn 'multi-line-secret'", "EVAL ?")]
    [InlineData("SET\tc14b:tab-secret", "SET ?")]
    [InlineData("SET   ", "SET")]
    [InlineData(" c14b:leading-secret", "?")]
    [InlineData("", "")]
    public void Redis_Statement_Keeps_The_Command_And_One_Placeholder(string text, string expected)
        => Assert.Equal(expected, DbSpanRules.RedisStatement(text));

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

    /// <summary>A span of the Redis source with <paramref name="tags"/>, after the processor (legacy mode as given).</summary>
    private static Activity Process(bool legacy, Action<Activity>? before, params (string Key, object? Value)[] tags)
    {
        using var source = new ActivitySource(DbSpanRules.StackExchangeRedisSource);
        using var listener = RecordAll(source);
        var activity = source.StartActivity("SET", ActivityKind.Client)!;
        foreach (var (key, value) in tags)
        {
            activity.SetTag(key, value);
        }

        before?.Invoke(activity);
        activity.Stop();
        new SanitizingProcessor(new TestDiagnostics().Sink, legacy).OnEnd(activity);
        return activity;
    }

    [Fact]
    public void Redis_Verbose_Statement_And_Query_Text_Lose_The_Key()
    {
        var span = Process(legacy: true, null, ("db.system", "redis"), ("db.statement", "SET c14b:key-secret"), ("db.query.text", "SET c14b:key-secret"));

        Assert.Equal("SET ?", span.GetTagItem("db.statement"));
        Assert.Equal("SET ?", span.GetTagItem("db.query.text"));
    }

    [Fact]
    public void Redis_Default_Statement_Is_Unchanged()
    {
        var span = Process(legacy: true, null, ("db.system", "redis"), ("db.statement", "SET"), ("db.redis.database_index", 0));

        Assert.Equal("SET", span.GetTagItem("db.statement"));
        Assert.Equal(0, span.GetTagItem("db.redis.database_index"));
    }

    [Fact]
    public void Redis_Default_Mode_Adds_The_New_Keys_Next_To_The_Legacy_Ones()
    {
        // O8 (b), reversed 2026-11-11: as Go (db.system.name, db.operation.name) and the SQL rule.
        var span = Process(legacy: true, null, ("db.system", "redis"), ("db.statement", "SET c14b:key-secret"), ("db.redis.database_index", 0));

        Assert.Equal("redis", span.GetTagItem("db.system"));
        Assert.Equal("redis", span.GetTagItem("db.system.name"));
        Assert.Equal("SET", span.GetTagItem("db.operation.name"));
        Assert.Equal("SET ?", span.GetTagItem("db.statement"));
        Assert.Equal("SET ?", span.GetTagItem("db.query.text"));
        Assert.Equal(0, span.GetTagItem("db.redis.database_index"));
        Assert.Null(span.GetTagItem("db.namespace"));
    }

    [Fact]
    public void Redis_Stable_Mode_Keeps_Only_The_New_Keys()
    {
        var span = Process(legacy: false, null, ("db.system", "redis"), ("db.statement", "HSET c14b:hash-secret"), ("db.redis.database_index", 0));

        Assert.Null(span.GetTagItem("db.redis.database_index"));
        Assert.Null(span.GetTagItem("db.system"));
        Assert.Null(span.GetTagItem("db.statement"));
        Assert.Equal("redis", span.GetTagItem("db.system.name"));
        Assert.Equal("HSET", span.GetTagItem("db.operation.name"));
        Assert.Equal("HSET ?", span.GetTagItem("db.query.text"));
    }

    [Theory]
    [InlineData(true)]
    [InlineData(false)]
    public void Redis_Keys_The_Instrumentation_Wrote_Are_Kept(bool legacy)
    {
        // database/dup or database read by the instrumentation itself: nothing is overwritten.
        var span = Process(legacy, null, ("db.system", "redis"), ("db.system.name", "redis"), ("db.statement", "GET k"),
            ("db.operation.name", "GETX"), ("db.query.text", "GET other"), ("db.namespace", "3"));

        Assert.Equal("GETX", span.GetTagItem("db.operation.name"));
        Assert.Equal("GET ?", span.GetTagItem("db.query.text"));
        Assert.Equal("3", span.GetTagItem("db.namespace"));
        Assert.Equal(legacy ? "GET ?" : null, span.GetTagItem("db.statement"));
        Assert.Equal(legacy ? "redis" : null, span.GetTagItem("db.system"));
    }

    [Fact]
    public void Redis_Operation_Falls_Back_To_The_Span_Name_But_Not_The_Unnamed_Fallback()
    {
        var named = Process(legacy: true, null, ("db.system", "redis"));
        Assert.Equal("SET", named.GetTagItem("db.operation.name"));

        using var source = new ActivitySource(DbSpanRules.StackExchangeRedisSource);
        using var listener = RecordAll(source);
        var unnamed = source.StartActivity(DbSpanRules.RedisUnnamedSpan, ActivityKind.Client)!;
        unnamed.SetTag("db.system", "redis");
        unnamed.SetTag("db.statement", " ");
        unnamed.Stop();
        new SanitizingProcessor(new TestDiagnostics().Sink, true).OnEnd(unnamed);
        Assert.Null(unnamed.GetTagItem("db.operation.name"));
        Assert.Equal("redis", unnamed.GetTagItem("db.system.name"));
    }

    [Fact]
    public void Redis_Span_Without_A_System_Key_Gets_None()
    {
        var span = Process(legacy: true, null, ("db.statement", "SET"));

        Assert.Null(span.GetTagItem("db.system.name"));
        Assert.Null(span.GetTagItem("db.system"));
    }

    [Fact]
    public void An_Unknown_Source_Never_Gets_A_System_Key()
    {
        // Exhaustive switch (code-review L3): no source falls into mysql.
        using var source = new ActivitySource("c14b-other");
        using var listener = RecordAll(source);
        var span = source.StartActivity("x")!;
        span.SetTag("db.system", "other");
        span.Stop();
        DbSpanRules.Apply(span, legacyKeys: true);
        // Also the key rule itself, for a source a future IsDbSource change could let through.
        DbSpanRules.ApplySemconvKeys(span, "c14b-other", legacyKeys: true);
        DbSpanRules.ApplySemconvKeys(span, "c14b-other", legacyKeys: false);

        Assert.Null(span.GetTagItem("db.system.name"));
        Assert.Equal("other", span.GetTagItem("db.system"));
    }

    [Fact]
    public void Redis_Failed_Status_Loses_Its_Description()
    {
        var span = Process(legacy: true, a => a.SetStatus(ActivityStatusCode.Error, "ERR unknown command 'X', with args beginning with: 'arg-secret'"), ("db.system", "redis"));

        Assert.Equal(ActivityStatusCode.Error, span.Status);
        Assert.Null(span.StatusDescription);
    }

    /// <summary>Records what the processor logs.</summary>
    private sealed class ListLogger : Microsoft.Extensions.Logging.ILogger
    {
        public List<(IReadOnlyList<KeyValuePair<string, object?>> State, string Message)> Records { get; } = new();

        public IDisposable? BeginScope<TState>(TState state) where TState : notnull => null;

        public bool IsEnabled(Microsoft.Extensions.Logging.LogLevel logLevel) => true;

        public void Log<TState>(Microsoft.Extensions.Logging.LogLevel logLevel, Microsoft.Extensions.Logging.EventId eventId, TState state, Exception? exception, Func<TState, Exception?, string> formatter)
            => Records.Add(((IReadOnlyList<KeyValuePair<string, object?>>)state!, formatter(state, exception)));
    }

    [Fact]
    public void Redis_Exception_Event_Keeps_Only_The_Type_And_The_Detail_Goes_To_A_Correlated_Log()
    {
        // Upstream adds no exception event (measured); an application Enrich callback can.
        using var source = new ActivitySource(DbSpanRules.StackExchangeRedisSource);
        using var listener = RecordAll(source);
        var logger = new ListLogger();
        var processor = new SanitizingProcessor(new TestDiagnostics().Sink) { ExceptionLogger = () => logger };
        var activity = source.StartActivity("LPUSH", ActivityKind.Client)!;
        activity.AddException(new RedisServerException("WRONGTYPE Operation against a key holding the wrong kind of value c14b:enrich-secret"));
        activity.Stop();

        processor.OnEnd(activity);

        var exception = Assert.Single(activity.Events);
        Assert.Equal(typeof(RedisServerException).FullName, exception.Tags.Single(t => t.Key == "exception.type").Value);
        Assert.DoesNotContain(exception.Tags, t => t.Key is "exception.message" or "exception.stacktrace");
        var record = Assert.Single(logger.Records);
        Assert.Contains("c14b:enrich-secret", record.Message, StringComparison.Ordinal);
        Assert.Equal(activity.SpanId.ToHexString(), record.State.Single(p => p.Key == "span_id").Value);
    }
}
