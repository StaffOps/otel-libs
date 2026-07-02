using System;
using System.Linq;
using Microsoft.Extensions.DependencyInjection;
using Microsoft.Extensions.Hosting;
using Microsoft.Extensions.Logging;
using OtelHelper;
using OtelHelper.Profiling;
using Xunit;

namespace OtelHelper.Tests;

/// <summary>
/// Tests for OtelHelper.Profiling — ProfilingPrerequisites and extension registration.
/// Env var tests use [Collection("EnvVarTests")] to prevent parallel execution.
/// </summary>
[Collection("EnvVarTests")]
public class ProfilingTests : IDisposable
{
    private readonly List<string> _envVarsToClean = new();

    private void SetEnvVar(string key, string? value)
    {
        Environment.SetEnvironmentVariable(key, value);
        _envVarsToClean.Add(key);
    }

    private void ClearAllProfilingEnvVars()
    {
        SetEnvVar(ProfilingPrerequisites.EnableProfilingEnvVar, null);
        SetEnvVar(ProfilingPrerequisites.ProfilingEnabledEnvVar, null);
        SetEnvVar(ProfilingPrerequisites.ServerAddressEnvVar, null);
        SetEnvVar(ProfilingPrerequisites.ApplicationNameEnvVar, null);
    }

    public void Dispose()
    {
        foreach (var key in _envVarsToClean)
            Environment.SetEnvironmentVariable(key, null);
    }

    // --- FromEnvironment: all set → IsSatisfied ---

    [Fact]
    public void ProfilingPrerequisites_FromEnvironment_AllSet_IsSatisfied()
    {
        ClearAllProfilingEnvVars();
        SetEnvVar(ProfilingPrerequisites.EnableProfilingEnvVar, "1");
        SetEnvVar(ProfilingPrerequisites.ProfilingEnabledEnvVar, "1");
        SetEnvVar(ProfilingPrerequisites.ServerAddressEnvVar, "http://pyroscope:4040");

        var prereqs = ProfilingPrerequisites.FromEnvironment();

        Assert.True(prereqs.IsSatisfied);
        Assert.True(prereqs.ClrProfilerEnabled);
        Assert.True(prereqs.ProfilingEnabled);
        Assert.Equal("http://pyroscope:4040", prereqs.ServerAddress);
    }

    // --- Missing server address → not satisfied ---

    [Fact]
    public void ProfilingPrerequisites_Missing_ServerAddress_NotSatisfied()
    {
        ClearAllProfilingEnvVars();
        SetEnvVar(ProfilingPrerequisites.EnableProfilingEnvVar, "1");
        SetEnvVar(ProfilingPrerequisites.ProfilingEnabledEnvVar, "1");
        // ServerAddress not set

        var prereqs = ProfilingPrerequisites.FromEnvironment();

        Assert.False(prereqs.IsSatisfied);
    }

    // --- Missing CLR profiler → not satisfied ---

    [Fact]
    public void ProfilingPrerequisites_Missing_ClrProfiler_NotSatisfied()
    {
        ClearAllProfilingEnvVars();
        // EnableProfiling not set
        SetEnvVar(ProfilingPrerequisites.ProfilingEnabledEnvVar, "1");
        SetEnvVar(ProfilingPrerequisites.ServerAddressEnvVar, "http://pyroscope:4040");

        var prereqs = ProfilingPrerequisites.FromEnvironment();

        Assert.False(prereqs.IsSatisfied);
    }

    // --- DescribeMissing lists missing vars ---

    [Fact]
    public void ProfilingPrerequisites_DescribeMissing_ListsMissingVars()
    {
        ClearAllProfilingEnvVars();
        // Nothing set

        var prereqs = ProfilingPrerequisites.FromEnvironment();
        var description = prereqs.DescribeMissing();

        Assert.NotNull(description);
        Assert.Contains(ProfilingPrerequisites.EnableProfilingEnvVar, description);
        Assert.Contains(ProfilingPrerequisites.ProfilingEnabledEnvVar, description);
        Assert.Contains(ProfilingPrerequisites.ServerAddressEnvVar, description);
    }

    // --- DescribeMissing returns null when satisfied ---

    [Fact]
    public void ProfilingPrerequisites_DescribeMissing_ReturnsNull_When_Satisfied()
    {
        ClearAllProfilingEnvVars();
        SetEnvVar(ProfilingPrerequisites.EnableProfilingEnvVar, "1");
        SetEnvVar(ProfilingPrerequisites.ProfilingEnabledEnvVar, "1");
        SetEnvVar(ProfilingPrerequisites.ServerAddressEnvVar, "http://pyroscope:4040");

        var prereqs = ProfilingPrerequisites.FromEnvironment();
        var description = prereqs.DescribeMissing();

        Assert.Null(description);
    }

    // --- IsTruthy accepts both "1" and "true" ---

    [Theory]
    [InlineData("1")]
    [InlineData("true")]
    [InlineData("True")]
    [InlineData("TRUE")]
    public void ProfilingPrerequisites_Truthy_AcceptsBothOneAndTrue(string value)
    {
        ClearAllProfilingEnvVars();
        SetEnvVar(ProfilingPrerequisites.EnableProfilingEnvVar, value);
        SetEnvVar(ProfilingPrerequisites.ProfilingEnabledEnvVar, "1");
        SetEnvVar(ProfilingPrerequisites.ServerAddressEnvVar, "http://pyroscope:4040");

        var prereqs = ProfilingPrerequisites.FromEnvironment();

        Assert.True(prereqs.ClrProfilerEnabled);
        Assert.True(prereqs.IsSatisfied);
    }

    // --- AddOtelHelperProfiling registers warning hosted service when prerequisites missing ---

    [Fact]
    public void AddOtelHelperProfiling_Registers_HostedService_When_Prerequisites_Missing()
    {
        ClearAllProfilingEnvVars();
        // No env vars → prerequisites not satisfied

        var services = new ServiceCollection();
        services.AddLogging();
        services.AddOtelHelper(opts =>
        {
            opts.ServiceName = "profiling-test";
            opts.OtelCollectorEndpoint = "http://localhost:4317";
        });

        services.AddOtelHelperProfiling();

        using var provider = services.BuildServiceProvider();
        var hostedServices = provider.GetServices<IHostedService>();
        Assert.Contains(hostedServices, s => s is ProfilingPrerequisiteWarning);
    }

    // --- ProfilingPrerequisiteWarning logs warning on start ---

    [Fact]
    public async Task ProfilingPrerequisiteWarning_LogsWarning_OnStart()
    {
        var loggerFactory = LoggerFactory.Create(builder => builder.AddDebug());
        var warning = new ProfilingPrerequisiteWarning(loggerFactory, "CORECLR_ENABLE_PROFILING=1");

        // Should complete without exception
        await warning.StartAsync(CancellationToken.None);
        await warning.StopAsync(CancellationToken.None);
    }

    // --- ProfilingPrerequisites_Missing_ProfilingEnabled_NotSatisfied ---

    [Fact]
    public void ProfilingPrerequisites_Missing_ProfilingEnabled_NotSatisfied()
    {
        ClearAllProfilingEnvVars();
        SetEnvVar(ProfilingPrerequisites.EnableProfilingEnvVar, "1");
        // ProfilingEnabled not set
        SetEnvVar(ProfilingPrerequisites.ServerAddressEnvVar, "http://pyroscope:4040");

        var prereqs = ProfilingPrerequisites.FromEnvironment();

        Assert.False(prereqs.IsSatisfied);
    }

    // --- ApplicationName is read from env ---

    [Fact]
    public void ProfilingPrerequisites_ApplicationName_IsRead()
    {
        ClearAllProfilingEnvVars();
        SetEnvVar(ProfilingPrerequisites.EnableProfilingEnvVar, "1");
        SetEnvVar(ProfilingPrerequisites.ProfilingEnabledEnvVar, "1");
        SetEnvVar(ProfilingPrerequisites.ServerAddressEnvVar, "http://pyroscope:4040");
        SetEnvVar(ProfilingPrerequisites.ApplicationNameEnvVar, "my-app");

        var prereqs = ProfilingPrerequisites.FromEnvironment();

        Assert.Equal("my-app", prereqs.ApplicationName);
    }

    // --- IsSatisfied does not require ApplicationName ---

    [Fact]
    public void ProfilingPrerequisites_IsSatisfied_Without_ApplicationName()
    {
        ClearAllProfilingEnvVars();
        SetEnvVar(ProfilingPrerequisites.EnableProfilingEnvVar, "1");
        SetEnvVar(ProfilingPrerequisites.ProfilingEnabledEnvVar, "1");
        SetEnvVar(ProfilingPrerequisites.ServerAddressEnvVar, "http://pyroscope:4040");
        // No ApplicationName

        var prereqs = ProfilingPrerequisites.FromEnvironment();

        Assert.True(prereqs.IsSatisfied);
        Assert.Null(prereqs.ApplicationName);
    }

    // --- IProfilingProvider registered in DI ---

    [Fact]
    public void AddOtelHelperProfiling_Registers_IProfilingProvider()
    {
        ClearAllProfilingEnvVars();

        var services = new ServiceCollection();
        services.AddLogging();
        services.AddOtelHelper(opts =>
        {
            opts.ServiceName = "profiling-provider-test";
            opts.OtelCollectorEndpoint = "http://localhost:4317";
        });

        services.AddOtelHelperProfiling();

        using var provider = services.BuildServiceProvider();
        var profilingProvider = provider.GetService<IProfilingProvider>();
        Assert.NotNull(profilingProvider);
    }

    // --- IsTruthy rejects invalid values ---

    [Theory]
    [InlineData("0")]
    [InlineData("false")]
    [InlineData("")]
    [InlineData("yes")]
    [InlineData("on")]
    public void ProfilingPrerequisites_NotTruthy_Rejects_InvalidValues(string value)
    {
        ClearAllProfilingEnvVars();
        SetEnvVar(ProfilingPrerequisites.EnableProfilingEnvVar, value);
        SetEnvVar(ProfilingPrerequisites.ProfilingEnabledEnvVar, "1");
        SetEnvVar(ProfilingPrerequisites.ServerAddressEnvVar, "http://pyroscope:4040");

        var prereqs = ProfilingPrerequisites.FromEnvironment();

        Assert.False(prereqs.ClrProfilerEnabled);
        Assert.False(prereqs.IsSatisfied);
    }

    // --- DescribeMissing includes only missing vars ---

    [Fact]
    public void ProfilingPrerequisites_DescribeMissing_OnlyListsMissing()
    {
        ClearAllProfilingEnvVars();
        SetEnvVar(ProfilingPrerequisites.EnableProfilingEnvVar, "1");
        // ProfilingEnabled and ServerAddress not set

        var prereqs = ProfilingPrerequisites.FromEnvironment();
        var description = prereqs.DescribeMissing();

        Assert.NotNull(description);
        // ClrProfiler IS set, so should not appear
        Assert.DoesNotContain("CORECLR_ENABLE_PROFILING=1", description);
        // These are missing
        Assert.Contains(ProfilingPrerequisites.ProfilingEnabledEnvVar, description);
        Assert.Contains(ProfilingPrerequisites.ServerAddressEnvVar, description);
    }
}
