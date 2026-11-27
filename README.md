# OpenTelemetry Helper Libraries

Standardized OpenTelemetry instrumentation for Go, Python, Node.js and .NET
services:
one call sets up traces, metrics and logs with the same configuration
contract and defaults in every language; emitted telemetry differs where noted
([known gaps](#known-emitted-telemetry-gaps)).

**Audience:** application engineers adding telemetry to a service, and platform
engineers who run the Collector it exports to.

## Libraries

| Language | Directory | Package | Version | Contract |
|----------|-----------|---------|---------|----------|
| Go | [`go/`](go/) | `github.com/staffops/otel-libs/go` + `ext/*` modules | `0.3.0` (unreleased, this branch); latest release `go/v0.2.1` | 0.3 |
| Python | [`python/`](python/) | `staffops-otel-helper` (extras `aws`, `redis`, `sql`) | `0.3.0` (unreleased, this branch); latest release `0.2.0` | 0.3 |
| .NET | [`dotnet/`](dotnet/) | `StaffOps.OtelHelper` + `.AWS` / `.Redis` / `.Sql` / `.Profiling` (NuGet, GitHub Packages; from `0.3.0-rc.2` — released `0.2.x` is `OtelHelper*`, ADR-011) | `0.3.0` (unreleased, this branch); release candidate `0.3.0-rc.2` (tag `dotnet/v0.3.0-rc.2`, published by the `publish-dotnet-release` job; `dotnet/v0.3.0-rc.1` was tagged but never published); latest stable release `0.2.1` (`OtelHelper*`, tag `dotnet/v0.2.1`) | 0.3 (Phase C; the released `0.2.1` is 0.2) |
| Node.js | [`nodejs/`](nodejs/) | `@staffops/otel-helper` + `-sql` / `-redis` / `-aws` / `-pino` / `-winston` (npm) | `0.3.0` (unreleased); release candidate `0.3.0-rc.1` (tag `nodejs/v0.3.0-rc.1`, npm dist-tag `next`; install the exact version) | 0.3 |

The rollout order is Go + Python, then Node.js, then .NET
([ADR-004](specs/decisions/ADR-004-phased-rollout-go-python-first.md)). Until a
language catches up, its differences are tracked as `xfail` cases in the
[parity fixture](#cross-language-parity). Upgrading from 0.2? Read the
[migration guide](CHANGELOG.md#unreleased) first.

## Architecture

```
[ Application (Go / Python / Node.js / .NET) ]
        │ OTLP (gRPC :4317 or http/protobuf :4318)
        ▼
[ OpenTelemetry Collector ]  ← tail sampling, k8s attributes, routing
        │
┌───────┴───────┬───────────────┐
│ Traces        │ Metrics       │ Logs
│ (Tempo)       │ (VictoriaM.)  │ (Loki)
└───────────────┴───────────────┘

No OTLP endpoint → metrics on a Prometheus /metrics endpoint (:9464),
logs as JSON on stdout, traces not exported.
```

## Principles

1. **OpenTelemetry as the single standard** — no vendor SDKs; only official
   OTel and OTel contrib packages.
2. **Everything via the Collector** — the SDK exports OTLP to the Collector,
   never directly to backends. One exception: with no OTLP endpoint, or when
   `OTEL_METRICS_EXPORTER` includes `prometheus`, metrics are served on
   `/metrics` (port 9464, `OTEL_EXPORTER_PROMETHEUS_PORT` or
   `OTEL_HELPER_METRICS_PORT`)
   ([ADR-007](specs/decisions/ADR-007-keep-prometheus-in-core.md)).
3. **Sampling at the Collector** — tail sampling at the gateway. The SDK
   default is `parentbased_always_on`
   ([ADR-002](specs/decisions/ADR-002-parentbased-sampler.md); the released .NET
   0.2.x uses a bare AlwaysOn sampler).
4. **Resource attributes at the Collector** — the helper sets only
   `service.name`, `deployment.environment.name` (never the legacy
   `deployment.environment`) and, in 0.3, a per-process
   `service.instance.id` when none is configured. `k8sattributes` adds the rest.
5. **TLS by default** — `https://` and schemeless endpoints use TLS (system CA).
   The scheme decides; `OTEL_EXPORTER_OTLP_INSECURE=true` only turns a
   schemeless endpoint into plaintext.
6. **Standard OTel variables win** — explicit code > standard `OTEL_*` env >
   legacy / `OTEL_HELPER_*` env > library default. The helper never invents a
   variable the OTel spec already defines.
7. **Don't re-implement the SDK** — where the SDK honors a variable per spec,
   the helper does not override it
   ([ADR-001](specs/decisions/ADR-001-defer-otlp-env-to-sdk.md)).
8. **Failures are loud, env never crashes the app** — an invalid env value
   warns (naming the variable, never printing secrets) and falls back to the
   default; an invalid value passed in code fails setup. Telemetry is never
   dropped silently.

## Quick start

### Go

```go
import otelhelper "github.com/staffops/otel-libs/go"

ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGTERM, os.Interrupt)
defer stop()

shutdown, err := otelhelper.Setup(ctx)
if err != nil {
    return err
}
defer func() {
    flushCtx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
    defer cancel()
    _ = shutdown(flushCtx)
}()
```

### Python

```python
import staffops_otel_helper  # before any opentelemetry.* import
from staffops_otel_helper import setup_telemetry, shutdown_telemetry

setup_telemetry()
# ... on SIGTERM or in the server's shutdown hook:
shutdown_telemetry()
```

### Node.js

Either preload it (configured from the environment):

```bash
node --import @staffops/otel-helper/register server.mjs   # setup() before the app loads
```

or call `setup()` first in a CommonJS entry
(`require('@staffops/otel-helper').setup()`; ES modules use a preload file of
their own). Then flush on SIGTERM; a process that ends on its own is flushed
by the helper, but `process.exit()` skips that, so `await shutdown()` first:

```js
import { shutdown } from '@staffops/otel-helper'; // CommonJS: require('@staffops/otel-helper')
process.once('SIGTERM', () => { shutdown().finally(() => process.exit(0)); });
```

### .NET

```csharp
using StaffOps.OtelHelper; // 0.2.x: using OtelHelper;

builder.Logging.ClearProviders(); // optional, before AddOtelHelper(): see below
builder.Services.AddOtelHelper();
```

Without an OTLP endpoint the helper writes each log record as a JSON line on
stdout; keep the host's console provider and every record appears twice, with
`otelhelper: WARN no logs exporter is active, so each record is written to standard output twice: …`
on stderr — `ClearProviders()` before `AddOtelHelper()` avoids it.

Then configure the service from the environment:

```bash
OTEL_SERVICE_NAME=orders-api
OTEL_RESOURCE_ATTRIBUTES=deployment.environment.name=DEV
OTEL_EXPORTER_OTLP_ENDPOINT=https://otel-collector.example.com:4317
```

Per-language guides (custom spans, metrics and logs; framework integration;
shutdown):

| | Overview | HOW-TO | Examples |
|---|---|---|---|
| Go | [README](go/README.md) | [HOW-TO](go/HOW-TO.md) | [examples](go/example/README.md) |
| Python | [README](python/README.md) | [HOW-TO](python/HOW-TO.md) | [examples](python/example/README.md) |
| Node.js | [README](nodejs/README.md) | [HOW-TO](nodejs/HOW-TO.md) | [examples](nodejs/example/README.md) |
| .NET | [README](dotnet/README.md) | [HOW-TO](dotnet/HOW-TO.md) | [examples](dotnet/example/README.md) |

## Environment variables (0.3 contract)

Every language on this branch resolves the rows below (.NET from Phase C; the
released .NET `0.2.1` keeps the 0.2 rules, listed row by row in the .NET
migration guide of [`CHANGELOG.md`](CHANGELOG.md)). Language-specific rows:
[Node.js notes](#nodejs-notes), [.NET notes](#net-notes).

Precedence for every setting: **explicit code > standard `OTEL_*` env >
legacy / `OTEL_HELPER_*` env > library default**. Per-signal variables fall
back per-signal → generic → default. The full resolution table and edge-case
rulings are in [design D1](specs/config-contract-v0.3/design.md).

### Identity and resource

| Variable | Default | Behavior |
|----------|---------|----------|
| `OTEL_SERVICE_NAME` | _(unset)_ → SDK `unknown_service:<exe>` + warning | Service name. Beats `OTEL_RESOURCE_ATTRIBUTES` and `SERVICE_NAME` |
| `OTEL_RESOURCE_ATTRIBUTES` | _(unset)_ | Set `deployment.environment.name=<env>` here; beats `ENVIRONMENT` and is never overwritten by a helper value. The resolved environment sets the log level: `LOCAL` DEBUG, `DEV`/`HML` INFO, `PRD` WARN; unset or any other value (emitted verbatim) INFO. Parsed by the helper: values percent-decoded then trimmed; an item without `=`, with an empty key or a bad `%` escape is warned by position (never by value); a blank identity value (`service.name=`) counts as unset; `telemetry.sdk.*` are ignored (the SDK sets them) (Go limitation: see [known gaps](#known-emitted-telemetry-gaps)) |
| `SERVICE_NAME` | — | **Deprecated** (warned once; removed in 1.0) → `OTEL_SERVICE_NAME` |
| `ENVIRONMENT` | — | **Deprecated alias** → `deployment.environment.name`. With neither set, the attribute is absent and one warning is logged — never `LOCAL` ([ADR-003](specs/decisions/ADR-003-environment-unset-not-local.md)) |

### OTLP export

| Variable | Default | Behavior |
|----------|---------|----------|
| `OTEL_EXPORTER_OTLP_ENDPOINT` | _(unset)_ | Collector endpoint. Unset (and no per-signal endpoint) → that signal is not exported over OTLP: metrics fall back to Prometheus `/metrics`, logs to stdout JSON. HTTP: `/v1/<signal>` appended, path prefix kept, no port → 443/80. gRPC URL without a port → `:4317` + warning. gRPC: a path (other than `/`) is ignored, warned once naming the variable, never the path; per-signal and code endpoints too (A29). Schemeless `host:port` → `https://` (deprecated for `http/protobuf`) |
| `OTEL_EXPORTER_OTLP_<SIGNAL>_ENDPOINT` | _(unset)_ | Used verbatim for `TRACES`, `METRICS`, `LOGS`; alone, it configures that signal |
| `OTEL_EXPORTER_OTLP[_<SIGNAL>]_PROTOCOL` | port inference | `grpc` or `http/protobuf`. Unset → `http/protobuf` when the resolved endpoint's port is `4318`, else `grpc`. `http/json` from env warns and is ignored |
| `OTEL_EXPORTER_OTLP[_<SIGNAL>]_INSECURE` | _(unset)_ | Only for schemeless endpoints: `true` → plaintext. Contradicting an explicit scheme → the scheme wins, warned. Values other than `true` are false (warned if not `false`) |
| `OTEL_EXPORTER_OTLP[_<SIGNAL>]_HEADERS` | _(unset)_ | `key=value,…`; keys `[A-Za-z0-9._-]+` (lowercased, last wins), values percent-decoded; malformed items skipped and warned by position. Put credentials here, not in the URL (userinfo/query are stripped). Their values, and each space-separated part of them (the token of `Bearer <token>`), of 4 characters or more are masked as `<redacted>` in SDK error text, and Go drops OTLP/HTTP response bodies from it (A29: a collector or proxy echoing the request no longer puts them in the log). Exception: the Go SDK's own internal logger can print a malformed item at startup ([known gaps](#known-emitted-telemetry-gaps)) |
| `OTEL_EXPORTER_OTLP[_<SIGNAL>]_TIMEOUT` | `10000` | Milliseconds, plain decimal, clamped to [1 ms, 24 h] |
| `OTEL_EXPORTER_OTLP[_<SIGNAL>]_COMPRESSION` | `gzip` | `gzip` or `none` (case-insensitive) |
| `OTEL_EXPORTER_OTLP[_<SIGNAL>]_CERTIFICATE`, `_CLIENT_CERTIFICATE`, `_CLIENT_KEY` | _(unset)_ | Custom CA / mTLS (SDK). Ignored with a warning on a plaintext endpoint |

### Signals, sampling and propagation

| Variable | Default | Behavior |
|----------|---------|----------|
| `OTEL_SDK_DISABLED` | `false` | `true` → no providers, exporters, `/metrics` listener or log handlers; propagators still installed |
| `OTEL_TRACES_EXPORTER`, `OTEL_LOGS_EXPORTER` | `otlp` when configured | `otlp`, `console`, `none` (or a list); `logging` = deprecated `console` (env only: in code `logging` is an unknown exporter and fails). Wins over `OTEL_HELPER_DISABLED_SIGNALS` for its signal |
| `OTEL_METRICS_EXPORTER` | OTLP if an endpoint is configured, else `prometheus` | `otlp`, `prometheus`, `otlp,prometheus`, `console`, `none`. Env `otlp` without an endpoint warns and falls back |
| `OTEL_EXPORTER_PROMETHEUS_PORT` | _(unset)_ | Port of the standalone `/metrics` listener, beats `OTEL_HELPER_METRICS_PORT` (code still wins). Same grammar; `0` = no listener (not an ephemeral port); invalid → warned, next level (A29) |
| `OTEL_EXPORTER_PROMETHEUS_HOST` | every interface | Bind address of the listener: an IPv4 or IPv6 literal (brackets allowed, no zone id) or `localhost` (= `127.0.0.1`); `::` = every interface. **Unset binds every interface, not the spec default `localhost`**, so other pods can scrape it. Another value (a host name, `*`) is warned and **every interface** is used — wider than the restriction it was meant to set (owner decision). Never turns the listener on by itself (A29). .NET: no IPv6 bind — "every interface" is every IPv4 interface, `::1` binds `127.0.0.1`, `::` every IPv4 interface (both warned), another IPv6 address gets no listener (one ERROR) ([known gaps](#known-emitted-telemetry-gaps)) |
| `OTEL_METRIC_EXPORT_INTERVAL` | `30000` | Milliseconds (helper default 30 s; SDK default 60 s): ASCII digits with an optional `+` (any length), > 0, else warned and the default; above 2 147 483 647 clamped with a warning. A code interval outside [1, 2 147 483 647] fails |
| `OTEL_CONFIG_FILE`, `OTEL_EXPERIMENTAL_CONFIG_FILE` | _(unset)_ | Declarative configuration is **not supported**: set, it is ignored with one warning per process (.NET: per container, [known gaps](#known-emitted-telemetry-gaps)) naming the variable (never the path); `OTEL_EXPERIMENTAL_CONFIG_FILE` is the deprecated name |
| `OTEL_TRACES_SAMPLER` / `OTEL_TRACES_SAMPLER_ARG` | `parentbased_always_on` | The six spec samplers (ASCII case-insensitive). Unknown names warn and use the default. With services in several languages use `parentbased_traceidratio`, not bare `traceidratio`: the SDKs (Node.js and .NET in particular) derive the ratio decision from the trace id differently, so bare ratio decisions are not consistent across a trace |
| `OTEL_PROPAGATORS` | `tracecontext,baggage` | Supported: `tracecontext`, `baggage`, `none`. Others (`b3`, `jaeger`, `xray`, …) are warned (one warning each, naming the effective list) and skipped; `none` with other values warns and disables propagation |
| `OTEL_SEMCONV_STABILITY_OPT_IN` | Python: `http,database/dup` | Python sets this default before instrumenting (stable HTTP semconv, both DB keys); a user value wins for HTTP. DB keys in every language: both `db.system.name` and the legacy `db.system` unless the value holds `database` without `database/dup` (A25; dup emits the full new and legacy sets). Python treats an empty value as unset, ASCII-folds every token (`HTTP/DUP` = `http/dup`) and writes the normalized value back to the environment (inherited by child processes). Go instrumentations emit stable HTTP semconv natively AWS RPC keys (A30): legacy by default in every language; `http,rpc` gives the current keys, `http,rpc/dup` both (a bare `rpc` has no `http` token: Python then uses the old HTTP conventions and Node.js warns). In Go `rpc/dup` also switches otelgrpc to dup, doubling the RPC metric series |

### Helper-specific

| Variable | Default | Behavior |
|----------|---------|----------|
| `OTEL_HELPER_DEBUG_LEVEL` | `false` | Debug log level and `debug=true` on entry spans (no parent or a remote parent) for Collector tail-sampling policies ([ADR-006](specs/decisions/ADR-006-debug-span-attribute.md)); the Collector policy needs an environment allow-list and a rate limit ([prerequisites](#collector-and-platform-prerequisites)). Python debug mode also turns on every installed `ext` instrumentor (SQL, Redis, AWS). Do not leave on in production |
| `OTEL_HELPER_METRICS_PORT` | `9464` | Standalone Prometheus `/metrics` listener port when `OTEL_EXPORTER_PROMETHEUS_PORT` is unset or invalid: ASCII digits with an optional `+`, in [0, 65535] (else warned, 9464); dual-stack, `GET`/`HEAD /metrics` only (.NET: IPv4 only, any method and `/metrics/*`, [known gaps](#known-emitted-telemetry-gaps)). `0` disables the listener (the mountable handler keeps working; .NET has none — mount the upstream ASP.NET Core endpoint, [.NET HOW-TO §15](dotnet/HOW-TO.md#aspnet-core-mounting-metrics-on-your-own-pipeline)) |
| `OTEL_HELPER_DISABLED_METRICS` | _(unset)_ | Comma list of case-sensitive instrument-name globs to drop from every exporter: `*` any run of characters, `?` one character, everything else literal |
| `OTEL_HELPER_HTTP_OPT_IN_METRICS` | `false` | `true` also records the HTTP metrics semconv makes opt-in: `http.server.active_requests` and the request/response body sizes (code: Go `WithHTTPOptInMetrics`, Python `http_opt_in_metrics`, Node.js `httpOptInMetrics`, .NET `TelemetryOptions.HttpOptInMetrics`; code wins). By default only `http.server.request.duration` and `http.client.request.duration` are recorded. Go has no active-requests metric upstream; Node.js records none of them; .NET records `http.server.active_requests` and its client connection metrics, no body sizes ([known gaps](#known-emitted-telemetry-gaps)) (A30, [design D1](specs/config-contract-v0.3/design.md) "HTTP opt-in metrics") |
| `OTEL_HELPER_SAMPLE_RATIO` | `1.0` | **Deprecated** → `OTEL_TRACES_SAMPLER=parentbased_traceidratio` + `OTEL_TRACES_SAMPLER_ARG`. Used only while `OTEL_TRACES_SAMPLER` is unset; builds `ParentBased(TraceIdRatioBased(x))`; a value that is not a plain decimal in [0, 1] warns and is ignored |
| `OTEL_HELPER_DISABLED_SIGNALS` | _(unset)_ | **Deprecated** → `OTEL_{TRACES,METRICS,LOGS}_EXPORTER=none`. A signal whose `OTEL_<SIGNAL>_EXPORTER` holds a valid entry (`none` included; only unknown entries count as unset) is not disabled by it, nor are metrics when code sets the metric exporters; unknown entries warn once. Disabled traces are a no-op tracer in every language (Node.js since A35), unless a foreign TracerProvider is kept (Node.js: `inactiveSignals` lists `traces`): a root carries no trace context, an inbound one is passed on unchanged, baggage propagates; `OTEL_TRACES_EXPORTER=none` keeps an SDK provider that samples roots and injects their context |
| `OTEL_HELPER_EXTRA_INSTRUMENTATION` | Python: `SQL` | **Go and Node.js: removed** (no effect, warns; Node.js: pass `sqlInstrumentation()` / `redisInstrumentation()` / `awsInstrumentation()` in `setup({ instrumentations })`). **Python: deprecated** — call `staffops_otel_helper.ext.instrument_sql()` / `instrument_redis()` / `instrument_aws()` before `setup_telemetry()`. .NET branch (task C16): no effect, warns once — add `StaffOps.OtelHelper.Sql` / `.AWS` / `.Redis` |

Health checks are configured in code, not env: by default server spans for
`/health /healthz /ready /readyz /live /livez /ping /metrics` (exact,
case-insensitive) and the `grpc.health.v1.Health` service are not traced, but
they **are counted** in the HTTP server metrics (and, in Go,
`rpc.server.call.duration`) — exclude them in SLI queries. Override with
`WithHealthPaths(...)` (Go), `TelemetryOptions(health_paths=[...])` (Python),
`setup({ healthPaths: [...] })` (Node.js) or `TelemetryOptions.HealthPaths`
(.NET).

### Node.js notes

The Node.js helper resolves every row above itself (the OpenTelemetry JS SDK
3.0 reads no environment variable). Node.js only: `OTEL_LOG_LEVEL` sets the
level of the SDK's own messages (default `WARN`); `OTEL_NODE_RESOURCE_DETECTORS`
is ignored (warned: the resource is built by the helper);
`OTEL_SEMCONV_STABILITY_OPT_IN` is not set by the helper and its HTTP tokens
have no effect (the Node.js HTTP instrumentations emit only the stable
conventions; `http/dup` or a value without `http` warns); the batch processor
and limit variables (`OTEL_BSP_*`, `OTEL_BLRP_*`, `OTEL_*_LIMIT`) are read by
the helper; `OTEL_METRICS_EXEMPLAR_FILTER` is not honored (no exemplars).
`Resolved.inactiveSignals` and `replaceGlobals` handle globals another SDK
registered first. Full table: [`nodejs/README.md`](nodejs/README.md#environment-variables).

### .NET notes

The .NET helper resolves the rows above itself and builds its own OTLP
exporters ([ADR-013](specs/decisions/ADR-013-dotnet-helper-owns-env-resolution.md)).
.NET only:

- **Where values are read.** The helper's rows are read from the **process
  environment**; a value only in `appsettings.json` (another `IConfiguration`
  source) has no effect — the sampler, propagator and interval variables say so
  in one warning, the OTLP endpoint, protocol, headers, timeout and compression
  are ignored silently. `OTEL_SDK_DISABLED` and
  `OTEL_EXPORTER_OTLP_METRICS_TEMPORALITY_PREFERENCE` are read from
  `IConfiguration`, as the SDK reads them; the attribute limits and the batch
  sizes (`OTEL_BSP_*`, `OTEL_BLRP_*`) stay with the SDK. `TelemetryOptions`
  bound from a configuration section count as code (they win over the
  environment). Details: [.NET HOW-TO §2](dotnet/HOW-TO.md).
- **Not honored:** `OTEL_BSP_EXPORT_TIMEOUT`, `OTEL_BLRP_EXPORT_TIMEOUT` and
  `OTEL_METRIC_EXPORT_TIMEOUT` (the OTLP timeout is every signal's deadline;
  each warns once). `OTEL_EXPORTER_OTLP_METRICS_DEFAULT_HISTOGRAM_AGGREGATION`
  is applied only for `base2_exponential_bucket_histogram` set in the process
  environment ([.NET HOW-TO §15](dotnet/HOW-TO.md#exponential-histograms-on-otlp)).
- **gRPC server spans** are RPC-shaped only when the application sets the
  upstream key `OTEL_DOTNET_EXPERIMENTAL_ASPNETCORE_ENABLE_GRPC_INSTRUMENTATION=true`
  (the helper never sets it and warns once when gRPC endpoints are mapped
  without it).
- **"Once" warnings** are once per diagnostics sink (service provider), not
  per process.
- **Do not combine `UseOtlpExporter()` with `AddOtelHelper()`**: every signal is
  exported twice (warned once).

The released `0.2.1` still implements the 0.2 contract (`SERVICE_NAME` before
`OTEL_SERVICE_NAME`, `LOCAL` / `my-service` defaults, a bare AlwaysOn sampler,
no per-signal variables); every change is in the .NET migration guide of
[`CHANGELOG.md`](CHANGELOG.md).

## Opt-in extensions

AWS, Redis and SQL instrumentations are separate packages; the core stays
lightweight. Node.js also ships its pino and winston log destinations as
`@staffops/otel-helper-pino` and `@staffops/otel-helper-winston`.

| Language | AWS | Redis | SQL |
|----------|-----|-------|-----|
| Go | `ext/otelaws` | `ext/otelredis` | `ext/otelsql` |
| Python | `staffops-otel-helper[aws]` | `staffops-otel-helper[redis]` | `staffops-otel-helper[sql]` |
| Node.js | `@staffops/otel-helper-aws` | `@staffops/otel-helper-redis` | `@staffops/otel-helper-sql` |
| .NET | `StaffOps.OtelHelper.AWS` | `StaffOps.OtelHelper.Redis` | `StaffOps.OtelHelper.Sql` |

.NET also ships `StaffOps.OtelHelper.Profiling` (Pyroscope continuous
profiling, no counterpart in the other languages; see the gap row below).

## Cross-language parity

Parity is enforced, not promised. [`parity/cases.json`](parity/cases.json) is a
hand-maintained, language-neutral fixture (395 cases on 2026-11-18, 6 added by D16 / A46, 1 by A33, 1 by A51; 387 cases on 2026-11-12: 45 added by later contract and .NET tasks to the 342 counted at A29 — 187 after A13, 17 added by the Node B3 review, 6 by A16, 40 by Node B4a and its rulings, 16 by A17, 2 by Node B4b, 13 by A18, 14 by A19 and its fix round, 1 by Node B5, 5 by A20, 3 by Node B7, 11 by A21, 6 by the B7/A21 fix rounds, 2 by A22 from the Node B6 fix round, 1 by A23 from Node B8, 18 by A29)
that every language's test suite runs — config resolution, wire-level export
against a fake OTLP receiver, sampling, propagation, health exclusion, logging
and `/metrics` status
([ADR-008](specs/decisions/ADR-008-shared-parity-fixture.md)). A language that
lags marks a case `xfail:<reason>`; cases are never deleted to make CI green.
Format and runner details: [`parity/README.md`](parity/README.md); the
end-to-end smoke against a real Collector: [`testing/`](testing/README.md).

### Known emitted-telemetry gaps

The configuration contract is shared; what the instrumentations emit is not
always identical (gRPC attributes and metrics, runtime metrics, exemplars,
database and HTTP/gRPC exception detail, `/metrics` exposition, and more).
The full table, one row per difference, is in the
[library overview](https://staffops.github.io/otel-libs/library/overview/#known-emitted-telemetry-gaps)
(source: [`docs/site/library/overview.md`](docs/site/library/overview.md)).

## Collector and platform prerequisites

The libraries assume a Collector and platform configured for them (tail
sampling, gateway load balancing, chart variables, metrics-backend OTLP
ingest, health-span filtering), and the observability pages need the
span-metrics and service-graph connectors, exemplar storage and Loki
structured metadata. The checklist is in
[Collector prerequisites](https://staffops.github.io/otel-libs/platform/collector/)
(source: [`docs/site/platform/collector.md`](docs/site/platform/collector.md));
the reason for each item is in
[design D11](specs/config-contract-v0.3/design.md#d11--collector--platform-prerequisites-reference-checklist).

## Installing

Only GitHub Packages needs a GitHub token with `read:packages`: .NET always
(GitHub Packages NuGet), Node.js only when installed from GitHub Packages
instead of npmjs. Python uses GitHub Release wheel assets, Go uses `go get`
from the repository. The Node.js packages are on npmjs (no token; release
candidate `0.3.0-rc.1`, dist-tag `next`, always installed at an exact
version such as `npm install @staffops/otel-helper@0.3.0-rc.1`, because
`latest` is a deprecated bootstrap placeholder until the first stable
release) and on GitHub Packages with the same version. See
[CONSUMING.md](CONSUMING.md) for auth, install commands and CI variants.

## Dashboards

Six Grafana dashboards built on the four-language demo — start page,
overview (RED), exceptions and logs, span hygiene, database errors,
exemplars — are in [`dashboards/demo/`](dashboards/demo/). What each one
answers and how to import it:
[Dashboards](https://staffops.github.io/otel-libs/dashboards/overview/). How to use the signals for
diagnosis: [Observability](https://staffops.github.io/otel-libs/observe/overview/).

## More

- [Changelog and migration guide](CHANGELOG.md)
- [Specs and decision records](specs/README.md)
- [Contributor and agent guide](AGENTS.md)
- [Documentation site](https://staffops.github.io/otel-libs/)

## License

[Apache License 2.0](LICENSE) — the same license as OpenTelemetry.
