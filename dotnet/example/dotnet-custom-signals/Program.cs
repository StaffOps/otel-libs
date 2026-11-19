// Custom signals with StaffOps.OtelHelper: spans, metrics and logs correlated
// with the active trace (task C17).
//
// A short-lived job that processes one batch of three simulated orders (ord-3
// fails) and exits. Same names and trace shape as the Go, Python and Node.js
// twins (go/example/go-custom-signals, python/example/python-custom-signals,
// nodejs/example/node-custom-signals):
//
// - spans: a batch root span (StartRootActivity) with one child span per order,
//   attributes, an event, status and the failure's type;
// - metrics: counter, histogram, up-down counter and observable gauge, each
//   with attributes;
// - logs: ILogger records, exported with the trace_id/span_id of the activity
//   that is current when the record is written.
//
// Exceptions (ADR-014): the failed span keeps status ERROR, error.type and an
// "exception" event with exception.type only; the message and the stack trace
// go to the "order failed" log record, written in the span's context, so it
// carries the same trace_id/span_id. On OTLP that record has the attributes
// exception.type, exception.message and exception.stacktrace; on the stdout
// fallback (no endpoint) one "exception" field (the exception's ToString()).
//
// Run it against a Collector (see ../README.md, "Custom signals"):
//
//   OTEL_SERVICE_NAME=dotnet-custom-signals \
//   OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4317 \
//   dotnet run --project dotnet/example/dotnet-custom-signals
//
// Without an endpoint, logs go to stdout as JSON and metrics to the Prometheus
// /metrics listener (gone when the process exits).
using System.Diagnostics;
using System.Diagnostics.Metrics;
using Microsoft.Extensions.DependencyInjection;
using Microsoft.Extensions.Hosting;
using Microsoft.Extensions.Logging;
using StaffOps.OtelHelper;

const string Scope = "orders"; // instrumentation scope of the ActivitySource and the Meter (as Go/Python/Node)

var builder = Host.CreateApplicationBuilder(args);
// The helper's exporter (or its stdout JSON fallback) is the only log output.
builder.Logging.ClearProviders();
builder.Services.AddOtelHelper(opts =>
{
    // A source or meter with another name than service.name is exported only when listed.
    opts.AdditionalActivitySources.Add(Scope);
    opts.AdditionalMeters.Add(Scope);
});
builder.Services.AddSingleton<OrderJob>();

using var host = builder.Build();
await host.StartAsync();

var exitCode = 0;
try
{
    await host.Services.GetRequiredService<OrderJob>()
        .RunAsync(host.Services.GetRequiredService<IHostApplicationLifetime>().ApplicationStopping);
}
catch (Exception ex)
{
    // The type only: an exception's message can carry data (ADR-014).
    Console.Error.WriteLine($"dotnet-custom-signals: failed with {ex.GetType().FullName}");
    exitCode = 1;
}

// Flush the batch span/log processors and export the last metric interval, then stop.
if (!host.Services.ForceFlushOtelHelper(5_000))
{
    Console.Error.WriteLine("telemetry flush incomplete");
}
await host.StopAsync();
return exitCode;

/// <summary>The business error of a failed charge.</summary>
internal sealed class PaymentDeclinedException(string orderId) : Exception("card declined by issuer")
{
    public string OrderId { get; } = orderId;
}

internal sealed record Order(string Id, string Kind, double Amount, bool Fail = false);

/// <summary>Processes one batch of orders with custom spans, metrics and logs.</summary>
internal sealed class OrderJob
{
    private static readonly ActivitySource Source = new("orders");
    private static readonly Meter Meter = new("orders");

    private static readonly Order[] Orders =
    [
        new("ord-1", "standard", 42.5),
        new("ord-2", "express", 99.9),
        new("ord-3", "standard", 10.5, Fail: true),
    ];

    // Instrument names follow OTel conventions: dot-separated, unit in `unit`, no
    // `_total` suffix (the Prometheus exporter adds it to counters).
    private static readonly Counter<long> Processed =
        Meter.CreateCounter<long>("order.processed", "{order}", "Orders processed");
    // Second-based histograms need their own boundaries: the SDK defaults
    // (0, 5, 10, ... 10000) suit milliseconds. InstrumentAdvice is honored by the SDK.
    private static readonly Histogram<double> Duration = Meter.CreateHistogram<double>(
        "order.duration", "s", "Time spent processing one order", tags: null,
        advice: new InstrumentAdvice<double> { HistogramBucketBoundaries = [0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1] });
    private static readonly UpDownCounter<long> Active =
        Meter.CreateUpDownCounter<long>("order.active", "{order}", "Orders being processed now");

    // State read by the observable gauge callback (the SDK calls it at every
    // collection: export interval, scrape, flush).
    private static int _queueDepth;

    private readonly ILogger _logger;

    public OrderJob(ILoggerFactory loggers)
    {
        // The category is the log records' instrumentation scope.
        _logger = loggers.CreateLogger("dotnet-custom-signals");
        Meter.CreateObservableGauge("order.queue.depth",
            () => new Measurement<int>(Volatile.Read(ref _queueDepth), new KeyValuePair<string, object?>("queue.name", "orders")),
            "{order}", "Orders waiting in the queue");
    }

    public async Task RunAsync(CancellationToken stopping)
    {
        Volatile.Write(ref _queueDepth, Orders.Length);

        // A root span starts a new trace, whatever activity is current (workers,
        // consumers, cron jobs: one trace per unit of work).
        using var batch = Source.StartRootActivity("orders.process-batch");
        batch?.SetTag("batch.size", Orders.Length);
        Console.WriteLine($"trace_id={batch?.TraceId}");

        var failed = 0;
        foreach (var order in Orders)
        {
            if (stopping.IsCancellationRequested) // SIGTERM / Ctrl-C: stop taking orders, still flush
            {
                Log(LogLevel.Warning, "batch interrupted", null, ("orders.pending", Volatile.Read(ref _queueDepth)));
                break;
            }

            if (!await ProcessOrderAsync(order))
            {
                failed++;
            }

            Interlocked.Decrement(ref _queueDepth);
        }

        if (failed > 0)
        {
            // Status only: the failure itself was recorded on the child span.
            batch?.SetStatus(ActivityStatusCode.Error, $"{failed} of {Orders.Length} orders failed");
        }

        // Correlated with orders.process-batch (the current activity again).
        Log(LogLevel.Information, "batch finished", null, ("batch.size", Orders.Length), ("orders.failed", failed));
    }

    /// <summary>Processes one order in a child span of the batch span; false when it failed.</summary>
    private async Task<bool> ProcessOrderAsync(Order order)
    {
        using var span = Source.StartActivity("order.process");
        span?.SetTag("order.id", order.Id); // fine on spans; never as a metric attribute
        span?.SetTag("order.type", order.Kind);

        var started = Stopwatch.GetTimestamp();
        var type = new KeyValuePair<string, object?>("order.type", order.Kind);
        Active.Add(1, type);
        var tags = new TagList { type };
        try
        {
            // An event is a timestamped annotation inside the span.
            span?.AddEvent(new ActivityEvent("order.validated",
                tags: new ActivityTagsCollection { ["order.amount"] = order.Amount }));
            await Task.Delay(20); // simulated call to a payment provider

            Charge(order);
            Log(LogLevel.Information, "order processed", null, ("order.id", order.Id), ("order.amount", order.Amount));
            return true;
        }
        catch (PaymentDeclinedException ex)
        {
            // ADR-014: on the span the type only (status, error.type, an exception
            // event without message or stack trace) ...
            // The short type name, as the Go, Python and Node.js twins write it
            // (PaymentDeclinedError there): the class name without its namespace.
            var errorType = ex.GetType().Name;
            span?.SetStatus(ActivityStatusCode.Error, "payment declined");
            span?.SetTag("error.type", errorType);
            span?.AddEvent(new ActivityEvent("exception",
                tags: new ActivityTagsCollection { ["exception.type"] = ex.GetType().FullName }));
            // error.type only on failure, low-cardinality: the exception's type name.
            tags.Add("error.type", errorType);
            // ... the detail in this record, written while order.process is current: it
            // carries the span's trace_id/span_id; OTLP: exception.type/message/stacktrace
            // attributes; stdout fallback: one "exception" field.
            Log(LogLevel.Error, "order failed", ex, ("order.id", order.Id));
            return false;
        }
        finally
        {
            Active.Add(-1, type);
            Duration.Record(Stopwatch.GetElapsedTime(started).TotalSeconds, tags);
            Processed.Add(1, tags);
        }
    }

    private static void Charge(Order order)
    {
        if (order.Fail)
        {
            throw new PaymentDeclinedException(order.Id);
        }
    }

    /// <summary>
    /// A record whose body is the fixed message and whose attributes carry the given
    /// names, passed as the record's state (a list of key/value pairs): the OTLP record
    /// gets them as attributes and the stdout fallback writes them as fields. A message
    /// template would put "{order.id}" placeholders into the body instead; a logging
    /// scope would reach OTLP only (the fallback writes no scopes).
    /// </summary>
    private void Log(LogLevel level, string message, Exception? exception, params (string Key, object? Value)[] attributes)
    {
        var state = attributes.Select(a => new KeyValuePair<string, object?>(a.Key, a.Value)).ToList();
        _logger.Log(level, default, state, exception, (_, _) => message);
    }
}
