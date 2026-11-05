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
| `ENVIRONMENT` | Helm Chart (`values.yaml`) | Environment: `LOCAL`, `DEV`, `HML`, `PRD`. Unrecognized value → `LOCAL`. | `LOCAL` |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | Helm Chart | Collector base host | `http://localhost` |

> These variables are injected automatically. Application teams **do not need to configure them manually**.

### Optional

| Variable | Description | Default |
|---|---|---|
| `OTEL_HELPER_DEBUG_LEVEL` | Debug mode: forces Debug log level, all instrumentations, attribute debug=true (`true`/`false`) | `false` |
| `OTEL_HELPER_METRICS_PORT` | Prometheus `/metrics` port when no OTLP endpoint is configured | `9464` |

### Extra Instrumentation

| Variable | Description | Default |
|---|---|---|
| `OTEL_HELPER_EXTRA_INSTRUMENTATION` | Extra instrumentations: `SQL`, `AWS`. **Deprecated** — use opt-in subpackages instead. | `SQL` |
| `OTEL_HELPER_SAMPLE_RATIO` | Head sampling ratio (0.0-1.0). 1.0 = AlwaysOn. | `1.0` |

> Debug mode (`OTEL_HELPER_DEBUG_LEVEL=true`) enables all extra instrumentations automatically.

> ⚠️ **`OTEL_HELPER_DEBUG_LEVEL=true` in production causes cost explosion and backend saturation.** Use only for targeted troubleshooting.

### Derived Ports

| Signal | Port | Resulting Endpoint |
|---|---|---|
| OTLP (traces/metrics/logs) | `:4317` | `{OTEL_EXPORTER_OTLP_ENDPOINT}:4317` |

### TLS / Transport Security

Transport is determined by the endpoint scheme:

| Endpoint | Transport |
|----------|-----------|
| `https://host:4317` | TLS (system CA trust store) |
| `http://host:4317` | Plaintext (insecure) |
| `host:4317` (no scheme) | TLS (secure by default) |

Override with `OTEL_EXPORTER_OTLP_INSECURE`: `true` forces plaintext, `false` forces TLS. Code config (`TelemetryOptions.OtelCollectorEndpoint` scheme) always wins over env var.

For a local plaintext collector, use `http://` in the endpoint or set `OTEL_EXPORTER_OTLP_INSECURE=true`.

### Behavior when no OTLP endpoint is configured

When `OTEL_EXPORTER_OTLP_ENDPOINT` is **not set**, the library automatically falls back to:

| Signal | Behavior |
|--------|----------|
| Metrics | Exposed via Prometheus HTTP `/metrics` on port 9464 |
| Traces | In-process only (context propagation works, no export) |
| Logs | stdout/console only (no OTel export) |

The Prometheus metrics port is configurable via `OTEL_HELPER_METRICS_PORT` env var (default: 9464).

This enables the standard Kubernetes pattern: deploy without a collector, and let Prometheus/VictoriaMetrics scrape `/metrics` directly from the pod.


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
| `OTEL_EXPORTER_OTLP_INSECURE` | TLS override: `true` = plaintext, `false` = TLS. Unset = derived from endpoint scheme (secure by default). | _(unset)_ |
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

Metrics are exported every **30 seconds** (overrides SDK default of 60s).

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

## Opt-in Subpackages

AWS, Redis, SQL, and Profiling instrumentations are **no longer bundled in core**. Install the subpackage you need:

| Package | Registration | What it instruments |
|---------|--------------|---------------------|
| `OtelHelper.AWS` | `services.AddOtelHelperAws()` | AWS SDK calls (S3, SQS, DynamoDB, etc.) |
| `OtelHelper.Redis` | `services.AddOtelHelperRedis(multiplexer)` | StackExchange.Redis commands |
| `OtelHelper.Sql` | `services.AddOtelHelperSql()` | SqlClient queries |
| `OtelHelper.Profiling` | `services.AddOtelHelperProfiling()` | Pyroscope continuous profiling |

### Usage

```csharp
services.AddOtelHelper();

// Add only the instrumentations you need:
services.AddOtelHelperAws();
services.AddOtelHelperRedis(connectionMultiplexer); // pass the IConnectionMultiplexer
services.AddOtelHelperSql();
```

> **Known issue in 0.2.x — see [CHANGELOG](../CHANGELOG.md):** the
> no-argument overload `services.AddOtelHelperRedis()` (resolves
> `IConnectionMultiplexer` from DI) throws `NotSupportedException` when the
> tracer provider is built. Pass the multiplexer explicitly as above.

### Profiling (Pyroscope)

```csharp
services.AddOtelHelperProfiling();
```

Requires these environment variables set on the container:

```yaml
env:
  - name: CORECLR_ENABLE_PROFILING
    value: "1"
  - name: CORECLR_PROFILER
    value: "{BD1A650D-AC5D-4896-B64F-D6FA25D6B26A}"
  - name: CORECLR_PROFILER_PATH
    value: "/opt/pyroscope/Pyroscope.Profiler.Native.so"
  - name: LD_PRELOAD
    value: "/opt/pyroscope/Pyroscope.Linux.ApiWrapper.x64.so"
```

> **Note**: `OTEL_HELPER_EXTRA_INSTRUMENTATION` env var still works for backward compatibility but subpackages are the recommended approach.

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
# SDK 10 builds both TFMs (SDK 8 cannot, NETSDK1045). This runs the net10.0 leg;
# the net8.0 leg needs the .NET 8 runtime installed next to SDK 10 (as CI does).
docker run --rm -v "$(pwd):/src" -w /src mcr.microsoft.com/dotnet/sdk:10.0 dotnet test OtelHelper.Tests -f net10.0
```

Coverage: see [TESTS.md](TESTS.md) for the full list.

### Examples

See [example/README.md](example/README.md) for sample apps with distributed traces.

---

## Supported Targets

Since `0.2.1` every package ships two assets: `lib/net8.0` and `lib/net10.0`.

- .NET 8 (LTS) — `net8.0` asset
- .NET 10 (LTS) — `net10.0` asset (no roll-forward needed)

Building the libraries from source needs the .NET 10 SDK (SDK 8 cannot build
the `net10.0` target, `NETSDK1045`).

### .NET 10 Note

`net10.0` apps get the `net10.0` asset directly; `DOTNET_ROLL_FORWARD` is no
longer needed. On a given runtime both assets behave the same (the helper has
no target-framework branches). Moving an app to the .NET 9+/10 **runtime**
changes three things in the emitted telemetry; they come from the runtime and
the upstream instrumentation, not from the helper, and were already true for
`0.2.0` running under roll-forward:

- **Runtime metrics** come from the built-in `System.Runtime` meter and are
  named `dotnet.*` (for example `dotnet.gc.collections`) instead of
  `process.runtime.dotnet.*`. Update dashboards and alerts that query the old
  names.
- **HttpClient `url.full`**: query redaction changes from per-value
  `?key=Redacted` to `?*` (the runtime's own redaction); userinfo was already
  removed on .NET 8.
- **ASP.NET Core 10** adds meters that the ASP.NET Core instrumentation
  subscribes to: `Microsoft.AspNetCore.Authorization`, `.Authentication`,
  `.Identity` and `.MemoryPool`, and the Blazor Components meters.

Runtime image: use `aspnet` (not `runtime`) — the lib depends on
`Microsoft.AspNetCore.App` for ASP.NET Core instrumentation.

The Profiling package (`OtelHelper.Profiling`) is not validated on .NET 10:
it loads the native Pyroscope profiler, which the test suite does not exercise.

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
