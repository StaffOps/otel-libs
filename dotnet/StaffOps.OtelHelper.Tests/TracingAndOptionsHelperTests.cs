using System;
using System.Collections.Generic;
using System.Diagnostics;
using Microsoft.Extensions.Logging;
using StaffOps.OtelHelper;
using StaffOps.OtelHelper.Tracing;
using Xunit;

namespace StaffOps.OtelHelper.Tests;

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

    private static ResolvedTelemetry WithDisabledSignals(string disabled)
        => TestDiagnostics.ResolveQuiet(o => o.DisabledSignals = disabled);

    [Fact]
    public void IsSignalEnabled_Returns_True_When_DisabledSignals_Empty()
    {
        var opts = WithDisabledSignals("");

        Assert.True(opts.IsSignalEnabled("traces"));
        Assert.True(opts.IsSignalEnabled("metrics"));
        Assert.True(opts.IsSignalEnabled("logs"));
    }

    [Fact]
    public void IsSignalEnabled_Returns_False_When_Signal_Disabled()
    {
        var opts = WithDisabledSignals("metrics,logs");

        Assert.True(opts.IsSignalEnabled("traces"));
        Assert.False(opts.IsSignalEnabled("metrics"));
        Assert.False(opts.IsSignalEnabled("logs"));
    }

    [Fact]
    public void IsSignalEnabled_Is_Case_Insensitive()
    {
        var opts = WithDisabledSignals("TRACES");

        Assert.False(opts.IsSignalEnabled("traces"));
        Assert.False(opts.IsSignalEnabled("Traces"));
        Assert.False(opts.IsSignalEnabled("TRACES"));
    }

    // --- ExtraInstrumentation: obsolete, no effect, warned ---

    [Fact]
    public void ExtraInstrumentation_Code_Value_Has_No_Effect_And_Warns_Once()
    {
        var diagnostics = new TestDiagnostics();
#pragma warning disable CS0618 // the obsolete member is part of the API under test
        diagnostics.Resolve(o => o.ExtraInstrumentation = "SQL,AWS");
        diagnostics.Resolve(o => o.ExtraInstrumentation = "SQL,AWS");
#pragma warning restore CS0618
        Assert.Single(diagnostics.Lines, l => l.Contains("TelemetryOptions.ExtraInstrumentation has no effect"));
    }

    // --- GetDefaultLogLevel ---

    [Fact]
    public void GetDefaultLogLevel_Returns_Correct_Values()
    {
        Assert.Equal(LogLevel.Debug, TelemetryOptions.GetDefaultLogLevel(DeploymentEnvironments.LOCAL));
        Assert.Equal(LogLevel.Information, TelemetryOptions.GetDefaultLogLevel(DeploymentEnvironments.DEV));
        Assert.Equal(LogLevel.Information, TelemetryOptions.GetDefaultLogLevel(DeploymentEnvironments.HML));
        Assert.Equal(LogLevel.Warning, TelemetryOptions.GetDefaultLogLevel(DeploymentEnvironments.PRD));

        // DebugLevel overrides all environments to Debug
        Assert.Equal(LogLevel.Debug, TelemetryOptions.GetDefaultLogLevel(DeploymentEnvironments.PRD, debug: true));
        Assert.Equal(LogLevel.Debug, TelemetryOptions.GetDefaultLogLevel(DeploymentEnvironments.DEV, debug: true));
    }

    // --- Validator (code values only, D-NET6) ---

    private static string? Fail(Action<TelemetryOptions> configure)
    {
        var opts = new TelemetryOptions();
        configure(opts);
        var result = new TelemetryOptionsValidator().Validate(null, opts);
        return result.Failed ? result.FailureMessage : null;
    }

    [Fact]
    public void Validator_Accepts_Defaults_And_Blank_Strings()
    {
        Assert.Null(Fail(_ => { }));
        Assert.Null(Fail(o =>
        {
            o.ServiceName = "";
            o.OtelCollectorEndpoint = "";
            o.OtlpProtocol = " ";
            o.Compression = "";
            o.MetricExporters = "";
        }));
    }

    [Theory]
    [InlineData("http://localhost:4317")]
    [InlineData("https://collector.example.com/otlp")]
    [InlineData("collector:4317")]
    [InlineData("collector")]
    [InlineData("[::1]:4317")]
    public void Validator_Accepts_Http_Urls_And_Schemeless_Endpoints(string endpoint)
    {
        Assert.Null(Fail(o => o.OtelCollectorEndpoint = endpoint));
    }

    [Theory]
    [InlineData("unix:/tmp/parity-secret", "unix:")]
    [InlineData("ftp://collector:21", "ftp://collector:21")]
    [InlineData("http://u:parity-secret@/x", "<unparseable URL>")]
    [InlineData("//collector:4317", "<unparseable URL>")]
    [InlineData("user:parity-secret@collector:4317", "user:")]
    public void Validator_Fails_On_Unsupported_Endpoint_Redacted(string endpoint, string rendered)
    {
        var message = Fail(o => o.OtelCollectorEndpoint = endpoint);
        Assert.NotNull(message);
        Assert.Contains("TelemetryOptions.OtelCollectorEndpoint", message);
        Assert.Contains(rendered, message);
        Assert.DoesNotContain("parity-secret", message);
    }

    [Theory]
    [InlineData("collector:4318")]
    [InlineData("unix:/x")]
    public void Validator_Per_Signal_Endpoints_Need_An_Http_Url(string endpoint)
    {
        Assert.Contains("TelemetryOptions.TracesEndpoint", Fail(o => o.TracesEndpoint = endpoint));
        Assert.Contains("TelemetryOptions.MetricsEndpoint", Fail(o => o.MetricsEndpoint = endpoint));
        Assert.Contains("TelemetryOptions.LogsEndpoint", Fail(o => o.LogsEndpoint = endpoint));
    }

    [Theory]
    [InlineData(0)]
    [InlineData(86_400_001)]
    [InlineData(-1)]
    public void Validator_Fails_On_Timeout_Out_Of_Range(int timeout)
    {
        var message = Fail(o => o.ExportTimeoutMs = timeout);
        Assert.Contains("TelemetryOptions.ExportTimeoutMs", message);
        Assert.Contains("86400000", message);
    }

    [Theory]
    [InlineData(0L)]
    [InlineData(2_147_483_648L)]
    public void Validator_Fails_On_Interval_Out_Of_Range(long interval)
    {
        var message = Fail(o => o.ExportIntervalMs = interval);
        Assert.Contains("TelemetryOptions.ExportIntervalMs", message);
        Assert.Contains("2147483647", message);
    }

    [Fact]
    public void Validator_Accepts_Interval_And_Timeout_Bounds()
    {
        Assert.Null(Fail(o => { o.ExportIntervalMs = int.MaxValue; o.ExportTimeoutMs = 86_400_000; }));
        Assert.Null(Fail(o => { o.ExportIntervalMs = 1; o.ExportTimeoutMs = 1; }));
    }

    [Theory]
    [InlineData(-1)]
    [InlineData(65_536)]
    public void Validator_Fails_On_Port_Out_Of_Range(int port)
    {
        Assert.Contains("TelemetryOptions.PrometheusMetricsPort", Fail(o => o.PrometheusMetricsPort = port));
    }

    [Theory]
    [InlineData(1.5)]
    [InlineData(-0.1)]
    [InlineData(double.NaN)]
    public void Validator_Fails_On_SampleRatio_Out_Of_Range(double ratio)
    {
        Assert.Contains("TelemetryOptions.SampleRatio", Fail(o => o.SampleRatio = ratio));
    }

    [Fact]
    public void Validator_Fails_On_Unknown_Protocol_And_Compression_Naming_The_Option()
    {
        Assert.Contains("TelemetryOptions.OtlpProtocol", Fail(o => o.OtlpProtocol = "http/json"));
        var compression = Fail(o => o.Compression = "zstd\nforged");
        Assert.Contains("TelemetryOptions.Compression", compression);
        Assert.DoesNotContain("\n", compression);
    }

    [Fact]
    public void Validator_MetricExporters_Code_Errors_Name_The_Option()
    {
        var unknown = Fail(o => o.MetricExporters = "logging");
        Assert.Contains("TelemetryOptions.MetricExporters", unknown);
        Assert.DoesNotContain("OTEL_METRICS_EXPORTER", unknown);
        Assert.Contains("cannot be combined", Fail(o => o.MetricExporters = "none,prometheus"));
        Assert.Contains("requires an endpoint", Fail(o => o.MetricExporters = "otlp"));
        Assert.Null(Fail(o => { o.MetricExporters = "otlp"; o.MetricsEndpoint = "http://c:4318/v1/metrics"; }));
    }

    [Fact]
    public void Validator_Reports_Every_Failure()
    {
        var message = Fail(o => { o.ExportTimeoutMs = 0; o.PrometheusMetricsPort = -1; });
        Assert.Contains("ExportTimeoutMs", message);
        Assert.Contains("PrometheusMetricsPort", message);
    }
}
