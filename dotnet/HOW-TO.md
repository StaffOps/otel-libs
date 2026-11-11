# HOW-TO: Using StaffOps.OtelHelper

Practical guide for developers. One line configures everything — traces, metrics, and correlated logs.

> **Version note.** This guide describes the shipped **0.2.x** .NET behavior.
> The `0.3.0` config contract ships in Go and Python first; .NET adopts it in
> Phase C ([tasks](https://github.com/StaffOps/otel-libs/blob/main/specs/config-contract-v0.3/tasks.md),
> ADR-004). See the [root README](https://github.com/StaffOps/otel-libs/blob/main/README.md) for the cross-language matrix.

---

## 1. Installation

```bash
dotnet add package StaffOps.OtelHelper
```

> **Package name**: `StaffOps.OtelHelper*` from `0.3.0-rc.1` (not published
> yet); the released `0.2.x` is `OtelHelper*` with `using OtelHelper;`. Map
> the IDs to the GitHub source with the `nuget.config` in
> [`CONSUMING.md`](https://github.com/StaffOps/otel-libs/blob/main/CONSUMING.md).

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

### Your own resource configuration

Register your own `ConfigureResource` **before** `AddOtelHelper()`:

```csharp
// Before: the helper keeps these attributes (they beat OTEL_RESOURCE_ATTRIBUTES,
// lose to TelemetryOptions.ResourceAttributes); service.name stays the resolved one.
builder.Services.AddOpenTelemetry().ConfigureResource(r => r.AddAttributes(new[]
{
    new KeyValuePair<string, object>("app.team", "checkout"),
}));
builder.Services.AddOtelHelper();
```

The SDK runs resource callbacks in registration order and has no hook after them, so a
`ConfigureResource` registered **after** `AddOtelHelper()` overrides the helper's resource on
every signal (an `AddService` there replaces `service.name` and the instance id, and its schema
URL conflicts with the helper's, leaving none; a `SetResourceBuilder(...)` there replaces that
signal's whole resource). At host start the helper compares each built provider's resource with
what it set and warns once on standard error naming the signals and what changed; without a host
(a bare `ServiceProvider`) there is no such check.

The helper rebuilds the resource from what earlier callbacks added and drops a value that equals
what the SDK's own `OTEL_RESOURCE_ATTRIBUTES` parse produces for the same key. When the SDK and the
helper decode a value differently (the SDK reads `+` as a space, the helper keeps it), an app value
equal to the SDK's spelling is dropped and the helper's parse is emitted instead.

A blank `ServiceName` or `Environment` (`""` or whitespace) fails at start; leave it `null`. Identity
warnings are written once per service provider, not once per process.

---

## 3. What Works Automatically

Without any additional code, the lib instruments:

| Signal | What is captured |
|---|---|
| **Traces** | Incoming HTTP requests (ASP.NET Core), outgoing HTTP requests (HttpClient), gRPC client calls (one CLIENT span per call), gRPC server calls (RPC spans only with the upstream key, below). SQL, AWS and Redis only through the opt-in subpackages (§14) |
| **Metrics** | .NET Runtime (GC, thread pool), ASP.NET Core and HttpClient request durations; the active requests and connection metrics only with `OTEL_HELPER_HTTP_OPT_IN_METRICS=true` (§9) |
| **Logs** | All logs via `ILogger` are exported via OTLP with automatic `traceId`/`spanId` |

### Extra Instrumentations

SQL, AWS and Redis instrumentation come only from the opt-in subpackages
(`StaffOps.OtelHelper.Sql`, `StaffOps.OtelHelper.AWS`, `StaffOps.OtelHelper.Redis` — see §14).
`OTEL_HELPER_EXTRA_INSTRUMENTATION` and the `[Obsolete]` option
`TelemetryOptions.ExtraInstrumentation` **have no effect** in core, and
`OTEL_HELPER_DEBUG_LEVEL=true` does not turn instrumentations on.

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

### gRPC server spans: set the upstream key (0.3, this branch, task C11)

The ASP.NET Core instrumentation traces gRPC server calls as RPC spans only
when the **application** sets its experimental key in its configuration; the
helper never sets it (owner decision O7 (b)):

```bash
OTEL_DOTNET_EXPERIMENTAL_ASPNETCORE_ENABLE_GRPC_INSTRUMENTATION=true
```

or in `appsettings.json`:

```json
{ "OTEL_DOTNET_EXPERIMENTAL_ASPNETCORE_ENABLE_GRPC_INSTRUMENTATION": "true" }
```

| | Without the key | With the key |
|---|---|---|
| Server span name | `POST /order.OrderService/CancelOrder` | `order.OrderService/CancelOrder` |
| Attributes | HTTP ones (`http.request.method`, `url.path`, `http.response.status_code=200`) | `rpc.system.name=grpc`, `rpc.method`, `rpc.response.status_code` |
| Status of a failed RPC | from the HTTP status: 200, so UNSET | ERROR for `UNKNOWN`, `DEADLINE_EXCEEDED`, `UNIMPLEMENTED`, `INTERNAL`, `UNAVAILABLE`, `DATA_LOSS`; UNSET for the other codes (`NOT_FOUND` included) |
| `grpc.health.v1.Health/*` | no span | no span |

When gRPC endpoints are mapped and the key is not `true`, the helper writes
one line at start:
`otelhelper: WARN gRPC endpoints are mapped but OTEL_DOTNET_EXPERIMENTAL_ASPNETCORE_ENABLE_GRPC_INSTRUMENTATION is not true in the application configuration: … variable=OTEL_DOTNET_EXPERIMENTAL_ASPNETCORE_ENABLE_GRPC_INSTRUMENTATION`.
The value is read as the instrumentation reads it (`bool.TryParse`, so `True`
works and `1` does not).

gRPC calls are counted in `http.server.request.duration` with
`http.response.status_code=200` even when the RPC fails: there is no RPC
metric in the .NET instrumentations. Compute gRPC error rates from spans.

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
| unset, or any other value | Information | Information, Warning, Error, Critical |

The environment name is matched ignoring ASCII case (`prd` picks Warning) and is emitted
as written. An unset environment is no longer `LOCAL`: the
`deployment.environment.name` attribute is absent, the level is Information and the
helper warns once on standard error.

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

The lib registers an `ActivitySource` and a `Meter` in DI with the service name, as keyed services (key `OtelHelperKeys.Instrumentation`; an unkeyed `ActivitySource` in a web host resolves to ASP.NET Core's own source). Inject them with the key:

```csharp
using System.Diagnostics;
using StaffOps.OtelHelper;

// Via DI (recommended)
public class MyService([FromKeyedServices(OtelHelperKeys.Instrumentation)] ActivitySource activitySource) { }

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
using StaffOps.OtelHelper; // extension method

while (!ct.IsCancellationRequested)
{
    // Each iteration is an independent trace
    using var span = activitySource.StartRootActivity("process-batch");
    span?.SetTag("batch.size", items.Count);

    // ... work ...

    await Task.Delay(interval, ct); // outside the span!
}
```

> `StartRootActivity` ignores the current activity and creates a root span (new traceId). Use whenever you want an independent trace per iteration. 0.3 (this branch, task C10): while the root runs it is `Activity.Current`; when it is disposed, the activity that was current before the call is current again (0.2.x left `Activity.Current` null). When no activity is created (no listener, or the sampler drops it) the call returns `null` and leaves `Activity.Current` as it was.

---

## 6. Metrics

The lib registers a `Meter` in DI with the service name (keyed, `OtelHelperKeys.Instrumentation`). Inject or create manually:

```csharp
using System.Diagnostics.Metrics;
using StaffOps.OtelHelper;

// Via DI (recommended)
public class MyService([FromKeyedServices(OtelHelperKeys.Instrumentation)] Meter meter) { }

// Or static field (minimal APIs)
var meter = new Meter(
    Environment.GetEnvironmentVariable("SERVICE_NAME") ?? "my-service");
```

> ⚠️ **The Meter name MUST be the service name, start with `<ServiceName>.`, or be listed in `AdditionalMeters`.** The lib exports metrics only from those Meters (0.3, this branch: `<ServiceName>.*`, for example `orders-api.billing`, is exported without listing it; 0.2.x exported only a Meter named exactly `ServiceName`).

```csharp
// A library meter of another name: list it.
builder.Services.AddOtelHelper(opts => opts.AdditionalMeters.Add("Example.Payments"));

// A child meter of the service: exported as is.
var billing = new Meter("orders-api.billing");
```

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

The helper sets the exemplar filter itself, because the .NET SDK default is `AlwaysOff` while the OpenTelemetry specification's is `trace_based`. 0.3 (this branch, task C9): `OTEL_METRICS_EXEMPLAR_FILTER` chooses it — `trace_based` (default: only measurements taken under a sampled span), `always_on`, `always_off`; any other value warns and `trace_based` is used. To set it in code, register your own call after `AddOtelHelper()`, which wins (a call registered before `AddOtelHelper()` is overridden by the helper's, without a warning):

```csharp
builder.Services.AddOtelHelper();
builder.Services.ConfigureOpenTelemetryMeterProvider(b => b.SetExemplarFilter(ExemplarFilterType.AlwaysOff));
```

`OTEL_DOTNET_EXPERIMENTAL_METRICS_EXEMPLAR_FILTER_HISTOGRAMS` (the .NET SDK's histogram-only filter) has no effect: the SDK reads it only when no filter was set in code, and the helper always sets one (0.2.x also set one, so it had no effect there either).

---

## 8. Complete Example

```csharp
using StaffOps.OtelHelper;
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

The lib **does not generate spans** for the following paths (inbound and
outbound), the contract list of every language (0.3, this branch, task C11):

- `/health`, `/healthz`
- `/ready`, `/readyz`
- `/live`, `/livez`
- `/ping`
- `/metrics`

Matching: the path is cut at `?`/`#`, percent-decoded, ASCII-lowercased and
stripped of trailing slashes, then compared exactly — `/Health/`,
`/healthz?probe=1`, `/%68ealth` and `/HEALTH%2f` match `/health`;
`/users/ping`, `/healthcare` and `/health/live` do not. The gRPC health
service `grpc.health.v1.Health` (Check, Watch, List) is never traced, on the
server or the client. Health requests are still counted in
`http.server.request.duration` (so probe failures stay visible; exclude health
routes in SLI queries) and spans started inside a health request are dropped
too. 0.2.x: `/ping`, `/health`, `/healthz`, `/ready` only, no trailing-slash,
query or encoding handling.

An HttpClient call to a health path is sent **without trace context** (no
`traceparent` or `tracestate` written by the propagators; C11 fix rounds 1
and 2), as Go's transport does: a `traceparent` would name a client span that
is never exported. Headers the application set on the request itself (its
own `baggage` or `tracestate`) are kept, and propagated baggage still goes
(it names no span). With traces disabled (pass-through) a health call carries
no inbound context either. Limit: when the client activity is not sampled
(an unsampled caller) the instrumentation's filter does not run, and the
OpenTelemetry propagator still writes `traceparent …-00` — unsampled, so it
names no exported span. The call is still counted in `http.client.request.duration`.

Replace the list in code (an empty list traces every HTTP path; each path
must be ASCII, start with `/` and have no `?`, `#` or `,`, or start fails):

```csharp
builder.Services.AddOtelHelper(opts => opts.HealthPaths = new List<string> { "/status", "/internal/ready" });
```

**Your own instrumentation options.** The helper sets `Filter` and
`RecordException` on `AspNetCoreTraceInstrumentationOptions`,
`FilterHttpRequestMessage` and `RecordException` on
`HttpClientTraceInstrumentationOptions`, and `SuppressDownstreamInstrumentation`
and `Filter` on `GrpcClientTraceInstrumentationOptions`. Configure these options
**after** `AddOtelHelper()` (`services.Configure<AspNetCoreTraceInstrumentationOptions>(…)`):
a `Filter` you set there replaces the helper's health filter (keep the health
behavior by calling your own check and returning `false` for health paths
yourself); a `Configure` registered before `AddOtelHelper()` is overwritten by
the helper's for the members above.

### HTTP opt-in metrics (0.3, this branch, task C11)

By default only `http.server.request.duration` and
`http.client.request.duration` are exported, as in Go, Python and Node.js.
`OTEL_HELPER_HTTP_OPT_IN_METRICS=true` (or `opts.HttpOptInMetrics = true`)
keeps what ASP.NET Core and `System.Net.Http` record besides:
`http.server.active_requests`, `http.client.active_requests`,
`http.client.open_connections`, `http.client.connection.duration` and
`http.client.request.time_in_queue` (one INFO line says so; .NET records no
body sizes). 0.2.x exported them always: dashboards on them need the switch.
An instrument of your own meter with one of these names is kept, and
`OTEL_HELPER_DISABLED_METRICS` still drops a switched-on one.

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
| `OTEL_SERVICE_NAME` | Service name (`service.name`); wins over `OTEL_RESOURCE_ATTRIBUTES` and `SERVICE_NAME` | `unknown_service:<process name>` + warning | CI/CD |
| `SERVICE_NAME` | Deprecated alias of `OTEL_SERVICE_NAME`, read last | — | CI/CD |
| `OTEL_RESOURCE_ATTRIBUTES` | `key=value,…` resource attributes (helper-parsed: percent-decoded, ASCII-trimmed, bad items skipped and warned by position); `deployment.environment.name` here wins over `ENVIRONMENT` | — | Helm Chart |
| `ENVIRONMENT` | Deprecated alias of `deployment.environment.name` (controls log level; known names canonicalized, others as written) | none: attribute absent + warning | Helm Chart |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | Collector endpoint; unset → no OTLP export, Prometheus `/metrics` fallback | _(unset)_ | Helm Chart |
| `OTEL_HELPER_DEBUG_LEVEL` | Debug mode: Debug log level + `debug=true` attribute on entry spans (no parent, or a remote one); the tracestate is not touched (0.2.x prepended `debug=true` to it) | `false` | Manual |
| `OTEL_HELPER_EXTRA_INSTRUMENTATION` | No effect in core (use the subpackages, §14) | `SQL` | — |
| `OTEL_HELPER_SAMPLE_RATIO` | Deprecated: a plain decimal in [0, 1] → `parentbased_traceidratio`; anything else warns and is ignored (not clamped); read only while `OTEL_TRACES_SAMPLER` is unset (§13) | _(unset)_ | Helm Chart |
| `OTEL_HELPER_METRICS_PORT` | Standalone Prometheus `/metrics` listener port (`0` disables it) | `9464` | Helm Chart |
| `OTEL_HELPER_HTTP_OPT_IN_METRICS` | `true` keeps the HTTP opt-in metrics (§9); not `true`/`false` → warning, `false` | `false` | Manual |
| `OTEL_DOTNET_EXPERIMENTAL_ASPNETCORE_ENABLE_GRPC_INSTRUMENTATION` | Upstream key, read from the app configuration: `true` traces gRPC server calls as RPC spans (§3) | _(unset)_ | Manual (app) |
| `OTEL_METRICS_EXPORTER` | Metric exporter(s): `otlp`, `prometheus`, `console`, a list such as `otlp,prometheus`, or `none` (`logging` = warned alias of `console`) | legacy inference | Manual |
| `OTEL_TRACES_EXPORTER`, `OTEL_LOGS_EXPORTER` | `otlp`, `console`, `none` (§15 "Choosing exporters per signal") | `otlp` | Manual |
| `OTEL_METRIC_EXPORT_INTERVAL` | OTLP and console metric export interval (ms; digits with an optional `+`, clamped at 2 147 483 647) | `30000` | Manual |
| `OTEL_METRICS_EXEMPLAR_FILTER` | `always_on`, `always_off`, `trace_based` (§7) | `trace_based` | Manual |
| `OTEL_SDK_DISABLED` | `true` turns telemetry off (propagators kept, one INFO line); not `true`/`false` → warning, `false` | `false` | Manual |
| `OTEL_HELPER_DISABLED_SIGNALS` | Deprecated: signals to disable; a set `OTEL_<SIGNAL>_EXPORTER` wins | _(unset)_ | Manual |
| `OTEL_TRACES_SAMPLER`, `OTEL_TRACES_SAMPLER_ARG` | Standard OTel sampler (the six spec names) and its ratio, parsed by the helper; takes priority over `OTEL_HELPER_SAMPLE_RATIO`; unknown names warn (§13) | `parentbased_always_on` | Manual |
| `OTEL_PROPAGATORS` | `tracecontext`, `baggage`, `none`; other tokens warn and are skipped; kept under `OTEL_SDK_DISABLED` (§13) | `tracecontext,baggage` | Manual |
| `OTEL_EXPORTER_OTLP_PROTOCOL` | OTLP wire protocol: `grpc` or `http/protobuf` | port-based inference | Manual |
| `OTEL_EXPORTER_OTLP_HEADERS` | Headers on every export, `key=value,…` (auth); `OTEL_EXPORTER_OTLP_<SIGNAL>_HEADERS` replaces them for one signal (§19) | empty | Helm Chart (secret) |
| `OTEL_EXPORTER_OTLP_TIMEOUT` | Export deadline in milliseconds, per signal with `_<SIGNAL>_TIMEOUT` (§19) | `10000` | Manual |
| `OTEL_EXPORTER_OTLP_COMPRESSION` | `gzip` or `none`, per signal with `_<SIGNAL>_COMPRESSION` (§19) | `gzip` | Manual |
| `OTEL_EXPORTER_OTLP_CERTIFICATE`, `_CLIENT_KEY`, `_CLIENT_CERTIFICATE` | TLS files (CA, client pair), per signal too (§17) | system roots, no client certificate | Helm Chart |

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

When active, the lib marks every **entry span** — a span with no parent or a remote one (`ParentSpanId == default || HasRemoteParent`), which includes server spans that continue a remote trace — with the attribute `debug=true`. A Collector tail-sampling `string_attribute` policy on it keeps those traces. The marker is never read from an inbound tracestate or baggage, and the tracestate is not written (0.3, this branch, task C10, ADR-006; 0.2.x prepended `debug=true` to it, which gave `debug=true,debug=true` under a remote parent, and also marked in-process children started from an explicit context). Under the default ParentBased sampler an entry span whose remote parent is unsampled is dropped before the marker runs: debug mode cannot force retention of a trace the caller did not sample.

Because a debug service marks every inbound request, not only the traces it starts, the Collector's `debug=true` policy must be combined (`and`) with an environment allow-list and a `rate_limiting` sub-policy ([design D11](https://github.com/StaffOps/otel-libs/blob/main/specs/config-contract-v0.3/design.md#d11--collector--platform-prerequisites-reference-checklist), item 2); otherwise one debug-enabled PRD service can force 100% retention of every trace it touches.

⚠️ **Do not leave debug enabled in production for long** — generates high volume and increases storage cost.

### The head sampler (0.3, this branch, task C10)

The helper always sets the SDK sampler (ADR-002), also when traces are off, so the SDK never reads `OTEL_TRACES_SAMPLER` itself. Precedence: `opts.Sampler` > `opts.SampleRatio` (`ParentBased(TraceIdRatioBased(ratio))`, 1.0 included) > `OTEL_TRACES_SAMPLER`/`_ARG` > the deprecated `OTEL_HELPER_SAMPLE_RATIO` (read only while `OTEL_TRACES_SAMPLER` is unset) > `parentbased_always_on`.

| Value | Result |
|---|---|
| unset | `parentbased_always_on`: roots sampled, children follow the parent's flag |
| `always_on`, `always_off`, `parentbased_always_off` | as named |
| `traceidratio`, `parentbased_traceidratio` | ratio from `OTEL_TRACES_SAMPLER_ARG` (plain decimal in [0, 1], else warned and 1.0) |
| `jaeger_remote`, `parentbased_jaeger_remote`, `xray` | `sampler is unsupported by this helper`, default |
| anything else (also a non-ASCII spelling) | `unknown sampler (valid: …)`, default |
| `OTEL_HELPER_SAMPLE_RATIO=0.1` | `parentbased_traceidratio` 0.1; `2` or `abc` → `sample ratio is not a number in [0, 1]`, default (0.2.x clamped and used a bare ratio sampler) |

The sampler variables are read from the process environment only: a value only in `appsettings.json` (`IConfiguration`) is not read and warns once (`… is set only in IConfiguration and is not read`). 0.2.1 already ignored such a value (it checked the process environment before leaving the sampler to the SDK), silently.

With `OTEL_TRACES_EXPORTER=none` the ParentBased sampler stays, as Go and Python keep an SDK provider there: a request that arrives with a sampled `traceparent` is passed on by `HttpClient` with the same trace id and the sampled flag, and a root request starts a sampled trace (measured by `Traces_Off_Server_Passes_The_Inbound_Trace_On`). An `AlwaysOffSampler` there would forward `00` and make every ParentBased service downstream drop the trace.

With traces **disabled** (`OTEL_HELPER_DISABLED_SIGNALS=traces`, `opts.DisabledSignals`) and under `OTEL_SDK_DISABLED=true`, propagation is a no-op tracer's, as in Go and Python: a root request sends no trace context downstream, and an inbound trace context is passed on unchanged — the same `traceparent` (trace id, parent span id and flags) and `tracestate` (measured by `Disabled_Traces_Propagate_As_A_No_Op_Tracer`). The sampler is then `ParentBased(root: AlwaysOff)`, so roots are not sampled. Under `OTEL_SDK_DISABLED=true` the helper adds a small non-recording activity listener so that ASP.NET Core creates a request activity for a request that continues a trace even without logging; roots get none.

### Propagators (0.3, this branch, task C10)

`OTEL_PROPAGATORS` is read per token: `tracecontext`, `baggage`, `none`. Other tokens warn once each and are skipped (`propagator not supported by the .NET helper, ignored` for `b3`, `b3multi`, `jaeger`, `xray`, `ottrace`; `unknown propagator, ignored` otherwise); each line names the effective list as `using=`. `none` disables propagation and wins over other tokens (warned when combined). The list is installed process-wide with `Sdk.SetDefaultTextMapPropagator`, also under `OTEL_SDK_DISABLED=true` (no span context then, baggage still propagates).

.NET carries context in three places, and all three follow the list in both directions: the OpenTelemetry propagator above; the runtime's `DistributedContextPropagator.Current`, with which `HttpClient` writes `traceparent`, `tracestate` and `baggage` on its own; and the `DistributedContextPropagator` service ASP.NET Core extracts inbound context with. The helper sets the first two at resolution and replaces the third in the container (`AddOtelHelper`; the web host adds the runtime default with `TryAddSingleton`, before or after the application's services — both orders are tested). Its propagator delegates to the runtime default and drops what is not listed:

| `OTEL_PROPAGATORS` | Inbound `traceparent` | Inbound `baggage` | Sent on by `HttpClient` |
|---|---|---|---|
| unset, `tracecontext,baggage` | continued | kept | `traceparent`, `tracestate`, `baggage` (as 0.2.x) |
| `tracecontext` | continued | ignored | `traceparent`, `tracestate` |
| `baggage` | ignored: a new trace | kept | `baggage` |
| `none` | ignored: a new trace | ignored | nothing |

Notes:

- `AddOtelHelper` already sets `DistributedContextPropagator.Current` (from `OTEL_PROPAGATORS`, which is read from the environment only), so an `HttpClient` created during configuration or before the host starts follows the list too; the traces-disabled and SDK-disabled settings are added when the host starts (the handler reads them per request). An `HttpClient` created before `AddOtelHelper` runs keeps the runtime default.
- A runtime propagator the application set (`DistributedContextPropagator.Current = …`), before or after `AddOtelHelper`, is kept for handlers created afterwards; one warning when the configuration needs another. A `DistributedContextPropagator` service it registered is kept too, with one warning that ASP.NET Core extracts inbound context with it, not as `OTEL_PROPAGATORS` says.
- Under `OTEL_SDK_DISABLED=true` with `tracecontext`, the helper's activity listener is process-wide: ASP.NET Core then creates a propagation-only (non-recorded) activity for every request that carries a `traceparent`, in every host of the process, until the container that started it is disposed.
- With traces or the SDK disabled, only an activity with a **remote** parent (`HasRemoteParent`, as ASP.NET Core and the instrumentations create) passes its inbound context on; an activity an application starts with `SetParentId(string)` or with a local context counts as started here and carries nothing. The SDK's expected "Failed to inject activity context … 'Invalid context'" line is not forwarded then.
- The settings are process-wide: the last container resolved wins, and nothing is restored when it is disposed.
- The HttpClient health list the runtime propagator checks (no trace context on a health request, §9) is process-wide too: the last container whose HttpClient instrumentation options were built wins (C11 fix round 2).
- A `SocketsHttpHandler` takes the runtime propagator when it is created: an `HttpClient` created before the host starts keeps the earlier one. `IHttpClientFactory` handlers are created later and follow the setting.
- Without a host (a bare `ServiceProvider`) the propagators are installed when the helper's settings are first resolved: when a provider is built, or the keyed `ActivitySource`/`Meter` (`OtelHelperKeys.Instrumentation`) is resolved. Under `OTEL_SDK_DISABLED=true` the no-op providers run no helper code, so resolve the keyed `ActivitySource` once, or use a host (it resolves the settings at start).

---

## 14. Opt-in Subpackages

AWS, Redis, SQL, and Profiling instrumentations are available as **separate NuGet packages** — not bundled in core.

> ⚠️ **Breaking change**: SQL and AWS were previously bundled in the core package and activated via `OTEL_HELPER_EXTRA_INSTRUMENTATION=SQL,AWS`. They are now separate packages. If you relied on that behavior, install the subpackages and call the corresponding extension methods explicitly.

### Installation

```bash
dotnet add package StaffOps.OtelHelper.AWS
dotnet add package StaffOps.OtelHelper.Redis
dotnet add package StaffOps.OtelHelper.Sql
dotnet add package StaffOps.OtelHelper.Profiling
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
| `StaffOps.OtelHelper.AWS` | `AddOtelHelperAws()` | AWS SDK calls (S3, SQS, DynamoDB, etc.) |
| `StaffOps.OtelHelper.Redis` | `AddOtelHelperRedis()` | StackExchange.Redis commands |
| `StaffOps.OtelHelper.Sql` | `AddOtelHelperSql()` | SqlClient queries |
| `StaffOps.OtelHelper.Profiling` | `AddOtelHelperProfiling()` | .NET CLR profiling via OTel |

### Profiling Prerequisites

`StaffOps.OtelHelper.Profiling` requires the following environment variables for the .NET CLR profiler:

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

The listener binds `http://+:<port>/metrics/` — every IPv4 address (the
managed `HttpListener` maps `+` to `IPAddress.Any`, as it did 0.2.x's `*`), so
a scraper in another pod or container reaches it — and answers each scrape
with a fresh collection (no response cache). There is no IPv6 bind: on an
IPv6-only cluster a scrape of the pod IP fails (known limitation, unchanged
from 0.2.x). The upstream exporter's own
`OTEL_EXPORTER_PROMETHEUS_HOST` / `OTEL_EXPORTER_PROMETHEUS_PORT` have no
effect; set the port with `OTEL_HELPER_METRICS_PORT`.
`testing/e2e/dotnet-prom-scrape.sh` checks the scrape from another container.

### Running OTLP push AND `/metrics` at the same time

`OTEL_METRICS_EXPORTER` selects which exporter(s) are active on the metrics pipeline, independent of the legacy endpoint-based fallback above:

| `OTEL_METRICS_EXPORTER` | Behavior |
|---|---|
| _(unset)_ | Legacy: OTLP if `OTEL_EXPORTER_OTLP_ENDPOINT` is set, else Prometheus fallback |
| `otlp` | OTLP only. Without an endpoint (0.3, this branch): one warning, `otlp` is dropped and the default applies (`prometheus`); in code (`MetricExporters = "otlp"`) host start fails |
| `prometheus` | `/metrics` only, even when an OTLP endpoint IS set |
| `console` | The OpenTelemetry console exporter on standard output, at the export interval (0.3, this branch; `logging` is a warned alias in the variable, an unknown exporter in code) |
| `otlp,prometheus` | **Both** — one `MeterProvider`, two readers. Same instrument, same values, in both outputs |
| `none` | Metrics disabled entirely (equivalent to `OTEL_HELPER_DISABLED_SIGNALS=metrics`; a set `OTEL_METRICS_EXPORTER` wins over that deprecated variable) |

The equivalent programmatic option — `TelemetryOptions.MetricExporters` (comma-separated string) — wins over the env var:

```csharp
builder.Services.AddOtelHelper(opts =>
{
    opts.MetricExporters = "otlp,prometheus";
});
```

`OTEL_METRIC_EXPORT_INTERVAL` (milliseconds) controls the OTLP push interval; the library default is 30000ms, overriding the SDK's own 60s default. `TelemetryOptions.ExportIntervalMs` wins over the env var.

0.3 (this branch, task C9): the variable must be digits with an optional `+` (any length, leading zeros allowed) and above 0; `5_000`, `-1`, `0`, `5e3` or non-ASCII digits warn and 30 000 is used, and a value above 2 147 483 647 is clamped with a warning. It is read from the process environment only, like every row the helper resolves (ADR-013): a value set only in `appsettings.json` has no effect, and the helper warns once naming the variable. The console reader uses the same interval.

### Choosing exporters per signal

0.3 (this branch, task C9). `OTEL_TRACES_EXPORTER` and `OTEL_LOGS_EXPORTER` take `otlp` (the default; exports only when an endpoint is configured), `console` or `none`, comma-separated:

```bash
# Spans on standard output instead of OTLP, logs not exported, metrics over OTLP:
OTEL_EXPORTER_OTLP_ENDPOINT=http://otel-collector:4317
OTEL_TRACES_EXPORTER=console
OTEL_LOGS_EXPORTER=none
```

- `console` is the OpenTelemetry console exporter (package `OpenTelemetry.Exporter.Console`): a multi-line, human-readable dump per span, log record or metric on `Console.Out`, meant for local debugging, not for a log pipeline. Each language prints its SDK's native format (JSON in Go, one object per line, and indented JSON in Python; `console.dir` objects in Node.js), so console output is not comparable across languages.
- `none` turns the signal off: no exporter, and for traces no `ActivitySource` of the helper is listened to. With `none` and other entries (`none,otlp`) the helper warns and `none` wins.
- `logging` is a deprecated alias of `console` in the variables (one warning); unknown entries (`zipkin`, `jaeger`) warn and are dropped; a variable with only unknown entries is treated as unset.
- The deprecated `OTEL_HELPER_DISABLED_SIGNALS` disables a signal only when that signal's `OTEL_<SIGNAL>_EXPORTER` is unset (or holds only unknown entries); for metrics a code `MetricExporters` wins too. `TelemetryOptions.DisabledSignals` (code) wins over all of them, and an unknown signal there fails start. Set in code means not null: `DisabledSignals = ""` disables nothing and still beats the variable, and `MetricExporters = ""` means no metric exporter (as Go, Python and Node.js treat an empty list; .NET also writes one INFO line, `TelemetryOptions.MetricExporters is empty: no metric exporter`, because a blank `appsettings.json` placeholder counted as unset before 0.3).
- `OTEL_SDK_DISABLED=true` overrides everything: no exporter at all.

### ASP.NET Core: mounting `/metrics` on your own pipeline

The standalone listener (`AddPrometheusHttpListener`, port 9464 by default) binds its own `HttpListener` — fine for workers/CLIs with no HTTP server of their own, but the **wrong** choice for a web app running behind Kestrel: it's a second port to expose, and it breaks under multiple workers/replicas sharing a pod (each process tries to bind the same port).

For ASP.NET Core apps, add `OpenTelemetry.Exporter.Prometheus.AspNetCore` to your own project (core does not take this dependency) and map the scrape endpoint on the app's existing pipeline:

```bash
dotnet add package OpenTelemetry.Exporter.Prometheus.AspNetCore
```

```csharp
using OpenTelemetry.Metrics;
using StaffOps.OtelHelper;

var builder = WebApplication.CreateBuilder(args);

builder.Services.AddOtelHelper(opts =>
{
    opts.MetricExporters = "prometheus"; // or "otlp,prometheus" for dual mode
    opts.PrometheusMetricsPort = 0;       // disable the standalone HttpListener
});
// The scraping endpoint needs its own exporter on the same MeterProvider.
builder.Services.AddOpenTelemetry().WithMetrics(b => b.AddPrometheusExporter());

var app = builder.Build();
app.MapPrometheusScrapingEndpoint(); // serves /metrics on the app's own Kestrel pipeline
app.Run();
```

`PrometheusMetricsPort = 0` is required here — otherwise the core library still binds its own listener on 9464 alongside the ASP.NET Core endpoint, which is redundant (and a second port to manage). Use the standalone listener (default `PrometheusMetricsPort = 9464`, no code change needed) only for headless processes without their own HTTP pipeline.

### Exponential histograms on OTLP

Set `OTEL_EXPORTER_OTLP_METRICS_DEFAULT_HISTOGRAM_AGGREGATION=base2_exponential_bucket_histogram`
in the **process environment** (or in an `IConfiguration` instance you register
before calling `AddOtelHelper`): the OTLP metrics then carry exponential
histograms. A value only in `appsettings.json` (or another host configuration
source) is warned, not applied: the host registers its configuration as a
factory, which `AddOtelHelper` cannot read. `explicit_bucket_histogram` is the
default; any other value (also a padded one) is warned and ignored.

**Do not combine `UseOtlpExporter()` with `AddOtelHelper()`.** Both export, so
every signal is sent twice; the helper warns once. With the histogram variable
set as well, a `UseOtlpExporter()` called after `AddOtelHelper()` makes the SDK
fail the `MeterProvider` build (the helper writes one ERROR line first).

### Scoping down what gets scraped

`OTEL_HELPER_DISABLED_METRICS` drops instruments from **every** active exporter — there's no supported way to push everything via OTLP while exposing only a subset on `/metrics` from the same `MeterProvider` (the OTel SDK's Views are per-provider, not per-reader). If you need a narrower `/metrics` output while keeping the full set on OTLP, filter on the scrape side instead — e.g. Prometheus `metric_relabel_configs` or a vmagent `drop` relabel rule matching the instrument name.

---

## 16. Metrics Export Interval

Metrics are exported every **30 seconds** (not the SDK default of 60s). This applies to both OTLP export and the Prometheus `/metrics` fallback.

0.3 (this branch, task C9): `OTEL_METRIC_EXPORT_INTERVAL` or `TelemetryOptions.ExportIntervalMs` changes it for the OTLP and console readers; the Prometheus endpoint is pull-based and collects on each scrape. Grammar and clamping: §15 ("Running OTLP push AND `/metrics` at the same time").

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
# Plaintext for a local collector without TLS: use http://, or a schemeless endpoint + INSECURE
OTEL_EXPORTER_OTLP_ENDPOINT=otel-collector:4317
OTEL_EXPORTER_OTLP_INSECURE=true                                # → http://otel-collector:4317
```

`OTEL_EXPORTER_OTLP_INSECURE` is consulted **only for schemeless endpoints** (`host:4317`): `true` → `http://`, otherwise `https://`. An explicit scheme — in env or in `TelemetryOptions.OtelCollectorEndpoint` — always wins, so `https://…` plus `INSECURE=true` still uses TLS.

0.3 (this branch, task C7a): the order is `TelemetryOptions.Insecure` >
`OTEL_EXPORTER_OTLP_<SIGNAL>_INSECURE` > `OTEL_EXPORTER_OTLP_INSECURE`. The
value is `true` or `false` (any ASCII case); anything else counts as `false`
and is warned once, even with no endpoint configured. A contradiction with an
explicit scheme is warned and the scheme wins:

```text
otelhelper: WARN INSECURE setting contradicts the endpoint scheme; the scheme wins variable=OTEL_EXPORTER_OTLP_INSECURE value=true using=https://
```

The same contradiction between `TelemetryOptions.Insecure` and a code URL
fails host start (`TelemetryOptions.Insecure = true contradicts the https://
scheme of TelemetryOptions.OtelCollectorEndpoint; …`).

### TLS files (0.3, this branch, task C8)

`OTEL_EXPORTER_OTLP_CERTIFICATE` (CA), `OTEL_EXPORTER_OTLP_CLIENT_KEY` and
`OTEL_EXPORTER_OTLP_CLIENT_CERTIFICATE` (the client pair, PEM; the key
unencrypted) are read by the helper, each per signal first
(`OTEL_EXPORTER_OTLP_TRACES_CERTIFICATE`, …), then generic. On both protocols
the files go to the helper's own HTTP client:

| Situation | What happens |
|---|---|
| a file cannot be read | `otelhelper: WARN a TLS file cannot be read and is ignored variable=OTEL_EXPORTER_OTLP_TRACES_CERTIFICATE using="the generic variable"`, then the next level (for the CA, finally the system roots) |
| a CA file with no PEM certificate (`/dev/null`) | `… holds no PEM certificate and is ignored …`, the next level |
| only the key or only the certificate resolves | warned naming the variable(s); server authentication with the resolved CA |
| the pair does not load (mismatch, bad PEM, encrypted key) | warned naming both variables; server authentication |
| the endpoint is plaintext (`http://`, or schemeless + `INSECURE=true`) | the files are not read; one warning names them and every signal they are ignored for: `TLS files are set but the endpoint is plaintext …; they are ignored for these signals … variable=OTEL_EXPORTER_OTLP_CERTIFICATE signals=traces,metrics,logs` |
| a file larger than 1 MiB, or a device (`/dev/zero`) | refused as "cannot be read" (the read is bounded) |
| a named pipe (FIFO) with no writer | the read waits for a writer: host start blocks. Point the variables at regular files (an operator error, not detected) |

Warnings never print a path or file content. Setup never fails on a TLS file.

The client key: on Linux it stays an ephemeral key, never written anywhere. On
Windows, SChannel cannot use an ephemeral key for client authentication, so
the key is imported into the user key store for the life of the certificate
(removed when the exporter is disposed); the process needs a user profile.

### Endpoint values the helper rejects or rewrites (0.3, this branch)

| Value | What happens |
|---|---|
| `http://user:pw@collector:4317/?token=x` | userinfo, query and fragment removed, one warning (`endpoint userinfo/query is not supported and was removed; put credentials in OTEL_EXPORTER_OTLP_HEADERS`) |
| `http://collector` with gRPC | dials `http://collector:4317`, warning `gRPC endpoint has no port; using 4317 - set an explicit port` |
| `collector:4318` with `http/protobuf` | `https://collector:4318/v1/<signal>` and a deprecation warning: use a URL |
| `http://bücher.de:4317` | dials `http://xn--bcher-kva.de:4317` (punycode) |
| `http://collector:4317/otlp` with gRPC | dials `http://collector:4317`; `otelhelper: WARN endpoint path is ignored by the gRPC exporter variable=OTEL_EXPORTER_OTLP_ENDPOINT`, once per variable, never the path (task C7b; a bare `/` is not warned) |
| `http://collector:04318` | port 4318, so `http/protobuf` is inferred (INFO) |
| `http://user:4318/secret@collector` | used as parsed; `endpoint path contains '@' - misplaced credentials?` naming the variable only |
| `http://coll ector:4317`, `http://[fe80::1%eth0]:4317`, `http://127.1:4317`, `http://h:0`, `https://h/x\y`, `http://h:4318/%zz` (`http/protobuf`) | invalid. Environment: `otelhelper: WARN invalid endpoint; ignored variable=OTEL_EXPORTER_OTLP_TRACES_ENDPOINT value="<unparseable URL>" using="next configured endpoint, else none"`, then the generic endpoint is used. Code: host start fails naming the option |
| `unix:/var/run/otel.sock`, `ftp://collector` | unsupported scheme: warned (`value=unix:`), no OTLP exporter for that signal |

**Globalization-invariant images.** IDN hosts need ICU. In an image without it
(`DOTNET_SYSTEM_GLOBALIZATION_INVARIANT=1`, common in chiseled and alpine
images) a non-ASCII host is invalid: an environment value warns
`an internationalized host needs ICU …` and the next endpoint is used, a code
value fails host start. Install ICU (`libicu`, or `icu-libs` on alpine, and
`DOTNET_SYSTEM_GLOBALIZATION_INVARIANT=0`), use an image with ICU, or write
the host in punycode (`xn--bcher-kva.de`), which needs no conversion.

**Paths are normalized by `System.Uri`.** The exporter takes a `Uri`, which
rewrites an http(s) path: dot segments are resolved (`/a/../v1` → `/v1`,
`/a/./b` → `/a/b`) and escapes of unreserved characters are decoded (`%7E` →
`~`, `%41` → `A`); `%2F` and `%20` stay. Go sends the path as written. Write
the path in its normal form.

---

## 18. OTLP Protocol (gRPC vs HTTP/protobuf)

By default, the SDK exports over **gRPC**. To use OTLP/HTTP instead (e.g. behind an ingress that doesn't proxy gRPC), set the standard env var:

```bash
OTEL_EXPORTER_OTLP_PROTOCOL=http/protobuf
```

Or in code:

```csharp
services.AddOtelHelper(opts =>
{
    opts.OtlpProtocol = "http/protobuf";
});
```

If left unset, the protocol is inferred from the endpoint port: `4318` resolves to `http/protobuf`, any other port (including the default `4317`) resolves to `grpc`.

Priority: code config (`TelemetryOptions.OtlpProtocol`) > `OTEL_EXPORTER_OTLP_PROTOCOL` env var > port-based inference (`4318` → `http/protobuf`) > `grpc` default.

0.3 (this branch, task C7a): `OTEL_EXPORTER_OTLP_<SIGNAL>_PROTOCOL` sits
between the code option and `OTEL_EXPORTER_OTLP_PROTOCOL`, and the inference
uses the endpoint each signal resolves to (a per-signal endpoint on port 4318
makes only that signal `http/protobuf`). The port is compared as a number
(`04318` is 4318) and the inference is logged once:
`otelhelper: INFO endpoint port 4318 and no protocol set; using http/protobuf set=OTEL_EXPORTER_OTLP_PROTOCOL`.

`http/json` is a valid value per the OTel spec but has no exporter implementation for traces/metrics/logs in this library: in `OTEL_EXPORTER_OTLP_PROTOCOL` it is warned on standard error (`otelhelper: WARN invalid OTEL_EXPORTER_OTLP_PROTOCOL …`) and the default applies; set in `TelemetryOptions.OtlpProtocol` it fails host start, naming the option.

`/v1/traces`, `/v1/metrics`, `/v1/logs` are appended to the endpoint automatically when `http/protobuf` is selected — do not include them in `OTEL_EXPORTER_OTLP_ENDPOINT`. A per-signal endpoint (`OTEL_EXPORTER_OTLP_TRACES_ENDPOINT`, `TelemetryOptions.TracesEndpoint`, …) is used as written. 0.3 (this branch) keeps the path of the generic endpoint (`https://gw.example.com/otlp` → `https://gw.example.com/otlp/v1/traces`); 0.2.x dropped it.

For local development with a plaintext collector, set `OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4317` (there is no default endpoint: unset means no OTLP export).

---

## 19. OTLP Headers, Timeout, Compression and Redirects (0.3, this branch)

Task C8: the helper resolves these per signal — code, then
`OTEL_EXPORTER_OTLP_<SIGNAL>_*`, then `OTEL_EXPORTER_OTLP_*`, then the
default — and gives each exporter its own HTTP client.

```bash
OTEL_EXPORTER_OTLP_ENDPOINT=https://otel-gateway.example.com:4317
OTEL_EXPORTER_OTLP_HEADERS="authorization=Bearer%20<token>,x-tenant=checkout"
OTEL_EXPORTER_OTLP_LOGS_HEADERS="authorization=Bearer%20<logs-token>"   # logs: replaces the generic headers
OTEL_EXPORTER_OTLP_TIMEOUT=5000                                       # ms
OTEL_EXPORTER_OTLP_TRACES_COMPRESSION=none                            # default gzip
```

```csharp
services.AddOtelHelper(opts =>
{
    opts.ExportTimeoutMs = 5_000;   // every signal; in [1, 86 400 000], else start fails
    opts.Compression = "gzip";      // every signal
});
```

**Headers.** `key=value` items separated by `,`. A key is letters, digits,
`.`, `_` and `-` (it is not percent-decoded); a value is percent-decoded
(`%20` is a space, `%2C` a comma) and trimmed. An item without `=` or with
another key is skipped; a value with an invalid escape (`a%zz`), or whose
bytes are not UTF-8 (`%FF`), is sent as written; a value that decodes to
anything outside printable ASCII (`%C3%A9`, `a%0D%0Ab`) is skipped. Each case
warns once naming the variable and the item position (`item="item #2"`), never
the value. For a key that appears twice the last one wins. On gRPC a key
ending in `-bin` is binary metadata: its value is sent base64. A
per-signal variable with at least one valid item replaces the generic one for
that signal.

**Timeout.** Decimal milliseconds (`500`, `250.5`; a fraction is rounded to
whole milliseconds, where Go keeps it); `0`, `+5`, `1e3`, `.5`,
`5.` are invalid: warned, and the next level applies (per-signal → generic →
10 000). Values above 24 h are clamped to 86 400 000, below 1 ms to 1, with a
warning. The value is the signal's export deadline: the exporter, the HTTP
client (`HttpClient.Timeout`), the batch span or log processor and the
periodic metric reader all use it, and gRPC exports carry it as
`grpc-timeout`. `OTEL_BSP_EXPORT_TIMEOUT`, `OTEL_BLRP_EXPORT_TIMEOUT` and
`OTEL_METRIC_EXPORT_TIMEOUT` are not honored (one warning each), and an
application `Configure<BatchExportActivityProcessorOptions>`,
`Configure<LogRecordExportProcessorOptions>` or `Configure<MetricReaderOptions>`
export timeout does not apply to the helper's exporters: set while its variable
is unset, each warns once naming the option. With
`OTEL_EXPORTER_OTLP_METRICS_DEFAULT_HISTOGRAM_AGGREGATION`, a timeout set on the named
options `"StaffOps.OtelHelper.OtlpMetrics"` after `AddOtelHelper` is replaced and warned;
one registered before `AddOtelHelper` is overwritten by the helper's copy without a warning. `grpc-timeout` carries the
configured deadline of each request, not the time left on an SDK retry.

**Compression.** `gzip` (default) or `none`, any ASCII case. Another value
(`deflate`, `zstd`) warns and the next level applies.

**Batch size.** `OTEL_BSP_MAX_EXPORT_BATCH_SIZE` above `OTEL_BSP_MAX_QUEUE_SIZE`
(likewise `OTEL_BLRP_*`; defaults 512 / 2048) warns once naming both and the
queue size is used.

**Redirects.** The helper's client never follows a redirect. An endpoint
that answers `3xx` loses that export, and the helper writes one ERROR per
signal and status code, without the `Location` value:

```text
otelhelper: ERROR the OTLP endpoint for traces answered HTTP 307 (a redirect); redirects are not followed, so these traces are not exported: configure the final endpoint URL signal=traces status=307
```

Configure the URL the redirect points to. (0.2.x followed redirects.)

**Response size.** Responses are buffered up to the SDK's
`MaxResponseSizeBytes`, as the SDK's own client does.

**Not covered: a value-length limit of `0`.**
`OTEL_ATTRIBUTE_VALUE_LENGTH_LIMIT=0` (or the `SPAN_`/`LOGRECORD_` variant)
is invalid by the contract, and the helper warns, but the .NET OTLP exporter
reads it itself and sends those attribute values empty; there is no public
API to override it. Unset it or use a positive limit. (Hiding the variable
while the exporter is built, as the Python helper does, would need
`Environment.SetEnvironmentVariable`, which the .NET helper never calls.)

