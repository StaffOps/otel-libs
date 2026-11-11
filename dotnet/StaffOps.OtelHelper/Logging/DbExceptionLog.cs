using System.Diagnostics;
using Microsoft.Extensions.Logging;
using OpenTelemetry;
using OpenTelemetry.Logs;

namespace StaffOps.OtelHelper.Logging
{
    /// <summary>
    /// The correlated log record of ADR-014 (task C14a round 2): one record per detailed
    /// <c>exception</c> event of a database client span, through the application's
    /// <see cref="ILogger"/> pipeline (OTLP when a logs exporter is active, else the C13 stdout line;
    /// the helper's level rules apply). Category <see cref="Category"/>, level Error, the message as
    /// the body, attributes <c>exception.type</c>, <c>exception.message</c>,
    /// <c>exception.stacktrace</c>. The span's ids travel as the attributes <c>trace_id</c> and
    /// <c>span_id</c>: <see cref="CorrelationProcessor"/> moves them into the OTLP record's trace
    /// context (the record is written after the span ended, often on the exporter's thread, where
    /// <see cref="Activity.Current"/> is not the span); the stdout line keeps them as fields.
    /// </summary>
    internal static class DbExceptionLog
    {
        /// <summary>The logger category of the records (filter it like any category).</summary>
        internal const string Category = "StaffOps.OtelHelper.Database";

        internal const string TraceIdKey = "trace_id";
        internal const string SpanIdKey = "span_id";

        /// <summary>The exception event's time (ISO 8601, UTC): the record's timestamp on OTLP and the stdout <c>time</c> (removed from the attributes).</summary>
        internal const string EventTimeKey = "otelhelper.event_time";

        /// <summary>Writes the record for <paramref name="e"/> of <paramref name="span"/> to <paramref name="logger"/>.</summary>
        internal static void Write(ILogger logger, Activity span, in ActivityEvent e)
        {
            if (!logger.IsEnabled(LogLevel.Error))
            {
                return;
            }

            var type = Tracing.DbExceptionEvents.Tag(e, Tracing.DbExceptionEvents.TypeKey);
            var message = Tracing.DbExceptionEvents.Tag(e, Tracing.DbExceptionEvents.MessageKey);
            var state = new List<KeyValuePair<string, object?>>
            {
                new(Tracing.DbExceptionEvents.TypeKey, type),
                new(Tracing.DbExceptionEvents.MessageKey, message),
                new(Tracing.DbExceptionEvents.StackTraceKey, Tracing.DbExceptionEvents.Tag(e, Tracing.DbExceptionEvents.StackTraceKey)),
                new(TraceIdKey, span.TraceId.ToHexString()),
                new(SpanIdKey, span.SpanId.ToHexString()),
                new(EventTimeKey, e.Timestamp.UtcDateTime.ToString("O", System.Globalization.CultureInfo.InvariantCulture)),
            };
            var body = message ?? type ?? "database client exception";

            // Not under SuppressInstrumentation on the exporter path: the helper's exporters sanitize
            // before the OTLP exporter enters its suppression scope (OtlpTraceExporter.Export); pinned by
            // the test Late_Event_Record_Reaches_The_Log_Pipeline_Through_The_Helper_Exporter.
            logger.Log(LogLevel.Error, default, state, null, (_, _) => body);
        }

        /// <summary>The span context and event time a record of <see cref="Category"/> names, when every value is valid.</summary>
        internal static bool TryReadContext(string? category, IEnumerable<KeyValuePair<string, object?>> attributes, out ActivityTraceId traceId, out ActivitySpanId spanId, out DateTime? eventTime)
        {
            traceId = default;
            spanId = default;
            eventTime = null;
            if (category != Category)
            {
                return false;
            }

            string? trace = null, span = null, time = null;
            foreach (var (key, value) in attributes)
            {
                switch (key)
                {
                    case TraceIdKey:
                        trace = value as string;
                        break;
                    case SpanIdKey:
                        span = value as string;
                        break;
                    case EventTimeKey:
                        time = value as string;
                        break;
                }
            }

            if (!IsHexId(trace, 32) || !IsHexId(span, 16))
            {
                return false;
            }

            traceId = ActivityTraceId.CreateFromString(trace!);
            spanId = ActivitySpanId.CreateFromString(span!);
            if (DateTime.TryParse(time, System.Globalization.CultureInfo.InvariantCulture, System.Globalization.DateTimeStyles.RoundtripKind, out var parsed))
            {
                eventTime = parsed.ToUniversalTime();
            }

            return true;
        }

        /// <summary>The attributes without the keys this record uses for transport.</summary>
        internal static bool IsTransportKey(string key) => key is TraceIdKey or SpanIdKey or EventTimeKey;

        /// <summary><paramref name="length"/> lower-case hex digits, not all zero (W3C trace context).</summary>
        internal static bool IsHexId(string? value, int length)
        {
            if (value is null || value.Length != length)
            {
                return false;
            }

            var nonZero = false;
            foreach (var c in value)
            {
                if (c is not ((>= '0' and <= '9') or (>= 'a' and <= 'f')))
                {
                    return false;
                }

                nonZero |= c != '0';
            }

            return nonZero;
        }

        /// <summary>
        /// Gives a record of <see cref="Category"/> the trace context and timestamp named by its
        /// transport attributes and removes them; other records, and a record whose ids are not valid
        /// lower-case non-zero hex, are left alone. Added before the helper's log exporters.
        /// </summary>
        internal sealed class CorrelationProcessor : BaseProcessor<LogRecord>
        {
            public override void OnEnd(LogRecord data)
            {
                if (data.Attributes is not { } attributes
                    || !TryReadContext(data.CategoryName, attributes, out var traceId, out var spanId, out var eventTime))
                {
                    return;
                }

                data.TraceId = traceId;
                data.SpanId = spanId;
                data.TraceFlags = ActivityTraceFlags.Recorded;
                if (eventTime is { } time)
                {
                    data.Timestamp = time;
                }

                data.Attributes = attributes.Where(a => !IsTransportKey(a.Key)).ToList();
            }
        }
    }
}
