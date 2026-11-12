using System;
using System.Diagnostics;
using System.Threading;
using Microsoft.Extensions.DependencyInjection;
using OpenTelemetry;
using OpenTelemetry.Logs;
using OpenTelemetry.Metrics;
using OpenTelemetry.Trace;
using StaffOps.OtelHelper.Metrics;

namespace StaffOps.OtelHelper
{
    /// <summary>
    /// Flushing what the helper exports, with a result that reflects those exports only (task C12).
    /// </summary>
    public static class TelemetryFlushExtensions
    {
        /// <summary>
        /// Exports everything the helper's providers hold now and reports the result: the
        /// <c>TracerProvider</c> and <c>LoggerProvider</c> flushes (the SDK's result: the batch was
        /// handed to the exporter within the timeout; a rejected batch is not reported there), and a
        /// collection of each metric reader that pushes and that the helper built (OTLP, console),
        /// whose result is the export result (a collector answering 500 makes it <c>false</c>). Unlike
        /// <c>MeterProvider.ForceFlush()</c>, the result ignores pull-only readers, which fail every
        /// collection outside a scrape: with the Prometheus listener active (dual mode
        /// <c>otlp,prometheus</c>) or a mounted scraping endpoint, the SDK method returns
        /// <c>false</c> even when the OTLP export succeeded. On the exponential-histogram path
        /// (<c>OTEL_EXPORTER_OTLP_METRICS_DEFAULT_HISTOGRAM_AGGREGATION</c>) the OTLP metric reader
        /// is the SDK's, so the metric part is <c>MeterProvider.ForceFlush()</c>, with that same
        /// limitation. Never throws for a failed export; under <c>OTEL_SDK_DISABLED</c> or with no
        /// helper there is nothing to flush and the result is <c>true</c>.
        /// </summary>
        /// <param name="services">The application's service provider (the one <c>AddOtelHelper</c> configured).</param>
        /// <param name="timeoutMilliseconds">The time for every step together; <see cref="Timeout.Infinite"/> (the default) waits as long as the exports take.</param>
        /// <returns><c>true</c> when every step succeeded within the timeout.</returns>
        /// <exception cref="ArgumentNullException"><paramref name="services"/> is null.</exception>
        /// <exception cref="ArgumentOutOfRangeException"><paramref name="timeoutMilliseconds"/> is below <see cref="Timeout.Infinite"/>.</exception>
        /// <exception cref="ObjectDisposedException"><paramref name="services"/> was disposed (call it before disposing the host).</exception>
        public static bool ForceFlushOtelHelper(this IServiceProvider services, int timeoutMilliseconds = Timeout.Infinite)
        {
            ArgumentNullException.ThrowIfNull(services);
            ArgumentOutOfRangeException.ThrowIfLessThan(timeoutMilliseconds, Timeout.Infinite);

            var clock = Stopwatch.StartNew();
            int Remaining() => timeoutMilliseconds == Timeout.Infinite
                ? Timeout.Infinite
                : (int)Math.Max(0, timeoutMilliseconds - clock.ElapsedMilliseconds);

            var ok = true;
            if (services.GetService<TracerProvider>() is { } tracerProvider)
            {
                ok &= tracerProvider.ForceFlush(Remaining());
            }

            if (services.GetService<MetricsSetup.HelperMetricReaders>() is { } readers)
            {
                if (services.GetService<MetricsSetup.HistogramAggregationPath>() is not null
                    && ResolvedAccess.TryGet(services, out var resolved) && MetricsSetup.HistogramPathActive(resolved))
                {
                    ok &= services.GetRequiredService<MeterProvider>().ForceFlush(Remaining());
                }
                else
                {
                    // Built before reading the list: the readers are created with the provider.
                    services.GetService<MeterProvider>();
                    foreach (var reader in readers.Snapshot())
                    {
                        ok &= reader.Collect(Remaining());
                    }
                }
            }

            if (services.GetService<LoggerProvider>() is { } loggerProvider)
            {
                ok &= loggerProvider.ForceFlush(Remaining());
            }

            return ok;
        }
    }
}
