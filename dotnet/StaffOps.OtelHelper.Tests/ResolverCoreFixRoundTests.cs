using System.Diagnostics.Metrics;
using System.Net;
using System.Net.Security;
using System.Security.Cryptography;
using System.Security.Cryptography.X509Certificates;
using Microsoft.Extensions.Configuration;
using Microsoft.Extensions.DependencyInjection;
using Microsoft.Extensions.Options;
using OpenTelemetry;
using OpenTelemetry.Metrics;
using OpenTelemetry.Trace;
using StaffOps.OtelHelper.Diagnostics;
using StaffOps.OtelHelper.Exporters;
using StaffOps.OtelHelper.Metrics;
using Xunit;

namespace StaffOps.OtelHelper.Tests;

/// <summary>Task C5 fix round (2026-11-10): the review findings, one test each.</summary>
[Collection("EnvVarTests")]
public class ResolverCoreFixRoundTests : IDisposable
{
    private static readonly string[] TouchedEnvVars =
    {
        "OTEL_EXPORTER_OTLP_ENDPOINT", "OTEL_EXPORTER_OTLP_HEADERS", "OTEL_EXPORTER_OTLP_METRICS_TEMPORALITY_PREFERENCE",
        "OTEL_EXPORTER_OTLP_PROTOCOL",
    };

    public ResolverCoreFixRoundTests() => ClearEnv();

    public void Dispose() => ClearEnv();

    private static void ClearEnv()
    {
        foreach (var v in TouchedEnvVars)
        {
            Environment.SetEnvironmentVariable(v, null);
        }
    }

    // ── HIGH 1: the response body is dropped by payload position ─────

    /// <summary>DN3: a body holding ". Exception: " never leaks (dev H1).</summary>
    [Fact]
    public void Response_Body_Is_Dropped_By_Payload_Position()
    {
        var text = SdkDiagnosticsForwarder.Format(SdkDiagnosticsForwarder.OtlpExporterSource, "HttpRequestFailed",
            "HTTP request to {0} failed. Response: {1}. Exception: {2}",
            new object?[] { "http://c:4318/v1/traces", "nope. Exception: {\"internal\":\"BODYLEAK\"}", "400 (Bad Request)" }, Array.Empty<string>());
        Assert.Equal("HTTP request to http://c:4318/v1/traces failed. Response: <response body omitted>. Exception: 400 (Bad Request)", text);

        // An empty body is kept as is, so the exception stays readable.
        Assert.Equal("HTTP request to x failed. Response: . Exception: e", SdkDiagnosticsForwarder.Format(SdkDiagnosticsForwarder.OtlpExporterSource,
            "HttpRequestFailed", "HTTP request to {0} failed. Response: {1}. Exception: {2}", new object?[] { "x", "", "e" }, Array.Empty<string>()));
    }

    [Theory]
    [InlineData("other Response: leak. Exception: {\"BODYLEAK\":1}", "other Response: <response body omitted>")]
    [InlineData("HTTP request to x failed. Response: . Exception: e", "HTTP request to x failed. Response: <response body omitted>")]
    public void Unstructured_Response_Text_Fails_Closed(string text, string expected)
    {
        // Only the forwarder's structured drop (bodyHandled) keeps the text after "Response: ".
        Assert.Equal(expected, SdkTextSanitizer.Sanitize(text, Array.Empty<string>()));
        Assert.Equal(text, SdkTextSanitizer.SanitizeWithForms(text, Array.Empty<string>(), bodyHandled: true));
    }

    [Fact]
    public void Echoed_Configuration_Values_Are_Cut()
    {
        var text = SdkDiagnosticsForwarder.Format("OpenTelemetry-Sdk", "InvalidConfigurationValue",
            "Configuration key '{0}' has an invalid value: '{1}'", new object?[] { "OTEL_X", new string('9', 500) }, Array.Empty<string>());
        Assert.Equal($"Configuration key 'OTEL_X' has an invalid value: '{new string('9', 64)}…'", text);
    }

    /// <summary>R8 (fix round 2): the value is masked and redacted before it is cut, so a long password keeps no prefix.</summary>
    [Fact]
    public void Echoed_Configuration_Value_Is_Masked_Before_It_Is_Cut()
    {
        var secret = string.Concat(Enumerable.Repeat("pw0123456789", 7)) + "abcd";
        var value = $"https://u:{secret}@h:bad";
        Environment.SetEnvironmentVariable("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", value);
        try
        {
            var text = SdkDiagnosticsForwarder.Format(SdkDiagnosticsForwarder.OtlpExporterSource, "InvalidConfigurationValue",
                "Configuration key '{0}' has an invalid value: '{1}'", new object?[] { "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", value }, Array.Empty<string>())!;
            for (var i = 0; i + 8 <= secret.Length; i++)
            {
                Assert.DoesNotContain(secret.Substring(i, 8), text);
            }

            // Also without the variable (code-set or unknown source): RedactText drops the userinfo first.
            Environment.SetEnvironmentVariable("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", null);
            var plain = SdkDiagnosticsForwarder.Format("OpenTelemetry-Sdk", "InvalidConfigurationValue",
                "Configuration key '{0}' has an invalid value: '{1}'", new object?[] { "K", value }, Array.Empty<string>())!;
            Assert.DoesNotContain(secret[..8], plain);
        }
        finally
        {
            Environment.SetEnvironmentVariable("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", null);
        }
    }

    // ── HIGH 2: metric temporality ───────────────────────────────────

    [Theory]
    [InlineData("delta", MetricReaderTemporalityPreference.Delta)]
    [InlineData("Delta", MetricReaderTemporalityPreference.Delta)]
    [InlineData("lowmemory", MetricReaderTemporalityPreference.LowMemory)]
    [InlineData("bogus", MetricReaderTemporalityPreference.Cumulative)]
    [InlineData(null, MetricReaderTemporalityPreference.Cumulative)]
    public void Otlp_Reader_Keeps_The_Temporality_Preference(string? value, MetricReaderTemporalityPreference expected)
    {
        Environment.SetEnvironmentVariable("OTEL_EXPORTER_OTLP_METRICS_TEMPORALITY_PREFERENCE", value);
        using var sp = TestPipelines.Build(o =>
        {
            o.OtelCollectorEndpoint = "http://127.0.0.1:1";
            o.PrometheusMetricsPort = 0;
        });
        var resolved = sp.GetRequiredService<ResolvedTelemetry>();
        var options = OtlpExporters.OptionsFor("metrics", resolved, new TestDiagnostics().Sink)!;
        using var reader = MetricsSetup.CreateOtlpReader(sp, resolved, options);
        Assert.Equal(expected, reader.TemporalityPreference);
    }

    [Fact]
    public void Sdk_Reader_Options_Still_Read_The_Metric_Export_Timeout()
    {
        Environment.SetEnvironmentVariable("OTEL_METRIC_EXPORT_TIMEOUT", "1234");
        try
        {
            using var sp = TestPipelines.Build(o => o.PrometheusMetricsPort = 0);
            var options = sp.GetRequiredService<IOptionsMonitor<MetricReaderOptions>>().Get(Options.DefaultName);
            Assert.Equal(1234, options.PeriodicExportingMetricReaderOptions.ExportTimeoutMilliseconds);
        }
        finally
        {
            Environment.SetEnvironmentVariable("OTEL_METRIC_EXPORT_TIMEOUT", null);
        }
    }

    [Fact]
    public void Invalid_Temporality_Warns_Naming_The_Variable_And_The_Default()
    {
        Environment.SetEnvironmentVariable("OTEL_EXPORTER_OTLP_METRICS_TEMPORALITY_PREFERENCE", "bogus");
        var diagnostics = new TestDiagnostics();
        Assert.Equal(MetricReaderTemporalityPreference.Cumulative, diagnostics.Resolve().MetricTemporality);
        Assert.Contains(diagnostics.Lines, l => l.Contains("invalid OTEL_EXPORTER_OTLP_METRICS_TEMPORALITY_PREFERENCE") && l.Contains("using cumulative"));
    }

    // ── MEDIUM 5: headers ────────────────────────────────────────────

    [Fact]
    public void Header_Items_With_Control_Characters_Or_Malformed_Are_Skipped_And_Warned_By_Name()
    {
        var diagnostics = new TestDiagnostics();
        var headers = HelperHttpClientFactory.ParseHeaders(
            "x-a=ok%0D%0AInjected: parity-value,x-%0Ab=1,x-c=2,broken-parity-secret,=x", diagnostics.Sink);
        Assert.Equal(new[] { "x-c=2" }, headers.Select(h => $"{h.Key}={h.Value}"));
        Assert.Contains(diagnostics.Lines, l => l.Contains("not printable ASCII") && l.Contains("item #1"));
        Assert.Equal(3, diagnostics.Lines.Count(l => l.Contains("malformed OTEL_EXPORTER_OTLP_HEADERS item was skipped")));
        Assert.DoesNotContain("parity-value", diagnostics.Text);
        Assert.DoesNotContain("parity-secret", diagnostics.Text);
        Assert.DoesNotContain("\n\n", diagnostics.Text);
    }

    // ── MEDIUM 6: invalid code options are never silent ──────────────

    [Fact]
    public void Early_Provider_Resolution_With_Invalid_Code_Options_Reports_One_Error()
    {
        var diagnostics = new TestDiagnostics();
        var services = new ServiceCollection();
        services.AddSingleton<IOtelHelperDiagnostics>(diagnostics.Sink);
        services.AddOtelHelper(o => o.OtlpProtocol = "bogus");
        using var sp = services.BuildServiceProvider();

        Assert.NotNull(sp.GetRequiredService<MeterProvider>());
        Assert.NotNull(sp.GetRequiredService<TracerProvider>());
        Assert.NotNull(sp.GetRequiredService<Microsoft.Extensions.Logging.ILoggerFactory>().CreateLogger("x"));
        var error = Assert.Single(diagnostics.Lines, l => l.Contains("invalid TelemetryOptions set in code"));
        Assert.StartsWith("otelhelper: ERROR ", error);
        Assert.Contains("TelemetryOptions.OtlpProtocol", error);
        Assert.Throws<OptionsValidationException>(() => sp.GetRequiredService<IOptions<TelemetryOptions>>().Value);
    }

    // ── MEDIUM 7: one forwarder per process ──────────────────────────

    [Fact]
    public void One_Forwarder_Writes_Each_Event_Once_To_The_Latest_Sink_And_Masks_Every_Live_Secret()
    {
        var before = SdkDiagnosticsForwarder.LiveLeases;
        var a = new TestDiagnostics();
        var b = new TestDiagnostics();
        var first = TestPipelines.Build(o => { o.OtelCollectorEndpoint = "http://ua:secret-a-123@127.0.0.1:1"; o.PrometheusMetricsPort = 0; }, a);
        var second = TestPipelines.Build(o => { o.OtelCollectorEndpoint = "http://127.0.0.1:1"; o.PrometheusMetricsPort = 0; }, b);
        try
        {
            Assert.Equal(before + 2, SdkDiagnosticsForwarder.LiveLeases);
            TestOtelEventSource.Log.ExportFailed("two-hosts", "secret-a-123");
            Assert.DoesNotContain(a.Lines, l => l.Contains("two-hosts"));
            var line = Assert.Single(b.Lines, l => l.Contains("export to two-hosts failed"));
            Assert.DoesNotContain("secret-a-123", line);

            second.Dispose();
            TestOtelEventSource.Log.ExportFailed("after-second", "x");
            Assert.Single(a.Lines, l => l.Contains("export to after-second failed"));
        }
        finally
        {
            second.Dispose();
            first.Dispose();
        }

        Assert.Equal(before, SdkDiagnosticsForwarder.LiveLeases);
    }

    // ── MEDIUM 8: narrowed drop of SDK parse warnings ────────────────

    [Theory]
    [InlineData("OTEL_EXPORTER_OTLP_ENDPOINT", true)]
    [InlineData("OTEL_EXPORTER_OTLP_PROTOCOL", true)]
    [InlineData("OTEL_EXPORTER_OTLP_COMPRESSION", true)]
    [InlineData("OTEL_EXPORTER_OTLP_TIMEOUT", true)]
    [InlineData("OTEL_EXPORTER_OTLP_TRACES_TIMEOUT", true)]
    [InlineData("OTEL_EXPORTER_OTLP_METRICS_COMPRESSION", true)]
    [InlineData("OTEL_EXPORTER_OTLP_LOGS_ENDPOINT", true)]
    [InlineData("OTEL_EXPORTER_OTLP_TRACES_PROTOCOL", true)]
    [InlineData("OTEL_EXPORTER_OTLP_TRACES_HEADERS", false)]
    [InlineData("OTEL_EXPORTER_OTLP_SPANS_TIMEOUT", false)]
    [InlineData("OTEL_EXPORTER_OTLP_HEADERS", false)]
    [InlineData("OTEL_EXPORTER_OTLP_CERTIFICATE", false)]
    [InlineData("OTEL_EXPORTER_OTLP_METRICS_TEMPORALITY_PREFERENCE", false)]
    public void Only_The_Resolved_Otlp_Keys_Are_Dropped(string key, bool dropped)
    {
        Assert.Equal(dropped, SdkTextSanitizer.IsNoise("OpenTelemetry-Exporter-OpenTelemetryProtocol", $"Configuration key '{key}' has an invalid value: 'x'"));
    }

    [Fact]
    public void Prometheus_Cancel_Is_Dropped_Only_When_Stopping()
    {
        const string cancel = "Canceled to export metrics: 'System.Threading.Tasks.TaskCanceledException: x'";
        Assert.True(SdkTextSanitizer.IsNoise("OpenTelemetry-Exporter-Prometheus-HttpListener", cancel, stopping: true));
        Assert.False(SdkTextSanitizer.IsNoise("OpenTelemetry-Exporter-Prometheus-HttpListener", cancel, stopping: false));
    }

    [Fact]
    public void View_Drop_Notices_Are_Not_Forwarded()
    {
        Assert.True(SdkTextSanitizer.IsNoise("OpenTelemetry-Sdk",
            "Measurements from Instrument 'a', Meter 'm' will be ignored. Reason: 'View configuration asks to drop this instrument.'. Suggested action: 'x'"));
    }

    // ── MEDIUM 9: the idle reader aggregates nothing ─────────────────

    [Theory]
    [InlineData("metrics", null)]
    [InlineData(null, "none")]
    public void No_Active_Exporter_Drops_Only_The_Helper_Instrumentation(string? disabledSignals, string? exporters)
    {
        var exported = new List<Metric>();
        var services = new ServiceCollection();
        services.AddSingleton<IOtelHelperDiagnostics>(new TestDiagnostics().Sink);
        services.AddOtelHelper(o =>
        {
            o.ServiceName = "idle-reader";
            o.DisabledSignals = disabledSignals;
            o.MetricExporters = exporters;
            o.PrometheusMetricsPort = 0;
        });
        // The application's own meters and reader.
        services.ConfigureOpenTelemetryMeterProvider(b => b.AddMeter("idle-reader", "idle-app", "System.Net.Http.parity").AddInMemoryExporter(exported));
        using var sp = services.BuildServiceProvider();
        var provider = sp.GetRequiredService<MeterProvider>();
        using var helperMeter = new Meter("idle-reader");
        using var appMeter = new Meter("idle-app");
        using var instrumentationLike = new Meter("System.Net.Http.parity");
        helperMeter.CreateCounter<long>("idle.helper").Add(1);
        appMeter.CreateCounter<long>("idle.app").Add(1);
        instrumentationLike.CreateCounter<long>("idle.http").Add(1);
        provider.ForceFlush();
        Assert.Contains(exported, m => m.Name == "idle.app");
        Assert.DoesNotContain(exported, m => m.Name == "idle.helper" || m.Name == "idle.http");
    }

    /// <summary>The HOW-TO recipe: `prometheus` with port 0 is an active exporter (mounted endpoint), not "off".</summary>
    [Fact]
    public void Prometheus_With_Port_Zero_Keeps_The_Helper_Meter()
    {
        var exported = new List<Metric>();
        var services = new ServiceCollection();
        services.AddSingleton<IOtelHelperDiagnostics>(new TestDiagnostics().Sink);
        services.AddOtelHelper(o =>
        {
            o.ServiceName = "mounted-endpoint";
            o.MetricExporters = "prometheus";
            o.PrometheusMetricsPort = 0;
        });
        services.ConfigureOpenTelemetryMeterProvider(b => b.AddInMemoryExporter(exported));
        using var sp = services.BuildServiceProvider();
        Assert.False(sp.GetRequiredService<ResolvedTelemetry>().PrometheusListenerActive);
        var provider = sp.GetRequiredService<MeterProvider>();
        using var meter = new Meter("mounted-endpoint");
        meter.CreateCounter<long>("mounted.hits").Add(1);
        provider.ForceFlush();
        Assert.Contains(exported, m => m.Name == "mounted.hits");
    }

    [Fact]
    public void Active_Exporter_Keeps_Instruments()
    {
        var exported = new List<Metric>();
        var services = new ServiceCollection();
        services.AddSingleton<IOtelHelperDiagnostics>(new TestDiagnostics().Sink);
        services.AddOtelHelper(o =>
        {
            o.ServiceName = "active-reader";
            o.OtelCollectorEndpoint = "http://127.0.0.1:1";
            o.MetricExporters = "otlp";
            o.PrometheusMetricsPort = 0;
        });
        services.ConfigureOpenTelemetryMeterProvider(b => b.AddInMemoryExporter(exported));
        using var sp = services.BuildServiceProvider();
        var provider = sp.GetRequiredService<MeterProvider>();
        using var meter = new Meter("active-reader");
        meter.CreateCounter<long>("active.hits").Add(1);
        provider.ForceFlush();
        Assert.Contains(exported, m => m.Name == "active.hits");
    }

    // ── MEDIUM 12: TLS custom root with an intermediate ──────────────

    // One whole-second instant per test (xUnit creates the class per test), and each level valid
    // strictly inside its issuer: two UtcNow reads a second apart made a leaf outlive its root
    // under load ("notAfter value ... is later than issuerCertificate.NotAfter", C6 fix round 1).
    private readonly DateTimeOffset _now = DateTimeOffset.FromUnixTimeSeconds(DateTimeOffset.UtcNow.ToUnixTimeSeconds());

    private (DateTimeOffset NotBefore, DateTimeOffset NotAfter) Validity(int level) => level switch
    {
        0 => (_now.AddMinutes(-10), _now.AddHours(3)),
        1 => (_now.AddMinutes(-5), _now.AddHours(2)),
        _ => (_now.AddMinutes(-5), _now.AddHours(1)),
    };

    /// <summary>A certificate at <paramref name="level"/>: 0 root, 1 intermediate, 2 leaf.</summary>
    private X509Certificate2 Issue(string name, X509Certificate2? issuer, bool ca, ECDsa key, int level)
    {
        var request = new CertificateRequest($"CN={name}", key, HashAlgorithmName.SHA256);
        request.CertificateExtensions.Add(new X509BasicConstraintsExtension(ca, false, 0, true));
        request.CertificateExtensions.Add(new X509SubjectKeyIdentifierExtension(request.PublicKey, false));
        var (notBefore, notAfter) = Validity(level);
        if (issuer is null)
        {
            return request.CreateSelfSigned(notBefore, notAfter);
        }

        var serial = new byte[8];
        RandomNumberGenerator.Fill(serial);
        using var signed = request.Create(issuer, notBefore, notAfter, serial);
        return signed.CopyWithPrivateKey(key);
    }

    [Fact]
    public void Custom_Root_Uses_The_Presented_Intermediate()
    {
        using var rootKey = ECDsa.Create(ECCurve.NamedCurves.nistP256);
        using var midKey = ECDsa.Create(ECCurve.NamedCurves.nistP256);
        using var leafKey = ECDsa.Create(ECCurve.NamedCurves.nistP256);
        using var root = Issue("parity-root", null, true, rootKey, 0);
        using var intermediate = Issue("parity-intermediate", root, true, midKey, 1);
        using var leaf = Issue("parity-leaf", intermediate, false, leafKey, 2);
        var roots = new X509Certificate2Collection { root };

        Assert.False(HelperHttpClientFactory.ValidateWithCustomRoot(leaf, SslPolicyErrors.RemoteCertificateChainErrors, roots));

        using var presented = new X509Chain();
        presented.ChainPolicy.TrustMode = X509ChainTrustMode.CustomRootTrust;
        presented.ChainPolicy.RevocationMode = X509RevocationMode.NoCheck;
        presented.ChainPolicy.CustomTrustStore.Add(root);
        presented.ChainPolicy.ExtraStore.Add(intermediate);
        presented.Build(leaf);
        Assert.True(HelperHttpClientFactory.ValidateWithCustomRoot(leaf, SslPolicyErrors.RemoteCertificateChainErrors, roots, presented));
    }

    // ── LOW: client disposal, user agent, fallback sink ──────────────

    [Fact]
    public void Exporter_Clients_Are_Disposed_With_The_Provider()
    {
        var sp = TestPipelines.Build(o => { o.OtelCollectorEndpoint = "http://127.0.0.1:1"; o.PrometheusMetricsPort = 0; });
        var clients = sp.GetRequiredService<ExporterClients>();
        Assert.True(clients.Count >= 2); // traces and metrics (the logger provider is built on first use)
        Assert.False(clients.Disposed);
        sp.Dispose();
        Assert.True(clients.Disposed);

        // The factory refuses to hand out a client after disposal.
        var resolved = TestDiagnostics.ResolveQuiet(o => o.OtelCollectorEndpoint = "http://127.0.0.1:1");
        var own = new ExporterClients();
        var options = OtlpExporters.OptionsFor("traces", resolved, new TestDiagnostics().Sink, own)!;
        var client = options.HttpClientFactory!();
        own.Dispose();
        Assert.Throws<ObjectDisposedException>(() => options.HttpClientFactory!());
        Assert.ThrowsAny<ObjectDisposedException>(() => client.CancelPendingRequests());
    }

    [Fact]
    public async Task Otlp_Requests_Carry_The_Sdk_User_Agent()
    {
        var port = TestPorts.FreeTcpPort();
        using var listener = new HttpListener();
        listener.Prefixes.Add($"http://localhost:{port}/");
        listener.Start();
        var agent = Task.Run(async () =>
        {
            var context = await listener.GetContextAsync();
            var value = context.Request.UserAgent;
            context.Response.StatusCode = 200;
            context.Response.Close();
            return value;
        });

        using (var sp = TestPipelines.Build(o =>
        {
            o.ServiceName = "ua-test";
            o.OtelCollectorEndpoint = $"http://localhost:{port}";
            o.OtlpProtocol = TelemetryOptions.ProtocolHttpProtobuf;
            o.PrometheusMetricsPort = 0;
            o.DisabledSignals = "metrics,logs";
        }))
        {
            using (var source = new System.Diagnostics.ActivitySource("ua-test"))
            using (source.StartActivity("span"))
            {
            }

            sp.GetRequiredService<TracerProvider>().ForceFlush();
        }

        var finished = await Task.WhenAny(agent, Task.Delay(TimeSpan.FromSeconds(10)));
        Assert.Same(agent, finished);
        Assert.StartsWith("OTel-OTLP-Exporter-Dotnet/", await agent);
    }

    [Fact]
    public void Double_Call_Without_A_Registered_Sink_Writes_To_The_Fallback()
    {
        var saved = OtelHelperDiagnostics.Fallback;
        var diagnostics = new TestDiagnostics();
        OtelHelperDiagnostics.Fallback = diagnostics.Sink;
        try
        {
            var services = new ServiceCollection();
            services.AddOtelHelper();
            services.AddOtelHelper();
            Assert.Single(diagnostics.Lines, l => l.Contains("AddOtelHelper was called more than once"));
        }
        finally
        {
            OtelHelperDiagnostics.Fallback = saved;
        }
    }

    // ── fix round 2 ──────────────────────────────────────────────────

    /// <summary>H2: a provider that does not forward still has its secrets masked in what another forwards.</summary>
    [Fact]
    public void Opted_Out_Host_Secrets_Are_Masked_By_The_Forwarding_Host()
    {
        var forwarding = new TestDiagnostics();
        var optedOut = new TestDiagnostics();
        using var b = TestPipelines.Build(o => { o.OtelCollectorEndpoint = "http://127.0.0.1:1"; o.PrometheusMetricsPort = 0; }, forwarding);
        using var a = TestPipelines.Build(o =>
        {
            o.OtelCollectorEndpoint = "http://127.0.0.1:1/?tok=opted-out-token-77";
            o.PrometheusMetricsPort = 0;
            o.ForwardSdkDiagnostics = false;
        }, optedOut);

        TestOtelEventSource.Log.ExportFailed("http://127.0.0.1:1/?tok=opted-out-token-77", "opted-out-token-77");
        var line = Assert.Single(forwarding.Lines, l => l.Contains("export to"));
        Assert.DoesNotContain("opted-out-token-77", line);
        Assert.DoesNotContain(optedOut.Lines, l => l.Contains("export to"));
    }

    /// <summary>M2: header values captured at setup stay masked after the environment changes.</summary>
    [Fact]
    public void Header_Values_Captured_At_Setup_Stay_Masked()
    {
        Environment.SetEnvironmentVariable("OTEL_EXPORTER_OTLP_HEADERS", "x-k=captured-secret-1");
        var diagnostics = new TestDiagnostics();
        using var sp = TestPipelines.Build(o => { o.OtelCollectorEndpoint = "http://127.0.0.1:1"; o.PrometheusMetricsPort = 0; }, diagnostics);
        Environment.SetEnvironmentVariable("OTEL_EXPORTER_OTLP_HEADERS", "x-k=rotated-secret-2");
        TestOtelEventSource.Log.ExportFailed("captured", "captured-secret-1 rotated-secret-2");
        var line = Assert.Single(diagnostics.Lines, l => l.Contains("export to captured"));
        Assert.DoesNotContain("captured-secret-1", line);
        Assert.DoesNotContain("rotated-secret-2", line);
    }

    /// <summary>M1: an env endpoint with :// that is not a usable http(s) URL warns and configures no endpoint.</summary>
    [Theory]
    [InlineData("http://collector:99999")]
    [InlineData("ftp://user:pw-secret@collector:21")]
    public void Unusable_Env_Url_Warns_And_Configures_No_Endpoint(string endpoint)
    {
        Environment.SetEnvironmentVariable("OTEL_EXPORTER_OTLP_ENDPOINT", endpoint);
        var diagnostics = new TestDiagnostics();
        var resolved = diagnostics.Resolve();
        Assert.Equal("", resolved.OtelCollectorEndpoint);
        var line = Assert.Single(diagnostics.Lines, l => l.Contains("; ignored variable=OTEL_EXPORTER_OTLP_ENDPOINT"));
        Assert.DoesNotContain("pw-secret", line);
    }

    /// <summary>M3: the Go HTML-escaped JSON form and that form JSON-escaped once more.</summary>
    [Fact]
    public void Masked_Forms_Include_Go_Html_Json()
    {
        var forms = SdkTextSanitizer.Forms(new[] { "a&b<c>d-secret" });
        Assert.Contains("a\\u0026b\\u003cc\\u003ed-secret", forms);
        Assert.Contains("a\\\\u0026b\\\\u003cc\\\\u003ed-secret", forms);
        Assert.Equal("x <redacted> y", SdkTextSanitizer.MaskLiterals("x a\\u0026b\\u003cc\\u003ed-secret y", forms));
    }

    [Fact]
    public void Header_Name_Rejected_By_The_Client_Is_Warned_By_Name()
    {
        Environment.SetEnvironmentVariable("OTEL_EXPORTER_OTLP_HEADERS", "content-type=v-secret,x-ok=1");
        var diagnostics = new TestDiagnostics();
        var resolved = diagnostics.Resolve(o => o.OtelCollectorEndpoint = "http://127.0.0.1:1");
        using var client = OtlpExporters.OptionsFor("traces", resolved, diagnostics.Sink)!.HttpClientFactory!();
        Assert.True(client.DefaultRequestHeaders.Contains("x-ok"));
        var line = Assert.Single(diagnostics.Lines, l => l.Contains("rejected by the HTTP client"));
        Assert.Contains("key=content-type", line);
        Assert.DoesNotContain("v-secret", diagnostics.Text);
    }

    /// <summary>H3: a code Configure&lt;MetricReaderOptions&gt; wins over the environment temporality.</summary>
    [Fact]
    public void Code_Reader_Options_Win_Over_The_Temporality_Variable()
    {
        Environment.SetEnvironmentVariable("OTEL_EXPORTER_OTLP_METRICS_TEMPORALITY_PREFERENCE", "cumulative");
        var services = new ServiceCollection();
        services.AddSingleton<IOtelHelperDiagnostics>(new TestDiagnostics().Sink);
        services.AddOtelHelper(o => { o.OtelCollectorEndpoint = "http://127.0.0.1:1"; o.PrometheusMetricsPort = 0; });
        services.Configure<MetricReaderOptions>(o => o.TemporalityPreference = MetricReaderTemporalityPreference.Delta);
        using var sp = services.BuildServiceProvider();
        var resolved = sp.GetRequiredService<ResolvedTelemetry>();
        var options = OtlpExporters.OptionsFor("metrics", resolved, new TestDiagnostics().Sink)!;
        using var reader = MetricsSetup.CreateOtlpReader(sp, resolved, options);
        Assert.Equal(MetricReaderTemporalityPreference.Delta, reader.TemporalityPreference);
    }

    /// <summary>A real 400 answer whose body carries a token: the token never reaches the diagnostics.</summary>
    [Fact]
    public async Task Error_Response_Body_Token_Never_Reaches_The_Diagnostics()
    {
        var port = TestPorts.FreeTcpPort();
        using var listener = new HttpListener();
        listener.Prefixes.Add($"http://localhost:{port}/");
        listener.Start();
        var serve = Task.Run(async () =>
        {
            var context = await listener.GetContextAsync();
            var body = System.Text.Encoding.UTF8.GetBytes("{\"error\":\"bad\",\"echo\":\"wire-body-token-4242\"}");
            context.Response.StatusCode = 400;
            await context.Response.OutputStream.WriteAsync(body);
            context.Response.Close();
        });

        var diagnostics = new TestDiagnostics();
        using (var sp = TestPipelines.Build(o =>
        {
            o.ServiceName = "wire-body";
            o.OtelCollectorEndpoint = $"http://localhost:{port}";
            o.OtlpProtocol = TelemetryOptions.ProtocolHttpProtobuf;
            o.PrometheusMetricsPort = 0;
            o.DisabledSignals = "metrics,logs";
        }, diagnostics))
        {
            using (var source = new System.Diagnostics.ActivitySource("wire-body"))
            using (source.StartActivity("span"))
            {
            }

            sp.GetRequiredService<TracerProvider>().ForceFlush();
            await Task.WhenAny(serve, Task.Delay(TimeSpan.FromSeconds(10)));
            for (var i = 0; i < 100 && !diagnostics.Lines.Any(l => l.Contains("HTTP request to")); i++)
            {
                await Task.Delay(50);
            }
        }

        Assert.Contains(diagnostics.Lines, l => l.Contains("HTTP request to") && l.Contains("<response body omitted>"));
        Assert.DoesNotContain("wire-body-token-4242", diagnostics.Text);
    }

    // ── fix round 3: histogram aggregation, option (c) ───────────────

    private const string HistogramVariable = "OTEL_EXPORTER_OTLP_METRICS_DEFAULT_HISTOGRAM_AGGREGATION";

    /// <summary>Reads the protobuf fields of a message: (field number, wire type, length-delimited payload or null).</summary>
    private static IEnumerable<(int Field, byte[]? Payload)> ProtoFields(byte[] message)
    {
        var i = 0;
        ulong Varint()
        {
            ulong value = 0;
            for (var shift = 0; ; shift += 7)
            {
                var b = message[i++];
                value |= (ulong)(b & 0x7f) << shift;
                if ((b & 0x80) == 0)
                {
                    return value;
                }
            }
        }

        var fields = new List<(int, byte[]?)>();
        while (i < message.Length)
        {
            var key = Varint();
            var field = (int)(key >> 3);
            switch ((int)(key & 7))
            {
                case 0:
                    Varint();
                    fields.Add((field, null));
                    break;
                case 1:
                    i += 8;
                    fields.Add((field, null));
                    break;
                case 2:
                    var length = (int)Varint();
                    fields.Add((field, message[i..(i + length)]));
                    i += length;
                    break;
                case 5:
                    i += 4;
                    fields.Add((field, null));
                    break;
                default:
                    throw new InvalidDataException("unsupported wire type");
            }
        }

        return fields;
    }

    /// <summary>The data field number (9 histogram, 10 exponential histogram, …) of the metric named <paramref name="name"/> in an ExportMetricsServiceRequest.</summary>
    private static int? MetricDataField(byte[] request, string name)
    {
        foreach (var (rf, resourceMetrics) in ProtoFields(request))
        {
            if (rf != 1 || resourceMetrics is null) continue;
            foreach (var (sf, scopeMetrics) in ProtoFields(resourceMetrics))
            {
                if (sf != 2 || scopeMetrics is null) continue;
                foreach (var (mf, metric) in ProtoFields(scopeMetrics))
                {
                    if (mf != 2 || metric is null) continue;
                    var fields = ProtoFields(metric).ToList();
                    if (fields.Any(f => f.Field == 1 && f.Payload is not null && System.Text.Encoding.UTF8.GetString(f.Payload) == name))
                    {
                        return fields.Select(f => f.Field).FirstOrDefault(f => f is >= 5 and <= 11);
                    }
                }
            }
        }

        return null;
    }

    private static async Task<int?> ExportHistogramAsync(string? aggregation)
    {
        var diagnostics = new TestDiagnostics();
        Environment.SetEnvironmentVariable(HistogramVariable, aggregation);
        try
        {
            var port = TestPorts.FreeTcpPort();
            using var listener = new HttpListener();
            listener.Prefixes.Add($"http://localhost:{port}/");
            listener.Start();
            var bodies = new System.Collections.Concurrent.ConcurrentQueue<byte[]>();
            _ = Task.Run(async () =>
            {
                while (true)
                {
                    HttpListenerContext context;
                    try { context = await listener.GetContextAsync(); } catch { return; }
                    using var buffer = new MemoryStream();
                    // gzip is the default compression since task C8.
                    var body = context.Request.Headers["Content-Encoding"] == "gzip"
                        ? new System.IO.Compression.GZipStream(context.Request.InputStream, System.IO.Compression.CompressionMode.Decompress)
                        : context.Request.InputStream;
                    await body.CopyToAsync(buffer);
                    if (context.Request.Url!.AbsolutePath.EndsWith("/v1/metrics", StringComparison.Ordinal))
                    {
                        bodies.Enqueue(buffer.ToArray());
                    }

                    context.Response.StatusCode = 200;
                    context.Response.Close();
                }
            });

            using (var sp = TestPipelines.Build(o =>
            {
                o.ServiceName = "hist-test";
                o.OtelCollectorEndpoint = $"http://localhost:{port}";
                o.OtlpProtocol = TelemetryOptions.ProtocolHttpProtobuf;
                o.PrometheusMetricsPort = 0;
                o.DisabledSignals = "traces,logs";
                o.ExportIntervalMs = 3_600_000;
            }, diagnostics))
            {
                Assert.Equal(aggregation is not null, sp.GetService<MetricsSetup.HistogramAggregationPath>() is not null);
                using var meter = new Meter("hist-test");
                meter.CreateHistogram<double>("hist.latency").Record(12.5);
                sp.GetRequiredService<MeterProvider>().ForceFlush();
            }

            await Task.Delay(200); // let the last request finish in the listener loop
            listener.Stop();
            // Without UseOtlpExporter() neither the doubling warning nor the (c)-path ERROR appears.
            Assert.DoesNotContain(diagnostics.Lines, l => l.Contains("UseOtlpExporter()"));
            return bodies.Select(b => MetricDataField(b, "hist.latency")).FirstOrDefault(f => f is not null);
        }
        finally
        {
            Environment.SetEnvironmentVariable(HistogramVariable, null);
        }
    }

    /// <summary>Owner decision (c): with the variable, the SDK's own OTLP reader path sends an exponential histogram (Metric field 10).</summary>
    [Fact]
    public async Task Histogram_Aggregation_Exponential_Reaches_The_Wire()
    {
        Assert.Equal(10, await ExportHistogramAsync("base2_exponential_bucket_histogram"));
    }

    [Fact]
    public async Task Histogram_Aggregation_Unset_Stays_Explicit_Bucket()
    {
        Assert.Equal(9, await ExportHistogramAsync(null));
    }

    [Fact]
    public async Task Histogram_Aggregation_Path_With_Inactive_Otlp_Sends_Nothing_And_Warns_Once()
    {
        Environment.SetEnvironmentVariable(HistogramVariable, "base2_exponential_bucket_histogram");
        try
        {
            var port = TestPorts.FreeTcpPort();
            using var listener = new HttpListener();
            listener.Prefixes.Add($"http://localhost:{port}/");
            listener.Start();
            var requests = 0;
            _ = Task.Run(async () =>
            {
                while (true)
                {
                    try { var c = await listener.GetContextAsync(); Interlocked.Increment(ref requests); c.Response.Close(); } catch { return; }
                }
            });

            var diagnostics = new TestDiagnostics();
            var answeredBefore = MetricsSetup.NoNetworkHandler.Answered;
            using (var sp = TestPipelines.Build(o =>
            {
                o.ServiceName = "hist-inactive";
                o.OtelCollectorEndpoint = $"http://localhost:{port}";
                o.OtlpProtocol = TelemetryOptions.ProtocolHttpProtobuf;
                o.MetricExporters = "prometheus";
                o.PrometheusMetricsPort = 0;
                o.DisabledSignals = "traces,logs";
            }, diagnostics))
            {
                using var meter = new Meter("hist-inactive");
                meter.CreateHistogram<double>("hist.idle").Record(1);
                sp.GetRequiredService<MeterProvider>().ForceFlush();
            }

            await Task.Delay(200);
            listener.Stop();
            Assert.Equal(0, requests);
            Assert.True(MetricsSetup.NoNetworkHandler.Answered > answeredBefore);
            Assert.Single(diagnostics.Lines, l => l.Contains(HistogramVariable + " is set but OTLP metrics are not active"));
        }
        finally
        {
            Environment.SetEnvironmentVariable(HistogramVariable, null);
        }
    }

    [Fact]
    public void Histogram_Aggregation_Path_Honors_Temporality_Interval_And_Code_Reader_Options()
    {
        Environment.SetEnvironmentVariable(HistogramVariable, "base2_exponential_bucket_histogram");
        Environment.SetEnvironmentVariable("OTEL_EXPORTER_OTLP_METRICS_TEMPORALITY_PREFERENCE", "delta");
        try
        {
            using (var sp = TestPipelines.Build(o => { o.OtelCollectorEndpoint = "http://127.0.0.1:1"; o.PrometheusMetricsPort = 0; o.ExportIntervalMs = 4321; }))
            {
                var named = sp.GetRequiredService<IOptionsMonitor<MetricReaderOptions>>().Get(MetricsSetup.HistogramPathName);
                Assert.Equal(MetricReaderTemporalityPreference.Delta, named.TemporalityPreference);
                Assert.Equal(4321, named.PeriodicExportingMetricReaderOptions.ExportIntervalMilliseconds);
            }

            var services = new ServiceCollection();
            services.AddSingleton<IOtelHelperDiagnostics>(new TestDiagnostics().Sink);
            services.AddOtelHelper(o => { o.OtelCollectorEndpoint = "http://127.0.0.1:1"; o.PrometheusMetricsPort = 0; });
            services.Configure<MetricReaderOptions>(MetricsSetup.HistogramPathName, o =>
            {
                o.TemporalityPreference = MetricReaderTemporalityPreference.LowMemory;
                o.PeriodicExportingMetricReaderOptions.ExportIntervalMilliseconds = 1234;
            });
            using var app = services.BuildServiceProvider();
            var withCode = app.GetRequiredService<IOptionsMonitor<MetricReaderOptions>>().Get(MetricsSetup.HistogramPathName);
            Assert.Equal(MetricReaderTemporalityPreference.LowMemory, withCode.TemporalityPreference);
            Assert.Equal(1234, withCode.PeriodicExportingMetricReaderOptions.ExportIntervalMilliseconds);
        }
        finally
        {
            Environment.SetEnvironmentVariable(HistogramVariable, null);
            Environment.SetEnvironmentVariable("OTEL_EXPORTER_OTLP_METRICS_TEMPORALITY_PREFERENCE", null);
        }
    }

    [Fact]
    public void Histogram_Aggregation_Variable_In_A_Registered_IConfiguration_Is_Seen()
    {
        var services = new ServiceCollection();
        services.AddSingleton<Microsoft.Extensions.Configuration.IConfiguration>(new Microsoft.Extensions.Configuration.ConfigurationBuilder()
            .AddInMemoryCollection(new Dictionary<string, string?> { [HistogramVariable] = "base2_exponential_bucket_histogram" }).Build());
        Assert.True(MetricsSetup.HistogramAggregationRequested(services));
        Assert.False(MetricsSetup.HistogramAggregationRequested(new ServiceCollection()));
    }

    // ── fix round 3: forwarder transitions, EKU, quiet meters ────────

    [Fact]
    public void Concurrent_Acquire_And_Release_Keep_The_Listener_Iff_Leases_Live()
    {
        var before = SdkDiagnosticsForwarder.LiveLeases;
        var failures = 0;
        void Loop()
        {
            for (var i = 0; i < 1500; i++)
            {
                var lease = SdkDiagnosticsForwarder.Acquire(new OtelHelperDiagnostics(() => TextWriter.Null), Array.Empty<string>());
                if (!SdkDiagnosticsForwarder.HasListener)
                {
                    Interlocked.Increment(ref failures);
                }

                lease.Dispose();
            }
        }

        var threads = new[] { new Thread(Loop), new Thread(Loop) };
        foreach (var t in threads) t.Start();
        foreach (var t in threads) t.Join();
        Assert.Equal(0, failures);
        Assert.Equal(before, SdkDiagnosticsForwarder.LiveLeases);
        Assert.Equal(before > 0, SdkDiagnosticsForwarder.HasListener);
    }

    [Fact]
    public void Custom_Root_Rejects_A_Client_Auth_Only_Leaf()
    {
        using var rootKey = ECDsa.Create(ECCurve.NamedCurves.nistP256);
        using var leafKey = ECDsa.Create(ECCurve.NamedCurves.nistP256);
        using var root = Issue("parity-eku-root", null, true, rootKey, 0);
        var request = new CertificateRequest("CN=parity-client-only", leafKey, HashAlgorithmName.SHA256);
        request.CertificateExtensions.Add(new X509EnhancedKeyUsageExtension(new OidCollection { new Oid("1.3.6.1.5.5.7.3.2") }, false));
        var serial = new byte[8];
        RandomNumberGenerator.Fill(serial);
        var (notBefore, notAfter) = Validity(2);
        using var signed = request.Create(root, notBefore, notAfter, serial);
        Assert.False(HelperHttpClientFactory.ValidateWithCustomRoot(signed, SslPolicyErrors.RemoteCertificateChainErrors, new X509Certificate2Collection { root }));
    }

    [Theory]
    [InlineData("MetricInstrumentIgnored", "quiet-svc", "Instrument belongs to a Meter not subscribed by the provider.", true)]
    [InlineData("MetricInstrumentIgnored", "quiet-svc", "View configuration asks to drop this instrument.", false)]
    [InlineData("MetricInstrumentIgnored", "other", "Instrument belongs to a Meter not subscribed by the provider.", false)]
    [InlineData("MetricViewIgnored", "quiet-svc", "Instrument belongs to a Meter not subscribed by the provider.", false)]
    public void Only_The_Not_Subscribed_Notice_For_A_Quiet_Meter_Is_Quiet(string eventName, string meter, string reason, bool quiet)
    {
        var payload = new object?[] { "c", meter, reason, "x" };
        Assert.Equal(quiet, SdkTextSanitizer.IsQuietMeterNotice(eventName, payload, new[] { "quiet-svc" }));
        Assert.False(SdkTextSanitizer.IsQuietMeterNotice(eventName, payload, Array.Empty<string>()));
    }

    [Fact]
    public void Recording_On_The_Keyed_Meter_With_Metrics_Off_Prints_No_Ignored_Notice()
    {
        var diagnostics = new TestDiagnostics();
        using var sp = TestPipelines.Build(o => { o.ServiceName = "quiet-svc"; o.DisabledSignals = "metrics"; o.PrometheusMetricsPort = 0; }, diagnostics);
        var meter = sp.GetRequiredKeyedService<Meter>(OtelHelperKeys.Instrumentation);
        meter.CreateCounter<long>("quiet.hits").Add(1);
        sp.GetRequiredService<MeterProvider>().ForceFlush();
        Assert.DoesNotContain(diagnostics.Lines, l => l.Contains("will be ignored") && l.Contains("quiet-svc"));

        // Positive control: the same scenario with no quiet meters delivers the SDK notice.
        sp.GetRequiredService<SdkDiagnosticsForwarder.Lease>().QuietMeters = Array.Empty<string>();
        meter.CreateCounter<long>("quiet.control").Add(1);
        Assert.Contains(diagnostics.Lines, l => l.Contains("will be ignored") && l.Contains("quiet.control") && l.Contains("not subscribed"));
    }

    // ── fix round 4 ──────────────────────────────────────────────────

    [Fact]
    public void Custom_Root_Accepts_The_Same_Leaf_With_Server_Auth()
    {
        using var rootKey = ECDsa.Create(ECCurve.NamedCurves.nistP256);
        using var leafKey = ECDsa.Create(ECCurve.NamedCurves.nistP256);
        using var root = Issue("parity-eku-root-ok", null, true, rootKey, 0);
        var request = new CertificateRequest("CN=parity-server", leafKey, HashAlgorithmName.SHA256);
        request.CertificateExtensions.Add(new X509EnhancedKeyUsageExtension(new OidCollection { new Oid("1.3.6.1.5.5.7.3.1") }, false));
        var serial = new byte[8];
        RandomNumberGenerator.Fill(serial);
        var (notBefore, notAfter) = Validity(2);
        using var signed = request.Create(root, notBefore, notAfter, serial);
        Assert.True(HelperHttpClientFactory.ValidateWithCustomRoot(signed, SslPolicyErrors.RemoteCertificateChainErrors, new X509Certificate2Collection { root }));
    }

    /// <summary>An appsettings-only value (a host registers IConfiguration as a factory) is warned once, not applied.</summary>
    [Fact]
    public async Task Histogram_Aggregation_Only_In_Host_Configuration_Warns_Once()
    {
        var diagnostics = new TestDiagnostics();
        var builder = Microsoft.Extensions.Hosting.Host.CreateApplicationBuilder();
        builder.Configuration.AddInMemoryCollection(new Dictionary<string, string?> { [HistogramVariable] = "base2_exponential_bucket_histogram" });
        builder.Services.AddSingleton<IOtelHelperDiagnostics>(diagnostics.Sink);
        builder.Services.AddOtelHelper(o => o.PrometheusMetricsPort = 0);
        using var host = builder.Build();
        await host.StartAsync();
        await host.StopAsync();
        Assert.Null(host.Services.GetService<MetricsSetup.HistogramAggregationPath>());
        Assert.Single(diagnostics.Lines, l => l.Contains(HistogramVariable + " is set only in IConfiguration"));
    }

    [Theory]
    [InlineData("base2_exponential_bucket_histgram", false, true)]
    [InlineData("explicit_bucket_histogram", false, false)]
    [InlineData("EXPLICIT_BUCKET_HISTOGRAM", false, false)]
    [InlineData("Base2_Exponential_Bucket_Histogram", true, false)]
    public void Histogram_Aggregation_Grammar(string value, bool path, bool warns)
    {
        Environment.SetEnvironmentVariable(HistogramVariable, value);
        try
        {
            var diagnostics = new TestDiagnostics();
            using var sp = TestPipelines.Build(o => { o.OtelCollectorEndpoint = "http://127.0.0.1:1"; o.PrometheusMetricsPort = 0; }, diagnostics);
            Assert.Equal(path, sp.GetService<MetricsSetup.HistogramAggregationPath>() is not null);
            Assert.Equal(warns, diagnostics.Lines.Any(l => l.Contains("invalid " + HistogramVariable)));
        }
        finally
        {
            Environment.SetEnvironmentVariable(HistogramVariable, null);
        }
    }

    [Fact]
    public void Histogram_Path_Starts_From_The_Default_Named_Reader_Options()
    {
        Environment.SetEnvironmentVariable(HistogramVariable, "base2_exponential_bucket_histogram");
        Environment.SetEnvironmentVariable("OTEL_METRIC_EXPORT_TIMEOUT", "2345");
        try
        {
            var services = new ServiceCollection();
            services.AddSingleton<IOtelHelperDiagnostics>(new TestDiagnostics().Sink);
            services.AddOtelHelper(o => { o.OtelCollectorEndpoint = "http://127.0.0.1:1"; o.PrometheusMetricsPort = 0; o.ExportTimeoutMs = 4321; });
            services.Configure<MetricReaderOptions>(o => o.TemporalityPreference = MetricReaderTemporalityPreference.LowMemory);
            using var sp = services.BuildServiceProvider();
            var named = sp.GetRequiredService<IOptionsMonitor<MetricReaderOptions>>().Get(MetricsSetup.HistogramPathName);
            Assert.Equal(MetricReaderTemporalityPreference.LowMemory, named.TemporalityPreference);
            // The export deadline of metrics (task C8, A18): OTEL_METRIC_EXPORT_TIMEOUT is not honored.
            Assert.Equal(4321, named.PeriodicExportingMetricReaderOptions.ExportTimeoutMilliseconds);
            Assert.Equal(TelemetryOptions.DefaultExportIntervalMs, named.PeriodicExportingMetricReaderOptions.ExportIntervalMilliseconds);
        }
        finally
        {
            Environment.SetEnvironmentVariable(HistogramVariable, null);
            Environment.SetEnvironmentVariable("OTEL_METRIC_EXPORT_TIMEOUT", null);
        }
    }

    [Fact]
    public void Histogram_Path_Numeric_Temporality_Is_Cumulative_As_Warned_And_Idle_Reader_Only_Collects()
    {
        Environment.SetEnvironmentVariable(HistogramVariable, "base2_exponential_bucket_histogram");
        Environment.SetEnvironmentVariable("OTEL_EXPORTER_OTLP_METRICS_TEMPORALITY_PREFERENCE", "2");
        try
        {
            var diagnostics = new TestDiagnostics();
            using (var sp = TestPipelines.Build(o => { o.OtelCollectorEndpoint = "http://127.0.0.1:1"; o.PrometheusMetricsPort = 0; }, diagnostics))
            {
                var named = sp.GetRequiredService<IOptionsMonitor<MetricReaderOptions>>().Get(MetricsSetup.HistogramPathName);
                Assert.Equal(MetricReaderTemporalityPreference.Cumulative, named.TemporalityPreference);
                Assert.Contains(diagnostics.Lines, l => l.Contains("invalid OTEL_EXPORTER_OTLP_METRICS_TEMPORALITY_PREFERENCE") && l.Contains("using cumulative"));
            }

            using var idle = TestPipelines.Build(o => { o.MetricExporters = "prometheus"; o.PrometheusMetricsPort = 0; });
            var idleOptions = idle.GetRequiredService<IOptionsMonitor<MetricReaderOptions>>().Get(MetricsSetup.HistogramPathName);
            Assert.Equal(int.MaxValue, idleOptions.PeriodicExportingMetricReaderOptions.ExportIntervalMilliseconds);
        }
        finally
        {
            Environment.SetEnvironmentVariable(HistogramVariable, null);
            Environment.SetEnvironmentVariable("OTEL_EXPORTER_OTLP_METRICS_TEMPORALITY_PREFERENCE", null);
        }
    }

    [Fact]
    public void Histogram_Path_Idle_Warning_Is_Skipped_When_Code_Options_Are_Invalid()
    {
        Environment.SetEnvironmentVariable(HistogramVariable, "base2_exponential_bucket_histogram");
        try
        {
            var diagnostics = new TestDiagnostics();
            var services = new ServiceCollection();
            services.AddSingleton<IOtelHelperDiagnostics>(diagnostics.Sink);
            services.AddOtelHelper(o => o.OtlpProtocol = "bogus");
            using var sp = services.BuildServiceProvider();
            sp.GetRequiredService<MeterProvider>();
            Assert.Single(diagnostics.Lines, l => l.Contains("invalid TelemetryOptions set in code"));
            Assert.DoesNotContain(diagnostics.Lines, l => l.Contains("OTLP metrics are not active"));
        }
        finally
        {
            Environment.SetEnvironmentVariable(HistogramVariable, null);
        }
    }

    // ── fix round 5: UseOtlpExporter() next to AddOtelHelper ─────────

    [Theory]
    [InlineData(true)]
    [InlineData(false)]
    public void UseOtlpExporter_Next_To_AddOtelHelper_Warns_About_Doubled_Export(bool before)
    {
        var diagnostics = new TestDiagnostics();
        var services = new ServiceCollection();
        services.AddSingleton<IOtelHelperDiagnostics>(diagnostics.Sink);
        if (before)
        {
            services.AddOpenTelemetry().UseOtlpExporter();
        }

        services.AddOtelHelper(o => { o.OtelCollectorEndpoint = "http://127.0.0.1:1"; o.PrometheusMetricsPort = 0; });
        if (!before)
        {
            services.AddOpenTelemetry().UseOtlpExporter();
        }

        using var sp = services.BuildServiceProvider();
        sp.GetRequiredService<ResolvedTelemetry>();
        Assert.Single(diagnostics.Lines, l => l.Contains("UseOtlpExporter() with AddOtelHelper exports every signal twice; drop one"));
    }

    [Fact]
    public void Without_UseOtlpExporter_No_Doubling_Warning()
    {
        var diagnostics = new TestDiagnostics();
        diagnostics.Resolve();
        using var sp = TestPipelines.Build(o => o.PrometheusMetricsPort = 0, diagnostics);
        Assert.DoesNotContain(diagnostics.Lines, l => l.Contains("exports every signal twice"));
    }

    [Fact]
    public void Histogram_Path_Is_Not_Registered_When_UseOtlpExporter_Comes_First()
    {
        Environment.SetEnvironmentVariable(HistogramVariable, "base2_exponential_bucket_histogram");
        try
        {
            var diagnostics = new TestDiagnostics();
            var services = new ServiceCollection();
            services.AddSingleton<IOtelHelperDiagnostics>(diagnostics.Sink);
            services.AddOpenTelemetry().UseOtlpExporter(OpenTelemetry.Exporter.OtlpExportProtocol.HttpProtobuf, new Uri("http://127.0.0.1:1"));
            services.AddOtelHelper(o => o.PrometheusMetricsPort = 0);
            using var sp = services.BuildServiceProvider();
            Assert.Null(sp.GetService<MetricsSetup.HistogramAggregationPath>());
            Assert.NotNull(sp.GetRequiredService<MeterProvider>());
            Assert.Single(diagnostics.Lines, l => l.Contains("its own reader applies " + HistogramVariable));
            Assert.DoesNotContain(diagnostics.Lines, l => l.Contains("is set only in IConfiguration"));
        }
        finally
        {
            Environment.SetEnvironmentVariable(HistogramVariable, null);
        }
    }

    [Fact]
    public void Histogram_Path_With_UseOtlpExporter_After_Reports_One_Error_Before_The_Sdk_Throws()
    {
        Environment.SetEnvironmentVariable(HistogramVariable, "base2_exponential_bucket_histogram");
        try
        {
            var diagnostics = new TestDiagnostics();
            var services = new ServiceCollection();
            services.AddSingleton<IOtelHelperDiagnostics>(diagnostics.Sink);
            services.AddOtelHelper(o => { o.OtelCollectorEndpoint = "http://127.0.0.1:1"; o.PrometheusMetricsPort = 0; });
            services.AddOpenTelemetry().UseOtlpExporter(OpenTelemetry.Exporter.OtlpExportProtocol.HttpProtobuf, new Uri("http://127.0.0.1:1"));
            using var sp = services.BuildServiceProvider();
            Assert.ThrowsAny<NotSupportedException>(() => sp.GetRequiredService<MeterProvider>());
            var error = Assert.Single(diagnostics.Lines, l => l.StartsWith("otelhelper: ERROR ", StringComparison.Ordinal) && l.Contains("UseOtlpExporter()"));
            Assert.Contains(HistogramVariable, error);
        }
        finally
        {
            Environment.SetEnvironmentVariable(HistogramVariable, null);
        }
    }

    [Fact]
    public void Host_Configuration_Warning_Is_Skipped_With_UseOtlpExporter()
    {
        var diagnostics = new TestDiagnostics();
        var builder = Microsoft.Extensions.Hosting.Host.CreateApplicationBuilder();
        builder.Configuration.AddInMemoryCollection(new Dictionary<string, string?> { [HistogramVariable] = "base2_exponential_bucket_histogram" });
        builder.Services.AddSingleton<IOtelHelperDiagnostics>(diagnostics.Sink);
        builder.Services.AddOtelHelper(o => { o.OtelCollectorEndpoint = "http://127.0.0.1:1"; o.PrometheusMetricsPort = 0; });
        builder.Services.AddOpenTelemetry().UseOtlpExporter(OpenTelemetry.Exporter.OtlpExportProtocol.HttpProtobuf, new Uri("http://127.0.0.1:1"));
        using var host = builder.Build();
        host.Services.GetRequiredService<ResolvedTelemetry>();
        Assert.DoesNotContain(diagnostics.Lines, l => l.Contains("is set only in IConfiguration") || l.Contains("differs between"));
        Assert.Single(diagnostics.Lines, l => l.Contains("exports every signal twice"));
    }

    [Fact]
    public void Host_Configuration_Value_Differing_From_The_Environment_Names_The_Used_Value()
    {
        Environment.SetEnvironmentVariable(HistogramVariable, "explicit_bucket_histogram");
        try
        {
            var diagnostics = new TestDiagnostics();
            var builder = Microsoft.Extensions.Hosting.Host.CreateApplicationBuilder();
            builder.Configuration.AddInMemoryCollection(new Dictionary<string, string?> { [HistogramVariable] = "base2_exponential_bucket_histogram" });
            builder.Services.AddSingleton<IOtelHelperDiagnostics>(diagnostics.Sink);
            builder.Services.AddOtelHelper(o => o.PrometheusMetricsPort = 0);
            using var host = builder.Build();
            host.Services.GetRequiredService<ResolvedTelemetry>();
            Assert.Null(host.Services.GetService<MetricsSetup.HistogramAggregationPath>());
            var line = Assert.Single(diagnostics.Lines, l => l.Contains("differs between IConfiguration and the process environment"));
            Assert.Contains("used=explicit_bucket_histogram", line);
        }
        finally
        {
            Environment.SetEnvironmentVariable(HistogramVariable, null);
        }
    }

    [Fact]
    public void Padded_Histogram_Value_Registers_No_Path_And_Warns()
    {
        Environment.SetEnvironmentVariable(HistogramVariable, " base2_exponential_bucket_histogram");
        try
        {
            var diagnostics = new TestDiagnostics();
            using var sp = TestPipelines.Build(o => { o.OtelCollectorEndpoint = "http://127.0.0.1:1"; o.PrometheusMetricsPort = 0; }, diagnostics);
            Assert.Null(sp.GetService<MetricsSetup.HistogramAggregationPath>());
            Assert.Single(diagnostics.Lines, l => l.Contains("invalid " + HistogramVariable) && l.Contains("without trimming"));
        }
        finally
        {
            Environment.SetEnvironmentVariable(HistogramVariable, null);
        }
    }

    // ── round 6 ──────────────────────────────────────────────────────

    [Fact]
    public void UseOtlpExporter_Without_A_Helper_Otlp_Target_Gives_No_Doubling_Warning()
    {
        var diagnostics = new TestDiagnostics();
        var services = new ServiceCollection();
        services.AddSingleton<IOtelHelperDiagnostics>(diagnostics.Sink);
        services.AddOtelHelper(o => o.PrometheusMetricsPort = 0); // no endpoint: Prometheus only
        services.AddOpenTelemetry().UseOtlpExporter(OpenTelemetry.Exporter.OtlpExportProtocol.HttpProtobuf, new Uri("http://127.0.0.1:1"));
        using var sp = services.BuildServiceProvider();
        sp.GetRequiredService<ResolvedTelemetry>();
        Assert.DoesNotContain(diagnostics.Lines, l => l.Contains("exports every signal twice"));
    }

    [Fact]
    public void UseOtlpExporter_Registration_Type_Still_Exists()
    {
        Assert.True(MetricsSetup.UseOtlpExporterRegistrationType is not null,
            "OpenTelemetry.Exporter.UseOtlpExporterRegistration is gone: walk the .NET SDK-bump checklist (release skill) and update MetricsSetup.UseOtlpExporterRegistrationType");
    }

    /// <summary>A command-line value outranks env in the default host: it holds base2 while env holds explicit.</summary>
    [Fact]
    public void Command_Line_Value_Outranking_The_Environment_Names_The_Used_Value()
    {
        Environment.SetEnvironmentVariable(HistogramVariable, "explicit_bucket_histogram");
        try
        {
            var diagnostics = new TestDiagnostics();
            var builder = Microsoft.Extensions.Hosting.Host.CreateApplicationBuilder(new[] { $"--{HistogramVariable}=base2_exponential_bucket_histogram" });
            builder.Services.AddSingleton<IOtelHelperDiagnostics>(diagnostics.Sink);
            builder.Services.AddOtelHelper(o => o.PrometheusMetricsPort = 0);
            using var host = builder.Build();
            host.Services.GetRequiredService<ResolvedTelemetry>();
            Assert.Null(host.Services.GetService<MetricsSetup.HistogramAggregationPath>());
            var line = Assert.Single(diagnostics.Lines, l => l.Contains("differs between IConfiguration and the process environment"));
            Assert.Contains("used=explicit_bucket_histogram", line);
        }
        finally
        {
            Environment.SetEnvironmentVariable(HistogramVariable, null);
        }
    }
}
