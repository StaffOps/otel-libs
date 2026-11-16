using System.Diagnostics;
using System.Diagnostics.Metrics;
using Grpc.Core;
using Npgsql;
using StaffOps.OtelLibs.Demo.V1;

namespace StaffOps.OtelLibs.Demo.Backend;

/// <summary>
/// The backend's order logic in the demo span <c>demo.order.process</c> (design "Scenario table"):
/// upsert + read back for ok, <see cref="OrderRejectedError"/> for an amount over the limit,
/// the trigger-raised duplicate <c>dup-&lt;run&gt;</c> → <see cref="OrderExistsError"/> for
/// db_error. A failure follows ADR-014 in this owning span (<see cref="Failure.Record"/>) and maps
/// to a gRPC status with a constant message. The custom metrics are recorded inside the span, so
/// they carry trace-based exemplars (DE-D9, DE-D10).
/// </summary>
public sealed class OrderProcessor
{
    /// <summary>The constant gRPC message of business_error.</summary>
    public const string RejectedMessage = "order rejected";

    /// <summary>The constant gRPC message of db_error.</summary>
    public const string ExistsMessage = "order exists";

    private static readonly Counter<long> Processed = DemoTelemetry.Meter.CreateCounter<long>(
        "demo.orders.processed", "{order}", "Orders processed by the demo backend");

    private static readonly Histogram<double> Duration = DemoTelemetry.Meter.CreateHistogram<double>(
        "demo.order.duration", "s", "Time the demo backend spent on one order", tags: null,
        advice: new InstrumentAdvice<double> { HistogramBucketBoundaries = [0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5] });

    private readonly IOrderStore _store;
    private readonly ILogger<OrderProcessor> _logger;
    private readonly string _run;
    private readonly TimeProvider _clock;

    /// <summary>Creates the processor for the run <paramref name="run"/> (the run tokens' suffix).</summary>
    public OrderProcessor(IOrderStore store, ILogger<OrderProcessor> logger, string run, TimeProvider? clock = null)
    {
        _store = store;
        _logger = logger;
        _run = run;
        _clock = clock ?? TimeProvider.System;
    }

    /// <summary>Processes one order; throws <see cref="RpcException"/> for every failure.</summary>
    public async Task<PlaceOrderResponse> PlaceAsync(PlaceOrderRequest request, CancellationToken cancellationToken)
    {
        if (request.Scenario is not (Scenario.Ok or Scenario.BusinessError or Scenario.DbError))
        {
            // unhandled never reaches the backend (DR7); anything else is a caller bug.
            throw new RpcException(new Status(StatusCode.InvalidArgument, "unknown scenario"));
        }

        using var span = DemoTelemetry.Source.StartActivity("demo.order.process");
        span?.SetTag("demo.scenario", request.Scenario);
        span?.SetTag("order.id", request.OrderId);

        var started = Stopwatch.GetTimestamp();
        var tags = new TagList { { "demo.scenario", request.Scenario } };
        try
        {
            var stored = await ProcessAsync(request, cancellationToken);
            tags.Add("demo.outcome", "ok");
            DemoLog.Write(_logger, LogLevel.Information, "order processed", null,
                ("order.id", stored), ("demo.scenario", request.Scenario));
            return new PlaceOrderResponse { OrderId = stored, Status = "processed" };
        }
        catch (Exception ex)
        {
            tags.Add("demo.outcome", "error");
            tags.Add("error.type", Failure.ErrorType(ex));
            Failure.Record(span, ex, _logger, ("order.id", request.OrderId), ("demo.scenario", request.Scenario));
            throw ex switch
            {
                OrderRejectedError => new RpcException(new Status(StatusCode.FailedPrecondition, RejectedMessage)),
                OrderExistsError => new RpcException(new Status(StatusCode.AlreadyExists, ExistsMessage)),
                OperationCanceledException when cancellationToken.IsCancellationRequested =>
                    new RpcException(new Status(StatusCode.Cancelled, "order cancelled")),
                _ => new RpcException(new Status(StatusCode.Internal, "order failed")),
            };
        }
        finally
        {
            // Still inside demo.order.process: the measurements get its trace_id / span_id as exemplars.
            Duration.Record(Stopwatch.GetElapsedTime(started).TotalSeconds, tags);
            Processed.Add(1, tags);
        }
    }

    /// <summary>The stored order id of a successful order.</summary>
    private async Task<string> ProcessAsync(PlaceOrderRequest request, CancellationToken cancellationToken)
    {
        if (request.Amount > ScenarioCycle.AmountLimit)
        {
            throw new OrderRejectedError($"amount over limit rej-{_run}");
        }

        var now = _clock.GetUtcNow();
        if (request.Scenario == Scenario.DbError)
        {
            var duplicate = $"dup-{_run}";
            try
            {
                await _store.InsertAsync(new StoredOrder(duplicate, request.Customer, (decimal)request.Amount, request.Scenario, now),
                    cancellationToken);
            }
            catch (PostgresException ex) when (ex.SqlState == PostgresErrorCodes.UniqueViolation)
            {
                throw new OrderExistsError($"order {duplicate} exists", ex);
            }

            // The schema's trigger raises for every dup- id; reaching this line means it is missing.
            throw new InvalidOperationException("the orders_dup trigger did not raise: demo/sql/schema.sql not applied");
        }

        await _store.UpsertAsync(new StoredOrder(request.OrderId, request.Customer, (decimal)request.Amount, request.Scenario, now),
            cancellationToken);
        var stored = await _store.GetAsync(request.OrderId, cancellationToken)
            ?? throw new InvalidOperationException("the upserted order was not found");
        return stored.Id;
    }
}
