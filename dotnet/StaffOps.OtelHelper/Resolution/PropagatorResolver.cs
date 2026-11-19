using Microsoft.Extensions.Configuration;
using StaffOps.OtelHelper.Diagnostics;

namespace StaffOps.OtelHelper
{
    /// <summary>
    /// <c>OTEL_PROPAGATORS</c> (contract R8, D1 rows "Propagators" and "Propagator diagnostics",
    /// A20; design D-NET9). The helper links the W3C propagators only: <c>tracecontext</c> and
    /// <c>baggage</c>; <c>none</c> disables propagation. Tokens are split on commas, trimmed with the
    /// contract set, ASCII-folded and de-duplicated; empty tokens are dropped. Every skipped token is
    /// warned on its own ("not supported" for spec values the helper does not link, else "unknown"),
    /// and <c>none</c> with other tokens warns too; each warning names the effective list as
    /// <c>using</c>. Nothing supported left → the default <c>tracecontext, baggage</c>. Go
    /// <c>parsePropagators</c> and Python <c>build_propagator</c> are the reference.
    /// </summary>
    internal static class PropagatorResolver
    {
        internal const string PropagatorsEnvVar = TelemetryOptions.PropagatorsEnvVar;

        internal static readonly string[] DefaultNames = { "tracecontext", "baggage" };

        /// <summary>Spec values the .NET helper does not link.</summary>
        internal static readonly string[] UnsupportedSpecNames = { "b3", "b3multi", "jaeger", "xray", "ottrace" };

        /// <summary>The effective propagator names, in the order given; empty = propagation disabled (<c>none</c>).</summary>
        internal static IReadOnlyList<string> Resolve(IOtelHelperDiagnostics diagnostics, IConfiguration? configuration = null)
        {
            var raw = System.Environment.GetEnvironmentVariable(PropagatorsEnvVar);
            if (ContractText.NonBlank(raw) is not { } text)
            {
                // Principle 8: nothing reads a value set only in IConfiguration (ADR-013); say so once.
                if (configuration is not null && ContractText.NonBlank(configuration[PropagatorsEnvVar]) is not null)
                {
                    diagnostics.ReportOnce("iconfiguration-only:" + PropagatorsEnvVar, DiagnosticLevel.Warn,
                        $"{PropagatorsEnvVar} is set only in IConfiguration and is not read; set it in the process environment",
                        ("variable", PropagatorsEnvVar), ("using", string.Join(", ", DefaultNames)));
                }

                return DefaultNames;
            }

            var names = new List<string>();
            var skipped = new List<(string Message, string Token)>();
            var seen = new HashSet<string>(StringComparer.Ordinal);
            var none = false;
            foreach (var part in text.Split(','))
            {
                var token = ContractText.AsciiLower(ContractText.Trim(part));
                if (token.Length == 0 || !seen.Add(token))
                {
                    continue;
                }

                switch (token)
                {
                    case "tracecontext":
                    case "baggage":
                        names.Add(token);
                        break;
                    case "none":
                        none = true;
                        break;
                    default:
                        skipped.Add((Array.IndexOf(UnsupportedSpecNames, token) >= 0
                            ? "propagator not supported by the .NET helper, ignored"
                            : "unknown propagator, ignored", token));
                        break;
                }
            }

            IReadOnlyList<string> effective = none ? Array.Empty<string>() : names.Count == 0 ? DefaultNames : names;
            var usingText = none ? "none" : string.Join(", ", effective);
            foreach (var (message, token) in skipped)
            {
                diagnostics.ReportOnce($"propagator:{token}:{usingText}", DiagnosticLevel.Warn, message,
                    ("variable", PropagatorsEnvVar), ("value", token), ("using", usingText));
            }

            if (none && (names.Count > 0 || skipped.Count > 0))
            {
                diagnostics.ReportOnce($"propagator-none:{text}", DiagnosticLevel.Warn, "\"none\" combined with other propagators; propagation disabled",
                    ("variable", PropagatorsEnvVar), ("value", text), ("using", "none"));
            }

            return effective;
        }
    }
}
