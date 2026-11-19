using System.Diagnostics;
using Microsoft.Extensions.Logging;
using StackExchange.Redis;
using StaffOps.OtelLibs.Demo.Worker;

namespace StaffOps.OtelLibs.Demo.Tests;

public sealed class ConsumerFlowTests : IDisposable
{
    private readonly SpanCollector _spans = new();
    private readonly FakeRedis _redis = new();
    private readonly RecordingLogger<Consumer> _logger = new();

    public void Dispose() => _spans.Dispose();

    private Consumer Consumer(TimeSpan? poll = null) =>
        new(_redis.Multiplexer, _logger, new Consumer.Settings("pod-1", poll ?? TimeSpan.FromSeconds(2), "demo-redis", 6379));

    private static StreamEntry Message(string id, ActivityContext producer)
    {
        var fields = MessageCarrier.Inject(producer, default)
            .Select(f => new NameValueEntry(f.Key, f.Value))
            .Append(new NameValueEntry("order_id", "ok-1"))
            .Append(new NameValueEntry("scenario", "ok"))
            .ToArray();
        return new StreamEntry(id, fields);
    }

    private static ActivityContext NewProducer() =>
        new(ActivityTraceId.CreateRandom(), ActivitySpanId.CreateRandom(), ActivityTraceFlags.Recorded);

    [Fact]
    public async Task Poll_ProcessesEachMessageInItsConsumerSpanAndAcksInsideIt()
    {
        var producer = NewProducer();
        _redis.Pending.Enqueue(Message("5-0", producer));
        _redis.Pending.Enqueue(Message("6-0", NewProducer()));
        var consumer = Consumer();

        await consumer.PollAsync(CancellationToken.None);
        await consumer.PollAsync(CancellationToken.None);

        Assert.Equal(1, _redis.GroupCreates);
        Assert.Equal(["5-0", "6-0"], _redis.Acked);
        // r3.6: the poll (and the group creation) run under the unrecorded quiet parent, the XACK
        // under its CONSUMER span.
        Assert.All(_redis.Contexts.Where(c => c.Command is "StreamReadGroupAsync" or "StreamCreateConsumerGroupAsync"),
            c => Assert.Equal(("otel-libs-demo.quiet", false), (c.Activity, c.Recorded)));
        Assert.All(_redis.Contexts.Where(c => c.Command == "StreamAcknowledgeAsync"),
            c => Assert.Equal(("process demo:dotnet:orders", true), (c.Activity, c.Recorded)));
        var spans = _spans.Started.Where(a => a.Kind == ActivityKind.Consumer).ToList();
        Assert.Equal(2, spans.Count);
        Assert.Equal(producer.SpanId, spans[0].ParentSpanId);
        Assert.Equal(producer.SpanId, Assert.Single(spans[0].Links).Context.SpanId);

        Assert.Equal(2, _logger.Entries.Count);
        var log = _logger.Entries[0];
        Assert.Equal((LogLevel.Information, "order event processed"), (log.Level, log.Message));
        Assert.Equal("ok-1", log.Attributes["order.id"]);
        Assert.Equal(spans[0].SpanId.ToHexString(), log.SpanId);
        Assert.Null(Activity.Current);
    }

    [Fact]
    public async Task Poll_ToleratesAnExistingGroup()
    {
        _redis.GroupError = new RedisServerException("BUSYGROUP Consumer Group name already exists");
        await Consumer().PollAsync(CancellationToken.None);
        Assert.Equal(1, _redis.GroupCreates);
        Assert.Contains("StreamReadGroupAsync", _redis.Commands);
    }

    [Fact]
    public async Task FailedAck_IsRecordedOnTheConsumerSpan()
    {
        _redis.Pending.Enqueue(Message("7-0", NewProducer()));
        _redis.AckError = new RedisTimeoutException("timeout", CommandStatus.Unknown);

        await Consumer().PollAsync(CancellationToken.None);

        var span = Assert.Single(_spans.Started, a => a.Kind == ActivityKind.Consumer);
        Assert.Equal(ActivityStatusCode.Error, span.Status);
        Assert.Equal("RedisTimeoutException", span.GetTagItem("error.type"));
        Assert.Equal(LogLevel.Error, _logger.Entries[^1].Level);
    }

    [Fact]
    public async Task Poll_RecreatesTheGroupAfterNoGroupWithoutAnErrorSpan()
    {
        var consumer = Consumer();
        await consumer.PollAsync(CancellationToken.None);
        _redis.ReadError = new RedisServerException("NOGROUP No such key or consumer group");

        await consumer.PollAsync(CancellationToken.None);

        Assert.Equal(2, _redis.GroupCreates);
        var log = Assert.Single(_logger.Entries);
        Assert.Equal((LogLevel.Information, "consumer group recreated"), (log.Level, log.Message));
        Assert.Empty(_spans.Started);
    }

    [Fact]
    public async Task Execute_LogsPollFailuresAndKeepsPolling()
    {
        _redis.ReadError = new RedisTimeoutException("timeout", CommandStatus.Unknown);
        var consumer = Consumer(TimeSpan.FromMilliseconds(20));

        await consumer.StartAsync(CancellationToken.None);
        for (var i = 0; i < 200 && _logger.Entries.Count(e => e.Message == "queue poll failed") < 2; i++)
        {
            await Task.Delay(10);
        }

        await consumer.StopAsync(CancellationToken.None);
        Assert.True(_logger.Entries.Count(e => e.Level == LogLevel.Warning && e.Message == "queue poll failed") >= 2);
        Assert.Equal(1, _redis.GroupCreates);
    }
}
