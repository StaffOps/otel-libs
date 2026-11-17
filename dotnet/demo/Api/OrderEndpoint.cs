using System.Diagnostics;
using System.Globalization;
using Grpc.Core;
using OpenTelemetry;
using StackExchange.Redis;
using StaffOps.OtelLibs.Demo.V1;

namespace StaffOps.OtelLibs.Demo.Api;

/// <summary>
/// <c>POST /orders</c> in the demo span <c>demo.order.accept</c> (design "Scenario table",
/// "Expected trace"): <c>unhandled</c> throws <see cref="DemoUnhandledError"/> before the cache;
/// otherwise a cache read (miss → write, TTL 30 s; a failure is a WARN, r3.5), <c>PlaceOrder</c> on
/// the backend (422 / 409 / 502 for its failures) and, for an accepted order, one PRODUCER span with
/// the <c>XADD</c> of the queue event (DE-D6; a failure is a WARN, the answer stays 201).
/// </summary>
public sealed class OrderEndpoint(
    OrderService.OrderServiceClient backend,
    IConnectionMultiplexer redis,
    ILogger<OrderEndpoint> logger,
    OrderEndpoint.Settings settings)
{
    /// <summary>Per-process settings of the endpoint.</summary>
    /// <param name="Run">The run id (suffix of the run tokens).</param>
    /// <param name="RedisHost">server.address of the messaging spans.</param>
    /// <param name="RedisPort">server.port of the messaging spans.</param>
    public sealed record Settings(string Run, string RedisHost, int RedisPort);

    /// <summary>TTL of a cached quote (DE-D4).</summary>
    public static readonly TimeSpan QuoteTtl = TimeSpan.FromSeconds(30);

    /// <summary>Approximate stream length cap (<c>MAXLEN ~ 1000</c>, DE-D4).</summary>
    public const int StreamMaxLength = 1000;

    /// <summary>
    /// The HTTP answer to a failed backend call (r3.5): 422 / 409 for the two business codes, 502 for
    /// any other gRPC failure.
    /// </summary>
    public static int HttpStatusFor(StatusCode code) => code switch
    {
        StatusCode.FailedPrecondition => StatusCodes.Status422UnprocessableEntity,
        StatusCode.AlreadyExists => StatusCodes.Status409Conflict,
        _ => StatusCodes.Status502BadGateway,
    };

    /// <summary>The four scenarios the api accepts (r3.5: anything else is a 400).</summary>
    public static bool IsKnownScenario(string scenario) =>
        scenario is Scenario.Ok or Scenario.BusinessError or Scenario.DbError or Scenario.Unhandled;

    /// <summary>The fields of the queue event (r3.5): the propagation fields, then <c>order_id</c> and <c>scenario</c> only.</summary>
    public static NameValueEntry[] StreamFields(IReadOnlyDictionary<string, string> propagation, string storedId, string scenario)
    {
        var entries = new List<NameValueEntry>(propagation.Count + 2);
        foreach (var (key, value) in propagation)
        {
            entries.Add(new NameValueEntry(key, value));
        }

        entries.Add(new NameValueEntry("order_id", storedId));
        entries.Add(new NameValueEntry("scenario", scenario));
        return entries.ToArray();
    }

    /// <summary>Handles one order request.</summary>
    public async Task<IResult> HandleAsync(OrderRequest? order, CancellationToken cancellationToken)
    {
        if (order is null || string.IsNullOrEmpty(order.OrderId) || string.IsNullOrEmpty(order.Customer) || string.IsNullOrEmpty(order.Scenario))
        {
            return Results.BadRequest(new { error = "order_id, customer and scenario are required" });
        }

        if (!IsKnownScenario(order.Scenario))
        {
            return Results.BadRequest(new { error = "unknown scenario" });
        }

        using var accept = DemoTelemetry.Source.StartActivity("demo.order.accept");
        accept?.SetTag("demo.scenario", order.Scenario);
        accept?.SetTag("order.id", order.OrderId);
        try
        {
            if (order.Scenario == Scenario.Unhandled)
            {
                throw new DemoUnhandledError($"boom unh-{settings.Run}");
            }

            var db = redis.GetDatabase();
            await ReadQuoteAsync(db, order);

            var reply = await backend.PlaceOrderAsync(new PlaceOrderRequest
            {
                OrderId = order.OrderId,
                Customer = order.Customer,
                Amount = order.Amount,
                Scenario = order.Scenario,
            }, cancellationToken: cancellationToken);

            await PublishAsync(db, order.Scenario, reply.OrderId);
            return Results.Json(new { order_id = reply.OrderId, status = reply.Status }, statusCode: StatusCodes.Status201Created);
        }
        catch (RpcException ex)
        {
            // The backend (or the transport) owns the failure; this span only propagates it (DE-D8
            // note of 2026-11-17): ERROR, error.type = the answered HTTP status, no event, no log.
            // The constant gRPC message is the body.
            var status = HttpStatusFor(ex.StatusCode);
            Failure.Propagate(accept, status);
            return Results.Json(new { error = ex.Status.Detail }, statusCode: status);
        }
        catch (Exception ex)
        {
            // Owning span of unhandled (and of any unexpected failure): type on the span, detail in
            // the log written here, while demo.order.accept is current, before the rethrow (DR3).
            Failure.Record(accept, ex, logger);
            throw;
        }
    }

    /// <summary>Cache read, write on a miss (TTL 30 s). Not fatal (r3.5): a failure is one WARN record.</summary>
    private async Task ReadQuoteAsync(IDatabase db, OrderRequest order)
    {
        try
        {
            var quoteKey = DemoSettings.QuoteKey(order.Customer);
            var quote = await db.StringGetAsync(quoteKey);
            if (!quote.HasValue)
            {
                await db.StringSetAsync(quoteKey, order.Amount.ToString(CultureInfo.InvariantCulture), QuoteTtl);
            }
        }
        catch (Exception ex) when (ex is RedisException or TimeoutException)
        {
            DemoLog.Write(logger, LogLevel.Warning, "quote cache unavailable", ex, ("demo.scenario", order.Scenario));
        }
    }

    /// <summary>
    /// The PRODUCER span <c>send demo:dotnet:orders</c> and its XADD (DE-D6). Not fatal (r3.5): a
    /// failed XADD is one WARN record written in the span; the span gets no exception event.
    /// </summary>
    private async Task PublishAsync(IDatabase db, string scenario, string storedId)
    {
        using var producer = DemoTelemetry.Source.StartActivity($"send {DemoSettings.StreamKey}", ActivityKind.Producer);
        producer?.SetTag("messaging.system", "redis");
        producer?.SetTag("messaging.operation.name", "send");
        producer?.SetTag("messaging.operation.type", "send");
        producer?.SetTag("messaging.destination.name", DemoSettings.StreamKey);
        producer?.SetTag("server.address", settings.RedisHost);
        producer?.SetTag("server.port", settings.RedisPort);

        var context = producer?.Context ?? Activity.Current?.Context ?? default;
        var fields = StreamFields(MessageCarrier.Inject(context, Baggage.Current), storedId, scenario);
        try
        {
            var id = await db.StreamAddAsync(DemoSettings.StreamKey, fields, maxLength: StreamMaxLength, useApproximateMaxLength: true);
            producer?.SetTag("messaging.message.id", id.ToString());
        }
        catch (Exception ex) when (ex is RedisException or TimeoutException)
        {
            producer?.SetStatus(ActivityStatusCode.Error);
            DemoLog.Write(logger, LogLevel.Warning, "order event not published", ex, ("demo.scenario", scenario));
        }
    }
}
