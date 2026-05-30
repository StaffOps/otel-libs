using System.Diagnostics;
using System.Diagnostics.Metrics;
using OtelHelper.Grpc;
using Grpc.Core;
using OpenTelemetry;

namespace DotnetBackend.Services;

public class OrderGrpcService : OrderService.OrderServiceBase
{
    private static readonly string ServiceName = Environment.GetEnvironmentVariable("SERVICE_NAME") ?? "sample-backend";
    private static readonly ActivitySource DbActivity = new("sample-backend.db");
    private static readonly ActivitySource ExternalActivity = new("sample-backend.external");
    private static readonly Meter Meter = new(ServiceName);

    private static readonly Counter<long> ProcessedCounter = Meter.CreateCounter<long>("orders.processed_total", "orders");
    private static readonly Counter<long> CancelledCounter = Meter.CreateCounter<long>("orders.cancelled_total", "orders");
    private static readonly Counter<long> ExternalCallsCounter = Meter.CreateCounter<long>("external.calls_total", "calls");
    private static readonly Histogram<double> DbQueryDuration = Meter.CreateHistogram<double>("db.query_duration_seconds", "s");
    private static readonly Histogram<double> ExternalCallDuration = Meter.CreateHistogram<double>("external.call_duration_seconds", "s");
    private static readonly Histogram<double> ProcessingDuration = Meter.CreateHistogram<double>("order.processing_duration_seconds", "s");

    private readonly IHttpClientFactory _httpFactory;
    private readonly ILogger<OrderGrpcService> _logger;

    public OrderGrpcService(IHttpClientFactory httpFactory, ILogger<OrderGrpcService> logger)
    {
        _httpFactory = httpFactory;
        _logger = logger;
    }

    public override async Task<ProcessOrderReply> ProcessOrder(ProcessOrderRequest request, ServerCallContext context)
    {
        var sw = Stopwatch.StartNew();

        using (var dbSpan = DbActivity.StartActivity("backend.db-query-order"))
        {
            dbSpan?.SetTag("db.system", "postgresql");
            dbSpan?.SetTag("db.statement", "SELECT * FROM orders WHERE id = @id");
            dbSpan?.SetTag("db.name", "orders_db");

            var dbSw = Stopwatch.StartNew();
            await Task.Delay(Random.Shared.Next(10, 80));
            DbQueryDuration.Record(dbSw.Elapsed.TotalSeconds, new KeyValuePair<string, object?>("operation", "select"));
            _logger.LogInformation("DB query for order {OrderId} took {Duration}ms", request.OrderId, dbSw.ElapsedMilliseconds);
        }

        using (var extSpan = ExternalActivity.StartActivity("backend.external-enrich-order"))
        {
            extSpan?.SetTag("peer.service", "enrichment-api");
            var extSw = Stopwatch.StartNew();
            var client = _httpFactory.CreateClient("external");
            try
            {
                await client.GetAsync("https://httpbin.org/delay/1");
                ExternalCallsCounter.Add(1, new KeyValuePair<string, object?>("status", "success"));
                _logger.LogInformation("External enrichment completed for order {OrderId}", request.OrderId);
            }
            catch (Exception ex)
            {
                ExternalCallsCounter.Add(1, new KeyValuePair<string, object?>("status", "fallback"));
                _logger.LogWarning(ex, "External enrichment failed for order {OrderId}, using fallback", request.OrderId);
                await Task.Delay(100);
            }
            ExternalCallDuration.Record(extSw.Elapsed.TotalSeconds);
        }

        ProcessedCounter.Add(1, new KeyValuePair<string, object?>("status", "completed"));
        ProcessingDuration.Record(sw.Elapsed.TotalSeconds);
        _logger.LogInformation("Order {OrderId} fully processed via gRPC", request.OrderId);

        return new ProcessOrderReply
        {
            OrderId = request.OrderId,
            Status = "completed",
            Enriched = true,
            ProcessedBy = ServiceName
        };
    }

    public override async Task<CancelOrderReply> CancelOrder(CancelOrderRequest request, ServerCallContext context)
    {
        var sw = Stopwatch.StartNew();

        using (var dbSpan = DbActivity.StartActivity("backend.db-cancel-order"))
        {
            dbSpan?.SetTag("db.system", "postgresql");
            dbSpan?.SetTag("db.statement", "UPDATE orders SET status='cancelled' WHERE id = @id");

            var dbSw = Stopwatch.StartNew();
            await Task.Delay(Random.Shared.Next(5, 30));
            DbQueryDuration.Record(dbSw.Elapsed.TotalSeconds, new KeyValuePair<string, object?>("operation", "update"));
            _logger.LogInformation("DB cancel for order {OrderId} took {Duration}ms", request.OrderId, dbSw.ElapsedMilliseconds);
        }

        CancelledCounter.Add(1);
        ProcessingDuration.Record(sw.Elapsed.TotalSeconds);
        _logger.LogInformation("Order {OrderId} cancelled via gRPC", request.OrderId);

        return new CancelOrderReply
        {
            OrderId = request.OrderId,
            Status = "cancelled",
            ProcessedBy = ServiceName
        };
    }

    public override async Task<SlowOperationReply> SlowOperation(SlowOperationRequest request, ServerCallContext context)
    {
        var sw = Stopwatch.StartNew();

        using (var dbSpan = DbActivity.StartActivity("backend.db-heavy-query"))
        {
            dbSpan?.SetTag("db.system", "postgresql");
            dbSpan?.SetTag("db.statement", "SELECT * FROM orders JOIN products ... (heavy aggregation)");

            _logger.LogWarning("Executing heavy DB query");
            await Task.Delay(Random.Shared.Next(3000, 5000));
            DbQueryDuration.Record(Stopwatch.StartNew().Elapsed.TotalSeconds, new KeyValuePair<string, object?>("operation", "heavy-query"));
        }

        using (var extSpan = ExternalActivity.StartActivity("backend.external-slow-enrichment"))
        {
            _logger.LogWarning("Calling slow external service");
            await Task.Delay(Random.Shared.Next(1000, 2000));
        }

        ProcessingDuration.Record(sw.Elapsed.TotalSeconds);

        return new SlowOperationReply
        {
            Status = "done",
            Warning = "this was slow",
            ProcessedBy = ServiceName
        };
    }

    private static int _unstableCallCount;

    public override async Task<UnstableReply> UnstableOperation(UnstableRequest request, ServerCallContext context)
    {
        var attempt = Interlocked.Increment(ref _unstableCallCount);

        using var span = DbActivity.StartActivity("backend.unstable-operation");
        span?.SetTag("request.id", request.RequestId);
        span?.SetTag("attempt", attempt);

        if (attempt % (request.FailCount + 1) != 0)
        {
            span?.SetStatus(ActivityStatusCode.Error, "Transient failure");
            _logger.LogWarning("UnstableOperation failed (attempt {Attempt}) for request {RequestId}", attempt, request.RequestId);
            await Task.Delay(Random.Shared.Next(10, 50));
            throw new RpcException(new Status(StatusCode.Unavailable, "Transient failure — try again"));
        }

        await Task.Delay(Random.Shared.Next(20, 100));
        _logger.LogInformation("UnstableOperation succeeded (attempt {Attempt}) for request {RequestId}", attempt, request.RequestId);

        return new UnstableReply
        {
            RequestId = request.RequestId,
            Status = "success",
            Attempts = attempt,
            ProcessedBy = ServiceName
        };
    }

    public override Task<BaggageReply> ReadBaggage(EmptyRequest request, ServerCallContext context)
    {
        using var span = DbActivity.StartActivity("backend.read-baggage");

        var reply = new BaggageReply { ProcessedBy = ServiceName };

        foreach (var item in Baggage.Current)
        {
            reply.BaggageItems.Add(item.Key, item.Value);
            span?.SetTag($"baggage.{item.Key}", item.Value);
        }

        _logger.LogInformation("ReadBaggage: found {Count} items", reply.BaggageItems.Count);
        return Task.FromResult(reply);
    }
}
