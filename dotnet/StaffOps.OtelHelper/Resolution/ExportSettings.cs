using System.Globalization;
using System.Security.Cryptography;
using System.Security.Cryptography.X509Certificates;
using System.Text;
using System.Text.RegularExpressions;
using StaffOps.OtelHelper.Diagnostics;

namespace StaffOps.OtelHelper
{
    /// <summary>The exporter settings of one signal with an OTLP target (contract A18, R2, R8, G10; task C8).</summary>
    /// <param name="TimeoutMs">The export deadline: exporter, HTTP client, batch processor and reader.</param>
    /// <param name="Compression"><c>gzip</c> or <c>none</c>.</param>
    /// <param name="Headers">The contract-parsed headers (keys lower case, one value per key); never logged.</param>
    /// <param name="Tls">The validated TLS material of a TLS target, or null (system roots, no client certificate).</param>
    internal sealed record SignalExportSettings(int TimeoutMs, string Compression, IReadOnlyList<(string Key, string Value)> Headers, TlsMaterial? Tls)
    {
        /// <summary>The settings of a signal that sets nothing: 10 s, gzip, no headers, system roots.</summary>
        internal static readonly SignalExportSettings Default = new(TelemetryOptions.DefaultExportTimeoutMs, TelemetryOptions.CompressionGzip, Array.Empty<(string, string)>(), null);
    }

    /// <summary>TLS files after the helper read and validated them (PEM text; never logged).</summary>
    /// <param name="CaPem">The CA certificates, or null for the system roots.</param>
    /// <param name="ClientCertificatePem">The client certificate, set together with <paramref name="ClientKeyPem"/> only when the pair loads.</param>
    /// <param name="ClientKeyPem">The client key.</param>
    internal sealed record TlsMaterial(string? CaPem, string? ClientCertificatePem, string? ClientKeyPem);

    /// <summary>
    /// The exporter rows of contract design D1 the .NET helper owns (ADR-013): timeout,
    /// compression, headers and TLS files, each per signal then generic, plus the A18 rulings
    /// (export deadline, value-length limit 0, batch size above queue size). Every warning names
    /// the variable; header values, file paths and file contents are never written (R8).
    /// </summary>
    internal static class ExportSettingsResolver
    {
        internal const string GenericPrefix = "OTEL_EXPORTER_OTLP_";

        /// <summary>Export-timeout variables the helper does not honor (contract row "Export deadline", A18).</summary>
        internal static readonly string[] IgnoredTimeoutVariables = { "OTEL_BSP_EXPORT_TIMEOUT", "OTEL_BLRP_EXPORT_TIMEOUT", "OTEL_METRIC_EXPORT_TIMEOUT" };

        /// <summary>Value-length variables whose 0 the contract rejects (row "Value-length limits", A18).</summary>
        internal static readonly string[] ValueLengthVariables = { "OTEL_ATTRIBUTE_VALUE_LENGTH_LIMIT", "OTEL_SPAN_ATTRIBUTE_VALUE_LENGTH_LIMIT", "OTEL_LOGRECORD_ATTRIBUTE_VALUE_LENGTH_LIMIT" };

        private static readonly Regex DecimalMs = new(@"^[0-9]+(\.[0-9]+)?$", RegexOptions.CultureInvariant, TimeSpan.FromMilliseconds(250));

        private static readonly string[] TlsSuffixes = { "CERTIFICATE", "CLIENT_CERTIFICATE", "CLIENT_KEY" };

        /// <summary>The generic variable of <paramref name="suffix"/>, e.g. <c>OTEL_EXPORTER_OTLP_TIMEOUT</c>.</summary>
        internal static string Generic(string suffix) => GenericPrefix + suffix;

        /// <summary>The per-signal variable then the generic one.</summary>
        private static string[] Levels(string signal, string suffix) => new[] { TelemetryResolver.SignalVariable(signal, suffix), Generic(suffix) };

        /// <summary>
        /// The settings of <paramref name="signal"/>, whose target is <paramref name="target"/>: a
        /// plaintext target ignores the TLS files (warned by <see cref="WarnPlaintextTlsFiles"/>).
        /// </summary>
        internal static SignalExportSettings Resolve(string signal, OtlpSignalTarget target, TelemetryOptions code, IOtelHelperDiagnostics diagnostics)
        {
            var tls = IsTls(target);
            return new SignalExportSettings(
                ResolveTimeout(signal, code, diagnostics),
                ResolveCompression(signal, code, diagnostics),
                ResolveHeaders(signal, diagnostics),
                tls ? ResolveTls(signal, diagnostics) : null);
        }

        // ── timeout ────────────────────────────────────────────────────

        /// <summary>
        /// Code &gt; <c>OTEL_EXPORTER_OTLP_&lt;SIGNAL&gt;_TIMEOUT</c> &gt; <c>OTEL_EXPORTER_OTLP_TIMEOUT</c> &gt; 10 000 ms.
        /// Plain decimal milliseconds only (<c>^[0-9]+(\.[0-9]+)?$</c> after trimming); anything else,
        /// or 0, warns and the next level applies; a value outside [1, 86 400 000] is clamped with a
        /// warning. Code values were bounded by the validator.
        /// </summary>
        internal static int ResolveTimeout(string signal, TelemetryOptions code, IOtelHelperDiagnostics? diagnostics)
        {
            if (code.ExportTimeoutMs is { } fromCode)
            {
                return fromCode;
            }

            foreach (var variable in signal.Length == 0 ? new[] { Generic("TIMEOUT") } : Levels(signal, "TIMEOUT"))
            {
                if (ContractText.NonBlank(Env(variable)) is not { } raw)
                {
                    continue;
                }

                if (!DecimalMs.IsMatch(raw)
                    || !double.TryParse(raw, NumberStyles.AllowDecimalPoint, CultureInfo.InvariantCulture, out var ms)
                    || ms <= 0)
                {
                    Warn(diagnostics, variable, raw, "not positive decimal milliseconds; using the next level, else 10000");
                    continue;
                }

                if (ms > TelemetryOptions.MaxExportTimeoutMs)
                {
                    Warn(diagnostics, variable, raw, "timeout above 24 h; clamped to 86400000");
                    return TelemetryOptions.MaxExportTimeoutMs;
                }

                if (ms < 1)
                {
                    Warn(diagnostics, variable, raw, "timeout below 1 ms; clamped to 1");
                    return 1;
                }

                return (int)Math.Round(ms, MidpointRounding.AwayFromZero);
            }

            return TelemetryOptions.DefaultExportTimeoutMs;
        }

        /// <summary>Warns once for each export-timeout variable of the SDK processors and reader that is set (not honored).</summary>
        internal static void WarnIgnoredTimeouts(IOtelHelperDiagnostics diagnostics)
        {
            foreach (var variable in IgnoredTimeoutVariables.Where(v => ContractText.NonBlank(Env(v)) is not null))
            {
                diagnostics.ReportOnce("ignored-timeout:" + variable, DiagnosticLevel.Warn,
                    $"{variable} is not honored: the OTLP export timeout is the export deadline of every signal; set OTEL_EXPORTER_OTLP_TIMEOUT or OTEL_EXPORTER_OTLP_<SIGNAL>_TIMEOUT",
                    ("variable", variable));
            }
        }

        /// <summary>
        /// An export timeout set on the SDK's processor or reader options (application code, or
        /// <c>IConfiguration</c>) is not used by the helper's exporters: warned once naming the option in the message (an
        /// attribute value would be cut at 64 characters) when <paramref name="variable"/> is unset (an empty name: no variable applies) (that case is warned by <see cref="WarnIgnoredTimeouts"/>).
        /// </summary>
        internal static void WarnIgnoredOptionTimeout(string option, string variable, Func<bool> isSet, IOtelHelperDiagnostics? diagnostics)
        {
            if (diagnostics is null || (variable.Length > 0 && ContractText.NonBlank(Env(variable)) is not null) || !isSet())
            {
                return;
            }

            diagnostics.ReportOnce("ignored-option-timeout:" + option, DiagnosticLevel.Warn,
                $"{option} is set (in code or IConfiguration) but not used by the helper's OTLP exporter: the OTLP export timeout is the export deadline of every signal; set TelemetryOptions.ExportTimeoutMs");
        }

        // ── compression ────────────────────────────────────────────────

        /// <summary>
        /// Code &gt; <c>OTEL_EXPORTER_OTLP_&lt;SIGNAL&gt;_COMPRESSION</c> &gt; <c>OTEL_EXPORTER_OTLP_COMPRESSION</c>
        /// &gt; <c>gzip</c> (contract D1.d). Environment values are <c>gzip</c> or <c>none</c>, ASCII
        /// case-insensitive; anything else warns and the next level applies.
        /// </summary>
        internal static string ResolveCompression(string signal, TelemetryOptions code, IOtelHelperDiagnostics? diagnostics)
        {
            if (ContractText.NonBlank(code.Compression) is { } fromCode)
            {
                return ContractText.AsciiLower(fromCode);
            }

            foreach (var variable in signal.Length == 0 ? new[] { Generic("COMPRESSION") } : Levels(signal, "COMPRESSION"))
            {
                if (ContractText.NonBlank(Env(variable)) is not { } raw)
                {
                    continue;
                }

                var value = ContractText.AsciiLower(raw);
                if (TelemetryOptions.ValidCompressions.Contains(value))
                {
                    return value;
                }

                Warn(diagnostics, variable, raw, "unsupported compression, using the next level, else gzip; valid values: gzip, none");
            }

            return TelemetryOptions.CompressionGzip;
        }

        // ── headers ────────────────────────────────────────────────────

        private static readonly Regex HeaderKey = new("^[A-Za-z0-9._-]+$", RegexOptions.CultureInvariant, TimeSpan.FromMilliseconds(250));

        /// <summary>
        /// <c>OTEL_EXPORTER_OTLP_&lt;SIGNAL&gt;_HEADERS</c> when it holds a valid item (it replaces the
        /// generic variable, no merge), else <c>OTEL_EXPORTER_OTLP_HEADERS</c>.
        /// </summary>
        internal static IReadOnlyList<(string Key, string Value)> ResolveHeaders(string signal, IOtelHelperDiagnostics diagnostics)
        {
            foreach (var variable in Levels(signal, "HEADERS"))
            {
                var raw = Env(variable);
                if (ContractText.NonBlank(raw) is null)
                {
                    continue;
                }

                var headers = ParseHeaders(raw, diagnostics, variable);
                if (headers.Count > 0)
                {
                    return headers;
                }
            }

            return Array.Empty<(string, string)>();
        }

        /// <summary>
        /// The contract header grammar (D1 rows "Header parsing details", "Header bytes"): items split
        /// on <c>,</c>; an item without <c>=</c> or whose trimmed key is outside <c>[A-Za-z0-9._-]+</c>
        /// (keys are not percent-decoded) is skipped; a value is percent-decoded as a whole, or kept
        /// literally (whole) when an escape is invalid or the bytes are not UTF-8; then trimmed; a
        /// value outside printable ASCII (0x20–0x7E) is skipped; keys are lower-cased and the last
        /// one wins. Warnings name the variable and the item position only, never a value.
        /// </summary>
        internal static IReadOnlyList<(string Key, string Value)> ParseHeaders(string? raw, IOtelHelperDiagnostics? diagnostics = null, string variable = "OTEL_EXPORTER_OTLP_HEADERS")
        {
            var result = new List<(string Key, string Value)>();
            if (raw is null || ContractText.NonBlank(raw) is null)
            {
                return result;
            }

            var items = raw.Split(',');
            for (var i = 0; i < items.Length; i++)
            {
                var item = items[i];
                var position = "item #" + (i + 1).ToString(CultureInfo.InvariantCulture);
                var eq = item.IndexOf('=');
                var key = eq >= 0 ? ContractText.Trim(item[..eq]) : "";
                if (eq < 0 || !HeaderKey.IsMatch(key))
                {
                    diagnostics?.ReportOnce($"header-malformed:{variable}:{i}", DiagnosticLevel.Warn,
                        $"a malformed {variable} item was skipped (expected key=value, the key of letters, digits, '.', '_' or '-')",
                        ("variable", variable), ("item", position));
                    continue;
                }

                var encoded = item[(eq + 1)..];
                var literal = !TryPercentDecode(encoded, out var decoded);
                var value = ContractText.Trim(literal ? encoded : decoded);
                if (value.Any(c => c < 0x20 || c > 0x7e))
                {
                    diagnostics?.ReportOnce($"header-bytes:{variable}:{i}", DiagnosticLevel.Warn,
                        $"a {variable} item was skipped: its decoded value is not printable ASCII",
                        ("variable", variable), ("item", position));
                    continue;
                }

                if (literal)
                {
                    diagnostics?.ReportOnce($"header-literal:{variable}:{i}", DiagnosticLevel.Warn,
                        $"a {variable} item has an invalid percent-escape, or is not UTF-8 once decoded, and is kept literally",
                        ("variable", variable), ("item", position));
                }

                var lower = ContractText.AsciiLower(key);
                result.RemoveAll(h => h.Key == lower);
                result.Add((lower, value));
            }

            return result;
        }

        /// <summary>Strict percent-decoding: false on a <c>%</c> not followed by two hex digits or on bytes that are not UTF-8.</summary>
        internal static bool TryPercentDecode(string text, out string decoded)
        {
            decoded = text;
            if (!text.Contains('%'))
            {
                return true;
            }

            var bytes = new List<byte>(text.Length);
            for (var i = 0; i < text.Length; i++)
            {
                var c = text[i];
                if (c == '%')
                {
                    if (i + 2 >= text.Length || !IsHex(text[i + 1]) || !IsHex(text[i + 2]))
                    {
                        return false;
                    }

                    bytes.Add(byte.Parse(text.AsSpan(i + 1, 2), NumberStyles.AllowHexSpecifier, CultureInfo.InvariantCulture));
                    i += 2;
                    continue;
                }

                bytes.AddRange(Encoding.UTF8.GetBytes(c.ToString()));
            }

            try
            {
                decoded = new UTF8Encoding(encoderShouldEmitUTF8Identifier: false, throwOnInvalidBytes: true).GetString(bytes.ToArray());
                return true;
            }
            catch (DecoderFallbackException)
            {
                return false;
            }
        }

        private static bool IsHex(char c) => c is >= '0' and <= '9' or >= 'a' and <= 'f' or >= 'A' and <= 'F';

        // ── TLS files ──────────────────────────────────────────────────

        /// <summary>The TLS file variables of <paramref name="signal"/> that are set (per-signal and generic), in contract order.</summary>
        internal static IReadOnlyList<string> SetTlsVariables(string signal)
            => TlsSuffixes.SelectMany(s => Levels(signal, s)).Where(v => ContractText.NonBlank(Env(v)) is not null).ToList();

        /// <summary>Whether <paramref name="target"/> uses TLS (the resolved URL is <c>https://</c>).</summary>
        internal static bool IsTls(OtlpSignalTarget target) => target.Endpoint.StartsWith("https://", StringComparison.OrdinalIgnoreCase);

        /// <summary>
        /// Plaintext targets (<c>http://</c>, or schemeless with INSECURE) with TLS files set: the files
        /// are ignored for those signals and never read (contract D1.c corollary). One warning per set
        /// of variables names them and every signal they are ignored for.
        /// </summary>
        internal static void WarnPlaintextTlsFiles(IEnumerable<(string Signal, OtlpSignalTarget Target)> targets, IOtelHelperDiagnostics diagnostics)
        {
            var groups = targets
                .Where(t => !IsTls(t.Target))
                .Select(t => (t.Signal, Variables: SetTlsVariables(t.Signal)))
                .Where(t => t.Variables.Count > 0)
                .GroupBy(t => string.Join(",", t.Variables), StringComparer.Ordinal);
            foreach (var group in groups)
            {
                var signals = string.Join(",", group.Select(t => t.Signal));
                // One "variable" attribute per name: an attribute value is cut at 64 characters.
                diagnostics.ReportOnce("tls-plaintext:" + group.Key + ":" + signals, DiagnosticLevel.Warn,
                    "TLS files are set but the endpoint is plaintext (http:// or schemeless with INSECURE=true); they are ignored for these signals: use https:// or unset them",
                    group.First().Variables.Select(v => ("variable", v)).Append(("signals", signals)).ToArray());
            }
        }

        /// <summary>
        /// The TLS files of a TLS target (contract D1 row "TLS files", A18): the CA, the client key and
        /// the client certificate each per-signal, then generic; a file that cannot be read (or a CA
        /// with no PEM certificate) warns and the next level applies, then the default (system roots,
        /// no client certificate). The key and the certificate are one pair: when only one resolves, or
        /// they do not load together, the helper warns naming the variables and the signal uses server
        /// authentication with the resolved CA. Null when no TLS file variable is set.
        /// </summary>
        internal static TlsMaterial? ResolveTls(string signal, IOtelHelperDiagnostics diagnostics)
        {
            if (SetTlsVariables(signal).Count == 0)
            {
                return null;
            }

            var (_, ca) = TlsFile(signal, "CERTIFICATE", isCa: true, diagnostics);
            var (keyVariable, key) = TlsFile(signal, "CLIENT_KEY", isCa: false, diagnostics);
            var (certVariable, cert) = TlsFile(signal, "CLIENT_CERTIFICATE", isCa: false, diagnostics);
            var pairVariables = new[] { keyVariable, certVariable }.Where(v => v is not null).Select(v => ("variable", v!)).ToList();
            var pair = string.Join(",", pairVariables.Select(v => v.Item2));
            if (key is null && cert is null)
            {
                return new TlsMaterial(ca, null, null);
            }

            if (key is null || cert is null)
            {
                diagnostics.ReportOnce("tls-pair-incomplete:" + pair, DiagnosticLevel.Warn,
                    "the client key and the client certificate must both be set and readable; the client certificate is not used",
                    pairVariables.Append(("using", "server authentication")).ToArray());
                return new TlsMaterial(ca, null, null);
            }

            if (!PairLoads(cert, key))
            {
                diagnostics.ReportOnce("tls-pair-rejected:" + pair, DiagnosticLevel.Warn,
                    "the client key and certificate were rejected (bad PEM, mismatch or encrypted key); the client certificate is not used",
                    pairVariables.Append(("using", "server authentication")).ToArray());
                return new TlsMaterial(ca, null, null);
            }

            return new TlsMaterial(ca, cert, key);
        }

        /// <summary>The first usable file among the per-signal and generic variables of <paramref name="suffix"/>; each skipped file warned by name.</summary>
        private static (string? Variable, string? Content) TlsFile(string signal, string suffix, bool isCa, IOtelHelperDiagnostics diagnostics)
        {
            var candidates = Levels(signal, suffix).Where(v => ContractText.NonBlank(Env(v)) is not null).ToList();
            for (var i = 0; i < candidates.Count; i++)
            {
                var variable = candidates[i];
                var next = i < candidates.Count - 1 ? "the generic variable" : isCa ? "the system roots" : "none";
                string content;
                try
                {
                    content = ReadBounded(ContractText.Trim(Env(variable)!));
                }
                catch (Exception ex) when (ex is IOException or UnauthorizedAccessException or ArgumentException or NotSupportedException or System.Security.SecurityException)
                {
                    diagnostics.ReportOnce("tls-unreadable:" + variable, DiagnosticLevel.Warn,
                        "a TLS file cannot be read (missing, not readable, or larger than 1 MiB) and is ignored", ("variable", variable), ("using", next));
                    continue;
                }

                if (isCa && !HoldsPemCertificate(content))
                {
                    diagnostics.ReportOnce("tls-not-pem:" + variable, DiagnosticLevel.Warn,
                        "a TLS CA file holds no PEM certificate and is ignored", ("variable", variable), ("using", next));
                    continue;
                }

                return (variable, content);
            }

            return (null, null);
        }

        /// <summary>Largest TLS file the helper reads: PEM files are a few KB; a device such as <c>/dev/zero</c> is cut here.</summary>
        internal const int MaxTlsFileBytes = 1 << 20;

        /// <summary>The file's text, read through a stream and refused (<see cref="IOException"/>) above <see cref="MaxTlsFileBytes"/>.</summary>
        internal static string ReadBounded(string path)
        {
            using var stream = new FileStream(path, FileMode.Open, FileAccess.Read, FileShare.Read, bufferSize: 1, FileOptions.None);
            var buffer = new byte[MaxTlsFileBytes + 1];
            var total = 0;
            int read;
            while (total < buffer.Length && (read = stream.Read(buffer, total, buffer.Length - total)) > 0)
            {
                total += read;
            }

            if (total > MaxTlsFileBytes)
            {
                throw new IOException("TLS file larger than the limit");
            }

            return Encoding.UTF8.GetString(buffer, 0, total);
        }

        /// <summary>Whether <paramref name="pem"/> holds at least one PEM certificate.</summary>
        internal static bool HoldsPemCertificate(string pem)
        {
            var collection = new X509Certificate2Collection();
            try
            {
                collection.ImportFromPem(pem);
                return collection.Count > 0;
            }
            catch (CryptographicException)
            {
                return false;
            }
            finally
            {
                foreach (var certificate in collection)
                {
                    certificate.Dispose();
                }
            }
        }

        /// <summary>Whether the certificate and the key load as one pair with a private key.</summary>
        internal static bool PairLoads(string certificatePem, string keyPem)
        {
            try
            {
                using var pair = X509Certificate2.CreateFromPem(certificatePem, keyPem);
                return pair.HasPrivateKey;
            }
            catch (Exception ex) when (ex is CryptographicException or ArgumentException)
            {
                return false;
            }
        }

        // ── A18: value-length limit 0, batch size above queue size ─────

        /// <summary>
        /// A value-length limit of 0 is invalid (contract row "Value-length limits"). The .NET OTLP
        /// exporter reads these variables itself and has no public override, so the helper can only
        /// warn (design D-NET15: parity skip); the warning names the variable that is 0.
        /// </summary>
        internal static void WarnZeroValueLength(IOtelHelperDiagnostics diagnostics)
        {
            foreach (var variable in ValueLengthVariables)
            {
                if (ContractText.NonBlank(Env(variable)) is { } raw
                    && int.TryParse(raw, NumberStyles.Integer, CultureInfo.InvariantCulture, out var limit) && limit == 0)
                {
                    diagnostics.ReportOnce("value-length-zero:" + variable, DiagnosticLevel.Warn,
                        $"{variable}=0 is not a valid value-length limit, but the .NET OTLP exporter applies it and sends those attribute values empty; unset it or set a positive limit",
                        ("variable", variable));
                }
            }
        }

        /// <summary>
        /// The batch size the helper passes (contract row "Batch size above queue size", A18). When
        /// <c>&lt;prefix&gt;_MAX_EXPORT_BATCH_SIZE</c> is larger than <c>&lt;prefix&gt;_MAX_QUEUE_SIZE</c>
        /// (process environment, defaults 512 / 2048; unparseable values are left to the SDK), the
        /// helper warns once naming both variables. The SDK's options already lower the batch size to
        /// the queue size silently; a larger code value would make the processor constructor throw, so
        /// the result never exceeds <paramref name="queueSize"/>.
        /// </summary>
        /// <param name="prefix"><c>OTEL_BSP</c> or <c>OTEL_BLRP</c>.</param>
        /// <param name="optionsType">The options type named when the values came from code.</param>
        /// <param name="queueSize">The queue size of the processor options.</param>
        /// <param name="batchSize">The batch size of the processor options.</param>
        /// <param name="diagnostics">The diagnostics sink.</param>
        internal static int ClampBatchSize(string prefix, string optionsType, int queueSize, int batchSize, IOtelHelperDiagnostics diagnostics)
        {
            string queueVariable = prefix + "_MAX_QUEUE_SIZE", batchVariable = prefix + "_MAX_EXPORT_BATCH_SIZE";
            var envQueue = EnvInt(queueVariable, 2048);
            var envBatch = EnvInt(batchVariable, 512);
            var envAbove = envQueue is > 0 && envBatch is { } b && b > envQueue;
            var optionsAbove = queueSize > 0 && batchSize > queueSize;
            if (envAbove)
            {
                diagnostics.ReportOnce("batch-clamp:" + prefix, DiagnosticLevel.Warn,
                    "the export batch size is larger than the queue size; using the queue size",
                    ("variable", batchVariable), ("variable", queueVariable),
                    ("value", envBatch!.Value.ToString(CultureInfo.InvariantCulture)), ("using", envQueue!.Value.ToString(CultureInfo.InvariantCulture)));
            }
            else if (optionsAbove)
            {
                // The values came from code (or IConfiguration): name the options, not the variables.
                diagnostics.ReportOnce("batch-clamp-options:" + prefix, DiagnosticLevel.Warn,
                    "the export batch size is larger than the queue size; using the queue size",
                    ("option", optionsType + ".MaxExportBatchSize"), ("option", optionsType + ".MaxQueueSize"),
                    ("value", batchSize.ToString(CultureInfo.InvariantCulture)), ("using", queueSize.ToString(CultureInfo.InvariantCulture)));
            }

            return optionsAbove ? queueSize : batchSize;
        }

        /// <summary>An integer variable: <paramref name="unset"/> when unset, null when not an integer.</summary>
        private static int? EnvInt(string variable, int unset)
        {
            if (ContractText.NonBlank(Env(variable)) is not { } raw)
            {
                return unset;
            }

            return int.TryParse(raw, NumberStyles.Integer, CultureInfo.InvariantCulture, out var value) ? value : null;
        }

        private static void Warn(IOtelHelperDiagnostics? diagnostics, string variable, string raw, string why)
            => diagnostics?.ReportOnce($"invalid:{variable}:{raw}", DiagnosticLevel.Warn, $"invalid {variable}: {why}", ("variable", variable), ("value", raw));

        private static string? Env(string name) => System.Environment.GetEnvironmentVariable(name);
    }
}
