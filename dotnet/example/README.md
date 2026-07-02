# OtelHelper — Examples

📖 **[EXAMPLES.md](EXAMPLES.md)** — Detailed description of each check, internals, and Grafana queries

Three services demonstrating complete instrumentation with `OtelHelper`:

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
- `ActivitySource` — for creating manual spans (name = ServiceName)
- `Meter` — for creating custom metrics (name = ServiceName)

### 2. Create spans in APIs (child of request)

```csharp
using var activity = activitySource.StartActivity("api.get-order");
activity?.SetTag("order.id", id);
```

### 3. Create spans in Workers (independent root)

```csharp
using OtelHelper;

using var span = activitySource.StartRootActivity("process-api-order");
span?.SetTag("endpoint", "/order/1");
```

### 4. Metrics

```csharp
var counter = meter.CreateCounter<long>("orders.received_total", "orders");
counter.Add(1, new KeyValuePair<string, object?>("source", "http-get"));
```

---

## Environment Variables

### dotnet-api

| Variable | Required | Description | Example |
|---|---|---|---|
| `SERVICE_NAME` | Yes | Service name | `dotnet-api` |
| `ENVIRONMENT` | Yes | Environment (controls log level) | `DEV` |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | No | Collector host | `http://localhost` |
| `BACKEND_URL` | Yes | Backend URL (gRPC) | `http://dotnet-backend:5100` |

### dotnet-backend

| Variable | Required | Description | Example |
|---|---|---|---|
| `SERVICE_NAME` | Yes | Service name | `dotnet-backend` |
| `ENVIRONMENT` | Yes | Environment | `DEV` |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | No | Collector host | `http://localhost` |

### dotnet-process

| Variable | Required | Description | Example |
|---|---|---|---|
| `SERVICE_NAME` | Yes | Service name | `dotnet-process` |
| `ENVIRONMENT` | Yes | Environment | `DEV` |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | No | Collector host | `http://localhost` |
| `API_URL` | Yes | dotnet-api URL | `http://dotnet-api:8080` |

### Optional (all)

| Variable | Description | Default |
|---|---|---|
| `OTEL_HELPER_DEBUG_LEVEL` | Debug mode: Debug log + 100% sampling + tracestate debug=true | `false` |
| `OTEL_HELPER_EXTRA_INSTRUMENTATION` | Legacy extra instrumentations (`SQL`, `AWS`). Prefer the `OtelHelper.AWS` / `OtelHelper.Sql` subpackages. | `SQL` |

---

## Build Images

From the dotnet/ directory:

```bash
docker build -f example/dotnet-api/Dockerfile -t sample/dotnet-api:latest .
docker build -f example/dotnet-backend/Dockerfile -t sample/dotnet-backend:latest .
docker build -f example/dotnet-process/Dockerfile -t sample/dotnet-process:latest .
```

---

## Local Test (Docker)

```bash
# Create network
docker network create sample-net

# Backend (gRPC on port 5100)
docker run -d --name dotnet-backend --network sample-net \
  -e SERVICE_NAME=dotnet-backend \
  -e ENVIRONMENT=DEV \
  sample/dotnet-backend:latest

# API (HTTP on port 8080, connects to Backend via gRPC)
docker run -d --name dotnet-api --network sample-net \
  -e SERVICE_NAME=dotnet-api \
  -e ENVIRONMENT=DEV \
  -e BACKEND_URL=http://dotnet-backend:5100 \
  -p 8080:8080 \
  sample/dotnet-api:latest

# Test
curl http://localhost:8080/order/42
# {"orderId":42,"status":"completed","enriched":true,"processedBy":"dotnet-backend"}

# Cleanup
docker rm -f dotnet-api dotnet-backend && docker network rm sample-net
```

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
using OtelHelper;

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
│   └── order.proto                         # Shared proto (5 RPCs, namespace OtelHelper.Grpc)
├── dotnet-api/
│   ├── dotnet-api.csproj                   # net10.0, Grpc.Net.Client
│   ├── Program.cs                          # 8 endpoints, gRPC client, metrics
│   ├── Dockerfile                          # SDK 10.0, aspnet 10.0, port 8080
│   └── Dockerfile.demo                     # CI: NuGet package instead of ProjectReference
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
    └── Dockerfile                          # SDK 10.0, aspnet 10.0, DOTNET_ROLL_FORWARD
```
