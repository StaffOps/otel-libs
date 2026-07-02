using System.Diagnostics;
using OpenTelemetry;

namespace OtelHelper.Profiling
{
    /// <summary>
    /// Abstraction for profiling providers.
    /// </summary>
    public interface IProfilingProvider : IAsyncDisposable
    {
        /// <summary>Starts the profiler.</summary>
        void Start();

        /// <summary>
        /// Returns the span processor for trace↔profile correlation, or null.
        /// </summary>
        BaseProcessor<Activity>? GetSpanProcessor();
    }
}
