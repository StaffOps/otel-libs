using System.Diagnostics;
using OpenTelemetry;
using OpenTelemetry.Trace;
using OtelHelper;
using OtelHelper.Tracing;
using Xunit;

namespace OtelHelper.Tests;

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

    private static TelemetryOptions Resolved(Action<TelemetryOptions>? configure = null)
    {
        var opts = new TelemetryOptions { ServiceName = "sampler-test" };
        configure?.Invoke(opts);
        new TelemetryOptionsPostConfigure().PostConfigure(null, opts);
        return opts;
    }

    private static bool BuildAndCheckSampled(TelemetryOptions opts)
    {
        using var provider = Sdk.CreateTracerProviderBuilder()
            .ConfigureTracing(opts)
            .Build();

        using var source = new ActivitySource(opts.ServiceName);
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

        // Helper stays on the AlwaysOnSampler default (no ratio applied)...
        Assert.IsType<AlwaysOnSampler>(opts.Sampler);
        // ...but the built provider must follow the SDK's own env-var reading
        // (ConfigureTracing skips SetSampler when the standard var is present).
        Assert.False(BuildAndCheckSampled(opts));
    }

    [Fact]
    public void StandardVarBeatsHelperVar()
    {
        Environment.SetEnvironmentVariable(TelemetryOptions.TracesSamplerEnvVar, "always_off");
        Environment.SetEnvironmentVariable(TelemetryOptions.SampleRatioEnvVar, "0.25");
        var opts = Resolved();

        // OTEL_HELPER_SAMPLE_RATIO must be ignored entirely when the standard var is set.
        Assert.IsType<AlwaysOnSampler>(opts.Sampler);
        Assert.False(BuildAndCheckSampled(opts));
    }

    [Fact]
    public void ExplicitRatioBeatsStandardVar()
    {
        // Env says always_on; explicit code says ratio 0. Explicit must win,
        // so the span must NOT be sampled.
        Environment.SetEnvironmentVariable(TelemetryOptions.TracesSamplerEnvVar, "always_on");
        var opts = Resolved(o => o.Sampler = new TraceIdRatioBasedSampler(0.0));

        Assert.False(BuildAndCheckSampled(opts));
    }

    [Fact]
    public void NoEnvNoOverride_DefaultsToAlwaysOn()
    {
        var opts = Resolved();

        Assert.IsType<AlwaysOnSampler>(opts.Sampler);
        Assert.True(BuildAndCheckSampled(opts));
    }
}
