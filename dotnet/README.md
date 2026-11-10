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
| `OTEL_EXPORTER_OTLP_ENDPOINT` | Helm Chart | Collector endpoint. Unset → no OTLP export, Prometheus `/metrics` fallback. 0.3 (this branch): parsed by the contract rules below ([Endpoint parsing](#endpoint-parsing-03-this-branch)); `OTEL_EXPORTER_OTLP_<SIGNAL>_ENDPOINT` beats it for one signal | _(unset)_ |

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

0.2.x rebuilt every endpoint as `scheme://host:port` (path dropped, `:4317`
when no port, also for `http/protobuf`). 0.3 (this branch, task C7a) follows
the contract:

| Endpoint | Protocol | Resulting target |
|---|---|---|
| `http://collector` (URL, no port) | `grpc` | `http://collector:4317`, with a warning to set an explicit port |
| `http://collector` (URL, no port) | `http/protobuf` | `http://collector/v1/traces` (scheme default port, 80 or 443) |
| `https://gw.example.com/otlp` | `http/protobuf` | `https://gw.example.com/otlp/v1/traces` (path kept; a per-signal value is used as written) |
| `https://gw.example.com/otlp` | `grpc` | `https://gw.example.com:4317` (gRPC dials `host:port`; the path is ignored and warned once per variable, task C7b) |
| `collector` (schemeless) | `grpc` / `http/protobuf` | `https://collector:4317` / `https://collector:4318/v1/traces` (deprecated for `http/protobuf`, warned) |

### Endpoint parsing (0.3, this branch)

Every endpoint, from code or the environment, is parsed by RFC 3986, as in Go,
Python and Node.js (contract design D1, A17):

- **Removed with one warning:** URL userinfo, query and fragment (an empty `?`
  or `#` too). Put credentials in `OTEL_EXPORTER_OTLP_HEADERS`. An `@` in the
  path is used as parsed and warned ("misplaced credentials?"), naming the
  variable only.
- **Invalid:** a control character or a backslash anywhere, a scheme without
  `//` and a host (`http:///x`), a schemeless value starting with `/`, a port
  outside 1–65535 (port 0 included; `04318` is 4318), a space, `%` or IPv6
  zone id in the host, a host that is neither an IPv6 literal nor an RFC 3986
  name, a host that ends in a number but is not a canonical dotted quad
  (`127.1`, `1.2.3.256`), a host that fails IDNA, and for `http/protobuf` an
  invalid `%` escape in the path. Also invalid in .NET only: a host `System.Uri`
  refuses (sub-delims other than `-` and `_`, such as `a!b` or `a~b`, and empty
  labels such as `a..b`). From the environment: one warning that shows
  `<unparseable URL>`, and the next value is used
  (`OTEL_EXPORTER_OTLP_<SIGNAL>_ENDPOINT` → `OTEL_EXPORTER_OTLP_ENDPOINT` →
  none). From code: the host fails to start, naming the option.
- **Unsupported scheme** (`ftp://`, `unix:/path`): warned, and the signal has
  no OTLP exporter (an explicit per-signal choice is not overridden).
- **IDN hosts** are converted to punycode (UTS #46 non-transitional, the
  runtime's ICU: `bücher.de` → `xn--bcher-kva.de`); hosts are lowercased.
  Without ICU (globalization-invariant mode, e.g. chiseled or alpine images
  with `DOTNET_SYSTEM_GLOBALIZATION_INVARIANT=1`) a non-ASCII host is invalid
  (the warning says it needs ICU): write it in punycode. With ICU, a non-ASCII
  host with a label over 63 characters or starting or ending with `-` is
  invalid, as in Python (Go and Node.js accept it).
- **Paths are normalized by `System.Uri`** before they are sent (Go sends them
  as written): `/a/../v1` → `/v1`, `%7E` → `~`; `%2F` stays. See HOW-TO §17.
- Diagnostics show `scheme://host:port/path` at most, only `scheme://` when
  the path or query holds an `@`, and `<scheme>:` for `unix:/…`.

### TLS / Transport Security

Transport is determined by the endpoint scheme:

| Endpoint | Transport |
|----------|-----------|
| `https://host:4317` | TLS (system CA trust store) |
| `http://host:4317` | Plaintext (insecure) |
| `host:4317` (no scheme) | TLS (secure by default) |

`OTEL_EXPORTER_OTLP_INSECURE` applies **only to schemeless endpoints**: `true` → `http://`, anything else → `https://`. An explicit `http://` or `https://` scheme always wins (env or code). The variable is read by this helper, not by the .NET SDK.

0.3 (this branch, task C7a): `OTEL_EXPORTER_OTLP_<SIGNAL>_INSECURE` beats it
for one signal and `TelemetryOptions.Insecure` beats both. The value is
`true` or `false`, ASCII case-insensitive; anything else is false and warned,
also when no endpoint is configured. A setting that contradicts an explicit
scheme (`https://` with `true`, `http://` with `false`) is warned and the
scheme wins; `TelemetryOptions.Insecure` contradicting a code URL fails at
start. 0.2.x treated any value but `true` as false, silently.

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
| `OTEL_EXPORTER_OTLP_PROTOCOL` | Read by the helper: `grpc` or `http/protobuf` (ASCII case-insensitive). Unset → `http/protobuf` when the port of the endpoint a signal uses is `4318` (compared numerically; logged once at INFO), else `grpc`. `http/json` and unknown values are warned and ignored (0.2.x: `http/json` failed validation). | port inference |
| `OTEL_EXPORTER_OTLP_INSECURE` | Read by the helper, schemeless endpoints only (see TLS above). | _(unset)_ |
| `OTEL_EXPORTER_OTLP_HEADERS` | Additional headers (e.g., auth), `key=value,…`. 0.3 (this branch, task C8): parsed by the helper and sent on its own HTTP client (0.2.x: by the SDK, a malformed item failed start). Keys `[A-Za-z0-9._-]+`, not percent-decoded; values percent-decoded as a whole (an invalid escape or non-UTF-8 bytes keep the whole value literally, warned); a value outside printable ASCII is skipped; the last of a key wins. Warnings name the variable and the item position, never a value. A gRPC key ending in `-bin` is sent base64. | empty |
| `OTEL_EXPORTER_OTLP_TIMEOUT` | 0.3 (this branch, task C8): export timeout in decimal milliseconds (`^[0-9]+(\.[0-9]+)?$`), clamped to [1, 86 400 000] with a warning; `0`, a sign, an exponent or a bare dot warn and the default applies. It is the deadline of the exporter, the HTTP client, the batch processors and the periodic reader; gRPC sends it as `grpc-timeout`. `TelemetryOptions.ExportTimeoutMs` wins. 0.2.x: not honored (fixed 10 s). | `10000` |
| `OTEL_EXPORTER_OTLP_COMPRESSION` | `gzip` or `none` (ASCII case-insensitive); anything else warns and the default applies. 0.3 (this branch, task C8): default `gzip` (0.2.x: none). | `gzip` |
| `OTEL_EXPORTER_OTLP_CERTIFICATE`, `_CLIENT_KEY`, `_CLIENT_CERTIFICATE` | TLS files of an `https://` (or schemeless, secure) endpoint, read and validated by the helper (task C8): an unreadable file or a CA with no PEM certificate warns and the next level applies, then the system roots; the client key and certificate are one pair, and an incomplete or rejected pair warns and server authentication is used. On a plaintext endpoint the files are ignored with one warning naming them and the signals. A file over 1 MiB (or a device) is "cannot be read". Warnings name the variables, never a path or content. | _(unset)_ |
| `OTEL_BSP_EXPORT_TIMEOUT`, `OTEL_BLRP_EXPORT_TIMEOUT`, `OTEL_METRIC_EXPORT_TIMEOUT` | Not honored (task C8): the OTLP export timeout is the deadline of every signal; set, each warns once. | — |
| `OTEL_BSP_MAX_EXPORT_BATCH_SIZE` / `OTEL_BLRP_MAX_EXPORT_BATCH_SIZE` | Read by the SDK; above `OTEL_BSP_MAX_QUEUE_SIZE` / `OTEL_BLRP_MAX_QUEUE_SIZE` (defaults 512 / 2048) the queue size is used and one warning names both variables. | `512` |
| `OTEL_ATTRIBUTE_VALUE_LENGTH_LIMIT` (and `OTEL_SPAN_…`, `OTEL_LOGRECORD_…`) | Read by the OTLP exporter. A `0` is warned (contract: invalid), but the .NET exporter has no public override and still sends those values empty: unset it or use a positive limit. | no limit |
| `OTEL_EXPORTER_OTLP_<SIGNAL>_*` | Not used in 0.2.x. 0.3 (this branch): `_ENDPOINT` (used as written, no `/v1/<signal>`), `_PROTOCOL` and `_INSECURE` (task C7a), and `_HEADERS`, `_TIMEOUT`, `_COMPRESSION`, `_CERTIFICATE`, `_CLIENT_KEY`, `_CLIENT_CERTIFICATE` (task C8) are read by the helper and beat the generic variable for that signal; a per-signal `_HEADERS` replaces the generic headers (no merge). | — |

> **Redirects are refused** (0.3, this branch, task C8). The helper's OTLP client never follows a `3xx`: that export is dropped and one ERROR names the signal and the status code (`otelhelper: ERROR the OTLP endpoint for traces answered HTTP 307 (a redirect); redirects are not followed, … signal=traces status=307`), never the `Location` value. 0.2.x followed redirects. Configure the final endpoint URL.

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
| `ExportTimeoutMs` | int? (in [1, 86 400 000]) | `OTEL_EXPORTER_OTLP_<SIGNAL>_TIMEOUT` / `OTEL_EXPORTER_OTLP_TIMEOUT` | `10000` (10s); the deadline of every signal |
| `ExtraInstrumentation` | string?, `[Obsolete]` | `OTEL_HELPER_EXTRA_INSTRUMENTATION` | none (no effect — use subpackages) |
| `Sampler` | Sampler? | — | `AlwaysOnSampler` |
| `MinimumLogLevel` | LogLevel? | — | `null` (auto per environment) |
| `ResourceAttributes` | Dictionary<string, object> | — | empty |
| `AdditionalActivitySources` | List\<string\> | — | empty |
| `TracesEndpoint`, `MetricsEndpoint`, `LogsEndpoint` | string? | `OTEL_EXPORTER_OTLP_<SIGNAL>_ENDPOINT` | none (per-signal endpoint, used as written; schemeless allowed) |
| `Insecure` | bool? | `OTEL_EXPORTER_OTLP_<SIGNAL>_INSECURE` / `OTEL_EXPORTER_OTLP_INSECURE` | `false` (schemeless endpoint → `https://`) |
| `Compression` | string? (`gzip`/`none`) | `OTEL_EXPORTER_OTLP_<SIGNAL>_COMPRESSION` / `OTEL_EXPORTER_OTLP_COMPRESSION` | `gzip` (0.2.x: `none`) |
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
