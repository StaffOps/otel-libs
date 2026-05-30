using OtelHelper;

var builder = Host.CreateApplicationBuilder(args);

var serviceName = Environment.GetEnvironmentVariable("SERVICE_NAME") ?? "sample-process";

builder.Services.AddOtelHelper(opts =>
{
    opts.ResourceAttributes = new Dictionary<string, object>
    {
        ["app.component"] = "batch-processor"
    };
    opts.AdditionalActivitySources = new List<string>
    {
        "sample-process.api-health",
        "sample-process.heavy-work",
        "sample-process.queue-consumer",
        "sample-process.scheduled-job"
    };
});

builder.Services.AddHttpClient("sample-api", client =>
{
    client.BaseAddress = new Uri(
        Environment.GetEnvironmentVariable("API_URL") ?? "http://localhost:5050");
});

builder.Services.AddHostedService<ApiHealthWorker>();
builder.Services.AddHostedService<HeavyProcessWorker>();
builder.Services.AddHostedService<QueueConsumerWorker>();
builder.Services.AddHostedService<ScheduledJobWorker>();

var host = builder.Build();
host.Run();
