using OtelHelper;
using DotnetBackend.Services;

var builder = WebApplication.CreateBuilder(args);

builder.Services.AddGrpc();
builder.Services.AddGrpcReflection();

// Istio ambient mode terminates TLS (https→http) causing :scheme mismatch.
// Kestrel rejects gRPC requests when scheme doesn't match. This allows it.
builder.WebHost.ConfigureKestrel(options =>
{
    options.AllowAlternateSchemes = true;
});
builder.Services.AddOtelHelper(opts =>
{
    opts.ResourceAttributes = new Dictionary<string, object>
    {
        ["app.component"] = "order-processor"
    };
    opts.AdditionalActivitySources = new List<string>
    {
        "sample-backend.db",
        "sample-backend.external"
    };
});
builder.Services.AddHttpClient("external");

var app = builder.Build();

app.MapGrpcService<OrderGrpcService>();
app.MapGrpcReflectionService();
app.MapGet("/", () => "SampleBackend gRPC is running");

app.Run();
