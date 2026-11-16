using System.Diagnostics;
using Microsoft.Extensions.Logging;
using OpenTelemetry;
using OpenTelemetry.Context.Propagation;

// The tests share process-wide state (activity listeners, the default propagator).
[assembly: Xunit.CollectionBehavior(DisableTestParallelization = true)]

namespace StaffOps.OtelLibs.Demo.Tests;

/// <summary>One captured log record.</summary>
internal sealed record LogEntry(LogLevel Level, string Message, Exception? Exception, IReadOnlyDictionary<string, object?> Attributes, string? TraceId, string? SpanId);

/// <summary>An ILogger that keeps every record, with the activity current when it was written.</summary>
internal sealed class RecordingLogger<T> : ILogger<T>
{
    public List<LogEntry> Entries { get; } = [];

    public IDisposable? BeginScope<TState>(TState state) where TState : notnull => null;

    public bool IsEnabled(LogLevel logLevel) => true;

    public void Log<TState>(LogLevel logLevel, EventId eventId, TState state, Exception? exception, Func<TState, Exception?, string> formatter)
    {
        var attributes = new Dictionary<string, object?>();
        if (state is IEnumerable<KeyValuePair<string, object?>> pairs)
        {
            foreach (var (key, value) in pairs)
            {
                attributes[key] = value;
            }
        }

        Entries.Add(new LogEntry(logLevel, formatter(state, exception), exception, attributes,
            Activity.Current?.TraceId.ToHexString(), Activity.Current?.SpanId.ToHexString()));
    }
}

/// <summary>Records every demo span started while it lives.</summary>
internal sealed class SpanCollector : IDisposable
{
    private readonly ActivityListener _listener;

    public SpanCollector()
    {
        // W3C TraceContext + Baggage, as the helper configures by default.
        Sdk.SetDefaultTextMapPropagator(new CompositeTextMapPropagator([new TraceContextPropagator(), new BaggagePropagator()]));
        _listener = new ActivityListener
        {
            ShouldListenTo = source => source.Name == DemoTelemetry.ScopeName,
            Sample = (ref ActivityCreationOptions<ActivityContext> _) => ActivitySamplingResult.AllDataAndRecorded,
            ActivityStarted = activity => Started.Add(activity),
        };
        ActivitySource.AddActivityListener(_listener);
    }

    public List<Activity> Started { get; } = [];

    public Activity Single(string name) => Assert.Single(Started, a => a.OperationName == name);

    public void Dispose() => _listener.Dispose();
}
