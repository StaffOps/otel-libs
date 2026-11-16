using System.Diagnostics;
using System.Diagnostics.Metrics;
using Grpc.Core;
using Microsoft.Extensions.Logging;
using Npgsql;
using StaffOps.OtelLibs.Demo.Backend;
using StaffOps.OtelLibs.Demo.V1;

namespace StaffOps.OtelLibs.Demo.Tests;

public sealed class OrderProcessorTests : IDisposable
{
    private readonly SpanCollector _spans = new();
    private readonly RecordingLogger<OrderProcessor> _logger = new();
    private readonly FakeStore _store = new();
    private readonly MeterListener _meters = new();
    private readonly List<(string Instrument, double Value, Dictionary<string, object?> Tags, string? SpanId)> _measurements = [];

    public OrderProcessorTests()
    {
        _meters.InstrumentPublished = (instrument, listener) =>
        {
            if (instrument.Meter.Name == DemoTelemetry.ScopeName)
            {
                listener.EnableMeasurementEvents(instrument);
            }
        };
        _meters.SetMeasurementEventCallback<long>((i, v, tags, _) => Add(i, v, tags));
        _meters.SetMeasurementEventCallback<double>((i, v, tags, _) => Add(i, v, tags));
        _meters.Start();
    }

    private void Add(Instrument instrument, double value, ReadOnlySpan<KeyValuePair<string, object?>> tags)
    {
        var map = new Dictionary<string, object?>();
        foreach (var (key, v) in tags)
        {
            map[key] = v;
        }

        _measurements.Add((instrument.Name, value, map, Activity.Current?.SpanId.ToHexString()));
    }

    public void Dispose()
    {
        _meters.Dispose();
        _spans.Dispose();
    }

    private OrderProcessor Processor() => new(_store, _logger, "r1");

    private static PlaceOrderRequest Request(string scenario, string id = "ok-1", double amount = 42) =>
        new() { OrderId = id, Customer = "alice", Amount = amount, Scenario = scenario };

    [Fact]
    public async Task Ok_UpsertsReadsBackLogsAndCountsInsideTheSpan()
    {
        var reply = await Processor().PlaceAsync(Request(Scenario.Ok), CancellationToken.None);

        Assert.Equal(("ok-1", "processed"), (reply.OrderId, reply.Status));
        Assert.Equal(["upsert ok-1", "get ok-1"], _store.Calls);
        var span = _spans.Single("demo.order.process");
        Assert.Equal(ActivityStatusCode.Unset, span.Status);
        Assert.Equal("ok", span.GetTagItem("demo.scenario"));
        Assert.Equal("ok-1", span.GetTagItem("order.id"));

        var log = Assert.Single(_logger.Entries);
        Assert.Equal((LogLevel.Information, "order processed"), (log.Level, log.Message));
        Assert.Equal(span.SpanId.ToHexString(), log.SpanId);

        Assert.Equal(2, _measurements.Count);
        var counter = Assert.Single(_measurements, m => m.Instrument == "demo.orders.processed");
        Assert.Equal(1, counter.Value);
        Assert.Equal(span.SpanId.ToHexString(), counter.SpanId);
        Assert.Equal(new Dictionary<string, object?> { ["demo.scenario"] = "ok", ["demo.outcome"] = "ok" }, counter.Tags);
        var duration = Assert.Single(_measurements, m => m.Instrument == "demo.order.duration");
        Assert.Equal(span.SpanId.ToHexString(), duration.SpanId);
        Assert.False(duration.Tags.ContainsKey("error.type"));
    }

    [Fact]
    public async Task BusinessError_RaisesRejectedAndMapsToFailedPrecondition()
    {
        var ex = await Assert.ThrowsAsync<RpcException>(() =>
            Processor().PlaceAsync(Request(Scenario.BusinessError, "ord-3", 5000), CancellationToken.None));

        Assert.Equal(StatusCode.FailedPrecondition, ex.StatusCode);
        Assert.Equal("order rejected", ex.Status.Detail);
        Assert.Empty(_store.Calls);
        AssertOwningFailure("OrderRejectedError", "amount over limit rej-r1", Scenario.BusinessError);
    }

    [Fact]
    public async Task DbError_InsertsTheDuplicateAndMapsTheTriggerErrorToAlreadyExists()
    {
        _store.InsertError = new PostgresException("duplicate order dup-r1", "ERROR", "ERROR", PostgresErrorCodes.UniqueViolation);

        var ex = await Assert.ThrowsAsync<RpcException>(() =>
            Processor().PlaceAsync(Request(Scenario.DbError, "ord-6"), CancellationToken.None));

        Assert.Equal(StatusCode.AlreadyExists, ex.StatusCode);
        Assert.Equal("order exists", ex.Status.Detail);
        Assert.Equal(["insert dup-r1"], _store.Calls);
        var log = AssertOwningFailure("OrderExistsError", "order dup-r1 exists", Scenario.DbError);
        Assert.IsType<PostgresException>(log.Exception!.InnerException);
    }

    [Fact]
    public async Task DbError_WithoutTheTriggerIsAnInternalError()
    {
        var ex = await Assert.ThrowsAsync<RpcException>(() =>
            Processor().PlaceAsync(Request(Scenario.DbError, "ord-6"), CancellationToken.None));
        Assert.Equal(StatusCode.Internal, ex.StatusCode);
        AssertOwningFailure("InvalidOperationException", "the orders_dup trigger did not raise: demo/sql/schema.sql not applied", Scenario.DbError);
    }

    [Fact]
    public async Task OtherDatabaseErrors_AreInternalAndCancellationIsCancelled()
    {
        _store.UpsertError = new PostgresException("boom", "ERROR", "ERROR", "08006");
        var internalError = await Assert.ThrowsAsync<RpcException>(() => Processor().PlaceAsync(Request(Scenario.Ok), CancellationToken.None));
        Assert.Equal(StatusCode.Internal, internalError.StatusCode);

        using var cts = new CancellationTokenSource();
        cts.Cancel();
        _store.UpsertError = new OperationCanceledException(cts.Token);
        var cancelled = await Assert.ThrowsAsync<RpcException>(() => Processor().PlaceAsync(Request(Scenario.Ok), cts.Token));
        Assert.Equal(StatusCode.Cancelled, cancelled.StatusCode);
    }

    [Fact]
    public async Task MissingRowAfterUpsert_IsInternal()
    {
        _store.ForgetRows = true;
        var ex = await Assert.ThrowsAsync<RpcException>(() => Processor().PlaceAsync(Request(Scenario.Ok), CancellationToken.None));
        Assert.Equal(StatusCode.Internal, ex.StatusCode);
    }

    [Theory]
    [InlineData(Scenario.Unhandled)]
    [InlineData("other")]
    public async Task UnknownScenario_IsRejectedWithoutSpanOrMetrics(string scenario)
    {
        var ex = await Assert.ThrowsAsync<RpcException>(() => Processor().PlaceAsync(Request(scenario), CancellationToken.None));
        Assert.Equal(StatusCode.InvalidArgument, ex.StatusCode);
        Assert.Empty(_spans.Started);
        Assert.Empty(_measurements);
    }

    private LogEntry AssertOwningFailure(string errorType, string message, string scenario)
    {
        var span = _spans.Single("demo.order.process");
        Assert.Equal(ActivityStatusCode.Error, span.Status);
        Assert.Null(span.StatusDescription);
        Assert.Equal(errorType, span.GetTagItem("error.type"));
        var ev = Assert.Single(span.Events);
        Assert.Equal(("exception", errorType), (ev.Name, Assert.Single(ev.Tags).Value as string));
        Assert.DoesNotContain(span.Tags, t => t.Value?.Contains("-r1") == true);

        var log = Assert.Single(_logger.Entries);
        Assert.Equal((LogLevel.Error, message), (log.Level, log.Message));
        Assert.Equal(span.SpanId.ToHexString(), log.SpanId);

        Assert.All(_measurements, m =>
        {
            Assert.Equal(scenario, m.Tags["demo.scenario"]);
            Assert.Equal("error", m.Tags["demo.outcome"]);
            Assert.Equal(errorType, m.Tags["error.type"]);
            Assert.Equal(span.SpanId.ToHexString(), m.SpanId);
        });
        Assert.Equal(2, _measurements.Count);
        return log;
    }

    private sealed class FakeStore : IOrderStore
    {
        private readonly Dictionary<string, StoredOrder> _rows = [];

        public List<string> Calls { get; } = [];
        public Exception? InsertError { get; set; }
        public Exception? UpsertError { get; set; }
        public bool ForgetRows { get; set; }

        public Task UpsertAsync(StoredOrder order, CancellationToken cancellationToken)
        {
            Calls.Add($"upsert {order.Id}");
            if (UpsertError is not null)
            {
                throw UpsertError;
            }

            Assert.Equal(TimeSpan.Zero, order.UpdatedAt.Offset);
            if (!ForgetRows)
            {
                _rows[order.Id] = order;
            }

            return Task.CompletedTask;
        }

        public Task InsertAsync(StoredOrder order, CancellationToken cancellationToken)
        {
            Calls.Add($"insert {order.Id}");
            return InsertError is null ? Task.CompletedTask : throw InsertError;
        }

        public Task<StoredOrder?> GetAsync(string id, CancellationToken cancellationToken)
        {
            Calls.Add($"get {id}");
            return Task.FromResult(_rows.GetValueOrDefault(id));
        }
    }
}
