using System.Diagnostics.Metrics;
using System.Net;
using Microsoft.Extensions.DependencyInjection;
using OpenTelemetry;
using OpenTelemetry.Exporter;
using OpenTelemetry.Metrics;
using StaffOps.OtelHelper;
using StaffOps.OtelHelper.Metrics;
using Xunit;

namespace StaffOps.OtelHelper.Tests;

/// <summary>
/// Prometheus fallback listener on OpenTelemetry.Exporter.Prometheus.HttpListener
/// 1.20.x (task C3, design D-NET11): the 0.2.x bind (every IPv4 interface on the
/// helper port) and fresh scrapes are kept although upstream now defaults the host
/// to <c>localhost</c>, caches responses for 300 ms and reads
/// <c>OTEL_EXPORTER_PROMETHEUS_HOST</c>/<c>_PORT</c>. Reachability from another
/// container is checked by <c>testing/e2e/dotnet-prom-scrape.sh</c>, so this
/// suite runs without a network.
/// </summary>
[Collection("EnvVarTests")]
public class PrometheusListenerTests : IDisposable
{
    private const string PromHostEnvVar = "OTEL_EXPORTER_PROMETHEUS_HOST";
    private const string PromPortEnvVar = "OTEL_EXPORTER_PROMETHEUS_PORT";

    private static readonly string[] TouchedEnvVars =
    {
        PromHostEnvVar,
        PromPortEnvVar,
        TelemetryOptions.CollectorEndpointEnvVar,
        TelemetryOptions.MetricsExporterEnvVar,
        TelemetryOptions.MetricsPortEnvVar,
    };

    public PrometheusListenerTests() => ClearEnv();
    public void Dispose() => ClearEnv();

    private static void ClearEnv()
    {
        foreach (var v in TouchedEnvVars)
            Environment.SetEnvironmentVariable(v, null);
    }

    [Fact]
    public void ListenerPrefix_IsWildcardHostMetricsPath()
    {
        Assert.Equal("http://+:9464/metrics/", MetricsSetup.PrometheusListenerPrefix(9464));
    }

    [Fact]
    public void ConfigurePrometheusListener_SetsHostPortCacheAndPrefixDelegate()
    {
        var opts = new PrometheusHttpListenerOptions { Host = "*", Port = 1234, ScrapeResponseCacheDurationMilliseconds = 300 };

        MetricsSetup.ConfigurePrometheusListener(opts, 9999);

        Assert.Equal("localhost", opts.Host);
        Assert.Equal(9999, opts.Port);
        Assert.Equal(0, opts.ScrapeResponseCacheDurationMilliseconds);
        Assert.NotNull(opts.ConfigureHttpListener);

        using var listener = new HttpListener();
        listener.Prefixes.Add("http://localhost:9999/metrics/");
        opts.ConfigureHttpListener!(opts, listener);
        Assert.Equal(new[] { "http://+:9999/metrics/" }, listener.Prefixes.ToArray());
    }

    [Fact]
    public void ConfigurePrometheusListener_Inactive_ClearsEveryPrefix()
    {
        // D-NET3 "no listener": the reader exists, the HttpListener gets no prefix.
        var opts = new PrometheusHttpListenerOptions();
        MetricsSetup.ConfigurePrometheusListener(opts, 9999, active: false);

        using var listener = new HttpListener();
        listener.Prefixes.Add("http://localhost:9999/metrics/");
        opts.ConfigureHttpListener!(opts, listener);
        Assert.Empty(listener.Prefixes);
    }

    [Fact]
    public async Task Scrape_IsFresh_NoResponseCache()
    {
        // Upstream caches scrape responses 300 ms by default; 0.2.x did not cache.
        var port = TestPorts.FreeTcpPort();
        using var meter = new Meter("prom-cache-test");
        using var sp = PrometheusOnly(port, "prom-cache-test");
        var counter = meter.CreateCounter<long>("cache_hits");
        using var http = new HttpClient();

        counter.Add(1);
        var first = await http.GetStringAsync($"http://localhost:{port}/metrics");
        counter.Add(41);
        var second = await http.GetStringAsync($"http://localhost:{port}/metrics");

        Assert.Equal("1", SampleLine(first, "cache_hits_total"));
        Assert.Equal("42", SampleLine(second, "cache_hits_total"));
    }

    [Fact]
    public async Task PrometheusEnvVars_HaveNoEffect_HelperPortServed()
    {
        // OTEL_EXPORTER_PROMETHEUS_HOST="*" aborts the 1.20.x provider build when it
        // reaches the exporter; _PORT would move the listener. The helper's values
        // win, as in 0.2.x where the exporter did not read either variable.
        var helperPort = TestPorts.FreeTcpPort();
        var envPort = TestPorts.FreeTcpPort();
        Environment.SetEnvironmentVariable(PromHostEnvVar, "*");
        Environment.SetEnvironmentVariable(PromPortEnvVar, envPort.ToString());
        Environment.SetEnvironmentVariable(TelemetryOptions.MetricsPortEnvVar, helperPort.ToString());

        var services = new ServiceCollection();
        services.AddOtelHelper(o => o.ServiceName = "prom-env-test");
        using var sp = services.BuildServiceProvider();
        var meterProvider = sp.GetRequiredService<MeterProvider>();
        Assert.NotNull(meterProvider);

        using var http = new HttpClient();
        var response = await http.GetAsync($"http://localhost:{helperPort}/metrics");
        Assert.Equal(HttpStatusCode.OK, response.StatusCode);
        await Assert.ThrowsAsync<HttpRequestException>(() => http.GetAsync($"http://localhost:{envPort}/metrics"));
    }

    private static ServiceProvider PrometheusOnly(int port, string meter)
        => TestPipelines.Build(o =>
        {
            o.ServiceName = "prom-listener-svc";
            o.MetricExporters = "prometheus";
            o.PrometheusMetricsPort = port;
            o.AdditionalMeters.Add(meter);
        });

    private static string SampleLine(string exposition, string metric)
    {
        // "<metric>{labels} <value>[ <timestamp>]" → value
        var line = exposition.Split('\n').First(l => l.StartsWith(metric + "{", StringComparison.Ordinal) || l.StartsWith(metric + " ", StringComparison.Ordinal));
        return line[(line.LastIndexOf('}') + 1)..].Trim().Split(' ')[0];
    }
}
