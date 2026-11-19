using System.Globalization;
using System.Net;
using System.Net.Sockets;
using System.Text.RegularExpressions;
using StaffOps.OtelHelper.Diagnostics;

namespace StaffOps.OtelHelper
{
    /// <summary>
    /// An endpoint split by RFC 3986 (contract design D1 ruling "Endpoint parsing follows RFC 3986,
    /// not WHATWG"): the authority ends only at <c>/</c>, <c>?</c> or <c>#</c>; userinfo is cut at
    /// its last <c>@</c>. Nothing is decoded or normalized except the scheme (lowercased) and the
    /// port (numeric, leading zeros removed).
    /// </summary>
    /// <param name="Scheme">The scheme, lowercased.</param>
    /// <param name="Userinfo">Whether the authority holds an <c>@</c>.</param>
    /// <param name="Host">The host as written (IPv6 in brackets); may be empty.</param>
    /// <param name="Port">The port in decimal without leading zeros (1–65535), empty when none is written.</param>
    /// <param name="Path">The path as written (escapes verbatim), empty when none.</param>
    /// <param name="HasQuery">A <c>?</c> is present, even with nothing after it.</param>
    /// <param name="HasFragment">A <c>#</c> is present, even with nothing after it.</param>
    /// <param name="Query">The text after <c>?</c> (before any <c>#</c>).</param>
    internal readonly record struct RfcEndpoint(
        string Scheme, bool Userinfo, string Host, string Port, string Path, bool HasQuery, bool HasFragment, string Query);

    /// <summary>
    /// The endpoint grammar of the contract (design D1 edge-case rulings, A17): the RFC 3986
    /// split, numeric ports, the host rules (characters, IDN, one host rule) and the rendering
    /// used by every diagnostic. Same semantics as Go <c>endpoint_parse.go</c> and Python
    /// <c>config.py</c>.
    /// </summary>
    internal static class EndpointGrammar
    {
        private const int MaxPort = 65535;

        private static readonly TimeSpan RegexTimeout = TimeSpan.FromSeconds(1);

        /// <summary><c>&lt;scheme&gt;://</c>, the only URL form an endpoint may take.</summary>
        private static readonly Regex HierUrl = new("^([A-Za-z][A-Za-z0-9+.-]*)://", RegexOptions.CultureInvariant, RegexTimeout);

        /// <summary><c>&lt;scheme&gt;:/…</c> without <c>//</c> (<c>unix:/x</c>, <c>collector:/v1</c>): an unsupported scheme, not a host.</summary>
        internal static readonly Regex OpaqueScheme = new("^[A-Za-z][A-Za-z0-9+.-]*:/", RegexOptions.CultureInvariant, RegexTimeout);

        /// <summary>A WHATWG IPv4 number in hex form (<c>0x</c>, <c>0x7f</c>).</summary>
        private static readonly Regex HexNumber = new("^0[xX][0-9A-Fa-f]*$", RegexOptions.CultureInvariant, RegexTimeout);

        /// <summary>A dotted quad, each octet 0–255 written without leading zeros.</summary>
        private static readonly Regex CanonicalIPv4 = new(
            @"^(25[0-5]|2[0-4][0-9]|1[0-9][0-9]|[1-9]?[0-9])(\.(25[0-5]|2[0-4][0-9]|1[0-9][0-9]|[1-9]?[0-9])){3}$",
            RegexOptions.CultureInvariant, RegexTimeout);

        /// <summary>
        /// Whether <see cref="IdnMapping"/> applies UTS #46 (the runtime has ICU or the Windows IDN
        /// API). In globalization-invariant mode it only punycode-encodes, without the mapping or the
        /// validity checks (measured on .NET 8: ZWJ, U+FFFD and fullwidth digits accepted, <c>Ü</c>
        /// not lowercased), so a non-ASCII host is then invalid: the helper never dials a host the
        /// other languages would convert differently. Probed once: UTS #46 maps <c>Ü</c> to <c>ü</c>.
        /// </summary>
        internal static readonly bool Uts46Available = ProbeUts46();

        private static bool ProbeUts46()
        {
            try
            {
                return new IdnMapping().GetAscii("\u00DC") == "xn--tda";
            }
            catch (Exception)
            {
                // Any failure (a missing native library included) means no UTS #46: a static
                // initializer that throws would break every AddOtelHelper call.
                return false;
            }
        }

        /// <summary>A byte below 0x20 or 0x7F (contract row "Endpoint env value with … control characters").</summary>
        internal static bool IsControl(char c) => c < 0x20 || c == 0x7f;

        internal static bool HasControl(string s)
        {
            foreach (var c in s)
            {
                if (IsControl(c))
                {
                    return true;
                }
            }

            return false;
        }

        /// <summary>
        /// Splits <paramref name="text"/> (<c>scheme://…</c>). False when there is no
        /// <c>scheme://</c> or the authority is malformed: unbalanced IPv6 brackets, text after
        /// <c>]</c> that is not a port, a port that is not 1–65535 (port 0 included).
        /// </summary>
        internal static bool TrySplit(string text, out RfcEndpoint endpoint)
        {
            endpoint = default;
            var match = HierUrl.Match(text);
            if (!match.Success)
            {
                return false;
            }

            var scheme = match.Groups[1].Value.ToLowerInvariant();
            var rest = text[match.Length..];
            var end = rest.IndexOfAny(new[] { '/', '?', '#' });
            if (end < 0)
            {
                end = rest.Length;
            }

            var authority = rest[..end];
            var after = rest[end..];
            var hasFragment = false;
            var fragmentAt = after.IndexOf('#');
            if (fragmentAt >= 0)
            {
                hasFragment = true;
                after = after[..fragmentAt];
            }

            var hasQuery = false;
            var query = "";
            var queryAt = after.IndexOf('?');
            if (queryAt >= 0)
            {
                hasQuery = true;
                query = after[(queryAt + 1)..];
                after = after[..queryAt];
            }

            var userinfo = false;
            var hostPort = authority;
            var at = authority.LastIndexOf('@');
            if (at >= 0)
            {
                userinfo = true;
                hostPort = authority[(at + 1)..];
            }

            string host;
            var portText = "";
            if (hostPort.StartsWith('['))
            {
                var closing = hostPort.IndexOf(']');
                if (closing < 0)
                {
                    return false;
                }

                host = hostPort[..(closing + 1)];
                var tail = hostPort[(closing + 1)..];
                if (tail.Length > 0)
                {
                    if (tail[0] != ':')
                    {
                        return false;
                    }

                    portText = tail[1..];
                }
            }
            else
            {
                var colon = hostPort.IndexOf(':');
                host = colon < 0 ? hostPort : hostPort[..colon];
                portText = colon < 0 ? "" : hostPort[(colon + 1)..];
            }

            // An empty port ("h:") is the scheme default, as RFC 3986 allows.
            var port = "";
            if (portText.Length > 0 && !TryParsePort(portText, out port))
            {
                return false;
            }

            endpoint = new RfcEndpoint(scheme, userinfo, host, port, after, hasQuery, hasFragment, query);
            return true;
        }

        /// <summary>
        /// A port compared numerically (D1 ruling: <c>04318</c> = 4318): ASCII digits only,
        /// leading zeros dropped, then 1–65535. Never converts the whole text, so any length is safe.
        /// </summary>
        internal static bool TryParsePort(string text, out string port)
        {
            port = "";
            foreach (var c in text)
            {
                if (c is < '0' or > '9')
                {
                    return false;
                }
            }

            var digits = text.TrimStart('0');
            if (digits.Length is 0 or > 5)
            {
                return false;
            }

            var value = int.Parse(digits, NumberStyles.None, CultureInfo.InvariantCulture); // ≤ 5 ASCII digits
            if (value > MaxPort)
            {
                return false;
            }

            port = digits;
            return true;
        }

        /// <summary>
        /// Validates the host of an http(s) endpoint and returns the spelling that is dialed and
        /// rendered (D1 rulings "Host characters", IDN hosts, "One host rule"): an empty host, a
        /// space or a <c>%</c> (a percent-encoded byte, or an unescaped IPv6 zone id: then
        /// <paramref name="zoneId"/> is true) is invalid; an IPv6 literal must parse; a non-ASCII
        /// host is converted by UTS #46 non-transitional (<see cref="IdnMapping"/>) and must keep
        /// a valid length; the ASCII result must be an RFC 3986 reg-name, and one that ends in a
        /// number must be a canonical dotted quad. Hosts are lowercased.
        /// </summary>
        internal static bool CheckHost(string host, out string normalized, out bool zoneId)
            => CheckHost(host, out normalized, out zoneId, Uts46Available);

        /// <inheritdoc cref="CheckHost(string, out string, out bool)"/>
        /// <param name="host">The host as written.</param>
        /// <param name="normalized">The host that is dialed and rendered.</param>
        /// <param name="zoneId">True when the host is invalid because of an unescaped IPv6 zone id.</param>
        /// <param name="uts46">Whether UTS #46 is available (<see cref="Uts46Available"/>); without it a non-ASCII host is invalid.</param>
        internal static bool CheckHost(string host, out string normalized, out bool zoneId, bool uts46)
        {
            normalized = "";
            zoneId = false;
            if (host.Length == 0 || host.Contains(' '))
            {
                return false;
            }

            if (host.Contains('%'))
            {
                zoneId = host.StartsWith('[');
                return false;
            }

            if (host.StartsWith('['))
            {
                var inner = host[1..^1];
                if (!inner.Contains(':') || !IPAddress.TryParse(inner, out var address) || address.AddressFamily != AddressFamily.InterNetworkV6)
                {
                    return false;
                }

                normalized = host.ToLowerInvariant();
                return true;
            }

            var ascii = host;
            if (!IsAscii(host))
            {
                if (!uts46)
                {
                    return false;
                }

                try
                {
                    ascii = new IdnMapping { AllowUnassigned = false, UseStd3AsciiRules = false }.GetAscii(host);
                }
                catch (ArgumentException)
                {
                    return false;
                }

                if (!ValidDomainLength(ascii))
                {
                    return false;
                }
            }

            foreach (var c in ascii)
            {
                if (!IsRegNameChar(c))
                {
                    return false;
                }
            }

            // One trailing '.' is allowed on a dotted quad ("10.0.0.1."), as for a domain.
            if (EndsInNumber(ascii) && !CanonicalIPv4.IsMatch(ascii.EndsWith('.') ? ascii[..^1] : ascii))
            {
                return false;
            }

            // .NET difference (C7a fix round 1, measured on .NET 8): System.Uri, which the exporter
            // needs, refuses hosts with sub-delims other than '-' and '_' ("a!b", "a~b") and empty
            // labels ("a..b", "1.2.3.4.."), which RFC 3986 and Go accept. Rejected here, so the
            // grammar's warning is the only one and no value reaches the exporter it cannot use.
            if (!Uri.TryCreate("http://" + ascii + "/", UriKind.Absolute, out _))
            {
                return false;
            }

            normalized = ascii.ToLowerInvariant();
            return true;
        }

        internal static bool IsAscii(string s)
        {
            foreach (var c in s)
            {
                if (c > 0x7f)
                {
                    return false;
                }
            }

            return true;
        }

        /// <summary>An RFC 3986 reg-name character (unreserved or sub-delims); pct-encoded is rejected before.</summary>
        private static bool IsRegNameChar(char c)
            => char.IsAsciiLetterOrDigit(c) || "-._~!$&'()*+,;=".Contains(c);

        /// <summary>
        /// WHATWG "ends in a number": the last label (a trailing empty label ignored) is decimal
        /// digits or a hex number, which WHATWG would read as an IPv4 address (127.1, 0x7f.0.0.1).
        /// </summary>
        private static bool EndsInNumber(string host)
        {
            var labels = host.Split('.');
            var count = labels.Length;
            if (count > 1 && labels[count - 1].Length == 0)
            {
                count--;
            }

            var last = labels[count - 1];
            if (last.Length == 0)
            {
                return false;
            }

            return HexNumber.IsMatch(last) || last.All(char.IsAsciiDigit);
        }

        /// <summary>A converted IDN host (one trailing '.' ignored) of at most 253 characters with no empty label.</summary>
        private static bool ValidDomainLength(string ascii)
        {
            var trimmed = ascii.EndsWith('.') ? ascii[..^1] : ascii;
            return trimmed.Length is > 0 and <= 253 && trimmed.Split('.').All(label => label.Length > 0);
        }

        /// <summary>An <c>@</c>, raw or percent-encoded (<c>%40</c>).</summary>
        internal static bool HasAt(string s) => s.Contains('@') || s.Contains("%40", StringComparison.OrdinalIgnoreCase);

        /// <summary>An <c>@</c> (or <c>%40</c>) in the path or query (D1 ruling: warned, used as parsed).</summary>
        internal static bool PathHasAt(RfcEndpoint endpoint) => HasAt(endpoint.Path) || HasAt(endpoint.Query);

        /// <summary>A <c>%</c> not followed by two hex digits (Go <c>url.PathUnescape</c> fails on it).</summary>
        internal static bool HasInvalidEscape(string path)
        {
            for (var i = 0; i < path.Length; i++)
            {
                if (path[i] == '%' && (i + 2 >= path.Length || !char.IsAsciiHexDigit(path[i + 1]) || !char.IsAsciiHexDigit(path[i + 2])))
                {
                    return true;
                }
            }

            return false;
        }

        /// <summary>
        /// Renders an endpoint for diagnostics (for an unsupported scheme such as <c>ftp://</c>
        /// the host is shown as written, without the http(s) host checks; userinfo, query and
        /// fragment are still dropped): scheme, host, port and path only (no userinfo,
        /// query or fragment), split exactly as the resolver splits it. Only <c>&lt;scheme&gt;://</c>
        /// when the path or query holds an <c>@</c> or <c>%40</c> (<see cref="DiagnosticText.SchemelessAtRendering"/>
        /// for a schemeless value); <c>&lt;scheme&gt;:</c> for an opaque value; the constant
        /// <see cref="DiagnosticText.UnparseableUrl"/> for anything the resolver rejects as
        /// unparseable — with <paramref name="protocol"/> <c>http/protobuf</c>, an invalid path
        /// escape too. A value with a line-breaking character is never rendered.
        /// </summary>
        internal static string Render(string raw, string? protocol = null)
        {
            var s = ContractText.Trim(raw);
            if (s.Length == 0 || HasControl(s) || s.Contains('\\') || s.Any(DiagnosticText.IsLineBreaking))
            {
                return DiagnosticText.UnparseableUrl;
            }

            var schemeless = !s.Contains("://", StringComparison.Ordinal);
            if (schemeless && OpaqueScheme.IsMatch(s))
            {
                // "unix:/path": no authority, and the rest may hold credentials (D1 ruling, A17).
                return s[..s.IndexOf(':')].ToLowerInvariant() + ":";
            }

            if (schemeless && s.StartsWith('/'))
            {
                return DiagnosticText.UnparseableUrl;
            }

            if (!TrySplit(schemeless ? "https://" + s : s, out var sp))
            {
                return DiagnosticText.UnparseableUrl;
            }

            var host = sp.Host;
            if (sp.Scheme is "http" or "https")
            {
                if (!CheckHost(sp.Host, out host, out _))
                {
                    return DiagnosticText.UnparseableUrl;
                }
            }
            else if (host.Length == 0)
            {
                return DiagnosticText.UnparseableUrl;
            }

            if (protocol == TelemetryOptions.ProtocolHttpProtobuf && HasInvalidEscape(sp.Path))
            {
                return DiagnosticText.UnparseableUrl;
            }

            if (PathHasAt(sp))
            {
                return schemeless ? DiagnosticText.SchemelessAtRendering : sp.Scheme + "://";
            }

            if (sp.Port.Length > 0)
            {
                host += ":" + sp.Port;
            }

            return schemeless ? host + sp.Path : sp.Scheme + "://" + host + sp.Path;
        }

        /// <summary>The port of an endpoint, schemeless or URL, as the resolver reads it (empty when none or unparseable) — for the port-4318 inference.</summary>
        internal static string EndpointPort(string raw)
        {
            var s = ContractText.Trim(raw);
            if (HasControl(s) || s.Contains('\\'))
            {
                return "";
            }

            if (!s.Contains("://", StringComparison.Ordinal))
            {
                s = "scheme://" + s;
            }

            return TrySplit(s, out var sp) ? sp.Port : "";
        }
    }
}
