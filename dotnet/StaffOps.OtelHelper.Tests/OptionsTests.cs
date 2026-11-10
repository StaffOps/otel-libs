using StaffOps.OtelHelper;
using Xunit;

namespace StaffOps.OtelHelper.Tests;

[Collection("EnvVarTests")]
public class OptionsTests
{
    private static ResolvedTelemetry CreateResolved(Action<TelemetryOptions>? configure = null)
        => TestDiagnostics.ResolveQuiet(configure);

    [Fact]
    public void Default_ServiceName_Is_MyService()
    {
        var opts = CreateResolved();
        Assert.Equal("my-service", opts.ServiceName);
    }

    [Fact]
    public void Default_CollectorEndpoint_Is_Empty_When_No_EnvVar()
    {
        var opts = CreateResolved();
        Assert.Equal("", opts.OtelCollectorEndpoint);
    }

    [Fact]
    public void Default_DebugLevel_Is_False()
    {
        var opts = CreateResolved();
        Assert.False(opts.DebugLevel);
    }

    [Fact]
    public void New_Options_Are_Not_Set_By_Default()
    {
        var opts = new TelemetryOptions();
        Assert.Null(opts.ServiceName);
        Assert.Null(opts.Environment);
        Assert.Null(opts.OtelCollectorEndpoint);
        Assert.Null(opts.TracesEndpoint);
        Assert.Null(opts.MetricsEndpoint);
        Assert.Null(opts.LogsEndpoint);
        Assert.Null(opts.DebugLevel);
        Assert.Null(opts.ExportTimeoutMs);
        Assert.Null(opts.Sampler);
        Assert.Null(opts.SampleRatio);
        Assert.Null(opts.DisabledSignals);
        Assert.Null(opts.DisabledMetrics);
        Assert.Null(opts.MetricExporters);
        Assert.Null(opts.ExportIntervalMs);
        Assert.Null(opts.PrometheusMetricsPort);
        Assert.Null(opts.OtlpProtocol);
        Assert.Null(opts.Insecure);
        Assert.Null(opts.Compression);
        Assert.Null(opts.HealthPaths);
        Assert.Empty(opts.AdditionalMeters);
        Assert.True(opts.ForwardSdkDiagnostics);
#pragma warning disable CS0618 // the obsolete member is part of the API under test
        Assert.Null(opts.ExtraInstrumentation);
#pragma warning restore CS0618
    }

    [Theory]
    [InlineData("LOCAL", DeploymentEnvironments.LOCAL)]
    [InlineData("DEV", DeploymentEnvironments.DEV)]
    [InlineData("HML", DeploymentEnvironments.HML)]
    [InlineData("PRD", DeploymentEnvironments.PRD)]
    [InlineData("prd", DeploymentEnvironments.PRD)]
    [InlineData("dev", DeploymentEnvironments.DEV)]
    public void ResolveEnvironment_Parses_Correctly(string envValue, string expected)
    {
        System.Environment.SetEnvironmentVariable("ENVIRONMENT", envValue);
        try
        {
            Assert.Equal(expected, CreateResolved().Environment);
        }
        finally
        {
            System.Environment.SetEnvironmentVariable("ENVIRONMENT", null);
        }
    }

    [Theory]
    [InlineData("invalid")]
    [InlineData("staging")]
    [InlineData("")]
    public void ResolveEnvironment_Invalid_Falls_Back_To_LOCAL(string envValue)
    {
        System.Environment.SetEnvironmentVariable("ENVIRONMENT", envValue);
        try
        {
            Assert.Equal(DeploymentEnvironments.LOCAL, CreateResolved().Environment);
        }
        finally
        {
            System.Environment.SetEnvironmentVariable("ENVIRONMENT", null);
        }
    }

    [Fact]
    public void Code_Environment_Is_Kept_As_Written()
    {
        Assert.Equal("staging", CreateResolved(o => o.Environment = "staging").Environment);
    }

    [Theory]
    [InlineData(DeploymentEnvironments.LOCAL, false, Microsoft.Extensions.Logging.LogLevel.Debug)]
    [InlineData(DeploymentEnvironments.DEV, false, Microsoft.Extensions.Logging.LogLevel.Information)]
    [InlineData(DeploymentEnvironments.HML, false, Microsoft.Extensions.Logging.LogLevel.Information)]
    [InlineData(DeploymentEnvironments.PRD, false, Microsoft.Extensions.Logging.LogLevel.Warning)]
    [InlineData("staging", false, Microsoft.Extensions.Logging.LogLevel.Information)]
    [InlineData(null, false, Microsoft.Extensions.Logging.LogLevel.Information)]
    [InlineData(DeploymentEnvironments.PRD, true, Microsoft.Extensions.Logging.LogLevel.Debug)]
    public void GetDefaultLogLevel_By_Environment_Name(string? environment, bool debug, Microsoft.Extensions.Logging.LogLevel expected)
    {
        Assert.Equal(expected, TelemetryOptions.GetDefaultLogLevel(environment, debug));
    }
}
