using Microsoft.Extensions.DependencyInjection;
using Microsoft.Extensions.Options;
using StaffOps.OtelHelper.Diagnostics;
using Xunit;

namespace StaffOps.OtelHelper.Tests;

/// <summary>
/// Task C7a: the endpoint grammar (contract design D1 edge-case rulings, A17) and its use by the
/// resolver — RFC 3986 split, ports, host rules, IDN, rendering, schemeless rewrite, portless
/// gRPC, INSECURE grammar and conflicts, protocol order and the 4318 inference, per-signal
/// fallback, userinfo/query removal and the '@'-in-path warning.
/// </summary>
public class EndpointGrammarTests
{
    // ── RFC 3986 split ──────────────────────────────────────────────────

    [Theory]
    [InlineData("http://u:p@h:4317/a/b?q=1#f", "http", true, "h", "4317", "/a/b", true, true, "q=1")]
    [InlineData("HTTPS://h", "https", false, "h", "", "", false, false, "")]
    [InlineData("http://h:", "http", false, "h", "", "", false, false, "")]
    [InlineData("http://user:12/pw@collector:4317", "http", false, "user", "12", "/pw@collector:4317", false, false, "")]
    [InlineData("http://a@b@h:1", "http", true, "h", "1", "", false, false, "")]
    [InlineData("http://[::1]:4317/x", "http", false, "[::1]", "4317", "/x", false, false, "")]
    [InlineData("http://h:4317/?", "http", false, "h", "4317", "/", true, false, "")]
    [InlineData("http://h:4317#", "http", false, "h", "4317", "", false, true, "")]
    [InlineData("http://h:04318", "http", false, "h", "4318", "", false, false, "")]
    public void Split_Follows_Rfc3986(string text, string scheme, bool userinfo, string host, string port, string path, bool hasQuery, bool hasFragment, string query)
    {
        Assert.True(EndpointGrammar.TrySplit(text, out var sp));
        Assert.Equal(new RfcEndpoint(scheme, userinfo, host, port, path, hasQuery, hasFragment, query), sp);
    }

    [Theory]
    [InlineData("collector:4317")]
    [InlineData("//h:4317")]
    [InlineData("http://[::1:4317")]
    [InlineData("http://[::1]x:4317")]
    [InlineData("http://h:0")]
    [InlineData("http://h:70000")]
    [InlineData("http://h:43a7")]
    [InlineData("http://h:1:2")]
    public void Split_Rejects_A_Malformed_Authority(string text)
    {
        Assert.False(EndpointGrammar.TrySplit(text, out _));
    }

    [Theory]
    [InlineData("4318", true, "4318")]
    [InlineData("04318", true, "4318")]
    [InlineData("65535", true, "65535")]
    [InlineData("1", true, "1")]
    [InlineData("0", false, "")]
    [InlineData("000", false, "")]
    [InlineData("65536", false, "")]
    [InlineData("100000", false, "")]
    [InlineData("-1", false, "")]
    [InlineData("+1", false, "")]
    [InlineData("\u0661", false, "")]
    public void Ports_Compare_Numerically(string text, bool ok, string port)
    {
        Assert.Equal(ok, EndpointGrammar.TryParsePort(text, out var parsed));
        Assert.Equal(port, parsed);
    }

    [Fact]
    public void A_Port_Of_Thousands_Of_Digits_Never_Throws()
    {
        // parity endpoint.port_long_digits: compared after stripping the zeros.
        Assert.True(EndpointGrammar.TryParsePort(new string('0', 5000) + "4318", out var port));
        Assert.Equal("4318", port);
        Assert.False(EndpointGrammar.TryParsePort(new string('9', 5000), out _));
    }

    // ── host rules ──────────────────────────────────────────────────────

    [Theory]
    [InlineData("collector", "collector")]
    [InlineData("Collector.Example.COM", "collector.example.com")]
    [InlineData("[::1]", "[::1]")]
    [InlineData("[FE80::1]", "[fe80::1]")]
    [InlineData("10.0.0.1", "10.0.0.1")]
    [InlineData("10.0.0.1.", "10.0.0.1.")]
    [InlineData("a-b_c", "a-b_c")]
    [InlineData("b\u00fccher.de", "xn--bcher-kva.de")]
    [InlineData("B\u00dcCHER.de", "xn--bcher-kva.de")]
    [InlineData("stra\u00dfe.de", "xn--strae-oqa.de")]
    [InlineData("\u00fc\u3002de", "xn--tda.de")]
    public void Valid_Hosts_Normalize(string host, string expected)
    {
        Assert.True(EndpointGrammar.CheckHost(host, out var normalized, out var zoneId));
        Assert.Equal(expected, normalized);
        Assert.False(zoneId);
    }

    [Theory]
    [InlineData("")]
    [InlineData("coll ector")]
    [InlineData("%63ollector")]
    [InlineData("a{b")]
    [InlineData("\"h\"")]
    [InlineData("127.1")]
    [InlineData("1.2.3.256")]
    [InlineData("1.2.3.4.5")]
    [InlineData("01.2.3.4")]
    [InlineData("0x7f.0.0.1")]
    [InlineData("2130706433")]
    [InlineData("h.0x")]
    [InlineData("[1.2.3.4]")]
    [InlineData("[::g]")]
    [InlineData("[]")]
    [InlineData("\ufffd.example")]
    [InlineData("a\u200db.example")]
    [InlineData("\uff11\uff12\uff17.\uff11")]
    [InlineData("a\u00fc..de")]
    [InlineData("a!b")]
    [InlineData("a$b")]
    [InlineData("a~b")]
    [InlineData("a=b")]
    [InlineData("a..b")]
    [InlineData("1.2.3.4..")]
    public void Invalid_Hosts_Are_Rejected(string host)
    {
        Assert.False(EndpointGrammar.CheckHost(host, out _, out var zoneId));
        Assert.False(zoneId);
    }

    [Fact]
    public void A_Non_Ascii_Host_Longer_Than_253_After_Punycode_Is_Invalid()
    {
        // Labels of 50 characters stay under 63, the whole host does not (D1 IDN row, A17 final round).
        var label = "\u00fc" + new string('a', 49);
        Assert.False(EndpointGrammar.CheckHost(string.Join(".", Enumerable.Repeat(label, 6)), out _, out _));
        Assert.True(EndpointGrammar.CheckHost(string.Join(".", Enumerable.Repeat(label, 4)), out var ok, out _));
        Assert.StartsWith("xn--", ok);
    }

    [Fact]
    public void An_Unescaped_Zone_Id_Is_Reported_As_Such()
    {
        Assert.False(EndpointGrammar.CheckHost("[fe80::1%eth0]", out _, out var zoneId));
        Assert.True(zoneId);
        Assert.False(EndpointGrammar.CheckHost("h%41", out _, out zoneId));
        Assert.False(zoneId);
    }

    [Fact]
    public void Without_Uts46_A_Non_Ascii_Host_Is_Invalid_And_Says_Why()
    {
        // Globalization-invariant mode: IdnMapping punycodes without UTS #46 (measured), so fail closed.
        Assert.True(EndpointGrammar.Uts46Available); // the test image has ICU
        Assert.False(EndpointGrammar.CheckHost("b\u00fccher.de", out _, out _, uts46: false));
        Assert.True(EndpointGrammar.CheckHost("xn--bcher-kva.de", out var ascii, out _, uts46: false));
        Assert.Equal("xn--bcher-kva.de", ascii);

        var outcome = EndpointResolver.Resolve(new EndpointInput
        {
            Raw = "http://b\u00fccher.de:4317", Variable = "OTEL_EXPORTER_OTLP_ENDPOINT", Signal = "traces",
            Protocol = TelemetryOptions.ProtocolGrpc, Uts46 = false,
        });
        Assert.Equal(EndpointFailure.Invalid, outcome.Failure);
        Assert.Contains("needs ICU", outcome.Warnings.Single().Message);
        Assert.Equal("http://xn--bcher-kva.de:4317", EndpointResolver.Resolve(new EndpointInput
        {
            Raw = "http://b\u00fccher.de:4317", Variable = "V", Signal = "traces", Protocol = TelemetryOptions.ProtocolGrpc,
        }).Url);
    }

    [Theory]
    [InlineData("/a%2Fb", false)]
    [InlineData("/a%zz", true)]
    [InlineData("/a%2", true)]
    [InlineData("/a%", true)]
    [InlineData("/plain", false)]
    public void Invalid_Escapes_Are_Found(string path, bool invalid)
    {
        Assert.Equal(invalid, EndpointGrammar.HasInvalidEscape(path));
    }

    // ── rendering ───────────────────────────────────────────────────────

    [Theory]
    [InlineData("http://b\u00fccher.de", null, "http://xn--bcher-kva.de")]
    [InlineData("http://Collector:04318/x", null, "http://collector:4318/x")]
    [InlineData("http://collector:4318/%zz", "http/protobuf", "<unparseable URL>")]
    [InlineData("http://collector:4318/%zz", "grpc", "http://collector:4318/%zz")]
    [InlineData("http://collector:4317\u007f", null, "<unparseable URL>")]
    [InlineData("http://h\u2028:4317", null, "<unparseable URL>")]
    [InlineData("http://parityadmin:1234/parity-rest@collector?x", null, "http://")]
    [InlineData("http://127.1:4317", null, "<unparseable URL>")]
    [InlineData("ftp://", null, "<unparseable URL>")]
    [InlineData("collector:abc", null, "<unparseable URL>")]
    [InlineData("user:pw@collector:4317", null, "collector:4317")]
    public void Render_Matches_The_Resolver(string raw, string? protocol, string expected)
    {
        Assert.Equal(expected, EndpointGrammar.Render(raw, protocol));
    }

    [Theory]
    [InlineData("http://h:04318", "4318")]
    [InlineData("h:4318", "4318")]
    [InlineData("h", "")]
    [InlineData("http://h:4318\n", "4318")]
    [InlineData("http://h:43\n18", "")]
    [InlineData("http://h\\:4318", "")]
    [InlineData("http://h:99999", "")]
    public void Endpoint_Port_Is_Read_As_The_Resolver_Reads_It(string raw, string port)
    {
        Assert.Equal(port, EndpointGrammar.EndpointPort(raw));
    }

    // ── one value: EndpointResolver ─────────────────────────────────────

    private static EndpointOutcome Resolve(string raw, string protocol = TelemetryOptions.ProtocolGrpc, bool fromCode = false,
        bool perSignal = false, bool? insecure = null, bool insecureFromCode = false)
        => EndpointResolver.Resolve(new EndpointInput
        {
            Raw = raw,
            Variable = fromCode ? "TelemetryOptions.OtelCollectorEndpoint" : "OTEL_EXPORTER_OTLP_ENDPOINT",
            Signal = "traces",
            Protocol = protocol,
            FromCode = fromCode,
            PerSignal = perSignal,
            Insecure = insecure,
            InsecureFromCode = insecureFromCode,
            InsecureVariable = insecureFromCode ? "TelemetryOptions.Insecure" : "OTEL_EXPORTER_OTLP_INSECURE",
        });

    private static string Messages(EndpointOutcome outcome)
        => string.Join("\n", outcome.Warnings.Select(w => OtelHelperDiagnostics.Format(DiagnosticLevel.Warn, w.Message, w.Attributes)));

    [Theory]
    [InlineData("http://collector:4317\u007f")]
    [InlineData("unix:/x\u007f")]
    [InlineData("http://user:parity-secret\\pw@collector:4317")]
    [InlineData("https://h:4318/x\\y")]
    [InlineData("http:///collector:4317")]
    [InlineData("//collector:4317")]
    [InlineData("http://%63ollector:4317")]
    [InlineData("http://coll ector:4317")]
    [InlineData("http://collector:0")]
    [InlineData("http://collector:70000")]
    [InlineData("http://:4317")]
    [InlineData("collector:abc")]
    [InlineData("http://127.1:4317")]
    [InlineData("http://1.2.3.256:4317")]
    [InlineData("http://a{b:4317")]
    [InlineData("http://\ufffd.example:4317")]
    [InlineData("http://a\u200db.example:4317")]
    [InlineData("http://\uff11\uff12\uff17.\uff11:4317")]
    public void Unparseable_Env_Values_Warn_The_Constant_And_Fall_Back(string raw)
    {
        var outcome = Resolve(raw);
        Assert.Equal(EndpointFailure.Invalid, outcome.Failure);
        Assert.Null(outcome.Url);
        var text = Messages(outcome);
        Assert.Contains("OTEL_EXPORTER_OTLP_ENDPOINT", text);
        Assert.Contains("value=\"<unparseable URL>\"", text);
        Assert.DoesNotContain("unsupported", text);
        Assert.DoesNotContain("parity-secret", text);
    }

    [Theory]
    [InlineData("http://collector:4317\u007f", "control character")]
    [InlineData("unix:/x\u007f", "control character")]
    [InlineData("https://h:4318/x\\y", "backslash")]
    [InlineData("http://[fe80::1%eth0]:4317", "zone id")]
    [InlineData("http://\ufffd.example:4317", "invalid endpoint")]
    public void Unparseable_Code_Values_Fail_Naming_The_Option(string raw, string why)
    {
        var outcome = Resolve(raw, fromCode: true);
        Assert.Equal(EndpointFailure.Invalid, outcome.Failure);
        Assert.Empty(outcome.Warnings);
        Assert.StartsWith("TelemetryOptions.OtelCollectorEndpoint <unparseable URL> is not a valid OTLP endpoint", outcome.Error);
        Assert.Contains(why, outcome.Error);
    }

    [Theory]
    [InlineData("unix:/parity-path@parity-sock", "unix:")]
    [InlineData("COLLECTOR:/v1/traces", "collector:")]
    [InlineData("ftp://user:pw@collector:21/x?token=parity-q", "ftp://collector:21/x")]
    public void Unsupported_Schemes_Leave_The_Signal_Unconfigured_Rendered_Redacted(string raw, string shown)
    {
        var outcome = Resolve(raw);
        Assert.Equal(EndpointFailure.Unsupported, outcome.Failure);
        var text = Messages(outcome);
        Assert.Contains("unsupported endpoint scheme", text);
        Assert.Contains("value=" + shown, text);
        Assert.DoesNotContain("parity-", text);
        Assert.Contains(shown, Resolve(raw, fromCode: true).Error);
    }

    [Fact]
    public void An_Invalid_Http_Path_Escape_Is_Invalid_For_Http_Only()
    {
        Assert.Equal(EndpointFailure.Invalid, Resolve("http://collector:4318/%zz", TelemetryOptions.ProtocolHttpProtobuf, perSignal: true).Failure);
        Assert.Equal("http://collector:4318", Resolve("http://collector:4318/%zz").Url);
        // A valid escape is kept verbatim, never decoded.
        Assert.Equal("http://collector:4318/a%2Fb", Resolve("http://collector:4318/a%2Fb", TelemetryOptions.ProtocolHttpProtobuf, perSignal: true).Url);
    }

    [Theory]
    [InlineData("collector:4317", TelemetryOptions.ProtocolGrpc, null, "https://collector:4317")]
    [InlineData("collector", TelemetryOptions.ProtocolGrpc, null, "https://collector:4317")]
    [InlineData("collector", TelemetryOptions.ProtocolGrpc, true, "http://collector:4317")]
    [InlineData("collector", TelemetryOptions.ProtocolGrpc, false, "https://collector:4317")]
    [InlineData("collector", TelemetryOptions.ProtocolHttpProtobuf, null, "https://collector:4318/v1/traces")]
    [InlineData("[::1]:4317", TelemetryOptions.ProtocolGrpc, true, "http://[::1]:4317")]
    [InlineData("Collector:04318/otlp", TelemetryOptions.ProtocolHttpProtobuf, true, "http://collector:4318/otlp/v1/traces")]
    public void Schemeless_Values_Are_Rewritten(string raw, string protocol, bool? insecure, string url)
    {
        var outcome = Resolve(raw, protocol, insecure: insecure);
        Assert.Equal(url, outcome.Url);
        var text = Messages(outcome);
        if (protocol == TelemetryOptions.ProtocolHttpProtobuf)
        {
            Assert.Contains("schemeless endpoint is deprecated for http/protobuf", text);
        }
        else
        {
            Assert.Empty(outcome.Warnings);
        }
    }

    [Fact]
    public void A_Schemeless_Value_With_An_At_In_The_Path_Renders_The_Constant()
    {
        var outcome = Resolve("user:4318/parity-secret@collector", TelemetryOptions.ProtocolHttpProtobuf);
        var text = Messages(outcome);
        Assert.Contains("<schemeless endpoint>", text);
        Assert.Contains("misplaced credentials", text);
        Assert.DoesNotContain("parity-secret", text);
    }

    [Theory]
    [InlineData("http://collector", "http://collector:4317", "using=http://collector:4317")]
    [InlineData("https://b\u00fccher.de", "https://xn--bcher-kva.de:4317", "using=https://xn--bcher-kva.de:4317")]
    [InlineData("http://user/parity-secret@collector", "http://user:4317", "using=http://")]
    public void Portless_Grpc_Urls_Get_4317_With_A_Warning(string raw, string url, string usingText)
    {
        var outcome = Resolve(raw);
        Assert.Equal(url, outcome.Url);
        var text = Messages(outcome);
        Assert.Contains("set an explicit port", text);
        Assert.Contains(usingText, text);
        Assert.DoesNotContain("parity-secret", text);
    }

    [Theory]
    [InlineData("http://collector", false, "http://collector/v1/traces")]
    [InlineData("https://collector/otlp/", false, "https://collector/otlp/v1/traces")]
    [InlineData("https://collector/otlp/", true, "https://collector/otlp/")]
    [InlineData("https://collector:4318", true, "https://collector:4318")]
    public void Http_Urls_Keep_Their_Path_And_Default_Port(string raw, bool perSignal, string url)
    {
        var outcome = Resolve(raw, TelemetryOptions.ProtocolHttpProtobuf, perSignal: perSignal);
        Assert.Equal(url, outcome.Url);
        Assert.Empty(outcome.Warnings);
    }

    [Fact]
    public void Grpc_Urls_Drop_The_Path()
    {
        Assert.Equal("https://collector:4317", Resolve("https://collector:4317/otlp", perSignal: true).Url);
    }

    [Theory]
    [InlineData("http://collector:4317/parity-grpc-path", true)]
    [InlineData("collector:4317/parity-schemeless-path", true)]
    [InlineData("http://collector:4317//", true)]
    [InlineData("http://collector:4317/?x=1", false)]
    [InlineData("http://collector:4317/", false)]
    [InlineData("http://collector:4317", false)]
    [InlineData("collector:4317/", false)]
    public void A_Grpc_Path_Warns_Naming_The_Variable_Only(string raw, bool warned)
    {
        // C7b (A29): a bare "/" is no path; the warning carries the variable and nothing else.
        var outcome = Resolve(raw, insecure: true);
        var path = outcome.Warnings.Where(w => w.Message == EndpointResolver.MsgGrpcPathIgnored).ToArray();
        Assert.Equal(warned ? 1 : 0, path.Length);
        if (warned)
        {
            Assert.Equal(new[] { ("variable", "OTEL_EXPORTER_OTLP_ENDPOINT") }, path[0].Attributes);
            Assert.DoesNotContain("parity-", Messages(outcome));
        }
    }

    [Fact]
    public void An_Http_Path_Is_Not_Warned_And_The_Grpc_Path_Key_Ignores_The_Value()
    {
        Assert.DoesNotContain(Resolve("http://collector:4318/prefix", TelemetryOptions.ProtocolHttpProtobuf).Warnings,
            w => w.Message == EndpointResolver.MsgGrpcPathIgnored);
        // Once per variable: two different paths in the same variable share one key.
        Assert.Equal(Resolve("http://c:4317/a").Warnings.Single().OnceKey, Resolve("http://c:4317/b").Warnings.Single().OnceKey);
    }

    [Theory]
    [InlineData("http://u:parity-secret-pw@collector:4317", "http://collector:4317")]
    [InlineData("http://collector:4317/?token=parity-secret-q", "http://collector:4317")]
    [InlineData("http://collector:4317/?", "http://collector:4317")]
    [InlineData("http://collector:4317#", "http://collector:4317")]
    [InlineData("http://collector:4317#parity-secret-f", "http://collector:4317")]
    public void Userinfo_Query_And_Fragment_Are_Removed_With_A_Warning(string raw, string url)
    {
        var outcome = Resolve(raw);
        Assert.Equal(url, outcome.Url);
        var text = Messages(outcome);
        Assert.Contains("userinfo/query is not supported and was removed", text);
        Assert.DoesNotContain("parity-secret", text);
        Assert.Single(outcome.Warnings, w => w.Message == EndpointResolver.MsgUserinfo);
    }

    [Fact]
    public void Env_Failures_Use_Gos_Wording_And_Warn_Once_Per_Raw_Value()
    {
        // Fix round 1: "<why>; ignored" with the variable as an attribute (Go), keyed by the raw value.
        var a = Resolve("http://coll ector:4317");
        var b = Resolve("http://co ll:4317");
        Assert.Equal("invalid endpoint; ignored", a.Warnings.Single().Message);
        Assert.Equal(("variable", "OTEL_EXPORTER_OTLP_ENDPOINT"), a.Warnings.Single().Attributes[0]);
        // Both render "<unparseable URL>", yet they are different values: two keys.
        Assert.NotEqual(a.Warnings.Single().OnceKey, b.Warnings.Single().OnceKey);
        Assert.Equal(a.Warnings.Single().OnceKey, Resolve("http://coll ector:4317").Warnings.Single().OnceKey);
        Assert.DoesNotContain("coll ector", a.Warnings.Single().OnceKey);
    }

    [Fact]
    public void The_Userinfo_Using_Is_The_Url_The_Signal_Uses()
    {
        var outcome = Resolve("http://u:p@collector:4318/otlp", TelemetryOptions.ProtocolHttpProtobuf);
        Assert.Contains("using=http://collector:4318/otlp/v1/traces", Messages(outcome));
    }

    [Fact]
    public void An_At_In_The_Path_Warns_Naming_The_Variable_Only()
    {
        var outcome = Resolve("http://parityadmin:1234/parity-rest@collector?x", TelemetryOptions.ProtocolHttpProtobuf, perSignal: true);
        Assert.Equal("http://parityadmin:1234/parity-rest@collector", outcome.Url);
        var text = Messages(outcome);
        Assert.Contains("misplaced credentials", text);
        Assert.Contains("value=http://", text);
        Assert.DoesNotContain("parityadmin", text);
        Assert.DoesNotContain("1234", text);
        Assert.DoesNotContain("parity-rest", text);
        var at = Assert.Single(outcome.Warnings, w => w.Message == EndpointResolver.MsgAtInPath);
        Assert.Equal(new[] { ("variable", "OTEL_EXPORTER_OTLP_ENDPOINT") }, at.Attributes);
        // %40 counts as '@'.
        Assert.Contains(Resolve("http://h:4318/a%40b", TelemetryOptions.ProtocolHttpProtobuf).Warnings, w => w.Message == EndpointResolver.MsgAtInPath);
    }

    [Theory]
    [InlineData("https://collector:4317", true)]
    [InlineData("http://collector:4317", false)]
    public void An_Env_Insecure_Contradicting_The_Scheme_Warns_And_The_Scheme_Wins(string raw, bool insecure)
    {
        var outcome = Resolve(raw, insecure: insecure);
        Assert.Equal(raw, outcome.Url);
        var text = Messages(outcome);
        Assert.Contains("contradicts the endpoint scheme; the scheme wins", text);
        Assert.Contains("variable=OTEL_EXPORTER_OTLP_INSECURE", text);
    }

    [Fact]
    public void A_Code_Insecure_Contradicting_A_Code_Url_Fails_Only_When_Both_Are_Code()
    {
        var both = Resolve("https://collector:4317", fromCode: true, insecure: true, insecureFromCode: true);
        Assert.StartsWith("TelemetryOptions.Insecure = true contradicts the https:// scheme of TelemetryOptions.OtelCollectorEndpoint", both.Error);
        Assert.Null(both.Url);

        // Code insecure, env URL: warned, the scheme wins.
        var mixed = Resolve("https://collector:4317", insecure: true, insecureFromCode: true);
        Assert.Equal("https://collector:4317", mixed.Url);
        Assert.Contains("variable=TelemetryOptions.Insecure", Messages(mixed));

        // Agreeing values are silent.
        Assert.Empty(Resolve("http://collector:4317", insecure: true).Warnings);
    }
}

/// <summary>Task C7a in the resolver: environment and code inputs, diagnostics through the sink.</summary>
[Collection("EnvVarTests")]
[ResetGlobalPropagation]
public class EndpointResolutionTests : IDisposable
{
    private static readonly string[] Variables =
    {
        "OTEL_EXPORTER_OTLP_ENDPOINT", "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", "OTEL_EXPORTER_OTLP_METRICS_ENDPOINT", "OTEL_EXPORTER_OTLP_LOGS_ENDPOINT",
        "OTEL_EXPORTER_OTLP_INSECURE", "OTEL_EXPORTER_OTLP_TRACES_INSECURE", "OTEL_EXPORTER_OTLP_METRICS_INSECURE", "OTEL_EXPORTER_OTLP_LOGS_INSECURE",
        "OTEL_EXPORTER_OTLP_PROTOCOL", "OTEL_EXPORTER_OTLP_TRACES_PROTOCOL", "OTEL_EXPORTER_OTLP_METRICS_PROTOCOL", "OTEL_EXPORTER_OTLP_LOGS_PROTOCOL",
    };

    public EndpointResolutionTests() => Clear();

    public void Dispose() => Clear();

    private static void Clear()
    {
        foreach (var v in Variables)
        {
            Environment.SetEnvironmentVariable(v, null);
        }
    }

    private static void Set(string name, string? value) => Environment.SetEnvironmentVariable(name, value);

    private static (ResolvedTelemetry Resolved, TestDiagnostics Diagnostics) Resolve(Action<TelemetryOptions>? configure = null)
    {
        var diagnostics = new TestDiagnostics();
        var resolved = diagnostics.Resolve(o =>
        {
            TestDiagnostics.Identified(o);
            configure?.Invoke(o);
        });
        return (resolved, diagnostics);
    }

    [Fact]
    public void An_Unparseable_Per_Signal_Value_Falls_Back_To_The_Generic_One()
    {
        Set("OTEL_EXPORTER_OTLP_ENDPOINT", "http://collector:4317");
        Set("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", "http://user:parity-secret\\pw@collector:4317");
        var (resolved, diagnostics) = Resolve();
        Assert.Equal(("http://collector:4317", TelemetryOptions.ProtocolGrpc), resolved.OtlpTarget("traces"));
        var line = Assert.Single(diagnostics.Lines);
        Assert.Contains("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", line);
        Assert.Contains("<unparseable URL>", line);
        Assert.DoesNotContain("parity-secret", diagnostics.Text);
    }

    [Fact]
    public void An_Unsupported_Per_Signal_Scheme_Leaves_Only_That_Signal_Unconfigured()
    {
        Set("OTEL_EXPORTER_OTLP_ENDPOINT", "http://collector:4317");
        Set("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", "unix:/parity-path@parity-sock");
        var (resolved, diagnostics) = Resolve();
        Assert.Null(resolved.OtlpTarget("traces"));
        Assert.Equal(("http://collector:4317", TelemetryOptions.ProtocolGrpc), resolved.OtlpTarget("metrics"));
        Assert.Contains("value=unix:", diagnostics.Text);
        Assert.DoesNotContain("parity-", diagnostics.Text);
    }

    [Fact]
    public void A_Per_Signal_Env_Value_Is_Used_As_Written_And_Drives_The_Inference()
    {
        Set("OTEL_EXPORTER_OTLP_ENDPOINT", "http://collector:4317");
        Set("OTEL_EXPORTER_OTLP_LOGS_ENDPOINT", "http://logs:04318/custom");
        var (resolved, diagnostics) = Resolve();
        Assert.Equal(("http://logs:4318/custom", TelemetryOptions.ProtocolHttpProtobuf), resolved.OtlpTarget("logs"));
        Assert.Equal(("http://collector:4317", TelemetryOptions.ProtocolGrpc), resolved.OtlpTarget("traces"));
        Assert.Single(diagnostics.Lines, l => l.Contains("INFO endpoint port 4318 and no protocol set; using http/protobuf"));
    }

    [Fact]
    public void The_4318_Inference_Is_Logged_Once_For_Three_Signals()
    {
        Set("OTEL_EXPORTER_OTLP_ENDPOINT", "http://collector:" + new string('0', 3000) + "4318");
        var (resolved, diagnostics) = Resolve();
        Assert.Equal(("http://collector:4318/v1/metrics", TelemetryOptions.ProtocolHttpProtobuf), resolved.OtlpTarget("metrics"));
        Assert.Single(diagnostics.Lines, l => l.Contains("http/protobuf"));
        Assert.Equal(TelemetryOptions.ProtocolHttpProtobuf, resolved.ResolvedOtlpProtocol());
    }

    [Fact]
    public void Code_Code_Per_Signal_Env_Generic_Env_Is_The_Order()
    {
        Set("OTEL_EXPORTER_OTLP_ENDPOINT", "http://env-generic:4317");
        Set("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", "http://env-traces:4317");
        Set("OTEL_EXPORTER_OTLP_METRICS_ENDPOINT", "http://env-metrics:4317");
        var (resolved, _) = Resolve(o =>
        {
            o.TracesEndpoint = "http://code-traces:4317";
            o.OtelCollectorEndpoint = "http://code-generic:4317";
        });
        Assert.Equal("http://code-traces:4317", resolved.OtlpTarget("traces")!.Value.Endpoint);
        Assert.Equal("http://code-generic:4317", resolved.OtlpTarget("metrics")!.Value.Endpoint);
        Assert.Equal("TelemetryOptions.OtelCollectorEndpoint", resolved.SignalTargets["logs"].Variable);

        var (envOnly, _) = Resolve();
        Assert.Equal("http://env-traces:4317", envOnly.OtlpTarget("traces")!.Value.Endpoint);
        Assert.Equal("http://env-generic:4317", envOnly.OtlpTarget("logs")!.Value.Endpoint);
    }

    [Fact]
    public void A_Generic_Http_Query_Is_Stripped_Before_The_Signal_Path()
    {
        // The old C5 issue: "/v1/<signal>" was appended after the query.
        Set("OTEL_EXPORTER_OTLP_PROTOCOL", "http/protobuf");
        Set("OTEL_EXPORTER_OTLP_ENDPOINT", "http://collector:4318/otlp?token=parity-secret-q");
        var (resolved, diagnostics) = Resolve();
        Assert.Equal("http://collector:4318/otlp/v1/traces", resolved.OtlpTarget("traces")!.Value.Endpoint);
        var options = StaffOps.OtelHelper.Exporters.OtlpExporters.OptionsFor("traces", resolved, diagnostics.Sink)!;
        Assert.Equal(new Uri("http://collector:4318/otlp/v1/traces"), options.Endpoint);
        Assert.Equal(OpenTelemetry.Exporter.OtlpExportProtocol.HttpProtobuf, options.Protocol);
        Assert.DoesNotContain("parity-secret", diagnostics.Text);
    }

    [Theory]
    [InlineData("OTEL_EXPORTER_OTLP_ENDPOINT", "http://collector:4317/p", 1)]
    [InlineData("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", "http://collector:4317/v1/traces", 1)]
    public void The_Grpc_Path_Warning_Is_Written_Once_Per_Variable(string variable, string value, int lines)
    {
        Set("OTEL_EXPORTER_OTLP_ENDPOINT", "http://collector:4317");
        Set(variable, value);
        var (resolved, diagnostics) = Resolve();
        var warned = diagnostics.Lines.Where(l => l.Contains("endpoint path is ignored by the gRPC exporter")).ToArray();
        Assert.Equal(lines, warned.Length);
        Assert.Contains("variable=" + variable, warned[0]);
        Assert.DoesNotContain(value, warned[0]);
        Assert.DoesNotContain("value=", warned[0]);
        Assert.Equal("http://collector:4317", resolved.OtlpTarget("traces")!.Value.Endpoint);
    }

    [Theory]
    [InlineData("traces")]
    [InlineData("metrics")]
    [InlineData("logs")]
    public void Exporter_Options_Carry_Each_Signals_Target(string signal)
    {
        // C7b audit: the fixture pins per-signal HTTP for traces only; every signal is checked here.
        // A per-signal HTTP URL is used as written, also without a path (no /v1/<signal> added).
        Set("OTEL_EXPORTER_OTLP_PROTOCOL", "http/protobuf");
        Set("OTEL_EXPORTER_OTLP_ENDPOINT", "https://gw:4318/prefix/");
        Set(TelemetryResolver.SignalVariable(signal, "ENDPOINT"), "http://own-" + signal + ":4318");
        var (resolved, diagnostics) = Resolve();
        foreach (var each in ResolvedTelemetry.Signals)
        {
            var options = StaffOps.OtelHelper.Exporters.OtlpExporters.OptionsFor(each, resolved, diagnostics.Sink)!;
            Assert.Equal(OpenTelemetry.Exporter.OtlpExportProtocol.HttpProtobuf, options.Protocol);
            Assert.Equal(each == signal ? "http://own-" + signal + ":4318/" : "https://gw:4318/prefix/v1/" + each, options.Endpoint.AbsoluteUri);
        }
    }

    [Fact]
    public void A_Signal_Without_A_Target_Gets_No_Exporter_Options()
    {
        // Per-signal "configured": only the signal with an endpoint has OTLP options.
        Set("OTEL_EXPORTER_OTLP_METRICS_ENDPOINT", "http://m:4317");
        var (resolved, diagnostics) = Resolve();
        Assert.NotNull(StaffOps.OtelHelper.Exporters.OtlpExporters.OptionsFor("metrics", resolved, diagnostics.Sink));
        Assert.Null(StaffOps.OtelHelper.Exporters.OtlpExporters.OptionsFor("traces", resolved, diagnostics.Sink));
        Assert.Null(StaffOps.OtelHelper.Exporters.OtlpExporters.OptionsFor("logs", resolved, diagnostics.Sink));
        Assert.Equal(new[] { "metrics" }, resolved.SignalTargets.Keys.ToArray());
    }

    [Fact]
    public void A_Skipped_Per_Signal_Value_With_A_Path_Is_Not_Warned()
    {
        // An invalid per-signal value that fell back is not the chosen one (A29 row).
        Set("OTEL_EXPORTER_OTLP_ENDPOINT", "http://collector:4317");
        Set("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", "http://coll ector:4317/x");
        Assert.DoesNotContain(Resolve().Diagnostics.Lines, l => l.Contains("ignored by the gRPC exporter"));
    }

    [Fact]
    public void A_Code_Grpc_Path_Warns_And_Does_Not_Fail_Start()
    {
        Assert.Null(Fail(o => o.OtelCollectorEndpoint = "http://collector:4317/parity-code-path"));
        var (_, diagnostics) = Resolve(o => o.OtelCollectorEndpoint = "http://collector:4317/parity-code-path");
        var line = Assert.Single(diagnostics.Lines, l => l.Contains("ignored by the gRPC exporter"));
        Assert.Contains("variable=TelemetryOptions.OtelCollectorEndpoint", line);
        Assert.DoesNotContain("parity-code-path", diagnostics.Text);
    }

    [Fact]
    public void The_Userinfo_Warning_Is_Written_Once_Per_Variable_And_Value()
    {
        Set("OTEL_EXPORTER_OTLP_PROTOCOL", "http/protobuf");
        Set("OTEL_EXPORTER_OTLP_ENDPOINT", "http://u:parity-pw@collector:4318");
        var (resolved, diagnostics) = Resolve();
        Assert.Single(diagnostics.Lines, l => l.Contains("userinfo"));
        Assert.DoesNotContain("parity-pw", diagnostics.Text);
        Assert.Equal("http://collector:4318/v1/logs", resolved.OtlpTarget("logs")!.Value.Endpoint);
    }

    [Theory]
    [InlineData("OTEL_EXPORTER_OTLP_INSECURE", "yes")]
    [InlineData("OTEL_EXPORTER_OTLP_TRACES_INSECURE", "maybe")]
    public void An_Unparseable_Insecure_Warns_Without_An_Endpoint(string variable, string value)
    {
        Set(variable, value);
        var (resolved, diagnostics) = Resolve();
        Assert.Empty(resolved.SignalTargets);
        var line = Assert.Single(diagnostics.Lines);
        Assert.Contains("invalid " + variable + ": not a boolean", line);
    }

    [Fact]
    public void A_Shadowed_Insecure_Stays_Silent()
    {
        Set("OTEL_EXPORTER_OTLP_INSECURE", "yes");
        Set("OTEL_EXPORTER_OTLP_TRACES_INSECURE", "true");
        Set("OTEL_EXPORTER_OTLP_METRICS_INSECURE", "true");
        Set("OTEL_EXPORTER_OTLP_LOGS_INSECURE", "TRUE");
        Assert.Empty(Resolve().Diagnostics.Lines);

        Clear();
        Set("OTEL_EXPORTER_OTLP_INSECURE", "yes");
        Assert.Empty(Resolve(o => o.Insecure = false).Diagnostics.Lines);
    }

    [Fact]
    public void The_Per_Signal_Insecure_Beats_The_Generic_One_For_Schemeless_Values()
    {
        Set("OTEL_EXPORTER_OTLP_ENDPOINT", "collector:4317");
        Set("OTEL_EXPORTER_OTLP_INSECURE", "true");
        Set("OTEL_EXPORTER_OTLP_TRACES_INSECURE", " False ");
        var (resolved, diagnostics) = Resolve();
        Assert.Equal("https://collector:4317", resolved.OtlpTarget("traces")!.Value.Endpoint);
        Assert.Equal("http://collector:4317", resolved.OtlpTarget("metrics")!.Value.Endpoint);
        Assert.Empty(diagnostics.Lines);
    }

    [Fact]
    public void An_Unparseable_Insecure_Means_False()
    {
        Set("OTEL_EXPORTER_OTLP_ENDPOINT", "collector:4317");
        Set("OTEL_EXPORTER_OTLP_INSECURE", "1");
        Assert.Equal("https://collector:4317", Resolve().Resolved.OtlpTarget("traces")!.Value.Endpoint);
    }

    [Fact]
    public void Per_Signal_Protocol_Beats_The_Generic_One_And_Bad_Values_Fall_Through()
    {
        Set("OTEL_EXPORTER_OTLP_ENDPOINT", "http://collector:4317");
        Set("OTEL_EXPORTER_OTLP_PROTOCOL", "grpc");
        Set("OTEL_EXPORTER_OTLP_TRACES_PROTOCOL", "HTTP/PROTOBUF");
        Set("OTEL_EXPORTER_OTLP_METRICS_PROTOCOL", "http/json");
        Set("OTEL_EXPORTER_OTLP_LOGS_PROTOCOL", "bogus");
        var (resolved, diagnostics) = Resolve();
        Assert.Equal(("http://collector:4317/v1/traces", TelemetryOptions.ProtocolHttpProtobuf), resolved.OtlpTarget("traces"));
        Assert.Equal(TelemetryOptions.ProtocolGrpc, resolved.OtlpTarget("metrics")!.Value.Protocol);
        Assert.Equal(TelemetryOptions.ProtocolGrpc, resolved.OtlpTarget("logs")!.Value.Protocol);
        Assert.Contains("invalid OTEL_EXPORTER_OTLP_METRICS_PROTOCOL: http/json is not supported", diagnostics.Text);
        Assert.Contains("invalid OTEL_EXPORTER_OTLP_LOGS_PROTOCOL: unsupported OTLP protocol", diagnostics.Text);
    }

    [Theory]
    [InlineData("bogus", "unsupported OTLP protocol")]
    [InlineData("HTTP/JSON", "http/json is not supported")]
    public void A_Bad_Generic_Protocol_Is_Warned_Once_And_Grpc_Is_Used(string value, string text)
    {
        Set("OTEL_EXPORTER_OTLP_ENDPOINT", "http://collector:4317");
        Set("OTEL_EXPORTER_OTLP_PROTOCOL", value);
        var (resolved, diagnostics) = Resolve();
        Assert.Null(resolved.OtlpProtocol);
        Assert.Equal(TelemetryOptions.ProtocolGrpc, resolved.OtlpTarget("logs")!.Value.Protocol);
        Assert.Single(diagnostics.Lines, l => l.Contains("invalid OTEL_EXPORTER_OTLP_PROTOCOL: " + text));
        Assert.Single(diagnostics.Lines);
    }

    [Fact]
    public void A_Code_Protocol_Beats_Every_Variable_And_The_Inference()
    {
        Set("OTEL_EXPORTER_OTLP_TRACES_PROTOCOL", "http/protobuf");
        var (resolved, diagnostics) = Resolve(o => { o.OtelCollectorEndpoint = "http://collector:4318"; o.OtlpProtocol = "GRPC"; });
        Assert.Equal(("http://collector:4318", TelemetryOptions.ProtocolGrpc), resolved.OtlpTarget("traces"));
        Assert.DoesNotContain("4318 and no protocol", diagnostics.Text);
    }

    [Fact]
    public void An_Idn_Host_Reaches_The_Exporter_Punycoded()
    {
        var (resolved, diagnostics) = Resolve(o => o.OtelCollectorEndpoint = "http://b\u00fccher.de");
        Assert.Equal("http://xn--bcher-kva.de:4317", resolved.OtlpTarget("traces")!.Value.Endpoint);
        Assert.Equal("http://xn--bcher-kva.de:4317", resolved.OtelCollectorEndpoint);
        Assert.Single(diagnostics.Lines, l => l.Contains("explicit port") && l.Contains("xn--bcher-kva.de"));
    }

    [Fact]
    public void Generic_Endpoint_Reports_Empty_When_The_Env_Value_Is_Unusable()
    {
        Set("OTEL_EXPORTER_OTLP_ENDPOINT", "ftp://collector:21");
        var (resolved, diagnostics) = Resolve();
        Assert.Equal("", resolved.OtelCollectorEndpoint);
        Assert.Empty(resolved.SignalTargets);
        Assert.Contains("unsupported endpoint scheme", diagnostics.Text);

        Set("OTEL_EXPORTER_OTLP_ENDPOINT", "http://:4317");
        Assert.Equal("", Resolve().Resolved.OtelCollectorEndpoint);
    }

    // ── code values: the validator ─────────────────────────────────────

    private static string? Fail(Action<TelemetryOptions> configure)
    {
        var options = new TelemetryOptions();
        TestDiagnostics.Identified(options);
        configure(options);
        var result = new TelemetryOptionsValidator().Validate(null, options);
        return result.Failed ? result.FailureMessage : null;
    }

    [Theory]
    [InlineData("http://[fe80::1%eth0]:4317", "zone id")]
    [InlineData("https://h:4318/x\\y", "backslash")]
    [InlineData("http://\ufffd.example:4317", "invalid endpoint")]
    [InlineData("unix:/x\u007f", "control character")]
    [InlineData("http://collector:0", "invalid endpoint")]
    public void Invalid_Code_Endpoints_Fail_At_Start(string endpoint, string why)
    {
        var message = Fail(o => o.OtelCollectorEndpoint = endpoint);
        Assert.NotNull(message);
        Assert.Contains("TelemetryOptions.OtelCollectorEndpoint", message);
        Assert.Contains(why, message);
    }

    [Fact]
    public void A_Code_Http_Path_Escape_Fails_Only_For_Http_Protobuf()
    {
        var message = Fail(o => { o.TracesEndpoint = "http://collector:4318/%zz"; o.OtlpProtocol = "http/protobuf"; });
        Assert.Contains("TelemetryOptions.TracesEndpoint", message);
        Assert.Contains("invalid percent-escape", message);
        Assert.Null(Fail(o => o.TracesEndpoint = "http://collector:4317/%zz"));

        // The per-signal variable decides the protocol of the code value too.
        Set("OTEL_EXPORTER_OTLP_TRACES_PROTOCOL", "http/protobuf");
        Assert.Contains("TelemetryOptions.TracesEndpoint", Fail(o => o.TracesEndpoint = "http://collector:4317/%zz"));
    }

    [Fact]
    public void A_Code_Insecure_Contradicting_A_Code_Url_Fails()
    {
        Assert.Contains("TelemetryOptions.Insecure = true contradicts the https:// scheme", Fail(o => { o.OtelCollectorEndpoint = "https://collector:4317"; o.Insecure = true; }));
        Assert.Contains("TelemetryOptions.Insecure = false contradicts the http:// scheme of TelemetryOptions.LogsEndpoint",
            Fail(o => { o.LogsEndpoint = "http://collector:4317"; o.Insecure = false; }));
        Assert.Null(Fail(o => { o.OtelCollectorEndpoint = "collector:4317"; o.Insecure = true; }));
    }

    [Fact]
    public void The_Same_Error_Is_Listed_Once_And_A_Shadowed_Generic_Value_Is_Not_Checked()
    {
        var message = Fail(o => o.OtelCollectorEndpoint = "http://coll ector:4317")!;
        Assert.Equal(message.IndexOf("TelemetryOptions.OtelCollectorEndpoint", StringComparison.Ordinal),
            message.LastIndexOf("TelemetryOptions.OtelCollectorEndpoint", StringComparison.Ordinal));

        Assert.Null(Fail(o =>
        {
            o.OtelCollectorEndpoint = "http://coll ector:4317";
            o.TracesEndpoint = o.MetricsEndpoint = o.LogsEndpoint = "http://collector:4317";
        }));
    }

    [Theory]
    [InlineData("collector:4318")]
    [InlineData("user:parity-secret@collector:4317")]
    [InlineData("http://u:parity-secret@collector:4317/?q=1")]
    public void Schemeless_And_Credential_Bearing_Code_Values_Are_Accepted(string endpoint)
    {
        // Contract D1: schemeless is legal in code (per-signal too); userinfo and query are removed with a warning.
        Assert.Null(Fail(o => o.TracesEndpoint = endpoint));
        var (resolved, diagnostics) = Resolve(o => o.TracesEndpoint = endpoint);
        Assert.NotNull(resolved.OtlpTarget("traces"));
        Assert.DoesNotContain("parity-secret", diagnostics.Text);
    }

    [Fact]
    public void Code_Warnings_Reach_The_Sink_At_Start()
    {
        var services = new ServiceCollection();
        var diagnostics = new TestDiagnostics();
        services.AddSingleton<IOtelHelperDiagnostics>(diagnostics.Sink);
        services.AddOtelHelper(o =>
        {
            TestDiagnostics.Identified(o);
            o.OtelCollectorEndpoint = "http://u:parity-secret@collector:4318";
            o.OtlpProtocol = "http/protobuf";
            o.PrometheusMetricsPort = 0;
        });
        using var provider = services.BuildServiceProvider();
        _ = provider.GetRequiredService<IOptions<TelemetryOptions>>().Value;
        var resolved = provider.GetRequiredService<ResolvedTelemetry>();
        Assert.Equal("http://collector:4318/v1/traces", resolved.OtlpTarget("traces")!.Value.Endpoint);
        Assert.Single(diagnostics.Lines, l => l.Contains("userinfo"));
        Assert.DoesNotContain("parity-secret", diagnostics.Text);
    }
}
