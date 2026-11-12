using System.Diagnostics;
using System.Diagnostics.Metrics;
using Microsoft.Extensions.DependencyInjection;
using StaffOps.OtelHelper;

/// <summary>
/// Simulates consuming messages from a queue (SQS/Kafka pattern).
/// Each message generates an independent trace. Demonstrates StartRootActivity in consumer loops.
/// </summary>
public class QueueConsumerWorker : BackgroundService
{
    private static readonly ActivitySource _activity = new("sample-process.queue-consumer");

    private readonly Counter<long> _messagesProcessed;
    private readonly Counter<long> _messagesFailed;
    private readonly Histogram<double> _processingDuration;

    private static int _queueDepth = 0;

    private readonly ILogger<QueueConsumerWorker> _logger;
    private readonly TimeSpan _pollInterval = TimeSpan.FromSeconds(30);

    public QueueConsumerWorker(ILogger<QueueConsumerWorker> logger, [FromKeyedServices(OtelHelperKeys.Instrumentation)] Meter meter)
    {
        // The helper's Meter, keyed (name = the resolved service.name, so it is always exported).
        _messagesProcessed = meter.CreateCounter<long>("queue.messages_processed_total", "messages", "Total messages processed");
        _messagesFailed = meter.CreateCounter<long>("queue.messages_failed_total", "messages", "Total messages failed");
        _processingDuration = meter.CreateHistogram<double>("queue.message_duration_seconds", "s", "Duration of message processing");
        _logger = logger;
        meter.CreateObservableGauge("queue.depth", () => _queueDepth, "messages", "Current queue depth");
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
                    // ADR-014: the span carries the failure and its type only; the message
                    // (here it holds the message id) and the stack trace go to the log record
                    // below, written in the span's context (same trace_id/span_id).
                    span?.SetStatus(ActivityStatusCode.Error);
                    span?.SetTag("error.type", ex.GetType().FullName);
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
