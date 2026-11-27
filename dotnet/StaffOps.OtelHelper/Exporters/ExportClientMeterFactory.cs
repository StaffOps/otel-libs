using System.Diagnostics.Metrics;

namespace StaffOps.OtelHelper.Exporters
{
    /// <summary>
    /// The <see cref="IMeterFactory"/> of the helper's own export clients (design D-NET2, note of
    /// 2026-11-12, C19 fix round 1). <see cref="System.Net.Http.SocketsHttpHandler"/> records its
    /// HTTP client metrics on the shared <c>System.Net.Http</c> meter unless it has a factory, and the
    /// helper subscribes that meter for the application: its OTLP exports were counted as application
    /// requests. This factory ignores the requested name and creates every meter under
    /// <see cref="MeterName"/>, which no helper provider subscribes; the meters are disposed with it.
    /// </summary>
    internal sealed class ExportClientMeterFactory : IMeterFactory
    {
        /// <summary>The meter name of the export clients' HTTP metrics (never exported by the helper).</summary>
        internal const string MeterName = "StaffOps.OtelHelper.Internal.OtlpExportClient";

        private readonly List<Meter> _meters = new();
        private bool _disposed;

        /// <inheritdoc />
        public Meter Create(MeterOptions options)
        {
            ArgumentNullException.ThrowIfNull(options);
            var meter = new Meter(new MeterOptions(MeterName) { Version = options.Version, Tags = options.Tags, Scope = this });
            lock (_meters)
            {
                if (_disposed)
                {
                    meter.Dispose();
                }
                else
                {
                    _meters.Add(meter);
                }
            }

            return meter;
        }

        /// <inheritdoc />
        public void Dispose()
        {
            lock (_meters)
            {
                if (_disposed)
                {
                    return;
                }

                _disposed = true;
                foreach (var meter in _meters)
                {
                    meter.Dispose();
                }

                _meters.Clear();
            }
        }
    }
}
