// .NET smoke program (task C17): emits one span, one metric and one log record
// through StaffOps.OtelHelper and exits; testing/e2e/smoke.sh checks the
// Collector received all three. Same markers as the Go and Node smoke programs
// (testing/e2e/go-smoke/main.go, testing/e2e/node-smoke/smoke.cjs): span
// "e2e-smoke-span", counter "e2e.smoke.runs", WARN record "e2e-smoke-log",
// written inside the span so the log record carries its trace_id/span_id.
// The dotnet/example apps are long-running services; this program stands in
// for them the way go-smoke does for the Go examples.
using System.Diagnostics;
using System.Diagnostics.Metrics;
using Microsoft.Extensions.DependencyInjection;
using Microsoft.Extensions.Hosting;
using Microsoft.Extensions.Logging;
using StaffOps.OtelHelper;

var builder = Host.CreateApplicationBuilder(args);
// The console provider would print every record a second time on stdout.
builder.Logging.ClearProviders();
builder.Services.AddOtelHelper();

using var host = builder.Build();
await host.StartAsync();

// The helper's ActivitySource and Meter (name = the resolved service.name) are
// keyed services: an unkeyed ActivitySource resolves to another source.
var source = host.Services.GetRequiredKeyedService<ActivitySource>(OtelHelperKeys.Instrumentation);
var meter = host.Services.GetRequiredKeyedService<Meter>(OtelHelperKeys.Instrumentation);
var logger = host.Services.GetRequiredService<ILoggerFactory>().CreateLogger("e2e-dotnet-smoke");

using (source.StartRootActivity("e2e-smoke-span"))
{
    meter.CreateCounter<long>("e2e.smoke.runs").Add(1);
    logger.LogWarning("e2e-smoke-log");
}

// Before the host is disposed: the result covers the helper's push exports only.
var flushed = host.Services.ForceFlushOtelHelper(10_000);
await host.StopAsync();
Console.Error.WriteLine($"dotnet-smoke: emitted span, metric and log (flush {(flushed ? "ok" : "incomplete")})");
return flushed ? 0 : 1;
