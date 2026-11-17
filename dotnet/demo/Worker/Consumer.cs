using System.Diagnostics;
using OpenTelemetry;
using StackExchange.Redis;

namespace StaffOps.OtelLibs.Demo.Worker;

/// <summary>
/// The queue consumer (DE-D5, DE-D6): every <c>DEMO_POLL_SECONDS</c> (2 s) one
/// <c>XREADGROUP COUNT 10</c> without <c>BLOCK</c>, not traced (r3.6); per message one CONSUMER span
/// <c>process demo:dotnet:orders</c> with the producer's context as parent and as its one link,
/// outside any other span, and the <c>XACK</c> inside it.
/// </summary>
public sealed class Consumer(IConnectionMultiplexer redis, ILogger<Consumer> logger, Consumer.Settings settings) : BackgroundService
{
    /// <summary>Per-process settings of the consumer.</summary>
    /// <param name="ConsumerName">This worker's consumer name in the group (the pod name).</param>
    /// <param name="Poll">Interval between polls.</param>
    /// <param name="RedisHost">server.address of the CONSUMER spans.</param>
    /// <param name="RedisPort">server.port of the CONSUMER spans.</param>
    public sealed record Settings(string ConsumerName, TimeSpan Poll, string RedisHost, int RedisPort);

    /// <summary>Messages read per poll.</summary>
    public const int BatchSize = 10;

    private bool _groupReady;

    /// <summary>The message's fields as a dictionary (the first value of a repeated name wins).</summary>
    public static Dictionary<string, string> Fields(StreamEntry entry)
    {
        var fields = new Dictionary<string, string>(StringComparer.Ordinal);
        foreach (var value in entry.Values)
        {
            fields.TryAdd(value.Name.ToString(), value.Value.ToString());
        }

        return fields;
    }

    /// <summary>The start attributes of the CONSUMER span of message <paramref name="messageId"/>.</summary>
    public static KeyValuePair<string, object?>[] ConsumerTags(string messageId, string host, int port) =>
    [
        new("messaging.system", "redis"),
        new("messaging.operation.name", "process"),
        new("messaging.operation.type", "process"),
        new("messaging.destination.name", DemoSettings.StreamKey),
        new("messaging.message.id", messageId),
        new("messaging.consumer.group.name", DemoSettings.ConsumerGroup),
        new("server.address", host),
        new("server.port", port),
    ];

    /// <summary>Starts the CONSUMER span of a message carrying <paramref name="fields"/>.</summary>
    public Activity? StartProcessSpan(string messageId, IReadOnlyDictionary<string, string> fields)
    {
        var parent = MessageCarrier.Extract(fields);
        // Outside any other span (DE-D6): the parent is the producer's context only.
        Activity.Current = null;
        Baggage.Current = parent.Baggage;
        ActivityLink[]? links = parent.ActivityContext.IsValid() ? [new ActivityLink(parent.ActivityContext)] : null;
        return DemoTelemetry.Source.StartActivity($"process {DemoSettings.StreamKey}", ActivityKind.Consumer,
            parent.ActivityContext, ConsumerTags(messageId, settings.RedisHost, settings.RedisPort), links);
    }

    /// <summary>One poll: read up to <see cref="BatchSize"/> new messages and process each.</summary>
    public async Task PollAsync(CancellationToken cancellationToken)
    {
        var db = redis.GetDatabase();
        // The poll itself is not traced (DE-D11 r3.6): an empty XREADGROUP gives no span; only a
        // received message gets one (its CONSUMER span, with the XACK under it).
        var entries = await QuietTracing.RunAsync(() => ReadAsync(db));

        foreach (var entry in entries)
        {
            cancellationToken.ThrowIfCancellationRequested();
            await ProcessAsync(db, entry);
        }
    }

    /// <summary>XREADGROUP (group created on first use; recreated after NOGROUP, r3.5).</summary>
    private async Task<StreamEntry[]> ReadAsync(IDatabase db)
    {
        if (!_groupReady)
        {
            await EnsureGroupAsync(db);
        }

        try
        {
            return await db.StreamReadGroupAsync(DemoSettings.StreamKey, DemoSettings.ConsumerGroup,
                settings.ConsumerName, StreamPosition.NewMessages, BatchSize);
        }
        catch (RedisServerException ex) when (ex.Message.StartsWith("NOGROUP", StringComparison.Ordinal))
        {
            // The stream or group was deleted: recreate it at "$" and read at the next tick; no
            // ERROR span, one INFO record.
            _groupReady = false;
            await EnsureGroupAsync(db);
            DemoLog.Write(logger, LogLevel.Information, "consumer group recreated", null);
            return [];
        }
    }

    private async Task ProcessAsync(IDatabase db, StreamEntry entry)
    {
        var fields = Fields(entry);
        var messageId = entry.Id.ToString();
        using var span = StartProcessSpan(messageId, fields);
        try
        {
            DemoLog.Write(logger, LogLevel.Information, "order event processed", null,
                ("order.id", fields.GetValueOrDefault("order_id")), ("demo.scenario", fields.GetValueOrDefault("scenario")));
            await db.StreamAcknowledgeAsync(DemoSettings.StreamKey, DemoSettings.ConsumerGroup, entry.Id);
        }
        catch (Exception ex) when (ex is RedisException or TimeoutException)
        {
            // The message stays pending (XPENDING); this span owns the failure.
            Failure.Record(span, ex, logger, ("messaging.message.id", messageId));
        }
        finally
        {
            Baggage.Current = default;
        }
    }

    private async Task EnsureGroupAsync(IDatabase db)
    {
        try
        {
            // At "$" with MKSTREAM (r3.5): only events published after the group exists are read.
            await db.StreamCreateConsumerGroupAsync(DemoSettings.StreamKey, DemoSettings.ConsumerGroup, StreamPosition.NewMessages, createStream: true);
        }
        catch (RedisServerException ex) when (ex.Message.StartsWith("BUSYGROUP", StringComparison.Ordinal))
        {
            // Another worker, or an earlier run of this one, created it.
        }

        _groupReady = true;
    }

    /// <inheritdoc />
    protected override async Task ExecuteAsync(CancellationToken stoppingToken)
    {
        await Task.Yield();
        using var timer = new PeriodicTimer(settings.Poll);
        do
        {
            try
            {
                await PollAsync(stoppingToken);
            }
            catch (Exception ex) when (ex is RedisException or TimeoutException)
            {
                // Redis not reachable yet: retry at the next tick.
                DemoLog.Write(logger, LogLevel.Warning, "queue poll failed", ex);
            }
        }
        while (await timer.WaitForNextTickAsync(stoppingToken));
    }
}
