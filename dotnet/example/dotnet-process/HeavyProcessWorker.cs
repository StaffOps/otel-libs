using System.Diagnostics;
using System.Diagnostics.Metrics;
using System.Security.Cryptography;
using System.Text;
using OtelHelper;

/// <summary>
/// Simulates heavy batch processing — CPU-bound hashing, memory allocation, concurrent work.
/// Runs every 2 minutes to generate observable stress patterns.
/// </summary>
public class HeavyProcessWorker : BackgroundService
{
    private static readonly ActivitySource _activity = new("sample-process.heavy-work");
    private static readonly Meter _meter = new(
        Environment.GetEnvironmentVariable("SERVICE_NAME") ?? "sample-process");

    private static readonly Counter<long> _batchesTotal = _meter.CreateCounter<long>(
        "heavy_work.batches_total", "batches", "Total batches processed");
    private static readonly Counter<long> _itemsTotal = _meter.CreateCounter<long>(
        "heavy_work.items_total", "items", "Total items processed");
    private static readonly Counter<long> _errorsTotal = _meter.CreateCounter<long>(
        "heavy_work.errors_total", "errors", "Total processing errors");
    private static readonly Histogram<double> _batchDuration = _meter.CreateHistogram<double>(
        "heavy_work.batch_duration_seconds", "s", "Duration of each batch");
    private static readonly Histogram<double> _itemDuration = _meter.CreateHistogram<double>(
        "heavy_work.item_duration_seconds", "s", "Duration of each item");

    private static int _activeItems;
    private static readonly object _lock = new();

    private readonly ILogger<HeavyProcessWorker> _logger;
    private readonly TimeSpan _interval = TimeSpan.FromMinutes(2);

    public HeavyProcessWorker(ILogger<HeavyProcessWorker> logger)
    {
        _logger = logger;
        _meter.CreateObservableGauge("heavy_work.items_active", () => _activeItems,
            "items", "Items currently being processed");
    }

    protected override async Task ExecuteAsync(CancellationToken ct)
    {
        await Task.Delay(10000, ct);

        while (!ct.IsCancellationRequested)
        {
            var batchSw = Stopwatch.StartNew();
            var batchSize = Random.Shared.Next(8, 20);

            using (var batchSpan = _activity.StartRootActivity("process.heavy-batch"))
            {
                batchSpan?.SetTag("batch.size", batchSize);

                _logger.LogInformation("Starting heavy batch: {BatchSize} items", batchSize);

                // Process items in parallel (fan-out)
                var tasks = Enumerable.Range(0, batchSize).Select(i => ProcessItem(i, ct));
                await Task.WhenAll(tasks);

                _batchesTotal.Add(1);
                _batchDuration.Record(batchSw.Elapsed.TotalSeconds);
                _logger.LogInformation("Batch completed: {BatchSize} items in {Duration}ms",
                    batchSize, batchSw.ElapsedMilliseconds);
            }

            await Task.Delay(_interval, ct);
        }
    }

    private async Task ProcessItem(int itemId, CancellationToken ct)
    {
        Interlocked.Increment(ref _activeItems);
        var sw = Stopwatch.StartNew();

        using var span = _activity.StartActivity("process.heavy-process-item");
        span?.SetTag("item.id", itemId);

        try
        {
            // CPU stress — hash a large string
            var data = new string('x', 50_000);
            for (int i = 0; i < 2000; i++)
            {
                using var sha = SHA256.Create();
                sha.ComputeHash(Encoding.UTF8.GetBytes(data + i));
            }

            // Memory allocation stress
            var buffers = new List<byte[]>();
            for (int i = 0; i < 500; i++)
                buffers.Add(new byte[Random.Shared.Next(4096, 32768)]);
            await Task.Delay(Random.Shared.Next(50, 200), ct);
            buffers.Clear();

            // 3. Simulate random failures (5%)
            if (Random.Shared.Next(100) < 5)
            {
                _errorsTotal.Add(1);
                span?.SetStatus(ActivityStatusCode.Error, "Random processing failure");
                _logger.LogError("Item {ItemId} failed during processing", itemId);
                return;
            }

            _itemsTotal.Add(1, new KeyValuePair<string, object?>("status", "success"));
        }
        catch (Exception ex)
        {
            _errorsTotal.Add(1);
            span?.SetStatus(ActivityStatusCode.Error, ex.Message);
            _logger.LogError(ex, "Item {ItemId} threw exception", itemId);
        }
        finally
        {
            Interlocked.Decrement(ref _activeItems);
            _itemDuration.Record(sw.Elapsed.TotalSeconds);
        }
    }
}
