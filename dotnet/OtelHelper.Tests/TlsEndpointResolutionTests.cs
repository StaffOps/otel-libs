using OtelHelper;
using Xunit;

namespace OtelHelper.Tests;

/// <summary>
/// Tests for TLS endpoint resolution in TelemetryOptionsPostConfigure.
/// Validates that the OTLP endpoint URI scheme is correctly resolved for TLS support:
/// - Explicit https:// scheme is preserved (TLS enabled).
/// - Explicit http:// scheme is preserved (plaintext).
/// - Schemeless endpoint defaults to https:// (secure by default).
/// - OTEL_EXPORTER_OTLP_INSECURE=true forces http:// for schemeless endpoints.
/// </summary>
[Collection("EnvVarTests")]
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

    private static TelemetryOptions PostConfigure()
    {
        var opts = new TelemetryOptions();
        new TelemetryOptionsPostConfigure().PostConfigure(null, opts);
        return opts;
    }

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

        var opts = new TelemetryOptions { OtelCollectorEndpoint = "https://my-custom-gw:4317" };
        new TelemetryOptionsPostConfigure().PostConfigure(null, opts);

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

    [Fact]
    public void ResolveEndpoint_Unit_Schemeless_Host_Port()
    {
        // Direct unit test of the static helper (no env var side effects for the endpoint itself,
        // but INSECURE env var is read inside).
        Environment.SetEnvironmentVariable(TelemetryOptions.InsecureEnvVar, null);

        var result = TelemetryOptionsPostConfigure.ResolveEndpoint("collector:4317");
        Assert.Equal("https://collector:4317", result);
    }

    [Fact]
    public void ResolveEndpoint_Unit_Schemeless_Host_Only()
    {
        Environment.SetEnvironmentVariable(TelemetryOptions.InsecureEnvVar, null);

        var result = TelemetryOptionsPostConfigure.ResolveEndpoint("collector");
        Assert.Equal("https://collector:4317", result);
    }

    [Fact]
    public void ResolveEndpoint_Unit_Http_Explicit()
    {
        Environment.SetEnvironmentVariable(TelemetryOptions.InsecureEnvVar, null);

        var result = TelemetryOptionsPostConfigure.ResolveEndpoint("http://collector:4317");
        Assert.Equal("http://collector:4317", result);
    }
}
