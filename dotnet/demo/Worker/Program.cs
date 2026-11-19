// demo-dotnet-worker (specs/demo-environment, task DE3d): the load driver and the
// queue consumer. Every setting comes from the environment (DR0; DE-D13 r3.3):
//   DEMO_API_URL (http://demo-dotnet-api:8080), DEMO_REDIS_ADDR (demo-redis:6379),
//   DEMO_REDIS_PASSWORD_FILE (required, read once), DEMO_INTERVAL_SECONDS (5),
//   DEMO_POLL_SECONDS (2).
using StaffOps.OtelHelper.Redis;
using StaffOps.OtelLibs.Demo;
using StaffOps.OtelLibs.Demo.Worker;

var builder = Host.CreateApplicationBuilder(args);
// The helper's exporter (or its stdout fallback) is the only log output.
builder.Logging.ClearProviders();

builder.Services.AddDemoTelemetry();
builder.Services.AddSingleton(_ => DemoRedis.ConnectFromEnvironment());
// Instruments the multiplexer registered above (resolved when the tracer provider is built).
builder.Services.AddOtelHelperRedis();

var apiBase = DemoSettings.ApiBase(DemoSettings.Get("DEMO_API_URL", "http://demo-dotnet-api:8080"));
var (redisHost, redisPort) = DemoRedis.ServerOf(DemoSettings.Get("DEMO_REDIS_ADDR", "demo-redis:6379"));

// One client for the process; its outbound spans come from the helper's HttpClient instrumentation.
builder.Services.AddSingleton(_ => new HttpClient { BaseAddress = apiBase, Timeout = TimeSpan.FromSeconds(10) });
builder.Services.AddSingleton(new Consumer.Settings(Environment.MachineName, DemoSettings.Seconds("DEMO_POLL_SECONDS", 2), redisHost, redisPort));
builder.Services.AddHostedService(sp => new Driver(sp.GetRequiredService<HttpClient>(), sp.GetRequiredService<ILogger<Driver>>(),
    DemoSettings.Seconds("DEMO_INTERVAL_SECONDS", 5)));
builder.Services.AddHostedService<Consumer>();

using var host = builder.Build();
await host.RunAsync();
