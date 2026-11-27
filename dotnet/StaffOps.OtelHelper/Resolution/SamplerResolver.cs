using System.Globalization;
using System.Text.RegularExpressions;
using Microsoft.Extensions.Configuration;
using OpenTelemetry.Trace;
using StaffOps.OtelHelper.Diagnostics;

namespace StaffOps.OtelHelper
{
    /// <summary>
    /// The trace sampler (contract R6, A20, ADR-002; design D-NET9). The helper always sets one, so
    /// the SDK never reads <c>OTEL_TRACES_SAMPLER</c> itself. Precedence: code
    /// <see cref="TelemetryOptions.Sampler"/> &gt; code <see cref="TelemetryOptions.SampleRatio"/>
    /// (<c>ParentBased(TraceIdRatioBased)</c>, any value, 1.0 included) &gt;
    /// <c>OTEL_TRACES_SAMPLER</c>/<c>_ARG</c> &gt; the deprecated <c>OTEL_HELPER_SAMPLE_RATIO</c>
    /// (read only while <c>OTEL_TRACES_SAMPLER</c> is unset) &gt; <c>parentbased_always_on</c>.
    /// Messages follow Go <c>buildSampler</c> and <c>samplerRatioArg</c>.
    /// </summary>
    internal static class SamplerResolver
    {
        internal const string TracesSamplerArgEnvVar = TelemetryOptions.TracesSamplerArgEnvVar;
        internal const string DefaultName = "parentbased_always_on";

        /// <summary>The six names the OTel SDK environment specification defines, in the order Go prints them.</summary>
        internal static readonly string[] KnownNames =
        {
            "always_on", "always_off", "traceidratio", "parentbased_always_on", "parentbased_always_off", "parentbased_traceidratio",
        };

        /// <summary>Spec names that need samplers the helper does not ship.</summary>
        internal static readonly string[] UnsupportedNames = { "jaeger_remote", "parentbased_jaeger_remote", "xray" };

        // A plain decimal: no sign, exponent, hex, digit separator or NaN/Inf spelling (contract D3).
        private static readonly Regex PlainDecimal = new(@"^[0-9]+(\.[0-9]+)?$", RegexOptions.CultureInvariant);

        /// <summary>The resolved sampler and the name the diagnostics print for it.</summary>
        internal readonly record struct Result(Sampler Sampler, string Name);

        internal static Result Resolve(TelemetryOptions code, IOtelHelperDiagnostics diagnostics, IConfiguration? configuration)
        {
            if (code.Sampler is not null)
            {
                return new Result(code.Sampler, "TelemetryOptions.Sampler");
            }

            if (code.SampleRatio is { } ratio)
            {
                return new Result(new ParentBasedSampler(new TraceIdRatioBasedSampler(ratio)), "parentbased_traceidratio");
            }

            var result = FromEnvironment(diagnostics);
            WarnConfigurationOnly(configuration, diagnostics, result.Name);
            return result;
        }

        private static Result FromEnvironment(IOtelHelperDiagnostics diagnostics)
        {
            var raw = ContractText.NonBlank(Env(TelemetryOptions.TracesSamplerEnvVar));
            if (raw is null)
            {
                return FromRatioAlias(diagnostics);
            }

            // ASCII folding only (A20): U+0130 is not "i", so a non-ASCII spelling is unknown.
            var name = ContractText.AsciiLower(raw);
            switch (name)
            {
                case "always_on":
                    return new Result(new AlwaysOnSampler(), name);
                case "always_off":
                    return new Result(new AlwaysOffSampler(), name);
                case "parentbased_always_on":
                    return Default();
                case "parentbased_always_off":
                    return new Result(new ParentBasedSampler(new AlwaysOffSampler()), name);
                case "traceidratio":
                    return new Result(new TraceIdRatioBasedSampler(RatioArgument(name, diagnostics)), name);
                case "parentbased_traceidratio":
                    return new Result(new ParentBasedSampler(new TraceIdRatioBasedSampler(RatioArgument(name, diagnostics))), name);
            }

            var message = Array.IndexOf(UnsupportedNames, name) >= 0
                ? "sampler is unsupported by this helper"
                : $"unknown sampler (valid: {string.Join(", ", KnownNames)})";
            diagnostics.ReportOnce($"sampler:{raw}", DiagnosticLevel.Warn, message,
                ("variable", TelemetryOptions.TracesSamplerEnvVar), ("value", raw), ("using", DefaultName));
            return Default();
        }

        /// <summary>
        /// <c>OTEL_TRACES_SAMPLER_ARG</c> for a ratio sampler: unset or blank → 1.0 silently; not a
        /// plain decimal in [0, 1] → warned, 1.0.
        /// </summary>
        private static double RatioArgument(string sampler, IOtelHelperDiagnostics diagnostics)
        {
            var raw = ContractText.NonBlank(Env(TracesSamplerArgEnvVar));
            if (raw is null)
            {
                return 1.0;
            }

            if (ParseRatio(raw) is { } ratio)
            {
                return ratio;
            }

            diagnostics.ReportOnce($"sampler-arg:{raw}", DiagnosticLevel.Warn, $"sampler argument is not a number in [0, 1] for {sampler}",
                ("variable", TracesSamplerArgEnvVar), ("value", raw), ("using", "1.0"));
            return 1.0;
        }

        /// <summary>
        /// The deprecated <c>OTEL_HELPER_SAMPLE_RATIO</c> (A20): a plain decimal in [0, 1] gives
        /// <c>ParentBased(TraceIdRatioBased(x))</c>; anything else is warned and ignored (not
        /// clamped), and the default applies. Its deprecation warning is task C16.
        /// </summary>
        private static Result FromRatioAlias(IOtelHelperDiagnostics diagnostics)
        {
            var raw = ContractText.NonBlank(Env(TelemetryOptions.SampleRatioEnvVar));
            if (raw is null)
            {
                return Default();
            }

            if (ParseRatio(raw) is { } ratio)
            {
                return new Result(new ParentBasedSampler(new TraceIdRatioBasedSampler(ratio)), "parentbased_traceidratio");
            }

            diagnostics.ReportOnce($"sample-ratio:{raw}", DiagnosticLevel.Warn, "sample ratio is not a number in [0, 1]",
                ("variable", TelemetryOptions.SampleRatioEnvVar), ("value", raw), ("using", DefaultName));
            return Default();
        }

        /// <summary>A plain decimal in [0, 1], else null (Go <c>parseRatio</c>).</summary>
        internal static double? ParseRatio(string text)
        {
            if (!PlainDecimal.IsMatch(text)
                || !double.TryParse(text, NumberStyles.AllowDecimalPoint, CultureInfo.InvariantCulture, out var value)
                || value > 1.0)
            {
                return null;
            }

            return value;
        }

        /// <summary>
        /// Principle 8: the helper always sets the sampler, so a sampler variable set only in
        /// <c>IConfiguration</c> (appsettings), which the SDK alone would have read, has no effect;
        /// say so once, naming the variable only (ADR-013, as C9 does for the export interval).
        /// </summary>
        private static void WarnConfigurationOnly(IConfiguration? configuration, IOtelHelperDiagnostics diagnostics, string usingName)
        {
            if (configuration is null)
            {
                return;
            }

            foreach (var variable in new[] { TelemetryOptions.TracesSamplerEnvVar, TracesSamplerArgEnvVar })
            {
                if (ContractText.NonBlank(Env(variable)) is null && ContractText.NonBlank(configuration[variable]) is not null)
                {
                    diagnostics.ReportOnce("iconfiguration-only:" + variable, DiagnosticLevel.Warn,
                        $"{variable} is set only in IConfiguration and is not read; set it in the process environment or TelemetryOptions.Sampler",
                        ("variable", variable), ("using", usingName));
                }
            }
        }

        private static Result Default() => new(new ParentBasedSampler(new AlwaysOnSampler()), DefaultName);

        private static string? Env(string name) => System.Environment.GetEnvironmentVariable(name);
    }
}
