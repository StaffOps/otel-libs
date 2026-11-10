using System.Net;
using System.Net.Security;
using System.Security.Cryptography;
using System.Security.Cryptography.X509Certificates;
using Microsoft.Extensions.Configuration;
using Microsoft.Extensions.DependencyInjection;
using Microsoft.Extensions.Hosting;
using Microsoft.Extensions.Options;
using OpenTelemetry.Exporter;
using StaffOps.OtelHelper.Diagnostics;
using StaffOps.OtelHelper.Exporters;
using Xunit;

namespace StaffOps.OtelHelper.Tests;

/// <summary>
/// Task C5, the resolver core (ADR-013, design D-NET3/D-NET4/D-NET6): helper-owned rows come
/// from code and the process environment only, the exporters get every option assigned,
/// env values warn while code values fail, and the SDK-owned <c>OTEL_SDK_DISABLED</c> is read
/// where the SDK reads it.
/// </summary>
[Collection("EnvVarTests")]
public class ResolverCoreTests : IDisposable
{
    private static readonly string[] TouchedEnvVars =
    {
        "OTEL_EXPORTER_OTLP_ENDPOINT", "OTEL_EXPORTER_OTLP_TIMEOUT", "OTEL_EXPORTER_OTLP_COMPRESSION",
        "OTEL_EXPORTER_OTLP_HEADERS", "OTEL_EXPORTER_OTLP_CERTIFICATE", "OTEL_EXPORTER_OTLP_CLIENT_CERTIFICATE",
        "OTEL_EXPORTER_OTLP_CLIENT_KEY", "OTEL_EXPORTER_OTLP_PROTOCOL", "OTEL_SERVICE_NAME", "SERVICE_NAME",
        "OTEL_SDK_DISABLED", "OTEL_CONFIG_FILE", "OTEL_EXPERIMENTAL_CONFIG_FILE", "OTEL_HELPER_METRICS_PORT",
        "OTEL_METRICS_EXPORTER", "ENVIRONMENT",
    };

    public ResolverCoreTests() => ClearEnv();

    public void Dispose() => ClearEnv();

    private static void ClearEnv()
    {
        foreach (var v in TouchedEnvVars)
        {
            Environment.SetEnvironmentVariable(v, null);
        }
    }

    private static IConfiguration Config(params (string Key, string Value)[] pairs)
        => new ConfigurationBuilder().AddInMemoryCollection(pairs.Select(p => new KeyValuePair<string, string?>(p.Key, p.Value))).Build();

    private static (ServiceProvider Provider, TestDiagnostics Diagnostics) Build(Action<TelemetryOptions> configure, IConfiguration? configuration = null)
    {
        var diagnostics = new TestDiagnostics();
        var services = new ServiceCollection();
        if (configuration is not null)
        {
            services.AddSingleton(configuration);
        }

        services.AddSingleton<IOtelHelperDiagnostics>(diagnostics.Sink);
        services.AddOtelHelper(configure);
        return (services.BuildServiceProvider(), diagnostics);
    }

    // ── IConfiguration neutralization (C1b refuter M4) ─────────────────

    /// <summary>DN2: helper-owned rows from the process environment only; every exporter option assigned (ADR-013).</summary>
    [Fact]
    public void Exporter_Options_Equal_The_Resolved_Values_And_IConfiguration_Only_Keys_Have_No_Effect()
    {
        // Process environment: endpoint and timeout. IConfiguration only: compression, headers,
        // certificate and another endpoint. ADR-013: the helper reads neither from IConfiguration.
        Environment.SetEnvironmentVariable("OTEL_EXPORTER_OTLP_ENDPOINT", "http://env-collector:4318");
        Environment.SetEnvironmentVariable("OTEL_EXPORTER_OTLP_TIMEOUT", "1234");
        var configuration = Config(
            ("OTEL_EXPORTER_OTLP_ENDPOINT", "http://config-collector:4317"),
            ("OTEL_EXPORTER_OTLP_TIMEOUT", "4321"),
            ("OTEL_EXPORTER_OTLP_COMPRESSION", "gzip"),
            ("OTEL_EXPORTER_OTLP_HEADERS", "x-config=1"),
            ("OTEL_EXPORTER_OTLP_CERTIFICATE", "/config/ca.pem"));

        var (provider, diagnostics) = Build(o => o.ServiceName = "neutral", configuration);
        using (provider)
        {
            var resolved = provider.GetRequiredService<ResolvedTelemetry>();
            Assert.Equal("http://env-collector:4318", resolved.OtelCollectorEndpoint);
            Assert.Equal(TelemetryOptions.DefaultExportTimeoutMs, resolved.ExportTimeoutMs);
            Assert.Equal(TelemetryOptions.CompressionNone, resolved.Compression);
            Assert.Null(resolved.Headers);
            Assert.Null(resolved.CertificatePath);

            // The SDK constructor reads the environment (timeout 1234); the helper overwrites it.
            Assert.Equal(1234, new OtlpExporterOptions().TimeoutMilliseconds);
            foreach (var signal in ResolvedTelemetry.Signals)
            {
                var options = OtlpExporters.OptionsFor(signal, resolved, diagnostics.Sink)!;
                Assert.Equal(new Uri($"http://env-collector:4318/v1/{signal}"), options.Endpoint);
                Assert.Equal(OtlpExportProtocol.HttpProtobuf, options.Protocol);
                Assert.Equal(resolved.ExportTimeoutMs, options.TimeoutMilliseconds);
                Assert.Equal(OtlpExportCompression.None, options.Compression);
                Assert.Equal(string.Empty, options.Headers);
                using var client = options.HttpClientFactory!();
                Assert.False(client.DefaultRequestHeaders.Contains("x-config"));
                Assert.Equal(TimeSpan.FromMilliseconds(resolved.ExportTimeoutMs), client.Timeout);
            }
        }
    }

    [Fact]
    public void Environment_Values_Reach_The_Exporter_Options()
    {
        Environment.SetEnvironmentVariable("OTEL_EXPORTER_OTLP_ENDPOINT", "http://collector:4317");
        Environment.SetEnvironmentVariable("OTEL_EXPORTER_OTLP_COMPRESSION", " GZip ");
        Environment.SetEnvironmentVariable("OTEL_EXPORTER_OTLP_HEADERS", "x-a=1, x-b = two%20words ,broken,=nokey,x-a=3");

        var resolved = TestDiagnostics.ResolveQuiet(o => o.ExportTimeoutMs = 2500);
        var options = OtlpExporters.OptionsFor("traces", resolved, new TestDiagnostics().Sink)!;
        Assert.Equal(new Uri("http://collector:4317"), options.Endpoint);
        Assert.Equal(OtlpExportProtocol.Grpc, options.Protocol);
        Assert.Equal(2500, options.TimeoutMilliseconds);
        Assert.Equal(OtlpExportCompression.GZip, options.Compression);
        Assert.Equal(string.Empty, options.Headers);

        // One long-lived client per exporter instance; headers on the client, never in Headers.
        var first = options.HttpClientFactory!();
        Assert.Same(first, options.HttpClientFactory!());
        Assert.Equal(new[] { "3" }, first.DefaultRequestHeaders.GetValues("x-a"));
        Assert.Equal(new[] { "two words" }, first.DefaultRequestHeaders.GetValues("x-b"));
        Assert.Equal(options.MaxResponseSizeBytes, first.MaxResponseContentBufferSize);
        first.Dispose();
    }

    [Theory]
    [InlineData(null, new string[0])]
    [InlineData("", new string[0])]
    [InlineData("k=%zz,ok=1", new[] { "k=%zz", "ok=1" })]
    [InlineData("a=b=c", new[] { "a=b=c" })]
    [InlineData("K=1,k=2", new[] { "k=2" })]
    public void Header_Parser_Skips_Malformed_Items_And_Never_Throws(string? raw, string[] expected)
    {
        Assert.Equal(expected, HelperHttpClientFactory.ParseHeaders(raw).Select(h => $"{h.Key}={h.Value}"));
    }

    [Fact]
    public void Code_Compression_Beats_The_Environment()
    {
        Environment.SetEnvironmentVariable("OTEL_EXPORTER_OTLP_COMPRESSION", "gzip");
        Assert.Equal(TelemetryOptions.CompressionNone, TestDiagnostics.ResolveQuiet(o => o.Compression = "NONE").Compression);
        Assert.Equal(TelemetryOptions.CompressionGzip, TestDiagnostics.ResolveQuiet(o => o.Compression = " ").Compression);
    }

    [Fact]
    public void Invalid_Env_Compression_Warns_And_Uses_None()
    {
        Environment.SetEnvironmentVariable("OTEL_EXPORTER_OTLP_COMPRESSION", "zstd");
        var diagnostics = new TestDiagnostics();
        Assert.Equal(TelemetryOptions.CompressionNone, diagnostics.Resolve().Compression);
        Assert.Contains(diagnostics.Lines, l => l.Contains("invalid OTEL_EXPORTER_OTLP_COMPRESSION") && l.Contains("value=zstd"));
    }

    [Fact]
    public void Per_Signal_Code_Endpoints_Are_Used_As_Written()
    {
        var resolved = TestDiagnostics.ResolveQuiet(o =>
        {
            o.OtelCollectorEndpoint = "http://collector:4318";
            o.TracesEndpoint = " http://traces:4318/custom/traces ";
        });
        Assert.Equal(("http://traces:4318/custom/traces", TelemetryOptions.ProtocolHttpProtobuf), resolved.OtlpTarget("traces"));
        Assert.Equal(("http://collector:4318/v1/metrics", TelemetryOptions.ProtocolHttpProtobuf), resolved.OtlpTarget("metrics"));
        Assert.Null(TestDiagnostics.ResolveQuiet().OtlpTarget("logs"));
        Assert.Equal(new[] { "otlp" }, TestDiagnostics.ResolveQuiet(o => o.MetricsEndpoint = "http://m:4317").MetricExporters);
    }

    [Fact]
    public void Unusable_Endpoint_Warns_And_Exports_Nothing()
    {
        Environment.SetEnvironmentVariable("OTEL_EXPORTER_OTLP_ENDPOINT", "bad host:4317");
        var diagnostics = new TestDiagnostics();
        var resolved = diagnostics.Resolve();
        Assert.Null(OtlpExporters.OptionsFor("logs", resolved, diagnostics.Sink));
        Assert.Contains(diagnostics.Lines, l => l.Contains("the OTLP endpoint for logs cannot be used"));
    }

    // ── bound options count as code (DN4) ──────────────────────────────

    /// <summary>DN4: a section bound into TelemetryOptions is code and beats OTEL_* env.</summary>
    [Fact]
    public void Options_Bound_From_IConfiguration_Count_As_Code_And_Beat_Env()
    {
        Environment.SetEnvironmentVariable("OTEL_SERVICE_NAME", "from-env");
        Environment.SetEnvironmentVariable("OTEL_EXPORTER_OTLP_PROTOCOL", "grpc");
        var configuration = Config(
            ("Otel:ServiceName", "from-binding"),
            ("Otel:OtlpProtocol", "http/protobuf"),
            ("Otel:ExportIntervalMs", "5000"),
            ("Otel:PrometheusMetricsPort", "0"));
        var services = new ServiceCollection();
        services.AddSingleton<IOtelHelperDiagnostics>(new TestDiagnostics().Sink);
        services.AddOtelHelper();
        services.Configure<TelemetryOptions>(configuration.GetSection("Otel"));
        using var provider = services.BuildServiceProvider();

        var resolved = provider.GetRequiredService<ResolvedTelemetry>();
        Assert.Equal("from-binding", resolved.ServiceName);
        Assert.Equal(TelemetryOptions.ProtocolHttpProtobuf, resolved.OtlpProtocol);
        Assert.Equal(5000, resolved.ExportIntervalMs);
        Assert.Equal(0, resolved.PrometheusMetricsPort);
    }

    [Fact]
    public void Code_Port_Beats_Env_Port_N8()
    {
        Environment.SetEnvironmentVariable("OTEL_HELPER_METRICS_PORT", " 9999");
        Assert.Equal(1234, TestDiagnostics.ResolveQuiet(o => o.PrometheusMetricsPort = 1234).PrometheusMetricsPort);
        Assert.Equal(9999, TestDiagnostics.ResolveQuiet().PrometheusMetricsPort);
    }

    [Fact]
    public void Env_Port_Out_Of_Range_Warns_And_Uses_The_Default()
    {
        Environment.SetEnvironmentVariable("OTEL_HELPER_METRICS_PORT", "70000");
        var diagnostics = new TestDiagnostics();
        Assert.Equal(TelemetryOptions.DefaultPrometheusMetricsPort, diagnostics.Resolve().PrometheusMetricsPort);
        Assert.Contains(diagnostics.Lines, l => l.Contains("invalid OTEL_HELPER_METRICS_PORT"));
    }

    [Fact]
    public void Code_Environment_And_Debug_Win()
    {
        Environment.SetEnvironmentVariable("ENVIRONMENT", "PRD");
        var resolved = TestDiagnostics.ResolveQuiet(o => { o.Environment = "staging"; o.DebugLevel = false; });
        Assert.Equal("staging", resolved.Environment);
        Assert.False(resolved.DebugLevel);
        Assert.Equal(Microsoft.Extensions.Logging.LogLevel.Information, resolved.MinimumLogLevel);
    }

    // ── unsupported variables and the SDK-owned switch ─────────────────

    [Theory]
    [InlineData("OTEL_CONFIG_FILE")]
    [InlineData("OTEL_EXPERIMENTAL_CONFIG_FILE")]
    public void Config_File_Variables_Warn_Once_Naming_Only_The_Variable(string variable)
    {
        Environment.SetEnvironmentVariable(variable, "/etc/parity-config-path/otel.yaml");
        var diagnostics = new TestDiagnostics();
        diagnostics.Resolve();
        diagnostics.Resolve();
        var line = Assert.Single(diagnostics.Lines);
        Assert.StartsWith($"otelhelper: WARN {variable} is not supported by the .NET helper and is ignored", line);
        Assert.DoesNotContain("parity-config-path", diagnostics.Text);
    }

    [Fact]
    public void Sdk_Disabled_Is_Read_From_IConfiguration_Like_The_Sdk()
    {
        // IConfiguration says true while the process environment says nothing.
        var (provider, diagnostics) = Build(o => o.ServiceName = "disabled", Config(("OTEL_SDK_DISABLED", "true")));
        using (provider)
        {
            var resolved = provider.GetRequiredService<ResolvedTelemetry>();
            Assert.True(resolved.SdkDisabled);
            Assert.Empty(resolved.ResolvedMetricExporters());
            Assert.False(resolved.PrometheusListenerActive);
            Assert.Contains("otelhelper: INFO OTEL_SDK_DISABLED=true: telemetry disabled, only propagators installed", diagnostics.Lines);
        }
    }

    [Fact]
    public void Sdk_Disabled_In_Env_Without_IConfiguration_Is_Honored()
    {
        Environment.SetEnvironmentVariable("OTEL_SDK_DISABLED", "TRUE");
        var diagnostics = new TestDiagnostics();
        Assert.True(diagnostics.Resolve().SdkDisabled);
        Assert.Single(diagnostics.Lines, l => l.Contains("INFO OTEL_SDK_DISABLED=true"));
        Environment.SetEnvironmentVariable("OTEL_SDK_DISABLED", "false");
        Assert.False(TestDiagnostics.ResolveQuiet().SdkDisabled);
    }

    [Fact]
    public async Task Host_Start_Resolves_And_Reports_Even_When_Nothing_Reads_The_Values()
    {
        var diagnostics = new TestDiagnostics();
        var builder = Host.CreateApplicationBuilder();
        builder.Configuration["OTEL_SDK_DISABLED"] = "true";
        builder.Services.AddSingleton<IOtelHelperDiagnostics>(diagnostics.Sink);
        builder.Services.AddOtelHelper(o => o.PrometheusMetricsPort = 0);
        using var host = builder.Build();
        await host.StartAsync();
        await host.StopAsync();
        Assert.Contains(diagnostics.Lines, l => l.Contains("OTEL_SDK_DISABLED=true"));
    }

    // ── code values fail at start (D-NET6, G1 analog) ──────────────────

    [Fact]
    public async Task Invalid_Code_Value_Fails_Host_Start_And_Leaves_No_Listener()
    {
        var port = TestPorts.FreeTcpPort();
        var builder = Host.CreateApplicationBuilder();
        builder.Services.AddSingleton<IOtelHelperDiagnostics>(new TestDiagnostics().Sink);
        builder.Services.AddOtelHelper(o =>
        {
            o.PrometheusMetricsPort = port;
            o.OtelCollectorEndpoint = "http://user:parity-secret@collector:4317/?q=parity-secret";
            o.OtlpProtocol = "bogus";
        });
        using var host = builder.Build();
        var ex = await Assert.ThrowsAsync<OptionsValidationException>(() => host.StartAsync());
        Assert.Contains("TelemetryOptions.OtlpProtocol", ex.Message);
        Assert.DoesNotContain("parity-secret", ex.Message);

        using var http = new HttpClient { Timeout = TimeSpan.FromSeconds(5) };
        await Assert.ThrowsAsync<HttpRequestException>(() => http.GetAsync($"http://localhost:{port}/metrics"));
    }

    // ── TLS through the helper handler ─────────────────────────────────

    private static X509Certificate2 SelfSigned(string name)
    {
        using var key = ECDsa.Create(ECCurve.NamedCurves.nistP256);
        var request = new CertificateRequest($"CN={name}", key, HashAlgorithmName.SHA256);
        request.CertificateExtensions.Add(new X509BasicConstraintsExtension(true, false, 0, true));
        return request.CreateSelfSigned(DateTimeOffset.UtcNow.AddMinutes(-5), DateTimeOffset.UtcNow.AddHours(1));
    }

    [Fact]
    public void Custom_Root_Accepts_Only_Chain_Errors_Fixed_By_The_Root()
    {
        using var root = SelfSigned("parity-root");
        using var other = SelfSigned("parity-other");
        var roots = new X509Certificate2Collection { root };

        Assert.True(HelperHttpClientFactory.ValidateWithCustomRoot(root, SslPolicyErrors.None, roots));
        Assert.True(HelperHttpClientFactory.ValidateWithCustomRoot(root, SslPolicyErrors.RemoteCertificateChainErrors, roots));
        Assert.False(HelperHttpClientFactory.ValidateWithCustomRoot(other, SslPolicyErrors.RemoteCertificateChainErrors, roots));
        Assert.False(HelperHttpClientFactory.ValidateWithCustomRoot(root, SslPolicyErrors.RemoteCertificateNameMismatch, roots));
        Assert.False(HelperHttpClientFactory.ValidateWithCustomRoot(null, SslPolicyErrors.RemoteCertificateChainErrors, roots));
    }

    [Fact]
    public void Tls_Files_From_The_Environment_Are_Loaded_Or_Warned()
    {
        var dir = Directory.CreateTempSubdirectory("otelhelper-tls-");
        try
        {
            using var cert = SelfSigned("parity-client");
            var certPath = Path.Combine(dir.FullName, "client.pem");
            var keyPath = Path.Combine(dir.FullName, "client.key");
            File.WriteAllText(certPath, cert.ExportCertificatePem());
            File.WriteAllText(keyPath, cert.GetECDsaPrivateKey()!.ExportPkcs8PrivateKeyPem());
            Environment.SetEnvironmentVariable("OTEL_EXPORTER_OTLP_ENDPOINT", "https://collector:4317");
            Environment.SetEnvironmentVariable("OTEL_EXPORTER_OTLP_CERTIFICATE", certPath);
            Environment.SetEnvironmentVariable("OTEL_EXPORTER_OTLP_CLIENT_CERTIFICATE", certPath);
            Environment.SetEnvironmentVariable("OTEL_EXPORTER_OTLP_CLIENT_KEY", keyPath);

            var good = new TestDiagnostics();
            using (OtlpExporters.OptionsFor("traces", good.Resolve(), good.Sink)!.HttpClientFactory!())
            {
            }

            Assert.Empty(good.Lines);

            Environment.SetEnvironmentVariable("OTEL_EXPORTER_OTLP_CERTIFICATE", Path.Combine(dir.FullName, "missing.pem"));
            Environment.SetEnvironmentVariable("OTEL_EXPORTER_OTLP_CLIENT_KEY", Path.Combine(dir.FullName, "missing.key"));
            var bad = new TestDiagnostics();
            using (OtlpExporters.OptionsFor("traces", bad.Resolve(), bad.Sink)!.HttpClientFactory!())
            {
            }

            Assert.Contains(bad.Lines, l => l.Contains("cannot read the OTLP CA certificate file") && l.Contains("variable=OTEL_EXPORTER_OTLP_CERTIFICATE"));
            Assert.Contains(bad.Lines, l => l.Contains("cannot load the OTLP client certificate"));
            Assert.DoesNotContain(dir.FullName, bad.Text);
        }
        finally
        {
            dir.Delete(recursive: true);
        }
    }

    // ── the helper client never follows a redirect (C3; the loud refusal is C8) ──

    [Fact]
    public async Task Helper_Client_Does_Not_Follow_Redirects()
    {
        var port = TestPorts.FreeTcpPort();
        using var listener = new HttpListener();
        listener.Prefixes.Add($"http://localhost:{port}/");
        listener.Start();
        var serve = Task.Run(async () =>
        {
            var context = await listener.GetContextAsync();
            context.Response.StatusCode = 307;
            context.Response.RedirectLocation = $"http://localhost:{port}/elsewhere";
            context.Response.Close();
        });

        var resolved = TestDiagnostics.ResolveQuiet(o => { o.OtelCollectorEndpoint = $"http://localhost:{port}"; o.OtlpProtocol = "http/protobuf"; });
        using var client = OtlpExporters.OptionsFor("traces", resolved, new TestDiagnostics().Sink)!.HttpClientFactory!();
        using var response = await client.GetAsync($"http://localhost:{port}/v1/traces");
        Assert.Equal(HttpStatusCode.TemporaryRedirect, response.StatusCode);
        await serve;
    }

    // ── forwarded SDK text: configured secrets masked (env) ────────────

    [Fact]
    public void Forwarded_Text_Masks_Header_And_Endpoint_Secrets_From_The_Environment()
    {
        Environment.SetEnvironmentVariable("OTEL_EXPORTER_OTLP_HEADERS", "authorization=Bearer parity-token-1,x-k=parity%20two");
        Environment.SetEnvironmentVariable("OTEL_EXPORTER_OTLP_ENDPOINT", "http://user:parity-pass@collector:4317");
        var text = "echo parity-token-1 \"parity two\" Bearer parity-token-1 parity-pass done";
        Assert.Equal("echo <redacted> \"<redacted>\" <redacted> <redacted> done", SdkTextSanitizer.Sanitize(text, Array.Empty<string>()));
    }
}
