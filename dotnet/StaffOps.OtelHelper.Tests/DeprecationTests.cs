using Microsoft.Extensions.DependencyInjection;
using Xunit;

namespace StaffOps.OtelHelper.Tests;

/// <summary>
/// Task C16, the deprecations sweep (contract design D8, R8, DN3): each deprecated variable that is
/// set is warned once per diagnostics sink, naming its replacement and never its value, also when
/// a standard variable or code wins; <c>OTEL_HELPER_EXTRA_INSTRUMENTATION</c> is warned as having
/// no effect. Written against the contract wording shared with Go and Node.js.
/// </summary>
[Collection("EnvVarTests")]
public class DeprecationTests : IDisposable
{
    // A value that must never reach a diagnostic (R8).
    private const string Sentinel = "c16-sentinel-value";

    private static readonly string[] TouchedEnvVars =
    {
        "SERVICE_NAME", "ENVIRONMENT", "OTEL_HELPER_SAMPLE_RATIO", "OTEL_HELPER_DISABLED_SIGNALS",
        "OTEL_HELPER_EXTRA_INSTRUMENTATION", "OTEL_SERVICE_NAME", "OTEL_RESOURCE_ATTRIBUTES",
        "OTEL_TRACES_EXPORTER", "OTEL_TRACES_SAMPLER",
    };

    public static readonly TheoryData<string, string> ExpectedLines = new()
    {
        {
            "SERVICE_NAME",
            "otelhelper: WARN SERVICE_NAME is deprecated and will be removed in 1.0; use OTEL_SERVICE_NAME variable=SERVICE_NAME replacement=OTEL_SERVICE_NAME"
        },
        {
            "ENVIRONMENT",
            "otelhelper: WARN ENVIRONMENT is deprecated and will be removed in 1.0; use OTEL_RESOURCE_ATTRIBUTES (deployment.environment.name=<value>) variable=ENVIRONMENT replacement=\"OTEL_RESOURCE_ATTRIBUTES (deployment.environment.name=<value>)\""
        },
        {
            "OTEL_HELPER_SAMPLE_RATIO",
            "otelhelper: WARN OTEL_HELPER_SAMPLE_RATIO is deprecated and will be removed in 1.0; use OTEL_TRACES_SAMPLER=parentbased_traceidratio with OTEL_TRACES_SAMPLER_ARG variable=OTEL_HELPER_SAMPLE_RATIO replacement=\"OTEL_TRACES_SAMPLER=parentbased_traceidratio with OTEL_TRACES_SAMPLER_ARG\""
        },
        {
            "OTEL_HELPER_DISABLED_SIGNALS",
            "otelhelper: WARN OTEL_HELPER_DISABLED_SIGNALS is deprecated and will be removed in 1.0; use OTEL_TRACES_EXPORTER/OTEL_METRICS_EXPORTER/OTEL_LOGS_EXPORTER=none variable=OTEL_HELPER_DISABLED_SIGNALS replacement=\"OTEL_TRACES_EXPORTER/OTEL_METRICS_EXPORTER/OTEL_LOGS_EXPORTER=none\""
        },
        {
            "OTEL_HELPER_EXTRA_INSTRUMENTATION",
            "otelhelper: WARN OTEL_HELPER_EXTRA_INSTRUMENTATION has no effect in .NET and is ignored; add the opt-in packages (StaffOps.OtelHelper.Sql, .AWS, .Redis) instead variable=OTEL_HELPER_EXTRA_INSTRUMENTATION"
        },
    };

    public DeprecationTests() => ClearEnv();

    public void Dispose() => ClearEnv();

    private static void ClearEnv()
    {
        foreach (var v in TouchedEnvVars)
        {
            Environment.SetEnvironmentVariable(v, null);
        }
    }

    private static void Set(string name, string? value) => Environment.SetEnvironmentVariable(name, value);

    [Theory]
    [MemberData(nameof(ExpectedLines))]
    public void Each_Variable_Warns_Once_Verbatim_Without_Its_Value(string variable, string expected)
    {
        // A sentinel value proves the D8 line never prints it. (The variable's own parser may
        // still warn about an invalid value and echo it: that is its contract row, not D8.)
        Set(variable, Sentinel);
        var diagnostics = new TestDiagnostics();
        diagnostics.Resolve(TestDiagnostics.Identified);
        diagnostics.Resolve(TestDiagnostics.Identified);
        var line = Assert.Single(diagnostics.Lines, l => l.Contains(variable + " is deprecated", StringComparison.Ordinal) || l.Contains(variable + " has no effect", StringComparison.Ordinal));
        Assert.Equal(expected, line);
        Assert.DoesNotContain(Sentinel, line);
    }

    [Fact]
    public void All_Set_Together_Give_One_Line_Each_In_A_Fixed_Order()
    {
        Set("SERVICE_NAME", "svc-legacy");
        Set("ENVIRONMENT", "DEV");
        Set("OTEL_HELPER_SAMPLE_RATIO", "0.5");
        Set("OTEL_HELPER_DISABLED_SIGNALS", "logs");
        Set("OTEL_HELPER_EXTRA_INSTRUMENTATION", "SQL");
        var diagnostics = new TestDiagnostics();
        diagnostics.Resolve(TestDiagnostics.Identified);
        diagnostics.Resolve(TestDiagnostics.Identified);
        Assert.Equal(ExpectedLines.Select(row => (string)row[1]).ToArray(), diagnostics.Lines);
    }

    [Theory]
    [InlineData("SERVICE_NAME")]
    [InlineData("ENVIRONMENT")]
    [InlineData("OTEL_HELPER_SAMPLE_RATIO")]
    [InlineData("OTEL_HELPER_DISABLED_SIGNALS")]
    [InlineData("OTEL_HELPER_EXTRA_INSTRUMENTATION")]
    public void Unset_Or_Blank_Is_Silent(string variable)
    {
        foreach (var value in new string?[] { null, "", " \t\r\n" })
        {
            Set(variable, value);
            var diagnostics = new TestDiagnostics();
            diagnostics.Resolve(TestDiagnostics.Identified);
            Assert.Empty(diagnostics.Lines);
        }
    }

    [Fact]
    public void Service_Name_Warns_While_The_Standard_Variable_Wins()
    {
        Set("OTEL_SERVICE_NAME", "svc-std");
        Set("SERVICE_NAME", "svc-legacy");
        var diagnostics = new TestDiagnostics();
        Assert.Equal("svc-std", diagnostics.Resolve().ServiceName);
        Assert.Contains(diagnostics.Lines, l => l.StartsWith("otelhelper: WARN SERVICE_NAME is deprecated", StringComparison.Ordinal));
    }

    [Fact]
    public void Environment_Warns_While_Resource_Attributes_Win()
    {
        Set("OTEL_RESOURCE_ATTRIBUTES", "deployment.environment.name=PRD");
        Set("ENVIRONMENT", "DEV");
        var diagnostics = new TestDiagnostics();
        Assert.Equal("PRD", diagnostics.Resolve(o => o.ServiceName = "svc").Environment);
        Assert.Contains(diagnostics.Lines, l => l.StartsWith("otelhelper: WARN ENVIRONMENT is deprecated", StringComparison.Ordinal));
    }

    [Fact]
    public void Disabled_Signals_Warns_While_The_Standard_Exporter_Variable_Wins()
    {
        // Parity case deprecation.disabled_signals_standard_var_set_still_warns.
        Set("OTEL_HELPER_DISABLED_SIGNALS", "traces");
        Set("OTEL_TRACES_EXPORTER", "otlp");
        var diagnostics = new TestDiagnostics();
        var resolved = diagnostics.Resolve(TestDiagnostics.Identified);
        Assert.Equal("", resolved.DisabledSignals);
        Assert.Contains(diagnostics.Lines, l => l.StartsWith("otelhelper: WARN OTEL_HELPER_DISABLED_SIGNALS is deprecated", StringComparison.Ordinal));
    }

    [Fact]
    public void Sample_Ratio_Warns_While_The_Standard_Sampler_Wins()
    {
        Set("OTEL_TRACES_SAMPLER", "always_off");
        Set("OTEL_HELPER_SAMPLE_RATIO", "0.5");
        var diagnostics = new TestDiagnostics();
        Assert.Equal("always_off", diagnostics.Resolve(TestDiagnostics.Identified).SamplerName);
        Assert.Equal(new[] { (string)ExpectedLines.ElementAt(2)[1] }, diagnostics.Lines);
    }

    [Fact]
    public void Deprecated_Variables_Keep_Their_Effect()
    {
        // The warning changes nothing: the legacy values still apply when nothing wins over them.
        Set("SERVICE_NAME", "svc-legacy");
        Set("ENVIRONMENT", "dev");
        Set("OTEL_HELPER_SAMPLE_RATIO", "0.5");
        Set("OTEL_HELPER_DISABLED_SIGNALS", "logs");
        var resolved = TestDiagnostics.ResolveQuiet();
        Assert.Equal("svc-legacy", resolved.ServiceName);
        Assert.Equal("DEV", resolved.Environment);
        Assert.Equal("parentbased_traceidratio", resolved.SamplerName);
        Assert.Equal("logs", resolved.DisabledSignals);
    }

    [Fact]
    public void Extra_Instrumentation_Adds_No_Source_Or_Meter()
    {
        Set("OTEL_HELPER_EXTRA_INSTRUMENTATION", "SQL,REDIS,AWS");
        var with = TestDiagnostics.ResolveQuiet(TestDiagnostics.Identified);
        Set("OTEL_HELPER_EXTRA_INSTRUMENTATION", null);
        var without = TestDiagnostics.ResolveQuiet(TestDiagnostics.Identified);
        Assert.Equal(without.AdditionalActivitySources, with.AdditionalActivitySources);
        Assert.Equal(without.AdditionalMeters, with.AdditionalMeters);
        Assert.Empty(with.AdditionalActivitySources);
    }

    [Fact]
    public void Replacement_Is_Written_Whole_Beyond_The_Echo_Limit()
    {
        // The OTEL_HELPER_SAMPLE_RATIO and OTEL_HELPER_DISABLED_SIGNALS replacements are longer than
        // DiagnosticText.MaxEchoedValue (64); as an echoed attribute they were cut (measured, first C16 run).
        Assert.True(TelemetryResolver.DeprecatedEnvVars.Single(d => d.Variable == "OTEL_HELPER_DISABLED_SIGNALS").Replacement.Length > Diagnostics.DiagnosticText.MaxEchoedValue);
        Set("OTEL_HELPER_DISABLED_SIGNALS", "logs");
        var diagnostics = new TestDiagnostics();
        diagnostics.Resolve(TestDiagnostics.Identified);
        Assert.DoesNotContain("…", Assert.Single(diagnostics.Lines));
    }

    [Theory]
    [InlineData("OTEL_CONFIG_FILE")]
    [InlineData("OTEL_EXPERIMENTAL_CONFIG_FILE")]
    public void Config_File_Variable_Holding_Only_A_No_Break_Space_Is_Set(string variable)
    {
        // Contract trim set (Go trimWS): U+00A0 is not trimmed, so the variable is set and warns;
        // ASCII whitespace alone is unset.
        try
        {
            Set(variable, "\u00A0");
            var diagnostics = new TestDiagnostics();
            diagnostics.Resolve(TestDiagnostics.Identified);
            Assert.StartsWith($"otelhelper: WARN {variable} is not supported by the .NET helper and is ignored", Assert.Single(diagnostics.Lines));

            Set(variable, " \t");
            var blank = new TestDiagnostics();
            blank.Resolve(TestDiagnostics.Identified);
            Assert.Empty(blank.Lines);
        }
        finally
        {
            Set(variable, null);
        }
    }

    [Fact]
    public void A_Lone_No_Break_Space_Counts_As_Set_For_A_Deprecated_Variable()
    {
        Set("SERVICE_NAME", "\u00A0");
        var diagnostics = new TestDiagnostics();
        diagnostics.Resolve(TestDiagnostics.Identified);
        Assert.Single(diagnostics.Lines, l => l.StartsWith("otelhelper: WARN SERVICE_NAME is deprecated", StringComparison.Ordinal));
    }

    [Fact]
    public void Sink_Constants_Are_Whole_And_Escaped_While_Attributes_Stay_Cut()
    {
        var diagnostics = new TestDiagnostics();
        var longText = new string('a', 100);
        Assert.True(diagnostics.Sink.ReportOnceWithConstants("k", Diagnostics.DiagnosticLevel.Warn, "m",
            new[] { ("user", longText) }, new[] { ("constant", longText), ("spaced", "a b\nc") }));
        Assert.False(diagnostics.Sink.ReportOnceWithConstants("k", Diagnostics.DiagnosticLevel.Warn, "m",
            Array.Empty<(string, string)>(), Array.Empty<(string, string)>()));
        var line = Assert.Single(diagnostics.Lines);
        Assert.Equal($"otelhelper: WARN m user={new string('a', 64)}… constant={longText} spaced=\"a b\\nc\"", line);
    }

    [Fact]
    public void Sink_Constant_Key_Is_Checked_Like_An_Attribute_Key()
    {
        var diagnostics = new TestDiagnostics();
        Assert.Throws<ArgumentException>(() => diagnostics.Sink.ReportOnceWithConstants("k", Diagnostics.DiagnosticLevel.Warn, "m",
            Array.Empty<(string, string)>(), new[] { ("bad key", "v") }));
    }

    [Fact]
    public void Once_Is_Per_Sink_A_Second_Container_Warns_Again()
    {
        // Design D-NET5 and the C6 note (n): "once" is once per diagnostics sink (container).
        Set("SERVICE_NAME", "svc-legacy");
        var first = new TestDiagnostics();
        var second = new TestDiagnostics();
        first.Resolve(TestDiagnostics.Identified);
        second.Resolve(TestDiagnostics.Identified);
        Assert.Single(first.Lines);
        Assert.Single(second.Lines);
    }

    [Fact]
    public void A_Built_Pipeline_Writes_Each_Warning_Once_Through_The_Container_Sink()
    {
        Set("OTEL_HELPER_SAMPLE_RATIO", "0.5");
        Set("OTEL_HELPER_EXTRA_INSTRUMENTATION", "SQL");
        var diagnostics = new TestDiagnostics();
        using (TestPipelines.Build(o => { o.ServiceName = "svc"; o.Environment = "DEV"; }, diagnostics))
        {
        }

        Assert.Equal(1, diagnostics.Lines.Count(l => l.Contains("OTEL_HELPER_SAMPLE_RATIO is deprecated", StringComparison.Ordinal)));
        Assert.Equal(1, diagnostics.Lines.Count(l => l.Contains("OTEL_HELPER_EXTRA_INSTRUMENTATION has no effect", StringComparison.Ordinal)));
    }
}
