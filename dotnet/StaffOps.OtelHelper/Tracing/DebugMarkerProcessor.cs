using System.Diagnostics;
using OpenTelemetry;

namespace StaffOps.OtelHelper.Tracing
{
    /// <summary>
    /// Debug mode (<c>OTEL_HELPER_DEBUG_LEVEL=true</c>, R9, ADR-006): sets the span attribute
    /// <c>debug=true</c> on the service's entry spans — spans with no parent or a remote one — so the
    /// Collector's tail sampler (<c>string_attribute</c> policy) keeps the trace. The test is
    /// <c>ParentSpanId == default || HasRemoteParent</c>: <see cref="Activity.Parent"/> is also null for
    /// an in-process child started from an explicit <see cref="ActivityContext"/>, which is not an
    /// entry span. The tracestate is never written (0.2.x prepended <c>debug=true</c>, which gave
    /// <c>debug=true,debug=true</c> under a remote parent).
    /// </summary>
    internal sealed class DebugMarkerProcessor : BaseProcessor<Activity>
    {
        public override void OnStart(Activity data)
        {
            if (IsEntrySpan(data))
            {
                data.SetTag("debug", "true");
            }
        }

        internal static bool IsEntrySpan(Activity activity)
            => activity.ParentSpanId == default || activity.HasRemoteParent;
    }
}
