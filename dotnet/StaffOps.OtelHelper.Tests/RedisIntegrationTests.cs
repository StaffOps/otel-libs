using System.Collections.Concurrent;
using System.Diagnostics;
using Microsoft.Extensions.DependencyInjection;
using OpenTelemetry;
using OpenTelemetry.Instrumentation.StackExchangeRedis;
using OpenTelemetry.Logs;
using OpenTelemetry.Trace;
using StackExchange.Redis;
using StaffOps.OtelHelper.Diagnostics;
using StaffOps.OtelHelper.Redis;
using Xunit;

namespace StaffOps.OtelHelper.Tests;

/// <summary>
/// Skipped unless <c>REDIS_TEST_URL</c> is set (compose <c>dotnet-tests</c> sets it for
/// <c>redis-test</c>); with <c>REDIS_TEST_REQUIRE=1</c> (compose) a missing URL fails the test
/// instead. CI's <c>dotnet-test</c> job sets both against its <c>redis</c> service.
/// </summary>
public sealed class RedisFactAttribute : FactAttribute
{
    public const string Variable = "REDIS_TEST_URL";

    public RedisFactAttribute()
    {
        if (string.IsNullOrEmpty(Environment.GetEnvironmentVariable(Variable)) && Environment.GetEnvironmentVariable("REDIS_TEST_REQUIRE") != "1")
        {
            Skip = $"{Variable} is not set (compose dotnet-tests sets it)";
        }
    }
}

/// <summary>
/// Task C14b against a real server (compose <c>redis-test</c>, <c>redis:7.4.12-alpine</c>,
/// throwaway password): <c>OpenTelemetry.Instrumentation.StackExchangeRedis</c> 1.20.1-beta.1 with
/// StackExchange.Redis 2.6.122 through <c>AddOtelHelper</c> + <c>AddOtelHelperRedis()</c> (the DI
/// overload), after the sanitizer. Failing commands whose error text quotes a key, an argument or a
/// script literal (WRONGTYPE, Lua <c>error_reply</c>, a Lua runtime error, an unknown command, a
/// wrong AUTH password): no key, value, argument, literal or password reaches a span attribute, an
/// event or the status, and upstream marks none of them failed (the recorded gap).
/// </summary>
[Collection("EnvVarTests")]
[ResetGlobalPropagation]
public sealed class RedisIntegrationTests
{
    private const string Secret = "c14b-secret";

    private sealed class Capture : BaseProcessor<Activity>
    {
        public ConcurrentQueue<Activity> Ended { get; } = new();

        public override void OnEnd(Activity data) => Ended.Enqueue(data);
    }

    private sealed class LogCapture : BaseProcessor<OpenTelemetry.Logs.LogRecord>
    {
        public ConcurrentQueue<string?> Categories { get; } = new();

        public override void OnEnd(OpenTelemetry.Logs.LogRecord data) => Categories.Enqueue(data.CategoryName);
    }

    /// <summary>The connection options of <c>redis://user:password@host:port/db</c> (password never printed).</summary>
    private static (ConfigurationOptions Options, string Password) Connection()
    {
        var value = Environment.GetEnvironmentVariable(RedisFactAttribute.Variable);
        Assert.False(string.IsNullOrEmpty(value), $"{RedisFactAttribute.Variable} is required (REDIS_TEST_REQUIRE=1)");
        var uri = new Uri(value!);
        var options = new ConfigurationOptions { AbortOnConnectFail = true, ConnectTimeout = 10000, SyncTimeout = 10000, AsyncTimeout = 10000 };
        options.EndPoints.Add(uri.Host, uri.Port);
        var password = string.Empty;
        if (uri.UserInfo.Split(':', 2) is [var user, var pass])
        {
            password = Uri.UnescapeDataString(pass);
            options.Password = password;
            if (user.Length > 0 && user != "default")
            {
                options.User = Uri.UnescapeDataString(user);
            }
        }

        return (options, password);
    }

    /// <summary>The Redis spans of the commands <paramref name="run"/> issues, after the helper's processors.</summary>
    private static async Task<(List<Activity> Spans, List<string?> Categories, string Password)> Run(bool verbose, Func<IDatabase, string, Task> run, string last = "AUTH", bool twice = false)
    {
        var (options, password) = Connection();
        using var multiplexer = await ConnectionMultiplexer.ConnectAsync(options);
        var capture = new Capture();
        var logs = new LogCapture();
        var services = new ServiceCollection();
        services.AddSingleton<IOtelHelperDiagnostics>(new TestDiagnostics().Sink);
        services.AddOtelHelper(o =>
        {
            TestDiagnostics.Identified(o);
            o.ServiceName = "c14b-redis";
            o.OtelCollectorEndpoint = "http://localhost:4317";
            o.PrometheusMetricsPort = 0;
        });
        services.AddSingleton<IConnectionMultiplexer>(multiplexer);
        services.AddOtelHelperRedis();
        if (twice)
        {
            services.AddOtelHelperRedis(multiplexer);
        }
        services.Configure<StackExchangeRedisInstrumentationOptions>(o =>
        {
            o.SetVerboseDatabaseStatements = verbose;
            o.FlushInterval = TimeSpan.FromMilliseconds(100);
        });
        services.ConfigureOpenTelemetryTracerProvider(b => b.AddProcessor(capture));
        services.ConfigureOpenTelemetryLoggerProvider(b => b.AddProcessor(logs));

        var prefix = $"c14b:{Guid.NewGuid():N}:{Secret}";
        using (var provider = services.BuildServiceProvider())
        {
            provider.GetRequiredService<Microsoft.Extensions.Logging.ILoggerFactory>();
            provider.GetRequiredService<TracerProvider>();
            using var parentSource = new ActivitySource("c14b-test");
            using var listener = new ActivityListener
            {
                ShouldListenTo = s => ReferenceEquals(s, parentSource),
                Sample = (ref ActivityCreationOptions<ActivityContext> _) => ActivitySamplingResult.AllDataAndRecorded,
            };
            ActivitySource.AddActivityListener(listener);
            using (parentSource.StartActivity("commands"))
            {
                await run(multiplexer.GetDatabase(), prefix);
            }

            // The instrumentation drains profiled commands on its own thread (FlushInterval); the
            // AUTH span is the last command's.
            var deadline = DateTime.UtcNow.AddSeconds(15);
            while (DateTime.UtcNow < deadline && !capture.Ended.Any(a => a.Source.Name == Tracing.DbSpanRules.StackExchangeRedisSource && a.DisplayName == last))
            {
                await Task.Delay(200);
            }
        }

        return (capture.Ended.Where(a => a.Source.Name == Tracing.DbSpanRules.StackExchangeRedisSource).ToList(), logs.Categories.ToList(), password);
    }

    /// <summary>The failing and passing commands of the measurement (task C14b): each error text quotes data.</summary>
    private static async Task Commands(IDatabase db, string prefix)
    {
        await db.StringSetAsync(prefix + ":string", "value-" + Secret);
        await db.HashSetAsync(prefix + ":hash", "field-" + Secret, "hvalue-" + Secret);
        // WRONGTYPE.
        await Assert.ThrowsAsync<RedisServerException>(() => db.ListLeftPushAsync(prefix + ":string", "lpush-" + Secret));
        // A Lua error_reply whose first word is a custom code and whose text is ARGV[1].
        var lua = await Assert.ThrowsAsync<RedisServerException>(() => db.ScriptEvaluateAsync("return redis.error_reply('LUAERR ' .. ARGV[1])", new RedisKey[] { prefix + ":string" }, new RedisValue[] { "arg-" + Secret }));
        Assert.Contains("arg-" + Secret, lua.Message, StringComparison.Ordinal);
        // A Lua runtime error in a script holding a literal.
        await Assert.ThrowsAsync<RedisServerException>(() => db.ScriptEvaluateAsync($"local x = 'literal-{Secret}' return x + 1"));
        // An unknown command quotes its first argument.
        var unknown = await Assert.ThrowsAsync<RedisServerException>(() => db.ExecuteAsync("C14BNOSUCHCMD", "unknown-" + Secret));
        Assert.Contains("unknown-" + Secret, unknown.Message, StringComparison.Ordinal);
        // A transaction (one EXEC span).
        var tx = db.CreateTransaction();
        _ = tx.StringSetAsync(prefix + ":tx", "tx-" + Secret);
        _ = tx.StringGetAsync(prefix + ":tx");
        Assert.True(await tx.ExecuteAsync());
        await db.KeyDeleteAsync(new RedisKey[] { prefix + ":string", prefix + ":hash", prefix + ":tx" });
        // A wrong password, last: AUTH arguments must never reach a span.
        await Assert.ThrowsAsync<RedisServerException>(() => db.ExecuteAsync("AUTH", "default", "wrong-auth-" + Secret));
    }

    private static string Texts(IEnumerable<Activity> spans)
        => string.Join("\n", spans.SelectMany(s => s.TagObjects.Concat(s.Events.SelectMany(e => e.Tags)).Select(t => $"{t.Key}={t.Value}").Append($"name={s.DisplayName} status={s.StatusDescription}")));

    private static void AssertCommonShape(List<Activity> spans, List<string?> categories, string password, bool legacy = true)
    {
        var names = spans.Select(s => s.DisplayName).ToList();
        foreach (var name in new[] { "SET", "HSET", "LPUSH", "EVAL", "C14BNOSUCHCMD", "EXEC", "UNLINK", "AUTH" })
        {
            Assert.Contains(name, names);
        }

        // A transaction is one EXEC span: no MULTI span, no span per queued command.
        Assert.DoesNotContain("MULTI", names);
        Assert.Equal(2, names.Count(n => n == "EVAL"));

        foreach (var span in spans)
        {
            // Upstream builds the spans from the profiler after the fact: no status, error.type or
            // exception event on a failed command (README gap row).
            Assert.Equal(ActivityStatusCode.Unset, span.Status);
            Assert.Null(span.GetTagItem("error.type"));
            Assert.DoesNotContain(span.Events, e => e.Name == "exception");
            Assert.Equal(legacy ? "redis" : null, span.GetTagItem("db.system"));
            Assert.Equal("redis", span.GetTagItem("db.system.name"));
            Assert.Equal(span.DisplayName, span.GetTagItem("db.operation.name"));
            Assert.Equal(ActivityKind.Client, span.Kind);
        }

        var texts = Texts(spans);
        Assert.DoesNotContain(Secret, texts, StringComparison.Ordinal);
        if (password.Length > 0)
        {
            Assert.DoesNotContain(password, texts, StringComparison.Ordinal);
        }

        // No exception event, so no ADR-014 record.
        Assert.DoesNotContain("StaffOps.OtelHelper.Database", categories);
    }

    [RedisFact]
    public async Task Default_Spans_Get_The_New_Keys_Next_To_The_Legacy_Ones()
    {
        var (spans, categories, password) = await Run(verbose: false, Commands);

        // O8 (b): the helper adds db.system.name, db.operation.name and db.query.text (AssertCommonShape).
        AssertCommonShape(spans, categories, password);
        foreach (var span in spans)
        {
            Assert.Equal(span.DisplayName, span.GetTagItem("db.statement"));
            Assert.Equal(span.DisplayName, span.GetTagItem("db.query.text"));
            Assert.Null(span.GetTagItem("db.namespace"));
            Assert.NotNull(span.GetTagItem("db.redis.database_index"));
        }
    }

    [RedisFact]
    public async Task Registering_The_Same_Multiplexer_Twice_Gives_One_Span_Per_Command()
    {
        var (spans, _, _) = await Run(verbose: false, async (db, prefix) =>
        {
            await db.StringSetAsync(prefix + ":string", "v");
            await db.KeyDeleteAsync(prefix + ":string");
        }, last: "UNLINK", twice: true);

        Assert.Single(spans, s => s.DisplayName == "SET");
    }

    [RedisFact]
    public async Task Batch_Commands_Produce_No_Span()
    {
        // StackExchange.Redis 2.6.122 attaches the profiling session in
        // ConnectionMultiplexer.PrepareToPushMessageToBridge; RedisBatch.Execute enqueues on the
        // bridge directly (RedisBatch.cs TryEnqueue), so batched commands are never profiled.
        var (spans, _, _) = await Run(verbose: false, async (db, prefix) =>
        {
            var batch = db.CreateBatch();
            var set = batch.StringSetAsync(prefix + ":batch", "v");
            var get = batch.StringGetAsync(prefix + ":batch");
            batch.Execute();
            await Task.WhenAll(set, get);
            await db.KeyDeleteAsync(prefix + ":batch");
        }, last: "UNLINK");

        Assert.Equal(new[] { "UNLINK" }, spans.Select(s => s.DisplayName));
    }

    [RedisFact]
    public async Task Stable_Opt_In_Keeps_Only_The_New_Keys()
    {
        // `database` and `DATABASE` are both read by the instrumentation itself (measured); the
        // helper's conversion of legacy keys in this mode is unit-tested (RedisTests).
        foreach (var value in new[] { "database", "DATABASE" })
        {
            Environment.SetEnvironmentVariable("OTEL_SEMCONV_STABILITY_OPT_IN", value);
            try
            {
                var (spans, categories, password) = await Run(verbose: true, Commands);

                AssertCommonShape(spans, categories, password, legacy: false);
                Assert.All(spans, s => Assert.Null(s.GetTagItem("db.statement")));
                Assert.All(spans, s => Assert.Null(s.GetTagItem("db.redis.database_index")));
                Assert.Equal("SET ?", spans.First(s => s.DisplayName == "SET").GetTagItem("db.query.text"));
                Assert.Equal("EVAL ?", spans.First(s => s.DisplayName == "EVAL").GetTagItem("db.query.text"));
                // The instrumentation reads both spellings itself (measured), so it writes db.namespace.
                Assert.Equal("0", spans.First(s => s.DisplayName == "SET").GetTagItem("db.namespace"));
            }
            finally
            {
                Environment.SetEnvironmentVariable("OTEL_SEMCONV_STABILITY_OPT_IN", null);
            }
        }
    }

    [RedisFact]
    public async Task Verbose_Statements_Keep_The_Command_And_One_Placeholder()
    {
        var (spans, categories, password) = await Run(verbose: true, Commands);

        AssertCommonShape(spans, categories, password);
        // Upstream wrote "SET <key>", "EVAL <script>" (measured); the sanitizer leaves the command.
        Assert.All(spans.Where(s => s.DisplayName is "SET" or "HSET" or "LPUSH" or "EVAL"),
            s => Assert.Equal(s.DisplayName + " ?", s.GetTagItem("db.statement")));
        Assert.Equal("AUTH", spans.Single(s => s.DisplayName == "AUTH").GetTagItem("db.statement"));
        Assert.Equal("EXEC", spans.Single(s => s.DisplayName == "EXEC").GetTagItem("db.statement"));
    }

    [RedisFact]
    public async Task Database_Dup_Opt_In_Adds_The_New_Keys_From_The_Process_Environment()
    {
        // O8 (c): the HOW-TO line — the instrumentation reads the variable when its options are created.
        Environment.SetEnvironmentVariable("OTEL_SEMCONV_STABILITY_OPT_IN", "database/dup");
        try
        {
            var (spans, categories, password) = await Run(verbose: true, Commands);

            AssertCommonShape(spans, categories, password);
            var set = spans.First(s => s.DisplayName == "SET");
            Assert.Equal("redis", set.GetTagItem("db.system.name"));
            Assert.Equal("SET", set.GetTagItem("db.operation.name"));
            Assert.Equal("0", set.GetTagItem("db.namespace"));
            Assert.Equal("SET ?", set.GetTagItem("db.query.text"));
            Assert.Equal("SET ?", set.GetTagItem("db.statement"));
        }
        finally
        {
            Environment.SetEnvironmentVariable("OTEL_SEMCONV_STABILITY_OPT_IN", null);
        }
    }
}
