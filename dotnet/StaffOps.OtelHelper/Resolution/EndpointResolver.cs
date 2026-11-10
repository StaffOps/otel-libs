using StaffOps.OtelHelper.Diagnostics;

namespace StaffOps.OtelHelper
{
    /// <summary>One endpoint value to resolve for one signal (everything <see cref="EndpointResolver.Resolve"/> reads, so it stays pure).</summary>
    internal sealed record EndpointInput
    {
        /// <summary>The value as configured, trimmed with the contract set.</summary>
        public required string Raw { get; init; }

        /// <summary>The option or environment variable it came from (named in diagnostics).</summary>
        public required string Variable { get; init; }

        public required string Signal { get; init; }

        /// <summary><c>grpc</c> or <c>http/protobuf</c>, resolved for this signal and value.</summary>
        public required string Protocol { get; init; }

        public bool FromCode { get; init; }

        /// <summary>A per-signal value: an HTTP path is used as written (no <c>/v1/&lt;signal&gt;</c>).</summary>
        public bool PerSignal { get; init; }

        /// <summary>The effective INSECURE setting of the signal; null when none is set.</summary>
        public bool? Insecure { get; init; }

        public bool InsecureFromCode { get; init; }

        /// <summary>The option or variable the INSECURE setting came from.</summary>
        public string InsecureVariable { get; init; } = "";

        /// <summary>Whether IDN hosts can be converted (<see cref="EndpointGrammar.Uts46Available"/>; tests set it).</summary>
        public bool Uts46 { get; init; } = EndpointGrammar.Uts46Available;
    }

    /// <summary>Why a value was not used: unparseable (fall back to the next level) or an unsupported scheme (the signal is not configured).</summary>
    internal enum EndpointFailure
    {
        None,
        Invalid,
        Unsupported,
    }

    /// <summary>A configuration warning of one resolution; <see cref="OnceKey"/> dedupes it per sink (once per variable and value).</summary>
    internal readonly record struct EndpointWarning(string OnceKey, string Message, (string Key, string Value)[] Attributes);

    /// <summary>The result of resolving one endpoint value.</summary>
    internal sealed class EndpointOutcome
    {
        /// <summary>The URL passed to the exporter (<c>scheme://host[:port][path]</c>); null when the value was not used.</summary>
        public string? Url { get; init; }

        /// <summary><c>http</c> or <c>https</c>.</summary>
        public string Scheme { get; init; } = "";

        public EndpointFailure Failure { get; init; }

        /// <summary>The fail-fast message of an unusable code value (it names the option); null otherwise.</summary>
        public string? Error { get; init; }

        public IReadOnlyList<EndpointWarning> Warnings { get; init; } = Array.Empty<EndpointWarning>();
    }

    /// <summary>
    /// Resolves one endpoint value (contract design D1.b–c and the D1 edge-case rulings, A17):
    /// RFC 3986 split, schemeless <c>host[:port]</c> rewritten to <c>https://</c> (<c>http://</c>
    /// with INSECURE=true) and the protocol's default port, portless gRPC URLs to 4317 with a
    /// warning, INSECURE/scheme conflicts (scheme wins; a code contradiction fails), userinfo,
    /// query and fragment removed with a redacted warning, generic HTTP values get
    /// <c>/v1/&lt;signal&gt;</c>. An environment value the helper cannot use is warned and
    /// reported as a failure; a code value yields <see cref="EndpointOutcome.Error"/>. No value is
    /// ever echoed except through <see cref="EndpointGrammar.Render"/>.
    /// </summary>
    internal static class EndpointResolver
    {
        internal const string DefaultGrpcPort = "4317";
        internal const string DefaultHttpPort = "4318";

        internal const string MsgPortlessGrpc = "gRPC endpoint has no port; using 4317 - set an explicit port (e.g. :4317, or :443 behind an ingress)";
        internal const string MsgUserinfo = "endpoint userinfo/query is not supported and was removed; put credentials in OTEL_EXPORTER_OTLP_HEADERS";
        internal const string MsgAtInPath = "endpoint path contains '@' - misplaced credentials?";
        internal const string MsgSchemelessHttpDeprecated = "schemeless endpoint is deprecated for http/protobuf and will be removed in 1.0; use a URL (http:// or https://)";
        internal const string MsgInsecureConflict = "INSECURE setting contradicts the endpoint scheme; the scheme wins";
        internal const string MsgGrpcPathIgnored = "endpoint path is ignored by the gRPC exporter";

        internal static EndpointOutcome Resolve(EndpointInput input)
        {
            var raw = input.Raw;

            // Before parsing: a control character is unparseable, also in an opaque value (D1 ruling).
            if (EndpointGrammar.HasControl(raw))
            {
                return Fail(input, EndpointFailure.Invalid, "endpoint contains a control character");
            }

            // WHATWG reads '\' as '/' and would split the authority elsewhere (D1 ruling: RFC 3986).
            if (raw.Contains('\\'))
            {
                return Fail(input, EndpointFailure.Invalid, "endpoint contains a backslash");
            }

            var schemeless = !raw.Contains("://", StringComparison.Ordinal);
            if (schemeless && EndpointGrammar.OpaqueScheme.IsMatch(raw))
            {
                // "unix:/path", "ftp:/x": a scheme, not a host named "unix".
                return Fail(input, EndpointFailure.Unsupported, "unsupported endpoint scheme (use http:// or https://)");
            }

            if (schemeless && raw.StartsWith('/'))
            {
                return Fail(input, EndpointFailure.Invalid, "invalid endpoint");
            }

            var text = schemeless ? (input.Insecure == true ? "http://" : "https://") + raw : raw;
            if (!EndpointGrammar.TrySplit(text, out var sp))
            {
                return Fail(input, EndpointFailure.Invalid, "invalid endpoint");
            }

            if (sp.Scheme is not ("http" or "https"))
            {
                return Fail(input, EndpointFailure.Unsupported, "unsupported endpoint scheme (use http:// or https://)");
            }

            if (!EndpointGrammar.CheckHost(sp.Host, out var host, out var zoneId, input.Uts46))
            {
                var why = zoneId ? "IPv6 zone ids are not supported in endpoint URLs"
                    : !input.Uts46 && !EndpointGrammar.IsAscii(sp.Host) ? "an internationalized host needs ICU (globalization-invariant mode is on); write it in punycode (xn--...)"
                    : "invalid endpoint";
                return Fail(input, EndpointFailure.Invalid, why);
            }

            var http = input.Protocol == TelemetryOptions.ProtocolHttpProtobuf;
            if (http && EndpointGrammar.HasInvalidEscape(sp.Path))
            {
                return Fail(input, EndpointFailure.Invalid, "endpoint path has an invalid percent-escape");
            }

            var warnings = new List<EndpointWarning>();
            var shown = EndpointGrammar.Render(raw, input.Protocol);
            var atInPath = EndpointGrammar.PathHasAt(sp);

            // With an '@' in the path or query the "host:port" may be "user:password-prefix": only
            // "<scheme>://" is rendered (D1 ruling, A17 final round; R8).
            string Shown(string rest) => atInPath ? sp.Scheme + "://" : sp.Scheme + "://" + rest;
            string HostWith(string p) => p.Length == 0 ? host : host + ":" + p;

            var port = sp.Port;
            if (schemeless)
            {
                if (port.Length == 0)
                {
                    port = http ? DefaultHttpPort : DefaultGrpcPort;
                }

                if (http)
                {
                    warnings.Add(Warning(MsgSchemelessHttpDeprecated, input.Variable, raw, shown, Shown(HostWith(port) + sp.Path)));
                }
            }
            else
            {
                if (input.Insecure is { } insecure && insecure != (sp.Scheme == "http"))
                {
                    if (input.FromCode && input.InsecureFromCode)
                    {
                        return new EndpointOutcome
                        {
                            Failure = EndpointFailure.Invalid,
                            Error = $"{input.InsecureVariable} = {(insecure ? "true" : "false")} contradicts the {sp.Scheme}:// scheme of {input.Variable}; " +
                                    "remove TelemetryOptions.Insecure or change the scheme.",
                        };
                    }

                    var value = insecure ? "true" : "false";
                    warnings.Add(Warning(MsgInsecureConflict, input.InsecureVariable, value, value, sp.Scheme + "://"));
                }

                if (!http && port.Length == 0)
                {
                    // gRPC has no scheme-default port: the exporter would dial 443 or 80 (D1.b).
                    port = DefaultGrpcPort;
                    warnings.Add(Warning(MsgPortlessGrpc, input.Variable, raw, shown, Shown(HostWith(port))));
                }
            }

            var path = !http ? "" : input.PerSignal ? sp.Path : sp.Path.TrimEnd('/') + "/v1/" + input.Signal;
            var url = sp.Scheme + "://" + HostWith(port) + path;

            if (!http && sp.Path is not ("" or "/"))
            {
                // gRPC has no URL path: the target is host:port (contract D1 row "Path on a gRPC
                // endpoint", A29). Once per variable, the variable only — never the value or the
                // path (R8). A bare "/" is no path, as in Go and Python.
                warnings.Add(new EndpointWarning($"endpoint:{MsgGrpcPathIgnored}:{input.Variable}", MsgGrpcPathIgnored, new[] { ("variable", input.Variable) }));
            }

            if (sp.Userinfo || sp.HasQuery || sp.HasFragment)
            {
                // Not supported in any endpoint: removed before the SDK sees the URL (R8); an empty
                // '?' or '#' counts too (D1 ruling). Once per variable and value, not per signal.
                // "using" is the URL this signal uses, as Go shows it (a generic HTTP value: the first signal's /v1/<signal>).
                warnings.Add(Warning(MsgUserinfo, input.Variable, raw, shown, Shown(HostWith(port) + path)));
            }

            if (atInPath)
            {
                // Used as parsed; the variable only: the value may hold the credentials (D1 ruling).
                warnings.Add(new EndpointWarning($"endpoint:{MsgAtInPath}:{input.Variable}", MsgAtInPath, new[] { ("variable", input.Variable) }));
            }

            return new EndpointOutcome { Url = url, Scheme = sp.Scheme, Warnings = warnings };
        }

        private static EndpointOutcome Fail(EndpointInput input, EndpointFailure failure, string why)
        {
            // An invalid value is never rendered, not even partly (D1 ruling); an unsupported
            // scheme parsed, so its redacted form is shown.
            var shown = failure == EndpointFailure.Unsupported ? EndpointGrammar.Render(input.Raw, input.Protocol) : DiagnosticText.UnparseableUrl;
            if (input.FromCode)
            {
                return new EndpointOutcome
                {
                    Failure = failure,
                    Error = $"{input.Variable} {shown} is not a valid OTLP endpoint: {why}; use an http:// or https:// URL or host:port.",
                };
            }

            var using_ = failure == EndpointFailure.Unsupported ? "no OTLP exporter for this signal" : "next configured endpoint, else none";
            // Go's wording: "<why>; ignored", the variable in the attributes.
            return new EndpointOutcome
            {
                Failure = failure,
                Warnings = new[] { Warning(why + "; ignored", input.Variable, input.Raw, shown, using_) },
            };
        }

        /// <summary>A warning keyed by message, variable and a hash of the raw value (once per variable and value; the key is never printed).</summary>
        private static EndpointWarning Warning(string message, string variable, string raw, string shown, string using_)
            => new($"endpoint:{message}:{variable}:{RawKey(raw)}", message, new[] { ("variable", variable), ("value", shown), ("using", using_) });

        private static string RawKey(string raw)
            => Convert.ToHexString(System.Security.Cryptography.SHA256.HashData(System.Text.Encoding.UTF8.GetBytes(raw)), 0, 8);
    }
}
