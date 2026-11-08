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
| .NET | [`dotnet/`](dotnet/) | `StaffOps.OtelHelper` + `.AWS` / `.Redis` / `.Sql` / `.Profiling` (NuGet; on this branch, not published yet — released `0.2.x` is `OtelHelper*`, ADR-011) | latest release `0.2.1` (`OtelHelper*`, tag `dotnet/v0.2.1`) | 0.2.x — the 0.3 contract lands in Phase C |
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
   ([ADR-002](specs/decisions/ADR-002-parentbased-sampler.md); .NET 0.2.x uses
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
| `OTEL_EXPORTER_PROMETHEUS_HOST` | every interface | Bind address of the listener: an IPv4 or IPv6 literal (brackets allowed, no zone id) or `localhost` (= `127.0.0.1`); `::` = every interface. **Unset binds every interface, not the spec default `localhost`**, so other pods can scrape it. Another value (a host name, `*`) is warned and **every interface** is used — wider than the restriction it was meant to set (owner decision). Never turns the listener on by itself (A29) |
| `OTEL_METRIC_EXPORT_INTERVAL` | `30000` | Milliseconds (helper default 30 s; SDK default 60 s): ASCII digits with an optional `+` (any length), > 0, else warned and the default; above 2 147 483 647 clamped with a warning. A code interval outside [1, 2 147 483 647] fails |
| `OTEL_CONFIG_FILE`, `OTEL_EXPERIMENTAL_CONFIG_FILE` | _(unset)_ | Declarative configuration is **not supported**: set, it is ignored with one warning per process naming the variable (never the path); `OTEL_EXPERIMENTAL_CONFIG_FILE` is the deprecated name |
| `OTEL_TRACES_SAMPLER` / `OTEL_TRACES_SAMPLER_ARG` | `parentbased_always_on` | The six spec samplers (ASCII case-insensitive). Unknown names warn and use the default. With services in several languages use `parentbased_traceidratio`, not bare `traceidratio`: the SDKs (Node.js and .NET in particular) derive the ratio decision from the trace id differently, so bare ratio decisions are not consistent across a trace |
| `OTEL_PROPAGATORS` | `tracecontext,baggage` | Supported: `tracecontext`, `baggage`, `none`. Others (`b3`, `jaeger`, `xray`, …) are warned (one warning each, naming the effective list) and skipped; `none` with other values warns and disables propagation |
| `OTEL_SEMCONV_STABILITY_OPT_IN` | Python: `http,database/dup` | Python sets this default before instrumenting (stable HTTP semconv, both DB keys); a user value wins for HTTP. DB keys in every language: both `db.system.name` and the legacy `db.system` unless the value holds `database` without `database/dup` (A25; dup emits the full new and legacy sets). Python treats an empty value as unset, ASCII-folds every token (`HTTP/DUP` = `http/dup`) and writes the normalized value back to the environment (inherited by child processes). Go instrumentations emit stable HTTP semconv natively AWS RPC keys (A30): legacy by default in every language; `http,rpc` gives the current keys, `http,rpc/dup` both (a bare `rpc` has no `http` token: Python then uses the old HTTP conventions and Node.js warns). In Go `rpc/dup` also switches otelgrpc to dup, doubling the RPC metric series |

### Helper-specific

| Variable | Default | Behavior |
|----------|---------|----------|
| `OTEL_HELPER_DEBUG_LEVEL` | `false` | Debug log level and `debug=true` on entry spans (no parent or a remote parent) for Collector tail-sampling policies ([ADR-006](specs/decisions/ADR-006-debug-span-attribute.md)); the Collector policy needs an environment allow-list and a rate limit ([prerequisites](#collector-and-platform-prerequisites)). Python debug mode also turns on every installed `ext` instrumentor (SQL, Redis, AWS). Do not leave on in production |
| `OTEL_HELPER_METRICS_PORT` | `9464` | Standalone Prometheus `/metrics` listener port when `OTEL_EXPORTER_PROMETHEUS_PORT` is unset or invalid: ASCII digits with an optional `+`, in [0, 65535] (else warned, 9464); dual-stack, `GET`/`HEAD /metrics` only. `0` disables the listener (the mountable handler keeps working) |
| `OTEL_HELPER_DISABLED_METRICS` | _(unset)_ | Comma list of case-sensitive instrument-name globs to drop from every exporter: `*` any run of characters, `?` one character, everything else literal |
| `OTEL_HELPER_HTTP_OPT_IN_METRICS` | `false` | `true` also records the HTTP metrics semconv makes opt-in: `http.server.active_requests` and the request/response body sizes (code: Go `WithHTTPOptInMetrics`, Python `http_opt_in_metrics`, Node.js `httpOptInMetrics`; code wins). By default only `http.server.request.duration` and `http.client.request.duration` are recorded. Go has no active-requests metric upstream; Node.js records none of them (A30, [design D1](specs/config-contract-v0.3/design.md) "HTTP opt-in metrics") |
| `OTEL_HELPER_SAMPLE_RATIO` | `1.0` | **Deprecated** → `OTEL_TRACES_SAMPLER=parentbased_traceidratio` + `OTEL_TRACES_SAMPLER_ARG`. Used only while `OTEL_TRACES_SAMPLER` is unset; builds `ParentBased(TraceIdRatioBased(x))`; a value that is not a plain decimal in [0, 1] warns and is ignored |
| `OTEL_HELPER_DISABLED_SIGNALS` | _(unset)_ | **Deprecated** → `OTEL_{TRACES,METRICS,LOGS}_EXPORTER=none`. A signal whose `OTEL_<SIGNAL>_EXPORTER` holds a valid entry (`none` included; only unknown entries count as unset) is not disabled by it, nor are metrics when code sets the metric exporters; unknown entries warn once |
| `OTEL_HELPER_EXTRA_INSTRUMENTATION` | Python: `SQL` | **Go and Node.js: removed** (no effect, warns; Node.js: pass `sqlInstrumentation()` / `redisInstrumentation()` / `awsInstrumentation()` in `setup({ instrumentations })`). **Python: deprecated** — call `staffops_otel_helper.ext.instrument_sql()` / `instrument_redis()` / `instrument_aws()` before `setup_telemetry()` |

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
[`specs/config-contract-v0.3/tasks.md`](specs/config-contract-v0.3/tasks.md).

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

## Cross-language parity

Parity is enforced, not promised. [`parity/cases.json`](parity/cases.json) is a
hand-maintained, language-neutral fixture (342 cases: 187 after A13, 17 added by the Node B3 review, 6 by A16, 40 by Node B4a and its rulings, 16 by A17, 2 by Node B4b, 13 by A18, 14 by A19 and its fix round, 1 by Node B5, 5 by A20, 3 by Node B7, 11 by A21, 6 by the B7/A21 fix rounds, 2 by A22 from the Node B6 fix round, 1 by A23 from Node B8, 18 by A29)
that every language's test suite runs — config resolution, wire-level export
against a fake OTLP receiver, sampling, propagation, health exclusion, logging
and `/metrics` status
([ADR-008](specs/decisions/ADR-008-shared-parity-fixture.md)). A language that
lags marks a case `xfail:<reason>`; cases are never deleted to make CI green.
Format and runner details: [`parity/README.md`](parity/README.md); the
end-to-end smoke against a real Collector: [`testing/`](testing/README.md).

### Known emitted-telemetry gaps

The configuration contract is shared; what the instrumentations emit is not
always identical:

| Area | Gap |
|------|-----|
| Python and Node.js gRPC | Spans carry the legacy `rpc.system`, `rpc.service`, `rpc.method` and `rpc.grpc.status_code` attributes, and no RPC metrics are emitted (upstream `opentelemetry-instrumentation-grpc`; `@opentelemetry/instrumentation-grpc` 0.300 in the Node.js helper, release candidate `0.3.0-rc.1`). Go emits the current RPC semconv and `rpc.server.call.duration` / `rpc.client.call.duration`. Node.js error spans also carry the non-semconv `grpc.error_name` / `grpc.error_message` and no `error.type` |
| Node.js gRPC handler that throws synchronously | Its SERVER span is never ended or exported (upstream `@opentelemetry/instrumentation-grpc`); Go and Python record the call with status UNKNOWN. Return errors through the callback or a rejected promise |
| Node.js routers created before `setup()` | An express app or router built before `setup()` gets a truncated `http.route` (`GET /api`) or none; require routes modules after `setup()` (the helper warns when express or grpc-js was loaded first) |
| Node.js HTTP/2 | `node:http2` servers and clients (h2c included) are not instrumented: there is no official HTTP/2 instrumentation |
| HTTP opt-in metrics | By default the instrumentation the helper installs records, in every language, only `http.server.request.duration` and `http.client.request.duration` (A30). With `OTEL_HELPER_HTTP_OPT_IN_METRICS=true` Python adds `http.server.active_requests` and the server body sizes (no client body sizes), Go the body sizes only (otelhttp has no active-requests metric), Node.js nothing (instrumentation-http records only the durations) ([design D1](specs/config-contract-v0.3/design.md), "HTTP opt-in metrics") The drop applies to the contrib instrumentations' scopes only, so an application's own instrument of those names is kept (Python: the FastAPI, ASGI, requests and httpx scopes, a closed list; another contrib instrumentation the app adds keeps its series) |
| Node.js `http.route` | Only with express; a plain `node:http` server has no route on its spans or its server metric |
| URL redaction | Userinfo: Node.js clients write it as `REDACTED:REDACTED` (`node:http`; `fetch` rejects URLs with credentials and undici drops userinfo from the origin), Go drops it. Query values of `sig`, `Signature`, `AWSAccessKeyId`, `X-Goog-Signature`, `X-Amz-Signature`, `X-Amz-Credential`, `X-Amz-Security-Token`, `access_token`, `X-Goog-Credential` and `GoogleAccessId` are written as `REDACTED` in client `url.full` and server `url.query` in Go, Python and Node.js (contract D1 "URL query redaction", A28, A30; Go records no server `url.query`); other values (`token`, `api_key`), lower-case names and names without a value are kept; Python also redacts the status description and exception events of a failed request, but not under a foreign global TracerProvider. Don't put secrets in query strings |
| AWS attribute values | Aligned since A30 on the semconv v1.43 values: `messaging.system` `aws.sns` / `aws_sqs`, DynamoDB `db.system.name=aws.dynamodb` (plus the legacy `db.system=dynamodb` unless `OTEL_SEMCONV_STABILITY_OPT_IN` holds `database` without `database/dup`), `server.address`/`server.port` = the request host and port (Go and Python; Node.js below), every AWS call span named `<Service>.<Operation>` (Bedrock GenAI spans keep their name), kind CLIENT except SQS/SNS send and publish (PRODUCER) and SQS receive (CONSUMER), `messaging.operation.name` = the AWS operation on SQS and SNS spans, and the legacy RPC keys `rpc.system`, `rpc.service`, `rpc.method=<Operation>` by default — `OTEL_SEMCONV_STABILITY_OPT_IN=http,rpc` gives `rpc.system.name` and `rpc.method=<Service>/<Operation>`, `http,rpc/dup` both with `rpc.method` holding `<Service>/<Operation>` (contract D1 "AWS service attribute values", "AWS RPC keys"). Still different: SNS `messaging.destination.name` (Go the short topic name, Python and Node.js the ARN; none on a Node.js `PublishBatch`), SQS `messaging.destination.name` (Node.js only), `messaging.operation.type` (Go on SNS, Node.js on SQS receive), SNS `error.type` (`InvalidParameter` in Go and Python, `InvalidParameterException` in Node.js) |
| Node.js with a foreign TracerProvider | When another SDK registered the global TracerProvider first, the instrumentations use it, so the helper's health-check drop and gRPC status rule do not apply to their spans |
| Go resource from `OTEL_RESOURCE_ATTRIBUTES` | The Go SDK providers parse the variable again under the helper's resource (`WithResource` merges `resource.Environment()`): a blank `deployment.environment.name=` comes back as `""` instead of being absent, and a key with surrounding Unicode whitespace (e.g. U+00A0) is emitted twice, verbatim and trimmed. Python and Node.js emit only the helper's parse. Workaround: don't set blank values; use ASCII keys ([design D1](specs/config-contract-v0.3/design.md)) |
| Go OTLP/HTTP endpoint path escapes | The Go exporters send the decoded path: `/a%2Fb` reaches the Collector as `/a/b` (`WithEndpointURL` keeps `url.URL.Path`). The helper warns once per variable. Python and Node.js send the path as written ([design D1](specs/config-contract-v0.3/design.md), "Go: path escapes decoded") |
| Python IDN hosts | Python validates IDN labels by IDNA 2008 (`idna` package), so it rejects some hosts Go and Node.js accept — emoji, `_` next to non-ASCII text, labels over 63 characters, hyphens in the third and fourth position — as `<unparseable URL>` (empty labels and hosts over 253 characters are invalid in every language) ([design D1](specs/config-contract-v0.3/design.md), "IDN hosts: Python residual divergence") |
| Python log value-length limit with SDK 1.42 | `OTEL_LOGRECORD_ATTRIBUTE_VALUE_LENGTH_LIMIT=0` (or the generic `0`) is invalid by contract (warned, no limit), but the SDK 1.42 `LoggerProvider` takes no limits, so log attribute values are still emptied there; SDKs whose `LoggerProvider` accepts `log_record_limits` (1.45) follow the contract ([design D1](specs/config-contract-v0.3/design.md), "Value-length limits") |
| Python `/metrics` scope labels on the lower bound | With `opentelemetry-exporter-prometheus` 0.63b0 (sdk 1.42) series carry no `otel_scope_name` / `otel_scope_version` labels; from 0.64b0 (sdk 1.43) they are written as in Go and Node.js (case `metrics.scrape_target_info_and_scope_label`, `python@sdk>=1.43`) |
| Go Prometheus names with a unit word, `total` or `ratio` inside | Go's exporter (`otlptranslator`) removes those words wherever they appear in the name: `probe.bytes-sent` in `By` → `probe_bytes_sent_total`, `req.total.count` → `req_count_total`, a Gauge `ratio.load` in `1` → `load_ratio`. Python and Node.js add a suffix only when the name does not already end with it (`probe_bytes_sent_bytes_total`, `req_total_count_total`, `ratio_load_ratio`) ([design D1](specs/config-contract-v0.3/design.md), "Suffix semantics") |
| Go scrape with an `otel.scope.*` data-point attribute | A data-point attribute named `otel.scope.name` (or `_version`, `_schema_url`) makes Go's `/metrics` fail (`duplicate label names`); Python and Node.js keep the data-point value. Don't use those attribute names |
| Python disabled-metric patterns and mixed-case names | The Python SDK lower-cases instrument names, so `OTEL_HELPER_DISABLED_METRICS=Orders.*` matches `Orders.Total` in Go and Node.js but nothing in Python (warned once, the pattern is not echoed), and a lower-case `orders.*` drops `Orders.Total` in Python while Go and Node.js keep it (patterns are case-sensitive). Use lower-case instrument names and patterns ([design D1](specs/config-contract-v0.3/design.md), "Disabled-metric patterns") |
| Stdout log fallback `logger` field | Python and Node.js write the logger name as `logger` in every line of the stdout JSON fallback; Go's `NewLogger` / `NewSlogHandler` take no name, so a Go line has a `logger` field only when the record (or its `With` attributes) carries a top-level `logger` attribute — written once, the last one winning. Pass `"logger", "<name>"` with `With` to get it on every Go line. The other fields — `time` (UTC, `Z`), `level`, `msg`, `exception` — are present everywhere; `exception` holds each language's native text (Go `Error()`, Python traceback, Node.js stack) ([design D1](specs/config-contract-v0.3/design.md), "Stdout fallback fields") |
| Database error text in `exception.message` | Failed DB spans keep status ERROR without a description in every language (A25; Python on both `instrument_sql()` and the default `SQL` path of `setup_telemetry()`, not when other code instrumented SQLAlchemy first, which is warned), but the semconv `exception` event is kept: Go writes the driver's error text there (it can quote a statement value, e.g. a duplicate key); Node.js pg writes a sanitized message (type and SQLSTATE), Node.js mysql2 no event, Python none on statement spans (the `connect` span's event carries the error text). Node.js failed SQL spans also carry `error.type=_OTHER` (its status wrapper sees the status, not the driver error; Go and Python set no `error.type` on SQL spans — Go's is on the duration metric only). Redis (A26): the status rule covers it in every language (a known reply code — `WRONGTYPE`, `ERR`, `NOAUTH`, ... from a fixed allowlist — becomes `db.response.status_code` and `error.type`, anything else `_OTHER`, so a Lua `error_reply`'s first word never reaches an attribute; Python on redis-py < 8 reads it as `ERR`), and the exception event carries the server text, which for an unknown command or a Lua `error_reply` can quote arguments (`ERR unknown command 'X', with args beginning with: '<arg>'`); a Python pipeline error's event keeps the command names only (`Command # 2 (LPUSH ? ?) of pipeline caused error: …`) |
| Redis `db.operation.name` and span names | Every language records `db.operation.name` on command spans as the client provides it (ioredis and Go `set`, node-redis and redis-py `SET`; none on Go and Python pipeline spans, Node.js `MULTI`/`PIPELINE` forms). Span names are the uppercased command everywhere (`SET`); a pipeline is one span in Go (`SET GET`, each name once) and Python (`SET GET`, every command), one span per command in Node.js; Go's dial span is `connect`, as Node.js's connection spans (A30; [design D1](specs/config-contract-v0.3/design.md), "Redis span names", "Redis `db.operation.name`"). A subcommand differs: Go `CLUSTER INFO`, Python `CLUSTER` |
| Node.js Redis metrics | None: `@opentelemetry/instrumentation-redis` / `-ioredis` record no metrics. Go `ext/otelredis` emits the redisotel pool metrics (`db.client.connections.*`); Python none |
| Node.js ioredis handshake spans | ioredis traces every connect and reconnect handshake (`connect`, `HELLO`, `CLIENT`, `INFO`; Cluster `CLUSTER SLOTS`) as root traces, ERROR when the server rejects the auth or is loading. Drop them at the Collector or wrap the client's lifetime in a span ([Node.js HOW-TO §9](nodejs/HOW-TO.md#redis-staffopsotel-helper-redis)) |
| Node.js AWS spans | No `server.address` / `server.port`: instrumentation-aws-sdk hands its hooks a request without the endpoint, so the helper cannot read it (upstream limitation; Go and Python write them, A30). The legacy `rpc.*` keys as Python (row above) |
| Node.js runtime metrics and exemplars | Node.js emits no runtime metrics (Go `go.*`, Python `system.*` / `process.*`) and its metric points carry no exemplars (`@opentelemetry/sdk-metrics` 3.0 exports no exemplar filter) |
| Go Redis pool metrics keep `db_system` | With `OTEL_SEMCONV_STABILITY_OPT_IN=database` (without `/dup`) Go `ext/otelredis` drops the legacy `db.system` from spans, but its pool metrics (`db_client_connections_*`) keep `db_system="redis"`: redisotel adds it to the metric attributes and Go has no meter wrapper ([design D1](specs/config-contract-v0.3/design.md), "Database semconv opt-in") |
| Go application error handler set after `Setup` | The helper's OTel error handler (drops OTLP/HTTP response bodies, masks the configured header values) wraps a handler installed **before** `Setup`; it receives a sanitized chain (`errors.Is` and `status.FromError` work; `errors.As` fails for every typed error of the chain, `*url.Error` included). `otel.SetErrorHandler` called **after** `Setup` replaces it (last writer wins), and that handler receives the raw SDK errors, body included. No sanitizer is exported (owner decision) ([Go HOW-TO](go/HOW-TO.md#sdk-errors-response-bodies-and-header-values), design D1 "SDK error text", A29) |
| Go SDK internal logger | The Go OTLP exporters parse `OTEL_EXPORTER_OTLP_*` again when they are built and report problems through the SDK's own logger (`otel.SetLogger`, stderr by default), outside the helper's masking: a malformed `OTEL_EXPORTER_OTLP_HEADERS` item or a value with a bad `%` escape is printed whole (`envconfig.go:166,181`), and so is an endpoint value it cannot parse (a schemeless value with a path). Keep header values well formed ([design D1](specs/config-contract-v0.3/design.md), "SDK's own log output of env values") |
| SDK error text redaction depth | Every language drops response bodies (Go by its handler; Python's exporters never log one; Node.js by `withoutResponseBodies`) and masks the configured OTLP header values. Node.js also applies pattern-based redaction (credential-named `key=value` pairs, `Cookie`, URL userinfo) to forwarded SDK text; Go and Python mask only the configured values, so a credential the helper was not given is printed as the SDK wrote it |
| .NET | Still on the 0.2.x contract and instrumentation set until Phase C ([differences](#net-02x-differences)); the Prometheus variables, the gRPC path warning and the SDK error text rule (A29) land in Phase C (C12, C7b, C8) |

## Collector and platform prerequisites

The libraries assume a Collector and platform configured for them. These items
live outside this repo and block the 0.3.0 rollout; the full checklist, with
the reason for each item, is
[design D11](specs/config-contract-v0.3/design.md#d11--collector--platform-prerequisites-reference-checklist):

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

Shared Grafana dashboards in [`dashboards/`](dashboards/) query the example
apps' business metrics, Collector `spanmetrics` and Tempo. They are language
agnostic.

## More

- [Changelog and migration guide](CHANGELOG.md)
- [Specs and decision records](specs/README.md)
- [Contributor and agent guide](AGENTS.md)
- [Documentation site](https://staffops.github.io/otel-libs/)

## License

[Apache License 2.0](LICENSE) — the same license as OpenTelemetry.
