using System.Diagnostics;
using System.Reflection;

namespace StaffOps.OtelHelper.Tracing
{
    /// <summary>
    /// ADR-014 for database client spans (task C14a round 2): an <c>exception</c> event keeps only
    /// <c>exception.type</c> (and any other non-detail attribute); its <c>exception.message</c> and
    /// <c>exception.stacktrace</c> go to one correlated log record. <see cref="Activity"/> has no API to
    /// remove or replace an event, and the OTLP serializer reads <see cref="Activity.EnumerateEvents"/>
    /// directly, so the event list is rebuilt through the private field <c>_events</c> (present in
    /// System.Diagnostics.DiagnosticSource 5.0 through 10.0; canary test). Without that field nothing
    /// is changed and the caller reports it (fail loud; the detail then stays on the span).
    /// </summary>
    internal static class DbExceptionEvents
    {
        internal const string EventName = "exception";
        internal const string TypeKey = "exception.type";
        internal const string MessageKey = "exception.message";
        internal const string StackTraceKey = "exception.stacktrace";

        /// <summary>The private event list of <see cref="Activity"/>, or null when this runtime has none.</summary>
        internal static readonly FieldInfo? ActivityEventsField = typeof(Activity).GetField("_events", BindingFlags.NonPublic | BindingFlags.Instance);

        /// <summary>What <see cref="Handle"/> did.</summary>
        internal enum Outcome
        {
            /// <summary>No exception event with detail.</summary>
            None,

            /// <summary>Each detailed exception event was logged and rewritten.</summary>
            Rewritten,

            /// <summary>Detail present but the event list cannot be rebuilt: nothing changed, nothing logged.</summary>
            Unsupported,
        }

        /// <summary>
        /// Rebuilds the span's events with <c>exception.message</c> and <c>exception.stacktrace</c>
        /// removed from each <c>exception</c> event (order, names, timestamps and the other attributes
        /// kept), then hands each of those events, as it was, to <paramref name="log"/>. Exactly once:
        /// the rewrite runs under a lock and re-checks for detail, so a concurrent second pass (two
        /// helper exporters, or the processor next to an exporter, on the same late-event span) finds
        /// none and logs nothing. Fail closed: the detail leaves the span before <paramref name="log"/>
        /// runs, so a logger that throws loses the record, never the redaction.
        /// </summary>
        internal static Outcome Handle(Activity activity, FieldInfo? eventsField, Action<Activity, ActivityEvent> log)
        {
            // Without the lock: most database spans carry no exception detail (code-review round 4).
            if (!AnyDetail(activity))
            {
                return Outcome.None;
            }

            if (eventsField is null)
            {
                return Outcome.Unsupported;
            }

            List<ActivityEvent> detailed;
            lock (Gate)
            {
                // A tiny section: re-check, rewrite, copy. The application's logger is called after it,
                // so a slow or blocking log sink never stalls another span's end.
                var events = new List<ActivityEvent>();
                foreach (ref readonly var e in activity.EnumerateEvents())
                {
                    events.Add(e);
                }

                detailed = events.Where(e => HasDetail(e)).ToList();
                if (detailed.Count == 0)
                {
                    return Outcome.None;
                }

                eventsField.SetValue(activity, null);
                foreach (var e in events)
                {
                    activity.AddEvent(HasDetail(e)
                        ? new ActivityEvent(e.Name, e.Timestamp, new ActivityTagsCollection(e.Tags.Where(t => t.Key is not (MessageKey or StackTraceKey))))
                        : e);
                }
            }

            foreach (var e in detailed)
            {
                log(activity, e);
            }

            return Outcome.Rewritten;
        }

        private static readonly object Gate = new();

        private static bool AnyDetail(Activity activity)
        {
            foreach (ref readonly var e in activity.EnumerateEvents())
            {
                if (HasDetail(e))
                {
                    return true;
                }
            }

            return false;
        }


        /// <summary>The value of <paramref name="key"/> in <paramref name="e"/> as text, or null.</summary>
        internal static string? Tag(in ActivityEvent e, string key)
        {
            foreach (var tag in e.Tags)
            {
                if (tag.Key == key)
                {
                    return tag.Value?.ToString();
                }
            }

            return null;
        }

        private static bool HasDetail(in ActivityEvent e)
            => e.Name == EventName && e.Tags.Any(t => t.Key is MessageKey or StackTraceKey);
    }
}
