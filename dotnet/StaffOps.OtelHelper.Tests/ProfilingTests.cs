using System;
using System.Collections.Concurrent;
using System.Diagnostics;
using System.Linq;
using System.Runtime.InteropServices;
using Microsoft.Extensions.DependencyInjection;
using Microsoft.Extensions.Hosting;
using Microsoft.Extensions.Logging;
using OpenTelemetry;
using OpenTelemetry.Trace;
using StaffOps.OtelHelper;
using StaffOps.OtelHelper.Diagnostics;
using StaffOps.OtelHelper.Profiling;
using Xunit;

namespace StaffOps.OtelHelper.Tests;

/// <summary>
/// Tests for StaffOps.OtelHelper.Profiling (task C15, N11): the prerequisite rules, the start
/// at host start, the warning through the diagnostics sink and the span processor wiring.
/// Rules measured 2026-11-12 against the native Pyroscope 0.14.5 profiler (linux/amd64).
/// Env var tests use [Collection("EnvVarTests")] to prevent parallel execution.
/// </summary>
[Collection("EnvVarTests")]
[ResetGlobalPropagation]
public class ProfilingTests : IDisposable
{
    private const string ProfilerPath = "/opt/pyroscope/Pyroscope.Profiler.Native.so";
    private const string WrapperPath = "/opt/pyroscope/Pyroscope.Linux.ApiWrapper.x64.so";

    private readonly List<string> _envVarsToClean = new();

    private void SetEnvVar(string key, string? value)
    {
        Environment.SetEnvironmentVariable(key, value);
        _envVarsToClean.Add(key);
    }

    public void Dispose()
    {
        foreach (var key in _envVarsToClean)
            Environment.SetEnvironmentVariable(key, null);
    }

    /// <summary>Every prerequisite met, as the README's deployment snippet.</summary>
    private static Dictionary<string, string?> CompleteEnv() => new()
    {
        [ProfilingPrerequisites.EnableProfilingEnvVar] = "1",
        [ProfilingPrerequisites.ProfilerEnvVar] = ProfilingPrerequisites.PyroscopeProfilerClsid,
        [ProfilingPrerequisites.ProfilerPathEnvVar] = ProfilerPath,
        [ProfilingPrerequisites.PreloadEnvVar] = WrapperPath,
        [ProfilingPrerequisites.ProfilingEnabledEnvVar] = "1",
        [ProfilingPrerequisites.ServerAddressEnvVar] = "http://pyroscope:4040",
    };

    private static ProfilingPrerequisites Read(
        Dictionary<string, string?> env,
        bool isLinux = true,
        Architecture architecture = Architecture.X64,
        params string[] existingFiles)
    {
        var files = existingFiles.Length == 0 ? new[] { ProfilerPath } : existingFiles;
        return ProfilingPrerequisites.From(name => env.TryGetValue(name, out var v) ? v : null, isLinux, architecture, files.Contains);
    }

    private static ProfilingPrerequisites With(string name, string? value, bool isLinux = true, Architecture architecture = Architecture.X64)
    {
        var env = CompleteEnv();
        env[name] = value;
        return Read(env, isLinux, architecture);
    }

    // ── Prerequisite rules ──────────────────────────────────────────────────

    [Fact]
    public void Complete_Environment_Is_Satisfied()
    {
        var prereqs = Read(CompleteEnv());

        Assert.True(prereqs.IsSatisfied);
        Assert.Empty(prereqs.Unmet);
        Assert.Null(prereqs.DescribeMissing());
        Assert.Equal(ProfilingPrerequisites.ProfilerPathEnvVar, prereqs.ProfilerPathVariable);
        Assert.True(prereqs.ProfilerPathExists);
        Assert.True(prereqs.PreloadHasApiWrapper);
    }

    [Theory]
    [InlineData(ProfilingPrerequisites.EnableProfilingEnvVar, null, "CORECLR_ENABLE_PROFILING=1")]
    [InlineData(ProfilingPrerequisites.EnableProfilingEnvVar, "0", "CORECLR_ENABLE_PROFILING=1")]
    // Measured: the runtime does not load the profiler with "true".
    [InlineData(ProfilingPrerequisites.EnableProfilingEnvVar, "true", "CORECLR_ENABLE_PROFILING=1")]
    [InlineData(ProfilingPrerequisites.ProfilerEnvVar, null, "CORECLR_PROFILER")]
    [InlineData(ProfilingPrerequisites.ProfilerEnvVar, "  ", "CORECLR_PROFILER")]
    [InlineData(ProfilingPrerequisites.ProfilerEnvVar, "{846F5F1C-F9AE-4B07-969E-05C26BC060D8}", "CORECLR_PROFILER: not the Pyroscope profiler")]
    // Measured: without braces the runtime does not load the profiler.
    [InlineData(ProfilingPrerequisites.ProfilerEnvVar, "BD1A650D-AC5D-4896-B64F-D6FA25D6B26A", "CORECLR_PROFILER: not the Pyroscope profiler")]
    [InlineData(ProfilingPrerequisites.ProfilerPathEnvVar, null, "CORECLR_PROFILER_PATH")]
    [InlineData(ProfilingPrerequisites.ProfilerPathEnvVar, "/opt/pyroscope/nope.so", "CORECLR_PROFILER_PATH: file not found")]
    [InlineData(ProfilingPrerequisites.PreloadEnvVar, null, "LD_PRELOAD: no Pyroscope.Linux.ApiWrapper")]
    [InlineData(ProfilingPrerequisites.PreloadEnvVar, "/usr/lib/libjemalloc.so.2", "LD_PRELOAD: no Pyroscope.Linux.ApiWrapper")]
    [InlineData(ProfilingPrerequisites.ProfilingEnabledEnvVar, null, "PYROSCOPE_PROFILING_ENABLED=1")]
    [InlineData(ProfilingPrerequisites.ServerAddressEnvVar, null, "PYROSCOPE_SERVER_ADDRESS")]
    [InlineData(ProfilingPrerequisites.ServerAddressEnvVar, " ", "PYROSCOPE_SERVER_ADDRESS")]
    public void One_Unmet_Prerequisite_Is_Named(string name, string? value, string expected)
    {
        var prereqs = With(name, value);

        Assert.False(prereqs.IsSatisfied);
        Assert.Equal(expected, Assert.Single(prereqs.Unmet));
        Assert.Equal(expected, prereqs.DescribeMissing());
    }

    [Fact]
    public void Clsid_Comparison_Ignores_Case()
    {
        // Measured: the runtime loads the profiler with the lowercase CLSID.
        Assert.True(With(ProfilingPrerequisites.ProfilerEnvVar, "{bd1a650d-ac5d-4896-b64f-d6fa25d6b26a}").IsSatisfied);
    }

    [Fact]
    public void Nothing_Set_Lists_Every_Prerequisite_In_Check_Order()
    {
        var prereqs = Read(new Dictionary<string, string?>());

        Assert.Equal(
            new[]
            {
                "CORECLR_ENABLE_PROFILING=1", "CORECLR_PROFILER", "CORECLR_PROFILER_PATH",
                "LD_PRELOAD: no Pyroscope.Linux.ApiWrapper", "PYROSCOPE_PROFILING_ENABLED=1", "PYROSCOPE_SERVER_ADDRESS",
            },
            prereqs.Unmet);
        Assert.Equal(
            "CORECLR_ENABLE_PROFILING=1, CORECLR_PROFILER, CORECLR_PROFILER_PATH, LD_PRELOAD: no Pyroscope.Linux.ApiWrapper, PYROSCOPE_PROFILING_ENABLED=1, PYROSCOPE_SERVER_ADDRESS",
            prereqs.DescribeMissing());
    }

    [Fact]
    public void Preload_Is_Not_Required_Off_Linux()
    {
        var prereqs = With(ProfilingPrerequisites.PreloadEnvVar, null, isLinux: false);

        Assert.False(prereqs.PreloadRequired);
        Assert.True(prereqs.IsSatisfied);
    }

    [Theory]
    [InlineData(Architecture.X64, "CORECLR_PROFILER_PATH_64")]
    [InlineData(Architecture.X86, "CORECLR_PROFILER_PATH_32")]
    [InlineData(Architecture.Arm64, "CORECLR_PROFILER_PATH_ARM64")]
    [InlineData(Architecture.Arm, "CORECLR_PROFILER_PATH_ARM32")]
    public void Architecture_Specific_Path_Wins_Over_The_Generic_One(Architecture architecture, string specific)
    {
        var env = CompleteEnv();
        env[ProfilingPrerequisites.ProfilerPathEnvVar] = "/opt/pyroscope/nope.so";
        env[specific] = ProfilerPath;

        var prereqs = Read(env, architecture: architecture);

        Assert.Equal(specific, prereqs.ProfilerPathVariable);
        Assert.True(prereqs.IsSatisfied);
    }

    [Fact]
    public void Architecture_Specific_Path_To_A_Missing_File_Is_Named_Even_With_A_Valid_Generic_Path()
    {
        var env = CompleteEnv();
        env["CORECLR_PROFILER_PATH_64"] = "/opt/pyroscope/nope.so";

        Assert.Equal("CORECLR_PROFILER_PATH_64: file not found", Assert.Single(Read(env).Unmet));
    }

    [Fact]
    public void Path_Variable_Of_Another_Architecture_Is_Ignored()
    {
        var env = CompleteEnv();
        env[ProfilingPrerequisites.ProfilerPathEnvVar] = null;
        env["CORECLR_PROFILER_PATH_64"] = ProfilerPath;

        Assert.Equal("CORECLR_PROFILER_PATH", Assert.Single(Read(env, architecture: Architecture.Arm64).Unmet));
    }

    [Fact]
    public void Unknown_Architecture_Reads_Only_The_Generic_Path()
    {
        Assert.Equal(
            new[] { ProfilingPrerequisites.ProfilerPathEnvVar },
            ProfilingPrerequisites.PathVariables(Architecture.S390x));
    }

    [Theory]
    [InlineData("1")]
    [InlineData("t")]
    [InlineData("Y")]
    [InlineData("true")]
    [InlineData("TRUE")]
    [InlineData("yes")]
    public void Pyroscope_Switch_Follows_Pyroscope_Boolean_Rule(string value)
    {
        Assert.True(With(ProfilingPrerequisites.ProfilingEnabledEnvVar, value).ProfilingEnabled);
    }

    [Theory]
    [InlineData("0")]
    [InlineData("false")]
    [InlineData("")]
    [InlineData("on")]
    [InlineData(" 1")]
    public void Pyroscope_Switch_Rejects_Other_Values(string value)
    {
        var prereqs = With(ProfilingPrerequisites.ProfilingEnabledEnvVar, value);

        Assert.False(prereqs.ProfilingEnabled);
        Assert.False(prereqs.IsSatisfied);
    }

    [Fact]
    public void FromEnvironment_Reads_The_Process_Environment()
    {
        var existing = typeof(object).Assembly.Location;
        SetEnvVar(ProfilingPrerequisites.EnableProfilingEnvVar, "1");
        SetEnvVar(ProfilingPrerequisites.ProfilerEnvVar, ProfilingPrerequisites.PyroscopeProfilerClsid);
        foreach (var name in ProfilingPrerequisites.PathVariables(RuntimeInformation.ProcessArchitecture))
            SetEnvVar(name, null);
        SetEnvVar(ProfilingPrerequisites.ProfilerPathEnvVar, existing);
        SetEnvVar(ProfilingPrerequisites.PreloadEnvVar, WrapperPath);
        SetEnvVar(ProfilingPrerequisites.ProfilingEnabledEnvVar, "1");
        SetEnvVar(ProfilingPrerequisites.ServerAddressEnvVar, "http://pyroscope:4040");
        SetEnvVar(ProfilingPrerequisites.ApplicationNameEnvVar, "my-app");

        var prereqs = ProfilingPrerequisites.FromEnvironment();

        Assert.True(prereqs.IsSatisfied, prereqs.DescribeMissing());
        Assert.Equal("my-app", prereqs.ApplicationName);
        Assert.Equal("http://pyroscope:4040", prereqs.ServerAddress);
        Assert.Equal(RuntimeInformation.IsOSPlatform(OSPlatform.Linux), prereqs.PreloadRequired);
    }

    [Fact]
    public void ApplicationName_Is_Optional()
    {
        var prereqs = Read(CompleteEnv());

        Assert.True(prereqs.IsSatisfied);
        Assert.Null(prereqs.ApplicationName);
    }

    // ── Start at host start, warning through the sink ───────────────────────

    private sealed class FakeProvider : IProfilingProvider
    {
        public int Starts { get; private set; }

        public Exception? Throw { get; init; }

        public void Start()
        {
            Starts++;
            if (Throw is not null) throw Throw;
        }

        public BaseProcessor<Activity>? GetSpanProcessor() => null;

        public ValueTask DisposeAsync() => ValueTask.CompletedTask;
    }

    private static (IServiceCollection Services, TestDiagnostics Diagnostics) Services(
        ProfilingPrerequisites prerequisites, IProfilingProvider? provider, bool withHelper = true)
    {
        var diagnostics = new TestDiagnostics();
        var services = new ServiceCollection();
        services.AddLogging();
        if (withHelper)
        {
            services.AddSingleton<IOtelHelperDiagnostics>(diagnostics.Sink);
            services.AddOtelHelper(o => o.ServiceName = "profiling-test");
        }

        services.AddSingleton(new ProfilingState(() => prerequisites));
        if (provider is not null) services.AddSingleton(provider);
        services.AddOtelHelperProfiling();
        return (services, diagnostics);
    }

    private static async Task StartHostedServices(IServiceProvider provider)
    {
        foreach (var hosted in provider.GetServices<IHostedService>().OfType<ProfilingStartup>())
            await hosted.StartAsync(CancellationToken.None);
    }

    [Fact]
    public async Task Profiler_Starts_When_The_Host_Starts_Not_At_Registration()
    {
        var fake = new FakeProvider();
        var (services, diagnostics) = Services(Read(CompleteEnv()), fake);

        using var provider = services.BuildServiceProvider();
        Assert.Equal(0, fake.Starts);

        await StartHostedServices(provider);

        Assert.Equal(1, fake.Starts);
        Assert.Empty(diagnostics.Lines);
    }

    [Fact]
    public async Task Generic_Host_Start_Starts_The_Profiler_Once()
    {
        SetEnvVar("OTEL_HELPER_METRICS_PORT", "0");
        var fake = new FakeProvider();
        var builder = Host.CreateApplicationBuilder();
        builder.Logging.ClearProviders();
        builder.Services.AddSingleton<IOtelHelperDiagnostics>(new TestDiagnostics().Sink);
        builder.Services.AddOtelHelper(o => o.ServiceName = "profiling-host");
        builder.Services.AddSingleton(new ProfilingState(() => Read(CompleteEnv())));
        builder.Services.AddSingleton<IProfilingProvider>(fake);
        builder.Services.AddOtelHelperProfiling();
        using var host = builder.Build();

        Assert.Equal(0, fake.Starts);
        await host.StartAsync();
        await host.StopAsync();

        Assert.Equal(1, fake.Starts);
    }

    [Fact]
    public async Task Unmet_Prerequisites_Warn_Once_Through_The_Sink_And_Do_Not_Start()
    {
        var fake = new FakeProvider();
        var (services, diagnostics) = Services(With(ProfilingPrerequisites.ProfilerEnvVar, null), fake);
        using var provider = services.BuildServiceProvider();

        await StartHostedServices(provider);
        await StartHostedServices(provider);

        Assert.Equal(0, fake.Starts);
        Assert.Equal(
            "otelhelper: WARN profiling prerequisites not met (CORECLR_PROFILER): the helper does not start the Pyroscope profiler and spans get no pyroscope.profile.id; " +
            "set these variables in the container environment (Helm values or Dockerfile), the runtime reads them when the process starts",
            Assert.Single(diagnostics.Lines));
    }

    [Fact]
    public async Task Warning_Writes_No_Logger_Record()
    {
        // Decision 2026-11-12 (C15): the warning left ILogger; the OtelHelper.Profiling category is gone.
        var categories = new ConcurrentQueue<string>();
        var (services, diagnostics) = Services(Read(new Dictionary<string, string?>()), new FakeProvider());
        services.AddLogging(b => b.AddProvider(new CategoryProvider(categories)));
        using var provider = services.BuildServiceProvider();

        await StartHostedServices(provider);

        Assert.Single(diagnostics.Lines);
        Assert.Empty(categories);
    }

    [Fact]
    public async Task Warning_Names_Variables_Never_Values()
    {
        var env = CompleteEnv();
        env[ProfilingPrerequisites.EnableProfilingEnvVar] = "0";
        env[ProfilingPrerequisites.ServerAddressEnvVar] = "https://user:s3cr3t-pass@pyroscope.example.com";
        env["PYROSCOPE_AUTH_TOKEN"] = "tok-s3cr3t";
        env[ProfilingPrerequisites.ProfilerPathEnvVar] = "/opt/s3cr3t/nope.so";
        var (services, diagnostics) = Services(Read(env), new FakeProvider());
        using var provider = services.BuildServiceProvider();

        await StartHostedServices(provider);

        var line = Assert.Single(diagnostics.Lines);
        Assert.Contains("(CORECLR_ENABLE_PROFILING=1, CORECLR_PROFILER_PATH: file not found)", line);
        Assert.DoesNotContain("s3cr3t", line);
        Assert.DoesNotContain("pyroscope.example.com", line);
    }

    [Fact]
    public async Task Start_That_Throws_Reports_One_Error_And_The_Host_Keeps_Starting()
    {
        var fake = new FakeProvider { Throw = new EntryPointNotFoundException("/opt/pyroscope/Pyroscope.Profiler.Native.so: SetExceptionTrackingEnabled") };
        var (services, diagnostics) = Services(Read(CompleteEnv()), fake);
        using var provider = services.BuildServiceProvider();

        await StartHostedServices(provider);
        await StartHostedServices(provider);

        Assert.Equal(2, fake.Starts);
        var line = Assert.Single(diagnostics.Lines);
        Assert.Equal(
            "otelhelper: ERROR the Pyroscope profiler could not be started: its profile types stay off and the application keeps running error.type=System.EntryPointNotFoundException",
            line);
        Assert.DoesNotContain("/opt/pyroscope", line);
    }

    [Fact]
    public async Task Without_AddOtelHelper_The_Warning_Goes_To_The_Fallback_Sink()
    {
        var fallback = new TestDiagnostics();
        var saved = OtelHelperDiagnostics.Fallback;
        OtelHelperDiagnostics.Fallback = fallback.Sink;
        try
        {
            var (services, _) = Services(With(ProfilingPrerequisites.ServerAddressEnvVar, null), new FakeProvider(), withHelper: false);
            using var provider = services.BuildServiceProvider();

            await StartHostedServices(provider);

            Assert.Contains("profiling prerequisites not met (PYROSCOPE_SERVER_ADDRESS)", Assert.Single(fallback.Lines));
        }
        finally
        {
            OtelHelperDiagnostics.Fallback = saved;
        }
    }

    [Fact]
    public void Calling_Twice_Registers_The_Profiler_Once()
    {
        var services = new ServiceCollection();
        services.AddOtelHelperProfiling();
        services.AddOtelHelperProfiling();

        Assert.Single(services, d => d.ServiceType == typeof(ProfilingState));
        Assert.Single(services, d => d.ServiceType == typeof(IProfilingProvider));
        Assert.Single(services, d => d.ServiceType == typeof(IHostedService) && d.ImplementationType == typeof(ProfilingStartup));
    }

    [Fact]
    public void Registration_Resolves_The_Pyroscope_Provider()
    {
        var services = new ServiceCollection();
        services.AddOtelHelperProfiling();
        using var provider = services.BuildServiceProvider();

        Assert.IsType<PyroscopeProfilingProvider>(provider.GetRequiredService<IProfilingProvider>());
        Assert.NotNull(provider.GetRequiredService<IProfilingProvider>().GetSpanProcessor());
    }

    [Fact]
    public void Synchronous_Container_Dispose_Does_Not_Throw()
    {
        // The container creates the Pyroscope provider: a provider that is only IAsyncDisposable
        // makes ServiceProvider.Dispose() (and `using var host`) throw.
        var services = new ServiceCollection();
        services.AddOtelHelperProfiling();
        var provider = services.BuildServiceProvider();
        _ = provider.GetRequiredService<IProfilingProvider>();

        provider.Dispose();
    }

    [Fact]
    public void Null_Services_Throws()
    {
        Assert.Throws<ArgumentNullException>(() => OtelProfilingExtensions.AddOtelHelperProfiling(null!));
    }

    // ── Span processor wiring ───────────────────────────────────────────────

    private static Activity? RootSpan(ProfilingPrerequisites prerequisites, int registrations = 1)
    {
        var services = new ServiceCollection();
        services.AddLogging();
        services.AddSingleton<IOtelHelperDiagnostics>(new TestDiagnostics().Sink);
        services.AddOtelHelper(o => o.ServiceName = "profiling-spans");
        services.AddSingleton(new ProfilingState(() => prerequisites));
        for (var i = 0; i < registrations; i++) services.AddOtelHelperProfiling();
        services.ConfigureOpenTelemetryTracerProvider(b => b.AddSource("profiling-test"));
        using var provider = services.BuildServiceProvider();
        using var tracer = provider.GetRequiredService<TracerProvider>();

        using var source = new ActivitySource("profiling-test");
        var span = source.StartActivity("root");
        span?.Stop();
        return span;
    }

    [Fact]
    public void Met_Prerequisites_Add_The_Span_Processor()
    {
        // Measured: the Pyroscope processor writes the root span id as pyroscope.profile.id.
        var span = RootSpan(Read(CompleteEnv()));

        Assert.NotNull(span);
        Assert.Equal(span!.SpanId.ToHexString(), span.GetTagItem("pyroscope.profile.id"));
    }

    [Fact]
    public void Unmet_Prerequisites_Leave_The_Span_Processor_Out()
    {
        var span = RootSpan(With(ProfilingPrerequisites.ProfilerEnvVar, null));

        Assert.NotNull(span);
        Assert.Null(span!.GetTagItem("pyroscope.profile.id"));
    }

    [Fact]
    public void Two_Registrations_Add_One_Span_Processor()
    {
        var span = RootSpan(Read(CompleteEnv()), registrations: 2);

        Assert.Single(span!.TagObjects, t => t.Key == "pyroscope.profile.id");
    }

    [Fact]
    public async Task Pyroscope_Provider_Disposes_Without_Error()
    {
        var provider = new PyroscopeProfilingProvider();
        Assert.NotNull(provider.GetSpanProcessor());

        await provider.DisposeAsync();
    }

    [Fact]
    public void State_Reads_The_Prerequisites_Once()
    {
        var reads = 0;
        var state = new ProfilingState(() => { reads++; return Read(CompleteEnv()); });

        _ = state.Prerequisites;
        _ = state.Prerequisites;

        Assert.Equal(1, reads);
    }

    [Fact]
    public void Default_State_Reads_The_Process_Environment()
    {
        SetEnvVar(ProfilingPrerequisites.EnableProfilingEnvVar, null);

        Assert.False(new ProfilingState().Prerequisites.ClrProfilerEnabled);
    }

    private sealed class CategoryProvider : ILoggerProvider
    {
        private readonly ConcurrentQueue<string> _categories;

        public CategoryProvider(ConcurrentQueue<string> categories) => _categories = categories;

        public ILogger CreateLogger(string categoryName) => new Category(_categories, categoryName);

        public void Dispose()
        {
        }

        private sealed class Category : ILogger
        {
            private readonly ConcurrentQueue<string> _categories;
            private readonly string _name;

            public Category(ConcurrentQueue<string> categories, string name)
            {
                _categories = categories;
                _name = name;
            }

            public IDisposable? BeginScope<TState>(TState state) where TState : notnull => null;

            public bool IsEnabled(LogLevel logLevel) => true;

            public void Log<TState>(LogLevel logLevel, EventId eventId, TState state, Exception? exception, Func<TState, Exception?, string> formatter)
            {
                if (_name.Contains("Profiling", StringComparison.Ordinal)) _categories.Enqueue(_name);
            }
        }
    }
}
