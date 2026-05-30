# OtelHelper — OpenTelemetry Helper for .NET

Observability library for .NET 8+ applications. Abstracts OpenTelemetry SDK configuration to provide standardized instrumentation.

📖 **[HOW-TO.md](HOW-TO.md)** — Developer guide (logs, traces, metrics, examples)
🧪 **[TESTS.md](TESTS.md)** — Full list of unit tests
🚀 **[example/README.md](example/README.md)** — Sample apps with test endpoints and Grafana queries

## Quick Start

```csharp
// Everything resolved via env vars — zero config in code
services.AddOtelHelper();
```

Optionally, with overrides:

```csharp
services.AddOtelHelper(opts =>
{
    opts.ServiceName = "checkout-api";
});
```

After registration, available via DI:
- `ActivitySource` — for creating manual spans
- `Meter` — for creating custom metrics
- `ActivitySourceExtensions.StartRootActivity()` — for independent traces in workers

---

## Environment Variables

### Required (injected automatically by infrastructure)

| Variable | Source | Description | Default |
|---|---|---|---|
| `SERVICE_NAME` | CI/CD Pipeline | Service name | `my-service` |
| `ENVIRONMENT` | Helm Chart (`values.yaml`) | Environment: `LOCAL`, `DEV`, `HML`, `PRD`, `BTC`. Unrecognized value → `LOCAL`. | `LOCAL` |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | Helm Chart | Collector base host | `http://localhost` |

> These variables are injected automatically. Application teams **do not need to configure them manually**.

### Optional

| Variable | Description | Default |
|---|---|---|
| `OTEL_HELPER_DEBUG_LEVEL` | Debug mode: forces Debug log level, all instrumentations, attribute debug=true (`true`/`false`) | `false` |

### Extra Instrumentation

| Variable | Description | Default |
|---|---|---|
| `OTEL_HELPER_EXTRA_INSTRUMENTATION` | Extra instrumentations: `SQL`, `AWS`. | `SQL` |
| `OTEL_HELPER_SAMPLE_RATIO` | Head sampling ratio (0.0-1.0). 1.0 = AlwaysOn. | `1.0` |

> Debug mode (`OTEL_HELPER_DEBUG_LEVEL=true`) enables all extra instrumentations automatically.

> ⚠️ **`OTEL_HELPER_DEBUG_LEVEL=true` in production causes cost explosion and backend saturation.** Use only for targeted troubleshooting.

### Derived Ports

| Signal | Port | Resulting Endpoint |
|---|---|---|
| OTLP (traces/metrics/logs) | `:4317` | `{OTEL_EXPORTER_OTLP_ENDPOINT}:4317` |


### Standard OpenTelemetry SDK Variables

Recognized natively by the SDK. The lib does not override them — if defined, the SDK respects them.

#### Sampling

| Variable | Description | Default |
|---|---|---|
| `OTEL_TRACES_SAMPLER` | SDK sampler. | `parentbased_always_on` |
| `OTEL_TRACES_SAMPLER_ARG` | Sampler argument. | empty |

#### Context Propagation

| Variable | Description | Default |
|---|---|---|
| `OTEL_PROPAGATORS` | W3C propagators. | `tracecontext,baggage` |

#### OTLP Exporter

| Variable | Description | Default |
|---|---|---|
| `OTEL_EXPORTER_OTLP_PROTOCOL` | OTLP protocol. | `grpc` |
| `OTEL_EXPORTER_OTLP_HEADERS` | Additional headers (e.g., auth). | empty |
| `OTEL_EXPORTER_OTLP_TIMEOUT` | Timeout in ms. | `10000` |

---

## Behavior per Environment

| Environment | Trace Sampling | Log Level |
|---|---|---|
| `LOCAL` | 100% (AlwaysOn) | Debug |
| `DEV` | 100% (AlwaysOn) | Information |
| `HML` | 100% (AlwaysOn) | Information |
| `PRD` | 100% (AlwaysOn) | Warning |
| `BTC` | 100% (AlwaysOn) | Warning |

> The SDK sends 100% of traces to the Collector in all environments. **Tail-based sampling is the Collector's responsibility** (Agent → Gateway), which decides what to keep based on errors, latency, and configured rate per environment.

---

## What is Instrumented Automatically

### Traces

| Instrumentation | What it captures |
|---|---|
| ASP.NET Core | Incoming HTTP requests (server spans) |
| HttpClient | Outgoing HTTP requests (client spans) |
| gRPC Client | Outgoing gRPC calls |
| SqlClient | SQL queries — requires `SQL` in `OTEL_HELPER_EXTRA_INSTRUMENTATION` (enabled by default) |
| AWS SDK | S3, SQS, DynamoDB calls, etc. — requires `AWS` in `OTEL_HELPER_EXTRA_INSTRUMENTATION` |
| Custom Sources | Spans created via `ActivitySource(serviceName)` |

Sampler: AlwaysOnSampler in all environments (configurable via `opts.Sampler`). Tail-based sampling is done in the Collector.

> Paths `/ping`, `/health`, `/healthz`, `/ready` are filtered automatically — they do not generate trace spans.

### Metrics

| Instrumentation | What it captures |
|---|---|
| .NET Runtime | GC, thread pool, JIT, assemblies |
| ASP.NET Core | Request duration, active requests, request size |
| HttpClient | Outbound request duration, active requests |
| Custom Meters | Business metrics via `Meter(serviceName)` |

Exemplars enabled (`ExemplarFilterType.TraceBased`) — metric → trace correlation in Grafana.

### Logs

| Feature | Description |
|---|---|
| OTLP Export | Logs sent via OTLP to the Collector |
| Trace Correlation | `traceId`/`spanId` automatic via `Activity.Current` |
| Formatted Message | Formatted message included |
| Scopes | ILogger scopes included |

> The lib **only adds** the OTLP exporter to the logging pipeline. Console logging and filters are the application's responsibility.

---

## Library Options (`TelemetryOptions`)

| Property | Type | Env Var | Default |
|---|---|---|---|
| `ServiceName` | string | `SERVICE_NAME` / `OTEL_SERVICE_NAME` | `my-service` |
| `Environment` | DeploymentEnvironment | `ENVIRONMENT` | `LOCAL` |
| `OtelCollectorEndpoint` | string | `OTEL_EXPORTER_OTLP_ENDPOINT` | `http://localhost:4317` |
| `DebugLevel` | bool | `OTEL_HELPER_DEBUG_LEVEL` | `false` |
| `ExportTimeoutMs` | int | — | `10000` (10s) |
| `ExtraInstrumentation` | string | `OTEL_HELPER_EXTRA_INSTRUMENTATION` | `SQL` |
| `Sampler` | Sampler | — | `AlwaysOnSampler` |
| `MinimumLogLevel` | LogLevel? | — | `null` (auto per environment) |
| `ResourceAttributes` | Dictionary<string, object> | — | empty |
| `AdditionalActivitySources` | List\<string\> | — | empty |

> `ServiceName` resolves: `SERVICE_NAME` > `OTEL_SERVICE_NAME` > `"my-service"`.
> `MinimumLogLevel` when null: LOCAL=Debug, DEV/HML=Information, PRD=Warning. Use `GetDefaultLogLevel()` to query.
> `Sampler` default is AlwaysOnSampler — override only if you have a specific reason.
> Env vars are applied via `IPostConfigureOptions` — consumer overrides take priority.

---

## Architecture

```
Application (.NET SDK) → OTLP gRPC :4317 → Agent Collector → Gateway Collector → Tempo / VictoriaMetrics / Loki
```

All telemetry goes through the Collector. The SDK does NOT export directly to backends.

---

## Tests

### Run unit tests

```bash
docker run --rm -v "$(pwd):/src" -w /src mcr.microsoft.com/dotnet/sdk:8.0 dotnet test OtelHelper.Tests
```

Coverage: see [TESTS.md](TESTS.md) for the full list.

### Examples

See [example/README.md](example/README.md) for sample apps with distributed traces.

---

## Supported Targets

- .NET 8 (LTS)
- .NET 10 (LTS) — via roll-forward

### .NET 10 Note

The lib is compiled for `net8.0` and runs on .NET 10 via roll-forward. Requirements:

- **Runtime image**: use `aspnet` (not `runtime`) — the lib depends on `Microsoft.AspNetCore.App` for ASP.NET Core instrumentation
- **Roll-forward**: add `DOTNET_ROLL_FORWARD=Major` env var if using a custom image:
  ```dockerfile
  ENV DOTNET_ROLL_FORWARD=Major
  ```

---

## Project Structure

```
OtelHelper.sln
├── OtelHelper/                         # Main library
│   ├── TelemetryExtensions.cs          # Entry point AddOtelHelper()
│   ├── ActivitySourceExtensions.cs     # StartRootActivity() for workers
│   ├── TracerSetup.cs                  # Tracing + health filter + sampler
│   ├── Tracing/
│   │   └── DebugTraceStateProcessor.cs # Injects tracestate debug=true for tail sampling
│   ├── MetricsSetup.cs                 # Metrics with exemplars
│   ├── LoggingSetup.cs                 # Logging via OpenTelemetry SDK
│   ├── Models/
│   │   ├── DeploymentEnvironment.cs    # Environment enum
│   │   ├── TelemetryOptions.cs         # Configuration POCO
│   │   ├── TelemetryOptionsPostConfigure.cs  # Env var resolution
│   │   └── TelemetryOptionsValidator.cs      # Startup validation
├── OtelHelper.Tests/                   # Unit tests (xUnit)
└── example/                            # Sample apps
    ├── dotnet-api/        # API frontend
    ├── dotnet-backend/    # Backend with DB + external call
    └── dotnet-process/    # Background worker
```
