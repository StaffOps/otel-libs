# HOW-TO: Using OtelHelper

Practical guide for developers. One line configures everything — traces, metrics, and correlated logs.

---

## 1. Installation

```bash
dotnet add package OtelHelper
```

> **Note .NET 10**: the lib is compiled for net8.0 and works on .NET 10 via roll-forward. Use the `aspnet` image (not `runtime`) and ensure `DOTNET_ROLL_FORWARD=Major` is set.

---

## 2. Configuration

In `Program.cs`:

```csharp
var builder = WebApplication.CreateBuilder(args);

// One line — everything configured via env vars
builder.Services.AddOtelHelper();

var app = builder.Build();
```

Optionally, with overrides:

```csharp
builder.Services.AddOtelHelper(opts =>
{
    opts.ServiceName = "my-service";
    opts.ResourceAttributes = new Dictionary<string, object>
    {
        ["app.version"] = "2.1.0",
        ["app.team"] = "checkout"
    };
});
```

> No need to configure endpoints, sampling, or exporters. The lib resolves everything via environment variables injected by infrastructure (`SERVICE_NAME`, `ENVIRONMENT`, `OTEL_EXPORTER_OTLP_ENDPOINT`).

---

## 3. What Works Automatically

Without any additional code, the lib instruments:

| Signal | What is captured |
|---|---|
| **Traces** | Incoming HTTP requests (ASP.NET Core), outgoing HTTP requests (HttpClient), gRPC calls. Optionally: SQL queries (`SQL`) and AWS SDK calls (`AWS`) via `OTEL_HELPER_EXTRA_INSTRUMENTATION` |
| **Metrics** | .NET Runtime (GC, thread pool), ASP.NET Core (duration, active requests), HttpClient (outbound requests) |
| **Logs** | All logs via `ILogger` are exported via OTLP with automatic `traceId`/`spanId` |

### Extra Instrumentations

By default, only `SQL` is enabled. To enable more:

```bash
# SQL only (default)
OTEL_HELPER_EXTRA_INSTRUMENTATION=SQL

# SQL + AWS
OTEL_HELPER_EXTRA_INSTRUMENTATION=SQL,AWS

# None
OTEL_HELPER_EXTRA_INSTRUMENTATION=

# Debug mode enables all automatically
OTEL_HELPER_DEBUG_LEVEL=true
```

### gRPC with Service Mesh (REQUIRED)

Applications serving **gRPC** MUST configure Kestrel:

```csharp
builder.WebHost.ConfigureKestrel(options =>
{
    options.AllowAlternateSchemes = true;
});
```

**Why?** The service mesh terminates TLS and forwards as HTTP, causing a `:scheme` header mismatch. Without this option, Kestrel rejects gRPC requests.

Complete example:

```csharp
var builder = WebApplication.CreateBuilder(args);

builder.Services.AddGrpc();
builder.Services.AddOtelHelper();

builder.WebHost.ConfigureKestrel(options =>
{
    options.AllowAlternateSchemes = true;
});

var app = builder.Build();
app.MapGrpcService<MyService>();
app.Run();
```

---

## 4. Logs

### Basic Usage

Inject `ILogger<T>` normally:

```csharp
app.MapGet("/order/{id}", (int id, ILogger<Program> logger) =>
{
    logger.LogInformation("Order {OrderId} received", id);
    // ... process ...
    logger.LogInformation("Order {OrderId} completed", id);
    return Results.Ok(new { orderId = id });
});
```

> `traceId` and `spanId` are added automatically by the SDK. No manual work needed.

### Log Levels

The lib sets the minimum level based on environment:

| Environment | Minimum Level | What appears |
|---|---|---|
| `LOCAL` | Debug | Everything |
| `DEV` | Information | Information, Warning, Error, Critical |
| `HML` | Information | Information, Warning, Error, Critical |
| `PRD` | Warning | Warning, Error, Critical |

### Structured Logs

Use `{Name}` placeholders instead of interpolation. This enables search and filtering:

```csharp
// ✅ Correct — structured, filterable
logger.LogInformation("Order {OrderId} from customer {CustomerId} processed in {Duration}ms", orderId, customerId, elapsed);

// ❌ Wrong — interpolated string, not filterable
logger.LogInformation($"Order {orderId} from customer {customerId} processed in {elapsed}ms");
```

---

## 5. Traces

### Auto-instrumentation (included)

HTTP, gRPC, and SQL requests already generate spans automatically. Example trace from a simple request:

```
GET /order/42                          ← ASP.NET Core (automatic)
  └── HTTP GET http://backend/process  ← HttpClient (automatic)
       └── SELECT * FROM orders        ← SqlClient (automatic, if SQL enabled)
            └── S3 GetObject           ← AWS SDK (automatic, if AWS enabled)
```

### Custom Traces (manual spans)

The lib registers an `ActivitySource` and a `Meter` in DI with the service name. Inject directly:

```csharp
using System.Diagnostics;

// Via DI (recommended)
public class MyService(ActivitySource activitySource) { }

// Or static field (minimal APIs)
var activitySource = new ActivitySource(
    Environment.GetEnvironmentVariable("SERVICE_NAME") ?? "my-service");
```

> **Important:** by default, the lib only captures spans from `ActivitySource` with the same name as `ServiceName`. If you need additional sources, register them in configuration:

```csharp
var orderSource = new ActivitySource("MyApp.Orders");
var paymentSource = new ActivitySource("MyApp.Payments");

services.AddOtelHelper(opts =>
{
    opts.AdditionalActivitySources = new List<string>
    {
        "MyApp.Orders",
        "MyApp.Payments"
    };
});
```

Using in endpoints:

```csharp
app.MapGet("/order/{id}", async (int id, ILogger<Program> logger) =>
{
    using var activity = activitySource.StartActivity("process-order");
    activity?.SetTag("order.id", id);

    using (var dbSpan = activitySource.StartActivity("query-database"))
    {
        dbSpan?.SetTag("db.system", "postgresql");
        await Task.Delay(50);
    }

    activity?.AddEvent(new ActivityEvent("order-validated"));
    return Results.Ok(new { orderId = id, status = "completed" });
});
```

### Mark Span with Error

```csharp
using var activity = activitySource.StartActivity("critical-operation");
try
{
    // ... logic ...
}
catch (Exception ex)
{
    activity?.SetStatus(ActivityStatusCode.Error, ex.Message);
    throw;
}
```

### Traces in Workers / Background Services

In APIs, each request already creates a new trace automatically. In workers (loops, timers, consumers), you need to create the root trace manually. Use `StartRootActivity`:

```csharp
using OtelHelper; // extension method

while (!ct.IsCancellationRequested)
{
    // Each iteration is an independent trace
    using var span = activitySource.StartRootActivity("process-batch");
    span?.SetTag("batch.size", items.Count);

    // ... work ...

    await Task.Delay(interval, ct); // outside the span!
}
```

> `StartRootActivity` clears the previous context and creates a root span (new traceId). Use whenever you want an independent trace per iteration.

---

## 6. Metrics

The lib registers a `Meter` in DI with the service name. Inject or create manually:

```csharp
using System.Diagnostics.Metrics;

// Via DI (recommended)
public class MyService(Meter meter) { }

// Or static field (minimal APIs)
var meter = new Meter(
    Environment.GetEnvironmentVariable("SERVICE_NAME") ?? "my-service");
```

> ⚠️ **The Meter name MUST match ServiceName.** The lib only exports metrics from Meters with that name.

### Counter — counts events

```csharp
var ordersTotal = meter.CreateCounter<long>("orders_total", "orders", "Total orders");
ordersTotal.Add(1, new KeyValuePair<string, object?>("type", "standard"));
```

### Histogram — measures distributions (latency, size)

```csharp
var duration = meter.CreateHistogram<double>("processing_duration_seconds", "s", "Processing duration");
var sw = Stopwatch.StartNew();
// ... process ...
duration.Record(sw.Elapsed.TotalSeconds);
```

### Gauge — value that goes up and down

```csharp
var queueItems = 0;
meter.CreateObservableGauge("queue_items_pending", () => queueItems, "items", "Items waiting in queue");
Interlocked.Increment(ref queueItems);
Interlocked.Decrement(ref queueItems);
```

---

## 7. Exemplars (metric → trace)

Exemplars are automatic. When a metric is recorded during an active trace, the SDK links the metric to the trace. In Grafana, clicking a metric point lets you jump directly to the corresponding trace.

---

## 8. Complete Example

```csharp
using OtelHelper;
using System.Diagnostics;
using System.Diagnostics.Metrics;

var builder = WebApplication.CreateBuilder(args);
builder.Services.AddOtelHelper();
builder.Services.AddHttpClient("backend", c => c.BaseAddress = new Uri("http://backend:8080"));

var app = builder.Build();

var serviceName = Environment.GetEnvironmentVariable("SERVICE_NAME") ?? "checkout-api";
var activitySource = new ActivitySource(serviceName);
var meter = new Meter(serviceName);

var ordersTotal = meter.CreateCounter<long>("orders_total", "orders");
var requestDuration = meter.CreateHistogram<double>("request_duration_seconds", "s");

app.MapPost("/checkout", async (HttpRequest req, IHttpClientFactory http, ILogger<Program> logger) =>
{
    var sw = Stopwatch.StartNew();
    using var activity = activitySource.StartActivity("checkout");

    try
    {
        var body = await req.ReadFromJsonAsync<CheckoutRequest>();
        activity?.SetTag("order.product", body?.Product);
        logger.LogInformation("Checkout started for {Product}", body?.Product);

        var client = http.CreateClient("backend");
        var response = await client.PostAsJsonAsync("/process", body);

        ordersTotal.Add(1, new KeyValuePair<string, object?>("status", "success"));
        return Results.Ok(new { status = "completed" });
    }
    catch (Exception ex)
    {
        activity?.SetStatus(ActivityStatusCode.Error, ex.Message);
        logger.LogError(ex, "Checkout failed");
        throw;
    }
    finally
    {
        requestDuration.Record(sw.Elapsed.TotalSeconds, new KeyValuePair<string, object?>("endpoint", "checkout"));
    }
});

app.Run();

record CheckoutRequest(string Product, int Quantity);
```

---

## 9. Automatic Behaviors

### Health Checks Filtered

The lib **does not generate spans** for the following paths (inbound and outbound):

- `/ping`
- `/health`
- `/healthz`
- `/ready`

### Exceptions Recorded in Spans

When an exception occurs in HTTP requests (inbound/outbound) or SQL queries, the lib automatically records a **span event** with `exception.type`, `exception.message`, and `exception.stacktrace`.

### Framework Logs Filtered

In non-debug environments (DEV, HML, PRD), logs from `Microsoft.*` and `System.Net.Http.*` are only exported if `Error` or `Critical`.

### Structured Logs (ParseStateValues)

When you use placeholders in logs, values are extracted as **separate attributes** in the exported log, enabling queries like: `{service_name="my-service"} | json | OrderId="12345"`

---

## 10. Environment Variables

| Variable | Effect | Default | Who injects |
|---|---|---|---|
| `SERVICE_NAME` | Service name (resource attribute) | `my-service` | CI/CD |
| `ENVIRONMENT` | Environment (controls log level) | `LOCAL` | Helm Chart |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | Collector host | `http://localhost` | Helm Chart |
| `OTEL_HELPER_DEBUG_LEVEL` | Debug mode (Debug logs, all instrumentations, 100% sampling) | `false` | Manual |
| `OTEL_HELPER_EXTRA_INSTRUMENTATION` | Extra instrumentations (SQL, AWS) | `SQL` | Helm Chart |
| `OTEL_HELPER_SAMPLE_RATIO` | Head sampling ratio (0.0-1.0). 1.0 = AlwaysOn | `1.0` | Helm Chart |
| `OTEL_HELPER_METRICS_PORT` | Standalone Prometheus `/metrics` listener port (`0` disables it) | `9464` | Helm Chart |
| `OTEL_METRICS_EXPORTER` | Metric exporter(s): `otlp`, `prometheus`, `otlp,prometheus`, `none` | legacy inference | Manual |
| `OTEL_METRIC_EXPORT_INTERVAL` | OTLP metric export interval (ms) | `30000` | Manual |
| `OTEL_TRACES_SAMPLER` | Standard OTel sampler config — takes priority over `OTEL_HELPER_SAMPLE_RATIO` when set | _(unset)_ | Manual |

### Configuration Priority

1. **Code** (override in `AddOtelHelper(opts => ...)`) — highest priority
2. **Standard OTel environment variable** (`OTEL_METRICS_EXPORTER`, `OTEL_METRIC_EXPORT_INTERVAL`, `OTEL_TRACES_SAMPLER`) — applied if code didn't set the equivalent option
3. **`OTEL_HELPER_*` environment variable** — applied if neither of the above is present
4. **Library default** — used if none of the above defined it

---

## 11. Naming Rules and Anti-patterns

### Naming

| Type | Convention | Example |
|---|---|---|
| Metrics | snake_case with unit | `request_duration_seconds`, `orders_total` |
| Spans | kebab-case | `process-order`, `query-database` |
| Tags | dot.notation | `order.id`, `db.system` |

### What NOT to do

- ❌ Configure sampling in code — it's the Collector's responsibility
- ❌ Export directly to Tempo/Loki/Prometheus — everything goes through the Collector
- ❌ Use `$"string {interpolated}"` in logs — use `{Name}` placeholders
- ❌ Create metrics with unlimited labels (user_id, request_id)
- ❌ Add resource attributes (service.version, k8s.*) — the Collector does this
- ❌ Create ActivitySources without registering in `AdditionalActivitySources`

---

## 12. FAQ

### Will my app crash if the Collector is down?

**No.** The SDK exports telemetry asynchronously (fire-and-forget). If the Collector is unavailable, data is silently discarded after timeout (10s default). The application continues functioning normally.

### Can I use the lib in Workers/Background Services?

Yes. The lib works with any app that uses `IServiceCollection`. Workers, console apps, APIs — all work.

---

## 13. Sampling — How it Works

### You do NOT control sampling in code

Sampling is done by the OTel Collector (Agent). The lib sends **all** spans to the Collector, which decides what to keep.

### Debug mode — force 100% sampling

If you need to see **all** traces from a service:

```bash
OTEL_HELPER_DEBUG_LEVEL=true
```

When active, the lib injects `tracestate: debug=true` in all traces. The Collector recognizes this flag and keeps 100% of traces from that service.

⚠️ **Do not leave debug enabled in production for long** — generates high volume and increases storage cost.

---

## 14. Opt-in Subpackages

AWS, Redis, SQL, and Profiling instrumentations are available as **separate NuGet packages** — not bundled in core.

> ⚠️ **Breaking change**: SQL and AWS were previously bundled in the core package and activated via `OTEL_HELPER_EXTRA_INSTRUMENTATION=SQL,AWS`. They are now separate packages. If you relied on that behavior, install the subpackages and call the corresponding extension methods explicitly.

### Installation

```bash
dotnet add package OtelHelper.AWS
dotnet add package OtelHelper.Redis
dotnet add package OtelHelper.Sql
dotnet add package OtelHelper.Profiling
```

### Usage

```csharp
var builder = WebApplication.CreateBuilder(args);

// Core — traces, metrics, logs
builder.Services.AddOtelHelper();

// Opt-in extensions (add only what you use)
builder.Services.AddOtelHelperAws();
builder.Services.AddOtelHelperSql();
builder.Services.AddOtelHelperRedis();
// Or with explicit multiplexer:
// builder.Services.AddOtelHelperRedis(connectionMultiplexer);
builder.Services.AddOtelHelperProfiling();
```

| Package | Extension Method | What it instruments |
|---------|-----------------|---------------------|
| `OtelHelper.AWS` | `AddOtelHelperAws()` | AWS SDK calls (S3, SQS, DynamoDB, etc.) |
| `OtelHelper.Redis` | `AddOtelHelperRedis()` | StackExchange.Redis commands |
| `OtelHelper.Sql` | `AddOtelHelperSql()` | SqlClient queries |
| `OtelHelper.Profiling` | `AddOtelHelperProfiling()` | .NET CLR profiling via OTel |

### Profiling Prerequisites

`OtelHelper.Profiling` requires the following environment variables for the .NET CLR profiler:

```bash
CORECLR_ENABLE_PROFILING=1
CORECLR_PROFILER={918728DD-259F-4A6A-AC2B-B85E1B658571}
CORECLR_PROFILER_PATH=/opt/profiler/libProfiler.so
LD_PRELOAD=/opt/profiler/libProfiler.so
```

These are typically set in the Dockerfile or Helm values for the target deployment.

---

## 15. Metrics Without a Collector (Prometheus `/metrics`)

When `OTEL_EXPORTER_OTLP_ENDPOINT` is **not set**, the library automatically falls back to local-only mode:

| Signal | Behavior |
|--------|----------|
| Metrics | Exposed via Prometheus HTTP `/metrics` on port 9464 |
| Traces | In-process only (context propagation works, no export) |
| Logs | stdout/console only (no OTel export) |

The port is configurable via `OTEL_HELPER_METRICS_PORT` env var (default: `9464`).

This enables the standard Kubernetes scrape pattern: deploy without a Collector, and let Prometheus/VictoriaMetrics scrape `/metrics` directly from the pod.

### Running OTLP push AND `/metrics` at the same time

`OTEL_METRICS_EXPORTER` selects which exporter(s) are active on the metrics pipeline, independent of the legacy endpoint-based fallback above:

| `OTEL_METRICS_EXPORTER` | Behavior |
|---|---|
| _(unset)_ | Legacy: OTLP if `OTEL_EXPORTER_OTLP_ENDPOINT` is set, else Prometheus fallback |
| `otlp` | OTLP only. Fails validation at startup if no endpoint is configured |
| `prometheus` | `/metrics` only, even when an OTLP endpoint IS set |
| `otlp,prometheus` | **Both** — one `MeterProvider`, two readers. Same instrument, same values, in both outputs |
| `none` | Metrics disabled entirely (equivalent to `OTEL_HELPER_DISABLED_SIGNALS=metrics`) |

The equivalent programmatic option — `TelemetryOptions.MetricExporters` (comma-separated string) — wins over the env var:

```csharp
builder.Services.AddOtelHelper(opts =>
{
    opts.MetricExporters = "otlp,prometheus";
});
```

`OTEL_METRIC_EXPORT_INTERVAL` (milliseconds) controls the OTLP push interval; the library default is 30000ms, overriding the SDK's own 60s default. `TelemetryOptions.ExportIntervalMs` wins over the env var.

### ASP.NET Core: mounting `/metrics` on your own pipeline

The standalone listener (`AddPrometheusHttpListener`, port 9464 by default) binds its own `HttpListener` — fine for workers/CLIs with no HTTP server of their own, but the **wrong** choice for a web app running behind Kestrel: it's a second port to expose, and it breaks under multiple workers/replicas sharing a pod (each process tries to bind the same port).

For ASP.NET Core apps, add `OpenTelemetry.Exporter.Prometheus.AspNetCore` to your own project (core does not take this dependency) and map the scrape endpoint on the app's existing pipeline:

```bash
dotnet add package OpenTelemetry.Exporter.Prometheus.AspNetCore
```

```csharp
var builder = WebApplication.CreateBuilder(args);

builder.Services.AddOtelHelper(opts =>
{
    opts.MetricExporters = "prometheus"; // or "otlp,prometheus" for dual mode
    opts.PrometheusMetricsPort = 0;       // disable the standalone HttpListener
});

var app = builder.Build();
app.MapPrometheusScrapingEndpoint(); // serves /metrics on the app's own Kestrel pipeline
app.Run();
```

`PrometheusMetricsPort = 0` is required here — otherwise the core library still binds its own listener on 9464 alongside the ASP.NET Core endpoint, which is redundant (and a second port to manage). Use the standalone listener (default `PrometheusMetricsPort = 9464`, no code change needed) only for headless processes without their own HTTP pipeline.

### Scoping down what gets scraped

`OTEL_HELPER_DISABLED_METRICS` drops instruments from **every** active exporter — there's no supported way to push everything via OTLP while exposing only a subset on `/metrics` from the same `MeterProvider` (the OTel SDK's Views are per-provider, not per-reader). If you need a narrower `/metrics` output while keeping the full set on OTLP, filter on the scrape side instead — e.g. Prometheus `metric_relabel_configs` or a vmagent `drop` relabel rule matching the instrument name.

---

## 16. Metrics Export Interval

Metrics are exported every **30 seconds** (not the SDK default of 60s). This applies to both OTLP export and the Prometheus `/metrics` fallback.

---

## 17. TLS Transport

OTLP export uses TLS by default. The transport is derived from the endpoint scheme:

| Endpoint | Transport |
|----------|-----------|
| `https://host:4317` | TLS (system CA trust store) |
| `http://host:4317` | Plaintext (insecure) |
| `host:4317` (no scheme) | TLS (secure by default) |

Override via env var:

```bash
# Force plaintext for a local collector without TLS
OTEL_EXPORTER_OTLP_ENDPOINT=https://otel-gateway.example:4317  # TLS
OTEL_EXPORTER_OTLP_INSECURE=true                                # forces plaintext regardless of scheme
```

Priority: code config (scheme in `TelemetryOptions.OtelCollectorEndpoint`) > `OTEL_EXPORTER_OTLP_INSECURE` env var > scheme-based detection.

For local development with a plaintext collector, the default `http://localhost` already resolves to plaintext — no changes needed.