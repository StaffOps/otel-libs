using System.Diagnostics;
using Grpc.Core;
using Microsoft.AspNetCore.Http;
using Microsoft.Extensions.Logging;
using StackExchange.Redis;
using StaffOps.OtelLibs.Demo.Api;
using StaffOps.OtelLibs.Demo.V1;

namespace StaffOps.OtelLibs.Demo.Tests;

public sealed class ApiFlowTests : IDisposable
{
    private readonly SpanCollector _spans = new();
    private readonly FakeRedis _redis = new();
    private readonly RecordingLogger<OrderEndpoint> _logger = new();

    public void Dispose() => _spans.Dispose();

    private OrderEndpoint Endpoint(Func<object, object> backend, out FakeInvoker invoker)
    {
        invoker = new FakeInvoker(backend);
        return new OrderEndpoint(new OrderService.OrderServiceClient(invoker), _redis.Multiplexer, _logger,
            new OrderEndpoint.Settings("r1", "demo-redis", 6379));
    }

    private static int StatusOf(IResult result) => Assert.IsAssignableFrom<IStatusCodeHttpResult>(result).StatusCode!.Value;

    [Fact]
    public async Task Ok_CachesCallsTheBackendAndPublishesInAProducerSpan()
    {
        var endpoint = Endpoint(r => new PlaceOrderResponse { OrderId = ((PlaceOrderRequest)r).OrderId, Status = "processed" }, out var invoker);

        Assert.Equal(201, StatusOf(await endpoint.HandleAsync(new OrderRequest("ok-1", "alice", 11, Scenario.Ok), CancellationToken.None)));
        Assert.Equal(201, StatusOf(await endpoint.HandleAsync(new OrderRequest("ok-2", "alice", 12, Scenario.Ok), CancellationToken.None)));

        // Miss then hit: one SET for the customer's quote.
        Assert.Equal(1, _redis.Commands.Count(c => c == "StringSetAsync"));
        Assert.Equal("11", _redis.Strings["demo:dotnet:quote:alice"]);
        Assert.Equal(2, invoker.Requests.Count);
        Assert.Equal("ok", ((PlaceOrderRequest)invoker.Requests[0]).Scenario);

        var producers = _spans.Started.Where(a => a.Kind == ActivityKind.Producer).ToList();
        Assert.Equal(2, producers.Count);
        var producer = producers[0];
        Assert.Equal("send demo:dotnet:orders", producer.OperationName);
        var accept = _spans.Started.First(a => a.OperationName == "demo.order.accept");
        Assert.Equal(accept.SpanId, producer.ParentSpanId);
        Assert.Equal("1-0", producer.GetTagItem("messaging.message.id"));
        Assert.Equal("send", producer.GetTagItem("messaging.operation.type"));
        Assert.Equal("demo-redis", producer.GetTagItem("server.address"));

        var (key, fields, _) = _redis.Added[0];
        Assert.Equal("demo:dotnet:orders", key);
        var traceparent = fields.Single(f => f.Name == "traceparent").Value.ToString();
        Assert.Equal($"00-{producer.TraceId}-{producer.SpanId}-01", traceparent);
        Assert.Equal(["traceparent", "order_id", "scenario"], fields.Select(f => f.Name.ToString()));
        Assert.Equal("ok-1", fields.Single(f => f.Name == "order_id").Value.ToString());
        Assert.Empty(_logger.Entries);
        Assert.Equal(ActivityStatusCode.Unset, accept.Status);
    }

    [Theory]
    [InlineData(StatusCode.FailedPrecondition, "order rejected", 422)]
    [InlineData(StatusCode.AlreadyExists, "order exists", 409)]
    public async Task BusinessFailures_PropagateWithoutEventOrLog(StatusCode code, string message, int expected)
    {
        var endpoint = Endpoint(_ => throw new RpcException(new Status(code, message)), out _);

        Assert.Equal(expected, StatusOf(await endpoint.HandleAsync(new OrderRequest("ord-3", "bob", 5000, Scenario.BusinessError), CancellationToken.None)));

        var accept = _spans.Single("demo.order.accept");
        Assert.Equal(ActivityStatusCode.Error, accept.Status);
        Assert.Null(accept.GetTagItem("error.type"));
        Assert.Empty(accept.Events);
        Assert.Empty(_logger.Entries);
        Assert.Empty(_redis.Added);
    }

    [Fact]
    public async Task OtherBackendFailures_Are502AndOnlyPropagated()
    {
        var endpoint = Endpoint(_ => throw new RpcException(new Status(StatusCode.Unavailable, "down")), out _);

        Assert.Equal(502, StatusOf(await endpoint.HandleAsync(new OrderRequest("ok-1", "bob", 1, Scenario.Ok), CancellationToken.None)));

        var accept = _spans.Single("demo.order.accept");
        Assert.Equal(ActivityStatusCode.Error, accept.Status);
        Assert.Null(accept.GetTagItem("error.type"));
        Assert.Empty(accept.Events);
        Assert.Empty(_logger.Entries);
    }

    [Fact]
    public async Task CacheFailure_IsOneWarnAndTheOrderStillSucceeds()
    {
        _redis.StringError = new RedisConnectionException(ConnectionFailureType.UnableToConnect, "redis down");
        var endpoint = Endpoint(r => new PlaceOrderResponse { OrderId = "ok-1", Status = "processed" }, out var invoker);

        Assert.Equal(201, StatusOf(await endpoint.HandleAsync(new OrderRequest("ok-1", "bob", 1, Scenario.Ok), CancellationToken.None)));

        Assert.Single(invoker.Requests);
        var log = Assert.Single(_logger.Entries);
        Assert.Equal((LogLevel.Warning, "quote cache unavailable"), (log.Level, log.Message));
        Assert.Equal(ActivityStatusCode.Unset, _spans.Single("demo.order.accept").Status);
    }

    [Fact]
    public async Task PublishFailure_IsOneWarnInTheProducerSpanAndTheAnswerStays201()
    {
        _redis.AddError = new RedisTimeoutException("timeout", CommandStatus.Unknown);
        var endpoint = Endpoint(r => new PlaceOrderResponse { OrderId = "ok-1", Status = "processed" }, out _);

        Assert.Equal(201, StatusOf(await endpoint.HandleAsync(new OrderRequest("ok-1", "bob", 1, Scenario.Ok), CancellationToken.None)));

        var producer = Assert.Single(_spans.Started, a => a.Kind == ActivityKind.Producer);
        Assert.Equal(ActivityStatusCode.Error, producer.Status);
        Assert.Empty(producer.Events);
        Assert.Null(producer.GetTagItem("messaging.message.id"));
        var log = Assert.Single(_logger.Entries);
        Assert.Equal((LogLevel.Warning, "order event not published"), (log.Level, log.Message));
        Assert.Equal(producer.SpanId.ToHexString(), log.SpanId);
    }
}
