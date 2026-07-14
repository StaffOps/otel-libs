using System.Diagnostics.Metrics;
using Microsoft.Extensions.DependencyInjection;
using OpenTelemetry;
using OpenTelemetry.Metrics;
using OtelHelper;
using OtelHelper.Metrics;
using Xunit;

namespace OtelHelper.Tests;

/// <summary>
/// Contract tests for OTEL_METRICS_EXPORTER / OTEL_METRIC_EXPORT_INTERVAL (US-1, US-2, US-6).
/// Cross-language parity: python/tests/test_metrics_contract.py and
/// go/metrics_contract_test.go assert the same table.
/// </summary>
[Collection("EnvVarTests")]
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

    private static TelemetryOptions Resolved(Action<TelemetryOptions>? configure = null)
    {
        var opts = new TelemetryOptions();
        configure?.Invoke(opts);
        new TelemetryOptionsPostConfigure().PostConfigure(null, opts);
        return opts;
    }

    private static string? Validate(TelemetryOptions opts)
    {
        var result = new TelemetryOptionsValidator().Validate(null, opts);
        return result.Failed ? result.FailureMessage : null;
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
        Assert.Null(Validate(opts));
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

    [Fact]
    public void UnknownValue_FailsValidation()
    {
        Environment.SetEnvironmentVariable(TelemetryOptions.MetricsExporterEnvVar, "statsd");
        var failure = Validate(Resolved());
        Assert.NotNull(failure);
        Assert.Contains("statsd", failure);
        Assert.Contains("Valid values", failure);
    }

    [Fact]
    public void OtlpWithoutEndpoint_FailsValidation()
    {
        Environment.SetEnvironmentVariable(TelemetryOptions.MetricsExporterEnvVar, "otlp");
        var failure = Validate(Resolved());
        Assert.NotNull(failure);
        Assert.Contains("requires an endpoint", failure);
    }

    [Fact]
    public void NoneCombined_FailsValidation()
    {
        Environment.SetEnvironmentVariable(TelemetryOptions.MetricsExporterEnvVar, "none,prometheus");
        var failure = Validate(Resolved());
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
        var failure = Validate(Resolved(o => o.ExportIntervalMs = 0));
        Assert.NotNull(failure);
        Assert.Contains("ExportIntervalMs", failure);
    }

    // --- US-4: port 0 = listener disabled passes validation ---

    [Fact]
    public void PortZero_IsValid()
    {
        Assert.Null(Validate(Resolved(o => o.PrometheusMetricsPort = 0)));
    }

    // --- US-1/US-6: dual-mode pipeline — same counter reaches both readers ---

    [Fact]
    public async Task DualMode_CounterVisible_InInMemoryAndPrometheusReaders()
    {
        // Single MeterProvider, two readers: an in-memory reader standing in for
        // the OTLP periodic reader, plus the Prometheus HTTP listener on a free
        // port. The same instrument must be visible in both without double counting.
        var port = FreeTcpPort();
        var exportedItems = new List<Metric>();
        using var meter = new Meter("contract-test");

        using var provider = Sdk.CreateMeterProviderBuilder()
            .AddMeter("contract-test")
            .AddInMemoryExporter(exportedItems)
            .AddPrometheusHttpListener(o => o.UriPrefixes = new[] { $"http://localhost:{port}/" })
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
        var port = FreeTcpPort();
        var opts = Resolved(o =>
        {
            o.ServiceName = "prom-svc";
            o.OtelCollectorEndpoint = "http://localhost:4317";
            o.MetricExporters = "prometheus";
            o.PrometheusMetricsPort = port;
        });

        using var provider = Sdk.CreateMeterProviderBuilder().ConfigureMetrics(opts).Build();
        Assert.NotNull(provider);

        using var http = new HttpClient();
        var response = await http.GetAsync($"http://localhost:{port}/metrics");
        Assert.Equal(System.Net.HttpStatusCode.OK, response.StatusCode);
    }

    [Fact]
    public void ConfigureMetrics_PortZero_SkipsListener()
    {
        var opts = Resolved(o =>
        {
            o.ServiceName = "noport-svc";
            o.MetricExporters = "prometheus";
            o.PrometheusMetricsPort = 0;
        });

        // Builds without binding anything — no listener, reader stays passive.
        using var provider = Sdk.CreateMeterProviderBuilder().ConfigureMetrics(opts).Build();
        Assert.NotNull(provider);
    }

    private static int FreeTcpPort()
    {
        var listener = new System.Net.Sockets.TcpListener(System.Net.IPAddress.Loopback, 0);
        listener.Start();
        var port = ((System.Net.IPEndPoint)listener.LocalEndpoint).Port;
        listener.Stop();
        return port;
    }
}
