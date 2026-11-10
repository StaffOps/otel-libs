using System.Diagnostics.Tracing;
using System.Globalization;

namespace StaffOps.OtelHelper.Diagnostics
{
    /// <summary>
    /// Forwards the OpenTelemetry SDK's own warnings and errors (its <c>OpenTelemetry*</c>
    /// <c>EventSource</c>s at Warning and above) to the helper's diagnostics sink with origin
    /// <c>sdk</c>, redacted (design D-NET5, requirement DN3): without it, export failures stay
    /// inside <c>EventSource</c> and are invisible (AGENTS.md principle 8). Turned off per
    /// service provider with <see cref="TelemetryOptions.ForwardSdkDiagnostics"/>.
    /// <para>
    /// <c>EventSource</c> events are process-wide, so one listener serves the whole process
    /// (<see cref="Acquire"/>). Every service provider holds a <see cref="Lease"/>, also one that
    /// does not forward: its secrets must be masked in what another provider forwards. Each
    /// event is written once, to the most recently acquired lease that forwards; the secrets
    /// of every live lease are masked; the Prometheus listener's shutdown cancel is noise when
    /// any lease is stopping. The listener goes away with the last lease.
    /// </para>
    /// </summary>
    internal sealed class SdkDiagnosticsForwarder : EventListener
    {
        /// <summary>The SDK event for two different resource schema URLs (<c>OpenTelemetrySdkEventSource.ResourceSchemaUrlMergeConflict</c>).</summary>
        internal const string SchemaUrlConflictEvent = "ResourceSchemaUrlMergeConflict";

        internal const string OtlpExporterSource = "OpenTelemetry-Exporter-OpenTelemetryProtocol";
        internal const string ResponseBodyOmitted = "<response body omitted>";
        private const string SourcePrefix = "OpenTelemetry";

        private static readonly object SharedGate = new();

        // Copy-on-write: OnEventWritten reads one immutable snapshot, without the lock.
        private static Lease[] _leases = Array.Empty<Lease>();
        private static SdkDiagnosticsForwarder? _shared;

        // Assigned after the base constructor, which may already call OnEventSourceCreated.
        private volatile IOtelHelperDiagnostics? _sink;
        private IReadOnlyList<string> _codeSecrets = Array.Empty<string>();

        /// <summary>A forwarder with its own sink (tests); applications get the shared one through <see cref="Acquire"/>.</summary>
        /// <param name="sink">The helper's diagnostics sink.</param>
        /// <param name="codeSecrets">Secret literals configured in code (endpoint userinfo and query values), masked in every record.</param>
        internal SdkDiagnosticsForwarder(IOtelHelperDiagnostics sink, IEnumerable<string>? codeSecrets = null)
        {
            _codeSecrets = codeSecrets?.ToArray() ?? Array.Empty<string>();
            _sink = sink;
        }

        private SdkDiagnosticsForwarder()
        {
        }

        /// <summary>One service provider's hold on the shared forwarder; disposing it releases the hold.</summary>
        internal sealed class Lease : IDisposable
        {
            private volatile IReadOnlyList<string> _secrets;

            internal Lease(IOtelHelperDiagnostics sink, IReadOnlyList<string> secrets)
            {
                Sink = sink;
                _secrets = secrets;
            }

            internal IOtelHelperDiagnostics Sink { get; }

            /// <summary>
            /// Secret literals captured for this provider (code endpoints, the resolved header values),
            /// masked in every forwarded record; the environment's current values are added at each record.
            /// </summary>
            internal IReadOnlyList<string> Secrets
            {
                get => _secrets;
                set => _secrets = value;
            }

            /// <summary>
            /// Whether this provider's sink receives SDK events (<see cref="TelemetryOptions.ForwardSdkDiagnostics"/>).
            /// False until the provider's options are resolved, so an opted-out provider never receives one.
            /// </summary>
            internal volatile bool Forwards;

            /// <summary>Meters whose "not subscribed" SDK warnings are noise for this provider (the helper's own, while helper metrics are off).</summary>
            internal volatile IReadOnlyList<string> QuietMeters = Array.Empty<string>();

            /// <summary>Set when the host starts stopping: the Prometheus listener's cancel is expected from then on.</summary>
            internal volatile bool Stopping;

            /// <summary>True when a host lifetime reports <see cref="Stopping"/>; without one the cancel is always treated as shutdown.</summary>
            internal volatile bool HasLifetime;

            public void Dispose() => Release(this);
        }

        /// <summary>
        /// Registers a provider with the process-wide forwarder, creating the listener with the first
        /// lease. Both transitions are decided under the lock; only the listener's construction and
        /// disposal run outside it (the EventListener constructor calls back for every source).
        /// </summary>
        internal static Lease Acquire(IOtelHelperDiagnostics sink, IEnumerable<string> secrets, bool forwards = false)
        {
            var lease = new Lease(sink, secrets.ToArray()) { Forwards = forwards };
            bool needListener;
            lock (SharedGate)
            {
                _leases = _leases.Append(lease).ToArray();
                needListener = _shared is null;
            }

            if (needListener)
            {
                var created = new SdkDiagnosticsForwarder();
                var publish = false;
                lock (SharedGate)
                {
                    if (_shared is null && _leases.Length > 0)
                    {
                        _shared = created;
                        publish = true;
                    }
                }

                if (!publish)
                {
                    created.Dispose();
                }
            }

            return lease;
        }

        /// <summary>Number of live leases (tests).</summary>
        internal static int LiveLeases => Volatile.Read(ref _leases).Length;

        /// <summary>Whether the shared listener exists (tests).</summary>
        internal static bool HasListener
        {
            get
            {
                lock (SharedGate)
                {
                    return _shared is not null;
                }
            }
        }

        private static void Release(Lease lease)
        {
            SdkDiagnosticsForwarder? toDispose = null;
            lock (SharedGate)
            {
                if (!_leases.Contains(lease))
                {
                    return;
                }

                _leases = _leases.Where(l => !ReferenceEquals(l, lease)).ToArray();
                if (_leases.Length == 0)
                {
                    toDispose = _shared;
                    _shared = null;
                }
            }

            toDispose?.Dispose();
        }

        protected override void OnEventSourceCreated(EventSource eventSource)
        {
            if (eventSource.Name.StartsWith(SourcePrefix, StringComparison.Ordinal))
            {
                EnableEvents(eventSource, EventLevel.Warning);
            }
        }

        protected override void OnEventWritten(EventWrittenEventArgs eventData)
        {
            if (eventData.Level > EventLevel.Warning || eventData.EventSource is null)
            {
                return;
            }

            IOtelHelperDiagnostics sink;
            IEnumerable<string> secrets;
            IReadOnlyCollection<string> quiet = Array.Empty<string>();
            bool stopping;
            if (_sink is { } own)
            {
                sink = own;
                secrets = _codeSecrets;
                stopping = true;
            }
            else
            {
                if (!ReferenceEquals(Volatile.Read(ref _shared), this))
                {
                    return;
                }

                var leases = Volatile.Read(ref _leases);
                var target = leases.LastOrDefault(l => l.Forwards);
                if (target is null)
                {
                    return;
                }

                sink = target.Sink;
                secrets = leases.SelectMany(l => l.Secrets).ToArray();
                quiet = target.QuietMeters.ToArray();
                stopping = leases.Any(l => l.Stopping) || leases.All(l => !l.HasLifetime);
            }

            var level = eventData.Level is EventLevel.Critical or EventLevel.Error ? DiagnosticLevel.Error : DiagnosticLevel.Warn;
            try
            {
                var source = eventData.EventSource.Name;
                var payload = eventData.Payload?.ToArray() ?? Array.Empty<object?>();
                if (eventData.EventName == SchemaUrlConflictEvent)
                {
                    // C6: with the helper's resource on 1.43.0, this conflict usually means a resource
                    // added after AddOtelHelper() (an app AddService declares the SDK's 1.44.0): say
                    // so as the helper, once, with both URLs, instead of the SDK's text per merge.
                    sink.ReportOnce("resource:schema-url-conflict", DiagnosticLevel.Warn,
                        "resources with different schema URLs were merged, so the resource has none (for example, a resource added after AddOtelHelper(), such as AddService in a later ConfigureResource); register it before AddOtelHelper()",
                        ("old", payload.Length > 0 ? payload[0]?.ToString() ?? "" : ""),
                        ("updating", payload.Length > 1 ? payload[1]?.ToString() ?? "" : ""));
                    return;
                }

                if (!SdkTextSanitizer.IsQuietMeterNotice(eventData.EventName, payload, quiet)
                    && Format(source, eventData.EventName, eventData.Message, payload, secrets, stopping) is { } text)
                {
                    sink.ReportSdk(level, source, text, SdkTextSanitizer.OnceKey(source, text));
                }
            }
            catch (Exception)
            {
                // Forwarding must never break the SDK thread that raised the event.
            }
        }

        /// <summary>
        /// The text written for an event, or null when it is noise: rendered (body dropped by
        /// payload position), then masked and redacted, and only then cut — an echoed value is
        /// never cut before its secrets are removed.
        /// </summary>
        internal static string? Format(string source, string? eventName, string? message, object?[] payload, IEnumerable<string> secrets, bool stopping = true)
        {
            var forms = SdkTextSanitizer.Forms(SdkTextSanitizer.ConfiguredSecrets().Concat(secrets));
            var text = Render(source, eventName, message, payload, forms, out var bodyHandled);
            return SdkTextSanitizer.IsNoise(source, text, stopping) ? null : SdkTextSanitizer.SanitizeWithForms(text, forms, bodyHandled);
        }

        /// <summary>
        /// The event's message with its payload filled in (payload only when the template does not
        /// fit). The response body of the OTLP exporter's <c>HttpRequestFailed</c> event is replaced
        /// by <see cref="ResponseBodyOmitted"/> by payload position, whatever it holds
        /// (<paramref name="bodyHandled"/> true); an echoed configuration value is masked with
        /// <paramref name="forms"/>, redacted, and then cut to 64 code points.
        /// </summary>
        internal static string Render(string source, string? eventName, string? message, object?[] payload, IReadOnlyList<string> forms, out bool bodyHandled)
        {
            payload = (object?[])payload.Clone();
            bodyHandled = false;
            if (source == OtlpExporterSource && eventName == "HttpRequestFailed" && payload.Length >= 2)
            {
                if (payload[1] is string body)
                {
                    bodyHandled = true;
                    if (body.Length > 0)
                    {
                        payload[1] = ResponseBodyOmitted;
                    }
                }
            }

            if (eventName == "InvalidConfigurationValue" && payload.Length >= 2 && payload[1] is string value)
            {
                payload[1] = DiagnosticText.Truncate(DiagnosticText.RedactText(SdkTextSanitizer.MaskLiterals(value, forms)));
            }

            if (message is null)
            {
                return string.Join(" ", payload.Select(p => Convert.ToString(p, CultureInfo.InvariantCulture)));
            }

            try
            {
                return string.Format(CultureInfo.InvariantCulture, message, payload);
            }
            catch (FormatException)
            {
                return message + " " + string.Join(" ", payload.Select(p => Convert.ToString(p, CultureInfo.InvariantCulture)));
            }
        }

        public override void Dispose()
        {
            _sink = null;
            base.Dispose();
        }
    }
}
