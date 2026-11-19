using StaffOps.OtelHelper;
using Xunit;

namespace StaffOps.OtelHelper.Tests;

/// <summary>
/// Tests for TLS endpoint resolution in TelemetryResolver.
/// Validates that the OTLP endpoint URI scheme is correctly resolved for TLS support:
/// - Explicit https:// scheme is preserved (TLS enabled).
/// - Explicit http:// scheme is preserved (plaintext).
/// - Schemeless endpoint defaults to https:// (secure by default).
/// - OTEL_EXPORTER_OTLP_INSECURE=true forces http:// for schemeless endpoints.
/// </summary>
[Collection("EnvVarTests")]
[ResetGlobalPropagation]
public class TlsEndpointResolutionTests : IDisposable
{
    private readonly string? _savedEndpoint;
    private readonly string? _savedInsecure;

    public TlsEndpointResolutionTests()
    {
        _savedEndpoint = Environment.GetEnvironmentVariable(TelemetryOptions.CollectorEndpointEnvVar);
        _savedInsecure = Environment.GetEnvironmentVariable(TelemetryOptions.InsecureEnvVar);
    }

    public void Dispose()
    {
        Environment.SetEnvironmentVariable(TelemetryOptions.CollectorEndpointEnvVar, _savedEndpoint);
        Environment.SetEnvironmentVariable(TelemetryOptions.InsecureEnvVar, _savedInsecure);
    }

    private static ResolvedTelemetry PostConfigure(Action<TelemetryOptions>? configure = null)
        => TestDiagnostics.ResolveQuiet(configure);

    [Fact]
    public void HttpsScheme_Preserved_When_Explicit()
    {
        Environment.SetEnvironmentVariable(TelemetryOptions.CollectorEndpointEnvVar, "https://gw:4317");
        Environment.SetEnvironmentVariable(TelemetryOptions.InsecureEnvVar, null);

        var opts = PostConfigure();

        Assert.Equal("https://gw:4317", opts.OtelCollectorEndpoint);
    }

    [Fact]
    public void HttpScheme_Preserved_When_Explicit()
    {
        Environment.SetEnvironmentVariable(TelemetryOptions.CollectorEndpointEnvVar, "http://gw:4317");
        Environment.SetEnvironmentVariable(TelemetryOptions.InsecureEnvVar, null);

        var opts = PostConfigure();

        Assert.Equal("http://gw:4317", opts.OtelCollectorEndpoint);
    }

    [Fact]
    public void Schemeless_Defaults_To_Https_SecureByDefault()
    {
        Environment.SetEnvironmentVariable(TelemetryOptions.CollectorEndpointEnvVar, "gw:4317");
        Environment.SetEnvironmentVariable(TelemetryOptions.InsecureEnvVar, null);

        var opts = PostConfigure();

        Assert.Equal("https://gw:4317", opts.OtelCollectorEndpoint);
    }

    [Fact]
    public void Schemeless_With_Insecure_True_Uses_Http()
    {
        Environment.SetEnvironmentVariable(TelemetryOptions.CollectorEndpointEnvVar, "gw:4317");
        Environment.SetEnvironmentVariable(TelemetryOptions.InsecureEnvVar, "true");

        var opts = PostConfigure();

        Assert.Equal("http://gw:4317", opts.OtelCollectorEndpoint);
    }

    [Fact]
    public void Schemeless_With_Insecure_False_Uses_Https()
    {
        Environment.SetEnvironmentVariable(TelemetryOptions.CollectorEndpointEnvVar, "gw:4317");
        Environment.SetEnvironmentVariable(TelemetryOptions.InsecureEnvVar, "false");

        var opts = PostConfigure();

        Assert.Equal("https://gw:4317", opts.OtelCollectorEndpoint);
    }

    [Fact]
    public void Schemeless_HostOnly_Defaults_Port_4317_And_Https()
    {
        Environment.SetEnvironmentVariable(TelemetryOptions.CollectorEndpointEnvVar, "otel-gateway-0.example.com");
        Environment.SetEnvironmentVariable(TelemetryOptions.InsecureEnvVar, null);

        var opts = PostConfigure();

        Assert.Equal("https://otel-gateway-0.example.com:4317", opts.OtelCollectorEndpoint);
    }

    [Fact]
    public void HttpsScheme_Preserves_Custom_Port()
    {
        Environment.SetEnvironmentVariable(TelemetryOptions.CollectorEndpointEnvVar, "https://gw:4318");
        Environment.SetEnvironmentVariable(TelemetryOptions.InsecureEnvVar, null);

        var opts = PostConfigure();

        Assert.Equal("https://gw:4318", opts.OtelCollectorEndpoint);
    }

    [Fact]
    public void Schemeless_With_Custom_Port_And_Insecure()
    {
        Environment.SetEnvironmentVariable(TelemetryOptions.CollectorEndpointEnvVar, "gw:4318");
        Environment.SetEnvironmentVariable(TelemetryOptions.InsecureEnvVar, "true");

        var opts = PostConfigure();

        Assert.Equal("http://gw:4318", opts.OtelCollectorEndpoint);
    }

    [Fact]
    public void ConsumerSet_Endpoint_Not_Overridden()
    {
        // If consumer already set the endpoint in code, PostConfigure must not overwrite it.
        Environment.SetEnvironmentVariable(TelemetryOptions.CollectorEndpointEnvVar, "https://env-gateway:4317");
        Environment.SetEnvironmentVariable(TelemetryOptions.InsecureEnvVar, null);

        var opts = PostConfigure(o => o.OtelCollectorEndpoint = "https://my-custom-gw:4317");

        Assert.Equal("https://my-custom-gw:4317", opts.OtelCollectorEndpoint);
    }

    [Fact]
    public void Insecure_EnvVar_Ignored_When_Scheme_Explicit_Https()
    {
        // Even with INSECURE=true, an explicit https:// scheme takes precedence.
        Environment.SetEnvironmentVariable(TelemetryOptions.CollectorEndpointEnvVar, "https://gw:4317");
        Environment.SetEnvironmentVariable(TelemetryOptions.InsecureEnvVar, "true");

        var opts = PostConfigure();

        Assert.Equal("https://gw:4317", opts.OtelCollectorEndpoint);
    }

    [Fact]
    public void HttpsScheme_DefaultPort_Gets_4317()
    {
        // https://gw (no port) -> scheme is https, port defaults to 443 in URI;
        // we normalize to 4317 since it's the OTLP standard port.
        Environment.SetEnvironmentVariable(TelemetryOptions.CollectorEndpointEnvVar, "https://gw");
        Environment.SetEnvironmentVariable(TelemetryOptions.InsecureEnvVar, null);

        var opts = PostConfigure();

        Assert.Equal("https://gw:4317", opts.OtelCollectorEndpoint);
    }

    [Theory]
    [InlineData("collector:4317", null, "https://collector:4317")]
    [InlineData("collector", null, "https://collector:4317")]
    [InlineData("collector", true, "http://collector:4317")]
    [InlineData("http://collector:4317", null, "http://collector:4317")]
    public void EndpointResolver_Unit_Grpc(string raw, bool? insecure, string expected)
    {
        // C7a: the pure resolver replaces the 0.2 ResolveEndpoint (no environment read).
        var outcome = EndpointResolver.Resolve(new EndpointInput
        {
            Raw = raw, Variable = "V", Signal = "traces", Protocol = TelemetryOptions.ProtocolGrpc, Insecure = insecure,
            InsecureVariable = "I",
        });
        Assert.Equal(expected, outcome.Url);
    }

    // --- N10 and the code Insecure option ---

    [Theory]
    [InlineData(null, "https://gw:4317")]
    [InlineData(true, "http://gw:4317")]
    [InlineData(false, "https://gw:4317")]
    public void Schemeless_Code_Endpoint_Is_Resolved_Not_A_Scheme(bool? insecure, string expected)
    {
        Environment.SetEnvironmentVariable(TelemetryOptions.InsecureEnvVar, "true");
        var opts = PostConfigure(o =>
        {
            o.OtelCollectorEndpoint = "gw:4317";
            o.Insecure = insecure ?? (bool?)null;
        });
        // Code Insecure beats the environment; unset, the environment (true) applies.
        Assert.Equal(insecure is null ? "http://gw:4317" : expected, opts.OtelCollectorEndpoint);
    }

    [Fact]
    public void Http_Code_Endpoint_Keeps_Its_Path_For_Http_Protobuf()
    {
        // C7a: an HTTP URL keeps its path (and the scheme default port); gRPC dials host:port only,
        // a portless gRPC URL gets 4317 (D1.b).
        var http = PostConfigure(o => { o.OtelCollectorEndpoint = "https://gw.example.com/otlp"; o.OtlpProtocol = TelemetryOptions.ProtocolHttpProtobuf; });
        Assert.Equal("https://gw.example.com/otlp", http.OtelCollectorEndpoint);
        Assert.Equal(("https://gw.example.com/otlp/v1/traces", TelemetryOptions.ProtocolHttpProtobuf), http.OtlpTarget("traces"));

        var grpc = PostConfigure(o => o.OtelCollectorEndpoint = "https://gw.example.com/otlp");
        Assert.Equal("https://gw.example.com:4317", grpc.OtelCollectorEndpoint);
    }

    [Fact]
    public void Code_Insecure_Applies_To_Schemeless_Env_Endpoint()
    {
        Environment.SetEnvironmentVariable(TelemetryOptions.CollectorEndpointEnvVar, "gw:4317");
        Environment.SetEnvironmentVariable(TelemetryOptions.InsecureEnvVar, null);
        Assert.Equal("http://gw:4317", PostConfigure(o => o.Insecure = true).OtelCollectorEndpoint);
    }
}
