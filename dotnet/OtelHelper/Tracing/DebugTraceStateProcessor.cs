using System.Diagnostics;
using OpenTelemetry;

namespace OtelHelper.Tracing
{
    /// <summary>
    /// Injects tracestate "debug=true" and span attribute "debug=true" on root spans when debug mode is enabled.
    /// The OTel Collector tail_sampling processor can use either:
    ///   - trace_state policy (key: debug, values: ["true"])
    ///   - string_attribute policy (key: debug, values: ["true"])
    /// Both are set for cross-language compatibility (Python can only set attributes, not tracestate).
    /// </summary>
    internal sealed class DebugTraceStateProcessor : BaseProcessor<Activity>
    {
        public override void OnStart(Activity data)
        {
            if (data.Parent == null)
            {
                data.TraceStateString = string.IsNullOrEmpty(data.TraceStateString)
                    ? "debug=true"
                    : $"debug=true,{data.TraceStateString}";
                data.SetTag("debug", "true");
            }
        }
    }
}
