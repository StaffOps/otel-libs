using System.Diagnostics;

namespace StaffOps.OtelHelper.Tracing
{
    /// <summary>
    /// The URL rule of the sanitizing processor (task C14c; contract A28 and its A30 amendment,
    /// design D-NET14 and owner decision O3). Upstream redacts the query first and the helper keeps
    /// that text: the ASP.NET Core and HttpClient contrib instrumentations write every value as
    /// <c>Redacted</c> (server <c>url.query</c>; client <c>url.full</c> on the .NET 8 runtime), and the
    /// .NET 9+ runtime writes the client query as <c>*</c> (measured 2026-11-11). This rule covers what
    /// is left when the application turns upstream redaction off
    /// (<c>OTEL_DOTNET_EXPERIMENTAL_HTTPCLIENT_DISABLE_URL_QUERY_REDACTION</c>,
    /// <c>OTEL_DOTNET_EXPERIMENTAL_ASPNETCORE_DISABLE_URL_QUERY_REDACTION</c>,
    /// <c>DOTNET_SYSTEM_NET_HTTP_DISABLEURIREDACTION</c> or the <c>System.Net.Http.DisableUriRedaction</c>
    /// switch), and any other source writing these keys: the values of the <see cref="Names"/> become
    /// <c>REDACTED</c> in <c>url.full</c>, <c>http.url</c>, <c>url.query</c> and <c>http.target</c>.
    /// Names match exactly (case-sensitive) after percent-decoding the name, as Go, Python and
    /// Node.js; a name without <c>=</c> has no value and is kept; every other byte of the value is
    /// kept as written. A value that already is upstream's <c>Redacted</c> is left alone, so the text
    /// of an enabled upstream redaction is unchanged.
    /// </summary>
    internal static class UrlQueryRedaction
    {
        /// <summary>The contract list (A28 seven, A30 three more), the same in every language.</summary>
        internal static readonly string[] Names =
        {
            "sig", "Signature", "AWSAccessKeyId", "X-Goog-Signature", "X-Amz-Signature", "X-Amz-Credential",
            "X-Amz-Security-Token", "access_token", "X-Goog-Credential", "GoogleAccessId",
        };

        internal const string Redacted = "REDACTED";

        /// <summary>The placeholder the contrib instrumentations write (kept as upstream wrote it, O3).</summary>
        internal const string UpstreamRedacted = "Redacted";

        private static readonly HashSet<string> NameSet = new(Names, StringComparer.Ordinal);

        /// <summary>The URL attributes: the full URL keys keep a fragment, the target and query keys are query up to their end.</summary>
        private static readonly (string Key, bool Fragment)[] Keys =
        {
            ("url.full", true), ("http.url", true), ("http.target", false), ("url.query", false),
        };

        /// <summary>
        /// Rewrites the URL attributes of <paramref name="activity"/>; a tag is written only when its
        /// value changes. A non-string value (a <see cref="Uri"/>, any object exported through its
        /// <c>ToString()</c>) is redacted as its text and written back as a string only when the text
        /// changed (C14c fix round 1); numbers and booleans are left alone.
        /// </summary>
        internal static void Apply(Activity activity)
        {
            foreach (var (key, fragment) in Keys)
            {
                if (TextOf(activity.GetTagItem(key)) is { } value)
                {
                    var redacted = key == "url.query" ? RedactQueryValue(value) : RedactUrl(value, fragment);
                    if (!ReferenceEquals(redacted, value))
                    {
                        activity.SetTag(key, redacted);
                    }
                }
            }
        }

        /// <summary>The text a URL attribute is exported as: a string itself, else the <c>ToString()</c> of a non-primitive object; null for none, numbers and booleans.</summary>
        internal static string? TextOf(object? value) => value switch
        {
            null => null,
            string text => text,
            bool or char or byte or sbyte or short or ushort or int or uint or long or ulong or float or double or decimal => null,
            _ => value.ToString(),
        };

        /// <summary>A URL or target with its query rewritten (after the first <c>?</c>, up to <c>#</c> when <paramref name="keepFragment"/>); the same instance when nothing changes.</summary>
        internal static string RedactUrl(string url, bool keepFragment = true)
        {
            var question = url.IndexOf('?');
            if (question < 0)
            {
                return url;
            }

            var end = keepFragment ? url.IndexOf('#', question + 1) : -1;
            if (end < 0)
            {
                end = url.Length;
            }

            var query = url.Substring(question + 1, end - question - 1);
            var redacted = RedactQuery(query);
            return ReferenceEquals(redacted, query) ? url : string.Concat(url.AsSpan(0, question + 1), redacted, url.AsSpan(end));
        }

        /// <summary>A <c>url.query</c> value: the query, with or without a leading <c>?</c> (ASP.NET Core writes one).</summary>
        internal static string RedactQueryValue(string value)
        {
            if (value.StartsWith('?'))
            {
                var redacted = RedactQuery(value[1..]);
                return redacted == value[1..] ? value : "?" + redacted;
            }

            return RedactQuery(value);
        }

        /// <summary><paramref name="query"/> (no <c>?</c>) with the listed values replaced; the same instance when nothing changes.</summary>
        internal static string RedactQuery(string query)
        {
            if (query.Length == 0 || !MayHoldName(query))
            {
                return query;
            }

            var parts = query.Split('&');
            var changed = false;
            for (var i = 0; i < parts.Length; i++)
            {
                var part = parts[i];
                var eq = part.IndexOf('=');
                if (eq < 0)
                {
                    continue;
                }

                var name = part[..eq];
                var value = part[(eq + 1)..];
                if (value != UpstreamRedacted && value != Redacted && NameSet.Contains(Decode(name)))
                {
                    parts[i] = name + "=" + Redacted;
                    changed = true;
                }
            }

            return changed ? string.Join('&', parts) : query;
        }

        /// <summary>
        /// Cheap pre-check: whether <paramref name="query"/> can hold a listed name — one of them as
        /// written (case-sensitive), or a <c>%</c> escape that could spell one.
        /// </summary>
        private static bool MayHoldName(string query)
        {
            if (query.Contains('%'))
            {
                return true;
            }

            foreach (var name in Names)
            {
                if (query.Contains(name, StringComparison.Ordinal))
                {
                    return true;
                }
            }

            return false;
        }

        /// <summary>The percent-decoded name (a malformed escape stays as written; <c>+</c> is not a space).</summary>
        private static string Decode(string name) => name.Contains('%') ? Uri.UnescapeDataString(name) : name;
    }
}
