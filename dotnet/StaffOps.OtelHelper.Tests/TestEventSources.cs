using System.Diagnostics.Tracing;

namespace StaffOps.OtelHelper.Tests;

[EventSource(Name = "OpenTelemetry-StaffOps-Test")]
internal sealed class TestOtelEventSource : EventSource
{
    public static readonly TestOtelEventSource Log = new();

    [Event(1, Level = EventLevel.Warning, Message = "export to {0} failed: {1}")]
    public void ExportFailed(string endpoint, string reason) => WriteEvent(1, endpoint, reason);

    [Event(2, Level = EventLevel.Informational, Message = "info {0}")]
    public void Info(string text) => WriteEvent(2, text);

    [Event(3, Level = EventLevel.Error, Message = "boom {0}")]
    public void Boom(string text) => WriteEvent(3, text);

    [Event(4, Level = EventLevel.Warning, Message = "broken {5} template")]
    public void Broken(string text) => WriteEvent(4, text);

    [Event(5, Level = EventLevel.Critical)]
    public void NoMessage(string a, int b) => WriteEvent(5, a, b);
}

[EventSource(Name = "Other-StaffOps-Test")]
internal sealed class OtherEventSource : EventSource
{
    public static readonly OtherEventSource Log = new();

    [Event(1, Level = EventLevel.Warning, Message = "other {0}")]
    public void Warn(string text) => WriteEvent(1, text);
}
