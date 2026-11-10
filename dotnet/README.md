# StaffOps.OtelHelper — OpenTelemetry Helper for .NET

Observability library for .NET 8+ applications. Abstracts OpenTelemetry SDK configuration to provide standardized instrumentation.

> **Version note.** This page documents the shipped **0.2.x** .NET behavior,
> except where a row says "0.3 (this branch)": the identity and resource rules
> (task C6) already follow the 0.3 contract here.
> The `0.3.0` config contract (standard `OTEL_*` variables winning, no
> `LOCAL`/`my-service` defaults, ParentBased sampler, per-signal OTLP
> variables, the shared health-path list) ships in **Go and Python first**;
> .NET adopts it later, in Phase C of
> [`specs/config-contract-v0.3/`](https://github.com/StaffOps/otel-libs/blob/main/specs/config-contract-v0.3/tasks.md)
> (ADR-004). Until then the differences below are expected and tracked as
> `xfail` cases in the shared parity fixture.
>
> **Names.** On this branch the packages, assemblies and namespace are
> `StaffOps.OtelHelper*` (ADR-011, task C2 of
> [`specs/dotnet-otel-helper/`](https://github.com/StaffOps/otel-libs/blob/main/specs/dotnet-otel-helper/tasks.md));
> they are first published as `0.3.0-rc.1`. The released `0.2.x` packages
> keep the IDs `OtelHelper*` and `using OtelHelper;` — no compatibility
> package. Environment variable names and the `AddOtelHelper*()` entry points
> do not change.

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
- `ActivitySource` — for creating manual spans (keyed: `[FromKeyedServices(OtelHelperKeys.Instrumentation)]`)
- `Meter` — for creating custom metrics (keyed: `[FromKeyedServices(OtelHelperKeys.Instrumentation)]`)
- `ActivitySourceExtensions.StartRootActivity()` — for independent traces in workers

---

## Environment Variables

### Required (injected automatically by infrastructure)

| Variable | Source | Description | Default |
|---|---|---|---|
| `OTEL_SERVICE_NAME` | CI/CD Pipeline | Service name, emitted as `service.name`. 0.3 (this branch): wins over `service.name` in `OTEL_RESOURCE_ATTRIBUTES` and over `SERVICE_NAME` | none: `unknown_service:<process name>` + one warning |
| `SERVICE_NAME` | CI/CD Pipeline | Deprecated alias. 0.3 (this branch): read after `OTEL_SERVICE_NAME` and `service.name` in `OTEL_RESOURCE_ATTRIBUTES` (0.2.x: read first) | — |
| `OTEL_RESOURCE_ATTRIBUTES` | Helm Chart | `key=value,…` resource attributes, parsed by the helper (0.3, this branch): blank items ignored, an item without `=` or with an empty key skipped, values percent-decoded (`%XX` only; `+` stays `+`) then trimmed of ASCII whitespace, an invalid escape kept literally; problems warned once by item position, never the value. `deployment.environment.name` here wins over `ENVIRONMENT`. `telemetry.sdk.*` keys are ignored | — |
| `ENVIRONMENT` | Helm Chart (`values.yaml`) | Deprecated alias of `deployment.environment.name`. 0.3 (this branch): a known name (`LOCAL`, `DEV`, `HML`, `PRD`, any case) in its canonical spelling, any other value emitted as written; unset everywhere → the attribute is absent, one warning, log level Information (0.2.x: `LOCAL`) | none |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | Helm Chart | Collector endpoint. Unset → no OTLP export, Prometheus `/metrics` fallback | _(unset)_ |

> These variables are injected automatically. Application teams **do not need to configure them manually**.

### Optional

| Variable | Description | Default |
|---|---|---|
| `OTEL_HELPER_DEBUG_LEVEL` | Debug mode: forces Debug log level, all instrumentations, attribute debug=true (`true`/`false`) | `false` |
| `OTEL_HELPER_METRICS_PORT` | Prometheus `/metrics` port when no OTLP endpoint is configured | `9464` |

### Extra Instrumentation

| Variable | Description | Default |
|---|---|---|
| `OTEL_HELPER_EXTRA_INSTRUMENTATION` | **No effect.** Nothing in core uses it since SQL/AWS moved to opt-in subpackages (the code option `TelemetryOptions.ExtraInstrumentation` is `[Obsolete]`). Use the subpackages below. | `SQL` |
| `OTEL_HELPER_SAMPLE_RATIO` | Head sampling ratio, clamped to 0.0–1.0. Below 1.0 → a bare `TraceIdRatioBasedSampler` (not parent-based). Ignored when `OTEL_TRACES_SAMPLER` is set. | `1.0` |

> Debug mode (`OTEL_HELPER_DEBUG_LEVEL=true`) does **not** enable extra instrumentations; it sets the Debug log level and marks entry spans — spans with no in-process parent, which includes spans that continue a remote trace — with a `debug=true` attribute, and prepends `debug=true` to the tracestate they propagate (see HOW-TO §13). Combine the Collector's `debug=true` policy with an environment allow-list and a rate limit (design D11).

> ⚠️ **`OTEL_HELPER_DEBUG_LEVEL=true` in production causes cost explosion and backend saturation.** Use only for targeted troubleshooting.

### Derived Ports

| Signal | Port | Resulting Endpoint |
|---|---|---|
| OTLP (traces/metrics/logs) | `:4317` when the endpoint has no port (also for `https://host`) | `http://collector` → `http://collector:4317` |

### TLS / Transport Security

Transport is determined by the endpoint scheme:

| Endpoint | Transport |
|----------|-----------|
| `https://host:4317` | TLS (system CA trust store) |
| `http://host:4317` | Plaintext (insecure) |
| `host:4317` (no scheme) | TLS (secure by default) |

`OTEL_EXPORTER_OTLP_INSECURE` applies **only to schemeless endpoints**: `true` → `http://`, anything else → `https://`. An explicit `http://` or `https://` scheme always wins (env or code). The variable is read by this helper, not by the .NET SDK.

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

The listener binds `http://+:<port>/metrics/`: every IPv4 address (the managed
`HttpListener` maps `+` to `IPAddress.Any`, as it did 0.2.x's `*`), so other
pods and containers can scrape it, and it serves a fresh collection on every
scrape. There is no IPv6 bind: on an IPv6-only cluster a scrape of the pod IP
fails (known limitation, unchanged from 0.2.x). `OTEL_EXPORTER_PROMETHEUS_HOST` and `OTEL_EXPORTER_PROMETHEUS_PORT`,
which the upstream exporter reads since 1.16, have no effect: use
`OTEL_HELPER_METRICS_PORT`.


### Standard OpenTelemetry SDK Variables

How the 0.2.x helper treats the standard variables. Some are read by the .NET SDK, some by the helper, and some are **not honored** in 0.2.x (they are in Go/Python 0.3).

#### Sampling

| Variable | Description | Default |
|---|---|---|
| `OTEL_TRACES_SAMPLER` | Read by the SDK — only when `TelemetryOptions.Sampler` is left at its default. Unset → the helper's `AlwaysOnSampler` (not parent-based). | _(unset)_ |
| `OTEL_TRACES_SAMPLER_ARG` | Sampler argument (SDK). | empty |

#### Context Propagation

| Variable | Description | Default |
|---|---|---|
| `OTEL_PROPAGATORS` | **Not read** by the .NET SDK or this helper; W3C TraceContext + Baggage are always used. | `tracecontext,baggage` (fixed) |

#### OTLP Exporter

| Variable | Description | Default |
|---|---|---|
| `OTEL_EXPORTER_OTLP_PROTOCOL` | Read by the helper: `grpc` or `http/protobuf`. Unset → `http/protobuf` when the endpoint port is `4318`, else `grpc`. | port inference |
| `OTEL_EXPORTER_OTLP_INSECURE` | Read by the helper, schemeless endpoints only (see TLS above). | _(unset)_ |
| `OTEL_EXPORTER_OTLP_HEADERS` | Additional headers (e.g., auth); read by the SDK. | empty |
| `OTEL_EXPORTER_OTLP_TIMEOUT` | **Not honored** in 0.2.x: the helper always passes `TelemetryOptions.ExportTimeoutMs` (default 10000 ms), which overrides it. | `10000` |
| `OTEL_EXPORTER_OTLP_<SIGNAL>_*` | Per-signal variables are not used by the helper's exporter wiring in 0.2.x. | — |

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
| SqlClient | SQL queries — only with the `StaffOps.OtelHelper.Sql` subpackage (`AddOtelHelperSql()`) |
| AWS SDK | S3, SQS, DynamoDB calls, etc. — only with the `StaffOps.OtelHelper.AWS` subpackage (`AddOtelHelperAws()`) |
| Custom Sources | Spans created via `ActivitySource(serviceName)` |

Sampler: `AlwaysOnSampler` in all environments (configurable via `opts.Sampler`). It ignores the parent's sampled flag; Go/Python 0.3 use `parentbased_always_on` (ADR-002), .NET follows in Phase C. Tail-based sampling is done in the Collector.

> Paths `/ping`, `/health`, `/healthz`, `/ready` (exact match, case-insensitive) are filtered automatically on ASP.NET Core server spans and HttpClient client spans — they do not generate trace spans. Go/Python 0.3 use a longer list (`/readyz`, `/live`, `/livez`, `/metrics` too) and keep these requests in the HTTP metrics; .NET adopts that in Phase C.

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
| `ServiceName` | string? | `OTEL_SERVICE_NAME` / `OTEL_RESOURCE_ATTRIBUTES` / `SERVICE_NAME` | `unknown_service:<process name>` + warning |
| `Environment` | string? (`DeploymentEnvironments.*` or any value) | `OTEL_RESOURCE_ATTRIBUTES` / `ENVIRONMENT` | none (attribute absent + warning) |
| `OtelCollectorEndpoint` | string? | `OTEL_EXPORTER_OTLP_ENDPOINT` | none (no OTLP export) |
| `DebugLevel` | bool? | `OTEL_HELPER_DEBUG_LEVEL` | `false` |
| `ExportTimeoutMs` | int? | — | `10000` (10s) |
| `ExtraInstrumentation` | string?, `[Obsolete]` | `OTEL_HELPER_EXTRA_INSTRUMENTATION` | none (no effect — use subpackages) |
| `Sampler` | Sampler? | — | `AlwaysOnSampler` |
| `MinimumLogLevel` | LogLevel? | — | `null` (auto per environment) |
| `ResourceAttributes` | Dictionary<string, object> | — | empty |
| `AdditionalActivitySources` | List\<string\> | — | empty |
| `TracesEndpoint`, `MetricsEndpoint`, `LogsEndpoint` | string? | — | none (per-signal endpoint, used as written) |
| `Insecure` | bool? | `OTEL_EXPORTER_OTLP_INSECURE` | `false` (schemeless endpoint → `https://`) |
| `Compression` | string? (`gzip`/`none`) | `OTEL_EXPORTER_OTLP_COMPRESSION` | `none` |
| `SampleRatio` | double? | — | none (`ParentBased(TraceIdRatioBased(ratio))` when set) |
| `HealthPaths` | List\<string\>? | — | `/ping`, `/health`, `/healthz`, `/ready` |
| `AdditionalMeters` | List\<string\> | — | empty |
| `ForwardSdkDiagnostics` | bool | — | `true` (SDK warnings on stderr, redacted) |

> `ServiceName` resolves (0.3, this branch): `ServiceName` > `ResourceAttributes["service.name"]` > `OTEL_SERVICE_NAME` > `service.name` in `OTEL_RESOURCE_ATTRIBUTES` > `SERVICE_NAME` > `unknown_service:<process name>` with one warning. A blank value counts as not set at every level.
> `Environment` resolves: `Environment` > `ResourceAttributes["deployment.environment.name"]` > `OTEL_RESOURCE_ATTRIBUTES` > `ENVIRONMENT` > absent with one warning; the value is emitted as written.
> `service.instance.id`: `ResourceAttributes` > `OTEL_RESOURCE_ATTRIBUTES` > one id per process (the one the SDK's `AddService` generates).
> `ResourceAttributes` beat `OTEL_RESOURCE_ATTRIBUTES` per key; an empty key or a value the SDK cannot hold fails host start; `telemetry.sdk.*` always come from the SDK (a code value is ignored with a warning). The resource declares the schema URL `https://opentelemetry.io/schemas/1.43.0`. Attributes added by an earlier `ConfigureResource` are kept (they beat `OTEL_RESOURCE_ATTRIBUTES`, lose to `ResourceAttributes`); their `service.name` is replaced by the resolved one, with a warning; their `deployment.environment.name` and an explicit `service.instance.id` are kept when the helper resolves none of its own (the environment then also picks the log level), else replaced with one warning per key.
> **Register `ConfigureResource` before `AddOtelHelper()`.** The SDK applies resource callbacks in registration order with no later hook, so one registered after `AddOtelHelper()` overrides the helper's resource (an `AddService` there replaces `service.name` and drops the schema URL; a `SetResourceBuilder(...)` there replaces the whole resource of that signal). At host start, after the providers are built, the helper compares each provider's resource with what it set and warns once naming the signals and the changed keys; an app without a host (a bare `ServiceProvider`) gets no such check. An environment set by an earlier `ConfigureResource` applies to the signals whose resource carries it, and picks the log level; one set only by a later callback does not. In a host the "environment not set" warning is written at start, only when no signal's resource carries it (one "differs between signals" line names the signals that lack it otherwise); without a host it is written when a provider builds its resource.
> A blank code `ServiceName` or `Environment` (`""`, whitespace) fails host start; leave it `null` for "not set".
> Identity and `OTEL_RESOURCE_ATTRIBUTES` warnings are written once per service provider (one diagnostics sink per container), not once per process as in Go and Python.
> `MinimumLogLevel` when null: LOCAL=Debug, DEV/HML=Information, PRD=Warning, unset or any other value=Information; the name is matched ignoring ASCII case (`prd` = `PRD`). Use `GetDefaultLogLevel()` to query.
> `Sampler` default is AlwaysOnSampler — override only if you have a specific reason.
> Options hold only what code sets (`null` = not set); a value set in code (also one bound from `IConfiguration`) wins over the env var. The full 0.3 option list and the breaking changes: `CHANGELOG.md`, .NET migration guide.

---

## Opt-in Subpackages

AWS, Redis, SQL, and Profiling instrumentations are **no longer bundled in core**. Install the subpackage you need:

| Package | Registration | What it instruments |
|---------|--------------|---------------------|
| `StaffOps.OtelHelper.AWS` | `services.AddOtelHelperAws()` | AWS SDK calls (S3, SQS, DynamoDB, etc.) |
| `StaffOps.OtelHelper.Redis` | `services.AddOtelHelperRedis()` | StackExchange.Redis commands |
| `StaffOps.OtelHelper.Sql` | `services.AddOtelHelperSql()` | SqlClient queries |
| `StaffOps.OtelHelper.Profiling` | `services.AddOtelHelperProfiling()` | Pyroscope continuous profiling |

### Usage

```csharp
services.AddOtelHelper();

// Add only the instrumentations you need:
services.AddOtelHelperAws();
services.AddOtelHelperRedis();
services.AddOtelHelperSql();
```

Redis with explicit connection:

```csharp
services.AddOtelHelperRedis(connectionMultiplexer);
```

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

> **Note**: `OTEL_HELPER_EXTRA_INSTRUMENTATION` no longer turns anything on in core — only the subpackages above add SQL/AWS/Redis instrumentation.

---

## Architecture

```
Application (.NET SDK) → OTLP gRPC :4317 → Agent Collector → Gateway Collector → Tempo / VictoriaMetrics / Loki
```

All telemetry goes through the Collector. The SDK does NOT export directly to backends.

---

## Tests

### Run unit tests

From `dotnet/`, with the repository root mounted (the parity runner reads
`parity/cases.json`):

```bash
docker run --rm -v "$(git rev-parse --show-toplevel):/src" -w /src/dotnet mcr.microsoft.com/dotnet/sdk:8.0 dotnet test StaffOps.OtelHelper.Tests
```

Coverage: see [TESTS.md](TESTS.md) for the full list.

### Examples

See [example/README.md](example/README.md) for sample apps with distributed traces.

---

## Supported Targets

- .NET 8 (LTS)
- .NET 10 (LTS) — via roll-forward

OpenTelemetry .NET 1.20.x (core and contrib, one generation; minimum
versions, NuGet semantics). Three packages exist only as prereleases, so the
helper depends on the newest prerelease of the matching minor: the contrib
instrumentations `OpenTelemetry.Instrumentation.GrpcNetClient` and
`OpenTelemetry.Instrumentation.StackExchangeRedis` (`StaffOps.OtelHelper.Redis`),
and the Prometheus listener `OpenTelemetry.Exporter.Prometheus.HttpListener`,
all `1.20.1-beta.1`.

### .NET 10 Note

The lib is compiled for `net8.0` and runs on .NET 10 via roll-forward. Requirements:

- **Runtime image**: use `aspnet` (not `runtime`) — the lib depends on `Microsoft.AspNetCore.App` for ASP.NET Core instrumentation
- **Roll-forward**: add `DOTNET_ROLL_FORWARD=Major` env var if using a custom image:
  ```dockerfile
  ENV DOTNET_ROLL_FORWARD=Major
  ```

Moving an app to the .NET 9+/10 **runtime** changes three things in the
emitted telemetry; they come from the runtime and the upstream
instrumentation, not from the helper (same as the `0.2.1` release notes):

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

---

## Project Structure

```
StaffOps.OtelHelper.sln
├── Directory.Build.props               # Shared build settings: SourceLink, warnings as errors
├── Directory.Build.targets             # Package metadata of the five packed libraries
├── StaffOps.OtelHelper/                # Main library
│   ├── TelemetryExtensions.cs          # Entry point AddOtelHelper()
│   ├── ActivitySourceExtensions.cs     # StartRootActivity() for workers
│   ├── TracerSetup.cs                  # Tracing + health filter + sampler
│   ├── Tracing/
│   │   └── DebugTraceStateProcessor.cs # Injects tracestate debug=true for tail sampling
│   ├── MetricsSetup.cs                 # Metrics with exemplars
│   ├── LoggingSetup.cs                 # Logging via OpenTelemetry SDK
│   ├── Models/
│   │   ├── DeploymentEnvironments.cs   # Environment name constants
│   │   ├── TelemetryOptions.cs         # Code options (nullable)
│   │   └── TelemetryOptionsValidator.cs      # Code-value validation at start
│   ├── Resolution/                     # Env + code resolution (ResolvedTelemetry)
│   ├── Diagnostics/                    # otelhelper: stderr sink, SDK EventSource forwarding
│   ├── Exporters/                      # OTLP exporter options and the helper HttpClient
├── StaffOps.OtelHelper.{AWS,Redis,Sql,Profiling}/  # Opt-in subpackages
├── StaffOps.OtelHelper.Tests/          # Unit tests (xUnit)
└── example/                            # Sample apps
    ├── dotnet-api/        # API frontend
    ├── dotnet-backend/    # Backend with DB + external call
    └── dotnet-process/    # Background worker
```
