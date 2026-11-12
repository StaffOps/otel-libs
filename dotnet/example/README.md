# StaffOps.OtelHelper — Examples

📖 **[EXAMPLES.md](EXAMPLES.md)** — Detailed description of each check, internals, and Grafana queries

Three services demonstrating complete instrumentation with `StaffOps.OtelHelper`
(HTTP → gRPC distributed tracing), plus a short job with custom spans, metrics
and logs ([Custom signals](#custom-signals-dotnet-custom-signals)):

```
┌─────────────────────────────────────────────────────────────────────┐
│ dotnet-process (Worker, .NET 8)                                      │
│   ├── ApiHealthWorker: calls dotnet-api every 1 min                  │
│   └── HeavyProcessWorker: CPU/memory stress every 2 min             │
└──────────────────────────┬──────────────────────────────────────────┘
                           │ HTTP :8080
                           ▼
┌─────────────────────────────────────────────────────────────────────┐
│ dotnet-api (Minimal API, .NET 10)                                    │
│   8 endpoints REST → calls Backend via gRPC                         │
└──────────────────────────┬──────────────────────────────────────────┘
                           │ gRPC :5100 (HTTP/2)
                           ▼
┌─────────────────────────────────────────────────────────────────────┐
│ dotnet-backend (gRPC Server, .NET 8)                                 │
│   3 RPCs: ProcessOrder, CancelOrder, SlowOperation                  │
│   Simulates: DB queries + external call (httpbin.org)                │
└─────────────────────────────────────────────────────────────────────┘
```

---

## How to Use the Lib (pattern demonstrated)

### 1. Register telemetry (Program.cs)

```csharp
// Minimal — everything via env vars
services.AddOtelHelper();

// With overrides
services.AddOtelHelper(opts =>
{
    opts.ResourceAttributes = new Dictionary<string, object>
    {
        ["app.component"] = "api-gateway"
    };
    opts.AdditionalActivitySources = new List<string>
    {
        "my-app.module-x"
    };
});
```

After registration, available via DI:
- `ActivitySource` — for creating manual spans (name = the resolved `service.name`; keyed service, inject with `[FromKeyedServices(OtelHelperKeys.Instrumentation)]`)
- `Meter` — for creating custom metrics (name = the resolved `service.name`; keyed, same key)

How the examples use the 0.3 API (task C17):

| Example | Spans | Metrics |
|---|---|---|
| `dotnet-api` | the keyed `ActivitySource` (`app.Services.GetRequiredKeyedService<ActivitySource>(OtelHelperKeys.Instrumentation)`) | the keyed `Meter` |
| `dotnet-backend` | named sources `sample-backend.db`, `sample-backend.external`, listed in `AdditionalActivitySources` | a fixed meter `sample-backend.orders`, listed in `AdditionalMeters` (a gRPC service instance lives for one call, so it does not take the keyed `Meter`) |
| `dotnet-process` | named sources `sample-process.*`, listed in `AdditionalActivitySources` | the keyed `Meter`, injected into each worker with `[FromKeyedServices(OtelHelperKeys.Instrumentation)] Meter meter` |
| `dotnet-custom-signals` | named source `orders`, listed in `AdditionalActivitySources` | named meter `orders`, listed in `AdditionalMeters` |

A source or meter whose name is not the resolved `service.name` is exported only
when it is listed; an unkeyed `ActivitySource` injected in a web app resolves to
ASP.NET Core's own source (spans exported under the `Microsoft.AspNetCore` scope),
in a worker it is not registered at all. Before 0.3 the examples named their
meter after `SERVICE_NAME`; with `OTEL_SERVICE_NAME` set to another value that
meter was not exported.

### 2. Create spans in APIs (child of request)

```csharp
using var activity = activitySource.StartActivity("api.get-order");
activity?.SetTag("order.id", id);
```

### 3. Create spans in Workers (independent root)

```csharp
using StaffOps.OtelHelper;

using var span = activitySource.StartRootActivity("process-api-order");
span?.SetTag("endpoint", "/order/1");
```

### 4. Errors on spans (ADR-014)

The examples keep the failure and its type on the span and write the exception
detail to a log record in the span's context, which carries the same
`trace_id`/`span_id`:

```csharp
catch (Exception ex)
{
    span?.SetStatus(ActivityStatusCode.Error);          // no description: it could carry data
    span?.SetTag("error.type", ex.GetType().FullName);
    _logger.LogError(ex, "Message {MessageId} processing failed", messageId);
}
```

The log record gets `exception.type`, `exception.message` and
`exception.stacktrace` (OTLP; on the stdout line one `exception` field). A fixed description that names no data (for example
`"Job timed out"`) stays on the status. `error.type` is the exception's
`FullName` here, the semantic conventions' "canonical class name" (a bounded
set: one value per exception type); `dotnet-custom-signals` writes the short
`Name` (`PaymentDeclinedException`) to match its Go, Python and Node.js twins.
Pick one form per service: a dashboard grouping by `error.type` sees the two
as different values.

### 5. Metrics

```csharp
var counter = meter.CreateCounter<long>("orders.received_total", "orders");
counter.Add(1, new KeyValuePair<string, object?>("source", "http-get"));
```

---

## Environment Variables

Every example reads the helper's variables ([../README.md](../README.md#environment-variables)).
The ones the examples are run with:

| Variable | Description | Example |
|---|---|---|
| `OTEL_SERVICE_NAME` | Service name (wins over the deprecated `SERVICE_NAME`) | `sample-api` |
| `OTEL_RESOURCE_ATTRIBUTES` | `deployment.environment.name` (controls the log level; the deprecated `ENVIRONMENT` still works and warns) | `deployment.environment.name=DEV` |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | Collector endpoint; unset → logs as JSON on stdout, metrics on `:9464/metrics`, no traces exported | `http://collector:4317` |

Example-specific:

| Example | Variable | Description | Default |
|---|---|---|---|
| `dotnet-api` | `BACKEND_URL` | Backend URL (gRPC) | `http://localhost:5100` |
| `dotnet-process` | `API_URL` | dotnet-api URL | `http://localhost:5050` |

### gRPC server spans (O7)

`dotnet-backend/appsettings.json` sets
`OTEL_DOTNET_EXPERIMENTAL_ASPNETCORE_ENABLE_GRPC_INSTRUMENTATION=true`: with it
the backend's server spans are RPC-shaped (`order.OrderService/ProcessOrder`,
`rpc.system.name`, `rpc.method`, `rpc.response.status_code`); without it they
stay HTTP-shaped (`POST`, status from HTTP 200) and the helper prints one
warning at start (owner decision O7 (b), [HOW-TO §3](../HOW-TO.md#grpc-server-spans-set-the-upstream-key-03-this-branch-task-c11)).
The key is read from the app's `IConfiguration`, so an environment variable of
the same name works too.

### Optional (all)

| Variable | Description | Default |
|---|---|---|
| `OTEL_HELPER_DEBUG_LEVEL` | Debug mode: Debug log + `debug=true` attribute on entry spans (no in-process parent, including spans continuing a remote trace) and prepended to the propagated tracestate | `false` |
| `OTEL_HELPER_EXTRA_INSTRUMENTATION` | No effect in core 0.2.x — use the `StaffOps.OtelHelper.AWS` / `StaffOps.OtelHelper.Sql` subpackages. | `SQL` |

---

## Build Images

From the dotnet/ directory:

```bash
docker build -f example/dotnet-api/Dockerfile -t sample/dotnet-api:latest .
docker build -f example/dotnet-backend/Dockerfile -t sample/dotnet-backend:latest .
docker build -f example/dotnet-process/Dockerfile -t sample/dotnet-process:latest .
docker build -f example/dotnet-custom-signals/Dockerfile -t sample/dotnet-custom-signals:latest .
```

The images run the helper **package**, as a consumer gets it, not a project
reference (task C17):

- `Dockerfile`: stage `pack` runs `dotnet pack` on `StaffOps.OtelHelper` from
  this tree into `/packages`; the build stage restores the example with
  `-p:OtelHelperVersion=<version>` and
  [`nuget.packages.config`](https://github.com/StaffOps/otel-libs/blob/main/dotnet/example/nuget.packages.config), which takes
  `StaffOps.OtelHelper*` only from `/packages` and everything else from
  nuget.org.
- `Dockerfile.demo` (what CI's `example-images` job builds): the same, with the
  packages CI packed from the commit (`build-lib` artifact, copied from
  `dotnet/packages/`) instead of the `pack` stage.

Each csproj switches on `OtelHelperVersion`: unset (the default, `dotnet run`
from the repository) it keeps the project reference; set, it is a
`PackageReference`. A build that finds zero or more than one
`StaffOps.OtelHelper.<version>.nupkg` in `/packages` fails with
`expected exactly one StaffOps.OtelHelper.<version>.nupkg in /packages, found: …`
(`testing/e2e/run-dotnet.sh` prefixes `run-dotnet: `); a restore that cannot find the
version in the local feed fails with `NU1101`/`NU1102` instead of falling back
to another feed.

---

## Local Test (Docker)

Against the repo's test Collector (`testing/docker-compose.yml`, debug exporter
with `verbosity: detailed`, which prints everything it receives):

```bash
docker compose -f testing/docker-compose.yml --profile e2e up -d collector
net=otel-libs-testing_default
otel="-e OTEL_RESOURCE_ATTRIBUTES=deployment.environment.name=DEV -e OTEL_EXPORTER_OTLP_ENDPOINT=http://collector:4317"

docker run -d --name dotnet-backend --network $net $otel -e OTEL_SERVICE_NAME=sample-backend sample/dotnet-backend:latest
docker run -d --name dotnet-api --network $net $otel -e OTEL_SERVICE_NAME=sample-api \
  -e BACKEND_URL=http://dotnet-backend:5100 -p 8080:8080 sample/dotnet-api:latest
docker run -d --name dotnet-process --network $net $otel -e OTEL_SERVICE_NAME=sample-process \
  -e API_URL=http://dotnet-api:8080 sample/dotnet-process:latest

curl http://localhost:8080/order/42
# {"orderId":42,"status":"completed","enriched":true,"processedBy":"sample-backend"}

# Cleanup
docker rm -f dotnet-api dotnet-backend dotnet-process
docker compose -f testing/docker-compose.yml --profile e2e stop collector
```

Measured 2026-11-12 (task C17): one `process.api-order` cycle of
`dotnet-process` is one trace across the three services, nine spans:

```text
sample-process  process.api-order                    Internal
sample-process  GET                                  Client
sample-api      GET /order/{id}                      Server
sample-api      api.get-order                        Internal
sample-api      order.OrderService/ProcessOrder      Client
sample-backend  order.OrderService/ProcessOrder      Server   (rpc.system.name grpc, rpc.method, rpc.response.status_code OK)
sample-backend  backend.db-query-order               Internal
sample-backend  backend.external-enrich-order        Internal
sample-backend  GET                                  Client
```

`GET /retry/7` gives a server span `order.OrderService/UnstableOperation` with
`rpc.response.status_code` `UNAVAILABLE` on the failed attempts; the API's
`api.retry-attempt` spans carry `error.type` `Unavailable` and no status
description, the gRPC detail text is in the WARN log of the same span.
The backend prints no gRPC warning (the O7 key is set).

---

## Custom signals (`dotnet-custom-signals`)

[`dotnet-custom-signals/Program.cs`](https://github.com/StaffOps/otel-libs/blob/main/dotnet/example/dotnet-custom-signals/Program.cs)
processes a batch of three simulated orders (`ord-3` fails) and exits, as
`go-custom-signals`, `python-custom-signals` and `node-custom-signals`: a root
span (`StartRootActivity`) with one child span per order (attributes, an event,
the failure's type and status), a counter, a histogram with second-based
buckets, an up-down counter and an observable gauge, and `ILogger` records
correlated with the current span. What it emits, signal by signal:
[EXAMPLES.md](EXAMPLES.md#custom-signals-dotnet-custom-signals).

```bash
docker build -t dotnet-custom-signals -f dotnet/example/dotnet-custom-signals/Dockerfile dotnet

docker compose -f testing/docker-compose.yml --profile e2e up -d collector
docker run --rm --network otel-libs-testing_default \
  -e OTEL_SERVICE_NAME=dotnet-custom-signals \
  -e OTEL_RESOURCE_ATTRIBUTES=deployment.environment.name=DEV \
  -e OTEL_EXPORTER_OTLP_ENDPOINT=http://collector:4317 \
  dotnet-custom-signals

# The Collector log keeps every earlier run: filter on the printed trace_id.
docker compose -f testing/docker-compose.yml --profile e2e logs --no-log-prefix --tail 3000 collector \
  | grep -B 4 -A 30 <trace_id>
docker compose -f testing/docker-compose.yml --profile e2e stop collector

# Without a Collector: JSON log lines on stdout, no traces
docker run --rm -e OTEL_SERVICE_NAME=dotnet-custom-signals dotnet-custom-signals
```

The job prints `trace_id=<id>` first and flushes with
`ForceFlushOtelHelper(5000)` before the host stops. Without a Collector
(recorded 2026-11-12 after C17 fix round 1, stack trimmed):

```text
otelhelper: WARN deployment.environment.name is not set; set it in OTEL_RESOURCE_ATTRIBUTES (e.g. deployment.environment.name=PRD); log level defaults to INFO using=absent
trace_id=9d1f3bdfc7343b71aea94526ddf9ca6b
{"time":"2026-11-12T20:28:25.326Z","level":"INFO","msg":"order processed","logger":"dotnet-custom-signals","trace_id":"9d1f3bdfc7343b71aea94526ddf9ca6b","span_id":"95703216a608f53b","order.id":"ord-1","order.amount":42.5}
{"time":"2026-11-12T20:28:25.361Z","level":"INFO","msg":"order processed","logger":"dotnet-custom-signals","trace_id":"9d1f3bdfc7343b71aea94526ddf9ca6b","span_id":"0ddfd6f4b54f71a9","order.id":"ord-2","order.amount":99.9}
{"time":"2026-11-12T20:28:25.383Z","level":"ERROR","msg":"order failed","logger":"dotnet-custom-signals","trace_id":"9d1f3bdfc7343b71aea94526ddf9ca6b","span_id":"9a3ced9425fb1e34","order.id":"ord-3","exception":"PaymentDeclinedException: card declined by issuer\n   at OrderJob.Charge(Order order) …"}
{"time":"2026-11-12T20:28:25.406Z","level":"INFO","msg":"batch finished","logger":"dotnet-custom-signals","trace_id":"9d1f3bdfc7343b71aea94526ddf9ca6b","span_id":"fb594123d6d21e3a","batch.size":3,"orders.failed":1}
```

The first line is a diagnostic (stderr). Unlike Go, Python and Node.js, the
.NET stdout fallback keeps `trace_id`/`span_id`. The attributes (`order.id`,
`order.amount`, `batch.size`, `orders.failed`) are the record's state, so they
are fields here and attributes on OTLP; the exception is one `exception` field
here and `exception.type`/`exception.message`/`exception.stacktrace` on OTLP.
Metrics are served on `:9464/metrics` only while the process runs.

---

## Endpoints — dotnet-api

| Method | Endpoint | Span Name | What it does |
|---|---|---|---|
| GET | `/` | — | Simple health check (no Backend) |
| GET | `/order/{id}` | `api.get-order` | gRPC → Backend ProcessOrder (DB + external) |
| POST | `/order` | `api.create-order` | JSON Body → gRPC → Backend ProcessOrder |
| GET | `/order/{id}/cancel` | `api.cancel-order` | gRPC → Backend CancelOrder (DB) |
| GET | `/slow` | `api.slow-operation` | gRPC → Backend SlowOperation (4-7s) |
| GET | `/batch` | `api.batch-orders` | 5× gRPC → Backend ProcessOrder (sequential) |
| GET | `/error` | `api.error-simulated` | Throws exception → span with ERROR |
| GET | `/health/ready` | `api.readiness-check` | gRPC → Backend as probe |

---

## Minimal Example — Individual Project

For a real service (own repo), the complete setup is:

**Program.cs:**
```csharp
using StaffOps.OtelHelper;

var builder = WebApplication.CreateBuilder(args);
builder.Services.AddOtelHelper();
var app = builder.Build();

// Your endpoints here
app.Run();
```

**Dockerfile:**
```dockerfile
FROM mcr.microsoft.com/dotnet/sdk:8.0 AS build
WORKDIR /src
COPY *.csproj .
RUN dotnet restore
COPY . .
RUN dotnet publish -c Release -o /app

FROM mcr.microsoft.com/dotnet/aspnet:8.0
WORKDIR /app
COPY --from=build /app .
ENTRYPOINT ["dotnet", "MyService.dll"]
```

**Result:** traces, metrics, and logs exported automatically. Zero instrumentation code beyond `AddOtelHelper()`.

---

## File Structure (this example)

```
example/
├── Protos/
│   └── order.proto                         # Shared proto (5 RPCs, namespace Examples.Grpc)
├── dotnet-api/
│   ├── dotnet-api.csproj                   # net10.0, Grpc.Net.Client
│   ├── Program.cs                          # 8 endpoints, gRPC client, metrics
│   ├── Dockerfile                          # SDK 10.0, aspnet 10.0, port 8080
│   └── Dockerfile.demo                     # CI: the build-lib packages from dotnet/packages/
├── nuget.packages.config                   # StaffOps.OtelHelper* from /packages only, the rest from nuget.org
├── dotnet-backend/
│   ├── dotnet-backend.csproj               # net8.0, Grpc.AspNetCore
│   ├── Program.cs                          # Minimal — registers gRPC service
│   ├── Services/
│   │   └── OrderGrpcService.cs             # 5 RPCs, manual spans, metrics
│   ├── appsettings.json                    # Kestrel HTTP/2 on port 5100
│   ├── Dockerfile                          # SDK 8.0, aspnet 8.0, port 5100
│   └── Dockerfile.demo                     # CI
└── dotnet-process/
    ├── dotnet-process.csproj               # net8.0, Worker SDK
    ├── Program.cs                          # Registers workers + HttpClient
    ├── ApiHealthWorker.cs                  # 6 checks/min, StartRootActivity
    ├── HeavyProcessWorker.cs              # CPU/memory stress, StartRootActivity
    ├── QueueConsumerWorker.cs             # Queue consumer pattern
    ├── ScheduledJobWorker.cs              # Scheduled job with circuit breaker
    ├── Dockerfile                          # SDK 8.0, aspnet 8.0 (packs the helper)
    └── Dockerfile.demo                     # CI
└── dotnet-custom-signals/
    ├── dotnet-custom-signals.csproj        # net8.0 console app
    ├── Program.cs                          # custom spans, metrics, logs (Go/Python/Node names)
    └── Dockerfile                          # SDK 8.0, aspnet 8.0 (packs the helper)
```

Every `Dockerfile` packs the helper in a `pack` stage; see [Build Images](#build-images).
