using System.Diagnostics;

namespace OtelHelper
{
    /// <summary>
    /// Extensions for ActivitySource to simplify common tracing patterns.
    /// </summary>
    public static class ActivitySourceExtensions
    {
        /// <summary>
        /// Starts a new root Activity (new trace), clearing any existing parent context.
        /// Use in workers/background services where each iteration should be an independent trace.
        /// </summary>
        public static Activity? StartRootActivity(this ActivitySource source, string name)
        {
            Activity.Current = null;
            return source.StartActivity(name);
        }
    }
}
