using System.Collections.Concurrent;
using System.Text.Json;
using Microsoft.Extensions.Configuration;
using Microsoft.Extensions.DependencyInjection;
using Microsoft.Extensions.Hosting;
using Microsoft.Extensions.Logging;
using Microsoft.Extensions.Options;
using OpenTelemetry.Logs;
using OpenTelemetry.Resources;
using OpenTelemetry.Trace;
using StaffOps.OtelHelper.Diagnostics;
using StaffOps.OtelHelper.Logging;
using Xunit;

namespace StaffOps.OtelHelper.Tests;

/// <summary>
/// Task C13 (design D-NET10, requirement DN8; contract D6, R4, A23): the JSON-line stdout fallback
/// (line shape, UTC time, level words, reserved keys, values), when it writes (no logs exporter)
/// and when the OpenTelemetry provider does, the R4 level, and the N9 rule that the helper's
/// level and framework filters apply to its own two providers only. Parity cases: <c>logging.*</c>.
/// </summary>
[Collection("EnvVarTests")]
[ResetGlobalPropagation]
public sealed class StdoutLoggingTests : IDisposable
{
    private static readonly string[] Variables =
    {
        "OTEL_SERVICE_NAME", "ENVIRONMENT", "OTEL_EXPORTER_OTLP_ENDPOINT", "OTEL_EXPORTER_OTLP_LOGS_ENDPOINT",
        "OTEL_LOGS_EXPORTER", "OTEL_HELPER_DISABLED_SIGNALS", "OTEL_SDK_DISABLED", "OTEL_HELPER_DEBUG_LEVEL",
        "OTEL_RESOURCE_ATTRIBUTES",
    };

    private static readonly DateTimeOffset Instant = new(2026, 1, 2, 3, 4, 5, 678, TimeSpan.Zero);

    public StdoutLoggingTests() => Clear();

    public void Dispose() => Clear();

    private static void Clear()
    {
        foreach (var variable in Variables)
        {
            Environment.SetEnvironmentVariable(variable, null);
        }
    }

    private static void Set(string variable, string? value) => Environment.SetEnvironmentVariable(variable, value);

    private static string Line(LogLevel level, IEnumerable<KeyValuePair<string, object?>>? state = null, Exception? exception = null, string category = "cat", string message = "m")
        => JsonLine.Format(Instant, level, category, (state ?? Array.Empty<KeyValuePair<string, object?>>()).ToList(), exception, (_, _) => message);

    private static KeyValuePair<string, object?> Kv(string key, object? value) => new(key, value);

    // ── Line shape (A23) ───────────────────────────────────────────────────

    [Fact]
    public void Line_Has_The_Contract_Fields_In_Order_Compact_And_One_Line()
    {
        Assert.Equal("{\"time\":\"2026-01-02T03:04:05.678Z\",\"level\":\"WARN\",\"msg\":\"m\",\"logger\":\"cat\"}\n", Line(LogLevel.Warning));
    }

    [Fact]
    public void Time_Is_Utc_With_Z_And_Truncated_Milliseconds_Whatever_The_Offset()
    {
        var local = new DateTimeOffset(2026, 1, 2, 0, 4, 5, TimeSpan.FromHours(-3)).AddTicks(6789 * 1000);
        Assert.Equal("2026-01-02T03:04:05.678Z", JsonLine.Time(local));
    }

    [Theory]
    [InlineData(LogLevel.Trace, "DEBUG")]
    [InlineData(LogLevel.Debug, "DEBUG")]
    [InlineData(LogLevel.Information, "INFO")]
    [InlineData(LogLevel.Warning, "WARN")]
    [InlineData(LogLevel.Error, "ERROR")]
    [InlineData(LogLevel.Critical, "ERROR")]
    public void Level_Uses_The_Four_Contract_Words(LogLevel level, string word)
    {
        Assert.Equal(word, JsonLine.LevelWord(level));
        Assert.Contains($"\"level\":\"{word}\"", Line(level));
    }

    [Fact]
    public void Reserved_Keys_Are_Dropped_And_Other_Attributes_Follow_Logger()
    {
        var line = Line(LogLevel.Warning, new[] { Kv("time", "x"), Kv("level", "x"), Kv("msg", "x"), Kv("logger", "x"), Kv("a", "kept") });
        Assert.Equal("{\"time\":\"2026-01-02T03:04:05.678Z\",\"level\":\"WARN\",\"msg\":\"m\",\"logger\":\"cat\",\"a\":\"kept\"}\n", line);
    }

    [Fact]
    public void Reserved_Keys_Are_Case_Sensitive()
    {
        Assert.Contains("\"Time\":\"kept\"", Line(LogLevel.Information, new[] { Kv("Time", "kept") }));
    }

    [Fact]
    public void Template_Values_Become_Attributes_Without_The_Original_Format_And_Duplicates_Keep_The_First()
    {
        var writer = new StringWriter();
        var provider = new JsonLineLoggerProvider(() => true) { Output = () => writer, Clock = () => Instant };
        provider.CreateLogger("cat").LogWarning("order {OrderId} by {Customer} and {OrderId}", 42, "ana", 43);
        Assert.Equal("{\"time\":\"2026-01-02T03:04:05.678Z\",\"level\":\"WARN\",\"msg\":\"order 42 by ana and 43\",\"logger\":\"cat\",\"OrderId\":42,\"Customer\":\"ana\"}\n", writer.ToString());
    }

    [Fact]
    public void Exception_Is_The_Last_Field_And_Replaces_An_Attribute_Of_That_Name()
    {
        var error = new InvalidOperationException("boom");
        var line = Line(LogLevel.Error, new[] { Kv("exception", "attr"), Kv("a", 1) }, error);
        using var doc = JsonDocument.Parse(line);
        var props = doc.RootElement.EnumerateObject().Select(p => p.Name).ToArray();
        Assert.Equal(new[] { "time", "level", "msg", "logger", "a", "exception" }, props);
        Assert.Equal(error.ToString(), doc.RootElement.GetProperty("exception").GetString());
        Assert.EndsWith("}\n", line);
        Assert.Single(line.Split('\n', StringSplitOptions.RemoveEmptyEntries));
    }

    [Fact]
    public void An_Exception_Attribute_Is_Kept_When_The_Record_Has_None()
    {
        Assert.EndsWith(",\"exception\":\"attr\"}\n", Line(LogLevel.Error, new[] { Kv("exception", "attr") }));
    }

    [Fact]
    public void Values_Keep_Their_Json_Type_And_The_Rest_Becomes_Text()
    {
        var line = Line(LogLevel.Information, new[]
        {
            Kv("s", "t"), Kv("b", true), Kv("i", 7), Kv("by", (byte)2), Kv("u", 3u), Kv("l", -9L), Kv("ul", 10UL), Kv("m", 1.5m),
            Kv("d", 0.25), Kv("f", 0.5f), Kv("n", null), Kv("nan", double.NaN), Kv("inf", double.PositiveInfinity),
            Kv("ninf", float.NegativeInfinity), Kv("dt", new DateTime(2026, 1, 2, 3, 4, 5, DateTimeKind.Utc)),
            Kv("dto", Instant), Kv("e", DayOfWeek.Friday), Kv("g", Guid.Empty), Kv("arr", new object?[] { 1, "x", new[] { 2 }, null }),
            Kv("dict", new Dictionary<string, int> { ["k"] = 1 }),
        });
        using var doc = JsonDocument.Parse(line);
        var root = doc.RootElement;
        Assert.Equal("t", root.GetProperty("s").GetString());
        Assert.True(root.GetProperty("b").GetBoolean());
        Assert.Equal(7, root.GetProperty("i").GetInt32());
        Assert.Equal(2, root.GetProperty("by").GetInt32());
        Assert.Equal(3u, root.GetProperty("u").GetUInt32());
        Assert.Equal(-9L, root.GetProperty("l").GetInt64());
        Assert.Equal(10UL, root.GetProperty("ul").GetUInt64());
        Assert.Equal(1.5m, root.GetProperty("m").GetDecimal());
        Assert.Equal(0.25, root.GetProperty("d").GetDouble());
        Assert.Equal(0.5, root.GetProperty("f").GetDouble());
        Assert.Equal(JsonValueKind.Null, root.GetProperty("n").ValueKind);
        Assert.Equal("NaN", root.GetProperty("nan").GetString());
        Assert.Equal("Infinity", root.GetProperty("inf").GetString());
        Assert.Equal("-Infinity", root.GetProperty("ninf").GetString());
        Assert.Equal("2026-01-02T03:04:05.0000000Z", root.GetProperty("dt").GetString());
        Assert.Equal("2026-01-02T03:04:05.6780000+00:00", root.GetProperty("dto").GetString());
        Assert.Equal("Friday", root.GetProperty("e").GetString());
        Assert.Equal(Guid.Empty.ToString(), root.GetProperty("g").GetString());
        Assert.Equal("[1,\"x\",\"System.Int32[]\",null]", root.GetProperty("arr").GetRawText());
        Assert.Equal(JsonValueKind.String, root.GetProperty("dict").ValueKind);
    }

    [Fact]
    public void Hostile_Values_Never_Lose_The_Record()
    {
        var line = JsonLine.Format(Instant, LogLevel.Warning, "cat", new HostileState(), new HostileException(), (_, _) => throw new InvalidOperationException("formatter"));
        using var doc = JsonDocument.Parse(line);
        var root = doc.RootElement;
        Assert.Equal("[unprintable HostileState]", root.GetProperty("msg").GetString());
        Assert.Equal("[unprintable HostileValue]", root.GetProperty("v").GetString());
        Assert.Equal("[unprintable ThrowingEnumerable]", root.GetProperty("items").GetString());
        Assert.False(root.TryGetProperty("after", out _));
        Assert.Equal("[unprintable HostileException]", root.GetProperty("exception").GetString());
    }

    [Fact]
    public void A_State_That_Is_Not_Pairs_Gives_Only_The_Message_And_A_Null_Formatter_Uses_Its_Text()
    {
        Assert.Equal("{\"time\":\"2026-01-02T03:04:05.678Z\",\"level\":\"INFO\",\"msg\":\"plain\",\"logger\":\"c\"}\n",
            JsonLine.Format<object>(Instant, LogLevel.Information, "c", "plain", null, null));
        Assert.Contains("\"msg\":\"\"", JsonLine.Format<object?>(Instant, LogLevel.Information, "c", null, null, (_, _) => null!));
    }

    [Fact]
    public void Control_Characters_Are_Escaped_Non_Ascii_And_Html_Characters_Are_Not()
    {
        var line = Line(LogLevel.Information, message: "a\nb\r\"c\\ é <x>&\u0001");
        Assert.Single(line.Split('\n', StringSplitOptions.RemoveEmptyEntries));
        Assert.Contains("\"msg\":\"a\\nb\\r\\\"c\\\\ é <x>&\\u0001\"", line);
        Assert.Equal("a\nb\r\"c\\ é <x>&\u0001", JsonDocument.Parse(line).RootElement.GetProperty("msg").GetString());
    }

    [Fact]
    public void Logger_Writes_Through_Output_Only_When_Active_And_Never_Throws()
    {
        var writer = new StringWriter();
        var inactive = new JsonLineLoggerProvider(() => false) { Output = () => writer };
        var logger = inactive.CreateLogger("c");
        Assert.False(logger.IsEnabled(LogLevel.Critical));
        logger.LogCritical("dropped");
        Assert.Equal(string.Empty, writer.ToString());

        var failing = new JsonLineLoggerProvider(() => throw new InvalidOperationException("resolution"));
        Assert.False(failing.Active);

        var active = new JsonLineLoggerProvider(() => true) { Output = () => throw new IOException("closed") };
        var activeLogger = active.CreateLogger("c");
        Assert.True(activeLogger.IsEnabled(LogLevel.Trace));
        Assert.False(activeLogger.IsEnabled(LogLevel.None));
        Assert.Null(activeLogger.BeginScope("scope"));
        activeLogger.LogWarning("no throw");
        active.Dispose();
    }

    [Fact]
    public void Default_Output_Is_The_Current_Console_Out()
    {
        var provider = new JsonLineLoggerProvider(() => true);
        Assert.Same(Console.Out, provider.Output());
        Assert.Equal(Console.OutputEncoding.CodePage, provider.OutputEncoding().CodePage);
        Assert.InRange(provider.Clock(), DateTimeOffset.UtcNow.AddMinutes(-1), DateTimeOffset.UtcNow.AddMinutes(1));
    }

    // ── When the fallback writes (D6) ──────────────────────────────────────

    private sealed class Pipeline : IDisposable
    {
        public Pipeline(ServiceProvider services)
        {
            Services = services;
            Stdout = services.GetServices<ILoggerProvider>().OfType<JsonLineLoggerProvider>().Single();
            Stdout.Output = () => Writer;
            Factory = services.GetRequiredService<ILoggerFactory>();
        }

        public ServiceProvider Services { get; }

        public JsonLineLoggerProvider Stdout { get; }

        public StringWriter Writer { get; } = new();

        public ILoggerFactory Factory { get; }

        public string[] Lines => Writer.ToString().Split('\n', StringSplitOptions.RemoveEmptyEntries);

        public LoggerFilterOptions Filters => Services.GetRequiredService<IOptionsMonitor<LoggerFilterOptions>>().CurrentValue;

        public void Dispose() => Services.Dispose();
    }

    private static Pipeline Build(Action<TelemetryOptions>? configure = null, Action<IServiceCollection>? before = null, Action<ILoggingBuilder>? logging = null, IConfiguration? configuration = null)
    {
        var services = new ServiceCollection();
        services.AddSingleton<IOtelHelperDiagnostics>(new TestDiagnostics().Sink);
        if (configuration is not null)
        {
            services.AddSingleton(configuration);
        }

        before?.Invoke(services);
        services.AddOtelHelper(o =>
        {
            o.ServiceName ??= "svc";
            configure?.Invoke(o);
        });
        services.AddLogging(b =>
        {
            if (configuration is not null)
            {
                b.AddConfiguration(configuration.GetSection("Logging"));
            }

            logging?.Invoke(b);
        });
        return new Pipeline(services.BuildServiceProvider());
    }

    [Theory]
    [InlineData(null, null, null, true)]
    [InlineData("http://127.0.0.1:4317", null, null, false)]
    [InlineData("http://127.0.0.1:4317", "none", null, true)]
    [InlineData("http://127.0.0.1:4317", "console", null, false)]
    [InlineData("http://127.0.0.1:4317", "otlp,console", null, false)]
    [InlineData(null, "console", null, false)]
    [InlineData(null, "otlp", null, true)]
    [InlineData("http://127.0.0.1:4317", null, "logs", true)]
    [InlineData("http://127.0.0.1:4317", null, "traces", false)]
    public void Fallback_Writes_Exactly_When_No_Logs_Exporter_Is_Active(string? endpoint, string? logsExporter, string? disabled, bool fallback)
    {
        Set("OTEL_EXPORTER_OTLP_ENDPOINT", endpoint);
        Set("OTEL_LOGS_EXPORTER", logsExporter);
        Set("OTEL_HELPER_DISABLED_SIGNALS", disabled);
        using var pipeline = Build(o => o.Environment = "DEV");
        pipeline.Factory.CreateLogger("app").LogWarning("record");

        Assert.Equal(fallback, pipeline.Stdout.Active);
        Assert.Equal(fallback ? 1 : 0, pipeline.Lines.Length);
        Assert.Equal(!fallback, LoggingSetup.LogsExporterActive(pipeline.Services.GetRequiredService<ResolvedTelemetry>()));
        // The idle provider is held at None, so the SDK provider never gets records without a processor.
        var idle = fallback ? LoggingSetup.OtelProviderName : LoggingSetup.JsonProviderName;
        Assert.Contains(pipeline.Filters.Rules, r => r.ProviderName == idle && r.CategoryName is null && r.LogLevel == LogLevel.None);
    }

    [Fact]
    public void Sdk_Disabled_Writes_The_Fallback()
    {
        Set("OTEL_EXPORTER_OTLP_ENDPOINT", "http://127.0.0.1:4317");
        Set("OTEL_SDK_DISABLED", "true");
        using var pipeline = Build(o => o.Environment = "DEV");
        pipeline.Factory.CreateLogger("app").LogWarning("record");
        Assert.Single(pipeline.Lines);
    }

    [Fact]
    public void Unusable_Endpoint_Writes_The_Fallback()
    {
        // An environment endpoint the helper cannot use is warned and logs get no exporter (C7a).
        Set("OTEL_EXPORTER_OTLP_LOGS_ENDPOINT", "http://[::1");
        using var pipeline = Build(o => o.Environment = "DEV");
        Assert.True(pipeline.Stdout.Active);
        Assert.False(LoggingSetup.LogsExporterActive(pipeline.Services.GetRequiredService<ResolvedTelemetry>()));
    }

    [Fact]
    public void Fallback_Line_Written_Through_The_Container_Has_The_Category_And_The_State()
    {
        using var pipeline = Build(o => o.Environment = "DEV");
        pipeline.Stdout.Clock = () => Instant;
        pipeline.Factory.CreateLogger("Orders.Api").LogWarning("order {OrderId}", 7);
        Assert.Equal("{\"time\":\"2026-01-02T03:04:05.678Z\",\"level\":\"WARN\",\"msg\":\"order 7\",\"logger\":\"Orders.Api\",\"OrderId\":7}", Assert.Single(pipeline.Lines));
    }

    [Fact]
    public void A_Second_AddOtelHelper_Registers_No_Second_Stdout_Provider()
    {
        var services = new ServiceCollection();
        services.AddSingleton<IOtelHelperDiagnostics>(new TestDiagnostics().Sink);
        services.AddOtelHelper(o => o.ServiceName = "svc");
        services.AddOtelHelper();
        using var provider = services.BuildServiceProvider();
        Assert.Single(provider.GetServices<ILoggerProvider>().OfType<JsonLineLoggerProvider>());
    }

    // ── R4 level ───────────────────────────────────────────────────────────

    [Theory]
    [InlineData("LOCAL", null, LogLevel.Debug)]
    [InlineData("DEV", null, LogLevel.Information)]
    [InlineData("HML", null, LogLevel.Information)]
    [InlineData("PRD", null, LogLevel.Warning)]
    [InlineData(null, null, LogLevel.Information)]
    [InlineData("staging", null, LogLevel.Information)]
    [InlineData("PRD", LogLevel.Trace, LogLevel.Trace)]
    [InlineData("LOCAL", LogLevel.Error, LogLevel.Error)]
    public void Stdout_Level_Follows_R4_And_Code_Wins(string? environment, LogLevel? code, LogLevel expected)
    {
        Set("ENVIRONMENT", environment);
        using var pipeline = Build(o => o.MinimumLogLevel = code);
        var logger = pipeline.Factory.CreateLogger("app");
        Assert.True(logger.IsEnabled(expected));
        if (expected > LogLevel.Trace)
        {
            Assert.False(logger.IsEnabled(expected - 1));
        }

        Assert.Equal(expected, LoggingSetup.HelperLevel(pipeline.Filters));
    }

    [Fact]
    public void Debug_Mode_Gives_Debug_And_Keeps_Framework_Categories()
    {
        Set("OTEL_HELPER_DEBUG_LEVEL", "true");
        using var pipeline = Build(o => o.Environment = "PRD");
        Assert.True(pipeline.Factory.CreateLogger("Microsoft.AspNetCore.Hosting").IsEnabled(LogLevel.Debug));
        Assert.DoesNotContain(pipeline.Filters.Rules, r => r.CategoryName == "Microsoft");
    }

    // ── N9: the application's providers and filters stay as they are ──────

    [Fact]
    public void App_Provider_Keeps_Its_Level_And_Framework_Warnings_While_The_Helper_Holds_Its_Own()
    {
        var app = new CapturingProvider();
        using var pipeline = Build(o => o.Environment = "PRD", logging: b => b.AddProvider(app));

        pipeline.Factory.CreateLogger("Microsoft.AspNetCore.Hosting").LogWarning("framework warning");
        pipeline.Factory.CreateLogger("System.Net.Http.HttpClient.Default").LogWarning("http warning");
        pipeline.Factory.CreateLogger("Orders").LogInformation("app info");
        pipeline.Factory.CreateLogger("Microsoft.AspNetCore.Hosting").LogError("framework error");

        // The application's provider: the framework default (Information), no helper rule (0.2.x: Microsoft.* at Error, MinLevel = Warning).
        Assert.Equal(new[] { "framework warning", "http warning", "app info", "framework error" }, app.Messages);
        Assert.Equal(LogLevel.Information, pipeline.Filters.MinLevel);
        Assert.DoesNotContain(pipeline.Filters.Rules, r => r.ProviderName is null);

        // The helper's stdout provider: R4 Warning, framework categories at Error.
        var line = Assert.Single(pipeline.Lines);
        Assert.Contains("framework error", line);
    }

    [Fact]
    public void App_Category_Rules_Without_Provider_Still_Apply_To_The_Helper_Providers_Also_When_Registered_Later()
    {
        using var pipeline = Build(o => o.Environment = "PRD", logging: b => b.AddFilter("Orders", LogLevel.Debug));
        pipeline.Factory.CreateLogger("Orders.Checkout").LogDebug("debug kept");
        pipeline.Factory.CreateLogger("Other").LogInformation("info dropped");
        Assert.Contains("debug kept", Assert.Single(pipeline.Lines));
        var copy = Assert.Single(pipeline.Filters.Rules, r => r.ProviderName == LoggingSetup.JsonProviderName && r.CategoryName == "Orders");
        Assert.Equal(LogLevel.Debug, copy.LogLevel);
    }

    [Fact]
    public void App_Rule_For_A_Framework_Category_Replaces_The_Helper_Error_Rule()
    {
        using var pipeline = Build(o => o.Environment = "PRD", logging: b => b.AddFilter("Microsoft", LogLevel.Warning));
        pipeline.Factory.CreateLogger("Microsoft.AspNetCore").LogWarning("framework warning");
        pipeline.Factory.CreateLogger("System.Net.Http.HttpClient").LogWarning("http dropped");
        Assert.Contains("framework warning", Assert.Single(pipeline.Lines));
    }

    [Fact]
    public void Longer_App_Category_Beats_The_Helper_Framework_Rule()
    {
        using var pipeline = Build(o => o.Environment = "PRD", logging: b => b.AddFilter("Microsoft.AspNetCore.Hosting", LogLevel.Information));
        pipeline.Factory.CreateLogger("Microsoft.AspNetCore.Hosting.Diagnostics").LogInformation("hosting info");
        pipeline.Factory.CreateLogger("Microsoft.AspNetCore.Routing").LogWarning("routing dropped");
        Assert.Contains("hosting info", Assert.Single(pipeline.Lines));
    }

    private static IConfiguration Config(params (string Key, string Value)[] values)
        => new ConfigurationBuilder().AddInMemoryCollection(values.Select(v => new KeyValuePair<string, string?>(v.Key, v.Value))).Build();

    [Fact]
    public void Template_Appsettings_In_Prd_Exports_Information_The_App_Default_Wins()
    {
        // The ASP.NET Core template appsettings.json. Owner decision 2026-11-11: the application's
        // explicit default wins over the R4 environment default (as Python and 0.2.x).
        var configuration = Config(("Logging:LogLevel:Default", "Information"), ("Logging:LogLevel:Microsoft.AspNetCore", "Warning"));
        using var pipeline = Build(o => o.Environment = "PRD", configuration: configuration);
        pipeline.Factory.CreateLogger("MyApp").LogInformation("app info kept");
        pipeline.Factory.CreateLogger("MyApp").LogDebug("app debug dropped");
        pipeline.Factory.CreateLogger("Microsoft.AspNetCore.Routing").LogWarning("aspnetcore warning kept");
        pipeline.Factory.CreateLogger("Microsoft.Hosting.Lifetime").LogInformation("framework info dropped");
        pipeline.Factory.CreateLogger("Microsoft.Hosting.Lifetime").LogError("framework error kept");
        Assert.Equal(new[] { "app info kept", "aspnetcore warning kept", "framework error kept" },
            pipeline.Lines.Select(l => JsonDocument.Parse(l).RootElement.GetProperty("msg").GetString()).ToArray());
        Assert.Equal(LogLevel.Information, LoggingSetup.HelperLevel(pipeline.Filters));
        Assert.DoesNotContain(pipeline.Filters.Rules, r => r.ProviderName == LoggingSetup.JsonProviderName && r.CategoryName is null);
    }

    [Fact]
    public void App_Default_Through_AddFilter_Wins_Over_R4()
    {
        using var pipeline = Build(o => o.Environment = "PRD", logging: b => b.AddFilter((string?)null, LogLevel.Debug));
        pipeline.Factory.CreateLogger("MyApp").LogDebug("debug kept");
        Assert.Single(pipeline.Lines);
        // A Debug default: no framework Error rules either.
        pipeline.Factory.CreateLogger("Microsoft.Extensions.Hosting").LogDebug("framework debug kept");
        Assert.Equal(2, pipeline.Lines.Length);
    }

    [Theory]
    [InlineData(LogLevel.Debug)]
    [InlineData(LogLevel.Information)]
    public void App_SetMinimumLevel_Wins_Over_R4(LogLevel level)
    {
        using var pipeline = Build(o => o.Environment = "PRD", logging: b => b.SetMinimumLevel(level));
        Assert.True(pipeline.Factory.CreateLogger("MyApp").IsEnabled(level));
        Assert.Equal(level, LoggingSetup.HelperLevel(pipeline.Filters));
    }

    [Fact]
    public void Code_MinimumLogLevel_Wins_Over_The_App_Default_And_The_Provider_Slot()
    {
        var configuration = Config(("Logging:LogLevel:Default", "Information"), ($"Logging:{JsonLineLoggerProvider.Alias}:LogLevel:Default", "Trace"));
        using var pipeline = Build(o =>
        {
            o.Environment = "LOCAL";
            o.MinimumLogLevel = LogLevel.Error;
        }, configuration: configuration);
        var logger = pipeline.Factory.CreateLogger("MyApp");
        Assert.False(logger.IsEnabled(LogLevel.Warning));
        Assert.True(logger.IsEnabled(LogLevel.Error));
        Assert.Equal(LogLevel.Error, LoggingSetup.HelperLevel(pipeline.Filters));
    }

    [Fact]
    public void App_Filter_Without_Level_Leaves_R4_And_Still_Runs()
    {
        var calls = new ConcurrentBag<string?>();
        using var pipeline = Build(o => o.Environment = "PRD", logging: b => b.AddFilter((provider, category, level) =>
        {
            calls.Add(category);
            return category != "Blocked";
        }));
        pipeline.Factory.CreateLogger("Orders").LogInformation("info dropped");
        pipeline.Factory.CreateLogger("Orders").LogWarning("warning kept");
        pipeline.Factory.CreateLogger("Blocked").LogError("filtered by the app");
        Assert.Contains("warning kept", Assert.Single(pipeline.Lines));
        Assert.Contains("Blocked", calls);
    }

    [Theory]
    [InlineData("OtelHelperStdout", 2, LogLevel.Trace)]
    [InlineData("StaffOps.OtelHelper.Logging.JsonLineLoggerProvider", 2, LogLevel.Trace)]
    // Measured (fix round 1): the framework's rule selector compares provider names ordinally, so a
    // lower-case alias is not that provider's rule; the helper ignores it too and R4 applies.
    [InlineData("otelhelperstdout", 0, LogLevel.Warning)]
    public void Provider_Specific_App_Configuration_Wins_For_That_Provider(string alias, int lines, LogLevel level)
    {
        var configuration = Config(($"Logging:{alias}:LogLevel:Default", "Trace"), ($"Logging:{alias}:LogLevel:Microsoft", "Information"));
        using var pipeline = Build(o => o.Environment = "PRD", configuration: configuration);
        pipeline.Factory.CreateLogger("Orders").LogTrace("trace kept");
        pipeline.Factory.CreateLogger("Microsoft.Extensions.Hosting").LogInformation("framework info kept");
        Assert.Equal(lines, pipeline.Lines.Length);
        Assert.Equal(level, LoggingSetup.HelperLevel(pipeline.Filters));
    }

    [Fact]
    public void Otel_Provider_Alias_Is_Read_From_The_Sdk()
        => Assert.Equal("OpenTelemetry", LoggingSetup.OtelProviderAlias);

    [Fact]
    public void Apply_Helper_Rules_Without_App_Rules()
    {
        var filters = new LoggerFilterOptions { MinLevel = LogLevel.Information };
        LoggingSetup.ApplyHelperRules(filters, null, LogLevel.Warning, appSetMinimumLevel: false, exporterActive: true);
        var otel = filters.Rules.Where(r => r.ProviderName == LoggingSetup.OtelProviderName).Select(r => (r.CategoryName, r.LogLevel)).ToArray();
        Assert.Equal(new (string?, LogLevel?)[] { (null, LogLevel.Warning), ("Microsoft", LogLevel.Error), ("System.Net.Http", LogLevel.Error) }, otel);
        var json = Assert.Single(filters.Rules, r => r.ProviderName == LoggingSetup.JsonProviderName);
        Assert.Equal(LogLevel.None, json.LogLevel);
        Assert.Equal(LogLevel.Warning, LoggingSetup.HelperLevel(filters));
        Assert.Equal(LogLevel.Information, filters.MinLevel);
    }

    [Theory]
    [InlineData("OpenTelemetry")]
    [InlineData("OpenTelemetry.Logs.OpenTelemetryLoggerProvider")]
    public void Apply_Helper_Rules_Leaves_Slots_The_App_Set_By_Alias(string name)
    {
        var filters = new LoggerFilterOptions();
        filters.Rules.Add(new LoggerFilterRule(name, null, LogLevel.Information, null));
        filters.Rules.Add(new LoggerFilterRule(name, "microsoft", LogLevel.Information, null));
        filters.Rules.Add(new LoggerFilterRule(null, "Orders", LogLevel.Trace, null));
        filters.Rules.Add(new LoggerFilterRule(name, "Orders", LogLevel.Critical, null));
        LoggingSetup.ApplyHelperRules(filters, null, LogLevel.Warning, appSetMinimumLevel: false, exporterActive: true);
        var added = filters.Rules.Skip(4).Where(r => r.ProviderName == LoggingSetup.OtelProviderName).Select(r => (r.CategoryName, r.LogLevel)).ToArray();
        Assert.Equal(new (string?, LogLevel?)[] { ("System.Net.Http", LogLevel.Error) }, added);
        Assert.Equal(LogLevel.Information, LoggingSetup.HelperLevel(filters));
    }

    [Fact]
    public void Apply_Helper_Rules_App_MinLevel_Gives_No_Default_Rule()
    {
        var filters = new LoggerFilterOptions { MinLevel = LogLevel.Debug };
        LoggingSetup.ApplyHelperRules(filters, null, LogLevel.Warning, appSetMinimumLevel: true, exporterActive: false);
        Assert.DoesNotContain(filters.Rules, r => r.ProviderName == LoggingSetup.JsonProviderName);
        Assert.Equal(LogLevel.Debug, LoggingSetup.HelperLevel(filters));
    }

    [Fact]
    public void App_SetMinimumLevel_Is_Detected()
    {
        using var plain = Build();
        Assert.False(LoggingSetup.AppSetMinimumLevel(plain.Services, plain.Filters));
        using var set = Build(logging: b => b.SetMinimumLevel(LogLevel.Information));
        Assert.True(LoggingSetup.AppSetMinimumLevel(set.Services, set.Filters));
    }

    // ── Duplicate stdout lines with the host console provider (fix round 1) ─

    [Theory]
    [InlineData(true, null, 1)]
    [InlineData(true, "http://127.0.0.1:4317", 0)]
    [InlineData(false, null, 0)]
    public async Task Host_Console_Provider_With_The_Fallback_Warns_Once(bool console, string? endpoint, int warnings)
    {
        Set("OTEL_SERVICE_NAME", "svc");
        Set("ENVIRONMENT", "DEV");
        Set("OTEL_EXPORTER_OTLP_ENDPOINT", endpoint);
        var diagnostics = new TestDiagnostics();
        var builder = Host.CreateApplicationBuilder(new HostApplicationBuilderSettings { DisableDefaults = true });
        if (console)
        {
            builder.Logging.AddConsole();
        }

        builder.Services.AddSingleton<IOtelHelperDiagnostics>(diagnostics.Sink);
        builder.Services.AddOtelHelper();
        using var host = builder.Build();
        await host.StartAsync();
        await host.StopAsync();
        Assert.Equal(warnings, diagnostics.Lines.Count(l => l.Contains("written to standard output twice", StringComparison.Ordinal)));
        if (warnings == 1)
        {
            Assert.Contains("ClearProviders()", diagnostics.Text);
        }
    }

    // ── Robustness (fix round 1) ───────────────────────────────────────────

    [Fact]
    public void Lone_Surrogates_In_Message_Value_And_Key_Become_Replacement_Characters()
    {
        var line = Line(LogLevel.Warning, new[] { Kv("k", "a\ud800b"), Kv("x\udc00", 1) }, message: "m\ud800");
        using var doc = JsonDocument.Parse(line);
        Assert.Equal("m�", doc.RootElement.GetProperty("msg").GetString());
        Assert.Equal("a�b", doc.RootElement.GetProperty("k").GetString());
        Assert.Equal(1, doc.RootElement.GetProperty("x�").GetInt32());
        Assert.Equal("ok\U0001F600", JsonLine.Clean("ok\U0001F600"));
    }

    [Fact]
    public void Characters_Outside_The_Bmp_Are_Written_Raw_And_A_Literal_Backslash_U_Stays_Escaped()
    {
        var literal = @"\" + "uD83D" + @"\" + "uDE00";  // the text backslash-u-D83D..., built in pieces
        var line = Line(LogLevel.Information, new[] { Kv("e", "\U0001F600") }, message: "smile \U0001F600 " + literal + " " + @"\" + "\U0001F600");
        Assert.Contains("\"e\":\"\U0001F600\"", line);
        var rawMessage = line[(line.IndexOf("\"msg\":", StringComparison.Ordinal) + 6)..line.IndexOf(",\"logger\"", StringComparison.Ordinal)];
        Assert.Equal("\"smile \U0001F600 \\\\uD83D\\\\uDE00 \\\\\U0001F600\"", rawMessage);
        Assert.Equal("smile \U0001F600 \\uD83D\\uDE00 \\\U0001F600", JsonDocument.Parse(line).RootElement.GetProperty("msg").GetString());
    }

    [Fact]
    public void Supplementary_Characters_Stay_Escaped_When_Stdout_Is_Not_Utf8()
    {
        Assert.True(JsonLine.RawSupplementaryFor(new System.Text.UTF8Encoding(false)));
        Assert.True(JsonLine.RawSupplementaryFor(System.Text.Encoding.UTF8));
        Assert.False(JsonLine.RawSupplementaryFor(System.Text.Encoding.ASCII));
        Assert.False(JsonLine.RawSupplementaryFor(System.Text.Encoding.Latin1));
        Assert.False(JsonLine.RawSupplementaryFor(System.Text.Encoding.Unicode));
        Assert.False(JsonLine.RawSupplementaryFor(null));

        var writer = new StringWriter();
        var ascii = new JsonLineLoggerProvider(() => true) { Output = () => writer, OutputEncoding = () => System.Text.Encoding.ASCII };
        ascii.CreateLogger("c").LogInformation("e \U0001F600");
        Assert.DoesNotContain("\U0001F600", writer.ToString());
        Assert.Equal("e \U0001F600", JsonDocument.Parse(writer.ToString()).RootElement.GetProperty("msg").GetString());

        var utf8Writer = new StringWriter();
        var utf8 = new JsonLineLoggerProvider(() => true) { Output = () => utf8Writer, OutputEncoding = () => new System.Text.UTF8Encoding(false) };
        utf8.CreateLogger("c").LogInformation("e \U0001F600");
        Assert.Contains("\"msg\":\"e \U0001F600\"", utf8Writer.ToString());
    }

    [Fact]
    public void Collections_Are_Capped()
    {
        using var doc = JsonDocument.Parse(Line(LogLevel.Information, new[] { Kv("endless", Endless()) }));
        var items = doc.RootElement.GetProperty("endless").EnumerateArray().ToArray();
        Assert.Equal(JsonLine.MaxItems + 1, items.Length);
        Assert.Equal("…", items[^1].GetString());

        static IEnumerable<int> Endless()
        {
            for (var i = 0; ; i++)
            {
                yield return i;
            }
        }
    }

    [Fact]
    public void A_Failure_Outside_Format_Guards_Writes_The_Minimal_Line()
    {
        var writer = new StringWriter();
        var provider = new JsonLineLoggerProvider(() => true) { Output = () => writer, Clock = () => throw new InvalidOperationException("clock") };
        provider.CreateLogger("c").LogError("lost");
        using var doc = JsonDocument.Parse(writer.ToString());
        Assert.Equal("ERROR", doc.RootElement.GetProperty("level").GetString());
        Assert.Equal("[unrenderable record]", doc.RootElement.GetProperty("msg").GetString());
    }

    [Fact]
    public void Level_Reapplies_When_The_App_Environment_Arrives()
    {
        var pipeline = Build(before: s => s.ConfigureOpenTelemetryTracerProvider(b => b.ConfigureResource(r => r.AddAttributes(new[]
        {
            new KeyValuePair<string, object>("deployment.environment.name", "PRD"),
        }))));
        using (pipeline)
        {
            Assert.Equal(LogLevel.Information, LoggingSetup.HelperLevel(pipeline.Filters));
            pipeline.Services.GetRequiredService<OpenTelemetry.Trace.TracerProvider>();
            Assert.Equal(LogLevel.Warning, LoggingSetup.HelperLevel(pipeline.Filters));
            Assert.False(pipeline.Factory.CreateLogger("app").IsEnabled(LogLevel.Information));
        }
    }

    [Fact]
    public async Task Host_Writes_The_Line_And_Keeps_The_App_Console_Provider()
    {
        Set("OTEL_SERVICE_NAME", "svc");
        Set("ENVIRONMENT", "DEV");
        var builder = Host.CreateApplicationBuilder(new HostApplicationBuilderSettings { DisableDefaults = true });
        var app = new CapturingProvider();
        builder.Logging.AddProvider(app);
        builder.Services.AddSingleton<IOtelHelperDiagnostics>(new TestDiagnostics().Sink);
        builder.Services.AddOtelHelper();
        using var host = builder.Build();
        var stdout = host.Services.GetServices<ILoggerProvider>().OfType<JsonLineLoggerProvider>().Single();
        var writer = new StringWriter();
        stdout.Output = () => writer;
        await host.StartAsync();
        host.Services.GetRequiredService<ILogger<StdoutLoggingTests>>().LogWarning("hosted");
        await host.StopAsync();
        Assert.Contains("\"msg\":\"hosted\",\"logger\":\"StaffOps.OtelHelper.Tests.StdoutLoggingTests\"}", writer.ToString());
        Assert.Contains("hosted", app.Messages);
    }

    // ── Helpers ────────────────────────────────────────────────────────────

    private sealed class CapturingProvider : ILoggerProvider
    {
        public ConcurrentQueue<string> Messages { get; } = new();

        public ConcurrentQueue<string> Categories { get; } = new();

        public ILogger CreateLogger(string categoryName) => new Capturing(this, categoryName);

        public void Dispose()
        {
        }

        private sealed class Capturing : ILogger
        {
            private readonly CapturingProvider _owner;
            private readonly string _category;

            public Capturing(CapturingProvider owner, string category)
            {
                _owner = owner;
                _category = category;
            }

            public IDisposable? BeginScope<TState>(TState state)
                where TState : notnull => null;

            public bool IsEnabled(LogLevel logLevel) => true;

            public void Log<TState>(LogLevel logLevel, EventId eventId, TState state, Exception? exception, Func<TState, Exception?, string> formatter)
            {
                _owner.Categories.Enqueue(_category);
                _owner.Messages.Enqueue(formatter(state, exception));
            }
        }
    }

    private sealed class HostileValue
    {
        public override string ToString() => throw new InvalidOperationException("value");
    }

    private sealed class HostileException : Exception
    {
        public override string ToString() => throw new InvalidOperationException("exception");
    }

    private sealed class ThrowingEnumerable : System.Collections.IEnumerable
    {
        public System.Collections.IEnumerator GetEnumerator() => throw new InvalidOperationException("enumerate");

        public override string ToString() => throw new InvalidOperationException("text");
    }

    private sealed class HostileState : IEnumerable<KeyValuePair<string, object?>>
    {
        public IEnumerator<KeyValuePair<string, object?>> GetEnumerator()
        {
            yield return new KeyValuePair<string, object?>("v", new HostileValue());
            yield return new KeyValuePair<string, object?>("items", new ThrowingEnumerable());
            throw new InvalidOperationException("state");
        }

        System.Collections.IEnumerator System.Collections.IEnumerable.GetEnumerator() => GetEnumerator();

        public override string ToString() => throw new InvalidOperationException("state text");
    }
}
