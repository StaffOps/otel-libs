using OtelHelper;
using OtelHelper.Grpc;
using Grpc.Net.Client;
using OpenTelemetry;
using System.Diagnostics;
using System.Diagnostics.Metrics;

var builder = WebApplication.CreateBuilder(args);

builder.Services.AddOtelHelper(opts =>
{
    opts.ResourceAttributes = new Dictionary<string, object>
    {
        ["app.component"] = "api-gateway"
    };
});

var backendUrl = Environment.GetEnvironmentVariable("BACKEND_URL") ?? "http://localhost:5100";
builder.Services.AddSingleton(_ => GrpcChannel.ForAddress(backendUrl));
builder.Services.AddScoped(sp => new OrderService.OrderServiceClient(sp.GetRequiredService<GrpcChannel>()));

var app = builder.Build();

var serviceName = Environment.GetEnvironmentVariable("SERVICE_NAME") ?? "sample-api";
var activitySource = new ActivitySource(serviceName);
var meter = new Meter(serviceName);

var orderCounter = meter.CreateCounter<long>("orders.received_total", "orders", "Total orders received");
var cancelCounter = meter.CreateCounter<long>("orders.cancelled_total", "orders", "Total orders cancelled");
var errorsCounter = meter.CreateCounter<long>("errors_total", "errors", "Total errors");
var revenueCounter = meter.CreateCounter<double>("revenue_total", "BRL", "Total revenue");
var requestDuration = meter.CreateHistogram<double>("request.duration_seconds", "s", "Request processing duration");
var activeRequests = 0;
meter.CreateObservableGauge("requests.active", () => activeRequests, "requests", "Currently active requests");

app.MapGet("/", () => "SampleApi is running — try GET /order/123");

// --- GET /order/{id} — trace: api.get-order ---
app.MapGet("/order/{id}", async (int id, OrderService.OrderServiceClient grpcClient, ILogger<Program> logger) =>
{
    var sw = Stopwatch.StartNew();
    Interlocked.Increment(ref activeRequests);
    try
    {
        using var activity = activitySource.StartActivity("api.get-order");
        activity?.SetTag("order.id", id);

        logger.LogInformation("API received GET order {OrderId}, calling backend via gRPC", id);
        orderCounter.Add(1, new KeyValuePair<string, object?>("source", "http-get"));
        revenueCounter.Add(Random.Shared.Next(50, 500));

        var reply = await grpcClient.ProcessOrderAsync(new ProcessOrderRequest { OrderId = id });

        logger.LogInformation("gRPC response for order {OrderId}: {Status}", id, reply.Status);
        return Results.Ok(new { reply.OrderId, reply.Status, reply.Enriched, reply.ProcessedBy });
    }
    finally
    {
        Interlocked.Decrement(ref activeRequests);
        requestDuration.Record(sw.Elapsed.TotalSeconds, new KeyValuePair<string, object?>("endpoint", "get-order"));
    }
});

// --- POST /order — trace: api.create-order ---
app.MapPost("/order", async (HttpRequest req, OrderService.OrderServiceClient grpcClient, ILogger<Program> logger) =>
{
    var sw = Stopwatch.StartNew();
    Interlocked.Increment(ref activeRequests);
    try
    {
        using var activity = activitySource.StartActivity("api.create-order");

        var body = await req.ReadFromJsonAsync<OrderRequest>();
        var id = Random.Shared.Next(1000, 9999);

        activity?.SetTag("order.id", id);
        activity?.SetTag("order.product", body?.Product);

        logger.LogInformation("API creating order {OrderId} for product {Product} via gRPC", id, body?.Product);
        orderCounter.Add(1, new KeyValuePair<string, object?>("source", "http-post"));
        revenueCounter.Add(body?.Quantity ?? 1 * Random.Shared.Next(100, 1000));

        var reply = await grpcClient.ProcessOrderAsync(new ProcessOrderRequest
        {
            OrderId = id,
            Product = body?.Product ?? "",
            Quantity = body?.Quantity ?? 1
        });

        return Results.Json(new { reply.OrderId, reply.Status, reply.Enriched, reply.ProcessedBy }, statusCode: 201);
    }
    finally
    {
        Interlocked.Decrement(ref activeRequests);
        requestDuration.Record(sw.Elapsed.TotalSeconds, new KeyValuePair<string, object?>("endpoint", "create-order"));
    }
});

// --- GET /order/{id}/cancel — trace: api.cancel-order ---
app.MapGet("/order/{id}/cancel", async (int id, OrderService.OrderServiceClient grpcClient, ILogger<Program> logger) =>
{
    var sw = Stopwatch.StartNew();
    Interlocked.Increment(ref activeRequests);
    try
    {
        using var activity = activitySource.StartActivity("api.cancel-order");
        activity?.SetTag("order.id", id);

        logger.LogInformation("API cancelling order {OrderId} via gRPC", id);
        cancelCounter.Add(1);

        var reply = await grpcClient.CancelOrderAsync(new CancelOrderRequest { OrderId = id });

        return Results.Ok(new { reply.OrderId, reply.Status, reply.ProcessedBy });
    }
    finally
    {
        Interlocked.Decrement(ref activeRequests);
        requestDuration.Record(sw.Elapsed.TotalSeconds, new KeyValuePair<string, object?>("endpoint", "cancel-order"));
    }
});

// --- GET /slow — trace: api.slow-operation ---
app.MapGet("/slow", async (OrderService.OrderServiceClient grpcClient, ILogger<Program> logger) =>
{
    var sw = Stopwatch.StartNew();
    Interlocked.Increment(ref activeRequests);
    try
    {
        using var activity = activitySource.StartActivity("api.slow-operation");
        logger.LogWarning("Starting slow operation via gRPC");

        var reply = await grpcClient.SlowOperationAsync(new SlowOperationRequest());

        return Results.Ok(new { reply.Status, reply.Warning, reply.ProcessedBy });
    }
    finally
    {
        Interlocked.Decrement(ref activeRequests);
        requestDuration.Record(sw.Elapsed.TotalSeconds, new KeyValuePair<string, object?>("endpoint", "slow"));
    }
});

// --- GET /batch — trace: api.batch-orders ---
app.MapGet("/batch", async (OrderService.OrderServiceClient grpcClient, ILogger<Program> logger) =>
{
    var sw = Stopwatch.StartNew();
    Interlocked.Increment(ref activeRequests);
    try
    {
        using var activity = activitySource.StartActivity("api.batch-orders");

        var ids = Enumerable.Range(1, 5).Select(_ => Random.Shared.Next(100, 999)).ToList();
        activity?.SetTag("batch.size", ids.Count);
        logger.LogInformation("API dispatching batch of {Count} orders via gRPC", ids.Count);

        var results = new List<object>();
        foreach (var id in ids)
        {
            using var itemSpan = activitySource.StartActivity("api.batch-item");
            itemSpan?.SetTag("order.id", id);

            var reply = await grpcClient.ProcessOrderAsync(new ProcessOrderRequest { OrderId = id });
            results.Add(new { reply.OrderId, reply.Status, reply.Enriched });
        }

        orderCounter.Add(ids.Count, new KeyValuePair<string, object?>("source", "batch"));
        logger.LogInformation("Batch completed: {Count} orders processed via gRPC", ids.Count);
        return Results.Ok(new { batchSize = ids.Count, orderIds = ids, results });
    }
    finally
    {
        Interlocked.Decrement(ref activeRequests);
        requestDuration.Record(sw.Elapsed.TotalSeconds, new KeyValuePair<string, object?>("endpoint", "batch"));
    }
});

// --- GET /error — trace: api.error ---
app.MapGet("/error", () =>
{
    using var activity = activitySource.StartActivity("api.error-simulated");
    errorsCounter.Add(1, new KeyValuePair<string, object?>("type", "simulated"));
    activity?.SetStatus(ActivityStatusCode.Error, "Simulated API error");
    throw new InvalidOperationException("Simulated API error");
});

// --- GET /health/ready — trace: api.readiness-check ---
app.MapGet("/health/ready", async (OrderService.OrderServiceClient grpcClient, ILogger<Program> logger) =>
{
    using var activity = activitySource.StartActivity("api.readiness-check");
    try
    {
        var reply = await grpcClient.ProcessOrderAsync(new ProcessOrderRequest { OrderId = 0 });
        return Results.Ok(new { status = "ready", backend = "up", protocol = "grpc" });
    }
    catch (Exception ex)
    {
        logger.LogError(ex, "Readiness check failed — backend unreachable via gRPC");
        return Results.Json(new { status = "not_ready", backend = "unreachable", protocol = "grpc" }, statusCode: 503);
    }
});

// --- GET /order/{id}/trace — demonstrates Baggage propagation ---
app.MapGet("/order/{id}/trace", async (int id, OrderService.OrderServiceClient grpcClient, ILogger<Program> logger) =>
{
    using var activity = activitySource.StartActivity("api.trace-with-baggage");
    activity?.SetTag("order.id", id);

    using (logger.BeginScope(new Dictionary<string, object> { ["TenantId"] = "acme-corp", ["OrderId"] = id }))
    {
        Baggage.SetBaggage("tenant.id", "acme-corp");
        Baggage.SetBaggage("order.id", id.ToString());
        Baggage.SetBaggage("feature.flag", "new-pricing-v2");

        logger.LogInformation("Calling Backend with baggage for order {OrderId}", id);

        var baggageReply = await grpcClient.ReadBaggageAsync(new EmptyRequest());

        return Results.Ok(new { orderId = id, baggageReceivedByBackend = baggageReply.BaggageItems, processedBy = baggageReply.ProcessedBy });
    }
});

// --- GET /order/{id}/events — demonstrates Span Events ---
app.MapGet("/order/{id}/events", async (int id, OrderService.OrderServiceClient grpcClient, ILogger<Program> logger) =>
{
    using var activity = activitySource.StartActivity("api.order-with-events");
    activity?.SetTag("order.id", id);

    activity?.AddEvent(new ActivityEvent("order.received", tags: new ActivityTagsCollection
    {
        { "order.id", id },
        { "order.source", "api" }
    }));

    await Task.Delay(Random.Shared.Next(5, 20));
    activity?.AddEvent(new ActivityEvent("order.validated"));

    var reply = await grpcClient.ProcessOrderAsync(new ProcessOrderRequest { OrderId = id });
    activity?.AddEvent(new ActivityEvent("order.enriched", tags: new ActivityTagsCollection
    {
        { "enriched_by", reply.ProcessedBy }
    }));

    activity?.AddEvent(new ActivityEvent("order.completed", tags: new ActivityTagsCollection
    {
        { "status", reply.Status }
    }));

    logger.LogInformation("Order {OrderId} processed with events", id);
    return Results.Ok(new { reply.OrderId, reply.Status, events = new[] { "received", "validated", "enriched", "completed" } });
});

// --- GET /parallel/{count} — demonstrates parallel fan-out ---
app.MapGet("/parallel/{count}", async (int count, OrderService.OrderServiceClient grpcClient, ILogger<Program> logger) =>
{
    var sw = Stopwatch.StartNew();
    using var activity = activitySource.StartActivity("api.parallel-fan-out");
    activity?.SetTag("fan_out.count", count);

    var tasks = Enumerable.Range(1, Math.Min(count, 20)).Select(async i =>
    {
        using var itemSpan = activitySource.StartActivity("api.parallel-item");
        itemSpan?.SetTag("item.id", i);

        var reply = await grpcClient.ProcessOrderAsync(new ProcessOrderRequest { OrderId = i, Product = $"parallel-{i}", Quantity = 1 });
        return new { reply.OrderId, reply.Status };
    });

    var results = await Task.WhenAll(tasks);

    requestDuration.Record(sw.Elapsed.TotalSeconds, new KeyValuePair<string, object?>("endpoint", "parallel"));
    logger.LogInformation("Parallel fan-out completed: {Count} items in {Duration}ms", count, sw.ElapsedMilliseconds);
    return Results.Ok(new { count = results.Length, durationMs = sw.ElapsedMilliseconds, results });
});

// --- GET /retry/{id} — demonstrates retry with span per attempt ---
app.MapGet("/retry/{id}", async (int id, OrderService.OrderServiceClient grpcClient, ILogger<Program> logger) =>
{
    using var activity = activitySource.StartActivity("api.retry-operation");
    activity?.SetTag("order.id", id);

    var maxRetries = 3;
    for (int attempt = 1; attempt <= maxRetries; attempt++)
    {
        using var retrySpan = activitySource.StartActivity("api.retry-attempt");
        retrySpan?.SetTag("retry.attempt", attempt);
        retrySpan?.SetTag("retry.max", maxRetries);

        try
        {
            var reply = await grpcClient.UnstableOperationAsync(new UnstableRequest { RequestId = id, FailCount = 2 });
            retrySpan?.SetTag("retry.result", "success");
            logger.LogInformation("Retry succeeded on attempt {Attempt} for request {RequestId}", attempt, id);
            return Results.Ok(new { reply.RequestId, reply.Status, reply.Attempts, attempt, reply.ProcessedBy });
        }
        catch (Grpc.Core.RpcException ex) when (ex.StatusCode == Grpc.Core.StatusCode.Unavailable)
        {
            retrySpan?.SetStatus(ActivityStatusCode.Error, $"Attempt {attempt} failed: {ex.Status.Detail}");
            logger.LogWarning(ex, "Retry attempt {Attempt} failed for request {RequestId}", attempt, id);

            if (attempt == maxRetries)
            {
                activity?.SetStatus(ActivityStatusCode.Error, "All retries exhausted");
                return Results.Json(new { requestId = id, status = "failed", attempts = attempt, error = ex.Status.Detail }, statusCode: 503);
            }

            await Task.Delay(100 * attempt);
        }
    }

    return Results.StatusCode(500);
});

// --- GET /cache/{id} — demonstrates cache hit/miss pattern ---
var cache = new System.Collections.Concurrent.ConcurrentDictionary<int, object>();
var cacheHits = meter.CreateCounter<long>("cache.hits_total", "hits", "Cache hits");
var cacheMisses = meter.CreateCounter<long>("cache.misses_total", "misses", "Cache misses");

app.MapGet("/cache/{id}", async (int id, OrderService.OrderServiceClient grpcClient, ILogger<Program> logger) =>
{
    using var activity = activitySource.StartActivity("api.cache-lookup");
    activity?.SetTag("order.id", id);

    if (cache.TryGetValue(id, out var cached))
    {
        activity?.SetTag("cache.hit", true);
        cacheHits.Add(1);
        logger.LogInformation("Cache HIT for order {OrderId}", id);
        return Results.Ok(new { source = "cache", data = cached });
    }

    activity?.SetTag("cache.hit", false);
    cacheMisses.Add(1);
    logger.LogInformation("Cache MISS for order {OrderId} — calling backend", id);

    var reply = await grpcClient.ProcessOrderAsync(new ProcessOrderRequest { OrderId = id });
    var result = new { reply.OrderId, reply.Status, reply.Enriched, reply.ProcessedBy };
    cache.TryAdd(id, result);

    return Results.Ok(new { source = "backend", data = result });
});

app.Run();

record OrderRequest(string Product, int Quantity);
