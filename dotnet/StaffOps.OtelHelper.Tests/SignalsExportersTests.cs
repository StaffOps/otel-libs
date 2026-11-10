using System.Diagnostics;
using System.Diagnostics.Metrics;
using Microsoft.Extensions.Configuration;
using Microsoft.Extensions.DependencyInjection;
using Microsoft.Extensions.Logging;
using OpenTelemetry;
using OpenTelemetry.Logs;
using OpenTelemetry.Metrics;
using OpenTelemetry.Trace;
using StaffOps.OtelHelper;
using Xunit;

namespace StaffOps.OtelHelper.Tests;

/// <summary>
/// Task C9 (contract A19, R2, R8): exporter lists of the three signals, the deprecated
/// disabled-signals precedence, the export-interval grammar, the exemplar filter the helper
/// always sets, and the <c>OTEL_SDK_DISABLED</c> boolean. Parity cases cover the same rows on
/// the wire (<c>export.*_exporter_*</c>, <c>metrics.interval_*</c>, <c>config.disabled_signals_*</c>).
/// </summary>
[Collection("EnvVarTests")]
public sealed class SignalsExportersTests : IDisposable
{
    private static readonly string[] Variables =
    {
        "OTEL_TRACES_EXPORTER", "OTEL_LOGS_EXPORTER", "OTEL_METRICS_EXPORTER", "OTEL_HELPER_DISABLED_SIGNALS",
        "OTEL_METRIC_EXPORT_INTERVAL", "OTEL_SDK_DISABLED", "OTEL_METRICS_EXEMPLAR_FILTER",
        "OTEL_EXPORTER_OTLP_ENDPOINT", "OTEL_EXPORTER_OTLP_METRICS_ENDPOINT",
    };

    public SignalsExportersTests() => Clear();

    public void Dispose() => Clear();

    private static void Clear()
    {
        foreach (var variable in Variables)
        {
            Environment.SetEnvironmentVariable(variable, null);
        }
    }

    private static void Set(string variable, string? value) => Environment.SetEnvironmentVariable(variable, value);

    private static string? Validate(Action<TelemetryOptions> configure)
    {
        var options = new TelemetryOptions();
        configure(options);
        var result = new TelemetryOptionsValidator().Validate(null, options);
        return result.Failed ? result.FailureMessage : null;
    }

    // ── OTEL_TRACES_EXPORTER / OTEL_LOGS_EXPORTER parsing ───────────────────

    [Theory]
    [InlineData("OTEL_TRACES_EXPORTER")]
    [InlineData("OTEL_LOGS_EXPORTER")]
    public void Trace_And_Log_Exporters_Default_To_Otlp(string variable)
    {
        var resolved = TestDiagnostics.ResolveQuiet();
        Assert.Equal(new[] { "otlp" }, variable == "OTEL_TRACES_EXPORTER" ? resolved.TracesExporters : resolved.LogsExporters);
    }

    [Theory]
    [InlineData("console", new[] { "console" })]
    [InlineData(" OTLP , Console ", new[] { "otlp", "console" })]
    [InlineData("console,console,otlp", new[] { "console", "otlp" })]
    [InlineData("none", new string[0])]
    [InlineData("NONE", new string[0])]
    public void Traces_Exporter_List_Is_Parsed(string value, string[] expected)
    {
        Set("OTEL_TRACES_EXPORTER", value);
        var diagnostics = new TestDiagnostics();
        Assert.Equal(expected, diagnostics.Resolve(TestDiagnostics.Identified).TracesExporters);
        Assert.Empty(diagnostics.Lines);
    }

    [Theory]
    [InlineData("OTEL_TRACES_EXPORTER")]
    [InlineData("OTEL_LOGS_EXPORTER")]
    [InlineData("OTEL_METRICS_EXPORTER")]
    public void Logging_Is_A_Warned_Alias_Of_Console(string variable)
    {
        Set(variable, "logging");
        var diagnostics = new TestDiagnostics();
        var resolved = diagnostics.Resolve(TestDiagnostics.Identified);
        var list = variable switch
        {
            "OTEL_TRACES_EXPORTER" => resolved.TracesExporters,
            "OTEL_LOGS_EXPORTER" => resolved.LogsExporters,
            _ => resolved.MetricExporters,
        };
        Assert.Equal(new[] { "console" }, list);
        var line = Assert.Single(diagnostics.Lines);
        Assert.StartsWith($"otelhelper: WARN deprecated exporter value 'logging' in {variable}; use 'console'", line);
    }

    [Fact]
    public void Logging_And_Console_Together_Are_One_Console_Exporter()
    {
        Set("OTEL_LOGS_EXPORTER", "logging,console");
        var diagnostics = new TestDiagnostics();
        Assert.Equal(new[] { "console" }, diagnostics.Resolve(TestDiagnostics.Identified).LogsExporters);
        Assert.Single(diagnostics.Lines);
    }

    [Fact]
    public void Unknown_Trace_Exporter_Warns_And_Only_Unknown_Uses_The_Default()
    {
        Set("OTEL_TRACES_EXPORTER", "zipkin");
        var diagnostics = new TestDiagnostics();
        Assert.Equal(new[] { "otlp" }, diagnostics.Resolve(TestDiagnostics.Identified).TracesExporters);
        var line = Assert.Single(diagnostics.Lines);
        Assert.Contains("invalid OTEL_TRACES_EXPORTER: unknown exporter ignored; valid values: otlp, console, none", line);
        Assert.Contains("value=zipkin", line);
    }

    [Fact]
    public void Prometheus_Is_Not_A_Logs_Exporter()
    {
        Set("OTEL_LOGS_EXPORTER", "prometheus,console");
        var diagnostics = new TestDiagnostics();
        Assert.Equal(new[] { "console" }, diagnostics.Resolve(TestDiagnostics.Identified).LogsExporters);
        Assert.Contains(diagnostics.Lines, l => l.Contains("invalid OTEL_LOGS_EXPORTER: unknown exporter ignored"));
    }

    [Theory]
    [InlineData("OTEL_TRACES_EXPORTER")]
    [InlineData("OTEL_LOGS_EXPORTER")]
    public void None_Combined_Warns_And_Wins(string variable)
    {
        Set(variable, "none,otlp");
        var diagnostics = new TestDiagnostics();
        var resolved = diagnostics.Resolve(TestDiagnostics.Identified);
        Assert.Empty(variable == "OTEL_TRACES_EXPORTER" ? resolved.TracesExporters : resolved.LogsExporters);
        var line = Assert.Single(diagnostics.Lines);
        Assert.Contains($"invalid {variable}: 'none' combined with other exporters", line);
    }

    [Fact]
    public void Blank_Or_Separator_Only_Exporter_Variable_Is_Unset()
    {
        Set("OTEL_TRACES_EXPORTER", " , ,");
        var diagnostics = new TestDiagnostics();
        Assert.Equal(new[] { "otlp" }, diagnostics.Resolve(TestDiagnostics.Identified).TracesExporters);
        Assert.Empty(diagnostics.Lines);
    }

    [Fact]
    public void Sdk_Disabled_Leaves_No_Signal_Exporter()
    {
        Set("OTEL_SDK_DISABLED", "true");
        Set("OTEL_TRACES_EXPORTER", "console");
        var resolved = TestDiagnostics.ResolveQuiet(TestDiagnostics.Identified);
        Assert.Empty(resolved.ResolvedSignalExporters("traces"));
        Assert.Empty(resolved.ResolvedSignalExporters("logs"));
    }

    // ── OTEL_METRICS_EXPORTER: console and otlp without an endpoint ─────────

    [Fact]
    public void Console_Metric_Exporter_Is_Accepted_From_Env_And_Code()
    {
        Set("OTEL_METRICS_EXPORTER", "console");
        var diagnostics = new TestDiagnostics();
        Assert.Equal(new[] { "console" }, diagnostics.Resolve(TestDiagnostics.Identified).ResolvedMetricExporters());
        Assert.Empty(diagnostics.Lines);

        Assert.Null(Validate(o => o.MetricExporters = "console,prometheus"));
        Assert.Equal(new[] { "console" }, TestDiagnostics.ResolveQuiet(o => o.MetricExporters = "Console").ResolvedMetricExporters());
    }

    [Fact]
    public void Code_Logging_Metric_Exporter_Fails_Naming_It()
    {
        var failure = Validate(o => o.MetricExporters = "logging");
        Assert.NotNull(failure);
        Assert.Contains("Unknown metric exporter(s) logging in TelemetryOptions.MetricExporters", failure);
        Assert.Contains("TelemetryOptions.MetricExporters", failure);
    }

    [Fact]
    public void Env_Otlp_Alone_Without_Endpoint_Warns_And_Falls_Back_To_Prometheus()
    {
        Set("OTEL_METRICS_EXPORTER", "otlp");
        var diagnostics = new TestDiagnostics();
        Assert.Equal(new[] { "prometheus" }, diagnostics.Resolve(TestDiagnostics.Identified).ResolvedMetricExporters());
        var line = Assert.Single(diagnostics.Lines);
        Assert.Contains("metric exporter 'otlp' has no endpoint and is ignored", line);
        Assert.Contains("variable=OTEL_METRICS_EXPORTER", line);
    }

    [Fact]
    public void Env_Otlp_And_Console_Without_Endpoint_Keeps_Console()
    {
        Set("OTEL_METRICS_EXPORTER", "otlp,console");
        Assert.Equal(new[] { "console" }, TestDiagnostics.ResolveQuiet(TestDiagnostics.Identified).ResolvedMetricExporters());
    }

    [Fact]
    public void Env_Otlp_With_A_Metrics_Endpoint_Is_Kept()
    {
        Set("OTEL_METRICS_EXPORTER", "otlp");
        Set("OTEL_EXPORTER_OTLP_METRICS_ENDPOINT", "http://collector:4318/v1/metrics");
        var diagnostics = new TestDiagnostics();
        Assert.Equal(new[] { "otlp" }, diagnostics.Resolve(TestDiagnostics.Identified).ResolvedMetricExporters());
        Assert.DoesNotContain(diagnostics.Lines, l => l.Contains("has no endpoint"));
    }

    // ── OTEL_HELPER_DISABLED_SIGNALS precedence ─────────────────────────────

    [Theory]
    [InlineData("OTEL_TRACES_EXPORTER", "otlp", "traces")]
    [InlineData("OTEL_TRACES_EXPORTER", "console", "traces")]
    [InlineData("OTEL_LOGS_EXPORTER", "otlp", "logs")]
    [InlineData("OTEL_METRICS_EXPORTER", "prometheus", "metrics")]
    public void A_Set_Standard_Exporter_Variable_Wins_Over_Disabled_Signals(string variable, string value, string signal)
    {
        Set("OTEL_HELPER_DISABLED_SIGNALS", signal);
        Set(variable, value);
        var resolved = TestDiagnostics.ResolveQuiet(TestDiagnostics.Identified);
        Assert.True(resolved.IsSignalEnabled(signal));
        Assert.Equal("", resolved.DisabledSignals);
    }

    [Fact]
    public void Standard_None_Counts_As_Set_And_Turns_The_Signal_Off()
    {
        Set("OTEL_HELPER_DISABLED_SIGNALS", "traces");
        Set("OTEL_TRACES_EXPORTER", "none");
        var resolved = TestDiagnostics.ResolveQuiet(TestDiagnostics.Identified);
        Assert.True(resolved.IsSignalEnabled("traces"));
        Assert.Empty(resolved.ResolvedSignalExporters("traces"));
    }

    [Fact]
    public void Standard_Variable_With_Only_Unknown_Entries_Counts_As_Unset()
    {
        Set("OTEL_HELPER_DISABLED_SIGNALS", "traces,metrics");
        Set("OTEL_TRACES_EXPORTER", "zipkin");
        Set("OTEL_METRICS_EXPORTER", "bogus");
        var diagnostics = new TestDiagnostics();
        var resolved = diagnostics.Resolve(TestDiagnostics.Identified);
        Assert.False(resolved.IsSignalEnabled("traces"));
        Assert.Empty(resolved.ResolvedMetricExporters());
        Assert.Contains(diagnostics.Lines, l => l.Contains("value=zipkin"));
        Assert.Contains(diagnostics.Lines, l => l.Contains("value=bogus"));
    }

    [Fact]
    public void Env_Otlp_Dropped_For_No_Endpoint_Still_Counts_As_Set()
    {
        Set("OTEL_HELPER_DISABLED_SIGNALS", "metrics");
        Set("OTEL_METRICS_EXPORTER", "otlp");
        Assert.Equal(new[] { "prometheus" }, TestDiagnostics.ResolveQuiet(TestDiagnostics.Identified).ResolvedMetricExporters());
    }

    [Fact]
    public void Code_Metric_Exporters_Win_Over_Disabled_Signals_For_Metrics_Only()
    {
        Set("OTEL_HELPER_DISABLED_SIGNALS", "metrics,logs");
        var resolved = TestDiagnostics.ResolveQuiet(o =>
        {
            TestDiagnostics.Identified(o);
            o.MetricExporters = "console";
        });
        Assert.Equal(new[] { "console" }, resolved.ResolvedMetricExporters());
        Assert.False(resolved.IsSignalEnabled("logs"));
        Assert.Equal("logs", resolved.DisabledSignals);
    }

    [Fact]
    public void Code_Disabled_Signals_Win_Over_A_Set_Standard_Variable()
    {
        Set("OTEL_TRACES_EXPORTER", "otlp");
        var resolved = TestDiagnostics.ResolveQuiet(o =>
        {
            TestDiagnostics.Identified(o);
            o.DisabledSignals = " Traces ,traces";
        });
        Assert.Equal("traces", resolved.DisabledSignals);
        Assert.Empty(resolved.ResolvedSignalExporters("traces"));
    }

    [Fact]
    public void Unknown_Disabled_Signal_Entry_Warns_Once_Echoing_It()
    {
        Set("OTEL_HELPER_DISABLED_SIGNALS", "profiles,PROFILES, logs");
        var diagnostics = new TestDiagnostics();
        var resolved = diagnostics.Resolve(TestDiagnostics.Identified);
        diagnostics.Resolve(TestDiagnostics.Identified);
        var line = Assert.Single(diagnostics.Lines);
        Assert.Contains("invalid OTEL_HELPER_DISABLED_SIGNALS: unknown signal ignored; valid values: traces, metrics, logs", line);
        Assert.Contains("value=profiles", line);
        Assert.Equal("logs", resolved.DisabledSignals);
    }

    [Fact]
    public void Unknown_Code_Disabled_Signal_Fails_Naming_The_Option()
    {
        var failure = Validate(o => o.DisabledSignals = "traces, Profiles,profiles");
        Assert.NotNull(failure);
        Assert.Contains("Unknown signal(s) profiles in TelemetryOptions.DisabledSignals", failure);
        Assert.Contains("Valid values: traces, metrics, logs", failure);
        Assert.Null(Validate(o => o.DisabledSignals = " Traces , LOGS "));
    }

    [Theory]
    [InlineData("")]
    [InlineData("  ")]
    public void Code_Empty_Disabled_Signals_Are_Set_And_Win_Over_The_Env(string value)
    {
        Set("OTEL_HELPER_DISABLED_SIGNALS", "traces,logs");
        var resolved = TestDiagnostics.ResolveQuiet(o => { TestDiagnostics.Identified(o); o.DisabledSignals = value; });
        Assert.Equal("", resolved.DisabledSignals);
        Assert.True(resolved.IsSignalEnabled("traces"));
        Assert.True(resolved.IsSignalEnabled("logs"));
        Assert.Null(Validate(o => o.DisabledSignals = value));
    }

    [Theory]
    [InlineData("")]
    [InlineData(" , ")]
    public void Code_Empty_Metric_Exporters_Are_Set_Mean_None_And_Win(string value)
    {
        Set("OTEL_METRICS_EXPORTER", "prometheus");
        Set("OTEL_HELPER_DISABLED_SIGNALS", "metrics");
        var resolved = TestDiagnostics.ResolveQuiet(o => { TestDiagnostics.Identified(o); o.MetricExporters = value; });
        Assert.Empty(resolved.ResolvedMetricExporters());
        Assert.True(resolved.IsSignalEnabled("metrics"));
        Assert.Null(Validate(o => o.MetricExporters = value));
    }

    [Fact]
    public void Code_Empty_Metric_Exporters_Say_So_Once_At_Info()
    {
        var diagnostics = new TestDiagnostics();
        diagnostics.Resolve(o => { TestDiagnostics.Identified(o); o.MetricExporters = ""; });
        diagnostics.Resolve(o => { TestDiagnostics.Identified(o); o.MetricExporters = ""; });
        Assert.Equal(new[] { "otelhelper: INFO TelemetryOptions.MetricExporters is empty: no metric exporter" }, diagnostics.Lines);

        var none = new TestDiagnostics();
        none.Resolve(o => { TestDiagnostics.Identified(o); o.MetricExporters = "none"; });
        Assert.Empty(none.Lines);
    }

    [Fact]
    public void Unknown_Entry_And_None_Combined_Each_Get_Their_Own_Line()
    {
        Set("OTEL_TRACES_EXPORTER", "bogus,none,otlp");
        var diagnostics = new TestDiagnostics();
        Assert.Empty(diagnostics.Resolve(TestDiagnostics.Identified).TracesExporters);
        Assert.Equal(2, diagnostics.Lines.Length);
        Assert.Contains(diagnostics.Lines, l => l.Contains("invalid OTEL_TRACES_EXPORTER: unknown exporter ignored"));
        Assert.Contains(diagnostics.Lines, l => l.Contains("invalid OTEL_TRACES_EXPORTER: 'none' combined with other exporters"));
    }

    // ── OTEL_METRIC_EXPORT_INTERVAL grammar ────────────────────────────────

    [Theory]
    [InlineData("5000", 5000L)]
    [InlineData("+5000", 5000L)]
    [InlineData(" 5000\t", 5000L)]
    [InlineData("0005000", 5000L)]
    [InlineData("1", 1L)]
    [InlineData("2147483647", 2147483647L)]
    public void Interval_Valid_Values_Are_Used_Without_A_Warning(string value, long expected)
    {
        Set("OTEL_METRIC_EXPORT_INTERVAL", value);
        var diagnostics = new TestDiagnostics();
        Assert.Equal(expected, diagnostics.Resolve(TestDiagnostics.Identified).ExportIntervalMs);
        Assert.Empty(diagnostics.Lines);
    }

    [Theory]
    [InlineData("abc")]
    [InlineData("0")]
    [InlineData("000")]
    [InlineData("+0")]
    [InlineData("+")]
    [InlineData("-5000")]
    [InlineData("++5000")]
    [InlineData("5_000")]
    [InlineData("5000ms")]
    [InlineData("5 000")]
    [InlineData("5e3")]
    [InlineData("٥٠٠٠")]
    public void Interval_Invalid_Values_Warn_And_Use_30000(string value)
    {
        Set("OTEL_METRIC_EXPORT_INTERVAL", value);
        var diagnostics = new TestDiagnostics();
        Assert.Equal(30_000, diagnostics.Resolve(TestDiagnostics.Identified).ExportIntervalMs);
        var line = Assert.Single(diagnostics.Lines);
        Assert.StartsWith("otelhelper: WARN invalid OTEL_METRIC_EXPORT_INTERVAL: not a positive integer number of milliseconds, using 30000", line);
    }

    [Theory]
    [InlineData("2147483648")]
    [InlineData("3000000000")]
    [InlineData("9999999999")]
    [InlineData("+00012147483648")]
    public void Interval_Above_The_Maximum_Is_Clamped_With_A_Warning(string value)
    {
        Set("OTEL_METRIC_EXPORT_INTERVAL", value);
        var diagnostics = new TestDiagnostics();
        Assert.Equal(int.MaxValue, diagnostics.Resolve(TestDiagnostics.Identified).ExportIntervalMs);
        var line = Assert.Single(diagnostics.Lines);
        Assert.Contains("invalid OTEL_METRIC_EXPORT_INTERVAL: above 2147483647 ms, clamped to 2147483647", line);
    }

    [Fact]
    public void Interval_Of_Thousands_Of_Digits_Is_Clamped_And_Echoed_Truncated()
    {
        Set("OTEL_METRIC_EXPORT_INTERVAL", new string('9', 5000));
        var diagnostics = new TestDiagnostics();
        Assert.Equal(int.MaxValue, diagnostics.Resolve(TestDiagnostics.Identified).ExportIntervalMs);
        var line = Assert.Single(diagnostics.Lines);
        Assert.Contains(new string('9', 64) + "…", line);
        Assert.DoesNotContain(new string('9', 65), line);
        Assert.True(line.Length < 400, line.Length.ToString(System.Globalization.CultureInfo.InvariantCulture));
    }

    [Fact]
    public void Interval_Code_Value_Beats_An_Invalid_Env_Value_Silently()
    {
        Set("OTEL_METRIC_EXPORT_INTERVAL", "abc");
        var diagnostics = new TestDiagnostics();
        Assert.Equal(1234, diagnostics.Resolve(o => { TestDiagnostics.Identified(o); o.ExportIntervalMs = 1234; }).ExportIntervalMs);
        Assert.Empty(diagnostics.Lines);
    }

    [Theory]
    [InlineData("5000")]
    [InlineData("abc")]
    public void Interval_Only_In_IConfiguration_Is_Not_Read_And_Warns_Once(string value)
    {
        // ADR-013: helper-owned rows come from code and the process environment only; principle 8:
        // the SDK's own parse line is dropped, so the helper says the value is not read.
        var configuration = new ConfigurationBuilder()
            .AddInMemoryCollection(new Dictionary<string, string?> { ["OTEL_METRIC_EXPORT_INTERVAL"] = value })
            .Build();
        var diagnostics = new TestDiagnostics();
        Assert.Equal(30_000, diagnostics.Resolve(TestDiagnostics.Identified, configuration).ExportIntervalMs);
        diagnostics.Resolve(TestDiagnostics.Identified, configuration);
        var line = Assert.Single(diagnostics.Lines);
        Assert.StartsWith("otelhelper: WARN OTEL_METRIC_EXPORT_INTERVAL is set only in IConfiguration and is not read", line);
        Assert.DoesNotContain(value, line);
    }

    [Fact]
    public void Interval_In_The_Environment_Seen_Through_IConfiguration_Gives_No_Not_Read_Line()
    {
        Set("OTEL_METRIC_EXPORT_INTERVAL", "5000");
        var configuration = new ConfigurationBuilder().AddEnvironmentVariables().Build();
        var diagnostics = new TestDiagnostics();
        Assert.Equal(5000, diagnostics.Resolve(TestDiagnostics.Identified, configuration).ExportIntervalMs);
        Assert.Empty(diagnostics.Lines);
    }

    [Fact]
    public void Interval_In_Code_Gives_No_Not_Read_Line()
    {
        var configuration = new ConfigurationBuilder()
            .AddInMemoryCollection(new Dictionary<string, string?> { ["OTEL_METRIC_EXPORT_INTERVAL"] = "5000" })
            .Build();
        var diagnostics = new TestDiagnostics();
        Assert.Equal(1234, diagnostics.Resolve(o => { TestDiagnostics.Identified(o); o.ExportIntervalMs = 1234; }, configuration).ExportIntervalMs);
        Assert.Empty(diagnostics.Lines);
    }

    // ── OTEL_SDK_DISABLED boolean ─────────────────────────────────────────

    [Theory]
    [InlineData("maybe")]
    [InlineData("1")]
    [InlineData("yes")]
    [InlineData("on")]
    public void Sdk_Disabled_Unparseable_Warns_Naming_The_Variable_And_Means_False(string value)
    {
        Set("OTEL_SDK_DISABLED", value);
        var diagnostics = new TestDiagnostics();
        Assert.False(diagnostics.Resolve(TestDiagnostics.Identified).SdkDisabled);
        var line = Assert.Single(diagnostics.Lines);
        Assert.StartsWith("otelhelper: WARN invalid OTEL_SDK_DISABLED: not a boolean (true or false), using false", line);
    }

    [Theory]
    [InlineData("false", false)]
    [InlineData(" False ", false)]
    [InlineData("TRUE", true)]
    public void Sdk_Disabled_Booleans_Do_Not_Warn(string value, bool expected)
    {
        Set("OTEL_SDK_DISABLED", value);
        var diagnostics = new TestDiagnostics();
        Assert.Equal(expected, diagnostics.Resolve(TestDiagnostics.Identified).SdkDisabled);
        Assert.DoesNotContain(diagnostics.Lines, l => l.Contains("WARN"));
    }

    [Fact]
    public void Sdk_Disabled_Unparseable_In_IConfiguration_Warns_Too()
    {
        var configuration = new ConfigurationBuilder()
            .AddInMemoryCollection(new Dictionary<string, string?> { ["OTEL_SDK_DISABLED"] = "maybe" })
            .Build();
        var diagnostics = new TestDiagnostics();
        Assert.False(diagnostics.Resolve(TestDiagnostics.Identified, configuration).SdkDisabled);
        Assert.Contains(diagnostics.Lines, l => l.Contains("invalid OTEL_SDK_DISABLED"));
    }

    // ── exemplar filter (ADR-013 exception: no fixture kind observes it) ───

    [Theory]
    [InlineData(null, OpenTelemetry.Metrics.ExemplarFilterType.TraceBased)]
    [InlineData("always_on", OpenTelemetry.Metrics.ExemplarFilterType.AlwaysOn)]
    [InlineData(" ALWAYS_OFF ", OpenTelemetry.Metrics.ExemplarFilterType.AlwaysOff)]
    [InlineData("Trace_Based", OpenTelemetry.Metrics.ExemplarFilterType.TraceBased)]
    public void Exemplar_Filter_Is_Parsed(string? value, OpenTelemetry.Metrics.ExemplarFilterType expected)
    {
        Set("OTEL_METRICS_EXEMPLAR_FILTER", value);
        var diagnostics = new TestDiagnostics();
        Assert.Equal(expected, diagnostics.Resolve(TestDiagnostics.Identified).ExemplarFilter);
        Assert.Empty(diagnostics.Lines);
    }

    [Fact]
    public void Exemplar_Filter_Unknown_Warns_And_Is_Trace_Based()
    {
        Set("OTEL_METRICS_EXEMPLAR_FILTER", "sometimes");
        var diagnostics = new TestDiagnostics();
        Assert.Equal(OpenTelemetry.Metrics.ExemplarFilterType.TraceBased, diagnostics.Resolve(TestDiagnostics.Identified).ExemplarFilter);
        var line = Assert.Single(diagnostics.Lines);
        Assert.Contains("invalid OTEL_METRICS_EXEMPLAR_FILTER: unsupported exemplar filter, using trace_based", line);
    }

    /// <summary>
    /// The unit test ADR-013 names: the helper sets the filter on the real provider. trace_based
    /// keeps exemplars only under a sampled span; always_on also without one; always_off never.
    /// The .NET SDK default (AlwaysOff) would give no exemplar in any row.
    /// </summary>
    [Theory]
    [InlineData(null, false, false)]
    [InlineData(null, true, true)]
    [InlineData("always_on", false, true)]
    [InlineData("always_off", true, false)]
    public void ExemplarFilter_DefaultsToTraceBased_AndHonorsEnv(string? value, bool underSampledSpan, bool expectExemplar)
    {
        Set("OTEL_METRICS_EXEMPLAR_FILTER", value);
        Assert.Equal(expectExemplar, RecordAndCountExemplars(underSampledSpan, laterAppFilter: null) > 0);
    }

    [Fact]
    public void An_Application_SetExemplarFilter_Registered_Later_Wins()
    {
        Set("OTEL_METRICS_EXEMPLAR_FILTER", "always_on");
        Assert.Equal(0, RecordAndCountExemplars(underSampledSpan: false, laterAppFilter: ExemplarFilterType.AlwaysOff));
    }

    private static int RecordAndCountExemplars(bool underSampledSpan, ExemplarFilterType? laterAppFilter)
    {
        var meterName = "c9-exemplar-" + Guid.NewGuid().ToString("N");
        var exporter = new ExemplarCountingExporter();
        var services = new ServiceCollection();
        services.AddSingleton<StaffOps.OtelHelper.Diagnostics.IOtelHelperDiagnostics>(new TestDiagnostics().Sink);
        services.AddOtelHelper(o =>
        {
            TestDiagnostics.Identified(o);
            o.PrometheusMetricsPort = 0;
            o.AdditionalMeters.Add(meterName);
        });
        services.ConfigureOpenTelemetryMeterProvider(b =>
        {
            b.AddReader(new BaseExportingMetricReader(exporter));
            if (laterAppFilter is { } filter)
            {
                b.SetExemplarFilter(filter);
            }
        });
        using var provider = services.BuildServiceProvider();
        var meterProvider = provider.GetRequiredService<MeterProvider>();
        using (var meter = new Meter(meterName))
        {
            var counter = meter.CreateCounter<long>("c9.exemplar.requests");
            Activity? span = null;
            if (underSampledSpan)
            {
                span = new Activity("c9-span").SetIdFormat(ActivityIdFormat.W3C);
                span.ActivityTraceFlags = ActivityTraceFlags.Recorded;
                span.Start();
            }

            counter.Add(1);
            span?.Stop();
            meterProvider.ForceFlush();
        }

        return exporter.Exemplars;
    }

    private sealed class ExemplarCountingExporter : BaseExporter<Metric>
    {
        public int Exemplars { get; private set; }

        public override ExportResult Export(in Batch<Metric> batch)
        {
            foreach (var metric in batch)
            {
                if (metric.Name != "c9.exemplar.requests")
                {
                    continue;
                }

                foreach (ref readonly var point in metric.GetMetricPoints())
                {
                    if (point.TryGetExemplars(out var exemplars))
                    {
                        foreach (ref readonly var exemplar in exemplars)
                        {
                            _ = exemplar;
                            Exemplars++;
                        }
                    }
                }
            }

            return ExportResult.Success;
        }
    }

    // ── SDK parse warnings for rows the helper owns are not forwarded ──────

    [Fact]
    public void Plus_Prefixed_Interval_Gives_No_Sdk_Line()
    {
        // The helper accepts "+1000"; the SDK's reader options reject it and say so (forwarded before C9 fix round 1).
        Set("OTEL_METRIC_EXPORT_INTERVAL", "+1000");
        var diagnostics = new TestDiagnostics();
        using (TestPipelines.Build(o => { TestDiagnostics.Identified(o); o.OtelCollectorEndpoint = "http://127.0.0.1:1"; o.PrometheusMetricsPort = 0; o.DisabledSignals = "traces,logs"; }, diagnostics))
        {
        }

        Assert.DoesNotContain("OTEL_METRIC_EXPORT_INTERVAL", diagnostics.Text);
    }

    [Fact]
    public void Unparseable_Sdk_Disabled_Is_Warned_Once_By_The_Helper_Only()
    {
        Set("OTEL_SDK_DISABLED", "maybe");
        var diagnostics = new TestDiagnostics();
        using (TestPipelines.Build(o => { TestDiagnostics.Identified(o); o.PrometheusMetricsPort = 0; }, diagnostics))
        {
        }

        var line = Assert.Single(diagnostics.Lines, l => l.Contains("OTEL_SDK_DISABLED"));
        Assert.StartsWith("otelhelper: WARN invalid OTEL_SDK_DISABLED", line);
    }

    [Theory]
    [InlineData("Configuration key 'OTEL_METRIC_EXPORT_INTERVAL' has an invalid value: '+1000'", true)]
    [InlineData("Configuration key 'OTEL_SDK_DISABLED' has an invalid value: 'maybe'", true)]
    [InlineData("Configuration key 'OTEL_METRIC_EXPORT_TIMEOUT' has an invalid value: 'x'", false)]
    public void Sdk_Parse_Warnings_Of_Helper_Owned_Rows_Are_Noise(string text, bool noise)
        => Assert.Equal(noise, StaffOps.OtelHelper.Diagnostics.SdkTextSanitizer.IsNoise("OpenTelemetry-Sdk", text));

    // ── "otlp" guard and "none" without a receiver (formerly parity-only) ───

    [Theory]
    [InlineData("traces", "console", false)]
    [InlineData("traces", "otlp", true)]
    [InlineData("logs", "none", false)]
    [InlineData("logs", "otlp", true)]
    public void Otlp_Is_Exported_Only_When_Listed(string signal, string exporters, bool expectConnection)
    {
        using var listener = new System.Net.Sockets.TcpListener(System.Net.IPAddress.Loopback, 0);
        listener.Start();
        var port = ((System.Net.IPEndPoint)listener.LocalEndpoint).Port;
        Set(signal == "traces" ? "OTEL_TRACES_EXPORTER" : "OTEL_LOGS_EXPORTER", exporters);
        var original = Console.Out;
        Console.SetOut(new StringWriter());
        try
        {
            using var provider = TestPipelines.Build(o =>
            {
                TestDiagnostics.Identified(o);
                o.ServiceName = "c9-guard";
                o.OtelCollectorEndpoint = $"http://127.0.0.1:{port}";
                o.OtlpProtocol = "http/protobuf";
                o.ExportTimeoutMs = 1000;
                o.PrometheusMetricsPort = 0;
                o.DisabledSignals = signal == "traces" ? "metrics,logs" : "traces,metrics";
            });
            if (signal == "traces")
            {
                using (var source = new ActivitySource("c9-guard"))
                using (source.StartActivity("c9-guard-span"))
                {
                }

                provider.GetRequiredService<OpenTelemetry.Trace.TracerProvider>().ForceFlush(3000);
            }
            else
            {
                provider.GetRequiredService<ILoggerFactory>().CreateLogger("c9.guard").LogWarning("c9-guard-log");
                provider.GetRequiredService<OpenTelemetry.Logs.LoggerProvider>().ForceFlush(3000);
            }

            Assert.Equal(expectConnection, listener.Pending());
        }
        finally
        {
            Console.SetOut(original);
        }
    }

    // ── console exporters and "none" on a built pipeline ───────────────────

    [Fact]
    public void Traces_None_Leaves_The_Service_Source_Without_A_Listener()
    {
        Set("OTEL_TRACES_EXPORTER", "none");
        using var provider = TestPipelines.Build(o => { o.ServiceName = "c9-traces-none"; o.Environment = "DEV"; o.PrometheusMetricsPort = 0; });
        using var source = new ActivitySource("c9-traces-none");
        Assert.Null(source.StartActivity("not-recorded"));
    }

    [Fact]
    public void Console_Exporters_Write_Spans_Logs_And_Metrics_To_Standard_Output()
    {
        Set("OTEL_TRACES_EXPORTER", "console");
        Set("OTEL_LOGS_EXPORTER", "logging");
        Set("OTEL_METRICS_EXPORTER", "console");
        var original = Console.Out;
        var captured = new StringWriter();
        Console.SetOut(TextWriter.Synchronized(captured));
        try
        {
            var provider = TestPipelines.Build(o => { o.ServiceName = "c9-console"; o.Environment = "DEV"; o.PrometheusMetricsPort = 0; });
            using (var source = new ActivitySource("c9-console"))
            using (source.StartActivity("c9-console-span"))
            {
            }

            provider.GetRequiredService<ILoggerFactory>().CreateLogger("c9.console").LogWarning("c9-console-log");
            using (var meter = new Meter("c9-console"))
            {
                meter.CreateCounter<long>("c9.console.requests").Add(1);
                provider.Dispose();
            }
        }
        finally
        {
            Console.SetOut(original);
        }

        var text = captured.ToString();
        Assert.Contains("c9-console-span", text);
        Assert.Contains("c9-console-log", text);
        Assert.Contains("c9.console.requests", text);
    }
}
