using System.Diagnostics;
using OpenTelemetry;
using OpenTelemetry.Context.Propagation;

namespace StaffOps.OtelLibs.Demo;

/// <summary>
/// W3C context in queue messages (DR2, DE-D6): the producer injects the current context with the
/// global propagator into the message fields (<c>traceparent</c>, <c>tracestate</c>,
/// <c>baggage</c>); the consumer extracts it from them.
/// </summary>
public static class MessageCarrier
{
    /// <summary>The propagation fields of <paramref name="context"/> and <paramref name="baggage"/>.</summary>
    public static Dictionary<string, string> Inject(ActivityContext context, Baggage baggage, TextMapPropagator? propagator = null)
    {
        var fields = new Dictionary<string, string>(StringComparer.Ordinal);
        (propagator ?? Propagators.DefaultTextMapPropagator).Inject(new PropagationContext(context, baggage), fields,
            static (carrier, key, value) => carrier[key] = value);
        return fields;
    }

    /// <summary>The context carried by <paramref name="fields"/>; <c>default</c> parts when absent.</summary>
    public static PropagationContext Extract(IReadOnlyDictionary<string, string> fields, TextMapPropagator? propagator = null)
    {
        ArgumentNullException.ThrowIfNull(fields);
        return (propagator ?? Propagators.DefaultTextMapPropagator).Extract(default, fields,
            static (carrier, key) => carrier.TryGetValue(key, out var value) ? [value] : []);
    }
}
