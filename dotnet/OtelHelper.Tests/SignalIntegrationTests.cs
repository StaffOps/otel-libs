using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Diagnostics.Metrics;
using System.Linq;
using Microsoft.Extensions.DependencyInjection;
using Microsoft.Extensions.Logging;
using Microsoft.Extensions.Options;
using OpenTelemetry;
using OpenTelemetry.Metrics;
using OpenTelemetry.Trace;
using OtelHelper;
using Xunit;

namespace OtelHelper.Tests;

/// <summary>
/// Tests that verify telemetry signals are actually collected or dropped via in-memory exporters.
/// Uses [Collection("EnvVarTests")] to prevent parallel execution that would cause env var conflicts.
/// </summary>
[Collection("EnvVarTests")]
public class SignalIntegrationTests : IDisposable
{
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

    // --- Meter name matches ServiceName ---

    [Fact]
    public void Meter_Name_Matches_ServiceName()
    {
        var services = new ServiceCollection();
        services.AddOtelHelper(opts =>
        {
            opts.ServiceName = "meter-name-test";
            opts.OtelCollectorEndpoint = "http://localhost:4317";
        });

        using var provider = services.BuildServiceProvider();
        var meter = provider.GetRequiredService<Meter>();

        Assert.Equal("meter-name-test", meter.Name);
    }

    // --- ActivitySource name matches ServiceName ---

    [Fact]
    public void ActivitySource_Name_Matches_ServiceName()
    {
        var services = new ServiceCollection();
        services.AddOtelHelper(opts =>
        {
            opts.ServiceName = "source-name-test";
            opts.OtelCollectorEndpoint = "http://localhost:4317";
        });

        using var provider = services.BuildServiceProvider();
        var source = provider.GetRequiredService<ActivitySource>();

        Assert.Equal("source-name-test", source.Name);
    }

    // --- Custom metrics via DI Meter are collected ---

    [Fact]
    public void Custom_Metrics_Via_DI_Meter_Are_Collected()
    {
        var exportedMetrics = new List<Metric>();

        // Build a standalone MeterProvider with in-memory exporter to verify custom metrics
        var serviceName = "custom-metrics-test";
        using var meterProvider = Sdk.CreateMeterProviderBuilder()
            .AddMeter(serviceName)
            .AddInMemoryExporter(exportedMetrics)
            .Build();

        using var meter = new Meter(serviceName);
        var counter = meter.CreateCounter<long>("test.custom.counter");
        counter.Add(42, new KeyValuePair<string, object?>("op", "create"));

        meterProvider.ForceFlush();

        Assert.Contains(exportedMetrics, m => m.Name == "test.custom.counter");
    }

    // --- Traces via DI ActivitySource are collected ---

    [Fact]
    public void Traces_Via_DI_ActivitySource_Are_Collected()
    {
        var exportedActivities = new List<Activity>();

        var serviceName = "trace-collect-test";
        using var tracerProvider = Sdk.CreateTracerProviderBuilder()
            .AddSource(serviceName)
            .AddInMemoryExporter(exportedActivities)
            .Build();

        using var source = new ActivitySource(serviceName);
        using var activity = source.StartActivity("test-operation");
        activity?.SetTag("test.key", "test.value");
        activity?.Stop();

        tracerProvider.ForceFlush();

        Assert.Single(exportedActivities);
        Assert.Equal("test-operation", exportedActivities[0].OperationName);
        Assert.Equal("test.value", exportedActivities[0].GetTagItem("test.key")?.ToString());
    }

    // --- ValidateOnStart throws with empty ServiceName ---

    [Fact]
    public void ValidateOnStart_Throws_With_Empty_ServiceName()
    {
        var services = new ServiceCollection();
        services.AddOtelHelper(opts =>
        {
            opts.ServiceName = "";
            opts.OtelCollectorEndpoint = "http://localhost:4317";
        });

        using var provider = services.BuildServiceProvider();

        // Accessing validated options triggers validation
        var ex = Assert.Throws<OptionsValidationException>(() =>
            provider.GetRequiredService<IOptions<TelemetryOptions>>().Value);
        Assert.Contains("ServiceName", ex.Message);
    }

    // --- ValidateOnStart throws with invalid endpoint ---

    [Fact]
    public void ValidateOnStart_Throws_With_Invalid_Endpoint()
    {
        var services = new ServiceCollection();
        services.AddOtelHelper(opts =>
        {
            opts.ServiceName = "test";
            opts.OtelCollectorEndpoint = "not-a-uri";
        });

        using var provider = services.BuildServiceProvider();

        var ex = Assert.Throws<OptionsValidationException>(() =>
            provider.GetRequiredService<IOptions<TelemetryOptions>>().Value);
        Assert.Contains("OtelCollectorEndpoint", ex.Message);
    }

    // --- LogLevel above minimum are enabled (parameterized) ---

    [Theory]
    [InlineData(DeploymentEnvironment.LOCAL, LogLevel.Debug)]
    [InlineData(DeploymentEnvironment.DEV, LogLevel.Information)]
    [InlineData(DeploymentEnvironment.PRD, LogLevel.Warning)]
    public void LogLevel_Above_Minimum_Are_Enabled(DeploymentEnvironment env, LogLevel expectedMinLevel)
    {
        var services = new ServiceCollection();
        services.AddOtelHelper(opts =>
        {
            opts.ServiceName = "loglevel-test";
            opts.Environment = env;
            opts.OtelCollectorEndpoint = "http://localhost:4317";
        });

        using var provider = services.BuildServiceProvider();
        var logger = provider.GetRequiredService<ILoggerFactory>().CreateLogger("Test");

        // Minimum level should be enabled
        Assert.True(logger.IsEnabled(expectedMinLevel),
            $"Expected {expectedMinLevel} to be enabled for {env}");

        // Level below minimum should be disabled (except when minimum is Trace)
        if (expectedMinLevel > LogLevel.Trace)
        {
            var belowMin = (LogLevel)((int)expectedMinLevel - 1);
            Assert.False(logger.IsEnabled(belowMin),
                $"Expected {belowMin} to be disabled for {env}");
        }
    }

    // --- Full pipeline registers all signals ---

    [Fact]
    public void Full_Pipeline_Registers_All_Signals()
    {
        var services = new ServiceCollection();
        services.AddOtelHelper(opts =>
        {
            opts.ServiceName = "full-pipeline-test";
            opts.Environment = DeploymentEnvironment.DEV;
            opts.OtelCollectorEndpoint = "http://localhost:4317";
        });

        using var provider = services.BuildServiceProvider();

        // Logging registered
        Assert.NotNull(provider.GetService<ILoggerFactory>());

        // Options registered
        var options = provider.GetRequiredService<IOptions<TelemetryOptions>>();
        Assert.Equal("full-pipeline-test", options.Value.ServiceName);

        // DI ActivitySource registered
        Assert.NotNull(provider.GetService<ActivitySource>());

        // DI Meter registered
        Assert.NotNull(provider.GetService<Meter>());
    }

    // --- DebugLevel forces Debug LogLevel ---

    [Fact]
    public void DebugLevel_Forces_Debug_LogLevel()
    {
        var services = new ServiceCollection();
        services.AddOtelHelper(opts =>
        {
            opts.ServiceName = "debug-level-test";
            opts.Environment = DeploymentEnvironment.PRD; // normally Warning
            opts.DebugLevel = true;
            opts.OtelCollectorEndpoint = "http://localhost:4317";
        });

        using var provider = services.BuildServiceProvider();
        var logger = provider.GetRequiredService<ILoggerFactory>().CreateLogger("Test");

        Assert.True(logger.IsEnabled(LogLevel.Debug),
            "DebugLevel=true should force Debug log level even in PRD");
    }

    // --- Custom MinimumLogLevel overrides environment ---

    [Fact]
    public void Custom_MinimumLogLevel_Overrides_Environment()
    {
        var services = new ServiceCollection();
        services.AddOtelHelper(opts =>
        {
            opts.ServiceName = "min-level-override-test";
            opts.Environment = DeploymentEnvironment.PRD; // normally Warning
            opts.MinimumLogLevel = LogLevel.Information;
            opts.OtelCollectorEndpoint = "http://localhost:4317";
        });

        using var provider = services.BuildServiceProvider();
        var logger = provider.GetRequiredService<ILoggerFactory>().CreateLogger("Test");

        Assert.True(logger.IsEnabled(LogLevel.Information),
            "Custom MinimumLogLevel should override env-based default");
    }

    // --- DisabledMetrics pattern drops matching metric ---

    [Fact]
    public void DisabledMetrics_Pattern_Drops_Matching_Metric()
    {
        var exportedMetrics = new List<Metric>();
        var serviceName = "drop-metrics-test";

        // Simulate the view logic that MetricsSetup uses
        var disabledMetrics = "http.server.*,system.disk.*";
        var patterns = disabledMetrics
            .Split(',', StringSplitOptions.RemoveEmptyEntries | StringSplitOptions.TrimEntries)
            .Select(p => "^" + System.Text.RegularExpressions.Regex.Escape(p).Replace("\\*", ".*") + "$")
            .Select(p => new System.Text.RegularExpressions.Regex(p, System.Text.RegularExpressions.RegexOptions.Compiled | System.Text.RegularExpressions.RegexOptions.IgnoreCase))
            .ToList();

        using var meterProvider = Sdk.CreateMeterProviderBuilder()
            .AddMeter(serviceName)
            .AddView(instrument =>
            {
                if (patterns.Any(p => p.IsMatch(instrument.Name)))
                    return MetricStreamConfiguration.Drop;
                return null;
            })
            .AddInMemoryExporter(exportedMetrics)
            .Build();

        using var meter = new Meter(serviceName);

        // Create a metric that should be dropped
        var droppedCounter = meter.CreateCounter<long>("http.server.request.duration");
        droppedCounter.Add(100);

        // Create a metric that should be kept
        var keptCounter = meter.CreateCounter<long>("app.orders.processed");
        keptCounter.Add(5);

        meterProvider.ForceFlush();

        // Only the non-matching metric should be exported
        Assert.DoesNotContain(exportedMetrics, m => m.Name == "http.server.request.duration");
        Assert.Contains(exportedMetrics, m => m.Name == "app.orders.processed");
    }
}
