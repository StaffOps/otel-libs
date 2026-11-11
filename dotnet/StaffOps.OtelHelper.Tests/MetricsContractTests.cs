using System.Diagnostics.Metrics;
using Microsoft.Extensions.DependencyInjection;
using OpenTelemetry;
using OpenTelemetry.Metrics;
using StaffOps.OtelHelper;
using StaffOps.OtelHelper.Metrics;
using Xunit;

namespace StaffOps.OtelHelper.Tests;

/// <summary>
/// Contract tests for OTEL_METRICS_EXPORTER / OTEL_METRIC_EXPORT_INTERVAL (US-1, US-2, US-6).
/// Cross-language parity: python/tests/test_metrics_contract.py and
/// go/metrics_contract_test.go assert the same table.
/// </summary>
[Collection("EnvVarTests")]
[ResetGlobalPropagation]
public class MetricsContractTests : IDisposable
{
    private static readonly string[] ContractEnvVars =
    {
        TelemetryOptions.MetricsExporterEnvVar,
        TelemetryOptions.MetricExportIntervalEnvVar,
        TelemetryOptions.CollectorEndpointEnvVar,
        TelemetryOptions.TracesSamplerEnvVar,
        TelemetryOptions.SampleRatioEnvVar,
    };

    public MetricsContractTests() => ClearEnv();
    public void Dispose() => ClearEnv();

    private static void ClearEnv()
    {
        foreach (var v in ContractEnvVars)
            Environment.SetEnvironmentVariable(v, null);
    }

    private static ResolvedTelemetry Resolved(Action<TelemetryOptions>? configure = null)
        => TestDiagnostics.ResolveQuiet(configure);

    private static string? Validate(Action<TelemetryOptions>? configure = null)
    {
        var opts = new TelemetryOptions();
        configure?.Invoke(opts);
        var result = new TelemetryOptionsValidator().Validate(null, opts);
        return result.Failed ? result.FailureMessage : null;
    }

    /// <summary>A service provider with the helper set up, its MeterProvider built.</summary>
    private static ServiceProvider BuildMeterPipeline(Action<TelemetryOptions> configure)
    {
        var services = new ServiceCollection();
        services.AddSingleton<StaffOps.OtelHelper.Diagnostics.IOtelHelperDiagnostics>(new TestDiagnostics().Sink);
        services.AddOtelHelper(configure);
        var provider = services.BuildServiceProvider();
        provider.GetRequiredService<MeterProvider>();
        return provider;
    }

    // --- US-1: exporter resolution table ---

    [Fact]
    public void Unset_WithEndpoint_IsOtlp()
    {
        var opts = Resolved(o => o.OtelCollectorEndpoint = "http://collector:4317");
        Assert.Equal(new[] { "otlp" }, opts.ResolvedMetricExporters());
    }

    [Fact]
    public void Unset_WithoutEndpoint_IsPrometheus()
    {
        var opts = Resolved();
        Assert.Equal(new[] { "prometheus" }, opts.ResolvedMetricExporters());
    }

    [Fact]
    public void PrometheusOnly_EvenWithEndpoint()
    {
        Environment.SetEnvironmentVariable(TelemetryOptions.MetricsExporterEnvVar, "prometheus");
        var opts = Resolved(o => o.OtelCollectorEndpoint = "http://collector:4317");
        Assert.Equal(new[] { "prometheus" }, opts.ResolvedMetricExporters());
    }

    [Fact]
    public void DualMode_BothExporters()
    {
        Environment.SetEnvironmentVariable(TelemetryOptions.MetricsExporterEnvVar, "otlp,prometheus");
        var opts = Resolved(o => o.OtelCollectorEndpoint = "http://collector:4317");
        Assert.Equal(new[] { "otlp", "prometheus" }, opts.ResolvedMetricExporters());
    }

    [Fact]
    public void None_DisablesMetrics()
    {
        Environment.SetEnvironmentVariable(TelemetryOptions.MetricsExporterEnvVar, "none");
        var opts = Resolved(o => o.OtelCollectorEndpoint = "http://collector:4317");
        Assert.Empty(opts.ResolvedMetricExporters());
    }

    [Fact]
    public void CaseAndWhitespace_Tolerated()
    {
        Environment.SetEnvironmentVariable(TelemetryOptions.MetricsExporterEnvVar, " OTLP , Prometheus ");
        var opts = Resolved(o => o.OtelCollectorEndpoint = "http://collector:4317");
        Assert.Equal(new[] { "otlp", "prometheus" }, opts.ResolvedMetricExporters());
    }

    [Fact]
    public void ExplicitOption_BeatsEnv()
    {
        Environment.SetEnvironmentVariable(TelemetryOptions.MetricsExporterEnvVar, "otlp");
        var opts = Resolved(o =>
        {
            o.OtelCollectorEndpoint = "http://collector:4317";
            o.MetricExporters = "prometheus";
        });
        Assert.Equal(new[] { "prometheus" }, opts.ResolvedMetricExporters());
    }

    // Env values warn and fall back (D-NET6); the same values set in code fail at start.

    [Fact]
    public void UnknownEnvValue_Warns_And_Is_Ignored()
    {
        Environment.SetEnvironmentVariable(TelemetryOptions.MetricsExporterEnvVar, "statsd,prometheus");
        var diagnostics = new TestDiagnostics();
        var opts = diagnostics.Resolve();
        Assert.Equal(new[] { "prometheus" }, opts.ResolvedMetricExporters());
        Assert.Contains(diagnostics.Lines, l => l.Contains("invalid OTEL_METRICS_EXPORTER") && l.Contains("value=statsd,prometheus"));
    }

    [Fact]
    public void OnlyUnknownEnvValues_Warn_And_Use_The_Default()
    {
        Environment.SetEnvironmentVariable(TelemetryOptions.MetricsExporterEnvVar, "statsd");
        var diagnostics = new TestDiagnostics();
        Assert.Equal(new[] { "prometheus" }, diagnostics.Resolve(TestDiagnostics.Identified).ResolvedMetricExporters());
        Assert.Single(diagnostics.Lines);
    }

    [Fact]
    public void UnknownCodeValue_FailsValidation()
    {
        var failure = Validate(o => o.MetricExporters = "statsd");
        Assert.NotNull(failure);
        Assert.Contains("statsd", failure);
        Assert.Contains("TelemetryOptions.MetricExporters", failure);
        Assert.Contains("Valid values", failure);
    }

    [Fact]
    public void OtlpEnvWithoutEndpoint_Warns_And_Is_Dropped()
    {
        Environment.SetEnvironmentVariable(TelemetryOptions.MetricsExporterEnvVar, "otlp,prometheus");
        var diagnostics = new TestDiagnostics();
        Assert.Equal(new[] { "prometheus" }, diagnostics.Resolve().ResolvedMetricExporters());
        Assert.Contains(diagnostics.Lines, l => l.Contains("'otlp' has no endpoint") && l.Contains("variable=OTEL_METRICS_EXPORTER"));
    }

    [Fact]
    public void OtlpCodeWithoutEndpoint_FailsValidation()
    {
        var failure = Validate(o => o.MetricExporters = "otlp");
        Assert.NotNull(failure);
        Assert.Contains("requires an endpoint", failure);
    }

    [Fact]
    public void OtlpCode_WithEnvEndpoint_IsValid()
    {
        Environment.SetEnvironmentVariable(TelemetryOptions.CollectorEndpointEnvVar, "http://collector:4317");
        try
        {
            Assert.Null(Validate(o => o.MetricExporters = "otlp"));
        }
        finally
        {
            Environment.SetEnvironmentVariable(TelemetryOptions.CollectorEndpointEnvVar, null);
        }
    }

    [Fact]
    public void NoneCombinedEnv_Warns_And_Disables()
    {
        Environment.SetEnvironmentVariable(TelemetryOptions.MetricsExporterEnvVar, "none,prometheus");
        var diagnostics = new TestDiagnostics();
        Assert.Empty(diagnostics.Resolve().ResolvedMetricExporters());
        Assert.Contains(diagnostics.Lines, l => l.Contains("'none' combined"));
    }

    [Fact]
    public void NoneCombinedCode_FailsValidation()
    {
        var failure = Validate(o => o.MetricExporters = "none,prometheus");
        Assert.NotNull(failure);
        Assert.Contains("cannot be combined", failure);
    }

    // --- US-2: OTEL_METRIC_EXPORT_INTERVAL precedence ---

    [Fact]
    public void Interval_DefaultIs30s()
    {
        Assert.Equal(30_000, Resolved().ExportIntervalMs);
    }

    [Fact]
    public void Interval_EnvHonored()
    {
        Environment.SetEnvironmentVariable(TelemetryOptions.MetricExportIntervalEnvVar, "5000");
        Assert.Equal(5000, Resolved().ExportIntervalMs);
    }

    [Fact]
    public void Interval_ExplicitBeatsEnv()
    {
        Environment.SetEnvironmentVariable(TelemetryOptions.MetricExportIntervalEnvVar, "5000");
        Assert.Equal(1234, Resolved(o => o.ExportIntervalMs = 1234).ExportIntervalMs);
    }

    [Fact]
    public void Interval_InvalidEnvFallsBack()
    {
        Environment.SetEnvironmentVariable(TelemetryOptions.MetricExportIntervalEnvVar, "not-a-number");
        Assert.Equal(30_000, Resolved().ExportIntervalMs);
    }

    [Fact]
    public void Interval_NonPositive_FailsValidation()
    {
        var failure = Validate(o => o.ExportIntervalMs = 0);
        Assert.NotNull(failure);
        Assert.Contains("ExportIntervalMs", failure);
    }

    // --- US-4: port 0 = listener disabled passes validation ---

    [Fact]
    public void PortZero_IsValid()
    {
        Assert.Null(Validate(o => o.PrometheusMetricsPort = 0));
        Assert.False(Resolved(o => o.PrometheusMetricsPort = 0).PrometheusListenerActive);
    }

    // --- US-1/US-6: dual-mode pipeline — same counter reaches both readers ---

    [Fact]
    public async Task DualMode_CounterVisible_InInMemoryAndPrometheusReaders()
    {
        // Single MeterProvider, two readers: an in-memory reader standing in for
        // the OTLP periodic reader, plus the Prometheus HTTP listener on a free
        // port. The same instrument must be visible in both without double counting.
        var port = TestPorts.FreeTcpPort();
        var exportedItems = new List<Metric>();
        using var meter = new Meter("contract-test");

        using var provider = Sdk.CreateMeterProviderBuilder()
            .AddMeter("contract-test")
            .AddInMemoryExporter(exportedItems)
            .AddPrometheusHttpListener(o => o.Port = port)
            .Build();

        var counter = meter.CreateCounter<long>("dual_mode_hits");
        counter.Add(7);

        provider.ForceFlush();
        Assert.Contains(exportedItems, m => m.Name == "dual_mode_hits");

        using var http = new HttpClient();
        var scrape = await http.GetStringAsync($"http://localhost:{port}/metrics");
        Assert.Contains("dual_mode_hits", scrape);
        Assert.Contains("7", scrape);
    }

    [Fact]
    public async Task ConfigureMetrics_PrometheusOnly_WithEndpoint_Builds()
    {
        // prometheus-only with an endpoint set must not register the OTLP exporter;
        // builder must build cleanly with only the listener (on a free port).
        var port = TestPorts.FreeTcpPort();
        using var provider = BuildMeterPipeline(o =>
        {
            o.ServiceName = "prom-svc";
            o.OtelCollectorEndpoint = "http://localhost:4317";
            o.MetricExporters = "prometheus";
            o.PrometheusMetricsPort = port;
        });

        using var http = new HttpClient();
        var response = await http.GetAsync($"http://localhost:{port}/metrics");
        Assert.Equal(System.Net.HttpStatusCode.OK, response.StatusCode);
    }

    [Fact]
    public void ConfigureMetrics_PortZero_SkipsListener()
    {
        // Builds without binding anything — the reader is registered, with no prefix (D-NET3).
        using var provider = BuildMeterPipeline(o =>
        {
            o.ServiceName = "noport-svc";
            o.MetricExporters = "prometheus";
            o.PrometheusMetricsPort = 0;
        });
        Assert.NotNull(provider.GetRequiredService<MeterProvider>());
    }

    [Theory]
    [InlineData("none")]
    [InlineData("otlp")]
    public async Task Inactive_Prometheus_Listener_Binds_Nothing(string exporters)
    {
        // The reader is always registered (D-NET3); without "prometheus" it gets no prefix.
        var port = TestPorts.FreeTcpPort();
        using var provider = BuildMeterPipeline(o =>
        {
            o.ServiceName = "inactive-svc";
            o.OtelCollectorEndpoint = "http://127.0.0.1:1";
            o.MetricExporters = exporters;
            o.PrometheusMetricsPort = port;
        });

        using var http = new HttpClient { Timeout = TimeSpan.FromSeconds(5) };
        await Assert.ThrowsAsync<HttpRequestException>(() => http.GetAsync($"http://localhost:{port}/metrics"));
    }

    [Fact]
    public async Task Late_Configure_Decides_Whether_The_Listener_Binds()
    {
        // N4/DN4: a Configure registered after AddOtelHelper also decides the listener.
        var port = TestPorts.FreeTcpPort();
        var services = new ServiceCollection();
        services.AddSingleton<StaffOps.OtelHelper.Diagnostics.IOtelHelperDiagnostics>(new TestDiagnostics().Sink);
        services.AddOtelHelper(o => { o.ServiceName = "late-svc"; o.PrometheusMetricsPort = 0; });
        services.Configure<TelemetryOptions>(o => o.PrometheusMetricsPort = port);
        using var provider = services.BuildServiceProvider();
        provider.GetRequiredService<MeterProvider>();

        using var http = new HttpClient { Timeout = TimeSpan.FromSeconds(5) };
        var response = await http.GetAsync($"http://localhost:{port}/metrics");
        Assert.Equal(System.Net.HttpStatusCode.OK, response.StatusCode);
    }
}
