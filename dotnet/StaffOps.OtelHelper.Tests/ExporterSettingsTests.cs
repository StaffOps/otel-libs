using System.Diagnostics;
using System.Diagnostics.Metrics;
using System.Net;
using System.Reflection;
using System.Security.Cryptography;
using System.Security.Cryptography.X509Certificates;
using Microsoft.Extensions.DependencyInjection;
using Microsoft.Extensions.Logging;
using OpenTelemetry.Exporter;
using OpenTelemetry.Logs;
using OpenTelemetry.Metrics;
using OpenTelemetry.Trace;
using StaffOps.OtelHelper.Exporters;
using StaffOps.OtelHelper.Logging;
using StaffOps.OtelHelper.Metrics;
using StaffOps.OtelHelper.Tracing;
using Xunit;

namespace StaffOps.OtelHelper.Tests;

/// <summary>
/// Task C8 (A18, R2, R8, G10, ADR-013): exporter settings per signal — headers, timeout,
/// compression, TLS files — and the helper client's duties (no redirects, refused loudly;
/// response bound; deadline), the batch-size clamp and the value-length warning.
/// </summary>
[Collection("EnvVarTests")]
[ResetGlobalPropagation]
public class ExporterSettingsTests : IDisposable
{
    private static readonly string[] Suffixes = { "HEADERS", "TIMEOUT", "COMPRESSION", "CERTIFICATE", "CLIENT_CERTIFICATE", "CLIENT_KEY", "ENDPOINT", "PROTOCOL", "INSECURE" };

    private static readonly string[] OtherVariables =
    {
        "OTEL_BSP_EXPORT_TIMEOUT", "OTEL_BLRP_EXPORT_TIMEOUT", "OTEL_METRIC_EXPORT_TIMEOUT",
        "OTEL_BSP_MAX_QUEUE_SIZE", "OTEL_BSP_MAX_EXPORT_BATCH_SIZE", "OTEL_BLRP_MAX_QUEUE_SIZE", "OTEL_BLRP_MAX_EXPORT_BATCH_SIZE",
        "OTEL_ATTRIBUTE_VALUE_LENGTH_LIMIT", "OTEL_SPAN_ATTRIBUTE_VALUE_LENGTH_LIMIT", "OTEL_LOGRECORD_ATTRIBUTE_VALUE_LENGTH_LIMIT",
    };

    private readonly DirectoryInfo _dir = Directory.CreateTempSubdirectory("otelhelper-c8-");

    public ExporterSettingsTests() => ClearEnv();

    public void Dispose()
    {
        ClearEnv();
        _dir.Delete(recursive: true);
    }

    private static IEnumerable<string> AllVariables()
        => Suffixes.SelectMany(s => new[] { "OTEL_EXPORTER_OTLP_" + s }.Concat(ResolvedTelemetry.Signals.Select(sig => TelemetryResolver.SignalVariable(sig, s))))
            .Concat(OtherVariables);

    private static void ClearEnv()
    {
        foreach (var v in AllVariables())
        {
            Environment.SetEnvironmentVariable(v, null);
        }
    }

    private static void Env(string name, string? value) => Environment.SetEnvironmentVariable(name, value);

    private static ResolvedTelemetry Resolve(TestDiagnostics diagnostics, string endpoint = "http://collector:4317", Action<TelemetryOptions>? more = null)
        => diagnostics.Resolve(o =>
        {
            TestDiagnostics.Identified(o);
            o.OtelCollectorEndpoint = endpoint;
            more?.Invoke(o);
        });

    // ── headers ──────────────────────────────────────────────────────

    /// <summary>Contract D1 rows "Header parsing details" and "Header bytes".</summary>
    [Theory]
    [InlineData("x-parity%2Dk=v,x-a=1", new[] { "x-a=1" })]           // keys are not decoded: % is outside the key set
    [InlineData("x=a%zzb", new[] { "x=a%zzb" })]                       // invalid escape: literal
    [InlineData("x=a%20b%zz", new[] { "x=a%20b%zz" })]                 // one invalid escape keeps the WHOLE value literal
    [InlineData("x=%FF", new[] { "x=%FF" })]                           // decodes to invalid UTF-8: literal
    [InlineData("x=%C3%A9,y=1", new[] { "y=1" })]                      // decoded non-ASCII: skipped
    [InlineData("x=é,y=1", new[] { "y=1" })]                      // raw non-ASCII: skipped
    [InlineData("x=a%0D%0Ay,y=1", new[] { "y=1" })]                    // decoded control character: skipped
    [InlineData("x=%0D%0Ay", new[] { "x=y" })]                          // trimmed after decoding (as Go and Python)
    [InlineData("X-A=1,x-a=2", new[] { "x-a=2" })]                     // case-insensitive, last wins, lower-cased
    [InlineData("k=%20v%2C1", new[] { "k=v,1" })]                      // decoded, then trimmed; a comma survives
    [InlineData("a=b=c", new[] { "a=b=c" })]
    [InlineData(" k = v ", new[] { "k=v" })]
    [InlineData("bad key=1,ok=2", new[] { "ok=2" })]
    [InlineData("é=1,ok=2", new[] { "ok=2" })]
    [InlineData("a=1,", new[] { "a=1" })]
    [InlineData("a=", new[] { "a=" })]
    [InlineData(",,", new string[0])]
    public void Header_Grammar(string raw, string[] expected)
    {
        Assert.Equal(expected, ExportSettingsResolver.ParseHeaders(raw).Select(h => $"{h.Key}={h.Value}"));
    }

    [Fact]
    public void Header_Warnings_Name_The_Variable_And_The_Position_Never_A_Value()
    {
        var diagnostics = new TestDiagnostics();
        var headers = ExportSettingsResolver.ParseHeaders(
            "authorization=Bearer%20parity-s3cr3t,broken-parity-s3cr3t,x=parity%zzs3cr3t,y=%C3%A9parity-s3cr3t", diagnostics.Sink, "OTEL_EXPORTER_OTLP_TRACES_HEADERS");
        Assert.Equal(new[] { "authorization", "x" }, headers.Select(h => h.Key));
        Assert.Contains(diagnostics.Lines, l => l.Contains("malformed OTEL_EXPORTER_OTLP_TRACES_HEADERS item") && l.Contains("item #2"));
        Assert.Contains(diagnostics.Lines, l => l.Contains("kept literally") && l.Contains("item #3"));
        Assert.Contains(diagnostics.Lines, l => l.Contains("not printable ASCII") && l.Contains("item #4"));
        Assert.Equal(3, diagnostics.Lines.Length);
        Assert.DoesNotContain("s3cr3t", diagnostics.Text);
    }

    [Fact]
    public void Per_Signal_Headers_Replace_The_Generic_Ones_And_An_Unusable_Per_Signal_Value_Falls_Back()
    {
        Env("OTEL_EXPORTER_OTLP_HEADERS", "x-g=1");
        Env("OTEL_EXPORTER_OTLP_TRACES_HEADERS", "x-t=1");
        Env("OTEL_EXPORTER_OTLP_METRICS_HEADERS", "broken");
        var diagnostics = new TestDiagnostics();
        var resolved = Resolve(diagnostics);
        Assert.Equal(new[] { "x-t" }, resolved.Export("traces").Headers.Select(h => h.Key));
        Assert.Equal(new[] { "x-g" }, resolved.Export("metrics").Headers.Select(h => h.Key));
        Assert.Equal(new[] { "x-g" }, resolved.Export("logs").Headers.Select(h => h.Key));
        Assert.Contains(diagnostics.Lines, l => l.Contains("OTEL_EXPORTER_OTLP_METRICS_HEADERS"));

        using var client = OtlpExporters.OptionsFor("traces", resolved, diagnostics.Sink)!.HttpClientFactory!();
        Assert.True(client.DefaultRequestHeaders.Contains("x-t"));
        Assert.False(client.DefaultRequestHeaders.Contains("x-g"));
    }

    [Theory]
    [InlineData("http/protobuf", "abc", "abc")]
    [InlineData("grpc", "abc", "YWJj")]
    [InlineData("grpc", "ab", "YWI")] // unpadded, as grpc-go
    public void Bin_Header_Values_Are_Base64_On_Grpc_Only(string protocol, string value, string wire)
    {
        Env("OTEL_EXPORTER_OTLP_HEADERS", $"x-parity-bin={value},x-a=1");
        var diagnostics = new TestDiagnostics();
        var resolved = Resolve(diagnostics, more: o => o.OtlpProtocol = protocol);
        using var client = OtlpExporters.OptionsFor("traces", resolved, diagnostics.Sink)!.HttpClientFactory!();
        Assert.Equal(new[] { wire }, client.DefaultRequestHeaders.GetValues("x-parity-bin"));
        Assert.Equal(new[] { "1" }, client.DefaultRequestHeaders.GetValues("x-a"));
    }

    // ── timeout ──────────────────────────────────────────────────────

    /// <summary>Contract D1 rows "Timeout range / syntax", "Env timeout garbage / 0 / negative".</summary>
    [Theory]
    [InlineData("500", 500, false)]
    [InlineData(" 700 ", 700, false)]
    [InlineData("250.4", 250, false)]
    [InlineData("250.5", 251, false)]
    [InlineData("0", 10_000, true)]
    [InlineData("0.0", 10_000, true)]
    [InlineData("-1", 10_000, true)]
    [InlineData("+5", 10_000, true)]
    [InlineData("1e3", 10_000, true)]
    [InlineData(".5", 10_000, true)]
    [InlineData("5.", 10_000, true)]
    [InlineData("0x10", 10_000, true)]
    [InlineData("1_000", 10_000, true)]
    [InlineData("abc", 10_000, true)]
    [InlineData("0.5", 1, true)]
    [InlineData("86400000", 86_400_000, false)]
    [InlineData("999999999999", 86_400_000, true)]
    [InlineData("1" + "00000000000000000000000000000000000000000", 86_400_000, true)]
    public void Timeout_Grammar(string raw, int expected, bool warns)
    {
        Env("OTEL_EXPORTER_OTLP_TIMEOUT", raw);
        var diagnostics = new TestDiagnostics();
        var resolved = Resolve(diagnostics);
        Assert.Equal(expected, resolved.ExportTimeoutMs);
        Assert.All(ResolvedTelemetry.Signals, s => Assert.Equal(expected, resolved.Export(s).TimeoutMs));
        Assert.Equal(warns, diagnostics.Lines.Any(l => l.Contains("invalid OTEL_EXPORTER_OTLP_TIMEOUT")));
    }

    [Fact]
    public void Per_Signal_Timeout_Falls_Back_To_The_Generic_One_And_Code_Wins()
    {
        Env("OTEL_EXPORTER_OTLP_TIMEOUT", "500");
        Env("OTEL_EXPORTER_OTLP_TRACES_TIMEOUT", "abc");
        Env("OTEL_EXPORTER_OTLP_LOGS_TIMEOUT", "300");
        var diagnostics = new TestDiagnostics();
        var resolved = Resolve(diagnostics);
        Assert.Equal(500, resolved.Export("traces").TimeoutMs);
        Assert.Equal(500, resolved.Export("metrics").TimeoutMs);
        Assert.Equal(300, resolved.Export("logs").TimeoutMs);
        Assert.Contains(diagnostics.Lines, l => l.Contains("invalid OTEL_EXPORTER_OTLP_TRACES_TIMEOUT"));

        var code = Resolve(new TestDiagnostics(), more: o => o.ExportTimeoutMs = 2000);
        Assert.All(ResolvedTelemetry.Signals, s => Assert.Equal(2000, code.Export(s).TimeoutMs));
    }

    [Fact]
    public void Timeout_Bounds_The_Exporter_The_Client_The_Batch_Processors_And_The_Reader()
    {
        Env("OTEL_EXPORTER_OTLP_TRACES_TIMEOUT", "1111");
        Env("OTEL_EXPORTER_OTLP_METRICS_TIMEOUT", "2222");
        Env("OTEL_EXPORTER_OTLP_LOGS_TIMEOUT", "3333");
        Env("OTEL_BSP_EXPORT_TIMEOUT", "1");
        Env("OTEL_BLRP_EXPORT_TIMEOUT", "1");
        Env("OTEL_METRIC_EXPORT_TIMEOUT", "1");
        var diagnostics = new TestDiagnostics();
        using var sp = TestPipelines.Build(o =>
        {
            TestDiagnostics.Identified(o);
            o.OtelCollectorEndpoint = "http://127.0.0.1:1";
            o.PrometheusMetricsPort = 0;
        }, diagnostics);
        var resolved = sp.GetRequiredService<ResolvedTelemetry>();

        foreach (var (signal, ms) in new[] { ("traces", 1111), ("metrics", 2222), ("logs", 3333) })
        {
            var options = OtlpExporters.OptionsFor(signal, resolved, diagnostics.Sink)!;
            Assert.Equal(ms, options.TimeoutMilliseconds);
            using var client = options.HttpClientFactory!();
            Assert.Equal(TimeSpan.FromMilliseconds(ms), client.Timeout);
        }

        using var bsp = TracerSetup.CreateOtlpProcessor(sp, resolved, OtlpExporters.OptionsFor("traces", resolved, diagnostics.Sink)!, diagnostics.Sink);
        Assert.Equal(1111, IntMember(bsp, "timeout"));
        using var blrp = LoggingSetup.CreateOtlpProcessor(sp, resolved, OtlpExporters.OptionsFor("logs", resolved, diagnostics.Sink)!, diagnostics.Sink);
        Assert.Equal(3333, IntMember(blrp, "timeout"));
        using var reader = MetricsSetup.CreateOtlpReader(sp, resolved, OtlpExporters.OptionsFor("metrics", resolved, diagnostics.Sink)!);
        Assert.Equal(2222, IntMember(reader, "timeout"));

        foreach (var variable in new[] { "OTEL_BSP_EXPORT_TIMEOUT", "OTEL_BLRP_EXPORT_TIMEOUT", "OTEL_METRIC_EXPORT_TIMEOUT" })
        {
            Assert.Single(diagnostics.Lines, l => l.Contains($"{variable} is not honored"));
        }
    }

    [Fact]
    public async Task A_Hanging_Collector_Ends_The_Export_At_The_Deadline()
    {
        var port = TestPorts.FreeTcpPort();
        using var listener = new HttpListener();
        listener.Prefixes.Add($"http://localhost:{port}/");
        listener.Start();
        var held = new List<HttpListenerContext>();
        var accept = Task.Run(async () =>
        {
            while (listener.IsListening)
            {
                try
                {
                    var context = await listener.GetContextAsync();
                    lock (held)
                    {
                        held.Add(context); // never answered
                    }
                }
                catch (Exception ex) when (ex is HttpListenerException or ObjectDisposedException or InvalidOperationException)
                {
                    return;
                }
            }
        });

        Env("OTEL_EXPORTER_OTLP_TIMEOUT", "400");
        using (var sp = TestPipelines.Build(o =>
        {
            TestDiagnostics.Identified(o);
            o.ServiceName = "deadline-test";
            o.OtelCollectorEndpoint = $"http://localhost:{port}";
            o.OtlpProtocol = TelemetryOptions.ProtocolHttpProtobuf;
            o.PrometheusMetricsPort = 0;
            o.DisabledSignals = "metrics,logs";
        }))
        {
            using (var source = new ActivitySource("deadline-test"))
            using (source.StartActivity("span"))
            {
            }

            var watch = Stopwatch.StartNew();
            sp.GetRequiredService<TracerProvider>().ForceFlush(30_000);
            watch.Stop();
            lock (held)
            {
                Assert.NotEmpty(held);
            }

            // The 10 s default (and the SDK's own 30 s processor timeout) would hold it far longer.
            Assert.True(watch.ElapsedMilliseconds < 5_000, $"export took {watch.ElapsedMilliseconds} ms");
        }

        listener.Stop();
        await Task.WhenAny(accept, Task.Delay(2_000));
    }

    [Theory]
    [InlineData(1, "1m")]
    [InlineData(500, "500m")]
    [InlineData(86_400_000, "86400000m")]
    [InlineData(99_999_999, "99999999m")]
    [InlineData(100_000_000, "100000S")]
    [InlineData(100_000_001, "100001S")]
    public void Grpc_Timeout_Header_Encoding(int ms, string expected)
    {
        Assert.Equal(expected, HelperExportHandler.GrpcTimeout(ms));
    }

    [Fact]
    public async Task Handler_Sends_Grpc_Timeout_On_Grpc_Only_And_Forwards_Send_And_SendAsync()
    {
        var inner = new RecordingHandler(HttpStatusCode.OK);
        var diagnostics = new TestDiagnostics();
        using (var grpc = new HttpClient(new HelperExportHandler(inner, "traces", 500, diagnostics.Sink)))
        {
            using var sync = grpc.Send(new HttpRequestMessage(HttpMethod.Post, "http://collector/x"));
            using var async = await grpc.SendAsync(new HttpRequestMessage(HttpMethod.Post, "http://collector/x"));
        }

        using (var http = new HttpClient(new HelperExportHandler(inner, "traces", null, diagnostics.Sink)))
        {
            using var sync = http.Send(new HttpRequestMessage(HttpMethod.Post, "http://collector/x"));
        }

        Assert.Equal(2, inner.SyncCalls);
        Assert.Equal(1, inner.AsyncCalls);
        Assert.Equal(new[] { "500m", "500m", null }, inner.GrpcTimeouts);
        Assert.Empty(diagnostics.Lines);
    }

    // ── compression ──────────────────────────────────────────────────

    [Fact]
    public void Compression_Defaults_To_Gzip_Per_Signal_With_Fallback_And_Code_Wins()
    {
        var plain = Resolve(new TestDiagnostics());
        Assert.Equal(TelemetryOptions.CompressionGzip, plain.Compression);
        Assert.All(ResolvedTelemetry.Signals, s => Assert.Equal(OtlpExportCompression.GZip, OtlpExporters.OptionsFor(s, plain, new TestDiagnostics().Sink)!.Compression));

        Env("OTEL_EXPORTER_OTLP_TRACES_COMPRESSION", "NONE");
        Env("OTEL_EXPORTER_OTLP_METRICS_COMPRESSION", "deflate");
        Env("OTEL_EXPORTER_OTLP_COMPRESSION", "none");
        Env("OTEL_EXPORTER_OTLP_LOGS_COMPRESSION", "gzip");
        var diagnostics = new TestDiagnostics();
        var resolved = Resolve(diagnostics);
        Assert.Equal("none", resolved.Export("traces").Compression);
        Assert.Equal("none", resolved.Export("metrics").Compression);
        Assert.Equal("gzip", resolved.Export("logs").Compression);
        Assert.Single(diagnostics.Lines, l => l.Contains("invalid OTEL_EXPORTER_OTLP_METRICS_COMPRESSION") && l.Contains("else gzip"));

        Env("OTEL_EXPORTER_OTLP_COMPRESSION", "zstd");
        Env("OTEL_EXPORTER_OTLP_METRICS_COMPRESSION", null);
        var generic = new TestDiagnostics();
        Assert.Equal("gzip", Resolve(generic).Export("metrics").Compression);
        Assert.Contains(generic.Lines, l => l.Contains("invalid OTEL_EXPORTER_OTLP_COMPRESSION"));

        var code = Resolve(new TestDiagnostics(), more: o => o.Compression = "none");
        Assert.All(ResolvedTelemetry.Signals, s => Assert.Equal("none", code.Export(s).Compression));
    }

    // ── TLS files ────────────────────────────────────────────────────

    private string WritePem(string name, string content)
    {
        var path = Path.Combine(_dir.FullName, name);
        File.WriteAllText(path, content);
        return path;
    }

    private static X509Certificate2 SelfSigned(string name)
    {
        using var key = ECDsa.Create(ECCurve.NamedCurves.nistP256);
        var request = new CertificateRequest($"CN={name}", key, HashAlgorithmName.SHA256);
        request.CertificateExtensions.Add(new X509BasicConstraintsExtension(true, false, 0, true));
        return request.CreateSelfSigned(DateTimeOffset.UtcNow.AddDays(-1), DateTimeOffset.UtcNow.AddDays(1));
    }

    [Fact]
    public void Plaintext_Target_Ignores_The_Tls_Files_With_One_Warning_Naming_Them()
    {
        using var ca = SelfSigned("parity-ca");
        Env("OTEL_EXPORTER_OTLP_CERTIFICATE", WritePem("ca.pem", ca.ExportCertificatePem()));
        Env("OTEL_EXPORTER_OTLP_TRACES_CLIENT_KEY", Path.Combine(_dir.FullName, "parity-secret-key.pem"));
        var diagnostics = new TestDiagnostics();
        var resolved = Resolve(diagnostics, "http://collector:4317");

        Assert.All(ResolvedTelemetry.Signals, s => Assert.Null(resolved.Export(s).Tls));
        var lines = diagnostics.Lines.Where(l => l.Contains("endpoint is plaintext")).ToList();
        Assert.Equal(2, lines.Count);
        Assert.Contains(lines, l => l.Contains("variable=OTEL_EXPORTER_OTLP_CERTIFICATE variable=OTEL_EXPORTER_OTLP_TRACES_CLIENT_KEY signals=traces"));
        Assert.Contains(lines, l => l.Contains("variable=OTEL_EXPORTER_OTLP_CERTIFICATE signals=metrics,logs"));
        Assert.DoesNotContain("cannot be read", diagnostics.Text);
        Assert.DoesNotContain(_dir.FullName, diagnostics.Text);
        Assert.DoesNotContain("parity-secret-key", diagnostics.Text);

        // Schemeless with INSECURE is plaintext too.
        var insecure = new TestDiagnostics();
        Resolve(insecure, "collector:4317", o => o.Insecure = true);
        Assert.Contains(insecure.Lines, l => l.Contains("endpoint is plaintext"));
    }

    [Fact]
    public void Ca_Per_Signal_Unreadable_Or_Not_Pem_Falls_Back_To_Generic_Then_The_System_Roots()
    {
        using var ca = SelfSigned("parity-ca");
        var good = WritePem("ca.pem", ca.ExportCertificatePem());
        var empty = WritePem("empty.pem", "");
        var garbage = WritePem("garbage.pem", "-----BEGIN CERTIFICATE-----\nnot base64\n-----END CERTIFICATE-----\n");
        Env("OTEL_EXPORTER_OTLP_TRACES_CERTIFICATE", empty);
        Env("OTEL_EXPORTER_OTLP_METRICS_CERTIFICATE", Path.Combine(_dir.FullName, "missing.pem"));
        Env("OTEL_EXPORTER_OTLP_LOGS_CERTIFICATE", garbage);
        Env("OTEL_EXPORTER_OTLP_CERTIFICATE", good);
        var diagnostics = new TestDiagnostics();
        var resolved = Resolve(diagnostics, "https://collector:4317");
        Assert.All(ResolvedTelemetry.Signals, s => Assert.Equal(ca.ExportCertificatePem(), resolved.Export(s).Tls!.CaPem));
        Assert.Contains(diagnostics.Lines, l => l.Contains("holds no PEM certificate") && l.Contains("variable=OTEL_EXPORTER_OTLP_TRACES_CERTIFICATE") && l.Contains("generic variable"));
        Assert.Contains(diagnostics.Lines, l => l.Contains("cannot be read") && l.Contains("variable=OTEL_EXPORTER_OTLP_METRICS_CERTIFICATE"));
        Assert.Contains(diagnostics.Lines, l => l.Contains("holds no PEM certificate") && l.Contains("variable=OTEL_EXPORTER_OTLP_LOGS_CERTIFICATE"));

        Env("OTEL_EXPORTER_OTLP_CERTIFICATE", empty);
        var none = new TestDiagnostics();
        var roots = Resolve(none, "https://collector:4317");
        Assert.All(ResolvedTelemetry.Signals, s => Assert.Null(roots.Export(s).Tls!.CaPem));
        Assert.Contains(none.Lines, l => l.Contains("variable=OTEL_EXPORTER_OTLP_CERTIFICATE ") && l.Contains("system roots"));
        Assert.DoesNotContain(_dir.FullName, diagnostics.Text + none.Text);

        // The system roots: no custom validation callback on the transport.
        var clients = new ExporterClients();
        using (OtlpExporters.OptionsFor("traces", roots, none.Sink, clients)!.HttpClientFactory!())
        {
            Assert.Null(clients.HelperFactories.Single().Transport!.SslOptions.RemoteCertificateValidationCallback);
        }

        clients.Dispose();
    }

    [Fact]
    public void Client_Pair_Incomplete_Or_Rejected_Uses_Server_Authentication_And_A_Good_Pair_Is_Presented()
    {
        using var ca = SelfSigned("parity-ca");
        var caPath = WritePem("ca.pem", ca.ExportCertificatePem());
        using var client = SelfSigned("parity-client");
        var certPath = WritePem("client.pem", client.ExportCertificatePem());
        var keyPath = WritePem("client.key", client.GetECDsaPrivateKey()!.ExportPkcs8PrivateKeyPem());
        Env("OTEL_EXPORTER_OTLP_CERTIFICATE", caPath);

        // Only the key.
        Env("OTEL_EXPORTER_OTLP_CLIENT_KEY", keyPath);
        var onlyKey = new TestDiagnostics();
        var tls = Resolve(onlyKey, "https://collector:4317").Export("traces").Tls!;
        Assert.NotNull(tls.CaPem);
        Assert.Null(tls.ClientCertificatePem);
        Assert.Contains(onlyKey.Lines, l => l.Contains("must both be set") && l.Contains("variable=OTEL_EXPORTER_OTLP_CLIENT_KEY ") && l.Contains("server authentication"));

        // A mismatch: the CA certificate given as the key.
        Env("OTEL_EXPORTER_OTLP_CLIENT_KEY", caPath);
        Env("OTEL_EXPORTER_OTLP_CLIENT_CERTIFICATE", caPath);
        var mismatch = new TestDiagnostics();
        tls = Resolve(mismatch, "https://collector:4317").Export("traces").Tls!;
        Assert.Null(tls.ClientCertificatePem);
        Assert.NotNull(tls.CaPem);
        Assert.Contains(mismatch.Lines, l => l.Contains("were rejected") && l.Contains("variable=OTEL_EXPORTER_OTLP_CLIENT_KEY variable=OTEL_EXPORTER_OTLP_CLIENT_CERTIFICATE"));

        // Per-signal key unreadable: the generic one is used, so the pair loads.
        Env("OTEL_EXPORTER_OTLP_CLIENT_KEY", keyPath);
        Env("OTEL_EXPORTER_OTLP_CLIENT_CERTIFICATE", certPath);
        Env("OTEL_EXPORTER_OTLP_TRACES_CLIENT_KEY", Path.Combine(_dir.FullName, "missing.key"));
        var good = new TestDiagnostics();
        var resolved = Resolve(good, "https://collector:4317");
        Assert.NotNull(resolved.Export("traces").Tls!.ClientCertificatePem);
        Assert.Single(good.Lines);
        Assert.Contains("variable=OTEL_EXPORTER_OTLP_TRACES_CLIENT_KEY", good.Lines[0]);

        var clients = new ExporterClients();
        using (OtlpExporters.OptionsFor("traces", resolved, good.Sink, clients)!.HttpClientFactory!())
        {
            var transport = clients.HelperFactories.Single().Transport!;
            var presented = Assert.IsType<X509Certificate2>(Assert.Single(transport.SslOptions.ClientCertificates!.Cast<X509Certificate>()));
            Assert.True(presented.HasPrivateKey);
            Assert.Equal(client.Thumbprint, presented.Thumbprint);
            Assert.NotNull(transport.SslOptions.RemoteCertificateValidationCallback);
        }

        clients.Dispose();
        Assert.DoesNotContain(_dir.FullName, onlyKey.Text + mismatch.Text + good.Text);
    }

    // ── client duties ────────────────────────────────────────────────

    [Fact]
    public void One_Client_Per_Exporter_Instance_With_The_Sdk_Response_Bound_And_No_Redirects()
    {
        var resolved = Resolve(new TestDiagnostics());
        var clients = new ExporterClients();
        var first = OtlpExporters.OptionsFor("traces", resolved, new TestDiagnostics().Sink, clients)!;
        var second = OtlpExporters.OptionsFor("traces", resolved, new TestDiagnostics().Sink, clients)!;
        var a = first.HttpClientFactory!();
        Assert.Same(a, first.HttpClientFactory!());
        Assert.NotSame(a, second.HttpClientFactory!());
        Assert.True(first.MaxResponseSizeBytes > 0);
        Assert.Equal(first.MaxResponseSizeBytes, a.MaxResponseContentBufferSize);
        Assert.All(clients.HelperFactories, f => Assert.False(f.Transport!.AllowAutoRedirect));
        clients.Dispose();
    }

    [Fact]
    public void Content_Header_Rejected_By_The_Client_Is_Warned_By_Key()
    {
        Env("OTEL_EXPORTER_OTLP_HEADERS", "content-type=v-secret,x-ok=1");
        var diagnostics = new TestDiagnostics();
        var resolved = Resolve(diagnostics);
        using var client = OtlpExporters.OptionsFor("traces", resolved, diagnostics.Sink)!.HttpClientFactory!();
        Assert.True(client.DefaultRequestHeaders.Contains("x-ok"));
        Assert.Single(diagnostics.Lines, l => l.Contains("rejected by the HTTP client") && l.Contains("key=content-type") && l.Contains("signal=traces"));
        Assert.DoesNotContain("v-secret", diagnostics.Text);
    }

    // ── redirects: refused loudly, per signal (release blocker, C3 review dev M2) ──

    [Theory]
    [InlineData("traces", 301)]
    [InlineData("traces", 302)]
    [InlineData("traces", 307)]
    [InlineData("traces", 308)]
    [InlineData("metrics", 301)]
    [InlineData("metrics", 302)]
    [InlineData("metrics", 307)]
    [InlineData("metrics", 308)]
    [InlineData("logs", 301)]
    [InlineData("logs", 302)]
    [InlineData("logs", 307)]
    [InlineData("logs", 308)]
    public async Task A_Redirect_Is_Refused_And_Reported_Naming_The_Signal_And_The_Status(string signal, int status)
    {
        var port = TestPorts.FreeTcpPort();
        using var listener = new HttpListener();
        listener.Prefixes.Add($"http://localhost:{port}/");
        listener.Start();
        var paths = new List<string>();
        var serve = Task.Run(async () =>
        {
            while (listener.IsListening)
            {
                try
                {
                    var context = await listener.GetContextAsync();
                    lock (paths)
                    {
                        paths.Add(context.Request.Url!.AbsolutePath);
                    }

                    context.Response.StatusCode = status;
                    context.Response.RedirectLocation = $"http://user:parity-redirect-pass@localhost:{port}/moved/parity-redirect-path?token=parity-redirect-secret";
                    context.Response.Close();
                }
                catch (Exception ex) when (ex is HttpListenerException or ObjectDisposedException or InvalidOperationException)
                {
                    return;
                }
            }
        });

        var others = string.Join(",", ResolvedTelemetry.Signals.Where(s => s != signal));
        var diagnostics = new TestDiagnostics();
        using (var sp = TestPipelines.Build(o =>
        {
            TestDiagnostics.Identified(o);
            o.ServiceName = "redirect-test";
            o.OtelCollectorEndpoint = $"http://localhost:{port}";
            o.OtlpProtocol = TelemetryOptions.ProtocolHttpProtobuf;
            o.PrometheusMetricsPort = 0;
            o.DisabledSignals = others;
            o.ExportIntervalMs = 3_600_000;
        }, diagnostics))
        {
            switch (signal)
            {
                case "traces":
                    using (var source = new ActivitySource("redirect-test"))
                    using (source.StartActivity("span"))
                    {
                    }

                    sp.GetRequiredService<TracerProvider>().ForceFlush(10_000);
                    break;
                case "metrics":
                    sp.GetRequiredKeyedService<Meter>(OtelHelperKeys.Instrumentation).CreateCounter<long>("redirect.count").Add(1);
                    sp.GetRequiredService<MeterProvider>().ForceFlush(10_000);
                    break;
                default:
                    sp.GetRequiredService<ILoggerFactory>().CreateLogger("redirect-test").LogWarning("redirect-log");
                    sp.GetRequiredService<LoggerProvider>().ForceFlush(10_000);
                    break;
            }

            for (var i = 0; i < 100 && !diagnostics.Lines.Any(l => l.Contains("redirect")); i++)
            {
                await Task.Delay(50);
            }
        }

        listener.Stop();
        await Task.WhenAny(serve, Task.Delay(2_000));

        var line = Assert.Single(diagnostics.Lines, l => l.Contains("a redirect"));
        Assert.StartsWith("otelhelper: ERROR", line);
        Assert.Contains($"signal={signal}", line);
        Assert.Contains($"status={status}", line);
        Assert.DoesNotContain("parity-redirect", diagnostics.Text);
        lock (paths)
        {
            Assert.NotEmpty(paths);
            Assert.All(paths, p => Assert.Equal($"/v1/{signal}", p)); // the Location was never requested
        }
    }

    // ── A18: batch size above queue size, value-length limit 0 ──────

    [Theory]
    [InlineData("OTEL_BSP", "10", "50", true)]
    [InlineData("OTEL_BSP", "100", null, true)]   // the default batch size 512 is above the queue size
    [InlineData("OTEL_BSP", "100", "100", false)]
    [InlineData("OTEL_BLRP", "10", "50", true)]
    [InlineData("OTEL_BLRP", "x", "50", false)]   // unparseable: left to the SDK
    [InlineData("OTEL_BLRP", null, "4096", true)] // above the default queue size 2048
    public void Batch_Size_Above_The_Queue_Size_Warns_Naming_Both(string prefix, string? queue, string? batch, bool warns)
    {
        Env(prefix + "_MAX_QUEUE_SIZE", queue);
        Env(prefix + "_MAX_EXPORT_BATCH_SIZE", batch);
        var diagnostics = new TestDiagnostics();
        Assert.Equal(64, ExportSettingsResolver.ClampBatchSize(prefix, "X", 128, 64, diagnostics.Sink));
        Assert.Equal(warns, diagnostics.Lines.Any(l => l.Contains($"variable={prefix}_MAX_EXPORT_BATCH_SIZE variable={prefix}_MAX_QUEUE_SIZE")));
    }

    [Fact]
    public void Batch_Size_Above_The_Queue_Size_In_The_Options_Is_Clamped()
    {
        var diagnostics = new TestDiagnostics();
        Assert.Equal(10, ExportSettingsResolver.ClampBatchSize("OTEL_BSP", "BatchExportActivityProcessorOptions", 10, 50, diagnostics.Sink));
        // From code: the options are named, not the variables (fix round 1, LOW 8).
        var line = Assert.Single(diagnostics.Lines, l => l.Contains("using=10"));
        Assert.Contains("option=BatchExportActivityProcessorOptions.MaxExportBatchSize option=BatchExportActivityProcessorOptions.MaxQueueSize", line);
        Assert.DoesNotContain("OTEL_BSP", line);
    }

    [Fact]
    public void Env_Batch_Size_Above_The_Queue_Size_Builds_And_The_Processors_Use_The_Queue_Size()
    {
        Env("OTEL_BSP_MAX_QUEUE_SIZE", "10");
        Env("OTEL_BSP_MAX_EXPORT_BATCH_SIZE", "50");
        Env("OTEL_BLRP_MAX_QUEUE_SIZE", "12");
        Env("OTEL_BLRP_MAX_EXPORT_BATCH_SIZE", "50");
        var diagnostics = new TestDiagnostics();
        using var sp = TestPipelines.Build(o =>
        {
            TestDiagnostics.Identified(o);
            o.OtelCollectorEndpoint = "http://127.0.0.1:1";
            o.PrometheusMetricsPort = 0;
        }, diagnostics);
        var resolved = sp.GetRequiredService<ResolvedTelemetry>();
        using var bsp = TracerSetup.CreateOtlpProcessor(sp, resolved, OtlpExporters.OptionsFor("traces", resolved, diagnostics.Sink)!, diagnostics.Sink);
        Assert.Equal(10, IntMember(bsp, "batch"));
        using var blrp = LoggingSetup.CreateOtlpProcessor(sp, resolved, OtlpExporters.OptionsFor("logs", resolved, diagnostics.Sink)!, diagnostics.Sink);
        Assert.Equal(12, IntMember(blrp, "batch"));
        Assert.Single(diagnostics.Lines, l => l.Contains("variable=OTEL_BSP_MAX_EXPORT_BATCH_SIZE variable=OTEL_BSP_MAX_QUEUE_SIZE"));
        Assert.Single(diagnostics.Lines, l => l.Contains("variable=OTEL_BLRP_MAX_EXPORT_BATCH_SIZE variable=OTEL_BLRP_MAX_QUEUE_SIZE"));
    }

    [Theory]
    [InlineData("OTEL_ATTRIBUTE_VALUE_LENGTH_LIMIT", "0", true)]
    [InlineData("OTEL_SPAN_ATTRIBUTE_VALUE_LENGTH_LIMIT", "00", true)]
    [InlineData("OTEL_LOGRECORD_ATTRIBUTE_VALUE_LENGTH_LIMIT", " 0 ", true)]
    [InlineData("OTEL_ATTRIBUTE_VALUE_LENGTH_LIMIT", "20", false)]
    [InlineData("OTEL_ATTRIBUTE_VALUE_LENGTH_LIMIT", "x", false)]
    public void Zero_Value_Length_Limit_Warns_Naming_Only_That_Variable(string variable, string value, bool warns)
    {
        Env(variable, value);
        Env(variable == "OTEL_SPAN_ATTRIBUTE_VALUE_LENGTH_LIMIT" ? "OTEL_ATTRIBUTE_VALUE_LENGTH_LIMIT" : "OTEL_SPAN_ATTRIBUTE_VALUE_LENGTH_LIMIT", "20");
        var diagnostics = new TestDiagnostics();
        Resolve(diagnostics);
        var lines = diagnostics.Lines.Where(l => l.Contains("VALUE_LENGTH_LIMIT")).ToList();
        Assert.Equal(warns ? 1 : 0, lines.Count);
        if (warns)
        {
            Assert.Contains($"variable={variable}", lines[0]);
        }
    }

    // ── fix round 1 ──────────────────────────────────────────────────

    /// <summary>MEDIUM: an export timeout set on the SDK's options is warned once, naming the option.</summary>
    [Fact]
    public void Application_Set_Export_Timeouts_Are_Warned_Naming_The_Option()
    {
        var diagnostics = new TestDiagnostics();
        var services = new ServiceCollection();
        services.AddSingleton<StaffOps.OtelHelper.Diagnostics.IOtelHelperDiagnostics>(diagnostics.Sink);
        services.AddOtelHelper(o =>
        {
            TestDiagnostics.Identified(o);
            o.OtelCollectorEndpoint = "http://127.0.0.1:1";
            o.PrometheusMetricsPort = 0;
        });
        services.Configure<BatchExportActivityProcessorOptions>(o => o.ExporterTimeoutMilliseconds = 1234);
        services.Configure<LogRecordExportProcessorOptions>(o => o.BatchExportProcessorOptions.ExporterTimeoutMilliseconds = 1234);
        services.Configure<MetricReaderOptions>(o => o.PeriodicExportingMetricReaderOptions.ExportTimeoutMilliseconds = 1234);
        using var sp = services.BuildServiceProvider();
        var resolved = sp.GetRequiredService<ResolvedTelemetry>();
        using var bsp = TracerSetup.CreateOtlpProcessor(sp, resolved, OtlpExporters.OptionsFor("traces", resolved, diagnostics.Sink)!, diagnostics.Sink);
        using var blrp = LoggingSetup.CreateOtlpProcessor(sp, resolved, OtlpExporters.OptionsFor("logs", resolved, diagnostics.Sink)!, diagnostics.Sink);
        using var reader = MetricsSetup.CreateOtlpReader(sp, resolved, OtlpExporters.OptionsFor("metrics", resolved, diagnostics.Sink)!);
        Assert.Equal(10_000, IntMember(bsp, "timeout"));
        foreach (var option in new[]
        {
            "BatchExportActivityProcessorOptions.ExporterTimeoutMilliseconds",
            "LogRecordExportProcessorOptions.BatchExportProcessorOptions.ExporterTimeoutMilliseconds",
            "MetricReaderOptions.PeriodicExportingMetricReaderOptions.ExportTimeoutMilliseconds",
        })
        {
            Assert.Single(diagnostics.Lines, l => l.Contains($"WARN {option} is set (in code or IConfiguration) but not used") && l.Contains("TelemetryOptions.ExportTimeoutMs"));
        }
    }

    [Fact]
    public void Untouched_Sdk_Options_Or_A_Set_Variable_Give_No_Option_Warning()
    {
        Env("OTEL_BSP_EXPORT_TIMEOUT", "1234"); // the SDK reads it into the options; the variable warning covers it
        var diagnostics = new TestDiagnostics();
        using var sp = TestPipelines.Build(o =>
        {
            TestDiagnostics.Identified(o);
            o.OtelCollectorEndpoint = "http://127.0.0.1:1";
            o.PrometheusMetricsPort = 0;
        }, diagnostics);
        var resolved = sp.GetRequiredService<ResolvedTelemetry>();
        using var bsp = TracerSetup.CreateOtlpProcessor(sp, resolved, OtlpExporters.OptionsFor("traces", resolved, diagnostics.Sink)!, diagnostics.Sink);
        using var blrp = LoggingSetup.CreateOtlpProcessor(sp, resolved, OtlpExporters.OptionsFor("logs", resolved, diagnostics.Sink)!, diagnostics.Sink);
        using var reader = MetricsSetup.CreateOtlpReader(sp, resolved, OtlpExporters.OptionsFor("metrics", resolved, diagnostics.Sink)!);
        Assert.DoesNotContain(diagnostics.Lines, l => l.Contains("but not used by the helper"));
        Assert.Single(diagnostics.Lines, l => l.Contains("OTEL_BSP_EXPORT_TIMEOUT is not honored"));
    }

    /// <summary>MEDIUM: on the histogram path the resolved deadline is applied last.</summary>
    [Fact]
    public void Histogram_Path_Deadline_Beats_A_Later_Named_Configure()
    {
        Env("OTEL_EXPORTER_OTLP_METRICS_DEFAULT_HISTOGRAM_AGGREGATION", "base2_exponential_bucket_histogram");
        try
        {
            var diagnostics = new TestDiagnostics();
            var services = new ServiceCollection();
            services.AddSingleton<StaffOps.OtelHelper.Diagnostics.IOtelHelperDiagnostics>(diagnostics.Sink);
            services.AddOtelHelper(o => { TestDiagnostics.Identified(o); o.OtelCollectorEndpoint = "http://127.0.0.1:1"; o.PrometheusMetricsPort = 0; o.ExportTimeoutMs = 4321; });
            services.Configure<MetricReaderOptions>(MetricsSetup.HistogramPathName, o => o.PeriodicExportingMetricReaderOptions.ExportTimeoutMilliseconds = 99);
            using var sp = services.BuildServiceProvider();
            var named = sp.GetRequiredService<Microsoft.Extensions.Options.IOptionsMonitor<MetricReaderOptions>>().Get(MetricsSetup.HistogramPathName);
            Assert.Equal(4321, named.PeriodicExportingMetricReaderOptions.ExportTimeoutMilliseconds);
            // Fix round 2: the replaced application value is warned, naming the named options.
            Assert.Single(diagnostics.Lines, l => l.Contains($"MetricReaderOptions(\"{MetricsSetup.HistogramPathName}\").PeriodicExportingMetricReaderOptions.ExportTimeoutMilliseconds is set"));

            // Without an application value on the named options: no warning.
            var quiet = new TestDiagnostics();
            var plain = new ServiceCollection();
            plain.AddSingleton<StaffOps.OtelHelper.Diagnostics.IOtelHelperDiagnostics>(quiet.Sink);
            plain.AddOtelHelper(o => { TestDiagnostics.Identified(o); o.OtelCollectorEndpoint = "http://127.0.0.1:1"; o.PrometheusMetricsPort = 0; });
            using var psp = plain.BuildServiceProvider();
            Assert.Equal(10_000, psp.GetRequiredService<Microsoft.Extensions.Options.IOptionsMonitor<MetricReaderOptions>>().Get(MetricsSetup.HistogramPathName).PeriodicExportingMetricReaderOptions.ExportTimeoutMilliseconds);
            Assert.DoesNotContain(quiet.Lines, l => l.Contains("but not used by the helper"));
        }
        finally
        {
            Env("OTEL_EXPORTER_OTLP_METRICS_DEFAULT_HISTOGRAM_AGGREGATION", null);
        }
    }

    /// <summary>LOW 1: a TLS file is read with a 1 MiB bound; a device or a large file is "cannot be read".</summary>
    [Fact]
    public void Oversized_Or_Device_Tls_Files_Are_Refused_As_Unreadable()
    {
        var big = Path.Combine(_dir.FullName, "big.pem");
        File.WriteAllBytes(big, new byte[ExportSettingsResolver.MaxTlsFileBytes + 1]);
        Assert.Throws<IOException>(() => ExportSettingsResolver.ReadBounded(big));
        var exact = Path.Combine(_dir.FullName, "exact.pem");
        File.WriteAllBytes(exact, new byte[ExportSettingsResolver.MaxTlsFileBytes]);
        Assert.Equal(ExportSettingsResolver.MaxTlsFileBytes, ExportSettingsResolver.ReadBounded(exact).Length);

        Env("OTEL_EXPORTER_OTLP_TRACES_CERTIFICATE", big);
        if (File.Exists("/dev/zero"))
        {
            Env("OTEL_EXPORTER_OTLP_CERTIFICATE", "/dev/zero");
        }

        var diagnostics = new TestDiagnostics();
        var resolved = Resolve(diagnostics, "https://collector:4317");
        Assert.Null(resolved.Export("traces").Tls!.CaPem);
        Assert.Contains(diagnostics.Lines, l => l.Contains("cannot be read") && l.Contains("variable=OTEL_EXPORTER_OTLP_TRACES_CERTIFICATE"));
        if (File.Exists("/dev/zero"))
        {
            Assert.Contains(diagnostics.Lines, l => l.Contains("cannot be read") && l.Contains("variable=OTEL_EXPORTER_OTLP_CERTIFICATE "));
        }

        Assert.DoesNotContain(_dir.FullName, diagnostics.Text);
    }

    // ── helpers ──────────────────────────────────────────────────────

    /// <summary>The one int field or property of <paramref name="instance"/> (or a base type) whose name contains <paramref name="part"/> (SDK internals, read for the test only).</summary>
    private static int IntMember(object instance, string part)
    {
        const BindingFlags flags = BindingFlags.Instance | BindingFlags.Public | BindingFlags.NonPublic | BindingFlags.DeclaredOnly;
        var values = new List<(string Name, int Value)>();
        for (var type = instance.GetType(); type is not null; type = type.BaseType)
        {
            values.AddRange(type.GetFields(flags).Where(f => f.FieldType == typeof(int) && f.Name.Contains(part, StringComparison.OrdinalIgnoreCase))
                .Select(f => (f.Name, (int)f.GetValue(instance)!)));
            values.AddRange(type.GetProperties(flags).Where(p => p.PropertyType == typeof(int) && p.GetIndexParameters().Length == 0 && p.Name.Contains(part, StringComparison.OrdinalIgnoreCase))
                .Select(p => (p.Name, (int)p.GetValue(instance)!)));
        }

        var distinct = values.Select(v => v.Value).Distinct().ToList();
        Assert.True(distinct.Count == 1, $"members containing '{part}': {string.Join(", ", values.Select(v => $"{v.Name}={v.Value}"))}");
        return distinct[0];
    }

    private sealed class RecordingHandler : HttpMessageHandler
    {
        private readonly HttpStatusCode _status;

        public RecordingHandler(HttpStatusCode status) => _status = status;

        public int SyncCalls { get; private set; }

        public int AsyncCalls { get; private set; }

        public List<string?> GrpcTimeouts { get; } = new();

        protected override HttpResponseMessage Send(HttpRequestMessage request, CancellationToken cancellationToken)
        {
            SyncCalls++;
            return Answer(request);
        }

        protected override Task<HttpResponseMessage> SendAsync(HttpRequestMessage request, CancellationToken cancellationToken)
        {
            AsyncCalls++;
            return Task.FromResult(Answer(request));
        }

        private HttpResponseMessage Answer(HttpRequestMessage request)
        {
            GrpcTimeouts.Add(request.Headers.TryGetValues("grpc-timeout", out var values) ? values.Single() : null);
            return new HttpResponseMessage(_status) { RequestMessage = request };
        }
    }
}
