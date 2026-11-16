using System.Diagnostics;
using Microsoft.Extensions.Logging;

namespace StaffOps.OtelLibs.Demo;

/// <summary>
/// The ADR-014 pattern every owning demo span uses (DR3, DE-D8): the span keeps status ERROR
/// without description, <c>error.type</c> = the short type name and one <c>exception</c> event
/// with <c>exception.type</c> only; the message and stack trace go to one ERROR log record written
/// while the span is current, so it carries the span's trace_id / span_id.
/// </summary>
public static class Failure
{
    /// <summary>The pinned <c>error.type</c> literal of an exception: its short type name.</summary>
    public static string ErrorType(Exception exception) => exception.GetType().Name;

    /// <summary>
    /// Records <paramref name="exception"/> on <paramref name="span"/> (type only) and logs its
    /// detail in an ERROR record whose body is the exception message. Call it while
    /// <paramref name="span"/> is <see cref="Activity.Current"/>, before any rethrow.
    /// </summary>
    public static void Record(Activity? span, Exception exception, ILogger logger, params (string Key, object? Value)[] attributes)
    {
        ArgumentNullException.ThrowIfNull(exception);
        ArgumentNullException.ThrowIfNull(logger);
        var type = ErrorType(exception);
        if (span is not null)
        {
            span.SetStatus(ActivityStatusCode.Error);
            span.SetTag("error.type", type);
            span.AddEvent(new ActivityEvent("exception",
                tags: new ActivityTagsCollection { ["exception.type"] = type }));
        }

        // The logging bridge writes exception.type / exception.message / exception.stacktrace.
        DemoLog.Write(logger, LogLevel.Error, exception.Message, exception, attributes);
    }

    /// <summary>
    /// Marks a parent demo span that only propagates a failure (DE-D8): status ERROR, no event, no log.
    /// </summary>
    public static void Propagate(Activity? span)
    {
        span?.SetStatus(ActivityStatusCode.Error);
    }
}

/// <summary>Log records with a fixed body and named attributes (the custom-signals pattern).</summary>
public static class DemoLog
{
    /// <summary>
    /// Writes one record whose body is <paramref name="message"/> and whose attributes are
    /// <paramref name="attributes"/>, passed as the record's state: no message template, so the
    /// body stays constant and the attributes keep their dotted names.
    /// </summary>
    public static void Write(ILogger logger, LogLevel level, string message, Exception? exception, params (string Key, object? Value)[] attributes)
    {
        ArgumentNullException.ThrowIfNull(logger);
        var state = new List<KeyValuePair<string, object?>>(attributes.Length);
        foreach (var (key, value) in attributes)
        {
            state.Add(new KeyValuePair<string, object?>(key, value));
        }

        logger.Log(level, default, state, exception, (_, _) => message);
    }
}
