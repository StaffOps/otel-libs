using System.Diagnostics;
using OpenTelemetry;
using OpenTelemetry.Trace;
using StaffOps.OtelHelper;
using StaffOps.OtelHelper.Tracing;
using Xunit;

namespace StaffOps.OtelHelper.Tests;

/// <summary>
/// US-2: OTEL_TRACES_SAMPLER (standard SDK var) must win over OTEL_HELPER_SAMPLE_RATIO.
/// Cross-language parity: python/tests/test_sampler_precedence.py and the
/// Test_Sampler_* cases in go/metrics_contract_test.go assert the same rules.
///
/// Verified empirically (not just by reading the SDK's source strings) that
/// OpenTelemetry .NET's TracerProviderBuilder reads OTEL_TRACES_SAMPLER /
/// OTEL_TRACES_SAMPLER_ARG on its own whenever SetSampler(...) is not called,
/// and that an explicit SetSampler(...) call still overrides the env var.
/// </summary>
[Collection("EnvVarTests")]
public class SamplerPrecedenceTests : IDisposable
{
    public SamplerPrecedenceTests() => ClearEnv();
    public void Dispose() => ClearEnv();

    private static void ClearEnv()
    {
        Environment.SetEnvironmentVariable(TelemetryOptions.TracesSamplerEnvVar, null);
        Environment.SetEnvironmentVariable(TelemetryOptions.TracesSamplerEnvVar + "_ARG", null);
        Environment.SetEnvironmentVariable(TelemetryOptions.SampleRatioEnvVar, null);
    }

    private static Action<TelemetryOptions> WithName(Action<TelemetryOptions>? configure) => o =>
    {
        o.ServiceName = "sampler-test";
        configure?.Invoke(o);
    };

    private static ResolvedTelemetry Resolved(Action<TelemetryOptions>? configure = null)
        => TestDiagnostics.ResolveQuiet(WithName(configure));

    private static bool BuildAndCheckSampled(Action<TelemetryOptions>? configure = null)
    {
        using var provider = TestPipelines.Build(WithName(configure));
        using var source = new ActivitySource("sampler-test");
        using var activity = source.StartActivity("probe");
        return activity?.Recorded ?? false;
    }

    [Fact]
    public void HelperVarOnly_AppliesRatio()
    {
        Environment.SetEnvironmentVariable(TelemetryOptions.SampleRatioEnvVar, "0.25");
        var opts = Resolved();

        Assert.IsType<TraceIdRatioBasedSampler>(opts.Sampler);
    }

    [Fact]
    public void StandardVarOnly_Wins()
    {
        Environment.SetEnvironmentVariable(TelemetryOptions.TracesSamplerEnvVar, "always_off");
        var opts = Resolved();

        // The helper sets no sampler (null)...
        Assert.Null(opts.Sampler);
        // ...so the built provider follows the SDK's own env-var reading.
        Assert.False(BuildAndCheckSampled());
    }

    [Fact]
    public void StandardVarBeatsHelperVar()
    {
        Environment.SetEnvironmentVariable(TelemetryOptions.TracesSamplerEnvVar, "always_off");
        Environment.SetEnvironmentVariable(TelemetryOptions.SampleRatioEnvVar, "0.25");
        var opts = Resolved();

        // OTEL_HELPER_SAMPLE_RATIO must be ignored entirely when the standard var is set.
        Assert.Null(opts.Sampler);
        Assert.False(BuildAndCheckSampled());
    }

    [Fact]
    public void ExplicitRatioBeatsStandardVar()
    {
        // Env says always_on; explicit code says ratio 0. Explicit must win,
        // so the span must NOT be sampled.
        Environment.SetEnvironmentVariable(TelemetryOptions.TracesSamplerEnvVar, "always_on");
        Assert.False(BuildAndCheckSampled(o => o.Sampler = new TraceIdRatioBasedSampler(0.0)));
    }

    [Fact]
    public void CodeAlwaysOn_BeatsStandardVar()
    {
        // "Set in code wins, whatever the value" (DN4): an explicit AlwaysOnSampler is not a default.
        Environment.SetEnvironmentVariable(TelemetryOptions.TracesSamplerEnvVar, "always_off");
        Assert.True(BuildAndCheckSampled(o => o.Sampler = new AlwaysOnSampler()));
    }

    [Fact]
    public void CodeSampleRatio_Is_ParentBased_And_Beats_Env()
    {
        Environment.SetEnvironmentVariable(TelemetryOptions.TracesSamplerEnvVar, "always_off");
        Environment.SetEnvironmentVariable(TelemetryOptions.SampleRatioEnvVar, "0");
        Assert.IsType<ParentBasedSampler>(Resolved(o => o.SampleRatio = 1.0).Sampler);
        Assert.True(BuildAndCheckSampled(o => o.SampleRatio = 1.0));
    }

    [Fact]
    public void CodeSampler_Beats_CodeSampleRatio()
    {
        Assert.IsType<AlwaysOffSampler>(Resolved(o => { o.SampleRatio = 1.0; o.Sampler = new AlwaysOffSampler(); }).Sampler);
    }

    [Fact]
    public void NoEnvNoOverride_DefaultsToAlwaysOn()
    {
        var opts = Resolved();

        Assert.IsType<AlwaysOnSampler>(opts.Sampler);
        Assert.True(BuildAndCheckSampled());
    }
}
