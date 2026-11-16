namespace StaffOps.OtelLibs.Demo;

/// <summary>The four scenarios of the demo (design "Scenario table").</summary>
public static class Scenario
{
    /// <summary>A processed order.</summary>
    public const string Ok = "ok";
    /// <summary>The backend rejects the order: gRPC FAILED_PRECONDITION, HTTP 422.</summary>
    public const string BusinessError = "business_error";
    /// <summary>The trigger-raised duplicate: gRPC ALREADY_EXISTS, HTTP 409.</summary>
    public const string DbError = "db_error";
    /// <summary>The api throws before the cache: HTTP 500.</summary>
    public const string Unhandled = "unhandled";
}

/// <summary>One request of the worker's cycle (the HTTP body, DE-D7).</summary>
/// <param name="Seq">1-based request number since the worker started.</param>
/// <param name="OrderId">Order id sent to the api.</param>
/// <param name="Customer">One of three customers, so the api cache hits and misses.</param>
/// <param name="Amount">Order amount; over <see cref="ScenarioCycle.AmountLimit"/> only for business_error.</param>
/// <param name="Scenario">The scenario at this position of the cycle.</param>
public sealed record PlannedOrder(long Seq, string OrderId, string Customer, double Amount, string Scenario);

/// <summary>
/// The fixed cycle of ten requests (DE-D7): <c>ok</c> at positions 1, 2, 4, 5, 7, 8, 10,
/// <c>business_error</c> at 3, <c>db_error</c> at 6, <c>unhandled</c> at 9.
/// </summary>
public static class ScenarioCycle
{
    /// <summary>The backend rejects an order whose amount is above this value.</summary>
    public const double AmountLimit = 1000;

    /// <summary>Number of distinct ok order ids (<c>ok-&lt;seq mod 500&gt;</c>, DE-D4): bounds the table.</summary>
    public const int OkIdSpace = 500;

    private static readonly string[] Customers = ["cust-a", "cust-b", "cust-c"];

    /// <summary>The scenario of the request numbered <paramref name="seq"/> (1-based).</summary>
    public static string At(long seq)
    {
        ArgumentOutOfRangeException.ThrowIfLessThan(seq, 1);
        return ((seq - 1) % 10 + 1) switch
        {
            3 => Scenario.BusinessError,
            6 => Scenario.DbError,
            9 => Scenario.Unhandled,
            _ => Scenario.Ok,
        };
    }

    /// <summary>
    /// The request numbered <paramref name="seq"/>. Order ids never hold a run token: the backend
    /// stores <c>ok-&lt;seq mod 500&gt;</c> for ok and inserts <c>dup-&lt;run&gt;</c> itself for
    /// db_error, so the token stays out of span attributes such as <c>order.id</c> (DR12).
    /// </summary>
    public static PlannedOrder Plan(long seq)
    {
        var scenario = At(seq);
        var slot = seq % OkIdSpace;
        var orderId = scenario == Scenario.Ok ? $"ok-{slot}" : $"ord-{slot}";
        var amount = scenario == Scenario.BusinessError ? AmountLimit * 5 : 10 + seq % 90;
        return new PlannedOrder(seq, orderId, Customers[seq % Customers.Length], amount, scenario);
    }
}
