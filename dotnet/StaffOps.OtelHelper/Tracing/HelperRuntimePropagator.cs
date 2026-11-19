using System.Diagnostics;

namespace StaffOps.OtelHelper.Tracing
{
    /// <summary>What the runtime propagation path carries (design D-NET9, fix round 1 of C10).</summary>
    /// <param name="TraceContext"><c>tracecontext</c> is in <c>OTEL_PROPAGATORS</c>: <c>traceparent</c>/<c>tracestate</c>.</param>
    /// <param name="Baggage"><c>baggage</c> is in <c>OTEL_PROPAGATORS</c>: <c>baggage</c> (and the legacy <c>Correlation-Context</c>).</param>
    /// <param name="PassThroughOnly">
    /// Traces disabled (<c>DisabledSignals</c>) or <c>OTEL_SDK_DISABLED=true</c>: as Go's and Python's no-op
    /// tracer, a trace started here carries no context, and a trace continued here is passed on exactly as
    /// it arrived (the <c>traceparent</c> of the local root's remote parent, and its tracestate).
    /// </param>
    internal sealed record PropagationSettings(bool TraceContext, bool Baggage, bool PassThroughOnly)
    {
        /// <summary>The 0.2.x and default behavior: everything the runtime propagator does.</summary>
        internal static readonly PropagationSettings Default = new(true, true, false);

        internal bool IsDefault => this == Default;

        internal static PropagationSettings From(ResolvedTelemetry resolved) => new(
            resolved.Propagators.Contains("tracecontext"),
            resolved.Propagators.Contains("baggage"),
            resolved.SdkDisabled || !resolved.IsSignalEnabled("traces"));

        /// <summary>
        /// The context a no-op tracer would hand on for <paramref name="activity"/>: the remote parent of
        /// its local root (inbound <c>traceparent</c>, flags kept), or none for a trace started here.
        /// </summary>
        internal static ActivityContext PassThrough(Activity activity)
        {
            var root = activity;
            while (root.Parent is not null)
            {
                root = root.Parent;
            }

            // Only a remote parent is "what arrived": an in-process parent given as an explicit context is not.
            return root.HasRemoteParent && root.ParentId is { } parentId && ActivityContext.TryParse(parentId, root.TraceStateString, isRemote: true, out var context)
                ? context
                : default;
        }
    }

    /// <summary>
    /// The runtime propagator (<see cref="DistributedContextPropagator"/>) that ASP.NET Core extracts with
    /// and <c>HttpClient</c> injects with, restricted to <see cref="PropagationSettings"/>: it delegates to
    /// the runtime default and drops what is not configured, in both directions. With
    /// <see cref="PropagationSettings.Default"/> it is the runtime default unchanged.
    /// </summary>
    internal sealed class HelperRuntimePropagator : DistributedContextPropagator
    {
        private static readonly DistributedContextPropagator Inner = CreateDefaultPropagator();
        private static readonly string[] TraceFields = { "traceparent", "tracestate", "Request-Id" };
        private static readonly string[] BaggageFields = { "baggage", "Correlation-Context" };

        private static volatile PropagationSettings s_global = PropagationSettings.Default;

        private readonly Func<PropagationSettings> _settings;

        internal HelperRuntimePropagator(Func<PropagationSettings> settings) => _settings = settings;

        /// <summary>The instance set as <see cref="DistributedContextPropagator.Current"/>; it follows the last container resolved.</summary>
        internal static HelperRuntimePropagator Global { get; } = new(() => s_global);

        internal static PropagationSettings GlobalSettings
        {
            get => s_global;
            set => s_global = value;
        }

        internal static bool IsRuntimeDefault(DistributedContextPropagator propagator)
            => propagator.GetType() == Inner.GetType();

        public override IReadOnlyCollection<string> Fields
        {
            get
            {
                var s = _settings();
                return Inner.Fields.Where(f => Allowed(s, f)).ToArray();
            }
        }

        public override void Inject(Activity? activity, object? carrier, PropagatorSetterCallback? setter)
        {
            var s = _settings();
            if (setter is null || activity is null)
            {
                return;
            }

            if (HealthMatching.IsClientHealthRequest(carrier))
            {
                // A health request names no span (C11): baggage still goes, trace fields do not.
                var write = setter;
                setter = (c, key, value) =>
                {
                    if (Array.IndexOf(TraceFields, key) < 0)
                    {
                        write(c, key, value);
                    }
                };
            }

            if (s.IsDefault)
            {
                Inner.Inject(activity, carrier, setter);
                return;
            }

            if (s.TraceContext && s.PassThroughOnly)
            {
                var context = PropagationSettings.PassThrough(activity);
                if (context != default)
                {
                    setter(carrier, "traceparent", $"00-{context.TraceId.ToHexString()}-{context.SpanId.ToHexString()}-{(byte)context.TraceFlags:x2}");
                    if (!string.IsNullOrEmpty(context.TraceState))
                    {
                        setter(carrier, "tracestate", context.TraceState);
                    }
                }
            }

            Inner.Inject(activity, carrier, (c, key, value) =>
            {
                var trace = Array.IndexOf(TraceFields, key) >= 0;
                if ((trace && s.TraceContext && !s.PassThroughOnly) || (!trace && Allowed(s, key)))
                {
                    setter(c, key, value);
                }
            });
        }

        public override void ExtractTraceIdAndState(object? carrier, PropagatorGetterCallback? getter, out string? traceId, out string? traceState)
        {
            if (_settings().TraceContext)
            {
                Inner.ExtractTraceIdAndState(carrier, getter, out traceId, out traceState);
                return;
            }

            traceId = null;
            traceState = null;
        }

        public override IEnumerable<KeyValuePair<string, string?>>? ExtractBaggage(object? carrier, PropagatorGetterCallback? getter)
            => _settings().Baggage ? Inner.ExtractBaggage(carrier, getter) : null;

        private static bool Allowed(PropagationSettings s, string field)
            => Array.IndexOf(TraceFields, field) >= 0 ? s.TraceContext
                : Array.IndexOf(BaggageFields, field) < 0 || s.Baggage;
    }
}
