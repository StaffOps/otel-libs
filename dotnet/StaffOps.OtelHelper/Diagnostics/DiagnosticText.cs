using System.Globalization;
using System.Text;
using System.Text.RegularExpressions;

namespace StaffOps.OtelHelper.Diagnostics
{
    /// <summary>
    /// Text rules every helper diagnostic follows (design D-NET5, contract D1 rows
    /// "Echoed values are truncated", "Opaque-scheme rendering", "Rendering when the
    /// path or query holds @"): values are cut, escaped so they cannot forge a line,
    /// and URLs lose userinfo, query and fragment. Never pass a header or key value.
    /// </summary>
    internal static class DiagnosticText
    {
        /// <summary>Longest echoed value, in code points, before it is cut with a trailing <c>…</c>.</summary>
        internal const int MaxEchoedValue = 64;

        /// <summary>Characters of a free text (an SDK message) that are scanned and kept.</summary>
        internal const int ScanLimit = 4096;

        internal const string UnparseableUrl = "<unparseable URL>";
        internal const string SchemelessAtRendering = "<schemeless endpoint>";

        private static readonly TimeSpan RegexTimeout = TimeSpan.FromMilliseconds(250);

        /// <summary><paramref name="value"/> cut to <see cref="MaxEchoedValue"/> code points plus <c>…</c> when longer.</summary>
        internal static string Truncate(string value)
        {
            if (value.Length <= MaxEchoedValue)
            {
                return value;
            }

            var builder = new StringBuilder();
            var count = 0;
            foreach (var rune in value.EnumerateRunes())
            {
                if (count == MaxEchoedValue)
                {
                    return builder.Append('…').ToString();
                }

                builder.Append(rune.ToString());
                count++;
            }

            return value;
        }

        /// <summary>
        /// True for a character that could forge, split or visually reorder a record: C0 and C1
        /// controls, DEL, U+2028 and U+2029, and the bidirectional format characters
        /// (U+061C, U+200E, U+200F, U+202A–U+202E, U+2066–U+2069).
        /// </summary>
        internal static bool IsLineBreaking(char c)
            => c < 0x20 || (c >= 0x7f && c <= 0x9f) || c == (char)0x2028 || c == (char)0x2029
                || c == (char)0x061C || c == (char)0x200E || c == (char)0x200F || (c >= (char)0x202A && c <= (char)0x202E) || (c >= (char)0x2066 && c <= (char)0x2069);

        /// <summary>
        /// An attribute value as written in a record: as is when it holds nothing but
        /// printable non-space characters, else JSON-quoted with every control character,
        /// DEL, C1, U+2028 and U+2029 escaped as <c>\uXXXX</c>.
        /// </summary>
        internal static string FormatValue(string value)
        {
            var needsQuotes = value.Length == 0;
            foreach (var c in value)
            {
                if (IsLineBreaking(c) || char.IsWhiteSpace(c) || c == '"' || c == '=' || c == '\\')
                {
                    needsQuotes = true;
                    break;
                }
            }

            if (!needsQuotes)
            {
                return value;
            }

            var builder = new StringBuilder(value.Length + 2).Append('"');
            foreach (var c in value)
            {
                switch (c)
                {
                    case '"':
                        builder.Append("\\\"");
                        break;
                    case '\\':
                        builder.Append("\\\\");
                        break;
                    case '\n':
                        builder.Append("\\n");
                        break;
                    case '\r':
                        builder.Append("\\r");
                        break;
                    case '\t':
                        builder.Append("\\t");
                        break;
                    default:
                        if (IsLineBreaking(c))
                        {
                            builder.Append("\\u").Append(((int)c).ToString("x4", CultureInfo.InvariantCulture));
                        }
                        else
                        {
                            builder.Append(c);
                        }

                        break;
                }
            }

            return builder.Append('"').ToString();
        }

        /// <summary>A free text (message) with every line-breaking character escaped as <c>\uXXXX</c>.</summary>
        internal static string EscapeInline(string text)
        {
            var clean = true;
            foreach (var c in text)
            {
                if (IsLineBreaking(c))
                {
                    clean = false;
                    break;
                }
            }

            if (clean)
            {
                return text;
            }

            var builder = new StringBuilder(text.Length + 8);
            foreach (var c in text)
            {
                if (IsLineBreaking(c))
                {
                    builder.Append("\\u").Append(((int)c).ToString("x4", CultureInfo.InvariantCulture));
                }
                else
                {
                    builder.Append(c);
                }
            }

            return builder.ToString();
        }

        /// <summary>A value echoed from the environment or code: redacted, cut, then escaped (quoted when needed).</summary>
        internal static string Echo(string value) => FormatValue(Truncate(RedactText(value)));

        // ── URL rendering ──────────────────────────────────────────────────

        /// <summary>
        /// Endpoint text safe for diagnostics (<see cref="EndpointGrammar.Render"/> without a
        /// protocol): scheme, host, port and path only (no userinfo, query or fragment); an opaque
        /// value renders as <c>&lt;scheme&gt;:</c>; only <c>&lt;scheme&gt;://</c> (or
        /// <see cref="SchemelessAtRendering"/>) when the path or query holds an <c>@</c> or
        /// <c>%40</c>; <see cref="UnparseableUrl"/> for anything the resolver cannot parse.
        /// </summary>
        internal static string RenderUrl(string raw) => EndpointGrammar.Render(raw);

        // ── free-text redaction (SDK messages) ─────────────────────────────

        // A URL in free text: the scheme and the rest up to whitespace or a quote.
        private static readonly Regex UrlInText = new(@"(?<![A-Za-z0-9])([a-z][a-z0-9+.-]{0,31}://)([^\s""'<>]*)", RegexOptions.IgnoreCase | RegexOptions.CultureInvariant, RegexTimeout);

        // Schemeless "user:password@host".
        private static readonly Regex SchemelessUserinfo = new(@"(?<![^\s""'(=,;])[^\s/@:""']{1,256}:[^\s/@]{1,256}@", RegexOptions.CultureInvariant, RegexTimeout);

        private static readonly Regex Bearer = new(@"(?<![A-Za-z0-9])(bearer)(\s*[:=]?\s*)[A-Za-z0-9._~+/=-]+", RegexOptions.IgnoreCase | RegexOptions.CultureInvariant, RegexTimeout);

        private static readonly Regex Basic = new(@"(?<![A-Za-z0-9])(basic)(\s+)[A-Za-z0-9+/]{8,}={0,2}(?![A-Za-z0-9+/=])", RegexOptions.IgnoreCase | RegexOptions.CultureInvariant, RegexTimeout);

        // "name: value" / "name=value" whose name looks like a credential (plain, quoted or JSON-escaped).
        private static readonly Regex SecretPair = new(
            @"(?<![A-Za-z0-9])((?:\\*[""'])?)([\w-]{0,64}(?:key|token|secret|passw(?:or)?d|pwd|jwt|auth|cookie|credential|session|signature)[\w-]{0,64})\1(\s*[:=]\s*)(\\*""[^""]*\\*""|'[^']*'|[^\s,;&]+)",
            RegexOptions.IgnoreCase | RegexOptions.CultureInvariant,
            RegexTimeout);

        // A .NET exception or error type name: capital first letter, letters and digits only.
        private static readonly Regex TypeName = new("^[A-Z][A-Za-z0-9]*(Exception|Error)$", RegexOptions.CultureInvariant, RegexTimeout);

        // ": " — a colon and whitespace, never '='.
        private static readonly Regex ProseSeparator = new(@"^:\s+$", RegexOptions.CultureInvariant, RegexTimeout);

        // A message that starts like prose ("Cannot …", "The …").
        private static readonly Regex ProseStart = new(@"^[A-Z][a-z]+\b", RegexOptions.CultureInvariant, RegexTimeout);

        /// <summary>
        /// An exception message such as "AuthenticationException: Cannot determine the frame size"
        /// is not a credential pair. Spared only when all three hold: the name is a type name
        /// (<see cref="TypeName"/>: no <c>-</c> or <c>_</c>), the separator is <c>:</c> plus
        /// whitespace, and the value starts like prose. Anything else is masked, so
        /// <c>TokenError: eyJ…</c>, <c>AuthError=x</c> and <c>X-Auth-Error: x</c> stay redacted.
        /// Go and Python mask only the configured header values, not names.
        /// </summary>
        private static bool IsExceptionMessage(Match m)
            => TypeName.IsMatch(m.Groups[2].Value) && ProseSeparator.IsMatch(m.Groups[3].Value) && ProseStart.IsMatch(m.Groups[4].Value);

        /// <summary>
        /// <paramref name="text"/> with credentials removed: URL userinfo, query and fragment
        /// (and a path holding <c>@</c>), schemeless <c>user:pw@</c>, bearer/basic tokens and
        /// the values of credential-like <c>name: value</c> pairs (<c>&lt;redacted&gt;</c>).
        /// Only the first <see cref="ScanLimit"/> characters are scanned and kept.
        /// </summary>
        internal static string RedactText(string text)
        {
            var scanned = text.Length > ScanLimit ? text[..ScanLimit] : text;
            try
            {
                var urls = UrlInText.Replace(scanned, m => m.Groups[1].Value + RedactAuthority(m.Groups[2].Value));
                var users = SchemelessUserinfo.Replace(urls, "");
                var tokens = Bearer.Replace(users, m => m.Groups[1].Value + (m.Groups[2].Value.Length == 0 ? " " : m.Groups[2].Value) + "<redacted>");
                tokens = Basic.Replace(tokens, m => m.Groups[1].Value + m.Groups[2].Value + "<redacted>");
                return SecretPair.Replace(tokens, m => IsExceptionMessage(m)
                    ? m.Value
                    : m.Groups[1].Value + m.Groups[2].Value + m.Groups[1].Value + m.Groups[3].Value + "<redacted>");
            }
            catch (RegexMatchTimeoutException)
            {
                // Fail closed: a text that cannot be scanned in time is not echoed.
                return "<diagnostic text omitted>";
            }
        }

        /// <summary>The part of a URL after <c>scheme://</c> without userinfo, query and fragment (a path holding <c>@</c> is dropped).</summary>
        private static string RedactAuthority(string rest)
        {
            var lead = rest.StartsWith('/') ? "/" : "";
            var body = rest[lead.Length..];
            var end = body.IndexOfAny(new[] { '/', '?', '#' });
            var authority = end < 0 ? body : body[..end];
            var tail = end < 0 ? "" : body[end..];
            var host = authority.Contains('@') ? authority[(authority.LastIndexOf('@') + 1)..] : authority;
            var cut = tail.IndexOfAny(new[] { '?', '#' });
            var path = cut < 0 ? tail : tail[..cut];
            return lead + host + (path.Contains('@') ? "" : path);
        }
    }
}
