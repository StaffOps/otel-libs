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
/// 1.20.x (tasks C3 and C12, design D-NET11): every IPv4 interface on the resolved
/// port unless <c>OTEL_EXPORTER_PROMETHEUS_HOST</c> names an address, and fresh
/// scrapes, although upstream defaults the host to <c>localhost</c> and caches
/// responses for 300 ms. Reachability from another container is checked by
/// <c>testing/e2e/dotnet-prom-scrape.sh</c>; the bind-host negative control is in
/// <c>PrometheusFallbackC12Tests</c>.
/// </summary>
[Collection("EnvVarTests")]
[ResetGlobalPropagation]
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

    [Theory]
    [InlineData("", "http://+:9464/metrics/")]
    [InlineData("0.0.0.0", "http://+:9464/metrics/")]
    [InlineData("127.0.0.1", "http://127.0.0.1:9464/metrics/ http://localhost:9464/metrics/")]
    [InlineData("10.1.2.3", "http://10.1.2.3:9464/metrics/")]
    public void ListenerPrefixes_FollowTheBindHost(string host, string prefixes)
    {
        // Every interface is the "+" prefix (the managed HttpListener refuses 0.0.0.0); an IPv4 literal
        // binds itself; 127.0.0.1 also answers Host: localhost (C12 fix round 1).
        Assert.Equal(prefixes.Split(' '), MetricsSetup.PrometheusListenerPrefixes(9464, host));
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
    public void ConfigurePrometheusListener_Host_ReplacesThePrefix()
    {
        var opts = new PrometheusHttpListenerOptions();
        MetricsSetup.ConfigurePrometheusListener(opts, 9999, "127.0.0.1");

        using var listener = new HttpListener();
        listener.Prefixes.Add("http://localhost:9999/metrics/");
        opts.ConfigureHttpListener!(opts, listener);
        Assert.Equal(new[] { "http://127.0.0.1:9999/metrics/", "http://localhost:9999/metrics/" }, listener.Prefixes.OrderBy(p => p, StringComparer.Ordinal).ToArray());
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
    public async Task PrometheusPortEnvVar_BeatsHelperPort_AndHostStarDoesNotAbort()
    {
        // A29 (C12): OTEL_EXPORTER_PROMETHEUS_PORT wins over OTEL_HELPER_METRICS_PORT; an invalid
        // host ("*", which aborts the 1.20.x build when it reaches the exporter) is warned and
        // binds every interface.
        var helperPort = TestPorts.FreeTcpPort();
        var envPort = TestPorts.FreeTcpPort();
        Environment.SetEnvironmentVariable(PromHostEnvVar, "*");
        Environment.SetEnvironmentVariable(PromPortEnvVar, envPort.ToString());
        Environment.SetEnvironmentVariable(TelemetryOptions.MetricsPortEnvVar, helperPort.ToString());

        var diagnostics = new TestDiagnostics();
        using var sp = TestPipelines.Build(o => o.ServiceName = "prom-env-test", diagnostics);

        using var http = new HttpClient();
        var response = await http.GetAsync($"http://localhost:{envPort}/metrics");
        Assert.Equal(HttpStatusCode.OK, response.StatusCode);
        await Assert.ThrowsAsync<HttpRequestException>(() => http.GetAsync($"http://localhost:{helperPort}/metrics"));
        Assert.Single(diagnostics.Lines, l => l.Contains(PromHostEnvVar, StringComparison.Ordinal));
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
