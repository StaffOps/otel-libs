using System.Text.Json.Serialization;

namespace StaffOps.OtelLibs.Demo;

/// <summary>The worker's HTTP body <c>{"order_id", "customer", "amount", "scenario"}</c> (DE-D7).</summary>
public sealed record OrderRequest(
    [property: JsonPropertyName("order_id")] string OrderId,
    [property: JsonPropertyName("customer")] string Customer,
    [property: JsonPropertyName("amount")] double Amount,
    [property: JsonPropertyName("scenario")] string Scenario)
{
    /// <summary>The body of a planned request.</summary>
    public static OrderRequest From(PlannedOrder order) => new(order.OrderId, order.Customer, order.Amount, order.Scenario);
}
