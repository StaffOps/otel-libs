using System.Diagnostics;
using System.Diagnostics.CodeAnalysis;
using OpenTelemetry;
using Pyroscope;

namespace StaffOps.OtelHelper.Profiling
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
    ///   - Supplies a PyroscopeSpanProcessor for trace↔profile correlation.
    ///   - Toggles the runtime tracking flags in Start() (only effective when the matching
    ///     PYROSCOPE_PROFILING_*_ENABLED env var is set and the native profiler
    ///     is loaded). Without the native library the first toggle makes Pyroscope print a
    ///     "[Profiler] Failed to load Pyroscope.Profiler.Native.so" exception text to
    ///     standard output, which is why the helper calls Start() only when the
    ///     prerequisites are met (ProfilingStartup).
    /// </summary>
    internal sealed class PyroscopeProfilingProvider : IProfilingProvider, IDisposable
    {
        // Built eagerly: the processor's constructor touches nothing native, and the tracer
        // provider may be built before the host starts the profiler (task C15).
        private readonly BaseProcessor<Activity> _spanProcessor = new Pyroscope.OpenTelemetry.PyroscopeSpanProcessor();

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
        }

        public BaseProcessor<Activity>? GetSpanProcessor() => _spanProcessor;

        // IDisposable too: the container creates this instance (task C15), and a synchronous
        // ServiceProvider.Dispose() (e.g. `using var host`) throws for a service that is only
        // IAsyncDisposable.
        public void Dispose() => _spanProcessor.Dispose();

        public ValueTask DisposeAsync()
        {
            Dispose();
            return ValueTask.CompletedTask;
        }
    }
}
