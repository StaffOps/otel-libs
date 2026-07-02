using OtelHelper;
using Xunit;

namespace OtelHelper.Tests;

public class OptionsTests
{
    private static TelemetryOptions CreateResolved(Action<TelemetryOptions>? configure = null)
    {
        var opts = new TelemetryOptions();
        configure?.Invoke(opts);
        new TelemetryOptionsPostConfigure().PostConfigure(null, opts);
        return opts;
    }

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

    [Theory]
    [InlineData("LOCAL", DeploymentEnvironment.LOCAL)]
    [InlineData("DEV", DeploymentEnvironment.DEV)]
    [InlineData("HML", DeploymentEnvironment.HML)]
    [InlineData("PRD", DeploymentEnvironment.PRD)]
    [InlineData("prd", DeploymentEnvironment.PRD)]
    [InlineData("dev", DeploymentEnvironment.DEV)]
    public void ResolveEnvironment_Parses_Correctly(string envValue, DeploymentEnvironment expected)
    {
        System.Environment.SetEnvironmentVariable("ENVIRONMENT", envValue);
        try
        {
            var opts = new TelemetryOptions();
            new TelemetryOptionsPostConfigure().PostConfigure(null, opts);
            Assert.Equal(expected, opts.Environment);
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
        var normalized = envValue.Replace("-", "_");
        var result = Enum.TryParse<DeploymentEnvironment>(normalized, ignoreCase: true, out _);
        Assert.False(result);
    }
}
