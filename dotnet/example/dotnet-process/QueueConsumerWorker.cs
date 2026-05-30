using System.Diagnostics;
using System.Diagnostics.Metrics;
using OtelHelper;

/// <summary>
/// Simulates consuming messages from a queue (SQS/Kafka pattern).
/// Each message generates an independent trace. Demonstrates StartRootActivity in consumer loops.
/// </summary>
public class QueueConsumerWorker : BackgroundService
{
    private static readonly ActivitySource _activity = new("sample-process.queue-consumer");
    private static readonly Meter _meter = new(
        Environment.GetEnvironmentVariable("SERVICE_NAME") ?? "sample-process");

    private static readonly Counter<long> _messagesProcessed = _meter.CreateCounter<long>(
        "queue.messages_processed_total", "messages", "Total messages processed");
    private static readonly Counter<long> _messagesFailed = _meter.CreateCounter<long>(
        "queue.messages_failed_total", "messages", "Total messages failed");
    private static readonly Histogram<double> _processingDuration = _meter.CreateHistogram<double>(
        "queue.message_duration_seconds", "s", "Duration of message processing");

    private static int _queueDepth = 0;

    private readonly ILogger<QueueConsumerWorker> _logger;
    private readonly TimeSpan _pollInterval = TimeSpan.FromSeconds(30);

    public QueueConsumerWorker(ILogger<QueueConsumerWorker> logger)
    {
        _logger = logger;
        _meter.CreateObservableGauge("queue.depth", () => _queueDepth, "messages", "Current queue depth");
    }

    protected override async Task ExecuteAsync(CancellationToken ct)
    {
        await Task.Delay(15000, ct);

        while (!ct.IsCancellationRequested)
        {
            // Simulate polling a queue — get 1-5 messages per poll
            var messageCount = Random.Shared.Next(1, 6);
            _queueDepth = messageCount;

            _logger.LogInformation("Polled queue: {Count} messages available", messageCount);

            for (int i = 0; i < messageCount; i++)
            {
                var messageId = $"msg-{Guid.NewGuid():N}".Substring(0, 12);

                // Each message is an independent trace
                using var span = _activity.StartRootActivity("process.queue-consume");
                span?.SetTag("messaging.message_id", messageId);
                span?.SetTag("messaging.system", "sqs");
                span?.SetTag("messaging.destination", "orders-queue");

                var sw = Stopwatch.StartNew();
                try
                {
                    // Simulate message processing
                    await ProcessMessage(messageId, ct);

                    _messagesProcessed.Add(1, new KeyValuePair<string, object?>("status", "success"));
                    _logger.LogInformation("Message {MessageId} processed successfully", messageId);
                }
                catch (Exception ex)
                {
                    span?.SetStatus(ActivityStatusCode.Error, ex.Message);
                    _messagesFailed.Add(1);
                    _logger.LogError(ex, "Message {MessageId} processing failed", messageId);
                }
                finally
                {
                    _processingDuration.Record(sw.Elapsed.TotalSeconds);
                    _queueDepth = Math.Max(0, _queueDepth - 1);
                }
            }

            await Task.Delay(_pollInterval, ct);
        }
    }

    private async Task ProcessMessage(string messageId, CancellationToken ct)
    {
        // Simulate work: parse → validate → persist
        using (var parseSpan = _activity.StartActivity("process.queue-parse"))
        {
            parseSpan?.SetTag("message.id", messageId);
            await Task.Delay(Random.Shared.Next(5, 20), ct);
        }

        using (var validateSpan = _activity.StartActivity("process.queue-validate"))
        {
            await Task.Delay(Random.Shared.Next(10, 50), ct);

            // 10% chance of validation failure
            if (Random.Shared.Next(100) < 10)
                throw new InvalidOperationException($"Validation failed for message {messageId}");
        }

        using (var persistSpan = _activity.StartActivity("process.queue-persist"))
        {
            persistSpan?.SetTag("db.system", "dynamodb");
            await Task.Delay(Random.Shared.Next(20, 80), ct);
        }
    }
}
