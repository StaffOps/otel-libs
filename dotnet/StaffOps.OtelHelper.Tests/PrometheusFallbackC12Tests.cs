using System.Diagnostics;
using System.Diagnostics.Metrics;
using System.Net;
using System.Net.NetworkInformation;
using System.Net.Sockets;
using Microsoft.Extensions.DependencyInjection;
using OpenTelemetry;
using OpenTelemetry.Exporter;
using OpenTelemetry.Logs;
using OpenTelemetry.Metrics;
using OpenTelemetry.Trace;
using StaffOps.OtelHelper.Metrics;
using Xunit;

namespace StaffOps.OtelHelper.Tests;

/// <summary>
/// Task C12 (design D-NET11, contract D1 rows "Metrics port grammar", "Prometheus exporter
/// variables" and "Disabled-metric patterns"; A21, A29): the port and host resolution, the bind
/// of the resolved host (with a negative control on a non-loopback address), the contract glob,
/// the listener reader registered only when it binds, and <c>ForceFlushOtelHelper</c>.
/// </summary>
[Collection("EnvVarTests")]
[ResetGlobalPropagation]
public class PrometheusFallbackC12Tests : IDisposable
{
    private const string PromHost = TelemetryResolver.PrometheusHostEnvVar;
    private const string PromPort = TelemetryResolver.PrometheusPortEnvVar;
    private const string HelperPort = TelemetryOptions.MetricsPortEnvVar;

    private static readonly string[] TouchedEnvVars =
    {
        PromHost, PromPort, HelperPort,
        TelemetryOptions.CollectorEndpointEnvVar,
        TelemetryOptions.MetricsExporterEnvVar,
        TelemetryOptions.DisabledMetricsEnvVar,
        "OTEL_SERVICE_NAME", "OTEL_SDK_DISABLED",
        "OTEL_EXPORTER_OTLP_METRICS_DEFAULT_HISTOGRAM_AGGREGATION",
    };

    public PrometheusFallbackC12Tests() => ClearEnv();

    public void Dispose() => ClearEnv();

    private static void ClearEnv()
    {
        foreach (var v in TouchedEnvVars)
        {
            Environment.SetEnvironmentVariable(v, null);
        }
    }

    private static void Env(string name, string? value) => Environment.SetEnvironmentVariable(name, value);

    // ── port grammar (A21) ──────────────────────────────────────────────

    [Theory]
    [InlineData("9464", 9464)]
    [InlineData("+9100", 9100)]
    [InlineData("0", 0)]
    [InlineData("65535", 65535)]
    [InlineData("+0000000000000000000000000000009100", 9100)]
    [InlineData("000000000", 0)]
    public void PortGrammar_Valid(string text, int expected)
    {
        Assert.True(TelemetryResolver.TryParseMetricsPort(text, out var port));
        Assert.Equal(expected, port);
    }

    [Theory]
    [InlineData("9_464")]
    [InlineData("-0")]
    [InlineData("+")]
    [InlineData("")]
    [InlineData("65536")]
    [InlineData("99999")]
    [InlineData("100000")]
    [InlineData("1111111111111111111111111111111111111111")]
    [InlineData("++9464")]
    [InlineData("9464 ")]
    [InlineData("٩٤")] // Arabic-Indic digits
    [InlineData("0x10")]
    public void PortGrammar_Invalid(string text)
        => Assert.False(TelemetryResolver.TryParseMetricsPort(text, out _));

    [Fact]
    public void UnparseableHelperPort_Warns_And_Uses9464()
    {
        // Silent before C12 (int.TryParse failed quietly; C5 note).
        Env(HelperPort, "nine");
        var diagnostics = new TestDiagnostics();
        var resolved = diagnostics.Resolve(TestDiagnostics.Identified);
        Assert.Equal(9464, resolved.PrometheusMetricsPort);
        var line = Assert.Single(diagnostics.Lines, l => l.Contains(HelperPort, StringComparison.Ordinal));
        Assert.Contains("invalid port", line, StringComparison.Ordinal);
        Assert.Contains("using 9464", line, StringComparison.Ordinal);
    }

    [Fact]
    public void HelperPort_IsTrimmedWithTheContractSet()
    {
        Env(HelperPort, " \t9100\n");
        Assert.Equal(9100, TestDiagnostics.ResolveQuiet(TestDiagnostics.Identified).PrometheusMetricsPort);

        // U+00A0 is not in the contract trim set (int.TryParse accepted it before C12).
        Env(HelperPort, " 9100");
        var diagnostics = new TestDiagnostics();
        Assert.Equal(9464, diagnostics.Resolve(TestDiagnostics.Identified).PrometheusMetricsPort);
        Assert.Single(diagnostics.Lines, l => l.Contains(HelperPort, StringComparison.Ordinal));
    }

    // ── port precedence (A29) ───────────────────────────────────────────

    [Fact]
    public void StandardPort_BeatsHelperPort_WithoutWarning()
    {
        Env(PromPort, "9300");
        Env(HelperPort, "9100");
        var diagnostics = new TestDiagnostics();
        Assert.Equal(9300, diagnostics.Resolve(TestDiagnostics.Identified).PrometheusMetricsPort);
        Assert.DoesNotContain(diagnostics.Lines, l => l.Contains("PROMETHEUS", StringComparison.Ordinal) || l.Contains(HelperPort, StringComparison.Ordinal));
    }

    [Fact]
    public void InvalidStandardPort_Warns_And_FallsToHelperPort()
    {
        Env(PromPort, "93_00");
        Env(HelperPort, "9100");
        var diagnostics = new TestDiagnostics();
        Assert.Equal(9100, diagnostics.Resolve(TestDiagnostics.Identified).PrometheusMetricsPort);
        var line = Assert.Single(diagnostics.Lines, l => l.Contains(PromPort, StringComparison.Ordinal));
        Assert.Contains("using OTEL_HELPER_METRICS_PORT, else 9464", line, StringComparison.Ordinal);
    }

    [Fact]
    public void BothPortsInvalid_EachWarnsOnce_9464()
    {
        Env(PromPort, "x93");
        Env(HelperPort, "y91");
        var diagnostics = new TestDiagnostics();
        Assert.Equal(9464, diagnostics.Resolve(TestDiagnostics.Identified).PrometheusMetricsPort);
        Assert.Single(diagnostics.Lines, l => l.Contains("x93", StringComparison.Ordinal));
        Assert.Single(diagnostics.Lines, l => l.Contains("y91", StringComparison.Ordinal));
    }

    [Fact]
    public void StandardPortZero_IsNoListener_NotTheNextLevel()
    {
        Env(PromPort, "0");
        Env(HelperPort, "9100");
        Assert.Equal(0, TestDiagnostics.ResolveQuiet(TestDiagnostics.Identified).PrometheusMetricsPort);
    }

    [Fact]
    public void EmptyStandardPort_IsUnset()
    {
        Env(PromPort, " ");
        Env(HelperPort, "9100");
        var diagnostics = new TestDiagnostics();
        Assert.Equal(9100, diagnostics.Resolve(TestDiagnostics.Identified).PrometheusMetricsPort);
        Assert.DoesNotContain(diagnostics.Lines, l => l.Contains(PromPort, StringComparison.Ordinal));
    }

    [Fact]
    public void CodePort_Beats_BothVariables_AndThey_AreNotRead()
    {
        Env(PromPort, "x93");
        Env(HelperPort, "9100");
        var diagnostics = new TestDiagnostics();
        Assert.Equal(9200, diagnostics.Resolve(o => { TestDiagnostics.Identified(o); o.PrometheusMetricsPort = 9200; }).PrometheusMetricsPort);
        Assert.DoesNotContain(diagnostics.Lines, l => l.Contains(PromPort, StringComparison.Ordinal));
    }

    [Fact]
    public void InvalidPortEcho_LosesUserinfoAndQuery()
    {
        Env(PromPort, "https://user:c12-port-s3cr3t@h?k=c12-query-s3cr3t");
        var diagnostics = new TestDiagnostics();
        diagnostics.Resolve(TestDiagnostics.Identified);
        Assert.Single(diagnostics.Lines, l => l.Contains(PromPort, StringComparison.Ordinal));
        Assert.DoesNotContain("s3cr3t", diagnostics.Text, StringComparison.Ordinal);
    }

    // ── host grammar (A29) ──────────────────────────────────────────────

    [Theory]
    [InlineData("127.0.0.1", "127.0.0.1")]
    [InlineData("0.0.0.0", "0.0.0.0")]
    [InlineData("10.1.2.3", "10.1.2.3")]
    [InlineData("LocalHost", "127.0.0.1")]
    [InlineData("localhost", "127.0.0.1")]
    [InlineData("::1", "::1")]
    [InlineData("[::1]", "::1")]
    [InlineData("::", "")]
    [InlineData("[::]", "")]
    [InlineData("0:0:0:0:0:0:0:0", "")]
    [InlineData("FE80::1", "fe80::1")]
    [InlineData("::ffff:127.0.0.1", "127.0.0.1")]
    [InlineData("::FFFF:7f00:1", "127.0.0.1")]
    [InlineData("[::ffff:10.0.0.1]", "10.0.0.1")]
    [InlineData("2001:db8::0.0.0.1", "2001:db8::0.0.0.1")]
    public void HostGrammar_Valid(string text, string expected)
    {
        Assert.True(TelemetryResolver.TryParsePrometheusHost(text, out var host));
        Assert.Equal(expected, host);
    }

    [Theory]
    [InlineData("*")]
    [InlineData("+")]
    [InlineData("127.000.0.1")]
    [InlineData("01.2.3.4")]
    [InlineData("127.1")]
    [InlineData("256.0.0.1")]
    [InlineData("0x7f.0.0.1")]
    [InlineData("[127.0.0.1]")]
    [InlineData("127.0.0.1:9464")]
    [InlineData("[::1]:9464")]
    [InlineData("fe80::1%eth0")]
    [InlineData("[fe80::1%25eth0]")]
    [InlineData("metrics.example.com")]
    [InlineData("localhost.")]
    [InlineData("http://127.0.0.1")]
    [InlineData("::1/128")]
    [InlineData("::ffff:127.000.0.1")]
    [InlineData("[::1")]
    [InlineData("1.2.3.4.5")]
    public void HostGrammar_Invalid(string text)
        => Assert.False(TelemetryResolver.TryParsePrometheusHost(text, out _));

    [Fact]
    public void InvalidHost_WarnsOnce_FailsOpen_And_RedactsTheEcho()
    {
        Env(PromHost, "https://user:c12-host-s3cr3t@metrics.example.com?k=c12-query-s3cr3t");
        var diagnostics = new TestDiagnostics();
        var resolved = diagnostics.Resolve(TestDiagnostics.Identified);
        Assert.Equal("", resolved.PrometheusMetricsHost);
        var line = Assert.Single(diagnostics.Lines, l => l.Contains(PromHost, StringComparison.Ordinal));
        Assert.Contains(TelemetryResolver.InvalidPrometheusHostMessage, line, StringComparison.Ordinal);
        Assert.DoesNotContain("s3cr3t", diagnostics.Text, StringComparison.Ordinal);
    }

    [Fact]
    public void Host_IsTrimmed_AndBlankIsUnset()
    {
        Env(PromHost, " 127.0.0.1\t");
        Assert.Equal("127.0.0.1", TestDiagnostics.ResolveQuiet(TestDiagnostics.Identified).PrometheusMetricsHost);
        Env(PromHost, "  ");
        var diagnostics = new TestDiagnostics();
        Assert.Equal("", diagnostics.Resolve(TestDiagnostics.Identified).PrometheusMetricsHost);
        Assert.DoesNotContain(diagnostics.Lines, l => l.Contains(PromHost, StringComparison.Ordinal));
    }

    [Fact]
    public void Host_IsParsedAndWarned_EvenWhenTheListenerDoesNotRun()
    {
        Env(PromHost, "*");
        Env(TelemetryOptions.MetricsExporterEnvVar, "none");
        var diagnostics = new TestDiagnostics();
        diagnostics.Resolve(TestDiagnostics.Identified);
        Assert.Single(diagnostics.Lines, l => l.Contains(PromHost, StringComparison.Ordinal));
    }

    // ── bind (D-NET11, A29; fix round 1) ────────────────────────────────

    [NonLoopbackFact]
    public async Task LoopbackHost_RefusesANonLoopbackConnect()
    {
        // The negative control the parity case cannot show (its runner connects to 127.0.0.1):
        // with OTEL_EXPORTER_PROMETHEUS_HOST=127.0.0.1 a connect to this machine's own
        // non-loopback address is refused, and without it the same connect is answered.
        var external = NonLoopbackIPv4()!;
        var port = TestPorts.FreeTcpPort();
        Env(PromHost, "127.0.0.1");
        using (Listening(port))
        {
            Assert.Equal(200, await StatusAsync(IPAddress.Loopback, port, "127.0.0.1"));
            await Assert.ThrowsAsync<SocketException>(() => ConnectAsync(external, port));
        }

        Env(PromHost, null);
        using (Listening(port))
        {
            Assert.Equal(200, await StatusAsync(external, port, external.ToString()));
        }
    }

    [Theory]
    [InlineData("127.0.0.1")]
    [InlineData("localhost")]
    [InlineData("LOCALHOST")]
    public async Task LoopbackHost_AnswersLocalhostAnd127_StillLoopbackOnly(string host)
    {
        // Fix round 1: the listener routes by Host, so the 127.0.0.1 bind also registers a localhost
        // prefix; a sidecar scraping http://localhost:<port>/metrics gets 200 (Go answers 200 too).
        // The localhost prefix binds what the resolver returns first (::1 here, 127.0.0.1 with IPv6
        // off); HttpClient and Go dial ::1 first (measured). curl and Node.js resolve with
        // AI_ADDRCONFIG and, with ::1 as the only IPv6 address, send Host: localhost to 127.0.0.1:
        // the documented residual 404.
        var port = TestPorts.FreeTcpPort();
        Env(PromHost, host);
        using var sp = Listening(port);
        using var http = new HttpClient();
        Assert.Equal(HttpStatusCode.OK, (await http.GetAsync($"http://localhost:{port}/metrics")).StatusCode);
        Assert.Equal(HttpStatusCode.OK, (await http.GetAsync($"http://127.0.0.1:{port}/metrics")).StatusCode);
        if (NonLoopbackIPv4() is { } external)
        {
            await Assert.ThrowsAsync<SocketException>(() => ConnectAsync(external, port));
        }
    }

    [Fact]
    public async Task OtherIpv4Host_RoutesByHostHeader()
    {
        // Any other specific address keeps the HttpListener's Host routing: 127.0.0.2 (loopback on
        // Linux) answers a request naming it and 404 for Host: localhost.
        var port = TestPorts.FreeTcpPort();
        Env(PromHost, "127.0.0.2");
        using var sp = Listening(port);
        var address = IPAddress.Parse("127.0.0.2");
        Assert.Equal(200, await StatusAsync(address, port, "127.0.0.2"));
        Assert.Equal(404, await StatusAsync(address, port, "localhost"));
    }

    [Theory]
    [InlineData("[::1]")]
    [InlineData("::1")]
    [InlineData("0:0:0:0:0:0:0:1")]
    public async Task Ipv6LoopbackHost_Binds127001_WithOneWarning(string host)
    {
        // Fix round 1 (coordinator decision): the managed HttpListener cannot bind ::1 (measured), so
        // the loopback restriction is kept on 127.0.0.1 instead; never widened, never a failed start.
        var port = TestPorts.FreeTcpPort();
        Env(PromHost, host);
        var diagnostics = new TestDiagnostics();
        using var sp = Listening(port, diagnostics);
        Assert.Equal(200, await StatusAsync(IPAddress.Loopback, port, "127.0.0.1"));
        using var http = new HttpClient();
        Assert.Equal(HttpStatusCode.OK, (await http.GetAsync($"http://localhost:{port}/metrics")).StatusCode);
        var line = Assert.Single(diagnostics.Lines, l => l.Contains(PromHost, StringComparison.Ordinal));
        Assert.StartsWith("otelhelper: WARN", line, StringComparison.Ordinal);
        Assert.Contains("binds 127.0.0.1", line, StringComparison.Ordinal);
        if (NonLoopbackIPv4() is { } external)
        {
            await Assert.ThrowsAsync<SocketException>(() => ConnectAsync(external, port));
        }
    }

    [Theory]
    [InlineData("fe80::1")]
    [InlineData("[2001:db8::1]")]
    public void OtherIpv6Host_NoListener_OneError_StartSucceeds(string host)
    {
        var port = TestPorts.FreeTcpPort();
        Env(PromHost, host);
        var diagnostics = new TestDiagnostics();
        using var sp = Listening(port, diagnostics);
        Assert.False(Answers(port));
        var line = Assert.Single(diagnostics.Lines, l => l.Contains("cannot bind", StringComparison.Ordinal));
        Assert.StartsWith("otelhelper: ERROR", line, StringComparison.Ordinal);
        Assert.Contains("no Prometheus listener runs", line, StringComparison.Ordinal);
        Assert.False(sp.GetRequiredService<ResolvedTelemetry>().PrometheusListenerActive);
    }

    [Theory]
    [InlineData("::")]
    [InlineData("[::]")]
    public void UnspecifiedIpv6Host_EveryIpv4Interface_OneWarning(string host)
    {
        var port = TestPorts.FreeTcpPort();
        Env(PromHost, host);
        var diagnostics = new TestDiagnostics();
        using var sp = Listening(port, diagnostics);
        Assert.True(Answers(port));
        var line = Assert.Single(diagnostics.Lines, l => l.Contains(PromHost, StringComparison.Ordinal));
        Assert.Contains("no IPv6 or dual-stack bind", line, StringComparison.Ordinal);
    }

    [Fact]
    public void Ipv6Host_WithoutListener_IsNotReported()
    {
        Env(PromHost, "fe80::1");
        var diagnostics = new TestDiagnostics();
        using var sp = TestPipelines.Build(o => { TestDiagnostics.Identified(o); o.MetricExporters = "prometheus"; o.PrometheusMetricsPort = 0; }, diagnostics);
        Assert.DoesNotContain(diagnostics.Lines, l => l.Contains("cannot bind", StringComparison.Ordinal));
    }

    [Fact]
    public void UnownedHost_FailsTheBuild_AfterOneHelperError()
    {
        // 192.0.2.1 is TEST-NET-1 (RFC 5737), never assigned to this machine.
        var port = TestPorts.FreeTcpPort();
        Env(PromHost, "192.0.2.1");
        var diagnostics = new TestDiagnostics();
        var ex = Assert.Throws<InvalidOperationException>(() => BuildMetricsOnly(port, diagnostics));
        Assert.Contains("HttpListener could not be started", ex.Message, StringComparison.Ordinal);
        var line = Assert.Single(diagnostics.Lines, l => l.Contains("cannot bind", StringComparison.Ordinal));
        Assert.StartsWith("otelhelper: ERROR", line, StringComparison.Ordinal);
        Assert.Contains("host=192.0.2.1", line, StringComparison.Ordinal);
        Assert.Contains(PromHost, line, StringComparison.Ordinal);
        Assert.Contains("TelemetryOptions.PrometheusMetricsPort", line, StringComparison.Ordinal);
        Assert.Contains("Cannot assign requested address", line, StringComparison.Ordinal);
    }

    [Fact]
    public void BusyPort_FailsTheBuild_AfterOneHelperError_NamingThePortSource()
    {
        var port = TestPorts.FreeTcpPort();
        Env(HelperPort, port.ToString(System.Globalization.CultureInfo.InvariantCulture));
        using var busy = new TcpListener(IPAddress.Any, port);
        busy.Start();
        var diagnostics = new TestDiagnostics();
        var services = new ServiceCollection();
        services.AddSingleton<StaffOps.OtelHelper.Diagnostics.IOtelHelperDiagnostics>(diagnostics.Sink);
        services.AddOtelHelper(o => { TestDiagnostics.Identified(o); o.MetricExporters = "prometheus"; });
        using (var sp = services.BuildServiceProvider())
        {
            Assert.Throws<InvalidOperationException>(() => sp.GetRequiredService<MeterProvider>());
        }

        var line = Assert.Single(diagnostics.Lines, l => l.Contains("cannot bind", StringComparison.Ordinal));
        Assert.Contains("the port set by " + HelperPort, line, StringComparison.Ordinal);
        Assert.Contains("host=\"every IPv4 interface\"", line, StringComparison.Ordinal);
        Assert.DoesNotContain(PromHost, line, StringComparison.Ordinal);
    }

    [Fact]
    public void UnrelatedReaderFactoryFailure_GetsNoBindError()
    {
        // Round 2: the bind wording is only for the listener start (HttpListenerException inside).
        var port = TestPorts.FreeTcpPort();
        var diagnostics = new TestDiagnostics();
        var services = new ServiceCollection();
        services.AddSingleton<StaffOps.OtelHelper.Diagnostics.IOtelHelperDiagnostics>(diagnostics.Sink);
        services.AddOtelHelper(o => { TestDiagnostics.Identified(o); o.MetricExporters = "prometheus"; o.PrometheusMetricsPort = port; });
        services.Configure<PrometheusHttpListenerOptions>(MetricsSetup.ListenerName, _ => throw new InvalidOperationException("c12-unrelated"));
        using (var sp = services.BuildServiceProvider())
        {
            var ex = Assert.Throws<InvalidOperationException>(() => sp.GetRequiredService<MeterProvider>());
            Assert.Equal("c12-unrelated", ex.Message);
        }

        Assert.DoesNotContain(diagnostics.Lines, l => l.Contains("cannot bind", StringComparison.Ordinal));
    }

    [NonLoopbackFact]
    public async Task UnspecifiedIpv4Host_BindsEveryIpv4Interface()
    {
        var external = NonLoopbackIPv4()!;
        var port = TestPorts.FreeTcpPort();
        Env(PromHost, "0.0.0.0");
        using var sp = Listening(port);
        Assert.Equal(200, await StatusAsync(external, port, external.ToString()));
        Assert.Equal(200, await StatusAsync(IPAddress.Loopback, port, "localhost"));
    }

    [Fact]
    public async Task HelperAndApplicationListeners_BothActive_OnTheirOwnPorts()
    {
        // Fix round 1 (code-review M3): the helper's listener uses named options, so an application
        // AddPrometheusHttpListener() keeps the default-named ones (before, both got the helper's prefix).
        var helperPort = TestPorts.FreeTcpPort();
        var appPort = TestPorts.FreeTcpPort();
        var services = new ServiceCollection();
        services.AddSingleton<StaffOps.OtelHelper.Diagnostics.IOtelHelperDiagnostics>(new TestDiagnostics().Sink);
        services.AddOtelHelper(o => { TestDiagnostics.Identified(o); o.MetricExporters = "prometheus"; o.PrometheusMetricsPort = helperPort; });
        services.AddOpenTelemetry().WithMetrics(b => b.AddPrometheusHttpListener(o => o.Port = appPort));
        using var sp = services.BuildServiceProvider();
        sp.GetRequiredService<MeterProvider>();

        using var http = new HttpClient();
        Assert.Equal(HttpStatusCode.OK, (await http.GetAsync($"http://localhost:{helperPort}/metrics")).StatusCode);
        Assert.Equal(HttpStatusCode.OK, (await http.GetAsync($"http://localhost:{appPort}/metrics")).StatusCode);
    }

    // ── SDK noise (fix round 1) ─────────────────────────────────────────

    [Theory]
    [InlineData("abc", 1)]   // the helper's warning only (the SDK's "invalid value" is helper-owned noise)
    [InlineData("+PORT", 0)] // accepted by the contract grammar; the SDK alone would call it invalid
    public void PrometheusPortVariable_IsWarnedByTheHelperOnly(string value, int expectedLines)
    {
        var port = TestPorts.FreeTcpPort();
        Env(PromPort, value.Replace("PORT", port.ToString(System.Globalization.CultureInfo.InvariantCulture), StringComparison.Ordinal));
        Env(HelperPort, port.ToString(System.Globalization.CultureInfo.InvariantCulture));
        var diagnostics = new TestDiagnostics();
        using var sp = TestPipelines.Build(o => { TestDiagnostics.Identified(o); o.MetricExporters = "prometheus"; o.ForwardSdkDiagnostics = true; }, diagnostics);
        Assert.True(Answers(port));
        Assert.Equal(expectedLines, diagnostics.Lines.Count(l => l.Contains(PromPort, StringComparison.Ordinal)));
    }

    // ── reader registration (D-NET3 revised) ────────────────────────────

    [Theory]
    [InlineData("none", 0)]
    [InlineData("otlp", 0)]
    [InlineData("prometheus", 0)] // port 0: no listener, no reader
    [InlineData("prometheus", 1)]
    [InlineData("otlp,prometheus", 1)]
    public void ListenerReader_IsRegistered_OnlyWhenItBinds(string exporters, int listeners)
    {
        var port = TestPorts.FreeTcpPort();
        using var receiver = new OtlpReceiver(200);
        using var sp = TestPipelines.Build(o =>
        {
            TestDiagnostics.Identified(o);
            o.MetricExporters = exporters;
            o.MetricsEndpoint = receiver.Url;
            o.OtlpProtocol = TelemetryOptions.ProtocolHttpProtobuf;
            o.PrometheusMetricsPort = exporters.Contains("prometheus") && listeners == 0 ? 0 : port;
        });

        Assert.Equal(listeners == 1, Answers(port));
    }

    [Fact]
    public async Task ApplicationListener_KeepsItsOptions_WhenTheHelperListenerIsInactive()
    {
        // Before C12 the helper's deferred options cleared every prefix of an inactive listener,
        // the application's own AddPrometheusHttpListener() included.
        var port = TestPorts.FreeTcpPort();
        var services = new ServiceCollection();
        services.AddSingleton<StaffOps.OtelHelper.Diagnostics.IOtelHelperDiagnostics>(new TestDiagnostics().Sink);
        services.AddOtelHelper(o => { TestDiagnostics.Identified(o); o.MetricExporters = "none"; });
        services.AddOpenTelemetry().WithMetrics(b => b.AddPrometheusHttpListener(o => o.Port = port));
        using var sp = services.BuildServiceProvider();
        sp.GetRequiredService<MeterProvider>();

        using var http = new HttpClient();
        Assert.Equal(HttpStatusCode.OK, (await http.GetAsync($"http://localhost:{port}/metrics")).StatusCode);
    }

    // ── ForceFlush / Shutdown (C8 release blocker) ──────────────────────

    [Fact]
    public void OtlpOnly_SdkForceFlushAndShutdown_ReportTheRealResult()
    {
        // Measured before C12: false and false with every request received (the idle pull reader).
        using var receiver = new OtlpReceiver(200);
        var sp = OtlpMetrics(receiver, "otlp");
        Record(sp);
        var provider = sp.GetRequiredService<MeterProvider>();
        Assert.True(provider.ForceFlush(10_000));
        Assert.True(receiver.Requests > 0);
        Assert.True(provider.Shutdown(10_000));
        sp.Dispose();
    }

    [Fact]
    public void OtlpOnly_SdkResults_AfterA500_OnlyTheHelperFlushIsFalse()
    {
        // HOW-TO §15 (fix round 1): with OTLP only and a collector answering 500, the helper flush
        // reports the failure; the SDK's MeterProvider results are pinned here as measured.
        using var receiver = new OtlpReceiver(500);
        var sp = OtlpMetrics(receiver, "otlp");
        Record(sp);
        Assert.False(sp.ForceFlushOtelHelper(10_000));
        var provider = sp.GetRequiredService<MeterProvider>();
        Record(sp);
        Assert.False(provider.ForceFlush(10_000));
        Record(sp);
        Assert.True(provider.Shutdown(10_000));
        sp.Dispose();
    }

    [Fact]
    public void DualMode_SdkForceFlushIsFalse_HelperFlushIsTrue()
    {
        using var receiver = new OtlpReceiver(200);
        using var sp = OtlpMetrics(receiver, "otlp,prometheus", TestPorts.FreeTcpPort());
        Record(sp);
        Assert.False(sp.GetRequiredService<MeterProvider>().ForceFlush(10_000)); // upstream: pull-only reader
        var before = receiver.Requests;
        Assert.True(sp.ForceFlushOtelHelper(10_000));
        Assert.True(receiver.Requests > before);
    }

    [Fact]
    public void HelperFlush_IsFalse_WhenTheOtlpExportFails()
    {
        using var receiver = new OtlpReceiver(500);
        using var sp = OtlpMetrics(receiver, "otlp,prometheus", TestPorts.FreeTcpPort());
        Record(sp);
        Assert.False(sp.ForceFlushOtelHelper(10_000));
        Assert.True(receiver.Requests > 0);
    }

    [Fact]
    public void HelperFlush_FlushesTracesAndLogs()
    {
        using var receiver = new OtlpReceiver(200);
        using var sp = TestPipelines.Build(o =>
        {
            TestDiagnostics.Identified(o);
            o.ServiceName = "flush-all";
            o.OtelCollectorEndpoint = receiver.BaseUrl;
            o.OtlpProtocol = TelemetryOptions.ProtocolHttpProtobuf;
            o.PrometheusMetricsPort = 0;
            o.DisabledSignals = "metrics";
        });
        using (var source = new ActivitySource("flush-all"))
        using (source.StartActivity("span"))
        {
        }

        sp.GetRequiredService<Microsoft.Extensions.Logging.ILoggerFactory>().CreateLogger("flush").LogWarningMessage();
        Assert.True(sp.ForceFlushOtelHelper(10_000));
        Assert.Contains("/v1/traces", receiver.Paths);
        Assert.Contains("/v1/logs", receiver.Paths);

        // The batch processors report "flushed within the timeout", not the export result
        // (upstream semantics, measured): a 500 still gives true for traces and logs.
        receiver.Status = 500;
        using (var source = new ActivitySource("flush-all"))
        using (source.StartActivity("span2"))
        {
        }

        Assert.True(sp.ForceFlushOtelHelper(10_000));
        Assert.Equal(2, receiver.Paths.Count(p => p == "/v1/traces"));
    }

    [Fact]
    public void HelperFlush_HistogramPath_UsesTheMeterProvider()
    {
        Env("OTEL_EXPORTER_OTLP_METRICS_DEFAULT_HISTOGRAM_AGGREGATION", "base2_exponential_bucket_histogram");
        using var receiver = new OtlpReceiver(200);
        using var sp = OtlpMetrics(receiver, "otlp");
        Record(sp);
        Assert.True(sp.ForceFlushOtelHelper(10_000));
        Assert.True(receiver.Requests > 0);
        receiver.Status = 500;
        Record(sp);
        Assert.False(sp.ForceFlushOtelHelper(10_000));
    }

    [Fact]
    public void HelperFlush_ArgumentsAndNoHelper()
    {
        Assert.Throws<ArgumentNullException>(() => TelemetryFlushExtensions.ForceFlushOtelHelper(null!));
        using var empty = new ServiceCollection().BuildServiceProvider();
        Assert.Throws<ArgumentOutOfRangeException>(() => empty.ForceFlushOtelHelper(-2));
        Assert.True(empty.ForceFlushOtelHelper(0));
    }

    [Fact]
    public void HelperFlush_DisposedProvider_Throws()
    {
        using var receiver = new OtlpReceiver(200);
        var sp = OtlpMetrics(receiver, "otlp");
        sp.Dispose();
        Assert.Throws<ObjectDisposedException>(() => sp.ForceFlushOtelHelper(1_000));
    }

    [Fact]
    public void HelperFlush_SdkDisabled_IsTrue()
    {
        Env("OTEL_SDK_DISABLED", "true");
        using var receiver = new OtlpReceiver(200);
        using var sp = OtlpMetrics(receiver, "otlp");
        Assert.True(sp.ForceFlushOtelHelper(10_000));
        Assert.Equal(0, receiver.Requests);
    }

    // ── glob (A21) ──────────────────────────────────────────────────────

    [Theory]
    [InlineData("parity.r?quests", "parity.requests", true)]
    [InlineData("parity.payloa??", "parity.payload", false)]
    [InlineData("PARITY.*", "parity.requests", false)]
    [InlineData("Parity.*", "Parity.Mixed", true)]
    [InlineData("Parity.*", "parity.requests", false)]
    [InlineData("a*", "a.b/c", true)]
    [InlineData("parity.[r]equests", "parity.[r]equests", true)]
    [InlineData("parity.[r]equests", "parity.requests", false)]
    [InlineData("a.b", "aXb", false)]
    [InlineData("a+b", "a+b", true)]
    [InlineData("x\\*", "x\\yz", true)]
    [InlineData("a.b", "a.b\n", false)] // \z, not $: a trailing newline is not ignored
    public void DisabledMetricGlob(string pattern, string name, bool dropped)
        => Assert.Equal(dropped, MetricsSetup.DisabledMetricPatterns(pattern).Any(p => p.IsMatch(name)));

    [Fact]
    public void DisabledMetricGlob_ItemsTrimmedWithTheContractSet()
    {
        var patterns = MetricsSetup.DisabledMetricPatterns(" a.x ,\t,, b.y");
        Assert.Equal(2, patterns.Length);
        Assert.Contains(patterns, p => p.IsMatch("a.x"));
        Assert.DoesNotContain(patterns, p => p.IsMatch("b.y"));
        Assert.Contains(patterns, p => p.IsMatch(" b.y"));
    }

    [Fact]
    public async Task DisabledMetrics_CaseSensitive_OnTheScrape()
    {
        var port = TestPorts.FreeTcpPort();
        Env(TelemetryOptions.DisabledMetricsEnvVar, "c12.R?quests");
        using var meter = new Meter("c12-glob");
        using var sp = TestPipelines.Build(o =>
        {
            TestDiagnostics.Identified(o);
            o.MetricExporters = "prometheus";
            o.PrometheusMetricsPort = port;
            o.AdditionalMeters.Add("c12-glob");
        });
        meter.CreateCounter<long>("c12.Requests").Add(1);
        meter.CreateCounter<long>("c12.requests").Add(1);

        using var http = new HttpClient();
        var text = await http.GetStringAsync($"http://localhost:{port}/metrics");
        Assert.DoesNotContain("c12_Requests_total", text, StringComparison.Ordinal);
        Assert.Contains("c12_requests_total", text, StringComparison.Ordinal);
    }

    // ── helpers ─────────────────────────────────────────────────────────

    private static ServiceProvider Listening(int port, TestDiagnostics? diagnostics = null)
        => TestPipelines.Build(o =>
        {
            TestDiagnostics.Identified(o);
            o.MetricExporters = "prometheus";
            o.PrometheusMetricsPort = port;
        }, diagnostics);

    /// <summary>
    /// Builds only the MeterProvider of a Prometheus-only helper and disposes the container even
    /// when the build throws: a leaked TracerProvider would keep sampling other tests' activities.
    /// </summary>
    private static void BuildMetricsOnly(int port, TestDiagnostics? diagnostics = null)
    {
        var services = new ServiceCollection();
        services.AddSingleton<StaffOps.OtelHelper.Diagnostics.IOtelHelperDiagnostics>((diagnostics ?? new TestDiagnostics()).Sink);
        services.AddOtelHelper(o =>
        {
            TestDiagnostics.Identified(o);
            o.MetricExporters = "prometheus";
            o.PrometheusMetricsPort = port;
        });
        using var sp = services.BuildServiceProvider();
        sp.GetRequiredService<MeterProvider>();
    }

    private static ServiceProvider OtlpMetrics(OtlpReceiver receiver, string exporters, int port = 0)
        => TestPipelines.Build(o =>
        {
            TestDiagnostics.Identified(o);
            o.ServiceName = "flush-svc";
            o.MetricExporters = exporters;
            o.MetricsEndpoint = receiver.Url;
            o.OtlpProtocol = TelemetryOptions.ProtocolHttpProtobuf;
            o.PrometheusMetricsPort = port;
            o.DisabledSignals = "traces,logs";
        });

    private static void Record(IServiceProvider sp)
        => sp.GetRequiredKeyedService<Meter>(OtelHelperKeys.Instrumentation).CreateCounter<long>("flush.count").Add(1);

    private static bool Answers(int port)
    {
        try
        {
            using var client = new TcpClient();
            client.Connect(IPAddress.Loopback, port);
            return true;
        }
        catch (SocketException)
        {
            return false;
        }
    }

    private static async Task ConnectAsync(IPAddress address, int port)
    {
        using var client = new TcpClient(address.AddressFamily);
        using var cts = new CancellationTokenSource(TimeSpan.FromSeconds(5));
        await client.ConnectAsync(address, port, cts.Token);
    }

    /// <summary>GET /metrics over a raw connection to <paramref name="address"/> with the given Host header.</summary>
    private static async Task<int> StatusAsync(IPAddress address, int port, string hostHeader)
    {
        using var client = new TcpClient(address.AddressFamily);
        await client.ConnectAsync(address, port);
        await using var stream = client.GetStream();
        var request = System.Text.Encoding.ASCII.GetBytes($"GET /metrics HTTP/1.1\r\nHost: {hostHeader}:{port}\r\nConnection: close\r\n\r\n");
        await stream.WriteAsync(request);
        using var reader = new StreamReader(stream);
        var status = await reader.ReadLineAsync() ?? "";
        return int.Parse(status.Split(' ')[1], System.Globalization.CultureInfo.InvariantCulture);
    }

    private static IPAddress? NonLoopbackIPv4()
        => NetworkInterface.GetAllNetworkInterfaces()
            .Where(n => n.OperationalStatus == OperationalStatus.Up && n.NetworkInterfaceType != NetworkInterfaceType.Loopback)
            .SelectMany(n => n.GetIPProperties().UnicastAddresses)
            .Select(a => a.Address)
            .FirstOrDefault(a => a.AddressFamily == AddressFamily.InterNetwork && !IPAddress.IsLoopback(a));

    /// <summary>An OTLP/HTTP receiver on loopback answering every request with <see cref="Status"/>.</summary>
    private sealed class OtlpReceiver : IDisposable
    {
        private readonly HttpListener _listener = new();
        private readonly Task _loop;
        private int _requests;
        private readonly List<string> _paths = new();

        public OtlpReceiver(int status)
        {
            Status = status;
            var port = TestPorts.FreeTcpPort();
            BaseUrl = $"http://127.0.0.1:{port}";
            _listener.Prefixes.Add(BaseUrl + "/");
            _listener.Start();
            _loop = Task.Run(async () =>
            {
                while (_listener.IsListening)
                {
                    try
                    {
                        var context = await _listener.GetContextAsync();
                        await context.Request.InputStream.CopyToAsync(Stream.Null);
                        Interlocked.Increment(ref _requests);
                        lock (_paths)
                        {
                            _paths.Add(context.Request.Url!.AbsolutePath);
                        }

                        context.Response.StatusCode = Status;
                        context.Response.ContentLength64 = 0;
                        context.Response.Close();
                    }
                    catch (Exception ex) when (ex is HttpListenerException or ObjectDisposedException or InvalidOperationException)
                    {
                        return;
                    }
                }
            });
        }

        public int Status { get; set; }

        public string BaseUrl { get; }

        public string Url => BaseUrl + "/v1/metrics";

        public int Requests => Volatile.Read(ref _requests);

        public string[] Paths
        {
            get
            {
                lock (_paths)
                {
                    return _paths.ToArray();
                }
            }
        }

        public void Dispose()
        {
            _listener.Stop();
            _listener.Close();
            try
            {
                _loop.Wait(TimeSpan.FromSeconds(5));
            }
            catch (AggregateException)
            {
            }
        }
    }
}

/// <summary>
/// A fact that needs a non-loopback IPv4 address of this machine (the bind negative control): skipped
/// with a reason offline; the cross-container check is <c>testing/e2e/dotnet-prom-scrape.sh</c>.
/// </summary>
public sealed class NonLoopbackFactAttribute : FactAttribute
{
    public NonLoopbackFactAttribute()
    {
        var any = NetworkInterface.GetAllNetworkInterfaces()
            .Where(n => n.OperationalStatus == OperationalStatus.Up && n.NetworkInterfaceType != NetworkInterfaceType.Loopback)
            .SelectMany(n => n.GetIPProperties().UnicastAddresses)
            .Any(a => a.Address.AddressFamily == AddressFamily.InterNetwork && !IPAddress.IsLoopback(a.Address));
        if (!any)
        {
            Skip = "no non-loopback IPv4 address (offline): the bind negative control needs one; testing/e2e/dotnet-prom-scrape.sh covers it across containers";
        }
    }
}

internal static class FlushTestLogging
{
    public static void LogWarningMessage(this Microsoft.Extensions.Logging.ILogger logger)
        => Microsoft.Extensions.Logging.LoggerExtensions.LogWarning(logger, "flush-record");
}
