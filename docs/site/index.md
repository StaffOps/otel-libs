<!-- Generated from the root README.md (links rewritten for MkDocs). Edit README.md, then regenerate. -->

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
| Go | [`go/`](go/README.md) | `github.com/staffops/otel-libs/go` + `ext/*` modules | `0.3.0` (unreleased, this branch); latest release `go/v0.2.1` | 0.3 |
| Python | [`python/`](python/README.md) | `staffops-otel-helper` (extras `aws`, `redis`, `sql`) | `0.3.0` (unreleased, this branch); latest release `0.2.0` | 0.3 |
| .NET | [`dotnet/`](dotnet/README.md) | `StaffOps.OtelHelper` + `.AWS` / `.Redis` / `.Sql` / `.Profiling` (NuGet; on this branch, not published yet — released `0.2.x` is `OtelHelper*`, ADR-011) | latest release `0.2.1` (`OtelHelper*`, tag `dotnet/v0.2.1`) | 0.2.x — the 0.3 contract lands in Phase C |
| Node.js | [`nodejs/`](nodejs/README.md) | `@staffops/otel-helper` + `-sql` / `-redis` / `-aws` / `-pino` / `-winston` (npm) | `0.3.0` (unreleased); release candidate `0.3.0-rc.1` (tag `nodejs/v0.3.0-rc.1`, npm dist-tag `next`; install the exact version) | 0.3 |

The rollout order is Go + Python, then Node.js, then .NET
([ADR-004](https://github.com/StaffOps/otel-libs/blob/main/specs/decisions/ADR-004-phased-rollout-go-python-first.md)). Until a
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
   ([ADR-007](https://github.com/StaffOps/otel-libs/blob/main/specs/decisions/ADR-007-keep-prometheus-in-core.md)).
3. **Sampling at the Collector** — tail sampling at the gateway. The SDK
   default is `parentbased_always_on`
   ([ADR-002](https://github.com/StaffOps/otel-libs/blob/main/specs/decisions/ADR-002-parentbased-sampler.md); .NET 0.2.x uses
   a bare AlwaysOn sampler).
4. **Resource attributes at the Collector** — the helper sets only
   `service.name`, `deployment.environment.name` (never the legacy
   `deployment.environment`) and, in Go/Python/Node.js 0.3, a per-process
   `service.instance.id` when none is configured. `k8sattributes` adds the rest.
5. **TLS by default** — `https://` and schemeless endpoints use TLS (system CA).
   The scheme decides; `OTEL_EXPORTER_OTLP_INSECURE=true` only turns a
   schemeless endpoint into plaintext.
6. **Standard OTel variables win** — explicit code > standard `OTEL_*` env >
   legacy / `OTEL_HELPER_*` env > library default. The helper never invents a
   variable the OTel spec already defines.
7. **Don't re-implement the SDK** — where the SDK honors a variable per spec,
   the helper does not override it
   ([ADR-001](https://github.com/StaffOps/otel-libs/blob/main/specs/decisions/ADR-001-defer-otlp-env-to-sdk.md)).
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

### .NET (0.2.x)

```csharp
builder.Services.AddOtelHelper();
```

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

## Environment variables (0.3 contract — Go, Python and Node.js)

Precedence for every setting: **explicit code > standard `OTEL_*` env >
legacy / `OTEL_HELPER_*` env > library default**. Per-signal variables fall
back per-signal → generic → default. The full resolution table and edge-case
rulings are in [design D1](https://github.com/StaffOps/otel-libs/blob/main/specs/config-contract-v0.3/design.md).

### Identity and resource

| Variable | Default | Behavior |
|----------|---------|----------|
| `OTEL_SERVICE_NAME` | _(unset)_ → SDK `unknown_service:<exe>` + warning | Service name. Beats `OTEL_RESOURCE_ATTRIBUTES` and `SERVICE_NAME` |
| `OTEL_RESOURCE_ATTRIBUTES` | _(unset)_ | Set `deployment.environment.name=<env>` here; beats `ENVIRONMENT` and is never overwritten by a helper value. The resolved environment sets the log level: `LOCAL` DEBUG, `DEV`/`HML` INFO, `PRD` WARN; unset or any other value (emitted verbatim) INFO. Parsed by the helper: values percent-decoded then trimmed; an item without `=`, with an empty key or a bad `%` escape is warned by position (never by value); a blank identity value (`service.name=`) counts as unset; `telemetry.sdk.*` are ignored (the SDK sets them) (Go limitation: see [known gaps](#known-emitted-telemetry-gaps)) |
| `SERVICE_NAME` | — | **Deprecated** (warned once; removed in 1.0) → `OTEL_SERVICE_NAME` |
| `ENVIRONMENT` | — | **Deprecated alias** → `deployment.environment.name`. With neither set, the attribute is absent and one warning is logged — never `LOCAL` ([ADR-003](https://github.com/StaffOps/otel-libs/blob/main/specs/decisions/ADR-003-environment-unset-not-local.md)) |

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
| `OTEL_EXPORTER_PROMETHEUS_HOST` | every interface | Bind address of the listener: an IPv4 or IPv6 literal (brackets allowed, no zone id) or `localhost` (= `127.0.0.1`); `::` = every interface. **Unset binds every interface, not the spec default `localhost`**, so other pods can scrape it. Another value (a host name, `*`) is warned and **every interface** is used — wider than the restriction it was meant to set (owner decision). Never turns the listener on by itself (A29) |
| `OTEL_METRIC_EXPORT_INTERVAL` | `30000` | Milliseconds (helper default 30 s; SDK default 60 s): ASCII digits with an optional `+` (any length), > 0, else warned and the default; above 2 147 483 647 clamped with a warning. A code interval outside [1, 2 147 483 647] fails |
| `OTEL_CONFIG_FILE`, `OTEL_EXPERIMENTAL_CONFIG_FILE` | _(unset)_ | Declarative configuration is **not supported**: set, it is ignored with one warning per process naming the variable (never the path); `OTEL_EXPERIMENTAL_CONFIG_FILE` is the deprecated name |
| `OTEL_TRACES_SAMPLER` / `OTEL_TRACES_SAMPLER_ARG` | `parentbased_always_on` | The six spec samplers (ASCII case-insensitive). Unknown names warn and use the default. With services in several languages use `parentbased_traceidratio`, not bare `traceidratio`: the SDKs (Node.js and .NET in particular) derive the ratio decision from the trace id differently, so bare ratio decisions are not consistent across a trace |
| `OTEL_PROPAGATORS` | `tracecontext,baggage` | Supported: `tracecontext`, `baggage`, `none`. Others (`b3`, `jaeger`, `xray`, …) are warned (one warning each, naming the effective list) and skipped; `none` with other values warns and disables propagation |
| `OTEL_SEMCONV_STABILITY_OPT_IN` | Python: `http,database/dup` | Python sets this default before instrumenting (stable HTTP semconv, both DB keys); a user value wins for HTTP. DB keys in every language: both `db.system.name` and the legacy `db.system` unless the value holds `database` without `database/dup` (A25; dup emits the full new and legacy sets). Python treats an empty value as unset, ASCII-folds every token (`HTTP/DUP` = `http/dup`) and writes the normalized value back to the environment (inherited by child processes). Go instrumentations emit stable HTTP semconv natively AWS RPC keys (A30): legacy by default in every language; `http,rpc` gives the current keys, `http,rpc/dup` both (a bare `rpc` has no `http` token: Python then uses the old HTTP conventions and Node.js warns). In Go `rpc/dup` also switches otelgrpc to dup, doubling the RPC metric series |

### Helper-specific

| Variable | Default | Behavior |
|----------|---------|----------|
| `OTEL_HELPER_DEBUG_LEVEL` | `false` | Debug log level and `debug=true` on entry spans (no parent or a remote parent) for Collector tail-sampling policies ([ADR-006](https://github.com/StaffOps/otel-libs/blob/main/specs/decisions/ADR-006-debug-span-attribute.md)); the Collector policy needs an environment allow-list and a rate limit ([prerequisites](#collector-and-platform-prerequisites)). Python debug mode also turns on every installed `ext` instrumentor (SQL, Redis, AWS). Do not leave on in production |
| `OTEL_HELPER_METRICS_PORT` | `9464` | Standalone Prometheus `/metrics` listener port when `OTEL_EXPORTER_PROMETHEUS_PORT` is unset or invalid: ASCII digits with an optional `+`, in [0, 65535] (else warned, 9464); dual-stack, `GET`/`HEAD /metrics` only. `0` disables the listener (the mountable handler keeps working) |
| `OTEL_HELPER_DISABLED_METRICS` | _(unset)_ | Comma list of case-sensitive instrument-name globs to drop from every exporter: `*` any run of characters, `?` one character, everything else literal |
| `OTEL_HELPER_HTTP_OPT_IN_METRICS` | `false` | `true` also records the HTTP metrics semconv makes opt-in: `http.server.active_requests` and the request/response body sizes (code: Go `WithHTTPOptInMetrics`, Python `http_opt_in_metrics`, Node.js `httpOptInMetrics`; code wins). By default only `http.server.request.duration` and `http.client.request.duration` are recorded. Go has no active-requests metric upstream; Node.js records none of them (A30, [design D1](https://github.com/StaffOps/otel-libs/blob/main/specs/config-contract-v0.3/design.md) "HTTP opt-in metrics") |
| `OTEL_HELPER_SAMPLE_RATIO` | `1.0` | **Deprecated** → `OTEL_TRACES_SAMPLER=parentbased_traceidratio` + `OTEL_TRACES_SAMPLER_ARG`. Used only while `OTEL_TRACES_SAMPLER` is unset; builds `ParentBased(TraceIdRatioBased(x))`; a value that is not a plain decimal in [0, 1] warns and is ignored |
| `OTEL_HELPER_DISABLED_SIGNALS` | _(unset)_ | **Deprecated** → `OTEL_{TRACES,METRICS,LOGS}_EXPORTER=none`. A signal whose `OTEL_<SIGNAL>_EXPORTER` holds a valid entry (`none` included; only unknown entries count as unset) is not disabled by it, nor are metrics when code sets the metric exporters; unknown entries warn once |
| `OTEL_HELPER_EXTRA_INSTRUMENTATION` | Python: `SQL` | **Go and Node.js: removed** (no effect, warns; Node.js: pass `sqlInstrumentation()` / `redisInstrumentation()` / `awsInstrumentation()` in `setup({ instrumentations })`). **Python: deprecated** — call `staffops_otel_helper.ext.instrument_sql()` / `instrument_redis()` / `instrument_aws()` before `setup_telemetry()`. .NET branch (task C16): no effect, warns once — add `StaffOps.OtelHelper.Sql` / `.AWS` / `.Redis` |

Health checks are configured in code, not env: by default server spans for
`/health /healthz /ready /readyz /live /livez /ping /metrics` (exact,
case-insensitive) and the `grpc.health.v1.Health` service are not traced, but
they **are counted** in the HTTP server metrics (and, in Go,
`rpc.server.call.duration`) — exclude them in SLI queries. Override with
`WithHealthPaths(...)` (Go), `TelemetryOptions(health_paths=[...])` (Python)
or `setup({ healthPaths: [...] })` (Node.js).

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

### .NET 0.2.x differences

.NET still implements the 0.2 contract: `SERVICE_NAME` beats `OTEL_SERVICE_NAME`
and defaults to `my-service`; `ENVIRONMENT` defaults to `LOCAL` and unknown
values become `LOCAL`; per-signal OTLP variables, `OTEL_PROPAGATORS`,
`OTEL_EXPORTER_OTLP_TIMEOUT` and `OTEL_{TRACES,LOGS}_EXPORTER` are not honored;
`http/json` fails startup validation; the sampler is a bare AlwaysOn; the health
filter is `/ping /health /healthz /ready` (traces only; metrics still count
those requests). `OTEL_HELPER_EXTRA_INSTRUMENTATION` has no effect. See
[`dotnet/README.md`](dotnet/README.md) and the Phase C backlog in
[`specs/config-contract-v0.3/tasks.md`](https://github.com/StaffOps/otel-libs/blob/main/specs/config-contract-v0.3/tasks.md).

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

Parity is enforced, not promised. [`parity/cases.json`](https://github.com/StaffOps/otel-libs/blob/main/parity/cases.json) is a
hand-maintained, language-neutral fixture (342 cases: 187 after A13, 17 added by the Node B3 review, 6 by A16, 40 by Node B4a and its rulings, 16 by A17, 2 by Node B4b, 13 by A18, 14 by A19 and its fix round, 1 by Node B5, 5 by A20, 3 by Node B7, 11 by A21, 6 by the B7/A21 fix rounds, 2 by A22 from the Node B6 fix round, 1 by A23 from Node B8, 18 by A29)
that every language's test suite runs — config resolution, wire-level export
against a fake OTLP receiver, sampling, propagation, health exclusion, logging
and `/metrics` status
([ADR-008](https://github.com/StaffOps/otel-libs/blob/main/specs/decisions/ADR-008-shared-parity-fixture.md)). A language that
lags marks a case `xfail:<reason>`; cases are never deleted to make CI green.
Format and runner details: [`parity/README.md`](https://github.com/StaffOps/otel-libs/blob/main/parity/README.md); the
end-to-end smoke against a real Collector: [`testing/`](https://github.com/StaffOps/otel-libs/blob/main/testing/README.md).

### Known emitted-telemetry gaps

The configuration contract is shared; what the instrumentations emit is not
always identical:

| Area | Gap |
|------|-----|
| Python and Node.js gRPC | Spans carry the legacy `rpc.system`, `rpc.service`, `rpc.method` and `rpc.grpc.status_code` attributes, and no RPC metrics are emitted (upstream `opentelemetry-instrumentation-grpc`; `@opentelemetry/instrumentation-grpc` 0.300 in the Node.js helper, release candidate `0.3.0-rc.1`). Go emits the current RPC semconv and `rpc.server.call.duration` / `rpc.client.call.duration`. Node.js error spans also carry the non-semconv `grpc.error_name` / `grpc.error_message` and no `error.type` |
| Node.js gRPC handler that throws synchronously | Its SERVER span is never ended or exported (upstream `@opentelemetry/instrumentation-grpc`); Go and Python record the call with status UNKNOWN. Return errors through the callback or a rejected promise |
| Node.js routers created before `setup()` | An express app or router built before `setup()` gets a truncated `http.route` (`GET /api`) or none; require routes modules after `setup()` (the helper warns when express or grpc-js was loaded first) |
| Node.js HTTP/2 | `node:http2` servers and clients (h2c included) are not instrumented: there is no official HTTP/2 instrumentation |
| HTTP opt-in metrics | By default the instrumentation the helper installs records, in every language, only `http.server.request.duration` and `http.client.request.duration` (A30). With `OTEL_HELPER_HTTP_OPT_IN_METRICS=true` Python adds `http.server.active_requests` and the server body sizes (no client body sizes), Go the body sizes only (otelhttp has no active-requests metric), Node.js nothing (instrumentation-http records only the durations) ([design D1](https://github.com/StaffOps/otel-libs/blob/main/specs/config-contract-v0.3/design.md), "HTTP opt-in metrics") The drop applies to the contrib instrumentations' scopes only, so an application's own instrument of those names is kept (Python: the FastAPI, ASGI, requests and httpx scopes, a closed list; another contrib instrumentation the app adds keeps its series) |
| .NET framework metrics (branch, task C11) | Besides the two HTTP durations, the .NET helper exports the series of the framework meters its instrumentations subscribe: `kestrel.*` (`active_connections`, `connection.duration`, `queued_connections`, `queued_requests`), `aspnetcore.routing.match_attempts`, `dns.lookup.duration` and the runtime `process.runtime.dotnet.*` (measured 2026-11-10 after one HTTP request, one HttpClient call and one gRPC call; other meters of the ASP.NET Core instrumentation, such as `Microsoft.AspNetCore.Diagnostics`, may add series in other code paths — not measured); Go, Python and Node.js have no counterpart. Drop what you do not need with `OTEL_HELPER_DISABLED_METRICS` |
| .NET gRPC metrics | No RPC metric (`rpc.server.*`, `rpc.client.*`) exists in the .NET instrumentations: a gRPC server call is counted in `http.server.request.duration` with status 200 even when the RPC fails, and a gRPC client call in `http.client.request.duration` as `POST` with status 200 and `network.protocol.version=2`. Go records `rpc.server.call.duration` / `rpc.client.call.duration` and no HTTP metric for gRPC |
| .NET log scope attributes (branch, task C17) | The .NET helper exports `ILogger` scopes as log attributes (`IncludeScopes`, as 0.2.x). In an ASP.NET Core request that includes the hosting scope `ConnectionId`, `RequestId` and `RequestPath` — the raw request path (e.g. `/order/1`), which is high cardinality and can carry identifiers; Go, Python and Node.js emit none of them. The activity-tracking scope (`SpanId`, `TraceId`, `ParentId`) is not exported (design D-NET10 (g)). To drop the hosting scope at the Collector (`transform` processor, contrib 0.160.0; validated and run against `dotnet-api` 2026-11-12: the three attributes gone): processor `transform/aspnetcore-request-scope` with `log_statements: [delete_key(log.attributes, "ConnectionId"), delete_key(log.attributes, "RequestId"), delete_key(log.attributes, "RequestPath")]`, listed in `service.pipelines.logs.processors` of the pipeline that exports the .NET logs (full YAML: [`dotnet/HOW-TO.md`](dotnet/HOW-TO.md) §4) |
| .NET HttpClient health calls | Not traced and sent without trace context (as Go), but still counted in `http.client.request.duration`: the `System.Net.Http` meter has no path attribute to filter on |
| Node.js `http.route` | Only with express; a plain `node:http` server has no route on its spans or its server metric |
| URL redaction | Userinfo: Node.js clients write it as `REDACTED:REDACTED` (`node:http`; `fetch` rejects URLs with credentials and undici drops userinfo from the origin), Go drops it. Query values of `sig`, `Signature`, `AWSAccessKeyId`, `X-Goog-Signature`, `X-Amz-Signature`, `X-Amz-Credential`, `X-Amz-Security-Token`, `access_token`, `X-Goog-Credential` and `GoogleAccessId` are written as `REDACTED` in client `url.full` and server `url.query` in Go, Python and Node.js (contract D1 "URL query redaction", A28, A30; Go records no server `url.query`); other values (`token`, `api_key`), lower-case names and names without a value are kept; Python also redacts the status description and exception events of a failed request, but not under a foreign global TracerProvider. Don't put secrets in query strings |
| AWS attribute values | Aligned since A30 on the semconv v1.43 values: `messaging.system` `aws.sns` / `aws_sqs`, DynamoDB `db.system.name=aws.dynamodb` (plus the legacy `db.system=dynamodb` unless `OTEL_SEMCONV_STABILITY_OPT_IN` holds `database` without `database/dup`), `server.address`/`server.port` = the request host and port (Go and Python; Node.js below), every AWS call span named `<Service>.<Operation>` (Bedrock GenAI spans keep their name), kind CLIENT except SQS/SNS send and publish (PRODUCER) and SQS receive (CONSUMER), `messaging.operation.name` = the AWS operation on SQS and SNS spans, and the legacy RPC keys `rpc.system`, `rpc.service`, `rpc.method=<Operation>` by default — `OTEL_SEMCONV_STABILITY_OPT_IN=http,rpc` gives `rpc.system.name` and `rpc.method=<Service>/<Operation>`, `http,rpc/dup` both with `rpc.method` holding `<Service>/<Operation>` (contract D1 "AWS service attribute values", "AWS RPC keys"). Still different: SNS `messaging.destination.name` (Go the short topic name, Python and Node.js the ARN; none on a Node.js `PublishBatch`), SQS `messaging.destination.name` (Node.js only), `messaging.operation.type` (Go on SNS, Node.js on SQS receive), SNS `error.type` (`InvalidParameter` in Go and Python, `InvalidParameterException` in Node.js) |
| Node.js with a foreign TracerProvider | When another SDK registered the global TracerProvider first, the instrumentations use it, so the helper's health-check drop and gRPC status rule do not apply to their spans |
| Go resource from `OTEL_RESOURCE_ATTRIBUTES` | The Go SDK providers parse the variable again under the helper's resource (`WithResource` merges `resource.Environment()`): a blank `deployment.environment.name=` comes back as `""` instead of being absent, and a key with surrounding Unicode whitespace (e.g. U+00A0) is emitted twice, verbatim and trimmed. Python and Node.js emit only the helper's parse. Workaround: don't set blank values; use ASCII keys ([design D1](https://github.com/StaffOps/otel-libs/blob/main/specs/config-contract-v0.3/design.md)) |
| Go OTLP/HTTP endpoint path escapes | The Go exporters send the decoded path: `/a%2Fb` reaches the Collector as `/a/b` (`WithEndpointURL` keeps `url.URL.Path`). The helper warns once per variable. Python and Node.js send the path as written ([design D1](https://github.com/StaffOps/otel-libs/blob/main/specs/config-contract-v0.3/design.md), "Go: path escapes decoded") |
| Python IDN hosts | Python validates IDN labels by IDNA 2008 (`idna` package), so it rejects some hosts Go and Node.js accept — emoji, `_` next to non-ASCII text, labels over 63 characters, hyphens in the third and fourth position — as `<unparseable URL>` (empty labels and hosts over 253 characters are invalid in every language) ([design D1](https://github.com/StaffOps/otel-libs/blob/main/specs/config-contract-v0.3/design.md), "IDN hosts: Python residual divergence") |
| .NET IDN hosts (branch, task C7a) | .NET converts IDN hosts with `IdnMapping`: without ICU (globalization-invariant mode) a non-ASCII host is invalid (warned "needs ICU", or start fails for a code value); with ICU it rejects labels over 63 characters and labels starting or ending with `-`, as Python does; it depends on the image's ICU (measured with ICU 72; Windows not measured). Hosts `System.Uri` refuses (`a!b`, `a..b`) are invalid too ([design D1](https://github.com/StaffOps/otel-libs/blob/main/specs/config-contract-v0.3/design.md), "IDN hosts: Python residual divergence") |
| Exemplar filter (`OTEL_METRICS_EXEMPLAR_FILTER`) | Go (its SDK reads the variable) and the .NET helper (branch, task C9: parsed by the helper, which always sets the filter because the .NET SDK default is `AlwaysOff`) honor `always_on`, `always_off` and `trace_based`, default `trace_based`. Python always uses `trace_based` until task A33 (the helper passes `TraceBasedExemplarFilter()`); Node.js records no exemplars (sdk-metrics has no exemplar filter) ([design D1](https://github.com/StaffOps/otel-libs/blob/main/specs/config-contract-v0.3/design.md), "Metrics exporters / interval / exemplars") |
| Python log value-length limit with SDK 1.42 | `OTEL_LOGRECORD_ATTRIBUTE_VALUE_LENGTH_LIMIT=0` (or the generic `0`) is invalid by contract (warned, no limit), but the SDK 1.42 `LoggerProvider` takes no limits, so log attribute values are still emptied there; SDKs whose `LoggerProvider` accepts `log_record_limits` (1.45) follow the contract ([design D1](https://github.com/StaffOps/otel-libs/blob/main/specs/config-contract-v0.3/design.md), "Value-length limits") |
| Python with `OTEL_PROPAGATORS=none` | Fixed on this branch (task A34, 2026-11-10); **affected release `0.3.0rc3`**: every FastAPI request answers 500 under `none`, with or without trace headers. On `0.3.0rc3` leave `OTEL_PROPAGATORS` unset or use `tracecontext` / `baggage` until the next Python release candidate |
| Node.js with traces disabled | `OTEL_HELPER_DISABLED_SIGNALS=traces` still injects a root span's `traceparent`; Go, Python and the .NET branch send none (no-op tracer) and pass an inbound context on unchanged (task A35; case `propagation.traces_disabled_root_no_traceparent`) |
| OTLP redirects in Go, Python and Node.js | The contract refuses redirects (owner decision 2026-11-10, task A31): a `3xx` on an OTLP export is a failed export with a loud diagnostic. Until A31, Go and Python follow 307/308 (Go also forwards the custom OTLP headers to the new host), and Node.js fails the export but writes no helper line; the .NET helper (branch, task C8) already refuses them with an ERROR line ([design D1](https://github.com/StaffOps/otel-libs/blob/main/specs/config-contract-v0.3/design.md), "OTLP export redirects") |
| .NET value-length limit 0 (branch, task C8) | `OTEL_ATTRIBUTE_VALUE_LENGTH_LIMIT=0` (or the span or log record variant) is invalid by contract; the .NET helper warns, but the OTLP exporter reads the variable itself (no public override) and sends those values empty ([design D1](https://github.com/StaffOps/otel-libs/blob/main/specs/config-contract-v0.3/design.md), "Value-length limits") |
| Python `/metrics` scope labels on the lower bound | With `opentelemetry-exporter-prometheus` 0.63b0 (sdk 1.42) series carry no `otel_scope_name` / `otel_scope_version` labels; from 0.64b0 (sdk 1.43) they are written as in Go and Node.js (case `metrics.scrape_target_info_and_scope_label`, `python@sdk>=1.43`) |
| Go Prometheus names with a unit word, `total` or `ratio` inside | Go's exporter (`otlptranslator`) removes those words wherever they appear in the name: `probe.bytes-sent` in `By` → `probe_bytes_sent_total`, `req.total.count` → `req_count_total`, a Gauge `ratio.load` in `1` → `load_ratio`. Python and Node.js add a suffix only when the name does not already end with it (`probe_bytes_sent_bytes_total`, `req_total_count_total`, `ratio_load_ratio`) ([design D1](https://github.com/StaffOps/otel-libs/blob/main/specs/config-contract-v0.3/design.md), "Suffix semantics") |
| Go scrape with an `otel.scope.*` data-point attribute | A data-point attribute named `otel.scope.name` (or `_version`, `_schema_url`) makes Go's `/metrics` fail (`duplicate label names`); Python and Node.js keep the data-point value. Don't use those attribute names |
| .NET `/metrics` unit-`1` Gauges (branch, task C12) | The .NET exporter (`OpenTelemetry.Exporter.Prometheus.HttpListener` 1.20.1-beta.1) adds no `_ratio` suffix to a Gauge in unit `1` (`parity_utilization`, `ratio_load`); Go, Python and Node.js write `parity_utilization_ratio`. Parity `skip:no-upstream-dotnet-prom-ratio` |
| .NET `/metrics` colliding label values (branch, task C12) | Colliding attribute names are merged into one label, but the .NET exporter joins the values in attribute order (`a_b="y;x"`, `target_info{x_y="2;1"}`); Go, Python and Node.js sort them (`a_b="x;y"`). Parity `skip:no-upstream-dotnet-prom-label-order` |
| .NET `/metrics` bind address (branch, task C12) | The .NET `HttpListener` cannot bind an IPv6 literal or a dual-stack socket: every interface (unset, `0.0.0.0`, `::`, `[::]`) is every IPv4 interface (`::` also warns); `OTEL_EXPORTER_PROMETHEUS_HOST=::1` binds `127.0.0.1` (one warning); any other IPv6 address gets no listener (one ERROR, never widened). Go, Python and Node.js bind the IPv6 address. A host-bound .NET listener routes by the `Host` header: `127.0.0.1` also answers `localhost` for Go and .NET clients (the `localhost` prefix binds the resolver's first loopback address, `::1` when there is one); curl and Node.js, resolving with `AI_ADDRCONFIG` on a host whose only IPv6 address is `::1`, get 404 there and must use `127.0.0.1`; any other address answers only requests naming it |
| Python disabled-metric patterns and mixed-case names | The Python SDK lower-cases instrument names, so `OTEL_HELPER_DISABLED_METRICS=Orders.*` matches `Orders.Total` in Go and Node.js but nothing in Python (warned once, the pattern is not echoed), and a lower-case `orders.*` drops `Orders.Total` in Python while Go and Node.js keep it (patterns are case-sensitive). Use lower-case instrument names and patterns ([design D1](https://github.com/StaffOps/otel-libs/blob/main/specs/config-contract-v0.3/design.md), "Disabled-metric patterns") |
| Stdout log fallback `logger` field | Python and Node.js write the logger name as `logger` in every line of the stdout JSON fallback; Go's `NewLogger` / `NewSlogHandler` take no name, so a Go line has a `logger` field only when the record (or its `With` attributes) carries a top-level `logger` attribute — written once, the last one winning. Pass `"logger", "<name>"` with `With` to get it on every Go line. The .NET branch (task C13) writes the logger category; parity case `logging.stdout_logger_field` is `skip:no-logger-name` for Go. The other fields — `time` (UTC, `Z`), `level`, `msg`, `exception` — are present everywhere; `exception` holds each language's native text (Go `Error()`, Python traceback, Node.js stack) ([design D1](https://github.com/StaffOps/otel-libs/blob/main/specs/config-contract-v0.3/design.md), "Stdout fallback fields") |
| Database error text in `exception.message` | Failed DB spans keep status ERROR without a description in every language (A25; Python on both `instrument_sql()` and the default `SQL` path of `setup_telemetry()`, not when other code instrumented SQLAlchemy first, which is warned), but the semconv `exception` event is kept: Go writes the driver's error text there (it can quote a statement value, e.g. a duplicate key); Node.js pg writes a sanitized message (type and SQLSTATE), Node.js mysql2 no event, Python none on statement spans (the `connect` span's event carries the error text). Node.js failed SQL spans also carry `error.type=_OTHER` (its status wrapper sees the status, not the driver error; Go and Python set no `error.type` on SQL spans — Go's is on the duration metric only). Redis (A26): the status rule covers it in every language (a known reply code — `WRONGTYPE`, `ERR`, `NOAUTH`, ... from a fixed allowlist — becomes `db.response.status_code` and `error.type`, anything else `_OTHER`, so a Lua `error_reply`'s first word never reaches an attribute; Python on redis-py < 8 reads it as `ERR`), and the exception event carries the server text, which for an unknown command or a Lua `error_reply` can quote arguments (`ERR unknown command 'X', with args beginning with: '<arg>'`); a Python pipeline error's event keeps the command names only (`Command # 2 (LPUSH ? ?) of pipeline caused error: …`). **.NET fixed ([ADR-014](https://github.com/StaffOps/otel-libs/blob/main/specs/decisions/ADR-014-exception-detail-in-correlated-logs.md)): the event keeps only `exception.type` and the detail is in a correlated log record; Go, Python and Node.js until task A36** |
| Redis `db.operation.name` and span names | Every language records `db.operation.name` on command spans as the client provides it (ioredis and Go `set`, node-redis and redis-py `SET`; none on Go and Python pipeline spans, Node.js `MULTI`/`PIPELINE` forms). Span names are the uppercased command everywhere (`SET`); a pipeline is one span in Go (`SET GET`, each name once) and Python (`SET GET`, every command), one span per command in Node.js; Go's dial span is `connect`, as Node.js's connection spans (A30; [design D1](https://github.com/StaffOps/otel-libs/blob/main/specs/config-contract-v0.3/design.md), "Redis span names", "Redis `db.operation.name`"). A subcommand differs: Go `CLUSTER INFO`, Python `CLUSTER` |
| Node.js Redis metrics | None: `@opentelemetry/instrumentation-redis` / `-ioredis` record no metrics. Go `ext/otelredis` emits the redisotel pool metrics (`db.client.connections.*`); Python none |
| Node.js ioredis handshake spans | ioredis traces every connect and reconnect handshake (`connect`, `HELLO`, `CLIENT`, `INFO`; Cluster `CLUSTER SLOTS`) as root traces, ERROR when the server rejects the auth or is loading. Drop them at the Collector or wrap the client's lifetime in a span ([Node.js HOW-TO §9](nodejs/HOW-TO.md#redis-staffopsotel-helper-redis)) |
| Node.js AWS spans | No `server.address` / `server.port`: instrumentation-aws-sdk hands its hooks a request without the endpoint, so the helper cannot read it (upstream limitation; Go and Python write them, A30). The legacy `rpc.*` keys as Python (row above) |
| Node.js runtime metrics and exemplars | Node.js emits no runtime metrics (Go `go.*`, Python `system.*` / `process.*`) and its metric points carry no exemplars (`@opentelemetry/sdk-metrics` 3.0 exports no exemplar filter) |
| Go Redis pool metrics keep `db_system` | With `OTEL_SEMCONV_STABILITY_OPT_IN=database` (without `/dup`) Go `ext/otelredis` drops the legacy `db.system` from spans, but its pool metrics (`db_client_connections_*`) keep `db_system="redis"`: redisotel adds it to the metric attributes and Go has no meter wrapper ([design D1](https://github.com/StaffOps/otel-libs/blob/main/specs/config-contract-v0.3/design.md), "Database semconv opt-in") |
| Go application error handler set after `Setup` | The helper's OTel error handler (drops OTLP/HTTP response bodies, masks the configured header values) wraps a handler installed **before** `Setup`; it receives a sanitized chain (`errors.Is` and `status.FromError` work; `errors.As` fails for every typed error of the chain, `*url.Error` included). `otel.SetErrorHandler` called **after** `Setup` replaces it (last writer wins), and that handler receives the raw SDK errors, body included. No sanitizer is exported (owner decision) ([Go HOW-TO](go/HOW-TO.md#sdk-errors-response-bodies-and-header-values), design D1 "SDK error text", A29) |
| Go SDK internal logger | The Go OTLP exporters parse `OTEL_EXPORTER_OTLP_*` again when they are built and report problems through the SDK's own logger (`otel.SetLogger`, stderr by default), outside the helper's masking: a malformed `OTEL_EXPORTER_OTLP_HEADERS` item or a value with a bad `%` escape is printed whole (`envconfig.go:166,181`), and so is an endpoint value it cannot parse (a schemeless value with a path). Keep header values well formed ([design D1](https://github.com/StaffOps/otel-libs/blob/main/specs/config-contract-v0.3/design.md), "SDK's own log output of env values") |
| SDK error text redaction depth | Every language drops response bodies (Go by its handler; Python's exporters never log one; Node.js by `withoutResponseBodies`) and masks the configured OTLP header values. Node.js also applies pattern-based redaction (credential-named `key=value` pairs, `Cookie`, URL userinfo) to forwarded SDK text; Go and Python mask only the configured values, so a credential the helper was not given is printed as the SDK wrote it |
| .NET database `db.system` / `db.system.name` (branch, task C14a) | On spans the helper follows R10 as Go does: both keys by default, only `db.system.name` when `OTEL_SEMCONV_STABILITY_OPT_IN` holds `database` without `database/dup` (SqlClient gets `db.system=mssql`, Npgsql `postgresql`, MySqlConnector `db.system.name=mysql` and `db.query.text` from its `db.statement`). The drivers' **metrics** keep what each driver writes (no view can add an attribute): SqlClient and Npgsql `db.system.name` only, MySqlConnector's pool metrics no system key at all. MySqlConnector also parses the variable itself (exact lower-case tokens, read once per process), so `database/dup` set in the deployment gives its native new keys too |
| .NET database query text and connection attributes (branch, task C14a) | .NET is stricter than Go and Python here: Npgsql and MySqlConnector record the command text as written, and the helper replaces its literals with `?` and drops comments (a PostgreSQL string with a backslash drops the rest of the statement, as its meaning depends on `standard_conforming_strings`); Go, Python and Node.js record the statement text the client sends, placeholders included, without that step; SqlClient sanitizes upstream. The helper also removes `db.connection_string`, `db.user` (in every mode) and every `db.query.parameter.*` and redacts password keys in `db.npgsql.data_source`. On the `Npgsql` and `MySqlConnector` meters a view drops the pool-name attribute (`db.client.connection.pool.name`, `pool.name`: an unnamed pool is named after its connection string, `SSL Password` / `Certificate Password` included) and keeps an allowlist of semconv keys, so pools are merged and a new driver attribute is dropped until listed |
| .NET MySqlConnector failed-command status (branch, task C14a) | MySqlConnector 2.6 sets a failed command's status after its span ended: the helper's OTLP and console exporters drop the description (ERROR kept, A25), but a processor or exporter the application adds sees it (the error code name, e.g. `NoSuchTable`, or for a non-MySQL error the exception message), also when the app traces only through its own exporter. `OTEL_HELPER_DISABLED_SIGNALS=traces` makes the helper's sampler record nothing, which also silences the application's own exporters on that provider; `OTEL_TRACES_EXPORTER=none` keeps the sampler and only drops the helper's exporter |
| .NET database exception events (branch, task C14a, ADR-014) | .NET fixed (ADR-014); Go, Python and Node.js until A36. On failed SqlClient, Npgsql and MySqlConnector spans the `exception` event keeps only `exception.type` (and its other non-detail attributes); the message and the stack trace go to one ERROR log record of category `StaffOps.OtelHelper.Database` with the span's `trace_id` / `span_id` (OTLP; on the stdout fallback line as `trace_id` / `span_id` fields). The event list is rebuilt through a private `Activity` field (no public API; one warning and no change if a runtime lacks it). MySqlConnector adds its event after the span ended: an application processor or exporter still sees its message (as its status description, row above) and no record is written for it on that path; the helper's exporters handle it (an ADR-014 exception: export through the helper, or delete the two attributes at the Collector — `dotnet/HOW-TO.md` §14, not tested) |
| .NET Redis failed commands and batches (branch, task C14b) | `OpenTelemetry.Instrumentation.StackExchangeRedis` builds its spans from the client's profiler after each command, without the result: a failed command (WRONGTYPE, a Lua error, an unknown command, a wrong `AUTH`) is **not** marked ERROR — status unset, no `error.type`, no `db.response.status_code`, no `exception` event (measured on `redis:7.4.12-alpine`). Go, Python and Node.js set ERROR without a description and the reply code (A26). Error ratios, the Tempo service graph and `status = error` tail sampling see 0% errors for .NET Redis: count failures in the application. `CreateBatch` commands produce no span at all (StackExchange.Redis does not profile batches). Span names are the command (`SET`); a transaction is one `EXEC` span (Python one `SET GET` pipeline span, Node.js `MULTI`). The statement is the command plus one `?` (`SET ?`; Python one `?` per argument, Go no statement); subcommands are not kept ([.NET HOW-TO §14](dotnet/HOW-TO.md#redis-semantic-conventions-and-failed-commands)) |
| .NET Redis keys and `database/dup` (branch, task C14b) | Owner decision O8 (b), 2026-11-11: .NET Redis spans follow the R10 rule as Go and the SQL drivers — by default the helper adds `db.system.name=redis`, `db.operation.name` (the command, upper case `SET`, as node-redis and redis-py; Go and ioredis `set`) and `db.query.text` next to the instrumentation's legacy `db.system`, `db.statement`, `db.redis.database_index`; `OTEL_SEMCONV_STABILITY_OPT_IN=database` keeps only the new keys. `db.namespace` is written only by the instrumentation, with `database/dup` or `database` in the process environment (Go and Python write none) |
| .NET AWS attribute values (branch, task C14c) | `OpenTelemetry.Instrumentation.AWS` 1.20.3 (its `V1_28_0` default) writes the legacy RPC keys as every language's default (`rpc.system=aws-api`, `rpc.service`, `rpc.method=<Operation>`), but not the A30 alignment: no `server.address` / `server.port` (Go and Python write them), no `messaging.system`, `messaging.operation.name` or `messaging.destination.name` on SQS / SNS spans, DynamoDB `db.system=dynamodb` without `db.system.name=aws.dynamodb`, the SQS queue URL as `aws.queue_url`, the legacy `http.status_code`; `SQS.SendMessage` and `SNS.Publish` are CLIENT (PRODUCER in Go, Python and Node.js; receive not measured); an S3 `HEAD` is named `S3.GetObjectMetadata` (the .NET request name; `S3.HeadObject` elsewhere). The helper does not apply the `OTEL_SEMCONV_STABILITY_OPT_IN=rpc` mapping to AWS spans (not measured whether upstream reads it). A transport error's `error.type` is its class (`HttpRequestException`, as Python's `EndpointConnectionError`; Go `_OTHER`); a publish to a phone number records no destination (as Go; Python and Node.js `phone_number:**`) ([dotnet HOW-TO §14](dotnet/HOW-TO.md#aws-spans-failed-calls-and-url-redaction)) |
| .NET AWS exception events (branch, task C14c, ADR-014) | .NET applies ADR-014 to AWS SDK spans: the `exception` event keeps only `exception.type` and the message goes to one ERROR log record of category `StaffOps.OtelHelper.Aws` with the span's ids (the SDK copies the service message verbatim and DynamoDB quotes request literals in it, measured on DynamoDB Local); AWSSDK.Core's `exception.message` / `exception.stacktrace` span attributes are removed. The error text of a failed call can include an SNS phone number or item values: it is never on a span, but it is in that log record (or on stdout without a logs exporter), so treat the log pipeline as sensitive. Python and Node.js keep the AWS error message in the `exception` event (the A27 residual), Go records no event ([Exceptions: traces vs logs](dotnet/HOW-TO.md#exceptions-traces-vs-logs)) |
| .NET AWS client spans (branch, task C14c) | One CLIENT span per call: `AddOtelHelperAws()` suppresses the HttpClient span under it, whose `url.full` path would be the S3 object key. An AWS client built before the tracer provider, or an `AddAWSInstrumentation()` of the application's own registered first (AWSSDK.Core keeps the first), gets that HttpClient span back; the helper replaces its URL path with `/` (scheme, host, port and redacted query kept), so the child span may exist but carries no key (fix round 1, measured on .NET 8 and 10) |
| .NET URL query redaction (branch, task C14c, owner decision O3) | .NET keeps upstream's text, stricter than the contract list: server `url.query` and the .NET 8 client `url.full` write every value as `Redacted`, the .NET 9+ client `url.full` writes the query as `*`. With upstream redaction turned off (`OTEL_DOTNET_EXPERIMENTAL_ASPNETCORE_DISABLE_URL_QUERY_REDACTION`, `OTEL_DOTNET_EXPERIMENTAL_HTTPCLIENT_DISABLE_URL_QUERY_REDACTION` on .NET 8, `DOTNET_SYSTEM_NET_HTTP_DISABLEURIREDACTION` on .NET 9+) the helper writes the ten contract names as `REDACTED`, as Go, Python and Node.js, and keeps the rest. Scope: the span URL attributes (`url.full`, `http.url`, `url.query`, `http.target`, also as `Uri` values), not free text — HttpClient error texts carry no URL (measured), but an application exception message embedding a URL is not covered ([dotnet HOW-TO §14](dotnet/HOW-TO.md#aws-spans-failed-calls-and-url-redaction)) |
| Stdout `trace_id` / `span_id` (contract A23 amendment 2026-11-11) | A stdout fallback line for a record emitted in a span context carries `trace_id` and `span_id` right after `logger`: .NET does (branch, task C14a; the database exception record uses its span's ids and the event time); Go, Python and Node.js write neither until task A36 (case `logging.stdout_span_context`, `xfail:A36`) |
| .NET warning cadence (branch, tasks C6, C16) | Helper warnings that are "once" — the deprecations (`SERVICE_NAME`, `ENVIRONMENT`, `OTEL_HELPER_SAMPLE_RATIO`, `OTEL_HELPER_DISABLED_SIGNALS`, `OTEL_HELPER_EXTRA_INSTRUMENTATION`) and the identity warnings — are written once per diagnostics sink (container): two hosts in one process each write them; Go, Python and Node.js write them once per process |
| .NET profiling (branch, task C15) | Only .NET has a profiling package (`StaffOps.OtelHelper.Profiling`, Grafana Pyroscope's native CLR profiler); Go, Python and Node.js have none. Its profiles go from the native profiler straight to `PYROSCOPE_SERVER_ADDRESS`, not through the Collector, and root spans get `pyroscope.profile.id`. The helper starts it at host start only when the `CORECLR_*`, `LD_PRELOAD` and `PYROSCOPE_*` prerequisites are met, else one `otelhelper: WARN profiling prerequisites not met (…)` line on standard error; measured on `linux/amd64` only ([`dotnet/HOW-TO.md`](dotnet/HOW-TO.md#profiling-pyroscope)) |
| .NET | Still on the 0.2.x contract and instrumentation set until Phase C ([differences](#net-02x-differences)); the Prometheus variables, the gRPC path warning and the SDK error text rule (A29) land in Phase C (C12, C7b, C8) |

## Collector and platform prerequisites

The libraries assume a Collector and platform configured for them. These items
live outside this repo and block the 0.3.0 rollout; the full checklist, with
the reason for each item, is
[design D11](https://github.com/StaffOps/otel-libs/blob/main/specs/config-contract-v0.3/design.md#d11--collector--platform-prerequisites-reference-checklist):

1. Tail sampling: a default branch for spans without
   `deployment.environment.name`, at the PRD rate.
2. The `debug=true` policy combined (`and`) with an environment allow-list and
   a `rate_limiting` sub-policy. Debug mode marks every entry span in every
   language — Go, Python and Node.js spans with no parent or a remote parent, .NET spans
   with no in-process parent (which also prepend `debug=true` to the propagated
   tracestate) — so a debug-enabled service marks every inbound request.
3. Gateway `loadbalancingexporter` routed by trace ID; `decision_wait` ≥ the
   observed p99 trace duration, and the sampled-decision cache enabled for late
   spans.
4. Charts set `OTEL_SERVICE_NAME` and `OTEL_RESOURCE_ATTRIBUTES` with
   `deployment.environment.name`; no `service.instance.id` for multi-worker
   containers.
5. Istio / mesh tracing at 100% or disabled (it interacts with the
   ParentBased sampler).
6. VictoriaMetrics OTLP ingest ignores volatile resource attributes
   (`-opentelemetry.ignoreResourceAttributes`, e.g. `process.pid`) and keeps
   `service.instance.id`.
7. A Collector `filterprocessor` dropping health spans, as defense in depth.
8. An explicit `:4317` on portless gRPC URLs.

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

Shared Grafana dashboards in [`dashboards/`](https://github.com/StaffOps/otel-libs/tree/main/dashboards/) query the example
apps' business metrics, Collector `spanmetrics` and Tempo. They are language
agnostic.

## More

- [Changelog and migration guide](CHANGELOG.md)
- [Specs and decision records](https://github.com/StaffOps/otel-libs/blob/main/specs/README.md)
- [Contributor and agent guide](https://github.com/StaffOps/otel-libs/blob/main/AGENTS.md)
- [Source repository](https://github.com/StaffOps/otel-libs)

## License

[Apache License 2.0](https://github.com/StaffOps/otel-libs/blob/main/LICENSE) — the same license as OpenTelemetry.
