using System.Net.Http;
using System.Net.Security;
using System.Security.Cryptography.X509Certificates;
using OpenTelemetry.Exporter;
using StaffOps.OtelHelper.Diagnostics;

namespace StaffOps.OtelHelper.Exporters
{
    /// <summary>
    /// OTLP exporter options built by the helper (ADR-013, "neutralizing SDK-read values"):
    /// the <see cref="OtlpExporterOptions"/> constructor reads the environment and
    /// <c>IConfiguration</c>, so every property the SDK would take from there is assigned
    /// from <see cref="ResolvedTelemetry"/>, and the HTTP client comes from the helper's
    /// <see cref="HelperHttpClientFactory"/> (one client per exporter instance).
    /// </summary>
    internal static class OtlpExporters
    {
        /// <summary>Options for <paramref name="signal"/>, or null when it has no OTLP target (or the target cannot be used).</summary>
        internal static OtlpExporterOptions? OptionsFor(string signal, ResolvedTelemetry resolved, IOtelHelperDiagnostics diagnostics, ExporterClients? clients = null)
        {
            if (resolved.OtlpTarget(signal) is not { } target)
            {
                return null;
            }

            if (!Uri.TryCreate(target.Endpoint, UriKind.Absolute, out var endpoint))
            {
                diagnostics.ReportOnce("unusable-endpoint:" + signal, DiagnosticLevel.Warn,
                    $"the OTLP endpoint for {signal} cannot be used; {signal} are not exported",
                    ("signal", signal), ("endpoint", DiagnosticText.RenderUrl(target.Endpoint)));
                return null;
            }

            var options = new OtlpExporterOptions
            {
                Endpoint = endpoint,
                Protocol = target.Protocol == TelemetryOptions.ProtocolHttpProtobuf ? OtlpExportProtocol.HttpProtobuf : OtlpExportProtocol.Grpc,
                TimeoutMilliseconds = resolved.ExportTimeoutMs,
                Compression = resolved.Compression == TelemetryOptions.CompressionGzip ? OtlpExportCompression.GZip : OtlpExportCompression.None,
                // Headers travel on the helper's HttpClient; the SDK parser never sees them (ADR-013).
                Headers = string.Empty,
            };
            var factory = new HelperHttpClientFactory(resolved, options.MaxResponseSizeBytes, diagnostics, endpoint.Scheme == Uri.UriSchemeHttps);
            clients?.Add(factory);
            options.HttpClientFactory = factory.Create;
            return options;
        }
    }

    /// <summary>
    /// The helper's <c>HttpClientFactory</c> for one exporter (ADR-013 client duties):
    /// one long-lived client on a <see cref="SocketsHttpHandler"/> (HTTP/protobuf calls the
    /// synchronous <c>Send</c>, gRPC <c>SendAsync</c>; the handler serves both), no redirects,
    /// the response buffer bounded like the SDK's own client, the export timeout, and the
    /// generic <c>OTEL_EXPORTER_OTLP_HEADERS</c> and certificate files from the process
    /// environment. Skeleton of task C5: the contract header parser, per-signal inputs and
    /// the TLS-file validation are task C8.
    /// </summary>
    internal sealed class HelperHttpClientFactory : IDisposable
    {
        private readonly ResolvedTelemetry _resolved;
        private readonly int _maxResponseBytes;
        private readonly IOtelHelperDiagnostics _diagnostics;
        private readonly object _gate = new();
        private HttpClient? _client;
        private X509Certificate2Collection? _roots;
        private X509Certificate2? _clientCertificate;
        private bool _disposed;
        private readonly bool _tls;

        /// <param name="resolved">The resolved settings.</param>
        /// <param name="maxResponseBytes">The response buffer bound (the SDK's <c>MaxResponseSizeBytes</c>).</param>
        /// <param name="diagnostics">The diagnostics sink.</param>
        /// <param name="tls">Whether the target uses TLS: the certificate files are read only then (a plaintext target never loads them; the plaintext-drop warning is task C8).</param>
        internal HelperHttpClientFactory(ResolvedTelemetry resolved, int maxResponseBytes, IOtelHelperDiagnostics diagnostics, bool tls = true)
        {
            _tls = tls;
            _resolved = resolved;
            _maxResponseBytes = maxResponseBytes;
            _diagnostics = diagnostics;
        }

        /// <summary>The exporter's client, created on the first call (the SDK calls it once per exporter).</summary>
        internal HttpClient Create()
        {
            lock (_gate)
            {
                ObjectDisposedException.ThrowIf(_disposed, this);
                return _client ??= Build();
            }
        }

        /// <summary>Disposes the client and the certificates it loaded (the service provider calls it at shutdown).</summary>
        public void Dispose()
        {
            lock (_gate)
            {
                if (_disposed)
                {
                    return;
                }

                _disposed = true;
                _client?.Dispose();
                _clientCertificate?.Dispose();
                foreach (var root in _roots ?? new X509Certificate2Collection())
                {
                    root.Dispose();
                }
            }
        }

        private HttpClient Build()
        {
            var handler = new SocketsHttpHandler
            {
                AllowAutoRedirect = false,
                PooledConnectionLifetime = TimeSpan.FromMinutes(5),
            };
            if (_tls)
            {
                ConfigureTls(handler);
            }

            var client = new HttpClient(handler, disposeHandler: true)
            {
                Timeout = TimeSpan.FromMilliseconds(_resolved.ExportTimeoutMs),
            };
            if (_maxResponseBytes > 0)
            {
                client.MaxResponseContentBufferSize = _maxResponseBytes;
            }

            foreach (var (key, value) in ParseHeaders(_resolved.Headers, _diagnostics))
            {
                if (!client.DefaultRequestHeaders.TryAddWithoutValidation(key, value))
                {
                    // An invalid header name (or a content header); the value is never echoed.
                    _diagnostics.ReportOnce("rejected:OTEL_EXPORTER_OTLP_HEADERS:" + key, DiagnosticLevel.Warn,
                        "an OTEL_EXPORTER_OTLP_HEADERS item was rejected by the HTTP client and skipped",
                        ("variable", "OTEL_EXPORTER_OTLP_HEADERS"), ("key", key));
                }
            }

            return client;
        }

        /// <summary>
        /// <c>k=v,k2=v2</c>: items without <c>=</c> or with an empty key are skipped, keys and
        /// values are trimmed and percent-decoded (an invalid escape is kept as written), the last
        /// value of a key wins (case-insensitive).
        /// Never fails and never logs a value.
        /// </summary>
        internal static IReadOnlyList<(string Key, string Value)> ParseHeaders(string? raw, IOtelHelperDiagnostics? diagnostics = null)
        {
            const string variable = "OTEL_EXPORTER_OTLP_HEADERS";
            var result = new List<(string Key, string Value)>();
            if (string.IsNullOrWhiteSpace(raw))
            {
                return result;
            }

            foreach (var item in raw.Split(','))
            {
                if (item.Trim().Length == 0)
                {
                    continue;
                }

                // Uri.UnescapeDataString keeps an invalid escape ("%zz") as written; it never throws here.
                var eq = item.IndexOf('=');
                var key = eq > 0 ? Uri.UnescapeDataString(item[..eq].Trim()) : "";
                var value = eq > 0 ? Uri.UnescapeDataString(item[(eq + 1)..].Trim()) : "";
                if (key.Length == 0)
                {
                    // Name only: a malformed item may hold a secret.
                    diagnostics?.ReportOnce("malformed:" + variable, DiagnosticLevel.Warn,
                        $"a malformed {variable} item was skipped (expected key=value)", ("variable", variable));
                    continue;
                }

                if (key.Any(IsControl) || value.Any(IsControl))
                {
                    // A decoded CR/LF would inject a header line; the value is never echoed.
                    diagnostics?.ReportOnce("control:" + variable + ":" + key, DiagnosticLevel.Warn,
                        $"a {variable} item with a control character was skipped", ("variable", variable), ("key", key));
                    continue;
                }

                result.RemoveAll(h => h.Key.Equals(key, StringComparison.OrdinalIgnoreCase));
                result.Add((key, value));
            }

            return result;
        }

        private static bool IsControl(char c) => c < 0x20 || c == 0x7f;

        private void ConfigureTls(SocketsHttpHandler handler)
        {
            var ca = LoadCertificates(_resolved.CertificatePath, "OTEL_EXPORTER_OTLP_CERTIFICATE");
            _roots = ca;
            if (ca is { Count: > 0 })
            {
                handler.SslOptions.RemoteCertificateValidationCallback = (_, certificate, presented, errors) =>
                    ValidateWithCustomRoot(certificate, errors, ca, presented);
            }

            if (_resolved.ClientCertificatePath is { } certPath && _resolved.ClientKeyPath is { } keyPath)
            {
                try
                {
                    var pair = X509Certificate2.CreateFromPemFile(certPath, keyPath);
                    _clientCertificate = pair;
                    handler.SslOptions.ClientCertificates = new X509CertificateCollection { pair };
                }
                catch (Exception ex) when (ex is IOException or UnauthorizedAccessException or System.Security.Cryptography.CryptographicException or ArgumentException)
                {
                    _diagnostics.ReportOnce("unreadable:OTEL_EXPORTER_OTLP_CLIENT_CERTIFICATE", DiagnosticLevel.Warn,
                        "cannot load the OTLP client certificate and key; exporting without a client certificate",
                        ("variable", "OTEL_EXPORTER_OTLP_CLIENT_CERTIFICATE"));
                }
            }
        }

        private X509Certificate2Collection? LoadCertificates(string? path, string variable)
        {
            if (path is null)
            {
                return null;
            }

            try
            {
                var collection = new X509Certificate2Collection();
                collection.ImportFromPemFile(path);
                return collection;
            }
            catch (Exception ex) when (ex is IOException or UnauthorizedAccessException or System.Security.Cryptography.CryptographicException or ArgumentException)
            {
                _diagnostics.ReportOnce("unreadable:" + variable, DiagnosticLevel.Warn,
                    "cannot read the OTLP CA certificate file; using the system trust store", ("variable", variable));
                return null;
            }
        }

        /// <summary>Accepts a server certificate that chains to one of <paramref name="roots"/>; name and other errors still fail.</summary>
        /// <param name="certificate">The server certificate.</param>
        /// <param name="errors">The platform's validation result.</param>
        /// <param name="roots">The CA certificates of <c>OTEL_EXPORTER_OTLP_CERTIFICATE</c>.</param>
        /// <param name="presented">The chain the server presented: its intermediates are added to the extra store.</param>
        internal static bool ValidateWithCustomRoot(X509Certificate? certificate, SslPolicyErrors errors, X509Certificate2Collection roots, X509Chain? presented = null)
        {
            if (errors == SslPolicyErrors.None)
            {
                return true;
            }

            if ((errors & ~SslPolicyErrors.RemoteCertificateChainErrors) != SslPolicyErrors.None || certificate is null)
            {
                return false;
            }

            using var chain = new X509Chain();
            chain.ChainPolicy.TrustMode = X509ChainTrustMode.CustomRootTrust;
            chain.ChainPolicy.RevocationMode = X509RevocationMode.NoCheck;
            chain.ChainPolicy.ApplicationPolicy.Add(new System.Security.Cryptography.Oid("1.3.6.1.5.5.7.3.1")); // server authentication
            chain.ChainPolicy.CustomTrustStore.AddRange(roots);
            foreach (var element in presented?.ChainElements ?? Enumerable.Empty<X509ChainElement>())
            {
                chain.ChainPolicy.ExtraStore.Add(element.Certificate);
            }

            using var leaf = new X509Certificate2(certificate);
            return chain.Build(leaf);
        }
    }
}

namespace StaffOps.OtelHelper.Exporters
{
    /// <summary>The helper clients of one service provider, disposed with it.</summary>
    internal sealed class ExporterClients : IDisposable
    {
        private readonly List<IDisposable> _factories = new();

        /// <summary>Factories registered so far (tests).</summary>
        internal int Count
        {
            get
            {
                lock (_factories)
                {
                    return _factories.Count;
                }
            }
        }

        /// <summary>Set by <see cref="Dispose"/> (tests).</summary>
        internal bool Disposed { get; private set; }

        internal void Add(IDisposable factory)
        {
            lock (_factories)
            {
                _factories.Add(factory);
            }
        }

        public void Dispose()
        {
            lock (_factories)
            {
                foreach (var factory in _factories)
                {
                    factory.Dispose();
                }

                _factories.Clear();
                Disposed = true;
            }
        }
    }
}
