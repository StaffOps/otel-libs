using System.Diagnostics;

namespace StaffOps.OtelHelper.Tracing
{
    /// <summary>
    /// Under <c>OTEL_SDK_DISABLED=true</c> no provider listens, and ASP.NET Core then creates a request
    /// activity only when logging or a diagnostic listener asks for one, so an inbound
    /// <c>traceparent</c> would not reach <c>HttpClient</c>. Go's no-op tracer always passes it on. This
    /// listener asks the host for a non-recorded activity when, and only when, the request continues a
    /// trace (a parent is present); roots get none. Started when <c>tracecontext</c> is configured.
    /// </summary>
    internal sealed class ContinuationListener : IDisposable
    {
        internal const string AspNetCoreSource = "Microsoft.AspNetCore";

        private ActivityListener? _listener;

        internal void Start()
        {
            if (_listener is not null)
            {
                return;
            }

            _listener = new ActivityListener
            {
                ShouldListenTo = source => source.Name == AspNetCoreSource,
                Sample = (ref ActivityCreationOptions<ActivityContext> options)
                    => options.Parent.TraceId != default ? ActivitySamplingResult.PropagationData : ActivitySamplingResult.None,
                SampleUsingParentId = (ref ActivityCreationOptions<string> options)
                    => string.IsNullOrEmpty(options.Parent) ? ActivitySamplingResult.None : ActivitySamplingResult.PropagationData,
            };
            ActivitySource.AddActivityListener(_listener);
        }

        public void Dispose() => _listener?.Dispose();
    }
}
