using System.Diagnostics;
using System.Diagnostics.CodeAnalysis;
using OpenTelemetry;
using Pyroscope;

namespace OtelHelper.Profiling
{
    /// <summary>
    /// Pyroscope-based profiling provider.
    ///
    /// IMPORTANT — division of responsibilities:
    /// The native Pyroscope profiler reads its configuration (server address,
    /// application name, which profile types to enable) from environment
    /// variables at CLR bootstrap, BEFORE any managed code runs. Therefore this
    /// provider CANNOT set the server address or application name — those MUST be
    /// provided as process environment variables by the deployment (Helm/Dockerfile):
    ///   PYROSCOPE_SERVER_ADDRESS, PYROSCOPE_APPLICATION_NAME,
    ///   PYROSCOPE_PROFILING_ENABLED, PYROSCOPE_PROFILING_*_ENABLED,
    ///   CORECLR_PROFILER, LD_PRELOAD, etc. (see README prerequisites).
    ///
    /// What this provider DOES do:
    ///   - Registers a PyroscopeSpanProcessor for trace↔profile correlation.
    ///   - Toggles the runtime tracking flags (only effective when the matching
    ///     PYROSCOPE_PROFILING_*_ENABLED env var is set and the native profiler
    ///     is loaded; otherwise these are no-ops).
    /// </summary>
    internal sealed class PyroscopeProfilingProvider : IProfilingProvider
    {
        private BaseProcessor<Activity>? _spanProcessor;

        [ExcludeFromCodeCoverage(Justification = "Requires native CLR profiler (Pyroscope.Profiler.Native.so), not loadable in unit tests")]
        public void Start()
        {
            // Runtime toggle of profile types. These only take effect when the
            // corresponding PYROSCOPE_PROFILING_*_ENABLED env var is configured
            // and the native profiler is loaded — otherwise they are no-ops.
            // We enable all types the in-process profiler can capture for managed
            // .NET (CPU, allocation, lock contention, exceptions).
            Profiler.Instance.SetCPUTrackingEnabled(true);
            Profiler.Instance.SetAllocationTrackingEnabled(true);
            Profiler.Instance.SetContentionTrackingEnabled(true);
            Profiler.Instance.SetExceptionTrackingEnabled(true);

            // Span processor links profiling samples to the active trace/span.
            // This is the part that always works regardless of native bootstrap.
            _spanProcessor = new Pyroscope.OpenTelemetry.PyroscopeSpanProcessor();
        }

        public BaseProcessor<Activity>? GetSpanProcessor() => _spanProcessor;

        public ValueTask DisposeAsync()
        {
            _spanProcessor?.Dispose();
            return ValueTask.CompletedTask;
        }
    }
}
