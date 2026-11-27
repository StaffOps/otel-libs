using Grpc.Core;
using StaffOps.OtelLibs.Demo.Backend;
using StaffOps.OtelLibs.Demo.V1;

namespace StaffOps.OtelLibs.Demo.Tests;

public class BackendServiceTests
{
    private sealed class MemoryStore : IOrderStore
    {
        public Dictionary<string, StoredOrder> Rows { get; } = [];

        public Task UpsertAsync(StoredOrder order, CancellationToken cancellationToken)
        {
            Rows[order.Id] = order;
            return Task.CompletedTask;
        }

        public Task InsertAsync(StoredOrder order, CancellationToken cancellationToken) => UpsertAsync(order, cancellationToken);

        public Task<StoredOrder?> GetAsync(string id, CancellationToken cancellationToken) => Task.FromResult(Rows.GetValueOrDefault(id));
    }

    [Fact]
    public async Task PlaceThenGet_RoundTripsThroughTheService()
    {
        using var spans = new SpanCollector();
        var store = new MemoryStore();
        var service = new OrderGrpcService(new OrderProcessor(store, new RecordingLogger<OrderProcessor>(), "r1"), store);
        var context = new FakeServerCallContext();

        var placed = await service.PlaceOrder(new PlaceOrderRequest { OrderId = "ok-7", Customer = "bob", Amount = 17.5, Scenario = Scenario.Ok }, context);
        var order = await service.GetOrder(new GetOrderRequest { OrderId = placed.OrderId }, context);

        Assert.Equal(("ok-7", "bob", 17.5, "ok"), (order.OrderId, order.Customer, order.Amount, order.Scenario));
        Assert.Matches(@"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?Z$", order.UpdatedAt);
    }

    [Fact]
    public async Task GetOrder_UnknownIdIsNotFound()
    {
        var store = new MemoryStore();
        var service = new OrderGrpcService(new OrderProcessor(store, new RecordingLogger<OrderProcessor>(), "r1"), store);
        var ex = await Assert.ThrowsAsync<RpcException>(() => service.GetOrder(new GetOrderRequest { OrderId = "nope" }, new FakeServerCallContext()));
        Assert.Equal(StatusCode.NotFound, ex.StatusCode);
    }
}
