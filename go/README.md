# OTel Helper — Go

OpenTelemetry helper for Go services. One `Setup` call configures traces,
metrics and logs: OTLP export to a Collector, W3C propagation, a ParentBased
sampler with health-check exclusion, Go runtime metrics, and a `log/slog`
handler correlated with traces.

- **[HOW-TO.md](HOW-TO.md)** — developer guide: HTTP, gRPC, workers, custom
  signals, configuration rules, lifecycle, troubleshooting
- **[TESTS.md](TESTS.md)** — running the unit, parity, ext and e2e tests
- **[example/](example/README.md)** — sample apps, including a custom-signals
  example verified against a real Collector
- **[Migration 0.2 → 0.3](#migration-from-020)** and the full
  [CHANGELOG](../CHANGELOG.md)

This page describes the **0.3.0** contract (unreleased, branch
`feat/v0.3-config-contract`), specified in
[`specs/config-contract-v0.3/`](https://github.com/StaffOps/otel-libs/blob/main/specs/config-contract-v0.3/design.md).

## Quick start

```go
func main() {
    // SIGTERM is what `docker stop` and Kubernetes send.
    ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
    defer stop()

    shutdown, err := otelhelper.Setup(ctx)
    if err != nil {
        log.Fatalf("otel setup: %v", err)
    }
    defer func() {
        // ctx is cancelled by the time this runs: flush under a fresh deadline.
        flushCtx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
        defer cancel()
        _ = shutdown(flushCtx)
    }()

    // ... run the service until ctx is done ...
}
```

Configure it with standard OTel variables (usually injected by the platform):

```bash
OTEL_SERVICE_NAME=orders-api
OTEL_RESOURCE_ATTRIBUTES=deployment.environment.name=PRD
OTEL_EXPORTER_OTLP_ENDPOINT=http://otel-collector:4317
```

## Installation

```bash
go get github.com/staffops/otel-libs/go
```

Requires Go **1.26** or newer (`go.mod`: `go 1.26`); CI runs 1.26.x and 1.27.x.
Older toolchains fail with `go: go.mod requires go >= 1.26.0`.

---

## Environment variables

Precedence, for every setting: **code (`With*` option) > standard `OTEL_*`
variable > deprecated / `OTEL_HELPER_*` variable > default**. A value that
cannot be parsed from the environment logs a warning (stderr) and falls back to
the next level; the same invalid value passed in code makes `Setup` return an
error naming the option. The resolution rules (endpoint, TLS, timeout,
compression, headers) are in [HOW-TO §9](HOW-TO.md#9-configuration-reference).

### Identity

| Variable | Default | Description |
|----------|---------|-------------|
| `OTEL_SERVICE_NAME` | _(unset)_ | `service.name`. Order: code > `OTEL_SERVICE_NAME` > `service.name` in `OTEL_RESOURCE_ATTRIBUTES` > `SERVICE_NAME` > SDK `unknown_service:<exe>` + warning. Whitespace-only = unset |
| `OTEL_RESOURCE_ATTRIBUTES` | _(unset)_ | Resource attributes, incl. `deployment.environment.name` and `service.instance.id`. A key set in code (`WithResourceAttributes`) wins; a helper default never overwrites a key set here. Parsed by the helper: values percent-decoded then trimmed; an item without `=`, with an empty key or a bad `%` escape is warned by position (never by value); a blank identity value (`service.name=`) counts as unset; `telemetry.sdk.*` are ignored (the SDK sets them). Limitation: the Go SDK providers re-read the variable, so a blank `deployment.environment.name=` comes back as `""` and a key with surrounding Unicode whitespace appears twice (the helper keeps the SDK from echoing malformed items) |
| `SERVICE_NAME` | _(unset)_ | **Deprecated** alias of `OTEL_SERVICE_NAME` (lowest priority); warns once per process when set |
| `ENVIRONMENT` | _(unset)_ | **Deprecated** alias of `deployment.environment.name` (`OTEL_RESOURCE_ATTRIBUTES` wins); warns once when set. Unset everywhere → attribute absent + one warning. Values other than `LOCAL`/`DEV`/`HML`/`PRD` are emitted verbatim |

The SDK resource also carries `service.instance.id`: a per-process UUID unless
code or `OTEL_RESOURCE_ATTRIBUTES` sets one
([design D4](https://github.com/StaffOps/otel-libs/blob/main/specs/config-contract-v0.3/design.md#d4--serviceinstanceid)).
Everything else (`k8s.*`, `service.version`, cloud attributes) is added by the
Collector (`k8sattributes`, `resourcedetection`).

### OTLP export

`<SIGNAL>` is `TRACES`, `METRICS` or `LOGS`. Every per-signal variable falls
back to the generic one, then to the default.

| Variable | Default | Description |
|----------|---------|-------------|
| `OTEL_EXPORTER_OTLP_ENDPOINT` | _(unset)_ | Generic Collector endpoint, `http(s)://host:port[/prefix]` (or deprecated-for-HTTP schemeless `host:port`). `/v1/<signal>` is appended for http/protobuf. **Unset = that signal is not exported** (see [No OTLP endpoint](#when-no-otlp-endpoint-is-configured)) |
| `OTEL_EXPORTER_OTLP_<SIGNAL>_ENDPOINT` | _(unset)_ | Per-signal endpoint, used verbatim; configures OTLP for that signal alone |
| `OTEL_EXPORTER_OTLP[_<SIGNAL>]_PROTOCOL` | `grpc` | `grpc` or `http/protobuf`. Unset: port `4318` on that signal's endpoint infers `http/protobuf`. `http/json` is warned and ignored |
| `OTEL_EXPORTER_OTLP[_<SIGNAL>]_INSECURE` | _(unset)_ | `true` = plaintext for **schemeless** endpoints only; with `http://`/`https://` the scheme decides and a contradicting value is warned and ignored |
| `OTEL_EXPORTER_OTLP[_<SIGNAL>]_CERTIFICATE` | _(unset)_ | CA bundle for TLS (read by the SDK) |
| `OTEL_EXPORTER_OTLP[_<SIGNAL>]_CLIENT_CERTIFICATE` / `_CLIENT_KEY` | _(unset)_ | mTLS client certificate and key (read by the SDK); ignored with a warning on a plaintext endpoint |
| `OTEL_EXPORTER_OTLP[_<SIGNAL>]_HEADERS` | _(unset)_ | `key=value,key2=value2`; values percent-decoded. Use it for auth (`Authorization=Basic%20<base64>`) |
| `OTEL_EXPORTER_OTLP[_<SIGNAL>]_TIMEOUT` | SDK `10000` | Export timeout in **milliseconds**, clamped to [1 ms, 24 h] |
| `OTEL_EXPORTER_OTLP[_<SIGNAL>]_COMPRESSION` | `gzip` | `gzip` or `none` (case-insensitive) |

### Signals, exporters and propagation

| Variable | Default | Description |
|----------|---------|-------------|
| `OTEL_TRACES_EXPORTER` / `OTEL_LOGS_EXPORTER` | `otlp` | Comma list of `otlp`, `console` (compact JSON on stdout), `none`. `logging` = deprecated `console` (warned); `none` in a list wins (warned) |
| `OTEL_METRICS_EXPORTER` | _(inferred)_ | `otlp`, `prometheus`, `console`, `none` or a list (`otlp,prometheus`). Unset: `otlp` when a metrics endpoint is configured, else `prometheus`. `otlp` without an endpoint is warned and dropped |
| `OTEL_METRIC_EXPORT_INTERVAL` | `30000` | Periodic export interval in ms (helper default; the SDK's is 60000) |
| `OTEL_METRICS_EXEMPLAR_FILTER` | SDK `trace_based` | Read by the SDK; the helper never sets it |
| `OTEL_TRACES_SAMPLER` / `OTEL_TRACES_SAMPLER_ARG` | `parentbased_always_on` | The six spec samplers (case-insensitive); unknown or unsupported names warn and use the default. `_ARG` is a plain decimal in [0, 1] |
| `OTEL_PROPAGATORS` | `tracecontext,baggage` | `tracecontext`, `baggage`, `none`. `b3`, `b3multi`, `jaeger`, `xray`, `ottrace` are warned and skipped |
| `OTEL_SDK_DISABLED` | `false` | `true` installs no-op providers (no exporters, no `/metrics` listener); propagation still works |
| `OTEL_SEMCONV_STABILITY_OPT_IN` | _(unset)_ | Read by contrib instrumentations: `rpc/dup` or `rpc/old` (otelgrpc metrics), `database` / `database/dup` (`ext/otelsql`) |

### Helper variables

| Variable | Default | Description |
|----------|---------|-------------|
| `OTEL_HELPER_DEBUG_LEVEL` | `false` | `true`: DEBUG log level **and** `debug=true` on entry spans ([Debug mode](HOW-TO.md#6-debug-mode)) |
| `OTEL_HELPER_METRICS_PORT` | `9464` | Port of the standalone Prometheus `/metrics` listener; `0` = no listener (mount `MetricsHandler()` yourself) |
| `OTEL_HELPER_DISABLED_METRICS` | _(unset)_ | Comma list of `path.Match` patterns (`http.server.*`); matching instruments are dropped from every exporter |
| `OTEL_HELPER_SAMPLE_RATIO` | _(unset)_ | **Deprecated** (warns once): ratio in [0, 1] → `ParentBased(TraceIDRatioBased(x))`, only while `OTEL_TRACES_SAMPLER` is unset. Use `OTEL_TRACES_SAMPLER=parentbased_traceidratio` + `_ARG` |
| `OTEL_HELPER_DISABLED_SIGNALS` | _(unset)_ | **Deprecated** (warns once): `traces`, `metrics`, `logs`. Use `OTEL_<SIGNAL>_EXPORTER=none` |
| `OTEL_HELPER_EXTRA_INSTRUMENTATION` | _(unset)_ | **Removed** in Go 0.3.0: no effect, warns once when set. Import an [`ext/*` module](#opt-in-extensions-ext) instead |

Deprecated variables keep working until 1.0 and warn even when the standard
variable wins, so they can be removed from manifests early.

> **`OTEL_HELPER_DEBUG_LEVEL=true` in production raises volume and cost.**
> Use it for targeted troubleshooting only.

---

## Options

Options passed to `Setup` win over every environment variable.

| Option | Effect |
|--------|--------|
| `WithServiceName(name)` | `service.name`; empty fails `Setup` |
| `WithEnvironment(env)` | `deployment.environment.name` (`LOCAL`, `DEV`, `HML`, `PRD` or any value, emitted verbatim); empty fails `Setup` |
| `WithResourceAttributes(map[string]string)` | Extra resource attributes; each key wins over `OTEL_RESOURCE_ATTRIBUTES` |
| `WithEndpoint(endpoint)` | Generic endpoint: URL (`http://c:4318/otlp`) or `host:port`; `/v1/<signal>` appended for http/protobuf |
| `WithTracesEndpoint` / `WithMetricsEndpoint` / `WithLogsEndpoint` | Per-signal endpoint URL, path used verbatim |
| `WithOtlpProtocol(p)` | `ProtocolGRPC` (`grpc`) or `ProtocolHTTP` (`http/protobuf`) for every signal; anything else fails `Setup` |
| `WithInsecure(bool)` | Plaintext/TLS for **schemeless** endpoints; contradicting a code URL's scheme fails `Setup` |
| `WithCompression(c)` | `CompressionGzip` (default) or `CompressionNone`, every signal |
| `WithExportTimeout(ms)` | Export timeout for every signal (> 0) |
| `WithMetricExporters(names...)` | `otlp`, `prometheus`, `console`, `none`; `otlp` without an endpoint fails `Setup` |
| `WithExportInterval(ms)` | Metric export interval (> 0) |
| `WithPrometheusMetricsPort(port)` | Standalone `/metrics` port in [0, 65535] |
| `WithoutMetricsListener()` | No standalone listener; mount `MetricsHandler()` on your mux |
| `WithDisabledMetrics(patterns)` | Drop instruments matching `path.Match` patterns; `nil` = use env, `[]string{}` = none (beats env) |
| `WithDisabledSignals(signals)` | `traces`, `metrics`, `logs` (other names fail `Setup`); `nil` = use env, `[]string{}` = none |
| `WithSampleRatio(ratio)` | Root-span ratio in [0, 1] → `ParentBased(TraceIDRatioBased)`, any value (1.0 included) beats `OTEL_TRACES_SAMPLER`; NaN / out of range fails `Setup` |
| `WithHealthPaths(paths...)` | Replace the [health path list](#health-checks-spans-dropped-metrics-kept); `WithHealthPaths()` = none. Paths must be ASCII, start with `/`, no `?` `#` `,` |
| `WithDebug()` | Debug mode (see `OTEL_HELPER_DEBUG_LEVEL`) |

**Removed in 0.3.0:** `WithExtraInstrumentation`, `Options.HasInstrumentation`
and the `Options.ExtraInstrumentation` field (they never changed what Go
instruments), and the `UnaryServerInterceptor` / `StreamServerInterceptor` /
`UnaryClientInterceptor` / `StreamClientInterceptor` functions (otelgrpc
dropped interceptors; use the stats handlers).

---

## API reference

### `Setup(ctx, ...Option) (Shutdown, error)`

Configures traces, metrics and logs. Every signal is built before anything is
installed: on error, what was built is released, the global providers are left
untouched and `Setup` can be retried (the returned `Shutdown` is a no-op).
While a pipeline is installed, later calls return its `Shutdown`. A `Setup`
called while a `Shutdown` is in flight waits for it, then builds a fresh
pipeline (or returns `ctx.Err()` if its own context ends first).

`Shutdown(ctx)` flushes logs, metrics and traces **concurrently** under `ctx`
(errors joined), is idempotent (later or concurrent calls return the first
result) and resets the helper, so a following `Setup` builds a new pipeline.
Tracers, meters and instrumented handlers obtained *before* that new `Setup`
stay bound to the shut-down providers — obtain them after it. The helper
installs **no signal handlers**: see [Lifecycle](HOW-TO.md#8-lifecycle-and-graceful-shutdown).

### `GetTracer(name ...string) trace.Tracer` / `GetMeter(name ...string) metric.Meter`

Tracer / Meter from the global provider; the name (instrumentation scope)
defaults to `otel-helper`.

### `StartRootSpan(ctx, tracer, name, ...SpanStartOption) (context.Context, trace.Span)`

Starts a span with no parent (a new trace) even if `ctx` carries one — one
trace per worker iteration or consumed message.

### `NewHTTPHandler(handler http.Handler, operation string) http.Handler`

otelhttp server instrumentation: a server span per request and
`http.server.request.duration` (seconds, stable semconv). Span names are
`<METHOD> <route>` from the `http.ServeMux` pattern (`GET /orders/{id}`, with
`http.route`), or `<METHOD>` without a pattern. The `operation` argument is
ignored since 0.3.0 (kept for source compatibility). Health checks are
measured; their spans are dropped by the sampler.

### `NewHTTPTransport(base http.RoundTripper) http.RoundTripper`

otelhttp client instrumentation (`nil` wraps `http.DefaultTransport`): client
spans named `HTTP <METHOD>`, `http.client.request.duration`, and `traceparent`
injection. Requests to a health path get no client span and no client metric.

### gRPC (stats handlers)

| Function | Description |
|----------|-------------|
| `GRPCServerOption()` | `grpc.ServerOption` installing the server stats handler (unary + streaming) |
| `GRPCDialOption()` | `grpc.DialOption` installing the client stats handler |
| `GRPCServerHandler()` / `GRPCClientHandler()` | The `stats.Handler`s themselves |

```go
srv := grpc.NewServer(otelhelper.GRPCServerOption())
conn, err := grpc.NewClient(addr, grpc.WithTransportCredentials(insecure.NewCredentials()),
    otelhelper.GRPCDialOption())
```

Metrics: `rpc.server.call.duration` / `rpc.client.call.duration` (seconds,
`rpc.system.name`, `rpc.method="pkg.Svc/Method"`). Server-side
`grpc.health.v1.Health/*` calls are counted, their spans dropped; client-side
health calls get neither span nor metric.

### Logging: `NewSlogHandler()`, `NewLogger(env, debug)`, `DefaultLogLevel(env, debug)`

`NewSlogHandler()` returns one `slog.Handler` that decides **per record**:

| State | Destination |
|-------|-------------|
| `Setup` installed a logs exporter (OTLP and/or `OTEL_LOGS_EXPORTER=console`) | OTel bridge (`otelslog`) → that pipeline's `LoggerProvider` |
| Before `Setup`, no logs endpoint, `OTEL_LOGS_EXPORTER=none`, after `Shutdown` | stdout, one JSON line per record |

A handler or logger created before `Setup` switches when `Setup` runs, and
back to stdout after `Shutdown`. Stdout shape (same as Python):

```json
{"time":"2026-10-28T18:41:25.734Z","level":"ERROR","msg":"order failed","order.id":"ord-3","exception":"card declined by issuer"}
```

`time` is RFC 3339 UTC with milliseconds; `level` is `DEBUG`/`INFO`/`WARN`/`ERROR`;
the first error-valued attribute of the record becomes `exception` (its
`Error()` text). Stdout records carry no `trace_id`/`span_id`.

**Trace correlation** needs the `*Context` calls: `logger.InfoContext(ctx, …)`
attaches the active span's `trace_id`/`span_id` to the exported record;
`logger.Info(…)` does not.

`NewSlogHandler` applies the level the last `Setup` resolved (INFO before it)
on both paths, stdout and the OTel bridge, as Python does;
`NewLogger(env, debug)` wraps the same handler with the level of `env`:

| `DefaultLogLevel(env, debug)` | Level |
|-------------|-------|
| `LOCAL` | Debug |
| `DEV`, `HML` | Info |
| `PRD` | Warn |
| unset / any other value | Info |
| `debug=true` | Debug |

### `MetricsHandler() http.Handler`

Prometheus exposition of the helper's dedicated registry (not the
`client_golang` global one). Status:

| State | Status |
|-------|--------|
| Before `Setup`, after a failed `Setup`, after `Shutdown` | `503` |
| Prometheus deliberately off (`OTEL_SDK_DISABLED=true`, or `OTEL_METRICS_EXPORTER` without `prometheus`) | `200`, empty body |
| Prometheus reader active | `200`, metrics |

### Other exports

`DefaultHealthPaths()`, the `DeploymentEnvironment` constants (`LOCAL`, `DEV`,
`HML`, `PRD`), `ProtocolGRPC` / `ProtocolHTTP`, `CompressionGzip` /
`CompressionNone`, and the `Env*` constants naming every variable above.

---

## What is configured automatically

| Area | Behavior |
|------|----------|
| Resource | `service.name`, `deployment.environment.name` (when resolved), `service.instance.id`, `telemetry.sdk.*`; schema URL `https://opentelemetry.io/schemas/1.43.0` |
| Propagation | W3C TraceContext + Baggage (`OTEL_PROPAGATORS`) |
| Sampler | `parentbased_always_on` by default ([ADR-002](https://github.com/StaffOps/otel-libs/blob/main/specs/decisions/ADR-002-parentbased-sampler.md)) wrapped by the health-check drop; tail sampling happens at the Collector |
| Traces | Batch span processor → OTLP |
| Metrics | Go runtime metrics (`go.memory.used`, `go.memory.limit` (only when `GOMEMLIMIT` is set), `go.memory.allocated`, `go.memory.allocations`, `go.memory.gc.goal`, `go.goroutine.count`, `go.processor.limit`, `go.config.gogc`); exemplars (SDK `trace_based`); export every 30 s |
| Logs | Batch log processor → OTLP, via the slog handler above |
| Diagnostics | Helper warnings go to a stderr `slog` text logger (never `slog.Default()`), and never print header values, client-key paths or URL userinfo/query |

`OTEL_GO_X_DEPRECATED_RUNTIME_METRICS=true` also emits the 0.2.0
`process.runtime.go.*` / `runtime.uptime` set.

### When no OTLP endpoint is configured

"OTLP configured" is decided **per signal**: a code endpoint, the generic env
endpoint, or that signal's env endpoint.

| Signal without an endpoint | Behavior |
|--------|----------|
| Traces | Not exported; spans are still created and context propagates |
| Metrics | Prometheus `/metrics` on `:9464` (`OTEL_HELPER_METRICS_PORT`) |
| Logs | stdout JSON (the shape above) |

So `OTEL_EXPORTER_OTLP_TRACES_ENDPOINT` alone exports traces, serves metrics on
`/metrics` and writes logs to stdout. The `/metrics` fallback is the one
exception to "everything via the Collector"
([ADR-007](https://github.com/StaffOps/otel-libs/blob/main/specs/decisions/ADR-007-keep-prometheus-in-core.md)).

### Health checks: spans dropped, metrics kept

The sampler drops the **server** span of a health check that has no local
parent (none, or a remote `traceparent`); spans the handler starts follow it
under the default ParentBased sampler. Health requests stay in
`http.server.request.duration` / `rpc.server.call.duration`, so failing probes
remain visible — exclude them in SLI queries
([design D5](https://github.com/StaffOps/otel-libs/blob/main/specs/config-contract-v0.3/design.md#d5--health-check-exclusion)).

- HTTP: `/health /healthz /ready /readyz /live /livez /ping /metrics` — exact,
  case-insensitive, trailing slash and query ignored (`/users/ping`,
  `/healthcare`, `/health/live` stay traced). Matched on `url.path`
  (`http.target` fallback). `WithHealthPaths(...)` replaces the list for
  server and client.
- gRPC: the whole `grpc.health.v1.Health/` service (Check, Watch, List), always.
- Clients (`NewHTTPTransport`, `GRPCDialOption`): health calls get no span
  **and no metric** (upstream filter behavior).

---

## Opt-in extensions (`ext/`)

Separate Go modules, so core stays free of AWS/Redis/SQL dependencies:

```bash
go get github.com/staffops/otel-libs/go/ext/otelaws
go get github.com/staffops/otel-libs/go/ext/otelredis
go get github.com/staffops/otel-libs/go/ext/otelsql
```

```go
// AWS SDK v2: middleware on the aws.Config (semconv v1.43, rpc.system.name)
otelaws.Instrument(&cfg)

// go-redis v9: tracing + pool metrics; call once per client
if err := otelredis.Instrument(rdb); err != nil { ... } // tracing is already installed on a metrics error

// database/sql: tracing + db.sql.* pool metrics
db, closeDB, err := otelsql.Open("pgx", dsn)
if err != nil { ... }
defer closeDB() // instead of db.Close(): unregisters the pool-metrics callback, then closes
```

| Module | What it instruments |
|--------|---------------------|
| `ext/otelaws` | AWS SDK v2 calls (S3, SQS, DynamoDB, …) |
| `ext/otelredis` | go-redis commands: spans with `db.system.name=redis`, no command values (`db.statement`) and no connection string; `db.client.connections.*` pool metrics (Prometheus `db_client_connections_usage`, `_max`, `_idle_max`, `_idle_min`, `_waits_total`, `_timeouts_total`, `_hits_total`, `_misses_total`, …) |
| `ext/otelsql` | `database/sql` queries and `db.sql.*` pool metrics. `db.system.name` from the driver: `pgx`, `pgx/v5`, `postgres` → `postgresql`; `sqlite`, `sqlite3` → `sqlite`; `mysql`; `sqlserver`, `mssql` → `microsoft.sql_server`; else `other_sql`. The legacy `db.system` key is emitted too until 1.0, unless `OTEL_SEMCONV_STABILITY_OPT_IN` contains `database` without `database/dup`. A pool-metrics registration failure is returned by `Open` (the DB is closed) |

([design D14](https://github.com/StaffOps/otel-libs/blob/main/specs/config-contract-v0.3/design.md#d14--go-ext-modules-api-a10-validation))

---

## Migration from 0.2.0

The full list, with every telemetry-output change for dashboards and alerts, is
in the [CHANGELOG](../CHANGELOG.md) (Unreleased → Go and the "telemetry output
changes" table). The changes most likely to need action:

| 0.2.0 | 0.3.0 — what to do |
|-------|--------------------|
| `SERVICE_NAME`, `ENVIRONMENT` | Still work, warn. Set `OTEL_SERVICE_NAME` and `OTEL_RESOURCE_ATTRIBUTES=deployment.environment.name=<env>` |
| Default `service.name` `my-service`, environment `LOCAL` | Unset name → `unknown_service:<exe>` + warning; unset environment → attribute **absent** (log level INFO, not DEBUG). The Collector needs a tail-sampling branch for spans without it |
| `OTEL_EXPORTER_OTLP_INSECURE=true` with `https://` gave plaintext | The scheme wins: use `http://` for plaintext |
| `https://host` (no port) under http/protobuf went to 4317 | Goes to 443; set the port explicitly |
| `/v1/<signal>` added by hand to work around a dropped path prefix | Remove it: prefixes are kept (`…/otlp` → `/otlp/v1/<signal>`) |
| Credentials in the endpoint URL (`https://user:pw@host`) | Stripped with a warning: use `OTEL_EXPORTER_OTLP_HEADERS=Authorization=Basic%20<base64>` |
| Header keys like `x+y` | Skipped with a warning (keys must match `[A-Za-z0-9._-]+`); keys compared case-insensitively |
| `OTEL_EXPORTER_OTLP_TIMEOUT` ignored (10 s fixed) | Honored, in milliseconds |
| `WithExtraInstrumentation`, `HasInstrumentation`, `OTEL_HELPER_EXTRA_INSTRUMENTATION` | Removed / no effect: import `ext/otelsql`, `ext/otelredis`, `ext/otelaws` |
| `otelsql.Open` returned `(db, err)` | Returns `(db, closeDB, err)`: `defer closeDB()` |
| `*Interceptor()` gRPC functions | `grpc.NewServer(otelhelper.GRPCServerOption())`, `grpc.NewClient(target, otelhelper.GRPCDialOption())` |
| `http.server.duration` (ms), `rpc.server.duration` (ms), `process.runtime.go.*` | `http.server.request.duration` (s), `rpc.server.call.duration` (s), `go.*` runtime metrics — update queries |
| Server span names from the `operation` argument | `<METHOD> <route>`: filter on `http.route` |
| Health requests had no metric | Counted in `http.server.request.duration`: exclude probe routes in SLIs |
| `MetricsHandler()` 503 when Prometheus was off | `200` with an empty body |
| `NewSlogHandler()` without an endpoint dropped records | Writes stdout JSON |
| Go 1.22 | Go 1.26 |

---

## Architecture

```
[ Go app: otelhelper.Setup ]
      │ OTLP (gRPC :4317 or http/protobuf :4318), gzip, TLS by scheme
      ▼
[ OTel Collector: k8sattributes, tail sampling ]
      ▼
 traces → Tempo · metrics → VictoriaMetrics/Prometheus · logs → Loki
```

- The SDK samples with `parentbased_always_on` (health spans dropped); what to
  keep is decided by tail sampling at the Collector gateway.
- The SDK sets only `service.name`, `deployment.environment.name` and
  `service.instance.id`; the Collector enriches the rest.
- Everything goes through the Collector, never directly to a backend — except
  the Prometheus `/metrics` fallback.

## Tests

See [TESTS.md](TESTS.md). Quick run (unit + parity, from the repo root):

```bash
docker compose -f testing/docker-compose.yml run --rm go-tests
```

## Project structure

```
go/
├── otelhelper.go      # Setup / Shutdown lifecycle, GetTracer, GetMeter, resource
├── options.go         # Options + With* functional options
├── config.go          # Env resolution, validation, deprecations, propagators
├── endpoint.go        # OTLP endpoint / TLS / timeout / compression / headers resolution
├── tracing.go         # TracerProvider, sampler, StartRootSpan
├── health.go          # Health-check sampler and path matching
├── processors.go      # debug=true marker on entry spans
├── metrics.go         # MeterProvider, runtime metrics, Prometheus reader, MetricsHandler
├── logging.go         # LoggerProvider, per-record slog handler, NewLogger, DefaultLogLevel
├── console.go         # console (stdout) exporters
├── middleware.go      # NewHTTPHandler, NewHTTPTransport, gRPC stats handlers
├── diag.go            # stderr diagnostics logger, redaction, warn-once
├── instrumentation.go # pointer to the ext/ modules
├── doc.go             # package documentation
├── *_test.go          # unit + parity tests (TESTS.md)
├── ext/               # otelaws, otelredis, otelsql (separate modules)
└── example/           # go-api, go-backend, go-process, go-custom-signals
```
