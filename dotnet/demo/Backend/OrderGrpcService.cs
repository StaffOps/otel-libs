using System.Globalization;
using Grpc.Core;
using StaffOps.OtelLibs.Demo.V1;

namespace StaffOps.OtelLibs.Demo.Backend;

/// <summary>The gRPC service <c>demo.v1.OrderService</c> (demo/proto, DE-D3).</summary>
public sealed class OrderGrpcService(OrderProcessor processor, IOrderStore store) : OrderService.OrderServiceBase
{
    /// <inheritdoc />
    public override Task<PlaceOrderResponse> PlaceOrder(PlaceOrderRequest request, ServerCallContext context) =>
        processor.PlaceAsync(request, context.CancellationToken);

    /// <inheritdoc />
    public override async Task<Order> GetOrder(GetOrderRequest request, ServerCallContext context)
    {
        var order = await store.GetAsync(request.OrderId, context.CancellationToken)
            ?? throw new RpcException(new Status(StatusCode.NotFound, "order not found"));
        return new Order
        {
            OrderId = order.Id,
            Customer = order.Customer,
            Amount = (double)order.Amount,
            Scenario = order.Scenario,
            UpdatedAt = order.UpdatedAt.ToUniversalTime().ToString("yyyy-MM-dd'T'HH:mm:ss.FFFFFFF'Z'", CultureInfo.InvariantCulture),
        };
    }
}
