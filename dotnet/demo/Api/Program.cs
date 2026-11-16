// demo-dotnet-api (specs/demo-environment, task DE3d): POST /orders, the gRPC
// client of demo.v1.OrderService, the Redis cache client and the queue producer.
// Every setting comes from the environment (DR0; DE-D13 r3.3):
//   DEMO_HTTP_PORT (8080), DEMO_BACKEND_ADDR (demo-dotnet-backend:50051),
//   DEMO_REDIS_ADDR (demo-redis:6379), DEMO_REDIS_PASSWORD_FILE (required, read once).
using StackExchange.Redis;
using StaffOps.OtelHelper.Redis;
using StaffOps.OtelLibs.Demo;
using StaffOps.OtelLibs.Demo.Api;
using StaffOps.OtelLibs.Demo.V1;

var builder = WebApplication.CreateBuilder(args);
// The helper's exporter (or its stdout fallback) is the only log output.
builder.Logging.ClearProviders();

var httpPort = DemoSettings.Port("DEMO_HTTP_PORT", 8080);
builder.WebHost.ConfigureKestrel(kestrel => kestrel.ListenAnyIP(httpPort));

builder.Services.AddDemoTelemetry();
builder.Services.AddSingleton(_ => DemoRedis.ConnectFromEnvironment());
// Instruments the multiplexer registered above (resolved when the tracer provider is built).
builder.Services.AddOtelHelperRedis();

var backendUri = DemoSettings.BackendUri(DemoSettings.Get("DEMO_BACKEND_ADDR", "demo-dotnet-backend:50051"));
builder.Services.AddSingleton(_ => Grpc.Net.Client.GrpcChannel.ForAddress(backendUri));
builder.Services.AddSingleton(sp => new OrderService.OrderServiceClient(sp.GetRequiredService<Grpc.Net.Client.GrpcChannel>()));

var (redisHost, redisPort) = DemoRedis.ServerOf(DemoSettings.Get("DEMO_REDIS_ADDR", "demo-redis:6379"));
builder.Services.AddSingleton(new OrderEndpoint.Settings(DemoSettings.CurrentRunId(), redisHost, redisPort));
builder.Services.AddSingleton<OrderEndpoint>();

var app = builder.Build();
app.MapPost("/orders", (OrderRequest? order, OrderEndpoint endpoint, CancellationToken cancellationToken) =>
    endpoint.HandleAsync(order, cancellationToken));
// Probe paths: excluded from traces by the helper's health-path filter (design D5).
// GET /health is the probe every language's api serves (DE-D7 r3.4).
app.MapGet("/health", () => Results.Ok("ok"));
app.MapGet("/healthz", () => Results.Ok("ok"));
app.MapGet("/ready", () => Results.Ok("ok"));
app.Run();
