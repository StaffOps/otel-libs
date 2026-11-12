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

            var settings = resolved.Export(signal);
            var grpc = target.Protocol != TelemetryOptions.ProtocolHttpProtobuf;
            var options = new OtlpExporterOptions
            {
                Endpoint = endpoint,
                Protocol = grpc ? OtlpExportProtocol.Grpc : OtlpExportProtocol.HttpProtobuf,
                TimeoutMilliseconds = settings.TimeoutMs,
                Compression = settings.Compression == TelemetryOptions.CompressionGzip ? OtlpExportCompression.GZip : OtlpExportCompression.None,
                // Headers travel on the helper's HttpClient; the SDK parser never sees them (ADR-013).
                Headers = string.Empty,
            };
            var factory = new HelperHttpClientFactory(signal, settings, grpc, options.MaxResponseSizeBytes, diagnostics);
            clients?.Add(factory);
            options.HttpClientFactory = factory.Create;
            return options;
        }

        /// <summary>The export deadline of <paramref name="signal"/>, passed to its batch processor or reader too (contract row "Export deadline", A18).</summary>
        internal static int TimeoutFor(string signal, ResolvedTelemetry resolved) => resolved.Export(signal).TimeoutMs;
    }

    /// <summary>
    /// The helper's <c>HttpClientFactory</c> for one exporter (ADR-013 client duties): one
    /// long-lived client per exporter instance, on a <see cref="SocketsHttpHandler"/> wrapped by
    /// <see cref="HelperExportHandler"/> (HTTP/protobuf calls the synchronous <c>Send</c>, gRPC
    /// <c>SendAsync</c>; both are forwarded), no redirects (refused loudly), the response buffer
    /// bounded like the SDK's own client, the signal's export timeout, its contract headers
    /// (gRPC <c>-bin</c> values base64) and its validated TLS material.
    /// </summary>
    internal sealed class HelperHttpClientFactory : IDisposable
    {
        private readonly string _signal;
        private readonly SignalExportSettings _settings;
        private readonly bool _grpc;
        private readonly int _maxResponseBytes;
        private readonly IOtelHelperDiagnostics _diagnostics;
        private readonly object _gate = new();
        private HttpClient? _client;
        private X509Certificate2Collection? _roots;
        private X509Certificate2? _clientCertificate;
        private bool _disposed;

        /// <summary>The transport of the built client (tests: redirects, TLS).</summary>
        internal SocketsHttpHandler? Transport { get; private set; }

        /// <param name="signal">The signal, named in diagnostics.</param>
        /// <param name="settings">The signal's resolved exporter settings.</param>
        /// <param name="grpc">Whether the exporter speaks gRPC (<c>grpc-timeout</c>, base64 <c>-bin</c> values).</param>
        /// <param name="maxResponseBytes">The response buffer bound (the SDK's <c>MaxResponseSizeBytes</c>).</param>
        /// <param name="diagnostics">The diagnostics sink.</param>
        internal HelperHttpClientFactory(string signal, SignalExportSettings settings, bool grpc, int maxResponseBytes, IOtelHelperDiagnostics diagnostics)
        {
            _signal = signal;
            _settings = settings;
            _grpc = grpc;
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
            var sockets = new SocketsHttpHandler
            {
                AllowAutoRedirect = false,
                PooledConnectionLifetime = TimeSpan.FromMinutes(5),
            };
            if (_settings.Tls is { } tls)
            {
                ConfigureTls(sockets, tls);
            }

            Transport = sockets;

            var handler = new HelperExportHandler(sockets, _signal, _grpc ? _settings.TimeoutMs : null, _diagnostics);
            var client = new HttpClient(handler, disposeHandler: true)
            {
                Timeout = TimeSpan.FromMilliseconds(_settings.TimeoutMs),
            };
            if (_maxResponseBytes > 0)
            {
                client.MaxResponseContentBufferSize = _maxResponseBytes;
            }

            foreach (var (key, value) in _settings.Headers)
            {
                // gRPC binary metadata travels base64 (unpadded, as grpc-go); HTTP sends the value as is.
                var wire = _grpc && key.EndsWith("-bin", StringComparison.Ordinal)
                    ? Convert.ToBase64String(System.Text.Encoding.ASCII.GetBytes(value)).TrimEnd('=')
                    : value;
                if (!client.DefaultRequestHeaders.TryAddWithoutValidation(key, wire))
                {
                    // A content header (e.g. content-type); the value is never echoed.
                    _diagnostics.ReportOnce("rejected-header:" + _signal + ":" + key, DiagnosticLevel.Warn,
                        "an OTLP header item was rejected by the HTTP client (a content header) and skipped",
                        ("signal", _signal), ("key", key));
                }
            }

            return client;
        }

        /// <summary>Parses a headers text with the contract grammar (<see cref="ExportSettingsResolver.ParseHeaders"/>).</summary>
        internal static IReadOnlyList<(string Key, string Value)> ParseHeaders(string? raw, IOtelHelperDiagnostics? diagnostics = null)
            => ExportSettingsResolver.ParseHeaders(raw, diagnostics);

        private void ConfigureTls(SocketsHttpHandler handler, TlsMaterial tls)
        {
            if (tls.CaPem is { } caPem)
            {
                var ca = new X509Certificate2Collection();
                ca.ImportFromPem(caPem);
                _roots = ca;
                handler.SslOptions.RemoteCertificateValidationCallback = (_, certificate, presented, errors) =>
                    ValidateWithCustomRoot(certificate, errors, ca, presented);
            }

            if (tls.ClientCertificatePem is { } certPem && tls.ClientKeyPem is { } keyPem)
            {
                // Validated by the resolver; re-imported from PKCS#12 so SslStream can use the key. Linux:
                // an ephemeral key, never written anywhere. Windows (SChannel) cannot use an ephemeral key
                // for client authentication, so there the key goes to the user key store for the life of
                // the certificate (removed on dispose); macOS has no ephemeral key set.
                using var ephemeral = X509Certificate2.CreateFromPem(certPem, keyPem);
                var flags = OperatingSystem.IsLinux() ? X509KeyStorageFlags.EphemeralKeySet : X509KeyStorageFlags.DefaultKeySet;
                var pair = new X509Certificate2(ephemeral.Export(X509ContentType.Pkcs12), (string?)null, flags);
                _clientCertificate = pair;
                handler.SslOptions.ClientCertificates = new X509CertificateCollection { pair };
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

        /// <summary>The helper factories registered so far (tests).</summary>
        internal IReadOnlyList<HelperHttpClientFactory> HelperFactories
        {
            get
            {
                lock (_factories)
                {
                    return _factories.OfType<HelperHttpClientFactory>().ToList();
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
