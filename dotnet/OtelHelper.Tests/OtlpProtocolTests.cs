using System;
using System.Diagnostics;
using System.Diagnostics.Metrics;
using System.Net;
using System.Threading.Tasks;
using OpenTelemetry;
using OpenTelemetry.Metrics;
using OpenTelemetry.Trace;
using OtelHelper;
using OtelHelper.Metrics;
using OtelHelper.Tracing;
using Xunit;

namespace OtelHelper.Tests;

/// <summary>
/// OTLP wire protocol selection (grpc vs http/protobuf) — tracing and metrics.
/// Ported from bdcotelhelper's port-based auto-detection (4318 -> http/protobuf),
/// extended to honor the standard OTEL_EXPORTER_OTLP_PROTOCOL env var first (see
/// TelemetryOptions.ResolvedOtlpProtocol for the full precedence order).
/// Cross-language parity: python/tests/test_otlp_protocol.py and go/otlp_protocol_test.go.
/// </summary>
[Collection("EnvVarTests")]
public class OtlpProtocolTests : IDisposable
{
    private static readonly string[] ContractEnvVars =
    {
        TelemetryOptions.OtlpProtocolEnvVar,
        TelemetryOptions.CollectorEndpointEnvVar,
    };

    public OtlpProtocolTests() => ClearEnv();
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

    // --- ResolvedOtlpProtocol precedence ---

    [Fact]
    public void DefaultIsGrpc()
    {
        var opts = Resolved(o => o.OtelCollectorEndpoint = "http://collector:4317");
        Assert.Equal(TelemetryOptions.ProtocolGrpc, opts.ResolvedOtlpProtocol());
    }

    [Fact]
    public void NoEndpoint_DefaultsToGrpc()
    {
        var opts = Resolved();
        Assert.Equal(TelemetryOptions.ProtocolGrpc, opts.ResolvedOtlpProtocol());
    }

    [Fact]
    public void Port4318_InfersHttp()
    {
        var opts = Resolved(o => o.OtelCollectorEndpoint = "http://collector:4318");
        Assert.Equal(TelemetryOptions.ProtocolHttpProtobuf, opts.ResolvedOtlpProtocol());
    }

    [Fact]
    public void Port4317_InfersGrpc()
    {
        var opts = Resolved(o => o.OtelCollectorEndpoint = "http://collector:4317");
        Assert.Equal(TelemetryOptions.ProtocolGrpc, opts.ResolvedOtlpProtocol());
    }

    [Fact]
    public void OtherPort_InfersGrpc()
    {
        var opts = Resolved(o => o.OtelCollectorEndpoint = "http://collector:9999");
        Assert.Equal(TelemetryOptions.ProtocolGrpc, opts.ResolvedOtlpProtocol());
    }

    [Fact]
    public void EnvVar_BeatsPortInference()
    {
        Environment.SetEnvironmentVariable(TelemetryOptions.OtlpProtocolEnvVar, "http/protobuf");
        var opts = Resolved(o => o.OtelCollectorEndpoint = "http://collector:4317"); // would infer grpc
        Assert.Equal(TelemetryOptions.ProtocolHttpProtobuf, opts.ResolvedOtlpProtocol());
    }

    [Fact]
    public void EnvVarGrpc_BeatsHttpPortInference()
    {
        Environment.SetEnvironmentVariable(TelemetryOptions.OtlpProtocolEnvVar, "grpc");
        var opts = Resolved(o => o.OtelCollectorEndpoint = "http://collector:4318"); // would infer http
        Assert.Equal(TelemetryOptions.ProtocolGrpc, opts.ResolvedOtlpProtocol());
    }

    [Fact]
    public void ExplicitOption_BeatsEnv()
    {
        Environment.SetEnvironmentVariable(TelemetryOptions.OtlpProtocolEnvVar, "http/protobuf");
        var opts = Resolved(o =>
        {
            o.OtelCollectorEndpoint = "http://collector:4317";
            o.OtlpProtocol = "grpc";
        });
        Assert.Equal(TelemetryOptions.ProtocolGrpc, opts.ResolvedOtlpProtocol());
    }

    [Fact]
    public void EnvVar_CaseAndWhitespaceTolerant()
    {
        Environment.SetEnvironmentVariable(TelemetryOptions.OtlpProtocolEnvVar, "  HTTP/PROTOBUF  ");
        var opts = Resolved(o => o.OtelCollectorEndpoint = "http://collector:4317");
        Assert.Equal(TelemetryOptions.ProtocolHttpProtobuf, opts.ResolvedOtlpProtocol());
    }

    // --- validation ---

    [Fact]
    public void UnknownValue_FailsValidation()
    {
        Environment.SetEnvironmentVariable(TelemetryOptions.OtlpProtocolEnvVar, "http/json");
        var failure = Validate(Resolved(o => o.ServiceName = "svc"));
        Assert.NotNull(failure);
        Assert.Contains("Unknown OTLP protocol", failure);
    }

    [Fact]
    public void HttpJson_RejectedEvenThoughSpecValid()
    {
        // http/json is a valid spec value but no .NET OTLP exporter implements it
        // for traces/metrics/logs — must fail loud, not silently downgrade.
        Environment.SetEnvironmentVariable(TelemetryOptions.OtlpProtocolEnvVar, "http/json");
        var failure = Validate(Resolved(o => o.ServiceName = "svc"));
        Assert.NotNull(failure);
    }

    [Theory]
    [InlineData("grpc")]
    [InlineData("http/protobuf")]
    public void GrpcAndHttpProtobuf_PassValidation(string protocol)
    {
        var opts = Resolved(o =>
        {
            o.ServiceName = "svc";
            o.OtlpProtocol = protocol;
        });
        Assert.Null(Validate(opts));
    }

    // --- live integration: HTTP protocol actually delivers to /v1/{signal} ---

    [Fact]
    public async Task Tracing_Http_DeliversToV1Traces()
    {
        var port = FreeTcpPort();
        string? gotPath = null;
        using var listener = new HttpListener();
        listener.Prefixes.Add($"http://localhost:{port}/");
        listener.Start();
        var listenTask = Task.Run(async () =>
        {
            var ctx = await listener.GetContextAsync();
            gotPath = ctx.Request.Url?.AbsolutePath;
            ctx.Response.StatusCode = 200;
            ctx.Response.Close();
        });

        var opts = Resolved(o =>
        {
            o.ServiceName = "http-trace-test";
            o.OtelCollectorEndpoint = $"http://localhost:{port}";
            o.OtlpProtocol = TelemetryOptions.ProtocolHttpProtobuf;
        });

        using var tp = Sdk.CreateTracerProviderBuilder().ConfigureTracing(opts).Build();
        using (var source = new ActivitySource("http-trace-test"))
        using (var activity = source.StartActivity("test-span"))
        {
        }
        tp.ForceFlush();

        await Task.WhenAny(listenTask, Task.Delay(TimeSpan.FromSeconds(5)));
        Assert.Equal("/v1/traces", gotPath);
    }

    [Fact]
    public async Task Metrics_Http_DeliversToV1Metrics()
    {
        var port = FreeTcpPort();
        string? gotPath = null;
        using var listener = new HttpListener();
        listener.Prefixes.Add($"http://localhost:{port}/");
        listener.Start();
        var listenTask = Task.Run(async () =>
        {
            var ctx = await listener.GetContextAsync();
            gotPath = ctx.Request.Url?.AbsolutePath;
            ctx.Response.StatusCode = 200;
            ctx.Response.Close();
        });

        var opts = Resolved(o =>
        {
            o.ServiceName = "http-metric-test";
            o.OtelCollectorEndpoint = $"http://localhost:{port}";
            o.MetricExporters = "otlp";
            o.OtlpProtocol = TelemetryOptions.ProtocolHttpProtobuf;
        });

        using var mp = Sdk.CreateMeterProviderBuilder().ConfigureMetrics(opts).Build();
        using var meter = new Meter("http-metric-test");
        var counter = meter.CreateCounter<long>("http_protocol_test");
        counter.Add(1);
        mp.ForceFlush();

        await Task.WhenAny(listenTask, Task.Delay(TimeSpan.FromSeconds(5)));
        Assert.Equal("/v1/metrics", gotPath);
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
