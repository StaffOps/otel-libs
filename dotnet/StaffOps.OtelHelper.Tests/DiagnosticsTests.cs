using System.Diagnostics.Tracing;
using StaffOps.OtelHelper.Diagnostics;
using Xunit;

namespace StaffOps.OtelHelper.Tests;

/// <summary>
/// The diagnostics sink and its text rules (design D-NET5, requirement DN3, contract D1
/// rows "Echoed values are truncated", "Opaque-scheme rendering", "Rendering when the path
/// or query holds @"), and the SDK EventSource forwarding.
/// </summary>
[Collection("EnvVarTests")]
public class DiagnosticsTests
{
    // ── record format ───────────────────────────────────────────────────

    /// <summary>DN3: one channel, one stderr line per record, prefixed <c>otelhelper:</c>.</summary>
    [Fact]
    public void Record_Is_One_Prefixed_Line_With_Level_And_Attributes()
    {
        var diagnostics = new TestDiagnostics();
        diagnostics.Sink.Report(DiagnosticLevel.Warn, "invalid X", ("variable", "OTEL_X"), ("value", "a b"));
        diagnostics.Sink.Report(DiagnosticLevel.Info, "info line");
        diagnostics.Sink.Report(DiagnosticLevel.Error, "error line");

        Assert.Equal(
            new[]
            {
                "otelhelper: WARN invalid X variable=OTEL_X value=\"a b\"",
                "otelhelper: INFO info line",
                "otelhelper: ERROR error line",
            },
            diagnostics.Lines);
    }

    [Theory]
    [InlineData("plain", "plain")]
    [InlineData("", "\"\"")]
    [InlineData("a=b", "\"a=b\"")]
    [InlineData("q\"uote", "\"q\\\"uote\"")]
    [InlineData("back\\slash", "\"back\\\\slash\"")]
    [InlineData("line\nbreak", "\"line\\nbreak\"")]
    [InlineData("cr\rx", "\"cr\\rx\"")]
    [InlineData("tab\tx", "\"tab\\tx\"")]
    [InlineData("del\u007fx", "\"del\\u007fx\"")]
    [InlineData("c1\u0085x", "\"c1\\u0085x\"")]
    [InlineData("nul\u0000x", "\"nul\\u0000x\"")]
    [InlineData("ls\u2028x", "\"ls\\u2028x\"")]
    [InlineData("ps\u2029x", "\"ps\\u2029x\"")]
    [InlineData("rlo\u202ex", "\"rlo\\u202ex\"")]
    [InlineData("lri\u2066x", "\"lri\\u2066x\"")]
    [InlineData("lrm\u200ex", "\"lrm\\u200ex\"")]
    [InlineData("alm\u061cx", "\"alm\\u061cx\"")]
    public void Values_Are_Quoted_And_Escaped_So_They_Cannot_Forge_A_Line(string value, string expected)
    {
        Assert.Equal(expected, DiagnosticText.FormatValue(value));
    }

    [Fact]
    public void A_Message_Cannot_Forge_A_Second_Record()
    {
        var diagnostics = new TestDiagnostics();
        diagnostics.Sink.Report(DiagnosticLevel.Warn, "first\notelhelper: ERROR forged\u2028x", ("value", "v\r\notelhelper: ERROR forged"));
        var line = Assert.Single(diagnostics.Lines);
        Assert.StartsWith("otelhelper: WARN first\\u000aotelhelper: ERROR forged\\u2028x", line);
        Assert.Contains("value=\"v\\r\\notelhelper: ERROR forged\"", line);
    }

    [Fact]
    public void Echoed_Values_Are_Cut_At_64_Code_Points()
    {
        Assert.Equal(new string('x', 64), DiagnosticText.Truncate(new string('x', 64)));
        Assert.Equal(new string('x', 64) + "…", DiagnosticText.Truncate(new string('x', 5000)));

        // By code point: a surrogate pair is never split.
        var emoji = string.Concat(Enumerable.Repeat("\U0001F600", 70));
        var cut = DiagnosticText.Truncate(emoji);
        Assert.Equal(64 * 2 + 1, cut.Length);
        Assert.EndsWith("…", cut);
        Assert.Equal(string.Concat(Enumerable.Repeat("\U0001F600", 33)), DiagnosticText.Truncate(string.Concat(Enumerable.Repeat("\U0001F600", 33))));
    }

    [Fact]
    public void Invalid_Attribute_Key_Is_A_Programming_Error()
    {
        var diagnostics = new TestDiagnostics();
        Assert.Throws<ArgumentException>(() => diagnostics.Sink.Report(DiagnosticLevel.Warn, "m", ("bad key", "v")));
        Assert.Throws<ArgumentException>(() => diagnostics.Sink.Report(DiagnosticLevel.Warn, "m", ("", "v")));
    }

    [Fact]
    public void Once_Is_Per_Sink()
    {
        var first = new TestDiagnostics();
        var second = new TestDiagnostics();
        Assert.True(first.Sink.ReportOnce("k", DiagnosticLevel.Warn, "once"));
        Assert.False(first.Sink.ReportOnce("k", DiagnosticLevel.Warn, "once"));
        Assert.True(second.Sink.ReportOnce("k", DiagnosticLevel.Warn, "once"));
        Assert.Single(first.Lines);
        Assert.Single(second.Lines);
    }

    [Fact]
    public void A_Broken_Writer_Or_Observer_Never_Throws()
    {
        var closed = new StringWriter();
        closed.Dispose();
        var sink = new OtelHelperDiagnostics(() => closed) { Observer = _ => throw new InvalidOperationException("observer") };
        sink.Report(DiagnosticLevel.Warn, "still fine");
        var throwing = new OtelHelperDiagnostics(() => throw new IOException("stderr closed"));
        throwing.Report(DiagnosticLevel.Warn, "still fine");
    }

    [Fact]
    public void Observer_Sees_Level_Origin_And_Line()
    {
        var records = new List<DiagnosticRecord>();
        var sink = new OtelHelperDiagnostics(() => TextWriter.Null) { Observer = records.Add };
        sink.Report(DiagnosticLevel.Info, "helper");
        sink.ReportSdk(DiagnosticLevel.Error, "OpenTelemetry-Sdk", "sdk text");

        Assert.Equal(DiagnosticOrigin.Helper, records[0].Origin);
        Assert.Equal(DiagnosticLevel.Info, records[0].Level);
        Assert.Equal(DiagnosticOrigin.Sdk, records[1].Origin);
        Assert.Equal("otelhelper: ERROR sdk OpenTelemetry-Sdk: sdk text", records[1].Line);
    }

    [Fact]
    public void Fallback_Writes_To_The_Current_Standard_Error()
    {
        Assert.NotNull(OtelHelperDiagnostics.Fallback);
        Assert.Same(OtelHelperDiagnostics.Fallback, OtelHelperDiagnostics.Fallback);
    }

    // ── URL rendering ───────────────────────────────────────────────────

    [Theory]
    [InlineData("http://user:pw@collector:4317/path?token=x#frag", "http://collector:4317/path")]
    [InlineData("https://collector", "https://collector")]
    [InlineData("  http://collector:4318/otlp  ", "http://collector:4318/otlp")]
    [InlineData("collector:4317", "collector:4317")]
    [InlineData("[::1]:4317", "[::1]:4317")]
    [InlineData("ftp://user:pw@collector:21/x?token=secret", "ftp://collector:21/x")]
    [InlineData("http://user:pw@/x", "<unparseable URL>")]
    [InlineData("unix:/var/run/secret.sock", "unix:")]
    [InlineData("UNIX:/x", "unix:")]
    [InlineData("http://admin:1234/rest@collector?x", "http://")]
    [InlineData("http://collector:4317/a%40b", "http://")]
    [InlineData("user:4318/secret@collector", "<schemeless endpoint>")]
    [InlineData("http://:4317", "<unparseable URL>")]
    [InlineData("", "<unparseable URL>")]
    [InlineData("http://h\\x", "<unparseable URL>")]
    [InlineData("http://h:43\n17/", "<unparseable URL>")]
    [InlineData("/just/a/path", "<unparseable URL>")]
    public void Endpoints_Render_Without_Credentials(string raw, string expected)
    {
        Assert.Equal(expected, DiagnosticText.RenderUrl(raw));
    }

    // ── free-text redaction (forwarded SDK text) ────────────────────────

    [Theory]
    [InlineData("export to http://u:p@collector:4318/v1/traces?sig=abc failed", "export to http://collector:4318/v1/traces failed")]
    [InlineData("dns:///user:pw@host:4317 refused", "dns:///host:4317 refused")]
    [InlineData("connect u:secretpw@collector:4317", "connect collector:4317")]
    [InlineData("Authorization: Bearer abc.def-ghi", "Authorization: <redacted> <redacted>")]
    [InlineData("token Bearer abc.def-ghi", "token Bearer <redacted>")]
    [InlineData("auth Basic dXNlcjpwYXNzd29yZA== sent", "auth Basic <redacted> sent")]
    [InlineData("api_key=parity-secret&x=1", "api_key=<redacted>&x=1")]
    [InlineData("{\"x-api-token\":\"parity-secret\"}", "{\"x-api-token\":<redacted>}")]
    [InlineData("basic auth failed", "basic auth failed")]
    public void Sdk_Text_Is_Redacted(string text, string expected)
    {
        Assert.Equal(expected, DiagnosticText.RedactText(text));
    }

    [Fact]
    public void Sdk_Text_Is_Cut_At_The_Scan_Limit_And_Escaped()
    {
        var diagnostics = new TestDiagnostics();
        diagnostics.Sink.ReportSdk(DiagnosticLevel.Warn, "OpenTelemetry-Exporter", new string('a', 10_000) + "\nforged");
        var line = Assert.Single(diagnostics.Lines);
        Assert.Equal("otelhelper: WARN sdk OpenTelemetry-Exporter: ".Length + DiagnosticText.ScanLimit, line.Length);
        Assert.Equal("x\\u000ay\\u2028", DiagnosticText.EscapeInline("x\ny\u2028"));
        Assert.Equal("clean", DiagnosticText.EscapeInline("clean"));
    }

    // ── SDK EventSource forwarding ──────────────────────────────────────

    [Fact]
    public void Forwarder_Sends_OpenTelemetry_Warnings_And_Errors_Redacted()
    {
        var diagnostics = new TestDiagnostics();
        using (new SdkDiagnosticsForwarder(diagnostics.Sink))
        {
            TestOtelEventSource.Log.ExportFailed("http://u:parity-secret@collector:4318/v1/traces", "refused");
            TestOtelEventSource.Log.Info("not forwarded");
            TestOtelEventSource.Log.Boom("x");
            TestOtelEventSource.Log.Broken("kept");
            TestOtelEventSource.Log.NoMessage("a", 7);
            OtherEventSource.Log.Warn("not an OpenTelemetry source");
        }

        TestOtelEventSource.Log.ExportFailed("after", "dispose");

        var mine = diagnostics.Lines.Where(l => l.Contains("OpenTelemetry-StaffOps-Test")).ToArray();
        Assert.Equal(
            new[]
            {
                "otelhelper: WARN sdk OpenTelemetry-StaffOps-Test: export to http://collector:4318/v1/traces failed: refused",
                "otelhelper: ERROR sdk OpenTelemetry-StaffOps-Test: boom x",
                "otelhelper: WARN sdk OpenTelemetry-StaffOps-Test: broken {5} template kept",
                "otelhelper: ERROR sdk OpenTelemetry-StaffOps-Test: a 7",
            },
            mine);
        Assert.DoesNotContain(diagnostics.Lines, l => l.Contains("parity-secret") || l.Contains("not forwarded") || l.Contains("Other-StaffOps-Test"));
    }

    [Fact]
    public void Forwarder_Is_Off_With_ForwardSdkDiagnostics_False()
    {
        var on = new TestDiagnostics();
        var off = new TestDiagnostics();
        using var withForwarding = TestPipelines.Build(o => { o.ServiceName = "fwd-on"; o.PrometheusMetricsPort = 0; }, on);
        using var withoutForwarding = TestPipelines.Build(o =>
        {
            o.ServiceName = "fwd-off";
            o.PrometheusMetricsPort = 0;
            o.ForwardSdkDiagnostics = false;
        }, off);

        TestOtelEventSource.Log.ExportFailed("fwd", "probe");

        Assert.Contains(on.Lines, l => l.Contains("export to fwd failed: probe"));
        Assert.DoesNotContain(off.Lines, l => l.Contains("export to fwd failed: probe"));
    }

    // ── SDK text sanitizer (no env) ─────────────────────────────────────

    [Theory]
    [InlineData("OpenTelemetry-Exporter-Prometheus-HttpListener", "Canceled to export metrics: 'System.Threading.Tasks.TaskCanceledException: x'", true)]
    [InlineData("OpenTelemetry-Exporter-Prometheus-HttpListener", "Canceled to export metrics: 'System.Exception: x'", false)]
    [InlineData("OpenTelemetry-Exporter-OpenTelemetryProtocol", "Configuration key 'OTEL_EXPORTER_OTLP_TIMEOUT' has an invalid value: '+1'", false)]
    [InlineData("OpenTelemetry-Exporter-OpenTelemetryProtocol", "Configuration key 'OTEL_EXPORTER_OTLP_PROTOCOL' has an invalid value: 'x'", true)]
    [InlineData("OpenTelemetry-Sdk", "Configuration key 'OTEL_SDK_DISABLED' has an invalid value: 'maybe'", false)]
    [InlineData("OpenTelemetry-Exporter-OpenTelemetryProtocol", "HTTP request to http://c failed", false)]
    public void Known_Noise_Is_Not_Forwarded(string source, string text, bool noise)
    {
        Assert.Equal(noise, SdkTextSanitizer.IsNoise(source, text));
    }

    [Fact]
    public void Sdk_Parse_Warnings_Are_Written_Once()
    {
        Assert.NotNull(SdkTextSanitizer.OnceKey("OpenTelemetry-Sdk", "Configuration key 'OTEL_SDK_DISABLED' has an invalid value: 'maybe'"));
        Assert.Null(SdkTextSanitizer.OnceKey("OpenTelemetry-Sdk", "Export failed"));
        var diagnostics = new TestDiagnostics();
        diagnostics.Sink.ReportSdk(DiagnosticLevel.Warn, "OpenTelemetry-Sdk", "same", "k");
        diagnostics.Sink.ReportSdk(DiagnosticLevel.Warn, "OpenTelemetry-Sdk", "same", "k");
        Assert.Single(diagnostics.Lines);
    }

    [Fact]
    public void Response_Bodies_Are_Dropped_And_Code_Secrets_Masked()
    {
        var text = "HTTP request to http://c:4318/v1/traces failed. Response: {\"echo\":\"tok-parity-12345\"}. Exception: 400";
        var sanitized = SdkTextSanitizer.Sanitize(text, new[] { "never-present" });
        // Unstructured text: everything after "Response: " goes (fail closed).
        Assert.Equal("HTTP request to http://c:4318/v1/traces failed. Response: <response body omitted>", sanitized);
        Assert.Equal("a <redacted> b", SdkTextSanitizer.Sanitize("a code-pass-1234 b", new[] { "code-pass-1234" }));
    }

    [Theory]
    [InlineData("http://user:parity-secret#pw@collector:4317", new[] { "user:parity-secret#pw", "user", "parity-secret", "pw" })]
    [InlineData("https://collector:4317/x?token=abcd&sig=efgh", new[] { "abcd", "efgh" })]
    [InlineData("collector:4317", new string[0])]
    [InlineData(null, new string[0])]
    public void Endpoint_Secrets_Are_Userinfo_Parts_And_Query_Values(string? endpoint, string[] expected)
    {
        Assert.Equal(expected, SdkTextSanitizer.EndpointSecrets(endpoint));
    }

    [Fact]
    public void Masked_Forms_Cover_Parts_Encodings_And_Overlaps()
    {
        var forms = SdkTextSanitizer.Forms(new[] { "Bearer tok/en+1 x", "abc" });
        Assert.Contains("Bearer tok/en+1 x", forms);
        Assert.Contains("tok/en+1", forms);
        Assert.Contains("tok%2Fen%2B1", forms);
        Assert.DoesNotContain("Bearer", forms);
        Assert.DoesNotContain("abc", forms);
        Assert.Equal(forms.OrderByDescending(f => f.Length).Select(f => f.Length), forms.Select(f => f.Length));

        // Overlapping values never leave a tail; adjacent masks collapse into one.
        Assert.Equal("x <redacted> y", SdkTextSanitizer.MaskLiterals("x team-secret-token y", SdkTextSanitizer.Forms(new[] { "team", "secret-token", "team-secret" })));
        Assert.Equal("nothing", SdkTextSanitizer.MaskLiterals("nothing", Array.Empty<string>()));
    }
}
