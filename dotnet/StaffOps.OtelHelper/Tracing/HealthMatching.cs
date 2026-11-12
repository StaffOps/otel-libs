using System.Text;
using Microsoft.AspNetCore.Http;
using Microsoft.AspNetCore.Http.Features;

namespace StaffOps.OtelHelper.Tracing
{
    /// <summary>
    /// Health matching of the contract (R7, design D5, D1 rows "Percent-encoded health paths" and
    /// "ASCII case folding"; .NET design D-NET8): an exact match of the normalized path against the
    /// normalized health list, plus the whole <c>grpc.health.v1.Health</c> service. Used by the
    /// ASP.NET Core server filter and by the HttpClient and gRPC client filters.
    /// </summary>
    internal static class HealthMatching
    {
        /// <summary>The contract default list (Go <c>DefaultHealthPaths</c>, Python, Node.js).</summary>
        internal static readonly string[] DefaultPaths = { "/health", "/healthz", "/ready", "/readyz", "/live", "/livez", "/ping", "/metrics" };

        /// <summary>The gRPC health service prefix, the method name without its leading <c>/</c>.</summary>
        internal const string GrpcHealthService = "grpc.health.v1.Health/";

        private static readonly UTF8Encoding StrictUtf8 = new(encoderShouldEmitUTF8Identifier: false, throwOnInvalidBytes: true);

        /// <summary>The normalized form of every path in <paramref name="paths"/> (ordinal set).</summary>
        internal static HashSet<string> PathSet(IEnumerable<string> paths)
            => new(paths.Select(Normalize), StringComparer.Ordinal);

        /// <summary>
        /// Query and fragment cut on the raw target, then the path percent-decoded and cut again at a
        /// decoded <c>?</c> or <c>#</c>, ASCII-lowercased, trailing slashes dropped (<c>/</c> stays).
        /// A malformed escape (or bytes that are not UTF-8) keeps the raw path.
        /// </summary>
        internal static string Normalize(string path) => AsciiLowerTrimmed(Decode(path));

        /// <summary>The path cut at <c>?</c>/<c>#</c> and percent-decoded (not case-folded).</summary>
        internal static string Decode(string path)
        {
            var p = CutQuery(path);
            if (p.Contains('%') && TryPercentDecode(p, out var decoded))
            {
                p = CutQuery(decoded);
            }

            return p;
        }

        /// <summary>Whether a request path names a <c>grpc.health.v1.Health</c> method (Check, Watch, List).</summary>
        internal static bool IsGrpcHealthMethod(string path)
        {
            var p = Decode(path);
            if (p.StartsWith('/'))
            {
                p = p[1..];
            }

            return p.StartsWith(GrpcHealthService, StringComparison.Ordinal);
        }

        /// <summary>Whether <paramref name="path"/> (raw, as sent or received) is a health request.</summary>
        internal static bool IsHealth(string path, HashSet<string> normalizedPaths)
            => normalizedPaths.Contains(Normalize(path)) || IsGrpcHealthMethod(path);

        /// <summary>
        /// Whether an inbound request is a health request. The path is the raw request target as the
        /// client sent it (Kestrel decodes <see cref="HttpRequest.Path"/> except <c>%2F</c>), normalized
        /// with one decoding; for a target that is not origin-form (absolute-form, <c>*</c>) it is
        /// <c>PathBase + Path</c>, which the server already decoded, so it is not decoded again
        /// (<c>http://host/%2568ealth</c> is the path <c>/%68ealth</c>, not <c>/health</c>).
        /// </summary>
        internal static bool IsServerHealth(HttpContext context, HashSet<string> normalizedPaths)
        {
            var raw = context.Features.Get<IHttpRequestFeature>()?.RawTarget;
            if (!string.IsNullOrEmpty(raw) && raw[0] == '/')
            {
                return IsHealth(raw, normalizedPaths);
            }

            var decoded = (context.Request.PathBase.Value ?? "") + (context.Request.Path.Value ?? "");
            return normalizedPaths.Contains(AsciiLowerTrimmed(decoded))
                || (decoded.StartsWith('/') ? decoded[1..] : decoded).StartsWith(GrpcHealthService, StringComparison.Ordinal);
        }

        private static volatile HashSet<string>? s_clientPaths;

        /// <summary>
        /// The normalized health list of the last container whose HttpClient instrumentation options were
        /// built (as the runtime propagator follows the last container); null before any.
        /// </summary>
        internal static HashSet<string>? ClientPaths
        {
            get => s_clientPaths;
            set => s_clientPaths = value;
        }

        /// <summary>
        /// Whether <paramref name="carrier"/> is an outgoing request to a health path of
        /// <see cref="ClientPaths"/>: the runtime propagator then writes no context on it (as Go's
        /// <c>NewHTTPTransport</c> bypasses otelhttp for a health path), so the callee never sees a
        /// parent span that is not exported. Matched on the request itself: a mark set by the
        /// instrumentation's filter was not seen at injection (measured, .NET 8).
        /// </summary>
        internal static bool IsClientHealthRequest(object? carrier)
            => carrier is HttpRequestMessage request && ClientPaths is { } paths && IsHealth(ClientPath(request), paths);

        /// <summary>
        /// Removes from an outgoing health request the trace context the propagators wrote for
        /// <paramref name="activity"/> (the client activity, current while the filter runs): a
        /// <c>traceparent</c> naming its trace and span id, a <c>Request-Id</c> equal to its id, a
        /// <c>tracestate</c> equal to its trace state. A value the application set itself is kept, and
        /// baggage is never touched (application data, it names no span). Go's transport bypass never
        /// touches the application's headers either.
        /// </summary>
        internal static void RemoveInjectedTraceContext(HttpRequestMessage request, System.Diagnostics.Activity? activity)
        {
            if (activity is null)
            {
                return;
            }

            var traceId = activity.TraceId.ToHexString();
            var spanId = activity.SpanId.ToHexString();
            RemoveValues(request, "traceparent", v => v.Split('-') is { Length: 4 } p && p[1] == traceId && p[2] == spanId);
            RemoveValues(request, "Request-Id", v => v == activity.Id);
            RemoveValues(request, "tracestate", v => !string.IsNullOrEmpty(activity.TraceStateString) && v == activity.TraceStateString);
        }

        private static void RemoveValues(HttpRequestMessage request, string header, Func<string, bool> injected)
        {
            if (!request.Headers.TryGetValues(header, out var values))
            {
                return;
            }

            var kept = values.Where(v => !injected(v.Trim())).ToList();
            request.Headers.Remove(header);
            foreach (var value in kept)
            {
                request.Headers.TryAddWithoutValidation(header, value);
            }
        }

        /// <summary>The path of an outgoing request as sent (escapes kept), without the query.</summary>
        internal static string ClientPath(HttpRequestMessage request)
            => request.RequestUri switch
            {
                null => "",
                { IsAbsoluteUri: true } uri => uri.AbsolutePath,
                var uri => uri.OriginalString,
            };

        private static string CutQuery(string path)
        {
            var cut = path.IndexOfAny(new[] { '?', '#' });
            return cut < 0 ? path : path[..cut];
        }

        private static string AsciiLowerTrimmed(string path)
        {
            var p = ContractText.AsciiLower(path);
            var end = p.Length;
            while (end > 1 && p[end - 1] == '/')
            {
                end--;
            }

            return p[..end];
        }

        /// <summary>
        /// Strict percent-decoding: every <c>%</c> starts two hex digits and each run of escapes is
        /// UTF-8 (as JavaScript's <c>decodeURIComponent</c>, the Node.js reference); literal characters are kept.
        /// </summary>
        private static bool TryPercentDecode(string path, out string decoded)
        {
            decoded = path;
            var text = new StringBuilder(path.Length);
            var run = new List<byte>();
            for (var i = 0; i < path.Length; i++)
            {
                if (path[i] != '%')
                {
                    text.Append(path[i]);
                    continue;
                }

                run.Clear();
                while (i < path.Length && path[i] == '%')
                {
                    if (i + 2 >= path.Length || !IsHex(path[i + 1]) || !IsHex(path[i + 2]))
                    {
                        return false;
                    }

                    run.Add((byte)((HexValue(path[i + 1]) << 4) | HexValue(path[i + 2])));
                    i += 3;
                }

                i--;
                try
                {
                    text.Append(StrictUtf8.GetString(run.ToArray()));
                }
                catch (DecoderFallbackException)
                {
                    return false;
                }
            }

            decoded = text.ToString();
            return true;
        }

        private static bool IsHex(char c) => c is >= '0' and <= '9' or >= 'a' and <= 'f' or >= 'A' and <= 'F';

        private static int HexValue(char c) => c <= '9' ? c - '0' : (c | 0x20) - 'a' + 10;
    }
}
