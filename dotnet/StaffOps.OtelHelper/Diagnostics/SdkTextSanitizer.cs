using System.Net;
using System.Text.Encodings.Web;
using System.Text.Json;
using System.Text.RegularExpressions;

namespace StaffOps.OtelHelper.Diagnostics
{
    /// <summary>
    /// What the SDK forwarder does to an SDK event before the sink writes it (design D-NET5,
    /// requirement DN3): forwarding makes text visible that 0.2 kept inside <c>EventSource</c>,
    /// so it must not leak. Drops an export response body, masks every configured secret
    /// literal (OTLP header values, endpoint userinfo and query values — contract D1 row "SDK
    /// error text", A29 masking rule), and tells which events are noise: the shutdown cancel of
    /// the Prometheus listener, and the SDK constructor's parse warnings for
    /// <c>OTEL_EXPORTER_OTLP_ENDPOINT</c>, <c>_PROTOCOL</c> and <c>_COMPRESSION</c>, which the
    /// helper resolves (and warns about) itself and assigns over the SDK's value (ADR-013).
    /// The SDK's warnings about the other OTLP variables (timeout, headers, certificates,
    /// temporality) are forwarded.
    /// </summary>
    internal static class SdkTextSanitizer
    {
        internal const string Masked = "<redacted>";
        internal const int MinMaskedSecret = 4;

        private static readonly TimeSpan RegexTimeout = TimeSpan.FromMilliseconds(250);

        private const string ResponseMarker = "Response: ";

        // The SDK's parse warnings for the rows the resolver resolves and warns about: the OTLP
        // exporter constructor's, and the reader options' interval and the SDK switch (task C9).
        private static readonly Regex HelperOwnedConfigurationWarning = new(
            @"^Configuration key '(?:OTEL_EXPORTER_OTLP_(?:(?:TRACES|METRICS|LOGS)_)?(?:ENDPOINT|PROTOCOL|COMPRESSION|TIMEOUT)|OTEL_METRIC_EXPORT_INTERVAL|OTEL_SDK_DISABLED|OTEL_EXPORTER_PROMETHEUS_(?:PORT|HOST))' has an invalid value", RegexOptions.CultureInvariant, RegexTimeout);

        private static readonly string[] HeaderVariables = TelemetryResolver.HeaderVariables;

        private static readonly string[] EndpointVariables =
        {
            "OTEL_EXPORTER_OTLP_ENDPOINT", "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", "OTEL_EXPORTER_OTLP_METRICS_ENDPOINT", "OTEL_EXPORTER_OTLP_LOGS_ENDPOINT",
        };

        private static readonly HashSet<string> AuthSchemes = new(StringComparer.OrdinalIgnoreCase) { "bearer", "basic", "digest", "negotiate" };

        /// <summary>
        /// Whether an event is known noise: the Prometheus listener's cancel at shutdown, or an
        /// SDK parse warning for an OTLP variable whose value the helper resolves (and warns about) itself.
        /// </summary>
        internal static bool IsNoise(string source, string text, bool stopping = true, bool invalidContextExpected = false)
            => (invalidContextExpected && IsInvalidContextInjection(source, text))
                || (stopping && source.StartsWith("OpenTelemetry-Exporter-Prometheus", StringComparison.Ordinal)
                    && text.StartsWith("Canceled to export metrics", StringComparison.Ordinal)
                    && text.Contains("CanceledException", StringComparison.Ordinal))
                || SafeIsMatch(HelperOwnedConfigurationWarning, text)
                // A view asked for the drop (the helper's disabled metrics or its "off" view).
                || text.Contains("will be ignored. Reason: 'View configuration asks to drop this instrument", StringComparison.Ordinal);

        /// <summary>
        /// The API's "Failed to inject activity context … 'Invalid context'" line: expected with
        /// traces or the SDK disabled (C10 fix round 2), where a trace started here deliberately carries
        /// no context (the helper's pass-through propagator hands the inner one an invalid context).
        /// </summary>
        internal static bool IsInvalidContextInjection(string source, string text)
            => source == "OpenTelemetry-Api"
                && text.StartsWith("Failed to inject activity context", StringComparison.Ordinal)
                && text.Contains("'Invalid context'", StringComparison.Ordinal);

        /// <summary>
        /// The SDK's event 33 (<c>MetricInstrumentIgnored</c>) with the not-subscribed reason, for an
        /// instrument of one of <paramref name="quietMeters"/> — the forwarding provider's own
        /// meters while its helper metrics are off (an entry ending in <c>.*</c> covers its children,
        /// <c>&lt;ServiceName&gt;.*</c>): the app records on the keyed service meter,
        /// which that provider does not subscribe then, by design. Any other reason is forwarded.
        /// </summary>
        internal static bool IsQuietMeterNotice(string? eventName, object?[] payload, IReadOnlyCollection<string> quietMeters)
            => quietMeters.Count > 0
                && eventName == "MetricInstrumentIgnored"
                && payload.Length >= 3
                && payload[1] is string meter && quietMeters.Any(q => q == meter || (q.EndsWith(".*", StringComparison.Ordinal) && meter.StartsWith(q[..^1], StringComparison.Ordinal)))
                && payload[2] is string reason && reason.Contains("not subscribed", StringComparison.OrdinalIgnoreCase);

        /// <summary>SDK parse warnings repeat once per provider; they are written once per sink.</summary>
        internal static string? OnceKey(string source, string text)
            => text.StartsWith("Configuration key '", StringComparison.Ordinal) ? "sdk:" + source + ":" + text : null;

        /// <summary>
        /// The event text with every configured secret masked. Body rule: the forwarder drops the
        /// OTLP response body by payload position (<see cref="SdkDiagnosticsForwarder.Render"/>,
        /// <paramref name="bodyHandled"/> true); in any other text, everything after
        /// <c>Response: </c> is cut (fail closed).
        /// </summary>
        internal static string SanitizeWithForms(string text, IReadOnlyList<string> forms, bool bodyHandled = false)
        {
            var at = text.IndexOf(ResponseMarker, StringComparison.Ordinal);
            if (at >= 0 && !bodyHandled)
            {
                text = text[..(at + ResponseMarker.Length)] + SdkDiagnosticsForwarder.ResponseBodyOmitted;
            }

            return MaskLiterals(text, forms);
        }

        /// <summary><see cref="SanitizeWithForms"/> with the configured secrets plus <paramref name="extraSecrets"/>.</summary>
        internal static string Sanitize(string text, IEnumerable<string> extraSecrets)
            => SanitizeWithForms(text, Forms(ConfiguredSecrets().Concat(extraSecrets)));

        /// <summary>
        /// Secret values configured in the environment, read at each call: header values (raw
        /// and percent-decoded, plus their whitespace-separated parts) and endpoint userinfo
        /// and query values.
        /// </summary>
        internal static IEnumerable<string> ConfiguredSecrets()
        {
            foreach (var variable in HeaderVariables)
            {
                foreach (var secret in HeaderSecrets(System.Environment.GetEnvironmentVariable(variable)))
                {
                    yield return secret;
                }
            }

            foreach (var variable in EndpointVariables)
            {
                foreach (var secret in EndpointSecrets(System.Environment.GetEnvironmentVariable(variable)))
                {
                    yield return secret;
                }
            }
        }

        /// <summary>The values of a <c>k=v,…</c> headers text, raw (trimmed) and percent-decoded.</summary>
        internal static IEnumerable<string> HeaderSecrets(string? raw)
        {
            if (string.IsNullOrEmpty(raw))
            {
                yield break;
            }

            foreach (var item in raw.Split(','))
            {
                var eq = item.IndexOf('=');
                if (eq < 0)
                {
                    continue;
                }

                var value = item[(eq + 1)..].Trim();
                yield return value;
                yield return Uri.UnescapeDataString(value);
            }
        }

        /// <summary>The userinfo parts and query values of an endpoint text (never parsed by Uri: it may be malformed).</summary>
        internal static IEnumerable<string> EndpointSecrets(string? endpoint)
        {
            if (string.IsNullOrWhiteSpace(endpoint))
            {
                yield break;
            }

            var text = endpoint.Trim();
            var start = text.IndexOf("://", StringComparison.Ordinal);
            var rest = start < 0 ? text : text[(start + 3)..];
            var at = rest.LastIndexOf('@');
            if (at > 0)
            {
                var userinfo = rest[..at];
                yield return userinfo;
                foreach (var part in userinfo.Split(new[] { ':', '#', '/', '?', ';', '&', '=' }, StringSplitOptions.RemoveEmptyEntries))
                {
                    yield return part;
                }
            }

            var query = rest.IndexOf('?');
            if (query >= 0)
            {
                foreach (var pair in rest[(query + 1)..].Split('&', '#'))
                {
                    var eq = pair.IndexOf('=');
                    if (eq >= 0)
                    {
                        yield return pair[(eq + 1)..];
                    }
                }
            }
        }

        /// <summary>
        /// Every masked form of <paramref name="secrets"/> (A29 rule): for each value and each of
        /// its whitespace-separated parts of at least <see cref="MinMaskedSecret"/> characters
        /// (a leading auth-scheme name is not a part) — the text, JSON-escaped once and twice,
        /// JSON with Go's HTML escaping (<c>\u0026</c>, <c>\u003c</c>, <c>\u003e</c>) and that form
        /// JSON-escaped once more, form-encoded and percent-encoded. Longest first.
        /// </summary>
        internal static IReadOnlyList<string> Forms(IEnumerable<string> secrets)
        {
            var forms = new HashSet<string>(StringComparer.Ordinal);
            foreach (var secret in secrets)
            {
                var parts = secret.Split((char[]?)null, StringSplitOptions.RemoveEmptyEntries).ToList();
                if (parts.Count > 1 && AuthSchemes.Contains(parts[0]))
                {
                    parts.RemoveAt(0);
                }

                foreach (var value in parts.Prepend(secret))
                {
                    if (value.Length < MinMaskedSecret)
                    {
                        continue;
                    }

                    var once = JsonEscape(value);
                    var html = once.Replace("&", "\\u0026", StringComparison.Ordinal)
                        .Replace("<", "\\u003c", StringComparison.Ordinal)
                        .Replace(">", "\\u003e", StringComparison.Ordinal);
                    foreach (var form in new[] { value, once, JsonEscape(once), html, JsonEscape(html), WebUtility.UrlEncode(value), Uri.EscapeDataString(value) })
                    {
                        if (form.Length > 0)
                        {
                            forms.Add(form);
                        }
                    }
                }
            }

            return forms.OrderByDescending(f => f.Length).ThenBy(f => f, StringComparer.Ordinal).ToList();
        }

        /// <summary>Every occurrence of every form is marked, overlapping ones included; each marked run becomes one <see cref="Masked"/>.</summary>
        internal static string MaskLiterals(string text, IReadOnlyList<string> forms)
        {
            if (forms.Count == 0 || text.Length == 0)
            {
                return text;
            }

            bool[]? marked = null;
            foreach (var form in forms)
            {
                for (var at = text.IndexOf(form, StringComparison.Ordinal); at >= 0; at = text.IndexOf(form, at + 1, StringComparison.Ordinal))
                {
                    marked ??= new bool[text.Length];
                    Array.Fill(marked, true, at, form.Length);
                }
            }

            if (marked is null)
            {
                return text;
            }

            var builder = new System.Text.StringBuilder(text.Length);
            for (var i = 0; i < text.Length;)
            {
                if (marked[i])
                {
                    builder.Append(Masked);
                    while (i < text.Length && marked[i])
                    {
                        i++;
                    }
                }
                else
                {
                    builder.Append(text[i]);
                    i++;
                }
            }

            return builder.ToString();
        }

        private static string JsonEscape(string value)
        {
            var quoted = JsonSerializer.Serialize(value, new JsonSerializerOptions { Encoder = JavaScriptEncoder.UnsafeRelaxedJsonEscaping });
            return quoted[1..^1];
        }

        private static bool SafeIsMatch(Regex regex, string text)
        {
            try
            {
                return regex.IsMatch(text);
            }
            catch (RegexMatchTimeoutException)
            {
                return false;
            }
        }
    }
}
