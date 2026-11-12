# HOW-TO: Using StaffOps.OtelHelper

Practical guide for developers. One line configures everything — traces, metrics, and correlated logs.

> **Version note.** This guide describes the `0.3` contract as the .NET helper
> implements it on this branch (Phase C of
> [`specs/dotnet-otel-helper/`](https://github.com/StaffOps/otel-libs/blob/main/specs/dotnet-otel-helper/tasks.md),
> tasks C4–C17), first published as the release candidate `0.3.0-rc.1` (tag
> `dotnet/v0.3.0-rc.1`) after the final validation, task C19. Notes marked
> "0.3 (this branch, task C<n>)" say what changed from the released `0.2.x`;
> "0.2.x:" gives the released behavior. The whole list of changes, in two parts
> (API; telemetry), is the .NET migration guide in
> [`CHANGELOG.md`](https://github.com/StaffOps/otel-libs/blob/main/CHANGELOG.md).
> See the [root README](https://github.com/StaffOps/otel-libs/blob/main/README.md) for the cross-language matrix.

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

> No need to configure endpoints, sampling, or exporters. The lib resolves everything via environment variables injected by infrastructure (`OTEL_SERVICE_NAME`, `OTEL_RESOURCE_ATTRIBUTES`, `OTEL_EXPORTER_OTLP_ENDPOINT`; the 0.2 names `SERVICE_NAME` and `ENVIRONMENT` still work and warn once, §10).

### Where the helper reads its settings: environment, `appsettings.json`, code (0.3, this branch, ADR-013)

The helper resolves every contract row itself and builds its own OTLP exporters
(ADR-013), so the SDK's own reading of `IConfiguration` no longer applies to them.

| Setting | Read from | A value only in `appsettings.json` (or another `IConfiguration` source) |
|---|---|---|
| Identity and resource: `OTEL_SERVICE_NAME`, `OTEL_RESOURCE_ATTRIBUTES`, `SERVICE_NAME`, `ENVIRONMENT` | process environment | no effect (the SDK detectors that read `IConfiguration` are replaced by the helper's resource) |
| OTLP export: `OTEL_EXPORTER_OTLP[_<SIGNAL>]_ENDPOINT`, `_PROTOCOL`, `_INSECURE`, `_HEADERS`, `_TIMEOUT`, `_COMPRESSION`, `_CERTIFICATE`, `_CLIENT_KEY`, `_CLIENT_CERTIFICATE` | process environment | no effect, **no warning** |
| `OTEL_TRACES_SAMPLER`, `OTEL_TRACES_SAMPLER_ARG`, `OTEL_PROPAGATORS`, `OTEL_METRIC_EXPORT_INTERVAL` | process environment | no effect, one warning (below) |
| `OTEL_{TRACES,METRICS,LOGS}_EXPORTER`, `OTEL_METRICS_EXEMPLAR_FILTER`, `OTEL_HELPER_*`, `OTEL_EXPORTER_PROMETHEUS_*` | process environment | no effect |
| `OTEL_EXPORTER_OTLP_METRICS_DEFAULT_HISTOGRAM_AGGREGATION` | process environment, or an `IConfiguration` **instance** registered before `AddOtelHelper()` | a host's `appsettings.json` value: one warning at provider build, not applied (§15) |
| SDK-owned rows: `OTEL_SDK_DISABLED`, `OTEL_EXPORTER_OTLP_METRICS_TEMPORALITY_PREFERENCE` | `IConfiguration` (which, in a host, includes the environment) — the helper reads the same key the SDK reads | applied |
| SDK-owned rows: attribute limits (`OTEL_ATTRIBUTE_VALUE_LENGTH_LIMIT`, `OTEL_SPAN_*`, `OTEL_LOGRECORD_*`), batch sizes and queues (`OTEL_BSP_*`, `OTEL_BLRP_*`) | read by the SDK (environment or `IConfiguration`) | applied by the SDK |
| `OTEL_DOTNET_EXPERIMENTAL_ASPNETCORE_ENABLE_GRPC_INSTRUMENTATION` (upstream key, §3) | application configuration (environment or `appsettings.json`) | applied |

The warning for the four rows that say so (example for the sampler; the
propagator line ends `set it in the process environment`, the interval line
`… or TelemetryOptions.ExportIntervalMs, using 30000`):

```text
otelhelper: WARN OTEL_TRACES_SAMPLER is set only in IConfiguration and is not read; set it in the process environment or TelemetryOptions.Sampler variable=OTEL_TRACES_SAMPLER using=parentbased_always_on
```

**Moving from 0.2.x:** the 0.2 helper let the SDK read `OTEL_*` keys from
`appsettings.json` (for example `OTEL_EXPORTER_OTLP_HEADERS` or
`OTEL_SERVICE_NAME`). Move those keys to the process environment (Helm values,
container env), or set the matching `TelemetryOptions` property.

### Options bound from configuration count as code

`TelemetryOptions` values bound from a configuration section are **code
values**: they win over the environment, and an invalid one fails host start
(the environment only warns, §10):

```csharp
// appsettings.json: { "Telemetry": { "ServiceName": "checkout-api", "HttpOptInMetrics": true } }
builder.Services.Configure<TelemetryOptions>(builder.Configuration.GetSection("Telemetry"));
builder.Services.AddOtelHelper();
```

A `Configure<TelemetryOptions>` registered after `AddOtelHelper()` is honored
too (values are read when the providers are built). Leave a property unset
(`null`) to let the environment decide.

### Keyed `ActivitySource` and `Meter`

The helper registers its `ActivitySource` and `Meter` as **keyed** services,
key `OtelHelperKeys.Instrumentation` (§5, §6). An unkeyed parameter still
compiles but gets something else: in a web host an unkeyed `ActivitySource` is
ASP.NET Core's own source (your spans are exported under the `Microsoft.AspNetCore`
scope) and an unkeyed `Meter` is null; in a worker both are null. Always inject
with `[FromKeyedServices(OtelHelperKeys.Instrumentation)]`.

### Your logging providers (`ClearProviders`)

The helper adds its own logging providers (OTLP; the stdout JSON line when no
logs exporter is active) and never removes yours. Call
`builder.Logging.ClearProviders()` **before** `AddOtelHelper()` to drop the
host's console provider (after it, it removes the helper's providers too) — see
§4 "Stdout fallback".

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

At application start, when gRPC endpoints are mapped, traces are exported and
the key is not `true`, the helper writes one line on standard error:

```text
otelhelper: WARN gRPC endpoints are mapped but OTEL_DOTNET_EXPERIMENTAL_ASPNETCORE_ENABLE_GRPC_INSTRUMENTATION is not true in the application configuration: gRPC server spans are HTTP-shaped (POST /pkg.Svc/Method, status from the HTTP status 200), without the RPC conventions; set it to true to trace them as RPC spans variable=OTEL_DOTNET_EXPERIMENTAL_ASPNETCORE_ENABLE_GRPC_INSTRUMENTATION
```

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

0.3 (this branch, task C13, N9): the level applies to the helper's two providers
only — the OpenTelemetry provider (alias `OpenTelemetry`) and the stdout provider
(alias `OtelHelperStdout`). The application's own providers, its rules and
`LoggerFilterOptions.MinLevel` are not changed (0.2.x set `MinLevel` for every
provider). An explicit level of the application wins over the environment default
above (owner decision 2026-11-11, as in Python and 0.2.x). Precedence, highest first:

| # | Source | Example |
|---|---|---|
| 1 | `TelemetryOptions.MinimumLogLevel` in code | `o.MinimumLogLevel = LogLevel.Debug` |
| 2 | a rule of yours for the helper's provider, no category | `Logging:OpenTelemetry:LogLevel:Default`, `Logging:OtelHelperStdout:LogLevel:Default` |
| 3 | your default for every provider | `Logging:LogLevel:Default`, `AddFilter((string?)null, level)`, `SetMinimumLevel(level)` |
| 4 | the environment default (table above) | `ENVIRONMENT=PRD` → Warning |

`SetMinimumLevel` counts as your choice wherever it is called, also from a library
that configures logging for you (including `SetMinimumLevel(LogLevel.Information)`).
So the ASP.NET Core template (`appsettings.json` with `"Default": "Information"`)
exports Information in PRD, as on 0.2.x; remove that line, or set
`MinimumLogLevel`, to get the PRD Warning default. Category rules work as for any
provider:

| Your configuration | Effect on the helper's providers |
|---|---|
| `Logging:LogLevel:<category>` (or `AddFilter("<category>", …)`), before or after `AddOtelHelper()` | applies to that category, as for every provider |
| `Logging:LogLevel:Microsoft` or `:System.Net.Http` exactly | replaces the helper's Error rule for that category (that rule applies only while the resulting default level is above Debug) |
| `Logging:OpenTelemetry:LogLevel:<category>`, `Logging:OtelHelperStdout:LogLevel:<category>` | applies; the helper adds no rule for that category. Provider names and aliases are case-sensitive (the framework compares them ordinally: `Logging:opentelemetry:…` is ignored) |

The helper's rules are visible in `LoggerFilterOptions.Rules` (`IOptionsMonitor<LoggerFilterOptions>.CurrentValue`),
with the provider's full type name (`OpenTelemetry.Logs.OpenTelemetryLoggerProvider`,
`StaffOps.OtelHelper.Logging.JsonLineLoggerProvider`). They are computed in a
`PostConfigure<LoggerFilterOptions>` step: rules your own `PostConfigure` registered
after `AddOtelHelper()` adds are not mirrored onto the helper's providers.

### Stdout fallback (no logs exporter)

With no logs exporter active — no OTLP endpoint, `OTEL_LOGS_EXPORTER=none`,
`OTEL_HELPER_DISABLED_SIGNALS=logs`, `OTEL_SDK_DISABLED=true`, or an endpoint the
helper cannot use — the helper writes each record as one JSON object on one line
of standard output, the same line Go and Python write (contract D6, A23):

```json
{"time":"2026-11-11T12:00:00.000Z","level":"WARN","msg":"Order 42 delayed","logger":"Orders.Api","OrderId":42}
```

| Field | Value |
|---|---|
| `time` | UTC, milliseconds, `Z`, whatever the process time zone |
| `level` | `DEBUG` (Trace, Debug), `INFO`, `WARN`, `ERROR` (Error **and** Critical: the four words Go, Python and Node.js write; Python maps CRITICAL to ERROR, Go and Node.js have no fifth level) |
| `msg` | the formatted message |
| `logger` | the category (`ILogger<T>`: the full type name) |
| `trace_id`, `span_id` | right after `logger`, only for a record written while a span is current (32 / 16 lower-case hex digits); a state value with either name is then dropped (contract A23 amendment of 2026-11-11; Go, Python and Node.js add them with task A36) |
| the state values | after `logger`, in template order; numbers and booleans as JSON, non-finite numbers and other values as text, a collection as an array of at most 1000 items (then `"…"`); `{OriginalFormat}` left out; the first of duplicate names kept; a value named `time`, `level`, `msg` or `logger` is dropped |
| `exception` | last, the exception's `ToString()` (one line: the line breaks are escaped) |

ILogger scopes are not written on this line. It is written synchronously, one
`Console.Out.Write` per record. Text is UTF-8 as written (emoji included) when `Console.OutputEncoding` is UTF-8 (the
Linux and container default); with another console encoding, characters outside the
BMP stay `\uXXXX\uXXXX` escapes (still valid JSON) instead of becoming `?`; a lone
surrogate becomes U+FFFD. A value whose `ToString()` recurses without end cannot be
guarded (a stack overflow ends the process in .NET). `AddJsonConsole()` is no
substitute: it writes `Timestamp` (`+00:00`), `LogLevel`, `Category` and `State`
(measured with Microsoft.Extensions.Logging.Console 8.0.1, task C13), not the
contract field names or level words. The provider writes nothing while a logs exporter
is active (no tee), and `OTEL_LOGS_EXPORTER=console` gives the OpenTelemetry
console exporter instead (§15), not this line.

The helper does not remove the host's console provider: an app that keeps it gets
each record twice on stdout, and the helper writes once at host start, on
standard error:

```text
otelhelper: WARN no logs exporter is active, so each record is written to standard output twice: as the helper's JSON line and by the console logging provider; call builder.Logging.ClearProviders() before AddOtelHelper(), or remove only the console provider, to keep one
```

To keep only the JSON line:

```csharp
var builder = WebApplication.CreateBuilder(args);
builder.Logging.ClearProviders();   // before AddOtelHelper(): it would remove the helper's providers too
builder.Services.AddOtelHelper();
```

### Structured Logs

Use `{Name}` placeholders instead of interpolation. This enables search and filtering:

```csharp
// ✅ Correct — structured, filterable
logger.LogInformation("Order {OrderId} from customer {CustomerId} processed in {Duration}ms", orderId, customerId, elapsed);

// ❌ Wrong — interpolated string, not filterable
logger.LogInformation($"Order {orderId} from customer {customerId} processed in {elapsed}ms");
```

The OTLP record's body is the formatted message (`Order 42 from customer 7
processed in 12ms`); each placeholder is an attribute, and the SDK adds the
template as `{OriginalFormat}`. A fixed body with attributes is a state of key/value pairs and a formatter —
the pattern of `example/dotnet-custom-signals`; the pairs are attributes on
OTLP and fields on the stdout fallback, and no `{OriginalFormat}` is added:

```csharp
var state = new List<KeyValuePair<string, object?>> { new("order.id", "ord-1") };
logger.Log(LogLevel.Information, default, state, null, (_, _) => "order processed");
// body "order processed", attribute order.id
```

Logging scopes are exported as attributes too (the helper sets
`IncludeScopes`), on OTLP only: the stdout fallback writes no scopes.

Under the generic host (`Host.CreateApplicationBuilder`,
`WebApplication.CreateBuilder`) the logger factory adds an activity-tracking
scope (`SpanId`, `TraceId`, `ParentId`) to every record written inside an
activity. The helper does **not** export that scope (task C17 fix round 1,
design D-NET10 (g)): the record's own trace context already says it. The filter
goes by origin, not by key — your own scope with a `SpanId` key is exported, and
so are the opt-in `ActivityTrackingOptions.Tags`/`Baggage` scopes. Other logging
providers (console, third-party) still get the scope. If the OpenTelemetry
logger provider takes no scope provider (not the case with OpenTelemetry 1.20),
the helper cannot apply the filter and writes once:

```text
otelhelper: WARN the OpenTelemetry logger provider accepts no scope provider: the activity-tracking scope (SpanId, TraceId, ParentId) will be exported as log attributes
```

```text
before (C17, measured):  attributes order.id, {OriginalFormat}, SpanId, TraceId, ParentId
after:                   attributes order.id, {OriginalFormat}     (Trace ID / Span ID on the record itself)
```

In an ASP.NET Core request the hosting scope adds `ConnectionId`, `RequestId`
and `RequestPath` (the raw path, for example `/order/1`). The helper keeps them
(application data, as 0.2.x did); they are high cardinality and `RequestPath`
can carry identifiers, so drop them at the Collector if your log backend
indexes attributes (validated and run against `dotnet-api`, contrib 0.160.0):

```yaml
processors:
  transform/aspnetcore-request-scope:
    log_statements:
      - delete_key(log.attributes, "ConnectionId")
      - delete_key(log.attributes, "RequestId")
      - delete_key(log.attributes, "RequestPath")
service:
  pipelines:
    logs:
      receivers: [otlp]
      processors: [memory_limiter, transform/aspnetcore-request-scope, batch]   # your existing processors around it
      exporters: [otlphttp/loki]                                                  # your existing exporters
```

The stdout fallback writes no scopes at all.

Build no second `LoggerFactory` over the host's providers (for example
`new LoggerFactory(sp.GetServices<ILoggerProvider>())`): a scope-aware provider
keeps only the last scope provider it was given, so the host's own loggers'
scopes stop reaching it — application scopes disappear from the records. That
is upstream `Microsoft.Extensions.Logging` behavior, with or without the
helper; resolve `ILoggerFactory` from the container instead.

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
    // The type stays on the span; the message and stack trace go to a log record
    // correlated by trace_id / span_id (written inside the activity), never the span.
    activity?.SetStatus(ActivityStatusCode.Error);
    activity?.SetTag("error.type", ex.GetType().FullName);
    logger.LogError(ex, "critical-operation failed");
    throw;
}
```

> Keep exception detail out of spans (ADR-014): no `ex.Message` in the status
> description or in a tag. The `ILogger` record written while the activity is
> current carries its `trace_id` and `span_id` (OTLP, and the stdout fallback
> line), so Grafana links the trace to the detail. `activity.AddException(ex)`
> would copy the message and stack trace into a span event — avoid it for
> errors whose text can hold data.

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

> `StartRootActivity` ignores the current activity and creates a root span (new traceId). Use whenever you want an independent trace per iteration. 0.3 (this branch, task C10): while the root runs it is `Activity.Current`; when it is disposed, the activity that was current before the call is current again (0.2.x left `Activity.Current` null). When no activity is created (no listener, or the sampler drops it) the call returns `null` and leaves `Activity.Current` as it was. The restore uses a private runtime field; on a runtime without it the helper writes once on standard error and `Activity.Current` is null after the root stops:
> `otelhelper: WARN StartRootActivity cannot restore the previous Activity.Current on this runtime; it is null after the root stops`

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

The helper sets the exemplar filter itself, because the .NET SDK default is `AlwaysOff` while the OpenTelemetry specification's is `trace_based`. 0.3 (this branch, task C9): `OTEL_METRICS_EXEMPLAR_FILTER` chooses it — `trace_based` (default: only measurements taken under a sampled span), `always_on`, `always_off`; any other value warns and `trace_based` is used (`otelhelper: WARN invalid OTEL_METRICS_EXEMPLAR_FILTER: unsupported exemplar filter, using trace_based; valid values: always_on, always_off, trace_based variable=OTEL_METRICS_EXEMPLAR_FILTER value=sometimes`). To set it in code, register your own call after `AddOtelHelper()`, which wins (a call registered before `AddOtelHelper()` is overridden by the helper's, without a warning):

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

// The helper's keyed ActivitySource and Meter: named after the resolved service.name,
// so they are always exported (an unkeyed ActivitySource is ASP.NET Core's own here).
var activitySource = app.Services.GetRequiredKeyedService<ActivitySource>(OtelHelperKeys.Instrumentation);
var meter = app.Services.GetRequiredKeyedService<Meter>(OtelHelperKeys.Instrumentation);

var ordersTotal = meter.CreateCounter<long>("orders.completed", "{order}");
var requestDuration = meter.CreateHistogram<double>("checkout.duration", "s");

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
        // ADR-014: the type on the span, message and stack trace in the log record
        // below (written in the span's context: same trace_id/span_id).
        activity?.SetStatus(ActivityStatusCode.Error);
        activity?.SetTag("error.type", ex.GetType().FullName);
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

### Running the examples and the e2e smoke (task C17)

The example apps in [`example/`](example/README.md) — `dotnet-api` (HTTP) →
`dotnet-backend` (gRPC) ← `dotnet-process` (workers), and the short job
`dotnet-custom-signals` (custom spans, metrics, logs with the Go/Python/Node.js
names) — are built from `dotnet/` with their `Dockerfile`, which packs
`StaffOps.OtelHelper` in a first stage and restores the example against that
`.nupkg` from a local feed (`example/nuget.packages.config`), as a consumer
does:

```bash
docker build -f example/dotnet-custom-signals/Dockerfile -t dotnet-custom-signals .
```

How to run them against a Collector, and what each one emits:
[example/README.md](example/README.md#local-test-docker),
[example/EXAMPLES.md](example/EXAMPLES.md#custom-signals-dotnet-custom-signals).
From the repository, `dotnet run --project example/<app>` keeps the project
reference (no `OtelHelperVersion` set).

The end-to-end smoke exports through the packed helper to a real
`otel/opentelemetry-collector-contrib` and checks where each signal arrived:

```bash
SMOKE_LANGS=dotnet testing/e2e/smoke.sh          # .NET rows + the Prometheus scrape row
SMOKE_LANGS=dotnet SMOKE_DOTNET_SCRAPE=0 testing/e2e/smoke.sh   # without the scrape row
```

Success (recorded 2026-11-12, abridged):

```text
OK    dotnet grpc        traces  ->   default
OK    dotnet http-prefix logs    ->   prefix
OK    dotnet per-signal  traces  -/-> default   absent
OK    dotnet tls         metrics ->   tls
OK    dotnet all         logs-correlated ->   -
OK    dotnet prometheus scrape from another container (e2e/dotnet-prom-scrape.sh)
```

A signal that did not arrive is `MISS` (exit 1), one that arrived where it must
not is `LEAK`; for the scrape row the script prints the scrape script's last
lines (for example `dotnet-prom-scrape: FAIL — http://…:9464/metrics not
reachable from another container`) before `MISS`. Every run prints the C16
deprecation warning for `ENVIRONMENT`, which the smoke sets as the other
languages do. The combinations (gRPC; http/protobuf with an `/otlp` prefix; a
per-signal traces endpoint; TLS with a generated CA in
`OTEL_EXPORTER_OTLP_CERTIFICATE`) are in `testing/e2e/combos.sh`; details in
[TESTS.md](TESTS.md#e2e-smoke-task-c17).

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
server or the client (the service name is matched case-sensitively, as gRPC
method paths are). Health requests are still counted in
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

An invalid item fails host start; the ERROR line on standard error ends with
the validation message, for example
`… host start fails: TelemetryOptions.HealthPaths item #2 (health) must be ASCII, start with "/" and have no query, fragment or comma.`

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
body sizes):

```text
otelhelper: INFO HTTP opt-in metrics on: ASP.NET Core and System.Net.Http record the active requests and the client connection metrics, and have no body-size instruments
```
 0.2.x exported them always: dashboards on them need the switch.
An instrument of your own meter with one of these names is kept, and
`OTEL_HELPER_DISABLED_METRICS` still drops a switched-on one.

### Exceptions Recorded in Spans

When an exception occurs in an HTTP request (inbound ASP.NET Core, which also carries gRPC server calls, or outbound HttpClient), the lib records a **span event** (`RecordException`) with `exception.type`, `exception.message` and `exception.stacktrace`.

0.3 (this branch): database client spans (SqlClient, Npgsql, MySqlConnector, Redis) and AWS SDK spans (task C14c) keep only `exception.type`; the message and stack trace go to a correlated log record — see [Exceptions: traces vs logs](#exceptions-traces-vs-logs) (§14, ADR-014; AWS examples in [AWS](#aws-spans-failed-calls-and-url-redaction)).

**HTTP and gRPC spans are not covered by that rule yet** (ADR-014 decision 4: their review is contract task A37, all languages): their `exception` event still holds the message and the stack trace, so treat traces as possibly holding data. For your own code, follow the rule by hand — the type on the span, the detail in a log record correlated by `trace_id` / `span_id` (§5 "Mark Span with Error"). To drop the event text from the framework spans, set `RecordException = false` in a `Configure<AspNetCoreTraceInstrumentationOptions>` / `Configure<HttpClientTraceInstrumentationOptions>` registered after `AddOtelHelper()` (not measured by the helper's tests).

### Framework Logs Filtered

In non-debug environments (DEV, HML, PRD), logs from `Microsoft.*` and `System.Net.Http.*` are only exported if `Error` or `Critical`.
0.3 (this branch, task C13, N9): only on the helper's providers (OTLP and the stdout fallback); the application's own providers (the host console) show framework warnings again. A rule of yours for exactly `Microsoft` or `System.Net.Http`, or a longer category (`Microsoft.AspNetCore`), wins over the helper's rule (§4).

### Structured Logs (ParseStateValues)

When you use placeholders in logs, values are extracted as **separate attributes** in the exported log, enabling queries like: `{service_name="my-service"} | json | OrderId="12345"`

---

## 10. Environment Variables

| Variable | Effect | Default | Who injects |
|---|---|---|---|
| `OTEL_SERVICE_NAME` | Service name (`service.name`); wins over `OTEL_RESOURCE_ATTRIBUTES` and `SERVICE_NAME` | `unknown_service:<process name>` + warning | CI/CD |
| `SERVICE_NAME` | Deprecated alias of `OTEL_SERVICE_NAME`, read last — warned once when set ([below](#deprecated-variables-03-this-branch-task-c16)) | — | CI/CD |
| `OTEL_RESOURCE_ATTRIBUTES` | `key=value,…` resource attributes (helper-parsed: percent-decoded, ASCII-trimmed, bad items skipped and warned by position); `deployment.environment.name` here wins over `ENVIRONMENT` | — | Helm Chart |
| `ENVIRONMENT` | Deprecated alias of `deployment.environment.name` (controls log level; known names canonicalized, others as written) — warned once when set ([below](#deprecated-variables-03-this-branch-task-c16)) | none: attribute absent + warning | Helm Chart |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | Collector endpoint; unset → no OTLP export, Prometheus `/metrics` fallback | _(unset)_ | Helm Chart |
| `OTEL_HELPER_DEBUG_LEVEL` | Debug mode: Debug log level + `debug=true` attribute on entry spans (no parent, or a remote one); the tracestate is not touched (0.2.x prepended `debug=true` to it) | `false` | Manual |
| `OTEL_HELPER_EXTRA_INSTRUMENTATION` | No effect in core (use the subpackages, §14) — warned once when set ([below](#deprecated-variables-03-this-branch-task-c16)) | `SQL` | — |
| `OTEL_HELPER_SAMPLE_RATIO` | Deprecated: a plain decimal in [0, 1] → `parentbased_traceidratio`; anything else warns and is ignored (not clamped); read only while `OTEL_TRACES_SAMPLER` is unset (§13) — warned once when set ([below](#deprecated-variables-03-this-branch-task-c16)) | _(unset)_ | Helm Chart |
| `OTEL_HELPER_METRICS_PORT` | Standalone Prometheus `/metrics` listener port (`0` disables it) | `9464` | Helm Chart |
| `OTEL_HELPER_HTTP_OPT_IN_METRICS` | `true` keeps the HTTP opt-in metrics (§9); not `true`/`false` → warning, `false` | `false` | Manual |
| `OTEL_DOTNET_EXPERIMENTAL_ASPNETCORE_ENABLE_GRPC_INSTRUMENTATION` | Upstream key, read from the app configuration: `true` traces gRPC server calls as RPC spans (§3) | _(unset)_ | Manual (app) |
| `OTEL_METRICS_EXPORTER` | Metric exporter(s): `otlp`, `prometheus`, `console`, a list such as `otlp,prometheus`, or `none` (`logging` = warned alias of `console`) | legacy inference | Manual |
| `OTEL_TRACES_EXPORTER`, `OTEL_LOGS_EXPORTER` | `otlp`, `console`, `none` (§15 "Choosing exporters per signal") | `otlp` | Manual |
| `OTEL_METRIC_EXPORT_INTERVAL` | OTLP and console metric export interval (ms; digits with an optional `+`, clamped at 2 147 483 647) | `30000` | Manual |
| `OTEL_METRICS_EXEMPLAR_FILTER` | `always_on`, `always_off`, `trace_based` (§7) | `trace_based` | Manual |
| `OTEL_SDK_DISABLED` | `true` turns telemetry off (propagators kept, one INFO line); not `true`/`false` → warning, `false` | `false` | Manual |
| `OTEL_HELPER_DISABLED_SIGNALS` | Deprecated: signals to disable; a set `OTEL_<SIGNAL>_EXPORTER` wins — warned once when set ([below](#deprecated-variables-03-this-branch-task-c16)) | _(unset)_ | Manual |
| `OTEL_TRACES_SAMPLER`, `OTEL_TRACES_SAMPLER_ARG` | Standard OTel sampler (the six spec names) and its ratio, parsed by the helper; takes priority over `OTEL_HELPER_SAMPLE_RATIO`; unknown names warn (§13) | `parentbased_always_on` | Manual |
| `OTEL_PROPAGATORS` | `tracecontext`, `baggage`, `none`; other tokens warn and are skipped; kept under `OTEL_SDK_DISABLED` (§13) | `tracecontext,baggage` | Manual |
| `OTEL_EXPORTER_OTLP_PROTOCOL` | OTLP wire protocol: `grpc` or `http/protobuf` | port-based inference | Manual |
| `OTEL_EXPORTER_OTLP_HEADERS` | Headers on every export, `key=value,…` (auth); `OTEL_EXPORTER_OTLP_<SIGNAL>_HEADERS` replaces them for one signal (§19) | empty | Helm Chart (secret) |
| `OTEL_EXPORTER_OTLP_TIMEOUT` | Export deadline in milliseconds, per signal with `_<SIGNAL>_TIMEOUT` (§19) | `10000` | Manual |
| `OTEL_EXPORTER_OTLP_COMPRESSION` | `gzip` or `none`, per signal with `_<SIGNAL>_COMPRESSION` (§19) | `gzip` | Manual |
| `OTEL_EXPORTER_OTLP_CERTIFICATE`, `_CLIENT_KEY`, `_CLIENT_CERTIFICATE` | TLS files (CA, client pair), per signal too (§17) | system roots, no client certificate | Helm Chart |

### Deprecated variables (0.3, this branch, task C16)

A deprecated variable still works until 1.0, but each one that is set writes
one line on standard error at start — once per container, also when a
standard variable or a code option wins over it. The value is never printed.

**Success — nothing deprecated set:** no line. Empty or whitespace-only values
count as unset, so `SERVICE_NAME=` writes nothing either.

**Warning — a chart still sets the old names:**

```bash
SERVICE_NAME=orders-api
ENVIRONMENT=PRD
OTEL_HELPER_SAMPLE_RATIO=0.1
OTEL_HELPER_DISABLED_SIGNALS=logs
OTEL_HELPER_EXTRA_INSTRUMENTATION=SQL
```

```text
otelhelper: WARN SERVICE_NAME is deprecated and will be removed in 1.0; use OTEL_SERVICE_NAME variable=SERVICE_NAME replacement=OTEL_SERVICE_NAME
otelhelper: WARN ENVIRONMENT is deprecated and will be removed in 1.0; use OTEL_RESOURCE_ATTRIBUTES (deployment.environment.name=<value>) variable=ENVIRONMENT replacement="OTEL_RESOURCE_ATTRIBUTES (deployment.environment.name=<value>)"
otelhelper: WARN OTEL_HELPER_SAMPLE_RATIO is deprecated and will be removed in 1.0; use OTEL_TRACES_SAMPLER=parentbased_traceidratio with OTEL_TRACES_SAMPLER_ARG variable=OTEL_HELPER_SAMPLE_RATIO replacement="OTEL_TRACES_SAMPLER=parentbased_traceidratio with OTEL_TRACES_SAMPLER_ARG"
otelhelper: WARN OTEL_HELPER_DISABLED_SIGNALS is deprecated and will be removed in 1.0; use OTEL_TRACES_EXPORTER/OTEL_METRICS_EXPORTER/OTEL_LOGS_EXPORTER=none variable=OTEL_HELPER_DISABLED_SIGNALS replacement="OTEL_TRACES_EXPORTER/OTEL_METRICS_EXPORTER/OTEL_LOGS_EXPORTER=none"
otelhelper: WARN OTEL_HELPER_EXTRA_INSTRUMENTATION has no effect in .NET and is ignored; add the opt-in packages (StaffOps.OtelHelper.Sql, .AWS, .Redis) instead variable=OTEL_HELPER_EXTRA_INSTRUMENTATION
```

The values still apply (service `orders-api`, environment `PRD`, ratio 0.1,
logs off). **Migrated** — the same behavior, no line:

```bash
OTEL_SERVICE_NAME=orders-api
OTEL_RESOURCE_ATTRIBUTES=deployment.environment.name=PRD
OTEL_TRACES_SAMPLER=parentbased_traceidratio
OTEL_TRACES_SAMPLER_ARG=0.1
OTEL_LOGS_EXPORTER=none
# OTEL_HELPER_EXTRA_INSTRUMENTATION: remove it; add the package instead
#   dotnet add package StaffOps.OtelHelper.Sql   →   builder.Services.AddOtelHelperSql();
```

The warning is written also when the old variable no longer decides anything,
so leftovers show up before 1.0: `OTEL_SERVICE_NAME` next to `SERVICE_NAME`
still warns about `SERVICE_NAME`, and `OTEL_TRACES_EXPORTER=otlp` next to
`OTEL_HELPER_DISABLED_SIGNALS=traces` keeps traces on and still warns about
the deprecated variable. A value the variable's own parser rejects adds that
parser's line (for example `sample ratio is not a number in [0, 1]` for
`OTEL_HELPER_SAMPLE_RATIO=2`, §13). Go and Node.js write the same messages;
Go, Python and Node.js write them once per process, .NET once per container
(two hosts in one process each write them).

The fifth deprecation of the contract (D8) is not a variable: a **schemeless
endpoint with `http/protobuf`** (`OTEL_EXPORTER_OTLP_ENDPOINT=collector:4318`)
writes `otelhelper: WARN schemeless endpoint is deprecated for http/protobuf and
will be removed in 1.0; use a URL (http:// or https://)` followed by
`variable=`, `value=` and `using=` (task C7a; migration guide telemetry
item 16). Schemeless gRPC endpoints are legal and do not warn.

An unknown `OTEL_HELPER_DISABLED_SIGNALS` entry is echoed **lowercased**
(`Profiles` → `value=profiles`), as in Go and Node.js: the list is trimmed and
ASCII-lowercased before it is checked.

The obsolete code option and the unsupported declarative-configuration
variables warn the same way, once per container (the value or path is never
printed):

```text
otelhelper: WARN TelemetryOptions.ExtraInstrumentation has no effect in .NET; add the opt-in packages (StaffOps.OtelHelper.Sql, .AWS, .Redis) instead
otelhelper: WARN OTEL_CONFIG_FILE is not supported by the .NET helper and is ignored; configure it with environment variables or TelemetryOptions variable=OTEL_CONFIG_FILE
otelhelper: WARN OTEL_EXPERIMENTAL_CONFIG_FILE is not supported by the .NET helper and is ignored; configure it with environment variables or TelemetryOptions variable=OTEL_EXPERIMENTAL_CONFIG_FILE
```

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

Sampling is done by the OTel Collector (Agent). The lib records every root span and every span whose parent is sampled (`parentbased_always_on`, below) and sends them to the Collector, which decides what to keep; a child of an unsampled caller is not recorded. Change the head sampler (`opts.Sampler`, `OTEL_TRACES_SAMPLER`) only with a reason.

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

The lines on standard error (one each per value; examples):

```text
otelhelper: WARN sampler is unsupported by this helper variable=OTEL_TRACES_SAMPLER value=xray using=parentbased_always_on
otelhelper: WARN unknown sampler (valid: always_on, always_off, traceidratio, parentbased_always_on, parentbased_always_off, parentbased_traceidratio) variable=OTEL_TRACES_SAMPLER value=foo using=parentbased_always_on
otelhelper: WARN sampler argument is not a number in [0, 1] for traceidratio variable=OTEL_TRACES_SAMPLER_ARG value=2 using=1.0
otelhelper: WARN sample ratio is not a number in [0, 1] variable=OTEL_HELPER_SAMPLE_RATIO value=abc using=parentbased_always_on
```

A valid value writes no line.

The sampler variables are read from the process environment only: a value only in `appsettings.json` (`IConfiguration`) is not read and warns once (§2):

```text
otelhelper: WARN OTEL_TRACES_SAMPLER is set only in IConfiguration and is not read; set it in the process environment or TelemetryOptions.Sampler variable=OTEL_TRACES_SAMPLER using=parentbased_always_on
```

0.2.1 already ignored such a value (it checked the process environment before leaving the sampler to the SDK), silently.

With `OTEL_TRACES_EXPORTER=none` the ParentBased sampler stays, as Go and Python keep an SDK provider there: a request that arrives with a sampled `traceparent` is passed on by `HttpClient` with the same trace id and the sampled flag, and a root request starts a sampled trace (measured by `Traces_Off_Server_Passes_The_Inbound_Trace_On`). An `AlwaysOffSampler` there would forward `00` and make every ParentBased service downstream drop the trace.

With traces **disabled** (`OTEL_HELPER_DISABLED_SIGNALS=traces`, `opts.DisabledSignals`) and under `OTEL_SDK_DISABLED=true`, propagation is a no-op tracer's, as in Go and Python: a root request sends no trace context downstream, and an inbound trace context is passed on unchanged — the same `traceparent` (trace id, parent span id and flags) and `tracestate` (measured by `Disabled_Traces_Propagate_As_A_No_Op_Tracer`). The sampler is then `ParentBased(root: AlwaysOff)`, so roots are not sampled. Under `OTEL_SDK_DISABLED=true` the helper adds a small non-recording activity listener so that ASP.NET Core creates a request activity for a request that continues a trace even without logging; roots get none.

### Propagators (0.3, this branch, task C10)

`OTEL_PROPAGATORS` is read per token: `tracecontext`, `baggage`, `none`. Other tokens warn once each and are skipped (`propagator not supported by the .NET helper, ignored` for `b3`, `b3multi`, `jaeger`, `xray`, `ottrace`; `unknown propagator, ignored` otherwise); each line names the effective list as `using=`. `none` disables propagation and wins over other tokens (warned when combined). Examples:

```text
otelhelper: WARN propagator not supported by the .NET helper, ignored variable=OTEL_PROPAGATORS value=b3 using="tracecontext, baggage"
otelhelper: WARN unknown propagator, ignored variable=OTEL_PROPAGATORS value=foo using="tracecontext, baggage"
otelhelper: WARN "none" combined with other propagators; propagation disabled variable=OTEL_PROPAGATORS value=none,tracecontext using=none
otelhelper: WARN OTEL_PROPAGATORS is set only in IConfiguration and is not read; set it in the process environment variable=OTEL_PROPAGATORS using="tracecontext, baggage"
```

 The list is installed process-wide with `Sdk.SetDefaultTextMapPropagator`, also under `OTEL_SDK_DISABLED=true` (no span context then, baggage still propagates).

.NET carries context in three places, and all three follow the list in both directions: the OpenTelemetry propagator above; the runtime's `DistributedContextPropagator.Current`, with which `HttpClient` writes `traceparent`, `tracestate` and `baggage` on its own; and the `DistributedContextPropagator` service ASP.NET Core extracts inbound context with. The helper sets the first two at resolution and replaces the third in the container (`AddOtelHelper`; the web host adds the runtime default with `TryAddSingleton`, before or after the application's services — both orders are tested). Its propagator delegates to the runtime default and drops what is not listed:

| `OTEL_PROPAGATORS` | Inbound `traceparent` | Inbound `baggage` | Sent on by `HttpClient` |
|---|---|---|---|
| unset, `tracecontext,baggage` | continued | kept | `traceparent`, `tracestate`, `baggage` (as 0.2.x) |
| `tracecontext` | continued | ignored | `traceparent`, `tracestate` |
| `baggage` | ignored: a new trace | kept | `baggage` |
| `none` | ignored: a new trace | ignored | nothing |

Notes:

- `AddOtelHelper` already sets `DistributedContextPropagator.Current` (from `OTEL_PROPAGATORS`, which is read from the environment only), so an `HttpClient` created during configuration or before the host starts follows the list too; the traces-disabled and SDK-disabled settings are added when the host starts (the handler reads them per request). An `HttpClient` created before `AddOtelHelper` runs keeps the runtime default.
- A runtime propagator the application set (`DistributedContextPropagator.Current = …`), before or after `AddOtelHelper`, is kept for handlers created afterwards; one warning when the configuration needs another. A `DistributedContextPropagator` service it registered is kept too, with one warning that ASP.NET Core extracts inbound context with it, not as `OTEL_PROPAGATORS` says:

  ```text
  otelhelper: WARN the application replaced DistributedContextPropagator.Current; HttpClient keeps writing context with it, not as OTEL_PROPAGATORS and the disabled signals say variable=OTEL_PROPAGATORS
  otelhelper: WARN the application registered its own DistributedContextPropagator service; ASP.NET Core extracts inbound context with it, not as OTEL_PROPAGATORS says variable=OTEL_PROPAGATORS
  ```
- Under `OTEL_SDK_DISABLED=true` with `tracecontext`, the helper's activity listener is process-wide: ASP.NET Core then creates a propagation-only (non-recorded) activity for every request that carries a `traceparent`, in every host of the process, until the container that started it is disposed.
- With traces or the SDK disabled, only an activity with a **remote** parent (`HasRemoteParent`, as ASP.NET Core and the instrumentations create) passes its inbound context on; an activity an application starts with `SetParentId(string)` or with a local context counts as started here and carries nothing. The SDK's expected "Failed to inject activity context … 'Invalid context'" line is not forwarded then.
- The settings are process-wide: the last container resolved wins, and nothing is restored when it is disposed. Known open item (C10 code review, carried to task C19): each `AddOtelHelper` call writes these settings, so a second host's `AddOtelHelper` briefly resets an earlier container's traces-disabled pass-through until the second container resolves.
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
| `StaffOps.OtelHelper.AWS` | `AddOtelHelperAws()` | AWS SDK for .NET v4 calls (S3, SQS, SNS, DynamoDB, etc.): one CLIENT span per call ([below](#aws-spans-failed-calls-and-url-redaction)) |
| `StaffOps.OtelHelper.Redis` | `AddOtelHelperRedis()` | StackExchange.Redis commands |
| `StaffOps.OtelHelper.Sql` | `AddOtelHelperSql()` | SqlClient, Npgsql and MySqlConnector spans and metrics (below) |
| `StaffOps.OtelHelper.Profiling` | `AddOtelHelperProfiling()` | Pyroscope continuous profiling (native CLR profiler), root spans linked to profiles by `pyroscope.profile.id` ([below](#profiling-pyroscope)) |

### SQL: drivers, sanitizing and semantic conventions

`AddOtelHelperSql()` subscribes SqlClient (tracing with exception events, and the
`db.client.operation.duration` metric, instrumentation 1.20.2), and the `Npgsql` and `MySqlConnector`
sources and meters of those drivers (task C14a). Call it next to
`AddOtelHelper()`; nothing else is needed for Npgsql or MySqlConnector (no
`Npgsql.OpenTelemetry`, no `AddNpgsql()`):

```csharp
builder.Services.AddOtelHelper();
builder.Services.AddOtelHelperSql();

// The driver of the application, as usual (here Npgsql.DependencyInjection):
builder.Services.AddNpgsqlDataSource(builder.Configuration.GetConnectionString("orders")!,
    b => b.Name = "orders");   // a name keeps the connection string out of metric attributes
```

What the spans look like after the helper's sanitizer (the application runs
`SELECT * FROM orders WHERE email = 'a@example.com' AND id = 42`):

| Attribute | Npgsql 10 | MySqlConnector 2.6 (default conventions) |
|---|---|---|
| `db.query.text` | `SELECT * FROM orders WHERE email = ? AND id = ?` | the same (added by the helper from `db.statement`) |
| `db.statement` | — | `SELECT * FROM orders WHERE email = ? AND id = ?` |
| `db.system.name` | `postgresql` | `mysql` (added by the helper) |
| `db.system` | `postgresql` (added by the helper) | `mysql` |
| status of a failed command | `ERROR`, no description; `error.type` = SQLSTATE (`22P02`) | `ERROR`, no description in what the helper exports; `error.type` = `MySqlConnector.MySqlException` |
| `db.connection_string` | — | removed |

SqlClient spans get `db.system=mssql` next to `db.system.name=microsoft.sql_server`.
With `OTEL_SEMCONV_STABILITY_OPT_IN=database` (without `database/dup`) the helper
keeps only the new keys on spans: no `db.system`, and no MySqlConnector
`db.statement`. A PostgreSQL string literal with a backslash drops the rest of the
statement from the text (its end depends on the server's
`standard_conforming_strings`).

Parameters (`$1`, `@p0`, `?`) are never values and stay as written; send values as
parameters, not inlined literals. `db.query.parameter.*` attributes (SqlClient with
`OTEL_DOTNET_EXPERIMENTAL_SQLCLIENT_ENABLE_TRACE_DB_QUERY_PARAMETERS`) are removed.

**Exception detail goes to a correlated log** ([ADR-014](https://github.com/StaffOps/otel-libs/blob/main/specs/decisions/ADR-014-exception-detail-in-correlated-logs.md)).
A database's error text can quote values (`Duplicate entry 'x' for key …`,
`invalid input syntax for type integer: "x"`). On a failed SqlClient, Npgsql or
MySqlConnector span the `exception` event keeps only `exception.type` (and any
other non-detail attribute); the helper writes the detail as one log record:

| Field | Value |
|---|---|
| category | `StaffOps.OtelHelper.Database` (filter it like any category, e.g. `Logging:LogLevel:StaffOps.OtelHelper.Database`) |
| level | Error (the helper's level rules apply; a level above Error drops it) |
| body | the exception message |
| attributes | `exception.type`, `exception.message`, `exception.stacktrace` |
| trace context | the span's `trace_id` / `span_id` on the OTLP record (Grafana: Tempo → Loki by trace id); on the stdout fallback line as the fields `trace_id` and `span_id` |

In Tempo a failed DB span shows the status, `error.type`,
`db.response.status_code` and the exception type; follow the trace-to-logs link
for the message and the stack trace. Treat logs as possibly holding data values
(retention, access). With no logs exporter and the stdout fallback off (an
application level above Error), the detail is not kept. To keep no exception
event at all for SqlClient, turn the recording off after `AddOtelHelperSql()`
(the helper then has nothing to log for SqlClient):

```csharp
builder.Services.Configure<OpenTelemetry.Instrumentation.SqlClient.SqlClientTraceInstrumentationOptions>(
    o => o.RecordException = false);
```

`System.Diagnostics.Activity` has no API to remove an event, so the helper
rebuilds the event list through a private field; if a future runtime lacks it,
the helper writes one warning on standard error and leaves the events as they
are:
`otelhelper: WARN cannot rewrite span events on this runtime (System.Diagnostics.Activity has no _events field); database and AWS exception events keep their message and stack trace (ADR-014)`. MySqlConnector adds its event after the span ended: only the helper's
exporters see it in time, so an exporter you add yourself still gets the full
event for MySqlConnector (and no log record is written for it on that path) —
an exception to ADR-014. Export through the helper, or delete the detail at the
Collector (`otelcol-contrib validate` passes with contrib 0.160.0, with the
wiring below; **not run against traffic**):

```yaml
processors:
  transform/db-exception-detail:
    error_mode: ignore
    trace_statements:
      - context: spanevent
        conditions:
          - name == "exception" and (span.attributes["db.system"] != nil or span.attributes["db.system.name"] != nil)
        statements:
          - delete_key(attributes, "exception.message")
          - delete_key(attributes, "exception.stacktrace")
service:
  pipelines:
    traces:
      receivers: [otlp]
      processors: [memory_limiter, transform/db-exception-detail, batch]   # your existing processors around it
      exporters: [otlp/tempo]                                               # your existing exporters
```

`StaffOps.OtelHelper.Database` is a reserved category: the helper reads the
`trace_id`, `span_id` and `otelhelper.event_time` attributes of its records to
set the record's trace context and time, so do not log through it yourself.
Other logger providers (Serilog, the host console) see those three attributes
as plain fields on its records.
The record's time is the exception event's time (OTLP and the stdout `time`).
A late MySqlConnector record written while the host shuts down is best-effort:
once the container is disposed the helper has no logger and writes none.

**Stdout lines carry the span context** (contract A23 amendment): when no logs
exporter is active, a line for any record written while a span is current has
`trace_id` and `span_id` right after `logger` (an attribute of either name is
then dropped):

```json
{"time":"2026-11-11T12:00:00.000Z","level":"WARN","msg":"order failed","logger":"Orders","trace_id":"4bf92f3577b34da6a3ce929d0e0e4736","span_id":"00f067aa0ba902b7","order":"o1"}
```

**Examples** (measured with Npgsql 10.0.3 on `postgres:17.11-alpine`, task C14b
fix round; host and user are placeholders). See also
[Exceptions: traces vs logs](#exceptions-traces-vs-logs).

A successful query:

```csharp
await using var cmd = new NpgsqlCommand(
    "SELECT id FROM (VALUES (42, 'a@example.com')) t(id, email) WHERE email = 'a@example.com'", conn);
await cmd.ExecuteScalarAsync();
```

```text
span "postgresql"  kind=Client  status=Unset
  db.system.name=postgresql  db.system=postgresql (helper)  db.namespace=otel_test
  db.query.text=SELECT id FROM (VALUES (?, ?)) t(id, email) WHERE email = ?
  server.address=<db-host>  db.npgsql.data_source=Host=<db-host>;Username=<user>;Database=otel_test
```

A failed query, and its correlated record (same `trace_id`, the span's `span_id`):

```csharp
await using var bad = new NpgsqlCommand("SELECT CAST('not-a-number' AS integer)", conn);
await bad.ExecuteScalarAsync();   // PostgresException 22P02
```

```text
span "postgresql"  status=Error (no description)  trace_id=97a4954c208df7560900fc0bd8aaded4  span_id=60f430f8062d0f02
  db.query.text=SELECT CAST(? AS integer)
  error.type=22P02  db.response.status_code=22P02
  event "exception" [exception.type=Npgsql.PostgresException]          <- no message, no stack trace

log record (OTLP)  category=StaffOps.OtelHelper.Database  severity=Error
  trace_id=97a4954c208df7560900fc0bd8aaded4  span_id=60f430f8062d0f02
  body=22P02: invalid input syntax for type integer: "not-a-number"\n\nPOSITION: 13
  exception.type=Npgsql.PostgresException
  exception.message=22P02: invalid input syntax for type integer: "not-a-number" ...
  exception.stacktrace=Npgsql.PostgresException (0x80004005): 22P02: ... at Npgsql.Internal.NpgsqlConnector.ReadMessageLong(...) ...
```

The same record on the stdout fallback (no OTLP endpoint), one line, stack trace
cut here (measured in a second run; its ids replaced by the span's above, so the
join is visible; on the stdout path they are fields of the line):

```json
{"time":"2026-11-11T18:06:08.187Z","level":"ERROR","msg":"22P02: invalid input syntax for type integer: \"not-a-number\"\n\nPOSITION: 13","logger":"StaffOps.OtelHelper.Database","trace_id":"97a4954c208df7560900fc0bd8aaded4","span_id":"60f430f8062d0f02","exception.type":"Npgsql.PostgresException","exception.message":"22P02: invalid input syntax for type integer: \"not-a-number\"\n\nPOSITION: 13","exception.stacktrace":"Npgsql.PostgresException (0x80004005): 22P02: invalid input syntax for type integer: \"not-a-number\"\n\nPOSITION: 13\n   at Npgsql.Internal.NpgsqlConnector.ReadMessageLong(…"}
```

A helper WARN you can hit next to these (measured, no Collector at the
endpoint): `otelhelper: WARN sdk OpenTelemetry-Exporter-OpenTelemetryProtocol: Transient HTTP error occurred when communicating with http://127.0.0.1:4317/opentelemetry.proto.collector.logs.v1.LogsService/Export. Exception: System.Net.Http.HttpRequestException: Connection refused (127.0.0.1:4317) …`
on standard error — the records are lost while the Collector is unreachable.



**Semantic conventions on metrics.** The drivers' metrics keep the keys each
driver writes (no view can add an attribute): SqlClient and Npgsql
`db.system.name`, MySqlConnector's pool metrics no system key. MySqlConnector
also reads `OTEL_SEMCONV_STABILITY_OPT_IN` itself (once per process, exact
lower-case tokens); `database/dup` set in the deployment gives its native new
keys too.

**Metric attributes.** Npgsql names a pool after the data source's `Name`, else
its connection string without `Password` (`SSL Password` stays); MySqlConnector
after `Application Name`, else the connection string without the password keys
(`Certificate Password` stays). The helper therefore drops the pool-name attribute
(`db.client.connection.pool.name`, `pool.name`) from every metric of the
`Npgsql` and `MySqlConnector` meters, also when the application subscribed them
itself: the pools of a meter are merged. The view keeps an allowlist of semconv
keys (`db.system.name`, `server.address`, `server.port`,
`db.client.connection.state`, `state`, …), so a new driver attribute is dropped
until it is listed. No switch exists yet to keep per-pool metrics (design D-NET15).
An application `MetricStreamConfiguration.Drop` view on one of these
instruments does not drop it (the helper's view adds its own stream); use
`OTEL_HELPER_DISABLED_METRICS` instead. Likewise an application view that returns its own
stream configuration for one of these instruments adds a second stream, which
keeps the pool name unless it sets `TagKeys`. The merge departs from the
semantic conventions (`db.client.connection.pool.name` is required on
`db.client.connection.*`) and sums the gauges of several pools into one series
(contract A25 note).

**Your own processors and exporters.** The sanitizer runs before every
processor registered after `AddOtelHelper()`. MySqlConnector sets the status
of a failed command after its span ended, so a processor or exporter you add
yourself still sees that status description (the error code name, for
example `NoSuchTable`), also when the app traces only through its own exporter;
the helper's own OTLP and console exporters remove it. Note the two ways to turn
the helper's traces off: `OTEL_TRACES_EXPORTER=none` drops only the helper's
exporter (your exporters on the same provider still get spans), while
`OTEL_HELPER_DISABLED_SIGNALS=traces` makes the sampler record nothing, which
silences your exporters too.

### Redis: semantic conventions and failed commands

Measured with `OpenTelemetry.Instrumentation.StackExchangeRedis` 1.20.1-beta.1,
StackExchange.Redis 2.6.122 and `redis:7.4.12-alpine` (task C14b and its fix
round; host names and ids below are placeholders or test values).

**Registration.** Two overloads:

```csharp
// 1. The multiplexer from DI (resolved when the tracer provider is built):
builder.Services.AddSingleton<IConnectionMultiplexer>(
    _ => ConnectionMultiplexer.Connect(builder.Configuration.GetConnectionString("redis")!));
builder.Services.AddOtelHelper();
builder.Services.AddOtelHelperRedis();

// 2. An explicit multiplexer (not in DI, keyed, or one of several):
builder.Services.AddOtelHelperRedis(cacheMultiplexer);
builder.Services.AddOtelHelperRedis(sessionMultiplexer);
```

| Case | Behavior |
|---|---|
| no-argument overload | instruments the **last** registered unkeyed `IConnectionMultiplexer` (what `GetService` returns); the registration may come before or after the call. 0.2.x threw `NotSupportedException` here |
| several multiplexers, or a keyed-only one | use the explicit overload, **once per multiplexer**; a keyed-only registration is not found by the no-argument overload |
| a factory registration (`AddSingleton(_ => ConnectionMultiplexer.Connect(...))`) | the factory runs when the tracer provider is built, so the app connects to Redis at start |
| no multiplexer registered | building the tracer provider (host start) fails with `InvalidOperationException` (below), **also with traces off** (`OTEL_TRACES_EXPORTER=none`, `OTEL_SDK_DISABLED`): the check runs whenever the provider is built |
| a container without `IServiceProviderIsService` | the check is skipped; with no multiplexer upstream instruments nothing, silently |
| the same multiplexer registered twice (`AddOtelHelperRedis()` and `AddOtelHelperRedis(mux)`) | two upstream registrations and two drain threads; the client keeps the last profiler, so each command still gives one span (measured). Call once per multiplexer |

The fail-fast message (measured):

```text
System.InvalidOperationException: AddOtelHelperRedis(): no IConnectionMultiplexer is registered in the service collection (a keyed-only registration is not found; pass it explicitly); register one or call AddOtelHelperRedis(connection). The check runs whenever the tracer provider is built, also with traces off
```

**Keys per `OTEL_SEMCONV_STABILITY_OPT_IN`** (owner decision O8 (b), 2026-11-11:
Redis follows the same rule as the SQL drivers and Go; "helper" marks a key the
helper adds, the others come from the instrumentation; a key the instrumentation
wrote is never overwritten):

| Attribute | unset (default) | `database/dup` | `database` |
|---|---|---|---|
| `db.system` | `redis` | `redis` | — |
| `db.system.name` | `redis` (helper) | `redis` | `redis` |
| `db.statement` | `SET` | `SET` | — |
| `db.operation.name` | `SET` (helper) | `SET` | `SET` |
| `db.query.text` | `SET` (helper, from `db.statement`) | `SET` | `SET` |
| `db.redis.database_index` | `0` | `0` | — |
| `db.namespace` | — (not derived; Go and Python write none) | `0` | `0` |
| `server.address`, `server.port` | yes | yes | yes |

The instrumentation reads the variable from the **process environment** only
(an `appsettings.json` or other `IConfiguration` key has no effect; `DATABASE`
is read like `database`, measured). The helper applies the contract rule too
(tokens trimmed and ASCII-folded, read when the tracer provider is built): if the
instrumentation still wrote legacy keys in the `database` mode, the helper
removes them (`db.system`, `db.statement`, and `db.redis.database_index`, which
has no current key) and keeps `db.query.text`. `db.namespace` comes only from the
instrumentation (`database/dup` or `database`); the default column has none.

**Statement shape.** `db.statement` / `db.query.text` is the command, plus one
`?` when anything follows it. Values and `AUTH` arguments are never present.
Subcommands are not kept: a statement `CONFIG GET maxmemory` becomes
`CONFIG ?` and `db.operation.name` is `CONFIG` (the rule, unit-tested; a live
`CONFIG` call was not measured — without verbose the instrumentation writes the
command only, `CONFIG`). `SetVerboseDatabaseStatements` (upstream option,
off by default) makes the instrumentation append the key or the whole Lua script;
the helper's sanitizer removes them again, so the option adds nothing on .NET:

| Command | upstream, verbose off | upstream, verbose on | after the helper |
|---|---|---|---|
| `SET orders:42 <value>` | `SET` | `SET orders:42` | `SET ?` |
| `PUBLISH <channel> <message>` | `PUBLISH` | `PUBLISH <channel>` | `PUBLISH ?` |
| `EVAL "local x = '<literal>' …"` | `EVAL` | `EVAL local x = '<literal>' …` | `EVAL ?` |
| `AUTH default <password>` | `AUTH` | `AUTH` | `AUTH` |
| `MSET k1 v1 k2 v2`, multi-key `UNLINK` | `MSET` | `MSET` | `MSET` |

Python writes one `?` per argument (`SET ? ?`), Go no statement at all.

**Span names.** The command as StackExchange.Redis formats it: `SET`, `HGET`,
`LPUSH`; `EVAL` for `ScriptEvaluate` and for a prepared `LuaScript`; the command
string given to `Execute` (`AUTH`). A transaction (`CreateTransaction`) is one
`EXEC` span: no `MULTI` span and no span per queued command. A batch
(`CreateBatch`) gives **no span**: StackExchange.Redis attaches the profiling
session in `ConnectionMultiplexer.PrepareToPushMessageToBridge`, and
`RedisBatch.Execute` enqueues on the connection directly (2.6.122 source), so
batched commands are never profiled (pinned by
`RedisIntegrationTests.Batch_Commands_Produce_No_Span`).

**Example: a successful command.**

```csharp
await db.StringSetAsync("orders:42", orderJson);
var id = await db.HashGetAsync("orders:index", "42");
```

```text
span "SET"   kind=Client  status=Unset
  db.system=redis  db.system.name=redis  db.operation.name=SET
  db.statement=SET  db.query.text=SET  db.redis.database_index=0
  server.address=<redis-host>  server.port=6379
span "HGET"  kind=Client  status=Unset
  db.system=redis  db.system.name=redis  db.operation.name=HGET
  db.statement=HGET  db.query.text=HGET  db.redis.database_index=0
  server.address=<redis-host>  server.port=6379
```

With `SetVerboseDatabaseStatements = true` the same calls give
`db.statement=SET ?`, `db.query.text=SET ?` (and `HGET ?`); with
`OTEL_SEMCONV_STABILITY_OPT_IN=database` the `SET` span carries
`db.system.name`, `db.operation.name`, `db.namespace=0`, `db.query.text`,
`server.*` only.

**Failed commands are not marked ERROR.** The instrumentation builds each span
from StackExchange.Redis's profiler after the command completed; the profiled
command carries no result, so the span gets no status, no `error.type`, no
`db.response.status_code` and no `exception` event, whatever the server
answered. Measured with WRONGTYPE, a Lua `redis.error_reply`, a Lua runtime
error, an unknown command and a wrong `AUTH` password: every span `Unset`, and
the error text (which quotes keys, arguments or script literals) reaches no span
field. **Dashboard impact:** error ratios and the Tempo service graph read 0%
errors for .NET Redis calls, and tail-sampling on `status = error` keeps none of
them; Go, Python and Node.js mark them ERROR (contract A26). Count Redis
failures from the application:

```csharp
// The span around the call is the one to mark; the Redis span stays Unset.
private static readonly Counter<long> RedisErrors =
    new Meter("Orders").CreateCounter<long>("orders.redis.errors");

try
{
    await db.ListLeftPushAsync(key, item);
}
catch (RedisServerException e)
{
    Activity.Current?.SetStatus(ActivityStatusCode.Error);
    Activity.Current?.SetTag("error.type", e.GetType().FullName);
    RedisErrors.Add(1, new KeyValuePair<string, object?>("error.type", e.GetType().FullName));
    logger.LogError("redis command failed: {error_type}", e.GetType().Name);
    throw;
}
```

What the failure looks like (measured, the stdout fallback without an OTLP
endpoint; the log line carries the `trace_id` and `span_id` of the current span,
the request's, so the trace is one click away):

```text
span "LPUSH"  kind=Client  status=Unset  (no error.type, no exception event)
  db.system=redis  db.system.name=redis  db.operation.name=LPUSH  db.statement=LPUSH ...
```

```json
{"time":"2026-11-11T18:06:07.980Z","level":"ERROR","msg":"redis command failed: RedisServerException","logger":"Orders","trace_id":"f3ed775ef0ae42e6d0299669b551eb5f","span_id":"190253fbfc8f7659","error_type":"RedisServerException"}
```

Do not log `e.Message` blindly: Redis error text can quote keys and arguments
(`ERR unknown command 'X', with args beginning with: '<arg>'`).

**`exception` events (ADR-014).** Upstream adds none. If an `Enrich` callback
adds one (`activity.AddException(e)`), the helper treats it as it does the SQL
drivers': the event keeps only `exception.type`, and the message and stack trace
go to one ERROR log record of category `StaffOps.OtelHelper.Database` with the
span's `trace_id` / `span_id` ([Exceptions: traces vs logs](#exceptions-traces-vs-logs)).
A failed status an `Enrich` callback sets keeps `ERROR` and loses its
description.

```csharp
builder.Services.Configure<OpenTelemetry.Instrumentation.StackExchangeRedis.StackExchangeRedisInstrumentationOptions>(o =>
    o.Enrich = (activity, context) => activity.AddException(new InvalidOperationException("cart <id> expired")));
```

```text
span "GET"  event "exception" [exception.type=System.InvalidOperationException]   <- type only
log record  category=StaffOps.OtelHelper.Database  severity=Error  trace_id/span_id = the span's
  body="cart <id> expired"  exception.type=System.InvalidOperationException
  exception.message="cart <id> expired"  exception.stacktrace=...
```

The same record shape as the SQL example above, with `exception.type` of your
event (the rule is unit-tested in
`RedisTests.Redis_Exception_Event_Keeps_Only_The_Type_And_The_Detail_Goes_To_A_Correlated_Log`;
this snippet itself was not run).

**Polling.** Spans appear when the profiler is drained: every 100 ms while
commands arrive, backing off to `FlushInterval` (default 10 s):

```csharp
builder.Services.Configure<OpenTelemetry.Instrumentation.StackExchangeRedis.StackExchangeRedisInstrumentationOptions>(
    o => o.FlushInterval = TimeSpan.FromSeconds(1));
```

The connection string (and its `password=`) never reaches a span.

### AWS: spans, failed calls and URL redaction

Measured with `OpenTelemetry.Instrumentation.AWS` 1.20.3 and `AWSSDK.Core` 4.0.3.3
(S3 4.0.14, DynamoDBv2 4.0.10.1, SNS 4.0.2.7, SQS 4.0.2.5) against a local fake
endpoint, and DynamoDB Local 2.6.1 for DynamoDB's error texts (task C14c,
2026-11-11; credentials are placeholders, ids are test values).

**Registration.** Build the tracer provider before you create AWS clients
(resolve them from DI after the host starts, or after `AddOtelHelper` has run):

```csharp
builder.Services.AddOtelHelper();
builder.Services.AddOtelHelperAws();
builder.Services.AddSingleton<IAmazonS3>(_ => new AmazonS3Client());   // created on first use, after start
```

| Case | Behavior |
|---|---|
| spans | one CLIENT span per call, `<Service>.<Operation>` (`S3.PutObject`, `DynamoDB.UpdateItem`, `SQS.SendMessage`), source `AWSSDK.<Service>` |
| HttpClient span under the call | none: `AddOtelHelperAws()` sets the upstream `SuppressDownstreamInstrumentation`. Without it each call also gives a `System.Net.Http` span whose `url.full` path is the S3 object key (`/orders/<key>`), plus the SDK's internal `HttpRequest` and `CredentialsRetrieval` spans (measured) |
| your own `AddAWSInstrumentation()` before `AddOtelHelperAws()` | AWSSDK.Core keeps the **first** pipeline customizer registered in the process, so your options win: no suppression unless you set it, and the HttpClient / `HttpRequest` / `CredentialsRetrieval` spans come back. The helper still removes the key from them (child-span rule below) |
| an AWS client created before the tracer provider is built | gets the SDK's spans but none of the instrumentation's: no `aws.s3.bucket` / table names and **no suppression**, so the HttpClient child span comes back; its URL path is replaced with `/` by the child-span rule (measured on .NET 8 and 10, C14c fix round 1). Create clients after the provider to get the instrumentation's attributes |
| HttpClient span with an AWS SDK span among its in-process ancestors (fix rounds 1 and 2; up to 16 levels, so a DelegatingHandler or resilience-pipeline span in between is covered) | its `url.full` / `http.url` path is replaced with `/` (scheme, host, port and the redacted query kept), `url.path` becomes `/`, `http.target` `/` plus its query: the path is the bucket and object key. The SDK's internal spans carry no URL (`HttpRequest`: method, status, content lengths; `CredentialsRetrieval`: RPC keys; measured) |
| span name of `GetObjectMetadata` (an S3 `HEAD`) | `S3.GetObjectMetadata`: the .NET request name, not the AWS operation (`HeadObject` in Go, Python and Node.js) |

**Example: a successful call** (S3 `PutObject`; the key is not recorded):

```csharp
await s3.PutObjectAsync(new PutObjectRequest { BucketName = "orders", Key = "invoices/2026/inv-42.pdf", ContentBody = pdf });
```

```text
span "S3.PutObject"  source=AWSSDK.S3  kind=Client  status=Unset
  rpc.system=aws-api  rpc.service=S3  rpc.method=PutObject  aws.s3.bucket=orders
```

The `rpc.*` keys are the **legacy** RPC keys (the instrumentation's `V1_28_0`
default, the contract default in every language); the current keys behind
`OTEL_SEMCONV_STABILITY_OPT_IN=rpc` were not measured on .NET and the helper
does not map them.

**Example: the HttpClient child of an early-built client** (or of your own
`AddAWSInstrumentation()` registered first), the same `PutObject`, measured:

```text
raw (the instrumentation without the helper's rules, measured in the C14c probe):
span "PUT"  source=System.Net.Http  parent=AWSSDK.S3 (HttpRequest)
  url.full=http://<s3-endpoint>/orders/invoices/2026/inv-42.pdf
after (0.3, this branch):
span "PUT"  source=System.Net.Http  parent=AWSSDK.S3 (HttpRequest)
  url.full=http://<s3-endpoint>/   server.address=<s3-endpoint>  http.response.status_code=200
```

**Rules on every `AWSSDK.*` span** (core sanitizing processor, also for sources
the application subscribed itself; contract A27):

| Rule | Effect |
|---|---|
| Failed call | status `ERROR` kept, description removed (upstream writes the exception message there) |
| `error.type` ownership | the helper owns it on AWS spans: a value set by an `Enrich` callback, upstream or another processor before the helper's is replaced by the rule below |
| `error.type` (upstream writes none) | the AWS error code (`aws.error_code`) when it matches `^[A-Z][A-Za-z0-9]{1,63}$` (`NoSuchKey`, `ValidationException`, `ConditionalCheckFailedException`, `InvalidParameter`); else the modeled exception's name without `Exception` (SQS: code `AWS.SimpleQueueService.NonExistentQueue`, exception `QueueDoesNotExistException` → `QueueDoesNotExist`); else, for an error from outside the SDK, its class name (`HttpRequestException` for a refused connection); else `_OTHER` (an unparseable response, `AmazonUnmarshallingException`; a generic `AmazonS3Exception` without a code). A body-less S3 `HEAD` 404 / 403 / 503 gives `NotFound` / `Forbidden` / `ServiceUnavailable` (the SDK's own code) |
| `aws.s3.key` | removed (upstream writes the request's `Key`); `aws.s3.copy_source`, `aws.s3.delete` and `aws.dynamodb.item_collection_metrics` are removed too (not written by 1.20.3; measured absent for `CopyObject`, `DeleteObjects` and a `PutItem` whose response had `ItemCollectionMetrics`) |
| `exception.message`, `exception.stacktrace` **attributes** | removed: AWSSDK.Core writes them on the span as well as in the event (`exception.type` and `aws.error_code` kept) |
| `exception` event ([ADR-014](https://github.com/StaffOps/otel-libs/blob/main/specs/decisions/ADR-014-exception-detail-in-correlated-logs.md)) | keeps only `exception.type`; the message and the stack trace go to one ERROR log record of category `StaffOps.OtelHelper.Aws` with the span's `trace_id` / `span_id` ([Exceptions: traces vs logs](#exceptions-traces-vs-logs)) |
| SNS publish to a phone number | never on a span (1.20.3 records no destination for a phone publish). The error text of a failed call, which can include the phone number (`Invalid parameter: PhoneNumber Reason: <phone> is not valid to publish to`) or item values, goes to the correlated log record — or to standard output without a logs exporter — by ADR-014's design: **treat the log pipeline as sensitive**. Python and Node.js write `messaging.destination.name=phone_number:**`, Go nothing |

Why the message goes to the log: the SDK copies the service's error message
verbatim, and DynamoDB quotes request literals in it (DynamoDB Local 2.6.1:
`UpdateExpression = "SET amount = 98765"` → `Invalid UpdateExpression: Syntax error; token: "98765", near: "= 98765"`;
`FilterExpression = "card = 'x'"` → `... near: "= 'x"`). A `ConditionalCheckFailed`
with `ReturnValuesOnConditionCheckFailure = ALL_OLD` keeps the old item out of
the message and the stack trace (`The conditional request failed`, measured).

**Example: a failed call** (S3 `GetObject` of a missing key; stdout fallback,
no OTLP endpoint — with OTLP the record carries the same ids as its trace context):

```text
span "S3.GetObject"  source=AWSSDK.S3  kind=Client  status=Error   (no description)
  rpc.system=aws-api  rpc.service=S3  rpc.method=GetObject  aws.s3.bucket=orders
  exception.type=Amazon.S3.AmazonS3Exception  aws.error_code=NoSuchKey  error.type=NoSuchKey
  http.status_code=404  aws.request_id=<request-id>
  event "exception" [exception.type=Amazon.S3.AmazonS3Exception]   <- type only
```

```json
{"time":"2026-11-11T19:46:29.722Z","level":"ERROR","msg":"The specified key does not exist.","logger":"StaffOps.OtelHelper.Aws","trace_id":"d11b35d9b7dbfe5d9b47d46cd0dc2c5f","span_id":"6954ed7d771d8492","exception.type":"Amazon.S3.AmazonS3Exception","exception.message":"The specified key does not exist.","exception.stacktrace":"Amazon.S3.AmazonS3Exception: The specified key does not exist.\n ---> Amazon.Runtime.Internal.HttpErrorResponseException: ..."}
```

**Example: a failed SNS publish to a phone number** (a fake `InvalidParameter`
response quoting the number; span measured, record body asserted by
`AwsTests.Sns_Publish_To_A_Phone_Number_Records_No_Number`, line shape as
measured for S3 above; the number is a placeholder):

```text
span "SNS.Publish"  source=AWSSDK.SNS  kind=Client  status=Error
  rpc.system=aws-api  rpc.service=SNS  rpc.method=Publish
  exception.type=Amazon.SimpleNotificationService.Model.InvalidParameterException
  aws.error_code=InvalidParameter  error.type=InvalidParameter  http.status_code=400
  event "exception" [exception.type=Amazon.SimpleNotificationService.Model.InvalidParameterException]
```

```json
{"time":"…","level":"ERROR","msg":"Invalid parameter: PhoneNumber Reason: <phone> is not valid to publish to","logger":"StaffOps.OtelHelper.Aws","trace_id":"<trace-id>","span_id":"<span-id>","exception.type":"Amazon.SimpleNotificationService.Model.InvalidParameterException","exception.message":"Invalid parameter: PhoneNumber Reason: <phone> is not valid to publish to","exception.stacktrace":"…"}
```

The phone number is in the log record only: restrict who reads those logs and
how long they are kept, as for the database records.

**Examples: the `error.type` fallbacks** (measured):

```text
refused connection (no AWS code):     exception.type=System.Net.Http.HttpRequestException     error.type=HttpRequestException
unparseable 200 response:             exception.type=Amazon.Runtime.AmazonUnmarshallingException  error.type=_OTHER
SQS with a dotted query code:         aws.error_code=AWS.SimpleQueueService.NonExistentQueue  error.type=QueueDoesNotExist
```

The same for DynamoDB (`error.type=ValidationException`, record body
`Invalid UpdateExpression: Syntax error; token: "98765", near: "= 98765"`) and
SQS (`aws.error_code=AWS.SimpleQueueService.NonExistentQueue`,
`error.type=QueueDoesNotExist`, body `The specified queue does not exist.`),
measured the same way. Count AWS failures from the spans
(`error.type`, status) and read the text from the record.

**URL query redaction** (contract A28; owner decision O3: keep upstream's text).
This applies to every HTTP span, not only AWS — typically a presigned S3 URL
your code fetches with `HttpClient`, or one a client sends to your API:

| Where | Default (upstream redaction on) | Upstream redaction turned off |
|---|---|---|
| server `url.query` (ASP.NET Core), every runtime | every value `Redacted`: `?X-Amz-Algorithm=Redacted&X-Amz-Credential=Redacted&…` | `OTEL_DOTNET_EXPERIMENTAL_ASPNETCORE_DISABLE_URL_QUERY_REDACTION=true`: values as sent, the helper writes the ten contract names as `REDACTED` |
| client `url.full` (HttpClient), .NET 8 runtime | every value `Redacted` | `OTEL_DOTNET_EXPERIMENTAL_HTTPCLIENT_DISABLE_URL_QUERY_REDACTION=true`: values as sent, the ten names `REDACTED` |
| client `url.full`, .NET 9+ runtime (measured on .NET 10) | the query is `*` (`/download?*`, names dropped) | `DOTNET_SYSTEM_NET_HTTP_DISABLEURIREDACTION=true` or the `System.Net.Http.DisableUriRedaction` switch (read once per process; the contrib variable has no effect there): values as sent, the ten names `REDACTED` |

The ten names (exact, case-sensitive, after percent-decoding the name, as Go,
Python and Node.js): `sig`, `Signature`, `AWSAccessKeyId`, `X-Goog-Signature`,
`X-Amz-Signature`, `X-Amz-Credential`, `X-Amz-Security-Token`, `access_token`,
`X-Goog-Credential`, `GoogleAccessId`. Other names (`X-Amz-Date`, `token`,
`api_key`), lower-case variants (`x-amz-signature`) and names without `=` are
kept; every other byte of the query stays as written; a value already
`Redacted` is left as upstream wrote it. The rule changes query values only:
userinfo in a span URL is left as written (HttpClient already removes it from
`url.full`), and IPv6 hosts (`http://[::1]:8080/?sig=x`) are handled like any
other host. Measured with both switches off
(.NET 8 runtime):

```text
span "GET /download"  kind=Server
  url.query=?X-Amz-Algorithm=AWS4-HMAC-SHA256&X-Amz-Credential=REDACTED&X-Amz-Date=20261111T120000Z&X-Amz-Expires=300&X-Amz-Security-Token=REDACTED&X-Amz-SignedHeaders=host&X-Amz-Signature=REDACTED
span "GET"  kind=Client
  url.full=http://127.0.0.1:<port>/download?X-Amz-Algorithm=AWS4-HMAC-SHA256&X-Amz-Credential=REDACTED&X-Amz-Date=20261111T120000Z&X-Amz-Expires=300&X-Amz-Security-Token=REDACTED&X-Amz-SignedHeaders=host&X-Amz-Signature=REDACTED
```

On the .NET 10 runtime HttpClient writes the client query as `?*`, and the
OpenTelemetry HttpClient switch (`OTEL_DOTNET_EXPERIMENTAL_HTTPCLIENT_DISABLE_URL_QUERY_REDACTION`)
has no effect there: only the runtime's `DOTNET_SYSTEM_NET_HTTP_DISABLEURIREDACTION`
(or the `System.Net.Http.DisableUriRedaction` switch) turns it off.

The redaction is on the span attributes `url.full`, `http.url`, `url.query`
and `http.target` only, also when the value is a `Uri` or another object (its
text is redacted and written back as a string; fix round 1). It is not a
free-text rule: an exception message of your own that embeds a URL is not
rewritten. HttpClient failures (`Connection refused (<host>:<port>)`,
`The SSL connection could not be established, see inner exception.`) carry no
URL in their status or exception text (measured), so nothing else is rewritten.
The AWS SDK's own calls are signed in headers: no signature in any query.

**Warnings you can hit** (the helper's diagnostics sink, standard error, written once):

```text
otelhelper: WARN span sanitizer failed; the span's status description, query text, connection, URL and AWS payload attributes were removed source=AWSSDK.S3 error=InvalidOperationException
otelhelper: WARN cannot rewrite span events on this runtime (System.Diagnostics.Activity has no _events field); database and AWS exception events keep their message and stack trace (ADR-014)
```

The first means a rule threw (a bug: report it); the span is exported without
those attributes. The second appears only on a runtime whose `Activity` lacks
the private event list the rewrite uses (the .NET 8 and 10 runtimes have it, canary test); then the
detail stays on the span and no record is written.

### Exceptions: traces vs logs

The helper's rule for exceptions whose text can carry data
([ADR-014](https://github.com/StaffOps/otel-libs/blob/main/specs/decisions/ADR-014-exception-detail-in-correlated-logs.md);
today the database clients — SqlClient, Npgsql, MySqlConnector, and Redis when
an `Enrich` callback adds an event — and the AWS SDK clients, task C14c):

1. **The span keeps the type**: status `ERROR` (no description), `error.type`,
   `db.response.status_code` (AWS: `aws.error_code`), and an `exception` event with only
   `exception.type`. That is enough to identify and count failures (span
   metrics, error ratio, tail sampling on `status = error`).
2. **The detail goes to the log**: one ERROR record, category
   `StaffOps.OtelHelper.Database` (AWS: `StaffOps.OtelHelper.Aws`), body = the message, attributes
   `exception.type`, `exception.message`, `exception.stacktrace`.
3. **The two are joined by `trace_id` / `span_id`**: the record carries the
   failed span's ids (OTLP trace context, or the `trace_id` / `span_id` fields of
   the stdout line), so the span leads to its log and back.

Treat logs as possibly holding data values (retention, access control). Logs
off, or an app level above Error, means the detail is kept nowhere.

**Following a failed span to its log in Grafana** (**not tested** in this
repository; adapt the data source UIDs and label names):

```yaml
# Tempo data source: trace to logs (provisioning). OTLP-ingested logs: Loki keeps
# trace_id / span_id as structured metadata, not in the line, so a line filter
# (filterByTraceID / filterBySpanID) finds nothing; query the metadata instead.
apiVersion: 1
datasources:
  - name: Tempo
    type: tempo
    uid: tempo
    jsonData:
      tracesToLogsV2:
        datasourceUid: loki
        spanStartTimeShift: "-5m"
        spanEndTimeShift: "5m"
        tags: [{ key: "service.name", value: "service_name" }]
        customQuery: true
        # $$ escapes the env-var expansion of provisioning files; in the UI type a single $.
        query: '{$${__tags}} | trace_id="$${__trace.traceId}" | span_id="$${__span.spanId}"'
        # stdout fallback lines (JSON in the line, scraped by an agent) instead:
        # query: '{$${__tags}} | json | trace_id="$${__trace.traceId}" | span_id="$${__span.spanId}"'
  # Loki data source: a derived field that turns trace_id into a Tempo link
  - name: Loki
    type: loki
    uid: loki
    jsonData:
      derivedFields:
        - name: trace_id
          matcherType: label          # OTLP logs: trace_id as structured metadata
          matcherRegex: trace_id
          datasourceUid: tempo
          url: "$${__value.raw}"
        - name: trace_id_json
          matcherRegex: '"trace_id":"(\w+)"'   # stdout fallback lines (JSON)
          datasourceUid: tempo
          url: "$${__value.raw}"
```

From the span in Tempo, "Logs for this span" opens Loki filtered by its trace
and span id; from the record in Loki, the `trace_id` link opens the trace. A
LogQL query for the records of one trace (stdout fallback):
`{service_name="orders-api"} | json | logger="StaffOps.OtelHelper.Database" | trace_id="<trace_id>"`.
Examples of the records: SQL and Redis above, AWS in the previous subsection
(for AWS use `logger="StaffOps.OtelHelper.Aws"`).


### Profiling (Pyroscope)

`AddOtelHelperProfiling()` adds Grafana Pyroscope's in-process .NET profiler
(packages `Pyroscope` 1.2.0 and `Pyroscope.OpenTelemetry` 0.5.0). The runtime
loads the native profiler itself, from environment variables it reads when the
process starts: the helper cannot set them in time, so it checks them and tells
you what is missing (task C15). Before 0.3 this section listed another
profiler's CLSID and library names; the values below are the ones measured.

**Deployment.** The native files come from the `pyroscope/pyroscope-dotnet`
image (`Pyroscope.Profiler.Native.so` and `Pyroscope.Linux.ApiWrapper.x64.so`
at its root):

```dockerfile
FROM pyroscope/pyroscope-dotnet:0.14.5-glibc-x86_64 AS pyroscope

FROM mcr.microsoft.com/dotnet/aspnet:8.0
COPY --from=pyroscope /Pyroscope.Profiler.Native.so /Pyroscope.Linux.ApiWrapper.x64.so /opt/pyroscope/
ENV CORECLR_ENABLE_PROFILING=1 \
    CORECLR_PROFILER={BD1A650D-AC5D-4896-B64F-D6FA25D6B26A} \
    CORECLR_PROFILER_PATH=/opt/pyroscope/Pyroscope.Profiler.Native.so \
    LD_PRELOAD=/opt/pyroscope/Pyroscope.Linux.ApiWrapper.x64.so \
    PYROSCOPE_PROFILING_ENABLED=1
# Per deployment (Helm values): PYROSCOPE_SERVER_ADDRESS, PYROSCOPE_APPLICATION_NAME.
```

```csharp
builder.Services.AddOtelHelper();
builder.Services.AddOtelHelperProfiling();
```

Profiles go from the native profiler straight to `PYROSCOPE_SERVER_ADDRESS`
(the profiler has no OTLP export), not through the Collector. A Pyroscope
behind authentication takes `PYROSCOPE_AUTH_TOKEN` or the basic-auth variables:
mount them from a Secret; the helper never reads them and never prints any
`PYROSCOPE_*` value.

**When it starts.** Nothing happens at registration. A hosted service runs the
checks below when the host starts:

- all met: it switches on Pyroscope's CPU, allocation, contention and
  exception profiling (`Profiler.Instance.Set…TrackingEnabled(true)`), and the
  tracer provider gets Pyroscope's span processor, which writes
  `pyroscope.profile.id` (the span id, 16 hex digits) on root spans and tells
  the profiler which span is running. A root span is, by the 0.5.0 code, one
  without an in-process parent, and also a direct child of a span that
  continues a remote trace (read in the source, not measured).
- one or more unmet: one `otelhelper: WARN` line (below), the profiler is not
  started and the span processor is not added. Not starting matters: without
  the native library, Pyroscope's first call prints a multi-line
  `[Profiler] Failed to load Pyroscope.Profiler.Native.so : System.DllNotFoundException: …`
  text on standard output (measured), which would land between the helper's
  JSON log lines.
- the start throws (for example a native library of another version without an
  entry point Pyroscope calls: Pyroscope catches only `DllNotFoundException`):
  one `otelhelper: ERROR` line with the exception type only, and the host keeps
  starting.

The checks are read once per container. Calling `AddOtelHelperProfiling()`
twice registers the profiler once. Without `AddOtelHelper()` the line is
written by the helper's registration-time sink (still standard error).

**Checks.** Measured 2026-11-12 with the native profiler 0.14.5
(`glibc-x86_64`, `linux/amd64` under emulation), `aspnet:8.0` and a Pyroscope
1.10.0 server; the "measured" column is what the runtime or the profiler did:

| Variable | Met when | Unmet item in the warning | Measured |
|---|---|---|---|
| `CORECLR_ENABLE_PROFILING` | exactly `1` | `CORECLR_ENABLE_PROFILING=1` | `true` does not load the profiler |
| `CORECLR_PROFILER` | `{BD1A650D-AC5D-4896-B64F-D6FA25D6B26A}`, any case, with braces | `CORECLR_PROFILER` (unset or blank), `CORECLR_PROFILER: not the Pyroscope profiler` | lowercase loads; without braces nothing loads; another CLSID: the library logs `is not CLSID_CorProfiler` |
| profiler path | `CORECLR_PROFILER_PATH_64` (x64), `_ARM64` (Arm64), `_32` / `_ARM32` first, then `CORECLR_PROFILER_PATH`; the first one set names an existing file | `CORECLR_PROFILER_PATH` (none set), `<variable>: file not found` | `CORECLR_PROFILER_PATH_64` alone loads on x64 |
| `LD_PRELOAD` (Linux only) | contains `Pyroscope.Linux.ApiWrapper` | `LD_PRELOAD: no Pyroscope.Linux.ApiWrapper` | without it the runtime loads the profiler, but Pyroscope's managed API cannot reach it (`DllNotFoundException`), so no profile types and no span link |
| `PYROSCOPE_PROFILING_ENABLED` | `1`, `t`, `y`, `true` or `yes`, any case (Pyroscope's own rule) | `PYROSCOPE_PROFILING_ENABLED=1` | unset: `.NET Profiler is explicitly disabled`; `yes` enables |
| `PYROSCOPE_SERVER_ADDRESS` | not blank | `PYROSCOPE_SERVER_ADDRESS` | — |
| `PYROSCOPE_APPLICATION_NAME` | optional | — | becomes the profiles' `service_name` |

Values are compared as written (no trimming: `" 1"` is not `1`). The items
appear in the table's order.

**Example — success** (measured): the helper writes nothing. The native
profiler writes its own lines on standard error (about 45 in a short run),
for example:

```text
[2026-11-12 04:14:30.516 | info | PId: 1 | TId: 1] .NET Profiler is explicitly enabled.
[2026-11-12 04:14:30.516 | info | PId: 1 | TId: 1] DllGetClassObject(): Returning an instance of CorProfilerCallbackFactory (hr=0x0)
[2026-11-12 04:14:30.545 | info | PId: 1 | TId: 1] timer_create-based Cpu Profiler started successfully.
```

A root span carried `pyroscope.profile.id=bf0c68a10cb14283` (its span id), and
Pyroscope listed the application: `POST /querier.v1.QuerierService/LabelValues`
with `{"name":"service_name"}` returned `{"names":["c15-probe","pyroscope"]}`.

**Example — a prerequisite missing** (measured, nothing set):

```text
otelhelper: WARN profiling prerequisites not met (CORECLR_ENABLE_PROFILING=1, CORECLR_PROFILER, CORECLR_PROFILER_PATH, LD_PRELOAD: no Pyroscope.Linux.ApiWrapper, PYROSCOPE_PROFILING_ENABLED=1, PYROSCOPE_SERVER_ADDRESS): the helper does not start the Pyroscope profiler and spans get no pyroscope.profile.id; set these variables in the container environment (Helm values or Dockerfile), the runtime reads them when the process starts
```

Other measured lines: another profiler's CLSID gives
`otelhelper: WARN profiling prerequisites not met (CORECLR_PROFILER: not the Pyroscope profiler): …`
(after the native library's own `DllGetClassObject(): the specified 'rclsid' … is not CLSID_CorProfiler.`);
`LD_PRELOAD` unset gives `(LD_PRELOAD: no Pyroscope.Linux.ApiWrapper)` although the
native profiler logged `.NET Profiler is explicitly enabled.`; a path to a missing
file gives `(CORECLR_PROFILER_PATH: file not found)`. What still works: traces,
metrics and logs, the host start, and `pyroscope.profile.id` is simply absent.
The line is written once per container, on standard error only (stdout stays
for the application's log lines); with a server address holding credentials
and `PYROSCOPE_AUTH_TOKEN` set, neither appeared in the output (measured).

**Example — the start throws** (unit test, not reproduced with a real
library):

```text
otelhelper: ERROR the Pyroscope profiler could not be started: its profile types stay off and the application keeps running error.type=System.EntryPointNotFoundException
```

**Migrating from 0.2.x.** The prerequisite warning was an `ILogger` record of
category `OtelHelper.Profiling`; it is now the `otelhelper: WARN` line above,
and the category no longer exists: `Logging:LogLevel:OtelHelper.Profiling`
filters have no effect (remove them), and a log query on that category finds
nothing — read the container's standard error instead. 0.2.x also started the
profiler and added the span processor at registration whatever the checks
said, so root spans carried `pyroscope.profile.id` even with no profiler, and
without the native library Pyroscope's `[Profiler] Failed to load …` text went
to standard output; now both happen only when every check passes.
`CORECLR_ENABLE_PROFILING=true` and an unset `CORECLR_PROFILER`, profiler path
or `LD_PRELOAD` used to pass the check; `PYROSCOPE_PROFILING_ENABLED=yes` (or
`t`, `y`) used to fail it.

**Limits.** Install `StaffOps.OtelHelper.Profiling` at the same version as
`StaffOps.OtelHelper`: it calls the core's internal diagnostics channel, so a
mismatched pair can fail at run time. Not measured: Arm64 (Pyroscope 1.2.0's
managed API links spans only in an x64 process: its `ProfilerStatus` checks
the process architecture, read in the source), musl images, and the .NET 10
runtime.

**Status: opt-in accessory, known open items (2026-11-12).** The package is
optional: core does not depend on it, and the Pyroscope native profiler is
heavy to install and measured only on x64 (`linux/amd64`). Open items, kept
as long-term TODOs (`specs/dotnet-otel-helper/tasks.md`, Promotion triggers):

- `DOTNET_EnableDiagnostics=0` or `DOTNET_EnableDiagnostics_Profiler=0` stops
  the runtime from loading the profiler, but the helper does not check them:
  no warning, and root spans still get `pyroscope.profile.id`.
- If `Start()` throws, the `otelhelper: ERROR` line is written but the span
  processor stays, so root spans carry a `pyroscope.profile.id` with no
  profile behind it.
- Apps without a generic host (no `IHostedService` run) never start the
  profiler.
- `CORECLR_ENABLE_PROFILING` must be the literal `1`; `01` or `0x1` are
  reported as unmet (whether the runtime accepts them is not measured).
- On a non-Linux OS the `LD_PRELOAD` item is skipped and no unmet item says
  that only the Linux profiler was measured.
- The exact-version requirement above (internal diagnostics channel).

---

## 15. Metrics Without a Collector (Prometheus `/metrics`)

When `OTEL_EXPORTER_OTLP_ENDPOINT` is **not set**, the library automatically falls back to local-only mode:

| Signal | Behavior |
|--------|----------|
| Metrics | Exposed via Prometheus HTTP `/metrics` on port 9464 |
| Traces | In-process only (context propagation works, no export) |
| Logs | stdout: one JSON object per line (§4 "Stdout fallback"; no OTel export) |

The port is configurable (default `9464`; precedence and bind address below).

This enables the standard Kubernetes scrape pattern: deploy without a Collector, and let Prometheus/VictoriaMetrics scrape `/metrics` directly from the pod.

The listener binds `http://+:<port>/metrics/` — every IPv4 address (the
managed `HttpListener` maps `+` to `IPAddress.Any`, as it did 0.2.x's `*`), so
a scraper in another pod or container reaches it — and answers each scrape
with a fresh collection (no response cache). There is no IPv6 bind: on an
IPv6-only cluster a scrape of the pod IP fails (known limitation, unchanged
from 0.2.x). 0.2.x ignored `OTEL_EXPORTER_PROMETHEUS_HOST` /
`OTEL_EXPORTER_PROMETHEUS_PORT`; the helper honors them now (below).
`testing/e2e/dotnet-prom-scrape.sh` checks the scrape from another container.

### Port, bind address and flushing (0.3, this branch, task C12)

The helper honors the standard variables (contract A29); the lines it writes
for invalid values, IPv6 hosts and a failed bind are listed in
[README "Prometheus fallback"](README.md#prometheus-fallback-03-this-branch):

```bash
# Port: code PrometheusMetricsPort > OTEL_EXPORTER_PROMETHEUS_PORT > OTEL_HELPER_METRICS_PORT > 9464
OTEL_EXPORTER_PROMETHEUS_PORT=9100

# Bind address: unset = every IPv4 interface. Restrict to loopback for a sidecar scraper:
OTEL_EXPORTER_PROMETHEUS_HOST=127.0.0.1
```

- Both port variables accept `^\+?[0-9]+$` in [0, 65535] (`0` = no listener);
  `9_100`, `-0` or a 40-digit string warn once and the next level applies.
- The host is an IPv4 literal, `localhost` (bound as `127.0.0.1`) or `0.0.0.0`
  (every IPv4 interface). Any other value warns once and binds every IPv4
  interface. .NET has no IPv6 bind: `::1` binds `127.0.0.1` (one warning), `::`
  every IPv4 interface (one warning), any other IPv6 address no listener (one
  ERROR; start continues).
- A bind failure (busy port, an address the machine does not own) writes one
  ERROR naming the address, the port and what set them; then the build fails.
- With `127.0.0.1` (or `localhost`, `::1`) the listener stays on loopback and
  answers `http://127.0.0.1:9100/metrics` always. `http://localhost:9100/metrics`
  answers clients that dial `::1` first (Go — Prometheus, vmagent — and .NET
  `HttpClient`, measured); curl and Node.js resolve `localhost` with
  `AI_ADDRCONFIG` and, in a container whose only IPv6 address is `::1`, get 404
  (the listener routes by `Host` and the `localhost` prefix sits on `::1`):
  point them at `127.0.0.1`. Any other address answers only requests that name
  it in `Host`.
- The listener reader exists only while the listener binds. With both `otlp`
  and `prometheus`, `MeterProvider.ForceFlush()` and `Shutdown()` return
  `false` whatever the OTLP result (upstream: a pull-only reader fails any
  collection outside a scrape; a mounted scraping endpoint, below, does the
  same). With OTLP only, `ForceFlush()` reports the export result, but
  `Shutdown()` returns `true` even after the collector answered 500 (upstream,
  measured). **Only `ForceFlushOtelHelper()` is a reliable success signal**:
  call it before a short-lived process exits:

```csharp
using StaffOps.OtelHelper;

var host = builder.Build();
await host.RunAsync();

// True when the helper's metric readers exported (a collector answering 500 → false)
// and the trace and log batches were handed to their exporters within 5 s.
if (!host.Services.ForceFlushOtelHelper(timeoutMilliseconds: 5_000))
{
    Console.Error.WriteLine("telemetry flush incomplete");
}
```

`ForceFlushOtelHelper` throws `ObjectDisposedException` once the service
provider is disposed, so call it before disposing the host. It flushes the
`TracerProvider` and `LoggerProvider` (for
them the result is the SDK's: "handed to the exporter in time", not
"accepted") and collects each push metric reader the helper built (OTLP,
console), returning their export result. With
`OTEL_EXPORTER_OTLP_METRICS_DEFAULT_HISTOGRAM_AGGREGATION=base2_exponential_bucket_histogram`
the OTLP metric reader is the SDK's, so the metric part is
`MeterProvider.ForceFlush()` (`false` in dual mode). Under
`OTEL_SDK_DISABLED=true` it returns `true`.

- `OTEL_HELPER_DISABLED_METRICS` patterns are case-sensitive: `Http.*` does not
  drop `http.server.request.duration`. `?` matches one character.

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
- `OTEL_METRICS_EXPORTER=otlp` without a metrics endpoint warns and is dropped; when nothing is left the default (`prometheus`) applies. In code, `MetricExporters = "logging"` or an unknown entry fails start.

The lines (examples):

```text
otelhelper: WARN deprecated exporter value 'logging' in OTEL_TRACES_EXPORTER; use 'console' variable=OTEL_TRACES_EXPORTER value=logging using=console
otelhelper: WARN invalid OTEL_TRACES_EXPORTER: unknown exporter ignored; valid values: otlp, console, none variable=OTEL_TRACES_EXPORTER value=zipkin,otlp
otelhelper: WARN invalid OTEL_LOGS_EXPORTER: 'none' combined with other exporters: the signal is not exported variable=OTEL_LOGS_EXPORTER value=none,otlp
otelhelper: WARN metric exporter 'otlp' has no endpoint and is ignored; set OTEL_EXPORTER_OTLP_ENDPOINT or OTEL_EXPORTER_OTLP_METRICS_ENDPOINT variable=OTEL_METRICS_EXPORTER
otelhelper: WARN invalid OTEL_HELPER_DISABLED_SIGNALS: unknown signal ignored; valid values: traces, metrics, logs variable=OTEL_HELPER_DISABLED_SIGNALS value=spans
```

A code value fails start with the validation message after `host start fails: `,
for example `Unknown metric exporter(s) zipkin in TelemetryOptions.MetricExporters. Valid values: otlp, prometheus, console, none.`
(`DisabledSignals`: `Unknown signal(s) spans in TelemetryOptions.DisabledSignals. Valid values: traces, metrics, logs.`).
- The deprecated `OTEL_HELPER_DISABLED_SIGNALS` disables a signal only when that signal's `OTEL_<SIGNAL>_EXPORTER` is unset (or holds only unknown entries); for metrics a code `MetricExporters` wins too. `TelemetryOptions.DisabledSignals` (code) wins over all of them, and an unknown signal there fails start. Set in code means not null: `DisabledSignals = ""` disables nothing and still beats the variable, and `MetricExporters = ""` means no metric exporter (as Go, Python and Node.js treat an empty list; .NET also writes one INFO line, `TelemetryOptions.MetricExporters is empty: no metric exporter`, because a blank `appsettings.json` placeholder counted as unset before 0.3).
- `OTEL_SDK_DISABLED=true` overrides everything: no exporter at all.
- With metrics disabled or no metric exporter active, a view drops the helper's
  instrumentation meters (the service meter and `<service>.*`,
  `AdditionalMeters`, `Microsoft.AspNetCore.*`, `System.Net.*`, `System.Runtime`,
  `OpenTelemetry.Instrumentation.*`) so they cost nothing. The view is on the
  provider, so it also drops your own `AddAspNetCoreInstrumentation()` /
  `AddHttpClientInstrumentation()` metrics and the meters named after the
  service on a reader you add yourself. `prometheus` with `PrometheusMetricsPort = 0`
  counts as active (task C5). No line is written.

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
default; any other value (also a padded one) is warned and ignored. With the
variable set but OTLP metrics inactive, its reader stays idle. The lines:

```text
otelhelper: WARN invalid OTEL_EXPORTER_OTLP_METRICS_DEFAULT_HISTOGRAM_AGGREGATION: unsupported aggregation (values are compared as written, without trimming), using explicit_bucket_histogram; valid values: explicit_bucket_histogram, base2_exponential_bucket_histogram variable=… value=…
otelhelper: WARN OTEL_EXPORTER_OTLP_METRICS_DEFAULT_HISTOGRAM_AGGREGATION is set only in IConfiguration, which is registered as a factory that AddOtelHelper cannot read, so it is not applied; set it in the process environment variable=…
otelhelper: WARN OTEL_EXPORTER_OTLP_METRICS_DEFAULT_HISTOGRAM_AGGREGATION differs between IConfiguration and the process environment; AddOtelHelper used the process environment value, so base2_exponential_bucket_histogram is not applied variable=… used=explicit_bucket_histogram
otelhelper: WARN OTEL_EXPORTER_OTLP_METRICS_DEFAULT_HISTOGRAM_AGGREGATION is set but OTLP metrics are not active: its OTLP reader stays idle and sends nothing variable=…
```

(`…`: the variable name is 66 characters, so its echo is cut at 64 with `…`.)
The "differs" line appears only when a source that outranks the environment
(the command line) holds `base2_exponential_bucket_histogram`.

**Do not combine `UseOtlpExporter()` with `AddOtelHelper()`.** Both export, so
every signal is sent twice; the helper warns once (only when it has an OTLP
target itself). With the histogram variable set as well: a `UseOtlpExporter()`
called **before** `AddOtelHelper()` applies the variable through its own reader
and the helper registers none (warned); one called **after** makes the SDK fail
the `MeterProvider` build with `NotSupportedException`, and the helper writes one
ERROR line first:

```text
otelhelper: WARN UseOtlpExporter() with AddOtelHelper exports every signal twice; drop one
otelhelper: WARN UseOtlpExporter() is registered: its own reader applies OTEL_EXPORTER_OTLP_METRICS_DEFAULT_HISTOGRAM_AGGREGATION, so AddOtelHelper registers no second reader for it variable=…
otelhelper: ERROR OTEL_EXPORTER_OTLP_METRICS_DEFAULT_HISTOGRAM_AGGREGATION made AddOtelHelper register the SDK's AddOtlpExporter reader, and UseOtlpExporter() was called after AddOtelHelper: the SDK refuses both and fails the MeterProvider build; drop UseOtlpExporter() or call it before AddOtelHelper variable=…
```

### Scoping down what gets scraped

`OTEL_HELPER_DISABLED_METRICS` drops instruments from **every** active exporter — there's no supported way to push everything via OTLP while exposing only a subset on `/metrics` from the same `MeterProvider` (the OTel SDK's Views are per-provider, not per-reader). If you need a narrower `/metrics` output while keeping the full set on OTLP, filter on the scrape side instead — e.g. Prometheus `metric_relabel_configs` or a vmagent `drop` relabel rule matching the instrument name.

---

## 16. Metrics Export Interval

Metrics are exported every **30 seconds** (not the SDK default of 60s). This applies to both OTLP export and the Prometheus `/metrics` fallback.

0.3 (this branch, task C9): `OTEL_METRIC_EXPORT_INTERVAL` or `TelemetryOptions.ExportIntervalMs` changes it for the OTLP and console readers; the Prometheus endpoint is pull-based and collects on each scrape. Grammar and clamping: §15 ("Running OTLP push AND `/metrics` at the same time"). The lines:

```text
otelhelper: WARN invalid OTEL_METRIC_EXPORT_INTERVAL: not a positive integer number of milliseconds, using 30000 variable=OTEL_METRIC_EXPORT_INTERVAL value=5_000
otelhelper: WARN invalid OTEL_METRIC_EXPORT_INTERVAL: above 2147483647 ms, clamped to 2147483647 variable=OTEL_METRIC_EXPORT_INTERVAL value=9999999999
otelhelper: WARN OTEL_METRIC_EXPORT_INTERVAL is set only in IConfiguration and is not read; set it in the process environment or TelemetryOptions.ExportIntervalMs, using 30000 variable=OTEL_METRIC_EXPORT_INTERVAL
```

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
otelhelper: WARN invalid OTEL_EXPORTER_OTLP_INSECURE: not a boolean (true or false), using false variable=OTEL_EXPORTER_OTLP_INSECURE value=yes
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
| a file cannot be read | `otelhelper: WARN a TLS file cannot be read (missing, not readable, or larger than 1 MiB) and is ignored variable=OTEL_EXPORTER_OTLP_CERTIFICATE using="the system roots"` (a per-signal variable shows `using="the generic variable"`; a client file `using=none`), then the next level (for the CA, finally the system roots) |
| a CA file with no PEM certificate (`/dev/null`) | `otelhelper: WARN a TLS CA file holds no PEM certificate and is ignored variable=… using=…`, the next level |
| only the key or only the certificate resolves | `otelhelper: WARN the client key and the client certificate must both be set and readable; the client certificate is not used …` naming the variable(s), `using="server authentication"`; server authentication with the resolved CA |
| the pair does not load (mismatch, bad PEM, encrypted key) | `otelhelper: WARN the client key and certificate were rejected (bad PEM, mismatch or encrypted key); the client certificate is not used …` naming both variables; server authentication |
| the endpoint is plaintext (`http://`, or schemeless + `INSECURE=true`) | the files are not read; one warning names them and every signal they are ignored for: `TLS files are set but the endpoint is plaintext …; they are ignored for these signals … variable=OTEL_EXPORTER_OTLP_CERTIFICATE signals=traces,metrics,logs` |
| a file larger than 1 MiB, or a device (`/dev/zero`) | refused as "cannot be read" (the read is bounded) |
| a named pipe (FIFO) with no writer | the read waits for a writer: host start blocks. Point the variables at regular files (an operator error, not detected) |

Warnings never print a path or file content. Setup never fails on a TLS file.

**What the custom CA does.** `OTEL_EXPORTER_OTLP_CERTIFICATE` adds trust; it
does not replace the system roots: a server certificate the system accepts
still passes. Otherwise the chain must end at one of the file's CAs, built with
the intermediates the server presents; the leaf must allow server
authentication (EKU `1.3.6.1.5.5.7.3.1`, so a client-auth-only certificate is
rejected); revocation is not checked on that path; a host-name mismatch still
fails (task C5).

The client key: on Linux it stays an ephemeral key, never written anywhere. On
Windows, SChannel cannot use an ephemeral key for client authentication, so
the key is imported into the user key store for the life of the certificate
(removed when the exporter is disposed); the process needs a user profile.

### Endpoint values the helper rejects or rewrites (0.3, this branch)

| Value | What happens |
|---|---|
| `http://user:pw@collector:4317/?token=x` | userinfo, query and fragment removed, one warning per variable and value (`endpoint userinfo/query is not supported and was removed; put credentials in OTEL_EXPORTER_OTLP_HEADERS`); its `using=` is the URL the signal uses, `/v1/<signal>` included for `http/protobuf` |
| `http://collector` with gRPC | dials `http://collector:4317`, warning `gRPC endpoint has no port; using 4317 - set an explicit port (e.g. :4317, or :443 behind an ingress)` |
| `collector:4318` with `http/protobuf` | `https://collector:4318/v1/<signal>` and a deprecation warning: use a URL |
| `http://bücher.de:4317` | dials `http://xn--bcher-kva.de:4317` (punycode) |
| `http://collector:4317/otlp` with gRPC | dials `http://collector:4317`; `otelhelper: WARN endpoint path is ignored by the gRPC exporter variable=OTEL_EXPORTER_OTLP_ENDPOINT`, once per variable, never the path (task C7b; a bare `/` is not warned; a code endpoint with a path warns the same way and does not fail start) |
| `http://collector:04318` | port 4318, so `http/protobuf` is inferred (INFO) |
| `http://user:4318/secret@collector` | used as parsed; `endpoint path contains '@' - misplaced credentials?` naming the variable only |
| `http://coll ector:4317`, `http://[fe80::1%eth0]:4317`, `http://127.1:4317`, `http://h:0`, `https://h/x\y`, `http://h:4318/%zz` (`http/protobuf`) | invalid. Environment: `otelhelper: WARN invalid endpoint; ignored variable=OTEL_EXPORTER_OTLP_TRACES_ENDPOINT value="<unparseable URL>" using="next configured endpoint, else none"`, then the generic endpoint is used. Code: host start fails naming the option |
| `unix:/var/run/otel.sock`, `ftp://collector` | unsupported scheme: `otelhelper: WARN unsupported endpoint scheme (use http:// or https://); ignored variable=OTEL_EXPORTER_OTLP_ENDPOINT value=unix: using="no OTLP exporter for this signal"`; that signal gets no OTLP exporter |

**Which endpoint a signal uses:** `TelemetryOptions.<Signal>Endpoint` >
`TelemetryOptions.OtelCollectorEndpoint` > `OTEL_EXPORTER_OTLP_<SIGNAL>_ENDPOINT` >
`OTEL_EXPORTER_OTLP_ENDPOINT` — a code generic endpoint beats an environment
per-signal one. An invalid code value fails host start with
`<option> <value> is not a valid OTLP endpoint: <reason>; use an http:// or https:// URL or host:port.`
after the `otelhelper: ERROR invalid TelemetryOptions set in code; …` line (README "Helper diagnostics").

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

`http/json` is a valid value per the OTel spec but has no exporter implementation for traces/metrics/logs in this library: in `OTEL_EXPORTER_OTLP_PROTOCOL` it is warned on standard error and the default applies (an unknown value too):

```text
otelhelper: WARN invalid OTEL_EXPORTER_OTLP_PROTOCOL: http/json is not supported by the .NET OTLP exporter; ignored (port inference, else grpc) variable=OTEL_EXPORTER_OTLP_PROTOCOL value=http/json
otelhelper: WARN invalid OTEL_EXPORTER_OTLP_PROTOCOL: unsupported OTLP protocol, ignored (port inference, else grpc); valid values: grpc, http/protobuf variable=OTEL_EXPORTER_OTLP_PROTOCOL value=grpcs
```

 set in `TelemetryOptions.OtlpProtocol` it fails host start, naming the option.

`/v1/traces`, `/v1/metrics`, `/v1/logs` are appended to the endpoint automatically when `http/protobuf` is selected — do not include them in `OTEL_EXPORTER_OTLP_ENDPOINT`. A per-signal endpoint (`OTEL_EXPORTER_OTLP_TRACES_ENDPOINT`, `TelemetryOptions.TracesEndpoint`, …) is used as written. 0.3 (this branch) keeps the path of the generic endpoint (`https://gw.example.com/otlp` → `https://gw.example.com/otlp/v1/traces`); 0.2.x dropped it.

For local development with a plaintext collector, set `OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4317` (there is no default endpoint: unset means no OTLP export).

---

## 19. OTLP Headers, Timeout, Compression and Redirects (0.3, this branch)

Task C8: the helper resolves these per signal — code, then
`OTEL_EXPORTER_OTLP_<SIGNAL>_*`, then `OTEL_EXPORTER_OTLP_*`, then the
default — and gives each exporter its own HTTP client. That client replaces
the SDK's: an `HttpClientFactory` or `Headers` set on `OtlpExporterOptions` by
the application, and the named `IHttpClientFactory` clients the 0.2.x exporter
used (`OtlpTraceExporter`, `OtlpMetricExporter`, `OtlpLogExporter`), do not
reach the helper's exporters (ADR-013; configure headers and TLS through the
variables or `TelemetryOptions`).

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
the value:

```text
otelhelper: WARN a malformed OTEL_EXPORTER_OTLP_HEADERS item was skipped (expected key=value, the key of letters, digits, '.', '_' or '-') variable=OTEL_EXPORTER_OTLP_HEADERS item="item #2"
otelhelper: WARN a OTEL_EXPORTER_OTLP_HEADERS item was skipped: its decoded value is not printable ASCII variable=OTEL_EXPORTER_OTLP_HEADERS item="item #1"
otelhelper: WARN a OTEL_EXPORTER_OTLP_HEADERS item has an invalid percent-escape, or is not UTF-8 once decoded, and is kept literally variable=OTEL_EXPORTER_OTLP_HEADERS item="item #1"
otelhelper: WARN an OTLP header item was rejected by the HTTP client (a content header) and skipped signal=traces key=content-type
```

Keys are lowercased; for a key that appears twice the last one wins. A content
header (`content-type`, `content-length`, …) is rejected by the HTTP client:
skipped, the key named, never the value. On gRPC a key ending in `-bin` is
binary metadata: its value is sent as unpadded base64 (`ab` → `YWI`, as Go and
Python; Node.js pads it). A
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
`OTEL_METRIC_EXPORT_TIMEOUT` are not honored (one warning each:
`otelhelper: WARN OTEL_BSP_EXPORT_TIMEOUT is not honored: the OTLP export timeout is the export deadline of every signal; set OTEL_EXPORTER_OTLP_TIMEOUT or OTEL_EXPORTER_OTLP_<SIGNAL>_TIMEOUT variable=OTEL_BSP_EXPORT_TIMEOUT`), and an
application `Configure<BatchExportActivityProcessorOptions>`,
`Configure<LogRecordExportProcessorOptions>` or `Configure<MetricReaderOptions>`
export timeout does not apply to the helper's exporters: set while its variable
is unset, each warns once naming the option (`otelhelper: WARN <option> is set (in code or IConfiguration) but not used by the helper's OTLP exporter: the OTLP export timeout is the export deadline of every signal; set TelemetryOptions.ExportTimeoutMs`). With
`OTEL_EXPORTER_OTLP_METRICS_DEFAULT_HISTOGRAM_AGGREGATION`, a timeout set on the named
options `"StaffOps.OtelHelper.OtlpMetrics"` after `AddOtelHelper` is replaced and warned;
one registered before `AddOtelHelper` is overwritten by the helper's copy without a warning. `grpc-timeout` carries the
configured deadline of each request, not the time left on an SDK retry. The
lines:

```text
otelhelper: WARN invalid OTEL_EXPORTER_OTLP_TIMEOUT: not positive decimal milliseconds; using the next level, else 10000 variable=OTEL_EXPORTER_OTLP_TIMEOUT value=0
otelhelper: WARN invalid OTEL_EXPORTER_OTLP_TIMEOUT: timeout above 24 h; clamped to 86400000 variable=OTEL_EXPORTER_OTLP_TIMEOUT value=90000000
otelhelper: WARN invalid OTEL_EXPORTER_OTLP_TIMEOUT: timeout below 1 ms; clamped to 1 variable=OTEL_EXPORTER_OTLP_TIMEOUT value=0.2
```

**Compression.** `gzip` (default) or `none`, any ASCII case. Another value
(`deflate`, `zstd`) warns and the next level applies:
`otelhelper: WARN invalid OTEL_EXPORTER_OTLP_COMPRESSION: unsupported compression, using the next level, else gzip; valid values: gzip, none variable=OTEL_EXPORTER_OTLP_COMPRESSION value=zstd`.

**Batch size.** `OTEL_BSP_MAX_EXPORT_BATCH_SIZE` above `OTEL_BSP_MAX_QUEUE_SIZE`
(likewise `OTEL_BLRP_*`; for both prefixes the default batch size is 512 and the
default queue size 2048) warns once naming both and the queue size is used
(set in code, the line names the options instead of the variables; the `variable=` key appears twice, so a logfmt parser keeps only one of the names):

```text
otelhelper: WARN the export batch size is larger than the queue size; using the queue size variable=OTEL_BSP_MAX_EXPORT_BATCH_SIZE variable=OTEL_BSP_MAX_QUEUE_SIZE value=4096 using=2048
```

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
API to override it (`otelhelper: WARN OTEL_ATTRIBUTE_VALUE_LENGTH_LIMIT=0 is not a valid value-length limit, but the .NET OTLP exporter applies it and sends those attribute values empty; unset it or set a positive limit variable=OTEL_ATTRIBUTE_VALUE_LENGTH_LIMIT`). Unset it or use a positive limit. (Hiding the variable
while the exporter is built, as the Python helper does, would need
`Environment.SetEnvironmentVariable`, which the .NET helper never calls.)

