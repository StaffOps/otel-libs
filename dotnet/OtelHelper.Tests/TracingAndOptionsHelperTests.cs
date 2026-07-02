using System;
using System.Collections.Generic;
using System.Diagnostics;
using Microsoft.Extensions.Logging;
using OtelHelper;
using OtelHelper.Tracing;
using Xunit;

namespace OtelHelper.Tests;

/// <summary>
/// Tests for tracing helpers (StartRootActivity, DebugTraceStateProcessor) and TelemetryOptions helper methods.
/// </summary>
public class TracingAndOptionsHelperTests
{
    // --- StartRootActivity ---

    [Fact]
    public void StartRootActivity_Creates_Root_Span_Without_Parent()
    {
        using var source = new ActivitySource("test-root-no-parent");
        using var listener = new ActivityListener
        {
            ShouldListenTo = _ => true,
            Sample = (ref ActivityCreationOptions<ActivityContext> _) => ActivitySamplingResult.AllData
        };
        ActivitySource.AddActivityListener(listener);

        // Simulate an existing parent Activity
        using var parent = source.StartActivity("existing-parent");
        Assert.NotNull(Activity.Current);

        // StartRootActivity should clear parent and create a root span
        using var root = source.StartRootActivity("root-operation");
        Assert.NotNull(root);
        Assert.Null(root!.Parent);
        Assert.Equal("root-operation", root.OperationName);
    }

    [Fact]
    public void StartRootActivity_Generates_New_TraceId_Each_Call()
    {
        using var source = new ActivitySource("test-root-traceids");
        using var listener = new ActivityListener
        {
            ShouldListenTo = _ => true,
            Sample = (ref ActivityCreationOptions<ActivityContext> _) => ActivitySamplingResult.AllData
        };
        ActivitySource.AddActivityListener(listener);

        using var span1 = source.StartRootActivity("op1");
        var traceId1 = span1?.TraceId;
        span1?.Stop();

        using var span2 = source.StartRootActivity("op2");
        var traceId2 = span2?.TraceId;

        Assert.NotNull(traceId1);
        Assert.NotNull(traceId2);
        Assert.NotEqual(traceId1, traceId2);
    }

    // --- DebugTraceStateProcessor ---

    [Fact]
    public void DebugMode_Sets_TraceState_And_Attribute_On_Root_Span()
    {
        using var source = new ActivitySource("test-debug-root");
        using var listener = new ActivityListener
        {
            ShouldListenTo = _ => true,
            Sample = (ref ActivityCreationOptions<ActivityContext> _) => ActivitySamplingResult.AllDataAndRecorded
        };
        ActivitySource.AddActivityListener(listener);

        var processor = new DebugTraceStateProcessor();
        using var root = source.StartActivity("root");
        Assert.NotNull(root);

        processor.OnStart(root!);

        Assert.Contains("debug=true", root!.TraceStateString);
        Assert.Equal("true", root.GetTagItem("debug")?.ToString());
    }

    [Fact]
    public void DebugMode_Does_Not_Set_On_Child_Span()
    {
        using var source = new ActivitySource("test-debug-child");
        using var listener = new ActivityListener
        {
            ShouldListenTo = _ => true,
            Sample = (ref ActivityCreationOptions<ActivityContext> _) => ActivitySamplingResult.AllDataAndRecorded
        };
        ActivitySource.AddActivityListener(listener);

        var processor = new DebugTraceStateProcessor();

        using var parent = source.StartActivity("parent");
        using var child = source.StartActivity("child");
        Assert.NotNull(child);

        processor.OnStart(child!);

        // Child has a parent, so processor should NOT set debug attributes
        Assert.Null(child!.GetTagItem("debug"));
        Assert.True(
            string.IsNullOrEmpty(child.TraceStateString) || !child.TraceStateString.Contains("debug=true"),
            "Child span should not have debug=true in tracestate");
    }

    // --- IsSignalEnabled ---

    [Fact]
    public void IsSignalEnabled_Returns_True_When_DisabledSignals_Empty()
    {
        var opts = new TelemetryOptions { DisabledSignals = "" };

        Assert.True(opts.IsSignalEnabled("traces"));
        Assert.True(opts.IsSignalEnabled("metrics"));
        Assert.True(opts.IsSignalEnabled("logs"));
    }

    [Fact]
    public void IsSignalEnabled_Returns_False_When_Signal_Disabled()
    {
        var opts = new TelemetryOptions { DisabledSignals = "metrics,logs" };

        Assert.True(opts.IsSignalEnabled("traces"));
        Assert.False(opts.IsSignalEnabled("metrics"));
        Assert.False(opts.IsSignalEnabled("logs"));
    }

    [Fact]
    public void IsSignalEnabled_Is_Case_Insensitive()
    {
        var opts = new TelemetryOptions { DisabledSignals = "TRACES" };

        Assert.False(opts.IsSignalEnabled("traces"));
        Assert.False(opts.IsSignalEnabled("Traces"));
        Assert.False(opts.IsSignalEnabled("TRACES"));
    }

    // --- HasInstrumentation ---

    [Fact]
    public void HasInstrumentation_Default_Has_SQL()
    {
        var opts = new TelemetryOptions(); // default ExtraInstrumentation = "SQL"

        Assert.True(opts.HasInstrumentation("SQL"));
        Assert.False(opts.HasInstrumentation("AWS"));
        Assert.False(opts.HasInstrumentation("REDIS"));
    }

    [Fact]
    public void HasInstrumentation_Multiple_Values()
    {
        var opts = new TelemetryOptions { ExtraInstrumentation = "SQL,AWS,REDIS" };

        Assert.True(opts.HasInstrumentation("SQL"));
        Assert.True(opts.HasInstrumentation("AWS"));
        Assert.True(opts.HasInstrumentation("REDIS"));
    }

    [Fact]
    public void HasInstrumentation_Case_Insensitive()
    {
        var opts = new TelemetryOptions { ExtraInstrumentation = "sql,aws" };

        Assert.True(opts.HasInstrumentation("SQL"));
        Assert.True(opts.HasInstrumentation("Sql"));
        Assert.True(opts.HasInstrumentation("AWS"));
        Assert.True(opts.HasInstrumentation("aws"));
    }

    [Fact]
    public void HasInstrumentation_Empty_Disables_All()
    {
        var opts = new TelemetryOptions { ExtraInstrumentation = "" };

        Assert.False(opts.HasInstrumentation("SQL"));
        Assert.False(opts.HasInstrumentation("AWS"));
        Assert.False(opts.HasInstrumentation("REDIS"));
    }

    [Fact]
    public void DebugLevel_Enables_All_Instrumentation()
    {
        var opts = new TelemetryOptions
        {
            DebugLevel = true,
            ExtraInstrumentation = "" // normally this would disable all
        };

        // DebugLevel=true bypasses the list check
        Assert.True(opts.HasInstrumentation("SQL"));
        Assert.True(opts.HasInstrumentation("AWS"));
        Assert.True(opts.HasInstrumentation("REDIS"));
        Assert.True(opts.HasInstrumentation("anything"));
    }

    // --- GetDefaultLogLevel ---

    [Fact]
    public void GetDefaultLogLevel_Returns_Correct_Values()
    {
        Assert.Equal(LogLevel.Debug, TelemetryOptions.GetDefaultLogLevel(DeploymentEnvironment.LOCAL));
        Assert.Equal(LogLevel.Information, TelemetryOptions.GetDefaultLogLevel(DeploymentEnvironment.DEV));
        Assert.Equal(LogLevel.Information, TelemetryOptions.GetDefaultLogLevel(DeploymentEnvironment.HML));
        Assert.Equal(LogLevel.Warning, TelemetryOptions.GetDefaultLogLevel(DeploymentEnvironment.PRD));

        // DebugLevel overrides all environments to Debug
        Assert.Equal(LogLevel.Debug, TelemetryOptions.GetDefaultLogLevel(DeploymentEnvironment.PRD, debugLevel: true));
        Assert.Equal(LogLevel.Debug, TelemetryOptions.GetDefaultLogLevel(DeploymentEnvironment.DEV, debugLevel: true));
    }

    // --- Validator ---

    [Fact]
    public void Validator_Fails_On_Empty_ServiceName()
    {
        var validator = new TelemetryOptionsValidator();
        var opts = new TelemetryOptions
        {
            ServiceName = "",
            OtelCollectorEndpoint = "http://localhost:4317"
        };

        var result = validator.Validate(null, opts);

        Assert.True(result.Failed);
        Assert.Contains("ServiceName", result.FailureMessage);
    }

    [Fact]
    public void Validator_Succeeds_On_Empty_Endpoint_Prometheus_Mode()
    {
        var validator = new TelemetryOptionsValidator();
        var opts = new TelemetryOptions
        {
            ServiceName = "test",
            OtelCollectorEndpoint = ""
        };

        var result = validator.Validate(null, opts);

        Assert.False(result.Failed);
    }

    [Fact]
    public void Validator_Fails_On_Invalid_URI()
    {
        var validator = new TelemetryOptionsValidator();
        var opts = new TelemetryOptions
        {
            ServiceName = "test",
            OtelCollectorEndpoint = "not-a-valid-uri"
        };

        var result = validator.Validate(null, opts);

        Assert.True(result.Failed);
        Assert.Contains("OtelCollectorEndpoint", result.FailureMessage);
    }

    [Fact]
    public void Validator_Fails_On_Zero_Timeout()
    {
        var validator = new TelemetryOptionsValidator();
        var opts = new TelemetryOptions
        {
            ServiceName = "test",
            OtelCollectorEndpoint = "http://localhost:4317",
            ExportTimeoutMs = 0
        };

        var result = validator.Validate(null, opts);

        Assert.True(result.Failed);
        Assert.Contains("ExportTimeoutMs", result.FailureMessage);
    }
}
