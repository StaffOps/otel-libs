using System.Diagnostics;
using System.Net;
using System.Net.Sockets;
using System.Text;
using Microsoft.AspNetCore.Builder;
using Microsoft.AspNetCore.Hosting;
using Microsoft.AspNetCore.Http;
using Microsoft.Extensions.Configuration;
using Microsoft.Extensions.DependencyInjection;
using Microsoft.Extensions.Hosting;
using Microsoft.Extensions.Logging;
using OpenTelemetry;
using OpenTelemetry.Context.Propagation;
using OpenTelemetry.Trace;
using StaffOps.OtelHelper;
using StaffOps.OtelHelper.Tracing;
using Xunit;

namespace StaffOps.OtelHelper.Tests;

/// <summary>
/// Task C10 (contract R6, R8, R9, A20; ADR-002, ADR-006; design D-NET9, N2, N3): the helper-parsed
/// sampler and its ratio alias, <c>OTEL_PROPAGATORS</c> per token (also under
/// <c>OTEL_SDK_DISABLED</c>, and on the runtime's own <c>HttpClient</c> injection), the debug
/// attribute on entry spans only, and <c>StartRootActivity</c> restoring the previous activity.
/// Parity cases cover the same rows (<c>sampler.*</c>, <c>propagation.*</c>, <c>debug.*</c>).
/// Requirement DN6: the debug marker on entry spans and <c>StartRootActivity</c> restoring context.
/// </summary>
[Collection("EnvVarTests")]
[ResetGlobalPropagation]
public sealed class SamplerPropagatorDebugTests : IDisposable
{
    private static readonly string[] Variables =
    {
        "OTEL_TRACES_SAMPLER", "OTEL_TRACES_SAMPLER_ARG", "OTEL_HELPER_SAMPLE_RATIO", "OTEL_PROPAGATORS",
        "OTEL_SDK_DISABLED", "OTEL_TRACES_EXPORTER", "OTEL_EXPORTER_OTLP_ENDPOINT", "OTEL_HELPER_DEBUG_LEVEL",
        "OTEL_HELPER_DISABLED_SIGNALS",
    };

    private static readonly ActivityTraceId TraceId = ActivityTraceId.CreateFromString("4bf92f3577b34da6a3ce929d0e0e4736");
    private static readonly ActivitySpanId SpanId = ActivitySpanId.CreateFromString("00f067aa0ba902b7");

    public SamplerPropagatorDebugTests() => Clear();

    // The process-wide propagators are reset by [ResetGlobalPropagation] after each test.
    public void Dispose() => Clear();

    private static void Clear()
    {
        foreach (var variable in Variables)
        {
            Environment.SetEnvironmentVariable(variable, null);
        }
    }

    private static void Set(string variable, string? value) => Environment.SetEnvironmentVariable(variable, value);

    private static SamplingDecision Decide(Sampler sampler, ActivityContext parent)
        => sampler.ShouldSample(new SamplingParameters(parent, TraceId, "probe", ActivityKind.Internal)).Decision;

    private static ActivityContext Remote(bool sampled, string? traceState = null)
        => new(TraceId, SpanId, sampled ? ActivityTraceFlags.Recorded : ActivityTraceFlags.None, traceState, isRemote: true);

    // ── sampler: the six names ───────────────────────────────────────────

    [Theory]
    // name, ARG, root sampled, remote sampled parent sampled, remote unsampled parent sampled
    [InlineData("always_on", null, true, true, true)]
    [InlineData("always_off", null, false, false, false)]
    [InlineData("parentbased_always_on", null, true, true, false)]
    [InlineData("parentbased_always_off", null, false, true, false)]
    [InlineData("traceidratio", "0", false, false, false)]
    [InlineData("traceidratio", "1", true, true, true)]
    [InlineData("parentbased_traceidratio", "0", false, true, false)]
    [InlineData("  Always_Off \t", null, false, false, false)]
    [InlineData("PARENTBASED_ALWAYS_OFF", null, false, true, false)]
    public void Each_Spec_Name_Builds_Its_Sampler(string name, string? arg, bool root, bool remoteSampled, bool remoteUnsampled)
    {
        Set("OTEL_TRACES_SAMPLER", name);
        Set("OTEL_TRACES_SAMPLER_ARG", arg);
        var diagnostics = new TestDiagnostics();
        var sampler = diagnostics.Resolve(TestDiagnostics.Identified).Sampler;
        Assert.Empty(diagnostics.Lines);
        Assert.Equal(root, Decide(sampler, default) == SamplingDecision.RecordAndSample);
        Assert.Equal(remoteSampled, Decide(sampler, Remote(true)) == SamplingDecision.RecordAndSample);
        Assert.Equal(remoteUnsampled, Decide(sampler, Remote(false)) == SamplingDecision.RecordAndSample);
    }

    [Fact]
    public void Default_Is_ParentBased_Always_On()
    {
        // ADR-002: 0.2.x set a bare AlwaysOnSampler, which sampled under an unsampled remote parent.
        var resolved = TestDiagnostics.ResolveQuiet(TestDiagnostics.Identified);
        Assert.Equal("parentbased_always_on", resolved.SamplerName);
        Assert.Equal(SamplingDecision.RecordAndSample, Decide(resolved.Sampler, default));
        Assert.Equal(SamplingDecision.Drop, Decide(resolved.Sampler, Remote(false)));
    }

    [Theory]
    [InlineData("bogus", "unknown sampler (valid: always_on, always_off, traceidratio, parentbased_always_on, parentbased_always_off, parentbased_traceidratio)")]
    [InlineData("PARENTBASED_TRACEİDRATIO", "unknown sampler")]
    [InlineData("jaeger_remote", "sampler is unsupported by this helper")]
    [InlineData("parentbased_jaeger_remote", "sampler is unsupported by this helper")]
    [InlineData("XRAY", "sampler is unsupported by this helper")]
    public void Unknown_Or_Unsupported_Name_Warns_Once_And_Uses_The_Default(string name, string message)
    {
        Set("OTEL_TRACES_SAMPLER", name);
        Set("OTEL_TRACES_SAMPLER_ARG", "0");
        var diagnostics = new TestDiagnostics();
        var resolved = diagnostics.Resolve(TestDiagnostics.Identified);
        diagnostics.Resolve(TestDiagnostics.Identified);
        var line = Assert.Single(diagnostics.Lines);
        Assert.StartsWith("otelhelper: WARN " + message, line);
        Assert.Contains("variable=OTEL_TRACES_SAMPLER", line);
        Assert.EndsWith("using=parentbased_always_on", line);
        // A20: with ARG=0 an accepted ratio sampler would drop the root.
        Assert.Equal("parentbased_always_on", resolved.SamplerName);
        Assert.Equal(SamplingDecision.RecordAndSample, Decide(resolved.Sampler, default));
    }

    [Theory]
    [InlineData("2")]
    [InlineData("-0.5")]
    [InlineData("+0.5")]
    [InlineData("1e-1")]
    [InlineData("0x1")]
    [InlineData("NaN")]
    [InlineData(".5")]
    [InlineData("0,5")]
    [InlineData("٠.5")]
    public void Invalid_Sampler_Argument_Warns_And_Uses_One(string arg)
    {
        Set("OTEL_TRACES_SAMPLER", "traceidratio");
        Set("OTEL_TRACES_SAMPLER_ARG", arg);
        var diagnostics = new TestDiagnostics();
        var resolved = diagnostics.Resolve(TestDiagnostics.Identified);
        var line = Assert.Single(diagnostics.Lines);
        Assert.StartsWith("otelhelper: WARN sampler argument is not a number in [0, 1] for traceidratio variable=OTEL_TRACES_SAMPLER_ARG", line);
        Assert.EndsWith("using=1.0", line);
        Assert.Equal(SamplingDecision.RecordAndSample, Decide(resolved.Sampler, default));
    }

    [Theory]
    [InlineData("0.25", 0.25)]
    [InlineData(" 1.0 ", 1.0)]
    [InlineData("0", 0.0)]
    public void Sampler_Argument_Is_A_Plain_Decimal(string arg, double expected)
        => Assert.Equal(expected, SamplerResolver.ParseRatio(arg.Trim()));

    [Fact]
    public void Argument_Of_A_Non_Ratio_Sampler_Is_Ignored_Silently()
    {
        Set("OTEL_TRACES_SAMPLER", "always_on");
        Set("OTEL_TRACES_SAMPLER_ARG", "garbage");
        var diagnostics = new TestDiagnostics();
        diagnostics.Resolve(TestDiagnostics.Identified);
        Assert.Empty(diagnostics.Lines);
    }

    // ── the deprecated ratio alias ───────────────────────────────────────

    [Fact]
    public void Ratio_Alias_Is_ParentBased()
    {
        Set("OTEL_HELPER_SAMPLE_RATIO", "0");
        var diagnostics = new TestDiagnostics();
        var resolved = diagnostics.Resolve(TestDiagnostics.Identified);
        Assert.Empty(diagnostics.Lines);
        Assert.Equal("parentbased_traceidratio", resolved.SamplerName);
        Assert.Equal(SamplingDecision.Drop, Decide(resolved.Sampler, default));
        Assert.Equal(SamplingDecision.RecordAndSample, Decide(resolved.Sampler, Remote(true)));
    }

    [Theory]
    [InlineData("2")]
    [InlineData("1.5")]
    [InlineData("-1")]
    [InlineData("abc")]
    [InlineData("1e0")]
    public void Invalid_Ratio_Alias_Warns_And_Is_Ignored_Not_Clamped(string value)
    {
        // 0.2.x clamped 2 to 1 and ignored "abc" silently.
        Set("OTEL_HELPER_SAMPLE_RATIO", value);
        var diagnostics = new TestDiagnostics();
        var resolved = diagnostics.Resolve(TestDiagnostics.Identified);
        diagnostics.Resolve(TestDiagnostics.Identified);
        var line = Assert.Single(diagnostics.Lines);
        Assert.StartsWith("otelhelper: WARN sample ratio is not a number in [0, 1] variable=OTEL_HELPER_SAMPLE_RATIO", line);
        Assert.EndsWith("using=parentbased_always_on", line);
        Assert.Equal("parentbased_always_on", resolved.SamplerName);
    }

    [Fact]
    public void Ratio_Alias_Is_Not_Read_While_The_Standard_Variable_Is_Set()
    {
        Set("OTEL_TRACES_SAMPLER", "always_on");
        Set("OTEL_HELPER_SAMPLE_RATIO", "garbage");
        var diagnostics = new TestDiagnostics();
        Assert.Equal("always_on", diagnostics.Resolve(TestDiagnostics.Identified).SamplerName);
        Assert.Empty(diagnostics.Lines);
    }

    [Fact]
    public void Code_Sampler_And_Ratio_Win_Over_Every_Variable_Without_A_Line()
    {
        Set("OTEL_TRACES_SAMPLER", "bogus");
        Set("OTEL_HELPER_SAMPLE_RATIO", "garbage");
        var diagnostics = new TestDiagnostics();
        var code = new AlwaysOffSampler();
        Assert.Same(code, diagnostics.Resolve(o => { TestDiagnostics.Identified(o); o.Sampler = code; o.SampleRatio = 1.0; }).Sampler);
        Assert.Equal("parentbased_traceidratio", diagnostics.Resolve(o => { TestDiagnostics.Identified(o); o.SampleRatio = 1.0; }).SamplerName);
        Assert.Empty(diagnostics.Lines);
    }

    // ── IConfiguration-only values (principle 8) ─────────────────────────

    [Theory]
    [InlineData("OTEL_TRACES_SAMPLER", "always_off")]
    [InlineData("OTEL_TRACES_SAMPLER_ARG", "0")]
    [InlineData("OTEL_PROPAGATORS", "none")]
    public void Value_Only_In_IConfiguration_Is_Not_Read_And_Warns_Once(string variable, string value)
    {
        var configuration = new ConfigurationBuilder()
            .AddInMemoryCollection(new Dictionary<string, string?> { [variable] = value })
            .Build();
        var diagnostics = new TestDiagnostics();
        var resolved = diagnostics.Resolve(TestDiagnostics.Identified, configuration);
        diagnostics.Resolve(TestDiagnostics.Identified, configuration);
        var line = Assert.Single(diagnostics.Lines);
        Assert.StartsWith($"otelhelper: WARN {variable} is set only in IConfiguration and is not read", line);
        Assert.Equal("parentbased_always_on", resolved.SamplerName);
        Assert.Equal(new[] { "tracecontext", "baggage" }, resolved.Propagators);
    }

    [Fact]
    public void Values_In_The_Environment_Seen_Through_IConfiguration_Give_No_Not_Read_Line()
    {
        Set("OTEL_TRACES_SAMPLER", "always_off");
        Set("OTEL_PROPAGATORS", "baggage");
        var configuration = new ConfigurationBuilder().AddEnvironmentVariables().Build();
        var diagnostics = new TestDiagnostics();
        var resolved = diagnostics.Resolve(TestDiagnostics.Identified, configuration);
        Assert.Empty(diagnostics.Lines);
        Assert.Equal("always_off", resolved.SamplerName);
        Assert.Equal(new[] { "baggage" }, resolved.Propagators);
    }

    [Fact]
    public void Sampler_In_IConfiguration_With_A_Code_Sampler_Gives_No_Line()
    {
        var configuration = new ConfigurationBuilder()
            .AddInMemoryCollection(new Dictionary<string, string?> { ["OTEL_TRACES_SAMPLER"] = "always_off" })
            .Build();
        var diagnostics = new TestDiagnostics();
        diagnostics.Resolve(o => { TestDiagnostics.Identified(o); o.SampleRatio = 0.5; }, configuration);
        Assert.Empty(diagnostics.Lines);
    }

    [Fact]
    public void The_Sdk_Does_Not_Apply_A_Sampler_Set_Only_In_IConfiguration()
    {
        // 0.2.1 already ignored an IConfiguration-only value (it checked the process environment
        // before leaving the sampler to the SDK); what C10 adds is the warning.
        var builder = Host.CreateApplicationBuilder();
        builder.Logging.ClearProviders();
        builder.Configuration.AddInMemoryCollection(new Dictionary<string, string?> { ["OTEL_TRACES_SAMPLER"] = "always_off" });
        var diagnostics = new TestDiagnostics();
        builder.Services.AddSingleton<StaffOps.OtelHelper.Diagnostics.IOtelHelperDiagnostics>(diagnostics.Sink);
        builder.Services.AddOtelHelper(o => { TestDiagnostics.Identified(o); o.ServiceName = "c10-iconfig"; o.PrometheusMetricsPort = 0; });
        using var host = builder.Build();
        host.Services.GetRequiredService<TracerProvider>();
        using var source = new ActivitySource("c10-iconfig");
        using var activity = source.StartActivity("probe");
        Assert.True(activity?.Recorded);
        Assert.Contains(diagnostics.Lines, l => l.StartsWith("otelhelper: WARN OTEL_TRACES_SAMPLER is set only in IConfiguration", StringComparison.Ordinal));
    }

    // ── OTEL_PROPAGATORS parsing ─────────────────────────────────────────

    public static TheoryData<string?, string[], string[]> PropagatorRows => new()
    {
        { null, new[] { "tracecontext", "baggage" }, Array.Empty<string>() },
        { " \t", new[] { "tracecontext", "baggage" }, Array.Empty<string>() },
        { "baggage", new[] { "baggage" }, Array.Empty<string>() },
        { "TraceContext,Baggage", new[] { "tracecontext", "baggage" }, Array.Empty<string>() },
        { "tracecontext,,baggage,", new[] { "tracecontext", "baggage" }, Array.Empty<string>() },
        { "baggage, tracecontext", new[] { "baggage", "tracecontext" }, Array.Empty<string>() },
        { "baggage,BAGGAGE", new[] { "baggage" }, Array.Empty<string>() },
        { "none", Array.Empty<string>(), Array.Empty<string>() },
        { "NONE", Array.Empty<string>(), Array.Empty<string>() },
        {
            "tracecontext,none,b3", Array.Empty<string>(), new[]
            {
                "otelhelper: WARN propagator not supported by the .NET helper, ignored variable=OTEL_PROPAGATORS value=b3 using=none",
                "otelhelper: WARN \"none\" combined with other propagators; propagation disabled variable=OTEL_PROPAGATORS value=tracecontext,none,b3 using=none",
            }
        },
        {
            "b3,bogus", new[] { "tracecontext", "baggage" }, new[]
            {
                "otelhelper: WARN propagator not supported by the .NET helper, ignored variable=OTEL_PROPAGATORS value=b3 using=\"tracecontext, baggage\"",
                "otelhelper: WARN unknown propagator, ignored variable=OTEL_PROPAGATORS value=bogus using=\"tracecontext, baggage\"",
            }
        },
        { "foo,baggage", new[] { "baggage" }, new[] { "otelhelper: WARN unknown propagator, ignored variable=OTEL_PROPAGATORS value=foo using=baggage" } },
        {
            "b3multi,jaeger,xray,ottrace", new[] { "tracecontext", "baggage" }, new[]
            {
                "otelhelper: WARN propagator not supported by the .NET helper, ignored variable=OTEL_PROPAGATORS value=b3multi using=\"tracecontext, baggage\"",
                "otelhelper: WARN propagator not supported by the .NET helper, ignored variable=OTEL_PROPAGATORS value=jaeger using=\"tracecontext, baggage\"",
                "otelhelper: WARN propagator not supported by the .NET helper, ignored variable=OTEL_PROPAGATORS value=xray using=\"tracecontext, baggage\"",
                "otelhelper: WARN propagator not supported by the .NET helper, ignored variable=OTEL_PROPAGATORS value=ottrace using=\"tracecontext, baggage\"",
            }
        },
        { "TRACECONTEXTİ", new[] { "tracecontext", "baggage" }, new[] { "otelhelper: WARN unknown propagator, ignored variable=OTEL_PROPAGATORS value=tracecontextİ using=\"tracecontext, baggage\"" } },
        { "none,none", Array.Empty<string>(), Array.Empty<string>() },
    };

    [Theory]
    [MemberData(nameof(PropagatorRows))]
    public void Propagators_Are_Parsed_Per_Token(string? value, string[] expected, string[] lines)
    {
        Set("OTEL_PROPAGATORS", value);
        var diagnostics = new TestDiagnostics();
        Assert.Equal(expected, diagnostics.Resolve(TestDiagnostics.Identified).Propagators);
        diagnostics.Resolve(TestDiagnostics.Identified);
        Assert.Equal(lines, diagnostics.Lines);
    }

    [Theory]
    [InlineData(null, new[] { "baggage", "traceparent" })]
    [InlineData("tracecontext", new[] { "traceparent" })]
    [InlineData("baggage", new[] { "baggage" })]
    [InlineData("none", new string[0])]
    public void The_Default_TextMap_Propagator_Is_The_Resolved_List(string? value, string[] headers)
    {
        Set("OTEL_PROPAGATORS", value);
        using var provider = TestPipelines.Build(o => { TestDiagnostics.Identified(o); o.ServiceName = "c10-textmap"; o.PrometheusMetricsPort = 0; });
        provider.GetRequiredService<ResolvedTelemetry>();
        Assert.Equal(headers, InjectedByOpenTelemetry());
    }

    [Fact]
    public void A_Lone_TraceContext_Is_The_Sdk_TraceContext_Propagator()
    {
        // The HTTP instrumentation writes context itself only when the default propagator is not a
        // TraceContextPropagator; for a lone tracecontext the helper installs that type.
        Assert.IsType<TraceContextPropagator>(PropagatorSetup.Create(new[] { "tracecontext" }));
        Assert.IsType<BaggagePropagator>(PropagatorSetup.Create(new[] { "baggage" }));
        Assert.IsType<CompositeTextMapPropagator>(PropagatorSetup.Create(Array.Empty<string>()));
    }

    [Theory]
    [InlineData(null, new[] { "baggage" })]
    [InlineData("none", new string[0])]
    public void Propagators_Are_Installed_Under_Sdk_Disabled(string? value, string[] headers)
    {
        // A19: no-op providers, propagators kept; the no-op span has no valid context.
        Set("OTEL_SDK_DISABLED", "true");
        Set("OTEL_PROPAGATORS", "tracecontext");
        using (var first = TestPipelines.Build(o => { TestDiagnostics.Identified(o); o.ServiceName = "c10-disabled"; o.PrometheusMetricsPort = 0; }))
        {
            first.GetRequiredService<ResolvedTelemetry>();
        }

        Set("OTEL_PROPAGATORS", value);
        using var provider = TestPipelines.Build(o => { TestDiagnostics.Identified(o); o.ServiceName = "c10-disabled"; o.PrometheusMetricsPort = 0; });
        provider.GetRequiredService<ResolvedTelemetry>();
        Assert.Equal(headers, InjectedByOpenTelemetry());
    }

    [Fact]
    public void Hostless_Sdk_Disabled_Installs_Once_The_Helper_Settings_Resolve()
    {
        // Without a host nothing resolves the helper's settings under OTEL_SDK_DISABLED (the no-op
        // providers run no helper callback): the default stays until the app resolves something of
        // the helper's, such as its keyed ActivitySource. Documented in HOW-TO §13.
        Set("OTEL_SDK_DISABLED", "true");
        Set("OTEL_PROPAGATORS", "none");
        using var provider = TestPipelines.Build(o => { TestDiagnostics.Identified(o); o.ServiceName = "c10-hostless"; o.PrometheusMetricsPort = 0; });
        Assert.Equal(new[] { "baggage" }, InjectedByOpenTelemetry());
        provider.GetRequiredKeyedService<ActivitySource>(OtelHelperKeys.Instrumentation);
        Assert.Empty(InjectedByOpenTelemetry());
    }

    private static string[] InjectedByOpenTelemetry()
    {
        using var source = new ActivitySource("c10-textmap");
        var carrier = new Dictionary<string, string>();
        Baggage.SetBaggage("k", "v");
        using (var span = source.StartActivity("span"))
        {
            Propagators.DefaultTextMapPropagator.Inject(new PropagationContext(span?.Context ?? default, Baggage.Current), carrier,
                static (map, key, value) => map[key] = value);
        }

        return carrier.Keys.Select(k => k.ToLowerInvariant()).OrderBy(k => k, StringComparer.Ordinal).ToArray();
    }

    // ── what HttpClient sends (runtime DistributedContextPropagator) ─────

    [Theory]
    [InlineData(null, true, true)]
    [InlineData("tracecontext", true, false)]
    [InlineData("baggage", false, true)]
    [InlineData("none", false, false)]
    public async Task HttpClient_Sends_Only_The_Configured_Context(string? value, bool traceparent, bool baggage)
    {
        Set("OTEL_PROPAGATORS", value);
        using var provider = TestPipelines.Build(o => { TestDiagnostics.Identified(o); o.ServiceName = "c10-http"; o.PrometheusMetricsPort = 0; });
        provider.GetRequiredService<ResolvedTelemetry>();
        using var source = new ActivitySource("c10-http");
        Baggage.SetBaggage("k", "v");
        using var parent = source.StartActivity("parent");
        Assert.NotNull(parent);
        var headers = await SendCapturedAsync();
        Assert.Equal(traceparent ? 1 : 0, headers.Count(h => h.Name == "traceparent"));
        Assert.Equal(baggage, headers.Any(h => h.Name == "baggage"));
        if (!traceparent)
        {
            Assert.DoesNotContain(headers, h => h.Name is "tracestate" or "request-id" or "correlation-context");
        }
        else
        {
            Assert.Contains(parent!.TraceId.ToHexString(), headers.Single(h => h.Name == "traceparent").Value);
        }
    }

    [Theory]
    [InlineData(null, true)]
    [InlineData("none", false)]
    public async Task HttpClient_Follows_The_List_Under_Sdk_Disabled(string? value, bool traceparent)
    {
        Set("OTEL_SDK_DISABLED", "true");
        Set("OTEL_PROPAGATORS", value);
        using var provider = TestPipelines.Build(o => { TestDiagnostics.Identified(o); o.ServiceName = "c10-http-off"; o.PrometheusMetricsPort = 0; });
        provider.GetRequiredService<ResolvedTelemetry>();

        // An activity no OpenTelemetry provider created, as the ASP.NET Core host makes for a request
        // that continues a trace: passed on unchanged, as a no-op tracer does (a root gets nothing).
        using (var root = new Activity("root").SetIdFormat(ActivityIdFormat.W3C).Start())
        {
            Assert.DoesNotContain(await SendCapturedAsync(), h => h.Name == "traceparent");
        }

        // As the host does through the helper's continuation listener: an ASP.NET Core activity with the
        // remote parent (HasRemoteParent). With "none" no listener is started and nothing is created.
        using var aspNetCore = new ActivitySource(ContinuationListener.AspNetCoreSource);
        using var continued = aspNetCore.StartActivity("continued", ActivityKind.Server, Remote(true));
        var headers = await SendCapturedAsync();
        Assert.Equal(traceparent ? new[] { InboundTraceparent } : Array.Empty<string>(),
            headers.Where(h => h.Name == "traceparent").Select(h => h.Value).ToArray());
    }

    [Fact]
    public async Task The_Runtime_Propagator_Follows_The_Last_Container()
    {
        Set("OTEL_PROPAGATORS", "none");
        using (var first = TestPipelines.Build(o => { TestDiagnostics.Identified(o); o.ServiceName = "c10-last"; o.PrometheusMetricsPort = 0; }))
        {
            first.GetRequiredService<ResolvedTelemetry>();
            Assert.Same(HelperRuntimePropagator.Global, DistributedContextPropagator.Current);
            using var manual = new Activity("manual").SetIdFormat(ActivityIdFormat.W3C).Start();
            Assert.DoesNotContain(await SendCapturedAsync(), h => h.Name == "traceparent");
        }

        // Not restored on dispose; the next container's settings apply (last container wins).
        Set("OTEL_PROPAGATORS", null);
        var diagnostics = new TestDiagnostics();
        using var second = TestPipelines.Build(o => { TestDiagnostics.Identified(o); o.ServiceName = "c10-last"; o.PrometheusMetricsPort = 0; }, diagnostics);
        second.GetRequiredService<ResolvedTelemetry>();
        Assert.Empty(HelperLines(diagnostics));
        Assert.True(HelperRuntimePropagator.GlobalSettings.IsDefault);
        using var again = new Activity("manual").SetIdFormat(ActivityIdFormat.W3C).Start();
        Assert.Contains(await SendCapturedAsync(), h => h.Name == "traceparent");
    }

    [Theory]
    [InlineData("none", true)]
    [InlineData(null, false)]
    public void An_Application_Runtime_Propagator_Is_Kept_And_Warned_When_It_Matters(string? value, bool warns)
    {
        var application = DistributedContextPropagator.CreatePassThroughPropagator();
        DistributedContextPropagator.Current = application;
        Set("OTEL_PROPAGATORS", value);
        var diagnostics = new TestDiagnostics();
        using var provider = TestPipelines.Build(o => { TestDiagnostics.Identified(o); o.ServiceName = "c10-app-propagator"; o.PrometheusMetricsPort = 0; }, diagnostics);
        provider.GetRequiredService<ResolvedTelemetry>();
        Assert.Same(application, DistributedContextPropagator.Current);
        Assert.Null(provider.GetService<DistributedContextPropagator>() as HelperRuntimePropagator);
        Assert.Equal(warns
            ? new[] { "otelhelper: WARN the application replaced DistributedContextPropagator.Current; HttpClient keeps writing context with it, not as OTEL_PROPAGATORS and the disabled signals say variable=OTEL_PROPAGATORS" }
            : Array.Empty<string>(), HelperLines(diagnostics));
    }

    /// <summary>The helper's own lines: SDK records another test's meters cause are forwarded too.</summary>
    private static string[] HelperLines(TestDiagnostics diagnostics)
        => diagnostics.Lines.Where(l => !l.StartsWith("otelhelper: WARN sdk ", StringComparison.Ordinal)).ToArray();

    // ── a real web host: inbound extraction and outbound headers ─────────

    private sealed record WebRun(ActivityTraceId ServerTraceId, ActivitySpanId ServerParentSpanId, IReadOnlyList<(string Name, string Value)> Outbound);

    private static readonly string InboundTraceparent = $"00-{TraceId.ToHexString()}-{SpanId.ToHexString()}-01";

    /// <summary>
    /// Starts an ASP.NET Core app with the helper, sends one request carrying <paramref name="inbound"/>
    /// headers, and returns the server activity's trace id and parent plus the headers the app's
    /// HttpClient call sent on (a fresh HttpClient created in the handler).
    /// </summary>
    private static async Task<WebRun> RunWebAsync(IReadOnlyDictionary<string, string> inbound, Action<TelemetryOptions>? configure = null, bool keepLogging = false, TestDiagnostics? diagnostics = null)
    {
        using var capture = new HeaderCapture();
        var builder = WebApplication.CreateBuilder();
        if (!keepLogging)
        {
            builder.Logging.ClearProviders();
        }

        builder.WebHost.ConfigureKestrel(k => k.Listen(IPAddress.Loopback, 0));
        builder.Services.AddSingleton<StaffOps.OtelHelper.Diagnostics.IOtelHelperDiagnostics>((diagnostics ?? new TestDiagnostics()).Sink);
        builder.Services.AddOtelHelper(o =>
        {
            TestDiagnostics.Identified(o);
            o.ServiceName = "c10-web";
            o.PrometheusMetricsPort = 0;
            configure?.Invoke(o);
        });
        await using var app = builder.Build();
        ActivityTraceId serverTrace = default;
        ActivitySpanId serverParent = default;
        app.MapGet("/work", async () =>
        {
            serverTrace = Activity.Current?.TraceId ?? default;
            serverParent = Activity.Current?.ParentSpanId ?? default;
            using var client = new HttpClient();
            await client.GetAsync($"http://127.0.0.1:{capture.Port}/downstream");
            return "ok";
        });
        await app.StartAsync();
        var port = new Uri(app.Urls.First()).Port;
        try
        {
            // The test's own client must not write context of its own: no runtime propagator, so no
            // DiagnosticsHandler, no client activity and nothing for the HTTP instrumentation to inject.
            using var client = new HttpClient(new SocketsHttpHandler { ActivityHeadersPropagator = null });
            using var request = new HttpRequestMessage(HttpMethod.Get, $"http://127.0.0.1:{port}/work");
            foreach (var (name, value) in inbound)
            {
                request.Headers.TryAddWithoutValidation(name, value);
            }

            using var response = await client.SendAsync(request);
            response.EnsureSuccessStatusCode();
            return new WebRun(serverTrace, serverParent, capture.Headers());
        }
        finally
        {
            await app.StopAsync();
        }
    }

    [Theory]
    // OTEL_PROPAGATORS; server continues the inbound trace; outbound traceparent; outbound baggage
    [InlineData("none", false, false, false)]
    [InlineData("baggage", false, false, true)]
    [InlineData("tracecontext", true, true, false)]
    [InlineData(null, true, true, true)]
    public async Task Web_Host_Extracts_And_Injects_Only_The_Configured_Context(string? value, bool continues, bool traceparent, bool baggage)
    {
        // Fix round 1 (MEDIUM 1, 2): the ASP.NET Core extraction path and the HttpClient injection path
        // follow OTEL_PROPAGATORS. Go: none/baggage start a new trace; tracecontext sends no baggage.
        Set("OTEL_PROPAGATORS", value);
        var run = await RunWebAsync(new Dictionary<string, string> { ["traceparent"] = InboundTraceparent, ["baggage"] = "user.id=42" });
        Assert.NotEqual(default, run.ServerTraceId);
        Assert.Equal(continues, run.ServerTraceId == TraceId);
        Assert.Equal(continues, run.ServerParentSpanId == SpanId);
        if (!continues)
        {
            Assert.Equal(default, run.ServerParentSpanId);
        }

        var sent = run.Outbound.Where(h => h.Name == "traceparent").ToList();
        Assert.Equal(traceparent ? 1 : 0, sent.Count);
        if (traceparent)
        {
            Assert.StartsWith($"00-{TraceId.ToHexString()}-", sent[0].Value);
            Assert.EndsWith("-01", sent[0].Value);
        }

        Assert.DoesNotContain(run.Outbound, h => h.Name == "tracestate" && !traceparent);
        var bag = run.Outbound.Where(h => h.Name is "baggage" or "correlation-context").ToList();
        Assert.Equal(baggage, bag.Count > 0);
        if (baggage)
        {
            Assert.Contains("42", Assert.Single(bag).Value);
        }
    }

    [Fact]
    public void The_Helper_Runtime_Propagator_Wins_In_Every_Host_Registration_Order()
    {
        // Measured: WebApplication.CreateBuilder registers the runtime default (TryAddSingleton of
        // DistributedContextPropagator.Current) before the application's services; HostBuilder
        // registers it when ConfigureWebHostDefaults' own ConfigureServices runs, before or after ours.
        var web = WebApplication.CreateBuilder();
        web.Services.AddOtelHelper(TestDiagnostics.Identified);
        using (var app = web.Build())
        {
            Assert.IsType<HelperRuntimePropagator>(app.Services.GetRequiredService<DistributedContextPropagator>());
        }

        foreach (var helperFirst in new[] { true, false })
        {
            var host = new HostBuilder();
            if (helperFirst)
            {
                host.ConfigureServices(s => s.AddOtelHelper(TestDiagnostics.Identified));
            }

            host.ConfigureWebHostDefaults(w => w.Configure(_ => { }));
            if (!helperFirst)
            {
                host.ConfigureServices(s => s.AddOtelHelper(TestDiagnostics.Identified));
            }

            using var built = host.Build();
            Assert.IsType<HelperRuntimePropagator>(built.Services.GetRequiredService<DistributedContextPropagator>());
        }
    }

    [Theory]
    [InlineData(null, false)]
    [InlineData("none", true)]
    public void An_Application_Registered_Runtime_Propagator_Service_Is_Kept(string? value, bool warns)
    {
        Set("OTEL_PROPAGATORS", value);
        var mine = DistributedContextPropagator.CreatePassThroughPropagator();
        var diagnostics = new TestDiagnostics();
        var services = new ServiceCollection();
        services.AddSingleton<StaffOps.OtelHelper.Diagnostics.IOtelHelperDiagnostics>(diagnostics.Sink);
        services.AddSingleton(mine);
        services.AddOtelHelper(TestDiagnostics.Identified);
        using var provider = services.BuildServiceProvider();
        provider.GetRequiredService<ResolvedTelemetry>();
        Assert.Same(mine, provider.GetRequiredService<DistributedContextPropagator>());
        Assert.Equal(warns
            ? new[] { "otelhelper: WARN the application registered its own DistributedContextPropagator service; ASP.NET Core extracts inbound context with it, not as OTEL_PROPAGATORS says variable=OTEL_PROPAGATORS" }
            : Array.Empty<string>(), HelperLines(diagnostics));
    }

    [Theory]
    [InlineData("none", false, false)]
    [InlineData("tracecontext", true, false)]
    [InlineData(null, true, true)]
    public async Task HttpClient_Created_Before_Build_Follows_The_List(string? value, bool traceparent, bool baggage)
    {
        // Fix round 2: a handler takes DistributedContextPropagator.Current when it is created, so
        // AddOtelHelper sets it already, from OTEL_PROPAGATORS (environment only).
        Set("OTEL_PROPAGATORS", value);
        var services = new ServiceCollection();
        services.AddSingleton<StaffOps.OtelHelper.Diagnostics.IOtelHelperDiagnostics>(new TestDiagnostics().Sink);
        services.AddOtelHelper(TestDiagnostics.Identified);
        using var early = new HttpClient();
        Assert.Same(HelperRuntimePropagator.Global, DistributedContextPropagator.Current);
        using var provider = services.BuildServiceProvider();
        provider.GetRequiredService<ResolvedTelemetry>();
        using var manual = new Activity("manual").SetIdFormat(ActivityIdFormat.W3C).AddBaggage("user.id", "42").Start();
        var headers = await SendCapturedAsync(early);
        Assert.Equal(traceparent, headers.Any(h => h.Name == "traceparent"));
        Assert.Equal(baggage, headers.Any(h => h.Name is "baggage" or "correlation-context"));
    }

    [Fact]
    public void A_Current_Set_By_The_Application_After_AddOtelHelper_Is_Kept_And_Warned()
    {
        Set("OTEL_PROPAGATORS", "none");
        var diagnostics = new TestDiagnostics();
        var services = new ServiceCollection();
        services.AddSingleton<StaffOps.OtelHelper.Diagnostics.IOtelHelperDiagnostics>(diagnostics.Sink);
        services.AddOtelHelper(TestDiagnostics.Identified);
        var application = DistributedContextPropagator.CreatePassThroughPropagator();
        DistributedContextPropagator.Current = application;
        using var provider = services.BuildServiceProvider();
        provider.GetRequiredService<ResolvedTelemetry>();
        Assert.Same(application, DistributedContextPropagator.Current);
        Assert.Contains(HelperLines(diagnostics), l => l.StartsWith("otelhelper: WARN the application replaced DistributedContextPropagator.Current", StringComparison.Ordinal));
    }

    [Theory]
    [InlineData(true, true)]
    [InlineData(false, false)]
    public void Invalid_Context_Injection_Is_Noise_Only_When_Expected(bool expected, bool noise)
        => Assert.Equal(noise, StaffOps.OtelHelper.Diagnostics.SdkTextSanitizer.IsNoise("OpenTelemetry-Api",
            "Failed to inject activity context in format: 'TraceContextPropagator', context: 'Invalid context'.", true, expected));

    [Theory]
    // settings: tracecontext, baggage; expected fields
    [InlineData(true, true, "traceparent,tracestate,baggage,Correlation-Context")]
    [InlineData(true, false, "traceparent,tracestate")]
    [InlineData(false, true, "baggage,Correlation-Context")]
    [InlineData(false, false, "")]
    public void Runtime_Propagator_Fields_And_Extraction_Follow_The_Settings(bool traceContext, bool baggage, string fields)
    {
        var propagator = new HelperRuntimePropagator(() => new PropagationSettings(traceContext, baggage, false));
        Assert.Equal(fields, string.Join(",", propagator.Fields));
        var headers = new Dictionary<string, string> { ["traceparent"] = InboundTraceparent, ["baggage"] = "user.id=42" };
        DistributedContextPropagator.PropagatorGetterCallback getter = (object? carrier, string name, out string? value, out IEnumerable<string>? values) =>
        {
            values = null;
            value = ((Dictionary<string, string>)carrier!).TryGetValue(name, out var v) ? v : null;
        };
        propagator.ExtractTraceIdAndState(headers, getter, out var traceId, out _);
        Assert.Equal(traceContext ? InboundTraceparent : null, traceId);
        Assert.Equal(baggage, propagator.ExtractBaggage(headers, getter)?.Any() ?? false);

        // Injection: Activity baggage (an app's AddBaggage, or what another path extracted) is written
        // only with baggage configured; trace context only with tracecontext.
        using var activity = new Activity("inject").SetIdFormat(ActivityIdFormat.W3C).AddBaggage("user.id", "42").Start();
        var sent = new Dictionary<string, string>();
        propagator.Inject(activity, sent, (carrier, key, value) => ((Dictionary<string, string>)carrier!)[key] = value);
        Assert.Equal(traceContext, sent.ContainsKey("traceparent"));
        Assert.Equal(baggage, sent.ContainsKey("baggage") || sent.ContainsKey("Correlation-Context"));
    }

    // ── traces off: does context still flow through the server? ─────────

    [Theory]
    // OTEL_TRACES_EXPORTER=none; inbound flags; the code sampler (null = resolved default); outbound flags
    [InlineData("01", false, "01")]
    [InlineData("00", false, "00")]
    [InlineData("01", true, "00")]
    public async Task Traces_Off_Server_Passes_The_Inbound_Trace_On(string inboundFlags, bool alwaysOff, string outboundFlags)
    {
        // Decision C10: with OTEL_TRACES_EXPORTER=none the provider keeps the resolved sampler
        // (ParentBased), as Go and Python keep an SDK provider. The AlwaysOffSampler row shows the
        // rejected alternative: it would forward 00 and make every ParentBased service downstream drop
        // a trace the caller sampled.
        Set("OTEL_TRACES_EXPORTER", "none");
        var run = await RunWebAsync(
            new Dictionary<string, string> { ["traceparent"] = $"00-{TraceId.ToHexString()}-{SpanId.ToHexString()}-{inboundFlags}" },
            o => { if (alwaysOff) { o.Sampler = new AlwaysOffSampler(); } });
        var outbound = run.Outbound.Single(h => h.Name == "traceparent").Value;
        Assert.StartsWith($"00-{TraceId.ToHexString()}-", outbound);
        Assert.EndsWith("-" + outboundFlags, outbound);
    }

    [Theory]
    // how traces are off; inbound traceparent (null = a root request); expected outbound traceparent
    // ("same" = exactly the inbound one, "new" = a new trace started here, null = none)
    [InlineData("disabled-env", null, null)]
    [InlineData("disabled-code", null, null)]
    [InlineData("sdk-disabled", null, null)]
    [InlineData("disabled-env", "01", "same")]
    [InlineData("disabled-env", "00", "same")]
    [InlineData("disabled-code", "01", "same")]
    [InlineData("sdk-disabled", "01", "same")]
    [InlineData("none", null, "new")]
    public async Task Disabled_Traces_Propagate_As_A_No_Op_Tracer(string how, string? inboundFlags, string? expected)
    {
        // Fix round 1 (MEDIUM 3, LOW 6): Go and Python install a no-op tracer for disabled traces and
        // under OTEL_SDK_DISABLED: a root carries no context, a continued trace is passed on unchanged.
        // OTEL_TRACES_EXPORTER=none keeps an SDK provider in Go and Python: a root starts a sampled trace.
        // The logging providers are cleared, so under the disabled SDK only the helper's continuation
        // listener makes ASP.NET Core create a request activity.
        Action<TelemetryOptions>? configure = null;
        switch (how)
        {
            case "disabled-env": Set("OTEL_HELPER_DISABLED_SIGNALS", "traces"); break;
            case "disabled-code": configure = o => o.DisabledSignals = "traces"; break;
            case "sdk-disabled": Set("OTEL_SDK_DISABLED", "true"); break;
            default: Set("OTEL_TRACES_EXPORTER", "none"); break;
        }

        var inbound = inboundFlags is null
            ? new Dictionary<string, string>()
            : new Dictionary<string, string> { ["traceparent"] = $"00-{TraceId.ToHexString()}-{SpanId.ToHexString()}-{inboundFlags}", ["tracestate"] = "vendor=1" };
        var run = await RunWebAsync(inbound, configure);
        var sent = run.Outbound.Where(h => h.Name == "traceparent").Select(h => h.Value).ToList();
        switch (expected)
        {
            case null:
                Assert.Empty(sent);
                Assert.DoesNotContain(run.Outbound, h => h.Name == "tracestate");
                break;
            case "same":
                Assert.Equal(inbound["traceparent"], Assert.Single(sent));
                Assert.Equal("vendor=1", run.Outbound.Single(h => h.Name == "tracestate").Value);
                break;
            default:
                Assert.DoesNotContain(TraceId.ToHexString(), Assert.Single(sent));
                Assert.EndsWith("-01", sent[0]);
                break;
        }
    }

    [Fact]
    public async Task Disabled_Traces_Root_With_Default_Logging_Sends_No_Context()
    {
        // With the default logging providers ASP.NET Core creates a plain request activity for every
        // request; a root still carries nothing on.
        Set("OTEL_HELPER_DISABLED_SIGNALS", "traces");
        var diagnostics = new TestDiagnostics();
        var run = await RunWebAsync(new Dictionary<string, string>(), keepLogging: true, diagnostics: diagnostics);
        Assert.DoesNotContain(run.Outbound, h => h.Name == "traceparent");

        // Fix round 2: the API's "invalid context" injection line is expected here, not forwarded.
        Assert.DoesNotContain(diagnostics.Lines, l => l.Contains("Failed to inject activity context", StringComparison.Ordinal));
    }

    [Theory]
    [InlineData(false)]
    [InlineData(true)]
    public void Disabled_Traces_Sampler_Does_Not_Sample_Roots(bool disabled)
    {
        // Traces disabled: ParentBased(root: AlwaysOff) on the instrumentation sources; a remote parent's
        // flag is followed. OTEL_TRACES_EXPORTER=none keeps the resolved default (roots sampled).
        Set(disabled ? "OTEL_HELPER_DISABLED_SIGNALS" : "OTEL_TRACES_EXPORTER", disabled ? "traces" : "none");
        using var provider = TestPipelines.Build(o => { TestDiagnostics.Identified(o); o.ServiceName = "c10-disabled-sampler"; o.PrometheusMetricsPort = 0; });
        using var http = new ActivitySource("System.Net.Http");
        using (var root = http.StartActivity("root", ActivityKind.Client, default(ActivityContext)))
        {
            Assert.Equal(!disabled, root?.Recorded ?? false);
        }

        using var continued = http.StartActivity("continued", ActivityKind.Client, Remote(true));
        Assert.True(continued?.Recorded);
    }

    [Fact]
    public void Pass_Through_Injection_Uses_The_Local_Root_Remote_Parent()
    {
        using var listener = RecordAll("c10-pass");
        using var source = new ActivitySource("c10-pass");
        var propagator = PropagatorSetup.Create(new[] { "tracecontext", "baggage" }, passThroughOnly: true);
        using (var root = source.StartActivity("root", ActivityKind.Server, default(ActivityContext)))
        {
            Assert.Empty(InjectCurrent(propagator, root!));
        }

        using var server = source.StartActivity("server", ActivityKind.Server, Remote(true, "vendor=1"));
        using var child = source.StartActivity("child");
        Assert.Equal(new[] { "traceparent=" + InboundTraceparent, "tracestate=vendor=1" }, InjectCurrent(propagator, child!));

        // An in-process parent given as an explicit context is not "what arrived" (fix round 2).
        Activity.Current = null;
        using (var local = source.StartActivity("local", ActivityKind.Internal, default(ActivityContext)))
        {
            Activity.Current = null;
            using var explicitChild = source.StartActivity("explicit", ActivityKind.Client, local!.Context);
            Assert.Empty(InjectCurrent(propagator, explicitChild!));
        }

        // A context other than the current activity's is injected as given.
        var other = new Dictionary<string, string>();
        propagator.Inject(new PropagationContext(new ActivityContext(ActivityTraceId.CreateRandom(), ActivitySpanId.CreateRandom(), ActivityTraceFlags.Recorded), default), other, (m, k, v) => m[k] = v);
        Assert.Contains("traceparent", other.Keys);
    }

    private static string[] InjectCurrent(TextMapPropagator propagator, Activity current)
    {
        var carrier = new Dictionary<string, string>();
        propagator.Inject(new PropagationContext(current.Context, default), carrier, (m, k, v) => m[k] = v);
        return carrier.OrderBy(kv => kv.Key, StringComparer.Ordinal).Select(kv => kv.Key + "=" + kv.Value).ToArray();
    }

    private static async Task<IReadOnlyList<(string Name, string Value)>> SendCapturedAsync(HttpClient? existing = null)
    {
        using var capture = new HeaderCapture();
        using var created = existing is null ? new HttpClient() : null;
        using var response = await (existing ?? created!).GetAsync($"http://127.0.0.1:{capture.Port}/capture");
        return capture.Headers();
    }

    /// <summary>Accepts one HTTP/1.1 request on loopback, answers 200 and keeps its header lines.</summary>
    private sealed class HeaderCapture : IDisposable
    {
        private readonly TcpListener _listener = new(IPAddress.Loopback, 0);
        private readonly Task<string> _request;

        public HeaderCapture()
        {
            _listener.Start();
            Port = ((IPEndPoint)_listener.LocalEndpoint).Port;
            _request = Task.Run(async () =>
            {
                using var socket = await _listener.AcceptTcpClientAsync();
                var stream = socket.GetStream();
                var buffer = new byte[16384];
                var text = new StringBuilder();
                while (!text.ToString().Contains("\r\n\r\n", StringComparison.Ordinal))
                {
                    var read = await stream.ReadAsync(buffer);
                    if (read == 0)
                    {
                        break;
                    }

                    text.Append(Encoding.ASCII.GetString(buffer, 0, read));
                }

                var reply = Encoding.ASCII.GetBytes("HTTP/1.1 200 OK\r\nContent-Length: 0\r\nConnection: close\r\n\r\n");
                await stream.WriteAsync(reply);
                return text.ToString();
            });
        }

        public int Port { get; }

        public IReadOnlyList<(string Name, string Value)> Headers()
        {
            Assert.True(_request.Wait(TimeSpan.FromSeconds(10)), "no request reached the capture listener");
            return _request.Result.Split("\r\n").Skip(1)
                .Where(l => l.Contains(':'))
                .Select(l => (l[..l.IndexOf(':')].Trim().ToLowerInvariant(), l[(l.IndexOf(':') + 1)..].Trim()))
                .ToList();
        }

        public void Dispose() => _listener.Stop();
    }

    // ── debug marker (N2) ─────────────────────────────────────────────────

    private static ActivityListener RecordAll(string name)
    {
        var listener = new ActivityListener
        {
            ShouldListenTo = s => s.Name == name,
            Sample = (ref ActivityCreationOptions<ActivityContext> _) => ActivitySamplingResult.AllDataAndRecorded,
        };
        ActivitySource.AddActivityListener(listener);
        return listener;
    }

    [Fact]
    public void Debug_Marks_A_Root_Without_Touching_The_TraceState()
    {
        using var listener = RecordAll("c10-debug-root");
        using var source = new ActivitySource("c10-debug-root");
        using var root = source.StartActivity("root", ActivityKind.Internal, default(ActivityContext));
        new DebugMarkerProcessor().OnStart(root!);
        Assert.Equal("true", root!.GetTagItem("debug"));
        Assert.Null(root.TraceStateString);
    }

    [Fact]
    public void Debug_Marks_A_Remote_Parent_Entry_Span_And_Keeps_Its_TraceState_Once()
    {
        // 0.2.x prepended debug=true to the tracestate: debug=true,debug=true under such a parent.
        using var listener = RecordAll("c10-debug-remote");
        using var source = new ActivitySource("c10-debug-remote");
        using var entry = source.StartActivity("entry", ActivityKind.Server, Remote(true, "vendor=1"));
        new DebugMarkerProcessor().OnStart(entry!);
        Assert.Equal("true", entry!.GetTagItem("debug"));
        Assert.Equal("vendor=1", entry.TraceStateString);
    }

    [Fact]
    public void Debug_Does_Not_Mark_An_In_Process_Child()
    {
        using var listener = RecordAll("c10-debug-child");
        using var source = new ActivitySource("c10-debug-child");
        using var parent = source.StartActivity("parent", ActivityKind.Internal, default(ActivityContext));
        using var child = source.StartActivity("child");
        new DebugMarkerProcessor().OnStart(child!);
        Assert.Null(child!.GetTagItem("debug"));
    }

    /// <summary>DN6: an explicit-context in-process child is not an entry span.</summary>
    [Fact]
    public void Debug_Does_Not_Mark_A_Child_Started_From_An_Explicit_Local_Context()
    {
        // As the Redis instrumentation does: Parent is null, the parent is local (N2).
        using var listener = RecordAll("c10-debug-explicit");
        using var source = new ActivitySource("c10-debug-explicit");
        using var parent = source.StartActivity("parent", ActivityKind.Internal, default(ActivityContext));
        Activity.Current = null;
        using var child = source.StartActivity("child", ActivityKind.Client, parent!.Context);
        Assert.Null(child!.Parent);
        Assert.False(child.HasRemoteParent);
        new DebugMarkerProcessor().OnStart(child);
        Assert.Null(child.GetTagItem("debug"));
    }

    [Fact]
    public void Debug_Is_Installed_Only_In_Debug_Mode()
    {
        Set("OTEL_HELPER_DEBUG_LEVEL", "true");
        using (var provider = TestPipelines.Build(o => { TestDiagnostics.Identified(o); o.ServiceName = "c10-debug-on"; o.PrometheusMetricsPort = 0; }))
        using (var source = new ActivitySource("c10-debug-on"))
        using (var span = source.StartActivity("root", ActivityKind.Internal, default(ActivityContext)))
        {
            Assert.Equal("true", span!.GetTagItem("debug"));
        }

        Set("OTEL_HELPER_DEBUG_LEVEL", null);
        using (var provider = TestPipelines.Build(o => { TestDiagnostics.Identified(o); o.ServiceName = "c10-debug-off"; o.PrometheusMetricsPort = 0; }))
        using (var source = new ActivitySource("c10-debug-off"))
        using (var span = source.StartActivity("root", ActivityKind.Internal, Remote(true, "debug=true")))
        {
            // R9: never derived from inbound tracestate.
            Assert.Null(span!.GetTagItem("debug"));
        }
    }

    // ── StartRootActivity (N3) ────────────────────────────────────────────

    /// <summary>DN6: the previous <see cref="Activity.Current"/> is current again after the root.</summary>
    [Fact]
    public void StartRootActivity_Restores_The_Previous_Activity_When_Disposed()
    {
        using var listener = RecordAll("c10-root");
        using var source = new ActivitySource("c10-root");
        using var outer = source.StartActivity("outer");
        Assert.Same(outer, Activity.Current);

        var root = source.StartRootActivity("root");
        Assert.NotNull(root);
        Assert.Same(root, Activity.Current);
        Assert.Equal(default, root!.ParentSpanId);
        Assert.NotEqual(outer!.TraceId, root.TraceId);

        root.Dispose();
        Assert.Same(outer, Activity.Current);
    }

    [Fact]
    public void StartRootActivity_Without_A_Previous_Activity_Leaves_None()
    {
        using var listener = RecordAll("c10-root-none");
        using var source = new ActivitySource("c10-root-none");
        Activity.Current = null;
        var root = source.StartRootActivity("root");
        Assert.Same(root, Activity.Current);
        root!.Dispose();
        Assert.Null(Activity.Current);
    }

    [Fact]
    public void StartRootActivity_That_Creates_Nothing_Leaves_The_Current_Activity()
    {
        using var unlistened = new ActivitySource("c10-root-unlistened");
        using var outer = new Activity("outer").Start();
        Assert.Null(unlistened.StartRootActivity("root"));
        Assert.Same(outer, Activity.Current);
    }

    [Fact]
    public void StartRootActivity_Restore_Needs_The_Runtime_Field()
    {
        using var root = new Activity("root");
        using var previous = new Activity("previous");
        Assert.False(ActivitySourceExtensions.TrySetPrevious(root, previous, null));
        Assert.False(ActivitySourceExtensions.TrySetPrevious(root, previous, typeof(Activity).GetField("_id", System.Reflection.BindingFlags.Instance | System.Reflection.BindingFlags.NonPublic)));
    }
}
